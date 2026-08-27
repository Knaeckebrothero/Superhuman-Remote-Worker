from __future__ import annotations
import pytest

from orchestrator.services.cloud.ro_probe import (
    check_version_floors,
    groupfolders_floor,
    probe_read_only,
    side_channel_probes,
    RoProbeResult,
    GROUPFOLDERS_PATCHED,
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
    # real DAV MOVE requests (see side_channel_probes), not the old
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
    # tus-create (a POST to the reader's OWN uploads root) was removed after
    # the live run: it can never demonstrate a mount bypass. The only
    # mount-touching step of a chunked/TUS upload is the finalize MOVE, so
    # that is THE test — no standalone POST side channel remains.
    assert not any(verb == "POST" for verb, _note, _req in probes)


def test_uploads_finalize_destination_targets_the_mount_not_home():
    # THE live regression (NC 31, 2026-07-12): a reader can always write its
    # own home, so the finalize Destination must land INSIDE the protected
    # mount or the probe false-positives (home dest -> 201; mount dest -> 403).
    mount = "https://cloud/remote.php/dav/files/alice/Proj"
    probes = side_channel_probes(
        "https://cloud/remote.php/dav", "alice", mount_url=mount
    )
    finalize = next(req for verb, note, req in probes if note == "uploads-finalize")
    assert finalize["headers"]["Destination"].startswith(mount)
    # never the reader's own files/<user> home root
    assert "/files/alice/srw-ro-probe" not in finalize["headers"]["Destination"]


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
    # groupfolders is branch-aware, not a single floor: GHSA-2vrq-fhmf-c49m
    # was patched in every maintained branch (each pinned to one NC major).
    assert "groupfolders" not in VERSION_FLOORS
    assert GROUPFOLDERS_PATCHED == {
        14: (14, 0, 11),
        15: (15, 3, 12),
        16: (16, 0, 15),
        17: (17, 0, 14),
        18: (18, 1, 8),
        19: (19, 1, 8),
        20: (20, 1, 2),
    }


def test_groupfolders_floor_per_branch():
    # In-table branches return their own patched release.
    assert groupfolders_floor(19) == (19, 1, 8)
    assert groupfolders_floor(20) == (20, 1, 2)
    # Branches born after the fix are safe from their first release.
    assert groupfolders_floor(21) == (21, 0, 0)
    assert groupfolders_floor(22) == (22, 0, 0)
    # Branches older than the patched set have no safe release.
    assert groupfolders_floor(13) is None


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
    assert len(res.skipped) == 3
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
    assert len(res.inconclusive) == 3
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
# Live-k3d validation findings (2026-07-12) — trash-restore denial is a 500,
# not a clean 403, so the probe verifies the EFFECT (item stays trashed).
# ---------------------------------------------------------------------------


_TRASH_ITEM = "srw-ro-trash-canary.txt.d1699999999"


def _trash_body(items):
    rows = "".join(
        "<d:response><d:href>/remote.php/dav/trashbin/alice/trash/"
        f"{it}</d:href><d:propstat><d:status>HTTP/1.1 200 OK</d:status>"
        "</d:propstat></d:response>"
        for it in items
    )
    return f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">{rows}</d:multistatus>'


@pytest.mark.asyncio
async def test_trash_restore_500_but_item_still_trashed_is_rejected():
    # Live shape (NC 31): the RO reader's trash-restore MOVE returns 500, but
    # the item STAYS in trash -> the restore had no effect -> RO held. The
    # probe must treat this as rejected, not "write path open".
    class _Client:
        async def request(self, method, url, **kw):
            if method == "PROPFIND" and "/trashbin/" in url:
                # effect check: the seeded item is still present
                return _TextResp(207, _trash_body([_TRASH_ITEM]))
            if method == "PROPFIND":
                return _FakeResp(207)  # read control
            if method == "MKCOL":
                return _FakeResp(404)  # no real upload session
            if method == "MOVE" and "/trashbin/" in url:
                return _FakeResp(500)  # NC's ungraceful RO denial
            if "/remote.php/" in url:
                return _FakeResp(403)
            return _FakeResp(403)

    res = await probe_read_only(
        _Client(),
        "https://cloud/dav",
        "f/",
        dav_root="https://cloud/remote.php/dav",
        username="alice",
        version_ref="12345/1",
        trash_ref=_TRASH_ITEM,
    )
    assert not any("trash-restore" in f for f in res.failures), res.failures


@pytest.mark.asyncio
async def test_trash_restore_item_gone_from_trash_is_failure():
    # If the item is NO longer in trash after the MOVE, the restore actually
    # succeeded — a real RO bypass — even if the status wasn't a clean 2xx.
    class _Client:
        async def request(self, method, url, **kw):
            if method == "PROPFIND" and "/trashbin/" in url:
                return _TextResp(207, _trash_body([]))  # item gone -> restored
            if method == "PROPFIND":
                return _FakeResp(207)
            if method == "MKCOL":
                return _FakeResp(404)
            if method == "MOVE" and "/trashbin/" in url:
                return _FakeResp(500)
            if "/remote.php/" in url:
                return _FakeResp(403)
            return _FakeResp(403)

    res = await probe_read_only(
        _Client(),
        "https://cloud/dav",
        "f/",
        dav_root="https://cloud/remote.php/dav",
        username="alice",
        version_ref="12345/1",
        trash_ref=_TRASH_ITEM,
    )
    assert res.ok is False
    assert any("trash-restore" in f and "write path open" in f for f in res.failures)


@pytest.mark.asyncio
async def test_trash_restore_unverifiable_trashbin_fails_closed():
    # If the effect-verification PROPFIND itself fails, the probe cannot
    # confirm the item stayed trashed -> must fail closed (a failure), never a
    # silent "rejected".
    class _Client:
        async def request(self, method, url, **kw):
            if method == "PROPFIND" and "/trashbin/" in url:
                raise ConnectionError("boom")
            if method == "PROPFIND":
                return _FakeResp(207)
            if method == "MKCOL":
                return _FakeResp(404)
            if method == "MOVE" and "/trashbin/" in url:
                return _FakeResp(500)
            if "/remote.php/" in url:
                return _FakeResp(403)
            return _FakeResp(403)

    res = await probe_read_only(
        _Client(),
        "https://cloud/dav",
        "f/",
        dav_root="https://cloud/remote.php/dav",
        username="alice",
        version_ref="12345/1",
        trash_ref=_TRASH_ITEM,
    )
    assert res.ok is False
    assert any("trash-restore" in f for f in res.failures)


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
async def test_groupfolders_appversion_below_branch_floor_reports_the_real_version():
    # 19.1.7 predates the 19.x branch fix (19.1.8) — must be READ (not
    # "unverifiable") and reported as below the branch floor.
    client = _FakeCapabilitiesClient(
        _nc_capabilities(31, 0, 14, groupfolders={"appVersion": "19.1.7"})
    )
    res = await check_version_floors(
        client, "https://cloud/remote.php/dav", backend="nextcloud"
    )
    assert res.ok is False
    assert any("19.1.7" in f and "below" in f for f in res.failures), res.failures
    assert not any("unverifiable" in f for f in res.failures), res.failures


@pytest.mark.asyncio
async def test_groupfolders_patched_nc31_branch_is_ok():
    # THE live regression (k3d NC 31.0.14, 2026-07-12): groupfolders 20.x is
    # NC 32-only, so on NC 31 the patched 19.1.x branch must pass — a single
    # 20.1.2 floor would refuse every fully patched NC 31 install forever.
    client = _FakeCapabilitiesClient(
        _nc_capabilities(31, 0, 14, groupfolders={"appVersion": "19.1.18"})
    )
    res = await check_version_floors(
        client, "https://cloud/remote.php/dav", backend="nextcloud"
    )
    assert res.ok, res.failures


@pytest.mark.asyncio
async def test_groupfolders_branch_born_after_fix_is_ok():
    # Branches newer than the advisory's table carry the fix from .0.0.
    client = _FakeCapabilitiesClient(
        _nc_capabilities(33, 0, 1, groupfolders={"appVersion": "21.0.9"})
    )
    res = await check_version_floors(
        client, "https://cloud/remote.php/dav", backend="nextcloud"
    )
    assert res.ok, res.failures


@pytest.mark.asyncio
async def test_groupfolders_branch_predating_patched_set_refuses():
    # 13.x has no GHSA-2vrq-fhmf-c49m-patched release at all — fail-closed.
    client = _FakeCapabilitiesClient(
        _nc_capabilities(28, 0, 3, groupfolders={"appVersion": "13.1.8"})
    )
    res = await check_version_floors(
        client, "https://cloud/remote.php/dav", backend="nextcloud"
    )
    assert res.ok is False
    assert any("13.x" in f and "fail-closed" in f for f in res.failures), res.failures


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


# ---------------------------------------------------------------------------
# Amended scope A1 — real version_ref/trash_ref passthrough
# ---------------------------------------------------------------------------


def test_side_channel_probes_use_real_refs_when_supplied():
    probes = side_channel_probes(
        "https://cloud/remote.php/dav",
        "alice",
        version_ref="12345/1699999999",
        trash_ref="probe.txt.d1699999999",
    )
    versions_probe = next(p for p in probes if p[1] == "versions-restore")
    assert versions_probe[2]["url"] == (
        "https://cloud/remote.php/dav/versions/alice/versions/12345/1699999999"
    )
    trash_probe = next(p for p in probes if p[1] == "trash-restore")
    assert trash_probe[2]["url"] == (
        "https://cloud/remote.php/dav/trashbin/alice/trash/probe.txt.d1699999999"
    )
    assert trash_probe[2]["headers"]["Destination"] == (
        "https://cloud/remote.php/dav/trashbin/alice/restore/probe.txt.d1699999999"
    )


def test_side_channel_probes_default_to_synthetic_ids_when_refs_absent():
    probes = side_channel_probes("https://cloud/remote.php/dav", "alice")
    versions_probe = next(p for p in probes if p[1] == "versions-restore")
    assert "999999999/1" in versions_probe[2]["url"]
    trash_probe = next(p for p in probes if p[1] == "trash-restore")
    assert "srw-ro-probe-item" in trash_probe[2]["url"]
    assert "srw-ro-probe-item" in trash_probe[2]["headers"]["Destination"]


# ---------------------------------------------------------------------------
# Amended scope A2 — uploads-finalize self-provisions a real upload session
# ---------------------------------------------------------------------------


class _UploadSessionClient:
    """Models the reader's own ``uploads/{username}/...`` namespace.

    ``mkcol_status`` controls whether provisioning a real session succeeds
    (201) or not (any other status). Every other verb/side channel is
    rejected (403) by default so only the uploads-finalize behavior is
    under test; PROPFIND (positive read control) passes (207).
    """

    def __init__(self, mkcol_status: int):
        self._mkcol_status = mkcol_status
        self.mkcol_urls: list[str] = []
        self.delete_urls: list[str] = []
        self.uploads_move_requests: list[tuple[str, dict]] = []

    async def request(self, method, url, **kw):
        if method == "PROPFIND":
            return _FakeResp(207)
        # Only the /uploads/ namespace is the A2 provisioning path; the
        # primary MUTATING_VERBS loop also probes MKCOL/DELETE against the
        # canary target URL and those must stay ordinary rejected (403)
        # writes, not be mistaken for session provisioning/cleanup.
        if method == "MKCOL" and "/uploads/" in url:
            self.mkcol_urls.append(url)
            return _FakeResp(self._mkcol_status)
        if method == "DELETE" and "/uploads/" in url:
            self.delete_urls.append(url)
            return _FakeResp(204)
        if method == "MOVE" and "/uploads/" in url:
            self.uploads_move_requests.append((url, kw))
            # Real session provisioned -> a genuine RO reader gets 403.
            # No session (fallback to synthetic id) -> server has never
            # heard of it -> 404 (inconclusive), same as pre-A2 behavior.
            return _FakeResp(403 if self._mkcol_status == 201 else 404)
        return _FakeResp(403)


@pytest.mark.asyncio
async def test_uploads_finalize_targets_real_session_when_mkcol_succeeds():
    client = _UploadSessionClient(mkcol_status=201)
    res = await probe_read_only(
        client,
        "https://cloud/dav",
        "f/",
        dav_root="https://cloud/remote.php/dav",
        username="alice",
    )
    # MKCOL was attempted against the reader's own uploads namespace.
    assert client.mkcol_urls == [
        "https://cloud/remote.php/dav/uploads/alice/srw-ro-probe"
    ]
    # uploads-finalize MOVE landed on that same real session, and its 403
    # counted as a verified rejection (not inconclusive).
    assert len(client.uploads_move_requests) == 1
    move_url, _ = client.uploads_move_requests[0]
    assert move_url == "https://cloud/remote.php/dav/uploads/alice/srw-ro-probe/.file"
    assert not any("uploads-finalize" in i for i in res.inconclusive)
    assert not any("uploads-finalize" in f for f in res.failures)
    # The provisioned session was cleaned up (best-effort DELETE).
    assert client.delete_urls == [
        "https://cloud/remote.php/dav/uploads/alice/srw-ro-probe"
    ]
    assert res.ok is True


@pytest.mark.asyncio
async def test_uploads_finalize_falls_back_to_synthetic_when_mkcol_fails():
    client = _UploadSessionClient(mkcol_status=404)
    res = await probe_read_only(
        client,
        "https://cloud/dav",
        "f/",
        dav_root="https://cloud/remote.php/dav",
        username="alice",
    )
    # MKCOL failed (not 201) -> no real session -> nothing to clean up.
    assert client.delete_urls == []
    # The finalize probe still ran (against the same synthetic-named URL —
    # the URL shape doesn't change, only whether a real session backs it),
    # and 404 lands in `inconclusive`, exactly as it did before A2.
    assert any("uploads-finalize" in i and "404" in i for i in res.inconclusive), (
        res.inconclusive
    )
    assert not any("uploads-finalize" in f for f in res.failures)


@pytest.mark.asyncio
async def test_mkcol_transport_error_is_not_recorded_as_a_probe_failure():
    class _MkcolBoomClient:
        async def request(self, method, url, **kw):
            if method == "PROPFIND":
                return _FakeResp(207)
            if method == "MKCOL" and "/uploads/" in url:
                raise ConnectionError("boom")
            if method == "DELETE" and "/uploads/" in url:
                raise AssertionError("no session was provisioned; must not DELETE")
            return _FakeResp(404 if "/uploads/" in url else 403)

    res = await probe_read_only(
        _MkcolBoomClient(),
        "https://cloud/dav",
        "f/",
        dav_root="https://cloud/remote.php/dav",
        username="alice",
    )
    # The MKCOL transport error itself must not appear as a failure entry —
    # it's setup, not a verb probe. It simply falls back to synthetic.
    assert not any("MKCOL" in f for f in res.failures), res.failures
    assert any("uploads-finalize" in i and "404" in i for i in res.inconclusive), (
        res.inconclusive
    )
