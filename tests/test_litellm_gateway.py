"""Unit tests for the LiteLLM gateway catalog-sync (orchestrator side).

Covers the pure reconcile logic + the catalog→desired-model mapping without a
live gateway: a mock postgres_db supplies endpoint models, a mock LiteLLMClient
records the admin calls. See docs/features/usage_monitoring_and_rate_limiting.md.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from orchestrator.services.litellm_gateway import (
    OWNED_ID_PREFIX,
    _needs_replace,
    _rev_for_endpoint,
    build_desired_models,
    sync_catalog_to_gateway,
)

_EP_ID = "f475b8e1-6839-4e54-a366-f1dfa692e4d4"
_UPDATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_REV = int(_UPDATED.timestamp())


def _endpoint(base_url="https://ai.h4ll.app/v1", api_key="sk-home", updated=_UPDATED):
    return {
        "id": _EP_ID,
        "base_url": base_url,
        "api_key": api_key,
        "updated_at": updated,
    }


def _model_row(catalog_id, model_id, endpoint_id=_EP_ID):
    return {"id": catalog_id, "provider_ref": endpoint_id, "model_id": model_id}


def _mock_db(model_rows, endpoint=None):
    db = AsyncMock()
    db.list_models.return_value = model_rows
    db.get_user_llm_endpoint.return_value = endpoint if endpoint is not None else _endpoint()
    return db


class TestRevForEndpoint:
    def test_uses_updated_at_timestamp(self):
        assert _rev_for_endpoint(_endpoint()) == _REV

    def test_missing_updated_at_is_zero(self):
        assert _rev_for_endpoint({"base_url": "x"}) == 0

    def test_bad_updated_at_is_zero(self):
        assert _rev_for_endpoint({"updated_at": "not-a-datetime"}) == 0


class TestBuildDesiredModels:
    @pytest.mark.asyncio
    async def test_endpoint_model_maps_to_openai_deployment(self):
        db = _mock_db([_model_row("cat-1", "gemma-4-moe-strix")])
        desired = await build_desired_models(db)

        # Slice 1 only asks for endpoint-kind, enabled models.
        db.list_models.assert_awaited_once_with(
            provider_kind="endpoint", enabled_only=True
        )
        assert list(desired) == [f"{OWNED_ID_PREFIX}cat-1"]
        spec = desired[f"{OWNED_ID_PREFIX}cat-1"]
        assert spec["model_name"] == "gemma-4-moe-strix"
        # openai/ prefix → OpenAI-compatible upstream; model_name stays the bare id.
        assert spec["litellm_params"]["model"] == "openai/gemma-4-moe-strix"
        assert spec["litellm_params"]["api_base"] == "https://ai.h4ll.app/v1"
        assert spec["litellm_params"]["api_key"] == "sk-home"
        assert spec["model_info"]["id"] == f"{OWNED_ID_PREFIX}cat-1"
        assert spec["model_info"]["srw_rev"] == _REV
        assert spec["model_info"]["srw_managed"] is True

    @pytest.mark.asyncio
    async def test_endpoint_resolved_once_for_many_models(self):
        db = _mock_db(
            [
                _model_row("cat-1", "gemma-4-moe-strix"),
                _model_row("cat-2", "qwen3-embedding-8b-strix"),
                _model_row("cat-3", "whisper-large-v3-strix"),
            ]
        )
        desired = await build_desired_models(db)
        assert len(desired) == 3
        # All share one endpoint → resolved + decrypted exactly once.
        db.get_user_llm_endpoint.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_keyless_endpoint_omits_api_key(self):
        db = _mock_db(
            [_model_row("cat-1", "local-model")], endpoint=_endpoint(api_key=None)
        )
        desired = await build_desired_models(db)
        assert "api_key" not in desired[f"{OWNED_ID_PREFIX}cat-1"]["litellm_params"]

    @pytest.mark.asyncio
    async def test_endpoint_without_base_url_skipped(self):
        db = _mock_db(
            [_model_row("cat-1", "broken")], endpoint=_endpoint(base_url=None)
        )
        assert await build_desired_models(db) == {}

    @pytest.mark.asyncio
    async def test_no_models_returns_empty(self):
        db = _mock_db([])
        assert await build_desired_models(db) == {}


class TestNeedsReplace:
    def _spec(self, model="openai/m", api_base="https://a/v1", rev=_REV):
        return {
            "litellm_params": {"model": model, "api_base": api_base},
            "model_info": {"srw_rev": rev},
        }

    def test_identical_no_replace(self):
        assert _needs_replace(self._spec(), self._spec()) is False

    def test_api_base_change_triggers_replace(self):
        assert _needs_replace(self._spec(api_base="https://old/v1"), self._spec()) is True

    def test_model_change_triggers_replace(self):
        assert _needs_replace(self._spec(model="openai/old"), self._spec()) is True

    def test_rev_change_triggers_replace(self):
        # Key rotation bumps updated_at → rev → replace, even though the masked
        # api_key never surfaces on read-back.
        assert _needs_replace(self._spec(rev=1), self._spec(rev=2)) is True

    def test_missing_current_rev_does_not_thrash(self):
        current = {"litellm_params": {"model": "openai/m", "api_base": "https://a/v1"}}
        assert _needs_replace(current, self._spec()) is False


class TestSyncCatalogToGateway:
    @pytest.mark.asyncio
    async def test_adds_missing_models(self):
        db = _mock_db([_model_row("cat-1", "gemma-4-moe-strix")])
        client = AsyncMock()
        client.list_managed_models.return_value = {}

        counts = await sync_catalog_to_gateway(db, client)

        client.add_model.assert_awaited_once()
        client.delete_model.assert_not_awaited()
        assert counts == {"added": 1, "replaced": 0, "deleted": 0, "managed": 1}

    @pytest.mark.asyncio
    async def test_deletes_stale_owned_models(self):
        db = _mock_db([])  # catalog now empty
        client = AsyncMock()
        client.list_managed_models.return_value = {
            f"{OWNED_ID_PREFIX}gone": {
                "model_name": "old",
                "litellm_params": {},
                "model_info": {"id": f"{OWNED_ID_PREFIX}gone"},
            }
        }

        counts = await sync_catalog_to_gateway(db, client)

        client.delete_model.assert_awaited_once_with(f"{OWNED_ID_PREFIX}gone")
        client.add_model.assert_not_awaited()
        assert counts["deleted"] == 1

    @pytest.mark.asyncio
    async def test_replaces_drifted_model(self):
        db = _mock_db([_model_row("cat-1", "gemma-4-moe-strix")])
        client = AsyncMock()
        # Same id present but pointing at a stale base_url → delete + re-add.
        client.list_managed_models.return_value = {
            f"{OWNED_ID_PREFIX}cat-1": {
                "model_name": "gemma-4-moe-strix",
                "litellm_params": {
                    "model": "openai/gemma-4-moe-strix",
                    "api_base": "https://OLD.example/v1",
                },
                "model_info": {"id": f"{OWNED_ID_PREFIX}cat-1", "srw_rev": _REV},
            }
        }

        counts = await sync_catalog_to_gateway(db, client)

        client.delete_model.assert_awaited_once_with(f"{OWNED_ID_PREFIX}cat-1")
        client.add_model.assert_awaited_once()
        assert counts == {"added": 0, "replaced": 1, "deleted": 0, "managed": 1}

    @pytest.mark.asyncio
    async def test_noop_when_in_sync(self):
        db = _mock_db([_model_row("cat-1", "gemma-4-moe-strix")])
        client = AsyncMock()
        client.list_managed_models.return_value = {
            f"{OWNED_ID_PREFIX}cat-1": {
                "model_name": "gemma-4-moe-strix",
                "litellm_params": {
                    "model": "openai/gemma-4-moe-strix",
                    "api_base": "https://ai.h4ll.app/v1",
                },
                "model_info": {"id": f"{OWNED_ID_PREFIX}cat-1", "srw_rev": _REV},
            }
        }

        counts = await sync_catalog_to_gateway(db, client)

        client.add_model.assert_not_awaited()
        client.delete_model.assert_not_awaited()
        assert counts == {"added": 0, "replaced": 0, "deleted": 0, "managed": 1}

    @pytest.mark.asyncio
    async def test_leaves_unowned_models_untouched(self):
        db = _mock_db([])
        client = AsyncMock()
        # A model an operator added by hand (no srw- prefix) — list_managed_models
        # filters it out, so the reconcile never sees or deletes it.
        client.list_managed_models.return_value = {}

        counts = await sync_catalog_to_gateway(db, client)

        client.delete_model.assert_not_awaited()
        assert counts["deleted"] == 0
