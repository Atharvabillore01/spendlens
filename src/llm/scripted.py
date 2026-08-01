"""A scripted stand-in for `OpenRouterClient`.

This lives in `src/` rather than in the test suite because it is a **product
feature**, not a fixture: `OFFLINE_LLM=1` is how the service is demonstrated
without an API key or a quota, and how `demo.py --offline` runs. Keeping it in
`tests/conftest.py` meant the running application imported test code — which
worked locally and failed the moment the container excluded `tests/`, exactly as
it should.

The tests import it from here too, so there is one implementation rather than
two that drift.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .openrouter_client import LLMResponse, LLMUnavailableError


class ScriptedLLMClient:
    """Returns a fixed sequence of responses instead of calling a network.

    `script` is a list of `LLMResponse`s (or exceptions) returned in order; the
    last entry repeats once exhausted, so a test does not have to predict how
    many calls the pipeline will make. Every call is recorded, so a test can
    assert on what was actually sent.
    """

    def __init__(self, script: Optional[list] = None, model: str = "scripted/offline"):
        self.script = list(script or [])
        self.model = model
        self.calls: list[dict] = []
        self.last_model_used = model
        self.last_provider_used = "scripted"
        # Mirrors the real client's surface so `health()` and the metrics layer
        # can introspect it without special-casing.
        self.providers: list = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: str = "auto",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        if not self.script:
            return LLMResponse(content="Scripted answer.", model=self.model)
        item = self.script[0] if len(self.script) == 1 else self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:  # parity with OpenRouterClient
        return None

    # -- helpers for building scripts -----------------------------------------

    @staticmethod
    def text(content: str, model: str = "scripted/offline") -> LLMResponse:
        return LLMResponse(content=content, model=model)

    @staticmethod
    def tool_call(
        name: str,
        arguments: dict | str,
        content: str = "",
        call_id: str = "call_1",
    ) -> LLMResponse:
        raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
        return LLMResponse(
            content=content,
            tool_calls=[{"id": call_id, "name": name, "arguments": raw}],
            model="scripted/offline",
        )

    @staticmethod
    def unavailable(message: str = "simulated outage") -> Exception:
        return LLMUnavailableError(message)


#: The suite referred to this by its old name for a long time; keep the alias so
#: renaming the class is not a churn of every test file.
FakeOpenRouterClient = ScriptedLLMClient


def offline_router() -> ScriptedLLMClient:
    """A scripted client that picks a plausible tool call and narrates the result.

    Lives here rather than in `demo.py` because it is now load-bearing at
    runtime: it is the last link in the OpenRouter -> Groq -> offline chain, so
    `src/` cannot import it from a top-level demo script. `demo.py` re-exports it
    under its original name.

    The narration is built from the tool payload the pipeline hands back, so
    every figure it states is genuinely grounded -- which is what a well-behaved
    model does, and what the hallucination check should let through untouched.
    """

    class Router(ScriptedLLMClient):
        def complete(self, messages, tools=None, tool_choice="auto", max_tokens=None, temperature=None):
            self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
            if tool_choice == "none":  # narration round-trip
                return self.text(self._narrate(messages))
            prompt = messages[1]["content"].lower()
            if "saving" in prompt or "financially" in prompt or "overspend" in prompt:
                return self.tool_call("plot_income_vs_expense", {"months": 6})
            if any(k in prompt for k in (
                "compare", "comparison", " vs ", "versus", "more than", "less than",
                "up or down", "grew", "growth", "than last", "than october",
                "than the previous",
            )):
                return self.tool_call("plot_period_comparison", {"period": "last_month"})
            if "trend" in prompt or "over time" in prompt or "changed" in prompt:
                return self.tool_call("plot_monthly_spending_trend", {"months": 6})
            if "merchant" in prompt or "shop" in prompt or "store" in prompt or "vendor" in prompt:
                args = {"period": "last_3_months"}
                category = self._named_category(prompt, tools)
                if category:
                    args["parent_category"] = category
                return self.tool_call("plot_top_merchants", args)

            # Drill into whichever parent category the prompt names. The
            # vocabulary comes from the tool schema the pipeline just sent, so
            # this tracks the taxonomy instead of hardcoding one category --
            # "break down my housing spending" has to work as well as food's.
            category = self._named_category(prompt, tools)
            if category:
                return self.tool_call(
                    "plot_category_breakdown",
                    {"period": "last_3_months", "parent_category": category},
                )
            return self.tool_call("plot_category_breakdown", {"period": "last_month"})

        @staticmethod
        def _named_category(prompt: str, tools) -> Optional[str]:
            """Longest matching parent category mentioned in the prompt."""
            for schema in tools or []:
                params = (schema.get("function") or schema).get("parameters", {})
                spec = (params.get("properties") or {}).get("parent_category", {})
                options = [c for c in (spec.get("enum") or []) if c]
                # Longest first so FASTFOOD-style compounds can't be shadowed by
                # a shorter name that happens to be a substring.
                for name in sorted(options, key=len, reverse=True):
                    if re.search(rf"\b{re.escape(str(name).lower())}\b", prompt):
                        return str(name)
            return None

        @staticmethod
        def _narrate(messages) -> str:
            # Local import: `src.pipeline` imports this module's neighbours, so a
            # module-level import here would close the cycle.
            from ..pipeline import money  # noqa: PLC0415

            payloads = [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]
            lines = []
            for payload in payloads:
                s = payload.get("summary", {})
                if payload.get("no_data"):
                    continue
                if payload["tool"] == "plot_category_breakdown" and s.get("top_category"):
                    top = s["top_category"]
                    lines.append(
                        f"You spent ${s['total_spend']:,.2f} in {s.get('period_label', s['period'])}, and "
                        f"{top['name'].title()} was your largest category at ${top['amount']:,.2f} "
                        f"({top['share_pct']}% of the total)."
                    )
                elif payload["tool"] == "plot_monthly_spending_trend":
                    lines.append(
                        f"Your spending averaged ${s['average_monthly_spend']:,.2f} a month over "
                        f"{s['months_covered']} months, peaking at ${s['highest_month']['expense']:,.2f} "
                        f"in {s['highest_month'].get('label', s['highest_month']['month'])}."
                    )
                elif payload["tool"] == "plot_top_merchants" and s.get("top_merchant"):
                    top = s["top_merchant"]
                    lines.append(
                        f"Across {s['merchant_count']} merchants in {s.get('period_label', s['period'])}, "
                        f"{top['name']} took the most at ${top['amount']:,.2f} over {top['visits']} "
                        f"transaction{'s' if top['visits'] != 1 else ''}."
                    )
                elif payload["tool"] == "plot_period_comparison":
                    direction = {"up": "up", "down": "down", "flat": "flat"}[s["direction"]]
                    pct = f" ({s['delta_pct']:+.1f}%)" if s.get("delta_pct") is not None else ""
                    line = (
                        f"You spent ${s['current_total']:,.2f} in {s['period_label']} against "
                        f"${s['previous_total']:,.2f} in {s['compare_period_label']} — "
                        f"{direction} ${abs(s['delta']):,.2f}{pct}."
                    )
                    if s.get("biggest_increase"):
                        top = s["biggest_increase"]
                        line += (
                            f" The biggest rise was {top['name'].title()}, up "
                            f"${abs(top['delta']):,.2f}."
                        )
                    lines.append(line)
                elif payload["tool"] == "plot_income_vs_expense":
                    verdict = "you're saving" if s["is_saving"] else "you're overspending"
                    lines.append(
                        f"Income of ${s['total_income']:,.2f} against ${s['total_expense']:,.2f} of spending means "
                        f"{verdict} — {money(s['net_savings'])} net."
                    )
            return " ".join(lines)

    return Router()
