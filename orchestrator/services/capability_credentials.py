"""Per-user model + credential resolution for capability-scoped LLM calls.

Shared by the TTS (``services/tts.py``) and speech-to-text
(``services/transcribe.py``) paths. Resolution mirrors the dispatcher's
per-user chain (user setting > system default), then resolves the model's
endpoint/provider into a ``(model, base_url, api_key)`` triple the OpenAI
client can use directly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from src.core.model_registry import (
    UnknownModelError,
    resolve_model as _resolve_model,
)

logger = logging.getLogger(__name__)


async def resolve_capability_credentials(
    *,
    capability: str,
    user_settings: dict[str, Any],
    user_id: str,
    resolved_keys: dict[str, str],
    postgres_db,
) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """Pick a model for ``capability`` and return (model, base_url, api_key).

    Resolution mirrors ``_inject_env_key_credentials`` in main.py:
      1. user_settings[default_<capability>_model]
      2. system default for the capability
      3. endpoint-anchored model → use endpoint base_url + api_key
      4. built-in model → use api_key from resolved_keys[provider]

    Returns ``None`` when no model is configured for the capability.
    """
    user_key = f"default_{capability}_model"
    model_id: Optional[str] = user_settings.get(user_key)
    if not model_id:
        model_id = await postgres_db.resolve_default_for_capability(capability)
    if not model_id:
        return None

    base_url: Optional[str] = None
    api_key: Optional[str] = None

    meta = None
    try:
        meta = await _resolve_model(model_id, user_id=user_id, capability=capability)
    except UnknownModelError:
        meta = None

    if (
        meta is not None
        and meta.origin in ("custom", "system", "catalog")
        and meta.endpoint_id
    ):
        endpoint_row = await postgres_db.get_user_llm_endpoint(meta.endpoint_id)
        if endpoint_row:
            base_url = endpoint_row.get("base_url")
            api_key = endpoint_row.get("api_key")
    else:
        provider = meta.api_key_ref if meta is not None else None
        if provider and provider in resolved_keys:
            api_key = resolved_keys[provider]

    return model_id, base_url, api_key
