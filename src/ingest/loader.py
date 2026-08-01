"""Getting client data in — validate, derive, then bulk-insert.

Design constraints that shaped this:

**Re-uploading a file must not double every figure.** Clients resend files: a
corrected export, a retried upload, an overlapping date range. Each row carries
a `row_hash` over its natural key and the table has a unique constraint on
`(tenant_id, row_hash)`, so a repeat insert is skipped rather than duplicated.
Without this, "why did my spending double?" becomes the most common support
ticket.

**Derived columns are computed here, once,** by the same `CategoryTaxonomy` the
DataFrame path uses. Doing it at read time would mean re-deriving them for every
query over every row forever, and would put the category filter out of reach of
an index.

**Bad rows are dropped and counted, not fatal.** A 200,000-row upload with nine
malformed dates should load 199,991 rows and tell you about the nine. Rejecting
the file wholesale means a client with one bad row has no service at all.

**The whole batch is one transaction.** A partial load is worse than no load:
figures computed over half a file are wrong without looking wrong.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError

from ..data.category_taxonomy import CategoryTaxonomy
from ..data.roster import name_parts
from ..db.schema import app_users, ingest_batches, tenants, transactions, user_name_parts

log = logging.getLogger("transaction_rag.ingest")

REQUIRED_COLUMNS = (
    "user_id",
    "user_name",
    "transaction_date",
    "transaction_amount",
    "transaction_category_detail",
)


class IngestError(RuntimeError):
    """Raised for problems with the file as a whole, never for a single row."""


@dataclass
class IngestReport:
    batch_id: str
    tenant_id: str
    filename: str = ""
    total_rows: int = 0
    inserted: int = 0
    skipped_duplicates: int = 0
    rejected_rows: int = 0
    users_seen: int = 0
    rejections: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "tenant_id": self.tenant_id,
            "filename": self.filename,
            "total_rows": self.total_rows,
            "inserted": self.inserted,
            "skipped_duplicates": self.skipped_duplicates,
            "rejected_rows": self.rejected_rows,
            "users_seen": self.users_seen,
            # Bounded: a wholly malformed file must not return 200k messages.
            "rejections": self.rejections[:20],
        }


def read_table(path: Path) -> pd.DataFrame:
    """Load a client file by extension. CSV and Excel are what clients send."""
    suffix = Path(path).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(path)
    if suffix == ".parquet":
        return pd.read_parquet(path)
    raise IngestError(f"unsupported file type '{suffix}' (expected .csv, .xlsx or .parquet)")


def row_hash(tenant_id: str, row: pd.Series) -> str:
    """Stable identity for a transaction, over its natural key.

    Two rows identical in every business field *are* the same transaction as far
    as a client file is concerned — there is no transaction id in the source.
    """
    parts = (
        str(tenant_id),
        str(row["user_id"]),
        pd.Timestamp(row["transaction_date"]).isoformat(),
        f"{float(row['transaction_amount']):.2f}",
        str(row["transaction_category_detail"]).strip().upper(),
        str(row.get("merchant_name", "")).strip().lower(),
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def normalize(df: pd.DataFrame, tenant_id: str, report: IngestReport) -> pd.DataFrame:
    """Validate, coerce and derive. Returns the frame ready for insertion."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise IngestError(f"file is missing required columns: {missing}")

    frame = df.copy()
    report.total_rows = len(frame)

    frame["transaction_date"] = pd.to_datetime(frame["transaction_date"], errors="coerce")
    frame["transaction_amount"] = pd.to_numeric(frame["transaction_amount"], errors="coerce")
    frame["user_id"] = frame["user_id"].astype(str).str.strip()
    frame["user_name"] = frame["user_name"].astype(str).str.strip()
    frame["transaction_category_detail"] = (
        frame["transaction_category_detail"].astype(str).str.strip().str.upper()
    )
    if "merchant_name" not in frame.columns:
        frame["merchant_name"] = ""
    frame["merchant_name"] = frame["merchant_name"].fillna("").astype(str).str.strip()

    before = len(frame)
    bad_date = frame["transaction_date"].isna()
    bad_amount = frame["transaction_amount"].isna()
    bad_user = frame["user_id"].isin(["", "nan", "None"])
    bad_category = frame["transaction_category_detail"].isin(["", "NAN", "NONE"])

    for label, mask in (
        ("unparseable transaction_date", bad_date),
        ("non-numeric transaction_amount", bad_amount),
        ("missing user_id", bad_user),
        ("missing transaction_category_detail", bad_category),
    ):
        count = int(mask.sum())
        if count:
            report.rejections.append(f"{count} row(s): {label}")

    frame = frame[~(bad_date | bad_amount | bad_user | bad_category)].copy()
    report.rejected_rows = before - len(frame)

    if frame.empty:
        return frame

    # Derived columns -- the same logic the DataFrame backend applies at load.
    taxonomy = CategoryTaxonomy.from_frame(frame)
    frame = taxonomy.annotate(frame)
    frame["is_income"] = frame["transaction_amount"] < 0
    frame["expense_amount"] = frame["transaction_amount"].clip(lower=0)
    frame["income_amount"] = (-frame["transaction_amount"]).clip(lower=0)
    frame["row_hash"] = frame.apply(lambda r: row_hash(tenant_id, r), axis=1)

    # A file that repeats a row internally should insert it once.
    frame = frame.drop_duplicates(subset=["row_hash"], keep="first")
    return frame


