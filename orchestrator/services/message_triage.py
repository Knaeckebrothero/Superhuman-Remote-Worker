"""AuxiliaryLLM message triage for inbound reply routing.

When a user sends an unprompted message (not a reply to a blocking request),
this service uses an LLM to decide whether to interrupt the running agent
immediately or queue the message for the next strategic phase.

Uses the builder's LLM configuration (same model/API key). Falls back to
"queue" on any failure — triage is advisory, never blocks delivery.

Environment:
    Uses existing builder LLM config (OPENAI_API_KEY, LLM_BASE_URL, etc.)
"""

import json
import logging
import os

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


async def triage_message(
    message: str,
    job_status: str,
    job_description: str,
    phase_number: int | None = None,
) -> dict:
    """Triage an inbound message to decide delivery strategy.

    Args:
        message: The inbound message text
        job_status: Current job status (e.g., "processing")
        job_description: Job description for context
        phase_number: Current phase number (optional)

    Returns:
        Dict with "action" ("interrupt" or "queue") and "reason".
        Falls back to {"action": "queue", "reason": "triage unavailable"} on failure.
    """
    fallback = {"action": "queue", "reason": "triage unavailable"}

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        # Try other providers
        for var in ("ANTHROPIC_API_KEY", "GROQ_API_KEY"):
            api_key = os.getenv(var, "")
            if api_key:
                break

    if not api_key:
        logger.debug("No API key for message triage — defaulting to queue")
        return fallback

    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    model = os.getenv("AUXILIARY_MODEL", os.getenv("TRIAGE_MODEL", "gpt-4o-mini"))

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
