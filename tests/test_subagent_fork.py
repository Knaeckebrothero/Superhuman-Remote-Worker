"""``fork=true`` seeding (U3 WP1, plan B.7): a pure function over messages."""

from __future__ import annotations

from unittest.mock import patch

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from src.core.message_markers import PERSIST_ROLE_KEY
from src.core.workspace_injection import create_phase_instruction_message
from src.subagents import seed_fork_history
from src.subagents.fork import FORK_NOTICE


def _parent_history():
    return [
        SystemMessage(content="PARENT PROMPT"),
        HumanMessage(content="turn 1"),
        create_phase_instruction_message(
            "skills/x/SKILL.md", "phase body", "tactical", "2:tactical"
        ),
        AIMessage(
            content="",
            tool_calls=[
                {"id": "p1", "name": "list_files", "args": {}, "type": "tool_call"}
            ],
        ),
        ToolMessage(content="a.md", tool_call_id="p1"),
        AIMessage(content="one file"),
        RemoveMessage(id="msg_old"),
        ToolMessage(content="orphan result", tool_call_id="never_called"),
        HumanMessage(content="turn 2"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "p2_open",
                    "name": "read_file",
                    "args": {"path": "a"},
                    "type": "tool_call",
                }
            ],
        ),
    ]


class TestSeedForkHistory:
    def test_drops_system_prompt_protected_blocks_orphans_and_open_calls(self):
        seed = seed_fork_history(_parent_history())
        assert not any(isinstance(m, SystemMessage) for m in seed)
        assert not any("phase body" in str(m.content) for m in seed)
        assert not any(type(m).__name__ == "RemoveMessage" for m in seed)
        assert not any(
            isinstance(m, ToolMessage) and m.tool_call_id == "never_called"
            for m in seed
        )
        assert not any(
            isinstance(m, AIMessage)
            and m.tool_calls
            and m.tool_calls[0]["id"] == "p2_open"
            for m in seed
        )
        kinds = [type(m).__name__ for m in seed]
        assert kinds == [
            "HumanMessage",
            "AIMessage",
            "ToolMessage",
            "AIMessage",
            "HumanMessage",
            "HumanMessage",
        ]
        notice = seed[-1]
        assert notice.content == FORK_NOTICE
        assert notice.additional_kwargs[PERSIST_ROLE_KEY] == "event"
        assert "brief" in FORK_NOTICE

    def test_only_a_leading_system_message_is_dropped(self):
        history = [
            HumanMessage(content="hi"),
            SystemMessage(content="[Summary of prior work] x"),
            AIMessage(content="ok"),
        ]
        seed = seed_fork_history(history)
        assert isinstance(seed[1], SystemMessage)  # a mid-history summary survives

    def test_never_mutates_the_parents_list_or_messages(self):
        history = _parent_history()
        before = [(type(m), getattr(m, "id", None)) for m in history]
        seed = seed_fork_history(history)
        assert [(type(m), getattr(m, "id", None)) for m in history] == before
        assert all(s is not p for s in seed for p in history)

    def test_empty_history_is_just_the_notice(self):
        seed = seed_fork_history([])
        assert len(seed) == 1 and seed[0].content == FORK_NOTICE

    def test_sanitizes_across_model_families(self):
        history = [HumanMessage(content="hi"), AIMessage(content="ok")]
        with patch(
            "src.subagents.fork.sanitize_history_for_provider_boundary",
            wraps=lambda msgs, model: list(msgs),
        ) as spy:
            seed_fork_history(
                history, child_model="claude-sonnet-4-5", parent_model="gpt-4o"
            )
            assert spy.call_count == 1
            assert spy.call_args.args[1] == "claude-sonnet-4-5"
            spy.reset_mock()
            seed_fork_history(history, child_model="gpt-4o-mini", parent_model="gpt-4o")
            assert spy.call_count == 0
            seed_fork_history(history, child_model=None, parent_model="gpt-4o")
            assert spy.call_count == 0
