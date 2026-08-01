"""Per-user access to the pre-loaded transactions DataFrame.

This class is the *structural* half of cross-user leakage prevention: nothing
downstream of `get_user_frame` ever holds a frame containing more than one
user's rows. The prompt-level check in `guardrails/input_guardrails.py` is the
other half; either alone would be insufficient.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .aggregates import FrameAggregates
from .category_taxonomy import CategoryTaxonomy
from .periods import Period
from .roster import InMemoryRoster


class UnknownUserError(KeyError):
    """Raised only at the boundary; the pipeline converts it to a structured error."""


class UserDataStore(FrameAggregates):
    REQUIRED_COLUMNS = (
        "user_id",
        "user_name",
        "transaction_date",
        "transaction_amount",
        "transaction_category_detail",
        "merchant_name",
    )

    def __init__(self, df: pd.DataFrame, as_of: Optional[pd.Timestamp] = None):
        missing = [c for c in self.REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"DataFrame is missing required columns: {missing}")

        frame = df.copy()
        frame["transaction_date"] = pd.to_datetime(frame["transaction_date"])
        frame["transaction_amount"] = pd.to_numeric(frame["transaction_amount"], errors="coerce")
        frame = frame.dropna(subset=["user_id", "transaction_date", "transaction_amount"])

        self.taxonomy = CategoryTaxonomy.from_frame(frame)
        frame = self.taxonomy.annotate(frame)

        # Sign convention (verified against the data: all 54 negative rows are
        # *_INCOME, and no *_INCOME row is positive):
        #   negative amount == income, positive amount == expense.
        frame["is_income"] = frame["transaction_amount"] < 0
        # Float, not the source dtype. This dataset's amounts happen to be whole
        # numbers, so these would otherwise land as int64 here and float64 from
        # SQL -- the same figures with different types, which is the sort of
        # divergence that surfaces much later as a confusing comparison failure.
        # Money is fractional in general; float is the honest type for both.
        frame["transaction_amount"] = frame["transaction_amount"].astype(float)
        frame["expense_amount"] = frame["transaction_amount"].clip(lower=0).astype(float)
        frame["income_amount"] = (-frame["transaction_amount"]).clip(lower=0).astype(float)

        self._df = frame.sort_values(["user_id", "transaction_date"]).reset_index(drop=True)
        self._users = (
            self._df.drop_duplicates("user_id").set_index("user_id")["user_name"].astype(str).to_dict()
        )
        self._as_of = pd.Timestamp(as_of).normalize() if as_of is not None else self._df["transaction_date"].max().normalize()

    # -- identity -------------------------------------------------------------

    @property
    def as_of(self) -> pd.Timestamp:
        """Anchor for every relative date expression. See `periods.py`."""
        return self._as_of

    @property
    def user_ids(self) -> tuple[str, ...]:
        return tuple(self._users)

    @property
    def user_names(self) -> tuple[str, ...]:
        return tuple(self._users.values())

    def make_roster(self) -> InMemoryRoster:
        """Cross-user lookup for the guardrail. Eager here: the roster is small
        and already in memory, so a set is strictly better than a query."""
        return InMemoryRoster(self._users.keys(), self._users.values())

    def validate_user(self, user_id: str) -> bool:
        return user_id in self._users

    def user_name(self, user_id: str) -> str:
        if not self.validate_user(user_id):
            raise UnknownUserError(user_id)
        return self._users[user_id]

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
        """Return a **copy** of this user's rows, optionally filtered.

        Always a copy: callers (chart functions, aggregations) mutate freely and
        can never write back into the shared frame.
        """
        if not self.validate_user(user_id):
            raise UnknownUserError(user_id)

        frame = self._df[self._df["user_id"] == user_id]

        if period is not None and not period.is_unbounded:
            frame = frame[period.mask(frame["transaction_date"])]
        if parent_category:
            normalized = self.taxonomy.normalize_parent(parent_category)
            if normalized:
                frame = frame[frame["parent_category"] == normalized]
        if subcategory:
            frame = frame[frame["subcategory"] == str(subcategory).strip().upper()]
        if not include_income:
            frame = frame[~frame["is_income"]]
        if not include_expenses:
            frame = frame[frame["is_income"]]

        return frame.copy()

    def date_range(self, user_id: str) -> tuple[pd.Timestamp, pd.Timestamp]:
        frame = self.get_user_frame(user_id)
        if frame.empty:
            return (self._as_of, self._as_of)
        return (frame["transaction_date"].min(), frame["transaction_date"].max())

    # -- aggregates used by profile, charts and the hallucination check --------
    # Inherited from `FrameAggregates`, shared with the SQL backend so the two
    # cannot compute a figure differently.
