"""LLM layer: retry/backoff, model fallback, circuit breaker, prompt assembly.

Uses `httpx.MockTransport` — no network, no API key, no cost.
"""

from __future__ import annotations

import httpx
import pandas as pd
import pytest

from src.data.profile_builder import ProfileBuilder
from src.llm.openrouter_client import LLMUnavailableError, OpenRouterClient
from src.llm.prompt_builder import FLAG_CONTEXT_TRIMMED, PromptBuilder
from src.observability.circuit_breaker import BreakerState, CircuitBreaker


def completion(content="Hello", tool_calls=None, model="fake/model:free"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"id": "x", "model": model, "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"total_tokens": 42}}


@pytest.fixture
def fast_settings(settings):
    return settings.model_copy(
        update={
            "llm_backoff_base_s": 0.001,
            "llm_max_retries": 3,
            "model_fallback_chain": ["model/a:free", "model/b:free", "model/c:free"],
        }
    )


def make_client(fast_settings, handler, breaker=None):
    return OpenRouterClient(
        fast_settings, breaker=breaker, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


# == happy path ===============================================================


def test_successful_completion_is_parsed(fast_settings):
    client = make_client(fast_settings, lambda r: httpx.Response(200, json=completion("Hi there")))
    response = client.complete([{"role": "user", "content": "hi"}])
    assert response.content == "Hi there"
    assert response.has_tool_calls is False
    assert response.usage["total_tokens"] == 42


def test_tool_calls_are_normalized(fast_settings):
    raw = [{"id": "c1", "type": "function", "function": {"name": "plot_category_breakdown", "arguments": '{"top_n": 5}'}}]
    client = make_client(fast_settings, lambda r: httpx.Response(200, json=completion("", raw)))
    response = client.complete([{"role": "user", "content": "hi"}])
    assert response.tool_calls == [{"id": "c1", "name": "plot_category_breakdown", "arguments": '{"top_n": 5}'}]


def test_api_key_is_sent_and_never_in_the_payload(fast_settings):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json=completion())

    make_client(fast_settings, handler).complete([{"role": "user", "content": "hi"}])
    assert seen["auth"] == f"Bearer {fast_settings.openrouter_api_key}"
    assert fast_settings.openrouter_api_key not in seen["body"]


def test_missing_api_key_fails_fast(fast_settings):
    client = make_client(fast_settings.model_copy(update={"openrouter_api_key": ""}), lambda r: httpx.Response(200))
    with pytest.raises(LLMUnavailableError):
        client.complete([{"role": "user", "content": "hi"}])


# == retry and fallback =======================================================


def test_429_is_retried_then_succeeds(fast_settings):
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=completion("recovered"))

    assert make_client(fast_settings, handler).complete([{"role": "user", "content": "hi"}]).content == "recovered"
    assert attempts["n"] == 3


def test_5xx_exhausts_retries_then_falls_through_to_the_next_model(fast_settings):
    models_tried = []

    def handler(request):
        import json as _json

        model = _json.loads(request.content)["model"]
        models_tried.append(model)
        if model == "model/a:free":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json=completion("from b", model=model))

    response = make_client(fast_settings, handler).complete([{"role": "user", "content": "hi"}])
    assert response.content == "from b"
    assert models_tried.count("model/a:free") == fast_settings.llm_max_retries
    assert "model/b:free" in models_tried


def test_non_retryable_4xx_skips_straight_to_the_next_model(fast_settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(404, json={"error": "no endpoints"})
        return httpx.Response(200, json=completion("second model"))

    assert make_client(fast_settings, handler).complete([{"role": "user", "content": "hi"}]).content == "second model"
    assert calls["n"] == 2, "404 must not be retried"


def test_timeout_is_retryable(fast_settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("too slow", request=request)
        return httpx.Response(200, json=completion("after timeout"))

    assert make_client(fast_settings, handler).complete([{"role": "user", "content": "hi"}]).content == "after timeout"


def test_exhausting_the_whole_chain_raises_unavailable(fast_settings):
    client = make_client(fast_settings, lambda r: httpx.Response(500, json={"error": "down"}))
    with pytest.raises(LLMUnavailableError) as exc:
        client.complete([{"role": "user", "content": "hi"}])
    for model in fast_settings.model_fallback_chain:
        assert model in str(exc.value)


# == circuit breaker ==========================================================


def test_breaker_opens_after_threshold_and_short_circuits(fast_settings):
    breaker = CircuitBreaker(failure_threshold=2, cooldown_s=60)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"error": "down"})

    client = make_client(fast_settings, handler, breaker=breaker)
    for _ in range(2):
        with pytest.raises(LLMUnavailableError):
            client.complete([{"role": "user", "content": "hi"}])
    assert breaker.state is BreakerState.OPEN

    calls_before = calls["n"]
    with pytest.raises(LLMUnavailableError, match="circuit breaker open"):
        client.complete([{"role": "user", "content": "hi"}])
    assert calls["n"] == calls_before, "open breaker must not hit the network"


