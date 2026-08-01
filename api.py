#!/usr/bin/env python3
"""FastAPI surface over the pipeline.

    uvicorn api:app --reload                 # http://127.0.0.1:8000
    OFFLINE_LLM=1 uvicorn api:app --reload   # scripted LLM (no key/quota)
    STORAGE_BACKEND=sql AUTH_REQUIRED=true uvicorn api:app   # multi-tenant

Endpoints:
    POST   /query               run a prompt as the authenticated user
    POST   /ingest              upload a client transaction file (scope ingest:write)
    GET    /ingest/batches      recent uploads for this tenant
    DELETE /ingest/{batch_id}   reverse one upload
    GET    /users               users in this tenant (paged)
    GET    /users/{id}/cache    inspect the three cache entries
    DELETE /users/{id}/cache    invalidate them
    GET    /charts/{name}       serve a rendered PNG, if it is yours
    GET    /healthz             liveness
    GET    /readyz              readiness

**Identity comes from the bearer token, never from the request body.** `/query`
has no `user_id` field for an ordinary caller; support tooling holding the
`read:any` scope may name one, and that is recorded as impersonation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Literal, Optional

import pandas as pd
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.auth.dependencies import (  # noqa: E402
    require_admin,
    require_ingest,
    require_principal,
    require_query,
)
from src.auth.accounts import (  # noqa: E402
    ROLES,
    authenticate,
    create_account,
    get_account,
    issue_for,
    list_accounts,
)
from src.auth.principal import SCOPE_READ_ANY, AuthError, Principal  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.db.engine import get_engine  # noqa: E402
from src.db.schema import create_all  # noqa: E402
from src.ingest.loader import IngestError, delete_batch, ingest_frame, read_table  # noqa: E402
from src.ingest.paste import parse_paste  # noqa: E402
from src import inbox  # noqa: E402
from src.cache.kv_cache import build_cache  # noqa: E402
from src.observability.circuit_breaker import BreakerState  # noqa: E402
from src.observability.metrics import metrics  # noqa: E402
from src.observability.rate_limit import RateLimiter  # noqa: E402
from src.pipeline import TransactionRAGPipeline, load_transactions  # noqa: E402
from src.tenancy import TenantPipelineCache  # noqa: E402

log = logging.getLogger("transaction_rag.api")

settings = get_settings()
app = FastAPI(
    title="Transaction RAG Pipeline",
    description=(
        "Multi-tenant agentic AI pipeline over tabular financial data, with "
        "token-scoped identity, per-user KV caching, tool-called visualizations "
        "and LLM guardrails."
    ),
    version="2.0.0",
)

# OFFLINE_LLM=1 swaps in the scripted client used by `demo.py --offline`, so the
# service can be demoed with no API key and no free-tier quota. Everything else
# -- guardrails, cache, dispatch, charts, composition -- runs unchanged.
_offline = os.getenv("OFFLINE_LLM") == "1"
_client = None
if _offline:
    from demo import offline_client  # noqa: PLC0415

    _client = offline_client()


# -- storage backends ---------------------------------------------------------
#
# "dataframe" is the original single-file deployment: one pipeline, one dataset,
# no tenancy. "sql" is the multi-tenant path -- pipelines are built per tenant
# and cached, and no client's transactions are held in process memory.

_engine = None
_tenants: Optional[TenantPipelineCache] = None
# Kept at module scope under its original name: the single-tenant deployment
# has exactly one, and the test suite and tooling reach for `api.pipeline`.
pipeline: Optional[TransactionRAGPipeline] = None

if settings.storage_backend == "sql":
    _engine = get_engine(settings)
    if settings.db_auto_create:
        create_all(_engine)
    _tenants = TenantPipelineCache(settings=settings, llm_client=_client, engine=_engine)
else:
    pipeline = TransactionRAGPipeline(
        df=load_transactions(), settings=settings, llm_client=_client
    )

# Logins are not transaction data. Even the single-file deployment has accounts,
# so the auth store is created regardless of which storage backend is serving
# transactions -- otherwise login would only work in `sql` mode.
auth_engine = _engine or get_engine(settings)
if settings.db_auto_create:
    create_all(auth_engine)

# Rate limiting shares the pipeline's cache backend, so switching CACHE_BACKEND
# to redis makes the limit correct across workers with no change here. Two
# buckets: asking is cheap and frequent, ingesting is neither.
_rl_cache = build_cache(settings)
query_limiter = RateLimiter(
    _rl_cache,
    settings.rate_limit_per_minute,
    settings.rate_limit_burst,
    settings.rate_limit_enabled,
    namespace="rl:query",
)
ingest_limiter = RateLimiter(
    _rl_cache,
    settings.ingest_rate_limit_per_minute,
    burst=2,
    enabled=settings.rate_limit_enabled,
    namespace="rl:ingest",
)


def enforce_limit(limiter: RateLimiter, subject: str, response: Response) -> None:
    """429 with Retry-After, and the allowance headers on every outcome.

    Headers are set even when the request is allowed so a well-behaved client
    can slow down *before* it is refused rather than after.
    """
    decision = limiter.check(subject or "anonymous")
    for header, value in decision.headers.items():
        response.headers[header] = value
    if not decision.allowed:
        metrics.record_rate_limited(limiter.namespace)
        log.warning("rate limited subject=%s retry_after=%.1fs", subject, decision.retry_after)
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limited",
                "message": f"Too many requests. Try again in {int(decision.retry_after) + 1}s.",
                "retry_after_s": int(decision.retry_after) + 1,
            },
            headers=decision.headers,
        )


def pipeline_for(principal: Principal) -> TransactionRAGPipeline:
    """The pipeline serving this caller's tenant."""
    if _tenants is not None:
        return _tenants.get(principal.tenant_id)
    return pipeline  # type: ignore[return-value]


