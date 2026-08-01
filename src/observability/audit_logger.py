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


class AuditLogger:
    def __init__(self, log_path: Optional[Path] = None, logger_obj: Optional[logging.Logger] = None):
        self.log = logger_obj or logger
        self.log_path = Path(log_path) if log_path else None
        self.records: list[dict[str, Any]] = []
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

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
    ) -> dict[str, Any]:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
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
        self.records.append(entry)
        line = json.dumps(entry, ensure_ascii=False)
        self.log.info(line)
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return entry
