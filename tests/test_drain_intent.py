"""Tests for Phase 1c drain-intent reaction.

Covers:
  - persistent_app._handle_heartbeat_intents — drain → detach + exit
  - dual_app._handle_heartbeat_intents — idle exit + busy flag
  - completion.determine_job_status — version_upgrade → paused
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# =============================================================================
# Persistent agent — drain intent triggers detach + exit
# =============================================================================


class TestPersistentDrainHandler:
    @pytest.mark.asyncio
    async def test_should_drain_triggers_detach_and_exit(self):
        from src.api import persistent_app

        # Reset module-level state so tests don't leak into each other.
        persistent_app._drain_intent_handled = False

        with (
            patch.object(persistent_app, "_detach_session", new=AsyncMock()) as detach,
            patch.object(persistent_app, "_schedule_exit") as exit_,
        ):
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True, "drain_reason": "stale_image"}}
            )
        detach.assert_awaited_once()
        exit_.assert_called_once()
        assert persistent_app._drain_intent_handled is True

    @pytest.mark.asyncio
    async def test_subsequent_calls_are_idempotent(self):
        from src.api import persistent_app

        persistent_app._drain_intent_handled = False

        with (
            patch.object(persistent_app, "_detach_session", new=AsyncMock()) as detach,
            patch.object(persistent_app, "_schedule_exit") as exit_,
        ):
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True}}
            )
            # Second call must not detach/exit again.
            await persistent_app._handle_heartbeat_intents(
                {"intents": {"should_drain": True}}
            )
        assert detach.await_count == 1
        assert exit_.call_count == 1

    @pytest.mark.asyncio
    async def test_no_intents_no_action(self):
        from src.api import persistent_app

        persistent_app._drain_intent_handled = False

        with (
            patch.object(persistent_app, "_detach_session", new=AsyncMock()) as detach,
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
