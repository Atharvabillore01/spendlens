"""Cache layer: TTL, isolation, ring buffer, and the `cache_hit` contract."""

from __future__ import annotations

import pytest

from src.cache import keys
from src.cache.kv_cache import InMemoryKVCache
from src.cache.user_cache import UserCache


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cache(clock):
    return InMemoryKVCache(time_fn=clock)


# -- KVCache ------------------------------------------------------------------


def test_set_get_delete(cache):
    cache.set("k", {"v": 1}, 60)
    assert cache.get("k") == {"v": 1}
    cache.delete("k")
    assert cache.get("k") is None


def test_missing_key_returns_none(cache):
    assert cache.get("nope") is None


def test_ttl_expiry(cache, clock):
    cache.set("k", {"v": 1}, ttl_seconds=10)
    clock.advance(9)
    assert cache.get("k") == {"v": 1}
    clock.advance(2)
    assert cache.get("k") is None


def test_zero_ttl_means_no_expiry(cache, clock):
    cache.set("k", {"v": 1}, ttl_seconds=0)
    clock.advance(10_000)
    assert cache.get("k") == {"v": 1}


def test_values_are_isolated_from_caller_mutation(cache):
    """A caller mutating a returned dict must not corrupt the cached copy."""
    original = {"nested": {"n": 1}}
    cache.set("k", original, 60)
    original["nested"]["n"] = 99
    fetched = cache.get("k")
    assert fetched["nested"]["n"] == 1
    fetched["nested"]["n"] = 42
    assert cache.get("k")["nested"]["n"] == 1


def test_hit_and_miss_counters(cache):
    cache.set("k", {"v": 1}, 60)
    cache.get("k")
    cache.get("absent")
    assert (cache.hits, cache.misses) == (1, 1)
    assert cache.hit_rate == 0.5


# -- key naming ---------------------------------------------------------------


def test_key_patterns_match_the_brief():
    assert keys.profile("usr_1") == "user:usr_1:profile"
    assert keys.query_history("usr_1") == "user:usr_1:query_history"
    assert keys.viz_state("usr_1") == "user:usr_1:viz_state"


# -- UserCache ----------------------------------------------------------------


@pytest.fixture
def user_cache(cache, settings):
    return UserCache(cache, settings)


def test_profile_miss_then_hit(user_cache):
    calls = []

    def builder(uid):
        calls.append(uid)
        return {"user_id": uid, "avg_monthly_spend": 100.0}

    first, hit1 = user_cache.get_or_build_profile("usr_1", builder)
    second, hit2 = user_cache.get_or_build_profile("usr_1", builder)

    assert (hit1, hit2) == (False, True)
    assert first == second
    assert calls == ["usr_1"], "profile must be computed exactly once"


def test_profile_ttl_forces_a_recompute(user_cache, clock, settings):
    builder = lambda uid: {"user_id": uid}  # noqa: E731
    user_cache.get_or_build_profile("usr_1", builder)
    clock.advance(settings.profile_ttl_s + 1)
    _, hit = user_cache.get_or_build_profile("usr_1", builder)
    assert hit is False


def test_profiles_are_per_user(user_cache):
    user_cache.get_or_build_profile("usr_1", lambda uid: {"user_id": uid})
    _, hit = user_cache.get_or_build_profile("usr_2", lambda uid: {"user_id": uid})
    assert hit is False
    assert user_cache.get_profile("usr_1")["user_id"] == "usr_1"


def test_query_history_is_a_capped_ring_buffer(user_cache, settings):
    for i in range(settings.query_history_max_n + 3):
        user_cache.append_query_history("usr_1", {"prompt": f"q{i}", "result_summary": str(i)})
    history = user_cache.get_query_history("usr_1")
    assert len(history) == settings.query_history_max_n
    assert history[-1]["prompt"] == f"q{settings.query_history_max_n + 2}"
    assert history[0]["prompt"] == "q3", "oldest entries are evicted first"


def test_query_history_starts_empty(user_cache):
    assert user_cache.get_query_history("nobody") == []


def test_viz_state_round_trip(user_cache):
    user_cache.set_viz_state("usr_1", {"chart_type": "plot_category_breakdown", "period": "2025-11"})
    assert user_cache.get_viz_state("usr_1")["chart_type"] == "plot_category_breakdown"


def test_invalidate_clears_every_entry_for_the_user(user_cache):
    user_cache.set_profile("usr_1", {"a": 1})
    user_cache.append_query_history("usr_1", {"prompt": "q"})
    user_cache.set_viz_state("usr_1", {"chart_type": "x"})
    user_cache.set_profile("usr_2", {"a": 2})

    user_cache.invalidate_user("usr_1")

    assert user_cache.get_profile("usr_1") is None
    assert user_cache.get_query_history("usr_1") == []
    assert user_cache.get_viz_state("usr_1") is None
    assert user_cache.get_profile("usr_2") == {"a": 2}, "other users untouched"
