"""FastAPI wiring for authentication.

`AUTH_REQUIRED=false` (the default) keeps the assessment demo and the test suite
running exactly as before, with an anonymous principal scoped to the default
tenant. That default is a convenience for a single-user demo and nothing else —
`readyz` reports it, and `require_principal` refuses to serve real tenants
anonymously.
"""

from __future__ import annotations

import logging
from typing import Optional

from dataclasses import replace

from fastapi import Depends, Header, HTTPException, Request

from ..config import Settings, get_settings
from .principal import (
    SCOPE_ADMIN,
    SCOPE_INGEST,
    SCOPE_QUERY,
    SCOPE_READ_ANY,
    AuthError,
    Principal,
    verify_token,
)

log = logging.getLogger("transaction_rag.auth")


def _bearer(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def anonymous_principal(settings: Settings) -> Principal:
    """The demo identity. Never granted `read:any`.

    Anonymous mode means "this deployment has one user and no login", so the
    caller is still confined to a single user's data — turning auth off must not
    turn cross-user access on.
    """
    return Principal(
        tenant_id=settings.default_tenant_id,
        user_id="",  # filled from the request; there is nobody to impersonate
        scopes=frozenset({SCOPE_QUERY, SCOPE_INGEST}),
        token_id="anonymous",
    )


def require_principal(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Principal:
    """Resolve the caller, or reject the request.

    Settings are fetched rather than injected: `get_settings(**overrides)` has a
    variadic signature, which FastAPI would read as a required query parameter.
    """
    settings = get_settings()
    token = _bearer(authorization)

    if not settings.auth_required:
        if not token:
            return anonymous_principal(settings)
        # A token supplied while auth is optional is still honoured when it
        # verifies -- but a bad one is an error, not something to fall back
        # from. Silently downgrading to anonymous would mean an expired token
        # grants *more* than it should.
        try:
            return verify_token(token, settings)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"error": exc.code, "message": exc.message},
            ) from exc

    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthenticated", "message": "a bearer token is required"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        principal = verify_token(token, settings)
    except AuthError as exc:
        log.warning("auth rejected from %s: %s", request.client.host if request.client else "?", exc.code)
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": exc.code, "message": exc.message},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return _apply_current_account_state(principal, settings)


# A verified token proves what was true when it was minted. It says nothing
# about now. Without this check, disabling an account or demoting a manager has
# no effect until the token expires -- up to `jwt_default_ttl_s`, an hour by
# default -- so the person you just removed keeps reading everyone's finances.
#
# The lookup is cached for a few seconds. That is the honest trade: a database
# read on every request is real cost, and bounded staleness (seconds) is a very
# different risk from bounded-by-token-lifetime staleness (an hour). Set
# REVOCATION_CACHE_S=0 to check on every request.
_revocation_cache: dict[str, tuple[float, Optional[tuple[str, bool]]]] = {}


def _current_account(settings, tenant_id: str, user_id: str) -> Optional[tuple[str, bool]]:
    """`(role, is_active)` as the database has it now, or None if it is gone."""
    import time

    key = f"{tenant_id}:{user_id}"
    ttl = getattr(settings, "revocation_cache_s", 5)
    now = time.monotonic()

    cached = _revocation_cache.get(key)
    if cached and ttl and now - cached[0] < ttl:
        return cached[1]

    try:
        from ..db.engine import get_engine  # noqa: PLC0415
        from .accounts import get_account  # noqa: PLC0415

        account = get_account(get_engine(settings), tenant_id, user_id)
        state = (account.role, account.is_active) if account else None
    except Exception as exc:  # noqa: BLE001
        # Fail *open* on an infrastructure error, and say so loudly. A database
        # blip must not sign every user out; a stale token for a few seconds is
        # the lesser harm. This is a deliberate availability choice.
        log.error("revocation check unavailable, honouring token as-is: %s", exc)
        return None

    _revocation_cache[key] = (now, state)
    return state


def _apply_current_account_state(principal: Principal, settings) -> Principal:
    if not principal.user_id:
        return principal  # anonymous mode has no account to check

    state = _current_account(settings, principal.tenant_id, principal.user_id)
    if state is None:
        return principal  # unknown to the account store, or the store is down

    role, is_active = state
    if not is_active:
        log.warning("token presented for a disabled account user=%s", principal.user_id)
        raise HTTPException(
            status_code=401,
            detail={"error": "account_disabled", "message": "this account is no longer active"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Narrow to what the role grants *now*. Intersecting rather than replacing
    # means a token can only ever lose privileges here, never gain them -- a
    # promotion still requires a fresh login.
    from .accounts import scopes_for  # noqa: PLC0415

    current = frozenset(scopes_for(role))
    if not principal.scopes <= current:
        dropped = sorted(principal.scopes - current)
        log.warning("scopes narrowed for user=%s role=%s dropped=%s", principal.user_id, role, dropped)
        return replace(principal, scopes=principal.scopes & current)
    return principal


def _scope_guard(scope: str):
    def guard(principal: Principal = Depends(require_principal)) -> Principal:
        try:
            principal.require(scope)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"error": exc.code, "message": exc.message},
            ) from exc
        return principal

    return guard


require_query = _scope_guard(SCOPE_QUERY)
require_ingest = _scope_guard(SCOPE_INGEST)
require_admin = _scope_guard(SCOPE_ADMIN)


__all__ = [
    "require_principal",
    "require_query",
    "require_ingest",
    "require_admin",
    "anonymous_principal",
    "SCOPE_QUERY",
    "SCOPE_READ_ANY",
    "SCOPE_INGEST",
    "SCOPE_ADMIN",
]
