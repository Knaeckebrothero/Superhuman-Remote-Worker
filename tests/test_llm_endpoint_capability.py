"""Tests for the capability-tagged LLM endpoint extension.

Covers the four additions that ship together with the ``capability``
column on ``user_llm_endpoint_models``:

1. Resolver: ``resolve_user_llm_model`` / ``resolve_system_llm_model``
   filter by capability so the same ``model_id`` can back distinct slots
   (e.g., GPT-4o as both chat and vision) without non-deterministic row
   selection.
2. Batch insert: ``batch_create_endpoint_models`` handles skip_duplicates
   against the composite ``(endpoint_id, model_id, capability)`` uniqueness.
3. Discovery probe: ``probe_endpoint_models`` parses ``GET /v1/models``
   and tags each model with a capability_hint (embed → embedding, vision →
   vision, whisper → whisper, fallback chat).
4. Route registration: the new ``/discover`` and ``/models:batch`` routes
   are wired on both the user-scoped and admin-scoped surfaces.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import httpx
import pytest

from orchestrator.database.postgres import PostgresDB
from orchestrator.services.llm_endpoint_probe import (
    ProbeResult,
    _capability_hint,
    probe_endpoint_models,
)

# The API-route smoke-tests need main.py importable.
_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))
os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")


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


# ---------------------------------------------------------------------------
# Resolver: capability is part of the WHERE clause
# ---------------------------------------------------------------------------


class TestResolverCapabilityFilter:
    @pytest.mark.asyncio
    async def test_system_resolver_passes_capability_arg(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        db = _make_db(conn)

        await db.resolve_system_llm_model("gpt-4o", capability="vision")

        sql, *args = conn.fetchrow.await_args.args
        assert "capability = $2" in sql
        assert args == ["gpt-4o", "vision"]

    @pytest.mark.asyncio
    async def test_system_resolver_defaults_to_chat(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        db = _make_db(conn)

        await db.resolve_system_llm_model("gpt-4o")

        _sql, *args = conn.fetchrow.await_args.args
        assert args[-1] == "chat"

    @pytest.mark.asyncio
    async def test_user_resolver_passes_capability_arg(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        db = _make_db(conn)

        uid = "11111111-1111-1111-1111-111111111111"
        await db.resolve_user_llm_model(uid, "qwen-embed", capability="embedding")

        sql, *args = conn.fetchrow.await_args.args
        assert "capability = $3" in sql
        assert args == [UUID(uid), "qwen-embed", "embedding"]

    @pytest.mark.asyncio
    async def test_resolver_returns_capability_in_result(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "endpoint_id": UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                "endpoint_label": "System vLLM",
                "base_url": "http://vllm/v1",
                "api_key": None,
                "model_id": "gpt-4o",
                "display_name": "GPT-4o",
                "family": "gpt-4o",
                "context_window": 128000,
                "reasoning_level": None,
                "capability": "vision",
                "enabled": True,
            }
        )
        db = _make_db(conn)

        result = await db.resolve_system_llm_model("gpt-4o", capability="vision")
        assert result is not None
        assert result["capability"] == "vision"


# ---------------------------------------------------------------------------
# Batch create: duplicate detection is (model_id, capability), not just model_id
# ---------------------------------------------------------------------------


class TestBatchCreateEndpointModels:
    @pytest.mark.asyncio
    async def test_skip_duplicates_filters_by_composite_key(self):
        conn = AsyncMock()
        # Pre-existing rows: gpt-4o as chat AND as vision.
        conn.fetch = AsyncMock(
            return_value=[
                {"model_id": "gpt-4o", "capability": "chat"},
                {"model_id": "gpt-4o", "capability": "vision"},
            ]
        )
        # fetchval returns a fresh UUID per new insert.
        uuids = iter(
            [
                UUID("bbbbbbbb-0000-0000-0000-000000000001"),
                UUID("bbbbbbbb-0000-0000-0000-000000000002"),
            ]
        )
        conn.fetchval = AsyncMock(side_effect=lambda *a, **kw: next(uuids))

        # transaction() returns an async-ctx mgr that's a no-op for the mock.
        @asynccontextmanager
        async def _txn():
            yield

        conn.transaction = MagicMock(return_value=_txn())
        db = _make_db(conn)

        result = await db.batch_create_endpoint_models(
            endpoint_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            models=[
                # Duplicate against existing chat row — skipped.
                {"model_id": "gpt-4o", "display_name": "GPT-4o", "capability": "chat"},
                # gpt-4o as embedding — *not* a duplicate of chat/vision rows, inserted.
                {
                    "model_id": "gpt-4o",
                    "display_name": "GPT-4o Embedding",
                    "capability": "embedding",
                },
                # Brand-new chat model — inserted.
                {
                    "model_id": "claude-opus-4-6",
                    "display_name": "Claude Opus",
                },
            ],
            skip_duplicates=True,
        )

        assert result["created"] == 2
        assert result["skipped"] == ["gpt-4o"]
        assert len(result["created_ids"]) == 2

    @pytest.mark.asyncio
    async def test_skip_duplicates_false_skips_preload(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock()  # should NOT be called
        uuids = iter([UUID("bbbbbbbb-0000-0000-0000-000000000003")])
        conn.fetchval = AsyncMock(side_effect=lambda *a, **kw: next(uuids))

        @asynccontextmanager
        async def _txn():
            yield

        conn.transaction = MagicMock(return_value=_txn())
        db = _make_db(conn)

        await db.batch_create_endpoint_models(
            endpoint_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            models=[{"model_id": "gpt-4o", "display_name": "GPT-4o"}],
            skip_duplicates=False,
        )

        # skip_duplicates=False means no SELECT to build the existing-set.
        conn.fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_within_batch_duplicates_are_caught(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        uuids = iter([UUID("bbbbbbbb-0000-0000-0000-000000000004")])
        conn.fetchval = AsyncMock(side_effect=lambda *a, **kw: next(uuids))

        @asynccontextmanager
        async def _txn():
            yield

        conn.transaction = MagicMock(return_value=_txn())
        db = _make_db(conn)

        result = await db.batch_create_endpoint_models(
            endpoint_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            models=[
                {"model_id": "gpt-4o", "display_name": "GPT-4o", "capability": "chat"},
                {
                    "model_id": "gpt-4o",
                    "display_name": "GPT-4o dup",
                    "capability": "chat",
                },
            ],
            skip_duplicates=True,
        )

        assert result["created"] == 1
        assert result["skipped"] == ["gpt-4o"]


# ---------------------------------------------------------------------------
# Discovery probe: capability_hint heuristic + response parsing
# ---------------------------------------------------------------------------


class TestCapabilityHint:
    @pytest.mark.parametrize(
        "model_id, expected",
        [
            ("text-embedding-3-large", "embedding"),
            ("qwen3-embedding-8b", "embedding"),
            ("bge-reranker-v2", "embedding"),  # rerank routed through embedding bucket
            ("whisper-1", "whisper"),
            ("distil-whisper-large", "whisper"),
            ("gpt-4o-vision-preview", "vision"),
            ("qwen2-vl-7b-instruct", "vision"),
            ("llama-3.2-multimodal", "vision"),
            ("gpt-4o", "chat"),
            ("claude-opus-4-6", "chat"),
            ("RedHatAI/gemma-4-31B-it-FP8-Dynamic", "chat"),
        ],
    )
    def test_hint_rules(self, model_id: str, expected: str):
        assert _capability_hint(model_id) == expected


class TestProbeEndpointModels:
    @pytest.mark.asyncio
    async def test_openai_format_response_parses(self, monkeypatch):
        class _FakeResp:
            status_code = 200
            text = '{"data":[...]}'  # unused; json() wins

            def json(self) -> dict:
                return {
                    "object": "list",
                    "data": [
                        {"id": "gpt-4o", "owned_by": "openai"},
                        {"id": "text-embedding-3-large", "owned_by": "openai"},
                        {"id": "whisper-1", "owned_by": "openai"},
                    ],
                }

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url, headers=None):
                return _FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await probe_endpoint_models(
            base_url="https://api.openai.com/v1", api_key="sk-test"
        )
        assert result.ok is True
        assert result.status == 200
        ids = [m["id"] for m in result.models]
        assert ids == ["gpt-4o", "text-embedding-3-large", "whisper-1"]
        hints = {m["id"]: m["capability_hint"] for m in result.models}
        assert hints == {
            "gpt-4o": "chat",
            "text-embedding-3-large": "embedding",
            "whisper-1": "whisper",
        }

    @pytest.mark.asyncio
    async def test_transport_error_returns_structured_failure(self, monkeypatch):
        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url, headers=None):
                raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await probe_endpoint_models(
            base_url="http://does-not-exist:9999/v1", api_key=None
        )
        assert result.ok is False
        assert result.status is None
        assert result.error is not None
        assert "ConnectError" in result.error
        assert result.models == []

    @pytest.mark.asyncio
    async def test_non_2xx_status_surfaces_body_preview(self, monkeypatch):
        class _FakeResp:
            status_code = 401
            text = '{"error":"invalid api key"}'

            def json(self):
                raise AssertionError("json() should not be called on 4xx")

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url, headers=None):
                return _FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        result = await probe_endpoint_models(
            base_url="https://api.openai.com/v1", api_key="bad"
        )
        assert result.ok is False
        assert result.status == 401
        assert "invalid api key" in (result.error or "")

    @pytest.mark.asyncio
    async def test_bearer_header_is_attached_when_key_present(self, monkeypatch):
        captured = {}

        class _FakeResp:
            status_code = 200
            text = '{"data":[]}'

            def json(self):
                return {"data": []}

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, url, headers=None):
                captured["headers"] = headers or {}
                return _FakeResp()

        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

        await probe_endpoint_models(
            base_url="https://api.openai.com/v1", api_key="sk-live-xxx"
        )
        assert captured["headers"].get("Authorization") == "Bearer sk-live-xxx"


# ---------------------------------------------------------------------------
# Route registration (the new discover + batch endpoints landed on both surfaces)
# ---------------------------------------------------------------------------


class TestRoutesRegistered:
    def test_new_routes_are_wired(self):
        from main import app

        routes = {
            (next(iter(r.methods)), r.path) for r in app.routes if hasattr(r, "methods")
        }
        assert ("POST", "/api/settings/llm-endpoints/{endpoint_id}/discover") in routes
        assert (
            "POST",
            "/api/settings/llm-endpoints/{endpoint_id}/models:batch",
        ) in routes
        assert (
            "POST",
            "/api/admin/providers/endpoints/{endpoint_id}/discover",
        ) in routes
        assert (
            "POST",
            "/api/admin/providers/endpoints/{endpoint_id}/models:batch",
        ) in routes

    def test_valid_default_model_kinds_extended(self):
        from main import VALID_DEFAULT_MODEL_KINDS

        assert {"embedding", "vision", "auxiliary"}.issubset(VALID_DEFAULT_MODEL_KINDS)

    def test_probe_result_shape(self):
        r = ProbeResult(ok=True, status=200, error=None, probe_url="u")
        assert r.models == []
