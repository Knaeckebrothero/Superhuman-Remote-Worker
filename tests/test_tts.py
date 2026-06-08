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
        assert result == ["First chunk.", "Second chunk."]

    @pytest.mark.asyncio
    async def test_falls_back_to_deterministic_without_aux(self):
        from services.tts import TTS_CHUNK_LIMIT, plan_tts_chunks

        long_text = "\n\n".join(f"Paragraph {i}. " * 40 for i in range(40))
        with patch("services.tts._resolve_capability_credentials", _caps(aux=None)):
            result = await plan_tts_chunks(
                content=long_text, user_id="u1", postgres_db=_mock_db()
            )
        assert result is not None and len(result) > 1
        assert all(len(c) <= TTS_CHUNK_LIMIT for c in result)

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
        assert result == ["short message"]

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
        assert result == ["short message"]


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
                AsyncMock(return_value=["chunk one", "chunk two"]),
            ),
        ):
            resp = await main.plan_thread_message_tts(
                thread_id="t1", request=MagicMock(), body={"content": "long message"}
            )
        assert resp.status_code == 200
        assert json.loads(resp.body) == {"chunks": ["chunk one", "chunk two"]}

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
