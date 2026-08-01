"""Derived category hierarchy.

The brief describes `transaction_category_detail` as hierarchical
("Food > Restaurants > Fast Food"). The delivered data is flat and uses a
`SUBCATEGORY_PARENT` convention instead (`RENT_HOUSING`, `FASTFOOD_FOOD`,
`SALARY_INCOME`). Verified across all 27 distinct values in the file: none
contains more than one underscore, so `rsplit("_", 1)` is unambiguous.

The vocabulary is *computed from the data*, never declared as an enum, so a
future data refresh introducing a new category needs no code change.
"""

from __future__ import annotations

from typing import Iterable, Optional

import pandas as pd

# Words a user (or the LLM) might use for a parent category that don't match
# the literal token. Kept small and obvious on purpose.
_PARENT_ALIASES = {
    "GROCERY": "FOOD",
    "GROCERIES": "FOOD",
    "DINING": "FOOD",
    "RESTAURANTS": "FOOD",
    "EATING": "FOOD",
    "EATINGOUT": "FOOD",
    "RENT": "HOUSING",
    "HOME": "HOUSING",
    "UTILITY": "HOUSING",
    "UTILITIES": "HOUSING",
    "TRANSPORTATION": "TRANSPORT",
    "COMMUTE": "TRANSPORT",
    "CAR": "TRANSPORT",
    "GAS": "TRANSPORT",
    "MEDICAL": "HEALTH",
    "FITNESS": "HEALTH",
    "WELLNESS": "HEALTH",
    "SALARY": "INCOME",
    "EARNINGS": "INCOME",
    "PAY": "INCOME",
    "PAYCHECK": "INCOME",
    "SHOPS": "SHOPPING",
    "RETAIL": "SHOPPING",
    "CLOTHES": "SHOPPING",
    "FUN": "ENTERTAINMENT",
    "LEISURE": "ENTERTAINMENT",
    "VACATION": "TRAVEL",
    "TRIPS": "TRAVEL",
    "SCHOOL": "EDUCATION",
    "LEARNING": "EDUCATION",
    "PET": "PETS",
    "INSURANCE": "FINANCE",
    "BANKING": "FINANCE",
    "FEES": "FINANCE",
}


class CategoryTaxonomy:
    """Splits `SUBCATEGORY_PARENT` details and rolls them up to parents."""

    SEPARATOR = "_"

    def __init__(self, details: Iterable[str]):
        details = sorted({str(d) for d in details if pd.notna(d)})
        self._details = tuple(details)

        children: dict[str, set[str]] = {}
        for detail in details:
            sub, parent = self.split(detail)
            children.setdefault(parent, set()).add(sub)

        self._parents = tuple(sorted(children))
        self._children = {p: tuple(sorted(subs)) for p, subs in children.items()}

        # Reverse index for normalization: every token that may legitimately
        # resolve to a parent (parent name, any subcategory, any alias).
        lookup: dict[str, str] = {p: p for p in self._parents}
        for parent, subs in self._children.items():
            for sub in subs:
                lookup.setdefault(sub, parent)
        for alias, parent in _PARENT_ALIASES.items():
            if parent in self._parents:
                lookup.setdefault(alias, parent)
        self._lookup = lookup

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_frame(cls, df: pd.DataFrame, column: str = "transaction_category_detail") -> "CategoryTaxonomy":
        return cls(df[column].dropna().unique())

    # -- vocabulary -----------------------------------------------------------

    @property
    def parents(self) -> tuple[str, ...]:
        return self._parents

    @property
    def details(self) -> tuple[str, ...]:
        return self._details

    @property
    def spend_parents(self) -> tuple[str, ...]:
        """Parents excluding INCOME -- the ones that mean 'money going out'."""
        return tuple(p for p in self._parents if p != "INCOME")

    def subcategories(self, parent: str) -> tuple[str, ...]:
        return self._children.get(parent.upper(), ())

    # -- splitting ------------------------------------------------------------

    @classmethod
    def split(cls, detail: str) -> tuple[str, str]:
        """`RENT_HOUSING` -> `("RENT", "HOUSING")`.

        A detail with no separator is treated as its own parent with an
        `UNSPECIFIED` subcategory, so malformed rows degrade instead of raising.
        """
        text = str(detail).strip().upper()
        if cls.SEPARATOR not in text:
            return ("UNSPECIFIED", text)
        sub, parent = text.rsplit(cls.SEPARATOR, 1)
        return (sub, parent)

    def parent_of(self, detail: str) -> str:
        return self.split(detail)[1]

    def normalize_parent(self, raw: Optional[str]) -> Optional[str]:
        """Best-effort map of free text to a known parent category.

        Returns None when the text matches nothing -- callers treat that as
        "no filter" rather than erroring, since the source is usually the LLM.
        """
        if raw is None:
            return None
        token = str(raw).strip().upper()
        if not token or token in {"NULL", "NONE", "ALL", "ANY", ""}:
            return None
        # Separators become underscores (not deleted) so "food spending" can be
        # split into words below rather than collapsing to "FOODSPENDING".
        token = "".join(ch if ch.isalnum() else "_" for ch in token).strip("_")
        if token in self._lookup:
            return self._lookup[token]
        # `FOOD_SPENDING`, `MY_FOOD`, ... -- try the individual words.
        for part in token.split("_"):
            if part in self._lookup:
                return self._lookup[part]
        # Singular/plural nudge.
        for candidate in (token.rstrip("S"), token + "S"):
            if candidate in self._lookup:
                return self._lookup[candidate]
        return None

    # -- frame helpers --------------------------------------------------------

    def annotate(self, df: pd.DataFrame, column: str = "transaction_category_detail") -> pd.DataFrame:
        """Attach `subcategory` / `parent_category` columns (returns a copy)."""
        out = df.copy()
        parts = out[column].map(self.split)
        out["subcategory"] = [p[0] for p in parts]
        out["parent_category"] = [p[1] for p in parts]
        return out

    def rollup(
        self,
        df: pd.DataFrame,
        top_n: int = 7,
        value_column: str = "transaction_amount",
        group_column: str = "parent_category",
    ) -> pd.DataFrame:
        """Sum by category, keeping the top N and bucketing the rest as `Other`.

        Returns a two-column frame [`category`, `amount`] sorted descending.
        """
        if df.empty:
            return pd.DataFrame({"category": [], "amount": []})

        totals = (
            df.groupby(group_column, observed=True)[value_column]
            .sum()
            .sort_values(ascending=False)
        )
        totals = totals[totals > 0]
        if totals.empty:
            return pd.DataFrame({"category": [], "amount": []})

        top_n = max(1, int(top_n))
        head = totals.head(top_n)
        tail_total = float(totals.iloc[top_n:].sum())

        rows = [{"category": str(k), "amount": float(v)} for k, v in head.items()]
        if tail_total > 0:
            rows.append({"category": "Other", "amount": tail_total})
        return pd.DataFrame(rows)

    def describe_for_prompt(self) -> str:
        """Compact vocabulary listing injected into the system prompt."""
        lines = []
        for parent in self._parents:
            subs = ", ".join(self._children[parent])
            lines.append(f"  {parent}: {subs}")
        return "\n".join(lines)
