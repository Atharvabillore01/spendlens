"""Prometheus exposition.

The risk with a hand-rolled metrics layer is producing text a scraper silently
rejects, so these assert the *format* as well as the arithmetic.
"""

from __future__ import annotations

import pytest

from src.observability.metrics import LATENCY_BUCKETS, Metrics


@pytest.fixture
def m() -> Metrics:
    return Metrics()


def test_counters_accumulate_per_label_set(m):
    m.counter("thing_total", "help", kind="a")
    m.counter("thing_total", "help", kind="a")
    m.counter("thing_total", "help", kind="b")
    out = m.render()
    assert 'thing_total{kind="a"} 2' in out
    assert 'thing_total{kind="b"} 1' in out


def test_gauges_replace_rather_than_accumulate(m):
    m.gauge("temp", 5)
    m.gauge("temp", 9)
    assert "temp 9" in m.render()


def test_histogram_buckets_are_cumulative(m):
    """Prometheus histograms are cumulative: a 0.3s observation belongs to
    every bucket at or above 0.5, not only to one."""
    m.observe("dur_seconds", 0.3)
    out = m.render()
    assert 'dur_seconds_bucket{le="0.25"} 0' in out
    assert 'dur_seconds_bucket{le="0.5"} 1' in out
    assert 'dur_seconds_bucket{le="1.0"} 1' in out
    assert 'dur_seconds_bucket{le="+Inf"} 1' in out
    assert "dur_seconds_count 1" in out
    assert "dur_seconds_sum 0.3" in out


def test_every_series_has_a_type_line(m):
    m.counter("a_total", "A.")
    m.gauge("b_state", 1, "B.")
    m.observe("c_seconds", 1.0, "C.")
    out = m.render()
    for name, kind in (("a_total", "counter"), ("b_state", "gauge"), ("c_seconds", "histogram")):
        assert f"# TYPE {name} {kind}" in out
        assert f"# HELP {name}" in out


def test_label_values_are_escaped(m):
    """An unescaped quote produces text a scraper rejects outright."""
    m.counter("thing_total", "help", note='he said "hi"\nand left')
    line = next(l for l in m.render().splitlines() if l.startswith("thing_total"))
    assert '\\"hi\\"' in line
    assert "\n" not in line


def test_output_ends_with_a_newline(m):
    """The exposition format requires it; some scrapers drop the last line."""
    m.counter("a_total")
    assert m.render().endswith("\n")


# == the domain helpers =======================================================


def test_a_turn_records_the_things_worth_alerting_on(m):
    m.record_turn(
        latency_ms=1500,
        cache_hit=True,
        degraded=False,
        flags=["hallucination_corrected"],
        provider="groq",
        model="llama-3.3-70b-versatile",
        tools=["plot_category_breakdown"],
    )
    out = m.render()
    assert "ledger_turns_total 1" in out
    assert 'ledger_cache_lookups_total{result="hit"} 1' in out
    assert 'ledger_guardrail_trips_total{flag="hallucination_corrected"} 1' in out
    assert 'ledger_llm_requests_total{outcome="success",provider="groq"} 1' in out
    assert 'ledger_tool_calls_total{tool="plot_category_breakdown"} 1' in out
    assert "ledger_turn_duration_seconds_sum 1.5" in out


def test_a_degraded_turn_is_counted_separately(m):
    m.record_turn(latency_ms=40, cache_hit=False, degraded=True, flags=["llm_unavailable"])
    out = m.render()
    assert "ledger_degraded_turns_total 1" in out
    assert 'ledger_cache_lookups_total{result="miss"} 1' in out


def test_a_turn_with_no_model_records_no_llm_request(m):
    """A guardrail-blocked turn never reached a provider; counting it as an LLM
    request would overstate usage and hide the real fallback rate."""
    m.record_turn(latency_ms=2, cache_hit=False, degraded=False, flags=["injection_detected"])
    assert "ledger_llm_requests_total" not in m.render()


def test_provider_failures_are_distinguishable_from_successes(m):
    m.record_llm_failure("openrouter")
    m.record_turn(latency_ms=10, cache_hit=False, degraded=False, flags=[], provider="groq", model="x")
    out = m.render()
    assert 'ledger_llm_requests_total{outcome="failure",provider="openrouter"} 1' in out
    assert 'ledger_llm_requests_total{outcome="success",provider="groq"} 1' in out


def test_ingest_counts_batches_and_rows(m):
    m.record_ingest(rows=347, source="file")
    m.record_ingest(rows=3, source="paste")
    out = m.render()
    assert 'ledger_ingest_rows_total{source="file"} 347' in out
    assert 'ledger_ingest_batches_total{source="paste"} 1' in out


def test_latency_buckets_cover_a_degraded_turn_and_a_slow_one():
    """Sub-second (no LLM) and multi-second (live model) must both land in a
    bucket that is not the overflow."""
    assert min(LATENCY_BUCKETS) <= 0.05
    assert max(LATENCY_BUCKETS) >= 10
