"""Model registry — single source of truth for model routing metadata.

Resolves a model ID to a ModelMeta (provider, family, base_url, api_key_ref,
etc.) via three sources:

1. The per-user ``user_llm_endpoint_models`` table, queried through a
   callable installed by ``register_custom_lookup`` at orchestrator startup
   (``origin='custom'``).
2. System-scoped rows in the same table (``user_id IS NULL``), queried
   through a callable installed by ``register_system_lookup``
   (``origin='system'``).
3. The built-in catalog loaded at import time from ``config/models.yaml``
   (``origin='builtin'``).

Unknown IDs raise ``UnknownModelError``. Dispatch code consumes the
ModelMeta to decide which LLM factory to invoke and whether to inject a
``base_url`` / ``api_key`` into the job's ``config_override``.

Built-in entries in the "Local" YAML group inherit ``LLM_BASE_URL`` from
the environment at load time; this preserves the legacy env-var routing
(with a deprecation warning) until users migrate to custom endpoints.
"""

from __future__ import annotations

import logging
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import yaml

logger = logging.getLogger(__name__)


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
    capability: str = "chat"


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
    """Parse config/models.yaml into a model_id → ModelMeta dict.

    Self-hosted "Local" group entries inherit ``LLM_BASE_URL`` from the
    environment when their YAML has no explicit ``base_url``. This keeps
    the legacy deployment path working (env-var-driven vLLM URL) while
    users migrate to per-user custom endpoints. A deprecation warning
    fires the first time the fallback is applied.
    """
    if not _CATALOG_PATH.exists():
        return {}

    with open(_CATALOG_PATH) as f:
        data = yaml.safe_load(f) or {}

    env_base_url = os.getenv("LLM_BASE_URL")
    env_fallback_used = False
    registry: dict[str, ModelMeta] = {}

    # Main groups: provider lives at group level, applies to every entry.
    for group in data.get("groups", []):
        yaml_provider = group.get("provider")
        provider = _factory_provider(yaml_provider)
        is_local_group = yaml_provider == "local"
        for entry in group.get("models", []):
            if "id" not in entry:
                continue
            meta = _entry_to_meta(entry, provider=provider)
            if is_local_group and meta.base_url is None and env_base_url:
                # Replace the immutable meta with one carrying the env URL.
                meta = ModelMeta(
                    model_id=meta.model_id,
                    provider=meta.provider,
                    family=meta.family,
                    display_name=meta.display_name,
                    base_url=env_base_url,
                    api_key_ref=meta.api_key_ref,
                    context_window=meta.context_window,
                    reasoning_level=meta.reasoning_level,
                    origin=meta.origin,
                    endpoint_id=meta.endpoint_id,
                )
                env_fallback_used = True
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

    if env_fallback_used:
        warnings.warn(
            "LLM_BASE_URL is being applied to built-in 'Local' models. "
            "This env-var routing is deprecated — configure a per-user LLM "
            "endpoint via the settings UI (/api/settings/llm-endpoints) "
            "instead. The env fallback will be removed in a future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        logger.warning(
            "Using LLM_BASE_URL=%s for local built-in models (deprecated)",
            env_base_url,
        )

    return registry


_builtin_registry: dict[str, ModelMeta] = _load_builtin_catalog()


# Dependency injection: the orchestrator registers DB-backed lookups at
# startup so src/core/ stays import-free of orchestrator/database/. In
# contexts without a DB (agent process, tests), the hooks stay None and
# resolve_model() skips straight to the built-in catalog.
CustomLookup = Callable[..., Awaitable[Optional[dict[str, Any]]]]
SystemLookup = Callable[..., Awaitable[Optional[dict[str, Any]]]]
CatalogLookup = Callable[..., Awaitable[Optional[dict[str, Any]]]]
_custom_lookup: Optional[CustomLookup] = None
_system_lookup: Optional[SystemLookup] = None
_catalog_lookup: Optional[CatalogLookup] = None


def register_custom_lookup(fn: Optional[CustomLookup]) -> None:
    """Install (or clear) the per-user custom-endpoint lookup callable.

    The callable takes (user_id, model_id, capability) and returns either
    None or a row dict with keys: endpoint_id, base_url, api_key, model_id,
    display_name, family, context_window, reasoning_level, capability.
    Typically wired to ``postgres_db.resolve_user_llm_model`` at orchestrator
    startup, whose ``capability`` parameter defaults to ``'chat'``.
    """
    global _custom_lookup
    _custom_lookup = fn


