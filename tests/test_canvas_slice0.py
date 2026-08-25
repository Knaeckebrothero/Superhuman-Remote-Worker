"""Dynamic Canvas Slice 0: durable state actions and owner-facing routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests._route_inventory import mounted_routes
from services.canvas import (
    BrowserSource,
    CanvasCapabilities,
    CanvasEditError,
    CanvasPreconditionFailed,
    CanvasPreconditionRequired,
    CanvasService,
    CanvasSetInput,
    WorkspaceAppRoute,
    WorkspaceAppSource,
    WorkspaceFileSource,
    build_public_canvas_representation,
    canonical_source_fingerprint,
    canvas_presentation_lock_key,
)

_THREAD_ID = "a3333333-3333-3333-3333-333333333333"
_WORKSPACE_GENERATION = UUID("11111111-aaaa-4aaa-8aaa-111111111111")
_OTHER_WORKSPACE_GENERATION = UUID("22222222-bbbb-4bbb-8bbb-222222222222")
_SOURCE_VERSION = "sha256:" + "a" * 64


class _FakeCanvasDB:
    """Small asyncpg-shaped store that exercises the service SQL branches."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
        self.in_transaction = False
        self.acquire_count = 0
        self.advisory_calls = 0
        self._transaction_lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self):
        self.acquire_count += 1
        yield self

    @asynccontextmanager
    async def transaction(self):
        async with self._transaction_lock:
            assert not self.in_transaction
            self.in_transaction = True
            try:
                yield
            finally:
                self.in_transaction = False

    def _tick(self) -> datetime:
        self.now += timedelta(microseconds=1)
        return self.now

    async def fetchval(self, query: str, *args: Any) -> Any:
        sql = " ".join(query.split())
        if sql == "SELECT pg_advisory_xact_lock($1)":
            assert self.in_transaction
            assert isinstance(args[0], int)
            self.advisory_calls += 1
            return None
        # Durable presentation copies are a separate table this double does not
        # model; a thread here never has one. Matched explicitly so a genuinely
        # unexpected statement still fails loudly.
        if sql.startswith("DELETE FROM canvas_snapshots"):
            return None
        raise AssertionError(f"unexpected Canvas SQL: {sql}")

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        sql = " ".join(query.split())

        if sql.startswith("SELECT id, user_id, metadata FROM threads"):
            return {
                "id": str(args[0]),
                "user_id": "user-1",
                "metadata": {},
            }

        if "FROM canvas_snapshots" in sql:
            return None

        if sql.startswith("SELECT thread_id"):
            key = (str(args[0]), str(args[1]))
            row = self.rows.get(key)
            return dict(row) if row is not None else None

        if sql.startswith("INSERT INTO canvases"):
            if len(args) == 11:
                (
                    thread_id,
                    canvas_id,
                    source_json,
                    title,
                    renderer,
                    editable,
                    alt_text,
                    source_fingerprint,
                    source_version,
                    origin_candidate,
                    new_app,
                ) = args
            else:
                (
                    thread_id,
                    canvas_id,
                    source_json,
                    title,
                    renderer,
                    editable,
                    alt_text,
                    source_fingerprint,
                    source_version,
                    origin_candidate,
                ) = args
                new_app = False
            key = (str(thread_id), str(canvas_id))
            existing = self.rows.get(key)
            now = self._tick()
            origin_generation = origin_candidate
            if (
                existing is not None
                and origin_candidate is not None
                and existing["origin_generation"] is not None
                and existing["source_fingerprint"] == source_fingerprint
                and not new_app
            ):
                origin_generation = existing["origin_generation"]
            row = {
                "thread_id": str(thread_id),
                "canvas_id": str(canvas_id),
                "source": source_json,
                "title": title,
                "renderer": renderer,
                "editable": editable,
                "alt_text": alt_text,
                "presentation_revision": (
                    existing["presentation_revision"] + 1 if existing else 1
                ),
                "source_fingerprint": source_fingerprint,
                "source_version": source_version,
                "origin_generation": origin_generation,
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
            }
            self.rows[key] = row
            return dict(row)

        if sql.startswith("UPDATE canvases SET source = NULL"):
            key = (str(args[0]), str(args[1]))
            existing = self.rows[key]
            row = {
                **existing,
                "source": None,
                "title": None,
                "renderer": "auto",
                "editable": False,
                "alt_text": None,
                "presentation_revision": existing["presentation_revision"] + 1,
                "source_fingerprint": None,
                "source_version": None,
                "origin_generation": None,
                "updated_at": self._tick(),
            }
            self.rows[key] = row
            return dict(row)

        if sql.startswith("UPDATE canvases SET origin_generation"):
            key = (str(args[0]), str(args[1]))
            existing = self.rows[key]
            row = {
                **existing,
                "origin_generation": args[2],
                "presentation_revision": existing["presentation_revision"] + 1,
                "updated_at": self._tick(),
            }
            self.rows[key] = row
            return dict(row)

        raise AssertionError(f"unexpected Canvas SQL: {sql}")


