"""Engine construction and pooling.

One engine per process, shared by every request. SQLAlchemy's pool is
thread-safe; the engine is the thing you are *supposed* to share, and building
one per request is the classic way to exhaust a database's connection limit.

Pool sizing is a real constraint at scale, not a default worth ignoring:
Postgres allows a few hundred connections total, so `db_pool_size *
worker_count` has to stay under it. `pool_pre_ping` costs one round-trip per
checkout and buys immunity to connections killed by a proxy or a failover.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

log = logging.getLogger("transaction_rag.db")

_engine: Optional[Engine] = None
_engine_url: Optional[str] = None


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _is_memory_sqlite(url: str) -> bool:
    return _is_sqlite(url) and (":memory:" in url or "mode=memory" in url)


def build_engine(settings, url: Optional[str] = None) -> Engine:
    """Construct an engine for `url`, applying dialect-appropriate pooling."""
    target = url or settings.database_url

    if _is_sqlite(target):
        kwargs: dict = {"echo": settings.db_echo, "future": True}
        if _is_memory_sqlite(target):
            # An in-memory database is per-connection; a normal pool would hand
            # each thread its own empty one. StaticPool shares the single
            # connection so the schema stays visible across threads.
            kwargs["connect_args"] = {"check_same_thread": False}
            kwargs["poolclass"] = StaticPool
        else:
            Path(target.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False}
        engine = create_engine(target, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            # WAL lets readers proceed during a write, which matters because
            # ingestion and queries overlap.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    return create_engine(
        target,
        echo=settings.db_echo,
        future=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        pool_recycle=settings.db_pool_recycle_s,
        pool_pre_ping=True,
    )


def get_engine(settings, url: Optional[str] = None) -> Engine:
    """Process-wide engine, rebuilt only if the URL changes (tests do this)."""
    global _engine, _engine_url
    target = url or settings.database_url
    if _engine is None or _engine_url != target:
        _engine = build_engine(settings, target)
        _engine_url = target
        log.info("database engine ready (%s)", _engine.dialect.name)
    return _engine


def reset_engine() -> None:
    """Dispose the cached engine. Used by tests between databases."""
    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None


__all__ = ["build_engine", "get_engine", "reset_engine"]
