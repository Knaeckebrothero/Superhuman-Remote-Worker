"""Tests for POST /api/sessions/{tid}/prepare and GET /api/sessions/{tid}/connection."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _install_fake_auth(monkeypatch, user_id: str = "u1") -> None:
    """Replace require_approved_user on the sessions module with a fake.

    The handlers call `require_approved_user(request, db)` inline rather than
    via Depends, so app.dependency_overrides won't intercept it — we patch the
    name on the importing module instead.
    """
    from orchestrator.routers import sessions as sessions_mod

    async def _fake(request, db):
        return {"id": user_id, "is_approved": True}

    monkeypatch.setattr(sessions_mod, "require_approved_user", _fake, raising=True)


@pytest.fixture
def app(monkeypatch):
    """Minimal FastAPI app with the sessions router mounted."""
    from orchestrator.routers.sessions import router as sessions_router

    app = FastAPI()

    _install_fake_auth(monkeypatch)

    def _capture_background_task(coro):
        coro.close()
        return MagicMock()

    monkeypatch.setattr(
        "orchestrator.routers.sessions._schedule_prepare_task",
        _capture_background_task,
        raising=True,
    )

    fake_db = AsyncMock()
    fake_db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "user_id": "u1",
            "agent_id": None,
            "config_name": "persistent_defaults",
        }
    )
    # advisory lock context manager
    lock_cm = AsyncMock()
    lock_cm.__aenter__.return_value = None
    lock_cm.__aexit__.return_value = False
    fake_db.thread_advisory_lock = MagicMock(return_value=lock_cm)

    # The router uses `from main import postgres_db` — patch the symbol on the
    # router module so the late import resolves to our fake.
    monkeypatch.setattr(
        "orchestrator.routers.sessions._get_db",
        lambda: fake_db,
        raising=False,
    )

    app.include_router(sessions_router)
    return app, fake_db


def test_prepare_returns_202_immediately(app):
    """POST /prepare returns 202 with state=provisioning before any work."""
    fastapi_app, fake_db = app
    client = TestClient(fastapi_app)

    resp = client.post("/api/sessions/t1/prepare", json={})
    assert resp.status_code == 202
    assert resp.json() == {"state": "provisioning"}


def test_prepare_returns_404_for_missing_thread(app):
    """POST /prepare returns 404 if thread doesn't exist."""
    fastapi_app, fake_db = app
    fake_db.get_thread.return_value = None
    client = TestClient(fastapi_app)

    resp = client.post("/api/sessions/missing/prepare", json={})
    assert resp.status_code == 404


