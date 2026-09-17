from __future__ import annotations

import hashlib
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS}


def excel_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(value[:10], fmt).date().isoformat()
            except ValueError:
                pass
        try:
            value = float(value.replace(",", "."))
        except ValueError:
            return value[:10]
    try:
        base = datetime(1899, 12, 30)
        return (base + timedelta(days=float(value))).date().isoformat()
    except (TypeError, ValueError):
        return None


def excel_datetime(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(value[:19], fmt).isoformat(sep=" ")
            except ValueError:
                pass
        try:
            value = float(value.replace(",", "."))
        except ValueError:
            return value
    try:
        base = datetime(1899, 12, 30)
        return (base + timedelta(days=float(value))).isoformat(sep=" ", timespec="seconds")
    except (TypeError, ValueError):
        return None


def num(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if not text:
        return 0.0
    # Handle both 1.234,56 and 1,234.56 styles conservatively.
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def norm(text: Any) -> str:
    return " ".join(str(text or "").strip().upper().split())


def fingerprint(*parts: Any) -> str:
    raw = "|".join(norm(x) if not isinstance(x, float) else f"{x:.2f}" for x in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


class XlsmReader:
    """Minimal XLSX/XLSM reader for the sheets used by this project.

    It uses only the OOXML zip/xml structure, so macro-enabled workbooks can be
    imported without executing VBA.
    """

    def __init__(self, path: str):
        self.path = path
        self.z = zipfile.ZipFile(path)
        self.shared = self._shared_strings()
        self.sheets = self._sheet_map()

    def close(self):
        self.z.close()

    def _shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self.z.namelist():
            return []
        root = ET.fromstring(self.z.read("xl/sharedStrings.xml"))
        values = []
        for si in root.findall("m:si", NS):
            values.append("".join(t.text or "" for t in si.iter(f"{{{MAIN_NS}}}t")))
        return values

    def _sheet_map(self) -> dict[str, str]:
        wb = ET.fromstring(self.z.read("xl/workbook.xml"))
        rels = ET.fromstring(self.z.read("xl/_rels/workbook.xml.rels"))
        relmap = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
        result = {}
        for s in wb.find("m:sheets", NS):
            name = s.attrib["name"]
            rid = s.attrib[f"{{{REL_NS}}}id"]
            target = relmap[rid].lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            result[name] = os.path.normpath(target).replace("\\", "/")
        return result

    def _cell_value(self, cell: ET.Element) -> Any:
        cell_type = cell.attrib.get("t")
        value = cell.find("m:v", NS)
        if cell_type == "inlineStr":
            inline = cell.find("m:is", NS)
            if inline is None:
                return ""
            return "".join(t.text or "" for t in inline.iter(f"{{{MAIN_NS}}}t"))
        if value is None:
            return ""
        text = value.text or ""
        if cell_type == "s":
            try:
                return self.shared[int(text)]
            except (ValueError, IndexError):
                return text
        if cell_type == "b":
            return text == "1"
        try:
            if any(ch in text.upper() for ch in (".", "E")):
                return float(text)
            return int(text)
        except ValueError:
            return text

    def rows(self, sheet_name: str) -> list[dict[str, Any]]:
        if sheet_name not in self.sheets:
            return []
        root = ET.fromstring(self.z.read(self.sheets[sheet_name]))
        rows = []
        for row in root.findall(".//m:sheetData/m:row", NS):
            values = {}
            for cell in row.findall("m:c", NS):
                ref = cell.attrib.get("r", "")
                match = re.match(r"([A-Z]+)", ref)
                if match:
                    values[match.group(1)] = self._cell_value(cell)
            rows.append(values)
        return rows


def parse_workbook(path: str) -> dict[str, Any]:
    reader = XlsmReader(path)
    try:
        dte_rows = reader.rows("DTE")
        ext_rows = reader.rows("extracto")
        company_rows = reader.rows("Empresa")
        client_rows = reader.rows("clientes")

        debts = []
        occurrence = defaultdict(int)
        for r in dte_rows[1:]:
            if r.get("A") in (None, "") or not str(r.get("B", "")).strip():
                continue
            date = excel_date(r.get("A"))
            concept = str(r.get("B") or "").strip()
            company = str(r.get("C") or "").strip()
            amount = round(num(r.get("D")), 2)
            base = (date, norm(concept), norm(company), amount)
            occurrence[base] += 1
            occ = occurrence[base]
            debt_id = fingerprint("DEBT", *base, occ)
            debts.append({
                "id": debt_id,
                "source_date": date,
                "concept": concept,
                "company": company,
                "amount": amount,
                "occurrence": occ,
            })

        deposits = []
        occurrence = defaultdict(int)
        for source_order, r in enumerate(ext_rows[1:], start=2):
            credit = round(num(r.get("F")), 2)
            if r.get("A") in (None, "") or abs(credit) < 0.005 or abs(credit - 1) < 0.005:
                continue
            dt = excel_datetime(r.get("A"))
            branch = str(r.get("B") or "").strip()
            description = str(r.get("C") or "").strip()
            reference = str(r.get("D") or "").strip()
            transaction = str(r.get("E") or "").strip()
            base = (dt, branch, description, reference, transaction, credit)
            occurrence[base] += 1
            occ = occurrence[base]
            dep_id = fingerprint("DEP", *base, occ)
            deposits.append({
                "id": dep_id,
                "posted_at": dt,
                "branch": branch,
                "description": description,
                "reference": reference,
                "transaction_code": transaction,
                "amount": credit,
                "occurrence": occ,
                "source_order": source_order,
            })

        companies = []
        for r in company_rows[1:]:
            company = str(r.get("B") or "").strip()
            if not company:
                continue
            companies.append({"name": company, "abbr": str(r.get("C") or "").strip()})

        # Existing client rows are the workbook's already-resolved pairings.
        clients = []
        for r in client_rows[1:]:
            detail = str(r.get("A") or "").strip()
            if not detail:
                continue
            concept, company = (detail.split(" - ", 1) + [""])[:2] if " - " in detail else ("DTE", detail)
            clients.append({
                "detail": detail,
                "concept": concept.strip(),
                "company": company.strip(),
                "source_date": excel_date(r.get("B")),
                "reference": str(r.get("C") or "").strip(),
                "transaction_code": str(r.get("D") or "").strip(),
                "debt_amount": round(num(r.get("E")), 2),
                "paid_amount": round(num(r.get("F")), 2),
                "payment_date": excel_datetime(r.get("J")),
                "status": str(r.get("M") or "").strip(),
                "tc_dte": num(r.get("N")) or None,
                "tc_payment": num(r.get("O")) or None,
                "tc_due": num(r.get("P")) or None,
                "tc_current": num(r.get("Q")) or None,
                "usd_due": num(r.get("R")) or None,
                "usd_today": num(r.get("S")) or None,
                "erosion_usd": num(r.get("T")) or None,
                "erosion_bs": num(r.get("U")) or None,
                "erosion_pct": num(r.get("V")) or None,
            })

        return {
            "debts": debts,
            "deposits": deposits,
            "companies": companies,
            "clients": clients,
            "sheet_names": list(reader.sheets.keys()),
        }
    finally:
        reader.close()
