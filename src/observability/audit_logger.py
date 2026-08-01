"""Structured, PII-redacted audit logging.

The brief requires logging every request but explicitly forbids logging raw PII.
Financial prose *is* PII, so this logger records shape rather than content:
prompt length + SHA-256 prefix, response length + a scrubbed one-line summary,
latency, flags, cache hit, model. Never the raw prompt or raw response.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_CURRENCY_RE = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?")
_LONG_NUMBER_RE = re.compile(r"\b\d[\d,]{3,}(?:\.\d+)?\b")

logger = logging.getLogger("transaction_rag.audit")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def redact(text: str, limit: int = 120) -> str:
    """Strip amounts and clip -- enough to debug, not enough to leak finances."""
    scrubbed = _CURRENCY_RE.sub("$<amt>", text or "")
    scrubbed = _LONG_NUMBER_RE.sub("<num>", scrubbed)
    scrubbed = " ".join(scrubbed.split())
    return scrubbed[:limit] + ("…" if len(scrubbed) > limit else "")


#: In-memory tail. Bounded because a long-lived process would otherwise hold
#: every entry it has ever written for the life of the container -- the audit
#: sink is the log, not this list, which exists for tests and introspection.
_RECENT_MAX = 1000


class AuditLogger:
    def __init__(self, log_path: Optional[Path] = None, logger_obj: Optional[logging.Logger] = None):
        self.log = logger_obj or logger
        self.log_path = Path(log_path) if log_path else None
        self.records: deque[dict[str, Any]] = deque(maxlen=_RECENT_MAX)
        if self.log_path:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                # Serverless filesystems are read-only outside /tmp. Losing the
                # file sink is bad; refusing to start -- and taking the service
                # with it -- is worse, and the stdout sink still carries every
                # entry.
                self.log.error(
                    "audit file sink unavailable at %s (%s); logging to stdout only",
                    self.log_path, exc,
                )
                self.log_path = None

    def record(
        self,
        *,
        user_id: str,
        prompt: str,
        response: str,
        latency_ms: int,
        guardrail_flags: list[str],
        cache_hit: bool,
        model_used: Optional[str],
        tool_calls: Optional[list[str]] = None,
        error: Optional[str] = None,
        actor_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        impersonated: bool = False,
    ) -> dict[str, Any]:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": "query",
            "tenant_id": tenant_id,
            # Who asked, as distinct from whose data was read. Without the
            # actor, a manager reading somebody's finances is indistinguishable
            # from that person asking about themselves -- which is precisely the
            # distinction an audit exists to make. `actor_id` is None for an
            # unauthenticated deployment, where there is no one to name.
            "actor_id": actor_id,
            "impersonated": impersonated,
            # user_id is a pseudonymous handle, not a name -- safe to log, and
            # required to make the audit trail useful. `user_name` never is.
            "user_id": user_id,
            "prompt_hash": _hash(prompt or ""),
            "prompt_chars": len(prompt or ""),
            "response_chars": len(response or ""),
            "response_summary": redact(response or ""),
            "latency_ms": latency_ms,
            "guardrail_flags": list(guardrail_flags or []),
            "cache_hit": cache_hit,
            "model_used": model_used,
            "tool_calls": list(tool_calls or []),
            "error": error,
        }
        return self._emit(entry)

    def event(self, name: str, **fields: Any) -> dict[str, Any]:
        """Record something that is not a query: a sign-in, an upload, a deletion.

        Same sink and same discipline as `record`. Callers pass identifiers and
        outcomes, never prose and never credentials -- there is no redaction
        pass here because nothing that needs redacting belongs in one of these.
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": name,
            **fields,
        }
        return self._emit(entry)

    def _emit(self, entry: dict[str, Any]) -> dict[str, Any]:
        self.records.append(entry)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        self.log.info(line)
        if self.log_path:
            try:
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                # An audit entry that cannot be written must still be emitted,
                # and the failure itself is worth knowing about.
                self.log.error("audit file write failed (%s); entry went to stdout only", exc)
        return entry
