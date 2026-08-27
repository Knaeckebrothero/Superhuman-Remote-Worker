"""Tests for the fail-closed RO engage gate (orchestrator/services/cloud/ro_engage.py).

Drives the gate with a fake SupportsRoReader backend, a fake httpx client the
probe hits (capabilities GET + PROPFIND read control + mutating verbs), and an
AsyncMock postgres. Covers: engage-on-ok (persist + no revoke), refuse-on-open-
write (revoke, no persist), and refuse-on-version-below-floor (revoke).
"""

from __future__ import annotations

import asyncio
from unittest.mock import DEFAULT, AsyncMock

import httpx
import pytest

from orchestrator.services.cloud.base import CanaryFixture, RoReaderGrant
from orchestrator.services.cloud.errors import CloudBackendError, CloudBackendErrorKind
from orchestrator.services.cloud.ro_engage import (
    RoEngageRefused,
    engage_ro_mount as _engage_ro_mount,
)
from orchestrator.services.cloud.protected_reader_authority import (
    ProtectedNextcloudReaderGrantPlan,
)
from orchestrator.services.cloud_staging.source_identity import (
    ProtectedMountSourceIdentity,
)


_RUNTIME_GENERATION = "11111111-1111-4111-8111-111111111111"
_ENGAGE_ATTEMPT = "22222222-2222-4222-8222-222222222222"
_BACKEND_INSTANCE = "33333333-3333-4333-8333-333333333333"
_PROJECT = "44444444-4444-4444-8444-444444444444"
_MOUNT = "55555555-5555-4555-8555-555555555555"
_PLAN = ProtectedNextcloudReaderGrantPlan(
    engage_attempt=_ENGAGE_ATTEMPT,
    backend_instance_id=_BACKEND_INSTANCE,
    source=ProtectedMountSourceIdentity(
        backend_instance_id=_BACKEND_INSTANCE,
        source_ref=_PROJECT,
        target_path="cloud",
        native_id="7",
        mountpoint="Project",
    ),
)


async def _dispatch_effect(_method: str, _path: str, _body: bytes):
    return httpx.Response(
        200,
        json={"ocs": {"meta": {"status": "ok", "statuscode": 100}}},
    )


async def engage_ro_mount(**kwargs):
    """Keep every unit case on the strict generation/attempt contract."""

    db = kwargs["postgres_db"]
    if db.install_ro_mount_engage_intent._mock_return_value is DEFAULT:
        db.install_ro_mount_engage_intent.return_value = {"id": "row-1"}
    if db.activate_ro_mount_attempt_with_baseline._mock_return_value is DEFAULT:
        db.activate_ro_mount_attempt_with_baseline.return_value = True
    db.begin_ro_mount_revocation_if_matches = AsyncMock(return_value=True)
    db.finish_ro_mount_revocation_if_matches = AsyncMock(return_value=True)
    kwargs.pop("handle", None)
    kwargs.pop("user_key", None)
    return await _engage_ro_mount(
        admission_check=kwargs.pop("admission_check", AsyncMock(return_value=True)),
        expected_runtime_generation=_RUNTIME_GENERATION,
        plan=_PLAN,
        credentials="pw",
        selected_mount_id=_MOUNT,
        effect_dispatcher=_dispatch_effect,
        **kwargs,
    )


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
    backend_instance_id = _BACKEND_INSTANCE

    def __init__(self, *, baseline=None, baseline_error=None):
        self.revoked: list[str] = []
        self.canary_removed = False
        self._baseline = {"a.txt": "e1"} if baseline is None else baseline
        self._baseline_error = baseline_error

    def build_protected_reader_grant(self, plan, *, credentials):
        return RoReaderGrant(
            reader_id=plan.reader_id,
            grant_handle=plan.grant_handle,
            webdav_url=f"https://nc/remote.php/dav/files/{plan.reader_id}/Proj/",
            credentials=credentials,
            auth_kind="basic",
        )

    async def grant_protected_reader_attempt(
        self, plan, *, credentials, dispatch_effect
    ):
        return self.build_protected_reader_grant(plan, credentials=credentials)

    async def revoke_protected_reader_attempt(self, plan):
        self.revoked.append(plan.grant_handle)

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
    return _PLAN.to_project_folder_handle()


