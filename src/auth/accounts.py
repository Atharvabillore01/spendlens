"""Accounts: password verification, roles, and the scopes a role grants.

Two things are deliberate.

**Roles are stored; scopes are derived.** The database records that somebody is
a `manager`. What a manager may do is decided here, at token-mint time. Widening
a role is then a code change plus a re-login, not a migration that rewrites
permission rows -- and a token issued yesterday cannot carry a permission the
role no longer has for longer than its TTL.

**Hashing is `scrypt` from the standard library.** It is memory-hard, it is in
Python's `hashlib`, and it needs no dependency. The parameters are stored inside
the hash string, so raising them later leaves existing hashes verifiable and
they upgrade on next login.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select, update

from ..db.schema import credentials
from .principal import (
    SCOPE_ADMIN,
    SCOPE_INGEST,
    SCOPE_QUERY,
    SCOPE_READ_ANY,
    AuthError,
    mint_token,
)

log = logging.getLogger("transaction_rag.auth")

ROLE_USER = "user"
ROLE_MANAGER = "manager"
ROLE_ADMIN = "admin"
ROLES = (ROLE_USER, ROLE_MANAGER, ROLE_ADMIN)

# What each role may do. `read:any` is the line that matters: it is what lets a
# manager read somebody else's data, and it is exactly what an ordinary user
# must never hold.
ROLE_SCOPES: dict[str, tuple[str, ...]] = {
    ROLE_USER: (SCOPE_QUERY,),
    ROLE_MANAGER: (SCOPE_QUERY, SCOPE_READ_ANY, SCOPE_INGEST),
    ROLE_ADMIN: (SCOPE_QUERY, SCOPE_READ_ANY, SCOPE_INGEST, SCOPE_ADMIN),
}

# scrypt cost. n=2**14 keeps a login around 50-100ms on a laptop, which is slow
# enough to make offline cracking expensive and fast enough to stay interactive.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32
MIN_PASSWORD_CHARS = 8


def scopes_for(role: str) -> list[str]:
    return list(ROLE_SCOPES.get(str(role).lower(), ROLE_SCOPES[ROLE_USER]))


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash`, all hex. Parameters travel with the hash."""
    if len(password or "") < MIN_PASSWORD_CHARS:
        raise AuthError(
            f"password must be at least {MIN_PASSWORD_CHARS} characters",
            status_code=400,
            code="password_too_short",
        )
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_LEN
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time comparison. Returns False on any malformed hash rather
    than raising -- a corrupt row must not become an authentication bypass or a
    500."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = str(encoded).split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            (password or "").encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
        )
    except (ValueError, TypeError, MemoryError):
        return False
    return hmac.compare_digest(digest, bytes.fromhex(hash_hex))


@dataclass(frozen=True)
class Account:
    tenant_id: str
    user_id: str
    email: str
    role: str
    is_active: bool

    @property
    def scopes(self) -> list[str]:
        return scopes_for(self.role)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "email": self.email,
            "role": self.role,
            "scopes": self.scopes,
            "is_active": self.is_active,
        }


def create_account(
    engine,
    tenant_id: str,
    user_id: str,
    email: str,
    password: str,
    role: str = ROLE_USER,
) -> Account:
    """Grant a login. Raises if the role is unknown or the email is taken."""
    role = str(role).lower()
    if role not in ROLES:
        raise AuthError(f"unknown role {role!r}", status_code=400, code="unknown_role")

    email = str(email).strip().lower()
    if not email:
        raise AuthError("email is required", status_code=400, code="email_required")

    encoded = hash_password(password)
    with engine.begin() as conn:
        # Both uniqueness rules are checked here so a collision surfaces as a
        # 409 with a usable message. Letting the constraint fire instead raises
        # a driver IntegrityError, which reaches the caller as a 500.
        taken_email = conn.execute(
            select(credentials.c.user_id).where(
                credentials.c.tenant_id == tenant_id, credentials.c.email == email
            )
        ).first()
        if taken_email:
            raise AuthError("that email already has a login", status_code=409, code="email_taken")

        taken_user = conn.execute(
            select(credentials.c.email).where(
                credentials.c.tenant_id == tenant_id, credentials.c.user_id == user_id
            )
        ).first()
        if taken_user:
            raise AuthError(
                f"{user_id} already has a login ({taken_user[0]})",
                status_code=409,
                code="user_has_login",
            )
        conn.execute(
            credentials.insert().values(
                tenant_id=tenant_id,
                user_id=user_id,
                email=email,
                password_hash=encoded,
                role=role,
                is_active=True,
            )
        )
    log.info("account created tenant=%s user=%s role=%s", tenant_id, user_id, role)
    return Account(tenant_id, user_id, email, role, True)


