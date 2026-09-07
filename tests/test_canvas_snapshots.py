"""Durable Canvas presentation: capture, offline serve, and generation re-pin.

Acceptance criteria are numbered in
``knowledge-base/knowledge/features/canvas_durable_presentation.md`` §7; each test names the ones
it covers. Object storage is stubbed — these never touch a live bucket.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.services import canvas_snapshots
from orchestrator.services.canvas import (
    CanvasEditError,
    CanvasRecord,
    CanvasService,
    WorkspaceAppSource,
    WorkspaceFileSource,
    canonical_source_fingerprint,
)
from orchestrator.services.canvas_files import CanvasFileError, ValidatedCanvasFile
from orchestrator.services.canvas_snapshots import CanvasSnapshotStore

THREAD_ID = "a3333333-3333-3333-3333-333333333333"
USER_ID = "b4444444-4444-4444-8444-444444444444"
GENERATION = UUID("11111111-aaaa-4aaa-8aaa-111111111111")
NEW_GENERATION = UUID("22222222-bbbb-4bbb-8bbb-222222222222")
NOW = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _file(
    data: bytes = b"# Canvas\n",
    *,
    renderer: str = "markdown",
    media_type: str = "text/markdown",
    path: str = "output/report.md",
) -> ValidatedCanvasFile:
    return ValidatedCanvasFile(
        path=path,
        data=data,
        media_type=media_type,
        renderer=renderer,  # type: ignore[arg-type]
        source_version=_sha(data),
        last_modified=NOW,
    )


def _record(
    data: bytes = b"# Canvas\n",
    *,
    revision: int = 1,
    generation: UUID = GENERATION,
    editable: bool = False,
) -> CanvasRecord:
    file = _file(data)
    source = WorkspaceFileSource(path=file.path, workspace_generation=generation)
    return CanvasRecord(
        thread_id=THREAD_ID,
        canvas_id="main",
        source=source,
        title="Report",
        renderer="markdown",
        editable=editable,
        alt_text=None,
        presentation_revision=revision,
        source_fingerprint=canonical_source_fingerprint(source),
        source_version=file.source_version,
        origin_generation=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _thread(*, generation: UUID = GENERATION) -> dict[str, Any]:
    return {
        "id": THREAD_ID,
        "user_id": USER_ID,
        "metadata": {
            "config_override": {"workspace": {"backend": "sandbox"}},
            "_workspace_binding": {
                "generation": str(generation),
                "kind": "remote",
                "backing_id": "k8s-pvc:srw:test",
                "ssh_host_key_fingerprint": "SHA256:test",
            },
            "workspace_container": {
                "status": "ready",
                "provisioner": "k8s",
                "pod_ip": "workspace.test",
                "pod_port": 30022,
                "pod_name": "workspace-test",
                "namespace": "srw",
                "_canvas_workspace_generation": str(generation),
                "_runtime_incarnation": "22222222-bbbb-4bbb-8bbb-222222222222",
            },
        },
    }


class _FakeBlobs:
    """Minimal SnapshotService surface: put/get/delete over a dict."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_keys: list[str] = []
        self.deleted: list[str] = []
        self.fail_put = False
        self.raise_get = False

    async def put_blob(self, key, data, *, content_type="application/octet-stream"):
        del content_type
        self.put_keys.append(key)
        if self.fail_put:
            return False
        self.objects[key] = bytes(data)
        return True

    async def get_blob(self, key):
        if self.raise_get:
            raise RuntimeError("object store unreachable")
        return self.objects.get(key)

    async def delete_blob(self, key):
        self.deleted.append(key)
        return self.objects.pop(key, None) is not None


