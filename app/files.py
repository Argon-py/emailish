"""Reading uploads (CSV / TXT / XLSX), detecting the email column, writing results.

All cells are read as strings so the user's data (ZIP codes, IDs, phone numbers)
is written back exactly as it came in.
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill

from .engine.models import VerificationResult

EMAIL_RE = re.compile(r"^\s*(mailto:)?<?[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+>?\s*$", re.I)
EMAIL_FIND = re.compile(r"[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
HEADER_HINTS = ("email", "e-mail", "e_mail", "mail", "emailaddress", "email address", "correo", "courriel")
SUPPORTED = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls")
MAX_XLSX_ROWS = 1_048_575


class FileError(ValueError):
    pass


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace")


def _looks_email(v: str) -> bool:
    return bool(v) and bool(EMAIL_RE.match(v))


def _finalize(raw: pd.DataFrame) -> pd.DataFrame:
    """raw has no header. Decide whether row 0 is a header; make unique string column names."""
    raw = raw.fillna("").astype(str)
    if raw.empty:
        raise FileError("The file contains no rows.")
    first = [str(x).strip() for x in raw.iloc[0].tolist()]
    has_header = not any(_looks_email(v) for v in first) and any(v for v in first)
    if has_header:
        names, body = first, raw.iloc[1:].reset_index(drop=True)
    else:
        names = ["email"] if raw.shape[1] == 1 else [f"column_{i + 1}" for i in range(raw.shape[1])]
        body = raw
    seen: dict[str, int] = {}
    cols = []
    for i, n in enumerate(names):
        n = n or f"column_{i + 1}"
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 0
        cols.append(n)
    body.columns = cols
    # drop completely empty trailing rows
    mask = body.apply(lambda r: any(str(x).strip() for x in r), axis=1)
    return body[mask].reset_index(drop=True) if len(body) else body


def _read_delimited(text: str) -> pd.DataFrame:
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        sep = dialect.delimiter
    except csv.Error:
        sep = ","
    raw = pd.read_csv(io.StringIO(text), sep=sep, header=None, dtype=str, keep_default_na=False,
                      na_filter=False, engine="python", skip_blank_lines=True, on_bad_lines="warn")
    return _finalize(raw)


def read_table(path: Path) -> pd.DataFrame:
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise FileError(f"Unsupported file type {ext!r}. Use CSV, TXT or XLSX.")
    if ext in (".xlsx", ".xlsm", ".xls"):
        try:
            raw = pd.read_excel(path, sheet_name=0, header=None, dtype=str, keep_default_na=False, na_filter=False)
        except ImportError as e:
            raise FileError("Old .xls files need the 'xlrd' package; please save as .xlsx.") from e
        except Exception as e:
            raise FileError(f"Could not read the Excel file: {e}") from e
        return _finalize(raw)
    text = _decode(path.read_bytes())
    if ext == ".txt":
        lines = [l for l in text.splitlines() if l.strip()]
        delimited = sum(1 for l in lines[:200] if re.search(r"[,;\t|]", l)) > 0.8 * min(len(lines), 200)
        if not delimited:
            # one value per line; pull the address out of noise like "John <john@x.com>"
            vals = []
            for l in lines:
                m = EMAIL_FIND.search(l)
                vals.append(m.group(0) if m else l.strip())
            if vals and vals[0].strip().lower() in HEADER_HINTS:
                vals = vals[1:]
            return pd.DataFrame({"email": vals}, dtype=str)
    return _read_delimited(text)


def detect_email_column(df: pd.DataFrame) -> tuple[str | None, dict[str, float]]:
    scores: dict[str, float] = {}
    sample = df.head(500)
    for col in df.columns:
        name = str(col).strip().lower().replace("_", " ")
        h = 0.0
        if name in HEADER_HINTS or name.replace(" ", "") in HEADER_HINTS:
            h = 1.0
        elif "email" in name or "e-mail" in name:
            h = 0.8
        elif "mail" in name:
            h = 0.4
        vals = [str(v) for v in sample[col].tolist() if str(v).strip()]
        frac = sum(_looks_email(v) for v in vals) / len(vals) if vals else 0.0
        scores[str(col)] = round(h + 3 * frac, 3)
    best = max(scores, key=scores.get) if scores else None
    if best is not None and scores[best] < 0.5:
        best = None
    return best, scores


def _result_columns(existing: list[str]) -> dict[str, str]:
    """Map our output column -> name used in the file (avoid clobbering user columns)."""
    taken = set(existing)
    out = {}
    for c in VerificationResult.OUTPUT_COLUMNS:
        name = c
        while name in taken:
            name = f"verify_{name}"
        out[c] = name
        taken.add(name)
    return out


def build_output(df: pd.DataFrame, email_col: str, results: dict[str, VerificationResult]) -> pd.DataFrame:
    colmap = _result_columns(list(df.columns))
    rows = [results[str(v)].to_row() for v in df[email_col].tolist()]
    res = pd.DataFrame(rows, columns=list(VerificationResult.OUTPUT_COLUMNS)).rename(columns=colmap)
    return pd.concat([df.reset_index(drop=True), res], axis=1)


STATUS_FILL = {
    "VALID": "C6EFCE", "INVALID": "FFC7CE", "CATCH_ALL": "FFEB9C", "DISPOSABLE": "F4B183",
    "ROLE_BASED": "DDEBF7", "RISKY": "FFE699", "UNKNOWN": "E7E6E6", "TIMEOUT": "E7E6E6",
}


def write_csv(out: pd.DataFrame, path: Path) -> None:
    out.to_csv(path, index=False, encoding="utf-8-sig")


def write_xlsx(out: pd.DataFrame, path: Path, status_col: str = "email_status") -> bool:
    if len(out) > MAX_XLSX_ROWS:
        return False
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("results")
    bold = Font(bold=True)
    header = []
    for c in out.columns:
        cell = WriteOnlyCell(ws, value=ILLEGAL_CHARACTERS_RE.sub("", str(c)))
        cell.font = bold
        header.append(cell)
    ws.append(header)
    status_idx = list(out.columns).index(status_col) if status_col in out.columns else -1
    fills = {k: PatternFill("solid", fgColor=v) for k, v in STATUS_FILL.items()}
    for row in out.itertuples(index=False, name=None):
        cells = []
        for i, v in enumerate(row):
            s = ILLEGAL_CHARACTERS_RE.sub("", "" if v is None else str(v))
            cell = WriteOnlyCell(ws, value=s)
            cell.data_type = "s"     # never let original data become a live formula
            if i == status_idx and s in fills:
                cell.fill = fills[s]
            cells.append(cell)
        ws.append(cells)
    wb.save(path)
    return True
