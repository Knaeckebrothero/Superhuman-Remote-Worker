"""Tests for the model registry (src/core/model_registry.py).

Locks in provider/family/origin contracts for every built-in model ID and
validates the registry's load-from-YAML path, so the PR 2 refactor that
swaps out _detect_provider / detect_model_family has a regression anchor.
"""

from pathlib import Path

import pytest
import yaml

from src.core.model_registry import (
    ModelMeta,
    UnknownModelError,
    _factory_provider,
    list_builtin_models,
    register_custom_lookup,
    register_system_lookup,
    reload_registry,
    resolve_model,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODELS_YAML = _REPO_ROOT / "config" / "models.yaml"
_SETTINGS_MATRIX_YAML = _REPO_ROOT / "config" / "settings_matrix.yaml"


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


class TestFactoryProviderMapping:
    """_factory_provider maps YAML labels to LLM factory keys."""

    def test_local_maps_to_openai(self):
        assert _factory_provider("local") == "openai"

    def test_none_maps_to_openai(self):
        assert _factory_provider(None) == "openai"

    def test_known_providers_pass_through(self):
        for p in ("openai", "anthropic", "google", "groq", "openrouter", "codex"):
            assert _factory_provider(p) == p

    def test_unknown_label_falls_back_to_openai(self):
        # Keeps the system forgiving of YAML typos — dispatch won't crash,
        # it'll just route through the OpenAI factory.
        assert _factory_provider("made-up-provider") == "openai"


class TestResolveBuiltinModels:
    """resolve_model() returns correct metadata for every built-in entry."""

    @pytest.mark.asyncio
    async def test_claude_opus(self):
        meta = await resolve_model("claude-opus-4-6")
        assert meta.provider == "anthropic"
        assert meta.family == "claude-opus"
        assert meta.origin == "builtin"
        assert meta.display_name == "Claude Opus 4.6"

    @pytest.mark.asyncio
    async def test_claude_sonnet(self):
        meta = await resolve_model("claude-sonnet-4-5-20250929")
        assert meta.provider == "anthropic"
        assert meta.family == "claude-sonnet"

    @pytest.mark.asyncio
    async def test_gemini_pro(self):
        meta = await resolve_model("gemini-2.5-pro")
        assert meta.provider == "google"
        assert meta.family == "gemini"

    @pytest.mark.asyncio
    async def test_gpt_4o(self):
        meta = await resolve_model("gpt-4o")
        assert meta.provider == "openai"
        assert meta.family == "default"

    @pytest.mark.asyncio
    async def test_groq_kimi(self):
        meta = await resolve_model("groq/moonshotai/kimi-k2-instruct-0905")
        assert meta.provider == "groq"
        assert meta.family == "default"

    @pytest.mark.asyncio
    async def test_groq_gpt_oss(self):
        meta = await resolve_model("groq/gpt-oss-120b")
        assert meta.provider == "groq"
        assert meta.family == "gpt-oss"

    @pytest.mark.asyncio
    async def test_openrouter_minimax(self):
        meta = await resolve_model("openrouter/minimax/minimax-m2.7")
        assert meta.provider == "openrouter"
        assert meta.family == "minimax"

    @pytest.mark.asyncio
    async def test_codex(self):
        meta = await resolve_model("codex/gpt-5.3-codex")
        assert meta.provider == "codex"
        assert meta.family == "gpt-5"

    @pytest.mark.asyncio
    async def test_local_model_routes_through_openai_factory(self):
        # Currently named with openai/ prefix; PR 2 drops the prefix.
        # Either way, the Local group's factory target is openai.
        meta = await resolve_model("RedHatAI/gemma-4-31B-it-FP8-Dynamic")
        assert meta.provider == "openai"
        assert meta.family == "gemma"
        assert meta.origin == "builtin"


class TestHelperOnlyModels:
    """Models that live only in builder/auxiliary/vision lists, not groups[]."""

    @pytest.mark.asyncio
    async def test_gpt_4_1_mini_from_vision_list(self):
        # gpt-4.1-mini is only in vision_models, not in groups[].
        meta = await resolve_model("gpt-4.1-mini")
        assert meta.provider == "openai"
        assert meta.origin == "builtin"


class TestUnknownModels:
    @pytest.mark.asyncio
    async def test_unknown_id_raises(self):
        with pytest.raises(UnknownModelError) as exc_info:
            await resolve_model("totally-made-up-model-xyz")
        assert exc_info.value.model_id == "totally-made-up-model-xyz"
        assert "totally-made-up-model-xyz" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_empty_id_raises(self):
        with pytest.raises(UnknownModelError):
            await resolve_model("")

    @pytest.mark.asyncio
    async def test_user_id_without_hook_hits_builtin(self):
        # If no custom-endpoint hook is registered (e.g., agent process,
        # unit test), user_id is accepted but ignored — built-in lookup runs.
        register_custom_lookup(None)
        meta = await resolve_model(
            "claude-opus-4-6",
            user_id="11111111-2222-3333-4444-555555555555",
        )
        assert meta.provider == "anthropic"
        assert meta.origin == "builtin"


class TestCatalogCoverage:
    """Contract tests against config/models.yaml — catches drift."""

    def test_every_groups_entry_is_registered(self):
        data = _load_yaml(_MODELS_YAML)
        all_ids = list_builtin_models()
        registered_ids = {m.model_id for m in all_ids}
        for group in data.get("groups", []):
            for entry in group.get("models", []):
                assert entry["id"] in registered_ids, (
                    f"Model {entry['id']!r} from group {group.get('name')!r} "
                    f"was not registered"
                )

    def test_every_family_has_settings_matrix_entry_or_default(self):
        """Every family used in models.yaml should resolve cleanly through
        settings_matrix.yaml — either via a dedicated entry or the 'default'
        fallback. Custom models default to family='default', so the default
        entry must exist.
        """
        matrix = _load_yaml(_SETTINGS_MATRIX_YAML)
        assert "default" in matrix, (
            "settings_matrix.yaml must contain a 'default' entry — custom "
            "models without a family rely on it."
        )

        families = {m.family for m in list_builtin_models()}
        for family in families:
            # Either the family has its own matrix entry, or 'default'
            # covers it. Both are acceptable.
            assert family == "default" or family in matrix or "default" in matrix

    def test_no_duplicate_model_ids(self):
        ids = [m.model_id for m in list_builtin_models()]
        assert len(ids) == len(set(ids)), (
            f"Duplicate model IDs in built-in registry: "
            f"{[i for i in ids if ids.count(i) > 1]}"
        )


class TestReloadRegistry:
    def test_reload_is_idempotent(self):
        before = {m.model_id for m in list_builtin_models()}
        reload_registry()
        after = {m.model_id for m in list_builtin_models()}
        assert before == after


class TestCustomEndpointLookup:
    """resolve_model() defers to the registered custom-endpoint hook when
    user_id is present; falls back to the built-in catalog otherwise.
    """

    @pytest.fixture(autouse=True)
    def _clear_hook(self):
        # Every test in this class starts with no hook registered.
        register_custom_lookup(None)
        yield
        register_custom_lookup(None)

    @pytest.mark.asyncio
    async def test_custom_lookup_wins_over_builtin(self):
        """A user-registered 'gpt-4o' should route to their endpoint, not OpenAI."""
        calls = []

        async def fake_lookup(user_id, model_id, capability="chat"):
            calls.append((user_id, model_id))
            return {
                "endpoint_id": "00000000-0000-0000-0000-000000000001",
                "base_url": "https://my-vllm.example/v1",
                "model_id": model_id,
                "display_name": "My Private GPT-4o",
                "family": "gpt-4o",
                "context_window": 128000,
                "reasoning_level": None,
            }

        register_custom_lookup(fake_lookup)

        meta = await resolve_model("gpt-4o", user_id="user-1")
        assert meta.origin == "custom"
        assert meta.provider == "openai"
        assert meta.base_url == "https://my-vllm.example/v1"
        assert meta.display_name == "My Private GPT-4o"
        assert meta.endpoint_id == "00000000-0000-0000-0000-000000000001"
        # api_key_ref is None — custom keys travel inline via endpoint_id.
        assert meta.api_key_ref is None
        assert calls == [("user-1", "gpt-4o")]

    @pytest.mark.asyncio
    async def test_missing_custom_row_falls_back_to_builtin(self):
        async def fake_lookup(user_id, model_id, capability="chat"):
            return None

        register_custom_lookup(fake_lookup)

        meta = await resolve_model("gpt-4o", user_id="user-1")
        assert meta.origin == "builtin"
        assert meta.provider == "openai"

    @pytest.mark.asyncio
    async def test_no_user_id_skips_hook(self):
        """Even with a hook registered, None user_id never calls it."""
        hook_called = False

        async def fake_lookup(user_id, model_id, capability="chat"):
            nonlocal hook_called
            hook_called = True
            return None

        register_custom_lookup(fake_lookup)

        meta = await resolve_model("gpt-4o", user_id=None)
        assert hook_called is False
        assert meta.origin == "builtin"

    @pytest.mark.asyncio
    async def test_hook_none_falls_back_to_builtin(self):
        """Registering None (orchestrator shutdown path) must not raise."""
        register_custom_lookup(None)
        meta = await resolve_model("gpt-4o", user_id="user-1")
        assert meta.origin == "builtin"

    @pytest.mark.asyncio
    async def test_custom_family_default_when_null(self):
        async def fake_lookup(user_id, model_id, capability="chat"):
            return {
                "endpoint_id": "00000000-0000-0000-0000-000000000001",
                "base_url": "https://x/v1",
                "model_id": model_id,
                "display_name": "X",
                "family": None,
                "context_window": None,
                "reasoning_level": None,
            }

        register_custom_lookup(fake_lookup)
        meta = await resolve_model("some-custom-id", user_id="user-1")
        assert meta.family == "default"

    @pytest.mark.asyncio
    async def test_unknown_with_hook_still_raises(self):
        """If custom lookup returns None and ID isn't built-in, still raises."""

        async def fake_lookup(user_id, model_id, capability="chat"):
            return None

        register_custom_lookup(fake_lookup)

        with pytest.raises(UnknownModelError):
            await resolve_model("nothing-anywhere", user_id="user-1")


class TestSystemEndpointLookup:
    """resolve_model() consults the system-scope hook after the user hook
    and before the built-in catalog. System rows are visible to all users.
    """

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        register_custom_lookup(None)
        register_system_lookup(None)
        yield
        register_custom_lookup(None)
        register_system_lookup(None)

    @pytest.mark.asyncio
    async def test_system_lookup_wins_over_builtin(self):
        """A seeded system endpoint for 'gpt-4o' should route there, not OpenAI."""

        async def fake_sys(model_id, capability="chat"):
            return {
                "endpoint_id": "00000000-0000-0000-0000-0000000000aa",
                "base_url": "http://vllm.ai.svc.cluster.local:8000/v1",
                "model_id": model_id,
                "display_name": "Shared vLLM",
                "family": "gpt-4o",
                "context_window": 128000,
                "reasoning_level": None,
            }

        register_system_lookup(fake_sys)
        meta = await resolve_model("gpt-4o")
        assert meta.origin == "system"
        assert meta.provider == "openai"
        assert meta.base_url == "http://vllm.ai.svc.cluster.local:8000/v1"
        assert meta.endpoint_id == "00000000-0000-0000-0000-0000000000aa"
        assert meta.api_key_ref is None

    @pytest.mark.asyncio
    async def test_user_custom_beats_system(self):
        """User-scoped endpoint takes precedence over system."""
        custom_calls = []
        system_calls = []

        async def fake_custom(user_id, model_id, capability="chat"):
            custom_calls.append((user_id, model_id))
            return {
                "endpoint_id": "11111111-1111-1111-1111-111111111111",
                "base_url": "https://user.example/v1",
                "model_id": model_id,
                "display_name": "User's override",
                "family": None,
                "context_window": None,
                "reasoning_level": None,
            }

        async def fake_sys(model_id, capability="chat"):
            system_calls.append(model_id)
            return None

        register_custom_lookup(fake_custom)
        register_system_lookup(fake_sys)

        meta = await resolve_model("gpt-4o", user_id="user-1")
        assert meta.origin == "custom"
        assert meta.base_url == "https://user.example/v1"
        # system lookup must not run if custom hit
        assert system_calls == []
        assert custom_calls == [("user-1", "gpt-4o")]

    @pytest.mark.asyncio
    async def test_system_lookup_runs_without_user_id(self):
        """System lookup is queried even when user_id is None."""
        called_with = []

        async def fake_sys(model_id, capability="chat"):
            called_with.append(model_id)
            return {
                "endpoint_id": "22222222-2222-2222-2222-222222222222",
                "base_url": "http://shared/v1",
                "model_id": model_id,
                "display_name": "Shared",
                "family": None,
                "context_window": None,
                "reasoning_level": None,
            }

        register_system_lookup(fake_sys)
        meta = await resolve_model("gpt-4o", user_id=None)
        assert meta.origin == "system"
        assert called_with == ["gpt-4o"]

    @pytest.mark.asyncio
    async def test_system_miss_falls_back_to_builtin(self):
        async def fake_sys(model_id, capability="chat"):
            return None

        register_system_lookup(fake_sys)
        meta = await resolve_model("gpt-4o")
        assert meta.origin == "builtin"

    @pytest.mark.asyncio
    async def test_unknown_still_raises_with_system_hook(self):
        async def fake_sys(model_id, capability="chat"):
            return None

        register_system_lookup(fake_sys)
        with pytest.raises(UnknownModelError):
            await resolve_model("nothing-anywhere")

    @pytest.mark.asyncio
    async def test_custom_miss_falls_through_to_system(self):
        """When user hook returns None, system hook still runs."""

        async def fake_custom(user_id, model_id, capability="chat"):
            return None

        async def fake_sys(model_id, capability="chat"):
            return {
                "endpoint_id": "33333333-3333-3333-3333-333333333333",
                "base_url": "http://sys/v1",
                "model_id": model_id,
                "display_name": "System fallback",
                "family": None,
                "context_window": None,
                "reasoning_level": None,
            }

        register_custom_lookup(fake_custom)
        register_system_lookup(fake_sys)

        meta = await resolve_model("gpt-4o", user_id="user-1")
        assert meta.origin == "system"
        assert meta.base_url == "http://sys/v1"


class TestModelMetaShape:
    """ModelMeta is frozen and carries the expected fields."""

    @pytest.mark.asyncio
    async def test_metadata_is_frozen(self):
        meta = await resolve_model("gpt-4o")
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            meta.provider = "anthropic"  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_default_origin_is_builtin(self):
        meta = await resolve_model("gpt-4o")
        assert meta.origin == "builtin"
        assert meta.endpoint_id is None

    @pytest.mark.asyncio
    async def test_api_key_ref_matches_provider_for_builtins(self):
        # For built-ins, api_key_ref == provider (used to look up the
        # right entry in user_api_keys at dispatch time).
        meta = await resolve_model("claude-opus-4-6")
        assert meta.api_key_ref == "anthropic"

    def test_dataclass_shape(self):
        # Construct a ModelMeta to validate the full field set.
        meta = ModelMeta(
            model_id="test/model",
            provider="openai",
            family="default",
            display_name="Test",
        )
        assert meta.base_url is None
        assert meta.context_window is None
        assert meta.reasoning_level is None
        assert meta.origin == "builtin"
        assert meta.endpoint_id is None
