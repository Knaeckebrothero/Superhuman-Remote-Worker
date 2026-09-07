"""Regression tests for the worker-pod-state zombie on cancel/pause.

Covers the three leak sites where dual_app's stop paths used to strand
``_pod_state=WORKING`` with no job (see
knowledge-history/done/worker_pod_state_zombie_on_cancel.md):

  - ``_complete_stop()``            — the shared reset+signal helper (sites A/B)
  - ``_process_orchestrator_job()`` — cooperative stop mid-job (site A)
  - ``/job/cancel`` hard-kill       — the 120s timeout branch (site C)

Each asserts the pod lands back at ``PodState.IDLE`` and emits a
ready/``job_id=None`` heartbeat, so the orchestrator's ``agents.status``
converges instead of flip-flopping between working and ready.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _TrackedStream:
    """Async iterator whose explicit close is observable independently."""

    def __init__(self, states, events, *, before_yield=None, error=None):
        self._states = list(states)
        self._events = events
        self._before_yield = before_yield
        self._error = error

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._states:
            if self._before_yield is not None:
                self._before_yield()
                self._before_yield = None
            return self._states.pop(0)
        if self._error is not None:
            error = self._error
            self._error = None
            raise error
        raise StopAsyncIteration

    async def aclose(self):
        self._events.append("close")


@pytest.fixture(autouse=True)
def _restore_dual_app_globals():
    """Snapshot and restore the dual_app module globals these tests mutate so
    they don't leak into other test files (the module keeps process-wide state)."""
    from agent.api import dual_app

    names = (
        "_pod_state",
        "_current_job_id",
        "_current_job_task",
        "_stop_reason",
        "_agent",
        "_orchestrator_client",
        "_pending_exit_task",
        "_heartbeat_task",
        "_shutdown_requested",
    )
    saved = {name: getattr(dual_app, name) for name in names}
    stop_req, stop_done = (
        dual_app._stop_requested.is_set(),
        dual_app._stop_completed.is_set(),
    )
    yield
    for name, val in saved.items():
        setattr(dual_app, name, val)
    (dual_app._stop_requested.set if stop_req else dual_app._stop_requested.clear)()
    (dual_app._stop_completed.set if stop_done else dual_app._stop_completed.clear)()


def _reset_module(dual_app, *, pod_state=None):
    """Set a clean per-test baseline and return the mock orchestrator client."""
    dual_app._pod_state = pod_state or dual_app.PodState.IDLE
    dual_app._current_job_id = None
    dual_app._current_job_task = None
    dual_app._clear_stop()
    client = AsyncMock()
    client.agent_id = "agent-1"
    client.heartbeat = AsyncMock(return_value={})
    client.report_completion = AsyncMock(return_value=None)
    client.stop_heartbeat = MagicMock()
    client.deregister = AsyncMock(return_value=True)
    dual_app._orchestrator_client = client
    return client


class TestCompleteStop:
    """The shared helper behind cooperative cancel/pause (sites A and B)."""

    @pytest.mark.asyncio
    async def test_resets_to_idle_then_signals(self):
        from agent.api import dual_app

        client = _reset_module(dual_app, pod_state=dual_app.PodState.WORKING)
        dual_app._current_job_id = "job-1"
        dual_app._stop_completed.clear()

        await dual_app._complete_stop("job cancel")

        # Pod is reusable again — no stranded WORKING/no-job zombie.
        assert dual_app._pod_state == dual_app.PodState.IDLE
        assert dual_app._current_job_id is None
        # Ordering: _reset_to_idle()->_clear_stop() clears _stop_completed, so
        # the helper must set it AFTER the reset. If it ran before, this is False.
        assert dual_app._stop_completed.is_set()
        # And _clear_stop() did run during the reset.
        assert not dual_app._stop_requested.is_set()
        assert dual_app._stop_reason is None
        # The reset pushed a ready / job_id=None heartbeat.
        client.heartbeat.assert_awaited()
        kwargs = client.heartbeat.await_args.kwargs
        assert kwargs.get("status") == "ready"
        assert kwargs.get("job_id") is None


