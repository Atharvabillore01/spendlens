"""Parse a block of spreadsheet cells pasted straight from Excel or Sheets.

Copying a range out of Excel puts **tab-separated text** on the clipboard, one
line per row. That looks trivial to split on `\\t` and rarely is, because real
spreadsheet data carries:

  * `1,234.56` and `(1,234.56)` and `$1,234` and `1 234,56` -- thousands
    separators, accounting negatives, currency symbols, non-breaking spaces
  * dates as `31/12/2025`, `12-31-2025`, `2025-12-31` or an Excel serial number
  * a header row that may or may not be present, and whose names rarely match
    ours exactly ("Txn Date", "Amount (USD)", "Category")
  * quoted cells containing the delimiter itself
  * trailing blank rows and a trailing newline

So this module does three separable jobs -- **split**, **map columns**, and
**coerce values** -- and reports what it did rather than guessing silently. The
caller previews the result before anything is written, because a paste is even
easier to get wrong than a file upload and there is no artifact to re-examine
afterwards.

Coercion is deliberately *not* shared with `loader.normalize`: that function is
the authority on what a valid transaction is, and it runs afterwards on the
frame this produces. This only turns text into the right dtypes.
"""

from __future__ import annotations

import csv
import io
import re
import warnings
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from .loader import REQUIRED_COLUMNS, IngestError

# Header spellings seen in real client exports, normalised to our column names.
# Matching is done on a squashed form (lowercase, alphanumerics only), so
# "Txn Date", "txn_date" and "TXN-DATE" all collapse to the same key.
HEADER_ALIASES: dict[str, str] = {
    "userid": "user_id",
    "user": "user_id",
    "customerid": "user_id",
    "accountid": "user_id",
    "username": "user_name",
    "name": "user_name",
    "customername": "user_name",
    "fullname": "user_name",
    "transactiondate": "transaction_date",
    "date": "transaction_date",
    "txndate": "transaction_date",
    "postingdate": "transaction_date",
    "valuedate": "transaction_date",
    "transactionamount": "transaction_amount",
    "amount": "transaction_amount",
    "value": "transaction_amount",
    "amountusd": "transaction_amount",
    "debitcredit": "transaction_amount",
    "transactioncategorydetail": "transaction_category_detail",
    "category": "transaction_category_detail",
    "categorydetail": "transaction_category_detail",
    "transactioncategory": "transaction_category_detail",
    "merchantname": "merchant_name",
    "merchant": "merchant_name",
    "description": "merchant_name",
    "payee": "merchant_name",
    "vendor": "merchant_name",
}

KNOWN_COLUMNS = tuple(REQUIRED_COLUMNS) + ("merchant_name",)

# Currency symbols, thousands separators and whitespace that Excel leaves behind
# (including U+00A0, which looks identical to a space and is not one).
_CURRENCY = re.compile(r"[^\d,.\-()]")
_NBSP = " "

MAX_PASTE_ROWS = 20_000
MAX_PASTE_CHARS = 8_000_000


@dataclass
class PasteReport:
    """What the parse made of the text, for the caller to show before writing."""

    rows_parsed: int = 0
    columns_detected: list[str] = field(default_factory=list)
    header_used: bool = True
    column_mapping: dict[str, str] = field(default_factory=dict)
    unmapped_columns: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    coercion_notes: list[str] = field(default_factory=list)
    dropped_blank_rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.missing_required and self.rows_parsed > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_parsed": self.rows_parsed,
            "columns_detected": self.columns_detected,
            "header_used": self.header_used,
            "column_mapping": self.column_mapping,
            "unmapped_columns": self.unmapped_columns,
            "missing_required": self.missing_required,
            "coercion_notes": self.coercion_notes[:20],
            "dropped_blank_rows": self.dropped_blank_rows,
            "ok": self.ok,
        }


def _squash(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).strip().lower())


def sniff_delimiter(text: str) -> str:
    """Tab unless the text is plainly comma- or semicolon-separated.

    Excel puts tabs on the clipboard, so tab is the default and the only reason
    to override it is clear evidence -- a CSV pasted from a text editor. Counting
    on the first few non-empty lines avoids being fooled by a single stray comma
    inside one description field.
    """
    sample = [line for line in text.splitlines() if line.strip()][:10]
    if not sample:
        return "\t"
    counts = {d: sum(line.count(d) for line in sample) for d in ("\t", ",", ";", "|")}
    if counts["\t"]:
        return "\t"
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else "\t"