def test_prepare_returns_403_when_thread_owned_by_other_user(app):
    """POST /prepare returns 403 if caller is not the thread owner."""
    fastapi_app, fake_db = app
    fake_db.get_thread.return_value = {"id": "t1", "user_id": "OTHER", "agent_id": None}
    client = TestClient(fastapi_app)

    resp = client.post("/api/sessions/t1/prepare", json={})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_do_prepare_emits_phases_for_warm_thread(monkeypatch):
    """When the thread already has an agent_id and the pod is ready,
    _do_prepare skips the actual provision work, runs the readiness probe,
    calls session_router.ensure_route, and emits provisioning → booting →
    ready. "provisioning" is emitted up-front (before lock acquisition) so
    the cockpit's progress UI surfaces the phase even when /resume's
    sibling reprovision wins the race to bind the agent_id."""
    from orchestrator.routers import sessions as sessions_mod

    db = AsyncMock()
    # Already bound and ready.
    db.get_thread.return_value = {
        "id": "t1",
        "user_id": "u1",
        "agent_id": "agent-xyz",
        "config_name": "persistent_defaults",
    }
    db.get_agent.return_value = {
        "id": "agent-xyz",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
        "hostname": "srw-agent-s-deadbeef",
        "pod_uid": "k8s-uid-1",
    }
    lock_cm = AsyncMock()
    lock_cm.__aenter__.return_value = None
    lock_cm.__aexit__.return_value = False
    db.thread_advisory_lock = MagicMock(return_value=lock_cm)
    monkeypatch.setattr(sessions_mod, "_get_db", lambda: db, raising=False)

    # Mock the readiness probe to immediately return ready=true.
    async def _ready_ok(pod_ip, pod_port, timeout_s):
        return True

    monkeypatch.setattr(sessions_mod, "wait_for_ready", _ready_ok, raising=True)

    # Mock session_router on main.
    import sys

    fake_main = MagicMock()
    fake_main.session_router = AsyncMock()
    fake_main.session_router.ensure_route = AsyncMock(return_value="/p/t1")
    monkeypatch.setitem(sys.modules, "main", fake_main)

    # Capture lifecycle emits at the call site inside sessions.py — patching
    # the bound `lifecycle_emit` name avoids the dual-module-path problem
    # (orchestrator.services.session_lifecycle vs services.session_lifecycle
    # under pytest's two-rooted sys.path).
    emit_calls: list[dict] = []

    def _capture_emit(user_id, thread_id, state, **extra):
        emit_calls.append(
            {"user_id": user_id, "thread_id": thread_id, "state": state, **extra}
        )

    monkeypatch.setattr(sessions_mod, "lifecycle_emit", _capture_emit, raising=True)

    await sessions_mod._do_prepare(
        thread_id="t1",
        user_id="u1",
        config_name="persistent_defaults",
        config_override=None,
    )

    # Expected phase sequence: provisioning (up-front) → booting → ready.
    # Agent was already bound, but "provisioning" is still emitted so the
    # cockpit's resume card surfaces the phase regardless of who won the race.
    states = [c["state"] for c in emit_calls]
    assert states == ["provisioning", "booting", "ready"]

    # ensure_route called with correct pod info.
    fake_main.session_router.ensure_route.assert_called_once_with(
        thread_id="t1",
        pod_name="srw-agent-s-deadbeef",
        pod_uid="k8s-uid-1",
    )


@pytest.mark.asyncio
async def test_do_prepare_emits_failed_when_pod_not_ready(monkeypatch):
    """If readiness probe times out, _do_prepare emits failed and does not
    call session_router.ensure_route."""
    from orchestrator.routers import sessions as sessions_mod

    db = AsyncMock()
    db.get_thread.return_value = {
        "id": "t1",
        "user_id": "u1",
        "agent_id": "agent-xyz",
        "config_name": "persistent_defaults",
    }
    db.get_agent.return_value = {
        "id": "agent-xyz",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
        "hostname": "srw-agent-x",
        "pod_uid": "uid",
    }
    lock_cm = AsyncMock()
    lock_cm.__aenter__.return_value = None
    lock_cm.__aexit__.return_value = False
    db.thread_advisory_lock = MagicMock(return_value=lock_cm)
    monkeypatch.setattr(sessions_mod, "_get_db", lambda: db, raising=False)

    # Readiness times out.
    async def _ready_timeout(pod_ip, pod_port, timeout_s):
        return False

    monkeypatch.setattr(sessions_mod, "wait_for_ready", _ready_timeout, raising=True)

    import sys

    fake_main = MagicMock()
    fake_main.session_router = AsyncMock()
    fake_main.session_router.ensure_route = AsyncMock()
    monkeypatch.setitem(sys.modules, "main", fake_main)

    emit_calls: list[dict] = []

    def _capture_emit(user_id, thread_id, state, **extra):
        emit_calls.append(
            {"user_id": user_id, "thread_id": thread_id, "state": state, **extra}
        )

    monkeypatch.setattr(sessions_mod, "lifecycle_emit", _capture_emit, raising=True)

    await sessions_mod._do_prepare(
        thread_id="t1",
        user_id="u1",
        config_name="persistent_defaults",
        config_override=None,
    )

    states = [c["state"] for c in emit_calls]
    assert "failed" in states
    fake_main.session_router.ensure_route.assert_not_called()