def resolve_user(principal: Principal, requested: Optional[str], pipe) -> tuple[str, bool]:
    """Which user this request reads, honouring scope.

    In anonymous mode the token carries no subject, so a single-tenant demo
    deployment still has to name a user. It may only name one that exists, and
    it can never be used to reach another tenant -- there is only one.
    """
    if principal.user_id:
        try:
            return principal.resolve_target_user(requested)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"error": exc.code, "message": exc.message},
            ) from exc

    if not requested:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "user_required",
                "message": "anonymous mode needs an explicit user_id; enable AUTH_REQUIRED to derive it from a token",
            },
        )
    return requested, False


ROOT = Path(__file__).resolve().parent
FRONTEND_DIST = ROOT / "frontend" / "dist"

# The UI is a Vite/React build. In development it is served by `npm run dev`
# (which proxies these API routes back here), so the bundle may legitimately be
# absent -- the API must still start.
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


def _spa_shell() -> Response:
    bundle = FRONTEND_DIST / "index.html"
    if not bundle.is_file():
        return PlainTextResponse(
            "Frontend not built.\n\n"
            "  cd frontend && npm install && npm run build\n\n"
            "Or run the dev server instead:  cd frontend && npm run dev",
            status_code=503,
        )
    # No-store: the HTML references hashed asset filenames, so a stale shell is
    # the one thing that can point a browser at a deleted bundle.
    return FileResponse(bundle, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def index() -> Response:
    """Serve the built single-page app."""
    return _spa_shell()


def chart_refs(paths: list[str]) -> list[str]:
    """Turn rendered chart paths into references a client can actually fetch.

    Filesystem paths are never returned: they are meaningless to a client that
    is not on this machine. Which reference is right depends on whether the
    process that drew the chart will still be there when the browser asks for
    it -- see `chart_delivery` in the settings for why serverless cannot use a
    URL. Inlining consumes the file, since nothing will serve it afterwards.
    """
    if settings.chart_delivery != "inline":
        return [f"/charts/{Path(p).name}" for p in paths]

    refs: list[str] = []
    for raw in paths:
        path = Path(raw)
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:  # noqa: PERF203 - one bad chart must not fail the turn
            log.warning("chart %s could not be inlined: %s", path.name, exc)
            continue
        refs.append(f"data:image/png;base64,{encoded}")
        path.unlink(missing_ok=True)
    return refs





# == auth =====================================================================


class LoginRequest(BaseModel):
    email: str = Field(..., examples=["manager@acme.com"])
    password: str = Field(..., min_length=1)
    tenant_id: Optional[str] = Field(
        None, description="Only needed where one deployment serves several tenants."
    )


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    email: str
    role: str
    scopes: list[str]


@app.post("/auth/login", response_model=LoginResponse)
def login(request: LoginRequest) -> dict[str, Any]:
    """Exchange email + password for a scoped bearer token.

    Failures are deliberately indistinguishable: a wrong password and an unknown
    account return the same 401, so this cannot be used to enumerate accounts.
    """
    tenant_id = request.tenant_id or settings.default_tenant_id
    try:
        account = authenticate(auth_engine, tenant_id, request.email, request.password)
        token = issue_for(settings, account)
    except AuthError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}
        ) from exc

    log.info("login tenant=%s user=%s role=%s", tenant_id, account.user_id, account.role)
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.jwt_default_ttl_s,
        **{k: v for k, v in account.as_dict().items() if k in {"user_id", "email", "role", "scopes"}},
    }


