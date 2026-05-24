"""Tests for src/llm/session_components.py (Plan 1 — persistent session component store).

Task 1 (this file's first test) pins the REAL shape the codex proxy returns for
gpt-5.5 so the adapter is built against reality, not an assumed API. See
docs/features/persistent_session_source_of_truth.md and
docs/superpowers/plans/2026-05-24-persistent-session-component-store.md.
"""

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "provider_responses"


def test_codex_proxy_returns_chat_completions_shape():
    """The live gpt-5.5 path is OpenAI Chat Completions (NOT the Responses API):
    reasoning is a flat `reasoning_content` field (may be null/hidden), tool calls
    use the standard CC shape. No reasoning↔function_call pairing constraint here."""
    raw = json.loads(
        (FIXTURE_DIR / "openai_chat_completions_toolcall.json").read_text()
    )
    assert raw["object"] == "chat.completion"  # not "response" (Responses API)
    msg = raw["choices"][0]["message"]
    # Reasoning channel is a flat field, not ordered Responses items.
    assert "reasoning_content" in msg
    assert "output" not in raw  # no Responses-API output[] array
    # Tool calls use the standard Chat Completions shape.
    tc = msg["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    assert isinstance(tc["function"]["arguments"], str)  # arguments is a JSON string