@pytest.mark.asyncio
async def test_do_prepare_waits_when_agent_pod_marker_in_flight(monkeypatch):
    """If create-thread already created a session pod, prepare should wait
    for that pod to register instead of provisioning a duplicate."""
    from orchestrator.routers import sessions as sessions_mod

    marker = {
        "status": "created",
        "pod_name": "srw-agent-s-existing",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    db = AsyncMock()
    db.get_thread = AsyncMock(
        side_effect=[
            {
                "id": "t1",
                "user_id": "u1",
                "agent_id": None,
                "config_name": "persistent_defaults",
                "metadata": {"agent_pod": marker},
            },
            {
                "id": "t1",
                "user_id": "u1",
                "agent_id": "agent-xyz",
                "config_name": "persistent_defaults",
            },
        ]
    )
    db.get_agent.return_value = {
        "id": "agent-xyz",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
        "hostname": "srw-agent-s-existing",
        "pod_uid": "k8s-uid-1",
    }
    lock_cm = AsyncMock()
    lock_cm.__aenter__.return_value = None
    lock_cm.__aexit__.return_value = False
    db.thread_advisory_lock = MagicMock(return_value=lock_cm)
    monkeypatch.setattr(sessions_mod, "_get_db", lambda: db, raising=False)

    provision = AsyncMock()
    monkeypatch.setattr(
        sessions_mod, "_provision_agent_for_thread", provision, raising=True
    )

    async def _bound(*args, **kwargs):
        return True

    async def _ready_ok(pod_ip, pod_port, timeout_s):
        return True

    monkeypatch.setattr(sessions_mod, "wait_for_binding", _bound, raising=True)
    monkeypatch.setattr(sessions_mod, "wait_for_ready", _ready_ok, raising=True)

    import sys

    fake_main = MagicMock()
    fake_main.session_router = AsyncMock()
    fake_main.session_router.ensure_route = AsyncMock(return_value="/p/t1")
    monkeypatch.setitem(sys.modules, "main", fake_main)

    emit_calls: list[dict] = []

    def _capture_emit(user_id, thread_id, state, **extra):
        emit_calls.append(
            {"user_id": user_id, "thread_id": thread_id, "state": state, **extra}
        )

    monkeypatch.setattr(sessions_mod, "lifecycle_emit", _capture_emit, raising=True)

    await sessions_mod._do_prepare(
        thread_id="t1",
        user_id="u1",
        config_name="persistent_defaults",
        config_override=None,
    )

    provision.assert_not_awaited()
    states = [c["state"] for c in emit_calls]
    assert states == ["provisioning", "booting", "ready"]
    fake_main.session_router.ensure_route.assert_called_once_with(
        thread_id="t1",
        pod_name="srw-agent-s-existing",
        pod_uid="k8s-uid-1",
    )


# --------------------------------------------------------------------------- #
# GET /api/sessions/{tid}/connection
# --------------------------------------------------------------------------- #


def test_connection_returns_ws_url_and_token_when_ready(monkeypatch):
    """GET /connection returns 200 with ws_url + token when bound + ready."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from orchestrator.routers.sessions import router as sessions_router
    from orchestrator.services.session_tokens import SessionTokenService

    app = FastAPI()
    _install_fake_auth(monkeypatch)

    fake_db = AsyncMock()
    fake_db.get_thread.return_value = {
        "id": "t1",
        "user_id": "u1",
        "agent_id": "agent-xyz",
    }
    fake_db.get_agent.return_value = {
        "id": "agent-xyz",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
        "status": "ready",
        "hostname": "srw-agent-x",
        "pod_uid": "k8s-uid-xyz",
    }
    from orchestrator.routers import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "_get_db", lambda: fake_db, raising=False)

    # /connection probes the agent's /ready before minting; stub it to
    # always pass so we exercise the 200 path.
    async def _probe_ok(pod_ip, pod_port):
        return True

    monkeypatch.setattr(sessions_mod, "probe_ready", _probe_ok, raising=True)

    # Inject a real SessionTokenService and a fake session_router.
    test_tokens = SessionTokenService(secret="test-secret-do-not-use", ttl_seconds=60)
    import sys

    fake_main = MagicMock()
    fake_main.session_tokens = test_tokens
    fake_main.session_router = MagicMock()
    fake_main.session_router.ensure_route = AsyncMock(return_value="/p/t1")
    monkeypatch.setitem(sys.modules, "main", fake_main)
    monkeypatch.setenv("SESSION_INGRESS_HOST", "api.test.example")

    app.include_router(sessions_router)
    client = TestClient(app)
    resp = client.get("/api/sessions/t1/connection")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ready"
    assert body["ws_url"].startswith("wss://api.test.example/p/t1/ws?t=")
    assert isinstance(body["token"], str) and body["token"]
    assert isinstance(body["expires_at"], int)
    # ensure_route is called idempotently on every /connection.
    fake_main.session_router.ensure_route.assert_called_once_with(
        thread_id="t1",
        pod_name="srw-agent-x",
        pod_uid="k8s-uid-xyz",
    )


def test_connection_returns_425_when_thread_unbound(monkeypatch):
    """If thread.agent_id is None, return 425 (cockpit must POST /prepare)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from orchestrator.routers.sessions import router as sessions_router

    app = FastAPI()
    _install_fake_auth(monkeypatch)

    fake_db = AsyncMock()
    fake_db.get_thread.return_value = {"id": "t1", "user_id": "u1", "agent_id": None}
    from orchestrator.routers import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "_get_db", lambda: fake_db, raising=False)

    app.include_router(sessions_router)
    client = TestClient(app)
    resp = client.get("/api/sessions/t1/connection")
    assert resp.status_code == 425