@app.get("/auth/me")
def me(principal: Principal = Depends(require_principal)) -> dict[str, Any]:
    """Who the bearer token says you are, and what it lets you do.

    The frontend uses this to decide which surfaces to render, but it is not the
    access control -- every endpoint re-checks scope for itself.
    """
    account = get_account(auth_engine, principal.tenant_id, principal.user_id)
    return {
        "tenant_id": principal.tenant_id,
        "user_id": principal.user_id,
        "scopes": sorted(principal.scopes),
        "role": account.role if account else None,
        "email": account.email if account else None,
        "can_read_all": principal.has(SCOPE_READ_ANY),
        "authenticated": bool(principal.user_id),
    }


class CreateAccountRequest(BaseModel):
    user_id: str
    email: str
    password: str = Field(..., min_length=8)
    role: str = Field("user", description="user | manager | admin")


@app.post("/auth/accounts", status_code=201)
def create_login(
    request: CreateAccountRequest,
    principal: Principal = Depends(require_admin),
) -> dict[str, Any]:
    """Grant somebody a login. Admin only."""
    if request.role not in ROLES:
        raise HTTPException(
            status_code=400,
            detail={"error": "unknown_role", "message": f"role must be one of {list(ROLES)}"},
        )
    try:
        account = create_account(
            auth_engine,
            principal.tenant_id,
            request.user_id,
            request.email,
            request.password,
            request.role,
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}
        ) from exc
    return account.as_dict()


class LoginHint(BaseModel):
    email: str
    role: str
    user_id: str


@app.get("/auth/hints")
def login_hints() -> dict[str, Any]:
    """Sign-in credentials for the login page.

    Unauthenticated by necessity -- it is read *before* anyone can sign in --
    which is precisely why it must stay off unless deliberately enabled. When
    disabled it 404s rather than returning an empty list, so the endpoint's
    existence is not itself a hint.
    """
    if not settings.show_login_hints:
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    accounts = list_accounts(auth_engine, settings.default_tenant_id)
    # Role filter, not a display preference: everything returned here is public,
    # so naming a privileged account alongside the shared password would publish
    # that privilege. The manager and admin doors still exist -- they just need
    # a credential nobody handed out.
    allowed = set(settings.login_hint_roles)
    return {
        "accounts": [
            {"email": a.email, "role": a.role, "user_id": a.user_id}
            for a in accounts
            if a.is_active and a.role in allowed
        ],
        # The server stores only hashes, so it cannot reveal real passwords.
        # This is the shared one an operator set when seeding.
        "password": settings.login_hint_password,
    }