class TestProcessJobCooperativeStop:
    """Site A — a cooperative stop while a job is streaming."""

    @pytest.mark.asyncio
    async def test_cooperative_stop_resets_and_does_not_exit(
        self, tmp_path, monkeypatch
    ):
        from agent.api import dual_app

        client = _reset_module(dual_app, pod_state=dual_app.PodState.WORKING)
        dual_app._current_job_id = "job-A"
        dual_app._stop_completed.clear()

        events: list[str] = []
        stream = _TrackedStream(
            [{"iteration": 1}],
            events,
            before_yield=lambda: dual_app._request_stop("cancel"),
        )

        agent = MagicMock()
        # process_job is awaited, then async-iterated → hand back the stream.
        agent.process_job = AsyncMock(return_value=stream)
        dual_app._agent = agent

        # Keep per-job file logging inside tmp_path.
        from agent.core import workspace as ws_mod

        monkeypatch.setattr(ws_mod, "get_logs_path", lambda: tmp_path)

        sched = MagicMock()
        monkeypatch.setattr(dual_app, "_schedule_exit", sched)
        complete_stop = dual_app._complete_stop

        async def _tracked_complete_stop(source):
            assert events == ["close"]
            events.append("reset")
            await complete_stop(source)

        monkeypatch.setattr(dual_app, "_complete_stop", _tracked_complete_stop)

        await dual_app._process_orchestrator_job("job-A", "desc")

        assert events[:2] == ["close", "reset"]
        assert dual_app._pod_state == dual_app.PodState.IDLE
        assert dual_app._current_job_id is None
        assert dual_app._stop_completed.is_set()
        # Cooperative stop must NOT schedule a pod exit (orchestrator may resume).
        sched.assert_not_called()
        # It's a stop, not a completion — no completion report, and the
        # agents row stays (the pod remains dispatchable).
        client.report_completion.assert_not_awaited()
        client.deregister.assert_not_awaited()


class TestProcessJobCompletionOrdering:
    @pytest.mark.asyncio
    async def test_reports_with_pinned_fence_before_ready_heartbeat(
        self, tmp_path, monkeypatch
    ):
        from agent.api import dual_app

        client = _reset_module(dual_app, pod_state=dual_app.PodState.WORKING)
        dual_app._current_job_id = "job-complete"
        monkeypatch.setenv("AGENT_LOOP", "1")

        events: list[str] = []

        async def _report(*_args, **_kwargs):
            events.append("report")
            return True

        async def _heartbeat(*_args, **_kwargs):
            events.append("heartbeat")
            return {}

        client.report_completion = AsyncMock(side_effect=_report)
        client.heartbeat = AsyncMock(side_effect=_heartbeat)

        final = {
            "should_stop": True,
            "goal_achieved": True,
            "error": None,
            "freeze_data": None,
        }
        stream = _TrackedStream([final], events)

        agent = MagicMock()
        agent.process_job = AsyncMock(return_value=stream)
        dual_app._agent = agent

        from agent.core import workspace as ws_mod

        monkeypatch.setattr(ws_mod, "get_logs_path", lambda: tmp_path)

        await dual_app._process_orchestrator_job("job-complete", "desc")

        assert events[:2] == ["close", "report"]
        client.report_completion.assert_awaited_once_with(
            "job-complete",
            final,
            agent_id="agent-1",
        )
        assert client.heartbeat.await_count >= 1

    @pytest.mark.asyncio
    async def test_stream_error_closes_before_error_report_and_reset(
        self, tmp_path, monkeypatch
    ):
        from agent.api import dual_app

        client = _reset_module(dual_app, pod_state=dual_app.PodState.WORKING)
        dual_app._current_job_id = "job-error"
        monkeypatch.setenv("AGENT_LOOP", "1")
        events: list[str] = []

        async def _report(*_args, **_kwargs):
            assert events == ["close"]
            events.append("report")

        async def _reset(*_args, **_kwargs):
            assert events == ["close", "report"]
            events.append("reset")

        client.report_completion = AsyncMock(side_effect=_report)
        agent = MagicMock()
        agent.process_job = AsyncMock(
            return_value=_TrackedStream([], events, error=RuntimeError("stream failed"))
        )
        dual_app._agent = agent
        monkeypatch.setattr(dual_app, "_reset_to_idle", _reset)

        from agent.core import workspace as ws_mod

        monkeypatch.setattr(ws_mod, "get_logs_path", lambda: tmp_path)

        await dual_app._process_orchestrator_job("job-error", "desc")

        assert events == ["close", "report", "reset"]

    @pytest.mark.asyncio
    async def test_lifecycle_failure_drains_without_report_or_idle_reset(
        self, tmp_path, monkeypatch
    ):
        from agent.api import dual_app
        from shared.subagent_lifecycle import SubagentQuiescenceError

        client = _reset_module(dual_app, pod_state=dual_app.PodState.WORKING)
        dual_app._current_job_id = "job-lifecycle"
        dual_app._shutdown_requested = False
        monkeypatch.setenv("AGENT_LOOP", "1")
        events: list[str] = []
        agent = MagicMock()
        agent._tool_context = None
        agent.abandon_worker_subagents = AsyncMock()
        agent.process_job = AsyncMock(
            return_value=_TrackedStream(
                [], events, error=SubagentQuiescenceError("delivery failed")
            )
        )
        dual_app._agent = agent
        reset = AsyncMock()
        schedule_exit = MagicMock()
        monkeypatch.setattr(dual_app, "_reset_to_idle", reset)
        monkeypatch.setattr(dual_app, "_schedule_exit", schedule_exit)

        from agent.core import workspace as ws_mod

        monkeypatch.setattr(ws_mod, "get_logs_path", lambda: tmp_path)

        await dual_app._process_orchestrator_job("job-lifecycle", "desc")

        assert events == ["close"]
        client.report_completion.assert_not_awaited()
        reset.assert_not_awaited()
        agent.abandon_worker_subagents.assert_awaited_once()
        assert client.heartbeat.await_args.kwargs["status"] == "draining"
        assert client.heartbeat.await_args.kwargs["job_id"] == "job-lifecycle"
        assert dual_app._shutdown_requested is True
        assert dual_app._pod_state == dual_app.PodState.WORKING
        schedule_exit.assert_called_once_with(delay=1.0)


