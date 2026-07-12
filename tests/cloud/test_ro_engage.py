"""Tests for the fail-closed RO engage gate (orchestrator/services/cloud/ro_engage.py).

Drives the gate with a fake SupportsRoReader backend, a fake httpx client the
probe hits (capabilities GET + PROPFIND read control + mutating verbs), and an
AsyncMock postgres. Covers: engage-on-ok (persist + no revoke), refuse-on-open-
write (revoke, no persist), and refuse-on-version-below-floor (revoke).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from orchestrator.services.cloud.base import CanaryFixture, RoReaderGrant
from orchestrator.services.cloud.errors import CloudBackendError, CloudBackendErrorKind
from orchestrator.services.cloud.ro_engage import RoEngageRefused, engage_ro_mount


class _Resp:
    def __init__(self, status, json_data=None):
        self.status_code = status
        self._json = json_data

    def json(self):
        return self._json


def _reader_client(
    *, all_rejected=True, read_control=207, nc_version="30.0.0", recorder=None
):
    major, minor, micro = (int(x) for x in nc_version.split("."))
    caps = {
        "ocs": {
            "data": {
                "version": {"major": major, "minor": minor, "micro": micro},
                "capabilities": {"groupfolders": "20.1.2"},
            }
        }
    }

    class _Client:
        async def request(self, method, url, **kw):
            if recorder is not None:
                recorder.append((method, url, kw))
            if method == "GET" and "capabilities" in url:
                return _Resp(200, caps)
            if method == "PROPFIND":  # positive read control (Depth:0)
                return _Resp(read_control)
            # Any other verb, including A2's uploads-finalize-session MKCOL:
            # rejecting it here (like every other write) makes that side
            # channel fall back to its synthetic transfer id — these
            # engage-level tests are about the dav_root/refs wiring, not
            # A2's own behavior (covered directly in test_ro_probe.py).
            return _Resp(403 if all_rejected else 201)

    return _Client()


class _FakeRoBackend:
    backend_id = "nextcloud"

    def __init__(self, *, baseline=None, baseline_error=None, reader_id=None):
        self.revoked: list[str] = []
        self.canary_removed = False
        self._baseline = {"a.txt": "e1"} if baseline is None else baseline
        self._baseline_error = baseline_error
        # Overridable so a test can mint a reader name the caller could NOT
        # re-derive from user_key — proving grant.reader_id is what flows.
        self._reader_id = reader_id

    async def ensure_ro_reader(self, *, user_key):
        return self._reader_id or f"srw-reader-{user_key}"

    async def mint_ro_grant(self, handle, *, user_key, grant_key):
        reader = self._reader_id or f"srw-reader-{user_key}"
        return RoReaderGrant(
            reader_id=reader,
            grant_handle=json.dumps({"group_id": "g1", "reader_id": reader}),
            webdav_url="https://nc/remote.php/dav/files/srw-reader-abc/Proj/",
            credentials="pw",
            auth_kind="basic",
        )

    async def revoke_ro_grant(self, grant_handle, *, user_key):
        self.revoked.append(grant_handle)

    async def seed_canary_fixture(self, handle):
        # A1/A3: a real canary carries real refs; the engage gate must pass
        # them through to the probe rather than leaving them stranded.
        return CanaryFixture(
            path=".srw-ro-canary/probe.txt",
            version_ref="99/1700000000",
            trash_ref="probe.txt.d1700000000",
        )

    async def remove_canary_fixture(self, handle, fixture):
        self.canary_removed = True

    async def capture_etag_baseline(self, handle):
        if self._baseline_error is not None:
            raise self._baseline_error
        return self._baseline


def _handle():
    return object()  # the gate treats the handle opaquely


@pytest.mark.asyncio
async def test_engage_persists_and_returns_grant_when_probe_ok():
    backend = _FakeRoBackend()
    recorded: list[tuple[str, str, dict]] = []
    probe_client = _reader_client(
        all_rejected=True, read_control=207, recorder=recorded
    )
    db = AsyncMock()
    db.create_ro_mount = AsyncMock(return_value="row-1")
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "staged_summary": None}
    )

    grant = await engage_ro_mount(
        backend=backend,
        handle=_handle(),
        user_key="abc",
        thread_id="t1",
        user_id="u1",
        postgres_db=db,
        http_client_factory=lambda creds, reader_id: probe_client,
    )

    assert grant.reader_id == "srw-reader-abc"
    db.create_ro_mount.assert_awaited_once()
    assert backend.revoked == []  # not revoked on success
    assert backend.canary_removed is True  # canary always cleaned up

    # A3: dav_root passed into the probe must be the true DAV root
    # (https://nc/remote.php/dav) derived from grant.webdav_url, NOT the
    # reader's files-namespace mount URL
    # (https://nc/remote.php/dav/files/srw-reader-abc/Proj/) — every
    # side-channel request the probe issued must hang directly off the
    # root, not under `files/<reader>/<mount>/`.
    side_channel_urls = [
        url
        for _method, url, _kw in recorded
        if any(marker in url for marker in ("/versions/", "/trashbin/", "/uploads/"))
    ]
    assert side_channel_urls, "expected side-channel requests to have been issued"
    for url in side_channel_urls:
        assert url.startswith("https://nc/remote.php/dav")
        assert "/files/srw-reader-abc/Proj/" not in url

    # A1: the canary's real refs (from _FakeRoBackend.seed_canary_fixture)
    # reached the versions-restore/trash-restore requests, not ro_probe's
    # synthetic placeholders.
    versions_move = next(
        url for method, url, _kw in recorded if method == "MOVE" and "/versions/" in url
    )
    assert versions_move.endswith("/versions/99/1700000000")
    trash_move = next(
        url for method, url, _kw in recorded if method == "MOVE" and "/trashbin/" in url
    )
    assert trash_move.endswith("/trash/probe.txt.d1700000000")


@pytest.mark.asyncio
async def test_engage_refuses_and_revokes_when_a_write_succeeds():
    backend = _FakeRoBackend()
    probe_client = _reader_client(all_rejected=False, read_control=207)  # PUT 201s
    db = AsyncMock()

    with pytest.raises(RoEngageRefused):
        await engage_ro_mount(
            backend=backend,
            handle=_handle(),
            user_key="abc",
            thread_id="t1",
            user_id="u1",
            postgres_db=db,
            http_client_factory=lambda creds, reader_id: probe_client,
        )

    db.create_ro_mount.assert_not_awaited()
    assert backend.revoked  # grant was rolled back
    assert backend.canary_removed is True
    # Pre-persist refusal: there is no row yet, so the row-aware rollback
    # must not fire a spurious mark_ro_mount_revoked.
    db.mark_ro_mount_revoked.assert_not_awaited()


@pytest.mark.asyncio
async def test_engage_refuses_when_read_control_fails():
    # A dead/expired credential 401s the positive read control; the gate must
    # refuse even though every mutating verb also "rejects".
    backend = _FakeRoBackend()
    probe_client = _reader_client(all_rejected=True, read_control=401)
    db = AsyncMock()

    with pytest.raises(RoEngageRefused):
        await engage_ro_mount(
            backend=backend,
            handle=_handle(),
            user_key="abc",
            thread_id="t1",
            user_id="u1",
            postgres_db=db,
            http_client_factory=lambda creds, reader_id: probe_client,
        )
    db.create_ro_mount.assert_not_awaited()
    assert backend.revoked


@pytest.mark.asyncio
async def test_engage_refuses_when_version_below_floor():
    backend = _FakeRoBackend()
    probe_client = _reader_client(
        all_rejected=True, read_control=207, nc_version="27.0.0"
    )
    db = AsyncMock()

    with pytest.raises(RoEngageRefused):
        await engage_ro_mount(
            backend=backend,
            handle=_handle(),
            user_key="abc",
            thread_id="t1",
            user_id="u1",
            postgres_db=db,
            http_client_factory=lambda creds, reader_id: probe_client,
        )
    db.create_ro_mount.assert_not_awaited()
    assert backend.revoked


@pytest.mark.asyncio
async def test_engage_captures_and_persists_etag_baseline():
    # design §3.4: without a baseline neither the staged-diff manifest nor
    # the apply conflict gate can classify writes, so a successful engage
    # must capture + persist one against the row it just created.
    backend = _FakeRoBackend(baseline={"a.txt": "e1"})
    probe_client = _reader_client(all_rejected=True, read_control=207)
    db = AsyncMock()
    db.create_ro_mount = AsyncMock(return_value="row-1")
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "staged_summary": None}
    )

    await engage_ro_mount(
        backend=backend,
        handle=_handle(),
        user_key="abc",
        thread_id="t1",
        user_id="u1",
        postgres_db=db,
        http_client_factory=lambda creds, reader_id: probe_client,
    )

    db.update_ro_mount_baseline.assert_awaited_once_with("row-1", {"a.txt": "e1"})


@pytest.mark.asyncio
async def test_engage_refuses_when_baseline_capture_fails():
    backend = _FakeRoBackend(
        baseline_error=CloudBackendError(
            CloudBackendErrorKind.UNAVAILABLE, "propfind failed", backend="nextcloud"
        )
    )
    probe_client = _reader_client(all_rejected=True, read_control=207)
    db = AsyncMock()
    db.create_ro_mount = AsyncMock(return_value="row-1")
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "staged_summary": None}
    )

    with pytest.raises(RoEngageRefused):
        await engage_ro_mount(
            backend=backend,
            handle=_handle(),
            user_key="abc",
            thread_id="t1",
            user_id="u1",
            postgres_db=db,
            http_client_factory=lambda creds, reader_id: probe_client,
        )

    # Fail-closed: the row was created but the grant must still be revoked
    # (no dangling reader access without a baseline to gate writes against),
    # and the baseline must never be persisted.
    assert backend.revoked
    db.update_ro_mount_baseline.assert_not_awaited()
    # ... and the already-persisted row must not survive as status='active'
    # with dead credentials and a NULL baseline — the rollback is row-aware.
    db.mark_ro_mount_revoked.assert_awaited_once_with("row-1")


@pytest.mark.asyncio
async def test_engage_refuses_when_baseline_persist_reports_inactive_row():
    # update_ro_mount_baseline returning False means the row is no longer
    # active (e.g. the Slice A reconciler revoked it mid-engage) — the
    # baseline did NOT persist, so engage must refuse with the same
    # rollback, never report success without a baseline on the row.
    backend = _FakeRoBackend()
    probe_client = _reader_client(all_rejected=True, read_control=207)
    db = AsyncMock()
    db.create_ro_mount = AsyncMock(return_value="row-1")
    db.update_ro_mount_baseline = AsyncMock(return_value=False)
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "staged_summary": None}
    )

    with pytest.raises(RoEngageRefused):
        await engage_ro_mount(
            backend=backend,
            handle=_handle(),
            user_key="abc",
            thread_id="t1",
            user_id="u1",
            postgres_db=db,
            http_client_factory=lambda creds, reader_id: probe_client,
        )

    assert backend.revoked
    db.mark_ro_mount_revoked.assert_awaited_once_with("row-1")


@pytest.mark.asyncio
async def test_reengage_preserves_baseline_under_live_staging():
    # Post-review hardening: a resume re-engage on a thread whose row
    # already carries a live staging (staged_summary not None) must NOT
    # recapture the baseline — the staged diff classifies its entries
    # against the EXISTING baseline, and a fresh capture here would
    # silently absorb whatever changed on the cloud since staging into
    # "the baseline", bypassing the apply conflict gate for those changes.
    backend = _FakeRoBackend(baseline={"a.txt": "fresh-etag-should-not-be-used"})
    probe_client = _reader_client(all_rejected=True, read_control=207)
    db = AsyncMock()
    db.create_ro_mount = AsyncMock(return_value="row-1")
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={
            "id": "row-1",
            "staged_summary": {"counts": {"added": 1}, "signature": "sig"},
        }
    )

    grant = await engage_ro_mount(
        backend=backend,
        handle=_handle(),
        user_key="abc",
        thread_id="t1",
        user_id="u1",
        postgres_db=db,
        http_client_factory=lambda creds, reader_id: probe_client,
    )

    # Engage still succeeds and returns the grant...
    assert grant.reader_id == "srw-reader-abc"
    db.create_ro_mount.assert_awaited_once()
    # ...but the baseline is neither captured nor persisted.
    db.update_ro_mount_baseline.assert_not_awaited()
    assert backend.revoked == []  # not a refusal — a normal, successful skip


@pytest.mark.asyncio
async def test_engage_recaptures_baseline_when_no_live_staging():
    # Sanity counterpart: a row with staged_summary=None (first-time engage,
    # or after a restage-clear/apply/reject) must still recapture normally —
    # the skip guard must not become an unconditional skip.
    backend = _FakeRoBackend(baseline={"a.txt": "e1"})
    probe_client = _reader_client(all_rejected=True, read_control=207)
    db = AsyncMock()
    db.create_ro_mount = AsyncMock(return_value="row-1")
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "staged_summary": None}
    )

    await engage_ro_mount(
        backend=backend,
        handle=_handle(),
        user_key="abc",
        thread_id="t1",
        user_id="u1",
        postgres_db=db,
        http_client_factory=lambda creds, reader_id: probe_client,
    )

    db.update_ro_mount_baseline.assert_awaited_once_with("row-1", {"a.txt": "e1"})


@pytest.mark.asyncio
async def test_engage_passes_reader_id_to_client_factory():
    # The fake mints a reader name that CANNOT be re-derived from
    # user_key/user_id ("srw-reader-u1" would collapse the two) — only
    # grant.reader_id itself can reach the factory with this value.
    backend = _FakeRoBackend(reader_id="reader-xyz-distinct")
    probe_client = _reader_client(all_rejected=True, read_control=207)
    recorder: list[tuple[str | None, str]] = []

    def factory(credentials, reader_id):
        recorder.append((credentials, reader_id))
        return probe_client

    db = AsyncMock()
    db.create_ro_mount = AsyncMock(return_value="row-1")
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "staged_summary": None}
    )

    grant = await engage_ro_mount(
        backend=backend,
        handle=_handle(),
        user_key="u1",
        thread_id="t1",
        user_id="u1",
        postgres_db=db,
        http_client_factory=factory,
    )

    assert grant.reader_id == "reader-xyz-distinct"
    assert recorder == [(grant.credentials, "reader-xyz-distinct")]
