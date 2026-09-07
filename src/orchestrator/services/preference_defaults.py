"""Resolve account preference defaults using app-supplied configuration."""

from collections.abc import Callable, Mapping
from typing import Any

from orchestrator.services.session_workspace_policy import (
    SESSION_DEFAULT_WORKSPACE_BACKEND,
)


async def resolve_preference_defaults(
    db: Any,
    *,
    role_base: Callable[[str], dict[str, Any]],
    environ: Mapping[str, str],
) -> dict[str, Any]:
    """Compute resolved default values for all user preference fields.

    The chat/auxiliary/session model defaults come from the DB model registry
    (``resolve_default_for_capability`` — the SAME source dispatch uses), so the
    UI shows the model the agent will actually run, not the worker base's
    placeholder. Non-model fields (autonomy, reasoning, helper-model env
    fallbacks) still read framework defaults / env vars. This lets the UI show
    the actual effective value instead of "Not set" / "Server default".
    """
    # The two role bases, fully merged (expert_base + overlay): `autonomy`
    # lives in the worker overlay and `llm.model` in expert_base, so neither
    # file alone answers.
    worker_cfg = role_base("worker")
    persistent_cfg = role_base("session")

    llm = worker_cfg.get("llm", {})
    aux = worker_cfg.get("auxiliary", {})
    p_llm = persistent_cfg.get("llm", {})

    # System chat/auxiliary defaults come from the DB model registry — the same
    # source dispatch resolves via resolve_default_for_capability — NOT the YAML
    # placeholder, so the displayed "default" is the model the agent will run.
    # Fall back to the YAML model only when the registry has no capability default.
    registry_chat = await db.resolve_default_for_capability("chat")
    registry_aux = await db.resolve_default_for_capability("auxiliary")
    # TTS is orchestrator-only (the read-aloud feature), resolved from the model
    # registry by resolve_capability_credentials — NOT an agent env-helper like
    # vision/whisper/embedding below. Resolve it the same way here so the Settings
    # voice picker shows the model actually in effect (and thus the right voice
    # list); env TTS_MODEL is only a last-ditch fallback.
    registry_tts = await db.resolve_default_for_capability("tts")

    return {
        "default_model": registry_chat or llm.get("model"),
        "default_autonomy": worker_cfg.get("autonomy"),
        "default_reasoning_level": llm.get("reasoning_level"),
        "default_auxiliary_model": registry_aux or aux.get("model") or llm.get("model"),
        # Helper-model defaults match the environment fallbacks in
        # src/agent/services/{vision_helper,audio_helper}.py and
        # src/shared/runtime/services/embedding_service.py.
        "default_vision_model": environ.get("VISION_MODEL", "gpt-4o"),
        "default_whisper_model": environ.get("WHISPER_MODEL", "whisper-1"),
        "default_tts_model": registry_tts or environ.get("TTS_MODEL", "tts-1"),
        "default_embedding_model": environ.get("EMBEDDING_MODEL", "qwen3-embedding-8b"),
        "embedding_provider": environ.get("EMBEDDING_PROVIDER", "local"),
        # Admin "View as" default — fleet-wide visibility unless the admin
        # has explicitly narrowed to their own data.
        "admin_view_mode": "all",
        "persistent_agent": {
            # Sessions resolve their base model via the same chat-capability
            # default (base_defaults in _resolve_session_config), so surface that
            # — not the session base's placeholder.
            "model": registry_chat or p_llm.get("model"),
            "permission_mode": "supervised",
            "idle_timeout_minutes": 30,
            "workspace_backend": SESSION_DEFAULT_WORKSPACE_BACKEND,
        },
    }
