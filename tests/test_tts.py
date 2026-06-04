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
