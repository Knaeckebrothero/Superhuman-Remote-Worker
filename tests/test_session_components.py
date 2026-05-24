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


from langchain_core.messages import AIMessage, ToolMessage
from src.llm.session_components import MessageComponents, normalize_response


def test_normalize_ai_message_extracts_four_components_and_raw():
    raw_response = {
        "object": "chat.completion",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Reading the file.",
                    "reasoning_content": "Let me read it.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "a.txt"}',
                            },
                        }
                    ],
                }
            }
        ],
    }
    msg = AIMessage(
        content="Reading the file.",
        tool_calls=[
            {
                "name": "read_file",
                "args": {"path": "a.txt"},
                "id": "call_1",
                "type": "tool_call",
            }
        ],
        additional_kwargs={"reasoning_content": "Let me read it."},
        response_metadata={"model_name": "gpt-5.5"},
    )
    comp = normalize_response(msg, provider="openai-chat", raw_output=raw_response)
    assert isinstance(comp, MessageComponents)
    assert comp.text == "Reading the file."
    assert comp.reasoning == "Let me read it."
    assert comp.tool_calls == [
        {
            "name": "read_file",
            "args": {"path": "a.txt"},
            "id": "call_1",
            "type": "tool_call",
        }
    ]
    assert comp.provider == "openai-chat"
    assert comp.provider_raw == raw_response
    assert comp.response_metadata == {"model_name": "gpt-5.5"}


def test_normalize_tool_message_becomes_tool_result_component():
    tm = ToolMessage(content="file contents", tool_call_id="call_1")
    comp = normalize_response(tm, provider="openai-chat", raw_output=None)
    assert comp.tool_results == [{"tool_call_id": "call_1", "content": "file contents"}]
    assert comp.text is None
    assert comp.tool_calls == []


import pytest
from src.llm.session_components import components_to_provider_messages


def test_rebuild_chat_completions_request_from_components():
    comps = [
        MessageComponents(role="human", text="read a.txt", provider="openai-chat"),
        MessageComponents(
            role="ai",
            text="ok",
            provider="openai-chat",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"path": "a.txt"},
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        ),
        MessageComponents(
            role="tool",
            provider="openai-chat",
            tool_results=[{"tool_call_id": "call_1", "content": "file body"}],
        ),
    ]
    out = components_to_provider_messages(comps, target_provider="openai-chat")
    assert out == [
        {"role": "user", "content": "read a.txt"},
        {
            "role": "assistant",
            "content": "ok",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "file body"},
    ]


def test_assistant_without_tool_calls_omits_key():
    comps = [MessageComponents(role="ai", text="hello", provider="openai-chat")]
    assert components_to_provider_messages(comps, target_provider="openai-chat") == [
        {"role": "assistant", "content": "hello"}
    ]


def test_non_cc_provider_replay_is_forward_compat():
    comps = [MessageComponents(role="ai", text="x", provider="anthropic")]
    with pytest.raises(NotImplementedError):
        components_to_provider_messages(comps, target_provider="anthropic")
