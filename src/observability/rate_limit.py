"""Per-user rate limiting: a token bucket on the shared cache backend.

Why this exists, concretely: a live run against Groq hit a tokens-per-minute
ceiling mid-request. Upstream quota is a shared resource, and without a limit in
front of it one user in a loop degrades the service for everyone else on the
same key -- the failure is not "that user is throttled", it is "the product
stopped working".

**Token bucket, not a fixed window.** A fixed window lets someone spend the
whole allowance in its last second and the whole next allowance in the first,
so the real burst is double the configured rate at the boundary. A bucket
refills continuously, so the sustained rate and the burst are two numbers an
operator can set independently and reason about.

**State lives in the cache, not the process.** The moment there is more than one
worker, an in-process counter limits each worker separately and the effective
limit is silently multiplied by the worker count. Backing it with the same
`KVCache` the rest of the pipeline uses means Redis makes it correct across
instances, with no code change here.

The check is deliberately fail-open: if the cache backend is unreachable, a
request is allowed rather than refused. A rate limiter that takes the service
down when *it* breaks has inverted its own purpose.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("transaction_rag.ratelimit")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: float
    #: Seconds until one more token is available. 0 when a token is in hand.
    retry_after: float
    limit: int

    @property
    def headers(self) -> dict[str, str]:
        """Standard-ish headers, so a client can back off without guessing."""
        out = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, int(self.remaining))),
        }
        if not self.allowed:
            # Ceil: a Retry-After that rounds down invites an immediate retry
            # that is still too early.
            out["Retry-After"] = str(max(1, int(self.retry_after + 0.999)))
        return out


class RateLimiter:
    """A refilling bucket per key.

    `rate_per_minute` is the sustained allowance; `burst` is how much may be
    spent at once after a quiet period. Burst defaults to the per-minute rate,
    which is the least surprising behaviour.
    """

    def __init__(
        self,
        cache,
        rate_per_minute: int,
        burst: Optional[int] = None,
        enabled: bool = True,
        namespace: str = "ratelimit",
        time_fn=time.time,
    ):
        # The clock is injectable so the refill curve can be asserted without
        # sleeping. A rate limiter tested against wall-clock time is a flaky
        # test that eventually gets deleted.
        self._time = time_fn
        self.cache = cache
        self.rate_per_minute = max(1, int(rate_per_minute))
        self.burst = max(1, int(burst if burst is not None else rate_per_minute))
        self.enabled = enabled
        self.namespace = namespace

    def _key(self, subject: str) -> str:
        return f"{self.namespace}:{subject}"

    def check(self, subject: str, cost: float = 1.0, peek: bool = False) -> Decision:
        """Spend `cost` tokens for `subject`, or report how long until it can.

        `peek` inspects without spending -- useful for reporting remaining
        allowance on a request that was rejected for some other reason.
        """
        if not self.enabled:
            return Decision(True, float(self.burst), 0.0, self.rate_per_minute)

        key = self._key(subject)
        now = self._time()
        per_second = self.rate_per_minute / 60.0

        try:
            state = self.cache.get(key) or {}
            tokens = float(state.get("tokens", self.burst))
            updated = float(state.get("updated", now))
        except Exception as exc:  # noqa: BLE001 -- fail open, see module docstring
            log.warning("rate limiter cache read failed, allowing request: %s", exc)
            return Decision(True, float(self.burst), 0.0, self.rate_per_minute)

        # Refill for the elapsed time, capped at the burst size. `max(0, …)`
        # guards a clock that stepped backwards, which would otherwise hand out
        # a negative refill and lock the key out.
        elapsed = max(0.0, now - updated)
        tokens = min(float(self.burst), tokens + elapsed * per_second)

        if tokens >= cost:
            if not peek:
                tokens -= cost
                self._store(key, tokens, now)
            return Decision(True, tokens, 0.0, self.rate_per_minute)

        # Persist the refill even on rejection, so `updated` does not drift and
        # the next caller is not refunded the same elapsed time twice.
        if not peek:
            self._store(key, tokens, now)
        needed = cost - tokens
        return Decision(False, tokens, needed / per_second, self.rate_per_minute)

    def _store(self, key: str, tokens: float, now: float) -> None:
        # TTL covers a full refill from empty, plus slack. Beyond that the
        # entry is indistinguishable from a fresh bucket, so keeping it wastes
        # memory in exchange for nothing.
        ttl = int((self.burst / (self.rate_per_minute / 60.0)) * 2) + 60
        try:
            self.cache.set(key, {"tokens": tokens, "updated": now}, ttl)
        except Exception as exc:  # noqa: BLE001
            log.warning("rate limiter cache write failed: %s", exc)

    def reset(self, subject: str) -> None:
        try:
            self.cache.delete(self._key(subject))
        except Exception as exc:  # noqa: BLE001
            log.warning("rate limiter reset failed: %s", exc)
