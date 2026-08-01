"""Parses, validates and executes the LLM's tool calls.

Two things this layer exists to guarantee:

1. **The LLM never chooses whose data is plotted.** `user_id` in a tool call is
   discarded and replaced with the authenticated id from `pipeline.run()`, so a
   prompt-injected tool call cannot pivot to another user even if every prompt
   guardrail were bypassed.
2. **Malformed output degrades, never crashes.** Free models emit unquoted JSON,
   markdown-fenced JSON, double-encoded strings, `"last month"` where an enum was
   requested, floats where ints were requested. All of it is repaired here or the
   call is dropped with a flag.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..data.category_taxonomy import CategoryTaxonomy
from ..data.periods import normalize_spec
from ..tools.visualizations import ChartResult, VisualizationTools

log = logging.getLogger("transaction_rag.tools")

FLAG_ARGS_REPAIRED = "tool_args_repaired"
FLAG_MALFORMED = "tool_call_malformed"
FLAG_UNKNOWN_TOOL = "unknown_tool_call"
FLAG_TOOL_FAILED = "tool_execution_failed"

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_TRUE = {"true", "yes", "1", "y", "on"}
_FALSE = {"false", "no", "0", "n", "off"}


@dataclass
class DispatchOutcome:
    results: list[ChartResult] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    executed: list[dict[str, Any]] = field(default_factory=list)  # {name, args}

    @property
    def chart_paths(self) -> list[str]:
        return [r.path for r in self.results if r.path]

    @property
    def grounding(self) -> list[float]:
        values: list[float] = []
        for result in self.results:
            values.extend(result.grounding)
        return values


def parse_arguments(raw: Any) -> Optional[dict[str, Any]]:
    """Best-effort JSON object out of whatever the model produced."""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    text = str(raw).strip()
    if not text:
        return {}
    text = _FENCE_RE.sub("", text).strip()

    for _ in range(2):  # handles double-encoded JSON strings
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            break
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            text = parsed.strip()
            continue
        return None

    # Last resort: pull the first {...} block out of surrounding prose.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            candidate = json.loads(match.group(0))
            if isinstance(candidate, dict):
                return candidate
        except json.JSONDecodeError:
            pass
    return None


class ToolDispatcher:
    def __init__(
        self,
        tools: VisualizationTools,
        parameter_specs: dict[str, dict[str, Any]],
        taxonomy: CategoryTaxonomy,
    ):
        self.tools = tools
        self.specs = parameter_specs
        self.taxonomy = taxonomy
        self.registry = tools.registry

    # -- validation -----------------------------------------------------------

    def _coerce(self, name: str, key: str, value: Any, spec: dict[str, Any]) -> tuple[Any, bool]:
        """Coerce one argument to its declared type. Returns `(value, repaired)`."""
        declared = spec.get("type")
        types = declared if isinstance(declared, list) else [declared]
        repaired = False

        if value is None or (isinstance(value, str) and value.strip().lower() in {"null", "none", ""}):
            return None, False

        if "integer" in types and not isinstance(value, bool):
            if not isinstance(value, int):
                try:
                    value = int(float(str(value).strip()))
                    repaired = True
                except (TypeError, ValueError):
                    return spec.get("default"), True
            lo, hi = spec.get("minimum"), spec.get("maximum")
            if lo is not None and value < lo:
                value, repaired = lo, True
            if hi is not None and value > hi:
                value, repaired = hi, True
            return value, repaired

        if "boolean" in types:
            if isinstance(value, bool):
                return value, False
            token = str(value).strip().lower()
            if token in _TRUE:
                return True, True
            if token in _FALSE:
                return False, True
            return bool(spec.get("default", True)), True

        if "string" in types:
            if not isinstance(value, str):
                value, repaired = str(value), True
            # Semantic normalization for the two constrained string arguments.
            if key in {"category_filter", "parent_category"}:
                normalized = self.taxonomy.normalize_parent(value)
                if normalized != value:
                    repaired = True
                return normalized, repaired
            if key == "period":
                normalized = normalize_spec(value)
                return normalized, normalized != value
            return value, repaired

        return value, repaired

    def validate(self, name: str, args: dict[str, Any], user_id: str) -> tuple[dict[str, Any], list[str]]:
        """Filter to declared params, coerce types, force the authenticated user."""
        spec = self.specs.get(name, {})
        properties: dict[str, Any] = spec.get("properties", {})
        flags: list[str] = []
        clean: dict[str, Any] = {}

        for key, value in (args or {}).items():
            if key == "user_id":
                continue  # always overridden below; never trusted
            if key not in properties:
                flags.append(FLAG_ARGS_REPAIRED)
                continue
            coerced, repaired = self._coerce(name, key, value, properties[key])
            if repaired:
                flags.append(FLAG_ARGS_REPAIRED)
            if coerced is not None:
                clean[key] = coerced

        # Authenticated identity wins, unconditionally.
        # The authenticated user is forced, always. This is the anti-pivot
        # guarantee: the model cannot choose whose data is plotted.
        clean["user_id"] = user_id

        # `other_user_id` is the one exception, and it is not an exception to
        # the rule above -- it is a *second* subject that only exists on a tool
        # the model was shown because the caller holds `read:any`. It is checked
        # against the roster so a hallucinated id cannot reach the data layer.
        if "other_user_id" in clean:
            other = str(clean["other_user_id"]).strip()
            if not self.taxonomy or not other or not self._known_user(other):
                clean.pop("other_user_id", None)
                flags.append(FLAG_ARGS_REPAIRED)
            elif other == user_id:
                clean.pop("other_user_id", None)
                flags.append(FLAG_ARGS_REPAIRED)

        return clean, sorted(set(flags))

    def _known_user(self, user_id: str) -> bool:
        store = getattr(self.tools, "store", None)
        try:
            return bool(store and store.validate_user(user_id))
        except Exception:  # noqa: BLE001 -- an unknown id is simply not known
            return False

    # -- execution ------------------------------------------------------------

    def dispatch(
        self,
        tool_calls: list[dict[str, Any]],
        user_id: str,
        theme: str = "light",
    ) -> DispatchOutcome:
        outcome = DispatchOutcome()
        seen: set[str] = set()

        for call in tool_calls or []:
            name = call.get("name")
            if name not in self.registry:
                log.warning("dropping unknown tool call: %r", name)
                outcome.flags.append(FLAG_UNKNOWN_TOOL)
                continue

            args = parse_arguments(call.get("arguments"))
            if args is None:
                log.warning("unparseable arguments for %s", name)
                outcome.flags.append(FLAG_MALFORMED)
                continue

            clean, flags = self.validate(name, args, user_id)
            outcome.flags.extend(flags)
            # Presentation, not a model-chooseable argument: injected after
            # validation so it can never appear in the tool schema.
            clean["theme"] = theme

            # Models sometimes emit the identical call twice; render once.
            fingerprint = f"{name}:{json.dumps(clean, sort_keys=True, default=str)}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)

            try:
                result = self.registry[name](**clean)
            except Exception as exc:  # noqa: BLE001 - a chart must never break the request
                log.exception("tool %s failed", name)
                outcome.flags.append(FLAG_TOOL_FAILED)
                outcome.results.append(
                    ChartResult(tool=name, path=None, empty=True, reason=f"Chart could not be generated ({type(exc).__name__}).")
                )
                continue

            outcome.results.append(result)
            outcome.executed.append({"name": name, "args": clean})

        outcome.flags = sorted(set(outcome.flags))
        return outcome

    # -- feedback to the LLM --------------------------------------------------

    @staticmethod
    def tool_messages(llm_tool_calls: list[dict[str, Any]], outcome: DispatchOutcome) -> list[dict[str, Any]]:
        """Build the `role: tool` messages for the narration round-trip."""
        messages: list[dict[str, Any]] = []
        by_tool: dict[str, list[ChartResult]] = {}
        for result in outcome.results:
            by_tool.setdefault(result.tool, []).append(result)

        for call in llm_tool_calls or []:
            bucket = by_tool.get(call.get("name"))
            if not bucket:
                continue
            result = bucket.pop(0)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": call.get("name"),
                    "content": json.dumps(result.as_tool_message(), default=str),
                }
            )
        return messages

    @staticmethod
    def viz_state(outcome: DispatchOutcome) -> Optional[dict[str, Any]]:
        """What gets cached at `user:{id}:viz_state` for cross-turn continuity."""
        if not outcome.executed:
            return None
        last = outcome.executed[-1]
        result = next((r for r in outcome.results if r.tool == last["name"]), None)
        state = {
            "chart_type": last["name"],
            "filters": {k: v for k, v in last["args"].items() if k not in {"user_id", "theme"}},
        }
        if result is not None:
            state["period"] = result.summary.get("period")
            state["axes"] = {
                "x": "month" if "monthly" in last["name"] or "income" in last["name"] else "category",
                "y": "amount",
            }
        return state
