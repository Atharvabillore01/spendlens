"""The audit trail's job is to answer "who read whose data, and when".

Everything here is about that question. The redaction rules have their own
coverage elsewhere; these are the properties that make an entry *accountable*.
"""

from __future__ import annotations

import json

import pytest

from src.observability.audit_logger import _RECENT_MAX, AuditLogger


def test_actor_is_recorded_separately_from_subject():
    """A manager reading an account must not look like that account asking.

    This is the distinction the whole scope model exists to enforce, and an
    audit that cannot express it is decorative.
    """
    audit = AuditLogger()
    entry = audit.record(
        user_id="usr_subject",
        prompt="what did I spend?",
        response="You spent $10.00.",
        latency_ms=1,
        guardrail_flags=[],
        cache_hit=False,
        model_used="m",
        actor_id="usr_manager",
        tenant_id="acme",
        impersonated=True,
    )
    assert entry["user_id"] == "usr_subject"
    assert entry["actor_id"] == "usr_manager"
    assert entry["impersonated"] is True
    assert entry["tenant_id"] == "acme"


def test_a_self_read_is_marked_as_such():
    audit = AuditLogger()
    entry = audit.record(
        user_id="usr_a",
        prompt="p",
        response="r",
        latency_ms=1,
        guardrail_flags=[],
        cache_hit=False,
        model_used=None,
        actor_id="usr_a",
    )
    assert entry["impersonated"] is False


def test_events_carry_a_type_and_reach_the_same_sink(tmp_path):
    path = tmp_path / "audit.log"
    audit = AuditLogger(path)
    audit.event("login", tenant_id="default", actor_id="usr_a", role="user")
    audit.event("ingest_reverted", tenant_id="default", actor_id="usr_admin", rows_removed=12)

    written = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e["event"] for e in written] == ["login", "ingest_reverted"]
    assert written[1]["rows_removed"] == 12
    # Every entry is timestamped, or it cannot be placed in a sequence.
    assert all(e["timestamp"] for e in written)


def test_queries_are_typed_too_so_one_stream_can_be_filtered():
    audit = AuditLogger()
    entry = audit.record(
        user_id="u", prompt="p", response="r", latency_ms=1,
        guardrail_flags=[], cache_hit=False, model_used=None,
    )
    assert entry["event"] == "query"


def test_the_in_memory_tail_is_bounded():
    """A long-lived process must not accumulate every entry it ever wrote."""
    audit = AuditLogger()
    for i in range(_RECENT_MAX + 50):
        audit.event("noise", n=i)
    assert len(audit.records) == _RECENT_MAX
    assert audit.records[-1]["n"] == _RECENT_MAX + 49


def test_an_unwritable_path_degrades_to_stdout_instead_of_failing(tmp_path):
    """Serverless filesystems are read-only outside /tmp.

    Losing the file sink is bad; taking the service down at import time because
    of it is worse, and the stdout sink still carries every entry.
    """
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    audit = AuditLogger(blocked / "nested" / "audit.log")
    assert audit.log_path is None
    entry = audit.event("login", actor_id="usr_a")  # must not raise
    assert entry["event"] == "login"


@pytest.mark.parametrize("secret", ["$3,099.00", "12345678"])
def test_amounts_never_reach_the_log(secret):
    audit = AuditLogger()
    entry = audit.record(
        user_id="u", prompt="p", response=f"You spent {secret} on rent.",
        latency_ms=1, guardrail_flags=[], cache_hit=False, model_used=None,
    )
    assert secret not in entry["response_summary"]
