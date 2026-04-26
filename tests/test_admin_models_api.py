"""Tests for the Admin → Models API + accessor surface (Phase C).

Covers:
- Every new ``/api/admin/providers/models/*`` route (and the families endpoint)
  is registered on the FastAPI app.
- Pydantic bodies (``CatalogModelCreate``, ``CatalogModelUpdate``) accept the
  expected shapes and reject invalid enums / missing required fields.
- ``params_json={"temperature": 0}`` and ``context_window=0`` round-trip as
  themselves through the DB accessors (LiteLLM #14661 hazard regression).
- ``resolve_catalog_model`` JOINs to the right transport (system vs endpoint)
  and prefers the system row when both are present.
- ``list_models_by_role_alphabetical`` filters on enabled and sorts.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

from main import (  # noqa: E402
    VALID_CATALOG_PROVIDER_KINDS,
    VALID_CATALOG_ROLES,
    CatalogModelCreate,
    CatalogModelUpdate,
    app,
)
from orchestrator.database.postgres import PostgresDB  # noqa: E402


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


CATALOG_ROUTES = {
    ("GET", "/api/admin/providers/models"),
    ("POST", "/api/admin/providers/models"),
    ("PATCH", "/api/admin/providers/models/{catalog_id}"),
    ("DELETE", "/api/admin/providers/models/{catalog_id}"),
    ("POST", "/api/admin/providers/models/{catalog_id}/test"),
    ("GET", "/api/admin/families"),
}


def _registered_routes() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        for m in methods:
            out.add((m, path))
    return out


class TestCatalogRoutesRegistered:
    def test_every_catalog_admin_route_is_wired(self):
        registered = _registered_routes()
        missing = [r for r in CATALOG_ROUTES if r not in registered]
        assert not missing, f"missing catalog admin routes: {missing}"


class TestCatalogConstants:
    def test_role_enum_locked(self):
        assert VALID_CATALOG_ROLES == (
            "chat",
            "auxiliary",
            "embedding",
            "vision",
            "whisper",
            "tts",
        )

    def test_provider_kinds_locked(self):
        assert VALID_CATALOG_PROVIDER_KINDS == ("system", "endpoint")


# ---------------------------------------------------------------------------
# Pydantic bodies
# ---------------------------------------------------------------------------


class TestCatalogModelCreate:
    def _ok_payload(self, **overrides):
        base = {
            "provider_kind": "system",
            "provider_ref": "anthropic",
            "model_id": "claude-opus-4-7",
            "display_label": "Claude Opus 4.7",
            "role": "chat",
            "family": "claude-opus",
        }
        base.update(overrides)
        return base

    def test_minimal_payload_accepted(self):
        body = CatalogModelCreate(**self._ok_payload())
        assert body.provider_kind == "system"
        assert body.enabled is True  # default

    def test_endpoint_kind_accepted(self):
        body = CatalogModelCreate(
            **self._ok_payload(
                provider_kind="endpoint",
                provider_ref="11111111-1111-1111-1111-111111111111",
            )
        )
        assert body.provider_kind == "endpoint"

    def test_invalid_role_rejected(self):
        with pytest.raises(Exception):
            CatalogModelCreate(**self._ok_payload(role="banana"))

    def test_invalid_provider_kind_rejected(self):
        with pytest.raises(Exception):
            CatalogModelCreate(**self._ok_payload(provider_kind="user"))

    def test_required_fields_enforced(self):
        for missing_field in (
            "provider_kind",
            "provider_ref",
            "model_id",
            "display_label",
            "role",
            "family",
        ):
            payload = self._ok_payload()
            payload.pop(missing_field)
            with pytest.raises(Exception):
                CatalogModelCreate(**payload)  # type: ignore[arg-type]

    def test_explicit_zero_context_window_preserved(self):
        body = CatalogModelCreate(**self._ok_payload(context_window=0))
        # Must not be coerced to None — explicit 0 is the override signal.
        assert body.context_window == 0

    def test_explicit_zero_temperature_preserved(self):
        body = CatalogModelCreate(**self._ok_payload(params_json={"temperature": 0}))
        assert body.params_json == {"temperature": 0}


class TestCatalogModelUpdate:
    def test_empty_update_accepted(self):
        # No fields set — caller can patch nothing without error.
        body = CatalogModelUpdate()
        # All Optional fields default to None.
        assert body.enabled is None

    def test_partial_update_only_sets_passed_fields(self):
        body = CatalogModelUpdate(enabled=False)
        as_dict = body.model_dump(exclude_unset=True)
        assert as_dict == {"enabled": False}

    def test_explicit_zero_round_trip(self):
        body = CatalogModelUpdate(context_window=0, params_json={"temperature": 0})
        as_dict = body.model_dump(exclude_unset=True)
        assert as_dict == {"context_window": 0, "params_json": {"temperature": 0}}


# ---------------------------------------------------------------------------
# DB accessors (mirrors test_admin_providers_db.py style)
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


def _row(**overrides):
    base = {
        "id": UUID("11111111-1111-1111-1111-111111111111"),
        "provider_kind": "system",
        "provider_ref": "anthropic",
        "model_id": "claude-opus-4-7",
        "display_label": "Claude Opus 4.7",
        "role": "chat",
        "family": "claude-opus",
        "context_window": None,
        "reasoning_level": None,
        "params_json": None,
        "enabled": True,
        "seeded_from": None,
        "notes": None,
        "created_at": None,
        "updated_at": None,
    }
    base.update(overrides)
    return base


class TestCreateModelJsonbHandling:
    """LiteLLM #14661 hazard — null vs explicit zero must be distinguished."""

    @pytest.mark.asyncio
    async def test_null_params_writes_sql_null(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(return_value=_row())
        db = _make_db(conn)

        await db.create_model(
            provider_kind="system",
            provider_ref="anthropic",
            model_id="claude-opus-4-7",
            display_label="Claude Opus 4.7",
            role="chat",
            family="claude-opus",
            params_json=None,
        )
        params_arg = conn.fetchrow.await_args.args[9]
        assert params_arg is None

    @pytest.mark.asyncio
    async def test_explicit_zero_temperature_serialized_as_json(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(return_value=_row())
        db = _make_db(conn)

        await db.create_model(
            provider_kind="system",
            provider_ref="anthropic",
            model_id="claude-opus-4-7",
            display_label="Claude Opus 4.7",
            role="chat",
            family="claude-opus",
            params_json={"temperature": 0},
        )
        params_arg = conn.fetchrow.await_args.args[9]
        assert params_arg is not None
        # round-trip via JSON to confirm the zero survived
        assert json.loads(params_arg) == {"temperature": 0}

    @pytest.mark.asyncio
    async def test_row_to_model_decodes_jsonb_string(self):
        # asyncpg returns JSONB as a string when the codec isn't registered;
        # _row_to_model must json.loads it transparently.
        row = _row(params_json='{"temperature": 0}')
        result = PostgresDB._row_to_model(row)
        assert result["params_json"] == {"temperature": 0}

    @pytest.mark.asyncio
    async def test_row_to_model_passes_through_dict(self):
        row = _row(params_json={"top_p": 0.9})
        result = PostgresDB._row_to_model(row)
        assert result["params_json"] == {"top_p": 0.9}


class TestListModelsFilters:
    @pytest.mark.asyncio
    async def test_no_filters_emits_bare_select(self):
        conn = _conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db(conn)

        await db.list_models()
        sql = conn.fetch.await_args.args[0]
        assert "WHERE" not in sql

    @pytest.mark.asyncio
    async def test_role_filter_added(self):
        conn = _conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db(conn)

        await db.list_models(role="auxiliary")
        sql = conn.fetch.await_args.args[0]
        args = conn.fetch.await_args.args[1:]
        assert "role = $1" in sql
        assert args == ("auxiliary",)

    @pytest.mark.asyncio
    async def test_enabled_only_adds_clause(self):
        conn = _conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db(conn)

        await db.list_models(enabled_only=True)
        sql = conn.fetch.await_args.args[0]
        assert "enabled = TRUE" in sql


class TestResolveCatalogModelTransportJoin:
    """resolve_catalog_model JOINs to system_api_keys or llm_endpoints
    depending on provider_kind, decrypts the api_key inline, and prefers the
    system row when both are present (ORDER BY provider_kind='system' DESC).
    """

    @pytest.mark.asyncio
    async def test_system_row_returns_decrypted_system_key(self):
        from orchestrator.security.crypto import encrypt

        conn = _conn()
        conn.fetchrow = AsyncMock(
            return_value=_row(
                system_api_key=encrypt("sk-ant-real"),
                endpoint_id=None,
                endpoint_label=None,
                endpoint_base_url=None,
                endpoint_api_key=None,
                catalog_id=UUID("11111111-1111-1111-1111-111111111111"),
            )
        )
        db = _make_db(conn)
        result = await db.resolve_catalog_model("claude-opus-4-7")
        assert result is not None
        assert result["api_key"] == "sk-ant-real"
        assert "system_api_key" not in result
        assert "endpoint_api_key" not in result

    @pytest.mark.asyncio
    async def test_endpoint_row_returns_decrypted_endpoint_key(self):
        from orchestrator.security.crypto import encrypt

        conn = _conn()
        conn.fetchrow = AsyncMock(
            return_value=_row(
                provider_kind="endpoint",
                provider_ref="11111111-1111-1111-1111-111111111111",
                system_api_key=None,
                endpoint_id=UUID("11111111-1111-1111-1111-111111111111"),
                endpoint_label="vLLM",
                endpoint_base_url="http://vllm.svc/v1",
                endpoint_api_key=encrypt("ep-secret"),
                catalog_id=UUID("22222222-2222-2222-2222-222222222222"),
            )
        )
        db = _make_db(conn)
        result = await db.resolve_catalog_model("RedHatAI/gemma")
        assert result is not None
        assert result["api_key"] == "ep-secret"
        assert result["endpoint_base_url"] == "http://vllm.svc/v1"

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(return_value=None)
        db = _make_db(conn)
        assert await db.resolve_catalog_model("nope") is None

    @pytest.mark.asyncio
    async def test_query_orders_system_first(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(return_value=None)
        db = _make_db(conn)
        await db.resolve_catalog_model("x")
        sql = conn.fetchrow.await_args.args[0]
        assert "ORDER BY (m.provider_kind = 'system') DESC" in sql


class TestListByRoleAlphabetical:
    @pytest.mark.asyncio
    async def test_filters_enabled_and_orders_by_label(self):
        conn = _conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db(conn)
        await db.list_models_by_role_alphabetical("auxiliary")
        sql = conn.fetch.await_args.args[0]
        assert "enabled = TRUE" in sql
        assert "ORDER BY display_label ASC" in sql
        assert conn.fetch.await_args.args[1:] == ("auxiliary",)
