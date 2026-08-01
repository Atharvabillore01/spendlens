"""`TransactionRAGPipeline` — the orchestrator and the only public entry point.

    pipeline = TransactionRAGPipeline(df=transactions_df)
    result   = pipeline.run(user_id="usr_a1b2c3d4", prompt="What did I spend the most on last month?")

Stages, in order:
  0. Input guardrails            -> block/redirect before anything is loaded
  1. User data fetch + profile   -> structured error on unknown user; cache on miss
  2. Context assembly            -> profile + few-shot history + viz state, budgeted
  3. LLM reasoning + tool dispatch -> charts rendered, then a narration round-trip
  4. Output guardrails           -> hallucination / toxicity / confidence
  5. Cache write                 -> query_history ring buffer + viz_state
  6. Audit log + structured return

Nothing here raises to the caller. Every failure path has a defined degraded
response.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .cache.kv_cache import KVCache, build_cache
from .cache.user_cache import UserCache
from .config import Settings, get_settings
from .data.periods import Period, month_label, month_name, resolve_period
from .data.profile_builder import ProfileBuilder
from .data.user_data_store import UnknownUserError, UserDataStore
from .guardrails.input_guardrails import InputGuardrails
from .guardrails.output_guardrails import OutputGuardrails
from .llm.openrouter_client import LLMError, LLMResponse, LLMUnavailableError, OpenRouterClient
from .llm.prompt_builder import PromptBuilder
from .llm.tool_dispatcher import ToolDispatcher
from .observability.audit_logger import AuditLogger
from .observability.circuit_breaker import CircuitBreaker
from .observability.trace import TurnTrace, configure as configure_trace
from .tools.schemas import build_tool_schemas, parameter_spec
from .tools.visualizations import ChartResult, VisualizationTools

log = logging.getLogger("transaction_rag.pipeline")

FLAG_LLM_UNAVAILABLE = "llm_unavailable"
FLAG_USER_NOT_FOUND = "user_not_found"
FLAG_NO_DATA = "no_data_for_query"
FLAG_TOOL_RETRY = "tool_call_retried"


def money(value: float) -> str:
    """`-$881.00`, not `$-881.00` — the sign belongs outside the symbol."""
    return f"-${abs(float(value)):,.2f}" if value < 0 else f"${float(value):,.2f}"


class TransactionRAGPipeline:
    def __init__(
        self,
        df: Optional[pd.DataFrame] = None,
        settings: Optional[Settings] = None,
        cache: Optional[KVCache] = None,
        llm_client: Optional[Any] = None,
        audit_logger: Optional[AuditLogger] = None,
        store: Optional[Any] = None,
    ):
        """Build a pipeline over either storage backend.

        `df` builds the in-memory `UserDataStore` (the assessment path, the demo
        and the tests). `store` accepts an already-constructed backend —
        `SqlUserDataStore` in production — which is how one process serves many
        tenants without holding any of their transactions in memory.
        """
        self.settings = settings or get_settings()

        if store is None and df is None:
            raise ValueError("TransactionRAGPipeline needs either `df` or `store`")

        if store is not None:
            self.store = store
        else:
            as_of = pd.Timestamp(self.settings.as_of_date) if self.settings.as_of_date else None
            self.store = UserDataStore(df, as_of=as_of)

        self.taxonomy = self.store.taxonomy
        self.profiles = ProfileBuilder(self.store)

        # Cache entries are namespaced by tenant, because `user_id` is only
        # unique within one. The DataFrame backend has no tenant, so it keeps
        # the brief's unprefixed key names.
        self.tenant_id = getattr(self.store, "tenant_id", None)
        self.cache = UserCache(cache or build_cache(self.settings), self.settings, self.tenant_id)

        self.input_guardrails = InputGuardrails(
            max_prompt_chars=self.settings.max_prompt_chars,
            # The roster is a lookup, not a list: the SQL backend answers
            # "is this someone else?" with an indexed query instead of loading
            # every user into the worker.
            roster=self.store.make_roster(),
            extra_finance_terms=[t.lower() for t in self.taxonomy.parents]
            + [s.lower() for p in self.taxonomy.parents for s in self.taxonomy.subcategories(p)],
        )
        self.output_guardrails = OutputGuardrails(
            rel_tolerance=self.settings.hallucination_rel_tolerance,
            abs_tolerance=self.settings.hallucination_abs_tolerance,
        )

        self.tool_schemas = build_tool_schemas(self.taxonomy)
        # A second set, offered only to a caller holding `read:any`. Built once
        # rather than per turn -- the roster changes with the data, not with the
        # request -- and refreshed by `invalidate_user` alongside everything else.
        self._manager_schemas = build_tool_schemas(
            self.taxonomy, can_read_all=True, roster=self._roster_pairs()
        )
        self.visualizations = VisualizationTools(
            self.store, Path(self.settings.chart_output_dir), self.settings.chart_dpi
        )
        # The dispatcher validates against the union, so a manager tool is
        # executable while remaining invisible to callers who were never shown it.
        self.dispatcher = ToolDispatcher(
            self.visualizations,
            parameter_spec(self.tool_schemas + self._manager_schemas),
            self.taxonomy,
        )

        self.breaker = CircuitBreaker(
            self.settings.circuit_breaker_failure_threshold, self.settings.circuit_breaker_cooldown_s
        )
        self.llm = llm_client if llm_client is not None else self._default_client()
        self.prompts = PromptBuilder(self.settings, self.taxonomy)
        self.audit = audit_logger or AuditLogger(self.settings.audit_log_path)
        if self.settings.trace_turns:
            configure_trace(self.settings.trace_level)

    def _default_client(self):
        """The live client, wrapped in the offline fallback unless disabled.

        The breaker stays attached to the live client, so `/readyz` still sees
        an outage even while the fallback is keeping answers flowing.
        """
        live = OpenRouterClient(self.settings, self.breaker)
        if not getattr(self.settings, "offline_fallback", True):
            return live
        from .llm.fallback import FallbackLLMClient  # noqa: PLC0415
        from .llm.scripted import offline_router  # noqa: PLC0415

        return FallbackLLMClient(live, offline_router)

    def _roster_pairs(self) -> list[tuple[str, str]]:
        """(user_id, name) for the accounts a manager may compare against."""
        try:
            return [(uid, self.store.user_name(uid)) for uid in self.store.user_ids]
        except Exception:  # noqa: BLE001 -- a SQL backend may not enumerate cheaply
            return []

    # ==== public API =========================================================

    def run(
        self,
        user_id: str,
        prompt: str,
        chart_theme: str = "light",
        can_read_all: bool = False,
        actor_id: Optional[str] = None,
        impersonated: bool = False,
    ) -> dict[str, Any]:
        """Run one turn.

        `chart_theme` is presentation only ("light" | "dark") -- it selects the
        chart palette so the web UI's dark mode gets charts rendered for the dark
        surface rather than a white rectangle. It is keyword-optional, so the
        interface contract in the brief (`run(user_id, prompt)`) is unchanged.
        """
        started = time.perf_counter()
        flags: list[str] = []
        trace = TurnTrace(
            user_id=user_id,
            prompt=prompt,
            enabled=self.settings.trace_turns,
            log_path=self.settings.trace_log_path,
        )

        # -- Stage 1a: user validation (before guardrails need the name) ------
        if not self.store.validate_user(user_id):
            return self._user_not_found(user_id, prompt, started, actor_id=actor_id, impersonated=impersonated)

        user_name = self.store.user_name(user_id)

        # -- Stage 0: input guardrails ---------------------------------------
        # The scope check needs to know whether a conversation is already under
        # way, so elliptical follow-ups ("how has that changed?") aren't refused
        # for lacking finance vocabulary. This reads only the caller's *own*
        # history, so no cross-user surface is opened before the guardrails run.
        history = self.cache.get_query_history(user_id, actor_id=actor_id)
        guard = self.input_guardrails.check(
            prompt,
            user_id,
            user_name,
            has_context=bool(history),
            # Population questions ("who spends the most?") are refused for an
            # ordinary caller and allowed for a role holding `read:any`. The
            # pipeline is told the answer; it does not decide it.
            can_read_all=can_read_all,
        )
        flags.extend(guard.flags)
        trace.guardrails(guard.allowed, guard.flags, bool(history))
        if guard.blocked:
            trace.answer(guard.refusal or "")
            trace.emit()
            # Refusals are returned without ever touching the data or the LLM.
            return self._finalize(
                user_id=user_id,
                user_name=user_name,
                prompt=prompt,
                response=guard.refusal or "",
                data_summary={},
                visualizations=[],
                cache_hit=False,
                flags=flags,
                started=started,
                model_used=None,
                record_history=False,
                actor_id=actor_id,
                impersonated=impersonated,
            )

        safe_prompt = guard.prompt

        # -- Stage 1b: profile (cache hit/miss) ------------------------------
        profile, cache_hit = self.cache.get_or_build_profile(user_id, self.profiles.build)
        viz_state = self.cache.get_viz_state(user_id, actor_id=actor_id)
        trace.cache(cache_hit, len(history))

        facts = self._fact_pack(user_id)
        if facts["transaction_count"] == 0:
            return self._finalize(
                user_id=user_id,
                user_name=user_name,
                prompt=prompt,
                response=(
                    "I don't have any transactions on file for you yet, so there's nothing to analyse. "
                    "Once transactions are imported I can break down your spending, trends and savings."
                ),
                data_summary={"transaction_count": 0},
                visualizations=[],
                cache_hit=cache_hit,
                flags=flags + [FLAG_NO_DATA],
                started=started,
                model_used=None,
                actor_id=actor_id,
                impersonated=impersonated,
            )

        # -- Stage 2: context assembly ---------------------------------------
        messages, budget_flags = self.prompts.build(
            user_id=user_id,
            prompt=safe_prompt,
            profile=profile,
            query_history=history,
            viz_state=viz_state,
            as_of=self.store.as_of,
            notice=guard.notice,
            can_read_all=can_read_all,
        )
        flags.extend(budget_flags)

        # -- Stage 3: LLM + tool dispatch ------------------------------------
        stage3 = self._reason_and_dispatch(
            messages, user_id, safe_prompt, chart_theme, can_read_all=can_read_all
        )
        flags.extend(stage3["flags"])

        results: list[ChartResult] = stage3["results"]
        data_summary = self._compose_data_summary(user_id, safe_prompt, results, facts)

        trace.model(stage3["model"], degraded=stage3["degraded"])
        trace.tool_calls(stage3["outcome"].executed if stage3["outcome"] else [])
        for result in results:
            trace.computed(result.tool, result.summary)

        # Grounding = the data retrieved for *this* turn, nothing else:
        #   - figures the executed tools computed
        #   - figures in the composed data_summary
        #   - figures from the cached profile (they are in the prompt, so the
        #     model may legitimately cite them)
        grounding = (
            list(stage3["grounding"])
            + self._numbers_in(data_summary)
            + self._numbers_in(
                {k: v for k, v in profile.items() if k not in {"date_range", "computed_at", "user_id", "user_name"}}
            )
        )

        rendered = [r for r in results if r.path]
        all_tools_empty = bool(results) and not rendered

        # Record who each chart belongs to, so `/charts/{name}` can authorise a
        # request instead of trusting whoever holds the URL.
        for result in rendered:
            self.cache.grant_chart(Path(result.path).name, user_id)

        # -- Stage 4: output guardrails --------------------------------------
        deterministic = self._compose_deterministic(user_name, results, data_summary, facts)
        checked = self.output_guardrails.check(
            stage3["text"],
            grounding=grounding,
            data_available=not all_tools_empty,
            empty_data_detail=next((r.reason for r in results if r.empty and r.reason), ""),
            deterministic_facts=deterministic,
        )
        flags.extend(checked.flags)
        if all_tools_empty:
            flags.append(FLAG_NO_DATA)

        response_text = checked.response
        if guard.notice and guard.notice not in response_text:
            response_text = f"{guard.notice} {response_text}"

        trace.output(checked.flags, stripped=len(getattr(checked, "stripped", []) or []))
        trace.answer(response_text)
        trace.emit()

        # -- Stages 5 & 6 -----------------------------------------------------
        return self._finalize(
            user_id=user_id,
            user_name=user_name,
            prompt=safe_prompt,
            response=response_text,
            data_summary=data_summary,
            visualizations=[r.path for r in rendered],
            cache_hit=cache_hit,
            flags=flags,
            started=started,
            model_used=stage3["model"],
            pandas_operation=stage3["pandas_operation"],
            viz_state=ToolDispatcher.viz_state(stage3["outcome"]) if stage3["outcome"] else None,
            degraded=stage3["degraded"],
            tool_names=[r.tool for r in results],
            actor_id=actor_id,
            impersonated=impersonated,
        )

    # ==== cache admin ========================================================

    def invalidate_user(self, user_id: str) -> None:
        """Call after refreshing the underlying DataFrame."""
        self.cache.invalidate_user(user_id)

    def cache_snapshot(self, user_id: str) -> dict[str, Any]:
        return self.cache.snapshot(user_id)

    def _live_client(self):
        """The client that talks to a network, unwrapping the fallback if present."""
        return getattr(self.llm, "primary", self.llm)

    def health(self) -> dict[str, Any]:
        backend = self.cache.backend
        return {
            "users": len(self.store.user_ids),
            "as_of": str(self.store.as_of.date()),
            "cache_backend": self.settings.cache_backend,
            "cache_ok": bool(getattr(backend, "ping", lambda: True)()),
            "circuit_breaker": self.breaker.state.value,
            "llm_configured": self.settings.llm_enabled,
            # Whether a *real* OpenRouter client is wired in — a scripted or
            # fake client must not be reported as "live". The fallback wrapper
            # is live if what it wraps is: it adds a degraded path, it does not
            # replace the live one.
            "llm_live": isinstance(self._live_client(), OpenRouterClient),
            # True only while the live chain is actually failing over, so a
            # dashboard can tell "answering offline" from "configured to be able
            # to". `llm_live` stays true throughout.
            "llm_degraded": bool(getattr(self.llm, "degraded", False)),
            "offline_fallback": self._live_client() is not self.llm,
            # Providers, in the order they will actually be tried. Reporting the
            # configured chain rather than one provider's list is what makes a
            # "which backend answered me?" question answerable.
            "providers": [p.name for p in getattr(self.llm, "providers", [])],
            "models": [
                f"{prov.name}/{model}"
                for prov in getattr(self.llm, "providers", [])
                for model in prov.models
            ] or list(self.settings.model_fallback_chain),
        }

    # ==== Stage 3 ============================================================

    def _reason_and_dispatch(
        self,
        messages: list[dict],
        user_id: str,
        prompt: str,
        theme: str = "light",
        can_read_all: bool = False,
    ) -> dict[str, Any]:
        """LLM call, tool execution, and the narration round-trip."""
        out: dict[str, Any] = {
            "text": "",
            "results": [],
            "grounding": [],
            "flags": [],
            "model": None,
            "outcome": None,
            "pandas_operation": None,
            "degraded": False,
        }

        try:
            tools = self._manager_schemas if can_read_all else self.tool_schemas
            first: LLMResponse = self.llm.complete(messages, tools=tools)
        except (LLMUnavailableError, LLMError) as exc:
            log.warning("LLM unavailable, degrading: %s", exc)
            out["flags"].append(FLAG_LLM_UNAVAILABLE)
            out["degraded"] = True
            out["results"] = self._degraded_charts(user_id, prompt, theme)
            out["grounding"] = [g for r in out["results"] for g in r.grounding]
            return out

        out["model"] = first.model

        if not first.has_tool_calls:
            out["text"] = first.content
            out["pandas_operation"] = "profile lookup (no tool call)"
            return out

        outcome = self.dispatcher.dispatch(first.tool_calls, user_id, theme)

        # Malformed-output resilience: if every call was rejected, ask once more
        # with an explicit correction, then give up on charts (spec §6).
        if not outcome.results and first.tool_calls:
            out["flags"].append(FLAG_TOOL_RETRY)
            retry_messages = messages + [
                {
                    "role": "system",
                    "content": (
                        "Your previous tool call could not be parsed. Reply with exactly one valid "
                        "tool call using only the documented parameter names and JSON types, or "
                        "answer in plain text with no tool call."
                    ),
                }
            ]
            try:
                first = self.llm.complete(retry_messages, tools=tools)
                out["model"] = first.model
                if first.has_tool_calls:
                    outcome = self.dispatcher.dispatch(first.tool_calls, user_id, theme)
            except (LLMUnavailableError, LLMError):
                out["flags"].append(FLAG_LLM_UNAVAILABLE)

        out["flags"].extend(outcome.flags)
        out["results"] = outcome.results
        out["grounding"] = outcome.grounding
        out["outcome"] = outcome
        out["pandas_operation"] = self._describe_operation(outcome)

        if not outcome.results:
            out["text"] = first.content
            return out

        # Narration round-trip: hand the real numbers back so the prose is
        # written from computed values rather than from the model's memory.
        tool_messages = self.dispatcher.tool_messages(first.tool_calls, outcome)
        assistant_turn = {
            "role": "assistant",
            "content": first.content or None,
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for call in first.tool_calls
            ],
        }
        try:
            second = self.llm.complete(
                messages + [assistant_turn] + tool_messages, tools=tools, tool_choice="none"
            )
            out["text"] = second.content or first.content
            out["model"] = second.model or out["model"]
        except (LLMUnavailableError, LLMError) as exc:
            log.warning("narration round-trip failed, composing deterministically: %s", exc)
            out["flags"].append(FLAG_LLM_UNAVAILABLE)
            out["text"] = ""  # Stage 4 substitutes the deterministic summary
        return out

    def _degraded_charts(self, user_id: str, prompt: str, theme: str = "light") -> list[ChartResult]:
        """Pick a chart heuristically when the LLM can't be reached."""
        lowered = prompt.lower()
        try:
            if any(w in lowered for w in ("saving", "save", "income", "earn", "bleeding", "doing financially")):
                return [self.visualizations.plot_income_vs_expense(user_id, months=6, theme=theme)]
            if any(w in lowered for w in ("trend", "over time", "changed", "changing", "history")):
                return [self.visualizations.plot_monthly_spending_trend(user_id, months=6, theme=theme)]
            return [self.visualizations.plot_category_breakdown(user_id, period="last_month", theme=theme)]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _describe_operation(outcome) -> Optional[str]:
        """Short, cacheable description of what was computed, for few-shot reuse."""
        if not outcome or not outcome.executed:
            return None
        parts = []
        for call in outcome.executed:
            args = ", ".join(f"{k}={v!r}" for k, v in call["args"].items() if k != "user_id")
            parts.append(f"{call['name']}({args})")
        return "; ".join(parts)

    # ==== fact pack & summaries ==============================================

    def _fact_pack(self, user_id: str) -> dict[str, Any]:
        """All-time Pandas aggregates for this user.

        Used to compose degraded-mode and fallback prose. Deliberately *not*
        used as hallucination grounding: an exhaustive set of every window ×
        category × merchant figure is so dense that almost any plausible number
        lands within tolerance of something, which would make the check
        meaningless. Grounding is restricted to what was actually retrieved for
        the current turn -- see `run()`.
        """
        frame = self.store.get_user_frame(user_id)
        return {
            "transaction_count": int(len(frame)),
            "totals": self.store.totals(frame),
            "monthly": self.store.monthly_totals(frame),
        }

    def _relevant_period(self, prompt: str) -> Period:
        """Cheap intent read used only to pick the window for `data_summary`."""
        lowered = prompt.lower()
        if "last month" in lowered or "previous month" in lowered:
            return resolve_period("last_month", self.store.as_of)
        if "this month" in lowered:
            return resolve_period("this_month", self.store.as_of)
        if "this year" in lowered or "year to date" in lowered or "ytd" in lowered:
            return resolve_period("ytd", self.store.as_of)
        if "all time" in lowered or "ever" in lowered or "overall" in lowered:
            return resolve_period("all", self.store.as_of)
        for n in (12, 6, 3, 2):
            if f"{n} month" in lowered:
                return resolve_period(f"last_{n}_months", self.store.as_of)
        return resolve_period("last_3_months", self.store.as_of)

    def _compose_data_summary(
        self, user_id: str, prompt: str, results: list[ChartResult], facts: dict
    ) -> dict[str, Any]:
        """The `data_summary` field: computed facts backing the response."""
        summary: dict[str, Any] = {}
        if results:
            for result in results:
                # `no_data` is part of the wire contract, not an internal
                # detail: a client cannot otherwise tell an empty window from a
                # populated one except by probing for fields that may be absent.
                # `ChartResult.empty` knows; the summary must say so too.
                payload = dict(result.summary)
                if result.empty:
                    payload["no_data"] = True
                    if result.reason:
                        payload["reason"] = result.reason
                summary[result.tool] = payload
            primary = next((r for r in results if not r.empty), None)
            if primary is not None:
                summary["period"] = primary.summary.get("period")
                if "top_category" in primary.summary:
                    summary["top_category"] = primary.summary["top_category"]
                if "total_spend" in primary.summary:
                    summary["total_spend"] = primary.summary["total_spend"]
                if "net_savings" in primary.summary:
                    summary["net_savings"] = primary.summary["net_savings"]
            return summary

        # No chart: summarise the window the question is about.
        period = self._relevant_period(prompt)
        frame = self.store.get_user_frame(user_id, period=period)
        totals = self.store.totals(frame)
        cats = self.store.top_categories(frame, n=5)
        summary = {
            "period": period.label,
            "period_label": month_name(period),
            "transaction_count": totals["transaction_count"],
            "total_spend": totals["total_expense"],
            "total_income": totals["total_income"],
            "net_savings": totals["net_savings"],
            "top_categories": [{"name": n, "amount": a} for n, a in cats],
        }
        if cats:
            summary["top_category"] = {"name": cats[0][0], "amount": cats[0][1]}
        return summary

    def _compose_deterministic(
        self, user_name: str, results: list[ChartResult], data_summary: dict, facts: dict
    ) -> str:
        """Pure-Pandas prose. Used for degraded mode and post-strip fallback."""
        parts: list[str] = []
        for result in results:
            if result.empty:
                continue
            s = result.summary
            if result.tool == "plot_category_breakdown" and s.get("top_category"):
                top = s["top_category"]
                parts.append(
                    f"In {s.get('period_label', s.get('period'))} you spent "
                    f"${s.get('total_spend', 0):,.2f} in total, and your largest category was "
                    f"{top['name'].title()} at ${top['amount']:,.2f} ({top['share_pct']}% of the total)."
                )
            elif result.tool == "plot_monthly_spending_trend":
                parts.append(
                    f"Across {s.get('months_covered')} months you spent ${s.get('total_spend', 0):,.2f}, "
                    f"averaging ${s.get('average_monthly_spend', 0):,.2f} per month, with the highest month "
                    f"being {(lambda h: h.get('label') or month_label(h.get('month', '')))(s.get('highest_month', {}))} at "
                    f"${s.get('highest_month', {}).get('expense', 0):,.2f}."
                )
            elif result.tool == "plot_income_vs_expense":
                verdict = "saving" if s.get("is_saving") else "spending more than you earn"
                parts.append(
                    f"Over the last {s.get('months_covered')} months you brought in "
                    f"${s.get('total_income', 0):,.2f} against ${s.get('total_expense', 0):,.2f} of spending, "
                    f"so you are {verdict} — a net of {money(s.get('net_savings', 0))}"
                    + (f" ({s['savings_rate_pct']}% of income)." if s.get("savings_rate_pct") is not None else ".")
                )

        if not parts:
            top = data_summary.get("top_category")
            window = data_summary.get("period_label") or data_summary.get("period") or "your recent history"
            if top:
                parts.append(
                    f"In {window} you spent ${data_summary.get('total_spend', 0):,.2f} in total, "
                    f"led by {top['name'].title()} at ${top['amount']:,.2f}."
                )
            else:
                totals = facts["totals"]
                parts.append(
                    f"Across your full history you have {totals['transaction_count']} transactions "
                    f"totalling ${totals['total_expense']:,.2f} of spending against "
                    f"${totals['total_income']:,.2f} of income."
                )
        return " ".join(parts)

    @staticmethod
    def _numbers_in(value: Any) -> list[float]:
        """Recursively collect every numeric leaf — these are all Pandas-derived."""
        found: list[float] = []
        if isinstance(value, dict):
            for item in value.values():
                found.extend(TransactionRAGPipeline._numbers_in(item))
        elif isinstance(value, (list, tuple)):
            for item in value:
                found.extend(TransactionRAGPipeline._numbers_in(item))
        elif isinstance(value, bool):
            pass
        elif isinstance(value, (int, float)):
            found.append(float(value))
        return found

    # ==== Stages 5 & 6 =======================================================

    def _finalize(
        self,
        *,
        user_id: str,
        user_name: Optional[str],
        prompt: str,
        response: str,
        data_summary: dict,
        visualizations: list[str],
        cache_hit: bool,
        flags: list[str],
        started: float,
        model_used: Optional[str],
        pandas_operation: Optional[str] = None,
        viz_state: Optional[dict] = None,
        record_history: bool = True,
        tool_names: Optional[list[str]] = None,
        degraded: bool = False,
        error: Optional[dict] = None,
        # Carried from run() purely so the audit entry can name who asked, as
        # distinct from whose data was read.
        actor_id: Optional[str] = None,
        impersonated: bool = False,
    ) -> dict[str, Any]:
        unique_flags = sorted(set(flags))

        if record_history and user_name is not None:
            self.cache.append_query_history(
                user_id,
                {
                    "prompt": prompt,
                    "pandas_operation": pandas_operation or "aggregate over filtered user frame",
                    "result_summary": self._history_summary(response, data_summary),
                },
                actor_id=actor_id,
            )
            if viz_state:
                self.cache.set_viz_state(user_id, viz_state, actor_id=actor_id)

        latency_ms = int((time.perf_counter() - started) * 1000)

        result: dict[str, Any] = {
            "user_name": user_name,
            "response": response,
            "data_summary": data_summary,
            "visualizations": visualizations,
            "cache_hit": cache_hit,
            "latency_ms": latency_ms,
            "guardrail_flags": unique_flags,
            # Additive fields — useful for the demo/API, not part of the brief's
            # required contract.
            "user_id": user_id,
            "model_used": model_used,
            "degraded": degraded,
        }
        if error:
            result.update(error)

        self.audit.record(
            user_id=user_id,
            prompt=prompt,
            response=response,
            latency_ms=latency_ms,
            guardrail_flags=unique_flags,
            cache_hit=cache_hit,
            model_used=model_used,
            tool_calls=list(tool_names or []),
            error=(error or {}).get("error"),
            actor_id=actor_id,
            tenant_id=self.tenant_id,
            impersonated=impersonated,
        )
        return result

    @staticmethod
    def _history_summary(response: str, data_summary: dict) -> str:
        top = data_summary.get("top_category")
        if top:
            return f"{top['name']}: {money(top['amount'])}"
        for key in ("net_savings", "total_spend"):
            if key in data_summary:
                return f"{key}: {money(data_summary[key])}"
        return (response or "")[:160]

    def _user_not_found(
        self,
        user_id: str,
        prompt: str,
        started: float,
        actor_id: Optional[str] = None,
        impersonated: bool = False,
    ) -> dict[str, Any]:
        """Structured error, never an exception trace (spec §6)."""
        return self._finalize(
            user_id=user_id,
            user_name=None,
            prompt=prompt,
            response=(
                f"I couldn't find any account matching '{user_id}'. "
                "Please check the user id and try again."
            ),
            data_summary={},
            visualizations=[],
            cache_hit=False,
            flags=[FLAG_USER_NOT_FOUND],
            started=started,
            model_used=None,
            record_history=False,
            error={"error": "user_not_found", "message": f"user_id '{user_id}' does not exist in the dataset"},
            actor_id=actor_id,
            impersonated=impersonated,
        )


def load_transactions(path: Optional[Path] = None) -> pd.DataFrame:
    """Convenience loader for the assessment workbook."""
    settings = get_settings()
    return pd.read_excel(path or settings.data_path)


__all__ = ["TransactionRAGPipeline", "load_transactions", "UnknownUserError"]
