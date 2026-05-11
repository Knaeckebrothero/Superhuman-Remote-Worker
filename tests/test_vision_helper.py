"""Regression-guard tests for src/services/vision_helper.py.

These do not exercise a real network call. Instead they monkeypatch the
underlying `AsyncOpenAI` client to capture the request body and verify
that the chat-completions payload matches the documented multimodal
schema.

The point is to lock in the wire shape so future refactors of the
shared `image_content` utility cannot silently change what `vision_helper`
sends to its side-channel vision model.
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.vision_helper import VisionHelper


# =============================================================================
# Helpers
# =============================================================================


def _make_helper_with_capture():
    """Return (helper, captured_calls) where captured_calls is a list of
    the kwargs that were passed to ``client.chat.completions.create``.

    The OpenAI client is fully mocked — no network access.
    """
    helper = VisionHelper.__new__(VisionHelper)
    helper.model = "test-vision-model"
    helper.timeout = 60

    captured_calls: list[dict] = []

    async def _fake_create(**kwargs):
        captured_calls.append(kwargs)
        # Build a minimal response object the helper can read.
        response = MagicMock()
        choice = MagicMock()
        choice.message.content = "fake description"
        response.choices = [choice]
        return response

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(side_effect=_fake_create)
    helper.client = fake_client

    # The archiver path is fire-and-forget; stub it so we don't touch
    # any orchestrator/database modules.
    helper._archive_vision_call = MagicMock()

    return helper, captured_calls


# =============================================================================
# Tests
# =============================================================================


class TestVisionHelperContentConstruction:
    def test_describe_image_uses_shared_content_block_shape(self):
        helper, captured = _make_helper_with_capture()
        bytes_in = b"\x89PNG\r\n\x1a\n_fake_png_body_"
        b64_in = base64.b64encode(bytes_in).decode()

        result = asyncio.run(
            helper.describe_image(image_data=bytes_in, mime_type="image/png")
        )

        assert result == "fake description"
        assert len(captured) == 1
        kwargs = captured[0]
        assert kwargs["model"] == "test-vision-model"
        msg = kwargs["messages"][0]
        assert msg["role"] == "user"
        # First content part: text prompt
        assert msg["content"][0]["type"] == "text"
        # Second content part: image_url block built by shared utility
        assert msg["content"][1] == {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64_in}"},
        }

    def test_describe_image_accepts_already_b64_input(self):
        """Caller may pass a pre-encoded base64 string instead of bytes."""
        helper, captured = _make_helper_with_capture()
        b64_in = base64.b64encode(b"already-encoded").decode()

        asyncio.run(helper.describe_image(image_data=b64_in, mime_type="image/jpeg"))

        msg = captured[0]["messages"][0]
        assert msg["content"][1] == {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64_in}"},
        }

    def test_describe_document_page_uses_shared_content_block_shape(self):
        helper, captured = _make_helper_with_capture()
        bytes_in = b"\x89PNG\r\n\x1a\nfake-page-body"
        b64_in = base64.b64encode(bytes_in).decode()

        result = asyncio.run(
            helper.describe_document_page(
                page_image=bytes_in,
                page_num=4,
                mime_type="image/png",
            )
        )

        assert result == "fake description"
        msg = captured[0]["messages"][0]
        assert msg["content"][0]["type"] == "text"
        assert "page" in msg["content"][0]["text"].lower()
        assert msg["content"][1] == {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64_in}"},
        }


@pytest.mark.parametrize(
    "mime",
    ["image/png", "image/jpeg", "image/webp"],
)
def test_data_url_mime_type_propagates(mime):
    helper, captured = _make_helper_with_capture()
    asyncio.run(helper.describe_image(image_data=b"abcd", mime_type=mime))
    url = captured[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith(f"data:{mime};base64,")