def register_system_lookup(fn: Optional[SystemLookup]) -> None:
    """Install (or clear) the system-scope endpoint lookup callable.

    The callable takes (model_id, capability) and returns either None or a
    row dict with the same shape as the custom lookup (minus user_id).
    Wired to ``postgres_db.resolve_system_llm_model`` at orchestrator
    startup, whose ``capability`` parameter defaults to ``'chat'``.
    """
    global _system_lookup
    _system_lookup = fn


def register_catalog_lookup(fn: Optional[CatalogLookup]) -> None:
    """Install (or clear) the DB-backed catalog lookup callable.

    The callable takes (model_id, capability='chat') and returns either None
    or a flattened row dict from the ``models`` table joined to its transport
    (system_api_keys or llm_endpoints). Wired to
    ``postgres_db.resolve_catalog_model`` at orchestrator startup.
    """
    global _catalog_lookup
    _catalog_lookup = fn


def reload_registry() -> None:
    """Reload the built-in catalog from disk.

    Used by tests and by the /api/models/reload endpoint after YAML edits.
    """
    global _builtin_registry
    _builtin_registry = _load_builtin_catalog()


def list_builtin_models() -> list[ModelMeta]:
    """Return every built-in ModelMeta in catalog order. For tests and UI."""
    return list(_builtin_registry.values())


def resolve_builtin(model_id: str) -> Optional[ModelMeta]:
    """Synchronous lookup against the built-in catalog only.

    Skips custom-endpoint resolution entirely. Used by sync call sites
    (settings-matrix family lookup, factory dispatch) that don't have
    user context or can't await. Returns None instead of raising so
    callers can apply defaults.
    """
    return _builtin_registry.get(model_id)