def test_connection_returns_404_for_missing_thread(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from orchestrator.routers.sessions import router as sessions_router

    app = FastAPI()
    _install_fake_auth(monkeypatch)
    fake_db = AsyncMock()
    fake_db.get_thread.return_value = None
    from orchestrator.routers import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "_get_db", lambda: fake_db, raising=False)

    app.include_router(sessions_router)
    client = TestClient(app)
    resp = client.get("/api/sessions/missing/connection")
    assert resp.status_code == 404


def test_connection_returns_403_when_other_user(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from orchestrator.routers.sessions import router as sessions_router

    app = FastAPI()
    _install_fake_auth(monkeypatch)
    fake_db = AsyncMock()
    fake_db.get_thread.return_value = {
        "id": "t1",
        "user_id": "OTHER",
        "agent_id": "agent-xyz",
    }
    from orchestrator.routers import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "_get_db", lambda: fake_db, raising=False)

    app.include_router(sessions_router)
    client = TestClient(app)
    resp = client.get("/api/sessions/t1/connection")
    assert resp.status_code == 403


def test_connection_returns_409_when_agent_not_ready(monkeypatch):
    """If the bound agent has no pod_ip or wrong status, return 409."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from orchestrator.routers.sessions import router as sessions_router

    app = FastAPI()
    _install_fake_auth(monkeypatch)
    fake_db = AsyncMock()
    fake_db.get_thread.return_value = {
        "id": "t1",
        "user_id": "u1",
        "agent_id": "agent-xyz",
    }
    fake_db.get_agent.return_value = {
        "id": "agent-xyz",
        "pod_ip": None,
        "status": "booting",
    }
    from orchestrator.routers import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "_get_db", lambda: fake_db, raising=False)

    app.include_router(sessions_router)
    client = TestClient(app)
    resp = client.get("/api/sessions/t1/connection")
    assert resp.status_code == 409


def test_connection_returns_425_when_pod_not_session_ready(monkeypatch):
    """If the agent is bound but its /ready endpoint reports not ready
    (Uvicorn still in lifespan / _attach_session not finished), return
    425 so the cockpit's _pollConnectionUntilReady waits instead of
    opening a WS that would 503 at Traefik."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from orchestrator.routers.sessions import router as sessions_router

    app = FastAPI()
    _install_fake_auth(monkeypatch)
    fake_db = AsyncMock()
    fake_db.get_thread.return_value = {
        "id": "t1",
        "user_id": "u1",
        "agent_id": "agent-xyz",
    }
    fake_db.get_agent.return_value = {
        "id": "agent-xyz",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
        "status": "ready",  # heartbeat-reported, but pod still mid-attach
        "hostname": "srw-agent-x",
        "pod_uid": "uid",
    }
    from orchestrator.routers import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "_get_db", lambda: fake_db, raising=False)

    async def _probe_not_ready(pod_ip, pod_port):
        return False

    monkeypatch.setattr(sessions_mod, "probe_ready", _probe_not_ready, raising=True)

    app.include_router(sessions_router)
    client = TestClient(app)
    resp = client.get("/api/sessions/t1/connection")
    assert resp.status_code == 425
