"""Regression tests for the resume endpoint's delegation to the dispatcher path.

Covers the 2026-07-17 incident where the cockpit Resume button's direct
fast-path POSTed the bare persisted ``jobs.config_override`` (creation-time,
no env_keys / LLM credentials / workspace config) straight to the agent's
``/job/resume``. Jobs landing on a fresh (clean-env) agent pod then failed —
reranker bind hard-fail or missing workspace SSH config (5 jobs killed).

The fix delegates the fast-path to ``_resume_job_on_agent`` — the dispatcher's
resume path, which performs the full dispatch-time injection — so these tests
pin (a) that the shared path injects before POSTing, and (b) that the endpoint
actually delegates to it and falls back to queue-for-dispatch on decline.

See docs/issues/job_resume_direct_path_skips_credential_injection.md.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add orchestrator/ to sys.path so its top-level modules import bare.
_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

import main  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

JOB_ID = "00000000-0000-0000-0000-000000000001"
AGENT_ID = "00000000-0000-0000-0000-0000000000a1"
PROJECT_ID = "00000000-0000-0000-0000-0000000000bb"

INJECTED_ENV = {
    "EMBEDDING_MODEL": "qwen3-embedding-8b",
    "EMBEDDING_BASE_URL": "http://router/v1",
    "EMBEDDING_API_KEY": "sk-emb",
}


def _job(**overrides) -> dict:
    job = {
        "id": JOB_ID,
        "user_id": None,
        "project_id": None,
        "config_name": "default",
        "config_override": {"llm": {"model": "gpt-5.6-terra"}},
        "context": {},
        "status": "paused",
        "assigned_agent_id": None,
        "priority": 1,
    }
    job.update(overrides)
    return job


def _agent(**overrides) -> dict:
    agent = {
        "id": AGENT_ID,
        "status": "ready",
        "pod_ip": "10.0.0.9",
        "pod_port": 8080,
    }
    agent.update(overrides)
    return agent


class _FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient capturing the resume POST."""

    posts: list[tuple[str, dict]] = []
    next_status: int = 202

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        _FakeAsyncClient.posts.append((url, json))
        return _FakeResponse(_FakeAsyncClient.next_status)


@pytest.fixture
def fake_conn(monkeypatch):
    """postgres_db.acquire() -> conn with a recorded execute()."""
    conn = SimpleNamespace(execute=AsyncMock())

    @asynccontextmanager
    async def _acquire():
        yield conn

    monkeypatch.setattr(main.postgres_db, "acquire", _acquire)
    return conn


@pytest.fixture
def injector(monkeypatch):
    """Replace `_inject_dispatch_credentials` with a marker injector."""

    async def _fake(job, config_override, *, include_kb_profile=False):
        config_override = config_override or {}
        config_override.setdefault("env_keys", {}).update(INJECTED_ENV)
        config_override.setdefault("llm", {})["api_key"] = "sk-llm"
        return config_override

    mock = AsyncMock(side_effect=_fake)
    monkeypatch.setattr(main, "_inject_dispatch_credentials", mock)
    return mock


