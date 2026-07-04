"""Tests for Phase 1c drain-intent reaction.

Covers:
  - persistent_app._handle_heartbeat_intents — drain → clean suspend when
    parked, defer while a turn is in flight, plain exit with no session
    (docs/issues/session_agent_drift_drain_kills_idle_sessions.md option b)
  - dual_app._handle_heartbeat_intents — idle exit + busy flag
  - completion.determine_job_status — version_upgrade → paused
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# Persistent agent — drain intent: suspend when parked, defer when busy
# =============================================================================


_PERSISTENT_GLOBALS = (
    "_drain_intent_handled",
    "_drain_deferred_logged",
    "_session",
    "_thread_id",
    "_awaiting_input",
    "_tool_inflight",
    "_loop_user_queue",
    "_loop_task",
    "_orchestrator_client",
    "_terminating",
    "_max_sessions_per_process",
)


class TestPersistentDrainHandler:
    @pytest.fixture(autouse=True)
    def _isolate_module_state(self):
        """Snapshot + restore persistent_app globals around every test."""
        from src.api import persistent_app

        saved = {name: getattr(persistent_app, name) for name in _PERSISTENT_GLOBALS}
        persistent_app._drain_intent_handled = False
        persistent_app._drain_deferred_logged = False
        persistent_app._session = None
        persistent_app._thread_id = None
        persistent_app._awaiting_input = False
        persistent_app._tool_inflight = False
        persistent_app._loop_user_queue = None
        persistent_app._orchestrator_client = None
        yield
        for name, value in saved.items():
            setattr(persistent_app, name, value)

    def _attach_parked_session(self, persistent_app):
        """Simulate an attached session parked between turns."""
        persistent_app._session = MagicMock()
        persistent_app._thread_id = "tid-drain-1"
        persistent_app._awaiting_input = True
        persistent_app._tool_inflight = False
        persistent_app._loop_user_queue = None
        client = AsyncMock()
        client.suspend_thread = AsyncMock(return_value=True)
        persistent_app._orchestrator_client = client
        return client

    @pytest.mark.asyncio
    async def test_no_session_exits_without_detach(self):
        from src.api import persistent_app

        with (
            patch.object(
                persistent_app, "_terminate_session", new=AsyncMock()
            ) as detach,
            patch.object(persistent_app, "_schedule_exit") as exit_,
        ):
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True, "drain_reason": "stale_image"}}
            )
        detach.assert_not_awaited()
        exit_.assert_called_once()
        assert persistent_app._drain_intent_handled is True

    @pytest.mark.asyncio
    async def test_parked_session_drain_suspends(self):
        from src.api import persistent_app

        client = self._attach_parked_session(persistent_app)

        with (
            patch.object(
                persistent_app, "_terminate_session", new=AsyncMock()
            ) as detach,
            patch.object(persistent_app, "_broadcast") as broadcast,
            patch.object(persistent_app, "_schedule_exit") as exit_,
        ):
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True, "drain_reason": "stale_image"}}
            )

        # Clean suspend: teardown WITHOUT the 'ended' write, orchestrator
        # suspend confirmed, no fallback status write, pod exit scheduled.
        detach.assert_awaited_once_with("drain", mark_thread=False)
        client.suspend_thread.assert_awaited_once_with("tid-drain-1")
        client.update_thread_status.assert_not_awaited()
        exit_.assert_called_once()
        assert broadcast.call_args[0][0] == "session.suspended"
        assert persistent_app._drain_intent_handled is True

    @pytest.mark.asyncio
    async def test_suspend_failure_falls_back_to_ended(self):
        from src.api import persistent_app

        client = self._attach_parked_session(persistent_app)
        client.suspend_thread = AsyncMock(return_value=False)

        with (
            patch.object(persistent_app, "_terminate_session", new=AsyncMock()),
            patch.object(persistent_app, "_broadcast"),
            patch.object(persistent_app, "_schedule_exit") as exit_,
        ):
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True}}
            )
        # Fallback goes through the client with the captured thread_id —
        # the module-global _thread_id is already cleared by teardown.
        client.update_thread_status.assert_awaited_once_with("tid-drain-1", "ended")
        exit_.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_in_flight_defers(self):
        from src.api import persistent_app

        self._attach_parked_session(persistent_app)
        persistent_app._awaiting_input = False  # loop not parked: mid-turn

        with (
            patch.object(
                persistent_app, "_terminate_session", new=AsyncMock()
            ) as detach,
            patch.object(persistent_app, "_schedule_exit") as exit_,
        ):
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True}}
            )
        detach.assert_not_awaited()
        exit_.assert_not_called()
        # NOT handled — the next heartbeat tick re-checks.
        assert persistent_app._drain_intent_handled is False
        assert persistent_app._drain_deferred_logged is True

    @pytest.mark.asyncio
    async def test_tool_inflight_defers(self):
        from src.api import persistent_app

        self._attach_parked_session(persistent_app)
        persistent_app._tool_inflight = True

        with patch.object(persistent_app, "_schedule_exit") as exit_:
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True}}
            )
        exit_.assert_not_called()
        assert persistent_app._drain_intent_handled is False

    @pytest.mark.asyncio
    async def test_queued_input_defers(self):
        from src.api import persistent_app

        self._attach_parked_session(persistent_app)
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait("pending user message")
        persistent_app._loop_user_queue = queue

        with patch.object(persistent_app, "_schedule_exit") as exit_:
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True}}
            )
        exit_.assert_not_called()
        assert persistent_app._drain_intent_handled is False

    @pytest.mark.asyncio
    async def test_deferred_drain_fires_once_loop_parks(self):
        from src.api import persistent_app

        client = self._attach_parked_session(persistent_app)
        persistent_app._awaiting_input = False  # busy on the first tick

        with (
            patch.object(persistent_app, "_terminate_session", new=AsyncMock()),
            patch.object(persistent_app, "_update_thread_status", new=AsyncMock()),
            patch.object(persistent_app, "_broadcast"),
            patch.object(persistent_app, "_schedule_exit") as exit_,
        ):
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True}}
            )
            assert persistent_app._drain_intent_handled is False

            persistent_app._awaiting_input = True  # loop parked
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True}}
            )
        client.suspend_thread.assert_awaited_once()
        exit_.assert_called_once()
        assert persistent_app._drain_intent_handled is True

    @pytest.mark.asyncio
    async def test_subsequent_calls_are_idempotent(self):
        from src.api import persistent_app

        client = self._attach_parked_session(persistent_app)

        with (
            patch.object(
                persistent_app, "_terminate_session", new=AsyncMock()
            ) as detach,
            patch.object(persistent_app, "_update_thread_status", new=AsyncMock()),
            patch.object(persistent_app, "_broadcast"),
            patch.object(persistent_app, "_schedule_exit") as exit_,
        ):
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True}}
            )
            # Second call must not suspend/exit again.
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True}}
            )
        assert detach.await_count == 1
        assert client.suspend_thread.await_count == 1
        assert exit_.call_count == 1

    @pytest.mark.asyncio
    async def test_loop_complete_reentry_skipped_during_drain_teardown(self):
        """Reproduces the live race from the 2026-06-10 k3d verification.

        Cancelling the loop task makes run_persistent_loop return CLEANLY
        (it swallows CancelledError in the input wait), so the completion
        handler re-enters _terminate_session("loop_complete") mid-teardown.
        Without the _terminating guard the inner call writes 'ended' and
        defeats the orchestrator's 'suspended' transition.
        """
        from src.api import persistent_app

        session = MagicMock()
        session.workspace_sync = None
        session.workspace_manager = None
        session.cleanup = AsyncMock()
        persistent_app._session = session
        persistent_app._thread_id = "tid-reentry-1"
        persistent_app._terminating = False
        persistent_app._max_sessions_per_process = 0

        status_writes: list[str] = []

        async def fake_update(status):
            status_writes.append(status)

        # A loop task that mimics run_persistent_loop's cancellation
        # swallowing: on cancel it re-enters _terminate_session cleanly,
        # exactly like _loop_completion_handler's else-branch does.
        async def fake_loop():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                pass
            await persistent_app._terminate_session("loop_complete")

        loop_task = asyncio.create_task(fake_loop())
        await asyncio.sleep(0)  # let it park in the sleep
        persistent_app._loop_task = loop_task

        with (
            patch.object(persistent_app, "_update_thread_status", new=fake_update),
            patch.object(persistent_app, "_stop_watchdogs"),
        ):
            await persistent_app._terminate_session("drain", mark_thread=False)

        # The re-entrant loop_complete call must NOT have written 'ended'.
        assert status_writes == []
        assert persistent_app._session is None
        assert persistent_app._terminating is False

    @pytest.mark.asyncio
    async def test_no_intents_no_action(self):
        from src.api import persistent_app

        with (
            patch.object(
                persistent_app, "_terminate_session", new=AsyncMock()
            ) as detach,
            patch.object(persistent_app, "_schedule_exit") as exit_,
        ):
            # Empty / missing / explicit-false should all be no-ops.
            await persistent_app._handle_heartbeat_intents({})
            await persistent_app._handle_heartbeat_intents({"intents": {}})
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": False}}
            )
        detach.assert_not_awaited()
        exit_.assert_not_called()
        assert persistent_app._drain_intent_handled is False


# =============================================================================
# Dual-mode (worker) — idle exit, busy flag
# =============================================================================


class TestDualDrainHandler:
    def _reset(self, dual_app):
        dual_app._drain_intent_received = False
        dual_app._drain_intent_handled = False
        dual_app._current_job_id = None
        dual_app._pod_state = dual_app.PodState.IDLE
        dual_app._orchestrator_client = AsyncMock()
        dual_app._orchestrator_client.heartbeat = AsyncMock(return_value={})

    @pytest.mark.asyncio
    async def test_idle_worker_exits_on_drain(self):
        from src.api import dual_app

        self._reset(dual_app)

        with patch("src.api.dual_app.os._exit") as fake_exit:
            await dual_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True, "drain_reason": "stale_image"}}
            )
        fake_exit.assert_called_once_with(0)
        # Drain heartbeat sent before exit (best-effort).
        dual_app._orchestrator_client.heartbeat.assert_awaited_once()
        assert dual_app._drain_intent_handled is True

    @pytest.mark.asyncio
    async def test_busy_worker_sets_flag_no_exit(self):
        from src.api import dual_app

        self._reset(dual_app)
        dual_app._current_job_id = "11111111-1111-1111-1111-111111111111"
        dual_app._pod_state = dual_app.PodState.WORKING

        with patch("src.api.dual_app.os._exit") as fake_exit:
            await dual_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True, "drain_reason": "stale_image"}}
            )
        fake_exit.assert_not_called()
        assert dual_app._drain_intent_received is True
        assert dual_app.is_drain_requested() is True

    @pytest.mark.asyncio
    async def test_session_worker_sets_flag_no_exit(self):
        from src.api import dual_app

        self._reset(dual_app)
        dual_app._pod_state = dual_app.PodState.SESSION

        with patch("src.api.dual_app.os._exit") as fake_exit:
            await dual_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True}}
            )
        fake_exit.assert_not_called()
        assert dual_app.is_drain_requested() is True

    @pytest.mark.asyncio
    async def test_no_drain_intent_no_state_change(self):
        from src.api import dual_app

        self._reset(dual_app)

        with patch("src.api.dual_app.os._exit") as fake_exit:
            await dual_app._handle_heartbeat_intents({"intents": {}})
            await dual_app._handle_heartbeat_intents({})
        fake_exit.assert_not_called()
        assert dual_app.is_drain_requested() is False


# =============================================================================
# Orchestrator — version_upgrade freeze type → paused
# =============================================================================


class TestVersionUpgradeFreeze:
    def test_version_upgrade_pauses_for_redispatch(self):
        from orchestrator.services.completion import determine_job_status

        job = {"id": "j1", "parent_job_id": None}
        result = {
            "should_stop": True,
            "goal_achieved": False,
            "freeze_data": {"freeze_type": "version_upgrade"},
        }
        status, err = determine_job_status(job, result)
        assert status == "paused"
        assert err is None

    def test_version_upgrade_paused_even_when_db_carries_freeze(self):
        # freeze_data may live on the job row or come in via the request;
        # determine_job_status reads either. Verify the DB-side path.
        from orchestrator.services.completion import determine_job_status

        job = {
            "id": "j1",
            "parent_job_id": None,
            "freeze_data": {"freeze_type": "version_upgrade"},
        }
        result = {"should_stop": True, "goal_achieved": False}
        status, _ = determine_job_status(job, result)
        assert status == "paused"

    def test_existing_freeze_types_unchanged(self):
        # Smoke: the new version_upgrade branch doesn't shadow the
        # previously handled non-completion freeze types.
        from orchestrator.services.completion import determine_job_status

        for ftype, expected in [
            ("delegation", "waiting"),
            ("vm_upgrade_required", "paused"),
            (None, "pending_review"),
        ]:
            job = {"id": "j", "parent_job_id": None}
            result = {
                "should_stop": True,
                "goal_achieved": False,
                "freeze_data": {"freeze_type": ftype} if ftype else {},
            }
            status, _ = determine_job_status(job, result)
            assert status == expected, (
                f"freeze_type={ftype} → {status}, expected {expected}"
            )


# =============================================================================
# Auto-continue resume-clear — the version_upgrade drain livelock + its fix
# (docs/issues/version_upgrade_drain_livelock.md)
#
# Reproduces the LangGraph semantics at the heart of the bug: a graph that ends
# with should_stop=True persists that in the checkpoint, and invoke(None) on the
# ended thread runs ZERO nodes and returns the terminal frozen state — so the
# in-graph "clear stop flags on resume" node never runs and the job re-freezes
# forever. The fix (clear the flags via update_state(as_node="__start__") before
# invoke) makes the graph re-enter and progress. These guard the exact mechanism
# src/agent.py depends on, on the installed LangGraph version.
# =============================================================================


def _build_drain_graph():
    """Minimal graph mirroring route_entry → restore → execute → transition →
    check, compiled with a checkpointer. ``flags['drain']`` models the
    process-local drain intent; ``ran`` records node execution order."""
    from typing import TypedDict

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    flags = {"drain": True}
    ran: list[str] = []

    class S(TypedDict, total=False):
        initialized: bool
        should_stop: bool
        goal_achieved: bool
        work_done: int
        freeze_data: dict

    # Node/router functions are left unannotated on purpose: LangGraph's branch
    # schema inference runs get_type_hints() on routers, which can't resolve a
    # function-local TypedDict. The state schema comes from StateGraph(S).
    def route_entry(state):
        return "restore" if state.get("initialized") else "init"

    def init(state):
        ran.append("init")
        return {"initialized": True}

    def restore(state):
        ran.append("restore")
        return {"should_stop": False, "goal_achieved": False}  # graph.py:3531

    def execute(state):
        ran.append("execute")  # audit proxy: real work happened
        return {"work_done": state.get("work_done", 0) + 1}

    def handle_transition(state):
        ran.append("handle_transition")
        if flags["drain"]:  # _is_drain_requested()
            return {
                "should_stop": True,
                "freeze_data": {"freeze_type": "version_upgrade", "phase_number": 3},
            }
        return {}

    def check_goal(state):
        ran.append("check_goal")
        return {}

    def route_after_check(state):
        # END on a freeze, or once the toy "goal" (3 phases) is reached.
        if state.get("should_stop") or state.get("work_done", 0) >= 3:
            return "__end__"
        return "execute"

    g = StateGraph(S)
    for name, fn in [
        ("init", init),
        ("restore", restore),
        ("execute", execute),
        ("handle_transition", handle_transition),
        ("check_goal", check_goal),
    ]:
        g.add_node(name, fn)
    g.add_conditional_edges(START, route_entry, {"init": "init", "restore": "restore"})
    g.add_edge("init", "execute")
    g.add_edge("restore", "execute")
    g.add_edge("execute", "handle_transition")
    g.add_edge("handle_transition", "check_goal")
    g.add_conditional_edges(
        "check_goal", route_after_check, {"execute": "execute", "__end__": END}
    )
    return g.compile(checkpointer=MemorySaver()), flags, ran


class TestAutoContinueResumeClear:
    def test_reinvoke_on_ended_stop_checkpoint_runs_no_nodes(self):
        # The BUG: invoke(None) on a thread that ended with should_stop=True runs
        # zero nodes and returns the terminal frozen state — no progress.
        app, flags, ran = _build_drain_graph()
        cfg = {"configurable": {"thread_id": "t"}, "recursion_limit": 50}
        s1 = app.invoke(
            {"initialized": False, "should_stop": False, "work_done": 0}, cfg
        )
        assert s1["should_stop"] is True
        assert s1["work_done"] == 1
        ran.clear()
        s2 = app.invoke(None, cfg)  # what agent.py did on a plain graceful resume
        assert ran == [], "expected zero nodes to run on re-invoke of ended thread"
        assert s2["work_done"] == 1, "no forward progress (the livelock)"
        assert s2["should_stop"] is True

    def test_clearing_stop_flags_reenters_and_progresses(self):
        # The FIX: clear the terminal stop flags via update_state(as_node=__start__)
        # before invoke(None) → the graph re-enters and advances one phase.
        app, flags, ran = _build_drain_graph()
        cfg = {"configurable": {"thread_id": "t"}, "recursion_limit": 50}
        app.invoke({"initialized": False, "should_stop": False, "work_done": 0}, cfg)
        app.update_state(
            cfg,
            {
                "should_stop": False,
                "goal_achieved": False,
                "is_final_phase": False,
                "freeze_data": None,
            },
            as_node="__start__",
        )
        ran.clear()
        s2 = app.invoke(None, cfg)  # drain still on → one phase, then re-freeze
        assert "execute" in ran, "graph must re-enter and run execute"
        assert s2["work_done"] == 2, "exactly one phase of forward progress"

    def test_fix_runs_to_completion_once_drain_clears(self):
        # With the image settled (drain gone), the cleared resume runs the phase
        # and does NOT re-freeze — the job proceeds normally.
        app, flags, ran = _build_drain_graph()
        cfg = {"configurable": {"thread_id": "t"}, "recursion_limit": 50}
        app.invoke({"initialized": False, "should_stop": False, "work_done": 0}, cfg)
        flags["drain"] = False  # image settled, reconciler no longer draining
        app.update_state(
            cfg,
            {"should_stop": False, "goal_achieved": False, "freeze_data": None},
            as_node="__start__",
        )
        s2 = app.invoke(None, cfg)
        assert s2["work_done"] >= 2
        assert s2.get("should_stop") is not True or s2.get("freeze_data") is None


class TestAutoContinueDrainBackstop:
    """Pure progress-aware drain counter (services.completion)."""

    def _update(self, ctx, freeze, cap=10):
        from orchestrator.services.completion import auto_continue_drain_update

        return auto_continue_drain_update(ctx, freeze, cap=cap)

    def test_same_phase_increments(self):
        ctx = {"auto_continue_drains": 2, "auto_continue_last_phase": 3}
        drains, last, alert = self._update(ctx, {"phase_number": 3})
        assert (drains, last, alert) == (3, 3, False)

    def test_advancing_phase_resets(self):
        # The happy path once the resume-clear fix works: phase advances → reset.
        ctx = {"auto_continue_drains": 5, "auto_continue_last_phase": 3}
        drains, last, alert = self._update(ctx, {"phase_number": 4})
        assert (drains, last, alert) == (0, 4, False)

    def test_absent_phase_number_is_no_signal(self):
        # llm_unavailable etc. carry no phase_number → never trips (has its own
        # ceiling elsewhere).
        ctx = {"auto_continue_drains": 9, "auto_continue_last_phase": 3}
        drains, last, alert = self._update(ctx, {"freeze_type": "llm_unavailable"})
        assert (drains, last, alert) == (0, None, False)

    def test_alerts_at_cap(self):
        ctx = {"auto_continue_drains": 9, "auto_continue_last_phase": 3}
        drains, _, alert = self._update(ctx, {"phase_number": 3}, cap=10)
        assert drains == 10 and alert is True

    def test_first_drain_from_empty_context(self):
        drains, last, alert = self._update({}, {"phase_number": 3})
        assert (drains, last, alert) == (0, 3, False)


class TestAutoContinueFreezeTypeScope:
    def test_auto_continue_set_covers_redispatch_types_only(self):
        from src.agent import _AUTO_CONTINUE_FREEZE_TYPES

        for ft in (
            "version_upgrade",
            "llm_unavailable",
            "memory_unavailable",
            "kb_unavailable",
            "workspace_upgrade_required",
        ):
            assert ft in _AUTO_CONTINUE_FREEZE_TYPES
        # Human-review / terminal stops must NOT be auto-cleared on resume.
        for ft in ("budget_exceeded", "job_complete", "vm_upgrade_required"):
            assert ft not in _AUTO_CONTINUE_FREEZE_TYPES
