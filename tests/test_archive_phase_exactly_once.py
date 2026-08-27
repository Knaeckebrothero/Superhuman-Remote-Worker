"""archive_phase exactly-once guard (phase-tags doc, fix direction 4).

A rejected phase transition routes back to execute; the retried completion
re-enters archive_phase with the SAME phase instance. Before the guard that
re-archived an emptied todo list, re-marked the plan, re-snapshotted and
re-extracted memories for a boundary that already happened. The checkpointed
``last_archived_phase`` key makes the node exactly-once per
``<phase_number>:<type>`` instance and re-arms automatically when the phase
advances.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.graph import create_archive_phase_node


def _config():
    return SimpleNamespace(
        agent_id="test-agent",
        extra={},  # real dict — MagicMock here infinite-loops yaml paths
        auxiliary=SimpleNamespace(enabled=False, tasks={}),
        context_management=SimpleNamespace(
            compact_on_archive=False, max_summary_length=500
        ),
    )


def _node(todo_manager, plan_manager):
    return create_archive_phase_node(
        todo_manager=todo_manager,
        plan_manager=plan_manager,
        config=_config(),
        context_mgr=MagicMock(),
        auxiliary_llm=None,
        summarization_prompt="",
    )


def _state(phase_number=3, is_strategic=False, last_archived=None):
    state = {
        "job_id": "job-arch-1",
        "iteration": 7,
        "phase_number": phase_number,
        "is_strategic_phase": is_strategic,
        "messages": [],
    }
    if last_archived is not None:
        state["last_archived_phase"] = last_archived
    return state


class TestArchivePhaseExactlyOnce:
    @pytest.mark.asyncio
    async def test_first_archive_records_instance_key(self):
        todo_manager = MagicMock()
        todo_manager.archive.return_value = "archive/phase_3_p.yaml"
        plan_manager = MagicMock()
        plan_manager.get_current_phase.return_value = "Phase 3"

        result = await _node(todo_manager, plan_manager)(_state())

        assert result["last_archived_phase"] == "3:tactical"
        todo_manager.archive.assert_called_once()
        plan_manager.mark_phase_complete.assert_called_once_with("Phase 3")

    @pytest.mark.asyncio
    async def test_same_instance_skips_all_side_effects(self):
        """The transition-rejection retry must not archive the boundary twice."""
        todo_manager = MagicMock()
        plan_manager = MagicMock()

        result = await _node(todo_manager, plan_manager)(
            _state(last_archived="3:tactical")
        )

        assert result == {}
        todo_manager.archive.assert_not_called()
        plan_manager.get_current_phase.assert_not_called()
        plan_manager.mark_phase_complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_next_phase_rearms_the_guard(self):
        todo_manager = MagicMock()
        todo_manager.archive.return_value = "archive/phase_4_p.yaml"
        plan_manager = MagicMock()
        plan_manager.get_current_phase.return_value = "Phase 4"

        result = await _node(todo_manager, plan_manager)(
            _state(phase_number=4, is_strategic=True, last_archived="3:tactical")
        )

        assert result["last_archived_phase"] == "4:strategic"
        todo_manager.archive.assert_called_once()

    @pytest.mark.asyncio
    async def test_same_number_different_type_is_a_new_instance(self):
        """Phase N strategic and phase N tactical are distinct boundaries."""
        todo_manager = MagicMock()
        todo_manager.archive.return_value = "a"
        plan_manager = MagicMock()
        plan_manager.get_current_phase.return_value = "P"

        result = await _node(todo_manager, plan_manager)(
            _state(phase_number=3, is_strategic=True, last_archived="3:tactical")
        )

        assert result["last_archived_phase"] == "3:strategic"
        todo_manager.archive.assert_called_once()
