"""AuxiliaryLLM message triage for inbound reply routing.

When a user sends an unprompted message (not a reply to a blocking request),
this service uses an LLM to decide whether to interrupt the running agent
immediately or queue the message for the next strategic phase.

Uses the builder's LLM configuration — the same default model, system
endpoint, and system API key that drive the instruction-builder chat. Falls
back to ``queue`` on any failure, since triage is advisory and never blocks
delivery.

Resolution: ``db.get_default_llm_model('builder')`` → registry-resolved base
URL + ``system_api_keys[provider]``. When the DB lookup yields nothing the
caller falls back to ``queue`` — the env-var legacy path was removed once
all call sites started passing ``db=postgres_db``.
"""

from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger(__name__)

TRIAGE_SYSTEM_PROMPT = """\
You are a message triage system for an AI agent orchestrator. An inbound \
message has arrived for a running agent job. Decide whether the message \
should interrupt the agent immediately or be queued for the next strategic \
review phase.

INTERRUPT if the message:
- Directly answers a pending question from the agent
- Contains urgent information (deadline, blocker, credential, critical fix)
- Explicitly says "urgent", "ASAP", "stop", or similar
- Provides information the agent needs to continue current work

QUEUE if the message:
- Is a general status check or FYI
- Contains suggestions or feedback that can wait
- Is a non-critical follow-up
- The agent is in a strategic phase (will naturally review messages)

Respond with JSON: {"action": "interrupt" or "queue", "reason": "brief explanation"}
"""

_PROVIDER_FOR_MODEL = {
    "claude": "anthropic",
    "anthropic": "anthropic",
    "groq": "groq",
    "gemini": "google",
    "gpt": "openai",
    "codex": "openai",
}


def _infer_provider(model: str) -> str:
    lowered = model.lower()
    for prefix, provider in _PROVIDER_FOR_MODEL.items():
        if lowered.startswith(prefix) or f"/{prefix}" in lowered:
            return provider
    return "openai"


async def _resolve_triage_config(db) -> tuple[str, str, str] | None:
    """Pull model, base_url, and api_key from the DB-backed config.

    Returns None when the DB has no default configured or no usable key —
    the caller treats that as ``triage unavailable`` and queues the message.
    """
    if db is None:
        return None

    try:
        model = await db.get_default_llm_model("builder")
    except Exception as e:
        logger.debug("triage DB default lookup failed: %s", e)
        return None
    if not model:
        return None

    base_url: str | None = None
    try:
        from src.core.model_registry import UnknownModelError, resolve_model

        meta = await resolve_model(model)
        if meta is not None and meta.base_url:
            base_url = meta.base_url
    except (UnknownModelError, ImportError, Exception) as e:  # noqa: BLE001
        logger.debug("triage registry lookup failed for %s: %s", model, e)

    if not base_url:
        base_url = "https://api.openai.com/v1"

    provider = _infer_provider(model)
    api_key: str | None = None
    try:
        api_key = await db.get_system_api_key(provider)
    except Exception as e:
        logger.debug("triage system_api_keys lookup failed for %s: %s", provider, e)

    if not api_key:
        return None

    return model, base_url, api_key


async def triage_message(
    message: str,
    job_status: str,
    job_description: str,
    phase_number: int | None = None,
    db=None,
) -> dict:
    """Triage an inbound message to decide delivery strategy.

    Args:
        message: The inbound message text
        job_status: Current job status (e.g., "processing")
        job_description: Job description for context
        phase_number: Current phase number (optional)
        db: ``PostgresDB`` handle for registry-backed config resolution.
            When omitted (or when DB lookup yields no default), the
            message is queued — the env-var legacy path was removed.

    Returns:
        Dict with "action" ("interrupt" or "queue") and "reason".
        Falls back to {"action": "queue", "reason": "triage unavailable"} on failure.
    """
    fallback = {"action": "queue", "reason": "triage unavailable"}

    resolved = await _resolve_triage_config(db)
    if resolved is None:
        logger.debug("No LLM config available for message triage — defaulting to queue")
        return fallback

    model, base_url, api_key = resolved

    phase_str = f"Phase {phase_number}" if phase_number is not None else "unknown phase"
    context = (
        f"Job: {job_description[:200]}\n"
        f"Status: {job_status}, {phase_str}\n\n"
        f"Inbound message:\n{message[:2000]}"
    )

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": TRIAGE_SYSTEM_PROMPT},
                        {"role": "user", "content": context},
                    ],
                    "temperature": 0,
                    "max_tokens": 100,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        result = json.loads(content)

        action = result.get("action", "queue")
        if action not in ("interrupt", "queue"):
            action = "queue"

        reason = result.get("reason", "")
        logger.info("Message triage: %s — %s", action, reason)
        return {"action": action, "reason": reason}

    except Exception as e:
        logger.warning("Message triage failed (defaulting to queue): %s", e)
        return fallback