def _file_set(
    *,
    path: str = "output/report.md",
    workspace_generation: UUID = _WORKSPACE_GENERATION,
    title: str = "Research report",
) -> CanvasSetInput:
    return CanvasSetInput(
        source=WorkspaceFileSource(
            path=path,
            workspace_generation=workspace_generation,
        ),
        title=title,
        renderer="markdown",
        source_version=_SOURCE_VERSION,
    )


def _browser_set(
    *,
    generation: UUID = UUID("33333333-cccc-4ccc-8ccc-333333333333"),
    title: str = "Shared browser",
) -> CanvasSetInput:
    return CanvasSetInput(
        source=BrowserSource(browser_generation=generation),
        title=title,
        renderer="auto",
        editable=False,
    )


def test_office_renderer_can_be_declared_editable_in_slice_two() -> None:
    source = WorkspaceFileSource(
        path="output/report.docx",
        workspace_generation=_WORKSPACE_GENERATION,
    )
    view = CanvasSetInput(
        source=source,
        title="Office report",
        renderer="office",
        source_version=_SOURCE_VERSION,
    )

    assert view.renderer == "office"
    assert view.editable is False
    editable = CanvasSetInput(
        source=source,
        title="Office report",
        renderer="office",
        editable=True,
        source_version=_SOURCE_VERSION,
    )
    assert editable.editable is True


def _seed_presented(db: _FakeCanvasDB) -> None:
    asyncio.run(CanvasService(db).set(_THREAD_ID, _file_set()))


def test_source_fingerprint_is_canonical_and_security_relevant() -> None:
    forward = WorkspaceAppSource(
        entry_port=5173,
        entry_path="/",
        routes=(
            WorkspaceAppRoute(path_prefix="/ws", port=8000),
            WorkspaceAppRoute(path_prefix="/api", port=8000),
        ),
        manifest_path=".srw/canvas.yaml",
        manifest_version="sha256:" + "b" * 64,
        workspace_generation=_WORKSPACE_GENERATION,
    )
    reverse = WorkspaceAppSource(
        entry_port=5173,
        entry_path="/",
        routes=tuple(reversed(forward.routes)),
        manifest_path="some/other/name.yaml",  # path is presentation metadata
        manifest_version=forward.manifest_version,
        workspace_generation=_WORKSPACE_GENERATION,
    )

    assert canonical_source_fingerprint(forward) == canonical_source_fingerprint(
        reverse
    )
    changed_generation = forward.model_copy(
        update={"workspace_generation": _OTHER_WORKSPACE_GENERATION}
    )
    changed_entry = forward.model_copy(update={"entry_port": 4173})
    assert canonical_source_fingerprint(forward) != canonical_source_fingerprint(
        changed_generation
    )
    assert canonical_source_fingerprint(forward) != canonical_source_fingerprint(
        changed_entry
    )