def set_password(engine, tenant_id: str, user_id: str, password: str) -> None:
    encoded = hash_password(password)
    with engine.begin() as conn:
        conn.execute(
            update(credentials)
            .where(credentials.c.tenant_id == tenant_id, credentials.c.user_id == user_id)
            .values(password_hash=encoded)
        )


def delete_account(engine, tenant_id: str, user_id: str) -> bool:
    """Revoke a login. The person's transactions are untouched -- credentials
    and data are separate tables precisely so this is not destructive."""
    with engine.begin() as conn:
        result = conn.execute(
            credentials.delete().where(
                credentials.c.tenant_id == tenant_id, credentials.c.user_id == user_id
            )
        )
    return bool(result.rowcount)


def get_account(engine, tenant_id: str, user_id: str) -> Optional[Account]:
    with engine.connect() as conn:
        row = conn.execute(
            select(
                credentials.c.tenant_id,
                credentials.c.user_id,
                credentials.c.email,
                credentials.c.role,
                credentials.c.is_active,
            ).where(credentials.c.tenant_id == tenant_id, credentials.c.user_id == user_id)
        ).first()
    return Account(*row) if row else None


def list_accounts(engine, tenant_id: str) -> list[Account]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                credentials.c.tenant_id,
                credentials.c.user_id,
                credentials.c.email,
                credentials.c.role,
                credentials.c.is_active,
            )
            .where(credentials.c.tenant_id == tenant_id)
            .order_by(credentials.c.email)
        ).all()
    return [Account(*row) for row in rows]


def authenticate(engine, tenant_id: str, email: str, password: str) -> Account:
    """Verify a login.

    Every failure returns the same message. Distinguishing "no such account"
    from "wrong password" turns the login form into an account-enumeration
    oracle, and the dummy hash keeps the timing of the two paths comparable.
    """
    email = str(email).strip().lower()
    with engine.connect() as conn:
        row = conn.execute(
            select(
                credentials.c.tenant_id,
                credentials.c.user_id,
                credentials.c.email,
                credentials.c.role,
                credentials.c.is_active,
                credentials.c.password_hash,
            ).where(credentials.c.tenant_id == tenant_id, credentials.c.email == email)
        ).first()

    invalid = AuthError("email or password is incorrect", status_code=401, code="invalid_credentials")

    if row is None:
        # Spend comparable time so a missing account is not detectable by clock.
        verify_password(password, _DUMMY_HASH)
        raise invalid

    account = Account(row[0], row[1], row[2], row[3], bool(row[4]))
    if not verify_password(password, row[5]):
        raise invalid
    if not account.is_active:
        raise AuthError("this account is disabled", status_code=403, code="account_disabled")

    with engine.begin() as conn:
        conn.execute(
            update(credentials)
            .where(
                credentials.c.tenant_id == account.tenant_id,
                credentials.c.user_id == account.user_id,
            )
            .values(last_login_at=_now())
        )
    return account


def issue_for(settings, account: Account) -> str:
    """Mint a token carrying exactly the scopes this account's role grants."""
    return mint_token(
        settings,
        tenant_id=account.tenant_id,
        user_id=account.user_id,
        scopes=account.scopes,
    )


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# Computed once at import so the "no such account" path costs roughly what a
# real verification costs.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(24))
