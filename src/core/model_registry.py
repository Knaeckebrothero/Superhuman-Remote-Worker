"""Model registry — single source of truth for model routing metadata.

Resolves a model ID to a ModelMeta (provider, family, base_url, api_key_ref,
etc.) via three DB-backed sources:

1. The per-user ``user_llm_endpoint_models`` table, queried through a
   callable installed by ``register_custom_lookup`` at orchestrator startup
   (``origin='custom'``).
2. System-scoped rows in the same table (``user_id IS NULL``), queried
   through a callable installed by ``register_system_lookup``
   (``origin='system'``).
3. The admin-curated ``models`` catalog table, queried through a callable
   installed by ``register_catalog_lookup`` (``origin='catalog'``).

Unknown IDs raise ``UnknownModelError``. Dispatch code consumes the
ModelMeta to decide which LLM factory to invoke and whether to inject a
``base_url`` / ``api_key`` into the job's ``config_override``.

The legacy YAML fallback (``config/models.yaml`` + ``LLM_BASE_URL``
env-var inheritance for self-hosted "Local" group models) was removed in
chunk 6 of the ``models_yaml_removal`` work. Self-hosted models are now
catalog rows pointing at an explicit ``llm_endpoints`` transport — the
previous "fall through to api.openai.com with `not-needed`" silent
failure mode is structurally impossible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class UnknownModelError(LookupError):
    """Raised when a model ID is not found in any registry source."""

    def __init__(self, model_id: str) -> None:
        super().__init__(
            f"Unknown model '{model_id}'. Configure it via Admin → Models "
            f"(catalog row anchored to a system API key or system endpoint), "
            f"or as a per-user endpoint via /api/settings/llm-endpoints."
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
    origin: str = "catalog"
    endpoint_id: Optional[str] = None
    capability: str = "chat"


_FACTORY_PROVIDERS = {
    "openai",
    "anthropic",
    "google",
    "groq",
    "openrouter",
    "mistral",
    "codex",
}


def _factory_provider(yaml_provider: Optional[str]) -> str:
    """Map a provider label to the LLM factory that serves it.

    Catalog rows use the same provider slugs as the legacy YAML (``local``
    for self-hosted OpenAI-compatible endpoints — those route through the
    openai factory because the wire protocol matches; the distinction only
    matters for UI filtering and access control, not for dispatch).
    """
    if yaml_provider is None or yaml_provider == "local":
        return "openai"
    if yaml_provider in _FACTORY_PROVIDERS:
        return yaml_provider
    return "openai"


# The system-seeded Codex proxy (CLIProxyAPI) is created under this label by
# ``ensure_codex_proxy_endpoint`` and its base_url points at the
# ``*-codex-proxy`` service. It speaks ONLY the OpenAI *Responses* API and
# surfaces model reasoning via ``reasoning.summary`` — which lives in the codex
# factory (``_create_codex_llm``). Endpoint-backed rows otherwise resolve to the
# generic ``openai`` (Chat Completions) factory, which forces
# ``use_responses_api=False`` and never requests a reasoning summary, so gpt-5.x
# / o-series / codex models wired to this endpoint silently lose their reasoning.
# See docs/done/litellm_gateway_drops_gpt_codex_reasoning_capture.md
CODEX_PROXY_ENDPOINT_LABEL = "codex-proxy"


def _endpoint_factory_provider(
    base_url: Optional[str], label: Optional[str] = None
) -> str:
    """Pick the agent-side LLM factory for an endpoint-backed row.

    Defaults to ``openai`` (the wire protocol is OpenAI-compatible) but returns
    ``codex`` for the system Codex proxy, whose Responses-API + reasoning-summary
    path lives in ``_create_codex_llm``. Detected by the well-known endpoint
    identity (label ``codex-proxy`` or a ``codex-proxy`` host in the base_url).
    """
    if label and label.strip().lower() == CODEX_PROXY_ENDPOINT_LABEL:
        return "codex"
    if base_url and CODEX_PROXY_ENDPOINT_LABEL in base_url.lower():
        return "codex"
    return "openai"


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


def family_of(model_id: str, default: str = "default") -> str:
    """Return the family string for a model ID.

    The DB-backed catalog row carries ``family`` explicitly; this helper
    is the sync prefix-pattern fallback used by call sites that don't have
    a row in hand (settings-matrix family lookup before resolution, log
    formatting, etc.).

    The heuristic strips common provider prefixes (``openrouter/``,
    ``groq/``, ``codex/``, ``openai/``) and pattern-matches against the
    known family names. Deliberately minimal and explicit-loss: returns
    ``default`` on any miss rather than inventing new families.
    """
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
    if "glm" in name:
        return "glm"
    if name.startswith(
        (
            "mistral",
            "codestral",
            "magistral",
            "ministral",
            "devstral",
            "pixtral",
            "voxtral",
        )
    ):
        # Mistral 3 family + specialists. Native api.mistral.ai serves bare ids
        # (mistral-large-latest, codestral-latest); the openrouter/ prefix is
        # stripped above. All share the `mistral` matrix family.
        return "mistral"
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
        # M3 (MSA architecture, 1M context, native multimodal) ships its own
        # prompt/settings family and must win over the generic minimax match.
        if "m3" in name:
            return "minimax-m3"
        return "minimax"

    return default


def _endpoint_row_to_meta(row: dict[str, Any], *, origin: str) -> ModelMeta:
    """Build a ModelMeta from a user/system endpoint lookup row.

    Endpoint-backed models route through the openai factory (the wire
    protocol is OpenAI-compatible) — except the system Codex proxy, which
    needs the codex factory's Responses-API + reasoning-summary path (see
    ``_endpoint_factory_provider``). api_key_ref is None because the key
    travels inline on the endpoint row — the dispatcher fetches it via
    get_user_llm_endpoint(endpoint_id), not through resolve_api_keys_for_job.
    """
    return ModelMeta(
        model_id=row["model_id"],
        provider=_endpoint_factory_provider(row.get("base_url"), row.get("label")),
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
            provider=_endpoint_factory_provider(
                row.get("endpoint_base_url"), row.get("endpoint_label")
            ),
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
        3. DB-backed catalog (``models`` table) — admin-curated offerings
           anchored to a transport (system_api_keys or system endpoint).
           When matched, the row's transport is joined inline so the
           returned ModelMeta carries either ``base_url`` (endpoint) or
           ``api_key_ref`` (system provider).
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

    raise UnknownModelError(model_id)
