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
from orchestrator.services.cloud.ro_engage import RoEngageRefused, engage_ro_mount


class _Resp:
    def __init__(self, status, json_data=None):
        self.status_code = status
        self._json = json_data

    def json(self):
        return self._json


def _reader_client(*, all_rejected=True, read_control=207, nc_version="30.0.0"):
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
            if method == "GET" and "capabilities" in url:
                return _Resp(200, caps)
            if method == "PROPFIND":  # positive read control (Depth:0)
                return _Resp(read_control)
            # any mutating verb / side channel
            return _Resp(403 if all_rejected else 201)

    return _Client()


class _FakeRoBackend:
    backend_id = "nextcloud"

    def __init__(self):
        self.revoked: list[str] = []
        self.canary_removed = False

    async def ensure_ro_reader(self, *, user_key):
        return f"srw-reader-{user_key}"

    async def mint_ro_grant(self, handle, *, user_key, grant_key):
        return RoReaderGrant(
            reader_id=f"srw-reader-{user_key}",
            grant_handle=json.dumps(
                {"group_id": "g1", "reader_id": f"srw-reader-{user_key}"}
            ),
            webdav_url="https://nc/remote.php/dav/files/srw-reader-abc/Proj/",
            credentials="pw",
            auth_kind="basic",
        )

    async def revoke_ro_grant(self, grant_handle, *, user_key):
        self.revoked.append(grant_handle)

    async def seed_canary_fixture(self, handle):
        return CanaryFixture(path=".srw-ro-canary/probe.txt")

    async def remove_canary_fixture(self, handle, fixture):
        self.canary_removed = True


def _handle():
    return object()  # the gate treats the handle opaquely


@pytest.mark.asyncio
async def test_engage_persists_and_returns_grant_when_probe_ok():
    backend = _FakeRoBackend()
    probe_client = _reader_client(all_rejected=True, read_control=207)
    db = AsyncMock()
    db.create_ro_mount = AsyncMock(return_value="row-1")

    grant = await engage_ro_mount(
        backend=backend, handle=_handle(), user_key="abc",
        thread_id="t1", user_id="u1", postgres_db=db,
        http_client_factory=lambda creds: probe_client,
    )

    assert grant.reader_id == "srw-reader-abc"
    db.create_ro_mount.assert_awaited_once()
    assert backend.revoked == []          # not revoked on success
    assert backend.canary_removed is True  # canary always cleaned up


@pytest.mark.asyncio
async def test_engage_refuses_and_revokes_when_a_write_succeeds():
    backend = _FakeRoBackend()
    probe_client = _reader_client(all_rejected=False, read_control=207)  # PUT 201s
    db = AsyncMock()

    with pytest.raises(RoEngageRefused):
        await engage_ro_mount(
            backend=backend, handle=_handle(), user_key="abc",
            thread_id="t1", user_id="u1", postgres_db=db,
            http_client_factory=lambda creds: probe_client,
        )

    db.create_ro_mount.assert_not_awaited()
    assert backend.revoked  # grant was rolled back
    assert backend.canary_removed is True


@pytest.mark.asyncio
async def test_engage_refuses_when_read_control_fails():
    # A dead/expired credential 401s the positive read control; the gate must
    # refuse even though every mutating verb also "rejects".
    backend = _FakeRoBackend()
    probe_client = _reader_client(all_rejected=True, read_control=401)
    db = AsyncMock()

    with pytest.raises(RoEngageRefused):
        await engage_ro_mount(
            backend=backend, handle=_handle(), user_key="abc",
            thread_id="t1", user_id="u1", postgres_db=db,
            http_client_factory=lambda creds: probe_client,
        )
    db.create_ro_mount.assert_not_awaited()
    assert backend.revoked


@pytest.mark.asyncio
async def test_engage_refuses_when_version_below_floor():
    backend = _FakeRoBackend()
    probe_client = _reader_client(all_rejected=True, read_control=207, nc_version="27.0.0")
    db = AsyncMock()

    with pytest.raises(RoEngageRefused):
        await engage_ro_mount(
            backend=backend, handle=_handle(), user_key="abc",
            thread_id="t1", user_id="u1", postgres_db=db,
            http_client_factory=lambda creds: probe_client,
        )
    db.create_ro_mount.assert_not_awaited()
    assert backend.revoked
