"""Last-resort degradation: OpenRouter -> Groq -> scripted offline answers.

`OpenRouterClient` already walks every configured provider and every model on
it. This wraps that walk so that its *exhaustion* is not the end of the request:
when no live model can be reached, the scripted client answers instead.

The trade is deliberate and worth stating plainly. An offline answer is produced
by keyword routing, not by a model, so it is a weaker interpretation of the
question -- but the numbers in it are computed by the same tools against the
same data, so it is never *wrong*, only less nuanced. For a public demo running
on free-tier keys, a slightly blunter answer beats an error page, which is the
only other thing a spent quota can produce.

Two things keep it honest:

  * `last_model_used` reports `scripted/offline`, so `/query` responses and the
    dev panel say which brain answered. A degraded answer never claims to be a
    live one.
  * The circuit breaker still records the failure. Falling back does not mask an
    outage from `/readyz` -- readiness reports degraded exactly as before.

Set `OFFLINE_FALLBACK=false` to switch it off and get the hard failure back,
which is what you want in a test that asserts on outage behaviour.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from .openrouter_client import LLMError, LLMResponse, LLMUnavailableError

log = logging.getLogger(__name__)


class FallbackLLMClient:
    """Delegates to `primary`, and to a scripted client when primary gives out.

    `secondary_factory` is called at most once and only on the first failure, so
    the offline path costs nothing while the live providers are healthy.
    """

    def __init__(self, primary: Any, secondary_factory: Callable[[], Any]):
        self.primary = primary
        self._secondary_factory = secondary_factory
        self._secondary: Optional[Any] = None
        #: True once a request has been served offline. Sticky, so it is visible
        #: to health output rather than only to the request that tripped it.
        self.degraded = False

    # -- introspection expected by health(), metrics and the dev panel --------

    @property
    def providers(self) -> list:
        return getattr(self.primary, "providers", [])

    @property
    def last_model_used(self) -> Optional[str]:
        source = self._secondary if self.degraded else self.primary
        return getattr(source, "last_model_used", None)

    @property
    def last_provider_used(self) -> Optional[str]:
        source = self._secondary if self.degraded else self.primary
        return getattr(source, "last_provider_used", None)

    # -- completion ------------------------------------------------------------

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        tool_choice: str = "auto",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        try:
            response = self.primary.complete(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except (LLMUnavailableError, LLMError) as exc:
            # Includes the breaker-open case: once it is open, every turn goes
            # offline until the cooldown lets a live call through again.
            log.warning("live providers unavailable, answering offline: %s", exc)
            self.degraded = True
            return self._offline().complete(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        # A turn that succeeds live clears the sticky flag: the deployment is
        # only "degraded" for as long as it is actually failing over.
        self.degraded = False
        return response

    def _offline(self) -> Any:
        if self._secondary is None:
            self._secondary = self._secondary_factory()
        return self._secondary

    def close(self) -> None:
        for client in (self.primary, self._secondary):
            if client is not None and hasattr(client, "close"):
                client.close()
