"""Closed pre-source and staged shared-browser capability contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from orchestrator.routers import canvases as canvas_routes
from orchestrator.routers import shared_browser as browser_routes
from orchestrator.services import shared_browser_canvas as browser_canvas
from orchestrator.services.canvas import BrowserSource, CanvasCapabilities, CanvasRecord

_THREAD_ID = "a3333333-3333-3333-3333-333333333333"
_WORKSPACE_GENERATION = UUID("11111111-aaaa-4aaa-8aaa-111111111111")
_BROWSER_GENERATION = UUID("5f0a9f5e-0000-4000-8000-000000000001")
_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def _thread(
    *,
    backend: str = "sandbox",
    ready: bool = True,
    attested: bool = True,
    host: str = "workspace.test",
    fingerprint: str = "SHA256:test",
    endpoint_generation: UUID = _WORKSPACE_GENERATION,
) -> dict:
    metadata: dict = {
        "config_override": {"workspace": {"backend": backend}},
    }
    if attested:
        metadata["_workspace_binding"] = {
            "generation": str(_WORKSPACE_GENERATION),
            "kind": "remote",
            "backing_id": "workspace-a",
            "ssh_host_key_fingerprint": fingerprint,
        }
    if ready:
        context = {
            "status": "ready",
            "ssh_host": host,
            "ssh_port": 30022,
            "_canvas_workspace_generation": str(endpoint_generation),
        }
        metadata["vm" if backend in {"vm", "remote"} else "workspace_container"] = (
            context
        )
    return {"id": _THREAD_ID, "user_id": "user-1", "metadata": metadata}


def _record() -> CanvasRecord:
    return CanvasRecord(
        thread_id=_THREAD_ID,
        canvas_id="main",
        source=BrowserSource(browser_generation=_BROWSER_GENERATION),
        title="Shared browser",
        renderer="auto",
        editable=False,
        alt_text=None,
        presentation_revision=1,
        source_fingerprint=None,
        source_version=None,
        origin_generation=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.fixture(autouse=True)
def _enabled_transport(monkeypatch):
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
    monkeypatch.setattr(browser_canvas, "resolve_ssh_key_path", lambda: "/tmp/key")
    monkeypatch.setattr(browser_canvas, "orchestrator_can_reach", lambda host: True)
    monkeypatch.setattr(browser_canvas, "asyncssh", object())


@pytest.mark.parametrize("backend", ["sandbox", "container"])
def test_cold_container_is_discoverable_and_openable(backend):
    capability = browser_canvas.browser_capability(
        _thread(backend=backend, ready=False, attested=False)
    )

    assert capability.model_dump(mode="json") == {
        "feature_enabled": True,
        "can_open_browser": True,
        "workspace_ready": False,
        "reason": None,
    }


@pytest.mark.parametrize("backend", ["virtual", "none"])
def test_lite_backend_requires_a_workspace(backend):
    capability = browser_canvas.browser_capability(
        _thread(backend=backend, ready=False, attested=False)
    )

    assert capability.can_open_browser is False
    assert capability.workspace_ready is False
    assert capability.reason == "workspace_required"


def test_cold_vm_is_not_treated_as_attested():
    capability = browser_canvas.browser_capability(
        _thread(backend="vm", ready=False, attested=False)
    )

    assert capability.can_open_browser is False
    assert capability.reason == "workspace_unattested"


@pytest.mark.parametrize(
    ("thread", "reason"),
    [
        (_thread(attested=False), "workspace_unattested"),
        (_thread(fingerprint="not-a-pin"), "workspace_unattested"),
        (
            _thread(endpoint_generation=UUID("22222222-bbbb-4bbb-8bbb-222222222222")),
            "workspace_unattested",
        ),
        (_thread(backend="custom"), "workspace_unattested"),
    ],
)
def test_ready_but_unattested_targets_fail_closed(thread, reason):
    capability = browser_canvas.browser_capability(thread)

    assert capability.feature_enabled is True
    assert capability.can_open_browser is False
    assert capability.workspace_ready is True
    assert capability.reason == reason


def test_missing_key_is_transport_unavailable(monkeypatch):
    monkeypatch.setattr(browser_canvas, "resolve_ssh_key_path", lambda: "")

    capability = browser_canvas.browser_capability(_thread())

    assert capability.can_open_browser is False
    assert capability.workspace_ready is True
    assert capability.reason == "transport_unavailable"


def test_enabled_but_invalid_transport_config_is_not_reported_as_disabled(
    monkeypatch,
):
    monkeypatch.setenv("CANVAS_BROWSER_STREAM_PORT", "not-a-port")

    capability = browser_canvas.browser_capability(_thread())

    assert capability.feature_enabled is True
    assert capability.can_open_browser is False
    assert capability.workspace_ready is True
    assert capability.reason == "transport_unavailable"


def test_unroutable_tailnet_is_distinct(monkeypatch):
    monkeypatch.setattr(browser_canvas, "orchestrator_can_reach", lambda host: False)

    capability = browser_canvas.browser_capability(
        _thread(backend="vm", host="100.64.23.180")
    )

    assert capability.can_open_browser is False
    assert capability.workspace_ready is True
    assert capability.reason == "workspace_unroutable"


def test_fully_attested_reachable_target_has_exact_public_shape():
    capability = browser_canvas.browser_capability(_thread())

    assert capability.model_dump(mode="json") == {
        "feature_enabled": True,
        "can_open_browser": True,
        "workspace_ready": True,
        "reason": None,
    }
    public = capability.model_dump_json()
    for private in (
        "workspace.test",
        "30022",
        "workspace-a",
        str(_WORKSPACE_GENERATION),
        "SHA256:test",
        "/tmp/key",
    ):
        assert private not in public


def test_disabled_flag_is_closed(monkeypatch):
    monkeypatch.delenv("CANVAS_SHARED_BROWSER_ENABLED", raising=False)

    capability = browser_canvas.browser_capability(_thread())

    assert capability.model_dump(mode="json") == {
        "feature_enabled": False,
        "can_open_browser": False,
        "workspace_ready": False,
        "reason": "feature_disabled",
    }


def test_capability_route_authorizes_before_disclosing_disabled_flag(monkeypatch):
    monkeypatch.delenv("CANVAS_SHARED_BROWSER_ENABLED", raising=False)
    calls = []

    async def owner(request, db, thread_id):
        calls.append((db, thread_id))
        return {"id": "user-1"}, _thread()

    db = object()
    monkeypatch.setattr(browser_routes, "require_thread_owner", owner)
    monkeypatch.setattr(browser_routes, "_get_db", lambda: db)
    app = FastAPI()
    app.include_router(browser_routes.router)

    response = TestClient(app).get(
        f"/api/persistent/threads/{_THREAD_ID}/browser/capability"
    )

    assert response.status_code == 200
    assert response.json() == {
        "feature_enabled": False,
        "can_open_browser": False,
        "workspace_ready": False,
        "reason": "feature_disabled",
    }
    assert calls == [(db, _THREAD_ID)]


@pytest.mark.parametrize("thread_exists", [False, True])
def test_capability_route_does_not_distinguish_absent_from_unauthorized(
    monkeypatch, thread_exists
):
    assert isinstance(thread_exists, bool)

    async def hidden(request, db, thread_id):
        del request, db, thread_id
        raise HTTPException(status_code=404, detail="Thread not found")

    monkeypatch.setattr(browser_routes, "require_thread_owner", hidden)
    monkeypatch.setattr(browser_routes, "_get_db", lambda: object())
    app = FastAPI()
    app.include_router(browser_routes.router)

    response = TestClient(app).get(
        f"/api/persistent/threads/{_THREAD_ID}/browser/capability"
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Thread not found"}


def test_staged_browser_capabilities_use_the_same_live_gate(monkeypatch):
    positive = asyncio.run(
        canvas_routes._represent(
            _THREAD_ID,
            _thread(),
            _record(),
            browser_content_url=False,
        )
    )
    assert positive.state.status == "ready"
    assert positive.state.capabilities == CanvasCapabilities(
        can_pop_out=True,
        can_take_control=True,
        can_stream_browser=True,
    )

    monkeypatch.setattr(browser_canvas, "resolve_ssh_key_path", lambda: "")
    negative = asyncio.run(
        canvas_routes._represent(
            _THREAD_ID,
            _thread(),
            _record(),
            browser_content_url=False,
        )
    )
    assert negative.state.status == "unavailable"
    assert negative.state.capabilities == CanvasCapabilities()


def test_default_stream_capability_is_false():
    assert CanvasCapabilities().can_stream_browser is False
