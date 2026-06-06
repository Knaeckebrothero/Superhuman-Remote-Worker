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
import json
import logging
import re
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
_PLAN_CACHE_MAX = 100

_audio_cache: "OrderedDict[str, bytes]" = OrderedDict()
_formulation_cache: "OrderedDict[str, str]" = OrderedDict()
_plan_cache: "OrderedDict[str, list[str]]" = OrderedDict()
_audio_lock = asyncio.Lock()
_formulation_lock = asyncio.Lock()
_plan_lock = asyncio.Lock()


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

# Long messages are read aloud as a sequence of separately-synthesized chunks.
# TTS_CHUNK_LIMIT is the API's hard input cap (OpenAI rejects >4096); the gate
# never exceeds it. TTS_CHUNK_TARGET is the size we actually aim chunks at, kept
# small because a CPU TTS model synthesizes at ~real-time (~38 chars/s measured
# for kokoro-cpu): ~1500 chars ≈ ~40 s of synthesis — fast first audio, inside
# the per-chunk timeout, and short enough to not trip the backend liveness probe.
# (Faster backends could use a larger target; backend-aware sizing is a TODO.)
TTS_CHUNK_LIMIT = 4096
TTS_CHUNK_TARGET = 1500

CHUNKING_SYSTEM_PROMPT = f"""You rewrite text so it sounds natural read aloud by a text-to-speech engine, AND split it into ordered chunks for sequential synthesis.

Rewrite rules:
1. Strip ALL markdown (asterisks, headers, code fences, link syntax).
2. Convert tables to descriptive sentences — never read tables cell-by-cell.
3. For code blocks: briefly describe what the code does in one sentence; never read syntax.
4. Convert bullet lists to flowing prose with words like "first", "next", "also".
5. Drop URLs (or say "a link").
6. Preserve the meaning, tone, technical terms, proper names, and ORDER. Do not summarize or omit content.
7. Keep numbers in a readable form.

Chunking rules:
8. Split the rewritten text into chunks of at most {TTS_CHUNK_TARGET} characters each (hard ceiling {TTS_CHUNK_LIMIT}).
9. Break ONLY at natural stopping points — the end of a section, paragraph, or complete thought — so each chunk ends on a natural pause and the audio never sounds cut off mid-idea. Never split mid-sentence.
10. Short input is a single chunk; long input becomes as many chunks as needed, in order.

Return ONLY a JSON array of strings (the chunks, in order), e.g. ["first chunk", "second chunk"]. No preamble, no commentary, no markdown fences."""


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
    timeout: float = 120.0,
) -> Optional[bytes]:
    """Call the TTS endpoint and return MP3 bytes.

    Timeout is generous (120 s) because a CPU TTS model synthesizes at
    ~real-time: a ~1500-char chunk is ~40 s, and we want headroom for a slower
    chunk or a loaded backend rather than a spurious failure.
    """
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


# ---------------------------------------------------------------------------
# Chunk planning — split a long message into ordered, speakable chunks so the
# UI can synthesize + play them one after another (no truncation, no single
# multi-minute request). The LLM picks natural breakpoints; code guarantees
# the 4096-char ceiling and never fully fails.
# ---------------------------------------------------------------------------


def _split_text_into_chunks(text: str, limit: int = TTS_CHUNK_LIMIT) -> list[str]:
    """Deterministically pack ``text`` into ``<=limit``-char chunks, breaking at
    paragraph then sentence boundaries. The fallback when the LLM can't chunk
    (unavailable, errored, truncated) and the hard backstop behind the gate.
    Always terminates; every returned chunk is ``<= limit``.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    # Break into atomic segments small enough to pack: paragraphs, then
    # sentences, then (last resort) hard slices of an over-long sentence.
    segments: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            segments.append(para)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= limit:
                segments.append(sentence)
            else:
                for i in range(0, len(sentence), limit):
                    segments.append(sentence[i : i + limit])

    chunks: list[str] = []
    current = ""
    for segment in segments:
        if not current:
            current = segment
        elif len(current) + 2 + len(segment) <= limit:
            current = f"{current}\n\n{segment}"
        else:
            chunks.append(current)
            current = segment
    if current:
        chunks.append(current)
    return chunks


def _enforce_chunk_limit(chunks: list[str], limit: int = TTS_CHUNK_LIMIT) -> list[str]:
    """Hard guarantee that every chunk is ``<= limit``. Any oversized chunk is
    deterministically re-split — the last resort after the LLM had its chance."""
    result: list[str] = []
    for chunk in chunks:
        chunk = (chunk or "").strip()
        if not chunk:
            continue
        if len(chunk) <= limit:
            result.append(chunk)
        else:
            result.extend(_split_text_into_chunks(chunk, limit))
    return result


def _parse_chunk_array(raw: str) -> Optional[list[str]]:
    """Parse the LLM's chunk output into a list of non-empty strings, tolerating
    code fences and surrounding prose. ``None`` when no usable array is found."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list):
        return None
    chunks = [
        str(item).strip()
        for item in parsed
        if isinstance(item, (str, int, float)) and str(item).strip()
    ]
    return chunks or None