@app.get("/auth/accounts")
def accounts(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    return {"accounts": [a.as_dict() for a in list_accounts(auth_engine, principal.tenant_id)]}


# == query ====================================================================


class QueryRequest(BaseModel):
    prompt: str = Field(..., examples=["What did I spend the most on last month?"])
    theme: Literal["light", "dark"] = Field(
        "light", description="Chart palette to render for. Presentation only."
    )
    user_id: Optional[str] = Field(
        None,
        description=(
            "Whose data to read. Ignored unless the token carries the 'read:any' "
            "scope; ordinary callers always read themselves."
        ),
    )


class QueryResponse(BaseModel):
    user_name: Optional[str]
    response: str
    data_summary: dict[str, Any]
    visualizations: list[str]
    cache_hit: bool
    latency_ms: int
    guardrail_flags: list[str]
    user_id: str
    model_used: Optional[str] = None
    degraded: bool = False
    error: Optional[str] = None
    message: Optional[str] = None


@app.post("/query", response_model=QueryResponse)
def query(
    request: QueryRequest,
    response: Response,
    principal: Principal = Depends(require_query),
) -> dict[str, Any]:
    """Run one turn. Never raises — failures come back as structured fields."""
    pipe = pipeline_for(principal)
    user_id, impersonated = resolve_user(principal, request.user_id, pipe)
    # Keyed on the *caller*, not the target: a manager reading ten accounts is
    # still one person spending one quota.
    enforce_limit(query_limiter, principal.user_id or user_id, response)

    if impersonated:
        # Support access to somebody else's financial data is exactly the event
        # an audit needs to show; logging it here means it is recorded even if
        # the turn itself fails.
        log.warning(
            "impersonation tenant=%s actor=%s target=%s",
            principal.tenant_id, principal.user_id, user_id,
        )

    result = pipe.run(
        user_id,
        request.prompt,
        chart_theme=request.theme,
        # A manager/analyst token may ask population questions ("who spends the
        # most?"). An ordinary caller asking the same words is refused.
        can_read_all=principal.has(SCOPE_READ_ANY),
    )
    # Return chart URLs rather than filesystem paths so the response is usable
    # by a client that isn't on this machine.
    result["visualizations"] = chart_refs(result["visualizations"])

    model = result.get("model_used") or ""
    metrics.record_turn(
        latency_ms=result.get("latency_ms", 0),
        cache_hit=bool(result.get("cache_hit")),
        degraded=bool(result.get("degraded")),
        flags=result.get("guardrail_flags") or [],
        # Only attribute a provider when this turn actually reached a model.
        # `last_provider_used` is sticky on the client, so a guardrail-blocked
        # turn would otherwise be counted as an LLM request that never happened.
        provider=getattr(pipe.llm, "last_provider_used", None) if model else None,
        model=model or None,
        tools=[k for k in (result.get("data_summary") or {}) if k.startswith("plot_")],
    )
    return result


# == ingestion ================================================================


class IngestResponse(BaseModel):
    batch_id: str
    tenant_id: str
    filename: str
    total_rows: int
    inserted: int
    skipped_duplicates: int
    rejected_rows: int
    users_seen: int
    rejections: list[str]


@app.post("/ingest", response_model=IngestResponse)
async def ingest(
    file: UploadFile = File(..., description="CSV, XLSX or Parquet of transactions"),
    tenant_id: Optional[str] = Form(None),
    principal: Principal = Depends(require_ingest),
) -> dict[str, Any]:
    """Upload one client's transaction file.

    Idempotent: re-uploading the same rows inserts nothing. Atomic: a file
    either lands entirely or not at all.
    """
    if _engine is None:
        raise HTTPException(
            status_code=501,
            detail={
                "error": "ingest_unavailable",
                "message": "ingestion requires STORAGE_BACKEND=sql",
            },
        )

    # A tenant may only load into itself. Admins may name another.
    target_tenant = principal.tenant_id
    if tenant_id and tenant_id != principal.tenant_id:
        try:
            principal.require("admin")
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"error": exc.code, "message": "cannot ingest into another tenant"},
            ) from exc
        target_tenant = tenant_id

    suffix = Path(file.filename or "upload.csv").suffix.lower()
    scratch = Path(settings.chart_output_dir).parent / ".uploads"
    scratch.mkdir(parents=True, exist_ok=True)
    staged = scratch / f"{os.urandom(8).hex()}{suffix}"

    try:
        staged.write_bytes(await file.read())
        frame = read_table(staged)
        report = ingest_frame(
            _engine,
            target_tenant,
            frame,
            filename=file.filename or staged.name,
            chunk_size=settings.ingest_chunk_size,
            max_rows=settings.ingest_max_rows,
        )
    except IngestError as exc:
        raise HTTPException(
            status_code=422, detail={"error": "invalid_file", "message": str(exc)}
        ) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("ingest failed for tenant %s", target_tenant)
        raise HTTPException(
            status_code=500, detail={"error": "ingest_failed", "message": str(exc)}
        ) from exc
    finally:
        staged.unlink(missing_ok=True)

    # New data means a new taxonomy, a new anchor and stale profiles.
    if _tenants is not None:
        _tenants.invalidate(target_tenant)

    return report.as_dict()


