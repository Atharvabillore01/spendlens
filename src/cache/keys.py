"""Centralized cache-key construction.

Key names live in exactly one place so a typo can't silently create a second,
never-read cache entry. The three per-user patterns are fixed by the brief (§2)
and are emitted verbatim for a single-tenant deployment.

**Tenant scoping.** `user_id` is only unique *within* a tenant. Two clients each
with a `usr_001` would share a cache entry under the bare pattern — one tenant's
profile served to another, which is a data leak the storage layer's tenant
filter cannot catch because the read never reaches storage. So every key gains a
`tenant:{id}:` prefix as soon as a tenant is named. The unprefixed form is kept
for the default tenant so the brief's key names, the demo and the tests are
unchanged.
"""

from __future__ import annotations

from typing import Optional

PROFILE = "user:{user_id}:profile"
QUERY_HISTORY = "user:{user_id}:query_history"
VIZ_STATE = "user:{user_id}:viz_state"
CHART_GRANT = "chart:{filename}:grant"

# The single-tenant deployment: its keys stay exactly as the brief specifies.
DEFAULT_TENANT = "default"


def _scoped(key: str, tenant_id: Optional[str]) -> str:
    if not tenant_id or tenant_id == DEFAULT_TENANT:
        return key
    return f"tenant:{tenant_id}:{key}"


def profile(user_id: str, tenant_id: Optional[str] = None) -> str:
    return _scoped(PROFILE.format(user_id=user_id), tenant_id)


def query_history(user_id: str, tenant_id: Optional[str] = None) -> str:
    return _scoped(QUERY_HISTORY.format(user_id=user_id), tenant_id)


def viz_state(user_id: str, tenant_id: Optional[str] = None) -> str:
    return _scoped(VIZ_STATE.format(user_id=user_id), tenant_id)


def chart_grant(filename: str) -> str:
    """Who a rendered chart belongs to.

    Not tenant-prefixed: the filename is already globally unique (it carries a
    random token), and the grant's *value* is what names the tenant.
    """
    return CHART_GRANT.format(filename=filename)


def all_for_user(user_id: str, tenant_id: Optional[str] = None) -> tuple[str, ...]:
    return (
        profile(user_id, tenant_id),
        query_history(user_id, tenant_id),
        viz_state(user_id, tenant_id),
    )
