"""Tests for the model registry (src/core/model_registry.py).

Pins the post-chunk-6 contract:

- Resolution chain is custom → system → catalog → ``UnknownModelError``.
  No YAML fallback, no ``LLM_BASE_URL`` env-var inheritance.
- ``family_of`` is a sync prefix-pattern fallback for callers that don't
  have a catalog row in hand.
- ``UnknownModelError``'s message points operators at the right admin
  surfaces (Admin → Models, /api/settings/llm-endpoints).
"""

from pathlib import Path

import pytest
import yaml

from src.core.model_registry import (
    ModelMeta,
    UnknownModelError,
    _factory_provider,
    family_of,
    register_catalog_lookup,
    register_custom_lookup,
    register_system_lookup,
    resolve_model,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODEL_CONFIG_MATRIX_YAML = _REPO_ROOT / "config" / "model_config_matrix.yaml"


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


class TestFactoryProviderMapping:
    """_factory_provider maps catalog provider slugs to LLM factory keys."""

    def test_local_maps_to_openai(self):
        assert _factory_provider("local") == "openai"

    def test_none_maps_to_openai(self):
        assert _factory_provider(None) == "openai"

    def test_known_providers_pass_through(self):
        for p in ("openai", "anthropic", "google", "groq", "openrouter", "codex"):
            assert _factory_provider(p) == p

    def test_unknown_label_falls_back_to_openai(self):
        # Keeps the system forgiving of catalog typos — dispatch won't crash,
        # it'll just route through the OpenAI factory.
        assert _factory_provider("made-up-provider") == "openai"


class TestFamilyOf:
    """family_of() — sync prefix-pattern fallback for callers without a row."""

    def test_claude_opus(self):
        assert family_of("claude-opus-4-7") == "claude-opus"

    def test_gpt_4o_uses_legacy_family(self):
        # `family_of`'s heuristic predates the family-matcher service and
        # still returns "gpt-4o" for native gpt-4o; the matcher service
        # (orchestrator/services/family_matcher.py) is the modern source
        # of truth and routes gpt-4o to "default". Pin both behaviors so a
        # future unification doesn't quietly break either caller.
        assert family_of("gpt-4o") == "gpt-4o"

    def test_gpt_5_pro(self):
        assert family_of("gpt-5.2-pro") == "gpt-5"

    def test_o_series(self):
        assert family_of("o3-mini") == "o-series"

    def test_codex_spark_beats_codex(self):
        assert family_of("gpt-5.3-codex-spark") == "codex-spark"

    def test_minimax_m2_stays_minimax(self):
        # M2.x must NOT be captured by the new minimax-m3 branch.
        assert family_of("minimax-m2.7") == "minimax"
        assert family_of("MiniMaxAI/MiniMax-M2.7") == "minimax"
        assert family_of("openrouter/minimax/minimax-m2.7") == "minimax"

    def test_minimax_m3_beats_generic_minimax(self):
        # minimax-m3 is a substring superset of "minimax"; the M3 branch must
        # win so M3 models pick up the 1M-context, multimodal matrix entry.
        assert family_of("minimax-m3") == "minimax-m3"
        assert family_of("MiniMaxAI/MiniMax-M3") == "minimax-m3"
        assert family_of("openrouter/minimax/minimax-m3") == "minimax-m3"

    def test_unknown_returns_default(self):
        assert family_of("totally-unknown-model") == "default"

    def test_glm(self):
        # GLM-5.2 (Zhipu, OpenRouter). Substring match covers every transport
        # form: bare, vendor-prefixed, and openrouter-prefixed.
        assert family_of("glm-5.2") == "glm"
        assert family_of("z-ai/glm-5.2") == "glm"
        assert family_of("openrouter/z-ai/glm-5.2") == "glm"

    def test_mistral(self):
        # Mistral 3 family + specialists. Native api.mistral.ai serves bare ids;
        # the openrouter/ prefix recurses on the trailing segment.
        assert family_of("mistral-large-latest") == "mistral"
        assert family_of("mistral-medium-latest") == "mistral"
        assert family_of("mistral-small-latest") == "mistral"
        assert family_of("codestral-latest") == "mistral"
        assert family_of("ministral-3-8b") == "mistral"
        assert family_of("openrouter/mistralai/mistral-large") == "mistral"


class TestUnknownModels:
    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        register_custom_lookup(None)
        register_system_lookup(None)
        register_catalog_lookup(None)
        yield
        register_custom_lookup(None)
        register_system_lookup(None)
        register_catalog_lookup(None)

    @pytest.mark.asyncio
    async def test_unknown_id_raises(self):
        """No hooks, unknown ID → UnknownModelError. Post chunk 6 there is
        no YAML fallback to soak up unrecognised IDs."""
        with pytest.raises(UnknownModelError) as exc_info:
            await resolve_model("totally-made-up-model-xyz")
        assert exc_info.value.model_id == "totally-made-up-model-xyz"
        assert "totally-made-up-model-xyz" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_unknown_id_message_points_at_admin_surfaces(self):
        """Error message must name the right admin paths so operators
        landing on the message can self-serve."""
        with pytest.raises(UnknownModelError) as exc_info:
            await resolve_model("never-heard-of")
        msg = str(exc_info.value)
        assert "Admin → Models" in msg or "Admin -> Models" in msg
        assert "/api/settings/llm-endpoints" in msg

    @pytest.mark.asyncio
    async def test_empty_id_raises(self):
        with pytest.raises(UnknownModelError):
            await resolve_model("")

    @pytest.mark.asyncio
    async def test_known_id_without_any_hook_still_raises(self):
        """Even a previously-built-in ID like gpt-4o resolves to
        UnknownModelError when no DB hook is registered — the YAML
        fallback that used to absorb this case is gone."""
        with pytest.raises(UnknownModelError):
            await resolve_model("gpt-4o")


class TestCatalogCoverage:
    """The ``model_config_matrix.yaml`` must always carry a ``default``
    family with a ``settings`` block — every catalog row whose family
    isn't explicit falls through to it."""

    def test_default_family_has_settings_subsection(self):
        matrix = _load_yaml(_MODEL_CONFIG_MATRIX_YAML)
        assert "default" in matrix and "settings" in matrix["default"], (
            "model_config_matrix.yaml must contain a 'default' family with a "
            "'settings' subsection — custom models without a family rely on it."
        )


class TestCustomEndpointLookup:
    """Per-user custom endpoints take precedence over system + catalog."""

    @pytest.fixture(autouse=True)
    def _clear_hook(self):
        register_custom_lookup(None)
        register_system_lookup(None)
        register_catalog_lookup(None)
        yield
        register_custom_lookup(None)
        register_system_lookup(None)
        register_catalog_lookup(None)

    @pytest.mark.asyncio
    async def test_custom_lookup_wins(self):
        """A user-registered 'gpt-4o' should route to their endpoint."""
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
        assert meta.endpoint_id == "00000000-0000-0000-0000-000000000001"
        assert meta.api_key_ref is None
        assert calls == [("user-1", "gpt-4o")]

    @pytest.mark.asyncio
    async def test_missing_custom_row_with_no_other_hooks_raises(self):
        """No catalog or system hook, custom returns None → UnknownModelError."""

        async def fake_lookup(user_id, model_id, capability="chat"):
            return None

        register_custom_lookup(fake_lookup)

        with pytest.raises(UnknownModelError):
            await resolve_model("gpt-4o", user_id="user-1")

    @pytest.mark.asyncio
    async def test_no_user_id_skips_hook(self):
        """user_id=None never invokes the per-user hook."""
        hook_called = False

        async def fake_lookup(user_id, model_id, capability="chat"):
            nonlocal hook_called
            hook_called = True
            return None

        register_custom_lookup(fake_lookup)

        with pytest.raises(UnknownModelError):
            await resolve_model("gpt-4o", user_id=None)
        assert hook_called is False

    @pytest.mark.asyncio
    async def test_hook_none_falls_to_unknown(self):
        """Registering None (orchestrator shutdown path) must not raise
        — but resolve_model itself does, since no other source matches."""
        register_custom_lookup(None)
        with pytest.raises(UnknownModelError):
            await resolve_model("gpt-4o", user_id="user-1")

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


class TestSystemEndpointLookup:
    """resolve_model() consults the system-scope hook after the user hook
    and before the catalog. System rows are visible to all users."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        register_custom_lookup(None)
        register_system_lookup(None)
        register_catalog_lookup(None)
        yield
        register_custom_lookup(None)
        register_system_lookup(None)
        register_catalog_lookup(None)

    @pytest.mark.asyncio
    async def test_system_lookup_wins(self):
        """A seeded system endpoint for 'gpt-4o' should route there."""

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
    async def test_system_miss_falls_to_unknown(self):
        async def fake_sys(model_id, capability="chat"):
            return None

        register_system_lookup(fake_sys)
        with pytest.raises(UnknownModelError):
            await resolve_model("gpt-4o")

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

    def test_metadata_is_frozen(self):
        meta = ModelMeta(
            model_id="test/model",
            provider="openai",
            family="default",
            display_name="Test",
        )
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            meta.provider = "anthropic"  # type: ignore[misc]

    def test_default_origin_is_catalog(self):
        """Default origin flipped from 'builtin' to 'catalog' in chunk 6 —
        the YAML-derived 'builtin' provenance no longer exists."""
        meta = ModelMeta(
            model_id="test/model",
            provider="openai",
            family="default",
            display_name="Test",
        )
        assert meta.origin == "catalog"
        assert meta.endpoint_id is None

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
        assert meta.endpoint_id is None
        assert meta.capability == "chat"


class TestCatalogLookup:
    """resolve_model() consults the DB-backed catalog hook between the
    system-endpoint hook and the (now removed) YAML fallback. Catalog rows
    carry their transport: 'endpoint' rows inherit base_url+api_key from
    the joined llm_endpoints row; 'system' rows carry api_key_ref so the
    dispatcher resolves the key via system_api_keys."""

    @pytest.fixture(autouse=True)
    def _clear_hooks(self):
        register_custom_lookup(None)
        register_system_lookup(None)
        register_catalog_lookup(None)
        yield
        register_custom_lookup(None)
        register_system_lookup(None)
        register_catalog_lookup(None)

    @pytest.mark.asyncio
    async def test_catalog_endpoint_row_resolves_with_inline_base_url(self):
        async def fake_catalog(model_id, capability="chat"):
            return {
                "provider_kind": "endpoint",
                "provider_ref": "11111111-1111-1111-1111-111111111111",
                "model_id": model_id,
                "display_label": "Local Gemma",
                "capability": capability,
                "family": "gemma",
                "context_window": 32000,
                "reasoning_level": None,
                "endpoint_id": "11111111-1111-1111-1111-111111111111",
                "endpoint_label": "vLLM",
                "endpoint_base_url": "http://vllm.svc/v1",
                "enabled": True,
            }

        register_catalog_lookup(fake_catalog)
        meta = await resolve_model("RedHatAI/gemma-4-31B-it-FP8-Dynamic")
        assert meta.origin == "catalog"
        assert meta.provider == "openai"  # endpoint rows route via openai factory
        assert meta.base_url == "http://vllm.svc/v1"
        assert meta.endpoint_id == "11111111-1111-1111-1111-111111111111"
        assert meta.api_key_ref is None
        assert meta.family == "gemma"

    @pytest.mark.asyncio
    async def test_catalog_system_row_resolves_with_api_key_ref(self):
        async def fake_catalog(model_id, capability="chat"):
            return {
                "provider_kind": "system",
                "provider_ref": "anthropic",
                "model_id": model_id,
                "display_label": "Claude Opus 4.7",
                "capability": capability,
                "family": "claude-opus",
                "context_window": 200000,
                "reasoning_level": None,
                "enabled": True,
            }

        register_catalog_lookup(fake_catalog)
        meta = await resolve_model("claude-opus-4-7")
        assert meta.origin == "catalog"
        assert meta.provider == "anthropic"
        assert meta.api_key_ref == "anthropic"
        assert meta.base_url is None
        assert meta.endpoint_id is None

    @pytest.mark.asyncio
    async def test_catalog_runs_after_system_lookup(self):
        """Catalog hook only fires when the system endpoint hook misses."""
        order: list[str] = []

        async def fake_sys(model_id, capability="chat"):
            order.append("system")
            return None

        async def fake_catalog(model_id, capability="chat"):
            order.append("catalog")
            return {
                "provider_kind": "system",
                "provider_ref": "openai",
                "model_id": model_id,
                "display_label": "Custom GPT",
                "capability": capability,
                "family": "default",
                "enabled": True,
            }

        register_system_lookup(fake_sys)
        register_catalog_lookup(fake_catalog)

        meta = await resolve_model("gpt-4o")
        assert order == ["system", "catalog"]
        assert meta.origin == "catalog"

    @pytest.mark.asyncio
    async def test_catalog_miss_raises_unknown(self):
        """When the catalog lookup misses and no other hook matches, the
        resolver raises — there is no longer a YAML fallback to soak up
        the miss. This is the behavioral contract change of chunk 6."""

        async def fake_catalog(model_id, capability="chat"):
            return None

        register_catalog_lookup(fake_catalog)

        with pytest.raises(UnknownModelError):
            await resolve_model("gpt-4o")

    @pytest.mark.asyncio
    async def test_catalog_passes_capability_through(self):
        """resolve_model(..., capability='auxiliary') must reach the catalog
        with capability='auxiliary' so non-chat catalog rows are matchable."""
        captured: dict = {}

        async def fake_catalog(model_id, capability="chat"):
            captured["model_id"] = model_id
            captured["capability"] = capability
            return None

        register_catalog_lookup(fake_catalog)

        with pytest.raises(UnknownModelError):
            await resolve_model(
                "openrouter/openai/text-embedding-3-large", capability="embedding"
            )
        assert captured == {
            "model_id": "openrouter/openai/text-embedding-3-large",
            "capability": "embedding",
        }
