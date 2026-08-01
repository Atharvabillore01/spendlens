"""Per-turn trace log: what the model chose, and what the data actually said.

This is the counterpart to `AuditLogger`, and the two exist for opposite
reasons. The audit log is the *production* record -- deliberately redacted, no
amounts, safe to ship to a log aggregator. It therefore cannot answer "is this
number right?", which is exactly the question you have when a chart looks wrong.

This logger answers that question, so it prints **real financial figures in
clear**. It is off by default and must stay off anywhere real user data lives.

    TRACE_TURNS=1 uvicorn api:app --reload           # to the console
    TRACE_TURNS=1 TRACE_LOG_PATH=output/trace.jsonl  # and to a file, as JSONL

Each turn emits one block:

    ── turn usr_a1b2c3d4 "break down my housing spending"
       guardrails   allowed  flags=[] context=yes
       cache        profile HIT
       model        inclusionai/ling-3.0-flash:free
       tool call    plot_category_breakdown(period='last_3_months',
                                            parent_category='HOUSING')
       computed     total_spend=$5,210.00  top=RENT $4,950.00 (95.0%)
                    grouped_by=subcategory  period=2025-10..2025-12  rows=14
       output       flags=[] stripped=0
       answer       "You spent $5,210.00 in October-December 2025..."
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("transaction_rag.trace")


def _money(value: Any) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"-${abs(n):,.2f}" if n < 0 else f"${n:,.2f}"


def _format_args(args: dict[str, Any]) -> str:
    """`period='last_month', months=6` -- reads like the call the model made."""
    parts = []
    for key, value in (args or {}).items():
        if value is None:
            continue
        parts.append(f"{key}={value!r}" if isinstance(value, str) else f"{key}={value}")
    return ", ".join(parts)


@dataclass
class TurnTrace:
    """Collects one turn's decisions, then emits them as a single block.

    Buffered rather than logged line-by-line so concurrent requests can't
    interleave into an unreadable transcript.
    """

    user_id: str
    prompt: str
    enabled: bool = True
    log_path: Optional[Path] = None
    lines: list[str] = field(default_factory=list)
    record: dict[str, Any] = field(default_factory=dict)

    def _add(self, label: str, body: str, **fields: Any) -> None:
        if not self.enabled:
            return
        self.lines.append(f"   {label:<12} {body}")
        if fields:
            self.record.setdefault(label.strip(), {}).update(fields)

    # -- stages ---------------------------------------------------------------

    def guardrails(self, allowed: bool, flags: list[str], has_context: bool) -> None:
        verdict = "allowed" if allowed else "BLOCKED"
        self._add(
            "guardrails",
            f"{verdict}  flags={flags or []} context={'yes' if has_context else 'no'}",
            allowed=allowed,
            flags=list(flags or []),
            has_context=has_context,
        )

    def cache(self, hit: bool, history_len: int) -> None:
        self._add(
            "cache",
            f"profile {'HIT' if hit else 'MISS'}  history={history_len} entries",
            profile_hit=hit,
            history_entries=history_len,
        )

    def model(self, name: Optional[str], degraded: bool = False) -> None:
        suffix = "  (degraded — no model reached)" if degraded else ""
        self._add("model", f"{name or '—'}{suffix}", name=name, degraded=degraded)

    def tool_calls(self, executed: list[dict[str, Any]]) -> None:
        if not executed:
            self._add("tool call", "none — answered from the profile", calls=[])
            return
        for call in executed:
            self._add("tool call", f"{call.get('name')}({_format_args(call.get('args', {}))})")
        self.record["tool call"] = {"calls": executed}

    def computed(self, tool: str, summary: dict[str, Any]) -> None:
        """The figures the chart was actually built from."""
        if not self.enabled:
            return
        bits: list[str] = []
        if "total_spend" in summary:
            bits.append(f"total_spend={_money(summary['total_spend'])}")
        if summary.get("top_category"):
            top = summary["top_category"]
            share = f" ({top['share_pct']}%)" if "share_pct" in top else ""
            bits.append(f"top={top.get('name')} {_money(top.get('amount'))}{share}")
        for key in ("total_income", "net_savings", "average_monthly_spend"):
            if key in summary and summary[key] is not None:
                bits.append(f"{key}={_money(summary[key])}")
        self._add("computed", f"{tool}: " + "  ".join(bits) if bits else f"{tool}: (no figures)")

        detail = [
            f"period={summary.get('period')}",
            f"rows={summary.get('transaction_count', '—')}",
        ]
        if summary.get("grouped_by"):
            detail.append(f"grouped_by={summary['grouped_by']}")
        if summary.get("parent_category"):
            detail.append(f"parent={summary['parent_category']}")
        self._add("", "  ".join(detail))

        # Full series, so a wrong bar can be traced to a wrong number.
        for row in summary.get("monthly_totals", []) or []:
            self._add("", f"  {row['month']}  {_money(row['expense'])}")
        for row in summary.get("monthly", []) or []:
            self._add(
                "",
                f"  {row['month']}  in {_money(row['income'])}  "
                f"out {_money(row['expense'])}  net {_money(row['net'])}",
            )
        # `categories` carries two shapes: a breakdown row (amount + share) and
        # a comparison row (current vs previous + delta). Render whichever it is
        # rather than assuming -- a trace that raises is worse than no trace.
        for row in summary.get("categories", []) or []:
            name = str(row.get("name", "?"))
            if "amount" in row:
                self._add("", f"  {name:<16} {_money(row['amount'])}  {row.get('share_pct', '')}%")
            elif "delta" in row:
                pct = f"  {row['delta_pct']:+.1f}%" if row.get("delta_pct") is not None else "  (new)"
                self._add(
                    "",
                    f"  {name:<16} {_money(row.get('previous', 0))} -> "
                    f"{_money(row.get('current', 0))}   {_money(row['delta']):>10}{pct}",
                )
        self.record.setdefault("computed", {})[tool] = summary

    def output(self, flags: list[str], stripped: int = 0) -> None:
        self._add(
            "output",
            f"flags={flags or []} stripped={stripped}",
            flags=list(flags or []),
            stripped=stripped,
        )

    def answer(self, text: str) -> None:
        clipped = " ".join((text or "").split())
        if len(clipped) > 160:
            clipped = clipped[:160] + "…"
        self._add("answer", f'"{clipped}"', text=text)

    # -- emit -----------------------------------------------------------------

    def emit(self) -> None:
        if not self.enabled or not self.lines:
            return
        header = f'── turn {self.user_id} "{" ".join(self.prompt.split())[:70]}"'
        logger.info("\n".join([header, *self.lines]))

        if self.log_path:
            self.record["user_id"] = self.user_id
            self.record["prompt"] = self.prompt
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(self.record, ensure_ascii=False, default=str) + "\n")
            except OSError as exc:  # never let tracing break a request
                logger.debug("could not write trace file: %s", exc)


def configure(level: str = "INFO") -> None:
    """Attach a plain handler to the trace logger.

    Kept separate from the app's own logging setup so enabling traces never
    reconfigures the root logger out from under the host process.
    """
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logger.propagate = False