def test_public_representation_hashes_exact_visible_bytes_and_redacts_identity() -> (
    None
):
    db = _FakeCanvasDB()
    _seed_presented(db)
    record = asyncio.run(CanvasService(db).get(_THREAD_ID))
    assert record is not None

    unavailable = build_public_canvas_representation(record)
    ready = build_public_canvas_representation(record, status="ready")
    editable = build_public_canvas_representation(
        record,
        capabilities=CanvasCapabilities(can_edit=True),
    )
    body = json.loads(unavailable.payload)

    assert body["source"] == {
        "type": "workspace_file",
        "path": "output/report.md",
    }
    assert "workspace_generation" not in unavailable.payload.decode()
    assert "source_fingerprint" not in unavailable.payload.decode()
    assert unavailable.etag.startswith('"canvas:1:')
    assert unavailable.etag != ready.etag
    assert unavailable.etag != editable.etag


@pytest.mark.asyncio
async def test_set_refresh_replace_and_origin_rotation_are_atomic() -> None:
    db = _FakeCanvasDB()
    callbacks: list[dict[str, Any]] = []

    async def callback(event) -> None:
        assert not db.in_transaction
        callbacks.append(event.model_dump(mode="json"))

    service = CanvasService(db, event_callback=callback)
    first = await service.set(_THREAD_ID, _file_set())
    refresh = await service.set(_THREAD_ID, _file_set(title="Updated label"))
    replacement = await service.set(_THREAD_ID, _file_set(path="output/other.md"))

    assert first.record is not None and first.record.presentation_revision == 1
    assert refresh.record is not None and refresh.record.presentation_revision == 2
    assert replacement.record is not None
    assert replacement.record.presentation_revision == 3
    assert first.record.source_fingerprint == refresh.record.source_fingerprint
    assert replacement.record.source_fingerprint != first.record.source_fingerprint
    assert [event["method"] for event in callbacks] == [
        "canvas.updated",
        "canvas.updated",
        "canvas.updated",
    ]

    app_source = WorkspaceAppSource(
        entry_port=5173,
        workspace_generation=_WORKSPACE_GENERATION,
    )
    app_first = await service.set(
        _THREAD_ID,
        CanvasSetInput(source=app_source, title="Prototype"),
    )
    app_refresh = await service.set(
        _THREAD_ID,
        CanvasSetInput(source=app_source, title="Prototype refreshed"),
    )
    app_reset = await service.set(
        _THREAD_ID,
        CanvasSetInput(source=app_source, title="Prototype", new_app=True),
    )
    assert app_first.record is not None and app_first.record.origin_generation
    assert app_refresh.record is not None
    assert app_refresh.record.origin_generation == app_first.record.origin_generation
    assert app_reset.record is not None
    assert app_reset.record.origin_generation != app_first.record.origin_generation


@pytest.mark.asyncio
async def test_browser_set_if_changed_is_idempotent_but_ordinary_set_is_not() -> None:
    db = _FakeCanvasDB()
    callbacks: list[int] = []

    async def callback(event) -> None:
        callbacks.append(event.params.presentation_revision)

    service = CanvasService(db, event_callback=callback)
    first = await service.set_if_changed(_THREAD_ID, _browser_set())
    repeated = await service.set_if_changed(_THREAD_ID, _browser_set())
    retitled = await service.set_if_changed(
        _THREAD_ID, _browser_set(title="Research browser")
    )
    replaced = await service.set_if_changed(
        _THREAD_ID,
        _browser_set(generation=UUID("44444444-dddd-4ddd-8ddd-444444444444")),
    )
    ordinary = await service.set(
        _THREAD_ID,
        _browser_set(generation=UUID("44444444-dddd-4ddd-8ddd-444444444444")),
    )

    assert first.changed is True
    assert first.record is not None and first.record.presentation_revision == 1
    assert repeated.changed is False
    assert repeated.record is not None and repeated.record.presentation_revision == 1
    assert retitled.changed is True
    assert retitled.record is not None and retitled.record.presentation_revision == 2
    assert replaced.changed is True
    assert replaced.record is not None and replaced.record.presentation_revision == 3
    assert ordinary.record is not None and ordinary.record.presentation_revision == 4
    assert callbacks == [1, 2, 3, 4]
    assert db.advisory_calls == 4


