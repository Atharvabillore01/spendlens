"""Prometheus metrics, in the text exposition format, with no dependency.

`prometheus_client` would do this too. It is not used because the whole surface
needed here is four metric types and a text encoder — about 120 lines — and the
library brings its own global registry, multiprocess mode and collector
lifecycle, none of which this service wants. If that changes, the names below
are the standard ones and swapping the backing implementation is contained.

What is deliberately measured, and why each earns its place:

  * **latency histogram** — an average hides the tail, and the tail is what a
    user experiences. Buckets are in seconds, chosen around the observed shape
    of a turn (sub-second when degraded, ~1-5s through a live model).
  * **cache hit rate** — the brief's central claim is that caching makes turn
    two fast. This is the number that proves or disproves it in production.
  * **guardrail trips by flag** — a sudden rise in `injection_detected` is an
    attack; a rise in `hallucination_corrected` is a model regression. They are
    different alerts, so they are different label values.
  * **provider outcomes** — which of OpenRouter/Groq actually served traffic,
    and how often the fallback was needed.
  * **circuit breaker state** — the one gauge that says "we are degraded right
    now" rather than "we were degraded at some point".

Every metric is a counter, gauge or histogram; nothing here is a timer that
resets, because a Prometheus scrape may be missed and a resettable metric loses
the interval it covered.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Iterable, Optional

# Seconds. The last bucket is deliberately generous: a request slower than 30s
# has already failed the user, and distinguishing 40s from 60s is not useful.
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in pairs)
    return "{" + inner + "}"


class Metrics:
    """A tiny, thread-safe registry.

    One instance per process. Cardinality is the failure mode of any metrics
    layer, so label values here are drawn only from closed sets — flag codes,
    provider names, tool names — never from user input.
    """

    def __init__(self, time_fn=time.time):
        self._lock = threading.Lock()
        self._time = time_fn
        self._counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple], float] = {}
        self._hist_buckets: dict[tuple[str, tuple], list[int]] = {}
        self._hist_sum: dict[tuple[str, tuple], float] = defaultdict(float)
        self._hist_count: dict[tuple[str, tuple], int] = defaultdict(int)
        self._help: dict[str, tuple[str, str]] = {}
        self.started_at = self._time()

    # -- recording ------------------------------------------------------------

    def counter(self, name: str, help_text: str = "", **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] += 1
            self._help.setdefault(name, (help_text, "counter"))

    def gauge(self, name: str, value: float, help_text: str = "", **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._gauges[key] = float(value)
            self._help.setdefault(name, (help_text, "gauge"))

    def observe(self, name: str, value: float, help_text: str = "", **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            if key not in self._hist_buckets:
                self._hist_buckets[key] = [0] * len(LATENCY_BUCKETS)
            for index, edge in enumerate(LATENCY_BUCKETS):
                if value <= edge:
                    self._hist_buckets[key][index] += 1
            self._hist_sum[key] += float(value)
            self._hist_count[key] += 1
            self._help.setdefault(name, (help_text, "histogram"))

    # -- domain helpers -------------------------------------------------------
    #
    # Callers use these rather than raw counter names, so a metric is spelled
    # one way in one place.

    def record_turn(
        self,
        *,
        latency_ms: int,
        cache_hit: bool,
        degraded: bool,
        flags: Iterable[str],
        provider: Optional[str] = None,
        model: Optional[str] = None,
        tools: Iterable[str] = (),
    ) -> None:
        self.counter("ledger_turns_total", "Turns served.")
        self.observe(
            "ledger_turn_duration_seconds", latency_ms / 1000.0, "Wall time for one turn."
        )
        self.counter(
            "ledger_cache_lookups_total",
            "Profile cache lookups by outcome.",
            result="hit" if cache_hit else "miss",
        )
        if degraded:
            self.counter("ledger_degraded_turns_total", "Turns answered without a model.")
        for flag in flags:
            self.counter("ledger_guardrail_trips_total", "Guardrail activations.", flag=flag)
        for tool in tools:
            self.counter("ledger_tool_calls_total", "Chart tools executed.", tool=tool)
        if provider:
            self.counter(
                "ledger_llm_requests_total",
                "LLM calls by provider and outcome.",
                provider=provider,
                outcome="success",
            )
        if model:
            self.counter("ledger_model_uses_total", "Turns served per model.", model=model)

    def record_llm_failure(self, provider: str) -> None:
        self.counter(
            "ledger_llm_requests_total",
            "LLM calls by provider and outcome.",
            provider=provider,
            outcome="failure",
        )

    def record_rate_limited(self, endpoint: str) -> None:
        self.counter("ledger_rate_limited_total", "Requests refused by the limiter.", endpoint=endpoint)

    def record_ingest(self, rows: int, source: str) -> None:
        self.counter("ledger_ingest_batches_total", "Ingest batches accepted.", source=source)
        with self._lock:
            key = ("ledger_ingest_rows_total", (("source", source),))
            self._counters[key] += rows
            self._help.setdefault("ledger_ingest_rows_total", ("Rows written by ingest.", "counter"))

    # -- exposition -----------------------------------------------------------

    def render(self) -> str:
        """Prometheus text format v0.0.4."""
        lines: list[str] = []
        with self._lock:
            by_name: dict[str, list[str]] = defaultdict(list)

            for (name, labels), value in sorted(self._counters.items()):
                by_name[name].append(f"{name}{_labels(labels)} {value:g}")
            for (name, labels), value in sorted(self._gauges.items()):
                by_name[name].append(f"{name}{_labels(labels)} {value:g}")

            for (name, labels), buckets in sorted(self._hist_buckets.items()):
                for edge, count in zip(LATENCY_BUCKETS, buckets):
                    bucket_labels = labels + (("le", str(edge)),)
                    by_name[name].append(f"{name}_bucket{_labels(bucket_labels)} {count}")
                total = self._hist_count[(name, labels)]
                by_name[name].append(f"{name}_bucket{_labels(labels + (('le', '+Inf'),))} {total}")
                by_name[name].append(f"{name}_sum{_labels(labels)} {self._hist_sum[(name, labels)]:g}")
                by_name[name].append(f"{name}_count{_labels(labels)} {total}")

            for name in sorted(by_name):
                help_text, kind = self._help.get(name, ("", "untyped"))
                if help_text:
                    lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} {kind}")
                lines.extend(by_name[name])

        return "\n".join(lines) + "\n"


#: Process-wide registry. One per process, as Prometheus expects.
metrics = Metrics()
