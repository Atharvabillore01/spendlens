"""Which LLM backends to try, in what order.

OpenRouter and Groq both speak the OpenAI `/chat/completions` shape, so they
differ only in base URL, auth header and model names. Keeping that difference in
data rather than in a second client means the retry, fallback, circuit-breaker
and parsing logic has exactly one implementation.

Order matters: with `LLM_PROVIDER=auto` OpenRouter is tried first and **Groq is
the fallback**, so a Groq outage or a spent quota degrades to OpenRouter's
breadth rather than failing the request. Naming a provider explicitly pins it
and disables the other, which is what you want when debugging one of them.

A provider with no key is not an error -- it is simply absent from the chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Provider:
    """One backend and the models to try on it."""

    name: str
    base_url: str
    api_key: str
    models: tuple[str, ...]
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.models)

    def headers(self) -> dict[str, str]:
        """The key is read at call time and never logged."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


def build_providers(settings: Any) -> list[Provider]:
    """Ordered, key-bearing providers for this configuration."""
    groq = Provider(
        name="groq",
        base_url=settings.groq_base_url,
        api_key=settings.groq_api_key,
        models=tuple(settings.groq_model_chain),
    )
    openrouter = Provider(
        name="openrouter",
        base_url=settings.openrouter_base_url,
        api_key=settings.openrouter_api_key,
        models=tuple(settings.model_fallback_chain),
        # Attribution headers; harmless if unset upstream.
        extra_headers={
            "HTTP-Referer": "https://localhost/transaction-rag-pipeline",
            "X-Title": "Transaction RAG Pipeline",
        },
    )

    preference = getattr(settings, "llm_provider", "auto")
    if preference == "groq":
        ordered = [groq]
    elif preference == "openrouter":
        ordered = [openrouter]
    else:
        # Primary first, fallback second.
        ordered = [openrouter, groq]

    return [p for p in ordered if p.configured]
