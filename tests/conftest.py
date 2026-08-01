"""Shared fixtures. No test in this suite makes a network call.

The suite is hermetic by construction: the environment below is pinned *before*
anything imports `src.config`, so a developer's `.env` cannot decide whether
these tests run against auth, a live model, a seeded database or a set of
published credentials. Without this, adding `AUTH_REQUIRED=true` to a local
`.env` silently turns every API test into a 401.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Env vars beat the .env file in pydantic-settings, so these win outright.
os.environ.update(
    {
        "AUTH_REQUIRED": "false",
        "SHOW_LOGIN_HINTS": "false",
        "STORAGE_BACKEND": "dataframe",
        "TRACE_TURNS": "false",
        "LLM_PROVIDER": "auto",
    }
)

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Settings  # noqa: E402
from src.data.user_data_store import UserDataStore  # noqa: E402
from src.llm.openrouter_client import LLMResponse, LLMUnavailableError  # noqa: E402
from src.llm.scripted import ScriptedLLMClient  # noqa: E402
from src.pipeline import TransactionRAGPipeline  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = PROJECT_ROOT / "assessment_transaction_data.xlsx"


# The scripted client ships in `src/` -- it is how OFFLINE_LLM=1 works in the
# running service, not a test-only construct. Re-exported here so the existing
# fixtures and imports keep working.
FakeOpenRouterClient = ScriptedLLMClient


@pytest.fixture(scope="session")
def raw_df() -> pd.DataFrame:
    if not DATA_PATH.exists():  # pragma: no cover
        pytest.skip(f"dataset not found at {DATA_PATH}")
    return pd.read_excel(DATA_PATH)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        openrouter_api_key="test-key-not-real",
        chart_output_dir=tmp_path / "charts",
        cache_backend="memory",
        max_prompt_chars=200,
        query_history_max_n=3,
        audit_log_path=None,
    )


@pytest.fixture
def store(raw_df) -> UserDataStore:
    return UserDataStore(raw_df)


@pytest.fixture
def user_ids(store) -> tuple[str, ...]:
    return store.user_ids


@pytest.fixture
def make_pipeline(raw_df, settings):
    """Factory: `make_pipeline(script=[...])` -> (pipeline, fake_client)."""

    def _factory(script=None, **setting_overrides):
        cfg = settings.model_copy(update=setting_overrides) if setting_overrides else settings
        client = FakeOpenRouterClient(script)
        pipeline = TransactionRAGPipeline(df=raw_df, settings=cfg, llm_client=client)
        return pipeline, client

    return _factory


@pytest.fixture
def pipeline(make_pipeline):
    pipe, _ = make_pipeline()
    return pipe
