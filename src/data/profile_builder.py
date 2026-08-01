"""Builds the payload cached at `user:{id}:profile`.

This is the "system already knows them" part of the brief: computed once,
cached for 24h, injected into every subsequent prompt without recomputation.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from .user_data_store import UserDataStore


class ProfileBuilder:
    def __init__(self, store: UserDataStore):
        self.store = store

    def build(self, user_id: str) -> dict:
        frame = self.store.get_user_frame(user_id)
        totals = self.store.totals(frame)
        monthly = self.store.monthly_totals(frame)

        n_months = max(len(monthly), 1)
        start, end = self.store.date_range(user_id)

        return {
            "user_id": user_id,
            "user_name": self.store.user_name(user_id),
            "date_range": [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")],
            "transaction_count": totals["transaction_count"],
            "months_observed": n_months,
            "top_categories": self.store.top_categories(frame, n=5),
            "top_merchants": self.store.top_merchants(frame, n=5),
            "total_expense": totals["total_expense"],
            "total_income": totals["total_income"],
            "net_savings": totals["net_savings"],
            "avg_monthly_spend": round(totals["total_expense"] / n_months, 2),
            "avg_monthly_income": round(totals["total_income"] / n_months, 2),
            "busiest_month": self._busiest_month(monthly),
            "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @staticmethod
    def _busiest_month(monthly: pd.DataFrame) -> dict | None:
        if monthly.empty:
            return None
        row = monthly.loc[monthly["expense"].idxmax()]
        return {"month": str(row["month"]), "expense": round(float(row["expense"]), 2)}

    @staticmethod
    def summarize_for_prompt(profile: dict) -> str:
        """Compact, token-cheap rendering for the system prompt."""
        cats = ", ".join(f"{name} ${amount:,.0f}" for name, amount in profile.get("top_categories", [])[:5])
        start, end = profile.get("date_range", ["?", "?"])
        lines = [
            f"Name: {profile.get('user_name')}",
            f"History: {start} to {end} ({profile.get('transaction_count')} transactions across {profile.get('months_observed')} months)",
            f"Average monthly spend: ${profile.get('avg_monthly_spend', 0):,.2f}",
            f"Average monthly income: ${profile.get('avg_monthly_income', 0):,.2f}",
            f"Top spending categories all-time: {cats or 'none'}",
        ]
        busiest = profile.get("busiest_month")
        if busiest:
            lines.append(f"Highest-spend month: {busiest['month']} (${busiest['expense']:,.2f})")
        return "\n".join(lines)
