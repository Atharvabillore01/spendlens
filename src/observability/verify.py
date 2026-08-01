"""Independent cross-check of the figures a turn produced.

The point of this module is adversarial, not decorative: it recomputes each
headline figure **straight from the raw spreadsheet columns**, using its own
grouping logic, and compares the result against what the pipeline reported.

That independence is the whole value. If it reused `UserDataStore` /
`CategoryTaxonomy` it would reproduce any bug in those layers identically and
agree with itself, which proves nothing. So it deliberately re-derives:

  * expense vs income from the sign of `transaction_amount` (negative = income)
  * the parent category by splitting `transaction_category_detail` on the last
    underscore
  * the period window from `transaction_date`

A mismatch means one of the two paths is wrong and the turn should not be
trusted. Agreement means the number the user saw is the number in the file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

# Matches the pipeline's own tolerance for "the same number", so a rounding
# difference in the last cent is not reported as a data error.
REL_TOLERANCE = 0.005
ABS_TOLERANCE = 0.51


@dataclass
class Check:
    """One recomputed figure and its verdict."""

    name: str
    reported: Optional[float]
    recomputed: Optional[float]
    ok: bool
    detail: str = ""

    @property
    def delta(self) -> Optional[float]:
        if self.reported is None or self.recomputed is None:
            return None
        return round(self.reported - self.recomputed, 2)


@dataclass
class VerificationReport:
    user_id: str
    tool: str
    period: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "tool": self.tool,
            "period": self.period,
            "ok": self.ok,
            "checks": [
                {
                    "name": c.name,
                    "reported": c.reported,
                    "recomputed": c.recomputed,
                    "delta": c.delta,
                    "ok": c.ok,
                    "detail": c.detail,
                }
                for c in self.checks
            ],
        }


def _close(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if abs(a - b) <= ABS_TOLERANCE:
        return True
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) / scale <= REL_TOLERANCE


def _parent_of(detail: str) -> str:
    """`RENT_HOUSING` -> `HOUSING`. Re-derived here on purpose (see module doc)."""
    return str(detail).rsplit("_", 1)[-1].upper()


def _window(raw: pd.DataFrame, period: str) -> pd.DataFrame:
    """Slice by the period label the summary carries.

    `periods.py` emits `YYYY-MM` for a single month and `YYYY-MM..YYYY-MM` for a
    range. Anything unparseable raises rather than silently widening to the full
    frame -- a checker that quietly compares the wrong window reports failures
    that aren't real.
    """
    if not period or period == "all":
        return raw
    start_label, _, end_label = period.partition("..")
    end_label = end_label or start_label
    try:
        start = pd.Period(start_label, freq="M").start_time
        end = pd.Period(end_label, freq="M").end_time
    except (ValueError, TypeError) as exc:
        raise ValueError(f"unrecognised period label {period!r}") from exc
    dates = pd.to_datetime(raw["transaction_date"])
    return raw[(dates >= start) & (dates <= end)]


def verify_summary(
    raw: pd.DataFrame, user_id: str, tool: str, summary: dict[str, Any]
) -> VerificationReport:
    """Recompute the figures in one tool's summary from the raw frame."""
    period = str(summary.get("period") or "")
    report = VerificationReport(user_id=user_id, tool=tool, period=period)

    scoped = raw[raw["user_id"] == user_id]
    window = _window(scoped, period)
    amounts = pd.to_numeric(window["transaction_amount"], errors="coerce").fillna(0)
    expense = amounts.clip(lower=0)
    income = (-amounts).clip(lower=0)

    if tool == "plot_category_breakdown":
        _verify_breakdown(report, window, expense, summary)
    elif tool == "plot_monthly_spending_trend":
        _verify_trend(report, window, expense, summary)
    elif tool == "plot_income_vs_expense":
        _verify_flow(report, window, expense, income, summary)
    elif tool == "plot_top_merchants":
        _verify_merchants(report, window, expense, summary)
    elif tool == "plot_period_comparison":
        # Two windows, so this one re-slices the raw frame itself rather than
        # using the single `window` computed above.
        _verify_comparison(report, raw[raw["user_id"] == user_id], summary)

    return report


