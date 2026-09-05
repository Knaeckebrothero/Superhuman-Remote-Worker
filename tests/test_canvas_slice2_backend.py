"""Dynamic Canvas Slice 2 conditional editing and concurrency contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import stat
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from orchestrator.services.canvas import (
    CanvasEditError,
    CanvasRecord,
    CanvasService,
    WorkspaceFileSource,
    build_public_canvas_representation,
    canonical_source_fingerprint,
    canvas_file_lock_key,
)
from orchestrator.services.canvas_files import (
    CanvasFileError,
    RawWorkspaceFile,
    ThreadWorkspaceFileGateway,
    _write_sftp_workspace_file,
)

THREAD_ID = "a3333333-3333-3333-3333-333333333333"
USER_ID = "b4444444-4444-4444-8444-444444444444"
GENERATION = UUID("11111111-aaaa-4aaa-8aaa-111111111111")
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
OLD_BYTES = b"# Original\n"
OLD_VERSION = "sha256:" + hashlib.sha256(OLD_BYTES).hexdigest()


def test_canvas_mutation_etags_are_exposed_to_cross_origin_cockpit() -> None:
    """The :4200 dev Cockpit must be able to read both save validators."""

    from fastapi.middleware.cors import CORSMiddleware
    from orchestrator.main import app

    cors = next(
        middleware
        for middleware in app.user_middleware
        if middleware.cls is CORSMiddleware
    )
    assert set(cors.kwargs["expose_headers"]) >= {
        "ETag",
        "X-Canvas-Content-ETag",
        "X-Canvas-Mutation-Changed",
    }


class _Magic:
    @staticmethod
    def from_buffer(data: bytes, mime: bool = False) -> str:
        del data, mime
        return "text/plain"


def _thread() -> dict[str, Any]:
    return {
        "id": THREAD_ID,
        "user_id": USER_ID,
        "metadata": {
            "config_override": {"workspace": {"backend": "sandbox"}},
            "_workspace_binding": {
                "generation": str(GENERATION),
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
                "_canvas_workspace_generation": str(GENERATION),
                "_runtime_incarnation": "22222222-bbbb-4bbb-8bbb-222222222222",
            },
        },
    }


def _record(*, revision: int = 1, source_version: str = OLD_VERSION) -> CanvasRecord:
    source = WorkspaceFileSource(
        path="output/report.md", workspace_generation=GENERATION
    )
    return CanvasRecord(
        thread_id=THREAD_ID,
        canvas_id="main",
        source=source,
        title="Report",
        renderer="markdown",
        editable=True,
        alt_text=None,
        presentation_revision=revision,
        source_fingerprint=canonical_source_fingerprint(source),
        source_version=source_version,
        origin_generation=None,
        created_at=NOW,
        updated_at=NOW,
    )


class _EditDB:
    """Asyncpg-shaped store with connection-scoped advisory lock ownership."""

    def __init__(self) -> None:
        record = _record()
        self.row = {
            **{
                name: getattr(record, name)
                for name in CanvasRecord.__dataclass_fields__
            },
            "source": json.dumps(record.source.model_dump(mode="json")),
        }
        self.thread = _thread()
        self.row_lock = asyncio.Lock()
        self.advisory_owner: _EditConnection | None = None
        self.acquire_count = 0
        self.now = NOW
        self.thread_lock_queries: list[str] = []

    @asynccontextmanager
    async def acquire(self):
        self.acquire_count += 1
        connection = _EditConnection(self)
        try:
            yield connection
        finally:
            assert self.advisory_owner is not connection


class _EditConnection:
    def __init__(self, db: _EditDB) -> None:
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        async with self.db.row_lock:
            yield

    async def fetchval(self, query: str, key: int) -> bool:
        del key
        if "pg_try_advisory_lock" in query:
            if self.db.advisory_owner in {None, self}:
                self.db.advisory_owner = self
                return True
            return False
        if "pg_advisory_unlock" in query:
            if self.db.advisory_owner is self:
                self.db.advisory_owner = None
                return True
            return False
        raise AssertionError(query)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        normalized = " ".join(query.split())
        if "FROM threads" in normalized:
            self.db.thread_lock_queries.append(normalized)
            return dict(self.db.thread)
        if normalized.startswith("SELECT thread_id"):
            return dict(self.db.row)
        if normalized.startswith("UPDATE canvases SET source_version"):
            self.db.now += timedelta(microseconds=1)
            self.db.row.update(
                source_version=args[2],
                presentation_revision=self.db.row["presentation_revision"] + 1,
                updated_at=self.db.now,
            )
            return dict(self.db.row)
        raise AssertionError(normalized)


@pytest.mark.asyncio
async def test_gateway_rechecks_hash_then_writes_and_validates_readback(
    monkeypatch,
) -> None:
    from orchestrator.services import canvas_files

    monkeypatch.setattr(canvas_files, "magic", _Magic)
    storage = {"output/report.md": OLD_BYTES}
    writes: list[bytes] = []

    async def load(thread, path, generation):
        del thread
        assert generation == GENERATION
        return RawWorkspaceFile(storage[path], NOW)

    async def write(thread, path, generation, data):
        del thread
        assert generation == GENERATION
        writes.append(data)
        storage[path] = data

    gateway = ThreadWorkspaceFileGateway(
        remote_loader=load,
        remote_writer=write,
    )
    candidate = b"# User edit\n"
    saved = await gateway.replace_current(
        _thread(),
        _record(),
        candidate,
        expected_source_version=OLD_VERSION,
    )

    assert writes == [candidate]
    assert storage["output/report.md"] == candidate
    assert saved.data == candidate
    assert saved.source_version == "sha256:" + hashlib.sha256(candidate).hexdigest()

    storage["output/report.md"] = b"# External writer won\n"
    with pytest.raises(CanvasFileError) as stale:
        await gateway.replace_current(
            _thread(),
            _record(),
            b"# Must not land\n",
            expected_source_version=OLD_VERSION,
        )
    assert stale.value.status_code == 412
    assert stale.value.code == "canvas_content_precondition_failed"
    assert writes == [candidate]

    with pytest.raises(CanvasFileError, match="UTF-8"):
        await gateway.validate_edit_candidate(_record(), b"\xff")


@pytest.mark.asyncio
async def test_office_renderer_enters_gateway_and_existing_coordinator(
    monkeypatch,
) -> None:
    from orchestrator.services import canvas_files

    class _OfficeMagic:
        @staticmethod
        def from_buffer(data: bytes, mime: bool = False) -> str:
            del data, mime
            return "application/zip"

    monkeypatch.setattr(canvas_files, "magic", _OfficeMagic)
    gateway = ThreadWorkspaceFileGateway(
        remote_writer=lambda *args: asyncio.sleep(0),
    )
    source = WorkspaceFileSource(
        path="output/report.docx",
        workspace_generation=GENERATION,
    )
    office = replace(
        _record(),
        source=source,
        source_fingerprint=canonical_source_fingerprint(source),
        renderer="office",
        editable=True,
    )

    assert gateway.supports_editing(_thread(), office) is True
    candidate = await gateway.validate_edit_candidate(office, b"PK\x03\x04office")
    assert candidate.renderer == "office"

    db = _EditDB()
    db.row.update(renderer="office", editable=True)
    writer_called = False

    async def writer(record, thread):
        nonlocal writer_called
        del record, thread
        writer_called = True
        return "sha256:" + "f" * 64

    fingerprint = _record().source_fingerprint
    assert fingerprint is not None
    mutation = await CanvasService(db).edit_file(
        THREAD_ID,
        expected_presentation_revision=1,
        expected_source_fingerprint=fingerprint,
        expected_source_version=OLD_VERSION,
        expected_thread_user_id=USER_ID,
        writer=writer,
    )
    assert mutation.record is not None
    assert mutation.record.presentation_revision == 2
    assert writer_called is True


@pytest.mark.asyncio
async def test_edit_coordinator_serializes_and_revalidates_after_lock() -> None:
    db = _EditDB()
    service = CanvasService(db)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_writer_called = False
    next_version = "sha256:" + "b" * 64

    async def first_writer(record: CanvasRecord, thread: dict[str, Any]) -> str:
        assert record.presentation_revision == 1
        assert thread["user_id"] == USER_ID
        # The initial state read and dedicated coordinator connection are the
        # only acquisitions. The writer consumes the transaction-locked thread
        # snapshot instead of opening a nested connection under the row lock.
        assert db.acquire_count == 2
        first_started.set()
        await release_first.wait()
        return next_version

    async def second_writer(record: CanvasRecord, thread: dict[str, Any]) -> str:
        nonlocal second_writer_called
        del record, thread
        second_writer_called = True
        return "sha256:" + "c" * 64

    fingerprint = _record().source_fingerprint
    assert fingerprint is not None
    first = asyncio.create_task(
        service.edit_file(
            THREAD_ID,
            expected_presentation_revision=1,
            expected_source_fingerprint=fingerprint,
            expected_source_version=OLD_VERSION,
            expected_thread_user_id=USER_ID,
            writer=first_writer,
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = asyncio.create_task(
        service.edit_file(
            THREAD_ID,
            expected_presentation_revision=1,
            expected_source_fingerprint=fingerprint,
            expected_source_version=OLD_VERSION,
            expected_thread_user_id=USER_ID,
            writer=second_writer,
            lock_timeout=1,
        )
    )
    await asyncio.sleep(0.06)
    assert not second.done()

    release_first.set()
    completed = await first
    assert completed.record is not None
    assert completed.record.presentation_revision == 2
    with pytest.raises(CanvasEditError) as conflict:
        await second
    assert conflict.value.status_code == 409
    assert conflict.value.code == "canvas_presentation_changed"
    assert second_writer_called is False
    assert db.advisory_owner is None


@pytest.mark.asyncio
async def test_edit_cancellation_releases_session_lock_and_capacity() -> None:
    db = _EditDB()
    service = CanvasService(db)
    started = asyncio.Event()
    never = asyncio.Event()
    fingerprint = _record().source_fingerprint
    assert fingerprint is not None

    async def blocked(record: CanvasRecord, thread: dict[str, Any]) -> str:
        del record, thread
        started.set()
        await never.wait()
        return "sha256:" + "d" * 64

    editing = asyncio.create_task(
        service.edit_file(
            THREAD_ID,
            expected_presentation_revision=1,
            expected_source_fingerprint=fingerprint,
            expected_source_version=OLD_VERSION,
            expected_thread_user_id=USER_ID,
            writer=blocked,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    editing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await editing
    assert db.advisory_owner is None

    completed = await service.edit_file(
        THREAD_ID,
        expected_presentation_revision=1,
        expected_source_fingerprint=fingerprint,
        expected_source_version=OLD_VERSION,
        expected_thread_user_id=USER_ID,
        writer=lambda record, thread: asyncio.sleep(0, result="sha256:" + "e" * 64),
    )
    assert completed.record is not None
    assert completed.record.presentation_revision == 2


@pytest.mark.asyncio
async def test_edit_admission_is_bounded_before_dedicated_connection(
    monkeypatch,
) -> None:
    from orchestrator.services import canvas as canvas_service

    db = _EditDB()
    service = CanvasService(db)
    saturated = asyncio.Semaphore(1)
    await saturated.acquire()
    monkeypatch.setattr(canvas_service, "_EDIT_COORDINATOR_SEMAPHORE", saturated)
    monkeypatch.setattr(canvas_service, "_EDIT_COORDINATOR_QUEUE_TIMEOUT", 0.01)
    record = _record()
    assert record.source_fingerprint is not None
    try:
        with pytest.raises(CanvasEditError) as error:
            await service.edit_file(
                THREAD_ID,
                expected_presentation_revision=1,
                expected_source_fingerprint=record.source_fingerprint,
                expected_source_version=OLD_VERSION,
                expected_thread_user_id=USER_ID,
                writer=lambda current, thread: asyncio.sleep(0, result=OLD_VERSION),
            )
    finally:
        saturated.release()
    assert error.value.status_code == 503
    assert error.value.code == "canvas_edit_capacity_exhausted"
    # One connection was used for the cheap initial state read. No dedicated
    # coordinator connection was acquired while admission was saturated.
    assert db.acquire_count == 1


@pytest.mark.asyncio
async def test_refresh_admission_is_bounded_before_connection(monkeypatch) -> None:
    from orchestrator.services import canvas as canvas_service

    db = _EditDB()
    service = CanvasService(db)
    saturated = asyncio.Semaphore(1)
    await saturated.acquire()
    monkeypatch.setattr(canvas_service, "_EDIT_COORDINATOR_SEMAPHORE", saturated)
    monkeypatch.setattr(canvas_service, "_EDIT_COORDINATOR_QUEUE_TIMEOUT", 0.01)
    try:
        with pytest.raises(CanvasEditError) as error:
            await service.refresh_file(
                THREAD_ID,
                expected_etag=build_public_canvas_representation(_record()).etag,
                expected_thread_user_id=USER_ID,
                refresher=lambda current, thread: asyncio.sleep(0, result=OLD_VERSION),
            )
    finally:
        saturated.release()
    assert error.value.status_code == 503
    assert error.value.code == "canvas_edit_capacity_exhausted"
    assert db.acquire_count == 0


@pytest.mark.asyncio
async def test_refresh_uses_transaction_locked_thread_snapshot() -> None:
    db = _EditDB()
    service = CanvasService(db)
    next_version = "sha256:" + "f" * 64

    async def refresh(current: CanvasRecord, thread: dict[str, Any]) -> str:
        assert current.presentation_revision == 1
        assert thread == {
            "id": THREAD_ID,
            "user_id": USER_ID,
            "metadata": db.thread["metadata"],
        }
        assert db.acquire_count == 1
        return next_version

    mutation = await service.refresh_file(
        THREAD_ID,
        expected_etag=build_public_canvas_representation(_record()).etag,
        expected_thread_user_id=USER_ID,
        refresher=refresh,
    )

    assert mutation.record is not None
    assert mutation.record.presentation_revision == 2
    assert mutation.record.source_version == next_version


def _mark_stateless_canvas_thread(db: _EditDB, lifecycle: str) -> None:
    db.thread["execution_lane"] = "stateless"
    db.thread["status"] = "active"
    metadata = dict(db.thread["metadata"])
    if lifecycle == "ended":
        db.thread["status"] = "ended"
    elif lifecycle == "suspended":
        db.thread["status"] = "suspended"
    elif lifecycle == "workspace-retirement":
        metadata["_stateless_workspace_retirement_pending"] = True
    elif lifecycle == "claim-retirement":
        metadata["_stateless_claim_retirement"] = {"terminal_token": 8}
    elif lifecycle == "claim-loss":
        metadata["_stateless_claim_losses"] = {
            "7": {"pod": "agent-a", "pod_uid": "uid-a", "quiesced": False}
        }
    elif lifecycle == "claim-loss-hold":
        metadata["_stateless_claim_loss_hold"] = {"lease_token": 8}
    elif lifecycle == "protected-cloud":
        metadata["protected_cloud"] = True
    else:  # pragma: no cover - closed test vocabulary
        raise AssertionError(lifecycle)
    db.thread["metadata"] = metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["edit", "refresh"])
@pytest.mark.parametrize(
    "lifecycle",
    [
        "ended",
        "suspended",
        "workspace-retirement",
        "claim-retirement",
        "claim-loss",
        "claim-loss-hold",
        "protected-cloud",
    ],
)
async def test_stateless_lifecycle_marker_precedes_canvas_workspace_writer(
    operation: str,
    lifecycle: str,
) -> None:
    """A marker holding the thread row wins before any remote callback."""

    db = _EditDB()
    _mark_stateless_canvas_thread(db, lifecycle)
    service = CanvasService(db)
    writer_called = False

    async def forbidden_writer(*_args) -> str:
        nonlocal writer_called
        writer_called = True
        raise AssertionError("stateless terminal state reached the workspace writer")

    record = _record()
    assert record.source_fingerprint is not None
    with pytest.raises(CanvasEditError) as error:
        if operation == "edit":
            await service.edit_file(
                THREAD_ID,
                expected_presentation_revision=record.presentation_revision,
                expected_source_fingerprint=record.source_fingerprint,
                expected_source_version=record.source_version,
                expected_thread_user_id=USER_ID,
                writer=forbidden_writer,
            )
        else:
            await service.refresh_file(
                THREAD_ID,
                expected_etag=build_public_canvas_representation(record).etag,
                expected_thread_user_id=USER_ID,
                refresher=forbidden_writer,
            )

    assert error.value.status_code == 409
    assert error.value.code == "canvas_editing_unavailable"
    assert writer_called is False
    assert db.row["presentation_revision"] == 1
    assert len(db.thread_lock_queries) == 1
    lock_sql = db.thread_lock_queries[0]
    assert "SELECT id, user_id, status, execution_lane, metadata" in lock_sql
    assert lock_sql.endswith("FOR SHARE")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker",
    [
        "_stateless_workspace_retirement_pending",
        "_stateless_claim_retirement",
        "_stateless_claim_loss_hold",
        "_stateless_claim_losses",
    ],
    ids=["workspace-retirement", "claim-retirement", "loss-hold", "losses"],
)
@pytest.mark.parametrize(
    "marker_value",
    [None, False, 0, "", [], {}],
    ids=["null", "false", "zero", "empty-string", "empty-list", "empty-map"],
)
async def test_present_falsey_lifecycle_marker_blocks_canvas_writer(
    marker, marker_value
) -> None:
    db = _EditDB()
    db.thread.update(execution_lane="stateless", status="active")
    db.thread["metadata"] = {
        **db.thread["metadata"],
        marker: marker_value,
    }
    writer_called = False

    async def forbidden_writer(*_args) -> str:
        nonlocal writer_called
        writer_called = True
        raise AssertionError("malformed lifecycle marker reached Canvas writer")

    record = _record()
    assert record.source_fingerprint is not None
    with pytest.raises(CanvasEditError) as error:
        await CanvasService(db).edit_file(
            THREAD_ID,
            expected_presentation_revision=record.presentation_revision,
            expected_source_fingerprint=record.source_fingerprint,
            expected_source_version=record.source_version,
            expected_thread_user_id=USER_ID,
            writer=forbidden_writer,
        )

    assert error.value.status_code == 409
    assert error.value.code == "canvas_editing_unavailable"
    assert writer_called is False
    assert db.row["presentation_revision"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protected_cloud",
    [None, True, 0, "", [], {}],
    ids=["null", "true", "zero", "empty-string", "empty-list", "empty-map"],
)
async def test_present_nonfalse_protected_cloud_blocks_canvas_writer(
    protected_cloud,
) -> None:
    db = _EditDB()
    db.thread.update(execution_lane="stateless", status="active")
    db.thread["metadata"] = {
        **db.thread["metadata"],
        "protected_cloud": protected_cloud,
    }
    writer_called = False

    async def forbidden_writer(*_args) -> str:
        nonlocal writer_called
        writer_called = True
        raise AssertionError("protected stateless thread reached Canvas writer")

    record = _record()
    assert record.source_fingerprint is not None
    with pytest.raises(CanvasEditError) as error:
        await CanvasService(db).edit_file(
            THREAD_ID,
            expected_presentation_revision=record.presentation_revision,
            expected_source_fingerprint=record.source_fingerprint,
            expected_source_version=record.source_version,
            expected_thread_user_id=USER_ID,
            writer=forbidden_writer,
        )

    assert error.value.status_code == 409
    assert error.value.code == "canvas_editing_unavailable"
    assert writer_called is False
    assert db.row["presentation_revision"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_class",
    ["vm", "unknown", "officer", "conference"],
)
async def test_pinned_only_session_or_workspace_class_blocks_canvas_writer(
    session_class,
) -> None:
    db = _EditDB()
    db.thread.update(execution_lane="stateless", status="active")
    if session_class == "vm":
        db.thread["metadata"]["vm"] = {"status": "ready", "host": "vm.test"}
    elif session_class == "unknown":
        db.thread["metadata"]["config_override"]["workspace"]["backend"] = "future-tier"
    else:
        db.thread["metadata"]["config_override"]["officer"] = {
            "enabled": session_class == "officer",
            "conference": session_class == "conference",
        }

    writer_called = False

    async def forbidden_writer(*_args) -> str:
        nonlocal writer_called
        writer_called = True
        raise AssertionError("pinned-only session class reached Canvas writer")

    record = _record()
    assert record.source_fingerprint is not None
    with pytest.raises(CanvasEditError) as error:
        await CanvasService(db).edit_file(
            THREAD_ID,
            expected_presentation_revision=record.presentation_revision,
            expected_source_fingerprint=record.source_fingerprint,
            expected_source_version=record.source_version,
            expected_thread_user_id=USER_ID,
            writer=forbidden_writer,
        )

    assert error.value.status_code == 409
    assert error.value.code == "canvas_editing_unavailable"
    assert writer_called is False
    assert db.row["presentation_revision"] == 1


@pytest.mark.asyncio
async def test_pinned_terminal_markers_do_not_change_canvas_writer_semantics() -> None:
    """The new lifecycle policy is scoped strictly to the stateless lane."""

    db = _EditDB()
    db.thread.update(execution_lane="pinned", status="ended")
    db.thread["metadata"] = {
        **db.thread["metadata"],
        "_stateless_workspace_retirement_pending": True,
        "_stateless_claim_losses": {"7": {"malformed": True}},
        "protected_cloud": True,
    }
    called = False

    async def writer(*_args) -> str:
        nonlocal called
        called = True
        return "sha256:" + "f" * 64

    record = _record()
    assert record.source_fingerprint is not None
    mutation = await CanvasService(db).edit_file(
        THREAD_ID,
        expected_presentation_revision=record.presentation_revision,
        expected_source_fingerprint=record.source_fingerprint,
        expected_source_version=record.source_version,
        expected_thread_user_id=USER_ID,
        writer=writer,
    )

    assert called is True
    assert mutation.record is not None
    assert mutation.record.presentation_revision == 2


@pytest.mark.asyncio
async def test_canvas_writer_first_serializes_stateless_retirement_marker() -> None:
    """End's thread update waits while a previously admitted writer holds SHARE."""

    db = _EditDB()
    db.thread.update(execution_lane="stateless", status="active")
    service = CanvasService(db)
    writer_started = asyncio.Event()
    release_writer = asyncio.Event()
    marker_acquired = asyncio.Event()
    events: list[str] = []

    async def writer(*_args) -> str:
        events.append("writer-start")
        writer_started.set()
        await release_writer.wait()
        events.append("writer-finish")
        return "sha256:" + "e" * 64

    async def publish_end_marker() -> None:
        events.append("marker-wait")
        # ``row_lock`` models the conflicting threads FOR UPDATE taken by End.
        async with db.row_lock:
            events.append("marker-acquired")
            db.thread["status"] = "ended"
            marker_acquired.set()

    record = _record()
    assert record.source_fingerprint is not None
    editing = asyncio.create_task(
        service.edit_file(
            THREAD_ID,
            expected_presentation_revision=record.presentation_revision,
            expected_source_fingerprint=record.source_fingerprint,
            expected_source_version=record.source_version,
            expected_thread_user_id=USER_ID,
            writer=writer,
        )
    )
    await asyncio.wait_for(writer_started.wait(), timeout=1)
    marker = asyncio.create_task(publish_end_marker())
    await asyncio.sleep(0)
    assert marker_acquired.is_set() is False

    release_writer.set()
    mutation = await editing
    await marker

    assert mutation.record is not None
    assert mutation.record.presentation_revision == 2
    assert events == [
        "writer-start",
        "marker-wait",
        "writer-finish",
        "marker-acquired",
    ]
    assert len(db.thread_lock_queries) == 1
    assert db.thread_lock_queries[0].endswith("FOR SHARE")


