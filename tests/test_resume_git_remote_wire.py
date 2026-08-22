"""The /job/resume wire must deliver git_remote_url into process_job metadata.

Agent-side half of knowledge-base/knowledge/issues/resume_fresh_workspace_no_clone_fallback.md:
the pod-handoff clone fallback in ``_setup_job_workspace`` keys on
``metadata["git_remote_url"]``, and ``JobResumeRequest`` historically had no
such field — the orchestrator could not send it, so a resume onto a fresh
workspace with no snapshot always blank-inited while the whole job history
sat in Gitea. These tests pin the dual_app handler seam (route-extraction
pattern from tests/test_dual_app_stop_reset.py).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import src.api.dual_app as dual_app
from src.api.models import JobResumeRequest


def _empty_async_gen():
    async def _gen():
        if False:  # pragma: no cover
            yield

    return _gen()


def _wire_idle_agent(monkeypatch):
    """A dual_app module primed with an idle mock agent."""
    agent = MagicMock()
    agent.process_job = AsyncMock(return_value=_empty_async_gen())
    monkeypatch.setattr(dual_app, "_agent", agent)
    monkeypatch.setattr(dual_app, "_pod_state", dual_app.PodState.IDLE)
    monkeypatch.setattr(dual_app, "_shutdown_requested", False)
    monkeypatch.setattr(dual_app, "_orchestrator_client", None)
    monkeypatch.setattr(dual_app, "_reset_to_idle", AsyncMock())
    # Keep the post-completion exit scheduler out of the test process.
    monkeypatch.setattr(dual_app, "_should_loop", lambda: True)
    dual_app._stop_requested.clear()
    return agent


def _resume_endpoint():
    app = dual_app.create_dual_app()
    return next(
        r.endpoint for r in app.routes if getattr(r, "path", "") == "/job/resume"
    )


def _sandbox_runtime() -> dict[str, str]:
    """Production-shaped server authority attached to every worker resume."""
    return {
        "requested_backend": "sandbox",
        "assigned_backend": "sandbox",
        "effective_backend": "sandbox",
        "state": "ready",
    }


def _sandbox_config() -> dict[str, object]:
    return {
        "workspace": {
            "backend": "sandbox",
            "remote": {"host": "workspace-test.svc"},
        }
    }


@pytest.mark.asyncio
async def test_resume_endpoint_forwards_git_remote_into_metadata(monkeypatch):
    agent = _wire_idle_agent(monkeypatch)
    resume_ep = _resume_endpoint()

    await resume_ep(
        JobResumeRequest(
            job_id="j1",
            previous_status="paused",
            git_remote_url="http://srw-gitea:3000/srw/job-j1.git",
            config_override=_sandbox_config(),
            workspace_runtime=_sandbox_runtime(),
        )
    )
    await dual_app._current_job_task

    kwargs = agent.process_job.await_args.kwargs
    assert kwargs["resume"] is True
    assert kwargs["metadata"]["git_remote_url"] == (
        "http://srw-gitea:3000/srw/job-j1.git"
    )


@pytest.mark.asyncio
async def test_resume_endpoint_omits_git_remote_when_not_sent(monkeypatch):
    agent = _wire_idle_agent(monkeypatch)
    resume_ep = _resume_endpoint()

    await resume_ep(
        JobResumeRequest(
            job_id="j2",
            previous_status="paused",
            config_override=_sandbox_config(),
            workspace_runtime=_sandbox_runtime(),
        )
    )
    await dual_app._current_job_task

    metadata = agent.process_job.await_args.kwargs["metadata"]
    assert not metadata or "git_remote_url" not in metadata
