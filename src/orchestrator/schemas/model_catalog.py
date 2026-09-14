"""Curated model catalogue request contracts and capability vocabulary."""

from typing import Any, Literal
from pydantic import BaseModel, Field


LLM_MODEL_CAPABILITIES = (
    "chat",
    "vision",
    "embedding",
    "auxiliary",
    "whisper",
    "tts",
    "search",
    "fetch",
    "rerank",
)


# Locked enum for the admin-curated catalog. Adding a new capability requires
# touching every consumer (resolver, dispatcher, default-model fallback),
# so the schema-level CHECK constraint and this Literal are kept in sync.
# ``rerank`` is the memory reranker's slot (a Cohere-shaped ``/rerank`` route;
# migration 0240). It is wired like ``embedding`` — required by the readiness
# gate, one default pin, flat ``RERANK_*`` env at dispatch — until embedding
# and rerank become optional together.
VALID_CATALOG_CAPABILITIES = (
    "chat",
    "auxiliary",
    "embedding",
    "vision",
    "whisper",
    "tts",
    "search",
    "fetch",
    "rerank",
)


VALID_CATALOG_PROVIDER_KINDS = ("system", "endpoint")


CatalogCapabilityLiteral = Literal[
    "chat",
    "auxiliary",
    "embedding",
    "vision",
    "whisper",
    "tts",
    "search",
    "fetch",
    "rerank",
]


class CatalogModelCreate(BaseModel):
    """Request body for inserting a catalog row (Admin → Models).

    ``capabilities`` is the source of truth — one row can serve multiple
    roles (e.g. ``['chat', 'auxiliary']`` for a chat-capable LLM,
    ``['chat', 'auxiliary', 'vision']`` for a multimodal one). The legacy
    singular ``capability`` form is no longer accepted; clients post the
    array directly.
    """

    provider_kind: Literal["system", "endpoint"]
    provider_ref: str = Field(
        ...,
        min_length=1,
        description=(
            "system_api_keys.provider slug for provider_kind='system' "
            "(e.g. 'anthropic'); llm_endpoints.id (UUID as text) for "
            "provider_kind='endpoint'."
        ),
    )
    model_id: str = Field(..., min_length=1, max_length=500)
    display_label: str = Field(..., min_length=1, max_length=200)
    capabilities: list[CatalogCapabilityLiteral] = Field(
        ...,
        min_length=1,
        description=(
            "The set of capabilities this model row claims. One row can "
            "serve multiple roles (e.g. ['chat', 'auxiliary'] for a "
            "chat-capable LLM, ['chat', 'auxiliary', 'vision'] for a "
            "multimodal one). Must be non-empty."
        ),
    )
    family: str = Field(
        ...,
        min_length=1,
        description="model_config_matrix.yaml key (e.g. 'claude-opus', 'gemini').",
    )
    context_window: int | None = Field(
        None,
        description=(
            "Optional override; falls back to model_config_matrix family "
            "default. Pass null (the default) to use the matrix; pass an "
            "explicit int to override (zero is allowed and round-trips as "
            "zero)."
        ),
    )
    reasoning_level: str | None = None
    params_json: dict[str, Any] | None = Field(
        None,
        description=(
            "Optional inference param overrides (e.g. {'temperature': 0.0}). "
            "Null means 'use family defaults'; explicit zero/false values "
            "round-trip as themselves (create_model accessor regression guard)."
        ),
    )
    enabled: bool = True
    notes: str | None = None


class CatalogModelUpdate(BaseModel):
    """Partial update — only fields explicitly set in the request body are
    applied. Pass ``null`` to clear an optional column to NULL.
    """

    provider_kind: Literal["system", "endpoint"] | None = None
    provider_ref: str | None = Field(None, min_length=1)
    model_id: str | None = Field(None, min_length=1, max_length=500)
    display_label: str | None = Field(None, min_length=1, max_length=200)
    capabilities: list[CatalogCapabilityLiteral] | None = Field(None, min_length=1)
    family: str | None = Field(None, min_length=1)
    context_window: int | None = None
    reasoning_level: str | None = None
    params_json: dict[str, Any] | None = None
    enabled: bool | None = None
    notes: str | None = None
