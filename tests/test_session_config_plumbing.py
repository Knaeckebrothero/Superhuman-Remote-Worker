"""Pins for the session config_name plumbing + detach-then-delete fixes.

Covers the three holes found during the memory-overhaul Phase-1 closure
step 1 (2026-06-11):

- Hole A: a bare ``POST /api/persistent/threads`` must land on the
  persistent base config, not the worker one
  (docs/issues/session_config_name_plumbing.md).
- Hole B: the idle-pool ``/session/attach`` path must carry the thread's
  config_name (orchestrator side) and resolve it as the session base
  (agent side) — otherwise pool-attached sessions bind the worker memory
  pipeline and silently lose ``teardown_extractor``.
- B11 k8s route: the user-facing thread DELETE must give a live session
  agent the chance to terminate (final memory capture + git push) before
  the workspace and pod are torn down (memory_bugs.md B11 addendum).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import orchestrator.main as orch_main
from src.api.persistent_app import _load_expert_config


class _FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class _FakeAsyncClient:
    """httpx.AsyncClient stand-in recording the last POST."""

    calls: list = []
    response_status: int = 200
    raise_on_post: Exception | None = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None):
        if _FakeAsyncClient.raise_on_post is not None:
            raise _FakeAsyncClient.raise_on_post
        _FakeAsyncClient.calls.append({"url": url, "json": json})
        return _FakeResponse(_FakeAsyncClient.response_status)


@pytest.fixture(autouse=True)
def _reset_fake_client():
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response_status = 200
    _FakeAsyncClient.raise_on_post = None
    yield


class TestThreadCreateDefault:
    """Hole A: the request-model default."""

    def test_bare_thread_create_defaults_to_persistent_config(self):
        assert orch_main.ThreadCreateRequest().config_name == "persistent_defaults"


class TestSendSessionAttachPayload:
    """Hole B, orchestrator side: the attach payload carries config_name."""

    @pytest.mark.asyncio
    async def test_payload_carries_config_name(self):
        _FakeAsyncClient.response_status = 500  # skip the DB-binding branch
        with patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient):
            ok = await orch_main._send_session_attach(
                {"id": "a1", "pod_ip": "10.0.0.1", "pod_port": 8001},
                "tid-1",
                {"llm": {"model": "m"}},
                ["p1"],
                datasources=None,
                config_name="persistent_defaults",
            )
        assert ok is False
        assert len(_FakeAsyncClient.calls) == 1
        call = _FakeAsyncClient.calls[0]
        assert call["url"] == "http://10.0.0.1:8001/session/attach"
        assert call["json"]["config_name"] == "persistent_defaults"
        assert call["json"]["thread_id"] == "tid-1"


class TestAttachRoutesForwardConfigName:
    """Hole B: BOTH /session/attach routes must forward config_name.

    The job pool runs the dual app, dedicated session pods run the
    persistent app — each registers its own /session/attach. The first
    live verify missed the dual route (it answered 200 and silently
    dropped config_name), so pin both at source level.
    """

    def test_both_attach_routes_forward_config_name(self):
        import inspect

        import src.api.dual_app as dual_app
        import src.api.persistent_app as papp

        assert 'config_name=request.get("config_name")' in inspect.getsource(dual_app)
        assert 'config_name=request.get("config_name")' in inspect.getsource(papp)

    def test_dual_detach_uses_rest_detach_reason(self):
        """Both detach routes must terminate with the documented
        "rest_detach" reason — the dual route used the "legacy" shim,
        which broke the greppable Terminate(rest_detach) signal."""
        import inspect

        import src.api.dual_app as dual_app

        assert '_terminate_session("rest_detach")' in inspect.getsource(dual_app)


class TestLoadExpertConfig:
    """Hole B, agent side: the named config resolves with its own pipeline."""

    def test_persistent_name_yields_persistent_pipeline(self):
        cfg = _load_expert_config("persistent_defaults")
        assert cfg.memory.pipeline.writers == [
            "persistent_interval_extractor",
            "teardown_extractor",
        ]

    def test_worker_name_yields_worker_pipeline(self):
        cfg = _load_expert_config("defaults")
        assert "teardown_extractor" not in cfg.memory.pipeline.writers
        assert "interval_extractor" in cfg.memory.pipeline.writers

    def test_unknown_name_raises(self):
        with pytest.raises(Exception):
            _load_expert_config("no-such-config-xyz")


class TestDetachAgentSession:
    """B11 k8s route: detach-then-delete preconditions and outcomes."""

    def _db(self, thread, agent_row):
        return SimpleNamespace(
            get_thread=AsyncMock(return_value=thread),
            fetchrow=AsyncMock(return_value=agent_row),
        )

    @pytest.mark.asyncio
    async def test_no_bound_agent_skips_without_http(self):
        db = self._db({"id": "t1", "agent_id": None}, None)
        with (
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            assert await orch_main._detach_agent_session("t1") is False
        assert _FakeAsyncClient.calls == []

    @pytest.mark.asyncio
    async def test_non_session_agent_skips_without_http(self):
        """Offline/ready agents (the orphan-reaper case) must not stall."""
        db = self._db(
            {"id": "t1", "agent_id": "a1"},
            {"pod_ip": "10.0.0.2", "pod_port": 8001, "status": "ready"},
        )
        with (
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            assert await orch_main._detach_agent_session("t1") is False
        assert _FakeAsyncClient.calls == []

    @pytest.mark.asyncio
    async def test_live_session_agent_detaches(self):
        db = self._db(
            {"id": "t1", "agent_id": "a1"},
            {"pod_ip": "10.0.0.2", "pod_port": 8001, "status": "session"},
        )
        with (
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            assert await orch_main._detach_agent_session("t1") is True
        assert _FakeAsyncClient.calls[0]["url"] == "http://10.0.0.2:8001/session/detach"

    @pytest.mark.asyncio
    async def test_http_failure_is_contained(self):
        """Teardown must proceed even when the agent is unreachable."""
        _FakeAsyncClient.raise_on_post = ConnectionError("refused")
        db = self._db(
            {"id": "t1", "agent_id": "a1"},
            {"pod_ip": "10.0.0.2", "pod_port": 8001, "status": "session"},
        )
        with (
            patch.object(orch_main, "postgres_db", db),
            patch.object(orch_main.httpx, "AsyncClient", _FakeAsyncClient),
        ):
            assert await orch_main._detach_agent_session("t1") is False

    @pytest.mark.asyncio
    async def test_release_runs_detach_before_workspace_cleanup(self):
        """Ordering pin: the agent must get its terminate (git push needs
        the workspace alive) before the workspace archive/cleanup step."""
        order: list = []

        async def _detach(thread_id, timeout=150.0):
            order.append("detach")
            return True

        async def _archive(thread_id, entity_type):
            order.append("workspace")

        provisioner = SimpleNamespace(
            is_available=True,
            delete_agent_pod_by_thread=AsyncMock(
                side_effect=lambda tid: order.append("pod") or True
            ),
        )
        with (
            patch.object(orch_main, "_detach_agent_session", _detach),
            patch.object(orch_main, "_archive_and_cleanup_workspace", _archive),
            patch.object(orch_main, "agent_provisioner", provisioner),
        ):
            await orch_main._release_thread_resources("t1")
        assert order == ["detach", "workspace", "pod"]
