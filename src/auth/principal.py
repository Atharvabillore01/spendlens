"""Verified identity — who is asking, and what they may ask for.

The rule this module exists to enforce: **`user_id` is derived from a signed
token, never read from the request body.** Before this, `POST /query` trusted
whatever `user_id` the caller sent, which meant anyone could read anyone's
financial history by changing one string. No guardrail downstream could help,
because the cross-user check inspects the *prompt text* — it has nothing to say
about a forged identity field.

Scopes are deliberately few. Every extra one is a chance to grant more than
intended:

  query        read your own data. What an end-user token carries.
  read:any     read any user in your tenant. Support tooling only, and the one
               scope that lets `user_id` come from the request — audited as
               impersonation whenever it is used.
  ingest:write upload transaction files for your tenant.
  admin        tenant administration.

`tenant_id` is mandatory on every token. A token without one cannot be scoped to
a tenant's data, and defaulting it would mean a malformed token silently reading
the default tenant.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import jwt

SCOPE_QUERY = "query"
SCOPE_READ_ANY = "read:any"
SCOPE_INGEST = "ingest:write"
SCOPE_ADMIN = "admin"

ALL_SCOPES = frozenset({SCOPE_QUERY, SCOPE_READ_ANY, SCOPE_INGEST, SCOPE_ADMIN})


class AuthError(Exception):
    """Authentication or authorisation failure. Carries an HTTP status."""

    def __init__(self, message: str, status_code: int = 401, code: str = "unauthenticated"):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    tenant_id: str
    user_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    token_id: str = ""
    claims: dict[str, Any] = field(default_factory=dict)

    def has(self, scope: str) -> bool:
        return scope in self.scopes or SCOPE_ADMIN in self.scopes

    def require(self, scope: str) -> None:
        if not self.has(scope):
            raise AuthError(
                f"this token lacks the '{scope}' scope",
                status_code=403,
                code="insufficient_scope",
            )

    def resolve_target_user(self, requested: Optional[str]) -> tuple[str, bool]:
        """Which user's data this request may read.

        Returns `(user_id, impersonated)`. A caller may always read themselves.
        Reading somebody else requires `read:any` and is reported back so the
        audit log can record it — an unlogged impersonation is indistinguishable
        from a breach after the fact.
        """
        if not requested or requested == self.user_id:
            return self.user_id, False
        if not self.has(SCOPE_READ_ANY):
            raise AuthError(
                "you may only query your own data",
                status_code=403,
                code="cross_user_denied",
            )
        return requested, True


# -- token verification -------------------------------------------------------


def _decode_options(settings) -> dict[str, Any]:
    return {
        "require": ["exp", "sub"],
        "verify_exp": True,
        "verify_signature": True,
        "verify_aud": bool(settings.jwt_audience),
    }


def verify_token(token: str, settings) -> Principal:
    """Verify a bearer token and return the principal it asserts."""
    key = settings.jwt_verification_key
    if not key:
        # Refusing here is the point: an empty key with `verify_signature`
        # would otherwise be a deployment that accepts unsigned tokens.
        raise AuthError(
            "server has no JWT verification key configured",
            status_code=500,
            code="auth_misconfigured",
        )

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=[settings.jwt_algorithm],
            options=_decode_options(settings),
            leeway=settings.jwt_leeway_s,
            audience=settings.jwt_audience or None,
            issuer=settings.jwt_issuer or None,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token has expired", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        # Deliberately unspecific to the caller: which check failed is useful to
        # an attacker and useless to a legitimate client.
        raise AuthError("token is not valid", code="invalid_token") from exc

    tenant_id = str(claims.get("tenant") or claims.get("tenant_id") or "").strip()
    if not tenant_id:
        raise AuthError("token carries no tenant", code="invalid_token")

    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        raise AuthError("token carries no subject", code="invalid_token")

    raw_scopes = claims.get("scope") or claims.get("scopes") or SCOPE_QUERY
    if isinstance(raw_scopes, str):
        scopes = {s for s in raw_scopes.replace(",", " ").split() if s}
    else:
        scopes = {str(s) for s in raw_scopes}

    return Principal(
        tenant_id=tenant_id,
        user_id=user_id,
        scopes=frozenset(scopes & ALL_SCOPES),
        token_id=str(claims.get("jti", "")),
        claims=claims,
    )


def mint_token(
    settings,
    tenant_id: str,
    user_id: str,
    scopes: Optional[list[str]] = None,
    ttl_s: Optional[int] = None,
) -> str:
    """Issue a token. For local runs, tests and support tooling.

    A real deployment issues these from its identity provider; this exists so
    the service is operable without standing one up first.
    """
    if settings.jwt_algorithm.startswith(("RS", "ES")):
        raise AuthError(
            "minting requires a symmetric algorithm; issue asymmetric tokens from your IdP",
            status_code=500,
            code="auth_misconfigured",
        )
    if not settings.jwt_secret:
        raise AuthError("JWT_SECRET is not set", status_code=500, code="auth_misconfigured")

    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": user_id,
        "tenant": tenant_id,
        "scope": " ".join(scopes or [SCOPE_QUERY]),
        "iat": now,
        "nbf": now,
        "exp": now + int(ttl_s or settings.jwt_default_ttl_s),
        "jti": uuid.uuid4().hex[:16],
    }
    if settings.jwt_issuer:
        claims["iss"] = settings.jwt_issuer
    if settings.jwt_audience:
        claims["aud"] = settings.jwt_audience

    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


__all__ = [
    "Principal",
    "AuthError",
    "verify_token",
    "mint_token",
    "SCOPE_QUERY",
    "SCOPE_READ_ANY",
    "SCOPE_INGEST",
    "SCOPE_ADMIN",
    "ALL_SCOPES",
]
