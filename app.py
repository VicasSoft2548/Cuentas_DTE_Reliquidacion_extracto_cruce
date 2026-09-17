from __future__ import annotations
import json
import mimetypes
import os
import tempfile
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from datetime import date, datetime

from db import connect, debt_rows, company_balance_rows, get_setting, import_workbook, init_db

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
        company_balances = (company_balance_rows(con))
        portfolio_balance = round( sum(r["balance"] for r in company_balances), 2)
        return {

            "as_of": date.today().isoformat(),
            "kpis": {
                "total_debt": round( total,2),
                "paid": round( paid, 2),
                "pending": round( pending, 2),
                "portfolio_balance": portfolio_balance,
                "overdue": round( overdue, 2),
                "due_soon": round( due_soon, 2),
                "erosion_usd": round( erosion, 2),
                "unmatched_deposits": unmatched_deposits,
                "unmatched_debts": unmatched_debts,
                "current_tc": float(get_setting( con, "current_tc", "11.77")),
            },

            "debts": debts,
            "company_balances":
                company_balances,
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

def api_opening_balance_post(data):
    posted_at = str(data.get("posted_at") or "").strip()
    side = str(data.get("side") or "").strip().upper()
    note = str(data.get("note") or "").strip()

    try:
        amount = round(abs(float(data.get("amount", 0))), 2)
    except (TypeError, ValueError):
        raise ValueError("Monto inválido.")

    if not posted_at:
        raise ValueError("Debe indicar la fecha del balance de apertura.")

    if side not in ("DEBE", "HABER"):
        raise ValueError("Seleccione DEBE o HABER.")

    if amount <= 0:
        raise ValueError("El monto debe ser mayor a cero.")

    with connect() as con:
        cur = con.execute("""
            INSERT INTO opening_balances(
                posted_at,
                side,
                amount,
                note
            )
            VALUES (?, ?, ?, ?)
        """, (
            posted_at,
            side,
            amount,
            note
        ))

        balance_id = cur.lastrowid
        con.commit()

    return {
        "ok": True,
        "id": balance_id,
        "message": "Balance de apertura registrado."
    }


def api_delete_opening_balance(balance_id):
    with connect() as con:
        row = con.execute("""
            SELECT id
            FROM opening_balances
            WHERE id=?
        """, (balance_id,)).fetchone()

        if not row:
            raise ValueError("Balance de apertura no encontrado.")

        con.execute("""
            DELETE FROM opening_balances
            WHERE id=?
        """, (balance_id,))

        con.commit()

    return {
        "ok": True,
        "message": "Balance de apertura eliminado."
    }

def api_raw_extracto():
    with connect() as con:

        deposits = rows_dict(con.execute("""
            SELECT
                id,
                posted_at,
                branch,
                description,
                reference,
                transaction_code,
                amount,
                source_order,
                movement_kind,
                movement_status,
                effective,
                linked_movement_id,

                CASE
                    WHEN amount < 0 THEN ABS(amount)
                    ELSE 0
                END AS debit,

                CASE
                    WHEN amount > 0 THEN amount
                    ELSE 0
                END AS credit,

                'BANCO' AS source,
                NULL AS opening_balance_id

            FROM deposits

            WHERE active=1
        """))

        openings = rows_dict(con.execute("""
            SELECT
                'OPENING-' || id AS id,
                posted_at,
                'MANUAL' AS branch,

                CASE
                    WHEN note IS NOT NULL AND TRIM(note) <> ''
                    THEN 'BALANCE DE APERTURA - ' || note
                    ELSE 'BALANCE DE APERTURA'
                END AS description,

                '' AS reference,
                '' AS transaction_code,

                CASE
                    WHEN side='DEBE' THEN -amount
                    ELSE amount
                END AS amount,

                -100000 + id AS source_order,

                'SALDO_APERTURA' AS movement_kind,
                'APERTURA' AS movement_status,
                1 AS effective,
                NULL AS linked_movement_id,

                CASE
                    WHEN side='DEBE' THEN amount
                    ELSE 0
                END AS debit,

                CASE
                    WHEN side='HABER' THEN amount
                    ELSE 0
                END AS credit,

                'MANUAL' AS source,
                id AS opening_balance_id

            FROM opening_balances
        """))

        rows = deposits + openings

        rows.sort(
            key=lambda r: (
                r["posted_at"] or "",
                r["source_order"] if r["source_order"] is not None else 999999
            )
        )

        return rows


def api_companies():
    with connect() as con:
        return rows_dict(con.execute("SELECT name,abbr FROM companies ORDER BY name"))


def api_settings_get():
    with connect() as con:
        return {"current_tc": float(get_setting(con, "current_tc", "11.77")),
                "payment_days": int(float(get_setting(con, "payment_days", "30")))}

def api_opening_balance_post(data):

    company = str(
        data.get("company")
        or ""
    ).strip()

    posted_at = str(
        data.get("posted_at")
        or ""
    ).strip()

    side = str(
        data.get("side")
        or ""
    ).strip().upper()

    note = str(
        data.get("note")
        or ""
    ).strip()

    try:
        amount = round(
            abs(
                float(
                    data.get(
                        "amount",
                        0
                    )
                )
            ),
            2
        )

    except (TypeError, ValueError):

        raise ValueError(
            "Monto inválido."
        )

    if not company:
        raise ValueError(
            "Debe seleccionar una empresa."
        )

    if not posted_at:
        raise ValueError(
            "Debe indicar la fecha."
        )

    if side not in (
        "DEBE",
        "HABER"
    ):
        raise ValueError(
            "Seleccione DEBE o HABER."
        )

    if amount <= 0:
        raise ValueError(
            "El monto debe ser mayor a cero."
        )

    with connect() as con:

        cur = con.execute("""
            INSERT INTO opening_balances(
                company,
                posted_at,
                side,
                amount,
                note
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            company,
            posted_at,
            side,
            amount,
            note
        ))

        balance_id = cur.lastrowid

        con.commit()

    return {
        "ok": True,
        "id": balance_id,
        "message":
            "Saldo de apertura registrado."
    }

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
            if path == "/api/company-statement":

                qs = parse_qs(urlparse(self.path).query)
                company = (qs.get( "company") or [""])[0]
                as_of = ( qs.get( "as_of" ) or [""])[0]

                return self.send_json(
                    api_company_statement(
                        company,
                        as_of
                    )
                )
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
            if path == "/api/opening-balance":
                return self.send_json(api_opening_balance_post(self.read_json()))
            if path == "/api/settings":
                return self.send_json(api_settings_post(self.read_json()))
            if path == "/api/debt-meta":
                return self.send_json(api_debt_meta(self.read_json()))
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
            opening_prefix = "/api/opening-balance/"
            if path.startswith(opening_prefix):
                balance_id = int(path[len(opening_prefix):])
                return self.send_json(api_delete_opening_balance(balance_id))
            return self.send_json({"error": "No encontrado"}, 404)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:
            return self.send_json({"error": str(e)}, 500)
def api_company_statement(
    company: str,
    as_of: str
):

    company = (
        company or ""
    ).strip()

    as_of = (
        as_of
        or date.today().isoformat()
    ).strip()

    try:

        cutoff = datetime.strptime(
            as_of,
            "%Y-%m-%d"
        ).date()

    except ValueError:

        raise ValueError(
            "Fecha hasta inválida."
        )

    all_companies = (
        company in (
            "",
            "__ALL__"
        )
    )

    movements = []

    with connect() as con:

        # =================================
        # 1. SALDO DE APERTURA
        # =================================

        sql = """
            SELECT
                id,
                company,
                posted_at,
                side,
                amount,
                note
            FROM opening_balances
        """

        params = []

        if not all_companies:

            sql += """
                WHERE company=?
            """

            params.append(company)

        for o in con.execute(
            sql,
            params
        ):

            if (
                not o["posted_at"]
                or o["posted_at"][:10]
                    > as_of
            ):
                continue

            side = (
                o["side"]
                or ""
            ).upper()

            movements.append({

                "id":
                    f"OPEN-{o['id']}",

                "company":
                    o["company"]
                    or "SIN EMPRESA",

                "date":
                    o["posted_at"][:10],

                "transaction_code": "",

                "dte_number": "",

                "debit":
                    float(o["amount"])
                    if side == "DEBE"
                    else 0.0,

                "credit":
                    float(o["amount"])
                    if side == "HABER"
                    else 0.0,

                "voucher":
                    "APERTURA",

                "detail":
                    o["note"]
                    or "Saldo de apertura",

                "_order": 0,
            })

        # =================================
        # 2. DTE / RELIQUIDACIONES
        # =================================

        sql = """
            SELECT
                id,
                source_date,
                concept,
                company,
                amount,
                dte_number,
                voucher
            FROM debts
            WHERE active=1
        """

        params = []

        if not all_companies:

            sql += """
                AND company=?
            """

            params.append(company)

        for d in con.execute(
            sql,
            params
        ):

            if (
                not d["source_date"]
                or d["source_date"] > as_of
            ):
                continue

            amount = float(
                d["amount"] or 0
            )

            movements.append({

                "id":
                    f"DEBT-{d['id']}",

                "company":
                    d["company"],

                "date":
                    d["source_date"],

                "transaction_code": "",

                "dte_number":
                    d["dte_number"]
                    or "",

                "debit":
                    amount
                    if amount >= 0
                    else 0.0,

                "credit":
                    abs(amount)
                    if amount < 0
                    else 0.0,

                "voucher":
                    d["voucher"]
                    or "",

                "detail":
                    d["concept"],

                "_order": 1,
            })

        # =================================
        # 3. PAGOS
        # =================================

        sql = """
            SELECT
                m.id,

                m.applied_amount,

                m.payment_date,

                m.transaction_code,

                m.reference,

                m.created_at,

                d.company,

                d.dte_number,

                d.voucher,

                d.concept,

                p.posted_at,

                p.transaction_code
                    AS dep_tx,

                p.reference
                    AS dep_ref

            FROM matches m

            JOIN debts d
                ON d.id=m.debt_id

            LEFT JOIN deposits p
                ON p.id=m.deposit_id

            WHERE d.active=1
        """

        params = []

        if not all_companies:

            sql += """
                AND d.company=?
            """

            params.append(company)

        for m in con.execute(
            sql,
            params
        ):

            movement_date = (

                m["payment_date"]

                or m["posted_at"]

                or m["created_at"]
            )

            if (
                not movement_date
                or movement_date[:10]
                    > as_of
            ):
                continue

            movements.append({

                "id":
                    f"PAY-{m['id']}",

                "company":
                    m["company"],

                "date":
                    movement_date[:10],

                "transaction_code": (
                    m["transaction_code"]
                    or m["dep_tx"]
                    or ""
                ),

                "dte_number":
                    m["dte_number"]
                    or "",

                "debit": 0.0,

                "credit": abs(
                    float(
                        m["applied_amount"]
                        or 0
                    )
                ),

                "voucher": (
                    m["reference"]
                    or m["dep_ref"]
                    or m["voucher"]
                    or ""
                ),

                "detail":
                    f"Pago {m['concept']}",

                "_order": 2,
            })

    # =========================
    # ORDEN CRONOLÓGICO
    # =========================

    movements.sort(
        key=lambda r: (
            r["company"],
            r["date"],
            r["_order"],
            r["id"]
        )
    )

    # =========================
    # SALDO ACUMULADO
    # =========================

    balances = {}

    total_debit = 0.0
    total_credit = 0.0

    for row in movements:

        company_name = (
            row["company"]
        )

        previous = balances.get(
            company_name,
            0.0
        )

        current = (
            previous
            + row["debit"]
            - row["credit"]
        )

        balances[
            company_name
        ] = round(
            current,
            2
        )

        row["debit"] = round(
            row["debit"],
            2
        )

        row["credit"] = round(
            row["credit"],
            2
        )

        row["balance"] = balances[
            company_name
        ]

        total_debit += (
            row["debit"]
        )

        total_credit += (
            row["credit"]
        )

        row.pop(
            "_order",
            None
        )

    return {

        "company": (
            "TODAS"
            if all_companies
            else company
        ),

        "as_of":
            cutoff.isoformat(),

        "rows":
            movements,

        "summary": {

            "debit": round(
                total_debit,
                2
            ),

            "credit": round(
                total_credit,
                2
            ),

            "balance": round(
                sum(
                    balances.values()
                ),
                2
            ),
        },
    }


def api_debt_meta(data):

    debt_id = str(
        data.get(
            "debt_id"
        )
        or ""
    ).strip()

    if not debt_id:

        raise ValueError(
            "Falta debt_id."
        )

    updates = []
    params = []

    if "dte_number" in data:

        updates.append(
            "dte_number=?"
        )

        params.append(
            str(
                data.get(
                    "dte_number"
                )
                or ""
            ).strip()
        )

    if "voucher" in data:

        updates.append(
            "voucher=?"
        )

        params.append(
            str(
                data.get(
                    "voucher"
                )
                or ""
            ).strip()
        )

    if not updates:

        raise ValueError(
            "No hay campos para actualizar."
        )

    with connect() as con:

        params.append(
            debt_id
        )

        cur = con.execute(
            f"""
            UPDATE debts
            SET {', '.join(updates)}
            WHERE id=?
            """,
            params
        )

        if cur.rowcount == 0:

            raise ValueError(
                "Deuda no encontrada."
            )

        con.commit()

    return {
        "ok": True
    }
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