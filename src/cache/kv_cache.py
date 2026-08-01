"""Key-value cache abstraction.

`KVCache` is deliberately tiny (get/set/delete). Everything the pipeline needs
beyond that -- TTL policy, the query-history ring buffer, key naming -- lives in
`UserCache`, so swapping the backend never touches pipeline logic.

`InMemoryKVCache` is the default: no external service, deterministic in tests,
and sufficient whenever one long-lived process serves every request.
`RedisKVCache` is what a deployment uses once that stops being true -- several
replicas, or serverless invocations that each start with empty memory. The
choice is `CACHE_BACKEND`; nothing above this module changes.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Optional


class KVCache(ABC):
    @abstractmethod
    def get(self, key: str) -> Optional[Any]: ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: int) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    def get_many(self, keys: list[str]) -> dict[str, Any]:
        return {k: v for k in keys if (v := self.get(k)) is not None}


class InMemoryKVCache(KVCache):
    """Thread-safe dict cache with lazy TTL expiry.

    Values are deep-copied on the way in and out so a caller mutating a returned
    dict can't corrupt the cached copy -- the same isolation a serializing
    backend (Redis) gives you for free.
    """

    def __init__(self, time_fn=time.monotonic):
        self._store: dict[str, tuple[Any, Optional[float]]] = {}
        self._lock = threading.RLock()
        self._time = time_fn
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            value, expires_at = entry
            if expires_at is not None and self._time() >= expires_at:
                del self._store[key]
                self.misses += 1
                return None
            self.hits += 1
            return copy.deepcopy(value)

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        expires_at = self._time() + ttl_seconds if ttl_seconds and ttl_seconds > 0 else None
        with self._lock:
            self._store[key] = (copy.deepcopy(value), expires_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    # -- introspection (tests, /readyz, metrics) ------------------------------

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._store)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def ping(self) -> bool:
        return True


class RedisKVCache(KVCache):
    """KVCache over redis-py, for deployments that are not a single process.

    The in-memory backend gives every process its own cache *and its own
    rate-limit buckets*. Across N replicas that quietly multiplies each user's
    allowance by N. On serverless it is worse than quiet: an invocation is a
    fresh process, so the bucket is always full on arrival and the limit never
    binds at all. Shared state is what makes the limiter mean anything once the
    deployment stops being one long-lived container.

    Values are JSON, never pickle. This store is reachable by anything holding
    the connection string, and unpickling data you do not exclusively control is
    remote code execution. JSON also gives the same copy-on-read isolation the
    in-memory backend gets by deep-copying.

    One honest limitation: the token bucket is a read-modify-write across two
    round trips, so two simultaneous requests can both read the same balance and
    both spend it. The error is bounded by concurrency and always in the
    caller's favour; closing it needs the arithmetic to happen inside Redis (a
    Lua script or INCR-based counter), which is a different design than the
    KVCache interface describes.
    """

    def __init__(self, url: str, *, timeout_s: float = 2.0):
        # Local import so the dependency is only required by the deployment that
        # selects this backend.
        import redis  # noqa: PLC0415

        self._redis = redis.Redis.from_url(
            url,
            decode_responses=True,
            # A cache that hangs must not take the request with it. Failing fast
            # lets the limiter fail open and the pipeline treat it as a miss.
            socket_timeout=timeout_s,
            socket_connect_timeout=timeout_s,
            health_check_interval=30,
        )
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        raw = self._redis.get(key)
        if raw is None:
            self.misses += 1
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            # Written by a different schema or a different version. Dropping it
            # is right: a cache is never the authority for anything.
            self._redis.delete(key)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        # default=str so a date or Decimal in a summary serialises instead of
        # raising -- the pipeline's payloads are not all JSON primitives.
        payload = json.dumps(value, default=str)
        if ttl_seconds and ttl_seconds > 0:
            self._redis.setex(key, int(ttl_seconds), payload)
        else:
            self._redis.set(key, payload)

    def delete(self, key: str) -> None:
        self._redis.delete(key)

    def get_many(self, keys: list[str]) -> dict[str, Any]:
        """MGET, so warming a user's three cache entries is one round trip.

        On a serverless invocation the connection is cold and every round trip
        is paid in full, which makes the default get-in-a-loop noticeably worse
        than it looks in a container.
        """
        if not keys:
            return {}
        found: dict[str, Any] = {}
        for key, raw in zip(keys, self._redis.mget(keys)):
            if raw is None:
                self.misses += 1
                continue
            try:
                found[key] = json.loads(raw)
                self.hits += 1
            except (TypeError, ValueError):
                self._redis.delete(key)
                self.misses += 1
        return found

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except Exception:  # noqa: BLE001 -- reported as unready, never raised
            return False


def build_cache(settings) -> KVCache:
    """Factory driven by `CACHE_BACKEND`."""
    if settings.cache_backend == "redis":
        return RedisKVCache(settings.redis_url)
    return InMemoryKVCache()
