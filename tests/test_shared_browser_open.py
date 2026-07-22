"""Open-endpoint flow tests with faked db/SSH/provisioning."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import shared_browser as sb
from services.browser_stream_broker import BrowserStreamUnavailable


_READY_THREAD = {
    "id": "t1",
    "metadata": {
        "workspace_backend": "container",
        "workspace_container": {
            "status": "ready",
            "ssh_host": "h",
            "ssh_port": 22,
        },
    },
}


@pytest.fixture()
def client(monkeypatch):
    app = FastAPI()
    app.include_router(sb.router)

    async def fake_owner(request, db, thread_id):
        return {"id": "u1"}, dict(_READY_THREAD)

    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
    monkeypatch.setattr(sb, "require_thread_owner", fake_owner)
    db = SimpleNamespace()

    async def get_thread(thread_id):
        del thread_id
        return dict(_READY_THREAD)

    db.get_thread = get_thread
    monkeypatch.setattr(sb, "_get_db", lambda: db)
    return app, TestClient(app)


def test_flag_off_is_404(client, monkeypatch):
    _, test_client = client
    monkeypatch.delenv("CANVAS_SHARED_BROWSER_ENABLED", raising=False)

    response = test_client.post(
        "/api/persistent/threads/t1/browser/open",
        json={},
    )

    assert response.status_code == 404


def test_lite_backend_is_409(client, monkeypatch):
    _, test_client = client

    async def lite_owner(request, db, thread_id):
        return {"id": "u1"}, {
            "id": "t1",
            "metadata": {"config_override": {"workspace": {"backend": "virtual"}}},
        }

    monkeypatch.setattr(sb, "require_thread_owner", lite_owner)

    response = test_client.post(
        "/api/persistent/threads/t1/browser/open",
        json={},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "workspace_required"


def test_cold_workspace_is_202_and_kicks_provisioning(client, monkeypatch):
    _, test_client = client
    kicked = {}

    async def cold_owner(request, db, thread_id):
        return {"id": "u1"}, {
            "id": "t1",
            "metadata": {"workspace_backend": "container"},
        }

    def fake_kick(thread_id, db):
        kicked["thread_id"] = thread_id

    monkeypatch.setattr(sb, "require_thread_owner", cold_owner)
    monkeypatch.setattr(sb, "_kick_workspace_provisioning", fake_kick)

    response = test_client.post(
        "/api/persistent/threads/t1/browser/open",
        json={},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "provisioning"
    assert kicked["thread_id"] == "t1"


def test_ready_workspace_sets_canvas_and_returns_generation(client, monkeypatch):
    _, test_client = client
    calls = {}

    async def fake_info(thread, *, initial_baton=None, generation_resolver):
        assert await generation_resolver() == _READY_THREAD
        calls["baton"] = initial_baton
        return {
            "generation": "5f0a9f5e-0000-4000-8000-000000000001",
            "token": "t" * 64,
            "port": 38801,
            "baton": initial_baton,
        }

    class FakeCanvasService:
        async def set(self, thread_id, presentation):
            calls["source_type"] = presentation.source.type
            calls["generation"] = str(presentation.source.browser_generation)
            return SimpleNamespace(changed=True)

    monkeypatch.setattr(sb, "exec_stream_info", fake_info)
    monkeypatch.setattr(sb, "_get_canvas_service", lambda db: FakeCanvasService())

    response = test_client.post(
        "/api/persistent/threads/t1/browser/open",
        json={"opened_by": "user"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["generation"] == "5f0a9f5e-0000-4000-8000-000000000001"
    assert body["stream_port"] == 38801
    assert calls == {
        "baton": "user",
        "source_type": "browser",
        "generation": "5f0a9f5e-0000-4000-8000-000000000001",
    }


def test_browser_unreachable_maps_status(client, monkeypatch):
    _, test_client = client

    async def fake_info(thread, *, initial_baton=None, generation_resolver):
        del generation_resolver
        raise BrowserStreamUnavailable(502, "browser-exec unreachable")

    monkeypatch.setattr(sb, "exec_stream_info", fake_info)

    response = test_client.post(
        "/api/persistent/threads/t1/browser/open",
        json={},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "browser-exec unreachable"