class TestJobStreamCloseCancellation:
    @pytest.mark.asyncio
    async def test_close_timeout_is_a_typed_lifecycle_failure(self, monkeypatch):
        from agent.api import dual_app
        from shared.subagent_lifecycle import SubagentQuiescenceError

        class _HungStream:
            async def aclose(self):
                await asyncio.Future()

        monkeypatch.setattr(dual_app, "_JOB_STREAM_CLOSE_TIMEOUT_SECONDS", 0.001)

        with pytest.raises(SubagentQuiescenceError, match="cleanup timed out"):
            await dual_app._close_job_stream(_HungStream(), job_id="hung-close")

    @pytest.mark.asyncio
    async def test_caller_cancellation_waits_for_close_owner(self):
        from agent.api import dual_app

        close_started = asyncio.Event()
        release_close = asyncio.Event()
        closed = asyncio.Event()

        class _BlockingCloseStream:
            async def aclose(self):
                close_started.set()
                await release_close.wait()
                closed.set()

        task = asyncio.create_task(
            dual_app._close_job_stream(_BlockingCloseStream(), job_id="job-cancel")
        )
        await close_started.wait()
        task.cancel()
        await asyncio.sleep(0)

        assert not task.done()
        assert not closed.is_set()

        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()


class TestResumeJobStreamClose:
    @pytest.mark.asyncio
    async def test_normal_resume_closes_before_report_and_reset(
        self, tmp_path, monkeypatch
    ):
        from agent.api import dual_app
        from agent.api.models import JobResumeRequest, PinnedJobRecipient

        client = _reset_module(dual_app)
        client.dispatch_process_generation = "process-resume"
        monkeypatch.delenv("POD_UID", raising=False)
        events: list[str] = []
        final = {"should_stop": True, "error": None}

        async def _report(*_args, **_kwargs):
            assert events == ["close"]
            events.append("report")

        async def _reset(*_args, **_kwargs):
            assert events == ["close", "report"]
            events.append("reset")

        client.report_completion = AsyncMock(side_effect=_report)
        agent = MagicMock()
        agent.process_job = AsyncMock(return_value=_TrackedStream([final], events))
        dual_app._agent = agent
        monkeypatch.setattr(dual_app, "_reset_to_idle", _reset)
        monkeypatch.setattr(dual_app, "_should_loop", lambda: True)

        from agent.core import workspace as ws_mod

        monkeypatch.setattr(ws_mod, "get_logs_path", lambda: tmp_path)
        app = dual_app.create_dual_app()
        resume_ep = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", "") == "/job/resume"
        )

        await resume_ep(
            JobResumeRequest(
                job_id="job-resume",
                config_override={
                    "workspace": {
                        "backend": "sandbox",
                        "remote": {"host": "workspace.test"},
                    }
                },
                workspace_runtime={
                    "requested_backend": None,
                    "assigned_backend": "sandbox",
                    "effective_backend": "sandbox",
                    "state": "ready",
                },
                recipient=PinnedJobRecipient(
                    expected_agent_id="agent-1",
                    expected_pod_uid=None,
                    expected_process_generation="process-resume",
                    expected_job_id="job-resume",
                ),
            )
        )
        await dual_app._current_job_task

        assert events == ["close", "report", "reset"]

    @pytest.mark.asyncio
    async def test_lifecycle_failure_resume_drains_without_report_or_reset(
        self, tmp_path, monkeypatch
    ):
        from agent.api import dual_app
        from agent.api.models import JobResumeRequest, PinnedJobRecipient
        from shared.subagent_lifecycle import SubagentRecoveryError

        client = _reset_module(dual_app)
        client.dispatch_process_generation = "process-lifecycle-resume"
        dual_app._shutdown_requested = False
        monkeypatch.delenv("POD_UID", raising=False)
        monkeypatch.setenv("AGENT_LOOP", "1")
        events: list[str] = []
        agent = MagicMock()
        agent._tool_context = None
        agent.abandon_worker_subagents = AsyncMock()
        agent.process_job = AsyncMock(
            return_value=_TrackedStream(
                [], events, error=SubagentRecoveryError("recovery failed")
            )
        )
        dual_app._agent = agent
        reset = AsyncMock()
        schedule_exit = MagicMock()
        monkeypatch.setattr(dual_app, "_reset_to_idle", reset)
        monkeypatch.setattr(dual_app, "_schedule_exit", schedule_exit)

        from agent.core import workspace as ws_mod

        monkeypatch.setattr(ws_mod, "get_logs_path", lambda: tmp_path)
        app = dual_app.create_dual_app()
        resume_ep = next(
            route.endpoint
            for route in app.routes
            if getattr(route, "path", "") == "/job/resume"
        )

        await resume_ep(
            JobResumeRequest(
                job_id="job-lifecycle-resume",
                config_override={
                    "workspace": {
                        "backend": "sandbox",
                        "remote": {"host": "workspace.test"},
                    }
                },
                workspace_runtime={
                    "requested_backend": None,
                    "assigned_backend": "sandbox",
                    "effective_backend": "sandbox",
                    "state": "ready",
                },
                recipient=PinnedJobRecipient(
                    expected_agent_id="agent-1",
                    expected_pod_uid=None,
                    expected_process_generation="process-lifecycle-resume",
                    expected_job_id="job-lifecycle-resume",
                ),
            )
        )
        await dual_app._current_job_task

        assert events == ["close"]
        client.report_completion.assert_not_awaited()
        reset.assert_not_awaited()
        agent.abandon_worker_subagents.assert_awaited_once()
        assert client.heartbeat.await_args.kwargs["status"] == "draining"
        assert client.heartbeat.await_args.kwargs["job_id"] == ("job-lifecycle-resume")
        assert dual_app._shutdown_requested is True
        schedule_exit.assert_called_once_with(delay=1.0)


