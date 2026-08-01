"""Postgres-backed store — the production path.

Interface-compatible with `UserDataStore`, so nothing downstream changes: the
pipeline, the chart tools and the profile builder all keep calling
`get_user_frame(...)` and receive the same annotated DataFrame.

What changes is where the filtering happens. The DataFrame backend holds every
tenant's rows in process memory and slices them; this one pushes user, window
and category into SQL and materialises only the result. Process memory becomes a
function of *one user's window* — hundreds of rows — instead of the corpus. That
is the difference between 39 GB per worker at 100M rows and a few megabytes.

Scoped to a single tenant by construction. There is no method that can return
another tenant's rows, because `tenant_id` is bound at `__init__` and appended
to every WHERE clause rather than being a caller-supplied argument.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import pandas as pd
from sqlalchemy import and_, distinct, func, select

from ..db.schema import app_users, tenants, transactions
from .aggregates import FrameAggregates
from .category_taxonomy import CategoryTaxonomy
from .periods import Period
from .roster import SqlRoster
from .user_data_store import UnknownUserError

log = logging.getLogger("transaction_rag.sql_store")

# Frames are per-user-per-window and normally small. This is the backstop for
# the case that isn't: a pathological account whose "all time" window would
# otherwise pull millions of rows into one request's memory.
DEFAULT_MAX_ROWS = 250_000

# What the query returns, in order.
SELECTED_COLUMNS = (
    "user_id",
    "transaction_date",
    "transaction_amount",
    "transaction_category_detail",
    "merchant_name",
    "subcategory",
    "parent_category",
    "is_income",
    "expense_amount",
    "income_amount",
)

# What callers receive: the above plus the denormalised `user_name`, matching
# the column set the DataFrame backend hands out.
FRAME_COLUMNS = ("user_id", "user_name", *SELECTED_COLUMNS[1:])


class _TTLValue:
    """One cached value with an expiry. Guards the per-tenant metadata reads."""

    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, expires_at: float):
        self.value = value
        self.expires_at = expires_at


class SqlUserDataStore(FrameAggregates):
    """Tenant-scoped, query-per-turn transaction access."""

    def __init__(
        self,
        engine,
        tenant_id: str,
        as_of: Optional[pd.Timestamp] = None,
        as_of_mode: str = "data_max",
        metadata_ttl_s: int = 300,
        max_rows: int = DEFAULT_MAX_ROWS,
    ):
        self.engine = engine
        self.tenant_id = tenant_id
        self.max_rows = int(max_rows)
        self._metadata_ttl_s = int(metadata_ttl_s)
        self._explicit_as_of = pd.Timestamp(as_of).normalize() if as_of is not None else None
        self._as_of_mode = as_of_mode
        self._lock = threading.Lock()
        self._cache: dict[str, _TTLValue] = {}

        # The taxonomy is derived from this tenant's own category vocabulary, so
        # two tenants with different category sets each get schemas and prompts
        # describing their own data.
        self.taxonomy = self._load_taxonomy()

    # -- small TTL cache over per-tenant metadata -----------------------------

    def _cached(self, key: str, producer):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit.expires_at > now:
                return hit.value
        value = producer()
        with self._lock:
            self._cache[key] = _TTLValue(value, now + self._metadata_ttl_s)
        return value

    def invalidate_metadata(self) -> None:
        """Call after an ingest: the anchor, roster and taxonomy may have moved."""
        with self._lock:
            self._cache.clear()
        self.taxonomy = self._load_taxonomy()

    # -- tenant metadata ------------------------------------------------------

    def _load_taxonomy(self) -> CategoryTaxonomy:
        statement = select(distinct(transactions.c.transaction_category_detail)).where(
            transactions.c.tenant_id == self.tenant_id
        )
        with self.engine.connect() as conn:
            details = [row[0] for row in conn.execute(statement) if row[0]]
        return CategoryTaxonomy(details)

    def _resolved_as_of_mode(self) -> str:
        """Tenant row wins over the process default, when it sets one."""

        def load() -> str:
            statement = select(tenants.c.as_of_mode).where(tenants.c.tenant_id == self.tenant_id)
            with self.engine.connect() as conn:
                row = conn.execute(statement).first()
            return (row[0] if row and row[0] else self._as_of_mode) or "data_max"

        return self._cached("as_of_mode", load)

    @property
    def as_of(self) -> pd.Timestamp:
        """Anchor for relative dates.

        `now` is correct for live client data — "last month" must mean the
        calendar month just gone. `data_max` is correct for a frozen historical
        upload, where wall-clock time is past the end of the data and every
        relative window would come back empty.
        """
        if self._explicit_as_of is not None:
            return self._explicit_as_of

        if self._resolved_as_of_mode() == "now":
            return pd.Timestamp.now().normalize()

        def load() -> Optional[pd.Timestamp]:
            statement = select(func.max(transactions.c.transaction_date)).where(
                transactions.c.tenant_id == self.tenant_id
            )
            with self.engine.connect() as conn:
                value = conn.execute(statement).scalar()
            return pd.Timestamp(value).normalize() if value is not None else None

        anchor = self._cached("as_of", load)
        return anchor if anchor is not None else pd.Timestamp.now().normalize()

    # -- identity -------------------------------------------------------------

    @property
    def user_ids(self) -> tuple[str, ...]:
        """Bounded page of this tenant's users.

        Deliberately capped. Callers that genuinely need every user (an export,
        an admin listing) should page `iter_users`; the guardrail path uses a
        roster lookup instead of this list precisely so that nothing on the hot
        path depends on materialising the whole roster.
        """
        return tuple(uid for uid, _ in self.list_users(limit=1000))

    @property
    def user_names(self) -> tuple[str, ...]:
        return tuple(name for _, name in self.list_users(limit=1000))

    def list_users(self, limit: int = 100, offset: int = 0) -> list[tuple[str, str]]:
        statement = (
            select(app_users.c.user_id, app_users.c.user_name)
            .where(app_users.c.tenant_id == self.tenant_id)
            .order_by(app_users.c.user_id)
            .limit(limit)
            .offset(offset)
        )
        with self.engine.connect() as conn:
            return [(row[0], row[1]) for row in conn.execute(statement)]

    def user_count(self) -> int:
        statement = select(func.count()).select_from(app_users).where(
            app_users.c.tenant_id == self.tenant_id
        )
        with self.engine.connect() as conn:
            return int(conn.execute(statement).scalar() or 0)

    def make_roster(self) -> SqlRoster:
        """Cross-user lookup for the guardrail — indexed, never materialised."""
        return SqlRoster(self.engine, self.tenant_id)

    def validate_user(self, user_id: str) -> bool:
        statement = (
            select(app_users.c.user_id)
            .where(app_users.c.tenant_id == self.tenant_id, app_users.c.user_id == user_id)
            .limit(1)
        )
        with self.engine.connect() as conn:
            return conn.execute(statement).first() is not None

    def user_name(self, user_id: str) -> str:
        statement = (
            select(app_users.c.user_name)
            .where(app_users.c.tenant_id == self.tenant_id, app_users.c.user_id == user_id)
            .limit(1)
        )
        with self.engine.connect() as conn:
            row = conn.execute(statement).first()
        if row is None:
            raise UnknownUserError(user_id)
        return str(row[0])

    # -- slicing --------------------------------------------------------------

    def get_user_frame(
        self,
        user_id: str,
        period: Optional[Period] = None,
        parent_category: Optional[str] = None,
        subcategory: Optional[str] = None,
        include_income: bool = True,
        include_expenses: bool = True,
    ) -> pd.DataFrame:
        """This user's rows, filtered in SQL, as the frame the pipeline expects."""
        if not self.validate_user(user_id):
            raise UnknownUserError(user_id)

        # tenant_id first and always -- the filter that makes cross-tenant
        # access impossible rather than merely unlikely.
        clauses = [
            transactions.c.tenant_id == self.tenant_id,
            transactions.c.user_id == user_id,
        ]

        if period is not None and not period.is_unbounded:
            if period.start is not None:
                clauses.append(transactions.c.transaction_date >= period.start.to_pydatetime())
            if period.end is not None:
                clauses.append(transactions.c.transaction_date <= period.end.to_pydatetime())
        if parent_category:
            normalized = self.taxonomy.normalize_parent(parent_category)
            if normalized:
                clauses.append(transactions.c.parent_category == normalized)
        if subcategory:
            clauses.append(transactions.c.subcategory == str(subcategory).strip().upper())
        if not include_income:
            clauses.append(transactions.c.is_income.is_(False))
        if not include_expenses:
            clauses.append(transactions.c.is_income.is_(True))

        statement = (
            select(
                transactions.c.user_id,
                transactions.c.transaction_date,
                transactions.c.transaction_amount,
                transactions.c.transaction_category_detail,
                transactions.c.merchant_name,
                transactions.c.subcategory,
                transactions.c.parent_category,
                transactions.c.is_income,
                transactions.c.expense_amount,
                transactions.c.income_amount,
            )
            .where(and_(*clauses))
            .order_by(transactions.c.transaction_date)
            .limit(self.max_rows + 1)
        )

        with self.engine.connect() as conn:
            rows = conn.execute(statement).fetchall()
        frame = pd.DataFrame(rows, columns=list(SELECTED_COLUMNS))

        if len(frame) > self.max_rows:
            log.warning(
                "user %s hit the %s-row cap; frame truncated", user_id, f"{self.max_rows:,}"
            )
            frame = frame.iloc[: self.max_rows]

        return self._coerce(frame, user_id)

    def _coerce(self, frame: pd.DataFrame, user_id: str) -> pd.DataFrame:
        """Give the frame the exact dtypes the DataFrame backend produces."""
        if frame.empty:
            frame = pd.DataFrame(columns=list(FRAME_COLUMNS))
            frame["transaction_date"] = pd.to_datetime(frame["transaction_date"])
            frame["is_income"] = frame["is_income"].astype(bool)
            for column in ("transaction_amount", "expense_amount", "income_amount"):
                frame[column] = frame[column].astype(float)
            return frame

        frame["transaction_date"] = pd.to_datetime(frame["transaction_date"])
        frame["is_income"] = frame["is_income"].astype(bool)
        for column in ("transaction_amount", "expense_amount", "income_amount"):
            frame[column] = frame[column].astype(float)
        # `user_name` is denormalised onto the frame because the chart titles and
        # profile read it from there, matching the source spreadsheet's shape.
        frame.insert(1, "user_name", self.user_name(user_id))
        return frame.reset_index(drop=True)

    def date_range(self, user_id: str) -> tuple[pd.Timestamp, pd.Timestamp]:
        statement = select(
            func.min(transactions.c.transaction_date),
            func.max(transactions.c.transaction_date),
        ).where(
            transactions.c.tenant_id == self.tenant_id,
            transactions.c.user_id == user_id,
        )
        with self.engine.connect() as conn:
            low, high = conn.execute(statement).first() or (None, None)
        if low is None or high is None:
            anchor = self.as_of
            return (anchor, anchor)
        return (pd.Timestamp(low), pd.Timestamp(high))


__all__ = ["SqlUserDataStore", "UnknownUserError"]
