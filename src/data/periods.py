"""Relative-date resolution.

Every "last month" / "last_3_months" / `months=6` in the system funnels through
`resolve_period` so there is exactly one definition of each phrase.

Why this module exists at all: the dataset ends 2025-12-31 but the process runs
at wall-clock "now". Anchoring relative dates to `datetime.now()` makes the
headline test query ("What did I spend the most on last month?") return an empty
frame. Everything therefore resolves against an explicit `as_of` anchor, which
defaults to max(transaction_date).

Semantics (documented, deliberately distinct):
  last_month      -> the previous *calendar* month relative to as_of
  this_month      -> as_of's calendar month
  last_N_months   -> trailing window of N calendar months, *including* as_of's
  ytd             -> Jan 1 of as_of's year .. as_of
  all             -> unbounded
  YYYY-MM         -> that exact calendar month
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import pandas as pd

_LAST_N_RE = re.compile(r"^(?:last|past|previous|trailing)_(\d{1,2})_months?$")
# Underscore as well as hyphen: `normalize_spec` rewrites separators before
# this pattern is applied, so "2025-07" arrives as "2025_07".
_YEAR_MONTH_RE = re.compile(r"^(\d{4})[-_](\d{1,2})$")


@dataclass(frozen=True)
class Period:
    """A closed date interval [start, end], both inclusive."""

    start: Optional[pd.Timestamp]
    end: Optional[pd.Timestamp]
    label: str
    spec: str

    def mask(self, dates: pd.Series) -> pd.Series:
        keep = pd.Series(True, index=dates.index)
        if self.start is not None:
            keep &= dates >= self.start
        if self.end is not None:
            keep &= dates <= self.end
        return keep

    @property
    def is_unbounded(self) -> bool:
        return self.start is None and self.end is None


def normalize_spec(spec: Optional[str]) -> str:
    """Coerce whatever the LLM produced into a canonical period spec.

    Free models emit `"last month"`, `"last_month"`, `"Last 3 Months"`,
    `"november"`, ... -- all of it lands here before it can reach the data.
    """
    if spec is None:
        return "last_3_months"
    s = str(spec).strip().lower()
    s = re.sub(r"^(the|for|in|during|over)\s+", "", s)
    s = re.sub(r"[\s\-/]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")

    aliases = {
        "": "last_3_months",
        "last": "last_month",
        "previous_month": "last_month",
        "prior_month": "last_month",
        "past_month": "last_month",
        "current_month": "this_month",
        "month": "this_month",
        "quarter": "last_3_months",
        "last_quarter": "last_3_months",
        "year": "last_12_months",
        "last_year": "last_12_months",
        "year_to_date": "ytd",
        "everything": "all",
        "all_time": "all",
        "full_history": "all",
        "lifetime": "all",
    }
    return aliases.get(s, s)


def month_bounds(ts: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = ts.normalize().replace(day=1)
    end = start + pd.offsets.MonthEnd(1)
    return start, end


def resolve_period(
    spec: Optional[str],
    as_of: pd.Timestamp,
    months: Optional[int] = None,
) -> Period:
    """Turn a period spec (or an integer month lookback) into a `Period`.

    `months` wins when supplied without an explicit spec -- that is the shape
    the `plot_monthly_spending_trend` / `plot_income_vs_expense` tools use.
    """
    as_of = pd.Timestamp(as_of).normalize()

    if spec is None and months is not None:
        spec = f"last_{int(months)}_months"
    canonical = normalize_spec(spec)

    if canonical == "all":
        return Period(None, None, "all time", "all")

    if canonical == "this_month":
        start, end = month_bounds(as_of)
        return Period(start, min(end, as_of), start.strftime("%Y-%m"), canonical)

    if canonical == "last_month":
        start, _ = month_bounds(as_of)
        prev_start = start - pd.offsets.MonthBegin(1)
        prev_end = start - pd.Timedelta(days=1)
        return Period(prev_start, prev_end, prev_start.strftime("%Y-%m"), canonical)

    if canonical == "ytd":
        start = as_of.replace(month=1, day=1)
        return Period(start, as_of, f"{as_of.year} YTD", canonical)

    m = _YEAR_MONTH_RE.match(canonical)
    if m:
        anchor = pd.Timestamp(year=int(m.group(1)), month=int(m.group(2)), day=1)
        start, end = month_bounds(anchor)
        return Period(start, end, start.strftime("%Y-%m"), canonical)

    m = _LAST_N_RE.match(canonical)
    if m:
        n = max(1, int(m.group(1)))
        cur_start, cur_end = month_bounds(as_of)
        start = cur_start - pd.offsets.MonthBegin(n - 1)
        end = min(cur_end, as_of)
        label = f"{start.strftime('%Y-%m')}..{end.strftime('%Y-%m')}" if n > 1 else start.strftime("%Y-%m")
        return Period(start, end, label, f"last_{n}_months")

    # Unrecognized spec: fall back to the documented default rather than
    # raising -- the LLM is not a trusted input source.
    return resolve_period("last_3_months", as_of)


def preceding_period(period: Period) -> Period:
    """The window of the same length immediately before `period`.

    "Did I spend more in November than October?" needs a baseline, and the only
    baseline that makes a comparison fair is one of equal length -- comparing a
    month against a quarter would make the quarter look alarming for no reason.
    Month-aligned windows step back whole months rather than a fixed number of
    days, so a 31-day month is compared against the 30-day one before it rather
    than against a 31-day slice straddling two.
    """
    if period.start is None or period.end is None:
        return Period(None, None, "all time", "all")

    start_month = period.start.to_period("M")
    end_month = period.end.to_period("M")
    span = (end_month - start_month).n + 1

    prev_end_month = start_month - 1
    prev_start_month = prev_end_month - (span - 1)

    start = prev_start_month.start_time.normalize()
    end = prev_end_month.end_time.normalize()
    label = (
        f"{start.strftime('%Y-%m')}..{end.strftime('%Y-%m')}"
        if span > 1
        else start.strftime("%Y-%m")
    )
    return Period(start, end, label, f"preceding_{span}_months")


def month_name(period: Period) -> str:
    """Human label for prose, e.g. 'November 2025' or 'October–December 2025'.

    Multi-month windows previously fell through to the machine label, so answers
    read "you spent $5,210.00 in 2025-10..2025-12". The wire format stays
    `period.label`; this is the string a person reads.
    """
    if period.start is None:
        return "your full history"
    if period.end is None:
        return period.start.strftime("%B %Y")
    if (period.end - period.start).days <= 31:
        return period.start.strftime("%B %Y")
    if period.start.year == period.end.year:
        return f"{period.start.strftime('%B')}–{period.end.strftime('%B %Y')}"
    return f"{period.start.strftime('%B %Y')}–{period.end.strftime('%B %Y')}"


def month_label(period_str: str) -> str:
    """`2025-07` -> `July 2025`. For the per-month keys inside a summary.

    The key itself stays `YYYY-MM` -- it is sorted, compared and cross-checked
    as a machine value. This is only ever the display form.
    """
    try:
        year, month = str(period_str).split("-")
        return f"{MONTHS[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return str(period_str)


MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
