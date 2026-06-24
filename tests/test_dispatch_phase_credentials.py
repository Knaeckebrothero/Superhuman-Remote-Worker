"""Regression test for `_inject_dispatch_credentials` phase-override handling.

Covers the 2026-05-12 bug where a job's `config_override` of the shape
`{"llm": {"tactical": {"model": "X"}, "strategic": {"model": "X"}}}`
walked through dispatch with no `base_url`/`api_key` injection on the
phase blocks. The agent's LLM factory then fell back to the parent's
`base_url` and returned `404 Model 'X' not found` in an infinite retry
loop.

See docs/issues/orchestrator_phase_override_credentials_not_injected.md
for the incident write-up.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Add orchestrator/ to sys.path so its top-level modules import bare.
_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

import main  # noqa: E402
from src.core.model_registry import ModelMeta  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _job(*, job_id: str = "00000000-0000-0000-0000-000000000001") -> dict:
    """Minimal job dict for the dispatch path."""
    return {
        "id": job_id,
        "user_id": "00000000-0000-0000-0000-0000000000aa",
        "project_id": "00000000-0000-0000-0000-0000000000bb",
    }


CODEX_ENDPOINT_ID = "11111111-1111-1111-1111-111111111111"
CODEX_BASE_URL = "http://srw-codex-proxy:8317/v1"
CODEX_API_KEY = "sk-codex-test"


@pytest.fixture
def patched_main(monkeypatch):
    """Patch the DB + registry collaborators of `_inject_dispatch_credentials`
    so the test exercises only the injection branching.

    `_resolve_model` is wired to a side_effect that returns a real
    `ModelMeta` for `gpt-5.3-codex-spark` (origin=custom, endpoint pointing
    at the codex proxy) and `None` for everything else — mirroring the
    failing job's registry state.
    """

    async def fake_resolve(model_id, user_id=None, capability="chat"):
        if model_id == "gpt-5.3-codex-spark":
            return ModelMeta(
                model_id="gpt-5.3-codex-spark",
                provider="openai",
                family="codex",
                display_name="GPT-5.3 Codex Spark",
                origin="custom",
                endpoint_id=CODEX_ENDPOINT_ID,
                api_key_ref="openai",
            )
        return None

    monkeypatch.setattr(
        main, "_resolve_model", AsyncMock(side_effect=fake_resolve), raising=True
    )

    async def fake_get_endpoint(endpoint_id):
        if endpoint_id == CODEX_ENDPOINT_ID:
            return {
                "id": CODEX_ENDPOINT_ID,
                "label": "codex-proxy",
                "base_url": CODEX_BASE_URL,
                "api_key": CODEX_API_KEY,
            }
        return None

    monkeypatch.setattr(
        main.postgres_db,
        "get_user_llm_endpoint",
        AsyncMock(side_effect=fake_get_endpoint),
    )
    monkeypatch.setattr(
        main.postgres_db,
        "resolve_api_keys_for_job",
        AsyncMock(return_value={}),
    )
    # No user-default fallback — the incident scenario is purely
    # job-override-driven.
    monkeypatch.setattr(
        main.postgres_db,
        "get_user_settings",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        main.postgres_db,
        "resolve_default_for_capability",
        AsyncMock(return_value=None),
    )


# ---------------------------------------------------------------------------
# Regression tests
# ---------------------------------------------------------------------------


class TestPhaseOverrideCredentialInjection:
    @pytest.mark.asyncio
    async def test_tactical_and_strategic_overrides_get_endpoint_injected(
        self, patched_main
    ):
        """The exact failing config_override from the 2026-05-12 incident."""
        override = {
            "llm": {
                "tactical": {"model": "gpt-5.3-codex-spark"},
                "strategic": {"model": "gpt-5.3-codex-spark"},
            }
        }

        result = await main._inject_dispatch_credentials(_job(), override)

        assert result["llm"]["tactical"]["base_url"] == CODEX_BASE_URL
        assert result["llm"]["tactical"]["api_key"] == CODEX_API_KEY
        assert result["llm"]["strategic"]["base_url"] == CODEX_BASE_URL
        assert result["llm"]["strategic"]["api_key"] == CODEX_API_KEY

    @pytest.mark.asyncio
    async def test_blob_delivery_injects_phase_pin_transport(self, patched_main):
        """eec20eeb regression (blob-delivery path).

        ``serialize_resolved_config`` emits the phase blocks with explicit
        ``base_url: None`` / ``provider: None`` leaves. ``inject_blob_credentials``
        only stripped None at the TOP level of ``llm``, so the nested phase
        leaves survived and defeated ``_inject_model_credentials``'s
        ``setdefault`` (a present-but-None key is not overwritten). Codex phase
        pins therefore shipped without transport and the agent fell back to
        api.openai.com → 401. The base model worked (its top-level None WAS
        stripped), masking the gap. This exercises the REAL resolve_config blob
        through the REAL injector — the path jobs actually use.
        """
        from orchestrator.services.config_resolver import (
            inject_blob_credentials,
            resolve_config,
        )

        blob = resolve_config(
            base_config_name="defaults",
            request_override={
                "llm": {
                    "strategic": {"model": "gpt-5.3-codex-spark"},
                    "tactical": {"model": "gpt-5.3-codex-spark"},
                }
            },
            expert_type="worker",
        )
        # Precondition: serialize emits the None transport leaves that trigger
        # the bug (guards against the fixture silently changing shape).
        assert "base_url" in blob["agent"]["llm"]["strategic"]
        assert blob["agent"]["llm"]["strategic"]["base_url"] is None

        delivered = await inject_blob_credentials(
            blob, lambda co: main._inject_dispatch_credentials(_job(), co)
        )

        llm = delivered["agent"]["llm"]
        assert llm["strategic"]["base_url"] == CODEX_BASE_URL
        assert llm["strategic"]["api_key"] == CODEX_API_KEY
        assert llm["tactical"]["base_url"] == CODEX_BASE_URL
        assert llm["tactical"]["api_key"] == CODEX_API_KEY

    @pytest.mark.asyncio
    async def test_auxiliary_override_also_injected(self, patched_main):
        """Same hole existed for top-level `auxiliary` overrides."""
        override = {"auxiliary": {"model": "gpt-5.3-codex-spark"}}

        result = await main._inject_dispatch_credentials(_job(), override)

        assert result["auxiliary"]["base_url"] == CODEX_BASE_URL
        assert result["auxiliary"]["api_key"] == CODEX_API_KEY

    @pytest.mark.asyncio
    async def test_existing_base_url_in_phase_block_is_not_overwritten(
        self, patched_main
    ):
        """Caller-supplied base_url wins; injection is additive-only."""
        override = {
            "llm": {
                "tactical": {
                    "model": "gpt-5.3-codex-spark",
                    "base_url": "https://caller-pinned.example/v1",
                }
            }
        }

        result = await main._inject_dispatch_credentials(_job(), override)

        assert (
            result["llm"]["tactical"]["base_url"] == "https://caller-pinned.example/v1"
        )
        # The guard skips injection entirely when base_url is already set,
        # so api_key from the endpoint is NOT injected either — matching
        # the pre-existing _inject_model_credentials early-return contract.
        assert "api_key" not in result["llm"]["tactical"]

    @pytest.mark.asyncio
    async def test_unknown_phase_model_logs_warning_no_crash(
        self, patched_main, caplog
    ):
        """A phase pin for a model the registry doesn't know must not crash —
        it should log a warning and leave the section unmodified for the
        downstream agent to surface a clear error."""
        override = {"llm": {"tactical": {"model": "does-not-exist"}}}

        with caplog.at_level("WARNING", logger=main.logger.name):
            result = await main._inject_dispatch_credentials(_job(), override)

        assert result["llm"]["tactical"]["model"] == "does-not-exist"
        assert "base_url" not in result["llm"]["tactical"]
        assert any(
            "does-not-exist" in rec.message and "tactical" in rec.message
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_empty_override_is_a_noop(self, patched_main):
        """No phase sections → no injection, no crashes."""
        result = await main._inject_dispatch_credentials(_job(), {})

        assert "tactical" not in result.get("llm", {})
        assert "strategic" not in result.get("llm", {})
        assert "auxiliary" not in result


# ---------------------------------------------------------------------------
# System-anchored provider routing (provider_kind='system')
#
# A catalog row anchored to a system_api_keys provider (e.g. OpenRouter)
# carries NO endpoint base_url — `_catalog_row_to_meta` leaves base_url=None
# and only sets api_key_ref. The dispatcher used to inject just the api_key,
# so the agent's create_llm fell back to the OpenAI factory default
# (api.openai.com) and rejected the OpenRouter `sk-or-v1…` key with a 401
# from platform.openai.com. The fix injects `meta.provider` (the factory
# name) so OpenRouter rows route through _create_openrouter_llm → openrouter.ai.
# ---------------------------------------------------------------------------

OR_KEY = "sk-or-v1-test00000000000000000000000000000000000000000000fc51"


@pytest.fixture
def patched_main_openrouter(monkeypatch):
    """Registry returns a system-anchored OpenRouter chat row (no endpoint,
    base_url=None, api_key_ref='openrouter', provider='openrouter') and the
    job resolves an OpenRouter system key."""

    async def fake_resolve(model_id, user_id=None, capability="chat"):
        if model_id == "minimax/minimax-m3":
            return ModelMeta(
                model_id="minimax/minimax-m3",
                provider="openrouter",  # _factory_provider('openrouter')
                family="minimax-m3",
                display_name="MiniMax M3",
                base_url=None,  # system rows carry no endpoint base_url
                api_key_ref="openrouter",  # resolves system_api_keys['openrouter']
                origin="catalog",
                endpoint_id=None,
            )
        return None

    monkeypatch.setattr(
        main, "_resolve_model", AsyncMock(side_effect=fake_resolve), raising=True
    )
    monkeypatch.setattr(
        main.postgres_db,
        "get_user_llm_endpoint",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        main.postgres_db,
        "resolve_api_keys_for_job",
        AsyncMock(return_value={"openrouter": OR_KEY}),
    )
    monkeypatch.setattr(
        main.postgres_db,
        "get_user_settings",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(
        main.postgres_db,
        "resolve_default_for_capability",
        AsyncMock(return_value=None),
    )


class TestSystemProviderRouting:
    @pytest.mark.asyncio
    async def test_main_llm_openrouter_row_injects_provider_and_key(
        self, patched_main_openrouter
    ):
        """The exact M3 incident: a system OpenRouter chat model must carry
        provider='openrouter' so the agent routes to openrouter.ai, not the
        OpenAI factory default."""
        override = {"llm": {"model": "minimax/minimax-m3"}}

        result = await main._inject_dispatch_credentials(_job(), override)

        assert result["llm"]["provider"] == "openrouter"
        assert result["llm"]["api_key"] == OR_KEY

    @pytest.mark.asyncio
    async def test_phase_override_openrouter_row_injects_provider(
        self, patched_main_openrouter
    ):
        """Phase overrides go through _inject_model_credentials — same fix
        must reach them."""
        override = {"llm": {"tactical": {"model": "minimax/minimax-m3"}}}

        result = await main._inject_dispatch_credentials(_job(), override)

        assert result["llm"]["tactical"]["provider"] == "openrouter"
        assert result["llm"]["tactical"]["api_key"] == OR_KEY

    @pytest.mark.asyncio
    async def test_caller_pinned_provider_is_not_overwritten(
        self, patched_main_openrouter
    ):
        """Injection is additive (setdefault) — an explicit provider wins."""
        override = {"llm": {"model": "minimax/minimax-m3", "provider": "openai"}}

        result = await main._inject_dispatch_credentials(_job(), override)

        assert result["llm"]["provider"] == "openai"


# ---------------------------------------------------------------------------
# Per-model context window injection
#
# A catalog/endpoint row's `context_window` drives the agent's working window:
# it is injected into the section's `model_max_context_tokens` (a flat llm key),
# survives the agent-side settings-matrix re-apply, and becomes the base for the
# derived limits. The worker top-level llm gets it via the inline block; phase
# sections (strategic/tactical, capability="chat") get it via
# `_inject_model_credentials`. Auxiliary sections (capability="auxiliary") must
# NOT — their window is not derived this way.
# ---------------------------------------------------------------------------

CTX_ENDPOINT_ID = "22222222-2222-2222-2222-222222222222"
CTX_BASE_URL = "http://self-hosted:8000/v1"
CTX_API_KEY = "sk-ctx-test"


@pytest.fixture
def patched_main_ctx(monkeypatch):
    """`_resolve_model` returns endpoint-backed metas whose `context_window`
    varies by model_id: 32000, None, or 0 (the explicit-zero signal)."""

    windows = {"ctx-32k": 32000, "ctx-none": None, "ctx-zero": 0}

    async def fake_resolve(model_id, user_id=None, capability="chat"):
        if model_id in windows:
            return ModelMeta(
                model_id=model_id,
                provider="openai",
                family="default",
                display_name=model_id,
                origin="custom",
                endpoint_id=CTX_ENDPOINT_ID,
                api_key_ref="openai",
                context_window=windows[model_id],
            )
        return None

    monkeypatch.setattr(
        main, "_resolve_model", AsyncMock(side_effect=fake_resolve), raising=True
    )

    async def fake_get_endpoint(endpoint_id):
        if endpoint_id == CTX_ENDPOINT_ID:
            return {
                "id": CTX_ENDPOINT_ID,
                "label": "self-hosted",
                "base_url": CTX_BASE_URL,
                "api_key": CTX_API_KEY,
            }
        return None

    monkeypatch.setattr(
        main.postgres_db,
        "get_user_llm_endpoint",
        AsyncMock(side_effect=fake_get_endpoint),
    )
    monkeypatch.setattr(
        main.postgres_db, "resolve_api_keys_for_job", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        main.postgres_db, "get_user_settings", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        main.postgres_db,
        "resolve_default_for_capability",
        AsyncMock(return_value=None),
    )


class TestContextWindowInjection:
    @pytest.mark.asyncio
    async def test_top_level_window_injected(self, patched_main_ctx):
        """A per-model context_window lands on the top-level llm section."""
        result = await main._inject_dispatch_credentials(
            _job(), {"llm": {"model": "ctx-32k"}}
        )
        assert result["llm"]["model_max_context_tokens"] == 32000
        # Routing creds still injected alongside.
        assert result["llm"]["base_url"] == CTX_BASE_URL

    @pytest.mark.asyncio
    async def test_none_window_not_injected(self, patched_main_ctx):
        """No context_window → the key is absent (agent falls back to family)."""
        result = await main._inject_dispatch_credentials(
            _job(), {"llm": {"model": "ctx-none"}}
        )
        assert "model_max_context_tokens" not in result["llm"]

    @pytest.mark.asyncio
    async def test_zero_window_not_injected(self, patched_main_ctx):
        """Explicit 0 is rejected by the truthy guard (Pydantic round-trips 0)."""
        result = await main._inject_dispatch_credentials(
            _job(), {"llm": {"model": "ctx-zero"}}
        )
        assert "model_max_context_tokens" not in result["llm"]

    @pytest.mark.asyncio
    async def test_caller_pinned_window_wins(self, patched_main_ctx):
        """Injection is additive (setdefault) — an explicit window wins."""
        result = await main._inject_dispatch_credentials(
            _job(), {"llm": {"model": "ctx-32k", "model_max_context_tokens": 64000}}
        )
        assert result["llm"]["model_max_context_tokens"] == 64000

    @pytest.mark.asyncio
    async def test_chat_phase_section_gets_window(self, patched_main_ctx):
        """Strategic/tactical phase pins (capability='chat') get the window."""
        result = await main._inject_dispatch_credentials(
            _job(), {"llm": {"tactical": {"model": "ctx-32k"}}}
        )
        assert result["llm"]["tactical"]["model_max_context_tokens"] == 32000

    @pytest.mark.asyncio
    async def test_auxiliary_section_does_not_get_window(self, patched_main_ctx):
        """Capability gating: auxiliary sections are not context-window-derived,
        but still get their routing creds."""
        result = await main._inject_dispatch_credentials(
            _job(), {"auxiliary": {"model": "ctx-32k"}}
        )
        assert result["auxiliary"]["base_url"] == CTX_BASE_URL
        assert "model_max_context_tokens" not in result["auxiliary"]


# ---------------------------------------------------------------------------
# Codex proxy bypasses the LiteLLM gateway (reasoning-capture regression)
#
# The Codex proxy speaks ONLY the Responses API; routing it through the LiteLLM
# gateway normalizes to Chat Completions and drops gpt-5.x reasoning. So an
# endpoint model whose meta.provider == "codex" must hit the endpoint directly
# even when the gateway is enabled, while non-codex endpoint models still route
# through the gateway.
# See docs/done/litellm_gateway_drops_gpt_codex_reasoning_capture.md
# ---------------------------------------------------------------------------

GATEWAY = ("http://srw-litellm:4000/v1", "sk-fleet")


def _patch_resolve_provider(monkeypatch, *, provider: str):
    async def fake_resolve(model_id, user_id=None, capability="chat"):
        return ModelMeta(
            model_id=model_id,
            provider=provider,
            family="gpt-5",
            display_name=model_id,
            origin="catalog",
            endpoint_id=CODEX_ENDPOINT_ID,
        )

    monkeypatch.setattr(
        main, "_resolve_model", AsyncMock(side_effect=fake_resolve), raising=True
    )

    async def fake_get_endpoint(endpoint_id):
        if endpoint_id == CODEX_ENDPOINT_ID:
            return {
                "id": CODEX_ENDPOINT_ID,
                "label": "codex-proxy",
                "base_url": CODEX_BASE_URL,
                "api_key": CODEX_API_KEY,
            }
        return None

    monkeypatch.setattr(
        main.postgres_db,
        "get_user_llm_endpoint",
        AsyncMock(side_effect=fake_get_endpoint),
    )


class TestCodexBypassesGateway:
    """meta.provider == 'codex' skips the gateway and injects the codex factory."""

    @pytest.mark.asyncio
    async def test_codex_model_hits_endpoint_not_gateway(self, monkeypatch):
        _patch_resolve_provider(monkeypatch, provider="codex")
        section = {"model": "gpt-5.5"}
        await main._inject_model_credentials(
            section=section,
            model_id="gpt-5.5",
            user_id="u",
            resolved_keys={},
            gateway_override=GATEWAY,
        )
        # Direct to the codex proxy — NOT the gateway — and built via the codex factory.
        assert section["base_url"] == CODEX_BASE_URL
        assert section["api_key"] == CODEX_API_KEY
        assert section.get("provider") == "codex"

    @pytest.mark.asyncio
    async def test_noncodex_endpoint_still_routes_via_gateway(self, monkeypatch):
        _patch_resolve_provider(monkeypatch, provider="openai")
        section = {"model": "gemma-4-moe"}
        await main._inject_model_credentials(
            section=section,
            model_id="gemma-4-moe",
            user_id="u",
            resolved_keys={},
            gateway_override=GATEWAY,
        )
        # Endpoint + gateway enabled → gateway (measurement/rate-limit chokepoint).
        assert section["base_url"] == GATEWAY[0]
        assert section["api_key"] == GATEWAY[1]


# ---------------------------------------------------------------------------
# Embedding credential reliability (memory + KB).
#
# The embedding key drives memory (RecallStore) + KB (KnowledgeStore). It was
# resolved ONLY inside the `if job.get("user_id")` block, so a job whose user
# had no embedding preference (or no user) silently shipped without a key and
# ran with memory/KB dead and no signal. The fix gives embedding the same
# system-default fallback the chat model has (outside the user gate), stops a
# pre-present EMBEDDING_MODEL from suppressing the key, and refuses to emit a
# half-credential when the endpoint key can't decrypt.
# See docs/issues/embedding_key_missing_silently_disables_memory_and_kb.md
# ---------------------------------------------------------------------------

EMB_ENDPOINT_ID = "33333333-3333-3333-3333-333333333333"
EMB_BASE_URL = "https://ai.h4ll.app/v1"
EMB_API_KEY = "sk-emb-test"
EMB_MODEL = "qwen3-embedding-8b"


def _job_no_user(*, job_id: str = "00000000-0000-0000-0000-000000000002") -> dict:
    """A job with NO user_id — the user-preference dispatch block is skipped."""
    return {"id": job_id, "project_id": "00000000-0000-0000-0000-0000000000bb"}


@pytest.fixture
def patched_main_embedding(monkeypatch):
    """System embedding model `qwen3-embedding-8b` on an endpoint with a key.

    No user settings; `resolve_default_for_capability` returns the embedding
    model only for the "embedding" capability (None elsewhere, so the chat /
    vision / aux fallbacks stay no-ops). Returns a mutable holder so a test can
    simulate a decrypt failure (`holder["api_key"] = None`).
    """
    holder = {"api_key": EMB_API_KEY}

    async def fake_resolve(model_id, user_id=None, capability="chat"):
        if model_id == EMB_MODEL:
            return ModelMeta(
                model_id=EMB_MODEL,
                provider="openai",
                family="default",
                display_name="Qwen3 Embedding 8B",
                origin="system",
                endpoint_id=EMB_ENDPOINT_ID,
                api_key_ref="openai",
            )
        return None

    monkeypatch.setattr(
        main, "_resolve_model", AsyncMock(side_effect=fake_resolve), raising=True
    )

    async def fake_get_endpoint(endpoint_id):
        if endpoint_id == EMB_ENDPOINT_ID:
            return {
                "id": EMB_ENDPOINT_ID,
                "label": "Local Router",
                "base_url": EMB_BASE_URL,
                "api_key": holder["api_key"],
            }
        return None

    monkeypatch.setattr(
        main.postgres_db,
        "get_user_llm_endpoint",
        AsyncMock(side_effect=fake_get_endpoint),
    )
    monkeypatch.setattr(
        main.postgres_db, "resolve_api_keys_for_job", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        main.postgres_db, "get_user_settings", AsyncMock(return_value={})
    )

    async def fake_default(capability):
        return EMB_MODEL if capability == "embedding" else None

    monkeypatch.setattr(
        main.postgres_db,
        "resolve_default_for_capability",
        AsyncMock(side_effect=fake_default),
    )
    return holder


class TestEmbeddingCredentialReliability:
    @pytest.mark.asyncio
    async def test_system_default_injected_without_user(self, patched_main_embedding):
        """No user_id → user block skipped; the system-default fallback still
        injects the embedding endpoint + key (the core asymmetry fix)."""
        result = await main._inject_dispatch_credentials(_job_no_user(), {})
        env = result["env_keys"]
        assert env["EMBEDDING_MODEL"] == EMB_MODEL
        assert env["EMBEDDING_BASE_URL"] == EMB_BASE_URL
        assert env["EMBEDDING_API_KEY"] == EMB_API_KEY

    @pytest.mark.asyncio
    async def test_user_without_embedding_pref_still_gets_key(
        self, patched_main_embedding
    ):
        """A job WITH a user but no embedding preference still resolves the
        system default embedding key."""
        result = await main._inject_dispatch_credentials(_job(), {})
        assert result["env_keys"]["EMBEDDING_API_KEY"] == EMB_API_KEY

    @pytest.mark.asyncio
    async def test_pre_present_model_does_not_suppress_key(
        self, patched_main_embedding
    ):
        """A pre-present EMBEDDING_MODEL must NOT skip _API_KEY injection."""
        result = await main._inject_dispatch_credentials(
            _job_no_user(), {"env_keys": {"EMBEDDING_MODEL": EMB_MODEL}}
        )
        assert result["env_keys"]["EMBEDDING_API_KEY"] == EMB_API_KEY

    @pytest.mark.asyncio
    async def test_preset_api_key_is_not_overwritten(self, patched_main_embedding):
        """A per-job/BYO embedding key already in env_keys wins (additive)."""
        result = await main._inject_dispatch_credentials(
            _job_no_user(), {"env_keys": {"EMBEDDING_API_KEY": "user-byo"}}
        )
        assert result["env_keys"]["EMBEDDING_API_KEY"] == "user-byo"

    @pytest.mark.asyncio
    async def test_decrypt_failure_emits_no_half_credential(
        self, patched_main_embedding, caplog
    ):
        """Endpoint has a base_url but the key didn't decrypt (api_key=None):
        do NOT inject a base_url-without-key half-credential, and log loudly."""
        patched_main_embedding["api_key"] = None
        with caplog.at_level("ERROR", logger=main.logger.name):
            result = await main._inject_dispatch_credentials(_job_no_user(), {})
        env = result.get("env_keys", {})
        assert "EMBEDDING_API_KEY" not in env
        assert "EMBEDDING_BASE_URL" not in env
        assert any(EMB_ENDPOINT_ID in rec.getMessage() for rec in caplog.records)
