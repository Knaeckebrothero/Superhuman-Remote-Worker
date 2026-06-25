"""Unit tests for the worker checkpointer config resolvers (D3).

Covers the pure env-driven helpers in ``src/utils/db_url.py`` that select the
LangGraph checkpointer backend and resolve the Postgres checkpoint DSN — no DB or
langgraph import needed. See
docs/issues/cross_pod_resume_cold_starts_checkpoint_not_replicated.md.
"""

from __future__ import annotations

import pytest

from src.utils.db_url import checkpointer_backend, resolve_checkpoint_url

_ENV_VARS = (
    "CHECKPOINTER_BACKEND",
    "CHECKPOINT_DB_URL",
    "CHECKPOINT_USER",
    "CHECKPOINT_PASSWORD",
    "CHECKPOINT_HOST",
    "CHECKPOINT_PORT",
    "CHECKPOINT_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "DATABASE_URL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


class TestCheckpointerBackend:
    def test_default_is_sqlite(self):
        assert checkpointer_backend() == "sqlite"

    def test_reads_env_normalized(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINTER_BACKEND", "  Postgres ")
        assert checkpointer_backend() == "postgres"


class TestResolveCheckpointUrl:
    def test_none_when_unconfigured(self):
        assert resolve_checkpoint_url() is None

    def test_explicit_checkpoint_db_url_wins(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINT_DB_URL", "postgresql://x:y@h:5432/ckpt")
        # app-DB vars present too — the explicit dedicated URL must take precedence
        monkeypatch.setenv("POSTGRES_USER", "srw")
        monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
        monkeypatch.setenv("POSTGRES_HOST", "srw-postgres")
        monkeypatch.setenv("POSTGRES_DB", "srw")
        assert resolve_checkpoint_url() == "postgresql://x:y@h:5432/ckpt"

    def test_dedicated_checkpoint_parts(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINT_USER", "ck")
        monkeypatch.setenv("CHECKPOINT_PASSWORD", "pw")
        monkeypatch.setenv("CHECKPOINT_HOST", "ckhost")
        monkeypatch.setenv("CHECKPOINT_DB", "ckdb")
        assert resolve_checkpoint_url() == "postgresql://ck:pw@ckhost:5432/ckdb"

    def test_falls_back_to_app_db(self, monkeypatch):
        monkeypatch.setenv("POSTGRES_USER", "srw")
        monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
        monkeypatch.setenv("POSTGRES_HOST", "srw-postgres")
        monkeypatch.setenv("POSTGRES_DB", "srw")
        assert resolve_checkpoint_url() == "postgresql://srw:pw@srw-postgres:5432/srw"

    def test_database_url_last_resort(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://legacy@h/db")
        assert resolve_checkpoint_url() == "postgresql://legacy@h/db"
