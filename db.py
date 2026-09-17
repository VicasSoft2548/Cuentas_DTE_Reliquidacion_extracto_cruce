from __future__ import annotations

import os
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timedelta, date
from typing import Any

from importer import parse_workbook, norm

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "cuentas.db")

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS companies (
    name TEXT PRIMARY KEY,
    abbr TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS debts (
    id TEXT PRIMARY KEY,
    source_date TEXT,
    concept TEXT NOT NULL,
    company TEXT NOT NULL,
    amount REAL NOT NULL,
    occurrence INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS deposits (
    id TEXT PRIMARY KEY,
    posted_at TEXT,
    branch TEXT,
    description TEXT,
    reference TEXT,
    transaction_code TEXT,
    amount REAL NOT NULL,
    occurrence INTEGER NOT NULL DEFAULT 1,
    source_order INTEGER,
    movement_kind TEXT NOT NULL DEFAULT 'CREDITO',
    movement_status TEXT NOT NULL DEFAULT 'VIGENTE',
    effective INTEGER NOT NULL DEFAULT 1,
    linked_movement_id TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    debt_id TEXT NOT NULL,
    deposit_id TEXT,
    applied_amount REAL NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('EXCEL','AUTO','MANUAL')),
    status TEXT NOT NULL DEFAULT 'EMPAREJADO',
    payment_date TEXT,
    reference TEXT,
    transaction_code TEXT,
    note TEXT,
    tc_dte REAL,
    tc_payment REAL,
    tc_due REAL,
    tc_current REAL,
    usd_due REAL,
    usd_today REAL,
    erosion_usd REAL,
    erosion_bs REAL,
    erosion_pct REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(debt_id) REFERENCES debts(id),
    FOREIGN KEY(deposit_id) REFERENCES deposits(id)
);
CREATE INDEX IF NOT EXISTS idx_matches_debt ON matches(debt_id);
CREATE INDEX IF NOT EXISTS idx_matches_dep ON matches(deposit_id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    debt_count INTEGER NOT NULL,
    deposit_count INTEGER NOT NULL,
    company_count INTEGER NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _ensure_deposit_columns(con: sqlite3.Connection) -> None:
    cols = {r["name"] for r in con.execute("PRAGMA table_info(deposits)")}
    additions = {
        "source_order": "INTEGER",
        "movement_kind": "TEXT NOT NULL DEFAULT 'CREDITO'",
        "movement_status": "TEXT NOT NULL DEFAULT 'VIGENTE'",
        "effective": "INTEGER NOT NULL DEFAULT 1",
        "linked_movement_id": "TEXT",
    }
    for name, ddl in additions.items():
        if name not in cols:
            con.execute(f"ALTER TABLE deposits ADD COLUMN {name} {ddl}")


def init_db() -> None:
    with connect() as con:
        con.executescript(SCHEMA)
        _ensure_deposit_columns(con)
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('payment_days','30')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('current_tc','11.77')")


def _key_debt(d: dict[str, Any]):
    return (d["source_date"], norm(d["concept"]), norm(d["company"]), round(float(d["amount"]), 2))


def import_workbook(path: str, filename: str | None = None) -> dict[str, int]:
    parsed = parse_workbook(path)
    init_db()
    with connect() as con:
        # Source='MANUAL' is intentionally preserved across imports.
        con.execute("UPDATE debts SET active=0")
        con.execute("UPDATE deposits SET active=0")
        con.execute("DELETE FROM matches WHERE source IN ('EXCEL','AUTO')")

        for c in parsed["companies"]:
            con.execute(
                "INSERT INTO companies(name,abbr) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET abbr=excluded.abbr",
                (c["name"], c["abbr"]),
            )
        for d in parsed["debts"]:
            con.execute(
                """INSERT INTO debts(id,source_date,concept,company,amount,occurrence,active)
                   VALUES(?,?,?,?,?,?,1)
                   ON CONFLICT(id) DO UPDATE SET source_date=excluded.source_date,concept=excluded.concept,
                   company=excluded.company,amount=excluded.amount,occurrence=excluded.occurrence,active=1""",
                (d["id"], d["source_date"], d["concept"], d["company"], d["amount"], d["occurrence"]),
            )
        for d in parsed["deposits"]:
            con.execute(
                """INSERT INTO deposits(id,posted_at,branch,description,reference,transaction_code,amount,occurrence,source_order,active)
                   VALUES(?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT(id) DO UPDATE SET posted_at=excluded.posted_at,branch=excluded.branch,
                   description=excluded.description,reference=excluded.reference,transaction_code=excluded.transaction_code,
                   amount=excluded.amount,occurrence=excluded.occurrence,source_order=excluded.source_order,active=1""",
                (d["id"], d["posted_at"], d["branch"], d["description"], d["reference"], d["transaction_code"], d["amount"], d["occurrence"], d.get("source_order")),
            )

        _classify_deposit_movements(con)
        _migrate_manual_reversed_matches(con)

        # Index debts by workbook identity so the historical 'clientes' sheet can be migrated.
        debt_queues = defaultdict(deque)
        for d in parsed["debts"]:
            debt_queues[_key_debt(d)].append(d["id"])
        dep_by_tx = defaultdict(list)
        dep_by_ref_amt = defaultdict(list)
        for d in parsed["deposits"]:
            if d["transaction_code"]:
                dep_by_tx[norm(d["transaction_code"])].append(d)
            dep_by_ref_amt[(norm(d["reference"]), round(d["amount"], 2))].append(d)

        for c in parsed["clients"]:
            key = (c["source_date"], norm(c["concept"]), norm(c["company"]), round(c["debt_amount"], 2))
            if not debt_queues[key]:
                continue
            debt_id = debt_queues[key].popleft()
            # Manual decisions are authoritative: do not duplicate them with an imported Excel match.
            manual_exists = con.execute("SELECT 1 FROM matches WHERE debt_id=? AND source='MANUAL' LIMIT 1", (debt_id,)).fetchone()
            if manual_exists:
                continue
            dep = None
            tx_candidates = dep_by_tx.get(norm(c["transaction_code"]), []) if c["transaction_code"] else []
            if len(tx_candidates) == 1:
                dep = tx_candidates[0]
            if dep is None:
                ref_candidates = dep_by_ref_amt.get((norm(c["reference"]), round(c["paid_amount"], 2)), [])
                if len(ref_candidates) == 1:
                    dep = ref_candidates[0]
            import_note = None
            if dep:
                dep_db = con.execute("SELECT * FROM deposits WHERE id=?", (dep["id"],)).fetchone()
                if dep_db and not dep_db["effective"] and dep_db["movement_kind"] == "CREDITO_REVERSADO":
                    repl = _replacement_for(con, dep_db["id"])
                    if repl:
                        dep = dict(repl)
                        import_note = "El credito original fue reversado; cruce reasignado al credito reaplicado vigente."
            con.execute(
                """INSERT INTO matches(debt_id,deposit_id,applied_amount,source,status,payment_date,reference,transaction_code,note,
                   tc_dte,tc_payment,tc_due,tc_current,usd_due,usd_today,erosion_usd,erosion_bs,erosion_pct)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (debt_id, dep["id"] if dep else None, c["paid_amount"], "EXCEL", c["status"] or "EMPAREJADO",
                 dep["posted_at"] if dep else c["payment_date"], dep["reference"] if dep else c["reference"], dep["transaction_code"] if dep else c["transaction_code"], import_note, c["tc_dte"], c["tc_payment"], c["tc_due"],
                 c["tc_current"], c["usd_due"], c["usd_today"], c["erosion_usd"], c["erosion_bs"], c["erosion_pct"]),
            )

        # Preserve a sensible TC from the source workbook when available.
        tcs = [c["tc_current"] for c in parsed["clients"] if c.get("tc_current")]
        if tcs:
            current_tc = tcs[-1]
            con.execute("INSERT INTO settings(key,value) VALUES('current_tc',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(current_tc),))

        _auto_match(con)
        con.execute(
            "INSERT INTO imports(filename,debt_count,deposit_count,company_count) VALUES(?,?,?,?)",
            (filename or os.path.basename(path), len(parsed["debts"]), len(parsed["deposits"]), len(parsed["companies"])),
        )
        con.commit()
    return {"debts": len(parsed["debts"]), "deposits": len(parsed["deposits"]), "companies": len(parsed["companies"])}


def _classify_deposit_movements(con: sqlite3.Connection) -> None:
    """Detecta reversos bancarios conservadoramente.

    Regla: un movimiento negativo cancela el positivo anterior más cercano del
    mismo importe y misma fecha. Si después del reverso aparece otro positivo
    por el mismo importe en esa fecha, se marca como reaplicación vigente.
    """
    con.execute("""UPDATE deposits
                   SET effective=CASE WHEN amount>0 THEN 1 ELSE 0 END,
                       movement_kind=CASE WHEN amount>0 THEN 'CREDITO' ELSE 'DEBITO_REVERSO' END,
                       movement_status=CASE WHEN amount>0 THEN 'VIGENTE' ELSE 'REVERSO / ANULACION' END,
                       linked_movement_id=NULL
                   WHERE active=1""")
    rows = [dict(r) for r in con.execute("SELECT * FROM deposits WHERE active=1 ORDER BY COALESCE(source_order,999999),posted_at,id")]
    used_positive = set()
    for neg in rows:
        if float(neg["amount"]) >= -0.005:
            continue
        target = round(abs(float(neg["amount"])), 2)
        day = (neg["posted_at"] or "")[:10]
        candidates = [p for p in rows
                      if p["id"] not in used_positive
                      and float(p["amount"]) > 0
                      and round(float(p["amount"]), 2) == target
                      and (p["posted_at"] or "")[:10] == day
                      and (p.get("source_order") or 0) < (neg.get("source_order") or 0)]
        if not candidates:
            continue
        prev = max(candidates, key=lambda x: (x.get("source_order") or 0))
        used_positive.add(prev["id"])
        con.execute("UPDATE deposits SET effective=0,movement_kind='CREDITO_REVERSADO',movement_status='REVERSADO',linked_movement_id=? WHERE id=?", (neg["id"], prev["id"]))
        con.execute("UPDATE deposits SET effective=0,movement_kind='DEBITO_REVERSO',movement_status='REVERSO / ANULACION',linked_movement_id=? WHERE id=?", (prev["id"], neg["id"]))
        replacements = [p for p in rows
                        if float(p["amount"]) > 0
                        and round(float(p["amount"]), 2) == target
                        and (p["posted_at"] or "")[:10] == day
                        and (p.get("source_order") or 0) > (neg.get("source_order") or 0)]
        if replacements:
            repl = min(replacements, key=lambda x: (x.get("source_order") or 0))
            con.execute("UPDATE deposits SET effective=1,movement_kind='CREDITO_REAPLICADO',movement_status='VIGENTE / REAPLICADO',linked_movement_id=? WHERE id=?", (prev["id"], repl["id"]))


def _replacement_for(con: sqlite3.Connection, deposit_id: str):
    return con.execute("""SELECT * FROM deposits
                          WHERE active=1 AND effective=1 AND movement_kind='CREDITO_REAPLICADO'
                            AND linked_movement_id=?
                          ORDER BY COALESCE(source_order,999999) LIMIT 1""", (deposit_id,)).fetchone()


def _migrate_manual_reversed_matches(con: sqlite3.Connection) -> None:
    rows = list(con.execute("""SELECT m.id,m.deposit_id,p.* FROM matches m
                               JOIN deposits p ON p.id=m.deposit_id
                               WHERE m.source='MANUAL' AND p.active=1 AND p.effective=0
                                 AND p.movement_kind='CREDITO_REVERSADO'"""))
    for r in rows:
        repl = _replacement_for(con, r["deposit_id"])
        if not repl:
            continue
        old_note = con.execute("SELECT note FROM matches WHERE id=?", (r["id"],)).fetchone()["note"] or ""
        msg = "Reasignado automaticamente al credito reaplicado despues de un reverso bancario"
        note = f"{old_note} · {msg}" if old_note else msg
        con.execute("""UPDATE matches SET deposit_id=?,payment_date=?,reference=?,transaction_code=?,note=? WHERE id=?""",
                    (repl["id"], repl["posted_at"], repl["reference"], repl["transaction_code"], note, r["id"]))


def _auto_match(con: sqlite3.Connection) -> None:
    """Conservative exact-amount matcher.

    It only auto-matches when the remaining amount is unique on both sides.
    Ambiguous cases are intentionally left for the Manual Matching tab.
    """
    debt_paid = {r["debt_id"]: r["paid"] for r in con.execute("SELECT debt_id,COALESCE(SUM(applied_amount),0) paid FROM matches GROUP BY debt_id")}
    dep_used = {r["deposit_id"]: r["used"] for r in con.execute("SELECT deposit_id,COALESCE(SUM(ABS(applied_amount)),0) used FROM matches WHERE deposit_id IS NOT NULL GROUP BY deposit_id")}
    debts = []
    for r in con.execute("SELECT * FROM debts WHERE active=1 AND amount>0"):
        rem = round(r["amount"] - debt_paid.get(r["id"], 0), 2)
        if rem > 0.01:
            debts.append((r, rem))
    deps = []
    for r in con.execute("SELECT * FROM deposits WHERE active=1 AND effective=1 AND amount>0"):
        rem = round(r["amount"] - dep_used.get(r["id"], 0), 2)
        if rem > 0.01:
            deps.append((r, rem))

    by_amount_debt = defaultdict(list)
    by_amount_dep = defaultdict(list)
    for r, rem in debts:
        by_amount_debt[round(rem, 2)].append(r)
    for r, rem in deps:
        by_amount_dep[round(rem, 2)].append(r)
    for amount, drows in by_amount_debt.items():
        prows = by_amount_dep.get(amount, [])
        if len(drows) != 1 or len(prows) != 1:
            continue
        debt, dep = drows[0], prows[0]
        # Payments before DTE date are not auto-linked.
        if dep["posted_at"] and debt["source_date"] and dep["posted_at"][:10] < debt["source_date"]:
            continue
        con.execute(
            """INSERT INTO matches(debt_id,deposit_id,applied_amount,source,status,payment_date,reference,transaction_code,note)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (debt["id"], dep["id"], amount, "AUTO", "EMPAREJADO AUTOMÁTICO", dep["posted_at"], dep["reference"], dep["transaction_code"], "Coincidencia exacta y única por monto"),
        )


def get_setting(con: sqlite3.Connection, key: str, default: str) -> str:
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def due_date(source_date: str | None, days: int) -> str | None:
    if not source_date:
        return None
    try:
        return (datetime.fromisoformat(source_date).date() + timedelta(days=days)).isoformat()
    except ValueError:
        return None


def debt_rows(con: sqlite3.Connection) -> list[dict[str, Any]]:
    days = int(float(get_setting(con, "payment_days", "30")))
    current_tc = float(get_setting(con, "current_tc", "11.77"))
    today = date.today()
    rows = []
    for d in con.execute("SELECT * FROM debts WHERE active=1 ORDER BY source_date, company, concept, amount"):
        ms = list(con.execute("SELECT * FROM matches WHERE debt_id=? ORDER BY created_at,id", (d["id"],)))
        paid = round(sum(float(m["applied_amount"]) for m in ms), 2)
        pending = round(float(d["amount"]) - paid, 2)
        due = due_date(d["source_date"], days)
        due_dt = datetime.fromisoformat(due).date() if due else None
        last_payment = max((m["payment_date"] for m in ms if m["payment_date"]), default=None)
        manual = any(m["source"] == "MANUAL" for m in ms)
        if float(d["amount"]) < 0:
            status = "AJUSTE / RELIQUIDACIÓN"
            mora = max((today - due_dt).days, 0) if due_dt else 0
        elif pending <= 0.01:
            if last_payment and due_dt:
                pay_dt = datetime.fromisoformat(last_payment[:10]).date()
                mora = max((pay_dt - due_dt).days, 0)
                status = "PAGADO EN PLAZO" if mora == 0 else ("PAGADO CON MORA <= 30 DIAS" if mora <= 30 else "PAGADO > 1 MES")
            else:
                mora = 0
                status = "PAGADO"
        else:
            mora = max((today - due_dt).days, 0) if due_dt else 0
            prefix = "PARCIAL - " if paid > 0.01 else ""
            status = prefix + ("VENCIDO" if due_dt and due_dt < today else "POR VENCER")

        # Prefer the workbook-provided exchange-rate fields. Unknown historical rates remain blank.
        last_with_meta = next((m for m in reversed(ms) if any(m[k] is not None for k in ("tc_dte","tc_payment","tc_due","erosion_usd"))), None)
        tc_dte = last_with_meta["tc_dte"] if last_with_meta else None
        tc_payment = last_with_meta["tc_payment"] if last_with_meta else None
        tc_due = last_with_meta["tc_due"] if last_with_meta else None
        ref = next((m["reference"] for m in reversed(ms) if m["reference"]), None)
        tx = next((m["transaction_code"] for m in reversed(ms) if m["transaction_code"]), None)
        days_to_pay = None
        if last_payment and d["source_date"]:
            try:
                days_to_pay = (datetime.fromisoformat(last_payment[:10]).date() - datetime.fromisoformat(d["source_date"]).date()).days
            except ValueError:
                pass
        usd_due = last_with_meta["usd_due"] if last_with_meta and last_with_meta["usd_due"] is not None else (float(d["amount"]) / tc_due if tc_due else None)
        usd_today = float(d["amount"]) / current_tc if current_tc else None
        erosion_usd = last_with_meta["erosion_usd"] if last_with_meta and last_with_meta["erosion_usd"] is not None else ((usd_due - usd_today) if usd_due is not None and usd_today is not None else None)
        erosion_bs = last_with_meta["erosion_bs"] if last_with_meta and last_with_meta["erosion_bs"] is not None else (erosion_usd * current_tc if erosion_usd is not None else None)
        erosion_pct = last_with_meta["erosion_pct"] * 100 if last_with_meta and last_with_meta["erosion_pct"] is not None else ((erosion_usd / usd_due * 100) if erosion_usd is not None and usd_due else None)
        rows.append({
            "id": d["id"], "date": d["source_date"], "concept": d["concept"], "company": d["company"],
            "reference": ref, "transaction_code": tx,
            "amount": round(float(d["amount"]), 2), "paid": paid, "pending": pending,
            "pending_usd": round(pending / current_tc, 2) if current_tc else None,
            "due_date": due, "last_payment": last_payment, "days_to_payment": days_to_pay,
            "days_late": mora, "status": status, "manual": manual, "match_count": len(ms),
            "tc_dte": tc_dte, "tc_payment": tc_payment, "tc_due": tc_due, "tc_current": current_tc,
            "usd_due": round(usd_due,2) if usd_due is not None else None,
            "usd_today": round(usd_today,2) if usd_today is not None else None,
            "erosion_usd": round(erosion_usd,2) if erosion_usd is not None else None,
            "erosion_bs": round(erosion_bs,2) if erosion_bs is not None else None,
            "erosion_pct": round(erosion_pct,2) if erosion_pct is not None else None,
        })
    return rows
