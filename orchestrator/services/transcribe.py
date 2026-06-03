"""Speech-to-text for the persistent-chat composer.

Mirrors ``services/tts.py``: resolves the user's Whisper (STT) model +
credentials via the shared per-user resolver, then calls an OpenAI-compatible
``audio.transcriptions`` endpoint. Language is **auto-detected** (no hint sent)
so mixed-language speakers transcribe correctly.

Returns ``None`` when no STT model is configured (→ the endpoint answers 204,
the "feature off" signal) or when the transcription call fails — non-fatal,
exactly like TTS. No caching: every recording is unique.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Optional

from openai import AsyncOpenAI

from services.capability_credentials import resolve_capability_credentials

logger = logging.getLogger(__name__)


def _extract_transcript(result: object) -> str:
    """Pull the transcript text out of whatever the STT endpoint returned.

    Well-behaved endpoints (JSON response format) yield a ``Transcription``
    object with a ``.text`` attribute. Some OpenAI-compatible servers instead
    return a bare string or a ``{"text": "..."}`` JSON blob regardless of the
    requested format — unwrap those so raw JSON never reaches the composer.
    """
    text = getattr(result, "text", None)
    if text is None:
        if isinstance(result, dict):
            text = result.get("text", "")
        elif isinstance(result, str):
            stripped = result.strip()
            if stripped.startswith("{") and '"text"' in stripped:
                try:
                    parsed = json.loads(stripped)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    text = parsed.get("text", stripped)
                else:
                    text = stripped
            else:
                text = stripped
        else:
            text = ""
    return (text or "").strip()


async def transcribe_thread_audio(
    *,
    audio_bytes: bytes,
    filename: str,
    user_id: str,
    postgres_db,
    timeout: float = 60.0,
) -> Optional[str]:
    """Transcribe recorded audio to text. ``None`` when STT is unavailable.

    Model + credential resolution mirrors the TTS path: user setting
    (``default_whisper_model``) > system default for the ``whisper``
    capability, then the model's endpoint/provider key.
    """
    if not audio_bytes:
        return None

    # Both the resolver inputs are drawn from the same pools the dispatcher and
    # TTS use, keyed on the authenticated user.
    user_settings = await postgres_db.get_user_settings(user_id) or {}
    resolved_keys = await postgres_db.resolve_api_keys_for_job(
        user_id=user_id, project_id=None
    )

    creds = await resolve_capability_credentials(
        capability="whisper",
        user_settings=user_settings,
        user_id=user_id,
        resolved_keys=resolved_keys,
        postgres_db=postgres_db,
    )
    if creds is None:
        logger.info("No STT (whisper) model configured for user %s", user_id)
        return None
    model, base_url, api_key = creds

    if not api_key:
        logger.warning("Transcription aborted: no API key for model %s", model)
        return None

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    try:
        # The OpenAI SDK infers the audio format from the filename extension,
        # so pass a (name, fileobj) tuple carrying the real extension (.webm).
        # Use the SDK's default JSON response format — NOT "text". With "text"
        # the SDK returns the raw HTTP body, and OpenAI-compatible endpoints that
        # answer with a JSON object even for a text request then leak the literal
        # `{"text": "..."}` blob into the transcript.
        file_tuple = (filename or "voice.webm", io.BytesIO(audio_bytes))
        result = await client.audio.transcriptions.create(
            model=model,
            file=file_tuple,
        )
    except Exception:
        logger.exception("Transcription failed for model %s", model)
        return None
    finally:
        await client.close()

    return _extract_transcript(result) or None
