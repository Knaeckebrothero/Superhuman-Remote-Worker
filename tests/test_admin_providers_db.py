"""Tests for the DB helpers added in stage 3 (Admin → Providers surface).

Covers:
- system_api_keys CRUD (upsert encrypts, get decrypts, list/delete).
- system_llm_endpoints CRUD (create/update/delete; decrypted get for probe).
- system_llm_endpoint_models CRUD.
- Default LLM model helpers over system_settings.
- resolve_api_keys_for_job precedence (system < project < user).

Mocks the asyncpg pool the same way tests/test_thread_db.py does.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from orchestrator.database.postgres import PostgresDB
from orchestrator.security.crypto import decrypt, encrypt, is_encrypted


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_db(mock_conn):
    db = PostgresDB.__new__(PostgresDB)
    db._pool = MagicMock()
    db._connection_string = "test"
    db._queries = {}

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    db.acquire = _acquire
    return db


def _conn():
    return AsyncMock()


# ---------------------------------------------------------------------------
# system_api_keys
# ---------------------------------------------------------------------------


class TestSystemApiKeys:
    @pytest.mark.asyncio
    async def test_upsert_encrypts_on_write(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": UUID("11111111-1111-1111-1111-111111111111"),
                "provider": "openai",
                "key_prefix": "sk-proj-",
                "label": None,
                "seeded_from": None,
                "created_at": None,
                "updated_at": None,
            }
        )
        db = _make_db(conn)

        await db.upsert_system_api_key(
            provider="openai",
            api_key="sk-proj-plaintext-123",
            key_prefix="sk-proj-",
        )

        args = conn.fetchrow.await_args.args
        # Args order: SQL, provider, encrypted, key_prefix, label, seeded_from
        assert args[1] == "openai"
        assert is_encrypted(args[2])
        assert decrypt(args[2]) == "sk-proj-plaintext-123"
        assert args[3] == "sk-proj-"

    @pytest.mark.asyncio
    async def test_get_decrypts_on_read(self):
        conn = _conn()
        conn.fetchval = AsyncMock(return_value=encrypt("sk-value"))
        db = _make_db(conn)

        assert await db.get_system_api_key("openai") == "sk-value"
        conn.fetchval.assert_awaited()

    @pytest.mark.asyncio
    async def test_get_returns_none_when_missing(self):
        conn = _conn()
        conn.fetchval = AsyncMock(return_value=None)
        db = _make_db(conn)

        assert await db.get_system_api_key("openai") is None

    @pytest.mark.asyncio
    async def test_list_returns_prefix_only(self):
        conn = _conn()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": UUID("22222222-2222-2222-2222-222222222222"),
                    "provider": "anthropic",
                    "key_prefix": "sk-ant-1",
                    "label": "team",
                    "seeded_from": "helm:llm.seed",
                    "created_at": None,
                    "updated_at": None,
                }
            ]
        )
        db = _make_db(conn)

        rows = await db.list_system_api_keys()
        assert rows[0]["provider"] == "anthropic"
        assert "api_key" not in rows[0]

    @pytest.mark.asyncio
    async def test_delete_returns_true_on_hit(self):
        conn = _conn()
        conn.execute = AsyncMock(return_value="DELETE 1")
        db = _make_db(conn)
        assert await db.delete_system_api_key("openai") is True

    @pytest.mark.asyncio
    async def test_delete_returns_false_on_miss(self):
        conn = _conn()
        conn.execute = AsyncMock(return_value="DELETE 0")
        db = _make_db(conn)
        assert await db.delete_system_api_key("openai") is False


# ---------------------------------------------------------------------------
# resolve_api_keys_for_job — precedence (system < project < user)
# ---------------------------------------------------------------------------


class TestResolveApiKeysPrecedence:
    @pytest.mark.asyncio
    async def test_system_only(self):
        conn = _conn()
        conn.fetch = AsyncMock(
            return_value=[{"provider": "openai", "api_key": encrypt("system-key")}]
        )
        db = _make_db(conn)

        resolved = await db.resolve_api_keys_for_job(user_id=None, project_id=None)
        assert resolved == {"openai": "system-key"}

    @pytest.mark.asyncio
    async def test_user_overrides_system(self):
        conn = _conn()
        # Order of calls: system fetch, user fetch.
        conn.fetch = AsyncMock(
            side_effect=[
                [{"provider": "openai", "api_key": encrypt("system-key")}],
                [{"provider": "openai", "api_key": encrypt("user-key")}],
            ]
        )
        db = _make_db(conn)

        resolved = await db.resolve_api_keys_for_job(
            user_id="aaaaaaaa-0000-0000-0000-000000000001", project_id=None
        )
        assert resolved == {"openai": "user-key"}

    @pytest.mark.asyncio
    async def test_project_overrides_system_user_overrides_project(self):
        conn = _conn()
        conn.fetch = AsyncMock(
            side_effect=[
                # system
                [
                    {"provider": "openai", "api_key": encrypt("system-openai")},
                    {"provider": "groq", "api_key": encrypt("system-groq")},
                ],
                # project
                [{"provider": "openai", "api_key": encrypt("proj-openai")}],
                # user
                [{"provider": "openai", "api_key": encrypt("user-openai")}],
            ]
        )
        db = _make_db(conn)

        resolved = await db.resolve_api_keys_for_job(
            user_id="aaaaaaaa-0000-0000-0000-000000000001",
            project_id="bbbbbbbb-0000-0000-0000-000000000002",
        )
        # openai: user wins; groq: only system had it, survives.
        assert resolved == {"openai": "user-openai", "groq": "system-groq"}

    @pytest.mark.asyncio
    async def test_non_encrypted_rows_are_dropped(self):
        """A legacy plaintext row is logged+skipped, not returned."""
        conn = _conn()
        conn.fetch = AsyncMock(
            return_value=[
                {"provider": "openai", "api_key": "sk-plain-legacy"},
                {"provider": "groq", "api_key": encrypt("valid")},
            ]
        )
        db = _make_db(conn)

        resolved = await db.resolve_api_keys_for_job(user_id=None, project_id=None)
        assert resolved == {"groq": "valid"}


# ---------------------------------------------------------------------------
# system_llm_endpoints
# ---------------------------------------------------------------------------


class TestSystemLlmEndpoints:
    @pytest.mark.asyncio
    async def test_create_encrypts_api_key(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": UUID("33333333-3333-3333-3333-333333333333"),
                "label": "Shared vLLM",
                "base_url": "http://vllm/v1",
                "key_prefix": "vllm-tok",
                "created_at": None,
                "updated_at": None,
            }
        )
        db = _make_db(conn)

        await db.create_system_llm_endpoint(
            label="Shared vLLM",
            base_url="http://vllm/v1",
            api_key="vllm-token-abc",
            key_prefix="vllm-tok",
        )

        # Args: SQL, label, base_url, encrypted_key, key_prefix
        args = conn.fetchrow.await_args.args
        assert args[1] == "Shared vLLM"
        assert args[2] == "http://vllm/v1"
        assert is_encrypted(args[3])
        assert decrypt(args[3]) == "vllm-token-abc"

    @pytest.mark.asyncio
    async def test_create_allows_null_api_key(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": UUID("33333333-3333-3333-3333-333333333334"),
                "label": "Open vLLM",
                "base_url": "http://open-vllm/v1",
                "key_prefix": None,
                "created_at": None,
                "updated_at": None,
            }
        )
        db = _make_db(conn)

        await db.create_system_llm_endpoint(
            label="Open vLLM",
            base_url="http://open-vllm/v1",
            api_key=None,
            key_prefix=None,
        )
        # The encrypted-key slot must be NULL (not an encrypted empty string).
        assert conn.fetchrow.await_args.args[3] is None

    @pytest.mark.asyncio
    async def test_get_returns_decrypted_api_key(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": UUID("33333333-3333-3333-3333-333333333333"),
                "label": "X",
                "base_url": "http://x/v1",
                "api_key": encrypt("secret-xyz"),
                "key_prefix": "secret-x",
                "created_at": None,
                "updated_at": None,
            }
        )
        db = _make_db(conn)

        result = await db.get_system_llm_endpoint(
            "33333333-3333-3333-3333-333333333333"
        )
        assert result is not None
        assert result["api_key"] == "secret-xyz"

    @pytest.mark.asyncio
    async def test_get_none_when_user_scoped(self):
        """Query uses `user_id IS NULL` — a user-scoped row never matches."""
        conn = _conn()
        conn.fetchrow = AsyncMock(return_value=None)
        db = _make_db(conn)
        assert (
            await db.get_system_llm_endpoint("33333333-3333-3333-3333-333333333333")
            is None
        )

    @pytest.mark.asyncio
    async def test_update_encrypts_new_key(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": UUID("33333333-3333-3333-3333-333333333333"),
                "label": "X",
                "base_url": "http://x/v1",
                "key_prefix": "new-pref",
                "created_at": None,
                "updated_at": None,
            }
        )
        db = _make_db(conn)

        await db.update_system_llm_endpoint(
            endpoint_id="33333333-3333-3333-3333-333333333333",
            api_key="new-secret",
            key_prefix="new-pref",
        )

        args = conn.fetchrow.await_args.args
        # api_key is argv[2] (after endpoint_id + SQL) — find it by scanning.
        sql = args[0]
        # Look up the encrypted arg among remaining args.
        cipher_args = [a for a in args[1:] if isinstance(a, str) and is_encrypted(a)]
        assert cipher_args, f"no encrypted arg in {args}"
        assert decrypt(cipher_args[0]) == "new-secret"
        assert "api_key" in sql

    @pytest.mark.asyncio
    async def test_update_clear_api_key(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": UUID("33333333-3333-3333-3333-333333333333"),
                "label": "X",
                "base_url": "http://x/v1",
                "key_prefix": None,
                "created_at": None,
                "updated_at": None,
            }
        )
        db = _make_db(conn)

        await db.update_system_llm_endpoint(
            endpoint_id="33333333-3333-3333-3333-333333333333",
            clear_api_key=True,
        )
        sql = conn.fetchrow.await_args.args[0]
        assert "api_key = NULL" in sql
        assert "key_prefix = NULL" in sql

    @pytest.mark.asyncio
    async def test_delete_filters_on_null_user(self):
        conn = _conn()
        conn.execute = AsyncMock(return_value="DELETE 1")
        db = _make_db(conn)

        ok = await db.delete_system_llm_endpoint("33333333-3333-3333-3333-333333333333")
        assert ok is True
        assert "user_id IS NULL" in conn.execute.await_args.args[0]


# Endpoint-model accessors (create/update/delete/resolve) and the
# user_llm_endpoint_models table were retired when the admin-curated
# `models` catalog became the single source of truth. Catalog accessor
# tests live in tests/test_admin_models_api.py.


# ---------------------------------------------------------------------------
# default LLM model helpers (thin wrapper over system_settings)
# ---------------------------------------------------------------------------


class TestDefaultLlmModelHelpers:
    @pytest.mark.asyncio
    async def test_get_returns_model_from_dict_value(self):
        db = PostgresDB.__new__(PostgresDB)
        db.get_system_setting = AsyncMock(
            return_value={
                "key": "llm.default_builder_model",
                "value": {"model": "gpt-4o"},
            }
        )
        assert await db.get_default_llm_model("builder") == "gpt-4o"
        db.get_system_setting.assert_awaited_once_with("llm.default_builder_model")

    @pytest.mark.asyncio
    async def test_get_returns_none_when_unset(self):
        db = PostgresDB.__new__(PostgresDB)
        db.get_system_setting = AsyncMock(return_value=None)
        assert await db.get_default_llm_model("browser") is None

    @pytest.mark.asyncio
    async def test_get_returns_none_when_model_missing(self):
        db = PostgresDB.__new__(PostgresDB)
        db.get_system_setting = AsyncMock(return_value={"value": {}})
        assert await db.get_default_llm_model("citation") is None

    @pytest.mark.asyncio
    async def test_set_delegates_to_upsert_with_dict_value(self):
        db = PostgresDB.__new__(PostgresDB)
        db.upsert_system_setting = AsyncMock()
        db.delete_system_setting = AsyncMock()

        await db.set_default_llm_model("builder", "gpt-4o", updated_by="admin-1")
        db.upsert_system_setting.assert_awaited_once_with(
            "llm.default_builder_model", {"model": "gpt-4o"}, updated_by="admin-1"
        )
        db.delete_system_setting.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_none_deletes_setting(self):
        db = PostgresDB.__new__(PostgresDB)
        db.upsert_system_setting = AsyncMock()
        db.delete_system_setting = AsyncMock()

        await db.set_default_llm_model("browser", None)
        db.delete_system_setting.assert_awaited_once_with("llm.default_browser_model")
        db.upsert_system_setting.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_empty_string_deletes_setting(self):
        db = PostgresDB.__new__(PostgresDB)
        db.upsert_system_setting = AsyncMock()
        db.delete_system_setting = AsyncMock()

        await db.set_default_llm_model("citation", "")
        db.delete_system_setting.assert_awaited_once_with("llm.default_citation_model")
