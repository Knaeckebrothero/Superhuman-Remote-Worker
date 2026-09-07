"""Slice-3A callable boundary for Dynamic Canvas live applications."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from orchestrator.routers import canvases
from orchestrator.services.canvas import (
    CanvasMutation,
    CanvasRecord,
    WorkspaceAppSource,
    canonical_source_fingerprint,
)
from orchestrator.services.canvas_apps import CanvasAppError, ValidatedCanvasApp
from orchestrator.services.canvas_ssh import RemoteWorkspaceTarget

THREAD_ID = "a3333333-3333-3333-8333-333333333333"
USER_ID = "b4444444-4444-4444-8444-444444444444"
GENERATION = UUID("11111111-aaaa-4aaa-8aaa-111111111111")
NEXT_GENERATION = UUID("22222222-bbbb-4bbb-8bbb-222222222222")
NOW = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)


def _thread(generation: UUID = GENERATION) -> dict:
    return {
        "id": THREAD_ID,
        "user_id": USER_ID,
        "metadata": {
            "_workspace_binding": {
                "generation": str(generation),
                "kind": "remote",
                "backing_id": "workspace-1",
                "ssh_host_key_fingerprint": "SHA256:test",
            },
            "workspace_container": {
                "status": "ready",
                "host": "workspace.test",
                "port": 30022,
                "_canvas_workspace_generation": str(generation),
            },
        },
    }


class _RouteDB:
    def __init__(self) -> None:
        self.thread = _thread()
        self.owner_checks = 0

    async def get_thread(self, thread_id: str):
        assert thread_id == THREAD_ID
        return self.thread


class _RouteService:
    def __init__(self) -> None:
        self.record: CanvasRecord | None = None
        self.presentations = []

    async def get(self, thread_id: str):
        assert thread_id == THREAD_ID
        return self.record

    async def set(self, thread_id: str, presentation):
        assert thread_id == THREAD_ID
        self.presentations.append(presentation)
        source = presentation.source
        self.record = CanvasRecord(
            thread_id=THREAD_ID,
            canvas_id="main",
            source=source,
            title=presentation.title,
            renderer=presentation.renderer,
            editable=presentation.editable,
            alt_text=presentation.alt_text,
            presentation_revision=1,
            source_fingerprint=canonical_source_fingerprint(source),
            source_version=presentation.source_version,
            origin_generation=UUID("33333333-cccc-4ccc-8ccc-333333333333"),
            created_at=NOW,
            updated_at=NOW,
        )
        return CanvasMutation(changed=True, record=self.record)


class _RouteAppGateway:
    def __init__(self, db: _RouteDB) -> None:
        self.db = db
        self.validation_calls: list[tuple[dict, int, str]] = []
        self.revalidation_calls: list[tuple[dict, ValidatedCanvasApp]] = []
        self.status_calls: list[tuple[dict, CanvasRecord]] = []
        self.validation_error: CanvasAppError | None = None
        self.validation_status = "starting"
        self.current_status = "ready"
        self.rotate_generation_after_validate = False
        self.change_endpoint_after_validate = False

    async def validate_for_presentation(self, thread, port, *, entry_path="/"):
        self.validation_calls.append((thread, port, entry_path))
        if self.validation_error is not None:
            raise self.validation_error
        result = ValidatedCanvasApp(
            source=WorkspaceAppSource(
                entry_port=port,
                entry_path=entry_path,
                workspace_generation=GENERATION,
            ),
            status=self.validation_status,
            _target=RemoteWorkspaceTarget(
                thread_id=THREAD_ID,
                generation=GENERATION,
                host="workspace.test",
                port=30022,
                fingerprint="SHA256:test",
            ),
        )
        if self.rotate_generation_after_validate:
            self.db.thread = _thread(NEXT_GENERATION)
        elif self.change_endpoint_after_validate:
            self.db.thread = _thread()
            self.db.thread["metadata"]["workspace_container"]["host"] = "other.test"
        return result

    def revalidate_for_commit(self, thread, validated):
        self.revalidation_calls.append((thread, validated))
        metadata = thread["metadata"]
        workspace = metadata["workspace_container"]
        binding = metadata["_workspace_binding"]
        current_target = RemoteWorkspaceTarget(
            thread_id=thread["id"],
            generation=UUID(binding["generation"]),
            host=workspace["host"],
            port=workspace["port"],
            fingerprint=binding["ssh_host_key_fingerprint"],
        )
        if (
            workspace["_canvas_workspace_generation"]
            != str(validated.source.workspace_generation)
            or current_target != validated._target
        ):
            raise CanvasAppError(
                409,
                "workspace_generation_changed",
                "The workspace changed while the Canvas application was validated",
            )

    async def status_for_record(self, thread, record):
        self.status_calls.append((thread, record))
        return self.current_status


def _route_client(monkeypatch, *, enabled: bool = True):
    db = _RouteDB()
    service = _RouteService()
    gateway = _RouteAppGateway(db)
    gate = SimpleNamespace(enabled=enabled)

    async def owner(request, received_db, thread_id):
        assert received_db is db and thread_id == THREAD_ID
        db.owner_checks += 1
        return {"id": USER_ID}, db.thread

    async def internal(request):
        if request.headers.get("X-Internal-Key") != "test-key":
            raise HTTPException(status_code=401, detail="internal auth required")

    monkeypatch.setattr(canvases, "_get_db", lambda: db)
    monkeypatch.setattr(canvases, "_get_canvas_service", lambda received: service)
    monkeypatch.setattr(canvases, "_get_app_gateway", lambda received=None: gateway)
    monkeypatch.setattr(canvases, "require_thread_owner", owner)
    monkeypatch.setattr(canvases, "require_internal", internal)
    monkeypatch.setattr(canvases, "canvas_live_preview_enabled", lambda: gate.enabled)
    app = FastAPI()
    app.include_router(canvases.router)
    app.include_router(canvases.internal_router)
    return TestClient(app), service, gateway, db, gate


def _internal_headers() -> dict[str, str]:
    return {"X-Internal-Key": "test-key", "X-MCP-User-Id": USER_ID}


def _set_url() -> str:
    return f"/api/internal/persistent/threads/{THREAD_ID}/canvases/main/set"


def _state_url() -> str:
    return f"/api/persistent/threads/{THREAD_ID}/canvases/main"


def test_agent_attach_live_capability_requires_gate_attestation_and_non_vm(
    monkeypatch,
) -> None:
    import orchestrator.main

    metadata = _thread()["metadata"]
    workspace = metadata["workspace_container"]
    monkeypatch.setattr(
        "orchestrator.services.ssh_helpers.orchestrator_can_reach", lambda host: True
    )
    monkeypatch.delenv("CANVAS_LIVE_PREVIEW_ENABLED", raising=False)
    monkeypatch.delenv("CANVAS_SHARED_BROWSER_ENABLED", raising=False)
    assert orchestrator.main._agent_canvas_workspace_capabilities(
        metadata, workspace, {}
    ) == (
        True,
        False,
        False,
    )

    monkeypatch.setenv("CANVAS_LIVE_PREVIEW_ENABLED", "true")
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
    assert orchestrator.main._agent_canvas_workspace_capabilities(
        metadata, workspace, {}
    ) == (
        True,
        True,
        True,
    )

    monkeypatch.setattr(
        "orchestrator.services.ssh_helpers.orchestrator_can_reach", lambda host: False
    )
    assert orchestrator.main._agent_canvas_workspace_capabilities(
        metadata, workspace, {}
    ) == (
        True,
        True,
        False,
    )

    unattested = _thread()["metadata"]
    unattested["_workspace_binding"].pop("ssh_host_key_fingerprint")
    assert orchestrator.main._agent_canvas_workspace_capabilities(
        unattested, unattested["workspace_container"], {}
    ) == (False, False, False)

    vm = {"status": "ready", "ssh_host": "vm.test"}
    assert orchestrator.main._agent_canvas_workspace_capabilities(
        metadata, workspace, vm
    ) == (
        False,
        False,
        False,
    )


def test_workspace_port_set_normalizes_and_returns_status_without_viewer_data(
    monkeypatch,
) -> None:
    client, service, gateway, db, _ = _route_client(monkeypatch)

    response = client.post(
        _set_url(),
        headers=_internal_headers(),
        json={
            "source_type": "workspace_port",
            "port": 8501,
            "entry_path": "/demo",
            "title": "Prototype",
            "new_app": True,
        },
    )

    assert response.status_code == 200
    state = response.json()
    assert state["status"] == "starting"
    assert state["source"]["type"] == "workspace_app"
    assert state["source"]["entry_path"] == "/demo"
    assert "entry_port" not in state["source"]
    assert "workspace_generation" not in state["source"]
    assert state["content_url"] is None
    assert state["capabilities"] == {
        "can_edit": False,
        "can_pop_out": False,
        "can_take_control": False,
        "can_create_viewer_session": False,
        "can_stream_browser": False,
        "can_view_office": False,
    }
    assert gateway.validation_calls == [(db.thread, 8501, "/demo")]
    assert len(gateway.revalidation_calls) == 1
    assert db.owner_checks == 2
    assert len(service.presentations) == 1
    presentation = service.presentations[0]
    assert presentation.source.entry_port == 8501
    assert presentation.source.entry_path == "/demo"
    assert presentation.title == "Prototype"
    assert presentation.renderer == "auto"
    assert presentation.editable is False
    assert presentation.new_app is True

    # The ordinary owner-facing state endpoint derives status but still cannot
    # mint a viewer URL or claim capabilities before the isolated proxy exists.
    visible = client.get(_state_url())
    assert visible.status_code == 200
    assert visible.json()["status"] == "ready"
    assert visible.json()["content_url"] is None
    assert visible.json()["capabilities"] == state["capabilities"]
    assert len(gateway.status_calls) == 1


def test_workspace_port_is_server_gated_and_existing_app_becomes_unavailable(
    monkeypatch,
) -> None:
    client, service, gateway, _, gate = _route_client(monkeypatch)
    created = client.post(
        _set_url(),
        headers=_internal_headers(),
        json={"source_type": "workspace_port", "port": 8501},
    )
    assert created.status_code == 200

    gate.enabled = False
    visible = client.get(_state_url())
    assert visible.status_code == 200
    assert visible.json()["status"] == "unavailable"
    assert gateway.status_calls == []

    blocked = client.post(
        _set_url(),
        headers=_internal_headers(),
        json={"source_type": "workspace_port", "port": 8502},
    )
    assert blocked.status_code == 503
    assert blocked.json()["detail"]["code"] == "canvas_live_preview_disabled"
    assert len(service.presentations) == 1
    assert len(gateway.validation_calls) == 1


def test_workspace_port_typed_gateway_error_is_preserved(monkeypatch) -> None:
    client, service, gateway, _, _ = _route_client(monkeypatch)
    gateway.validation_error = CanvasAppError(
        422, "canvas_port_reserved", "Canvas application port is reserved"
    )

    response = client.post(
        _set_url(),
        headers=_internal_headers(),
        json={"source_type": "workspace_port", "port": 30022},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "canvas_port_reserved"
    assert service.presentations == []


@pytest.mark.parametrize(
    "payload",
    [
        {"source_type": "workspace_port"},
        {"source_type": "workspace_port", "port": 8501, "path": "index.html"},
        {"source_type": "workspace_port", "port": 8501, "renderer": "html"},
        {"source_type": "workspace_port", "port": 8501, "editable": True},
        {"source_type": "workspace_port", "port": 8501, "alt_text": "app"},
        {
            "source_type": "workspace_file",
            "path": "index.html",
            "port": 8501,
        },
        {
            "source_type": "workspace_file",
            "path": "index.html",
            "new_app": True,
        },
    ],
)
def test_internal_set_rejects_missing_and_cross_kind_fields(
    monkeypatch, payload
) -> None:
    client, service, gateway, _, _ = _route_client(monkeypatch)

    response = client.post(_set_url(), headers=_internal_headers(), json=payload)

    assert response.status_code == 422
    assert service.presentations == []
    assert gateway.validation_calls == []


def test_workspace_generation_rotation_after_health_check_prevents_commit(
    monkeypatch,
) -> None:
    client, service, gateway, db, _ = _route_client(monkeypatch)
    gateway.rotate_generation_after_validate = True

    response = client.post(
        _set_url(),
        headers=_internal_headers(),
        json={"source_type": "workspace_port", "port": 8501},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "workspace_generation_changed"
    assert db.owner_checks == 2
    assert service.presentations == []


def test_same_generation_endpoint_change_after_health_check_prevents_commit(
    monkeypatch,
) -> None:
    client, service, gateway, db, _ = _route_client(monkeypatch)
    gateway.change_endpoint_after_validate = True

    response = client.post(
        _set_url(),
        headers=_internal_headers(),
        json={"source_type": "workspace_port", "port": 8501},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "workspace_generation_changed"
    assert db.thread["metadata"]["_workspace_binding"]["generation"] == str(GENERATION)
    assert db.owner_checks == 2
    assert len(gateway.revalidation_calls) == 1
    assert service.presentations == []
