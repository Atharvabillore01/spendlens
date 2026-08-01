"""Frame-level aggregates shared by every storage backend.

These operate on an already-filtered, single-user frame, so they are identical
whether those rows came from an Excel file or a Postgres query. Keeping one copy
is the point: the hallucination check compares the model's prose against these
numbers, so two backends computing "total spend" even slightly differently would
make groundedness depend on where the data happened to be stored.
"""

from __future__ import annotations

import pandas as pd


def totals(frame: pd.DataFrame) -> dict[str, float]:
    expense = float(frame["expense_amount"].sum())
    income = float(frame["income_amount"].sum())
    return {
        "total_expense": round(expense, 2),
        "total_income": round(income, 2),
        "net_savings": round(income - expense, 2),
        "transaction_count": int(len(frame)),
    }


def monthly_totals(frame: pd.DataFrame) -> pd.DataFrame:
    """Expense / income / net aggregated per calendar month."""
    if frame.empty:
        return pd.DataFrame(columns=["month", "expense", "income", "net"])
    grouped = (
        frame.assign(month=frame["transaction_date"].dt.to_period("M"))
        .groupby("month", observed=True)[["expense_amount", "income_amount"]]
        .sum()
        .sort_index()
    )
    out = grouped.reset_index()
    out["month"] = out["month"].astype(str)
    out = out.rename(columns={"expense_amount": "expense", "income_amount": "income"})
    out["net"] = out["income"] - out["expense"]
    return out


def top_by(frame: pd.DataFrame, group_column: str, n: int = 5) -> list[tuple[str, float]]:
    """Top spend by any grouping column as [(name, amount)], income excluded."""
    expenses = frame[~frame["is_income"]]
    if expenses.empty:
        return []
    grouped = (
        expenses.groupby(group_column, observed=True)["expense_amount"]
        .sum()
        .sort_values(ascending=False)
        .head(n)
    )
    return [(str(k), round(float(v), 2)) for k, v in grouped.items()]


class FrameAggregates:
    """Mixin giving a store the aggregate methods the pipeline calls.

    `totals` and `monthly_totals` stay static so both `store.totals(frame)` and
    `UserDataStore.totals(frame)` keep working.
    """

    totals = staticmethod(totals)
    monthly_totals = staticmethod(monthly_totals)

    def top_categories(
        self,
        frame: pd.DataFrame,
        n: int = 5,
        group_column: str = "parent_category",
    ) -> list[tuple[str, float]]:
        return top_by(frame, group_column, n)

    def top_merchants(self, frame: pd.DataFrame, n: int = 5) -> list[tuple[str, float]]:
        return top_by(frame, "merchant_name", n)


__all__ = ["totals", "monthly_totals", "top_by", "FrameAggregates"]
