"""Model registry — single source of truth for model routing metadata.

Replaces the string-prefix detection scattered across src/core/loader.py and
orchestrator/main.py (_detect_provider, _detect_provider_from_model,
detect_model_family, _needs_custom_base_url) with a single lookup that
returns a ModelMeta per model ID.

PR 1 scope: scaffold only. Built-ins are loaded from config/models.yaml at
import time; resolve_model() serves them and raises UnknownModelError on a
miss. No call sites wire to this module yet — the old detection functions
still drive routing. PR 2 wires consumers and adds the per-user custom
endpoint lookup that the async signature reserves room for.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


class UnknownModelError(LookupError):
    """Raised when a model ID is not found in any registry source."""

    def __init__(self, model_id: str) -> None:
        super().__init__(
            f"Unknown model '{model_id}'. Built-in models are defined in "
            f"config/models.yaml; user-defined models come from per-user "
            f"endpoints (see /api/settings/llm-endpoints)."
        )
        self.model_id = model_id


@dataclass(frozen=True)
class ModelMeta:
    """Routing + display metadata for a single model."""

    model_id: str
    provider: str
    family: str
    display_name: str
    base_url: Optional[str] = None
    api_key_ref: Optional[str] = None
    context_window: Optional[int] = None
    reasoning_level: Optional[str] = None
    origin: str = "builtin"
    endpoint_id: Optional[str] = None


_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_CATALOG_PATH = _CONFIG_DIR / "models.yaml"

_FACTORY_PROVIDERS = {"openai", "anthropic", "google", "groq", "openrouter", "codex"}


def _factory_provider(yaml_provider: Optional[str]) -> str:
    """Map a YAML provider label to the LLM factory that serves it.

    YAML uses `local` for self-hosted OpenAI-compatible endpoints (vLLM,
    Ollama). They route through the openai factory because the wire
    protocol is OpenAI-compatible; the distinction only matters for UI
    filtering and access control, not for dispatch.
    """
    if yaml_provider is None or yaml_provider == "local":
        return "openai"
    if yaml_provider in _FACTORY_PROVIDERS:
        return yaml_provider
    return "openai"


def _entry_to_meta(entry: dict[str, Any], *, provider: str) -> ModelMeta:
    model_id = entry["id"]
    return ModelMeta(
        model_id=model_id,
        provider=provider,
        family=entry.get("family") or "default",
        display_name=entry.get("display_name") or entry.get("label") or model_id,
        base_url=entry.get("base_url"),
        api_key_ref=provider,
        context_window=entry.get("context_window"),
        reasoning_level=entry.get("reasoning_level"),
        origin="builtin",
    )


def _load_builtin_catalog() -> dict[str, ModelMeta]:
    """Parse config/models.yaml into a model_id → ModelMeta dict."""
    if not _CATALOG_PATH.exists():
        return {}

    with open(_CATALOG_PATH) as f:
        data = yaml.safe_load(f) or {}

    registry: dict[str, ModelMeta] = {}

    # Main groups: provider lives at group level, applies to every entry.
    for group in data.get("groups", []):
        provider = _factory_provider(group.get("provider"))
        for entry in group.get("models", []):
            if "id" not in entry:
                continue
            meta = _entry_to_meta(entry, provider=provider)
            registry[meta.model_id] = meta

    # Helper lists: provider is per-entry. Register only the ones that
    # aren't already covered by the main groups (helper lists may include
    # duplicates for UI filtering).
    for section in ("builder_models", "auxiliary_models", "vision_models"):
        for entry in data.get(section, []):
            model_id = entry.get("id")
            if not model_id or model_id in registry:
                continue
            provider = _factory_provider(entry.get("provider"))
            meta = _entry_to_meta(entry, provider=provider)
            registry[meta.model_id] = meta

    return registry


_builtin_registry: dict[str, ModelMeta] = _load_builtin_catalog()


def reload_registry() -> None:
    """Reload the built-in catalog from disk.

    Used by tests and by the /api/models/reload endpoint after YAML edits.
    """
    global _builtin_registry
    _builtin_registry = _load_builtin_catalog()


def list_builtin_models() -> list[ModelMeta]:
    """Return every built-in ModelMeta in catalog order. For tests and UI."""
    return list(_builtin_registry.values())


async def resolve_model(
    model_id: str,
    user_id: Optional[str] = None,
) -> ModelMeta:
    """Resolve a model ID to its routing metadata.

    Resolution order:
        1. Per-user custom endpoints (PR 2 — currently a no-op stub).
        2. Built-in catalog from config/models.yaml.
        3. Miss → UnknownModelError.

    Args:
        model_id: Model identifier (e.g., "claude-opus-4-6",
            "openai/RedHatAI/gemma-4-31B-it-FP8-Dynamic").
        user_id: Optional user UUID. Reserved for PR 2 (custom-endpoint
            lookup); ignored today.

    Returns:
        ModelMeta with provider/family/routing fields populated.

    Raises:
        UnknownModelError: Model ID not found in any source.
    """
    # PR 2 will add: custom-endpoint lookup using user_id. Keeping the
    # parameter in the signature now so callers compile-time-match later
    # without touching the async boundary.
    _ = user_id

    meta = _builtin_registry.get(model_id)
    if meta is not None:
        return meta

    raise UnknownModelError(model_id)