@app.delete("/ingest/{batch_id}", status_code=200)
def revert_ingest(
    batch_id: str,
    principal: Principal = Depends(require_ingest),
) -> dict[str, Any]:
    """Reverse one upload. The reason every row carries its batch id."""
    if _engine is None:
        raise HTTPException(
            status_code=501,
            detail={"error": "ingest_unavailable", "message": "requires STORAGE_BACKEND=sql"},
        )
    removed = delete_batch(_engine, principal.tenant_id, batch_id)
    if _tenants is not None:
        _tenants.invalidate(principal.tenant_id)
    return {"batch_id": batch_id, "rows_removed": removed}


# == paste ingest =============================================================
#
# A sibling of POST /ingest rather than a mode of it: pasted text needs
# delimiter sniffing, header detection and column mapping before it is a table
# at all, and it is previewed before anything is written. A file upload has an
# artifact you can re-examine; a paste does not, so the preview is the safety net.


class PasteRequest(BaseModel):
    text: str = Field(..., description="Raw clipboard text, tab- or comma-separated.")
    has_header: Optional[bool] = Field(
        None, description="Omit to auto-detect from the first row."
    )
    delimiter: Optional[str] = Field(None, description="Omit to sniff; Excel pastes are tabs.")
    column_overrides: dict[str, str] = Field(
        default_factory=dict,
        description='Correct a mis-detected column: {"2": "transaction_date"} or {"Txn Date": "transaction_date"}.',
    )
    commit: bool = Field(False, description="False previews; true writes.")


@app.post("/ingest/paste")
def ingest_paste(
    request: PasteRequest,
    response: Response,
    principal: Principal = Depends(require_ingest),
) -> dict[str, Any]:
    """Parse pasted spreadsheet cells. Previews by default; writes on commit."""
    # Only the write costs anything meaningful; previewing is local parsing and
    # limiting it would make the column-mapping UI unusable.
    if request.commit:
        enforce_limit(ingest_limiter, principal.user_id or "anonymous", response)
    try:
        frame, report = parse_paste(
            request.text,
            delimiter=request.delimiter,
            has_header=request.has_header,
            column_overrides=request.column_overrides,
        )
    except IngestError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "paste_unreadable", "message": str(exc)}
        ) from exc

    payload: dict[str, Any] = {"parse": report.as_dict(), "committed": False}
    # A sample of what would land, so the mapping can be judged on real values
    # rather than on column names alone.
    payload["preview_rows"] = json.loads(
        frame.head(10).to_json(orient="records", date_format="iso")
    )

    if not report.ok:
        return payload
    if not request.commit:
        return payload

    # Previewing is pure parsing and always available. Committing needs a
    # transaction store: `POST /ingest` already refuses here, and reporting
    # "committed, N rows inserted" while writing somewhere nothing reads is
    # worse than refusing — the operator sees success and no change.
    if _engine is None:
        raise HTTPException(
            status_code=501,
            detail={
                "error": "ingest_unavailable",
                "message": (
                    "Importing needs STORAGE_BACKEND=sql. The paste was parsed "
                    "correctly but there is no transaction store to write it to."
                ),
            },
        )

    try:
        result = ingest_frame(
            _engine,
            principal.tenant_id,
            frame,
            filename="pasted",
            chunk_size=settings.ingest_chunk_size,
            max_rows=settings.ingest_max_rows,
        )
    except IngestError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "ingest_failed", "message": str(exc)}
        ) from exc

    _invalidate_tenant(principal.tenant_id)
    payload["committed"] = True
    payload["ingest"] = result.as_dict()
    return payload


