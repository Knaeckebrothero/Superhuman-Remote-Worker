"""Tests for the persistent-session speech-to-text path.

Covers the transcribe service (``orchestrator/services/transcribe.py``) and the
``POST /api/persistent/threads/{id}/transcribe`` endpoint wiring in main.py.

The service is tested in isolation by patching the shared credential resolver
and the ``AsyncOpenAI`` client (mirrors ``tests/test_audio_helper.py``). The
endpoint is checked for route registration and exercised by calling the handler
directly with mocked auth + service — the orchestrator main app is too heavy for
a full TestClient, matching ``tests/test_admin_providers_api.py``.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Allow importing the orchestrator main module (matches test_admin_providers_api.py).
_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))
os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")


def _mock_db() -> MagicMock:
    """A postgres_db double whose pool-reading methods are awaitable no-ops."""
    db = MagicMock()
    db.get_user_settings = AsyncMock(return_value={})
    db.resolve_api_keys_for_job = AsyncMock(return_value={})
    return db


def _mock_openai(create_return):
    """Build a (class, client) pair where audio.transcriptions.create resolves
    to ``create_return`` and close() is awaitable."""
    client = MagicMock()
    client.audio.transcriptions.create = AsyncMock(return_value=create_return)
    client.close = AsyncMock()
    cls = MagicMock(return_value=client)
    return cls, client


# ---------------------------------------------------------------------------
# Service: transcribe_thread_audio
# ---------------------------------------------------------------------------


class TestTranscribeService:
    @pytest.mark.asyncio
    async def test_returns_text_on_success(self):
        from services.transcribe import transcribe_thread_audio

        cls, client = _mock_openai("hello world")
        with (
            patch("services.transcribe.AsyncOpenAI", cls),
            patch(
                "services.transcribe.resolve_capability_credentials",
                AsyncMock(return_value=("whisper-1", None, "sk-key")),
            ),
        ):
            text = await transcribe_thread_audio(
                audio_bytes=b"\x00\x01\x02",
                filename="voice.webm",
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert text == "hello world"
        client.audio.transcriptions.create.assert_awaited_once()
        client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_none_when_no_model_configured(self):
        from services.transcribe import transcribe_thread_audio

        cls, _ = _mock_openai("unused")
        with (
            patch("services.transcribe.AsyncOpenAI", cls),
            patch(
                "services.transcribe.resolve_capability_credentials",
                AsyncMock(return_value=None),
            ),
        ):
            text = await transcribe_thread_audio(
                audio_bytes=b"\x00\x01",
                filename="voice.webm",
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert text is None
        cls.assert_not_called()  # no client built when STT is unconfigured

    @pytest.mark.asyncio
    async def test_none_when_no_api_key(self):
        from services.transcribe import transcribe_thread_audio

        cls, _ = _mock_openai("x")
        with (
            patch("services.transcribe.AsyncOpenAI", cls),
            patch(
                "services.transcribe.resolve_capability_credentials",
                AsyncMock(return_value=("whisper-1", None, None)),
            ),
        ):
            text = await transcribe_thread_audio(
                audio_bytes=b"\x00",
                filename="voice.webm",
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert text is None
        cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_on_empty_audio(self):
        from services.transcribe import transcribe_thread_audio

        text = await transcribe_thread_audio(
            audio_bytes=b"",
            filename="voice.webm",
            user_id="u1",
            postgres_db=_mock_db(),
        )
        assert text is None

    @pytest.mark.asyncio
    async def test_none_on_openai_error(self):
        from services.transcribe import transcribe_thread_audio

        client = MagicMock()
        client.audio.transcriptions.create = AsyncMock(side_effect=RuntimeError("boom"))
        client.close = AsyncMock()
        cls = MagicMock(return_value=client)
        with (
            patch("services.transcribe.AsyncOpenAI", cls),
            patch(
                "services.transcribe.resolve_capability_credentials",
                AsyncMock(return_value=("whisper-1", None, "sk-key")),
            ),
        ):
            text = await transcribe_thread_audio(
                audio_bytes=b"\x00\x01",
                filename="voice.webm",
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert text is None
        client.close.assert_awaited_once()  # cleaned up even on failure

    @pytest.mark.asyncio
    async def test_handles_object_response_with_text_attr(self):
        from services.transcribe import transcribe_thread_audio

        resp_obj = MagicMock()
        resp_obj.text = "  spoken words  "
        cls, _ = _mock_openai(resp_obj)  # not a str → service reads .text
        with (
            patch("services.transcribe.AsyncOpenAI", cls),
            patch(
                "services.transcribe.resolve_capability_credentials",
                AsyncMock(return_value=("whisper-1", None, "sk-key")),
            ),
        ):
            text = await transcribe_thread_audio(
                audio_bytes=b"\x00",
                filename="voice.webm",
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert text == "spoken words"

    @pytest.mark.asyncio
    async def test_unwraps_json_string_blob(self):
        """Non-compliant endpoints return a `{"text": ...}` JSON *string* instead
        of a parsed object — the JSON must be unwrapped, not leaked verbatim."""
        from services.transcribe import transcribe_thread_audio

        cls, _ = _mock_openai('{"text": "Hey there"}')
        with (
            patch("services.transcribe.AsyncOpenAI", cls),
            patch(
                "services.transcribe.resolve_capability_credentials",
                AsyncMock(return_value=("whisper-1", None, "sk-key")),
            ),
        ):
            text = await transcribe_thread_audio(
                audio_bytes=b"\x00",
                filename="voice.webm",
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert text == "Hey there"

    @pytest.mark.asyncio
    async def test_handles_dict_result(self):
        from services.transcribe import transcribe_thread_audio

        cls, _ = _mock_openai({"text": "Hi"})
        with (
            patch("services.transcribe.AsyncOpenAI", cls),
            patch(
                "services.transcribe.resolve_capability_credentials",
                AsyncMock(return_value=("whisper-1", None, "sk-key")),
            ),
        ):
            text = await transcribe_thread_audio(
                audio_bytes=b"\x00",
                filename="voice.webm",
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert text == "Hi"

    @pytest.mark.asyncio
    async def test_plain_string_passthrough(self):
        from services.transcribe import transcribe_thread_audio

        cls, _ = _mock_openai("Just plain words")
        with (
            patch("services.transcribe.AsyncOpenAI", cls),
            patch(
                "services.transcribe.resolve_capability_credentials",
                AsyncMock(return_value=("whisper-1", None, "sk-key")),
            ),
        ):
            text = await transcribe_thread_audio(
                audio_bytes=b"\x00",
                filename="voice.webm",
                user_id="u1",
                postgres_db=_mock_db(),
            )
        assert text == "Just plain words"


# ---------------------------------------------------------------------------
# Endpoint: POST /api/persistent/threads/{thread_id}/transcribe
# ---------------------------------------------------------------------------


def _upload(data: bytes):
    from fastapi import UploadFile

    return UploadFile(file=io.BytesIO(data), filename="voice.webm")


class TestTranscribeEndpoint:
    def test_route_is_registered(self):
        from main import app

        routes = {
            (m, getattr(r, "path", ""))
            for r in app.routes
            for m in (getattr(r, "methods", None) or set())
        }
        assert (
            "POST",
            "/api/persistent/threads/{thread_id}/transcribe",
        ) in routes

    @pytest.mark.asyncio
    async def test_returns_text(self):
        import main

        with (
            patch.object(
                main,
                "require_thread_owner",
                AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
            ),
            patch(
                "services.transcribe.transcribe_thread_audio",
                AsyncMock(return_value="hello world"),
            ),
        ):
            resp = await main.transcribe_thread_audio_endpoint(
                thread_id="t1", request=MagicMock(), audio=_upload(b"\x00\x01\x02")
            )
        assert resp.status_code == 200
        assert json.loads(resp.body) == {"text": "hello world"}

    @pytest.mark.asyncio
    async def test_204_when_unavailable(self):
        import main

        with (
            patch.object(
                main,
                "require_thread_owner",
                AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
            ),
            patch(
                "services.transcribe.transcribe_thread_audio",
                AsyncMock(return_value=None),
            ),
        ):
            resp = await main.transcribe_thread_audio_endpoint(
                thread_id="t1", request=MagicMock(), audio=_upload(b"\x00\x01")
            )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_400_on_empty_audio(self):
        import main
        from fastapi import HTTPException

        with patch.object(
            main,
            "require_thread_owner",
            AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
        ):
            with pytest.raises(HTTPException) as exc:
                await main.transcribe_thread_audio_endpoint(
                    thread_id="t1", request=MagicMock(), audio=_upload(b"")
                )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_413_when_too_large(self):
        import main
        from fastapi import HTTPException

        big = _upload(b"\x00" * (25 * 1024 * 1024 + 1))
        with patch.object(
            main,
            "require_thread_owner",
            AsyncMock(return_value=({"id": "u1"}, {"id": "t1"})),
        ):
            with pytest.raises(HTTPException) as exc:
                await main.transcribe_thread_audio_endpoint(
                    thread_id="t1", request=MagicMock(), audio=big
                )
        assert exc.value.status_code == 413
