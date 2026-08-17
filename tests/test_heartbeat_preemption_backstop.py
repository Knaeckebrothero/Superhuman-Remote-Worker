"""An agent must learn its job was terminated out from under it.

Regression guard for Defect 3 of
knowledge-base/knowledge/issues/transient_db_error_hard_fails_job_and_destroys_vm.md.

The heartbeat was one-directional: the agent asserted liveness and learned
nothing back. Job c6dd288d was failed at ~09:27:46 and kept streaming until
09:49:32 — 21 minutes and 45 LLM calls — discovering the truth only when its VM
was collected underneath it, which it misread as "my workspace died".

The orchestrator's push stop signal is the fast path. This is the backstop for
the 13+ call sites that can write a terminal status without sending one.

The deployed handler is dual_app, which for months read only
``intents.should_drain`` and ignored ``job_status`` — every out-of-band
steer/pause/cancel left the incumbent pod running to natural END as an orphan
(P0-D of knowledge-base/knowledge/issues/officer_blind_reads_and_worker_bureaucracy.md). The
second half of this file guards the dual_app port. Note the response carries
no job-id echo: the orchestrator derives ``job_status`` from the job id THIS
pod sent in its heartbeat, so "denied status for some other job" is not a
representable response shape.
"""

from unittest.mock import patch

import pytest

import src.api.app as app_module
import src.api.dual_app as dual_app
from src.api.app import _clear_stop, _on_heartbeat_response


@pytest.fixture(autouse=True)
def _reset_stop_state():
    _clear_stop()
    app_module._current_job_id = None
    yield
    _clear_stop()
    app_module._current_job_id = None


def _running(job_id="job-under-test"):
    app_module._current_job_id = job_id


class TestStopsWhenPreempted:
    @pytest.mark.parametrize(
        "job_status,expected_reason",
        [("failed", "cancel"), ("cancelled", "cancel"), ("paused", "pause")],
    )
    def test_preempted_statuses_request_stop(self, job_status, expected_reason):
        _running()
        _on_heartbeat_response({"status": "ok", "job_status": job_status})
        assert app_module._stop_requested.is_set()
        assert app_module._stop_reason == expected_reason

    def test_the_incident_shape(self):
        """Failed out-of-band while the agent streamed on."""
        _running("c6dd288d")
        _on_heartbeat_response({"status": "ok", "job_status": "failed"})
        assert app_module._stop_requested.is_set()


class TestKeepsRunning:
    @pytest.mark.parametrize(
        "job_status",
        ["processing", "reviewing", "pending_review", "completed", "waiting"],
    )
    def test_live_statuses_do_not_stop_the_run(self, job_status):
        _running()
        _on_heartbeat_response({"status": "ok", "job_status": job_status})
        assert not app_module._stop_requested.is_set()

    def test_missing_job_status_degrades_to_push_only(self):
        """Older orchestrator, or the lookup failed — must not guess."""
        _running()
        _on_heartbeat_response({"status": "ok"})
        assert not app_module._stop_requested.is_set()

    def test_unknown_status_fails_open(self):
        """A new status must not silently start killing healthy runs."""
        _running()
        _on_heartbeat_response({"status": "ok", "job_status": "some_new_state"})
        assert not app_module._stop_requested.is_set()

    def test_no_current_job_is_a_no_op(self):
        _on_heartbeat_response({"status": "ok", "job_status": "failed"})
        assert not app_module._stop_requested.is_set()

    def test_malformed_response_is_a_no_op(self):
        _running()
        _on_heartbeat_response(None)
        _on_heartbeat_response("nonsense")
        assert not app_module._stop_requested.is_set()


class TestDoesNotDowngradeAnExistingStop:
    def test_a_pending_cancel_is_not_overwritten_by_a_pause(self):
        """_request_stop refuses the downgrade; the backstop must not race it."""
        _running()
        _on_heartbeat_response({"status": "ok", "job_status": "cancelled"})
        assert app_module._stop_reason == "cancel"
        _on_heartbeat_response({"status": "ok", "job_status": "paused"})
        assert app_module._stop_reason == "cancel"


# =============================================================================
# dual_app port (P0-D) — the DEPLOYED handler. Same deny-list, same stop
# machinery /job/cancel and /job/pause drive, gated on the pod state machine.
# Driven through _handle_heartbeat_intents, the callback actually registered
# with run_heartbeat_loop, so the wiring is covered too.
# =============================================================================