def _invalidate_tenant(tenant_id: str) -> None:
    """New rows mean cached profiles and the tenant's pipeline are stale."""
    if _tenants is not None:
        _tenants.invalidate(tenant_id)


# == ask your manager =========================================================


class AskManagerRequest(BaseModel):
    question: str = Field(..., examples=["Why is my rent split across two categories?"])


class ReplyRequest(BaseModel):
    reply: str = Field(..., min_length=1)


@app.post("/requests", status_code=201)
def submit_request(
    request: AskManagerRequest,
    principal: Principal = Depends(require_query),
) -> dict[str, Any]:
    """Send a question to whoever manages this tenant."""
    pipe = pipeline_for(principal)
    user_id, _ = resolve_user(principal, None, pipe)
    name = pipe.store.user_name(user_id) if pipe.store.validate_user(user_id) else user_id
    try:
        created = inbox.submit(auth_engine, principal.tenant_id, user_id, name, request.question)
    except inbox.InboxError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}
        ) from exc
    return created.as_dict()


@app.get("/requests")
def list_requests(
    status: Optional[str] = Query(None, description="open | answered | closed"),
    principal: Principal = Depends(require_query),
) -> dict[str, Any]:
    """Your own questions, or every question in the tenant if you may read all.

    The scope check happens inside the query: an ordinary user's rows are never
    fetched and then filtered.
    """
    can_read_all = principal.has(SCOPE_READ_ANY)
    pipe = pipeline_for(principal)
    requester = principal.user_id or resolve_user(principal, None, pipe)[0]
    items = inbox.list_for(
        auth_engine,
        principal.tenant_id,
        requester_id=requester,
        can_read_all=can_read_all,
        status=status,
    )
    return {
        "requests": [i.as_dict() for i in items],
        "can_reply": can_read_all,
        "counts": inbox.counts(
            auth_engine, principal.tenant_id, requester_id=requester, can_read_all=can_read_all
        ),
    }


@app.post("/requests/{request_id}/run")
def run_request(
    request_id: str,
    response: Response,
    theme: Literal["light", "dark"] = "light",
    principal: Principal = Depends(require_query),
) -> dict[str, Any]:
    """Answer a question against the asker's own data.

    This is the reason the feature is worth building: the manager holds
    `read:any`, so the pipeline can compute a real answer with real figures
    instead of the manager reconstructing them by hand. Restricted to
    `read:any` -- without it a user could run their own question against
    themselves, which is just the chat box.
    """
    if not principal.has(SCOPE_READ_ANY):
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "running a request requires read:any"},
        )
    enforce_limit(query_limiter, principal.user_id or "anonymous", response)
    item = inbox.get(auth_engine, principal.tenant_id, request_id)
    if item is None:
        raise HTTPException(status_code=404, detail={"error": "request_not_found"})

    pipe = pipeline_for(principal)
    log.info(
        "inbox run tenant=%s actor=%s target=%s request=%s",
        principal.tenant_id, principal.user_id, item.from_user_id, request_id,
    )
    result = pipe.run(
        item.from_user_id, item.question, chart_theme=theme, can_read_all=True
    )
    result["visualizations"] = chart_refs(result["visualizations"])
    updated = inbox.attach_computed(
        auth_engine, principal.tenant_id, request_id, result["response"], result["data_summary"]
    )
    return {"request": updated.as_dict(), "result": result}


