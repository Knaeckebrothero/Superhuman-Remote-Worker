"""Text-to-speech with optional LLM-based formulation.

Two-step pipeline used by the persistent-chat ``Speak`` button:

1. **Formulation** (auxiliary LLM) — strips markdown, summarizes code
   blocks, converts tables/lists into flowing prose so the TTS output
   sounds natural. Skipped for short or already-clean text.
2. **Synthesis** (TTS model) — generates MP3 via the user's configured
   TTS endpoint (OpenAI-compatible).

Model + endpoint resolution mirrors the dispatcher's per-user chain
(user > project > system). Results are LRU-cached in process so
clicking Play twice on the same message doesn't re-bill.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from typing import Any, Optional

from openai import AsyncOpenAI

from services.capability_credentials import (
    resolve_capability_credentials as _resolve_capability_credentials,
)

logger = logging.getLogger(__name__)


class TtsSynthesisError(RuntimeError):
    """Raised when a TTS model *is* configured but synthesis fails (missing
    key, upstream 5xx, timeout). Distinct from "no model configured" (which
    returns ``None``) so the endpoint can answer ``502`` for a real failure
    instead of the ``204`` "feature off" signal — i.e. the button surfaces an
    error instead of silently doing nothing.
    """


# Cache caps. ~50 MP3s × ~200 KB each ≈ 10 MB. The cache is per-process
# and resets on orchestrator restart — TTS audio isn't worth persisting
# to disk.
_AUDIO_CACHE_MAX = 50
_FORMULATION_CACHE_MAX = 200

_audio_cache: "OrderedDict[str, bytes]" = OrderedDict()
_formulation_cache: "OrderedDict[str, str]" = OrderedDict()
_audio_lock = asyncio.Lock()
_formulation_lock = asyncio.Lock()


# Voice defaults per language. OpenAI's TTS-1 voices are language-agnostic
# but tonally distinct; alloy/nova render German fine. Override per-thread
# later if the user wants a different voice.
DEFAULT_VOICE_EN = "alloy"
DEFAULT_VOICE_DE = "nova"

# Heuristic: skip the formulation LLM for short or markdown-free text.
_FORMULATION_MIN_LEN = 60
_MARKDOWN_HINTS = ("**", "```", "|", "# ", "## ", "### ", "- ", "* ", "1.", "[", "](")

FORMULATION_SYSTEM_PROMPT = """You rewrite text so it sounds natural when read aloud by a text-to-speech engine.

Rules:
1. Strip ALL markdown (asterisks, headers, code fences, link syntax).
2. Convert tables to descriptive sentences — never read tables cell-by-cell.
3. For code blocks: briefly describe what the code does in one sentence; never read syntax.
4. Convert bullet lists to flowing prose with words like "first", "next", "also".
5. Drop URLs (or say "a link").
6. Preserve the meaning, tone, technical terms, and proper names.
7. Keep numbers in a readable form.