class TestCancelHardKillResets:
    """Site C — the cancel handler's 120s hard-kill timeout branch."""

    @pytest.mark.asyncio
    async def test_hard_kill_timeout_resets_to_idle(self, monkeypatch):
        from agent.api import dual_app
        from agent.api.models import JobCancelByOrchestratorRequest, PinnedJobRecipient

        client = _reset_module(dual_app, pod_state=dual_app.PodState.WORKING)
        client.dispatch_process_generation = "process-C"
        monkeypatch.delenv("POD_UID", raising=False)
        dual_app._current_job_id = "job-C"

        # A real, never-finishing job task for the handler to hard-kill.
        async def _never():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_never())
        dual_app._current_job_task = task

        # Force the graceful 120s wait to time out immediately.
        def _raise_timeout(awaitable, *args, **kwargs):
            if hasattr(awaitable, "close"):
                awaitable.close()  # suppress "coroutine never awaited"
            raise asyncio.TimeoutError

        monkeypatch.setattr(dual_app.asyncio, "wait_for", _raise_timeout)

        app = dual_app.create_dual_app()
        cancel_ep = next(
            r.endpoint for r in app.routes if getattr(r, "path", "") == "/job/cancel"
        )

        result = await cancel_ep(
            JobCancelByOrchestratorRequest(
                reason="test",
                recipient=PinnedJobRecipient(
                    expected_agent_id="agent-1",
                    expected_pod_uid=None,
                    expected_process_generation="process-C",
                    expected_job_id="job-C",
                ),
            )
        )

        assert result["status"] == "cancelled"
        assert result["graceful"] is False
        # The whole point: even on a hard kill the pod returns to IDLE.
        assert dual_app._pod_state == dual_app.PodState.IDLE
        assert dual_app._current_job_id is None
        assert task.cancelled()


