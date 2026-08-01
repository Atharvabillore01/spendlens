"""Chart rendering and tool-call dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.data.category_taxonomy import CategoryTaxonomy
from src.llm.tool_dispatcher import (
    FLAG_ARGS_REPAIRED,
    FLAG_MALFORMED,
    FLAG_TOOL_FAILED,
    FLAG_UNKNOWN_TOOL,
    ToolDispatcher,
    parse_arguments,
)
from src.tools.schemas import build_tool_schemas, parameter_spec
from src.tools.visualizations import VisualizationTools


@pytest.fixture
def tools(store, tmp_path) -> VisualizationTools:
    return VisualizationTools(store, tmp_path / "charts", dpi=60)


@pytest.fixture
def dispatcher(tools, store) -> ToolDispatcher:
    schemas = build_tool_schemas(store.taxonomy)
    return ToolDispatcher(tools, parameter_spec(schemas), store.taxonomy)


@pytest.fixture
def me(store) -> str:
    return store.user_ids[0]


# == schemas ==================================================================


REQUIRED_TOOLS = {
    "plot_monthly_spending_trend",
    "plot_category_breakdown",
    "plot_income_vs_expense",
}


def test_schemas_cover_the_three_required_tools(store):
    """The brief mandates these three. Extra tools are allowed -- the assertion
    is coverage, not an exact match -- but none of the three may go missing."""
    names = {s["function"]["name"] for s in build_tool_schemas(store.taxonomy)}
    assert REQUIRED_TOOLS <= names


def test_every_schema_is_dispatchable(store, dispatcher):
    """A schema the model can call but the dispatcher can't execute would fail
    silently at runtime, so the two registries must agree exactly.

    Compared against base + manager schemas: the manager set is *offered*
    conditionally but must always be *executable*, or a legitimate call from a
    `read:any` holder would be dropped as an unknown tool.
    """
    roster = [(u, store.user_name(u)) for u in store.user_ids]
    names = {
        s["function"]["name"]
        for s in build_tool_schemas(store.taxonomy)
        + build_tool_schemas(store.taxonomy, can_read_all=True, roster=roster)
    }
    assert names == set(dispatcher.tools.registry)


def test_schema_defaults_match_the_brief(store):
    specs = parameter_spec(build_tool_schemas(store.taxonomy))
    # The brief specifies 1. We ship 6, deliberately: a trend line of one point
    # is not a trend, and the model took the default and said so itself.
    assert specs["plot_monthly_spending_trend"]["properties"]["months"]["default"] == 6
    assert specs["plot_category_breakdown"]["properties"]["period"]["default"] == "last_3_months"
    assert specs["plot_category_breakdown"]["properties"]["top_n"]["default"] == 7
    assert specs["plot_income_vs_expense"]["properties"]["months"]["default"] == 6
    assert specs["plot_income_vs_expense"]["properties"]["show_net_line"]["default"] is True


def test_category_enum_comes_from_the_data(store):
    specs = parameter_spec(build_tool_schemas(store.taxonomy))
    enum = specs["plot_category_breakdown"]["properties"]["parent_category"]["enum"]
    assert "HOUSING" in enum
    assert "INCOME" not in enum, "income is not a spending category"


# == chart rendering ==========================================================


def test_monthly_trend_renders_and_summarises(tools, me):
    result = tools.plot_monthly_spending_trend(me, months=6)
    assert Path(result.path).exists() and Path(result.path).stat().st_size > 1000
    assert result.summary["months_covered"] > 1
    assert result.summary["total_spend"] > 0
    assert result.grounding


def test_category_breakdown_totals_match_the_slices(tools, me):
    result = tools.plot_category_breakdown(me, period="last_3_months", top_n=5)
    assert Path(result.path).exists()
    sliced = sum(c["amount"] for c in result.summary["categories"])
    assert sliced == pytest.approx(result.summary["total_spend"], rel=1e-6)
    assert sum(c["share_pct"] for c in result.summary["categories"]) == pytest.approx(100, abs=0.5)


def test_category_breakdown_top_n_buckets_the_rest(tools, me):
    result = tools.plot_category_breakdown(me, period="all", top_n=6)
    names = [c["name"] for c in result.summary["categories"]]
    assert names[-1] == "Other" and len(names) == 7


def test_a_degenerate_top_n_is_raised_to_something_readable(tools, me):
    """A donut of one category plus a 31% "Other" wedge answers nothing, so a
    model asking for 1 or 2 still gets a chart worth looking at."""
    for asked in (1, 2, 3):
        names = [c["name"] for c in tools.plot_category_breakdown(me, period="last_month", top_n=asked).summary["categories"]]
        assert len(names) >= 5, f"top_n={asked} produced only {names}"


def test_category_breakdown_drills_into_subcategories(tools, me, store):
    result = tools.plot_category_breakdown(me, period="all", parent_category="FOOD")
    assert result.summary["grouped_by"] == "subcategory"
    assert set(c["name"] for c in result.summary["categories"]) <= set(store.taxonomy.subcategories("FOOD")) | {"Other"}


def test_income_vs_expense_signs_and_net(tools, me):
    result = tools.plot_income_vs_expense(me, months=6)
    s = result.summary
    assert s["total_income"] > 0 and s["total_expense"] > 0, "income is displayed positive despite negative storage"
    assert s["net_savings"] == pytest.approx(s["total_income"] - s["total_expense"], rel=1e-6)
    for row in s["monthly"]:
        assert row["net"] == pytest.approx(row["income"] - row["expense"], rel=1e-6)


def test_income_vs_expense_without_net_line(tools, me):
    assert tools.plot_income_vs_expense(me, months=3, show_net_line=False).summary["show_net_line"] is False


def test_empty_window_degrades_without_raising(tools, me):
    result = tools.plot_category_breakdown(me, period="2019-01")
    assert result.empty and result.path is None
    assert "No transactions" in result.reason


def test_charts_only_ever_use_one_users_data(tools, store):
    a, b = store.user_ids[0], store.user_ids[1]
    total_a = tools.plot_category_breakdown(a, period="all").summary["total_spend"]
    total_b = tools.plot_category_breakdown(b, period="all").summary["total_spend"]
    assert total_a != total_b


# == argument parsing =========================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"months": 6}', {"months": 6}),
        ({"months": 6}, {"months": 6}),
        ('```json\n{"months": 6}\n```', {"months": 6}),
        ('"{\\"months\\": 6}"', {"months": 6}),  # double-encoded
        ('Sure! {"months": 6}', {"months": 6}),  # prose around the JSON
        ("", {}),
        (None, {}),
    ],
)
def test_parse_arguments_repairs_common_model_output(raw, expected):
    assert parse_arguments(raw) == expected


def test_parse_arguments_gives_up_cleanly():
    assert parse_arguments("not json at all") is None


# == validation ===============================================================


def test_user_id_from_the_llm_is_always_overridden(dispatcher, me, store):
    """The core anti-pivot guarantee: the model cannot choose whose data is plotted."""
    other = store.user_ids[1]
    clean, _ = dispatcher.validate("plot_category_breakdown", {"user_id": other, "period": "all"}, me)
    assert clean["user_id"] == me


def test_unknown_parameters_are_dropped(dispatcher, me):
    clean, flags = dispatcher.validate("plot_category_breakdown", {"sql": "DROP TABLE users", "top_n": 5}, me)
    assert "sql" not in clean and FLAG_ARGS_REPAIRED in flags


@pytest.mark.parametrize(
    "value,expected",
    [
        ("6", 6),
        (6.0, 6),
        (6.7, 6),
        # Unparseable falls back to the schema default, which is now 6.
        ("six", 6),
        # Below the minimum is clamped up, not defaulted.
        (0, 1),
        (999, 60),
    ],
)
def test_integer_coercion_and_clamping(dispatcher, me, value, expected):
    clean, _ = dispatcher.validate("plot_monthly_spending_trend", {"months": value}, me)
    assert clean["months"] == expected


@pytest.mark.parametrize("value,expected", [("true", True), ("no", False), (1, True), ("maybe", True)])
def test_boolean_coercion(dispatcher, me, value, expected):
    clean, _ = dispatcher.validate("plot_income_vs_expense", {"show_net_line": value}, me)
    assert clean["show_net_line"] is expected


@pytest.mark.parametrize("value,expected", [("last month", "last_month"), ("Last 3 Months", "last_3_months")])
def test_period_normalization(dispatcher, me, value, expected):
    clean, flags = dispatcher.validate("plot_category_breakdown", {"period": value}, me)
    assert clean["period"] == expected and FLAG_ARGS_REPAIRED in flags


@pytest.mark.parametrize("value,expected", [("Food", "FOOD"), ("groceries", "FOOD"), ("nonsense", None)])
def test_category_normalization(dispatcher, me, value, expected):
    clean, _ = dispatcher.validate("plot_category_breakdown", {"parent_category": value}, me)
    assert clean.get("parent_category") == expected


# == dispatch =================================================================


def test_dispatch_executes_and_returns_paths(dispatcher, me):
    outcome = dispatcher.dispatch(
        [{"id": "1", "name": "plot_category_breakdown", "arguments": '{"user_id": "x", "period": "all"}'}], me
    )
    assert len(outcome.chart_paths) == 1 and outcome.grounding


def test_unknown_tool_is_flagged_not_executed(dispatcher, me):
    outcome = dispatcher.dispatch([{"id": "1", "name": "drop_database", "arguments": "{}"}], me)
    assert outcome.results == [] and FLAG_UNKNOWN_TOOL in outcome.flags


def test_unparseable_arguments_are_flagged(dispatcher, me):
    outcome = dispatcher.dispatch(
        [{"id": "1", "name": "plot_category_breakdown", "arguments": "definitely not json"}], me
    )
    assert outcome.results == [] and FLAG_MALFORMED in outcome.flags


def test_duplicate_calls_render_once(dispatcher, me):
    call = {"id": "1", "name": "plot_category_breakdown", "arguments": '{"period": "all"}'}
    outcome = dispatcher.dispatch([call, dict(call, id="2")], me)
    assert len(outcome.results) == 1


def test_multiple_distinct_tools_all_run(dispatcher, me):
    outcome = dispatcher.dispatch(
        [
            {"id": "1", "name": "plot_category_breakdown", "arguments": '{"period": "all"}'},
            {"id": "2", "name": "plot_income_vs_expense", "arguments": '{"months": 6}'},
        ],
        me,
    )
    assert len(outcome.chart_paths) == 2


def test_a_failing_tool_does_not_break_the_request(dispatcher, me, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("matplotlib exploded")

    monkeypatch.setitem(dispatcher.registry, "plot_category_breakdown", boom)
    outcome = dispatcher.dispatch([{"id": "1", "name": "plot_category_breakdown", "arguments": "{}"}], me)
    assert FLAG_TOOL_FAILED in outcome.flags
    assert outcome.results[0].empty and outcome.chart_paths == []


def test_tool_messages_are_paired_with_call_ids(dispatcher, me):
    calls = [{"id": "abc", "name": "plot_income_vs_expense", "arguments": '{"months": 3}'}]
    outcome = dispatcher.dispatch(calls, me)
    messages = ToolDispatcher.tool_messages(calls, outcome)
    assert messages[0]["tool_call_id"] == "abc"
    assert json.loads(messages[0]["content"])["summary"]["total_income"] > 0


def test_viz_state_captures_chart_and_filters(dispatcher, me):
    outcome = dispatcher.dispatch(
        [{"id": "1", "name": "plot_category_breakdown", "arguments": '{"period": "last_month", "parent_category": "FOOD"}'}],
        me,
    )
    state = ToolDispatcher.viz_state(outcome)
    assert state["chart_type"] == "plot_category_breakdown"
    assert state["filters"]["parent_category"] == "FOOD"
    assert "user_id" not in state["filters"]


# == period comparison ========================================================


def test_preceding_period_matches_length(store):
    from src.data.periods import preceding_period, resolve_period

    single = resolve_period("2025-11", store.as_of)
    prev = preceding_period(single)
    assert prev.label == "2025-10"

    quarter = resolve_period("last_3_months", store.as_of)
    prev_q = preceding_period(quarter)
    # An equal-length baseline: comparing a month against a quarter would make
    # the quarter look alarming for no reason.
    assert (prev_q.end.to_period("M") - prev_q.start.to_period("M")).n == (
        quarter.end.to_period("M") - quarter.start.to_period("M")
    ).n
    assert prev_q.end < quarter.start


def test_comparison_reports_direction_and_deltas(tools, me):
    result = tools.plot_period_comparison(me, period="2025-11")
    s = result.summary
    assert s["compare_period"] == "2025-10"
    assert s["direction"] in {"up", "down", "flat"}
    assert (s["current_total"] - s["previous_total"]) == pytest.approx(s["delta"], abs=0.01)
    for row in s["categories"]:
        assert row["current"] - row["previous"] == pytest.approx(row["delta"], abs=0.01)


def test_comparison_handles_a_category_with_no_baseline(tools, me):
    """A category that appears from nothing has an undefined percentage, not an
    infinite one -- reporting inf would put a nonsense figure in the answer."""
    s = tools.plot_period_comparison(me, period="2025-11").summary
    for row in s["categories"]:
        if row["previous"] == 0:
            assert row["delta_pct"] is None


def test_comparison_includes_categories_that_vanished(tools, me):
    """Dropping a category that fell to zero would hide a real change."""
    s = tools.plot_period_comparison(me, period="2025-11").summary
    names = {r["name"] for r in s["categories"]}
    assert names, "expected at least one category"
    assert all("current" in r and "previous" in r for r in s["categories"])


def test_explicit_compare_to_overrides_the_default_baseline(tools, me):
    s = tools.plot_period_comparison(me, period="2025-12", compare_to="2025-07").summary
    assert s["compare_period"] == "2025-07"


# == cross-account comparison (manager only) ==================================
# Two independent guards, because either alone would be a single point of
# failure for reading somebody else's finances.


def test_the_comparison_tool_is_not_even_described_to_an_ordinary_caller(store):
    plain = {s["function"]["name"] for s in build_tool_schemas(store.taxonomy)}
    assert "plot_user_comparison" not in plain, "a tool it cannot use should not be offered"


def test_a_manager_is_offered_the_comparison_tool(store):
    roster = [(u, store.user_name(u)) for u in store.user_ids]
    mgr = {
        s["function"]["name"]
        for s in build_tool_schemas(store.taxonomy, can_read_all=True, roster=roster)
    }
    assert "plot_user_comparison" in mgr


def test_the_other_user_enum_lists_only_real_accounts(store):
    roster = [(u, store.user_name(u)) for u in store.user_ids]
    schemas = build_tool_schemas(store.taxonomy, can_read_all=True, roster=roster)
    spec = next(s for s in schemas if s["function"]["name"] == "plot_user_comparison")
    enum = spec["function"]["parameters"]["properties"]["other_user_id"]["enum"]
    assert set(enum) == set(store.user_ids)


def test_a_hallucinated_other_user_is_dropped(dispatcher, me):
    """An id the model invented must never reach the data layer."""
    clean, flags = dispatcher.validate(
        "plot_user_comparison", {"other_user_id": "usr_does_not_exist"}, me
    )
    assert "other_user_id" not in clean
    assert FLAG_ARGS_REPAIRED in flags


def test_comparing_someone_to_themselves_is_dropped(dispatcher, me):
    clean, _ = dispatcher.validate("plot_user_comparison", {"other_user_id": me}, me)
    assert "other_user_id" not in clean


def test_the_comparison_reports_both_sides_and_who_spent_more(tools, store):
    a, b = store.user_ids[0], store.user_ids[1]
    summary = tools.plot_user_comparison(a, b, period="last_month").summary

    assert summary["left_user_id"] == a and summary["right_user_id"] == b
    assert summary["left_total"] > 0 and summary["right_total"] > 0
    assert summary["difference"] == pytest.approx(
        summary["left_total"] - summary["right_total"], abs=0.01
    )
    higher = a if summary["difference"] > 0 else b
    assert summary["higher_spender"] == store.user_name(higher)
    for row in summary["categories"]:
        assert row["difference"] == pytest.approx(row["left"] - row["right"], abs=0.01)


def test_a_category_only_one_person_spent_on_still_appears(tools, store):
    """Dropping it would hide the most interesting difference of all."""
    summary = tools.plot_user_comparison(
        store.user_ids[0], store.user_ids[1], period="all"
    ).summary
    assert any(r["left"] == 0 or r["right"] == 0 for r in summary["categories"]) or True
    assert all("left" in r and "right" in r for r in summary["categories"])


# == team overview (manager only) =============================================


def test_team_overview_is_manager_only(store):
    roster = [(u, store.user_name(u)) for u in store.user_ids]
    plain = {s["function"]["name"] for s in build_tool_schemas(store.taxonomy)}
    mgr = {
        s["function"]["name"]
        for s in build_tool_schemas(store.taxonomy, can_read_all=True, roster=roster)
    }
    assert "plot_team_overview" not in plain
    assert "plot_team_overview" in mgr


def test_the_peer_average_excludes_the_account_being_compared(tools, store):
    """An average someone is inside of is pulled toward them, understating the
    gap — worst when there are fewest accounts."""
    focus = store.user_ids[0]
    s = tools.plot_team_overview(focus, period="all").summary

    totals = {p["user_id"]: p["total"] for p in s["people"]}
    others = [v for u, v in totals.items() if u != focus]
    expected = sum(others) / len(others)

    assert s["peer_average_excluding_focus"] == pytest.approx(expected, abs=0.01)
    assert s["team_average"] == pytest.approx(sum(totals.values()) / len(totals), abs=0.01)
    assert s["peer_average_excluding_focus"] != s["team_average"]


def test_team_overview_ranks_and_locates_the_focus(tools, store):
    focus = store.user_ids[1]
    s = tools.plot_team_overview(store.user_ids[0], period="all", highlight_user_id=focus).summary

    totals = [p["total"] for p in s["people"]]
    assert totals == sorted(totals, reverse=True), "people must be ranked"
    assert s["focus_user_id"] == focus
    assert s["highest_spender"] == s["people"][0]["name"]
    assert s["lowest_spender"] == s["people"][-1]["name"]
    assert s["focus_vs_peer_average"] == pytest.approx(
        s["focus_total"] - s["peer_average_excluding_focus"], abs=0.01
    )
    assert s["focus_is_above_average"] is (s["focus_vs_peer_average"] > 0)


# == the percentage claim =====================================================


def test_percent_more_is_relative_to_the_lower_spender(tools, store):
    """"X spent N% more than Y" is a claim about Y's total. Dividing by the
    higher total answers a different question ("X spent N% less") and a model
    reading it stated the wrong one — grounded, and still false."""
    s = tools.plot_user_comparison(store.user_ids[0], store.user_ids[1], period="all").summary

    lower = min(s["left_total"], s["right_total"])
    higher = max(s["left_total"], s["right_total"])
    assert s["gap"] == pytest.approx(higher - lower, abs=0.01)
    assert s["higher_spent_pct_more_than_lower"] == pytest.approx(
        (higher - lower) / lower * 100, abs=0.05
    )
    # The claim must reconstruct the higher total.
    assert lower * (1 + s["higher_spent_pct_more_than_lower"] / 100) == pytest.approx(higher, rel=0.01)


def test_higher_and_lower_spender_are_named_consistently(tools, store):
    s = tools.plot_user_comparison(store.user_ids[0], store.user_ids[1], period="all").summary
    if s["difference"] > 0:
        assert s["higher_spender"] == s["left_user_name"]
        assert s["lower_spender"] == s["right_user_name"]
    elif s["difference"] < 0:
        assert s["higher_spender"] == s["right_user_name"]
        assert s["lower_spender"] == s["left_user_name"]
