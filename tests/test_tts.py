"""Tests for the persistent-chat text-to-speech path.

Covers the TTS service (``orchestrator/services/tts.py``) and the
``POST /api/persistent/threads/{id}/tts`` endpoint wiring in main.py.

Mirrors ``tests/test_transcribe.py``: the service is tested in isolation by
patching the shared credential resolver and ``AsyncOpenAI``; the endpoint is
exercised by calling the handler directly with mocked auth + service (the
orchestrator main app is too heavy for a full TestClient).
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Allow importing the orchestrator main module (matches test_transcribe.py).
_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))
os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")


@pytest.fixture(autouse=True)
def _clear_tts_caches():
    """The audio/formulation caches are module-level and persist across the
    process — clear them so tests don't bleed cache hits into one another."""
    from services import tts

    tts._audio_cache.clear()
    tts._formulation_cache.clear()
    tts._plan_cache.clear()
    yield


def _mock_db() -> MagicMock:
    """A postgres_db double whose pool-reading methods are awaitable no-ops."""
    db = MagicMock()
    db.get_user_settings = AsyncMock(return_value={})
    db.resolve_api_keys_for_job = AsyncMock(return_value={})
    db.resolve_catalog_model = AsyncMock(return_value=None)  # no per-model voice
    return db


def _mock_openai(*, speech=b"MP3", speech_error=None, chat_text="reworded"):
    """Build a (class, client) pair for the TTS paths.

    ``speech`` is returned as ``audio.speech.create(...).content``; pass
    ``speech_error`` to raise instead. ``chat_text`` backs the formulation
    ``chat.completions.create`` call.
    """
    client = MagicMock()
    if speech_error is not None:
        client.audio.speech.create = AsyncMock(side_effect=speech_error)
    else:
        client.audio.speech.create = AsyncMock(return_value=MagicMock(content=speech))
    msg = MagicMock()
    msg.content = chat_text
    client.chat.completions.create = AsyncMock(
        return_value=MagicMock(choices=[MagicMock(message=msg)])
    )
    client.close = AsyncMock()
    cls = MagicMock(return_value=client)
    return cls, client