def test_breaker_half_opens_after_cooldown_then_closes_on_success():
    clock = type("C", (), {"t": 0.0, "__call__": lambda self: self.t})()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=30, time_fn=clock)
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN and not breaker.allows_request()
    clock.t = 31
    assert breaker.state is BreakerState.HALF_OPEN and breaker.allows_request()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_failed_probe_reopens_the_breaker():
    clock = type("C", (), {"t": 0.0, "__call__": lambda self: self.t})()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=30, time_fn=clock)
    breaker.record_failure()
    clock.t = 31
    assert breaker.state is BreakerState.HALF_OPEN
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    clock.t = 40
    assert breaker.state is BreakerState.OPEN, "cooldown restarts from the failed probe"


def test_success_resets_the_failure_count():
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED and breaker.consecutive_failures == 1


# == prompt builder ===========================================================


@pytest.fixture
def builder(settings, store):
    return PromptBuilder(settings, store.taxonomy)


@pytest.fixture
def profile(store):
    return ProfileBuilder(store).build(store.user_ids[0])


def test_prompt_contains_every_required_section(builder, profile, store):
    messages, _ = builder.build(
        user_id=store.user_ids[0],
        prompt="What did I spend the most on?",
        profile=profile,
        query_history=[{"prompt": "earlier question", "pandas_operation": "groupby", "result_summary": "HOUSING: $1"}],
        viz_state={"chart_type": "plot_category_breakdown"},
        as_of=store.as_of,
    )
    system = messages[0]["content"]
    assert "financial analyst" in system.lower()
    assert "transaction_amount" in system and "Negative = income" in system
    assert "HOUSING" in system
    assert profile["user_name"] in system
    assert "earlier question" in system, "few-shot history injected"
    assert "plot_category_breakdown" in system
    assert "2025-12-31" in system, "as-of anchor stated"
    assert "NEVER invent a number" in system
    assert messages[1] == {"role": "user", "content": "What did I spend the most on?"}


def test_no_history_means_no_few_shot_block(builder, profile, store):
    messages, _ = builder.build(store.user_ids[0], "hi", profile, query_history=[])
    assert "RECENT CONVERSATION" not in messages[0]["content"]


def test_over_budget_trims_history_before_the_question(builder, profile, store, settings):
    tiny = settings.model_copy(update={"token_budget_input": 400})
    builder.settings = tiny
    history = [{"prompt": "q" * 500, "pandas_operation": "op" * 200, "result_summary": "r" * 500} for _ in range(5)]
    messages, flags = builder.build(store.user_ids[0], "What did I spend?", profile, query_history=history)
    assert FLAG_CONTEXT_TRIMMED in flags
    assert messages[1]["content"] == "What did I spend?", "the user's question is never trimmed"


def test_truncation_notice_reaches_the_model(builder, profile, store):
    messages, _ = builder.build(store.user_ids[0], "hi", profile, notice="[Note: shortened.]")
    assert "shortened" in messages[1]["content"]


# == providers ================================================================
# Groq and OpenRouter speak the same OpenAI chat-completions shape, so they are
# data (base URL + key + models), not a second client.

from src.config import Settings  # noqa: E402
from src.llm.providers import build_providers  # noqa: E402


def test_a_provider_without_a_key_is_absent_not_an_error():
    assert build_providers(Settings(groq_api_key="", openrouter_api_key="")) == []
    names = [p.name for p in build_providers(Settings(groq_api_key="k", openrouter_api_key=""))]
    assert names == ["groq"]


def test_auto_uses_groq_as_the_fallback():
    """OpenRouter is primary; Groq catches a spent quota or an outage."""
    names = [p.name for p in build_providers(Settings(groq_api_key="k", openrouter_api_key="k"))]
    assert names == ["openrouter", "groq"]


