"""Tests for the global models API: catalog loading, provider filtering, dispatch injection."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# ---------------------------------------------------------------------------
# Import orchestrator helpers under test
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent / "orchestrator"))

# main.py validates VECTOR_DB_URL at module level; set a dummy value so the
# import succeeds — tests here only exercise pure utility functions.
os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

from main import (
    _detect_provider_from_model,
    _get_system_providers,
    _load_model_catalog,
    _PROVIDER_ENV_KEYS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_catalog_cache():
    """Reset catalog cache between tests."""
    import main as orchestrator_main

    orchestrator_main._model_catalog_cache = None
    yield
    orchestrator_main._model_catalog_cache = None


@pytest.fixture
def catalog():
    """Load the real models.yaml catalog."""
    catalog_path = Path(__file__).parent.parent / "config" / "models.yaml"
    with open(catalog_path) as f:
        return yaml.safe_load(f)


# =============================================================================
# _detect_provider_from_model — consistency with loader
# =============================================================================


class TestDetectProviderFromModel:
    """Provider detection must match src/core/loader.py:_detect_provider()."""

    def test_openrouter_prefix(self):
        assert (
            _detect_provider_from_model("openrouter/minimax/minimax-m2.7")
            == "openrouter"
        )

    def test_groq_prefix(self):
        assert (
            _detect_provider_from_model("groq/moonshotai/kimi-k2-instruct-0905")
            == "groq"
        )

    def test_codex_prefix(self):
        assert _detect_provider_from_model("codex/gpt-5.3-codex") == "codex"

    def test_codex_spark(self):
        assert _detect_provider_from_model("codex/gpt-5.3-codex-spark") == "codex"

    def test_claude_anthropic(self):
        assert _detect_provider_from_model("claude-opus-4-6") == "anthropic"

    def test_claude_sonnet(self):
        assert _detect_provider_from_model("claude-sonnet-4-5-20250929") == "anthropic"

    def test_gemini_google(self):
        assert _detect_provider_from_model("gemini-2.5-pro") == "google"

    def test_gemini_flash(self):
        assert _detect_provider_from_model("gemini-2.5-flash") == "google"

    def test_gpt_openai(self):
        assert _detect_provider_from_model("gpt-5.4") == "openai"

    def test_gpt4o_openai(self):
        assert _detect_provider_from_model("gpt-4o") == "openai"

    def test_local_openai_prefix(self):
        """openai/ prefix models fall through to openai provider."""
        assert _detect_provider_from_model("openai/gpt-oss-120b") == "openai"

    def test_case_insensitive(self):
        assert _detect_provider_from_model("CODEX/GPT-5.3-codex") == "codex"
        assert _detect_provider_from_model("Claude-Opus-4-6") == "anthropic"
        assert _detect_provider_from_model("Gemini-2.5-Pro") == "google"

    def test_unknown_defaults_to_openai(self):
        assert _detect_provider_from_model("some-unknown-model") == "openai"


# =============================================================================
# _get_system_providers
# =============================================================================


class TestGetSystemProviders:
    def test_no_env_vars(self):
        with patch.dict(os.environ, {}, clear=True):
            # Clear all provider env vars
            for env_vars in _PROVIDER_ENV_KEYS.values():
                for v in env_vars:
                    os.environ.pop(v, None)
            assert _get_system_providers() == set()

    def test_openai_key_set(self):
        env = {"OPENAI_API_KEY": "sk-test"}
        with patch.dict(os.environ, env, clear=False):
            providers = _get_system_providers()
            assert "openai" in providers

    def test_multiple_keys(self):
        env = {
            "OPENAI_API_KEY": "sk-test",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "GROQ_API_KEY": "gsk-test",
        }
        with patch.dict(os.environ, env, clear=False):
            providers = _get_system_providers()
            assert providers >= {"openai", "anthropic", "groq"}

    def test_empty_key_ignored(self):
        env = {"GOOGLE_API_KEY": ""}
        with patch.dict(os.environ, env, clear=False):
            providers = _get_system_providers()
            assert "google" not in providers


# =============================================================================
# _load_model_catalog
# =============================================================================


class TestLoadModelCatalog:
    def test_loads_real_file(self):
        with patch.dict(
            os.environ, {"CONFIG_DIR": str(Path(__file__).parent.parent / "config")}
        ):
            catalog = _load_model_catalog()
            assert "groups" in catalog
            assert "presets" in catalog
            assert "builder_models" in catalog
            assert "auxiliary_models" in catalog
            assert "vision_models" in catalog
            assert "whisper_models" in catalog
            assert "embedding_models" in catalog

    def test_groups_have_required_fields(self):
        with patch.dict(
            os.environ, {"CONFIG_DIR": str(Path(__file__).parent.parent / "config")}
        ):
            catalog = _load_model_catalog()
            for group in catalog["groups"]:
                assert "name" in group
                assert "provider" in group
                assert "models" in group
                for model in group["models"]:
                    assert "id" in model
                    assert "display_name" in model

    def test_all_providers_are_known(self):
        known_providers = {
            "local",
            "openai",
            "codex",
            "anthropic",
            "google",
            "groq",
            "openrouter",
        }
        with patch.dict(
            os.environ, {"CONFIG_DIR": str(Path(__file__).parent.parent / "config")}
        ):
            catalog = _load_model_catalog()
            for group in catalog["groups"]:
                assert group["provider"] in known_providers, (
                    f"Unknown provider '{group['provider']}' in group '{group['name']}'"
                )

    def test_embedding_models_have_dimensions(self):
        with patch.dict(
            os.environ, {"CONFIG_DIR": str(Path(__file__).parent.parent / "config")}
        ):
            catalog = _load_model_catalog()
            for m in catalog["embedding_models"]:
                assert "dimensions" in m, (
                    f"Embedding model {m['id']} missing dimensions"
                )
                assert isinstance(m["dimensions"], int)

    def test_caching(self):
        with patch.dict(
            os.environ, {"CONFIG_DIR": str(Path(__file__).parent.parent / "config")}
        ):
            cat1 = _load_model_catalog()
            cat2 = _load_model_catalog()
            assert cat1 is cat2  # Same object — cached

    def test_missing_file_returns_empty(self, tmp_path):
        with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path)}):
            catalog = _load_model_catalog()
            assert catalog == {"groups": [], "presets": [], "builder_models": []}


# =============================================================================
# Provider filtering logic (unit test of the filtering algorithm)
# =============================================================================


class TestProviderFiltering:
    """Test the filtering logic used by GET /api/models."""

    def _filter(self, catalog, available_providers):
        """Replicate the filtering from list_available_models."""
        groups = []
        available_model_ids = set()
        for group in catalog.get("groups", []):
            if group["provider"] in available_providers:
                model_ids = [m["id"] for m in group.get("models", [])]
                groups.append({"group": group["name"], "models": model_ids})
                available_model_ids.update(model_ids)

        presets = [
            p
            for p in catalog.get("presets", [])
            if p["strategic"] in available_model_ids
            and p["tactical"] in available_model_ids
        ]

        builder_models = [
            {"label": m["label"], "id": m["id"]}
            for m in catalog.get("builder_models", [])
            if m.get("provider", "local") in available_providers
        ]

        def _filter_helper(key):
            return [
                m
                for m in catalog.get(key, [])
                if m.get("provider", "local") in available_providers
            ]

        return {
            "groups": groups,
            "presets": presets,
            "builder_models": builder_models,
            "auxiliary_models": _filter_helper("auxiliary_models"),
            "vision_models": _filter_helper("vision_models"),
            "whisper_models": _filter_helper("whisper_models"),
            "embedding_models": _filter_helper("embedding_models"),
        }

    def test_local_only(self, catalog):
        """With only 'local' provider, only local models are visible."""
        result = self._filter(catalog, {"local"})
        group_names = [g["group"] for g in result["groups"]]
        assert "Local" in group_names
        assert "OpenAI" not in group_names
        assert "Anthropic" not in group_names

    def test_local_plus_openai(self, catalog):
        result = self._filter(catalog, {"local", "openai"})
        group_names = [g["group"] for g in result["groups"]]
        assert "Local" in group_names
        assert "OpenAI" in group_names
        assert "Anthropic" not in group_names

    def test_codex_separate_from_openai(self, catalog):
        """Codex group requires 'codex' provider, not 'openai'."""
        result = self._filter(catalog, {"local", "openai"})
        group_names = [g["group"] for g in result["groups"]]
        assert "Codex" not in group_names

        result_with_codex = self._filter(catalog, {"local", "openai", "codex"})
        group_names = [g["group"] for g in result_with_codex["groups"]]
        assert "Codex" in group_names

    def test_all_providers_shows_everything(self, catalog):
        all_providers = {
            "local",
            "openai",
            "codex",
            "anthropic",
            "google",
            "groq",
            "openrouter",
        }
        result = self._filter(catalog, all_providers)
        assert len(result["groups"]) == len(catalog["groups"])
        assert len(result["presets"]) == len(catalog["presets"])
        assert len(result["builder_models"]) == len(catalog["builder_models"])

    def test_preset_filtering_both_models_required(self, catalog):
        """A preset only shows when both strategic and tactical models are available."""
        # Only anthropic — "Opus + Sonnet" preset needs both claude models
        result = self._filter(catalog, {"local", "anthropic"})
        preset_labels = [p["label"] for p in result["presets"]]
        assert "Opus + Sonnet" in preset_labels
        # "Codex + Spark" needs codex — should be missing
        assert "Codex + Spark" not in preset_labels

    def test_preset_cross_provider_filtered(self, catalog):
        """Presets spanning two providers are hidden if one is missing."""
        # "K2 + OSS 120B (Groq)" needs groq for both
        result_no_groq = self._filter(catalog, {"local", "openai"})
        labels = [p["label"] for p in result_no_groq["presets"]]
        assert "K2 + OSS 120B (Groq)" not in labels

        result_with_groq = self._filter(catalog, {"local", "openai", "groq"})
        labels = [p["label"] for p in result_with_groq["presets"]]
        assert "K2 + OSS 120B (Groq)" in labels

    def test_vision_models_filtered_by_provider(self, catalog):
        result = self._filter(catalog, {"local", "openai"})
        vision_ids = [m["id"] for m in result["vision_models"]]
        assert "gpt-4o" in vision_ids
        # Anthropic vision model should be absent
        assert "claude-sonnet-4-5-20250929" not in vision_ids

    def test_vision_models_with_anthropic(self, catalog):
        result = self._filter(catalog, {"local", "openai", "anthropic"})
        vision_ids = [m["id"] for m in result["vision_models"]]
        assert "claude-sonnet-4-5-20250929" in vision_ids

    def test_whisper_models_local_always_available(self, catalog):
        result = self._filter(catalog, {"local"})
        whisper_ids = [m["id"] for m in result["whisper_models"]]
        assert "whisper-large-v3" in whisper_ids
        assert "whisper-large-v3-turbo" in whisper_ids
        # OpenAI whisper needs openai provider
        assert "whisper-1" not in whisper_ids

    def test_whisper_with_openai(self, catalog):
        result = self._filter(catalog, {"local", "openai"})
        whisper_ids = [m["id"] for m in result["whisper_models"]]
        assert "whisper-1" in whisper_ids

    def test_embedding_models_include_dimensions(self, catalog):
        result = self._filter(catalog, {"local", "openai"})
        for m in result["embedding_models"]:
            assert "dimensions" in m

    def test_embedding_openrouter_filtered(self, catalog):
        result_no_or = self._filter(catalog, {"local", "openai"})
        emb_ids = [m["id"] for m in result_no_or["embedding_models"]]
        assert "openrouter/qwen/qwen3-embedding-8b" not in emb_ids

        result_with_or = self._filter(catalog, {"local", "openai", "openrouter"})
        emb_ids = [m["id"] for m in result_with_or["embedding_models"]]
        assert "openrouter/qwen/qwen3-embedding-8b" in emb_ids

    def test_auxiliary_models_filtered(self, catalog):
        result = self._filter(catalog, {"local"})
        aux_ids = [m["id"] for m in result["auxiliary_models"]]
        assert "openai/RedHatAI/gemma-4-31B-it-FP8-Dynamic" in aux_ids  # local provider
        assert "gpt-4o-mini" not in aux_ids  # openai provider

    def test_builder_models_filtered(self, catalog):
        result = self._filter(catalog, {"local", "codex"})
        builder_ids = [m["id"] for m in result["builder_models"]]
        assert "openai/RedHatAI/gemma-4-31B-it-FP8-Dynamic" in builder_ids  # local
        assert "codex/gpt-5.4" in builder_ids  # codex
        assert "claude-opus-4-6" not in builder_ids  # anthropic — not available


# =============================================================================
# _detect_provider_from_model vs loader._detect_provider consistency
# =============================================================================


class TestProviderDetectionConsistency:
    """Verify orchestrator and agent loader agree on provider for every cataloged model."""

    def test_all_catalog_models_consistent(self, catalog):
        """For every model in the catalog, the detected provider must match
        the catalog's declared provider (or 'openai' for 'local' models
        that use the openai/ prefix)."""
        for group in catalog["groups"]:
            declared_provider = group["provider"]
            for model in group["models"]:
                detected = _detect_provider_from_model(model["id"])
                # Local models with openai/ prefix detect as "openai" — that's expected
                if declared_provider == "local" and model["id"].startswith("openai/"):
                    assert detected == "openai", (
                        f"Model {model['id']}: expected 'openai' for local/openai-prefix, got '{detected}'"
                    )
                elif declared_provider == "local":
                    pass  # Non-prefixed local models — detection varies
                else:
                    assert detected == declared_provider, (
                        f"Model {model['id']}: catalog says '{declared_provider}', "
                        f"detected '{detected}'"
                    )