def _mock_openai_chat(content: str, *, finish_reason: str = "stop"):
    """(class, client) whose chat.completions.create returns ``content`` with
    the given finish_reason — for the chunk-planner tests."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock(message=msg, finish_reason=finish_reason)
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=MagicMock(choices=[choice]))
    client.close = AsyncMock()
    cls = MagicMock(return_value=client)
    return cls, client


def _caps(tts=("kokoro-strix", None, "sk-key"), aux=("gemma-aux", None, "sk-key")):
    """AsyncMock for _resolve_capability_credentials keyed on capability."""

    def _resolve(*, capability, **_):
        return {"tts": tts, "auxiliary": aux}.get(capability)

    return AsyncMock(side_effect=_resolve)


# ---------------------------------------------------------------------------
# Service: generate_message_tts
# ---------------------------------------------------------------------------


class TestGenerateMessageTts:
    @pytest.mark.asyncio
    async def test_returns_text_and_audio_on_success(self):
        from services.tts import generate_message_tts

        cls, client = _mock_openai(speech=b"AUDIOBYTES")
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("tts-1", None, "sk-key")),
            ),
        ):
            result = await generate_message_tts(
                content="short clean text",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert result == ("short clean text", b"AUDIOBYTES")
        client.audio.speech.create.assert_awaited_once()
        client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_none_when_no_model_configured(self):
        from services.tts import generate_message_tts

        cls, _ = _mock_openai()
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=None),
            ),
        ):
            result = await generate_message_tts(
                content="hi there",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert result is None
        cls.assert_not_called()  # no client built when TTS is unconfigured

    @pytest.mark.asyncio
    async def test_none_on_empty_content(self):
        from services.tts import generate_message_tts

        result = await generate_message_tts(
            content="   ",
            language="en",
            reformulate=False,
            user_id="u1",
            postgres_db=_mock_db(),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_raises_when_synthesis_fails(self):
        """A configured model whose synthesis call errors must raise (→ 502),
        not silently return None (→ 204) — the whole point of the fix."""
        from services.tts import TtsSynthesisError, generate_message_tts

        cls, client = _mock_openai(speech_error=RuntimeError("upstream 503"))
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("tts-1", None, "sk-key")),
            ),
        ):
            with pytest.raises(TtsSynthesisError):
                await generate_message_tts(
                    content="some text to read",
                    language="en",
                    reformulate=False,
                    user_id="u1",
                    postgres_db=_mock_db(),
                )
        client.close.assert_awaited_once()  # cleaned up even on failure

    @pytest.mark.asyncio
    async def test_raises_when_no_api_key(self):
        """A configured model with no resolvable key is a real misconfig — it
        must raise (→ 502), not look like 'not configured' (→ 204)."""
        from services.tts import TtsSynthesisError, generate_message_tts

        cls, client = _mock_openai(speech=b"X")
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("tts-1", None, None)),  # no api_key
            ),
        ):
            with pytest.raises(TtsSynthesisError):
                await generate_message_tts(
                    content="text to speak",
                    language="en",
                    reformulate=False,
                    user_id="u1",
                    postgres_db=_mock_db(),
                )
        client.audio.speech.create.assert_not_awaited()  # never tried without a key

    @pytest.mark.asyncio
    async def test_reformulation_rewrites_spoken_text(self):
        """With reformulate=True and markdown present, the returned spoken text
        is the auxiliary-LLM rewrite, not the raw markdown."""
        from services.tts import generate_message_tts

        cls, client = _mock_openai(speech=b"AUDIO", chat_text="plain spoken prose")
        markdown = "Here is a table:\n\n| a | b |\n| - | - |\n| 1 | 2 |\n\nThat's all."
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("tts-1", None, "sk-key")),
            ),
        ):
            result = await generate_message_tts(
                content=markdown,
                language="en",
                reformulate=True,
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert result is not None
        spoken, audio = result
        assert spoken == "plain spoken prose"
        assert audio == b"AUDIO"
        client.chat.completions.create.assert_awaited_once()  # formulation ran

    @pytest.mark.asyncio
    async def test_uses_voice_from_params_json(self):
        """A TTS catalog model with params_json.voice uses that voice."""
        from services.tts import generate_message_tts

        cls, client = _mock_openai(speech=b"AUDIO")
        db = _mock_db()
        db.resolve_catalog_model = AsyncMock(
            return_value={"params_json": {"voice": "af_heart"}}
        )
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("kokoro-strix", None, "sk-key")),
            ),
        ):
            result = await generate_message_tts(
                content="short clean text",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=db,
            )
        assert result is not None
        assert client.audio.speech.create.call_args.kwargs["voice"] == "af_heart"

    @pytest.mark.asyncio
    async def test_falls_back_to_default_voice_without_params(self):
        """No params_json voice → the per-language default (alloy for en)."""
        from services.tts import generate_message_tts

        cls, client = _mock_openai(speech=b"AUDIO")
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("kokoro-strix", None, "sk-key")),
            ),
        ):
            await generate_message_tts(
                content="short clean text",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=_mock_db(),  # resolve_catalog_model → None
            )
        assert client.audio.speech.create.call_args.kwargs["voice"] == "alloy"


# ---------------------------------------------------------------------------
# Phase 3: content-language voice selection, user voice choice, instructions
# ---------------------------------------------------------------------------


class TestVoiceSelectionAndInstructions:
    def test_detect_language_english(self):
        from services.tts import _detect_language

        assert _detect_language("This is a plain English sentence about a cat.") == "en"

    def test_detect_language_german_umlaut(self):
        from services.tts import _detect_language

        assert _detect_language("Größe und Qualität sind wichtig.") == "de"

    def test_detect_language_german_function_words(self):
        from services.tts import _detect_language

        assert (
            _detect_language("Der Test ist nicht fertig und das ist ein Problem.")
            == "de"
        )

    @pytest.mark.asyncio
    async def test_voice_follows_content_language_not_request_hint(self):
        """A German message uses the German default voice even when the request's
        language hint says 'en' (voice follows content, fixing defect 5)."""
        from services.tts import DEFAULT_VOICE_DE, generate_message_tts

        cls, client = _mock_openai(speech=b"AUDIO")
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("gpt-4o-mini-tts", None, "sk-key")),
            ),
        ):
            await generate_message_tts(
                content="Das ist ein deutscher Satz mit Umlauten: schön und gut.",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert client.audio.speech.create.call_args.kwargs["voice"] == DEFAULT_VOICE_DE

    @pytest.mark.asyncio
    async def test_user_default_voice_overrides_catalog(self):
        from services.tts import generate_message_tts

        cls, client = _mock_openai(speech=b"AUDIO")
        db = _mock_db()
        db.get_user_settings = AsyncMock(return_value={"default_tts_voice": "shimmer"})
        db.resolve_catalog_model = AsyncMock(
            return_value={"params_json": {"voice": "af_heart"}}
        )
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("kokoro", None, "sk-key")),
            ),
        ):
            await generate_message_tts(
                content="short clean text",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=db,
            )
        assert client.audio.speech.create.call_args.kwargs["voice"] == "shimmer"

    @pytest.mark.asyncio
    async def test_per_language_voice_map(self):
        from services.tts import generate_message_tts

        cls, client = _mock_openai(speech=b"AUDIO")
        db = _mock_db()
        db.resolve_catalog_model = AsyncMock(
            return_value={"params_json": {"voices": {"en": "alloy", "de": "onyx"}}}
        )
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("gpt-4o-mini-tts", None, "sk-key")),
            ),
        ):
            await generate_message_tts(
                content="Der Satz ist auf Deutsch und schön.",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=db,
            )
        assert client.audio.speech.create.call_args.kwargs["voice"] == "onyx"

    @pytest.mark.asyncio
    async def test_instructions_passed_through_when_set(self):
        from services.tts import generate_message_tts

        cls, client = _mock_openai(speech=b"AUDIO")
        db = _mock_db()
        db.resolve_catalog_model = AsyncMock(
            return_value={
                "params_json": {"voice": "sage", "instructions": "warm and unhurried"}
            }
        )
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("gpt-4o-mini-tts", None, "sk-key")),
            ),
        ):
            await generate_message_tts(
                content="short clean text",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=db,
            )
        kwargs = client.audio.speech.create.call_args.kwargs
        assert kwargs["voice"] == "sage"
        assert kwargs["instructions"] == "warm and unhurried"

    @pytest.mark.asyncio
    async def test_no_instructions_kwarg_when_unset(self):
        """tts-1 / Kokoro reject an `instructions` param, so it must be omitted
        entirely (not sent as None) when no catalog instructions are set."""
        from services.tts import generate_message_tts

        cls, client = _mock_openai(speech=b"AUDIO")
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("tts-1", None, "sk-key")),
            ),
        ):
            await generate_message_tts(
                content="short clean text",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert "instructions" not in client.audio.speech.create.call_args.kwargs


# ---------------------------------------------------------------------------
# Endpoint: POST /api/persistent/threads/{thread_id}/tts
# ---------------------------------------------------------------------------


class TestTtsEndpoint:
    def test_route_is_registered(self):
        from main import app

        routes = {
            (m, getattr(r, "path", ""))
            for r in app.routes
            for m in (getattr(r, "methods", None) or set())
        }
        assert ("POST", "/api/persistent/threads/{thread_id}/tts") in routes

    @pytest.mark.asyncio
    async def test_returns_json_text_and_audio(self):
        import main

        with (
            patch.object(
                main,
                "require_thread_owner",
                AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
            ),
            patch(
                "services.tts.generate_message_tts",
                AsyncMock(return_value=("spoken words", b"\x00\x01\x02")),
            ),
        ):
            resp = await main.synthesize_thread_message_tts(
                thread_id="t1", request=MagicMock(), body={"content": "hello"}
            )
        assert resp.status_code == 200
        payload = json.loads(resp.body)
        assert payload["text"] == "spoken words"
        assert base64.b64decode(payload["audio"]) == b"\x00\x01\x02"

    @pytest.mark.asyncio
    async def test_204_when_not_configured(self):
        import main

        with (
            patch.object(
                main,
                "require_thread_owner",
                AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
            ),
            patch(
                "services.tts.generate_message_tts",
                AsyncMock(return_value=None),
            ),
        ):
            resp = await main.synthesize_thread_message_tts(
                thread_id="t1", request=MagicMock(), body={"content": "hello"}
            )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_502_on_synthesis_failure(self):
        import main
        from fastapi import HTTPException

        from services.tts import TtsSynthesisError

        with (
            patch.object(
                main,
                "require_thread_owner",
                AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
            ),
            patch(
                "services.tts.generate_message_tts",
                AsyncMock(side_effect=TtsSynthesisError("down")),
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await main.synthesize_thread_message_tts(
                    thread_id="t1", request=MagicMock(), body={"content": "hello"}
                )
        assert exc.value.status_code == 502

    @pytest.mark.asyncio
    async def test_400_on_empty_content(self):
        import main
        from fastapi import HTTPException

        with patch.object(
            main,
            "require_thread_owner",
            AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
        ):
            with pytest.raises(HTTPException) as exc:
                await main.synthesize_thread_message_tts(
                    thread_id="t1", request=MagicMock(), body={"content": "   "}
                )
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Chunk planning: splitter / gate / parser (pure) + plan_tts_chunks
# ---------------------------------------------------------------------------


class TestChunkSplitting:
    def test_short_text_is_one_chunk(self):
        from services.tts import _split_text_into_chunks

        assert _split_text_into_chunks("Just a short line.") == ["Just a short line."]

    def test_long_text_splits_under_limit(self):
        from services.tts import TTS_CHUNK_LIMIT, _split_text_into_chunks

        text = "\n\n".join(f"Paragraph number {i}. " * 30 for i in range(40))
        chunks = _split_text_into_chunks(text)
        assert len(chunks) > 1
        assert all(len(c) <= TTS_CHUNK_LIMIT for c in chunks)

    def test_enforce_limit_resplits_oversized(self):
        from services.tts import TTS_CHUNK_LIMIT, _enforce_chunk_limit

        out = _enforce_chunk_limit(["word " * 2000])  # ~10k, no breaks
        assert len(out) > 1
        assert all(len(c) <= TTS_CHUNK_LIMIT for c in out)

    def test_parse_plain_array(self):
        from services.tts import _parse_chunk_array

        assert _parse_chunk_array('["a", "b"]') == ["a", "b"]

    def test_parse_fenced_array(self):
        from services.tts import _parse_chunk_array

        assert _parse_chunk_array('```json\n["a", "b"]\n```') == ["a", "b"]

    def test_parse_array_with_prose(self):
        from services.tts import _parse_chunk_array

        assert _parse_chunk_array('Sure! ["a", "b"] hope that helps') == ["a", "b"]

    def test_parse_non_array_is_none(self):
        from services.tts import _parse_chunk_array

        assert _parse_chunk_array("I cannot do that") is None
        assert _parse_chunk_array("") is None


class TestPlanTtsChunks:
    @pytest.mark.asyncio
    async def test_none_when_no_tts_model(self):
        from services.tts import plan_tts_chunks

        with patch("services.tts._resolve_capability_credentials", _caps(tts=None)):
            result = await plan_tts_chunks(
                content="anything", user_id="u1", postgres_db=_mock_db()
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_uses_llm_chunks(self):
        from services.tts import plan_tts_chunks

        cls, _ = _mock_openai_chat('["First chunk.", "Second chunk."]')
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch("services.tts._resolve_capability_credentials", _caps()),
        ):
            result = await plan_tts_chunks(
                content="some markdown **message**",
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert result["chunks"] == ["First chunk.", "Second chunk."]
        assert result["rewritten"] is True

    @pytest.mark.asyncio
    async def test_falls_back_to_deterministic_without_aux(self):
        from services.tts import TTS_CHUNK_LIMIT, plan_tts_chunks

        long_text = "\n\n".join(f"Paragraph {i}. " * 40 for i in range(40))
        with patch("services.tts._resolve_capability_credentials", _caps(aux=None)):
            result = await plan_tts_chunks(
                content=long_text, user_id="u1", postgres_db=_mock_db()
            )
        assert result is not None and len(result["chunks"]) > 1
        assert all(len(c) <= TTS_CHUNK_LIMIT for c in result["chunks"])
        # No aux model ran → deterministic split of raw markdown.
        assert result["rewritten"] is False

    @pytest.mark.asyncio
    async def test_falls_back_when_llm_unparseable(self):
        from services.tts import plan_tts_chunks

        cls, _ = _mock_openai_chat("Sorry, I can't help with that")
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch("services.tts._resolve_capability_credentials", _caps()),
        ):
            result = await plan_tts_chunks(
                content="short message", user_id="u1", postgres_db=_mock_db()
            )
        assert result["chunks"] == ["short message"]
        # LLM ran but couldn't deliver → fell back → not a real rewrite.
        assert result["rewritten"] is False

    @pytest.mark.asyncio
    async def test_truncated_llm_output_falls_back(self):
        from services.tts import plan_tts_chunks

        cls, _ = _mock_openai_chat('["partial', finish_reason="length")
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch("services.tts._resolve_capability_credentials", _caps()),
        ):
            result = await plan_tts_chunks(
                content="short message", user_id="u1", postgres_db=_mock_db()
            )
        assert result["chunks"] == ["short message"]
        assert result["rewritten"] is False

    @pytest.mark.asyncio
    async def test_first_chunk_is_shortened_for_fast_ttfa(self):
        from services.tts import TTS_FIRST_CHUNK_TARGET, plan_tts_chunks

        # A single long paragraph of many sentences the LLM returns as one chunk;
        # the planner must split off a short first chunk for fast time-to-first-audio.
        one_big = " ".join(f"This is sentence number {i}." for i in range(80))
        cls, _ = _mock_openai_chat(json.dumps([one_big]))
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch("services.tts._resolve_capability_credentials", _caps()),
        ):
            result = await plan_tts_chunks(
                content=one_big, user_id="u1", postgres_db=_mock_db()
            )
        assert len(result["chunks"]) >= 2
        assert len(result["chunks"][0]) <= TTS_FIRST_CHUNK_TARGET
        # Split at a sentence boundary — no mid-sentence cut.
        assert result["chunks"][0].endswith(".")


# ---------------------------------------------------------------------------
# Endpoint: POST /api/persistent/threads/{thread_id}/tts/plan
# ---------------------------------------------------------------------------


class TestTtsPlanEndpoint:
    def test_route_is_registered(self):
        from main import app

        routes = {
            (m, getattr(r, "path", ""))
            for r in app.routes
            for m in (getattr(r, "methods", None) or set())
        }
        assert ("POST", "/api/persistent/threads/{thread_id}/tts/plan") in routes

    @pytest.mark.asyncio
    async def test_returns_chunks(self):
        import main

        with (
            patch.object(
                main,
                "require_thread_owner",
                AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
            ),
            patch(
                "services.tts.plan_tts_chunks",
                AsyncMock(
                    return_value={
                        "chunks": ["chunk one", "chunk two"],
                        "rewritten": True,
                    }
                ),
            ),
        ):
            resp = await main.plan_thread_message_tts(
                thread_id="t1", request=MagicMock(), body={"content": "long message"}
            )
        assert resp.status_code == 200
        assert json.loads(resp.body) == {
            "chunks": ["chunk one", "chunk two"],
            "rewritten": True,
        }

    @pytest.mark.asyncio
    async def test_204_when_not_configured(self):
        import main

        with (
            patch.object(
                main,
                "require_thread_owner",
                AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
            ),
            patch("services.tts.plan_tts_chunks", AsyncMock(return_value=None)),
        ):
            resp = await main.plan_thread_message_tts(
                thread_id="t1", request=MagicMock(), body={"content": "hello"}
            )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_400_on_empty_content(self):
        import main
        from fastapi import HTTPException

        with patch.object(
            main,
            "require_thread_owner",
            AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
        ):
            with pytest.raises(HTTPException) as exc:
                await main.plan_thread_message_tts(
                    thread_id="t1", request=MagicMock(), body={"content": ""}
                )
        assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Usage metering (rate-limiting v2): the direct-SDK path must feed usage_events.
# ---------------------------------------------------------------------------


def _mock_ledger(*, available=True):
    """A UsageLedger double that records events into an AsyncMock."""
    led = MagicMock()
    led.is_available = available
    led.record_events = AsyncMock(return_value=1)
    return led


class TestTtsMetering:
    @pytest.mark.asyncio
    async def test_records_tts_character_event(self):
        from services.tts import generate_message_tts

        cls, _ = _mock_openai(speech=b"AUDIO")
        led = _mock_ledger()
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("kokoro", None, "sk-key")),
            ),
        ):
            result = await generate_message_tts(
                content="hello there",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=_mock_db(),
                ledger=led,
                ref_id="t1",
            )
        assert result is not None
        led.record_events.assert_awaited_once()
        (events,), _ = led.record_events.call_args
        assert len(events) == 1
        ev = events[0]
        assert ev.category == "tts"
        assert ev.unit == "tts-character"
        assert ev.quantity == len("hello there")
        assert ev.resource == "kokoro"
        assert ev.user_id == "u1"
        assert ev.ref_id == "t1"
        assert ev.ref_kind == "thread"

    @pytest.mark.asyncio
    async def test_no_write_when_ledger_unavailable(self):
        from services.tts import generate_message_tts

        cls, _ = _mock_openai(speech=b"AUDIO")
        led = _mock_ledger(available=False)
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("kokoro", None, "sk-key")),
            ),
        ):
            result = await generate_message_tts(
                content="hello there",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=_mock_db(),
                ledger=led,
                ref_id="t1",
            )
        assert result is not None
        led.record_events.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_metering_failure_is_non_fatal(self):
        from services.tts import generate_message_tts

        cls, _ = _mock_openai(speech=b"AUDIO")
        led = _mock_ledger()
        led.record_events = AsyncMock(side_effect=RuntimeError("audit down"))
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch(
                "services.tts._resolve_capability_credentials",
                AsyncMock(return_value=("kokoro", None, "sk-key")),
            ),
        ):
            result = await generate_message_tts(
                content="hello there",
                language="en",
                reformulate=False,
                user_id="u1",
                postgres_db=_mock_db(),
                ledger=led,
                ref_id="t1",
            )
        # A ledger hiccup must never break playback.
        assert result == ("hello there", b"AUDIO")


# ---------------------------------------------------------------------------
# Endpoint: GET /api/voice/capabilities (kills the silent 204 up front)
# ---------------------------------------------------------------------------


class TestVoiceCapabilitiesEndpoint:
    def test_route_is_registered(self):
        from main import app

        routes = {
            (m, getattr(r, "path", ""))
            for r in app.routes
            for m in (getattr(r, "methods", None) or set())
        }
        assert ("GET", "/api/voice/capabilities") in routes

    @pytest.mark.asyncio
    async def test_available_from_user_setting(self):
        import main

        db = MagicMock()
        db.get_user_settings = AsyncMock(return_value={"default_tts_model": "kokoro"})
        db.resolve_default_for_capability = AsyncMock(return_value=None)
        with (
            patch.object(
                main, "require_approved_user", AsyncMock(return_value={"id": "u1"})
            ),
            patch.object(main, "postgres_db", db),
        ):
            result = await main.voice_capabilities(MagicMock())
        assert result == {"tts": True, "stt": False}

    @pytest.mark.asyncio
    async def test_available_from_system_default(self):
        import main

        db = MagicMock()
        db.get_user_settings = AsyncMock(return_value={})

        async def _resolve(cap):
            return "whisper-1" if cap == "whisper" else None

        db.resolve_default_for_capability = AsyncMock(side_effect=_resolve)
        with (
            patch.object(
                main, "require_approved_user", AsyncMock(return_value={"id": "u1"})
            ),
            patch.object(main, "postgres_db", db),
        ):
            result = await main.voice_capabilities(MagicMock())
        assert result == {"tts": False, "stt": True}


# ---------------------------------------------------------------------------
# Markdown-strip fallback: a timed-out/absent rewrite must still read cleanly
# (no "asterisk asterisk", no table pipes).
# ---------------------------------------------------------------------------


class TestStripMarkdownForSpeech:
    def test_strips_emphasis_and_headers(self):
        from services.tts import _strip_markdown_for_speech

        out = _strip_markdown_for_speech("# Title\n\nThis is **bold** and *italic*.")
        assert "*" not in out
        assert "#" not in out
        assert "bold" in out and "italic" in out

    def test_links_become_text_images_dropped(self):
        from services.tts import _strip_markdown_for_speech

        out = _strip_markdown_for_speech("See [the docs](http://x) and ![alt](y.png).")
        assert "the docs" in out
        assert "http://x" not in out and "y.png" not in out and "alt" not in out

    def test_table_becomes_sentences_no_pipes(self):
        from services.tts import _strip_markdown_for_speech

        md = "| Metal | Price |\n| --- | --- |\n| Neodymium | 155 |\n| Terbium | 1103 |"
        out = _strip_markdown_for_speech(md)
        assert "|" not in out
        assert "Neodymium, 155." in out
        assert "Terbium, 1103." in out
        assert "---" not in out

    def test_code_fence_becomes_placeholder(self):
        from services.tts import _strip_markdown_for_speech

        out = _strip_markdown_for_speech(
            "Before\n\n```python\nprint('hi')\n```\n\nAfter"
        )
        assert "```" not in out and "print" not in out
        assert "code snippet" in out
        assert "Before" in out and "After" in out

    def test_preserves_snake_case_identifiers(self):
        from services.tts import _strip_markdown_for_speech

        # Single underscores (identifiers) must survive — only emphasis is stripped.
        assert "user_id" in _strip_markdown_for_speech("The user_id column is a key.")


# ---------------------------------------------------------------------------
# Streaming chunk plan: sentinel parsing + stream_tts_chunks generator.
# ---------------------------------------------------------------------------


class TestDrainSentinels:
    def test_no_sentinel_all_remainder(self):
        from services.tts import _drain_sentinels

        assert _drain_sentinels("partial text") == ([], "partial text")

    def test_splits_complete_chunks_keeps_tail(self):
        from services.tts import _drain_sentinels

        chunks, rem = _drain_sentinels("First.[[BREAK]]Second.[[BREAK]]Third")
        assert chunks == ["First.", "Second."]
        assert rem == "Third"

    def test_partial_sentinel_stays_in_remainder(self):
        from services.tts import _drain_sentinels

        chunks, rem = _drain_sentinels("First.[[BR")
        assert chunks == []
        assert rem == "First.[[BR"


class _FakeStream:
    """Async-iterable OpenAI streaming double: yields content-delta events for
    each string in ``pieces``, then a final usage-only event."""

    def __init__(self, pieces, usage=None):
        self._events = [
            MagicMock(choices=[MagicMock(delta=MagicMock(content=p))], usage=None)
            for p in pieces
        ]
        self._events.append(MagicMock(choices=[], usage=usage))

    def __aiter__(self):
        self._it = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def _mock_openai_stream(pieces, usage=None):
    """(class, client) whose chat.completions.create(stream=True) yields ``pieces``."""
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=_FakeStream(pieces, usage))
    client.close = AsyncMock()
    return MagicMock(return_value=client), client


async def _collect(agen):
    return [ev async for ev in agen]


class TestStreamTtsChunks:
    @pytest.mark.asyncio
    async def test_unavailable_without_tts_model(self):
        from services.tts import stream_tts_chunks

        with patch("services.tts._resolve_capability_credentials", _caps(tts=None)):
            events = await _collect(
                stream_tts_chunks(content="hello", user_id="u1", postgres_db=_mock_db())
            )
        assert events == [{"type": "unavailable"}]

    @pytest.mark.asyncio
    async def test_streams_chunks_split_on_sentinel(self):
        from services.tts import stream_tts_chunks

        # Deltas deliberately split a sentinel across the boundary.
        cls, _ = _mock_openai_stream(
            ["First chunk.[[BR", "EAK]]Second chunk.[[BREAK]]Third chunk."]
        )
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch("services.tts._resolve_capability_credentials", _caps()),
        ):
            events = await _collect(
                stream_tts_chunks(
                    content="some **markdown** message",
                    user_id="u1",
                    postgres_db=_mock_db(),
                )
            )
        chunks = [e for e in events if e["type"] == "chunk"]
        assert [c["text"] for c in chunks] == [
            "First chunk.",
            "Second chunk.",
            "Third chunk.",
        ]
        assert all(c["rewritten"] is True for c in chunks)
        assert events[-1] == {"type": "done", "total": 3, "rewritten": True}

    @pytest.mark.asyncio
    async def test_size_flush_without_sentinel(self):
        """The real-world case: the model rewrites well but ignores the sentinel
        and streams one blob. The parser must still chunk incrementally by size at
        sentence boundaries — a short first chunk, then target-sized chunks."""
        from services.tts import TTS_FIRST_CHUNK_TARGET, stream_tts_chunks

        pieces = [f"This is sentence number {i}. " for i in range(120)]  # ~3.3k chars
        cls, _ = _mock_openai_stream(pieces)
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch("services.tts._resolve_capability_credentials", _caps()),
        ):
            events = await _collect(
                stream_tts_chunks(content="long", user_id="u1", postgres_db=_mock_db())
            )
        chunks = [e for e in events if e["type"] == "chunk"]
        assert len(chunks) >= 2  # chunked despite no sentinel
        assert len(chunks[0]["text"]) <= TTS_FIRST_CHUNK_TARGET  # short first chunk
        assert chunks[0]["text"].endswith(".")  # cut at a sentence boundary
        assert all(c["rewritten"] is True for c in chunks)

    @pytest.mark.asyncio
    async def test_fallback_without_aux_strips_markdown(self):
        from services.tts import stream_tts_chunks

        with patch("services.tts._resolve_capability_credentials", _caps(aux=None)):
            events = await _collect(
                stream_tts_chunks(
                    content="Here is **bold** text with a | pipe | table.",
                    user_id="u1",
                    postgres_db=_mock_db(),
                )
            )
        chunks = [e for e in events if e["type"] == "chunk"]
        assert chunks and all(c["rewritten"] is False for c in chunks)
        joined = " ".join(c["text"] for c in chunks)
        assert "*" not in joined and "|" not in joined
        assert events[-1]["type"] == "done" and events[-1]["rewritten"] is False

    @pytest.mark.asyncio
    async def test_empty_stream_falls_back(self):
        from services.tts import stream_tts_chunks

        cls, _ = _mock_openai_stream([""])  # model produced nothing usable
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch("services.tts._resolve_capability_credentials", _caps()),
        ):
            events = await _collect(
                stream_tts_chunks(
                    content="short message", user_id="u1", postgres_db=_mock_db()
                )
            )
        chunks = [e for e in events if e["type"] == "chunk"]
        assert [c["text"] for c in chunks] == ["short message"]
        assert all(c["rewritten"] is False for c in chunks)

    @pytest.mark.asyncio
    async def test_meters_stream_usage(self):
        from services.tts import stream_tts_chunks

        usage = MagicMock(prompt_tokens=11, completion_tokens=22)
        cls, _ = _mock_openai_stream(["One.[[BREAK]]Two."], usage=usage)
        led = _mock_ledger()
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch("services.tts._resolve_capability_credentials", _caps()),
        ):
            await _collect(
                stream_tts_chunks(
                    content="hi",
                    user_id="u1",
                    postgres_db=_mock_db(),
                    ledger=led,
                    ref_id="t1",
                )
            )
        led.record_events.assert_awaited()
        (events,), _ = led.record_events.call_args
        assert {e.unit for e in events} == {"prompt-token", "completion-token"}
        assert all(e.details["stage"] == "chunk-stream" for e in events)


class TestPlanRawMode:
    @pytest.mark.asyncio
    async def test_reformulate_false_skips_aux_and_strips_markdown(self):
        from services.tts import plan_tts_chunks

        cls, _ = _mock_openai_chat('["should not be used"]')
        with (
            patch("services.tts.AsyncOpenAI", cls),
            patch("services.tts._resolve_capability_credentials", _caps()),
        ):
            result = await plan_tts_chunks(
                content="This is **bold** and has a | table | row.",
                user_id="u1",
                postgres_db=_mock_db(),
                reformulate=False,
            )
        cls.assert_not_called()  # aux LLM never invoked in raw mode
        assert result["rewritten"] is False
        joined = " ".join(result["chunks"])
        assert "*" not in joined and "|" not in joined


class TestTtsPlanStreamEndpoint:
    def test_route_is_registered(self):
        from main import app

        routes = {
            (m, getattr(r, "path", ""))
            for r in app.routes
            for m in (getattr(r, "methods", None) or set())
        }
        assert (
            "POST",
            "/api/persistent/threads/{thread_id}/tts/plan/stream",
        ) in routes

    @pytest.mark.asyncio
    async def test_emits_sse_frames(self):
        """The handler must wrap the service generator into the house SSE wire
        format: a kickstart comment, `event: chunk` frames, then `event: done`."""
        import main

        async def _fake_stream(**_):
            yield {"type": "chunk", "index": 0, "text": "Hello.", "rewritten": True}
            yield {"type": "chunk", "index": 1, "text": "World.", "rewritten": True}
            yield {"type": "done", "total": 2, "rewritten": True}

        with (
            patch.object(
                main,
                "require_thread_owner",
                AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
            ),
            patch("services.tts.stream_tts_chunks", _fake_stream),
        ):
            resp = await main.stream_thread_message_tts_plan(
                thread_id="t1", request=MagicMock(), body={"content": "hi"}
            )
            assert resp.media_type == "text/event-stream"
            frames = "".join([chunk async for chunk in resp.body_iterator])

        assert frames.startswith(": open")  # kickstart comment
        assert frames.count("event: chunk") == 2
        assert '"index": 0' in frames and '"text": "Hello."' in frames
        assert "event: done" in frames and '"total": 2' in frames

    @pytest.mark.asyncio
    async def test_maps_unavailable_event(self):
        import main

        async def _fake_stream(**_):
            yield {"type": "unavailable"}

        with (
            patch.object(
                main,
                "require_thread_owner",
                AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
            ),
            patch("services.tts.stream_tts_chunks", _fake_stream),
        ):
            resp = await main.stream_thread_message_tts_plan(
                thread_id="t1", request=MagicMock(), body={"content": "hi"}
            )
            frames = "".join([chunk async for chunk in resp.body_iterator])
        assert "event: unavailable" in frames
        assert "event: chunk" not in frames

    @pytest.mark.asyncio
    async def test_400_on_empty_content(self):
        import main
        from fastapi import HTTPException

        with patch.object(
            main,
            "require_thread_owner",
            AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
        ):
            with pytest.raises(HTTPException) as exc:
                await main.stream_thread_message_tts_plan(
                    thread_id="t1", request=MagicMock(), body={"content": "  "}
                )
        assert exc.value.status_code == 400
