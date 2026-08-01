"""Key-value cache abstraction.

`KVCache` is deliberately tiny (get/set/delete). Everything the pipeline needs
beyond that -- TTL policy, the query-history ring buffer, key naming -- lives in
`UserCache`, so swapping the backend never touches pipeline logic.

`InMemoryKVCache` is the shipped backend: no external service, deterministic in
tests, and sufficient for every caching behaviour the brief asks for. A Redis
implementation is a ~20-line subclass against this same interface and is what a
multi-instance deployment would use (see README §Scaling).
"""

from __future__ import annotations

import copy
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


def build_cache(settings) -> KVCache:
    """Factory driven by `CACHE_BACKEND`."""
    if settings.cache_backend == "redis":  # pragma: no cover - not shipped
        raise NotImplementedError(
            "RedisKVCache is intentionally not shipped in this submission. "
            "Implement KVCache against redis-py and set CACHE_BACKEND=redis; "
            "no other code changes are required."
        )
    return InMemoryKVCache()
