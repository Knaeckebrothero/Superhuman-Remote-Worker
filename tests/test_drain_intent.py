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