Return ONLY the rewritten text, no preamble, no commentary."""


def _hash_key(*parts: str) -> str:
    """Stable cache key from any string parts."""
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def _cache_get(cache: OrderedDict, key: str) -> Any:
    val = cache.get(key)
    if val is not None:
        cache.move_to_end(key)
    return val


def _cache_put(cache: OrderedDict, key: str, value: Any, max_size: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > max_size:
        cache.popitem(last=False)


def _voice_for_language(language: str) -> str:
    if language and language.lower().startswith("de"):
        return DEFAULT_VOICE_DE
    return DEFAULT_VOICE_EN


async def _resolve_voice(model_id: str, language: str, postgres_db) -> str:
    """Voice for ``model_id``: the catalog row's ``params_json.voice`` (set in
    Admin → Providers) when present, else the per-language default.

    Different TTS backends expose different voice catalogs (Kokoro ``af_*``,
    Piper, OpenAI ``alloy``/``nova``), so the voice can't be one hardcoded name.
    Reads the ``models`` catalog row; system/custom-endpoint models (no catalog
    row) fall back to the language default. Any lookup error is non-fatal.
    """
    try:
        row = await postgres_db.resolve_catalog_model(model_id, capability="tts")
        params = (row or {}).get("params_json")
        if isinstance(params, dict):
            voice = (params.get("voice") or "").strip()
            if voice:
                return voice
    except Exception:
        logger.debug("Could not read params_json voice for %s; using default", model_id)
    return _voice_for_language(language)


def _needs_formulation(text: str) -> bool:
    """Cheap markdown sniffer to skip the formulation LLM when not useful."""
    if not text or len(text) < _FORMULATION_MIN_LEN:
        return False
    return any(hint in text for hint in _MARKDOWN_HINTS)


async def _formulate_for_speech(
    text: str,
    *,
    model: str,
    base_url: Optional[str],
    api_key: Optional[str],
    timeout: float = 30.0,
) -> str:
    """Run the auxiliary LLM to rewrite ``text`` for natural speech."""
    cache_key = _hash_key(model, text)
    async with _formulation_lock:
        cached = _cache_get(_formulation_cache, cache_key)
    if cached is not None:
        logger.debug("TTS formulation cache hit (%s chars)", len(text))
        return cached

    if not api_key:
        logger.warning("Formulation skipped: no API key for model %s", model)
        return text

    # max_retries=0: a down/erroring endpoint must fail in seconds, not retry
    # with backoff for minutes — the "Read aloud" button would otherwise appear
    # to hang. (A 503 here previously stretched a single click to ~213s.)
    client = AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": FORMULATION_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
        )
        rewritten = (response.choices[0].message.content or "").strip()
    except Exception:
        logger.exception("TTS formulation LLM call failed; falling back to raw text")
        return text
    finally:
        await client.close()

    if not rewritten:
        return text

    async with _formulation_lock:
        _cache_put(_formulation_cache, cache_key, rewritten, _FORMULATION_CACHE_MAX)
    logger.info("TTS formulation: %d → %d chars", len(text), len(rewritten))
    return rewritten


async def _synthesize_speech(
    text: str,
    *,
    model: str,
    voice: str,
    base_url: Optional[str],
    api_key: Optional[str],
    timeout: float = 60.0,
) -> Optional[bytes]:
    """Call the TTS endpoint and return MP3 bytes."""
    if not api_key:
        logger.warning("TTS synthesis aborted: no API key for model %s", model)
        return None

    # max_retries=0: a down/erroring endpoint must fail in seconds, not retry
    # with backoff for minutes — the "Read aloud" button would otherwise appear
    # to hang. (A 503 here previously stretched a single click to ~213s.)
    client = AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0
    )
    try:
        response = await client.audio.speech.create(
            model=model,
            voice=voice,
            input=text,
            response_format="mp3",
        )
        return response.content
    except Exception:
        logger.exception("TTS synthesis failed for model %s", model)
        return None
    finally:
        await client.close()


async def generate_message_tts(
    *,
    content: str,
    language: str,
    reformulate: bool,
    user_id: str,
    postgres_db,
) -> Optional[tuple[str, bytes]]:
    """End-to-end: formulate (optional) → synthesize → ``(spoken_text, mp3)``.

    Returns the **spoken text** (the formulation-rewritten version actually
    sent to the TTS model, or the raw content when formulation was skipped)
    alongside the MP3 bytes, so the UI can show what was read aloud.

    Returns ``None`` when no TTS model is configured (the endpoint maps this to
    ``204``). Raises :class:`TtsSynthesisError` when a model *is* configured but
    synthesis fails, so the endpoint can answer ``502`` rather than silently
    no-op'ing. Errors during *formulation* remain non-fatal — synthesis falls
    back to the raw text in that case.
    """
    if not content or not content.strip():
        return None

    # Resolve user settings + API keys once. Both auxiliary and TTS draw
    # from the same pools.
    user_settings = await postgres_db.get_user_settings(user_id) or {}
    resolved_keys = await postgres_db.resolve_api_keys_for_job(
        user_id=user_id, project_id=None
    )

    tts_creds = await _resolve_capability_credentials(
        capability="tts",
        user_settings=user_settings,
        user_id=user_id,
        resolved_keys=resolved_keys,
        postgres_db=postgres_db,
    )
    if tts_creds is None:
        logger.info("No TTS model configured for user %s", user_id)
        return None
    tts_model, tts_base_url, tts_api_key = tts_creds
    voice = await _resolve_voice(tts_model, language, postgres_db)

    speech_input = content
    if reformulate and _needs_formulation(content):
        aux_creds = await _resolve_capability_credentials(
            capability="auxiliary",
            user_settings=user_settings,
            user_id=user_id,
            resolved_keys=resolved_keys,
            postgres_db=postgres_db,
        )
        if aux_creds is not None:
            aux_model, aux_base_url, aux_api_key = aux_creds
            speech_input = await _formulate_for_speech(
                content,
                model=aux_model,
                base_url=aux_base_url,
                api_key=aux_api_key,
            )
        else:
            logger.debug(
                "Reformulation requested but no auxiliary model configured; "
                "synthesizing raw content"
            )

    audio_key = _hash_key(tts_model, voice, speech_input)
    async with _audio_lock:
        cached = _cache_get(_audio_cache, audio_key)
    if cached is not None:
        logger.debug("TTS audio cache hit")
        return speech_input, cached

    audio = await _synthesize_speech(
        speech_input,
        model=tts_model,
        voice=voice,
        base_url=tts_base_url,
        api_key=tts_api_key,
    )
    if not audio:
        # Model is configured but synthesis produced nothing (missing key,
        # upstream error, timeout). Raise so the endpoint returns 502 — a 204
        # here would be indistinguishable from "TTS not configured" and the
        # button would silently do nothing.
        raise TtsSynthesisError(f"TTS synthesis failed for model {tts_model}")
    async with _audio_lock:
        _cache_put(_audio_cache, audio_key, audio, _AUDIO_CACHE_MAX)
    return speech_input, audio