@pytest.mark.asyncio
async def test_engage_persists_and_returns_grant_when_probe_ok():
    backend = _FakeRoBackend()
    recorded: list[tuple[str, str, dict]] = []
    probe_client = _reader_client(
        all_rejected=True, read_control=207, recorder=recorded
    )
    db = AsyncMock()
    db.install_ro_mount_engage_intent = AsyncMock(return_value={"id": "row-1"})
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "staged_summary": None}
    )
    db.activate_ro_mount_attempt_with_baseline = AsyncMock(return_value=True)

    grant = await engage_ro_mount(
        backend=backend,
        handle=_handle(),
        user_key="abc",
        thread_id="t1",
        user_id="u1",
        postgres_db=db,
        http_client_factory=lambda creds, reader_id: probe_client,
    )

    assert grant.reader_id == _PLAN.reader_id
    db.install_ro_mount_engage_intent.assert_awaited_once()
    db.activate_ro_mount_attempt_with_baseline.assert_awaited_once()
    assert backend.revoked == []  # not revoked on success
    assert backend.canary_removed is True  # canary always cleaned up

    # A3: dav_root passed into the probe must be the true DAV root
    # (https://nc/remote.php/dav) derived from grant.webdav_url, NOT the
    # reader's files-namespace mount URL
    # (https://nc/remote.php/dav/files/<attempt-reader>/Proj/) — every
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
        assert f"/files/{_PLAN.reader_id}/Proj/" not in url

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

    db.install_ro_mount_engage_intent.assert_awaited_once()
    assert backend.revoked  # grant was rolled back
    assert backend.canary_removed is True
    db.begin_ro_mount_revocation_if_matches.assert_awaited_once()


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
    db.install_ro_mount_engage_intent.assert_awaited_once()
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
    db.install_ro_mount_engage_intent.assert_awaited_once()
    assert backend.revoked


@pytest.mark.asyncio
async def test_engage_captures_and_persists_etag_baseline():
    # design §3.4: without a baseline neither the staged-diff manifest nor
    # the apply conflict gate can classify writes, so a successful engage
    # must capture + persist one against the row it just created.
    backend = _FakeRoBackend(baseline={"a.txt": "e1"})
    probe_client = _reader_client(all_rejected=True, read_control=207)
    db = AsyncMock()
    db.install_ro_mount_engage_intent = AsyncMock(return_value={"id": "row-1"})
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "staged_summary": None}
    )
    db.activate_ro_mount_attempt_with_baseline = AsyncMock(return_value=True)

    await engage_ro_mount(
        backend=backend,
        handle=_handle(),
        user_key="abc",
        thread_id="t1",
        user_id="u1",
        postgres_db=db,
        http_client_factory=lambda creds, reader_id: probe_client,
    )

    db.activate_ro_mount_attempt_with_baseline.assert_awaited_once_with(
        "row-1",
        {"a.txt": "e1"},
        thread_id="t1",
        user_id="u1",
        selected_mount_id=_MOUNT,
        expected_runtime_generation=_RUNTIME_GENERATION,
        plan=_PLAN,
    )


@pytest.mark.asyncio
async def test_engage_row_is_not_deliverable_while_baseline_capture_is_blocked():
    entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockedBaseline(_FakeRoBackend):
        async def capture_etag_baseline(self, handle):
            entered.set()
            await release.wait()
            return {"a.txt": "e1"}

    backend = _BlockedBaseline()
    db = AsyncMock()
    db.install_ro_mount_engage_intent = AsyncMock(return_value={"id": "row-1"})
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "status": "engaging", "staged_summary": None}
    )
    db.activate_ro_mount_attempt_with_baseline = AsyncMock(return_value=True)
    task = asyncio.create_task(
        engage_ro_mount(
            backend=backend,
            handle=_handle(),
            user_key="abc",
            thread_id="t1",
            user_id="u1",
            postgres_db=db,
            http_client_factory=lambda _creds, _reader: _reader_client(),
            admission_check=AsyncMock(return_value=True),
        )
    )

    await entered.wait()
    db.install_ro_mount_engage_intent.assert_awaited_once()
    db.activate_ro_mount_attempt_with_baseline.assert_not_awaited()
    release.set()
    await task
    db.activate_ro_mount_attempt_with_baseline.assert_awaited_once()


@pytest.mark.asyncio
async def test_end_during_blocked_baseline_revokes_unpublished_attempt():
    entered = asyncio.Event()
    release = asyncio.Event()
    admitted = True

    class _BlockedBaseline(_FakeRoBackend):
        async def capture_etag_baseline(self, handle):
            entered.set()
            await release.wait()
            return {"a.txt": "e1"}

    async def _admission() -> bool:
        return admitted

    backend = _BlockedBaseline()
    db = AsyncMock()
    db.install_ro_mount_engage_intent = AsyncMock(return_value={"id": "row-1"})
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "status": "engaging", "staged_summary": None}
    )
    task = asyncio.create_task(
        engage_ro_mount(
            backend=backend,
            handle=_handle(),
            user_key="abc",
            thread_id="t1",
            user_id="u1",
            postgres_db=db,
            http_client_factory=lambda _creds, _reader: _reader_client(),
            admission_check=_admission,
        )
    )

    await entered.wait()
    admitted = False
    release.set()
    with pytest.raises(RoEngageRefused, match="no longer admits"):
        await task
    db.activate_ro_mount_attempt_with_baseline.assert_not_awaited()
    db.begin_ro_mount_revocation_if_matches.assert_awaited_once()
    assert backend.revoked


