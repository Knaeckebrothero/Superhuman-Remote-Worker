from __future__ import annotations
import pytest

from orchestrator.services.cloud.ro_probe import (
    check_version_floors,
    probe_read_only,
    side_channel_probes,
    RoProbeResult,
    MUTATING_VERBS,
    VERSION_FLOORS,
)


class _FakeResp:
    def __init__(self, status, json_data=None, json_error=None):
        self.status_code = status
        self._json_data = json_data
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._json_data


class _FakeClient:
    """Returns a per-verb status from a dict; defaults to 403 (rejected).

    ``raises`` maps a verb to an exception instance raised instead of
    returning a response — used to simulate transport failures (timeouts,
    connection errors) for the fail-closed transport-error path.
    """
    def __init__(self, statuses, raises=None):
        self._s = statuses
        self._raises = raises or {}

    async def request(self, method, url, **kw):
        if method in self._raises:
            raise self._raises[method]
        return _FakeResp(self._s.get(method, 403))


@pytest.mark.asyncio
async def test_all_verbs_rejected_is_ok():
    res = await probe_read_only(_FakeClient({}), "https://cloud/dav", "folder/")
    assert isinstance(res, RoProbeResult)
    assert res.ok is True
    assert res.failures == []


@pytest.mark.asyncio
async def test_any_write_success_fails_closed():
    # PUT unexpectedly accepted (201) -> not read-only
    res = await probe_read_only(_FakeClient({"PUT": 201}), "https://cloud/dav", "f/")
    assert res.ok is False
    assert "PUT" in res.failures[0]


def test_side_channel_verbs_are_probed():
    # versions/trash restore + upload-finalize CVE class must be probed via
    # real DAV MOVE/POST requests (see side_channel_probes), not the old
    # fake-POST-to-the-generic-folder placeholder.
    probes = side_channel_probes("https://cloud/dav", "alice")
    assert any(
        verb == "MOVE" and "version" in note and "restore" in note
        for verb, note, _req in probes
    )
    assert any(
        verb == "MOVE" and "trash" in note and "restore" in note
        for verb, note, _req in probes
    )
    assert any(
        verb == "MOVE" and "upload" in note and "finalize" in note
        for verb, note, _req in probes
    )
    assert any(verb == "POST" and "tus" in note.lower() for verb, note, _req in probes)


def test_mutating_verbs_no_longer_contain_fake_post_side_channels():
    # Finding 3: the two fake generic-folder POSTs must be gone; the real
    # verbs stay.
    for verb in ("PUT", "DELETE", "MKCOL", "MOVE", "PROPPATCH", "COPY"):
        assert any(v[0] == verb for v in MUTATING_VERBS)
    assert not any(v[0] == "POST" for v in MUTATING_VERBS)


# ---------------------------------------------------------------------------
# Finding 1 — transport exceptions must fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transport_error_fails_closed_and_keeps_probing():
    # PUT raises (simulated connection error); DELETE is unexpectedly
    # accepted (201) to prove probing continued past the exception.
    client = _FakeClient(
        {"DELETE": 201}, raises={"PUT": ConnectionError("boom")}
    )
    res = await probe_read_only(client, "https://cloud/dav", "f/")
    assert res.ok is False
    put_failure = next(f for f in res.failures if f.startswith("PUT"))
    assert "transport error" in put_failure
    assert "ConnectionError" in put_failure
    assert "fail-closed" in put_failure
    # DELETE was still probed after the PUT exception and recorded its own
    # failure.
    assert any(f.startswith("DELETE") for f in res.failures)


# ---------------------------------------------------------------------------
# Finding 2 — version floors
# ---------------------------------------------------------------------------


def _nc_capabilities(major, minor, micro, groupfolders=None):
    caps = {}
    if groupfolders is not None:
        caps["groupfolders"] = groupfolders
    return {
        "ocs": {
            "data": {
                "version": {"major": major, "minor": minor, "micro": micro},
                "capabilities": caps,
            }
        }
    }


class _FakeCapabilitiesClient:
    def __init__(self, json_data=None, status=200, json_error=None, raises=None):
        self._json_data = json_data
        self._status = status
        self._json_error = json_error
        self._raises = raises

    async def request(self, method, url, **kw):
        if self._raises is not None:
            raise self._raises
        return _FakeResp(self._status, self._json_data, self._json_error)


