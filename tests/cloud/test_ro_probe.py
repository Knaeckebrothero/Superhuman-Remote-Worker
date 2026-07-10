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

    ``PROPFIND`` (the Finding 1 positive read control) defaults to 207
    instead — a fake RO identity that is live and can read the target,
    which is what every pre-existing happy-path test intends. Override
    ``statuses["PROPFIND"]`` explicitly to exercise a failing read control.

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
        default = 207 if method == "PROPFIND" else 403
        return _FakeResp(self._s.get(method, default))


@pytest.mark.asyncio
async def test_all_verbs_rejected_is_ok():
    # Full coverage: dav_root/username supplied so the side channels are
    # attempted too, and everything (primary verbs + side channels) comes
    # back rejected -> the engage gate opens.
    res = await probe_read_only(
        _FakeClient({}),
        "https://cloud/dav",
        "folder/",
        dav_root="https://cloud/remote.php/dav",
        username="alice",
    )
    assert isinstance(res, RoProbeResult)
    assert res.ok is True
    assert res.failures == []
    assert res.inconclusive == []
    assert res.skipped == []


def test_ok_requires_all_three_lists_empty():
    # ok is the strict engage gate: ANY entry in failures, skipped, or
    # inconclusive refuses — not just failures.
    assert RoProbeResult().ok is True
    assert RoProbeResult(failures=["x"]).ok is False
    assert RoProbeResult(skipped=["x"]).ok is False
    assert RoProbeResult(inconclusive=["x"]).ok is False


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


class _BlanketStatusClient:
    """Every request — the read control included — gets the same status.

    Models a wrong/expired RO credential: the server 401s indiscriminately,
    on the read-control PROPFIND *and* on every mutating verb. This is
    exactly the shape of the whole-branch-review Finding 1 bug: with the
    old ``REJECTED_STATUSES = {401, 403, 405}``, every mutation-401 counted
    as "verified rejected" -> ``ok=True`` having authenticated nothing.
    """

    def __init__(self, status):
        self._status = status

    async def request(self, method, url, **kw):
        return _FakeResp(self._status)


@pytest.mark.asyncio
async def test_blanket_401_is_not_ok_finding_1_positive_read_control():
    # THE BUG (whole-branch review, Finding 1): a client that 401s on
    # everything — including the read-control PROPFIND — must NOT report
    # ok=True. Before the fix this assertion is FALSE (old code had no
    # positive read control, and 401 was in REJECTED_STATUSES, so a
    # blanket-401 server sailed through as "read-only verified").
    # RED (pre-fix): res.ok is True, so `assert res.ok is False` fails.
    # GREEN (post-fix): the read control fails (PROPFIND -> 401, not 2xx),
    # recorded in `failures`, and `ok` is False.
    client = _BlanketStatusClient(401)
    res = await probe_read_only(
        client,
        "https://cloud/dav",
        "folder/",
        dav_root="https://cloud/remote.php/dav",
        username="alice",
    )
    assert res.ok is False
    assert any("read control failed" in f and "PROPFIND" in f for f in res.failures)


@pytest.mark.asyncio
async def test_positive_read_control_passes_and_mutations_rejected_is_ok():
    # Positive control passes (PROPFIND Depth:0 -> 207) and every mutation
    # is genuinely rejected (403 default) -> ok is True. This is the
    # legitimate RO-identity shape the probe exists to certify.
    res = await probe_read_only(
        _FakeClient({}),
        "https://cloud/dav",
        "folder/",
        dav_root="https://cloud/remote.php/dav",
        username="alice",
    )
    assert res.ok is True
    assert res.failures == []


@pytest.mark.asyncio
async def test_read_control_passes_but_401_mutation_fails_closed():
    # Finding 1 also drops 401 from REJECTED_STATUSES: once the credential
    # is proven live (read control passes), a genuine RO identity should
    # only ever see 403/405 on a mutation. A 401 here is anomalous (the
    # credential that just read fine is being told "unauthenticated" on a
    # write attempt) and must fail closed, not be read as "rejected".
    res = await probe_read_only(_FakeClient({"PUT": 401}), "https://cloud/dav", "f/")
    assert res.ok is False
    put_failure = next(f for f in res.failures if f.startswith("PUT"))
    assert "401" in put_failure


@pytest.mark.asyncio
async def test_transport_error_fails_closed_and_keeps_probing():
    # PUT raises (simulated connection error); DELETE is unexpectedly
    # accepted (201) to prove probing continued past the exception.
    client = _FakeClient({"DELETE": 201}, raises={"PUT": ConnectionError("boom")})
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
    client = _FakeCapabilitiesClient(_nc_capabilities(28, 0, 3, groupfolders="20.1.2"))
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
    res = await check_version_floors(client, "https://cloud/dav", backend="opencloud")
    assert res.ok is True
    assert res.failures == []


@pytest.mark.asyncio
async def test_unknown_backend_fails_closed():
    # Allowlist, not "anything that isn't nextcloud passes": a typo'd or
    # wrong-case backend value must refuse, not silently skip the floors.
    client = _FakeCapabilitiesClient(_nc_capabilities(30, 0, 0))
    res = await check_version_floors(
        client, "https://cloud/remote.php/dav", backend="Nextcloud"
    )
    assert res.ok is False
    assert any("unknown backend" in f and "fail-closed" in f for f in res.failures)