@pytest.mark.asyncio
async def test_engage_refuses_when_baseline_capture_fails():
    backend = _FakeRoBackend(
        baseline_error=CloudBackendError(
            CloudBackendErrorKind.UNAVAILABLE, "propfind failed", backend="nextcloud"
        )
    )
    probe_client = _reader_client(all_rejected=True, read_control=207)
    db = AsyncMock()
    db.install_ro_mount_engage_intent = AsyncMock(return_value={"id": "row-1"})
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
    db.begin_ro_mount_revocation_if_matches.assert_awaited_once()


@pytest.mark.asyncio
async def test_engage_refuses_when_baseline_persist_reports_inactive_row():
    # activate_ro_mount_with_baseline returning False means the row is no longer
    # active (e.g. the Slice A reconciler revoked it mid-engage) — the
    # baseline did NOT persist, so engage must refuse with the same
    # rollback, never report success without a baseline on the row.
    backend = _FakeRoBackend()
    probe_client = _reader_client(all_rejected=True, read_control=207)
    db = AsyncMock()
    db.install_ro_mount_engage_intent = AsyncMock(return_value={"id": "row-1"})
    db.activate_ro_mount_attempt_with_baseline = AsyncMock(return_value=False)
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
    db.begin_ro_mount_revocation_if_matches.assert_awaited_once()


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
    db.install_ro_mount_engage_intent = AsyncMock(return_value={"id": "row-1"})
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={
            "id": "row-1",
            "staged_summary": {"counts": {"added": 1}, "signature": "sig"},
            "etag_baseline": {"a.txt": "old-etag"},
        }
    )
    db.activate_ro_mount_attempt_with_baseline = AsyncMock(return_value=True)

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
    assert grant.reader_id == _PLAN.reader_id
    db.install_ro_mount_engage_intent.assert_awaited_once()
    # ...but the baseline is neither captured nor persisted.
    db.update_ro_mount_baseline.assert_not_awaited()
    assert db.activate_ro_mount_attempt_with_baseline.await_args.args[1] == {
        "a.txt": "old-etag"
    }
    assert backend.revoked == []  # not a refusal — a normal, successful skip


@pytest.mark.asyncio
async def test_engage_recaptures_baseline_when_no_live_staging():
    # Sanity counterpart: a row with staged_summary=None (first-time engage,
    # or after a restage-clear/apply/reject) must still recapture normally —
    # the skip guard must not become an unconditional skip.
    backend = _FakeRoBackend(baseline={"a.txt": "e1"})
    probe_client = _reader_client(all_rejected=True, read_control=207)
    db = AsyncMock()
    db.install_ro_mount_engage_intent = AsyncMock(return_value={"id": "row-1"})
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "staged_summary": None}
    )
    db.activate_ro_mount_attempt_with_baseline = AsyncMock(return_value=True)

    await engage_ro_mount(
        backend=backend,
        handle=_handle(),
        user_key="abc",
        thread_id="t1",
        user_id="u1",
        postgres_db=db,
        http_client_factory=lambda creds, reader_id: probe_client,
    )

    assert db.activate_ro_mount_attempt_with_baseline.await_args.args[1] == {
        "a.txt": "e1"
    }


@pytest.mark.asyncio
async def test_engage_passes_reader_id_to_client_factory():
    # Reader identity is derived only from the exact attempt plan, never from
    # the user id or thread id at the credential/probe boundary.
    backend = _FakeRoBackend()
    probe_client = _reader_client(all_rejected=True, read_control=207)
    recorder: list[tuple[str | None, str]] = []

    def factory(credentials, reader_id):
        recorder.append((credentials, reader_id))
        return probe_client

    db = AsyncMock()
    db.install_ro_mount_engage_intent = AsyncMock(return_value={"id": "row-1"})
    db.get_ro_mount_by_thread = AsyncMock(
        return_value={"id": "row-1", "staged_summary": None}
    )
    db.activate_ro_mount_attempt_with_baseline = AsyncMock(return_value=True)

    grant = await engage_ro_mount(
        backend=backend,
        handle=_handle(),
        user_key="u1",
        thread_id="t1",
        user_id="u1",
        postgres_db=db,
        http_client_factory=factory,
    )

    assert grant.reader_id == _PLAN.reader_id
    assert recorder == [(grant.credentials, _PLAN.reader_id)]
