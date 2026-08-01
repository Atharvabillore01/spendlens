"""Identity and scope.

The property under test throughout: **a caller cannot read data belonging to a
user they are not.** Before identity moved into the token, `user_id` was a body
field and this was untestable, because there was nothing to violate.
"""

from __future__ import annotations

import time

import jwt
import pytest

from src.auth.principal import (
    SCOPE_ADMIN,
    SCOPE_INGEST,
    SCOPE_QUERY,
    SCOPE_READ_ANY,
    AuthError,
    Principal,
    mint_token,
    verify_token,
)


@pytest.fixture
def auth_settings(settings):
    return settings.model_copy(
        update={
            "auth_required": True,
            # >= 32 bytes: PyJWT warns below that for HS256, and a deployment
            # secret shorter than the digest is worth catching in tests too.
            "jwt_secret": "test-secret-not-a-real-one-0123456789abcdef",
            "jwt_algorithm": "HS256",
        }
    )


# == token verification =======================================================


def test_round_trip_carries_tenant_subject_and_scopes(auth_settings):
    token = mint_token(auth_settings, "acme", "usr_1", scopes=[SCOPE_QUERY, SCOPE_INGEST])
    principal = verify_token(token, auth_settings)

    assert principal.tenant_id == "acme"
    assert principal.user_id == "usr_1"
    assert principal.has(SCOPE_QUERY)
    assert principal.has(SCOPE_INGEST)
    assert not principal.has(SCOPE_READ_ANY)


def test_expired_token_is_rejected(auth_settings):
    # Comfortably past `jwt_leeway_s`, which exists to tolerate clock skew and
    # would otherwise let a just-expired token through.
    token = mint_token(auth_settings, "acme", "usr_1", ttl_s=-(auth_settings.jwt_leeway_s + 60))
    with pytest.raises(AuthError) as caught:
        verify_token(token, auth_settings)
    assert caught.value.code == "token_expired"


def test_token_signed_with_another_key_is_rejected(auth_settings):
    forged = jwt.encode(
        {"sub": "usr_1", "tenant": "acme", "exp": int(time.time()) + 600},
        "a-different-secret",
        algorithm="HS256",
    )
    with pytest.raises(AuthError) as caught:
        verify_token(forged, auth_settings)
    assert caught.value.code == "invalid_token"


def test_unsigned_token_is_rejected(auth_settings):
    """`alg: none` is the classic JWT bypass; PyJWT must not accept it here."""
    unsigned = jwt.encode(
        {"sub": "usr_1", "tenant": "acme", "exp": int(time.time()) + 600},
        key="",
        algorithm="none",
    )
    with pytest.raises(AuthError):
        verify_token(unsigned, auth_settings)


def test_token_without_a_tenant_is_rejected(auth_settings):
    """Defaulting the tenant would let a malformed token read the default one."""
    token = jwt.encode(
        {"sub": "usr_1", "exp": int(time.time()) + 600},
        auth_settings.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(AuthError) as caught:
        verify_token(token, auth_settings)
    assert caught.value.code == "invalid_token"


def test_unknown_scopes_are_discarded(auth_settings):
    """A token asking for a scope we don't define gets nothing, not everything."""
    token = jwt.encode(
        {
            "sub": "usr_1",
            "tenant": "acme",
            "scope": "query superuser:*",
            "exp": int(time.time()) + 600,
        },
        auth_settings.jwt_secret,
        algorithm="HS256",
    )
    principal = verify_token(token, auth_settings)
    assert principal.scopes == frozenset({SCOPE_QUERY})


def test_verification_refuses_when_no_key_is_configured(settings):
    """An empty key must fail closed, not verify everything."""
    token = jwt.encode({"sub": "u", "tenant": "t", "exp": int(time.time()) + 60}, "k", algorithm="HS256")
    with pytest.raises(AuthError) as caught:
        verify_token(token, settings.model_copy(update={"jwt_secret": ""}))
    assert caught.value.code == "auth_misconfigured"


# == scope enforcement ========================================================


def test_a_caller_always_reads_themselves():
    principal = Principal(tenant_id="acme", user_id="usr_1", scopes=frozenset({SCOPE_QUERY}))
    assert principal.resolve_target_user(None) == ("usr_1", False)
    assert principal.resolve_target_user("usr_1") == ("usr_1", False)


def test_reading_another_user_without_the_scope_is_denied():
    principal = Principal(tenant_id="acme", user_id="usr_1", scopes=frozenset({SCOPE_QUERY}))
    with pytest.raises(AuthError) as caught:
        principal.resolve_target_user("usr_2")
    assert caught.value.status_code == 403
    assert caught.value.code == "cross_user_denied"


def test_read_any_permits_impersonation_and_reports_it():
    """The flag is the point: an unlogged impersonation is indistinguishable
    from a breach after the fact."""
    principal = Principal(
        tenant_id="acme", user_id="support_1", scopes=frozenset({SCOPE_QUERY, SCOPE_READ_ANY})
    )
    assert principal.resolve_target_user("usr_2") == ("usr_2", True)


def test_admin_implies_every_scope():
    principal = Principal(tenant_id="acme", user_id="root", scopes=frozenset({SCOPE_ADMIN}))
    assert principal.has(SCOPE_INGEST)
    assert principal.resolve_target_user("anyone") == ("anyone", True)


def test_require_raises_for_a_missing_scope():
    principal = Principal(tenant_id="acme", user_id="usr_1", scopes=frozenset({SCOPE_QUERY}))
    with pytest.raises(AuthError) as caught:
        principal.require(SCOPE_INGEST)
    assert caught.value.status_code == 403