# ---------------------------------------------------------------------------
# Finding 3 — side channels: skipped / inconclusive / real failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_side_channels_skipped_and_recorded_when_username_absent():
    # No dav_root/username -> side channels never attempted -> the gate
    # REFUSES (spec: side channels "all must 403/405 or protected mode
    # refuses to engage"). The refusal is curable — supply dav_root and
    # username — which is why it lands in `skipped`, not `failures`.
    res = await probe_read_only(_FakeClient({}), "https://cloud/dav", "f/")
    assert res.ok is False
    assert len(res.skipped) == 4
    assert res.failures == []
    assert res.inconclusive == []


@pytest.mark.asyncio
async def test_side_channel_404_is_inconclusive_and_refuses():
    # Synthetic ids are expected to 404 against a real server since they
    # don't exist; that proves the request round-tripped, NOT that the
    # authz layer rejected a real restore — so the gate refuses, with the
    # entries in `inconclusive` (curable: probe real fixture ids) rather
    # than `failures` (a write path demonstrably open).
    # dav_root deliberately contains "/remote.php/" so the fake client can
    # tell side-channel requests (which go through it) apart from the
    # primary-verb requests (which target base_url + path and must still
    # come back rejected, or this test would conflate two different
    # findings).
    class _404ForSideChannelsClient:
        async def request(self, method, url, **kw):
            if method == "PROPFIND":
                # Positive read control passes — this test is about
                # side-channel 404s being inconclusive, not about the
                # read control itself.
                return _FakeResp(207)
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
    assert res.ok is False
    assert res.failures == []
    assert len(res.inconclusive) == 4
    assert res.skipped == []


@pytest.mark.asyncio
async def test_side_channel_201_flips_ok_false():
    # Same URL-based split as above: primary verbs must stay rejected so
    # only the deliberate versions-restore 201 is under test.
    class _OneSucceedsClient:
        async def request(self, method, url, **kw):
            if method == "PROPFIND":
                # Positive read control passes — this test is about the
                # one deliberately-open mutation, not the read control.
                return _FakeResp(207)
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


# ---------------------------------------------------------------------------
# Live-k3d validation findings (2026-07-09) — three real ro_probe bugs the
# httpx fakes couldn't surface; each reproduces the exact real-Nextcloud shape.
# ---------------------------------------------------------------------------


class _TextResp:
    """A response exposing .text (for the PROPPATCH multistatus body)."""

    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


# --- Finding A: groupfolders version lives under `appVersion`, not `version` ---


@pytest.mark.asyncio
async def test_groupfolders_appversion_at_floor_is_ok():
    # Real NC exposes capabilities.groupfolders = {"appVersion": "20.1.2", ...}.
    client = _FakeCapabilitiesClient(
        _nc_capabilities(
            31, 0, 14, groupfolders={"appVersion": "20.1.2", "hasGroupFolders": False}
        )
    )
    res = await check_version_floors(
        client, "https://cloud/remote.php/dav", backend="nextcloud"
    )
    assert res.ok, res.failures


@pytest.mark.asyncio
async def test_groupfolders_appversion_below_floor_reports_the_real_version():
    # 19.1.18 is what dev actually runs — must be READ (not "unverifiable")
    # and reported as below the floor.
    client = _FakeCapabilitiesClient(
        _nc_capabilities(31, 0, 14, groupfolders={"appVersion": "19.1.18"})
    )
    res = await check_version_floors(
        client, "https://cloud/remote.php/dav", backend="nextcloud"
    )
    assert res.ok is False
    assert any("19.1.18" in f and "below" in f for f in res.failures), res.failures
    assert not any("unverifiable" in f for f in res.failures), res.failures


# --- Finding B: PROPPATCH 207 is the multistatus envelope; read inner status ---


class _ProppatchClient:
    def __init__(self, inner_status: int):
        self._inner = inner_status

    async def request(self, method, url, **kw):
        if method == "PROPFIND":
            return _TextResp(207)
        if method == "PROPPATCH":
            body = (
                '<?xml version="1.0"?>'
                '<d:multistatus xmlns:d="DAV:"><d:response>'
                "<d:href>/f</d:href><d:propstat><d:prop><d:displayname/></d:prop>"
                f"<d:status>HTTP/1.1 {self._inner} X</d:status>"
                "</d:propstat></d:response></d:multistatus>"
            )
            return _TextResp(207, body)
        return _TextResp(403)


@pytest.mark.asyncio
async def test_proppatch_207_with_inner_403_is_rejected_not_a_failure():
    res = await probe_read_only(
        _ProppatchClient(403), "https://cloud/dav/files/reader/proj/", "hello.txt"
    )
    assert not any("PROPPATCH" in f for f in res.failures), res.failures


@pytest.mark.asyncio
async def test_proppatch_207_with_inner_2xx_is_an_open_write():
    # A property actually got written -> a real RO bypass -> must fail.
    res = await probe_read_only(
        _ProppatchClient(200), "https://cloud/dav/files/reader/proj/", "hello.txt"
    )
    assert any("PROPPATCH" in f for f in res.failures), res.failures


# --- Finding C: COPY needs a Destination header, else NC 400s (masks 403) ---


class _CopyRecorderClient:
    def __init__(self):
        self.copy_headers = None

    async def request(self, method, url, **kw):
        if method == "PROPFIND":
            return _TextResp(207)
        if method == "COPY":
            self.copy_headers = kw.get("headers") or {}
            # NC: 403 when Destination present (authz reached), 400 when absent.
            return _TextResp(403 if "Destination" in self.copy_headers else 400)
        return _TextResp(403)


@pytest.mark.asyncio
async def test_copy_probe_sends_destination_and_reads_403():
    client = _CopyRecorderClient()
    res = await probe_read_only(
        client, "https://cloud/dav/files/reader/proj/", "hello.txt"
    )
    assert client.copy_headers and "Destination" in client.copy_headers
    assert not any("COPY" in f for f in res.failures), res.failures
