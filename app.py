from __future__ import annotations
import json
import mimetypes
import os
import tempfile
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from db import connect, debt_rows, get_setting, import_workbook, init_db

BASE = os.path.dirname(os.path.abspath(__file__))


def rows_dict(rows):
    return [dict(r) for r in rows]


def api_summary():
    with connect() as con:
        debts = debt_rows(con)
        positive = [d for d in debts if d["amount"] >= 0]
        total = sum(d["amount"] for d in debts)
        paid = sum(min(max(d["paid"], 0), max(d["amount"], 0)) for d in positive)
        pending = sum(d["pending"] for d in debts)
        overdue = sum(d["pending"] for d in positive if "VENCIDO" in d["status"])
        due_soon = sum(d["pending"] for d in positive if "POR VENCER" in d["status"])
        erosion = sum((d["erosion_usd"] or 0) for d in positive)
        unmatched_deposits = con.execute("""
          SELECT COUNT(*) n FROM (
            SELECT d.id, d.amount-COALESCE(SUM(ABS(m.applied_amount)),0) rem
            FROM deposits d LEFT JOIN matches m ON m.deposit_id=d.id
            WHERE d.active=1 AND d.effective=1 AND d.amount>0 GROUP BY d.id HAVING rem>0.01
          )""").fetchone()["n"]
        unmatched_debts = sum(1 for d in positive if d["pending"] > 0.01)
        return {
            "kpis": {"total_debt": round(total, 2), "paid": round(paid, 2), "pending": round(pending, 2),
                     "overdue": round(overdue, 2), "due_soon": round(due_soon, 2), "erosion_usd": round(erosion, 2),
                     "unmatched_deposits": unmatched_deposits, "unmatched_debts": unmatched_debts,
                     "current_tc": float(get_setting(con, "current_tc", "11.77"))},
            "debts": debts,
        }


def api_unmatched():
    with connect() as con:
        debts = [d for d in debt_rows(con) if d["amount"] > 0 and d["pending"] > 0.01]
        deps = con.execute("""
          SELECT d.id,d.posted_at,d.branch,d.description,d.reference,d.transaction_code,d.amount,d.movement_kind,d.movement_status,
                 ROUND(d.amount-COALESCE(SUM(ABS(m.applied_amount)),0),2) AS remaining
          FROM deposits d LEFT JOIN matches m ON m.deposit_id=d.id
          WHERE d.active=1 AND d.effective=1 AND d.amount>0
          GROUP BY d.id HAVING remaining>0.01
          ORDER BY d.posted_at
        """)
        return {"debts": debts, "deposits": rows_dict(deps)}


def api_matches():
    with connect() as con:
        rows = con.execute("""
          SELECT m.id,m.source,m.status,m.applied_amount,m.payment_date,m.reference,m.transaction_code,m.note,m.created_at,
                 d.source_date AS debt_date,d.concept,d.company,d.amount AS debt_amount,
                 p.posted_at,p.branch,p.description,p.amount AS deposit_amount,p.id AS deposit_id,d.id AS debt_id
          FROM matches m JOIN debts d ON d.id=m.debt_id
          LEFT JOIN deposits p ON p.id=m.deposit_id
          WHERE d.active=1
          ORDER BY COALESCE(m.payment_date,m.created_at) DESC,m.id DESC
        """)
        return rows_dict(rows)


def api_raw_dte():
    with connect() as con:
        return rows_dict(con.execute("SELECT id,source_date AS date,concept,company,amount FROM debts WHERE active=1 AND UPPER(concept)='DTE' ORDER BY source_date,company"))


def api_raw_reliquidacion():
    with connect() as con:
        return rows_dict(con.execute("SELECT id,source_date AS date,concept,company,amount FROM debts WHERE active=1 AND UPPER(concept) LIKE 'RELIQ%' ORDER BY source_date,company"))


def api_raw_extracto():
    with connect() as con:
        return rows_dict(con.execute("""SELECT id,posted_at,branch,description,reference,transaction_code,amount,source_order,movement_kind,movement_status,effective,linked_movement_id,
                                      CASE WHEN amount<0 THEN ABS(amount) ELSE 0 END AS debit,
                                      CASE WHEN amount>0 THEN amount ELSE 0 END AS credit
                               FROM deposits WHERE active=1 ORDER BY COALESCE(source_order,999999),posted_at,id"""))