class _SnapshotDB:
    """asyncpg-shaped double for the canvas_snapshots statements only."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, query: str, *args: Any):
        sql = " ".join(query.split())
        assert "FROM canvas_snapshots" in sql, sql
        return self.rows.get((str(args[0]), str(args[1])))

    async def fetchval(self, query: str, *args: Any):
        sql = " ".join(query.split())
        key = (str(args[0]), str(args[1]))
        if sql.startswith("SELECT object_key"):
            row = self.rows.get(key)
            return row["object_key"] if row else None
        if sql.startswith("DELETE FROM canvas_snapshots"):
            row = self.rows.pop(key, None)
            return row["object_key"] if row else None
        raise AssertionError(f"unexpected SQL: {sql}")

    async def execute(self, query: str, *args: Any):
        sql = " ".join(query.split())
        assert sql.startswith("INSERT INTO canvas_snapshots"), sql
        (
            thread_id,
            canvas_id,
            path,
            renderer,
            media_type,
            source_version,
            object_key,
            byte_size,
            last_modified,
        ) = args
        self.rows[(str(thread_id), str(canvas_id))] = {
            "thread_id": str(thread_id),
            "canvas_id": str(canvas_id),
            "path": path,
            "renderer": renderer,
            "media_type": media_type,
            "source_version": source_version,
            "object_key": object_key,
            "byte_size": byte_size,
            "last_modified": last_modified,
            "captured_at": NOW,
        }


def _store() -> tuple[CanvasSnapshotStore, _SnapshotDB, _FakeBlobs]:
    db = _SnapshotDB()
    blobs = _FakeBlobs()
    return CanvasSnapshotStore(db, blobs=blobs), db, blobs


# ---------------------------------------------------------------------------
# Store behavior (criteria 1, 5, 6, 7, 9, 10, 11, 12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_writes_row_and_object_keyed_by_thread() -> None:
    """AC1: one row, one object, thread-scoped key, matching byte_size."""

    store, db, blobs = _store()
    file = _file()

    assert await store.capture(THREAD_ID, file) is True

    row = db.rows[(THREAD_ID, "main")]
    digest = file.source_version.split(":", 1)[1]
    assert row["object_key"] == f"canvas/{THREAD_ID}/main/{digest}"
    assert row["source_version"] == file.source_version
    assert row["byte_size"] == len(file.data)
    assert blobs.objects[row["object_key"]] == file.data


@pytest.mark.asyncio
async def test_republishing_new_bytes_discards_the_superseded_object() -> None:
    """AC6: replacement deletes the object the previous version pointed at."""

    store, db, blobs = _store()
    first = _file(b"# One\n")
    await store.capture(THREAD_ID, first)
    first_key = db.rows[(THREAD_ID, "main")]["object_key"]

    second = _file(b"# Two\n")
    await store.capture(THREAD_ID, second)

    assert first_key in blobs.deleted
    assert first_key not in blobs.objects
    assert len(db.rows) == 1
    assert blobs.objects[db.rows[(THREAD_ID, "main")]["object_key"]] == second.data


@pytest.mark.asyncio
async def test_delete_removes_row_then_object() -> None:
    """AC6: clearing forgets both halves."""

    store, db, blobs = _store()
    await store.capture(THREAD_ID, _file())
    key = db.rows[(THREAD_ID, "main")]["object_key"]

    await store.delete(THREAD_ID)

    assert db.rows == {}
    assert blobs.objects == {}
    assert blobs.deleted == [key]


@pytest.mark.asyncio
async def test_failed_object_delete_still_drops_the_row() -> None:
    """AC7: a delete_blob failure leaks an object, never a live pointer."""

    store, db, blobs = _store()
    await store.capture(THREAD_ID, _file())

    async def refuse(key):
        return False

    blobs.delete_blob = refuse  # type: ignore[assignment]
    await store.delete(THREAD_ID)
    assert db.rows == {}


@pytest.mark.asyncio
async def test_snapshot_for_a_different_version_is_never_usable() -> None:
    """AC9: the live row's source_version is the only key that serves."""

    store, _, _ = _store()
    await store.capture(THREAD_ID, _file(b"# One\n"))

    assert await store.usable(THREAD_ID, _sha(b"# One\n")) is not None
    assert await store.usable(THREAD_ID, _sha(b"# Different\n")) is None
    assert await store.usable(THREAD_ID, None) is None


