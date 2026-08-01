"""OpenRouter chat-completions client.

Resilience layering, outermost first:
  CircuitBreaker  -- skip the network entirely during a sustained outage
    model chain   -- try each configured free model in order
      tenacity    -- exponential backoff on 429 / 5xx / timeout, per model
        httpx     -- hard per-request timeout

A failure that exhausts every retry for one model falls through to the next
model rather than failing the request; only exhausting the whole chain raises.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..observability.circuit_breaker import CircuitBreaker
from .providers import Provider, build_providers

log = logging.getLogger("transaction_rag.llm")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """Base for every LLM-layer failure the pipeline degrades on."""


class LLMUnavailableError(LLMError):
    """Whole fallback chain failed, or the circuit breaker is open."""


class RetryableHTTPError(LLMError):
    def __init__(self, status_code: int, body: str = ""):
        super().__init__(f"HTTP {status_code}: {body[:200]}")
        self.status_code = status_code


@dataclass
class LLMResponse:
    """Normalized shape of one assistant turn."""

    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: Optional[str] = None
    finish_reason: Optional[str] = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw_message: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class OpenRouterClient:
    def __init__(self, settings, breaker: Optional[CircuitBreaker] = None, http_client: Optional[httpx.Client] = None):
        self.settings = settings
        self.breaker = breaker or CircuitBreaker(
            settings.circuit_breaker_failure_threshold, settings.circuit_breaker_cooldown_s
        )
        self._client = http_client
        self._owns_client = http_client is None
        self.last_model_used: Optional[str] = None
        self.last_provider_used: Optional[str] = None
        # Providers are resolved once per client. A provider with no key is
        # absent from the chain rather than an error.
        self.providers: list[Provider] = build_providers(settings)

    # -- transport ------------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.settings.llm_timeout_s)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def _post_once(self, provider: Provider, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.post(
                provider.endpoint,
                headers=provider.headers(),
                json=payload,
                timeout=self.settings.llm_timeout_s,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise RetryableHTTPError(504, f"{type(exc).__name__}: {exc}") from exc

        if response.status_code in RETRYABLE_STATUS:
            raise RetryableHTTPError(response.status_code, response.text)
        if response.status_code >= 400:
            # 401/403/404 are configuration problems: retrying cannot help, so
            # fall through to the next model instead of burning the budget.
            raise LLMError(f"HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def _post_with_retry(self, provider: Provider, payload: dict[str, Any]) -> dict[str, Any]:
        retrying = retry(
            reraise=True,
            stop=stop_after_attempt(max(1, self.settings.llm_max_retries)),
            wait=wait_exponential(
                multiplier=self.settings.llm_backoff_base_s,
                exp_base=self.settings.llm_backoff_factor,
                max=8,
            ),
            retry=retry_if_exception_type(RetryableHTTPError),
        )
        return retrying(self._post_once)(provider, payload)

    # -- public API -----------------------------------------------------------

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: str = "auto",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        """Run one completion through the fallback chain.

        Raises `LLMUnavailableError` only when every model has been exhausted;
        the pipeline turns that into a degraded-mode answer, never a traceback.
        """
        if not self.providers:
            raise LLMUnavailableError(
                "no LLM provider is configured; set GROQ_API_KEY or OPENROUTER_API_KEY"
            )

        if not self.breaker.allows_request():
            raise LLMUnavailableError(
                f"circuit breaker open after {self.breaker.consecutive_failures} consecutive failures"
            )

        base_payload: dict[str, Any] = {
            "messages": messages,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.token_budget_output,
        }
        if tools:
            base_payload["tools"] = tools
            base_payload["tool_choice"] = tool_choice

        errors: list[str] = []
        # Providers outer, models inner: exhausting one provider's models is a
        # reason to try the next provider, not to give up.
        for provider in self.providers:
            for model in provider.models:
                payload = dict(base_payload, model=model)
                try:
                    data = self._post_with_retry(provider, payload)
                    parsed = self._parse(data, model)
                    self.breaker.record_success()
                    self.last_model_used = parsed.model or model
                    self.last_provider_used = provider.name
                    return parsed
                except (LLMError, RetryError, ValueError, KeyError) as exc:
                    errors.append(f"{provider.name}/{model}: {type(exc).__name__}: {exc}")
                    log.warning("%s/%s failed, falling through: %s", provider.name, model, exc)
                    continue

        self.breaker.record_failure()
        raise LLMUnavailableError("every provider and model failed | " + " | ".join(errors))

    # -- parsing --------------------------------------------------------------

    @staticmethod
    def _parse(data: dict[str, Any], model: str) -> LLMResponse:
        if data.get("error"):
            raise LLMError(str(data["error"])[:200])
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("response contained no choices")
        choice = choices[0]
        message = choice.get("message") or {}

        raw_calls = message.get("tool_calls") or []
        tool_calls = []
        for call in raw_calls:
            fn = call.get("function") or {}
            if not fn.get("name"):
                continue
            tool_calls.append(
                {
                    "id": call.get("id") or f"call_{len(tool_calls)}",
                    "name": fn["name"],
                    "arguments": fn.get("arguments") or "{}",
                }
            )

        return LLMResponse(
            content=(message.get("content") or "").strip(),
            tool_calls=tool_calls,
            model=data.get("model") or model,
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage") or {},
            raw_message=message,
        )
