"""Consecutive-failure circuit breaker.

Wraps the whole LLM call (across the full model fallback chain), so a sustained
OpenRouter outage costs one timeout, not one per request per model.

States: CLOSED -> (N consecutive failures) -> OPEN -> (cooldown elapses) ->
HALF_OPEN -> (one success) -> CLOSED, or (one failure) -> OPEN again.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Optional


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 60.0, time_fn=time.monotonic):
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_s = float(cooldown_s)
        self._time = time_fn
        self._lock = threading.RLock()
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._state = BreakerState.CLOSED

    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._failures

    def allows_request(self) -> bool:
        return self.state is not BreakerState.OPEN

    def _maybe_half_open(self) -> None:
        if (
            self._state is BreakerState.OPEN
            and self._opened_at is not None
            and self._time() - self._opened_at >= self.cooldown_s
        ):
            self._state = BreakerState.HALF_OPEN

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._state = BreakerState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._maybe_half_open()
            if self._state is BreakerState.HALF_OPEN:
                # The probe failed: straight back to open, restart the cooldown.
                self._failures = self.failure_threshold
                self._opened_at = self._time()
                self._state = BreakerState.OPEN
                return
            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._opened_at = self._time()
                self._state = BreakerState.OPEN

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._state = BreakerState.CLOSED