@pytest.mark.asyncio
async def test_load_degrades_when_the_object_store_is_unreachable() -> None:
    """AC10: store failures degrade, they do not raise."""

    store, _, blobs = _store()
    file = _file()
    await store.capture(THREAD_ID, file)

    blobs.raise_get = True
    assert await store.load(THREAD_ID, file.source_version) is None

    blobs.raise_get = False
    blobs.objects.clear()
    assert await store.load(THREAD_ID, file.source_version) is None


@pytest.mark.asyncio
async def test_load_rejects_and_drops_bytes_that_fail_their_hash() -> None:
    """A wrong-bytes object must never be served under a strong ETag."""

    store, db, blobs = _store()
    file = _file()
    await store.capture(THREAD_ID, file)
    key = db.rows[(THREAD_ID, "main")]["object_key"]
    blobs.objects[key] = b"tampered"

    assert await store.load(THREAD_ID, file.source_version) is None
    assert db.rows == {}


@pytest.mark.asyncio
async def test_capture_failure_is_non_fatal() -> None:
    """AC11: an upload failure writes no row and reports False."""

    store, db, blobs = _store()
    blobs.fail_put = True

    assert await store.capture(THREAD_ID, _file()) is False
    assert db.rows == {}


@pytest.mark.asyncio
async def test_office_and_oversize_files_are_not_eligible(monkeypatch) -> None:
    """AC5 + §4.2: Office is excluded; the cap is a hard gate."""

    store, db, _ = _store()
    office = _file(
        b"PK\x03\x04office",
        renderer="office",
        media_type="application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document",
        path="output/report.docx",
    )
    assert await store.capture(THREAD_ID, office) is False

    monkeypatch.setattr(canvas_snapshots, "CANVAS_SNAPSHOT_MAX_BYTES", 4)
    assert await store.capture(THREAD_ID, _file(b"# far too long\n")) is False
    assert db.rows == {}


@pytest.mark.asyncio
async def test_zero_cap_disables_capture_and_lookup(monkeypatch) -> None:
    """AC12: the kill switch removes the departure entirely."""

    store, db, _ = _store()
    await store.capture(THREAD_ID, _file())
    monkeypatch.setattr(canvas_snapshots, "CANVAS_SNAPSHOT_MAX_BYTES", 0)

    assert await store.capture(THREAD_ID, _file(b"# other\n")) is False
    assert await store.lookup(THREAD_ID) is None
    assert await store.usable(THREAD_ID, _sha(b"# Canvas\n")) is None
    assert len(db.rows) == 1  # pre-existing rows are left alone, just unused


# ---------------------------------------------------------------------------
# Route behavior (criteria 2, 3, 4, 8)
# ---------------------------------------------------------------------------


class _RouteService:
    def __init__(self, record: CanvasRecord | None) -> None:
        self.record = record

    async def get(self, thread_id, **kwargs):
        del kwargs
        assert thread_id == THREAD_ID
        return self.record

    async def repin_workspace_generation(self, thread_id, **kwargs):
        del thread_id, kwargs
        from orchestrator.services.canvas import CanvasMutation

        return CanvasMutation(changed=False, record=self.record)


class _OfflineGateway:
    """A workspace that cannot answer for anything."""

    def __init__(self, code: str = "workspace_unavailable") -> None:
        self.code = code
        self.calls = 0

    async def materialize_current(self, thread, record):
        del thread, record
        self.calls += 1
        raise CanvasFileError(409, self.code, "workspace is gone")

    def supports_editing(self, thread, record=None) -> bool:
        del thread, record
        return True