class TestFinalIdleStatusOnExit:
    """Finding 5 of
    knowledge-history/done/stale_agent_detector_sql_crash_disables_recovery_sweeps.md:
    a one-shot worker's final heartbeat must not assert 'ready' — the process
    exits ~2s later, and a fresh-looking 'ready' row lets the dispatcher
    claim a job for the dead pod during the ~3-min offline window."""

    @pytest.mark.asyncio
    async def test_non_loop_reset_asserts_draining(self, monkeypatch):
        from agent.api import dual_app

        client = _reset_module(dual_app)
        monkeypatch.delenv("AGENT_LOOP", raising=False)

        assert dual_app._final_idle_status() == "draining"
        await dual_app._reset_to_idle(
            "test exit", final_status=dual_app._final_idle_status()
        )

        assert client.heartbeat.await_args.kwargs["status"] == "draining"

    @pytest.mark.asyncio
    async def test_loop_mode_reset_asserts_ready(self, monkeypatch):
        from agent.api import dual_app

        client = _reset_module(dual_app)
        monkeypatch.setenv("AGENT_LOOP", "1")

        assert dual_app._final_idle_status() == "ready"
        await dual_app._reset_to_idle(
            "test loop", final_status=dual_app._final_idle_status()
        )

        assert client.heartbeat.await_args.kwargs["status"] == "ready"


class TestScheduleExitDeregisters:
    """Every graceful exit through _schedule_exit deregisters the agent.

    os._exit bypasses the lifespan shutdown, so without the in-path
    deregister every clean completion/drain exit leaves an agents row the
    3-minute heartbeat sweep flips to offline and reports as a
    fleet:agents_offline corpse. Best-effort only: a hung or failing
    deregister must never hold up or abort the exit.
    """

    async def _run_scheduled_exit(self, dual_app):
        with patch("agent.api.dual_app.os._exit") as fake_exit:
            dual_app._schedule_exit(delay=0)
            await asyncio.wait_for(dual_app._pending_exit_task, timeout=2.0)
        return fake_exit

    @pytest.mark.asyncio
    async def test_deregisters_then_exits(self):
        from agent.api import dual_app

        client = _reset_module(dual_app)

        fake_exit = await self._run_scheduled_exit(dual_app)

        client.deregister.assert_awaited_once()
        client.stop_heartbeat.assert_called_once()
        fake_exit.assert_called_once_with(0)

    @pytest.mark.asyncio
    async def test_exit_proceeds_when_deregister_hangs(self, monkeypatch):
        from agent.api import dual_app

        client = _reset_module(dual_app)

        async def _hang():
            await asyncio.sleep(60)

        client.deregister = AsyncMock(side_effect=_hang)
        monkeypatch.setattr(dual_app, "_DEREGISTER_ON_EXIT_TIMEOUT_S", 0.05)

        fake_exit = await self._run_scheduled_exit(dual_app)

        fake_exit.assert_called_once_with(0)

    @pytest.mark.asyncio
    async def test_exit_proceeds_when_deregister_errors(self):
        from agent.api import dual_app

        client = _reset_module(dual_app)
        client.deregister = AsyncMock(side_effect=RuntimeError("orchestrator 500"))

        fake_exit = await self._run_scheduled_exit(dual_app)

        client.deregister.assert_awaited_once()
        fake_exit.assert_called_once_with(0)

    @pytest.mark.asyncio
    async def test_exit_proceeds_without_client(self):
        from agent.api import dual_app

        _reset_module(dual_app)
        dual_app._orchestrator_client = None

        fake_exit = await self._run_scheduled_exit(dual_app)

        fake_exit.assert_called_once_with(0)

    @pytest.mark.asyncio
    async def test_heartbeat_task_cancelled_before_deregister(self):
        # A heartbeat landing mid-deregister would 404 and re-register,
        # resurrecting the row the exit just deleted.
        from agent.api import dual_app

        client = _reset_module(dual_app)
        hb = asyncio.create_task(asyncio.sleep(60))
        dual_app._heartbeat_task = hb
        try:
            fake_exit = await self._run_scheduled_exit(dual_app)

            assert hb.cancelled() or hb.cancelling()
            client.deregister.assert_awaited_once()
            fake_exit.assert_called_once_with(0)
        finally:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb

    @pytest.mark.asyncio
    async def test_unregistered_client_skips_deregister_call(self):
        # Never registered (agent_id unset) → nothing to delete, but the
        # heartbeat loop must still be stopped so it can't register a row
        # for a dying pod.
        from agent.api import dual_app

        client = _reset_module(dual_app)
        client.agent_id = None

        fake_exit = await self._run_scheduled_exit(dual_app)

        client.deregister.assert_not_awaited()
        client.stop_heartbeat.assert_called_once()
        fake_exit.assert_called_once_with(0)