@app.post("/requests/{request_id}/reply")
def reply_to_request(
    request_id: str,
    request: ReplyRequest,
    principal: Principal = Depends(require_query),
) -> dict[str, Any]:
    if not principal.has(SCOPE_READ_ANY):
        raise HTTPException(
            status_code=403,
            detail={"error": "forbidden", "message": "replying requires read:any"},
        )
    try:
        updated = inbox.reply(
            auth_engine, principal.tenant_id, request_id, principal.user_id or "manager", request.reply
        )
    except inbox.InboxError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"error": exc.code, "message": exc.message}
        ) from exc
    return updated.as_dict()


# == users ====================================================================


@app.get("/users")
def users(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Users in the caller's tenant. Paged — a tenant may have very many."""
    pipe = pipeline_for(principal)
    store = pipe.store

    if hasattr(store, "list_users"):
        rows = store.list_users(limit=limit, offset=offset)
        total = store.user_count()
    else:
        all_ids = list(store.user_ids)
        rows = [(uid, store.user_name(uid)) for uid in all_ids[offset : offset + limit]]
        total = len(all_ids)

    # Transaction counts are per-user scans; only worth it for a small page.
    include_counts = len(rows) <= 50
    return {
        "users": [
            {
                "user_id": user_id,
                "user_name": user_name,
                **(
                    {"transaction_count": len(store.get_user_frame(user_id))}
                    if include_counts
                    else {}
                ),
            }
            for user_id, user_name in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "as_of": str(store.as_of.date()),
        "tenant_id": principal.tenant_id,
    }


@app.get("/users/{user_id}/cache")
def user_cache(
    user_id: str,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    pipe = pipeline_for(principal)
    target, _ = resolve_user(principal, user_id, pipe)
    if not pipe.store.validate_user(target):
        raise HTTPException(status_code=404, detail={"error": "user_not_found", "user_id": target})
    return pipe.cache_snapshot(target)


@app.delete("/users/{user_id}/cache", status_code=204)
def invalidate(
    user_id: str,
    principal: Principal = Depends(require_principal),
) -> Response:
    pipe = pipeline_for(principal)
    target, _ = resolve_user(principal, user_id, pipe)
    if not pipe.store.validate_user(target):
        raise HTTPException(status_code=404, detail={"error": "user_not_found", "user_id": target})
    pipe.invalidate_user(target)
    return Response(status_code=204)


# == charts ===================================================================


@app.get("/charts/{filename}")
def chart(
    filename: str,
    principal: Principal = Depends(require_principal),
) -> FileResponse:
    """Serve a rendered chart, if it belongs to the caller.

    Two checks, and both matter. The basename strip stops path traversal. The
    grant lookup stops one user reading another's chart -- the filename is
    unguessable, but a URL that leaks (a screenshot, a support ticket) would
    otherwise stay valid for anyone holding it.
    """
    safe_name = Path(filename).name
    path = Path(settings.chart_output_dir) / safe_name
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail={"error": "chart_not_found", "filename": safe_name}
        )

    pipe = pipeline_for(principal)
    grant = pipe.cache.chart_grant(safe_name)

    if grant is None:
        # Expired or evicted. Refusing is the safe reading: an unknown chart is
        # not demonstrably the caller's.
        raise HTTPException(
            status_code=404, detail={"error": "chart_expired", "filename": safe_name}
        )

    owner = grant.get("user_id")
    same_tenant = grant.get("tenant_id") == (pipe.tenant_id or "default")
    if not same_tenant or (principal.user_id and owner != principal.user_id and not principal.has("read:any")):
        raise HTTPException(
            status_code=403, detail={"error": "chart_forbidden", "filename": safe_name}
        )

    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


# == health ===================================================================


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    """Scrape endpoint.

    Unauthenticated on purpose: a scrape comes from the cluster, not a browser,
    and it carries no per-user data -- only counts, durations and states. Where
    the network cannot be trusted, restrict it at the ingress rather than by
    putting a bearer token in a Prometheus config.
    """
    # Sampled at scrape time rather than continuously: these are cheap reads and
    # a gauge that is only updated on request would go stale between turns.
    breaker = getattr(pipeline_for_metrics(), "breaker", None)
    if breaker is not None:
        metrics.gauge(
            "ledger_circuit_breaker_open",
            1 if breaker.state == BreakerState.OPEN else 0,
            "1 when the LLM circuit breaker is open.",
        )
    cache_backend = getattr(getattr(pipeline_for_metrics(), "cache", None), "backend", None)
    if cache_backend is not None and hasattr(cache_backend, "hit_rate"):
        metrics.gauge(
            "ledger_cache_hit_ratio", cache_backend.hit_rate, "Process-local cache hit ratio."
        )
    metrics.gauge("ledger_up", 1, "1 while the process is serving.")
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


def pipeline_for_metrics():
    """Any pipeline will do for process-level gauges; in multi-tenant mode the
    breaker and cache are shared, so the first warm tenant is representative."""
    if pipeline is not None:
        return pipeline
    warm = getattr(_tenants, "warm", None)
    return warm()[0] if callable(warm) and warm() else None


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"status": "ok", "mode": "offline" if _offline else "live"}


@app.get("/readyz")
def readyz(response: Response) -> dict[str, Any]:
    pipe = pipeline
    if pipe is None and _tenants is not None:
        pipe = _tenants.get(settings.default_tenant_id)

    health = pipe.health()
    ready = health["cache_ok"] and health["circuit_breaker"] != BreakerState.OPEN.value

    if _engine is not None:
        try:
            from sqlalchemy import text  # noqa: PLC0415

            with _engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            health["database_ok"] = True
        except Exception as exc:  # noqa: BLE001
            log.error("database unreachable: %s", exc)
            health["database_ok"] = False
            ready = False

    health["storage_backend"] = settings.storage_backend
    health["auth_required"] = settings.auth_required
    # A deployment holding real client data with auth off is not ready, whatever
    # else is healthy -- it is serving that data to anyone who asks.
    if settings.storage_backend == "sql" and not settings.auth_required:
        health["warning"] = "AUTH_REQUIRED is false while serving multi-tenant data"
        ready = False
    # Publishing working logins is a deliberate convenience, never a production
    # posture. Say so loudly rather than letting it pass a health check.
    health["rate_limit"] = (
        f"{settings.rate_limit_per_minute}/min" if settings.rate_limit_enabled else "disabled"
    )
    health["login_hints"] = settings.show_login_hints
    if settings.show_login_hints and settings.storage_backend == "sql":
        # Publishing a credential is only a fault when it reaches something
        # privileged. Ordinary account holders on demo data is a deliberate
        # posture for a public try-it deployment -- reported, never silent, but
        # not a reason to declare the service unfit to serve.
        # Readiness answers "can this serve traffic", and publishing a
        # credential does not stop it serving. What the operator needs is to see
        # exactly which roles they have put on the internet, every time they
        # look -- so this names them and stays out of the ready/not-ready
        # decision. A privileged role here is a deliberate choice for a demo,
        # and it is spelled out rather than silently tolerated.
        published = ", ".join(sorted(settings.login_hint_roles)) or "none"
        health["warning"] = f"SHOW_LOGIN_HINTS publishes working credentials for: {published}"

    if not ready:
        response.status_code = 503
    return {"ready": ready, **health}


# Client-side routes (/login, /manager/login) are addresses a browser can be
# pointed at directly -- a bookmark, a shared link, or a refresh. The server has
# no such routes, so without this a deep link 404s and the app looks broken.
#
# Declared last so it can never shadow a real endpoint, and it serves the shell
# only for known client routes: anything else still returns a JSON 404, so a
# typo'd API call does not come back as a page of HTML the caller cannot parse.
SPA_ROUTES = frozenset({"login", "manager/login"})


@app.get("/{full_path:path}", include_in_schema=False)
def spa_fallback(full_path: str) -> Response:
    if full_path in SPA_ROUTES:
        return _spa_shell()
    raise HTTPException(status_code=404, detail={"error": "not_found", "path": f"/{full_path}"})