@pytest.mark.asyncio
async def test_browser_set_if_changed_rejects_a_stale_presentation_revision() -> None:
    db = _FakeCanvasDB()
    service = CanvasService(db)
    presented = await service.set(_THREAD_ID, _file_set())
    assert presented.record is not None

    with pytest.raises(CanvasPreconditionFailed):
        await service.set_if_changed(
            _THREAD_ID,
            _browser_set(),
            expected_presentation_revision=presented.record.presentation_revision + 1,
        )

    unchanged = await service.get(_THREAD_ID)
    assert unchanged is not None
    assert isinstance(unchanged.source, WorkspaceFileSource)
    assert unchanged.presentation_revision == presented.record.presentation_revision

    replaced = await service.set_if_changed(
        _THREAD_ID,
        _browser_set(),
        expected_presentation_revision=presented.record.presentation_revision,
    )
    assert replaced.changed is True
    assert replaced.record is not None
    assert isinstance(replaced.record.source, BrowserSource)
    assert replaced.record.presentation_revision == 2


@pytest.mark.asyncio
async def test_browser_set_if_changed_serializes_concurrent_missing_row() -> None:
    db = _FakeCanvasDB()
    callbacks: list[int] = []

    async def callback(event) -> None:
        callbacks.append(event.params.presentation_revision)

    first, second = await asyncio.gather(
        CanvasService(db, event_callback=callback).set_if_changed(
            _THREAD_ID, _browser_set()
        ),
        CanvasService(db, event_callback=callback).set_if_changed(
            _THREAD_ID, _browser_set()
        ),
    )

    assert sorted((first.changed, second.changed)) == [False, True]
    assert first.record is not None and first.record.presentation_revision == 1
    assert second.record is not None and second.record.presentation_revision == 1
    assert callbacks == [1]
    assert db.advisory_calls == 2


@pytest.mark.asyncio
async def test_browser_set_if_changed_rejects_noncanonical_presentation() -> None:
    db = _FakeCanvasDB()
    presentation = _browser_set().model_copy(update={"renderer": "markdown"})

    with pytest.raises(ValueError, match="restricted to Browser"):
        await CanvasService(db).set_if_changed(_THREAD_ID, presentation)

    assert db.rows == {}
    assert db.advisory_calls == 0


def test_browser_presentation_lock_is_stable_and_domain_separated() -> None:
    first = canvas_presentation_lock_key(_THREAD_ID, "main")
    second = canvas_presentation_lock_key(_THREAD_ID, "main")

    assert first == second
    assert -(2**63) <= first < 2**63
    assert first != canvas_presentation_lock_key(_THREAD_ID, "secondary")


@pytest.mark.asyncio
async def test_conditional_clear_and_repeated_clear_semantics() -> None:
    db = _FakeCanvasDB()
    callbacks: list[str] = []

    async def callback(event) -> None:
        assert not db.in_transaction
        callbacks.append(event.method)

    service = CanvasService(db, event_callback=callback)
    absent = await service.clear(_THREAD_ID, expected_etag=None)
    assert not absent.changed and absent.record is None

    presented = await service.set(_THREAD_ID, _file_set())
    assert presented.record is not None
    before = build_public_canvas_representation(presented.record)

    with pytest.raises(CanvasPreconditionRequired):
        await service.clear(_THREAD_ID, expected_etag=None)
    with pytest.raises(CanvasPreconditionFailed):
        await service.clear(_THREAD_ID, expected_etag='"canvas:stale"')
    unchanged = await service.get(_THREAD_ID)
    assert unchanged is not None and unchanged.presentation_revision == 1

    cleared = await service.clear(_THREAD_ID, expected_etag=before.etag)
    assert cleared.changed and cleared.record is not None
    assert cleared.record.presentation_revision == 2
    assert cleared.record.source is None
    assert cleared.record.title is None
    assert cleared.record.renderer == "auto"
    assert cleared.record.editable is False
    assert cleared.record.source_fingerprint is None
    assert cleared.record.source_version is None
    assert cleared.record.origin_generation is None
    assert build_public_canvas_representation(cleared.record).state.status == "cleared"

    repeated = await service.clear(_THREAD_ID, expected_etag=None)
    assert not repeated.changed
    assert repeated.record is not None
    assert repeated.record.presentation_revision == 2
    assert callbacks == ["canvas.updated", "canvas.cleared"]

    represented = await service.set(_THREAD_ID, _file_set())
    assert represented.record is not None
    assert represented.record.presentation_revision == 3


