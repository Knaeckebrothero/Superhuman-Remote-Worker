from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI, HTTPException
import httpx
import pytest
import pytest_asyncio

from orchestrator.security.vm_guest import VmGuestIdentity
from orchestrator.services.vm_guest_events import record_register
from routers import vm_guest
from services.sudo_gate import SudoOpenResult, SudoRequestConflict

ENTITY_ID = "11111111-1111-4111-8111-111111111111"
REQUEST_ID = "22222222-2222-4222-8222-222222222222"
GENERATION = "33333333-3333-4333-8333-333333333333"
pytestmark = pytest.mark.asyncio


def sudo_body(**overrides):
    result = {
        "request_id": REQUEST_ID,
        "command": "apt-get",
        "argv": ["install", "-y", "curl"],
        "runas_user": "root",
        "user": "agent-host",
        "host": "vm-one",
        "tty": "pts/1",
        "cwd": "/workspace",
        "pid": 42,
    }
    result.update(overrides)
    return result


@pytest_asyncio.fixture
async def route_env(monkeypatch):
    app = FastAPI()
    app.include_router(vm_guest.router)
    identity = VmGuestIdentity("job", ENTITY_ID, GENERATION)
    auth = AsyncMock(return_value=identity)
    db = AsyncMock()
    gate = MagicMock()
    gate.http_request_exists = AsyncMock(return_value=False)
    gate.count_pending_for_entity = AsyncMock(return_value=0)
    gate.open_request = AsyncMock(
        return_value=SudoOpenResult(
            REQUEST_ID,
            "pending",
            None,
            datetime.now(timezone.utc),
            True,
        )
    )
    gate.wait_for_decision = AsyncMock(
        return_value={
            "request_id": REQUEST_ID,
            "status": "approved",
            "reason": "ok",
        }
    )
    register = AsyncMock()
    heartbeat = AsyncMock()
    monkeypatch.setattr(vm_guest, "require_vm_guest", auth)
    monkeypatch.setattr(vm_guest, "_get_db", lambda: db)
    monkeypatch.setattr(vm_guest, "_get_sudo_gate", lambda: gate)
    monkeypatch.setattr(vm_guest, "record_register", register)
    monkeypatch.setattr(vm_guest, "record_heartbeat", heartbeat)
    vm_guest._rate_limits.reset()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield SimpleNamespace(
            client=client,
            identity=identity,
            auth=auth,
            db=db,
            gate=gate,
            register=register,
            heartbeat=heartbeat,
        )
    vm_guest._rate_limits.reset()