def ensure_tenant(engine, tenant_id: str, name: str = "", as_of_mode: Optional[str] = None) -> None:
    """Create the tenant row if absent. Idempotent."""
    with engine.begin() as conn:
        exists = conn.execute(
            select(tenants.c.tenant_id).where(tenants.c.tenant_id == tenant_id)
        ).first()
        if exists is None:
            conn.execute(
                insert(tenants).values(
                    tenant_id=tenant_id,
                    name=name or tenant_id,
                    as_of_mode=as_of_mode,
                    is_active=True,
                )
            )


def _upsert_users(conn, tenant_id: str, frame: pd.DataFrame) -> int:
    """Register users and their searchable name parts."""
    people = (
        frame[["user_id", "user_name"]]
        .drop_duplicates(subset=["user_id"], keep="last")
        .to_dict("records")
    )
    if not people:
        return 0

    known = {
        row[0]
        for row in conn.execute(
            select(app_users.c.user_id).where(app_users.c.tenant_id == tenant_id)
        )
    }

    fresh = [p for p in people if p["user_id"] not in known]
    if fresh:
        conn.execute(
            insert(app_users),
            [
                {"tenant_id": tenant_id, "user_id": p["user_id"], "user_name": p["user_name"]}
                for p in fresh
            ],
        )

    # Name parts feed the cross-user guardrail's indexed lookup. Rewritten for
    # every user in the batch so a corrected spelling doesn't leave a stale part
    # behind that would keep refusing legitimate questions.
    touched = [p["user_id"] for p in people]
    conn.execute(
        delete(user_name_parts).where(
            user_name_parts.c.tenant_id == tenant_id,
            user_name_parts.c.user_id.in_(touched),
        )
    )
    rows = [
        {"tenant_id": tenant_id, "part": part, "user_id": p["user_id"]}
        for p in people
        for part in name_parts(p["user_name"])
    ]
    if rows:
        conn.execute(insert(user_name_parts), rows)

    return len(fresh)