class TestDualAppPreemption:
    @pytest.fixture(autouse=True)
    def _restore_dual_app_globals(self):
        """Snapshot/restore the module globals these tests mutate (the module
        keeps process-wide state), then set a clean WORKING-less baseline."""
        names = (
            "_pod_state",
            "_current_job_id",
            "_current_job_task",
            "_stop_reason",
            "_drain_intent_received",
            "_drain_intent_handled",
        )
        saved = {name: getattr(dual_app, name) for name in names}
        stop_req = dual_app._stop_requested.is_set()
        stop_done = dual_app._stop_completed.is_set()

        dual_app._pod_state = dual_app.PodState.IDLE
        dual_app._current_job_id = None
        dual_app._current_job_task = None
        dual_app._drain_intent_received = False
        dual_app._drain_intent_handled = False
        dual_app._clear_stop()
        yield
        for name, val in saved.items():
            setattr(dual_app, name, val)
        (dual_app._stop_requested.set if stop_req else dual_app._stop_requested.clear)()
        (
            dual_app._stop_completed.set
            if stop_done
            else dual_app._stop_completed.clear
        )()

    def _working(self, job_id="job-under-test"):
        dual_app._pod_state = dual_app.PodState.WORKING
        dual_app._current_job_id = job_id

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "job_status,expected_reason",
        [("failed", "cancel"), ("cancelled", "cancel"), ("paused", "pause")],
    )
    async def test_preempted_statuses_request_stop(self, job_status, expected_reason):
        self._working()
        await dual_app._handle_heartbeat_intents(
            {"status": "ok", "job_status": job_status}
        )
        assert dual_app._stop_requested.is_set()
        assert dual_app._stop_reason == expected_reason

    @pytest.mark.asyncio
    async def test_the_orphan_shape(self):
        """The 05:33 urgent steer: row flipped to paused + unassigned, no push
        signal — the incumbent pod must stop instead of orphaning."""
        self._working("steered-job")
        await dual_app._handle_heartbeat_intents(
            {"status": "ok", "intents": {}, "job_status": "paused"}
        )
        assert dual_app._stop_requested.is_set()
        assert dual_app._stop_reason == "pause"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "job_status",
        ["processing", "reviewing", "pending_review", "completed", "waiting"],
    )
    async def test_live_statuses_do_not_stop_the_run(self, job_status):
        self._working()
        await dual_app._handle_heartbeat_intents(
            {"status": "ok", "job_status": job_status}
        )
        assert not dual_app._stop_requested.is_set()

    @pytest.mark.asyncio
    async def test_missing_or_unknown_status_fails_open(self):
        self._working()
        await dual_app._handle_heartbeat_intents({"status": "ok"})
        await dual_app._handle_heartbeat_intents(
            {"status": "ok", "job_status": "some_new_state"}
        )
        assert not dual_app._stop_requested.is_set()

    @pytest.mark.asyncio
    async def test_idle_pod_is_a_no_op(self):
        await dual_app._handle_heartbeat_intents(
            {"status": "ok", "job_status": "failed"}
        )
        assert not dual_app._stop_requested.is_set()

    @pytest.mark.asyncio
    async def test_completion_window_is_a_no_op(self):
        """Own-report race: the completion paths null _current_job_id BEFORE
        report_completion while _pod_state is still WORKING. A heartbeat
        landing in that window must not stop the pod's orderly shutdown."""
        dual_app._pod_state = dual_app.PodState.WORKING
        dual_app._current_job_id = None
        await dual_app._handle_heartbeat_intents(
            {"status": "ok", "job_status": "paused"}
        )
        assert not dual_app._stop_requested.is_set()

    @pytest.mark.asyncio
    async def test_session_pod_is_a_no_op(self):
        """The deny-list is a worker concern — SESSION pods have no job and
        must never be stopped by it."""
        dual_app._pod_state = dual_app.PodState.SESSION
        await dual_app._handle_heartbeat_intents(
            {"status": "ok", "job_status": "cancelled"}
        )
        assert not dual_app._stop_requested.is_set()

    @pytest.mark.asyncio
    async def test_repeated_denied_heartbeats_stop_once(self):
        """Idempotency: once _stop_requested is set, further denied heartbeats
        no-op — no stacked cancels, no downgrade of cancel to pause."""
        self._working()
        await dual_app._handle_heartbeat_intents(
            {"status": "ok", "job_status": "cancelled"}
        )
        assert dual_app._stop_reason == "cancel"
        completed_before = dual_app._stop_completed.is_set()
        await dual_app._handle_heartbeat_intents(
            {"status": "ok", "job_status": "cancelled"}
        )
        await dual_app._handle_heartbeat_intents(
            {"status": "ok", "job_status": "paused"}
        )
        assert dual_app._stop_reason == "cancel"
        assert dual_app._stop_requested.is_set()
        assert dual_app._stop_completed.is_set() == completed_before

    @pytest.mark.asyncio
    async def test_busy_drain_intent_and_denied_status_both_land(self):
        """A drain intent riding the same response must still be recorded —
        the preemption check runs first and must not short-circuit it."""
        self._working()
        with patch("src.api.dual_app.os._exit") as fake_exit:
            await dual_app._handle_heartbeat_intents(
                {
                    "status": "ok",
                    "intents": {"should_drain": True, "drain_reason": "stale_image"},
                    "job_status": "paused",
                }
            )
        fake_exit.assert_not_called()
        assert dual_app._stop_requested.is_set()
        assert dual_app._stop_reason == "pause"
        assert dual_app.is_drain_requested() is True
