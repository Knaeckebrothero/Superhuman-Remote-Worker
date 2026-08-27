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

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.api.app as primary_app
import src.api.dual_app as dual_app
from orchestrator.services.config_resolver import resolve_config
from src.api.models import JobResumeRequest
from src.core.loader import get_all_tool_names, load_config_from_resolved


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


def _primary_resume_endpoint():
    app = primary_app.create_app()
    return next(
        r.endpoint for r in app.routes if getattr(r, "path", "") == "/job/resume"
    )


@pytest.mark.asyncio
async def test_worker_ready_endpoints_advertise_resolved_resume(monkeypatch):
    agent = MagicMock()
    agent.get_status.return_value = {
        "initialized": True,
        "connections": {"postgres": True},
    }

    monkeypatch.setattr(primary_app, "_agent", agent)
    monkeypatch.setattr(primary_app, "_shutdown_requested", False)
    primary_ready = next(
        route.endpoint
        for route in primary_app.create_app().routes
        if getattr(route, "path", "") == "/ready"
    )
    primary_response = await primary_ready()

    monkeypatch.setattr(dual_app, "_agent", agent)
    monkeypatch.setattr(dual_app, "_shutdown_requested", False)
    monkeypatch.setattr(dual_app, "_pod_state", dual_app.PodState.IDLE)
    dual_ready = next(
        route.endpoint
        for route in dual_app.create_dual_app().routes
        if getattr(route, "path", "") == "/ready"
    )
    dual_response = await dual_ready()

    assert primary_response.capabilities["resolved_config_resume"] is True
    assert dual_response.capabilities["resolved_config_resume"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protected_required", "joined_ready", "expected_protected_ready"),
    [(False, True, False), (True, False, False), (True, True, True)],
)
async def test_dual_session_ready_serializes_exact_protected_contract(
    monkeypatch, protected_required, joined_ready, expected_protected_ready
):
    """A protected warm attach on a dual agent advertises the same exact
    readiness contract as persistent mode; ordinary sessions remain ready
    without claiming an active protected mount."""

    import src.api.persistent_app as persistent_app

    agent = MagicMock()
    agent.get_status.return_value = {
        "initialized": True,
        "connections": {"postgres": True},
    }
    session = MagicMock()
    session.protected_cloud_required = protected_required
    session.protected_cloud_ready.return_value = joined_ready
    monkeypatch.setattr(dual_app, "_agent", agent)
    monkeypatch.setattr(dual_app, "_shutdown_requested", False)
    monkeypatch.setattr(dual_app, "_pod_state", dual_app.PodState.SESSION)
    monkeypatch.setattr(persistent_app, "_session", session)
    monkeypatch.setattr(persistent_app, "_session_ready", lambda: joined_ready)

    ready = next(
        route.endpoint
        for route in dual_app.create_dual_app().routes
        if getattr(route, "path", "") == "/ready"
    )
    response = await ready()
    serialized = json.loads(response.model_dump_json())

    assert serialized["ready"] is joined_ready
    assert serialized["capabilities"]["durable_input_delivery"] is True
    assert type(serialized["capabilities"]["protected_cloud_contract"]) is int
    assert serialized["capabilities"]["protected_cloud_contract"] == 1
    assert (
        serialized["capabilities"]["protected_cloud_ready"] is expected_protected_ready
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
    assert not metadata or "resolved_config" not in metadata


@pytest.mark.asyncio
async def test_dual_resume_endpoint_hydrates_non_default_expert_tools(monkeypatch):
    agent = _wire_idle_agent(monkeypatch)
    resume_ep = _resume_endpoint()
    blob = resolve_config(
        base_config_name="developer", request_override=_sandbox_config()
    )

    await resume_ep(
        JobResumeRequest(
            job_id="j3",
            config_name="developer",
            previous_status="processing",
            resolved_config=blob,
            workspace_runtime=_sandbox_runtime(),
        )
    )
    await dual_app._current_job_task

    metadata = agent.process_job.await_args.kwargs["metadata"]
    effective = load_config_from_resolved(metadata["resolved_config"])
    assert effective.agent_id == "developer"
    assert {"run_command", "cancel_command", "shell_read"} <= set(
        get_all_tool_names(effective)
    )
    assert "config_override" not in metadata


@pytest.mark.asyncio
async def test_primary_resume_endpoint_hydrates_non_default_expert_tools(monkeypatch):
    agent = MagicMock()
    agent.config.agent_id = "worker_base"
    agent.process_job = AsyncMock(return_value=_empty_async_gen())
    monkeypatch.setattr(primary_app, "_agent", agent)
    monkeypatch.setattr(primary_app, "_current_job_id", None)
    monkeypatch.setattr(primary_app, "_shutdown_requested", False)
    monkeypatch.setattr(primary_app, "_orchestrator_client", None)
    monkeypatch.setattr(primary_app, "_setup_job_file_logging", MagicMock())
    monkeypatch.setattr(primary_app, "_cleanup_job_file_handler", MagicMock())
    primary_app._stop_requested.clear()
    resume_ep = _primary_resume_endpoint()
    blob = resolve_config(
        base_config_name="developer", request_override=_sandbox_config()
    )

    await resume_ep(
        JobResumeRequest(
            job_id="j4",
            config_name="developer",
            previous_status="processing",
            resolved_config=blob,
            workspace_runtime=_sandbox_runtime(),
        ),
        MagicMock(),
    )
    await primary_app._current_job_task

    metadata = agent.process_job.await_args.kwargs["metadata"]
    effective = load_config_from_resolved(metadata["resolved_config"])
    assert effective.agent_id == "developer"
    assert {"run_command", "cancel_command", "shell_read"} <= set(
        get_all_tool_names(effective)
    )
    assert "config_override" not in metadata