def ingest_frame(
    engine,
    tenant_id: str,
    df: pd.DataFrame,
    filename: str = "",
    chunk_size: int = 5_000,
    max_rows: int = 2_000_000,
    batch_id: Optional[str] = None,
) -> IngestReport:
    """Load one client file into the store. Atomic: all rows or none."""
    batch = batch_id or uuid.uuid4().hex[:16]
    report = IngestReport(batch_id=batch, tenant_id=tenant_id, filename=filename)

    if len(df) > max_rows:
        raise IngestError(f"file has {len(df):,} rows, above the {max_rows:,}-row limit")

    ensure_tenant(engine, tenant_id)
    frame = normalize(df, tenant_id, report)

    if frame.empty:
        _record_batch(engine, report, status="empty")
        return report

    existing_hashes = _existing_hashes(engine, tenant_id, frame["row_hash"].tolist(), chunk_size)
    pending = frame[~frame["row_hash"].isin(existing_hashes)]
    report.skipped_duplicates = len(frame) - len(pending)

    payload = [
        {
            "tenant_id": tenant_id,
            "user_id": row.user_id,
            "transaction_date": row.transaction_date.to_pydatetime(),
            "transaction_amount": float(row.transaction_amount),
            "transaction_category_detail": row.transaction_category_detail,
            "merchant_name": row.merchant_name,
            "subcategory": row.subcategory,
            "parent_category": row.parent_category,
            "is_income": bool(row.is_income),
            "expense_amount": float(row.expense_amount),
            "income_amount": float(row.income_amount),
            "batch_id": batch,
            "row_hash": row.row_hash,
        }
        for row in pending.itertuples(index=False)
    ]

    try:
        with engine.begin() as conn:
            report.users_seen = _upsert_users(conn, tenant_id, frame)
            for start in range(0, len(payload), chunk_size):
                conn.execute(insert(transactions), payload[start : start + chunk_size])
        report.inserted = len(payload)
    except IntegrityError as exc:
        # A concurrent upload of the same file can still collide on the unique
        # constraint between our check and our insert. That is the constraint
        # doing its job, so report it rather than failing the client.
        log.warning("ingest %s hit a uniqueness collision: %s", batch, exc.orig)
        report.skipped_duplicates += len(payload)
        report.inserted = 0
        _record_batch(engine, report, status="duplicate")
        return report

    _record_batch(engine, report, status="complete")
    log.info(
        "ingest %s tenant=%s inserted=%d skipped=%d rejected=%d",
        batch, tenant_id, report.inserted, report.skipped_duplicates, report.rejected_rows,
    )
    return report


def _existing_hashes(engine, tenant_id: str, hashes: list[str], chunk_size: int) -> set[str]:
    """Which of these rows do we already hold? Chunked to keep IN() bounded."""
    found: set[str] = set()
    with engine.connect() as conn:
        for start in range(0, len(hashes), chunk_size):
            window = hashes[start : start + chunk_size]
            rows = conn.execute(
                select(transactions.c.row_hash).where(
                    transactions.c.tenant_id == tenant_id,
                    transactions.c.row_hash.in_(window),
                )
            )
            found.update(row[0] for row in rows)
    return found


def _record_batch(engine, report: IngestReport, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(ingest_batches).values(
                batch_id=report.batch_id,
                tenant_id=report.tenant_id,
                filename=report.filename,
                row_count=report.total_rows,
                inserted_count=report.inserted,
                skipped_count=report.skipped_duplicates + report.rejected_rows,
                status=status,
                error="; ".join(report.rejections[:5]) or None,
            )
        )


def ingest_file(engine, tenant_id: str, path: Path, **kwargs) -> IngestReport:
    """Convenience wrapper: read a file from disk and ingest it."""
    path = Path(path)
    return ingest_frame(engine, tenant_id, read_table(path), filename=path.name, **kwargs)


def delete_batch(engine, tenant_id: str, batch_id: str) -> int:
    """Reverse one upload. The reason `batch_id` is on every row."""
    with engine.begin() as conn:
        result = conn.execute(
            delete(transactions).where(
                transactions.c.tenant_id == tenant_id,
                transactions.c.batch_id == batch_id,
            )
        )
        conn.execute(
            ingest_batches.update()
            .where(
                ingest_batches.c.tenant_id == tenant_id,
                ingest_batches.c.batch_id == batch_id,
            )
            .values(status="reverted")
        )
    return int(result.rowcount or 0)


__all__ = [
    "IngestError",
    "IngestReport",
    "ingest_frame",
    "ingest_file",
    "read_table",
    "delete_batch",
    "ensure_tenant",
    "normalize",
]
