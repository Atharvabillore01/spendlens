"""Single source of truth for every tunable in the pipeline.

Nothing in `src/` is allowed to contain a magic number or a hardcoded model
name -- it all lands here, typed, with an env-var override. See README §Config.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Verified against OpenRouter's live model list: free tier AND advertising
# `tools` in supported_parameters AND confirmed to emit a real tool_call.
# Shipped as a default only; override with the MODEL_FALLBACK_CHAIN env var
# when OpenRouter's free-tier availability shifts (it does, often).
DEFAULT_MODEL_CHAIN = [
    "inclusionai/ling-3.0-flash:free",
    "google/gemma-4-31b-it:free",
    "openai/gpt-oss-20b:free",
]

# Groq models that support tool calling. Ordered capable-first: the pipeline
# needs a model that reliably emits a well-formed tool_call, and falling back to
# a smaller one costs accuracy in tool *selection* before it costs anything else.
# Availability shifts, so this is a default, not a constant -- override with
# GROQ_MODEL_CHAIN.
DEFAULT_GROQ_CHAIN = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-20b",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # `model_fallback_chain` would otherwise collide with pydantic's
        # reserved `model_` namespace.
        protected_namespaces=(),
    )

    # ---- LLM ----------------------------------------------------------------
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    model_fallback_chain: list[str] = Field(default_factory=lambda: list(DEFAULT_MODEL_CHAIN))

    # Groq. Same OpenAI chat-completions wire format as OpenRouter, so it needs
    # a key, a base URL and a model list -- no second client.
    #
    # `llm_provider` orders them: "auto" tries every provider that has a key,
    # Groq first because it is markedly faster and its free tier is not shared
    # with the rest of OpenRouter's traffic. Naming one provider pins it.
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model_chain: list[str] = Field(default_factory=lambda: list(DEFAULT_GROQ_CHAIN))
    llm_provider: Literal["auto", "groq", "openrouter"] = "auto"
    # When every live provider and model has been exhausted, answer from the
    # scripted client instead of failing the request. The numbers still come
    # from the real tools against the real data; only the interpretation of the
    # question is weaker, and the response reports `scripted/offline` as the
    # model so a degraded answer is never mistaken for a live one. Turn it off
    # to get a hard LLMUnavailableError back.
    offline_fallback: bool = True
    llm_timeout_s: float = 25.0
    llm_max_retries: int = 3
    llm_backoff_base_s: float = 0.5
    llm_backoff_factor: float = 2.0
    llm_temperature: float = 0.2

    # ---- Cache --------------------------------------------------------------
    cache_backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/0"
    profile_ttl_s: int = 60 * 60 * 24  # 24h
    query_history_ttl_s: int = 60 * 60 * 24 * 7  # 7d
    query_history_max_n: int = 5
    viz_state_ttl_s: int = 60 * 60  # 1h

    # ---- Guardrails ---------------------------------------------------------
    max_prompt_chars: int = 2000
    token_budget_input: int = 6000
    token_budget_output: int = 800
    # A number in the LLM's prose is "grounded" if it matches a computed value
    # within this relative tolerance (or `hallucination_abs_tolerance`).
    hallucination_rel_tolerance: float = 0.02
    hallucination_abs_tolerance: float = 1.0
    # Per-user rate limit. Upstream LLM quota is a shared resource: without
    # this, one user in a loop degrades the service for everyone on the same
    # key. `burst` is what may be spent at once after a quiet period.
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 20
    rate_limit_burst: int = 10
    # Ingestion is far more expensive per call, so it gets its own, tighter one.
    ingest_rate_limit_per_minute: int = 6
    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_cooldown_s: float = 60.0

    # ---- Storage ------------------------------------------------------------
    # How a rendered PNG reaches the client.
    #
    # "url"    -- write to disk, return /charts/{name}, serve it on a later
    #             request. Correct when one long-lived process both renders and
    #             serves, which is every container deployment.
    # "inline" -- return the PNG as a data: URI in the same response that
    #             rendered it, and delete the file. Required on serverless,
    #             where the GET that fetches the chart may land on a different
    #             instance than the POST that drew it -- different /tmp,
    #             different memory, so both the file and its ownership grant are
    #             gone. It also sidesteps a real limitation of the download
    #             link: <a download> is a plain browser navigation carrying no
    #             Authorization header, so with AUTH_REQUIRED=true the URL form
    #             is refused. A data: URI needs no credential at all.
    chart_delivery: Literal["url", "inline"] = "url"
    chart_output_dir: Path = PROJECT_ROOT / "output"
    chart_dpi: int = 120
    audit_log_path: Optional[Path] = None  # None -> stdout logger only

    # ---- Turn tracing -------------------------------------------------------
    # Prints every figure a turn computed, in clear, so a wrong-looking chart
    # can be traced to the number behind it. OFF by default and unsafe to enable
    # against real user data -- unlike the audit log, this is not redacted.
    trace_turns: bool = False
    trace_log_path: Optional[Path] = None  # additionally append JSONL here
    trace_level: str = "INFO"

    # ---- Time ---------------------------------------------------------------
    # Anchor for relative date expressions. Defaults at runtime to
    # max(transaction_date) so "last month" resolves inside the dataset
    # instead of against wall-clock time (the data ends 2025-12-31).
    as_of_date: Optional[date] = None

    # ---- Data ---------------------------------------------------------------
    data_path: Path = PROJECT_ROOT / "assessment_transaction_data.xlsx"

    # ---- Persistence --------------------------------------------------------
    # "dataframe" keeps the original single-file behaviour (the assessment
    # deliverable, the demo, and the test suite). "sql" is the multi-tenant
    # production path: transactions live in Postgres and are queried per user
    # per window, so process memory no longer scales with the dataset.
    storage_backend: Literal["dataframe", "sql"] = "dataframe"
    # Create missing tables during startup. Convenient for a container that
    # starts once and for tests that start against an empty database; wrong for
    # serverless, where "startup" happens on every cold invocation and this
    # spends seconds of DDL and reflection round trips to confirm a schema that
    # has not changed since the last deploy. Set false once the schema exists
    # and create it deliberately with `manage_accounts.py`.
    db_auto_create: bool = True
    # postgresql+psycopg://user:pass@host/db in production; sqlite:///... works
    # for local runs and is what the tests use.
    database_url: str = "sqlite:///" + str(PROJECT_ROOT / "data" / "ledger.db")
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_s: int = 30
    db_pool_recycle_s: int = 1800
    db_echo: bool = False
    # Rows per executemany batch during ingestion.
    ingest_chunk_size: int = 5_000
    # Cap on a single upload, so one client cannot exhaust the box.
    ingest_max_rows: int = 2_000_000

    # ---- Multi-tenancy ------------------------------------------------------
    # How many tenants' pipelines stay warm in one process. Each holds a
    # taxonomy and tool schemas, not transaction data, so these are small.
    tenant_cache_size: int = 256
    tenant_cache_ttl_s: int = 900
    # Default tenant for single-tenant deployments and the demo.
    default_tenant_id: str = "default"

    # ---- Auth ---------------------------------------------------------------
    # OFF by default so the assessment demo and tests run unchanged; any
    # deployment carrying real client data MUST set AUTH_REQUIRED=true.
    auth_required: bool = False
    # Lists the tenant's accounts on the sign-in page, with a shared password,
    # so the app can be handed to someone without a credentials email first.
    #
    # This publishes working logins to anyone who loads the page. It is OFF by
    # default and `/readyz` reports it as not-ready when it is on together with
    # real multi-tenant data -- an operator has to make that trade knowingly.
    show_login_hints: bool = False
    login_hint_password: str = ""
    # Which roles a published hint may name. `/auth/hints` is unauthenticated
    # by necessity -- it is read before anyone can sign in -- so whatever it
    # lists is public, and listing a manager or an admin next to a shared
    # password is granting that role to the internet. A public demo wants the
    # click-to-try convenience for ordinary account holders and nothing more.
    login_hint_roles: list[str] = Field(default_factory=lambda: ["user"])
    jwt_secret: str = ""
    jwt_algorithm: Literal["HS256", "HS384", "HS512", "RS256", "ES256"] = "HS256"
    # Asymmetric verification: PEM public key (RS256/ES256). Ignored for HS*.
    jwt_public_key: str = ""
    jwt_issuer: str = ""
    jwt_audience: str = ""
    jwt_leeway_s: int = 30
    jwt_default_ttl_s: int = 3600
    # How long a revoked account or a demoted role may keep working. 0 checks
    # the database on every request; a few seconds avoids a read per request
    # while keeping staleness far below the token lifetime.
    revocation_cache_s: int = 5
    # Signed chart URLs expire; a leaked link stops working.
    chart_token_ttl_s: int = 3600

    # ---- Time ---------------------------------------------------------------
    # "data_max" anchors relative dates to max(transaction_date) -- correct for
    # a frozen historical upload, and what the assessment data needs.
    # "now" anchors to wall-clock -- correct for live client data. Per-tenant
    # overrides live in the `tenants` table.
    as_of_mode: Literal["data_max", "now"] = "data_max"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def jwt_verification_key(self) -> str:
        """The key `jwt.decode` should verify with, per algorithm family."""
        return self.jwt_public_key if self.jwt_algorithm.startswith(("RS", "ES")) else self.jwt_secret

    @model_validator(mode="after")
    def _auth_must_be_usable(self) -> "Settings":
        """Refuse to start with auth enabled but unenforceable.

        Failing at boot is the whole point. The alternative -- starting, then
        rejecting every request at runtime, or worse, verifying against an empty
        key -- turns a configuration mistake into a live security incident.
        """
        if not self.auth_required:
            return self
        if not self.jwt_verification_key:
            field = "JWT_PUBLIC_KEY" if self.jwt_algorithm.startswith(("RS", "ES")) else "JWT_SECRET"
            raise ValueError(f"AUTH_REQUIRED is set but {field} is empty")
        if self.jwt_algorithm.startswith("HS") and len(self.jwt_secret) < 32:
            raise ValueError(
                f"JWT_SECRET is {len(self.jwt_secret)} chars; {self.jwt_algorithm} needs at least 32"
            )
        return self


_settings: Optional[Settings] = None


def get_settings(**overrides) -> Settings:
    """Process-wide settings singleton. Pass overrides in tests."""
    global _settings
    if overrides:
        return Settings(**overrides)
    if _settings is None:
        _settings = Settings()
    return _settings
