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

from src.core.model_registry import (
    UnknownModelError,
    resolve_model as _resolve_model,
)

logger = logging.getLogger(__name__)

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


def _needs_formulation(text: str) -> bool:
    """Cheap markdown sniffer to skip the formulation LLM when not useful."""
    if not text or len(text) < _FORMULATION_MIN_LEN:
        return False
    return any(hint in text for hint in _MARKDOWN_HINTS)


async def _resolve_capability_credentials(
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

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
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

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
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
) -> Optional[bytes]:
    """End-to-end: formulate (optional) → synthesize → return MP3 bytes.

    Returns ``None`` when no TTS model is configured or synthesis fails.
    Errors during formulation are non-fatal — synthesis falls back to raw
    text in that case.
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
    voice = _voice_for_language(language)

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
        return cached

    audio = await _synthesize_speech(
        speech_input,
        model=tts_model,
        voice=voice,
        base_url=tts_base_url,
        api_key=tts_api_key,
    )
    if audio:
        async with _audio_lock:
            _cache_put(_audio_cache, audio_key, audio, _AUDIO_CACHE_MAX)
    return audio
