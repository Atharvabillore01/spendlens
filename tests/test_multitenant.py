"""The pipeline running on the SQL backend, across tenants, behind auth.

`test_sql_backend.py` proves the store is equivalent. This file proves the whole
turn is: same figures, same guardrails, same cache semantics — and that none of
it crosses a tenant boundary or serves an unauthenticated caller.
"""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from src.auth.dependencies import require_principal
from src.auth.principal import SCOPE_QUERY, Principal, mint_token
from src.cache import keys
from src.cache.kv_cache import InMemoryKVCache
from src.data.sql_store import SqlUserDataStore
from src.db.engine import build_engine
from src.db.schema import create_all
from src.ingest.loader import ingest_frame
from src.pipeline import TransactionRAGPipeline
from src.tenancy import TenantPipelineCache

from conftest import FakeOpenRouterClient


@pytest.fixture
def engine(settings):
    eng = build_engine(settings, "sqlite:///:memory:")
    create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def two_tenants(engine, raw_df):
    """Same user ids in both tenants, different amounts — so a leak is visible."""
    acme = raw_df.copy()
    acme["user_id"] = "usr_shared"
    acme["user_name"] = "Acme Person"

    globex = raw_df.copy()
    globex["user_id"] = "usr_shared"
    globex["user_name"] = "Globex Person"
    globex["transaction_amount"] = globex["transaction_amount"] * 3

    ingest_frame(engine, "acme", acme)
    ingest_frame(engine, "globex", globex)
    return engine


def sql_pipeline(engine, tenant_id, settings, script=None, cache=None):
    store = SqlUserDataStore(engine, tenant_id)
    return TransactionRAGPipeline(
        store=store,
        settings=settings,
        cache=cache,
        llm_client=FakeOpenRouterClient(script),
    )


# == the pipeline on SQL ======================================================


def test_a_full_turn_runs_on_the_sql_backend(engine, raw_df, settings):
    ingest_frame(engine, "acme", raw_df)
    store = SqlUserDataStore(engine, "acme")
    user_id = store.user_ids[0]

    pipe = sql_pipeline(
        engine,
        "acme",
        settings,
        script=[
            FakeOpenRouterClient.tool_call("plot_category_breakdown", {"period": "last_month"}),
            FakeOpenRouterClient.text("Housing led your spending last month."),
        ],
    )
    result = pipe.run(user_id, "What did I spend the most on last month?")

    assert result["user_name"]
    assert result["visualizations"]
    assert result["data_summary"]["plot_category_breakdown"]["total_spend"] > 0
    assert "user_not_found" not in result.get("guardrail_flags", [])


def test_sql_and_dataframe_turns_produce_the_same_figures(engine, raw_df, settings):
    """The equivalence that matters: the number the user sees is backend-independent."""
    ingest_frame(engine, "acme", raw_df)
    script = [
        FakeOpenRouterClient.tool_call("plot_category_breakdown", {"period": "last_month"}),
        FakeOpenRouterClient.text("A grounded sentence with no figures."),
    ]

    sql_pipe = sql_pipeline(engine, "acme", settings, script=list(script))
    df_pipe = TransactionRAGPipeline(
        df=raw_df, settings=settings, llm_client=FakeOpenRouterClient(list(script))
    )

    user_id = df_pipe.store.user_ids[0]
    sql_result = sql_pipe.run(user_id, "What did I spend the most on last month?")
    df_result = df_pipe.run(user_id, "What did I spend the most on last month?")

    sql_summary = sql_result["data_summary"]["plot_category_breakdown"]
    df_summary = df_result["data_summary"]["plot_category_breakdown"]
    assert sql_summary["total_spend"] == df_summary["total_spend"]
    assert sql_summary["top_category"] == df_summary["top_category"]


def test_an_unknown_user_is_a_structured_error_not_an_exception(engine, raw_df, settings):
    ingest_frame(engine, "acme", raw_df)
    pipe = sql_pipeline(engine, "acme", settings)
    result = pipe.run("usr_not_here", "what did I spend?")
    assert result["error"] == "user_not_found"


def test_the_cross_user_guardrail_works_against_the_sql_roster(engine, raw_df, settings):
    ingest_frame(engine, "acme", raw_df)
    store = SqlUserDataStore(engine, "acme")
    me, other = store.user_ids[0], store.user_ids[1]
    other_name = store.user_name(other).split()[0]

    pipe = sql_pipeline(engine, "acme", settings)
    result = pipe.run(me, f"How much did {other_name} spend last month?")
    assert "cross_user_access_attempt" in result["guardrail_flags"]


# == tenant isolation, end to end =============================================

def test_two_tenants_sharing_a_user_id_get_their_own_figures(two_tenants, settings):
    script = [
        FakeOpenRouterClient.tool_call("plot_category_breakdown", {"period": "last_month"}),
        FakeOpenRouterClient.text("No figures here."),
    ]
    acme = sql_pipeline(two_tenants, "acme", settings, script=list(script))
    globex = sql_pipeline(two_tenants, "globex", settings, script=list(script))

    acme_result = acme.run("usr_shared", "What did I spend the most on last month?")
    globex_result = globex.run("usr_shared", "What did I spend the most on last month?")

    assert acme_result["user_name"] == "Acme Person"
    assert globex_result["user_name"] == "Globex Person"

    acme_spend = acme_result["data_summary"]["plot_category_breakdown"]["total_spend"]
    globex_spend = globex_result["data_summary"]["plot_category_breakdown"]["total_spend"]
    assert globex_spend == pytest.approx(acme_spend * 3)