async def test_register_route_records_non_authoritative_guest_data(route_env):
    response = await route_env.client.post(
        f"/api/internal/vm/{ENTITY_ID}/register",
        json={"hostname": "vm-one", "ip": "10.42.0.9", "pid": 77},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert route_env.register.await_args.kwargs["authoritative"] is False


async def test_heartbeat_route(route_env):
    response = await route_env.client.post(
        f"/api/internal/vm/{ENTITY_ID}/heartbeat",
        json={
            "cpu_percent": 1.5,
            "memory_percent": 2.5,
            "disk_percent": 3.5,
            "code_server_connections": 1,
        },
    )
    assert response.status_code == 200
    route_env.heartbeat.assert_awaited_once()


async def test_sudo_create_and_idempotent_repost_status_codes(route_env):
    created = await route_env.client.post(
        f"/api/internal/vm/{ENTITY_ID}/sudo", json=sudo_body()
    )
    assert created.status_code == 201
    assert created.json()["status"] == "pending"

    route_env.gate.http_request_exists.return_value = True
    route_env.gate.open_request.return_value = SudoOpenResult(
        REQUEST_ID,
        "approved",
        "rule",
        datetime.now(timezone.utc),
        False,
    )
    replay = await route_env.client.post(
        f"/api/internal/vm/{ENTITY_ID}/sudo", json=sudo_body()
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "approved"


async def test_sudo_wait_route_caps_wait_at_30(route_env):
    response = await route_env.client.get(
        f"/api/internal/vm/{ENTITY_ID}/sudo/{REQUEST_ID}?wait=300"
    )
    assert response.status_code == 200
    assert route_env.gate.wait_for_decision.await_args.args[1] == 30


async def test_auth_failure_is_401(route_env):
    route_env.auth.side_effect = HTTPException(status_code=401, detail="Unauthorized")
    response = await route_env.client.post(
        f"/api/internal/vm/{ENTITY_ID}/register",
        json={"hostname": "vm-one", "ip": "10.42.0.9", "pid": 77},
    )
    assert response.status_code == 401


async def test_unknown_scoped_sudo_request_is_404(route_env):
    route_env.gate.wait_for_decision.return_value = None
    response = await route_env.client.get(
        f"/api/internal/vm/{ENTITY_ID}/sudo/{REQUEST_ID}?wait=0"
    )
    assert response.status_code == 404


async def test_idempotency_payload_conflict_is_409(route_env):
    route_env.gate.open_request.side_effect = SudoRequestConflict("different payload")
    response = await route_env.client.post(
        f"/api/internal/vm/{ENTITY_ID}/sudo", json=sudo_body()
    )
    assert response.status_code == 409


async def test_malformed_body_is_422(route_env):
    response = await route_env.client.post(
        f"/api/internal/vm/{ENTITY_ID}/sudo", json={"request_id": "not-a-uuid"}
    )
    assert response.status_code == 422


async def test_pending_limit_is_429_with_retry_after(route_env):
    route_env.gate.count_pending_for_entity.return_value = 2
    response = await route_env.client.post(
        f"/api/internal/vm/{ENTITY_ID}/sudo", json=sudo_body()
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"


async def test_sudo_create_token_bucket_is_429(route_env):
    for index in range(6):
        response = await route_env.client.post(
            f"/api/internal/vm/{ENTITY_ID}/sudo",
            json=sudo_body(request_id=f"22222222-2222-4222-8222-{index:012d}"),
        )
        assert response.status_code == 201
    response = await route_env.client.post(
        f"/api/internal/vm/{ENTITY_ID}/sudo",
        json=sudo_body(request_id="22222222-2222-4222-8222-999999999999"),
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "10"


async def test_sudo_wait_token_bucket_is_429(route_env):
    for _ in range(30):
        response = await route_env.client.get(
            f"/api/internal/vm/{ENTITY_ID}/sudo/{REQUEST_ID}?wait=0"
        )
        assert response.status_code == 200
    response = await route_env.client.get(
        f"/api/internal/vm/{ENTITY_ID}/sudo/{REQUEST_ID}?wait=0"
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "2"


async def test_heartbeat_token_bucket_is_429(route_env):
    body = {
        "cpu_percent": 1,
        "memory_percent": 2,
        "disk_percent": 3,
        "code_server_connections": 0,
    }
    for _ in range(12):
        response = await route_env.client.post(
            f"/api/internal/vm/{ENTITY_ID}/heartbeat", json=body
        )
        assert response.status_code == 200
    response = await route_env.client.post(
        f"/api/internal/vm/{ENTITY_ID}/heartbeat", json=body
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"


async def test_global_waiter_cap_is_503(route_env):
    vm_guest._rate_limits.waiters = 200
    response = await route_env.client.get(
        f"/api/internal/vm/{ENTITY_ID}/sudo/{REQUEST_ID}?wait=1"
    )
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


@pytest.mark.asyncio
async def test_non_authoritative_register_never_writes_readiness():
    db = AsyncMock()
    db.merge_vm_context_if_provision_generation.return_value = True
    identity = VmGuestIdentity("job", ENTITY_ID, GENERATION)

    await record_register(
        db,
        identity,
        {"hostname": "vm-one", "ip": "10.42.0.9", "pid": 77},
        authoritative=False,
    )

    updates = db.merge_vm_context_if_provision_generation.await_args.args[2]
    assert updates == {
        "hostname": "vm-one",
        "daemon_pid": 77,
        "reported_ip": "10.42.0.9",
        "registered_at": updates["registered_at"],
    }
    assert "status" not in updates
    assert "ssh_host" not in updates