def _route_client(monkeypatch, *, record, gateway, store):
    from orchestrator.routers import canvases

    db = object()
    service = _RouteService(record)

    async def owner(request, received_db, thread_id):
        del request, received_db
        assert thread_id == THREAD_ID
        return {"id": USER_ID}, _thread()

    monkeypatch.setattr(canvases, "_get_db", lambda: db)
    monkeypatch.setattr(canvases, "_get_canvas_service", lambda received: service)
    monkeypatch.setattr(canvases, "_get_file_gateway", lambda received=None: gateway)
    monkeypatch.setattr(canvases, "_get_snapshot_store", lambda received: store)
    monkeypatch.setattr(canvases, "require_thread_owner", owner)
    app = FastAPI()
    app.include_router(canvases.router)
    return TestClient(app)


@pytest.mark.asyncio
async def test_offline_state_is_ready_from_the_snapshot(monkeypatch) -> None:
    """AC2: a sleeping workspace yields a read-only ready state, not dark."""

    store, _, _ = _store()
    record = _record(editable=True)
    await store.capture(THREAD_ID, _file())
    client = _route_client(
        monkeypatch, record=record, gateway=_OfflineGateway(), store=store
    )

    response = client.get(f"/api/persistent/threads/{THREAD_ID}/canvases/main")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["content_origin"] == "snapshot"
    assert body["content_captured_at"] is not None
    assert body["capabilities"]["can_edit"] is False
    assert body["capabilities"]["can_pop_out"] is True
    assert body["content_url"]


@pytest.mark.asyncio
async def test_offline_state_without_a_snapshot_stays_unavailable(
    monkeypatch,
) -> None:
    """AC4/AC5: no stored copy means exactly the pre-existing behavior."""

    store, _, _ = _store()
    client = _route_client(
        monkeypatch, record=_record(), gateway=_OfflineGateway(), store=store
    )

    body = client.get(f"/api/persistent/threads/{THREAD_ID}/canvases/main").json()

    assert body["status"] == "unavailable"
    assert body["content_origin"] is None


@pytest.mark.asyncio
async def test_source_changed_never_falls_back_to_the_snapshot(
    monkeypatch,
) -> None:
    """Changed bytes are an honest source_changed, not a papered-over copy."""

    store, _, _ = _store()
    await store.capture(THREAD_ID, _file())
    client = _route_client(
        monkeypatch,
        record=_record(),
        gateway=_OfflineGateway(code="source_changed"),
        store=store,
    )

    body = client.get(f"/api/persistent/threads/{THREAD_ID}/canvases/main").json()

    assert body["status"] == "source_changed"
    assert body["content_origin"] is None


@pytest.mark.asyncio
async def test_offline_content_serves_stored_bytes_with_the_same_contract(
    monkeypatch,
) -> None:
    """AC3: identical ETag, headers, conditional and range behavior."""

    store, _, _ = _store()
    file = _file()
    record = _record()
    await store.capture(THREAD_ID, file)
    client = _route_client(
        monkeypatch, record=record, gateway=_OfflineGateway(), store=store
    )

    base = f"/api/persistent/threads/{THREAD_ID}/canvases/main/content"
    query = {
        "presentation_revision": record.presentation_revision,
        "source_fingerprint": record.source_fingerprint,
        "source_version": record.source_version,
        "ngsw-bypass": "true",
    }

    response = client.get(base, params=query)
    assert response.status_code == 200
    assert response.content == file.data
    assert response.headers["ETag"] == f'"{file.source_version}"'
    assert response.headers["X-Canvas-Content-Origin"] == "snapshot"
    assert response.headers["Content-Type"].startswith("text/markdown")
    assert response.headers["X-Content-Type-Options"] == "nosniff"

    not_modified = client.get(
        base, params=query, headers={"If-None-Match": f'"{file.source_version}"'}
    )
    assert not_modified.status_code == 304

    ranged = client.get(base, params=query, headers={"Range": "bytes=0-2"})
    assert ranged.status_code == 206
    assert ranged.content == file.data[:3]