@pytest.fixture
def resume_collaborators(monkeypatch, fake_conn, injector):
    """Patch every DB/transport collaborator of `_resume_job_on_agent`."""
    _FakeAsyncClient.posts = []
    _FakeAsyncClient.next_status = 202
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)

    monkeypatch.setattr(main, "_user_experts_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(
        main.postgres_db, "resolve_datasources_for_job", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(main, "_apply_cloud_storage_override", MagicMock())
    monkeypatch.setattr(main, "_build_datasources_payload", MagicMock(return_value=[]))
    monkeypatch.setattr(
        main,
        "_build_datasource_tool_override",
        MagicMock(side_effect=lambda ds, co: co),
    )
    monkeypatch.setattr(main.postgres_db, "delete_job_context_keys", AsyncMock())
    monkeypatch.setattr(main.postgres_db, "update_job_status", AsyncMock())
    monkeypatch.setattr(main.postgres_db, "heartbeat", AsyncMock())
    return SimpleNamespace(conn=fake_conn, injector=injector)


def _posted_payload() -> dict:
    assert len(_FakeAsyncClient.posts) == 1, "expected exactly one resume POST"
    return _FakeAsyncClient.posts[0][1]


# ---------------------------------------------------------------------------
# _resume_job_on_agent — the shared payload builder
# ---------------------------------------------------------------------------


class TestResumeJobOnAgentInjection:
    @pytest.mark.asyncio
    async def test_injected_credentials_reach_the_posted_payload(
        self, resume_collaborators
    ):
        ok = await main._resume_job_on_agent(_job(project_id=PROJECT_ID), _agent())

        assert ok is True
        payload = _posted_payload()
        assert payload["config_override"]["env_keys"] == INJECTED_ENV
        assert payload["config_override"]["llm"]["api_key"] == "sk-llm"
        main.postgres_db.update_job_status.assert_awaited_once_with(
            job_id=JOB_ID, status="processing", assigned_agent_id=AGENT_ID
        )

    @pytest.mark.asyncio
    async def test_kb_profile_follows_project_scope(self, resume_collaborators):
        await main._resume_job_on_agent(_job(project_id=PROJECT_ID), _agent())
        assert (
            resume_collaborators.injector.await_args.kwargs["include_kb_profile"]
            is True
        )

    @pytest.mark.asyncio
    async def test_kb_profile_off_without_project_or_kb_datasource(
        self, resume_collaborators
    ):
        await main._resume_job_on_agent(_job(), _agent())
        assert (
            resume_collaborators.injector.await_args.kwargs["include_kb_profile"]
            is False
        )

    @pytest.mark.asyncio
    async def test_kb_datasource_alone_enables_kb_profile(
        self, resume_collaborators, monkeypatch
    ):
        monkeypatch.setattr(
            main.postgres_db,
            "resolve_datasources_for_job",
            AsyncMock(return_value=[{"type": "kb", "id": "ds-1"}]),
        )
        await main._resume_job_on_agent(_job(), _agent())
        assert (
            resume_collaborators.injector.await_args.kwargs["include_kb_profile"]
            is True
        )

    @pytest.mark.asyncio
    async def test_config_upload_id_passthrough(self, resume_collaborators):
        await main._resume_job_on_agent(
            _job(context={"config_upload_id": "upload-7"}), _agent()
        )
        assert _posted_payload()["config_upload_id"] == "upload-7"

    @pytest.mark.asyncio
    async def test_config_upload_id_absent_stays_off_the_wire(
        self, resume_collaborators
    ):
        await main._resume_job_on_agent(_job(), _agent())
        assert "config_upload_id" not in _posted_payload()

    @pytest.mark.asyncio
    async def test_queued_feedback_delivered_then_cleared(self, resume_collaborators):
        ok = await main._resume_job_on_agent(
            _job(context={"queued_feedback": "fix the tests"}), _agent()
        )

        assert ok is True
        assert _posted_payload()["feedback"] == "fix the tests"
        main.postgres_db.delete_job_context_keys.assert_awaited_once_with(
            JOB_ID, ["queued_feedback"]
        )


class TestResumeJobOnAgentRejection:
    @pytest.mark.asyncio
    async def test_409_demotes_stale_ready_agent(self, resume_collaborators):
        _FakeAsyncClient.next_status = 409

        ok = await main._resume_job_on_agent(_job(), _agent())

        assert ok is False
        demote_sql = resume_collaborators.conn.execute.await_args.args[0]
        assert "UPDATE agents SET status = 'working'" in demote_sql
        main.postgres_db.update_job_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_rejects_do_not_demote(self, resume_collaborators):
        _FakeAsyncClient.next_status = 500

        ok = await main._resume_job_on_agent(_job(), _agent())

        assert ok is False
        resume_collaborators.conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejected_resume_keeps_queued_feedback(self, resume_collaborators):
        _FakeAsyncClient.next_status = 409

        await main._resume_job_on_agent(
            _job(context={"queued_feedback": "keep me"}), _agent()
        )

        main.postgres_db.delete_job_context_keys.assert_not_awaited()


# ---------------------------------------------------------------------------
# resume_job endpoint — delegation + fallback
# ---------------------------------------------------------------------------


@pytest.fixture
def endpoint_collaborators(monkeypatch, fake_conn):
    """Patch the endpoint's collaborators around the delegation seam."""
    job = _job(assigned_agent_id=AGENT_ID)
    agent = _agent()

    monkeypatch.setattr(
        main, "require_internal_or_job_access", AsyncMock(return_value=(None, job))
    )
    monkeypatch.setattr(main, "_user_experts_enabled", AsyncMock(return_value=False))
    monkeypatch.setattr(main.postgres_db, "get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(main.postgres_db, "merge_job_context", AsyncMock())
    queue_for_resume = AsyncMock(return_value=True)
    monkeypatch.setattr(main.postgres_db, "queue_job_for_resume", queue_for_resume)
    monkeypatch.setattr(main, "snapshot_service", SimpleNamespace(is_available=False))
    monkeypatch.setattr(main, "_trigger_dispatch", MagicMock())
    delegate = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "_resume_job_on_agent", delegate)
    return SimpleNamespace(
        job=job,
        agent=agent,
        delegate=delegate,
        conn=fake_conn,
        queue_for_resume=queue_for_resume,
    )


class TestResumeEndpointDelegation:
    @pytest.mark.asyncio
    async def test_fast_path_delegates_to_shared_resume(self, endpoint_collaborators):
        result = await main.resume_job(MagicMock(), JOB_ID, main.JobResumeRequest())

        assert result == {
            "status": "resumed",
            "job_id": JOB_ID,
            "agent_id": AGENT_ID,
        }
        endpoint_collaborators.delegate.assert_awaited_once()
        args = endpoint_collaborators.delegate.await_args.args
        assert args[0]["id"] == JOB_ID
        assert args[1] is endpoint_collaborators.agent
        main.postgres_db.merge_job_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_feedback_is_merged_and_stamped_on_the_delegated_job(
        self, endpoint_collaborators
    ):
        await main.resume_job(
            MagicMock(), JOB_ID, main.JobResumeRequest(feedback="try again")
        )

        main.postgres_db.merge_job_context.assert_awaited_once_with(
            JOB_ID, {"queued_feedback": "try again"}
        )
        delegated_job = endpoint_collaborators.delegate.await_args.args[0]
        assert delegated_job["context"]["queued_feedback"] == "try again"

    @pytest.mark.asyncio
    async def test_declined_resume_falls_back_to_queue(self, endpoint_collaborators):
        endpoint_collaborators.delegate.return_value = False

        result = await main.resume_job(MagicMock(), JOB_ID, main.JobResumeRequest())

        assert result["status"] == "queued"
        assert result["job_id"] == JOB_ID
        # The re-queue WRITE itself (status flip, agent unassign, freeze shed —
        # the dispatcher-visibility contract) is locked against real Postgres in
        # tests/test_queue_job_for_resume.py. Here we only pin the seam: a
        # declined delegation must fall back to that write.
        endpoint_collaborators.queue_for_resume.assert_awaited_once_with(JOB_ID, None)
