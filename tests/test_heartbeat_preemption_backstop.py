"""An agent must learn its job was terminated out from under it.

Regression guard for Defect 3 of
docs/issues/transient_db_error_hard_fails_job_and_destroys_vm.md.

The heartbeat was one-directional: the agent asserted liveness and learned
nothing back. Job c6dd288d was failed at ~09:27:46 and kept streaming until
09:49:32 — 21 minutes and 45 LLM calls — discovering the truth only when its VM
was collected underneath it, which it misread as "my workspace died".

The orchestrator's push stop signal is the fast path. This is the backstop for
the 13+ call sites that can write a terminal status without sending one.
"""

import pytest

import src.api.app as app_module
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