def test_advisory_lock_key_is_stable_signed_and_path_scoped() -> None:
    first = canvas_file_lock_key(THREAD_ID, "output/report.md")
    assert first == canvas_file_lock_key(THREAD_ID, "output/report.md")
    assert -(2**63) <= first < 2**63
    assert first != canvas_file_lock_key(THREAD_ID, "output/other.md")


@pytest.mark.asyncio
async def test_sftp_temp_replace_preserves_mode_and_cleans_failed_temp() -> None:
    destination = "/home/agent-host/workspace/output/report.md"

    class _RemoteFile:
        def __init__(self, *, reading: bool):
            self.reading = reading

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def read(self, limit):
            assert self.reading and limit > len(OLD_BYTES)
            return OLD_BYTES

        async def write(self, data):
            assert not self.reading and data == b"# saved\n"
            return len(data)

    class _SFTP:
        def __init__(self):
            self.temporary: str | None = None
            self.chmod_mode: int | None = None
            self.removed: list[str] = []

        async def lstat(self, path):
            is_file = path == destination
            mode = (stat.S_IFREG | 0o755) if is_file else (stat.S_IFDIR | 0o700)
            return SimpleNamespace(
                permissions=mode,
                size=len(OLD_BYTES) if is_file else None,
                mtime=None,
            )

        async def realpath(self, path):
            return posixpath.normpath(path)

        def open(self, path, mode):
            if mode == "rb":
                assert path == destination
                return _RemoteFile(reading=True)
            assert mode == "xb"
            self.temporary = path
            return _RemoteFile(reading=False)

        async def chmod(self, path, mode):
            assert path == self.temporary
            self.chmod_mode = mode

        async def posix_rename(self, old, new):
            assert old == self.temporary and new == destination
            raise OSError("rename failed")

        async def remove(self, path):
            self.removed.append(path)

    sftp = _SFTP()
    with pytest.raises(OSError, match="rename failed"):
        await _write_sftp_workspace_file(
            sftp,
            root="/home/agent-host/workspace",
            path="output/report.md",
            data=b"# saved\n",
        )
    assert sftp.chmod_mode == 0o755
    assert sftp.removed == [sftp.temporary]