@pytest.mark.asyncio
async def test_conditional_live_app_origin_reset_preserves_source() -> None:
    db = _FakeCanvasDB()
    callbacks: list[str] = []

    async def callback(event) -> None:
        assert not db.in_transaction
        callbacks.append(event.method)

    service = CanvasService(db, event_callback=callback)
    source = WorkspaceAppSource(
        entry_port=5173,
        entry_path="/demo",
        workspace_generation=_WORKSPACE_GENERATION,
    )
    presented = await service.set(
        _THREAD_ID,
        CanvasSetInput(source=source, title="Prototype"),
    )
    assert presented.record is not None
    before = presented.record
    representation = build_public_canvas_representation(before)

    with pytest.raises(CanvasPreconditionRequired):
        await service.reset_origin(
            _THREAD_ID,
            expected_etag=None,
            expected_thread_user_id="user-1",
        )
    with pytest.raises(CanvasPreconditionFailed):
        await service.reset_origin(
            _THREAD_ID,
            expected_etag='"canvas:stale"',
            expected_thread_user_id="user-1",
        )

    reset = await service.reset_origin(
        _THREAD_ID,
        expected_etag=representation.etag,
        expected_thread_user_id="user-1",
    )
    assert reset.record is not None
    assert reset.record.source == before.source
    assert reset.record.source_fingerprint == before.source_fingerprint
    assert reset.record.title == before.title
    assert reset.record.presentation_revision == before.presentation_revision + 1
    assert reset.record.origin_generation != before.origin_generation
    assert callbacks == ["canvas.updated", "canvas.updated"]


@pytest.mark.asyncio
async def test_origin_reset_revalidates_owner_and_rejects_files() -> None:
    db = _FakeCanvasDB()
    service = CanvasService(db)
    await service.set(_THREAD_ID, _file_set())
    record = await service.get(_THREAD_ID)
    assert record is not None
    etag = build_public_canvas_representation(record).etag

    with pytest.raises(CanvasEditError) as not_app:
        await service.reset_origin(
            _THREAD_ID,
            expected_etag=etag,
            expected_thread_user_id="user-1",
        )
    assert not_app.value.status_code == 409
    assert not_app.value.code == "canvas_not_live_app"

    with pytest.raises(CanvasEditError) as unauthorized:
        await service.reset_origin(
            _THREAD_ID,
            expected_etag=etag,
            expected_thread_user_id="other-user",
        )
    assert unauthorized.value.status_code == 403
    assert unauthorized.value.code == "canvas_not_authorized"


@pytest.mark.asyncio
async def test_only_main_canvas_is_accepted() -> None:
    service = CanvasService(_FakeCanvasDB())
    with pytest.raises(ValueError, match="only canvas_id='main'"):
        await service.get(_THREAD_ID, canvas_id="secondary")
    with pytest.raises(ValueError, match="only canvas_id='main'"):
        await service.set(_THREAD_ID, _file_set(), canvas_id="secondary")