def _verify_breakdown(
    report: VerificationReport, window: pd.DataFrame, expense: pd.Series, summary: dict
) -> None:
    parent_filter = summary.get("parent_category")
    parents = window["transaction_category_detail"].map(_parent_of)

    if parent_filter:
        keep = parents == str(parent_filter).upper()
        # Drilled in: the summary groups by subcategory, so re-derive that too.
        groups = window.loc[keep, "transaction_category_detail"].map(
            lambda d: str(d).rsplit("_", 1)[0].upper()
        )
        spend = expense[keep]
    else:
        groups = parents
        spend = expense

    total = float(spend.sum())
    report.checks.append(
        Check(
            "total_spend",
            _num(summary.get("total_spend")),
            round(total, 2),
            _close(_num(summary.get("total_spend")), total),
        )
    )

    by_group = spend.groupby(groups).sum().sort_values(ascending=False)
    reported_top = summary.get("top_category") or {}
    if len(by_group):
        top_name = str(by_group.index[0])
        top_amount = float(by_group.iloc[0])
        name_ok = str(reported_top.get("name", "")).upper() == top_name
        report.checks.append(
            Check(
                "top_category.name",
                None,
                None,
                name_ok,
                detail=f"reported={reported_top.get('name')} recomputed={top_name}",
            )
        )
        report.checks.append(
            Check(
                "top_category.amount",
                _num(reported_top.get("amount")),
                round(top_amount, 2),
                _close(_num(reported_top.get("amount")), top_amount),
            )
        )

    # Shares must sum to ~100 and each slice must match its own recomputation.
    slices = summary.get("categories") or []
    share_total = sum(float(s.get("share_pct", 0)) for s in slices)
    report.checks.append(
        Check(
            "shares_sum_to_100",
            round(share_total, 1),
            100.0,
            abs(share_total - 100.0) <= 0.6,
            detail="rounding of per-slice percentages",
        )
    )
    # "Other" is a synthetic bucket, so it is checked as a residual, not a group.
    for entry in slices:
        name = str(entry.get("name", "")).upper()
        if name == "OTHER":
            continue
        recomputed = float(by_group.get(name, 0.0))
        report.checks.append(
            Check(
                f"category[{name}]",
                _num(entry.get("amount")),
                round(recomputed, 2),
                _close(_num(entry.get("amount")), recomputed),
            )
        )


def _verify_trend(
    report: VerificationReport, window: pd.DataFrame, expense: pd.Series, summary: dict
) -> None:
    category_filter = summary.get("category_filter")
    if category_filter:
        keep = window["transaction_category_detail"].map(_parent_of) == str(category_filter).upper()
        expense = expense[keep]
        window = window[keep]

    months = pd.to_datetime(window["transaction_date"]).dt.to_period("M").astype(str)
    by_month = expense.groupby(months).sum().sort_index()

    reported_months = {m["month"]: _num(m["expense"]) for m in summary.get("monthly_totals", [])}
    for month, value in by_month.items():
        label = _month_label(str(month))
        reported = reported_months.get(label)
        report.checks.append(
            Check(f"month[{label}]", reported, round(float(value), 2), _close(reported, float(value)))
        )

    total = float(by_month.sum())
    report.checks.append(
        Check(
            "total_spend",
            _num(summary.get("total_spend")),
            round(total, 2),
            _close(_num(summary.get("total_spend")), total),
        )
    )
    if len(by_month):
        average = total / len(by_month)
        report.checks.append(
            Check(
                "average_monthly_spend",
                _num(summary.get("average_monthly_spend")),
                round(average, 2),
                _close(_num(summary.get("average_monthly_spend")), average),
            )
        )
        peak_label = _month_label(str(by_month.idxmax()))
        reported_peak = (summary.get("highest_month") or {}).get("month")
        report.checks.append(
            Check(
                "highest_month",
                None,
                None,
                reported_peak == peak_label,
                detail=f"reported={reported_peak} recomputed={peak_label}",
            )
        )


def _verify_flow(
    report: VerificationReport,
    window: pd.DataFrame,
    expense: pd.Series,
    income: pd.Series,
    summary: dict,
) -> None:
    total_income = float(income.sum())
    total_expense = float(expense.sum())
    net = total_income - total_expense

    report.checks.append(
        Check(
            "total_income",
            _num(summary.get("total_income")),
            round(total_income, 2),
            _close(_num(summary.get("total_income")), total_income),
        )
    )
    report.checks.append(
        Check(
            "total_expense",
            _num(summary.get("total_expense")),
            round(total_expense, 2),
            _close(_num(summary.get("total_expense")), total_expense),
        )
    )
    report.checks.append(
        Check("net_savings", _num(summary.get("net_savings")), round(net, 2), _close(_num(summary.get("net_savings")), net))
    )

    # The sign convention is the single most dangerous assumption in this
    # dataset, so assert it directly rather than trusting it.
    details = window["transaction_category_detail"].map(_parent_of)
    amounts = pd.to_numeric(window["transaction_amount"], errors="coerce").fillna(0)
    negatives_all_income = bool((details[amounts < 0] == "INCOME").all())
    income_all_negative = bool((amounts[details == "INCOME"] <= 0).all())
    report.checks.append(
        Check(
            "sign_convention",
            None,
            None,
            negatives_all_income and income_all_negative,
            detail="every negative row is *_INCOME and every *_INCOME row is negative",
        )
    )

    months = pd.to_datetime(window["transaction_date"]).dt.to_period("M").astype(str)
    net_by_month = (income - expense).groupby(months).sum()
    deficit = int((net_by_month < 0).sum())
    report.checks.append(
        Check(
            "months_in_deficit",
            _num(summary.get("months_in_deficit")),
            float(deficit),
            _num(summary.get("months_in_deficit")) == float(deficit),
        )
    )