def test_version_floors_constant():
    assert VERSION_FLOORS["nextcloud"] == (28, 0, 3)
    assert VERSION_FLOORS["groupfolders"] == (20, 1, 2)


@pytest.mark.asyncio
async def test_nc_above_floor_but_groupfolders_absent_is_not_ok():
    client = _FakeCapabilitiesClient(_nc_capabilities(30, 0, 0))
    res = await check_version_floors(
        client, "https://cloud/remote.php/dav", backend="nextcloud"
    )
    assert res.ok is False
    assert any("groupfolders" in f and "unverifiable" in f for f in res.failures)


@pytest.mark.asyncio
async def test_nc_below_server_floor_is_not_ok():
    client = _FakeCapabilitiesClient(
        _nc_capabilities(28, 0, 2, groupfolders={"version": "20.1.2"})
    )
    res = await check_version_floors(
        client, "https://cloud/remote.php/dav", backend="nextcloud"
    )
    assert res.ok is False
    assert any("28.0.2" in f for f in res.failures)


@pytest.mark.asyncio
async def test_nc_at_floor_exactly_with_groupfolders_is_ok():
    client = _FakeCapabilitiesClient(
        _nc_capabilities(28, 0, 3, groupfolders="20.1.2")
    )
    res = await check_version_floors(
        client, "https://cloud/remote.php/dav", backend="nextcloud"
    )
    assert res.ok is True
    assert res.failures == []


@pytest.mark.asyncio
async def test_unparseable_capabilities_json_is_not_ok():
    client = _FakeCapabilitiesClient(json_error=ValueError("bad json"))
    res = await check_version_floors(
        client, "https://cloud/remote.php/dav", backend="nextcloud"
    )
    assert res.ok is False


@pytest.mark.asyncio
async def test_opencloud_has_no_version_floor_and_is_ok():
    client = _FakeCapabilitiesClient(None)
    res = await check_version_floors(
        client, "https://cloud/dav", backend="opencloud"
    )
    assert res.ok is True
    assert res.failures == []


# ---------------------------------------------------------------------------
# Finding 3 — side channels: skipped / inconclusive / real failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_side_channels_skipped_and_recorded_when_username_absent():
    res = await probe_read_only(_FakeClient({}), "https://cloud/dav", "f/")
    assert res.ok is True
    assert len(res.skipped) == 4
    assert res.inconclusive == []


@pytest.mark.asyncio
async def test_side_channel_404_is_inconclusive_not_ok_flip():
    # Synthetic ids are expected to 404 against a real server since they
    # don't exist; that must not be conflated with "verified rejected".
    # dav_root deliberately contains "/remote.php/" so the fake client can
    # tell side-channel requests (which go through it) apart from the
    # primary-verb requests (which target base_url + path and must still
    # come back rejected, or this test would conflate two different
    # findings).
    class _404ForSideChannelsClient:
        async def request(self, method, url, **kw):
            if "/remote.php/" in url:
                return _FakeResp(404)
            return _FakeResp(403)

    res = await probe_read_only(
        _404ForSideChannelsClient(),
        "https://cloud/dav",
        "f/",
        dav_root="https://cloud/remote.php/dav",
        username="alice",
    )
    assert res.ok is True
    assert res.failures == []
    assert len(res.inconclusive) == 4
    assert res.skipped == []


@pytest.mark.asyncio
async def test_side_channel_201_flips_ok_false():
    # Same URL-based split as above: primary verbs must stay rejected so
    # only the deliberate versions-restore 201 is under test.
    class _OneSucceedsClient:
        async def request(self, method, url, **kw):
            if method == "MOVE" and "/versions/" in url:
                return _FakeResp(201)
            if "/remote.php/" in url:
                return _FakeResp(403)
            return _FakeResp(403)

    res = await probe_read_only(
        _OneSucceedsClient(),
        "https://cloud/dav",
        "f/",
        dav_root="https://cloud/remote.php/dav",
        username="alice",
    )
    assert res.ok is False
    assert any("201" in f for f in res.failures)
