"""Login, roles, paste-ingest and the manager inbox.

These run against a real (temporary) database and a real FastAPI app with auth
switched **on**, because the thing worth asserting is not that the functions
work in isolation — it is that a `user` token cannot reach a `manager` surface
through the HTTP layer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from src.auth.accounts import (
    ROLE_ADMIN,
    ROLE_MANAGER,
    ROLE_USER,
    authenticate,
    create_account,
    hash_password,
    scopes_for,
    verify_password,
)
from src.auth.principal import AuthError
from src.db.schema import create_all


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    create_all(engine)
    return engine


# == passwords ================================================================


def test_hash_is_opaque_and_salted():
    a, b = hash_password("correct horse battery"), hash_password("correct horse battery")
    assert a != b, "identical passwords must not produce identical hashes"
    assert "correct horse" not in a
    assert verify_password("correct horse battery", a)
    assert not verify_password("wrong", a)


def test_short_passwords_are_refused():
    with pytest.raises(AuthError, match="at least"):
        hash_password("short")


@pytest.mark.parametrize("corrupt", ["", "garbage", "scrypt$notanumber$8$1$aa$bb", "md5$x$y"])
def test_a_corrupt_hash_is_false_not_an_exception(corrupt):
    """A damaged row must fail closed, not 500 and not authenticate."""
    assert verify_password("anything", corrupt) is False


# == roles ====================================================================


def test_only_manager_and_admin_can_read_across_users():
    assert "read:any" not in scopes_for(ROLE_USER)
    assert "read:any" in scopes_for(ROLE_MANAGER)
    assert "read:any" in scopes_for(ROLE_ADMIN)
    assert "admin" in scopes_for(ROLE_ADMIN)
    assert "admin" not in scopes_for(ROLE_MANAGER)


def test_unknown_role_falls_back_to_the_least_privilege():
    assert scopes_for("wizard") == scopes_for(ROLE_USER)


# == authentication ===========================================================


def test_login_is_case_and_space_tolerant(engine):
    create_account(engine, "t", "usr_1", "Jose@Acme.com", "hunter2password", ROLE_USER)
    assert authenticate(engine, "t", "  jose@acme.com ", "hunter2password").user_id == "usr_1"


def test_wrong_password_and_unknown_account_are_indistinguishable(engine):
    """Anything else turns the login form into an account-enumeration oracle."""
    create_account(engine, "t", "usr_1", "jose@acme.com", "hunter2password", ROLE_USER)

    with pytest.raises(AuthError) as wrong:
        authenticate(engine, "t", "jose@acme.com", "not-the-password")
    with pytest.raises(AuthError) as missing:
        authenticate(engine, "t", "nobody@acme.com", "hunter2password")

    assert wrong.value.code == missing.value.code == "invalid_credentials"
    assert wrong.value.message == missing.value.message


def test_duplicate_email_and_duplicate_user_are_both_clean_conflicts(engine):
    """Letting the constraint fire instead surfaces as a driver error — a 500."""
    create_account(engine, "t", "usr_1", "jose@acme.com", "hunter2password", ROLE_USER)

    with pytest.raises(AuthError) as email:
        create_account(engine, "t", "usr_2", "jose@acme.com", "hunter2password", ROLE_USER)
    assert email.value.code == "email_taken"

    with pytest.raises(AuthError) as user:
        create_account(engine, "t", "usr_1", "other@acme.com", "hunter2password", ROLE_USER)
    assert user.value.code == "user_has_login"


def test_tenants_are_isolated(engine):
    """The same email in two tenants is two different people."""
    create_account(engine, "a", "usr_1", "jose@acme.com", "hunter2password", ROLE_USER)
    create_account(engine, "b", "usr_9", "jose@acme.com", "otherpassword1", ROLE_MANAGER)
    assert authenticate(engine, "a", "jose@acme.com", "hunter2password").user_id == "usr_1"
    assert authenticate(engine, "b", "jose@acme.com", "otherpassword1").role == ROLE_MANAGER


# == HTTP: the scope boundary =================================================


@pytest.fixture
def authed(tmp_path, monkeypatch):
    """A live app with auth on, and one account per role."""
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("JWT_SECRET", "test-secret-long-enough-for-hs256-ok")
    monkeypatch.setenv("SHOW_LOGIN_HINTS", "false")
    monkeypatch.setenv("OFFLINE_LLM", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")

    import src.config as config
    from src.auth.dependencies import _revocation_cache

    config._settings = None  # drop the cached singleton so the env above applies
    # The revocation cache is process-global and keyed by tenant:user, which is
    # correct in production and leaks between tests that rebuild the app on a
    # fresh database. Start each one clean.
    _revocation_cache.clear()
    import importlib

    import api as api_module

    api = importlib.reload(api_module)

    for uid, email, role in (
        ("usr_a1b2c3d4", "user@t.com", ROLE_USER),
        ("usr_mgr", "manager@t.com", ROLE_MANAGER),
        ("usr_admin", "admin@t.com", ROLE_ADMIN),
    ):
        create_account(api.auth_engine, "default", uid, email, "passwordpassword", role)

    client = TestClient(api.app)

    def token(email: str) -> dict[str, str]:
        body = client.post("/auth/login", json={"email": email, "password": "passwordpassword"})
        return {"Authorization": f"Bearer {body.json()['access_token']}"}

    yield client, token
    _revocation_cache.clear()
    config._settings = None


def test_no_token_is_rejected_everywhere(authed):
    client, _ = authed
    for method, path in (("get", "/users"), ("get", "/auth/me"), ("get", "/requests")):
        assert getattr(client, method)(path).status_code == 401, path
    assert client.post("/query", json={"prompt": "hi"}).status_code == 401


def test_role_shapes_the_identity(authed):
    client, token = authed
    user = client.get("/auth/me", headers=token("user@t.com")).json()
    manager = client.get("/auth/me", headers=token("manager@t.com")).json()
    assert user["role"] == "user" and user["can_read_all"] is False
    assert manager["role"] == "manager" and manager["can_read_all"] is True


def test_a_user_cannot_read_another_user(authed):
    client, token = authed
    response = client.post(
        "/query",
        json={"prompt": "What did I spend?", "user_id": "usr_e5f6g7h8"},
        headers=token("user@t.com"),
    )
    assert response.status_code == 403


def test_a_manager_can_read_another_user(authed):
    client, token = authed
    response = client.post(
        "/query",
        json={"prompt": "What did I spend the most on last month?", "user_id": "usr_a1b2c3d4"},
        headers=token("manager@t.com"),
    )
    assert response.status_code == 200
    assert response.json()["user_name"] == "Jose BazBaz"


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/ingest/paste", {"text": "a\tb", "commit": False}),
        ("get", "/auth/accounts", None),
    ],
)
def test_privileged_endpoints_refuse_an_ordinary_user(authed, method, path, body):
    client, token = authed
    call = getattr(client, method)
    response = call(path, json=body, headers=token("user@t.com")) if body else call(
        path, headers=token("user@t.com")
    )
    assert response.status_code == 403


def test_login_hints_are_absent_unless_enabled(authed):
    """Off by default, and a 404 rather than an empty list — the endpoint's
    existence should not itself disclose that accounts are listable."""
    client, _ = authed
    assert client.get("/auth/hints").status_code == 404


# == the manager inbox ========================================================


def test_a_question_reaches_the_manager_and_the_reply_comes_back(authed):
    client, token = authed
    user, manager = token("user@t.com"), token("manager@t.com")

    created = client.post("/requests", json={"question": "Why is rent split?"}, headers=user)
    assert created.status_code == 201
    request_id = created.json()["request_id"]

    # The manager sees it; the user sees only their own.
    inbox = client.get("/requests", headers=manager).json()
    assert inbox["can_reply"] is True
    assert any(r["request_id"] == request_id for r in inbox["requests"])
    assert inbox["counts"]["open"] >= 1

    mine = client.get("/requests", headers=user).json()
    assert mine["can_reply"] is False

    # Running it answers from the *asker's* data, not the manager's.
    ran = client.post(f"/requests/{request_id}/run", headers=manager)
    assert ran.status_code == 200
    assert ran.json()["result"]["user_id"] == "usr_a1b2c3d4"

    client.post(f"/requests/{request_id}/reply", json={"reply": "It isn't."}, headers=manager)
    answered = client.get("/requests", headers=user).json()["requests"][0]
    assert answered["status"] == "answered"
    assert answered["reply"] == "It isn't."
    # The computed answer is kept distinct from what the person wrote.
    assert answered["computed_answer"] and answered["computed_answer"] != answered["reply"]


def test_a_user_can_neither_run_nor_reply(authed):
    client, token = authed
    user, manager = token("user@t.com"), token("manager@t.com")
    rid = client.post("/requests", json={"question": "Anything?"}, headers=user).json()["request_id"]

    assert client.post(f"/requests/{rid}/run", headers=user).status_code == 403
    assert client.post(f"/requests/{rid}/reply", json={"reply": "hi"}, headers=user).status_code == 403
    # And the manager still can, so the refusal is about scope, not the route.
    assert client.post(f"/requests/{rid}/reply", json={"reply": "hi"}, headers=manager).status_code == 200


def test_empty_question_is_refused(authed):
    client, token = authed
    response = client.post("/requests", json={"question": "   "}, headers=token("user@t.com"))
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "question_required"


# == rate limiting over HTTP ==================================================


def test_the_expensive_endpoint_returns_429_with_retry_after(authed, monkeypatch):
    """Asserted by draining the bucket directly rather than by hammering the
    endpoint, so the test does not depend on how long a chart takes to render."""
    import api as api_module

    client, token = authed
    headers = token("user@t.com")

    # Empty this user's bucket.
    for _ in range(api_module.query_limiter.burst + 1):
        api_module.query_limiter.check("usr_a1b2c3d4")

    response = client.post("/query", json={"prompt": "Am I saving money?"}, headers=headers)
    assert response.status_code == 429
    assert response.json()["detail"]["error"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1

    # Another user is unaffected — the limit is per person, not global.
    api_module.query_limiter.reset("usr_mgr")
    assert client.post("/query", json={"prompt": "hi"}, headers=token("manager@t.com")).status_code != 429
    api_module.query_limiter.reset("usr_a1b2c3d4")


def test_cheap_endpoints_are_not_rate_limited(authed):
    import api as api_module

    client, token = authed
    headers = token("user@t.com")
    for _ in range(api_module.query_limiter.burst + 5):
        api_module.query_limiter.check("usr_a1b2c3d4")

    # Reading who you are, or your own inbox, costs nothing upstream.
    assert client.get("/auth/me", headers=headers).status_code == 200
    assert client.get("/requests", headers=headers).status_code == 200
    api_module.query_limiter.reset("usr_a1b2c3d4")


def test_allowed_requests_advertise_the_remaining_allowance(authed):
    """A client should be able to slow down before it is refused."""
    import api as api_module

    client, token = authed
    api_module.query_limiter.reset("usr_a1b2c3d4")
    response = client.post(
        "/query", json={"prompt": "Am I saving money?"}, headers=token("user@t.com")
    )
    assert response.status_code == 200
    assert "X-RateLimit-Remaining" in response.headers
    api_module.query_limiter.reset("usr_a1b2c3d4")


# == revocation ===============================================================
# A verified token proves what was true when it was minted, not what is true
# now. Without a live check, removing someone has no effect until their token
# expires — up to an hour — and they keep whatever access they had.


def test_a_disabled_account_is_rejected_immediately(authed):
    import api as api_module
    from src.auth.dependencies import _revocation_cache
    from sqlalchemy import update

    from src.db.schema import credentials

    client, token = authed
    headers = token("manager@t.com")
    assert client.get("/auth/me", headers=headers).status_code == 200

    with api_module.auth_engine.begin() as conn:
        conn.execute(
            update(credentials).where(credentials.c.user_id == "usr_mgr").values(is_active=False)
        )
    _revocation_cache.clear()

    response = client.get("/auth/me", headers=headers)
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "account_disabled"


def test_a_demoted_manager_loses_read_any_without_re_logging_in(authed):
    import api as api_module
    from src.auth.dependencies import _revocation_cache
    from sqlalchemy import update

    from src.db.schema import credentials

    client, token = authed
    headers = token("manager@t.com")
    assert (
        client.post(
            "/query", json={"prompt": "What did I spend?", "user_id": "usr_a1b2c3d4"}, headers=headers
        ).status_code
        == 200
    )

    with api_module.auth_engine.begin() as conn:
        conn.execute(update(credentials).where(credentials.c.user_id == "usr_mgr").values(role="user"))
    _revocation_cache.clear()

    # The token still *says* read:any; the role no longer grants it.
    assert client.get("/auth/me", headers=headers).json()["can_read_all"] is False
    assert (
        client.post(
            "/query", json={"prompt": "What did I spend?", "user_id": "usr_a1b2c3d4"}, headers=headers
        ).status_code
        == 403
    )
    # Their own data still works — this narrows privilege, it does not lock out.
    assert client.post("/query", json={"prompt": "Am I saving money?"}, headers=headers).status_code == 200


def test_scopes_can_only_narrow_never_widen(authed):
    """A promotion must still require a fresh login: intersecting means a token
    cannot gain a privilege it was not issued with."""
    import api as api_module
    from src.auth.dependencies import _revocation_cache
    from sqlalchemy import update

    from src.db.schema import credentials

    client, token = authed
    headers = token("user@t.com")  # issued as a plain user

    with api_module.auth_engine.begin() as conn:
        conn.execute(
            update(credentials).where(credentials.c.user_id == "usr_a1b2c3d4").values(role="admin")
        )
    _revocation_cache.clear()

    assert client.get("/auth/me", headers=headers).json()["can_read_all"] is False
    assert client.get("/auth/accounts", headers=headers).status_code == 403


def test_paste_preview_works_but_commit_refuses_without_a_store(authed):
    """Reporting "committed, N rows inserted" while writing where nothing reads
    is worse than refusing: the operator sees success and no change."""
    client, token = authed
    headers = token("manager@t.com")
    paste = "user_id\tuser_name\tdate\tamount\tcategory\nusr_z\tZed\t2025-11-02\t10\tX_FOOD"

    preview = client.post("/ingest/paste", json={"text": paste, "commit": False}, headers=headers)
    assert preview.status_code == 200
    assert preview.json()["parse"]["rows_parsed"] == 1
    assert preview.json()["committed"] is False

    commit = client.post("/ingest/paste", json={"text": paste, "commit": True}, headers=headers)
    assert commit.status_code == 501
    assert commit.json()["detail"]["error"] == "ingest_unavailable"
