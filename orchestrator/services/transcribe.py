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
import logging
from typing import Optional

from openai import AsyncOpenAI

from services.capability_credentials import resolve_capability_credentials

logger = logging.getLogger(__name__)


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
        file_tuple = (filename or "voice.webm", io.BytesIO(audio_bytes))
        result = await client.audio.transcriptions.create(
            model=model,
            file=file_tuple,
            response_format="text",
        )
    except Exception:
        logger.exception("Transcription failed for model %s", model)
        return None
    finally:
        await client.close()

    # response_format="text" yields a plain string; guard for SDKs that wrap it.
    text = result if isinstance(result, str) else getattr(result, "text", "")
    text = (text or "").strip()
    return text or None
