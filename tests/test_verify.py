"""The cross-check itself, and the guarantee it exists to provide.

`test_every_reported_figure_matches_the_spreadsheet` is the regression gate: if
any change to the data layer, the taxonomy, the period resolver or a chart
function starts producing a number that isn't in the file, this fails.
"""

from __future__ import annotations

import pandas as pd
import pytest

from demo import offline_client
from src.observability.verify import _window, verify_result, verify_summary
from src.pipeline import TransactionRAGPipeline


@pytest.fixture
def raw(raw_df) -> pd.DataFrame:
    return raw_df


@pytest.fixture
def routed(raw_df, settings) -> TransactionRAGPipeline:
    """A pipeline whose scripted client actually *chooses* a tool per prompt.

    The shared `pipeline` fixture returns plain text with no tool calls, so it
    produces no figures to cross-check. This uses the same router `demo.py` and
    `verify_data.py` use, so the test exercises the real dispatch path.
    """
    return TransactionRAGPipeline(df=raw_df, settings=settings, llm_client=offline_client())


PROMPTS = [
    "What did I spend the most on last month?",
    "Show me my spending trend",
    "Am I saving money?",
    "Show me my food spending",
    "Show me my top merchants",
    "Did I spend more in November than October?",
]


def test_every_reported_figure_matches_the_spreadsheet(routed, raw):
    """Two independent computations of the same figures must agree."""
    checked = 0
    failures = []

    for user_id in routed.store.user_ids:
        for prompt in PROMPTS:
            result = routed.run(user_id, prompt)
            for report in verify_result(raw, result):
                checked += len(report.checks)
                failures.extend(
                    f"{user_id} {report.tool} {check.name}: "
                    f"reported={check.reported} recomputed={check.recomputed} {check.detail}"
                    for check in report.failures
                )

    assert checked > 50, "the cross-check exercised suspiciously few figures"
    assert not failures, "figures disagree with the raw data:\n" + "\n".join(failures)


# -- the checker's own correctness -------------------------------------------


def test_window_parses_single_month_and_range(raw):
    single = _window(raw, "2025-11")
    dates = pd.to_datetime(single["transaction_date"])
    assert dates.min() >= pd.Timestamp("2025-11-01")
    assert dates.max() <= pd.Timestamp("2025-11-30 23:59:59")

    ranged = _window(raw, "2025-07..2025-12")
    dates = pd.to_datetime(ranged["transaction_date"])
    assert dates.min() >= pd.Timestamp("2025-07-01")
    assert dates.max() <= pd.Timestamp("2025-12-31 23:59:59")
    assert len(ranged) > len(single)


def test_window_refuses_an_unrecognised_label(raw):
    """Silently widening to the whole frame would report failures that aren't
    real -- which is exactly the bug this check exists to prevent."""
    with pytest.raises(ValueError, match="unrecognised period label"):
        _window(raw, "2025-07:2025-12")


def test_checker_actually_catches_a_wrong_number(routed, raw):
    """A checker that never fails proves nothing. Corrupt a figure and confirm
    the mismatch is reported."""
    user_id = routed.store.user_ids[0]
    result = routed.run(user_id, "What did I spend the most on last month?")
    summary = dict(result["data_summary"]["plot_category_breakdown"])
    summary["total_spend"] = float(summary["total_spend"]) + 500.0

    report = verify_summary(raw, user_id, "plot_category_breakdown", summary)
    assert not report.ok
    assert any(check.name == "total_spend" for check in report.failures)


def test_sign_convention_is_asserted(routed, raw):
    """Negative = income is the most dangerous assumption in this dataset."""
    user_id = routed.store.user_ids[0]
    result = routed.run(user_id, "Am I saving money?")
    report = next(
        r for r in verify_result(raw, result) if r.tool == "plot_income_vs_expense"
    )
    sign = next(c for c in report.checks if c.name == "sign_convention")
    assert sign.ok