def api_companies():
    with connect() as con:
        return rows_dict(con.execute("SELECT name,abbr FROM companies ORDER BY name"))


def api_settings_get():
    with connect() as con:
        return {"current_tc": float(get_setting(con, "current_tc", "11.77")),
                "payment_days": int(float(get_setting(con, "payment_days", "30")))}


def api_settings_post(data):
    with connect() as con:
        for key in ("current_tc", "payment_days"):
            if key in data and data[key] not in (None, ""):
                value = str(float(data[key])) if key == "current_tc" else str(int(float(data[key])))
                con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        con.commit()
    return api_settings_get()


def api_imports():
    with connect() as con:
        return rows_dict(con.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 15"))


def api_manual_match(data):
    """Guarda uno o varios documentos de deuda contra un mismo depósito.

    Payload nuevo:
      {"deposit_id": "...", "allocations": [{"debt_id":"...", "amount": 100.0}, ...], "note":"..."}

    Se mantiene compatibilidad con el payload anterior debt_id + amount.
    """
    deposit_id = data.get("deposit_id")
    note = str(data.get("note") or "").strip()
    allocations = data.get("allocations")
    if not allocations:
        allocations = [{"debt_id": data.get("debt_id"), "amount": data.get("amount")}]
    if not deposit_id or not isinstance(allocations, list) or not allocations:
        raise ValueError("Debe seleccionar un depósito y al menos una deuda.")

    cleaned = []
    seen = set()
    for item in allocations:
        debt_id = item.get("debt_id") if isinstance(item, dict) else None
        try:
            amount = round(float(item.get("amount") or 0), 2) if isinstance(item, dict) else 0
        except (TypeError, ValueError):
            amount = 0
        if not debt_id or amount <= 0:
            raise ValueError("Cada DTE/Reliquidación seleccionada debe tener un monto mayor a 0.")
        if debt_id in seen:
            raise ValueError("Un mismo documento no puede repetirse en el mismo emparejamiento.")
        seen.add(debt_id)
        cleaned.append((debt_id, amount))

    with connect() as con:
        dep = con.execute("SELECT * FROM deposits WHERE id=? AND active=1 AND effective=1 AND amount>0", (deposit_id,)).fetchone()
        if not dep:
            raise ValueError("El depósito ya no está disponible (puede estar reversado o ya no estar activo).")
        dep_used = con.execute("SELECT COALESCE(SUM(ABS(applied_amount)),0) x FROM matches WHERE deposit_id=?", (deposit_id,)).fetchone()["x"]
        dep_remaining = round(float(dep["amount"]) - float(dep_used or 0), 2)
        total = round(sum(amount for _, amount in cleaned), 2)
        if total > dep_remaining + 0.01:
            raise ValueError(f"La suma a aplicar (Bs {total:.2f}) supera el depósito disponible (Bs {dep_remaining:.2f}).")

        debts = {}
        for debt_id, amount in cleaned:
            debt = con.execute("SELECT * FROM debts WHERE id=? AND active=1", (debt_id,)).fetchone()
            if not debt:
                raise ValueError("Uno de los DTE/Reliquidaciones seleccionados ya no está activo.")
            debt_used = con.execute("SELECT COALESCE(SUM(applied_amount),0) x FROM matches WHERE debt_id=?", (debt_id,)).fetchone()["x"]
            debt_remaining = round(float(debt["amount"]) - float(debt_used or 0), 2)
            if amount > debt_remaining + 0.01:
                raise ValueError(f"Monto demasiado alto para {debt['concept']} - {debt['company']}. Pendiente Bs {debt_remaining:.2f}.")
            debts[debt_id] = debt

        for debt_id, amount in cleaned:
            debt = debts[debt_id]
            batch_note = note
            if len(cleaned) > 1:
                suffix = f"Aplicación múltiple: {len(cleaned)} documentos desde un depósito"
                batch_note = f"{note} · {suffix}" if note else suffix
            con.execute("""INSERT INTO matches(debt_id,deposit_id,applied_amount,source,status,payment_date,reference,transaction_code,note)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (debt_id, deposit_id, amount, "MANUAL", "EMPAREJADO MANUAL", dep["posted_at"], dep["reference"], dep["transaction_code"], batch_note))
        con.commit()
    return {"ok": True, "allocations": len(cleaned), "total": total}


def api_delete_manual(match_id):
    with connect() as con:
        row = con.execute("SELECT source FROM matches WHERE id=?", (match_id,)).fetchone()
        if not row:
            raise ValueError("Emparejamiento no encontrado")
        if row["source"] != "MANUAL":
            raise ValueError("Solo se pueden deshacer emparejamientos MANUAL desde la web.")
        con.execute("DELETE FROM matches WHERE id=?", (match_id,))
        con.commit()
    return {"ok": True}


def parse_multipart(headers, body):
    content_type = headers.get("Content-Type", "")
    raw = (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode() + body
    msg = BytesParser(policy=default).parsebytes(raw)
    fields = {}
    files = {}
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[name] = (filename, payload)
        elif name:
            fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    server_version = "CuentasWeb/1.0"

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_bytes(self, data: bytes, content_type="application/octet-stream", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8", status)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b"{}"
        return json.loads(body.decode("utf-8") or "{}")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/":
                return self.serve_file(os.path.join(BASE, "templates", "index.html"), "text/html; charset=utf-8")
            if path.startswith("/static/"):
                safe = os.path.normpath(path[len('/static/'):]).lstrip(os.sep)
                file_path = os.path.join(BASE, "static", safe)
                if not os.path.abspath(file_path).startswith(os.path.abspath(os.path.join(BASE, "static"))):
                    return self.send_json({"error": "Ruta inválida"}, 400)
                return self.serve_file(file_path)
            routes = {
                "/api/summary": api_summary,
                "/api/unmatched": api_unmatched,
                "/api/matches": api_matches,
                "/api/raw/dte": api_raw_dte,
                "/api/raw/reliquidacion": api_raw_reliquidacion,
                "/api/raw/extracto": api_raw_extracto,
                "/api/companies": api_companies,
                "/api/settings": api_settings_get,
                "/api/imports": api_imports,
            }
            if path in routes:
                return self.send_json(routes[path]())
            return self.send_json({"error": "No encontrado"}, 404)
        except Exception as e:
            return self.send_json({"error": str(e)}, 500)

    def serve_file(self, path, content_type=None):
        if not os.path.isfile(path):
            return self.send_json({"error": "Archivo no encontrado"}, 404)
        with open(path, "rb") as f:
            data = f.read()
        ctype = content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        return self.send_bytes(data, ctype)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/manual-match":
                return self.send_json(api_manual_match(self.read_json()))
            if path == "/api/settings":
                return self.send_json(api_settings_post(self.read_json()))
            if path == "/api/import":
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length > 30 * 1024 * 1024:
                    return self.send_json({"error": "El archivo supera 30 MB"}, 413)
                body = self.rfile.read(length)
                _, files = parse_multipart(self.headers, body)
                if "file" not in files:
                    return self.send_json({"error": "Seleccione un archivo .xlsm o .xlsx"}, 400)
                filename, payload = files["file"]
                ext = os.path.splitext(filename)[1].lower()
                if ext not in (".xlsm", ".xlsx"):
                    return self.send_json({"error": "Formato no permitido. Use .xlsm o .xlsx"}, 400)
                fd, tmp = tempfile.mkstemp(suffix=ext)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(payload)
                    result = import_workbook(tmp, filename)
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                return self.send_json({"ok": True, **result, "message": "Datos importados. Los emparejamientos manuales existentes se conservaron."})
            return self.send_json({"error": "No encontrado"}, 404)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:
            return self.send_json({"error": str(e)}, 500)

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            prefix = "/api/manual-match/"
            if path.startswith(prefix):
                match_id = int(path[len(prefix):])
                return self.send_json(api_delete_manual(match_id))
            return self.send_json({"error": "No encontrado"}, 404)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:
            return self.send_json({"error": str(e)}, 500)


if __name__ == "__main__":
    init_db()

    host = "127.0.0.1"
    port = 5000

    print("")
    print("==========================================")
    print("       CUENTAS POR COBRAR")
    print("==========================================")
    print("")
    print(f"Abre: http://127.0.0.1:{port}")
    print("Cierre con Ctrl+C.")
    print("")

    ThreadingHTTPServer((host, port), Handler).serve_forever()