def test_the_cache_does_not_pool_a_shared_user_id_across_tenants(two_tenants, settings):
    """A bare `user:{id}:profile` key would serve Acme's profile to Globex.

    The read never reaches storage, so the tenant filter in SQL cannot save it —
    the key itself has to carry the tenant.
    """
    shared_cache = InMemoryKVCache()

    acme = sql_pipeline(two_tenants, "acme", settings, cache=shared_cache)
    globex = sql_pipeline(two_tenants, "globex", settings, cache=shared_cache)

    acme_profile, acme_hit = acme.cache.get_or_build_profile(
        "usr_shared", acme.profiles.build
    )
    globex_profile, globex_hit = globex.cache.get_or_build_profile(
        "usr_shared", globex.profiles.build
    )

    assert acme_hit is False
    # The second tenant must miss, not inherit the first tenant's entry.
    assert globex_hit is False
    assert acme_profile["user_name"] == "Acme Person"
    assert globex_profile["user_name"] == "Globex Person"

    stored = set(shared_cache.keys())
    assert any("tenant:acme" in k for k in stored)
    assert any("tenant:globex" in k for k in stored)


def test_default_tenant_keeps_the_briefs_unprefixed_key_names():
    """The brief fixes these key names; a single-tenant deployment keeps them."""
    assert keys.profile("usr_1") == "user:usr_1:profile"
    assert keys.profile("usr_1", "default") == "user:usr_1:profile"
    assert keys.profile("usr_1", "acme") == "tenant:acme:user:usr_1:profile"


# == the tenant pipeline cache ================================================


def test_the_same_tenant_gets_the_same_warm_pipeline(two_tenants, settings):
    cache = TenantPipelineCache(settings=settings, engine=two_tenants)
    assert cache.get("acme") is cache.get("acme")
    assert cache.get("acme") is not cache.get("globex")
    assert cache.warm_tenants == 2


def test_eviction_keeps_the_cache_bounded(two_tenants, settings):
    small = settings.model_copy(update={"tenant_cache_size": 1})
    cache = TenantPipelineCache(settings=small, engine=two_tenants)
    cache.get("acme")
    cache.get("globex")
    assert cache.warm_tenants == 1


def test_invalidating_a_tenant_rebuilds_it(two_tenants, settings):
    cache = TenantPipelineCache(settings=settings, engine=two_tenants)
    first = cache.get("acme")
    cache.invalidate("acme")
    assert cache.get("acme") is not first


def test_a_new_upload_is_visible_after_invalidation(engine, raw_df, settings):
    """Ingest must not leave a tenant serving a stale taxonomy."""
    ingest_frame(engine, "acme", raw_df[raw_df["transaction_category_detail"].str.endswith("_FOOD")])
    cache = TenantPipelineCache(settings=settings, engine=engine)
    before = set(cache.get("acme").taxonomy.parents)

    ingest_frame(engine, "acme", raw_df)
    cache.invalidate("acme")
    after = set(cache.get("acme").taxonomy.parents)

    assert before < after


# == authentication at the HTTP boundary ======================================


@pytest.fixture
def auth_settings(settings):
    return settings.model_copy(
        update={
            "auth_required": True,
            "jwt_secret": "test-secret-not-a-real-one-0123456789abcdef",
            "jwt_algorithm": "HS256",
        }
    )


@pytest.fixture
def guarded_app(auth_settings, monkeypatch):
    """A minimal app exercising the real dependency against real settings."""
    monkeypatch.setattr("src.auth.dependencies.get_settings", lambda: auth_settings)

    app = FastAPI()

    @app.get("/whoami")
    def whoami(principal: Principal = Depends(require_principal)):
        return {
            "tenant_id": principal.tenant_id,
            "user_id": principal.user_id,
            "scopes": sorted(principal.scopes),
        }

    return TestClient(app, raise_server_exceptions=False)


def test_a_request_with_no_token_is_401(guarded_app):
    response = guarded_app.get("/whoami")
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthenticated"


def test_a_valid_token_identifies_the_caller(guarded_app, auth_settings):
    token = mint_token(auth_settings, "acme", "usr_1", scopes=[SCOPE_QUERY])
    response = guarded_app.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"tenant_id": "acme", "user_id": "usr_1", "scopes": ["query"]}


def test_a_forged_token_is_401(guarded_app):
    import jwt as pyjwt  # noqa: PLC0415

    forged = pyjwt.encode({"sub": "usr_1", "tenant": "acme", "exp": 9_999_999_999}, "wrong-key-but-long-enough-x", algorithm="HS256")
    response = guarded_app.get("/whoami", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


def test_a_malformed_authorization_header_is_401(guarded_app):
    for header in ("", "Basic abc", "Bearer", "Bearer   "):
        response = guarded_app.get("/whoami", headers={"Authorization": header})
        assert response.status_code == 401, header


def test_auth_cannot_be_enabled_without_a_usable_key(settings):
    """Fail at construction, not per-request: an empty key verifies nothing."""
    with pytest.raises(ValueError, match="JWT_SECRET is empty"):
        type(settings)(auth_required=True, jwt_secret="", jwt_algorithm="HS256")


def test_a_short_hmac_secret_is_refused(settings):
    with pytest.raises(ValueError, match="at least 32"):
        type(settings)(auth_required=True, jwt_secret="tooshort", jwt_algorithm="HS256")


def test_asymmetric_auth_needs_a_public_key(settings):
    with pytest.raises(ValueError, match="JWT_PUBLIC_KEY is empty"):
        type(settings)(auth_required=True, jwt_algorithm="RS256", jwt_public_key="")
