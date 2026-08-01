"""JSON tool schemas registered with the LLM.

Built from the live taxonomy rather than hardcoded, so the enum of valid
categories is always exactly what is in the data. Defaults mirror the brief §4.1.
"""

from __future__ import annotations

from typing import Any, Optional

from ..data.category_taxonomy import CategoryTaxonomy

PERIOD_HINT = (
    "Time window. One of: last_month, this_month, last_3_months, last_6_months, "
    "last_12_months, ytd, all — or an explicit calendar month as YYYY-MM."
)


def build_tool_schemas(
    taxonomy: Optional[CategoryTaxonomy] = None,
    can_read_all: bool = False,
    roster: Optional[list[tuple[str, str]]] = None,
) -> list[dict[str, Any]]:
    """Tools offered to the model.

    `can_read_all` adds the cross-account comparison. An ordinary caller is not
    merely refused it -- the tool is never described to the model at all, so
    there is nothing to be talked into calling.
    """
    categories = list(taxonomy.spend_parents) if taxonomy else []
    category_prop = {
        "type": ["string", "null"],
        "description": "Restrict to one parent category. Omit for all spending.",
    }
    if categories:
        category_prop["enum"] = categories + [None]

    return [
        {
            "type": "function",
            "function": {
                "name": "plot_monthly_spending_trend",
                "description": (
                    "Line chart of monthly spending totals with a rolling-average overlay. "
                    "Use for 'how has my spending changed over time', 'show me my trend', "
                    "'am I spending more than before'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "Target user."},
                        "months": {
                            "type": "integer",
                            # DELIBERATE DEVIATION from the brief, which
                            # specifies 1. A trend line of a single point is not
                            # a trend: asked for "my spending trend" the model
                            # took the default and produced one December bar,
                            # then apologised for it in its own answer. The
                            # description said "use 6" and was ignored, because
                            # a declared default outranks prose. 6 months is the
                            # shortest window in which a trend is visible.
                            "description": "Lookback in months. 6 unless the user names a shorter window.",
                            "default": 6,
                            "minimum": 1,
                            "maximum": 60,
                        },
                        "category_filter": category_prop,
                    },
                    "required": ["user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plot_category_breakdown",
                "description": (
                    "Donut chart of proportional spending by category, total spend in the centre. "
                    "Use for 'where is my money going', 'what did I spend the most on'. "
                    "Set parent_category to drill into one category's subcategories "
                    "(e.g. parent_category=FOOD for 'show me my food spending')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "Target user."},
                        "period": {
                            "type": "string",
                            "description": PERIOD_HINT,
                            "default": "last_3_months",
                        },
                        "top_n": {
                            "type": "integer",
                            # Floor of 4, not 1. A breakdown of two slices where
                            # one is "Other" is a chart that answers nothing --
                            # the whole point is seeing where the money split.
                            "description": (
                                "Number of categories to show; the rest are grouped as 'Other'. "
                                "Keep the default unless the user asks for a specific number."
                            ),
                            "default": 7,
                            "minimum": 4,
                            "maximum": 20,
                        },
                        "parent_category": category_prop,
                    },
                    "required": ["user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plot_income_vs_expense",
                "description": (
                    "Grouped bar chart of income vs expense per month with an optional net-savings "
                    "line. Use for 'am I saving money', 'how am I doing financially', "
                    "'am I bleeding money'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "Target user."},
                        "months": {
                            "type": "integer",
                            "description": "Lookback in months.",
                            "default": 6,
                            "minimum": 1,
                            "maximum": 60,
                        },
                        "show_net_line": {
                            "type": "boolean",
                            "description": "Overlay the net savings line.",
                            "default": True,
                        },
                    },
                    "required": ["user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plot_top_merchants",
                "description": (
                    "Ranked bar chart of the merchants a user spent the most with. "
                    "Use for 'show me my top merchants', 'who am I spending the most with', "
                    "'which shops take my money'. Prefer this over plot_category_breakdown "
                    "whenever the question names merchants, shops, stores or vendors."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "Target user."},
                        "period": {"type": "string", "description": PERIOD_HINT, "default": "last_3_months"},
                        "top_n": {
                            "type": "integer",
                            "description": "How many merchants to rank.",
                            "default": 8,
                            "minimum": 1,
                            "maximum": 25,
                        },
                        "parent_category": category_prop,
                    },
                    "required": ["user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plot_period_comparison",
                "description": (
                    "Diverging bar chart of how spending changed between two windows, "
                    "per category. Use whenever the question compares periods -- "
                    "'did I spend more in November than October', 'is my spending up or "
                    "down versus last month', 'which category grew the fastest', "
                    "'compare this quarter to last'. Prefer this over "
                    "plot_monthly_spending_trend when the user names two periods or asks "
                    "'more/less than'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "Target user."},
                        "period": {
                            "type": "string",
                            "description": "The window of interest. " + PERIOD_HINT,
                            "default": "last_month",
                        },
                        "compare_to": {
                            "type": ["string", "null"],
                            "description": (
                                "Baseline window. Omit to use the equal-length window "
                                "immediately before `period`, which is almost always what "
                                "the user means."
                            ),
                        },
                        "top_n": {
                            "type": "integer",
                            "description": "How many categories to show, by size of change.",
                            "default": 8,
                            "minimum": 1,
                            "maximum": 20,
                        },
                    },
                    "required": ["user_id"],
                },
            },
        },
    ] + (_manager_schemas(roster or []) if can_read_all else [])


def _manager_schemas(roster: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Tools only a `read:any` holder is shown."""
    people = [uid for uid, _ in roster]
    who = ", ".join(f"{uid} ({name})" for uid, name in roster[:25]) or "another account holder"
    other = {
        "type": "string",
        "description": f"The other account holder's user_id. Known: {who}.",
    }
    if people:
        other["enum"] = people

    return [
        {
            "type": "function",
            "function": {
                "name": "plot_user_comparison",
                "description": (
                    "Grouped bar chart comparing TWO account holders' spending, per category. "
                    "Use when the question names another person -- 'compare to Sarah', "
                    "'how does Jose compare to Marcus', 'who spent more'. "
                    "`user_id` is the account currently in view; `other_user_id` is the "
                    "person named in the question."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "Account currently in view."},
                        "other_user_id": other,
                        "period": {"type": "string", "description": PERIOD_HINT, "default": "last_month"},
                        "top_n": {"type": "integer", "default": 8, "minimum": 4, "maximum": 20},
                    },
                    "required": ["user_id", "other_user_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "plot_team_overview",
                "description": (
                    "Every account holder's spending, ranked, with the average marked. "
                    "Use for questions about the group rather than one pair: 'how does this "
                    "account compare to the average', 'average of the others', 'who spends "
                    "the most', 'rank everyone'. "
                    "Use plot_user_comparison instead when the question names exactly two people."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string", "description": "Account currently in view."},
                        "period": {"type": "string", "description": PERIOD_HINT, "default": "last_3_months"},
                        "highlight_user_id": {
                            "type": ["string", "null"],
                            "description": "Account to compare against the average. Defaults to the one in view.",
                        },
                    },
                    "required": ["user_id"],
                },
            },
        },
    ]


def parameter_spec(schemas: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """`{tool_name: json_schema_of_parameters}` — used by the dispatcher."""
    return {s["function"]["name"]: s["function"]["parameters"] for s in schemas}