@pytest.mark.parametrize("pinned", ["groq", "openrouter"])
def test_naming_a_provider_pins_it(pinned):
    settings = Settings(groq_api_key="k", openrouter_api_key="k", llm_provider=pinned)
    assert [p.name for p in build_providers(settings)] == [pinned]


def test_groq_endpoint_and_auth_shape():
    provider = build_providers(Settings(groq_api_key="gsk_secret", openrouter_api_key=""))[0]
    assert provider.endpoint == "https://api.groq.com/openai/v1/chat/completions"
    assert provider.headers()["Authorization"] == "Bearer gsk_secret"
    # OpenRouter's attribution headers must not be sent to Groq.
    assert "HTTP-Referer" not in provider.headers()


def test_client_falls_through_from_one_provider_to_the_next(monkeypatch):
    """Exhausting one provider's models is a reason to try the next provider,
    not to fail the request."""
    from src.llm.openrouter_client import LLMError, OpenRouterClient

    settings = Settings(groq_api_key="k", openrouter_api_key="k", llm_max_retries=1)
    client = OpenRouterClient(settings)
    seen: list[str] = []

    def fake_post(provider, payload):
        seen.append(f"{provider.name}/{payload['model']}")
        if provider.name == "openrouter":
            raise LLMError("openrouter is down")
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "model": payload["model"],
        }

    monkeypatch.setattr(client, "_post_with_retry", fake_post)
    result = client.complete([{"role": "user", "content": "hi"}])

    assert result.content == "ok"
    assert client.last_provider_used == "groq"
    assert [s.split("/")[0] for s in seen[:3]] == ["openrouter", "openrouter", "openrouter"]
    assert seen[-1].startswith("groq/")


def test_no_provider_configured_is_a_clean_unavailable():
    from src.llm.openrouter_client import LLMUnavailableError, OpenRouterClient

    client = OpenRouterClient(Settings(groq_api_key="", openrouter_api_key=""))
    with pytest.raises(LLMUnavailableError, match="no LLM provider is configured"):
        client.complete([{"role": "user", "content": "hi"}])


# == the three-tier fallback ==================================================
# OpenRouter -> Groq -> no LLM at all. The third tier is the one that matters
# operationally: when every provider is unreachable the product must still
# answer from the data rather than showing an error.


def test_no_llm_tier_still_answers_from_the_data(raw_df, tmp_path):
    from src.llm.openrouter_client import LLMUnavailableError, OpenRouterClient
    from src.pipeline import TransactionRAGPipeline

    class EveryProviderDown(OpenRouterClient):
        def complete(self, *args, **kwargs):
            raise LLMUnavailableError("openrouter and groq both unreachable")

    settings = Settings(
        openrouter_api_key="x",
        groq_api_key="y",
        chart_output_dir=tmp_path / "charts",
        audit_log_path=None,
    )
    pipeline = TransactionRAGPipeline(
        df=raw_df, settings=settings, llm_client=EveryProviderDown(settings)
    )

    for prompt, expected_tool in (
        ("What did I spend the most on last month?", "plot_category_breakdown"),
        ("Am I saving money?", "plot_income_vs_expense"),
        ("Show me my spending trend", "plot_monthly_spending_trend"),
    ):
        result = pipeline.run(raw_df["user_id"].iloc[0], prompt)

        assert result["degraded"] is True
        assert "llm_unavailable" in result["guardrail_flags"]
        # Still the right chart, still rendered, still real figures.
        assert expected_tool in result["data_summary"]
        assert result["visualizations"], "degraded mode must still produce the chart"
        assert result["response"].strip(), "degraded mode must still say something"
        # And no traceback ever reaches the caller.
        assert result.get("error") is None


def test_degraded_prose_uses_human_month_labels(raw_df, tmp_path):
    """`2025-11` leaking into an answer is a bug users see."""
    from src.llm.openrouter_client import LLMUnavailableError, OpenRouterClient
    from src.pipeline import TransactionRAGPipeline

    class Down(OpenRouterClient):
        def complete(self, *args, **kwargs):
            raise LLMUnavailableError("down")

    settings = Settings(openrouter_api_key="x", chart_output_dir=tmp_path / "c", audit_log_path=None)
    pipeline = TransactionRAGPipeline(df=raw_df, settings=settings, llm_client=Down(settings))
    response = pipeline.run(raw_df["user_id"].iloc[0], "Show me my spending trend")["response"]

    assert "2025-11" not in response
    assert "November 2025" in response