def _route_app(monkeypatch, db: _FakeCanvasDB):
    from routers import canvases as canvases_router_module

    owner_calls: list[tuple[Any, str]] = []

    async def owner(request, received_db, thread_id):
        owner_calls.append((received_db, thread_id))
        return {"id": "user-1"}, {"id": thread_id, "user_id": "user-1"}

    monkeypatch.setattr(canvases_router_module, "_get_db", lambda: db)
    monkeypatch.setattr(canvases_router_module, "require_thread_owner", owner)
    app = FastAPI()
    app.include_router(canvases_router_module.router)
    return app, owner_calls


def test_get_route_returns_204_for_absent_and_revalidates_present_state(
    monkeypatch,
) -> None:
    db = _FakeCanvasDB()
    app, owner_calls = _route_app(monkeypatch, db)
    client = TestClient(app)

    absent = client.get(f"/api/persistent/threads/{_THREAD_ID}/canvases/main")
    assert absent.status_code == 204
    assert absent.headers["cache-control"] == "private, no-cache"

    _seed_presented(db)
    current = client.get(f"/api/persistent/threads/{_THREAD_ID}/canvases/main")
    assert current.status_code == 200
    assert current.headers["etag"].startswith('"canvas:1:')
    assert current.headers["cache-control"] == "private, no-cache"
    assert current.json()["status"] == "unavailable"
    assert current.json()["capabilities"] == {
        "can_edit": False,
        "can_pop_out": False,
        "can_take_control": False,
        "can_create_viewer_session": False,
        "can_stream_browser": False,
        "can_view_office": False,
    }

    not_modified = client.get(
        f"/api/persistent/threads/{_THREAD_ID}/canvases/main",
        headers={"If-None-Match": f"W/{current.headers['etag']}"},
    )
    assert not_modified.status_code == 304
    assert not not_modified.content
    # Present state is re-authorized after its potentially remote
    # representation is materialized, including the conditional-GET path.
    assert len(owner_calls) == 5
    assert all(call == (db, _THREAD_ID) for call in owner_calls)


def test_delete_route_requires_exact_etag_then_becomes_idempotent(monkeypatch) -> None:
    db = _FakeCanvasDB()
    _seed_presented(db)
    app, _ = _route_app(monkeypatch, db)
    client = TestClient(app)
    url = f"/api/persistent/threads/{_THREAD_ID}/canvases/main"

    missing = client.delete(url)
    assert missing.status_code == 428
    assert missing.json()["detail"]["code"] == "canvas_precondition_required"

    stale = client.delete(url, headers={"If-Match": '"canvas:0:stale"'})
    assert stale.status_code == 412
    assert stale.json()["detail"]["code"] == "canvas_precondition_failed"

    current = client.get(url)
    cleared = client.delete(url, headers={"If-Match": current.headers["etag"]})
    assert cleared.status_code == 200
    assert cleared.json()["source"] is None
    assert cleared.json()["status"] == "cleared"
    assert cleared.json()["presentation_revision"] == 2
    assert cleared.headers["etag"].startswith('"canvas:2:')

    repeated = client.delete(url)
    assert repeated.status_code == 204
    persisted = client.get(url)
    assert persisted.status_code == 200
    assert persisted.json()["presentation_revision"] == 2


def test_delete_route_accepts_the_weakened_form_of_its_own_etag(monkeypatch) -> None:
    """The read path already tolerates ``W/``; every write path must agree.

    A compressing CDN in front of the API rewrites our strong ETag to its weak
    form, so that is the only value the browser holds for the state it is
    conditioning on. Nothing about the represented state changed.
    """

    db = _FakeCanvasDB()
    _seed_presented(db)
    app, _ = _route_app(monkeypatch, db)
    client = TestClient(app)
    url = f"/api/persistent/threads/{_THREAD_ID}/canvases/main"

    current = client.get(url)
    cleared = client.delete(url, headers={"If-Match": f"W/{current.headers['etag']}"})

    assert cleared.status_code == 200
    assert cleared.json()["status"] == "cleared"