def looks_like_header(cells: list[str]) -> bool:
    """A header row is text that isn't data.

    The reliable signal is that a header cell is never a number and never a
    date; requiring a *known* alias instead would reject files whose headers we
    simply haven't seen, which is the common case.
    """
    if not cells:
        return False
    known = sum(1 for c in cells if _squash(c) in HEADER_ALIASES or _squash(c) in {_squash(k) for k in KNOWN_COLUMNS})
    if known >= 2:
        return True
    numeric = 0
    for cell in cells:
        text = str(cell).strip()
        if not text:
            continue
        if _coerce_number(text) is not None or _coerce_date(text) is not None:
            numeric += 1
    return numeric == 0


def _coerce_number(value: Any) -> Optional[float]:
    """`$1,234.56`, `(89.00)` and `1 234,56` all become floats.

    Parenthesised values are accounting notation for a negative, which is easy
    to lose and expensive to lose in a financial system.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)

    text = str(value).replace(_NBSP, " ").strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = _CURRENCY.sub("", text).replace("(", "").replace(")", "")
    if not text or text in {"-", ".", ","}:
        return None

    # European style: "1.234,56" -> comma is the decimal separator.
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif text.count(",") == 1 and len(text.split(",")[-1]) in {1, 2}:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")

    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def _coerce_date(value: Any) -> Optional[pd.Timestamp]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (pd.Timestamp,)):
        return value

    text = str(value).strip()
    if not text:
        return None

    # An Excel serial date pasted as a bare number. The 1899-12-30 origin is
    # Excel's own off-by-one, not a typo.
    if re.fullmatch(r"\d{5}", text):
        try:
            return pd.Timestamp("1899-12-30") + pd.Timedelta(days=int(text))
        except (ValueError, OverflowError):
            return None

    # Both orders are tried, so pandas' "this looked day-first" advisory is
    # noise here -- 31/12/2025 only parses one way and that is the way we want.
    # Genuinely ambiguous dates (05/01) are flagged by `is_ambiguous_date`, not
    # resolved here: month-first is the assumption, and the caller says so.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        for dayfirst in (False, True):
            try:
                parsed = pd.to_datetime(text, dayfirst=dayfirst, errors="raise")
                if not pd.isna(parsed):
                    return pd.Timestamp(parsed)
            except (ValueError, TypeError, pd.errors.ParserError):
                continue
    return None


# `05/01/2026` is January 5th in most of the world and May 1st in the US. There
# is no way to tell from the value alone, so the parse must not pretend
# otherwise: it applies one rule consistently and tells the user which.
_AMBIGUOUS_DATE = re.compile(r"^\s*(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\s*$")


def is_ambiguous_date(value: Any) -> bool:
    match = _AMBIGUOUS_DATE.match(str(value or ""))
    if not match:
        return False
    first, second = int(match.group(1)), int(match.group(2))
    return 1 <= first <= 12 and 1 <= second <= 12 and first != second


def split_rows(text: str, delimiter: Optional[str] = None) -> list[list[str]]:
    """Split pasted text into cells, honouring quoted fields."""
    if len(text) > MAX_PASTE_CHARS:
        raise IngestError(f"pasted text is {len(text):,} characters, above the limit")

    delimiter = delimiter or sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, skipinitialspace=True)
    rows = [[cell.replace(_NBSP, " ").strip() for cell in row] for row in reader]
    return [r for r in rows if any(c for c in r)]


def map_columns(header: list[str], report: PasteReport) -> list[str]:
    """Header cells -> our column names, recording what could not be mapped."""
    mapping: dict[str, str] = {}
    resolved: list[str] = []
    known_squashed = {_squash(c): c for c in KNOWN_COLUMNS}

    for index, cell in enumerate(header):
        squashed = _squash(cell)
        target = known_squashed.get(squashed) or HEADER_ALIASES.get(squashed)
        if target and target not in resolved:
            mapping[cell or f"column {index + 1}"] = target
            resolved.append(target)
        else:
            resolved.append(cell or f"column_{index + 1}")
            if not target:
                report.unmapped_columns.append(cell or f"column {index + 1}")

    report.column_mapping = mapping
    return resolved


def parse_paste(
    text: str,
    delimiter: Optional[str] = None,
    has_header: Optional[bool] = None,
    column_overrides: Optional[dict[str, str]] = None,
) -> tuple[pd.DataFrame, PasteReport]:
    """Turn pasted spreadsheet text into a frame `loader.normalize` can accept.

    `column_overrides` maps a position (`"0"`, `"1"`, …) or a raw header name to
    one of our column names, so the UI can correct a mis-detected column without
    the user re-pasting.
    """
    report = PasteReport()
    rows = split_rows(text, delimiter)
    if not rows:
        raise IngestError("nothing to import — the pasted text held no rows")

    header_present = looks_like_header(rows[0]) if has_header is None else bool(has_header)
    report.header_used = header_present

    if header_present:
        columns = map_columns(rows[0], report)
        body = rows[1:]
    else:
        # No header: assume our canonical order, which is what a user pasting a
        # bare range from our own template will have.
        columns = list(KNOWN_COLUMNS)[: len(rows[0])]
        body = rows
        report.column_mapping = {f"column {i + 1}": c for i, c in enumerate(columns)}

    for key, target in (column_overrides or {}).items():
        if target not in KNOWN_COLUMNS:
            continue
        if key.isdigit():
            index = int(key)
            if 0 <= index < len(columns):
                columns[index] = target
        elif key in columns:
            columns[columns.index(key)] = target
    if column_overrides:
        report.column_mapping.update({k: v for k, v in column_overrides.items()})

    if not body:
        raise IngestError("the paste contained a header but no data rows")
    if len(body) > MAX_PASTE_ROWS:
        raise IngestError(f"{len(body):,} rows pasted, above the {MAX_PASTE_ROWS:,} limit — upload a file instead")

    width = len(columns)
    padded = []
    for row in body:
        if len(row) < width:
            row = row + [""] * (width - len(row))
        padded.append(row[:width])

    frame = pd.DataFrame(padded, columns=columns)
    before = len(frame)
    frame = frame.dropna(how="all")
    frame = frame[~(frame.astype(str).apply(lambda r: all(not c.strip() for c in r), axis=1))]
    report.dropped_blank_rows = before - len(frame)

    report.columns_detected = list(frame.columns)
    report.missing_required = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if report.missing_required:
        return frame, report

    # -- coercion ------------------------------------------------------------
    amounts = frame["transaction_amount"].map(_coerce_number)
    bad_amounts = int(amounts.isna().sum())
    if bad_amounts:
        report.coercion_notes.append(f"{bad_amounts} row(s) had an unreadable amount and were dropped")
    frame["transaction_amount"] = amounts

    # Warn before coercing, so the note names the assumption the user is about
    # to accept rather than describing it after the fact.
    ambiguous = int(frame["transaction_date"].map(is_ambiguous_date).sum())
    if ambiguous:
        report.coercion_notes.append(
            f"{ambiguous} date(s) like 05/01/2026 are ambiguous — read as MONTH/DAY/YEAR. "
            "Check the preview; re-paste as YYYY-MM-DD if that is wrong."
        )

    dates = frame["transaction_date"].map(_coerce_date)
    bad_dates = int(dates.isna().sum())
    if bad_dates:
        report.coercion_notes.append(f"{bad_dates} row(s) had an unreadable date and were dropped")
    frame["transaction_date"] = dates

    frame = frame.dropna(subset=["transaction_amount", "transaction_date"])

    for column in ("user_id", "user_name", "transaction_category_detail", "merchant_name"):
        if column in frame.columns:
            frame[column] = frame[column].astype(str).str.strip()

    if "merchant_name" not in frame.columns:
        frame["merchant_name"] = ""
        report.coercion_notes.append("no merchant column found; merchant left blank")

    report.rows_parsed = len(frame)
    if report.rows_parsed == 0:
        report.coercion_notes.append("every row was dropped — check the date and amount columns")
    return frame, report