async def _resplit_oversized(
    chunks: list[str],
    *,
    model: str,
    base_url: Optional[str],
    api_key: Optional[str],
    timeout: float = 60.0,
) -> list[str]:
    """Re-prompt the LLM to split any chunk that exceeds the limit at a natural
    break (one pass, in order). Anything still over is left for the code gate."""
    client = AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0
    )
    result: list[str] = []
    prompt = (
        f"Split the following text into a JSON array of chunks, each under "
        f"{TTS_CHUNK_TARGET} characters, breaking ONLY at natural stopping "
        f"points (end of a sentence or paragraph). Return ONLY the JSON array."
    )
    try:
        for chunk in chunks:
            if len(chunk) <= TTS_CHUNK_LIMIT:
                result.append(chunk)
                continue
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": chunk},
                    ],
                    temperature=0.3,
                )
                sub = _parse_chunk_array(response.choices[0].message.content or "")
                result.extend(sub if sub else [chunk])
            except Exception:
                logger.exception("TTS re-split call failed; deferring to code gate")
                result.append(chunk)
    finally:
        await client.close()
    return result


async def _llm_clean_and_chunk(
    text: str,
    *,
    model: str,
    base_url: Optional[str],
    api_key: Optional[str],
    timeout: float = 30.0,
) -> Optional[list[str]]:
    """LLM cleanup + natural chunking → ordered chunk list, or ``None`` to signal
    the caller should fall back to deterministic splitting.

    ``None`` on: no key, call error, truncated output (hit max tokens — the
    array would be incomplete), or unparseable output. Oversized chunks are
    re-prompted once; the caller's gate enforces the hard ceiling regardless.
    """
    if not api_key:
        return None
    client = AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CHUNKING_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
        )
        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            logger.warning(
                "TTS chunk plan truncated (max tokens); using deterministic split"
            )
            return None
        chunks = _parse_chunk_array(choice.message.content or "")
    except Exception:
        logger.exception(
            "TTS chunk-planning LLM call failed; using deterministic split"
        )
        return None
    finally:
        await client.close()

    if not chunks:
        return None
    if any(len(chunk) > TTS_CHUNK_LIMIT for chunk in chunks):
        chunks = await _resplit_oversized(
            chunks, model=model, base_url=base_url, api_key=api_key
        )
    return chunks


async def plan_tts_chunks(
    *,
    content: str,
    user_id: str,
    postgres_db,
) -> Optional[list[str]]:
    """Clean ``content`` for speech and split it into ordered ``<=4096``-char
    chunks at natural breakpoints, for sequential synthesis + playback.

    Returns ``None`` when no TTS model is configured (the endpoint maps this to
    ``204``). Otherwise always returns at least one chunk — the deterministic
    splitter guarantees it even when the LLM is unavailable or fails.
    """
    if not content or not content.strip():
        return None

    cache_key = _hash_key("plan", content)
    async with _plan_lock:
        cached = _cache_get(_plan_cache, cache_key)
    if cached is not None:
        logger.debug("TTS chunk-plan cache hit")
        return list(cached)

    user_settings = await postgres_db.get_user_settings(user_id) or {}
    resolved_keys = await postgres_db.resolve_api_keys_for_job(
        user_id=user_id, project_id=None
    )

    # A TTS model must be configured at all, so the endpoint can 204 cleanly
    # (the chunks are useless without something to synthesize them).
    tts_creds = await _resolve_capability_credentials(
        capability="tts",
        user_settings=user_settings,
        user_id=user_id,
        resolved_keys=resolved_keys,
        postgres_db=postgres_db,
    )
    if tts_creds is None:
        logger.info("No TTS model configured for user %s; cannot plan chunks", user_id)
        return None

    chunks: Optional[list[str]] = None
    aux_creds = await _resolve_capability_credentials(
        capability="auxiliary",
        user_settings=user_settings,
        user_id=user_id,
        resolved_keys=resolved_keys,
        postgres_db=postgres_db,
    )
    if aux_creds is not None:
        aux_model, aux_base_url, aux_api_key = aux_creds
        chunks = await _llm_clean_and_chunk(
            content, model=aux_model, base_url=aux_base_url, api_key=aux_api_key
        )

    # Fallback: deterministic split of the raw content (no cleanup, but always
    # playable) when there's no aux model or the LLM couldn't deliver. Pack to
    # the synthesis-sized target, not the 4096 ceiling, so each chunk stays
    # fast to synthesize.
    if not chunks:
        chunks = _split_text_into_chunks(content, TTS_CHUNK_TARGET)

    # Hard gate: nothing over the ceiling reaches synthesis.
    chunks = _enforce_chunk_limit(chunks)
    if not chunks:
        chunks = _split_text_into_chunks(content, TTS_CHUNK_TARGET) or [content.strip()]

    async with _plan_lock:
        _cache_put(_plan_cache, cache_key, list(chunks), _PLAN_CACHE_MAX)
    return chunks
