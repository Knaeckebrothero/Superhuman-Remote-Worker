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

from orchestrator.schemas.provider_catalog import VALID_DEFAULT_MODEL_KINDS
from orchestrator.seed.llm_config import (
    DEFAULT_PIN_CAPABILITY_BY_KIND,
    SEEDED_FROM_TAG,
    load_payload,
    seed,
)
from shared.subscription_routing import SUBSCRIPTION_PROXY_TRANSPORT


def _fake_db(
    *,
    existing_api_keys: list[dict] | None = None,
    existing_endpoints: list[dict] | None = None,
    existing_catalog_keys: set[tuple[str, str]] | None = None,
    catalog_rows: list[dict] | None = None,
    existing_defaults: dict[str, str] | None = None,
):
    """Build a ``PostgresDB``-shaped mock that tracks mutations.

    ``existing_catalog_keys`` is a set of (provider_ref, model_id) pairs
    that simulate already-present catalog rows: ``create_model`` returns
    None for those (matching the ``ON CONFLICT DO NOTHING`` path).

    ``catalog_rows`` are pre-existing rows as ``list_models`` returns them
    (``model_id`` + ``capabilities`` + ``enabled``); rows ``create_model``
    inserts during the run are appended, so the defaults section sees what
    the same payload just seeded. ``existing_defaults`` pre-populates the
    ``llm.default_<kind>_model`` pins.
    """
    db = MagicMock()
    db.list_system_api_keys = AsyncMock(return_value=list(existing_api_keys or []))
    db.list_system_llm_endpoints = AsyncMock(
        return_value=[dict(e) for e in (existing_endpoints or [])]
    )
    db.upsert_system_api_key = AsyncMock()

    async def _create_endpoint(
        *, label, base_url, api_key, key_prefix, transport_kind=None
    ):
        new_id = f"endpoint-{label}"
        return {
            "id": new_id,
            "label": label,
            "base_url": base_url,
            "key_prefix": key_prefix,
            "transport_kind": transport_kind,
        }

    catalog_keys = set(existing_catalog_keys or set())

    rows = [dict(r) for r in (catalog_rows or [])]

    async def _create_model(**kwargs):
        key = (kwargs["provider_ref"], kwargs["model_id"])
        if key in catalog_keys:
            return None
        catalog_keys.add(key)
        row = {
            "id": f"catalog-{kwargs['model_id']}",
            "provider_ref": kwargs["provider_ref"],
            "model_id": kwargs["model_id"],
            "capabilities": list(kwargs.get("capabilities") or []),
            "enabled": kwargs.get("enabled", True),
        }
        rows.append(row)
        return row

    async def _list_models(
        *, capabilities=None, provider_kind=None, provider_ref=None, enabled_only=False
    ):
        out = []
        for row in rows:
            if capabilities and not (set(capabilities) & set(row["capabilities"])):
                continue
            if enabled_only and not row.get("enabled", True):
                continue
            out.append(dict(row))
        return out

    pins = dict(existing_defaults or {})

    async def _get_default(kind):
        return pins.get(kind)

    async def _set_default(kind, model, *, updated_by=None):
        if model:
            pins[kind] = model
        else:
            pins.pop(kind, None)

    db.create_system_llm_endpoint = AsyncMock(side_effect=_create_endpoint)
    db.create_model = AsyncMock(side_effect=_create_model)
    db.list_models = AsyncMock(side_effect=_list_models)
    db.get_default_llm_model = AsyncMock(side_effect=_get_default)
    db.set_default_llm_model = AsyncMock(side_effect=_set_default)
    db._pins = pins
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
            transport_kind=None,
        )
        # Model entries become catalog rows now (provider_kind='endpoint').
        # The seed pipeline always emits the array spelling — passing the
        # singular form is allowed at the helm-values layer for ergonomics
        # but the seeder canonicalizes before calling create_model so the
        # accessor sees the array.
        db.create_model.assert_awaited_once()
        kwargs = db.create_model.await_args.kwargs
        assert kwargs["provider_kind"] == "endpoint"
        assert kwargs["model_id"] == "RedHatAI/gemma-4-31B-it-FP8-Dynamic"
        assert kwargs["display_label"] == "Gemma 4 31B"
        assert kwargs["family"] == "gemma"
        assert kwargs["context_window"] == 128000
        # No explicit `capability` in the helm entry → defaults to 'chat'
        # which auto-expands to ['chat', 'auxiliary'].
        assert kwargs["capabilities"] == ["chat", "auxiliary"]
        assert kwargs["seeded_from"] == "helm:llm.seed"
        assert kwargs["on_conflict_do_nothing"] is True
        assert report.endpoints_seeded == ["Local Gemma"]
        assert report.models_seeded == [
            ("Local Gemma", "RedHatAI/gemma-4-31B-it-FP8-Dynamic")
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport_field", ["transportKind", "transport_kind"])
    async def test_transport_marker_reuses_new_proxy_under_another_label(
        self, transport_field
    ):
        db = _fake_db()
        payload = {
            "systemEndpoints": [
                {
                    "label": label,
                    "baseUrl": "http://proxy.svc/v1",
                    transport_field: SUBSCRIPTION_PROXY_TRANSPORT,
                    "models": [{"id": model_id}],
                }
                for label, model_id in [("Primary", "m1"), ("Alias", "m2")]
            ]
        }

        report = await seed(db, payload)

        db.create_system_llm_endpoint.assert_awaited_once_with(
            label="Primary",
            base_url="http://proxy.svc/v1",
            api_key=None,
            key_prefix=None,
            transport_kind=SUBSCRIPTION_PROXY_TRANSPORT,
        )
        db.update_system_llm_endpoint.assert_not_called()
        assert report.endpoints_seeded == ["Primary"]
        assert report.endpoints_skipped == ["Primary"]
        assert [
            call.kwargs["provider_ref"] for call in db.create_model.await_args_list
        ] == ["endpoint-Primary", "endpoint-Primary"]
        assert report.models_seeded == [("Primary", "m1"), ("Alias", "m2")]

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


# ---------------------------------------------------------------------------
# Capabilities-array seed semantics
# ---------------------------------------------------------------------------


class TestCapabilitiesArraySemantics:
    """Pin the helm-values shape contract introduced in chunk 2 of the
    model_capabilities_array work."""

    @pytest.mark.asyncio
    async def test_explicit_capabilities_array_passed_through(self):
        """`capabilities: [chat, vision]` (explicit array) lands as-is — no
        auto-expansion to chat+auxiliary. Operator gets exact control."""
        payload = {
            "systemModels": [
                {
                    "provider": "openai",
                    "id": "gpt-4-vision",
                    "displayName": "GPT-4 Vision",
                    "capabilities": ["chat", "vision"],
                    "family": "gpt-4o",
                }
            ],
        }
        # API key is pre-seeded so _seed_system_models accepts the entry.
        db = _fake_db(existing_api_keys=[{"provider": "openai"}])
        await seed(db, payload)
        assert db.create_model.await_args.kwargs["capabilities"] == [
            "chat",
            "vision",
        ]

    @pytest.mark.asyncio
    async def test_multimodal_flag_adds_vision_to_chat_row(self):
        """`multimodal: true` on a chat-capable row adds `vision` to the
        capabilities[] array. Lets operators flag known multimodal models
        without writing the full array."""
        payload = {
            "systemModels": [
                {
                    "provider": "openai",
                    "id": "gpt-4o",
                    "displayName": "GPT-4o",
                    "capability": "chat",
                    "multimodal": True,
                    "family": "gpt-4o",
                }
            ],
        }
        db = _fake_db(existing_api_keys=[{"provider": "openai"}])
        await seed(db, payload)
        assert db.create_model.await_args.kwargs["capabilities"] == [
            "chat",
            "auxiliary",
            "vision",
        ]

    @pytest.mark.asyncio
    async def test_multimodal_flag_no_op_on_non_chat_row(self):
        """`multimodal: true` only affects chat-capable rows. Embedding,
        whisper, tts entries with the flag still land as singletons."""
        payload = {
            "systemModels": [
                {
                    "provider": "openai",
                    "id": "text-embedding-3-large",
                    "displayName": "Embedding",
                    "capability": "embedding",
                    "multimodal": True,
                    "family": "openai-embedding",
                }
            ],
        }
        db = _fake_db(existing_api_keys=[{"provider": "openai"}])
        await seed(db, payload)
        assert db.create_model.await_args.kwargs["capabilities"] == ["embedding"]

    @pytest.mark.asyncio
    async def test_rerank_capability_seeds_as_a_singleton(self):
        # `capability: rerank` is the memory reranker's own slot (migration
        # 0240): no chat/auxiliary expansion, never filed under `embedding`.
        payload = {
            "systemModels": [
                {
                    "provider": "openai",
                    "id": "qwen3-reranker-8b",
                    "displayName": "Reranker",
                    "capability": "rerank",
                    "family": "qwen",
                }
            ],
        }
        db = _fake_db(existing_api_keys=[{"provider": "openai"}])
        await seed(db, payload)
        assert db.create_model.await_args.kwargs["capabilities"] == ["rerank"]

    @pytest.mark.asyncio
    async def test_duplicate_provider_model_aggregates_capabilities(self):
        """Two helm entries pointing at the same (provider, model_id) collapse
        into a single insert with the union of capabilities[]. Required because
        the new UNIQUE (provider_kind, provider_ref, model_id) key would otherwise
        reject the second entry under ON CONFLICT DO NOTHING — losing data."""
        payload = {
            "systemModels": [
                {
                    "provider": "openai",
                    "id": "gpt-4o",
                    "displayName": "GPT-4o",
                    "capability": "chat",
                    "family": "gpt-4o",
                },
                {
                    "provider": "openai",
                    "id": "gpt-4o",
                    "displayName": "GPT-4o (Vision)",
                    "capability": "vision",
                    "family": "gpt-4o",
                },
            ],
        }
        db = _fake_db(existing_api_keys=[{"provider": "openai"}])
        await seed(db, payload)
        # ONE insert, capabilities is the union.
        assert db.create_model.await_count == 1
        kwargs = db.create_model.await_args.kwargs
        assert kwargs["capabilities"] == ["chat", "auxiliary", "vision"]
        # First entry's display_label wins (operator metadata precedence).
        assert kwargs["display_label"] == "GPT-4o"

    @pytest.mark.asyncio
    async def test_endpoint_models_aggregate_per_endpoint(self):
        """Same aggregation contract applies inside a single endpoint's
        models[] list — duplicates collapse."""
        payload = {
            "systemEndpoints": [
                {
                    "label": "vLLM",
                    "baseUrl": "http://vllm/v1",
                    "models": [
                        {"id": "gemma", "capability": "chat"},
                        {"id": "gemma", "capability": "vision"},
                    ],
                }
            ]
        }
        db = _fake_db()
        await seed(db, payload)
        assert db.create_model.await_count == 1
        assert db.create_model.await_args.kwargs["capabilities"] == [
            "chat",
            "auxiliary",
            "vision",
        ]


# ---------------------------------------------------------------------------
# seed — default-model pins
# ---------------------------------------------------------------------------


def _chat_row(model_id: str, *, enabled: bool = True, caps=("chat", "auxiliary")):
    return {"model_id": model_id, "capabilities": list(caps), "enabled": enabled}


class TestSeedDefaults:
    def test_kind_map_matches_admin_schema(self):
        # Admin → Models → Defaults and the seed must accept the same kinds;
        # a kind added to one side without the other is a drift bug.
        assert set(DEFAULT_PIN_CAPABILITY_BY_KIND) == VALID_DEFAULT_MODEL_KINDS

    @pytest.mark.asyncio
    async def test_pins_absent_kind_to_catalog_model(self):
        db = _fake_db(catalog_rows=[_chat_row("gpt-5-mini")])
        report = await seed(db, {"defaults": {"chat": "gpt-5-mini"}})

        db.set_default_llm_model.assert_awaited_once_with(
            "chat", "gpt-5-mini", updated_by=SEEDED_FROM_TAG
        )
        assert report.defaults_seeded == [("chat", "gpt-5-mini")]
        assert report.defaults_skipped == []
        assert db._pins == {"chat": "gpt-5-mini"}

    @pytest.mark.asyncio
    async def test_existing_pin_is_left_alone(self):
        db = _fake_db(
            catalog_rows=[_chat_row("gpt-5-mini"), _chat_row("MiniMax-M3")],
            existing_defaults={"chat": "MiniMax-M3"},
        )
        report = await seed(db, {"defaults": {"chat": "gpt-5-mini"}})

        db.set_default_llm_model.assert_not_awaited()
        assert report.defaults_seeded == []
        assert report.defaults_skipped == [("chat", "MiniMax-M3")]
        assert db._pins == {"chat": "MiniMax-M3"}

    @pytest.mark.asyncio
    async def test_model_missing_from_catalog_is_skipped_with_warning(self, caplog):
        db = _fake_db(catalog_rows=[_chat_row("gpt-5-mini")])
        with caplog.at_level("WARNING", logger="orchestrator.seed.llm_config"):
            report = await seed(db, {"defaults": {"chat": "gpt-5-minl"}})

        db.set_default_llm_model.assert_not_awaited()
        assert report.defaults_skipped == [("chat", "gpt-5-minl")]
        assert "gpt-5-minl" in caplog.text
        assert "not an enabled catalog row" in caplog.text

    @pytest.mark.asyncio
    async def test_model_without_the_kinds_capability_is_skipped(self):
        # A chat-only row cannot be the embedding default.
        db = _fake_db(catalog_rows=[_chat_row("gpt-5-mini", caps=("chat",))])
        report = await seed(db, {"defaults": {"embedding": "gpt-5-mini"}})

        db.set_default_llm_model.assert_not_awaited()
        assert report.defaults_skipped == [("embedding", "gpt-5-mini")]

    @pytest.mark.asyncio
    async def test_disabled_catalog_row_is_skipped(self):
        db = _fake_db(catalog_rows=[_chat_row("gpt-5-mini", enabled=False)])
        report = await seed(db, {"defaults": {"chat": "gpt-5-mini"}})

        db.set_default_llm_model.assert_not_awaited()
        assert report.defaults_skipped == [("chat", "gpt-5-mini")]

    @pytest.mark.asyncio
    async def test_browser_and_citation_validate_against_chat(self):
        db = _fake_db(catalog_rows=[_chat_row("gpt-5-mini", caps=("chat",))])
        report = await seed(
            db, {"defaults": {"browser": "gpt-5-mini", "citation": "gpt-5-mini"}}
        )
        assert sorted(report.defaults_seeded) == [
            ("browser", "gpt-5-mini"),
            ("citation", "gpt-5-mini"),
        ]

    @pytest.mark.asyncio
    async def test_search_fallback_validates_against_search(self):
        db = _fake_db(
            catalog_rows=[{"model_id": "searxng", "capabilities": ["search"]}]
        )
        report = await seed(db, {"defaults": {"search_fallback": "searxng"}})
        assert report.defaults_seeded == [("search_fallback", "searxng")]

    @pytest.mark.asyncio
    async def test_unknown_kind_is_skipped_with_warning(self, caplog):
        db = _fake_db(catalog_rows=[_chat_row("gpt-5-mini")])
        with caplog.at_level("WARNING", logger="orchestrator.seed.llm_config"):
            report = await seed(db, {"defaults": {"chatt": "gpt-5-mini"}})

        db.set_default_llm_model.assert_not_awaited()
        assert report.defaults_skipped == [("chatt", "gpt-5-mini")]
        assert "unknown kind" in caplog.text

    @pytest.mark.asyncio
    async def test_empty_or_null_model_is_ignored(self):
        db = _fake_db(catalog_rows=[_chat_row("gpt-5-mini")])
        report = await seed(db, {"defaults": {"chat": "", "auxiliary": None}})

        db.set_default_llm_model.assert_not_awaited()
        db.get_default_llm_model.assert_not_awaited()
        assert report.defaults_seeded == []
        assert report.defaults_skipped == []

    @pytest.mark.asyncio
    async def test_accepts_mapping_form(self):
        db = _fake_db(catalog_rows=[_chat_row("gpt-5-mini")])
        report = await seed(db, {"defaults": {"chat": {"model": " gpt-5-mini "}}})
        assert report.defaults_seeded == [("chat", "gpt-5-mini")]

    @pytest.mark.asyncio
    async def test_rows_seeded_in_the_same_payload_are_pinnable(self):
        db = _fake_db(existing_api_keys=[{"provider": "openai"}])
        payload = {
            "systemModels": [
                {
                    "provider": "openai",
                    "id": "gpt-5-mini",
                    "capability": "chat",
                    "family": "gpt-5",
                }
            ],
            "defaults": {"chat": "gpt-5-mini", "auxiliary": "gpt-5-mini"},
        }
        report = await seed(db, payload)

        assert report.models_seeded == [("openai", "gpt-5-mini")]
        assert sorted(report.defaults_seeded) == [
            ("auxiliary", "gpt-5-mini"),
            ("chat", "gpt-5-mini"),
        ]

    @pytest.mark.asyncio
    async def test_second_run_is_noop(self):
        db = _fake_db(catalog_rows=[_chat_row("gpt-5-mini")])
        payload = {"defaults": {"chat": "gpt-5-mini"}}
        first = await seed(db, payload)
        second = await seed(db, payload)

        assert first.defaults_seeded == [("chat", "gpt-5-mini")]
        assert second.defaults_seeded == []
        assert second.defaults_skipped == [("chat", "gpt-5-mini")]
        assert db.set_default_llm_model.await_count == 1

    @pytest.mark.asyncio
    async def test_rejects_non_mapping_defaults(self):
        db = _fake_db()
        with pytest.raises(ValueError):
            await seed(db, {"defaults": ["chat"]})
