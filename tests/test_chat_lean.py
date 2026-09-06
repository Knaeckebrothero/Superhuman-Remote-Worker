"""Lean chat-listing projection + MCP chat formatter context collapsing.

DB-free: ``_lean_chat_doc`` is a pure function over the wire doc, and the
formatter operates on plain dicts. The endpoint/reader plumbing around them is
exercised on a live stack (debug chat panel), not here.
"""

from __future__ import annotations

from orchestrator.database.audit_store import _lean_chat_doc
from shared.orch_surface.formatters import _format_chat_entry


def _doc() -> dict:
    long = "x" * 900
    return {
        "id": 7,
        "job_id": "j",
        "inputs": [
            {
                "type": "tool",
                "tool_call_id": "call_1",
                "content": long,
                "content_preview": long[:500] + "... [truncated]",
            },
            {"type": "human", "content": "short", "content_preview": "short"},
            {
                "type": "context",
                "kind": "todos",
                "hash": "abcd1234",
                "chars": 900,
                "content": long,
                "content_preview": long[:500] + "... [truncated]",
            },
        ],
        "response": {
            "content": long,
            "content_preview": long[:500] + "... [truncated]",
            "has_tool_calls": True,
            "tool_calls": [
                {
                    "id": "c1",
                    "name": "shell",
                    "args_preview": "a" * 200,
                    "args": "a" * 900,
                },
                {"id": "c2", "name": "read_file", "args_preview": "{'path': 'a.md'}"},
            ],
        },
        "reasoning": {
            "content": long,
            "content_preview": long[:500] + "... [truncated]",
        },
    }


class TestLeanChatDoc:
    def test_strips_bodies_keeps_previews(self):
        doc = _doc()
        lean = _lean_chat_doc(doc)

        tool_in = lean["inputs"][0]
        assert "content" not in tool_in
        assert tool_in["truncated"] is True
        assert tool_in["chars"] == 900
        assert tool_in["content_preview"].endswith("[truncated]")

        # Short elements pass through whole (no pointless truncated flag).
        assert lean["inputs"][1] == {
            "type": "human",
            "content": "short",
            "content_preview": "short",
        }

        # Context change-turn copies are stripped too (hydration restores them).
        ctx = lean["inputs"][2]
        assert "content" not in ctx and ctx["truncated"] is True

        assert "content" not in lean["response"]
        assert lean["response"]["truncated"] is True

        tcs = {t["id"]: t for t in lean["response"]["tool_calls"]}
        assert "args" not in tcs["c1"] and tcs["c1"]["args_truncated"] is True
        assert "args_truncated" not in tcs["c2"]

        assert "content" not in lean["reasoning"]

    def test_non_mutating(self):
        doc = _doc()
        _lean_chat_doc(doc)
        assert doc["inputs"][0]["content"] == "x" * 900
        assert doc["response"]["tool_calls"][0]["args"] == "a" * 900
        assert doc["reasoning"]["content"] == "x" * 900

    def test_tolerates_missing_sections(self):
        assert _lean_chat_doc({"id": 1}) == {"id": 1}
        assert _lean_chat_doc({"inputs": None, "response": "?"}) == {
            "inputs": None,
            "response": "?",
        }


class TestChatFormatterContextCollapse:
    def test_new_style_context_entries_collapse(self):
        entry = {
            "inputs": [
                {"type": "tool", "tool_call_id": "call_1", "content_preview": "result"},
                {"type": "context", "kind": "knowledge", "content_preview": "..."},
                {"type": "context", "kind": "todos", "content_preview": "..."},
            ],
            "response": {"content_preview": "ok", "tool_calls": []},
        }
        lines = _format_chat_entry(entry, 1)
        joined = "\n".join(lines)
        assert "[tool]: result" in joined
        assert "[context]: knowledge, todos" in joined
        # The raw injected block is not spelled out per entry.
        assert joined.count("[context]") == 1

    def test_legacy_rows_classify_by_markers(self):
        entry = {
            "inputs": [
                {
                    "type": "human",
                    "content_preview": "<active_tasks>\nCurrent Tasks — Phase 1",
                },
                {
                    "type": "tool",
                    "tool_call_id": "knowledge_inject_d449e62d",
                    "content_preview": "--- Project Knowledge ---",
                },
                {"type": "human", "content_preview": "real user turn"},
            ],
            "response": {"content_preview": "ok"},
        }
        lines = _format_chat_entry(entry, 2)
        joined = "\n".join(lines)
        assert "[context]: todos, knowledge" in joined
        assert "[human]: real user turn" in joined
        assert "<active_tasks>" not in joined
