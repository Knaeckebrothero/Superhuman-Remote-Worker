"""Tests for the stage-4 system LLM config seeder.

The seeder is the glue between a helm-rendered YAML payload and the
``system_api_keys`` / ``llm_endpoints`` tables. These tests drive it
against a fake ``PostgresDB`` so we can assert idempotence (re-runs are
no-ops) without standing up Postgres.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from orchestrator.seed.llm_config import (
    SEEDED_FROM_TAG,
    load_payload,
    seed,
)


def _fake_db(
    *,
    existing_api_keys: list[dict] | None = None,
    existing_endpoints: list[dict] | None = None,
    existing_catalog_keys: set[tuple[str, str]] | None = None,
):
    """Build a ``PostgresDB``-shaped mock that tracks mutations.

    ``existing_catalog_keys`` is a set of (provider_ref, model_id) pairs
    that simulate already-present catalog rows: ``create_model`` returns
    None for those (matching the ``ON CONFLICT DO NOTHING`` path).
    """
    db = MagicMock()
    db.list_system_api_keys = AsyncMock(return_value=list(existing_api_keys or []))
    db.list_system_llm_endpoints = AsyncMock(
        return_value=[dict(e) for e in (existing_endpoints or [])]
    )
    db.upsert_system_api_key = AsyncMock()

    async def _create_endpoint(*, label, base_url, api_key, key_prefix):
        new_id = f"endpoint-{label}"
        return {
            "id": new_id,
            "label": label,
            "base_url": base_url,
            "key_prefix": key_prefix,
        }

    catalog_keys = set(existing_catalog_keys or set())

    async def _create_model(**kwargs):
        key = (kwargs["provider_ref"], kwargs["model_id"])
        if key in catalog_keys:
            return None
        catalog_keys.add(key)
        return {
            "id": f"catalog-{kwargs['model_id']}",
            "provider_ref": kwargs["provider_ref"],
            "model_id": kwargs["model_id"],
        }

    db.create_system_llm_endpoint = AsyncMock(side_effect=_create_endpoint)
    db.create_model = AsyncMock(side_effect=_create_model)
    return db


# ---------------------------------------------------------------------------
# load_payload
# ---------------------------------------------------------------------------


class TestLoadPayload:
    def test_missing_file_is_empty_dict(self, tmp_path):
        assert load_payload(tmp_path / "does-not-exist.yaml") == {}

    def test_empty_file_is_empty_dict(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        assert load_payload(p) == {}

    def test_top_level_must_be_mapping(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("- just\n- a\n- list\n")
        with pytest.raises(ValueError):
            load_payload(p)

    def test_roundtrip(self, tmp_path):
        p = tmp_path / "ok.yaml"
        payload = {
            "systemApiKeys": [{"provider": "openai", "apiKey": "sk-xxx"}],
            "systemEndpoints": [],
        }
        p.write_text(yaml.safe_dump(payload))
        assert load_payload(p) == payload


# ---------------------------------------------------------------------------
# seed — API keys
# ---------------------------------------------------------------------------


class TestSeedApiKeys:
    @pytest.mark.asyncio
    async def test_inserts_missing_provider(self):
        db = _fake_db()
        payload = {
            "systemApiKeys": [
                {"provider": "openai", "apiKey": "sk-plaintext", "label": "Main"}
            ]
        }
        report = await seed(db, payload)

        db.upsert_system_api_key.assert_awaited_once()
        kwargs = db.upsert_system_api_key.await_args.kwargs
        assert kwargs["provider"] == "openai"
        assert kwargs["api_key"] == "sk-plaintext"
        assert kwargs["key_prefix"] == "sk-plain"
        assert kwargs["label"] == "Main"
        assert kwargs["seeded_from"] == SEEDED_FROM_TAG
        assert report.api_keys_seeded == ["openai"]
        assert report.api_keys_skipped == []

    @pytest.mark.asyncio
    async def test_skips_existing_provider(self):
        db = _fake_db(existing_api_keys=[{"provider": "openai"}])
        payload = {
            "systemApiKeys": [
                {"provider": "openai", "apiKey": "sk-new-plaintext"},
            ]
        }
        report = await seed(db, payload)

        db.upsert_system_api_key.assert_not_awaited()
        assert report.api_keys_skipped == ["openai"]
        assert report.api_keys_seeded == []

    @pytest.mark.asyncio
    async def test_skips_entry_without_provider_or_key(self):
        db = _fake_db()
        payload = {
            "systemApiKeys": [
                {"apiKey": "sk-no-provider"},
                {"provider": "openai"},
                {"provider": "anthropic", "apiKey": ""},
            ]
        }
        report = await seed(db, payload)
        db.upsert_system_api_key.assert_not_awaited()
        assert report.api_keys_seeded == []

    @pytest.mark.asyncio
    async def test_accepts_snake_case_api_key_alias(self):
        db = _fake_db()
        payload = {
            "systemApiKeys": [{"provider": "groq", "api_key": "gsk-abc"}],
        }
        await seed(db, payload)
        db.upsert_system_api_key.assert_awaited_once()
        assert db.upsert_system_api_key.await_args.kwargs["api_key"] == "gsk-abc"

    @pytest.mark.asyncio
    async def test_resolves_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("SEED_OPENAI_KEY", "sk-from-env")
        db = _fake_db()
        payload = {
            "systemApiKeys": [{"provider": "openai", "apiKeyEnv": "SEED_OPENAI_KEY"}],
        }
        await seed(db, payload)
        db.upsert_system_api_key.assert_awaited_once()
        assert db.upsert_system_api_key.await_args.kwargs["api_key"] == "sk-from-env"

    @pytest.mark.asyncio
    async def test_missing_env_var_skips_entry(self, monkeypatch):
        monkeypatch.delenv("SEED_MISSING_KEY", raising=False)
        db = _fake_db()
        payload = {
            "systemApiKeys": [{"provider": "openai", "apiKeyEnv": "SEED_MISSING_KEY"}],
        }
        report = await seed(db, payload)
        db.upsert_system_api_key.assert_not_awaited()
        assert report.api_keys_seeded == []


# ---------------------------------------------------------------------------
# seed — endpoints + models
# ---------------------------------------------------------------------------


class TestSeedEndpoints:
    @pytest.mark.asyncio
    async def test_creates_endpoint_and_models(self):
        db = _fake_db()
        payload = {
            "systemEndpoints": [
                {
                    "label": "Local Gemma",
                    "baseUrl": "http://vllm.svc/v1",
                    "models": [
                        {
                            "id": "RedHatAI/gemma-4-31B-it-FP8-Dynamic",
                            "displayName": "Gemma 4 31B",
                            "family": "gemma",
                            "contextWindow": 128000,
                        }
                    ],
                }
            ]
        }
        report = await seed(db, payload)

        db.create_system_llm_endpoint.assert_awaited_once_with(
            label="Local Gemma",
            base_url="http://vllm.svc/v1",
            api_key=None,
            key_prefix=None,
        )
        # Model entries become catalog rows now (provider_kind='endpoint').
        db.create_model.assert_awaited_once()
        kwargs = db.create_model.await_args.kwargs
        assert kwargs["provider_kind"] == "endpoint"
        assert kwargs["model_id"] == "RedHatAI/gemma-4-31B-it-FP8-Dynamic"
        assert kwargs["display_label"] == "Gemma 4 31B"
        assert kwargs["family"] == "gemma"
        assert kwargs["context_window"] == 128000
        assert kwargs["role"] == "chat"
        assert kwargs["seeded_from"] == "helm:llm.seed"
        assert kwargs["on_conflict_do_nothing"] is True
        assert report.endpoints_seeded == ["Local Gemma"]
        assert report.models_seeded == [
            ("Local Gemma", "RedHatAI/gemma-4-31B-it-FP8-Dynamic")
        ]

    @pytest.mark.asyncio
    async def test_endpoint_with_api_key_captures_prefix(self):
        db = _fake_db()
        payload = {
            "systemEndpoints": [
                {
                    "label": "Keyed",
                    "baseUrl": "https://example/v1",
                    "apiKey": "abcd1234efgh5678",
                    "models": [],
                }
            ]
        }
        await seed(db, payload)
        kwargs = db.create_system_llm_endpoint.await_args.kwargs
        assert kwargs["api_key"] == "abcd1234efgh5678"
        assert kwargs["key_prefix"] == "abcd1234"

    @pytest.mark.asyncio
    async def test_existing_endpoint_left_alone_but_missing_models_added(self):
        existing_endpoint = [
            {
                "id": "ep-1",
                "label": "Local Gemma",
                "base_url": "http://vllm.svc/v1",
                "models": [],
            }
        ]
        # Pre-seed the catalog with one row — create_model returns None for it
        # (matching the ON CONFLICT DO NOTHING behavior).
        db = _fake_db(
            existing_endpoints=existing_endpoint,
            existing_catalog_keys={("ep-1", "RedHatAI/gemma-4-31B-it-FP8-Dynamic")},
        )
        payload = {
            "systemEndpoints": [
                {
                    "label": "Local Gemma",
                    "baseUrl": "http://vllm.svc/v1",
                    "models": [
                        {"id": "RedHatAI/gemma-4-31B-it-FP8-Dynamic"},  # existing
                        {"id": "RedHatAI/gemma-4-9B-it"},  # new
                    ],
                }
            ]
        }
        report = await seed(db, payload)

        db.create_system_llm_endpoint.assert_not_awaited()
        # Both attempted; first returns None (already in catalog), second inserts.
        assert db.create_model.await_count == 2
        assert report.endpoints_skipped == ["Local Gemma"]
        assert report.models_seeded == [("Local Gemma", "RedHatAI/gemma-4-9B-it")]
        assert report.models_skipped == [
            ("Local Gemma", "RedHatAI/gemma-4-31B-it-FP8-Dynamic")
        ]

    @pytest.mark.asyncio
    async def test_skips_endpoint_missing_required_fields(self):
        db = _fake_db()
        payload = {
            "systemEndpoints": [
                {"baseUrl": "http://x/v1", "models": []},  # no label
                {"label": "NoUrl", "models": []},  # no baseUrl
            ]
        }
        await seed(db, payload)
        db.create_system_llm_endpoint.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_model_without_id(self):
        db = _fake_db()
        payload = {
            "systemEndpoints": [
                {
                    "label": "Local",
                    "baseUrl": "http://x/v1",
                    "models": [{"displayName": "orphan"}],
                }
            ]
        }
        report = await seed(db, payload)
        db.create_system_llm_endpoint.assert_awaited_once()
        db.create_model.assert_not_awaited()
        assert report.endpoints_seeded == ["Local"]


# ---------------------------------------------------------------------------
# Full-payload idempotence
# ---------------------------------------------------------------------------


class TestSeedIdempotence:
    @pytest.mark.asyncio
    async def test_second_run_is_noop(self):
        """Every insert from run #1 appears in the existing-state for run #2."""
        payload = {
            "systemApiKeys": [{"provider": "openai", "apiKey": "sk-xxx"}],
            "systemEndpoints": [
                {
                    "label": "Local",
                    "baseUrl": "http://x/v1",
                    "models": [{"id": "m1"}],
                }
            ],
        }

        # Run 1: empty DB.
        db1 = _fake_db()
        report1 = await seed(db1, payload)
        assert report1.api_keys_seeded == ["openai"]
        assert report1.endpoints_seeded == ["Local"]
        assert report1.models_seeded == [("Local", "m1")]

        # Run 2: state reflects run 1 (catalog row already present).
        db2 = _fake_db(
            existing_api_keys=[{"provider": "openai"}],
            existing_endpoints=[
                {
                    "id": "ep-1",
                    "label": "Local",
                    "base_url": "http://x/v1",
                    "models": [],
                }
            ],
            existing_catalog_keys={("ep-1", "m1")},
        )
        report2 = await seed(db2, payload)
        db2.upsert_system_api_key.assert_not_awaited()
        db2.create_system_llm_endpoint.assert_not_awaited()
        # create_model is still called but returns None (already present).
        assert db2.create_model.await_count == 1
        assert report2.api_keys_skipped == ["openai"]
        assert report2.endpoints_skipped == ["Local"]
        assert report2.models_skipped == [("Local", "m1")]

    @pytest.mark.asyncio
    async def test_empty_payload_is_safe(self):
        db = _fake_db()
        report = await seed(db, {})
        db.upsert_system_api_key.assert_not_awaited()
        db.create_system_llm_endpoint.assert_not_awaited()
        assert report.api_keys_seeded == []
        assert report.endpoints_seeded == []

    @pytest.mark.asyncio
    async def test_rejects_non_list_sections(self):
        db = _fake_db()
        with pytest.raises(ValueError):
            await seed(db, {"systemApiKeys": {"openai": "sk"}})
