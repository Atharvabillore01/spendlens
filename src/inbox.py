"""Questions users send to their manager, and the answers that come back.

The flow this supports, and why each step exists:

  1. A user asks something the assistant could not settle -- "why is my rent
     categorised as housing when I split it with a flatmate?" -- and sends it on.
  2. The manager opens the inbox and can **run** the question against that
     user's data. This is the point of the feature: the manager holds
     `read:any`, so the same pipeline answers with real figures instead of the
     manager reconstructing them by hand.
  3. The manager replies in their own words, with the computed answer in front
     of them.

`computed_answer` and `reply` are stored separately on purpose. One is what the
system calculated; the other is what a person chose to say. Collapsing them
would make it impossible, later, to tell which of the two was wrong.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import desc, select, update

from .db.schema import manager_requests

log = logging.getLogger("transaction_rag.inbox")

STATUS_OPEN = "open"
STATUS_ANSWERED = "answered"
STATUS_CLOSED = "closed"

MAX_QUESTION_CHARS = 2000
MAX_REPLY_CHARS = 4000


class InboxError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400, code: str = "inbox_error"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass
class ManagerRequest:
    tenant_id: str
    request_id: str
    from_user_id: str
    from_user_name: str
    question: str
    status: str
    created_at: Optional[datetime]
    computed_answer: Optional[str]
    computed_summary: Optional[str]
    computed_at: Optional[datetime]
    reply: Optional[str]
    replied_by: Optional[str]
    replied_at: Optional[datetime]

    def as_dict(self, include_summary: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "request_id": self.request_id,
            "from_user_id": self.from_user_id,
            "from_user_name": self.from_user_name,
            "question": self.question,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "computed_answer": self.computed_answer,
            "computed_at": _iso(self.computed_at),
            "reply": self.reply,
            "replied_by": self.replied_by,
            "replied_at": _iso(self.replied_at),
        }
        if include_summary and self.computed_summary:
            try:
                payload["computed_summary"] = json.loads(self.computed_summary)
            except (ValueError, TypeError):
                payload["computed_summary"] = None
        return payload


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_request(row) -> ManagerRequest:
    return ManagerRequest(*row)


_COLUMNS = (
    manager_requests.c.tenant_id,
    manager_requests.c.request_id,
    manager_requests.c.from_user_id,
    manager_requests.c.from_user_name,
    manager_requests.c.question,
    manager_requests.c.status,
    manager_requests.c.created_at,
    manager_requests.c.computed_answer,
    manager_requests.c.computed_summary,
    manager_requests.c.computed_at,
    manager_requests.c.reply,
    manager_requests.c.replied_by,
    manager_requests.c.replied_at,
)


def submit(engine, tenant_id: str, user_id: str, user_name: str, question: str) -> ManagerRequest:
    text = (question or "").strip()
    if not text:
        raise InboxError("the question is empty", 400, "question_required")
    if len(text) > MAX_QUESTION_CHARS:
        raise InboxError(
            f"questions are limited to {MAX_QUESTION_CHARS} characters", 400, "question_too_long"
        )

    request_id = uuid.uuid4().hex[:16]
    with engine.begin() as conn:
        conn.execute(
            manager_requests.insert().values(
                tenant_id=tenant_id,
                request_id=request_id,
                from_user_id=user_id,
                from_user_name=user_name or user_id,
                question=text,
                status=STATUS_OPEN,
            )
        )
    log.info("inbox: question submitted tenant=%s from=%s id=%s", tenant_id, user_id, request_id)
    return get(engine, tenant_id, request_id)  # type: ignore[return-value]


def get(engine, tenant_id: str, request_id: str) -> Optional[ManagerRequest]:
    with engine.connect() as conn:
        row = conn.execute(
            select(*_COLUMNS).where(
                manager_requests.c.tenant_id == tenant_id,
                manager_requests.c.request_id == request_id,
            )
        ).first()
    return _row_to_request(row) if row else None


def list_for(
    engine,
    tenant_id: str,
    *,
    requester_id: str,
    can_read_all: bool,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[ManagerRequest]:
    """The inbox, scoped to what the caller may see.

    A manager sees every question in the tenant; everyone else sees only their
    own. This is enforced in the query, not by filtering afterwards -- an
    ordinary user's rows never leave the database.
    """
    query = select(*_COLUMNS).where(manager_requests.c.tenant_id == tenant_id)
    if not can_read_all:
        query = query.where(manager_requests.c.from_user_id == requester_id)
    if status:
        query = query.where(manager_requests.c.status == status)
    query = query.order_by(desc(manager_requests.c.created_at)).limit(max(1, min(limit, 500)))

    with engine.connect() as conn:
        rows = conn.execute(query).all()
    return [_row_to_request(r) for r in rows]


def attach_computed(
    engine, tenant_id: str, request_id: str, answer: str, summary: Optional[dict] = None
) -> ManagerRequest:
    """Record what the pipeline said when the manager ran the question."""
    with engine.begin() as conn:
        conn.execute(
            update(manager_requests)
            .where(
                manager_requests.c.tenant_id == tenant_id,
                manager_requests.c.request_id == request_id,
            )
            .values(
                computed_answer=answer,
                computed_summary=json.dumps(summary or {}, default=str),
                computed_at=_now(),
            )
        )
    return get(engine, tenant_id, request_id)  # type: ignore[return-value]


def reply(engine, tenant_id: str, request_id: str, manager_id: str, text: str) -> ManagerRequest:
    body = (text or "").strip()
    if not body:
        raise InboxError("the reply is empty", 400, "reply_required")
    if len(body) > MAX_REPLY_CHARS:
        raise InboxError(f"replies are limited to {MAX_REPLY_CHARS} characters", 400, "reply_too_long")

    existing = get(engine, tenant_id, request_id)
    if existing is None:
        raise InboxError("no such request", 404, "request_not_found")

    with engine.begin() as conn:
        conn.execute(
            update(manager_requests)
            .where(
                manager_requests.c.tenant_id == tenant_id,
                manager_requests.c.request_id == request_id,
            )
            .values(
                reply=body,
                replied_by=manager_id,
                replied_at=_now(),
                status=STATUS_ANSWERED,
            )
        )
    log.info("inbox: replied tenant=%s id=%s by=%s", tenant_id, request_id, manager_id)
    return get(engine, tenant_id, request_id)  # type: ignore[return-value]


def close(engine, tenant_id: str, request_id: str) -> Optional[ManagerRequest]:
    with engine.begin() as conn:
        conn.execute(
            update(manager_requests)
            .where(
                manager_requests.c.tenant_id == tenant_id,
                manager_requests.c.request_id == request_id,
            )
            .values(status=STATUS_CLOSED)
        )
    return get(engine, tenant_id, request_id)


def counts(engine, tenant_id: str, *, requester_id: str, can_read_all: bool) -> dict[str, int]:
    """Badge counts for the nav."""
    items = list_for(
        engine, tenant_id, requester_id=requester_id, can_read_all=can_read_all, limit=500
    )
    return {
        "open": sum(1 for i in items if i.status == STATUS_OPEN),
        "answered": sum(1 for i in items if i.status == STATUS_ANSWERED),
        "total": len(items),
    }
