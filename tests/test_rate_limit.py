"""Per-user rate limiting.

Driven by an injected clock rather than `sleep`, so the refill curve is asserted
exactly and the suite stays fast and deterministic.
"""

from __future__ import annotations

import pytest

from src.cache.kv_cache import InMemoryKVCache
from src.observability.rate_limit import RateLimiter


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def limiter(clock):
    # 60/min == 1 token per second, burst 3. Round numbers make the arithmetic
    # in each assertion obvious.
    return RateLimiter(InMemoryKVCache(), rate_per_minute=60, burst=3, time_fn=clock)


def test_burst_is_spendable_immediately_then_refused(limiter):
    assert [limiter.check("u").allowed for _ in range(3)] == [True, True, True]
    refused = limiter.check("u")
    assert refused.allowed is False
    assert refused.retry_after == pytest.approx(1.0, abs=0.01)


def test_tokens_refill_at_the_configured_rate(limiter, clock):
    for _ in range(3):
        limiter.check("u")
    assert not limiter.check("u").allowed

    clock.advance(1.0)  # exactly one token
    assert limiter.check("u").allowed
    assert not limiter.check("u").allowed

    clock.advance(2.0)  # two more
    assert limiter.check("u").allowed
    assert limiter.check("u").allowed
    assert not limiter.check("u").allowed


def test_refill_is_capped_at_the_burst(limiter, clock):
    """A long quiet period must not bank an unlimited allowance."""
    for _ in range(3):
        limiter.check("u")
    clock.advance(3600)  # an hour of refill
    assert [limiter.check("u").allowed for _ in range(4)] == [True, True, True, False]


def test_buckets_are_per_subject(limiter):
    for _ in range(3):
        limiter.check("alice")
    assert not limiter.check("alice").allowed
    assert limiter.check("bob").allowed, "one user must not consume another's allowance"


def test_a_backwards_clock_does_not_lock_a_user_out(limiter, clock):
    """NTP stepping the clock back would otherwise produce a negative refill."""
    limiter.check("u")
    clock.advance(-300)
    decision = limiter.check("u")
    assert decision.allowed
    assert decision.remaining >= 0


def test_peek_reports_without_spending(limiter):
    before = limiter.check("u", peek=True)
    after = limiter.check("u", peek=True)
    assert before.remaining == after.remaining == 3


def test_rejection_still_advances_the_clock_so_time_is_not_refunded(limiter, clock):
    """If a refused call left `updated` stale, the next caller would be credited
    the same elapsed seconds twice."""
    for _ in range(3):
        limiter.check("u")
    clock.advance(0.5)
    limiter.check("u")  # refused, but banks the 0.5s of refill
    clock.advance(0.5)
    assert limiter.check("u").allowed, "0.5 + 0.5 seconds is one whole token"


def test_headers_tell_a_client_how_to_back_off(limiter):
    for _ in range(3):
        limiter.check("u")
    headers = limiter.check("u").headers
    assert headers["X-RateLimit-Limit"] == "60"
    assert headers["X-RateLimit-Remaining"] == "0"
    # Rounded up: a Retry-After that rounds down invites a retry that is still
    # too early.
    assert int(headers["Retry-After"]) >= 1


def test_allowed_responses_carry_the_allowance_but_no_retry_after(limiter):
    headers = limiter.check("u").headers
    assert "Retry-After" not in headers
    assert headers["X-RateLimit-Remaining"] == "2"


def test_disabled_limiter_always_allows():
    limiter = RateLimiter(InMemoryKVCache(), rate_per_minute=1, burst=1, enabled=False)
    assert all(limiter.check("u").allowed for _ in range(50))


def test_it_fails_open_when_the_cache_is_unreachable():
    """A limiter that takes the service down when *it* breaks has inverted its
    own purpose."""

    class Broken:
        def get(self, key):
            raise RuntimeError("redis unreachable")

        def set(self, key, value, ttl):
            raise RuntimeError("redis unreachable")

    assert RateLimiter(Broken(), rate_per_minute=1, burst=1).check("u").allowed


def test_reset_clears_a_subject(limiter):
    for _ in range(3):
        limiter.check("u")
    assert not limiter.check("u").allowed
    limiter.reset("u")
    assert limiter.check("u").allowed