@pytest.mark.asyncio
async def test_offline_content_without_a_snapshot_reports_the_workspace_error(
    monkeypatch,
) -> None:
    """AC10: the fallback is additive; its absence changes nothing."""

    store, _, _ = _store()
    record = _record()
    client = _route_client(
        monkeypatch, record=record, gateway=_OfflineGateway(), store=store
    )

    response = client.get(
        f"/api/persistent/threads/{THREAD_ID}/canvases/main/content",
        params={
            "presentation_revision": record.presentation_revision,
            "source_fingerprint": record.source_fingerprint,
            "source_version": record.source_version,
            "ngsw-bypass": "true",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "workspace_unavailable"


# ---------------------------------------------------------------------------
# Re-pin (criteria 13, 14, 15, 16, 17)
# ---------------------------------------------------------------------------


class _RepinDB:
    """asyncpg-shaped double for repin_workspace_generation's statements."""

    def __init__(
        self,
        record: CanvasRecord,
        *,
        generation: UUID,
        thread: dict[str, Any] | None = None,
    ) -> None:
        self.record = record
        self.generation = generation
        self.thread = thread or _thread(generation=generation)
        self.updates = 0
        self.thread_lock_queries: list[str] = []

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield

    def _row(self) -> dict[str, Any]:
        record = self.record
        source = record.source
        return {
            "thread_id": record.thread_id,
            "canvas_id": record.canvas_id,
            "source": (
                json.dumps(source.model_dump(mode="json"))
                if source is not None
                else None
            ),
            "title": record.title,
            "renderer": record.renderer,
            "editable": record.editable,
            "alt_text": record.alt_text,
            "presentation_revision": record.presentation_revision,
            "source_fingerprint": record.source_fingerprint,
            "source_version": record.source_version,
            "origin_generation": record.origin_generation,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    async def fetchrow(self, query: str, *args: Any):
        sql = " ".join(query.split())
        if "FROM threads" in sql:
            self.thread_lock_queries.append(sql)
            return dict(self.thread)
        if sql.startswith("SELECT thread_id"):
            return self._row()
        if sql.startswith("UPDATE canvases"):
            self.updates += 1
            source = json.loads(args[2])
            self.record = replace(
                self.record,
                source=WorkspaceFileSource(**source),
                source_fingerprint=args[3],
                presentation_revision=self.record.presentation_revision + 1,
            )
            return self._row()
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.mark.asyncio
async def test_repin_adopts_the_new_generation_when_bytes_are_identical() -> None:
    """AC13: the suspend -> S3 restore case, where only the UUID moved."""

    record = _record()
    db = _RepinDB(record, generation=NEW_GENERATION)
    service = CanvasService(db)

    async def verifier(locked, thread):
        del locked, thread
        return NEW_GENERATION

    mutation = await service.repin_workspace_generation(
        THREAD_ID, expected_source_version=record.source_version, verifier=verifier
    )

    assert mutation.changed is True
    assert mutation.record is not None
    assert isinstance(mutation.record.source, WorkspaceFileSource)
    assert mutation.record.source.workspace_generation == NEW_GENERATION
    # The revision must advance: source_fingerprint covers the generation and
    # is embedded in every issued content_url.
    assert mutation.record.presentation_revision == record.presentation_revision + 1
    assert mutation.record.source_fingerprint != record.source_fingerprint
    assert mutation.record.source_version == record.source_version


@pytest.mark.asyncio
async def test_repin_declines_when_the_verifier_finds_different_bytes() -> None:
    """AC14/AC15: a changed or missing file is never silently re-pinned."""

    record = _record()
    db = _RepinDB(record, generation=NEW_GENERATION)
    service = CanvasService(db)

    async def verifier(locked, thread):
        del locked, thread
        return None

    mutation = await service.repin_workspace_generation(
        THREAD_ID, expected_source_version=record.source_version, verifier=verifier
    )

    assert mutation.changed is False
    assert db.updates == 0


@pytest.mark.asyncio
async def test_repin_refuses_stateless_claim_loss_before_workspace_verifier() -> None:
    """Generation repair is remote I/O and shares the terminal write fence."""

    record = _record()
    thread = _thread(generation=NEW_GENERATION)
    thread.update(execution_lane="stateless", status="active")
    thread["metadata"] = {
        **thread["metadata"],
        "_stateless_claim_losses": {
            "7": {"pod": "agent-a", "pod_uid": "uid-a", "quiesced": False}
        },
    }
    db = _RepinDB(record, generation=NEW_GENERATION, thread=thread)
    verifier_called = False

    async def verifier(*_args):
        nonlocal verifier_called
        verifier_called = True
        raise AssertionError("retiring stateless thread reached workspace verifier")

    with pytest.raises(CanvasEditError) as error:
        await CanvasService(db).repin_workspace_generation(
            THREAD_ID,
            expected_source_version=record.source_version,
            verifier=verifier,
        )

    assert error.value.status_code == 409
    assert error.value.code == "canvas_editing_unavailable"
    assert verifier_called is False
    assert db.updates == 0
    assert len(db.thread_lock_queries) == 1
    assert (
        "SELECT id, user_id, status, execution_lane, metadata"
        in (db.thread_lock_queries[0])
    )
    assert db.thread_lock_queries[0].endswith("FOR SHARE")


@pytest.mark.asyncio
async def test_repin_is_a_no_op_once_another_request_already_did_it() -> None:
    """AC17: concurrent readers produce exactly one revision bump."""

    record = _record(generation=NEW_GENERATION)
    db = _RepinDB(record, generation=NEW_GENERATION)
    service = CanvasService(db)

    async def verifier(locked, thread):
        del locked, thread
        return NEW_GENERATION

    mutation = await service.repin_workspace_generation(
        THREAD_ID, expected_source_version=record.source_version, verifier=verifier
    )

    assert mutation.changed is False
    assert db.updates == 0


@pytest.mark.asyncio
async def test_repin_declines_when_the_presentation_moved_underneath() -> None:
    """A stale caller must not repair somebody else's presentation."""

    record = _record()
    db = _RepinDB(record, generation=NEW_GENERATION)
    service = CanvasService(db)

    async def verifier(locked, thread):
        raise AssertionError("verifier must not run for a superseded version")

    mutation = await service.repin_workspace_generation(
        THREAD_ID,
        expected_source_version=_sha(b"# something else\n"),
        verifier=verifier,
    )

    assert mutation.changed is False
    assert db.updates == 0


@pytest.mark.asyncio
async def test_live_app_sources_are_never_repinned() -> None:
    """AC16: a port on a rebuilt workspace is a genuinely different thing."""

    source = WorkspaceAppSource(entry_port=8080, workspace_generation=GENERATION)
    record = CanvasRecord(
        thread_id=THREAD_ID,
        canvas_id="main",
        source=source,
        title="App",
        renderer="auto",
        editable=False,
        alt_text=None,
        presentation_revision=1,
        source_fingerprint=canonical_source_fingerprint(source),
        source_version=None,
        origin_generation=UUID("33333333-cccc-4ccc-8ccc-333333333333"),
        created_at=NOW,
        updated_at=NOW,
    )
    db = _RepinDB(record, generation=NEW_GENERATION)
    service = CanvasService(db)

    async def verifier(locked, thread):
        raise AssertionError("verifier must not run for a live-app source")

    mutation = await service.repin_workspace_generation(
        THREAD_ID, expected_source_version="sha256:" + "0" * 64, verifier=verifier
    )

    assert mutation.changed is False
    assert db.updates == 0


# ---------------------------------------------------------------------------
# Re-pin retry policy
# ---------------------------------------------------------------------------


class _RepinProbeService:
    """Records re-pin calls and reports what the verifier decided."""

    def __init__(self, record: CanvasRecord) -> None:
        self.record = record
        self.calls = 0
        self.verifier_results: list[Any] = []

    async def repin_workspace_generation(
        self, thread_id, *, expected_source_version, verifier
    ):
        from orchestrator.services.canvas import CanvasMutation

        del thread_id, expected_source_version
        self.calls += 1
        self.verifier_results.append(await verifier(self.record, _thread()))
        return CanvasMutation(changed=False, record=self.record)


class _VerifyGateway:
    """Stands in for the file gateway during re-pin verification."""

    def __init__(self, *, outcome: str) -> None:
        self.outcome = outcome
        self.reads = 0

    async def validate_for_presentation(
        self, thread, path, *, requested_renderer, alt_text
    ):
        del thread, requested_renderer, alt_text
        self.reads += 1
        if self.outcome == "unreadable":
            raise CanvasFileError(404, "canvas_file_not_found", "not restored yet")
        data = b"# Canvas\n" if self.outcome == "match" else b"# Changed\n"
        return NEW_GENERATION, _file(data, path=path)


async def _run_maybe_repin(monkeypatch, *, outcome: str, times: int):
    from orchestrator.routers import canvases

    canvases._REPIN_ATTEMPTED.clear()
    record = _record()
    service = _RepinProbeService(record)
    gateway = _VerifyGateway(outcome=outcome)
    monkeypatch.setattr(canvases, "_get_canvas_service", lambda db: service)
    monkeypatch.setattr(canvases, "_get_file_gateway", lambda db=None: gateway)

    for _ in range(times):
        await canvases._maybe_repin(
            object(), THREAD_ID, _thread(generation=NEW_GENERATION), record
        )
    return service, gateway


@pytest.mark.asyncio
async def test_a_workspace_still_restoring_is_retried(monkeypatch) -> None:
    """An unreadable file is transient: a restore in progress must not latch.

    The guard exists to stop a genuinely changed file from costing an SFTP
    round-trip on every state GET. Applying it to a workspace that simply has
    not finished restoring would strand the Canvas on its stored copy until the
    next publish or a process restart.
    """

    service, gateway = await _run_maybe_repin(
        monkeypatch, outcome="unreadable", times=3
    )

    assert service.calls == 3
    assert gateway.reads == 3


@pytest.mark.asyncio
async def test_a_definitively_changed_file_is_attempted_once(monkeypatch) -> None:
    """A settled mismatch stops re-reading the workspace on every GET."""

    service, gateway = await _run_maybe_repin(monkeypatch, outcome="mismatch", times=3)

    assert service.calls == 1
    assert gateway.reads == 1


@pytest.mark.asyncio
async def test_a_matching_file_is_verified_without_latching(monkeypatch) -> None:
    """A successful verification never records a suppressing attempt."""

    from orchestrator.routers import canvases

    service, _ = await _run_maybe_repin(monkeypatch, outcome="match", times=1)

    assert service.verifier_results == [NEW_GENERATION]
    assert not canvases._REPIN_ATTEMPTED


@pytest.mark.asyncio
async def test_repin_is_skipped_entirely_when_the_generation_still_matches(
    monkeypatch,
) -> None:
    """The staleness test is metadata-only: a healthy Canvas reads no files."""

    from orchestrator.routers import canvases

    canvases._REPIN_ATTEMPTED.clear()
    record = _record()
    service = _RepinProbeService(record)
    gateway = _VerifyGateway(outcome="match")
    monkeypatch.setattr(canvases, "_get_canvas_service", lambda db: service)
    monkeypatch.setattr(canvases, "_get_file_gateway", lambda db=None: gateway)

    result = await canvases._maybe_repin(
        object(), THREAD_ID, _thread(generation=GENERATION), record
    )

    assert result is record
    assert service.calls == 0
    assert gateway.reads == 0