def test_delete_route_refuses_a_weakened_etag_for_another_state(monkeypatch) -> None:
    """Tolerating the weak marker must not loosen the digest comparison."""

    db = _FakeCanvasDB()
    _seed_presented(db)
    app, _ = _route_app(monkeypatch, db)
    client = TestClient(app)
    url = f"/api/persistent/threads/{_THREAD_ID}/canvases/main"

    stale = client.delete(url, headers={"If-Match": 'W/"canvas:1:' + "0" * 64 + '"'})
    assert stale.status_code == 412
    assert stale.json()["detail"]["code"] == "canvas_precondition_failed"

    wildcard = client.delete(url, headers={"If-Match": "*"})
    assert wildcard.status_code == 412
    assert wildcard.json()["detail"]["code"] == "canvas_precondition_failed"


def test_reset_origin_route_accepts_the_weakened_form_of_its_own_etag(
    monkeypatch,
) -> None:
    """The shape gate must admit the weak marker the CDN prepends."""

    db = _FakeCanvasDB()
    source = WorkspaceAppSource(
        entry_port=5173,
        entry_path="/demo",
        workspace_generation=_WORKSPACE_GENERATION,
    )
    asyncio.run(
        CanvasService(db).set(
            _THREAD_ID,
            CanvasSetInput(source=source, title="Prototype"),
        )
    )
    app, _ = _route_app(monkeypatch, db)
    client = TestClient(app)
    base = f"/api/persistent/threads/{_THREAD_ID}/canvases/main"

    current = client.get(base)
    reset = client.post(
        f"{base}/reset-origin",
        headers={"If-Match": f"W/{current.headers['etag']}"},
    )

    assert reset.status_code == 200
    assert reset.json()["presentation_revision"] == 2


def test_reset_origin_route_is_conditional_and_never_exposes_generation(
    monkeypatch,
) -> None:
    db = _FakeCanvasDB()
    source = WorkspaceAppSource(
        entry_port=5173,
        entry_path="/demo",
        workspace_generation=_WORKSPACE_GENERATION,
    )
    asyncio.run(
        CanvasService(db).set(
            _THREAD_ID,
            CanvasSetInput(source=source, title="Prototype"),
        )
    )
    before = asyncio.run(CanvasService(db).get(_THREAD_ID))
    assert before is not None and before.origin_generation is not None

    app, _ = _route_app(monkeypatch, db)
    client = TestClient(app)
    base = f"/api/persistent/threads/{_THREAD_ID}/canvases/main"
    reset_url = f"{base}/reset-origin"

    missing = client.post(reset_url)
    assert missing.status_code == 428
    assert missing.json()["detail"]["code"] == "canvas_precondition_required"

    malformed = client.post(reset_url, headers={"If-Match": "not-an-etag"})
    assert malformed.status_code == 400
    assert malformed.json()["detail"]["code"] == "invalid_canvas_precondition"

    current = client.get(base)
    assert current.status_code == 200
    stale = client.post(
        reset_url,
        headers={"If-Match": '"canvas:1:' + "0" * 64 + '"'},
    )
    assert stale.status_code == 412
    assert stale.json()["detail"]["code"] == "canvas_precondition_failed"

    reset = client.post(reset_url, headers={"If-Match": current.headers["etag"]})
    assert reset.status_code == 200
    assert reset.headers["cache-control"] == "private, no-cache"
    assert reset.headers["etag"].startswith('"canvas:2:')
    assert reset.json()["presentation_revision"] == 2
    assert reset.json()["source"] == {
        "type": "workspace_app",
        "manifest_path": None,
        "entry_path": "/demo",
    }
    assert "origin_generation" not in reset.text

    persisted = asyncio.run(CanvasService(db).get(_THREAD_ID))
    assert persisted is not None
    assert persisted.source == before.source
    assert persisted.origin_generation != before.origin_generation


