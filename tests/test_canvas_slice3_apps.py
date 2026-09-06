"""Dynamic Canvas Slice 3A live-app validation and health contracts."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest

from orchestrator.services.canvas import (
    CanvasRecord,
    WorkspaceAppSource,
    canonical_source_fingerprint,
)
from orchestrator.services.canvas_apps import (
    CANVAS_APP_DENYLIST_ENV,
    CanvasAppError,
    ThreadWorkspaceAppGateway,
    canonical_canvas_app_path,
    canvas_live_preview_enabled,
    validate_workspace_port,
)
from orchestrator.services.canvas_ssh import (
    CanvasDirectChannelUnavailable,
    CanvasSSHError,
)

THREAD_ID = "a3333333-3333-3333-3333-333333333333"
GENERATION = UUID("11111111-aaaa-4aaa-8aaa-111111111111")
NOW = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)


def _thread(
    *,
    generation: UUID = GENERATION,
    host: str = "workspace.test",
    fingerprint: str = "SHA256:test",
) -> dict[str, Any]:
    return {
        "id": THREAD_ID,
        "metadata": {
            "_workspace_binding": {
                "generation": str(generation),
                "kind": "remote",
                "backing_id": "workspace-a",
                "ssh_host_key_fingerprint": fingerprint,
            },
            "workspace_container": {
                "status": "ready",
                "host": host,
                "port": 30022,
                "_canvas_workspace_generation": str(generation),
            },
        },
    }


def _record(source: WorkspaceAppSource) -> CanvasRecord:
    return CanvasRecord(
        thread_id=THREAD_ID,
        canvas_id="main",
        source=source,
        title="Preview",
        renderer="auto",
        editable=False,
        alt_text=None,
        presentation_revision=1,
        source_fingerprint=canonical_source_fingerprint(source),
        source_version=None,
        origin_generation=None,
        created_at=NOW,
        updated_at=NOW,
    )


class _AppPool:
    def __init__(self) -> None:
        self.mode = "ready"
        self.calls: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def open_loopback_connection(self, **kwargs: Any):
        self.calls.append(kwargs)
        self.entered.set()
        await kwargs["generation_resolver"]()
        if self.mode == "starting":
            raise CanvasDirectChannelUnavailable("closed")
        if self.mode == "ssh_error":
            raise CanvasSSHError(503, "workspace_unavailable", "SSH failed")
        if self.mode == "block":
            await self.release.wait()
        yield object(), object()
        await kwargs["generation_resolver"]()


def _clear_port_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        CANVAS_APP_DENYLIST_ENV,
        "CANVAS_LIVE_PREVIEW_DENY_PORTS",
        "CANVAS_APP_DENIED_PORTS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_live_preview_gate_is_explicit_and_default_off(monkeypatch) -> None:
    monkeypatch.delenv("CANVAS_LIVE_PREVIEW_ENABLED", raising=False)
    assert canvas_live_preview_enabled() is False
    for value in ("1", "TRUE", "yes", "on"):
        monkeypatch.setenv("CANVAS_LIVE_PREVIEW_ENABLED", value)
        assert canvas_live_preview_enabled() is True
    monkeypatch.setenv("CANVAS_LIVE_PREVIEW_ENABLED", "enabled")
    assert canvas_live_preview_enabled() is False


def test_workspace_port_bounds_fixed_and_deployment_denylist(monkeypatch) -> None:
    _clear_port_env(monkeypatch)
    assert validate_workspace_port(1024) == 1024
    assert validate_workspace_port(65535) == 65535
    for value in (True, 1023, 65536):
        with pytest.raises(CanvasAppError) as invalid:
            validate_workspace_port(value)
        assert invalid.value.code == "invalid_canvas_port"
    for value in (9222, 30022, 38080):
        with pytest.raises(CanvasAppError) as denied:
            validate_workspace_port(value)
        assert denied.value.code == "canvas_port_reserved"

    monkeypatch.setenv(CANVAS_APP_DENYLIST_ENV, " 8501, 9000 ")
    with pytest.raises(CanvasAppError) as configured:
        validate_workspace_port(8501)
    assert configured.value.code == "canvas_port_reserved"
    monkeypatch.setenv(CANVAS_APP_DENYLIST_ENV, "8501,broken")
    with pytest.raises(CanvasAppError) as bad_config:
        validate_workspace_port(8502)
    assert bad_config.value.status_code == 503
    assert bad_config.value.code == "canvas_configuration_invalid"


@pytest.mark.parametrize(
    ("path", "canonical"),
    [
        ("/", "/"),
        ("/app", "/app"),
        ("/app/", "/app/"),
        ("/%61pp/a%20b", "/app/a%20b"),
        ("/%e2%82%ac", "/%E2%82%AC"),
    ],
)
def test_entry_path_is_normatively_canonicalized(path: str, canonical: str) -> None:
    assert canonical_canvas_app_path(path) == canonical


@pytest.mark.parametrize(
    "path",
    [
        "",
        "relative",
        "//authority",
        "/a//b",
        "/a?query=1",
        "/a#fragment",
        "/a\\b",
        "/café",
        "/bad%",
        "/%2f",
        "/%5C",
        "/%00",
        "/.",
        "/..",
        "/%252e%252e",
        "/_canvas",
        "/app/_CANVAS/control",
        "/" + " " * 700,
    ],
)
def test_entry_path_rejects_ambiguous_unsafe_and_expanding_inputs(path: str) -> None:
    with pytest.raises(CanvasAppError) as error:
        canonical_canvas_app_path(path)
    assert error.value.code == "invalid_canvas_entry_path"


@pytest.mark.asyncio
async def test_gateway_normalizes_source_and_checks_only_bound_loopback_port() -> None:
    pool = _AppPool()
    thread = _thread()

    async def load(thread_id: str) -> dict[str, Any]:
        assert thread_id == THREAD_ID
        return thread

    gateway = ThreadWorkspaceAppGateway(
        thread_loader=load,
        transport_pool=pool,  # type: ignore[arg-type]
        key_path_resolver=lambda: "/tmp/key",
    )
    validated = await gateway.validate_for_presentation(
        thread, 8501, entry_path="/%61pp/"
    )

    assert validated.status == "ready"
    assert validated.source == WorkspaceAppSource(
        entry_port=8501,
        entry_path="/app/",
        routes=(),
        manifest_path=None,
        manifest_version=None,
        workspace_generation=GENERATION,
    )
    assert pool.calls[0]["destination_port"] == 8501
    assert pool.calls[0]["target"].host == "workspace.test"
    assert pool.calls[0]["target"].fingerprint == "SHA256:test"


@pytest.mark.asyncio
async def test_closed_app_is_starting_but_ssh_failure_is_typed_unavailable() -> None:
    pool = _AppPool()
    gateway = ThreadWorkspaceAppGateway(
        transport_pool=pool,  # type: ignore[arg-type]
        key_path_resolver=lambda: "/tmp/key",
    )
    pool.mode = "starting"
    validated = await gateway.validate_for_presentation(_thread(), 8501)
    assert validated.status == "starting"

    pool.mode = "ssh_error"
    with pytest.raises(CanvasAppError) as error:
        await gateway.validate_for_presentation(_thread(), 8501)
    assert error.value.status_code == 503
    assert error.value.code == "workspace_unavailable"


@pytest.mark.asyncio
async def test_full_binding_is_revalidated_immediately_before_commit() -> None:
    pool = _AppPool()
    gateway = ThreadWorkspaceAppGateway(
        transport_pool=pool,  # type: ignore[arg-type]
        key_path_resolver=lambda: "/tmp/key",
    )
    validated = await gateway.validate_for_presentation(_thread(), 8501)
    gateway.revalidate_for_commit(_thread(), validated)

    for replacement in (
        _thread(host="replacement.test"),
        _thread(fingerprint="SHA256:replacement"),
    ):
        with pytest.raises(CanvasAppError) as changed:
            gateway.revalidate_for_commit(replacement, validated)
        assert changed.value.code == "workspace_generation_changed"


@pytest.mark.asyncio
async def test_health_queue_is_bounded_and_releases_capacity() -> None:
    pool = _AppPool()
    pool.mode = "block"
    gateway = ThreadWorkspaceAppGateway(
        transport_pool=pool,  # type: ignore[arg-type]
        key_path_resolver=lambda: "/tmp/key",
        health_semaphore=asyncio.Semaphore(1),
        queue_timeout=0.01,
        connect_timeout=1,
    )
    first = asyncio.create_task(gateway.validate_for_presentation(_thread(), 8501))
    await pool.entered.wait()

    with pytest.raises(CanvasAppError) as exhausted:
        await gateway.validate_for_presentation(_thread(), 8502)
    assert exhausted.value.status_code == 503
    assert exhausted.value.code == "canvas_capacity_exhausted"

    pool.release.set()
    assert (await first).status == "ready"


@pytest.mark.asyncio
async def test_persisted_app_status_maps_expected_health_without_throwing() -> None:
    pool = _AppPool()
    gateway = ThreadWorkspaceAppGateway(
        transport_pool=pool,  # type: ignore[arg-type]
        key_path_resolver=lambda: "/tmp/key",
    )
    source = WorkspaceAppSource(
        entry_port=8501,
        entry_path="/",
        workspace_generation=GENERATION,
    )
    record = _record(source)
    assert await gateway.status_for_record(_thread(), record) == "ready"

    pool.mode = "starting"
    assert await gateway.status_for_record(_thread(), record) == "starting"
    pool.mode = "ssh_error"
    assert await gateway.status_for_record(_thread(), record) == "unavailable"
    assert (
        await gateway.status_for_record(
            _thread(generation=UUID("22222222-bbbb-4bbb-8bbb-222222222222")),
            record,
        )
        == "unavailable"
    )
