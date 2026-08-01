"""One process, many tenants.

A `TransactionRAGPipeline` is bound to a tenant: its taxonomy, tool schemas and
guardrail vocabulary are all derived from that tenant's own data. Building one
per request would mean re-reading the category vocabulary on every call;
building one per tenant and keeping it forever would mean unbounded growth as
tenants come and go.

So: a small LRU with a TTL. What each entry holds is the important part —
schemas, a taxonomy and a store *handle*, but no transactions. With the SQL
backend an idle tenant's pipeline costs kilobytes, which is what makes "keep a
few hundred warm" a reasonable default rather than a memory leak.

The TTL matters for correctness too, not just eviction: a tenant that uploads
new data gets a rebuilt taxonomy within `tenant_cache_ttl_s`, and `invalidate`
makes that immediate at the end of an ingest.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

from .cache.kv_cache import KVCache
from .config import Settings, get_settings
from .data.sql_store import SqlUserDataStore
from .db.engine import get_engine
from .observability.audit_logger import AuditLogger
from .pipeline import TransactionRAGPipeline

log = logging.getLogger("transaction_rag.tenancy")


class _Entry:
    __slots__ = ("pipeline", "created_at")

    def __init__(self, pipeline: TransactionRAGPipeline, created_at: float):
        self.pipeline = pipeline
        self.created_at = created_at


class TenantPipelineCache:
    """Thread-safe LRU of per-tenant pipelines."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        cache: Optional[KVCache] = None,
        llm_client: Optional[Any] = None,
        audit_logger: Optional[AuditLogger] = None,
        engine: Optional[Any] = None,
        builder: Optional[Callable[[str], TransactionRAGPipeline]] = None,
    ):
        self.settings = settings or get_settings()
        self.cache = cache
        self.llm_client = llm_client
        self.audit_logger = audit_logger or AuditLogger(self.settings.audit_log_path)
        self.engine = engine if engine is not None else get_engine(self.settings)
        self._builder = builder
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()

    # -- construction ---------------------------------------------------------

    def _build(self, tenant_id: str) -> TransactionRAGPipeline:
        if self._builder is not None:
            return self._builder(tenant_id)

        store = SqlUserDataStore(
            self.engine,
            tenant_id,
            as_of_mode=self.settings.as_of_mode,
            metadata_ttl_s=self.settings.tenant_cache_ttl_s,
        )
        return TransactionRAGPipeline(
            store=store,
            settings=self.settings,
            cache=self.cache,
            llm_client=self.llm_client,
            audit_logger=self.audit_logger,
        )

    # -- access ---------------------------------------------------------------

    def get(self, tenant_id: str) -> TransactionRAGPipeline:
        now = time.monotonic()
        ttl = self.settings.tenant_cache_ttl_s

        with self._lock:
            entry = self._entries.get(tenant_id)
            if entry is not None and (now - entry.created_at) < ttl:
                self._entries.move_to_end(tenant_id)
                return entry.pipeline
            if entry is not None:
                del self._entries[tenant_id]

        # Built outside the lock: constructing a store touches the database, and
        # holding the lock across that would serialise every tenant's first
        # request behind whichever one is slowest.
        pipeline = self._build(tenant_id)

        with self._lock:
            existing = self._entries.get(tenant_id)
            if existing is not None:
                # Another thread won the race; keep theirs so callers in flight
                # never see two different pipelines for one tenant.
                self._entries.move_to_end(tenant_id)
                return existing.pipeline
            self._entries[tenant_id] = _Entry(pipeline, now)
            while len(self._entries) > self.settings.tenant_cache_size:
                evicted, _ = self._entries.popitem(last=False)
                log.debug("evicted tenant pipeline %s", evicted)

        return pipeline

    def invalidate(self, tenant_id: str) -> None:
        """Drop a tenant's pipeline. Call after ingest so the taxonomy reloads."""
        with self._lock:
            self._entries.pop(tenant_id, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    @property
    def warm_tenants(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = ["TenantPipelineCache"]