def test_reset_origin_route_rejects_non_app_and_request_body(monkeypatch) -> None:
    db = _FakeCanvasDB()
    _seed_presented(db)
    app, _ = _route_app(monkeypatch, db)
    client = TestClient(app)
    base = f"/api/persistent/threads/{_THREAD_ID}/canvases/main"
    state = client.get(base)

    body = client.post(
        f"{base}/reset-origin",
        headers={"If-Match": state.headers["etag"]},
        json={"source": "caller-controlled"},
    )
    assert body.status_code == 400
    assert body.json()["detail"]["code"] == "invalid_canvas_refresh"

    not_app = client.post(
        f"{base}/reset-origin",
        headers={"If-Match": state.headers["etag"]},
    )
    assert not_app.status_code == 409
    assert not_app.json()["detail"]["code"] == "canvas_not_live_app"


def test_routes_fail_at_owner_gate_before_canvas_access(monkeypatch) -> None:
    from routers import canvases as canvases_router_module

    db = _FakeCanvasDB()

    async def denied(request, received_db, thread_id):
        raise HTTPException(status_code=403, detail="Not your thread")

    monkeypatch.setattr(canvases_router_module, "_get_db", lambda: db)
    monkeypatch.setattr(canvases_router_module, "require_thread_owner", denied)
    app = FastAPI()
    app.include_router(canvases_router_module.router)
    response = TestClient(app).get(
        f"/api/persistent/threads/{_THREAD_ID}/canvases/main"
    )
    assert response.status_code == 403
    assert db.acquire_count == 0


def test_main_app_mounts_slice_one_canvas_routes() -> None:
    from main import app

    routes = mounted_routes(app)
    path = "/api/persistent/threads/{thread_id}/canvases/main"
    assert ("GET", path) in routes
    assert ("DELETE", path) in routes
    assert (
        "POST",
        "/api/persistent/threads/{thread_id}/canvases/main/reset-origin",
    ) in routes
    internal = "/api/internal/persistent/threads/{thread_id}/canvases/main"
    assert ("GET", internal) in routes
    assert ("DELETE", internal) in routes
    assert (
        "POST",
        "/api/internal/persistent/threads/{thread_id}/canvases/main/set",
    ) in routes


def test_main_cors_exposes_canvas_etag_to_local_cockpit(monkeypatch) -> None:
    from main import app
    from routers import canvases as canvases_router_module

    db = _FakeCanvasDB()
    _seed_presented(db)

    async def owner(request, received_db, thread_id):
        return {"id": "user-1"}, {"id": thread_id, "user_id": "user-1"}

    monkeypatch.setattr(canvases_router_module, "_get_db", lambda: db)
    monkeypatch.setattr(canvases_router_module, "require_thread_owner", owner)
    response = TestClient(app).get(
        f"/api/persistent/threads/{_THREAD_ID}/canvases/main",
        headers={"Origin": "http://localhost:4200"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-expose-headers"] == (
        "ETag, X-Canvas-Content-ETag, X-Canvas-Mutation-Changed"
    )
    assert response.headers["access-control-allow-origin"] == "http://localhost:4200"


def test_canvas_migration_has_thread_cascade_and_single_slot_key() -> None:
    migration_path = Path("orchestrator/database/migrations/app/0058_canvases.sql")
    migration_bytes = migration_path.read_bytes()
    migration = migration_bytes.decode()
    assert hashlib.sha256(migration_bytes).hexdigest() == (
        "e04eb6a4e27a120ec86682226b3cfa9c6abeeeb64d53b9781a45ae83ef11cff5"
    )
    assert (
        "thread_id             UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE"
        in migration
    )
    assert "UNIQUE (thread_id, canvas_id)" in migration
    assert "presentation_revision BIGINT NOT NULL DEFAULT 0" in migration
    assert "uq_canvases_origin_generation" in migration


def test_browser_source_public_shape_does_not_expose_generation() -> None:
    source = BrowserSource(
        browser_generation=UUID("33333333-cccc-4ccc-8ccc-333333333333")
    )
    fingerprint = canonical_source_fingerprint(source)
    assert fingerprint.startswith("sha256:") and len(fingerprint) == 71