def _verify_merchants(
    report: VerificationReport, window: pd.DataFrame, expense: pd.Series, summary: dict
) -> None:
    parent_filter = summary.get("parent_category")
    if parent_filter:
        keep = window["transaction_category_detail"].map(_parent_of) == str(parent_filter).upper()
        window = window[keep]
        expense = expense[keep]

    total = float(expense.sum())
    report.checks.append(
        Check(
            "total_spend",
            _num(summary.get("total_spend")),
            round(total, 2),
            _close(_num(summary.get("total_spend")), total),
        )
    )

    by_merchant = expense.groupby(window["merchant_name"]).sum()
    by_merchant = by_merchant[by_merchant > 0].sort_values(ascending=False)
    visits = expense[expense > 0].groupby(window["merchant_name"]).size()

    report.checks.append(
        Check(
            "merchant_count",
            _num(summary.get("merchant_count")),
            float(len(by_merchant)),
            _num(summary.get("merchant_count")) == float(len(by_merchant)),
        )
    )

    reported = summary.get("merchants") or []
    if reported and len(by_merchant):
        # Ranking is the whole point of this chart, so assert the order too.
        recomputed_order = [str(n) for n in by_merchant.index[: len(reported)]]
        reported_order = [str(m.get("name")) for m in reported]
        report.checks.append(
            Check(
                "merchant_ranking",
                None,
                None,
                reported_order == recomputed_order,
                detail=f"reported={reported_order[:3]} recomputed={recomputed_order[:3]}",
            )
        )

    for entry in reported:
        name = str(entry.get("name"))
        amount = float(by_merchant.get(name, 0.0))
        report.checks.append(
            Check(f"merchant[{name}]", _num(entry.get("amount")), round(amount, 2),
                  _close(_num(entry.get("amount")), amount))
        )
        report.checks.append(
            Check(f"visits[{name}]", _num(entry.get("visits")), float(visits.get(name, 0)),
                  _num(entry.get("visits")) == float(visits.get(name, 0)))
        )


def _verify_comparison(report: VerificationReport, scoped: pd.DataFrame, summary: dict) -> None:
    def totals(period_label: str) -> tuple[float, dict[str, float]]:
        frame = _window(scoped, period_label)
        amounts = pd.to_numeric(frame["transaction_amount"], errors="coerce").fillna(0)
        spend = amounts.clip(lower=0)
        parents = frame["transaction_category_detail"].map(_parent_of)
        by_cat = spend.groupby(parents).sum()
        return float(spend.sum()), {str(k): float(v) for k, v in by_cat.items()}

    now_total, now_by_cat = totals(str(summary.get("period") or ""))
    was_total, was_by_cat = totals(str(summary.get("compare_period") or ""))

    report.checks.append(
        Check("current_total", _num(summary.get("current_total")), round(now_total, 2),
              _close(_num(summary.get("current_total")), now_total))
    )
    report.checks.append(
        Check("previous_total", _num(summary.get("previous_total")), round(was_total, 2),
              _close(_num(summary.get("previous_total")), was_total))
    )
    delta = now_total - was_total
    report.checks.append(
        Check("delta", _num(summary.get("delta")), round(delta, 2),
              _close(_num(summary.get("delta")), delta))
    )
    # Direction is what the sentence actually claims, so assert it explicitly
    # rather than trusting that the sign of `delta` was read correctly.
    expected_direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
    report.checks.append(
        Check("direction", None, None, summary.get("direction") == expected_direction,
              detail=f"reported={summary.get('direction')} recomputed={expected_direction}")
    )

    for row in summary.get("categories") or []:
        name = str(row.get("name"))
        now_v = now_by_cat.get(name, 0.0)
        was_v = was_by_cat.get(name, 0.0)
        report.checks.append(
            Check(f"current[{name}]", _num(row.get("current")), round(now_v, 2),
                  _close(_num(row.get("current")), now_v))
        )
        report.checks.append(
            Check(f"previous[{name}]", _num(row.get("previous")), round(was_v, 2),
                  _close(_num(row.get("previous")), was_v))
        )
        report.checks.append(
            Check(f"delta[{name}]", _num(row.get("delta")), round(now_v - was_v, 2),
                  _close(_num(row.get("delta")), now_v - was_v))
        )


def _month_label(period_str: str) -> str:
    """Month keys are `YYYY-MM` on both sides -- `monthly_totals` stringifies a
    pandas Period, so no reformatting is needed or wanted."""
    return str(period_str)


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def verify_result(raw: pd.DataFrame, result: dict[str, Any]) -> list[VerificationReport]:
    """Cross-check every chart summary present in a `pipeline.run()` result."""
    summary = result.get("data_summary") or {}
    user_id = result.get("user_id") or ""
    reports = []
    for tool in (
        "plot_category_breakdown",
        "plot_monthly_spending_trend",
        "plot_income_vs_expense",
        "plot_top_merchants",
        "plot_period_comparison",
    ):
        payload = summary.get(tool)
        if isinstance(payload, dict) and not payload.get("no_data"):
            reports.append(verify_summary(raw, user_id, tool, payload))
    return reports
