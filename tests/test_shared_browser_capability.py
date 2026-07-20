"""Browser canvas records expose a fail-closed stream capability."""

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from routers import canvases as canvas_routes
from services.canvas import BrowserSource, CanvasCapabilities, CanvasRecord

_GENERATION = UUID("5f0a9f5e-0000-4000-8000-000000000001")
_NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)
_READY_THREAD = {
    "metadata": {
        "workspace_container": {
            "status": "ready",
            "ssh_host": "h",
            "ssh_port": 22,
        }
    }
}


def _record() -> CanvasRecord:
    return CanvasRecord(
        thread_id="t1",
        canvas_id="main",
        source=BrowserSource(browser_generation=_GENERATION),
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


def test_capability_on_when_enabled_and_ready(monkeypatch):
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
    representation = asyncio.run(
        canvas_routes._represent(
            "t1",
            _READY_THREAD,
            _record(),
            browser_content_url=False,
        )
    )
    assert representation.state.status == "ready"
    assert representation.state.capabilities.can_stream_browser is True
    assert representation.state.source.type == "browser"


def test_capability_off_when_disabled(monkeypatch):
    monkeypatch.delenv("CANVAS_SHARED_BROWSER_ENABLED", raising=False)
    representation = asyncio.run(
        canvas_routes._represent(
            "t1",
            _READY_THREAD,
            _record(),
            browser_content_url=False,
        )
    )
    assert representation.state.status == "unavailable"
    assert representation.state.capabilities.can_stream_browser is False


def test_capability_off_when_workspace_down(monkeypatch):
    monkeypatch.setenv("CANVAS_SHARED_BROWSER_ENABLED", "true")
    representation = asyncio.run(
        canvas_routes._represent(
            "t1",
            {"metadata": {}},
            _record(),
            browser_content_url=False,
        )
    )
    assert representation.state.status == "unavailable"
    assert representation.state.capabilities.can_stream_browser is False


def test_default_field_value():
    assert CanvasCapabilities().can_stream_browser is False