def family_of(model_id: str, default: str = "default") -> str:
    """Return the family string for a model ID.

    Primary source: the built-in registry (authoritative for every entry
    in ``config/models.yaml``).

    Best-effort fallback for unknown IDs: strip common provider prefixes
    and pattern-match against the known family names. This keeps the
    settings_matrix lookup graceful for bare model names, custom-endpoint
    models without an explicit ``family``, and anything else not in the
    catalog. The heuristic is deliberately minimal and explicit-loss:
    it returns ``default`` on any miss rather than inventing new families.
    """
    meta = _builtin_registry.get(model_id)
    if meta is not None:
        return meta.family

    name = model_id.lower()
    for prefix in ("openrouter/", "groq/", "codex/"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            if "/" in name:
                name = name.split("/", 1)[1]
            break
    if name.startswith("openai/"):
        name = name[len("openai/") :]

    if name.startswith("claude-opus"):
        return "claude-opus"
    if name.startswith("claude-sonnet"):
        return "claude-sonnet"
    if name.startswith("claude-haiku"):
        return "claude-haiku"
    if "codex-spark" in name:
        return "codex-spark"
    if "codex" in name and name.startswith("gpt-5"):
        return "codex"
    if name.startswith("gpt-5"):
        return "gpt-5"
    if name.startswith("gpt-4o"):
        return "gpt-4o"
    if name.startswith(("o1", "o3", "o4")):
        return "o-series"
    if "deepseek" in name:
        return "deepseek"
    if "qwen" in name or "qwq" in name:
        return "qwen"
    if "llama" in name:
        return "llama"
    if name.startswith("gemini"):
        return "gemini"
    if name.startswith("gpt-oss"):
        return "gpt-oss"
    if "gemma" in name:
        return "gemma"
    if "minimax" in name:
        return "minimax"

    return default


def _endpoint_row_to_meta(row: dict[str, Any], *, origin: str) -> ModelMeta:
    """Build a ModelMeta from a user/system endpoint lookup row.

    Endpoint-backed models always route through the openai factory (the
    wire protocol is OpenAI-compatible). api_key_ref is None because the
    key travels inline on the endpoint row — the dispatcher fetches it
    via get_user_llm_endpoint(endpoint_id), not through
    resolve_api_keys_for_job.
    """
    return ModelMeta(
        model_id=row["model_id"],
        provider="openai",
        family=row.get("family") or "default",
        display_name=row.get("display_name") or row["model_id"],
        base_url=row["base_url"],
        api_key_ref=None,
        context_window=row.get("context_window"),
        reasoning_level=row.get("reasoning_level"),
        origin=origin,
        endpoint_id=str(row["endpoint_id"]),
        capability=row.get("capability") or "chat",
    )


# Kept for backwards-compat with any external imports. Prefer
# ``_endpoint_row_to_meta`` in new code.
def _custom_row_to_meta(row: dict[str, Any]) -> ModelMeta:
    return _endpoint_row_to_meta(row, origin="custom")


def _catalog_row_to_meta(row: dict[str, Any]) -> ModelMeta:
    """Build a ModelMeta from a ``models`` catalog row joined to its transport.

    Two shapes:
    - ``provider_kind='endpoint'`` — inherits ``base_url`` + ``api_key``
      from the joined ``llm_endpoints`` row. Routes through the
      openai factory (OpenAI-compatible wire protocol).
    - ``provider_kind='system'`` — sets ``api_key_ref`` to the provider
      slug so the dispatcher resolves the key via ``system_api_keys``;
      ``base_url`` stays None so the factory uses its hardcoded default.
    """
    provider_kind = row["provider_kind"]
    provider_ref = row["provider_ref"]
    capability = row.get("capability") or "chat"
    if provider_kind == "endpoint":
        return ModelMeta(
            model_id=row["model_id"],
            provider="openai",
            family=row.get("family") or "default",
            display_name=row.get("display_label") or row["model_id"],
            base_url=row.get("endpoint_base_url"),
            api_key_ref=None,
            context_window=row.get("context_window"),
            reasoning_level=row.get("reasoning_level"),
            origin="catalog",
            endpoint_id=str(row["endpoint_id"]) if row.get("endpoint_id") else None,
            capability=capability,
        )
    return ModelMeta(
        model_id=row["model_id"],
        provider=_factory_provider(provider_ref),
        family=row.get("family") or "default",
        display_name=row.get("display_label") or row["model_id"],
        base_url=None,
        api_key_ref=provider_ref,
        context_window=row.get("context_window"),
        reasoning_level=row.get("reasoning_level"),
        origin="catalog",
        capability=capability,
    )


async def resolve_model(
    model_id: str,
    user_id: Optional[str] = None,
    capability: str = "chat",
) -> ModelMeta:
    """Resolve a model ID to its routing metadata.

    Resolution order:
        1. Per-user custom endpoints (when user_id is set and the custom
           lookup hook has been registered).
        2. System-scoped endpoints (when the system lookup hook has been
           registered). Shared across all users; seeded by helm or
           managed via Admin → Providers.
        2.5. DB-backed catalog (``models`` table) — admin-curated offerings
           anchored to a transport (system_api_keys or system endpoint).
           When matched, the row's transport is joined inline so the
           returned ModelMeta carries either ``base_url`` (endpoint) or
           ``api_key_ref`` (system provider).
        3. Built-in catalog from config/models.yaml (chat-capability only;
           kept as a fallback during the catalog rollout window — every
           hit logs at WARN so coverage gaps are visible before the YAML
           is deleted).
        4. Miss → UnknownModelError.

    Args:
        model_id: Model identifier (e.g., "claude-opus-4-6",
            "RedHatAI/gemma-4-31B-it-FP8-Dynamic").
        user_id: Optional user UUID. Used for custom-endpoint lookup when
            a hook is registered; ignored otherwise.
        capability: Which capability slot to resolve ('chat', 'vision',
            'embedding', 'auxiliary', 'whisper'). Defaults to 'chat' so
            existing callers resolving chat-completion models keep working
            unchanged; non-chat resolvers pass explicitly.

    Returns:
        ModelMeta with provider/family/routing fields populated.

    Raises:
        UnknownModelError: Model ID not found in any source.
    """
    if user_id and _custom_lookup is not None:
        row = await _custom_lookup(user_id, model_id, capability)
        if row is not None:
            return _endpoint_row_to_meta(row, origin="custom")

    if _system_lookup is not None:
        row = await _system_lookup(model_id, capability)
        if row is not None:
            return _endpoint_row_to_meta(row, origin="system")

    if _catalog_lookup is not None:
        row = await _catalog_lookup(model_id, capability=capability)
        if row is not None:
            return _catalog_row_to_meta(row)

    if capability == "chat":
        meta = _builtin_registry.get(model_id)
        if meta is not None:
            logger.warning(
                "Resolved model %s from YAML fallback — should be in catalog "
                "(seed via _seed_models_from_yaml or add via Admin → Models)",
                model_id,
            )
            return meta

    raise UnknownModelError(model_id)
