"""Fail-closed read-only verification for a protected-mount identity.

Given credentials for the dedicated RO account (NOT agent-service — see
docs/design/cloud_access_unification.md §3.3), attempt every mutating
WebDAV verb AND the version/trash side channels that had real RO-bypass
CVEs (Nextcloud GHSA-5mq8-738w-5942 / GHSA-2vrq-fhmf-c49m). Protected
cloud mode must refuse to engage unless every one is rejected.

This module is a permanent Phase-1 artifact (not spike-only code), so it
is held to fail-closed standards throughout:

* **Transport errors fail closed.** A timeout or connection error while
  probing a verb is recorded as a failure, not swallowed — an
  unreachable server must never be read as "read-only verified".
* **Version floors are enforced, not assumed.** ``check_version_floors``
  turns the CVE-fix versions from the spec (Nextcloud server >= 28.0.3,
  groupfolders >= 20.1.2) into a runtime capabilities check instead of a
  documentation-only assumption.
* **Side channels are real requests, not decoys.** ``side_channel_probes``
  builds the actual DAV MOVE/POST requests a real restore/finalize
  operation would issue (dedicated ``versions``/``trashbin``/``uploads``
  namespaces), not a second POST to the same generic folder URL every
  other verb already exercises — the latter can 405 for reasons that
  have nothing to do with restore-permission enforcement, giving false
  assurance against the very CVE class this module exists to catch.

Important caveat, tracked explicitly rather than glossed over: the exact
success/rejection status codes for the side channels (in particular
whether synthetic, nonexistent ids come back 404/409 vs. some other
code on a given server/version) are **assumed, not yet validated**
against a live backend. That validation is out of scope for this spike
and happens in the design's live-probe run
(docs/design/cloud_access_unification.md §6.4 / §6 item 4 — "Live RO
probe"). Until that run has executed against a real Nextcloud (>=28.0.3)
and OpenCloud instance, Phase 1 callers MUST treat a non-empty
``RoProbeResult.skipped`` as "side channels unverified" — i.e. not a
substitute for having actually probed them — and should surface that
distinction to whatever gates protected-mode engagement rather than
silently claiming full coverage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

REJECTED_STATUSES = frozenset({401, 403, 405})

# Status codes for side-channel probes that mean "the request didn't reach
# far enough to prove anything" (synthetic ids are expected to 404/409 on a
# server that never heard of them) — recorded as inconclusive, not ok.
_INCONCLUSIVE_STATUSES = frozenset({404, 409})

# Version floors from docs/design/cloud_access_unification.md §3.3: the
# releases that fixed the RO-bypass CVEs this module probes for.
#   nextcloud    >= 28.0.3  (GHSA-5mq8-738w-5942, versions-restore)
#   groupfolders >= 20.1.2  (GHSA-2vrq-fhmf-c49m, trash-restore)
VERSION_FLOORS: dict[str, tuple[int, int, int]] = {
    "nextcloud": (28, 0, 3),
    "groupfolders": (20, 1, 2),
}

# (verb, note, body-or-None). note documents the side channel; body used
# for endpoints that need a payload to be a fair test. The historical
# versions-restore/trash-restore CVE class is NOT covered by a verb here
# — a generic POST to this same folder URL can 405 for reasons unrelated
# to restore-permission enforcement. See ``side_channel_probes`` for the
# real DAV requests that actually exercise those side channels.
MUTATING_VERBS: list[tuple[str, str | None, bytes | None]] = [
    ("PUT", None, b"srw-ro-probe"),
    ("DELETE", None, None),
    ("MKCOL", None, None),
    ("MOVE", None, None),
    ("PROPPATCH", None, b'<?xml version="1.0"?><d:propertyupdate xmlns:d="DAV:"/>'),
    ("COPY", None, None),
]

# Synthetic ids used to build side-channel requests. They don't exist on
# any real server, so 404/409 is the *expected* non-write response —
# that's why those statuses land in ``inconclusive`` rather than
# ``failures``: they prove the request round-tripped, not that a real
# restore attempt would have been rejected by the authz layer.
_SYNTHETIC_FILEID = "999999999"
_SYNTHETIC_VERSIONID = "1"
_SYNTHETIC_TRASH_ITEM = "srw-ro-probe-item"
_SYNTHETIC_TRANSFER_ID = "srw-ro-probe"
_SYNTHETIC_UPLOAD_TARGET = "srw-ro-probe-target"


def side_channel_probes(
    dav_root: str, username: str
) -> list[tuple[str, str, dict[str, Any]]]:
    """Build the real restore/finalize requests for the RO-bypass CVE class.

    Returns a list of ``(verb, note, request_kwargs)`` where
    ``request_kwargs`` always has a ``"url"`` key plus any ``"headers"``
    needed, ready to be passed to ``client.request(verb, **request_kwargs)``
    (after popping ``"url"`` for the positional slot — see
    ``probe_read_only``).

    Four real operations, not a fourth generic-folder POST:

    * **versions-restore** (GHSA-5mq8-738w-5942): ``MOVE`` a version into
      the restore target, under the dedicated ``versions`` DAV namespace.
    * **trash-restore** (GHSA-2vrq-fhmf-c49m): ``MOVE`` a trashed item
      back into ``files``, under the dedicated ``trashbin`` namespace.
    * **chunked-upload finalize** (the spec's missing
      "chunked-upload/TUS finalize" verb): ``MOVE`` an in-progress upload
      chunk collection's ``.file`` marker into ``files`` — this is how a
      chunked upload is finalized/committed on Nextcloud's ``uploads``
      DAV namespace.
    * **TUS creation** (oCIS/OpenCloud path): a TUS-style ``POST`` with
      ``Tus-Resumable``/``Upload-Length`` headers against the uploads
      collection, attempting to start a brand-new upload session.

    Synthetic ids (see the ``_SYNTHETIC_*`` constants) stand in for real
    file/version/transfer ids so no real cloud state needs to exist for
    the probe to run.
    """
    root = dav_root.rstrip("/")
    return [
        (
            "MOVE",
            "versions-restore",
            {
                "url": (
                    f"{root}/versions/{username}/versions/"
                    f"{_SYNTHETIC_FILEID}/{_SYNTHETIC_VERSIONID}"
                ),
                "headers": {
                    "Destination": f"{root}/versions/{username}/restore/target"
                },
            },
        ),
        (
            "MOVE",
            "trash-restore",
            {
                "url": f"{root}/trashbin/{username}/trash/{_SYNTHETIC_TRASH_ITEM}",
                "headers": {
                    "Destination": (
                        f"{root}/trashbin/{username}/restore/"
                        f"{_SYNTHETIC_TRASH_ITEM}"
                    )
                },
            },
        ),
        (
            "MOVE",
            "uploads-finalize",
            {
                "url": f"{root}/uploads/{username}/{_SYNTHETIC_TRANSFER_ID}/.file",
                "headers": {
                    "Destination": (
                        f"{root}/files/{username}/{_SYNTHETIC_UPLOAD_TARGET}"
                    )
                },
            },
        ),
        (
            "POST",
            "tus-create",
            {
                "url": f"{root}/uploads/{username}/",
                "headers": {"Tus-Resumable": "1.0.0", "Upload-Length": "1"},
            },
        ),
    ]


@dataclass
class RoProbeResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    # 404/409 on a side channel: the request round-tripped but synthetic
    # ids mean we can't tell whether the authz layer was even reached.
    # Does NOT flip ``ok`` — it is neither a pass nor a fail.
    inconclusive: list[str] = field(default_factory=list)
    # Side channels never attempted (``dav_root``/``username`` not
    # supplied to ``probe_read_only``). Non-empty means the result makes
    # no claim about the CVE-class side channels at all; see the module
    # docstring's "Phase 1 MUST treat non-empty skipped as unverified"
    # note.
    skipped: list[str] = field(default_factory=list)


async def probe_read_only(
    client,
    base_url: str,
    path: str,
    *,
    dav_root: str | None = None,
    username: str | None = None,
) -> RoProbeResult:
    target = base_url.rstrip("/") + "/" + path.lstrip("/")
    failures: list[str] = []
    inconclusive: list[str] = []
    skipped: list[str] = []

    for verb, note, body in MUTATING_VERBS:
        label = verb if not note else f"{verb} ({note})"
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["content"] = body
        if verb == "MOVE":
            kwargs["headers"] = {"Destination": target + ".moved"}
        try:
            resp = await client.request(verb, target, **kwargs)
        except Exception as e:
            # Finding 1: a transport failure (timeout, connection error,
            # ...) must refuse, not crash the probe or silently skip the
            # verb. Continue probing the remaining verbs regardless.
            failures.append(
                f"{label} -> transport error {type(e).__name__} (fail-closed)"
            )
            continue
        if resp.status_code not in REJECTED_STATUSES:
            failures.append(f"{label} -> {resp.status_code} (expected 401/403/405)")

    if dav_root is not None and username is not None:
        for verb, note, req in side_channel_probes(dav_root, username):
            label = f"{verb} ({note})"
            url = req["url"]
            kwargs = {k: v for k, v in req.items() if k != "url"}
            try:
                resp = await client.request(verb, url, **kwargs)
            except Exception as e:
                failures.append(
                    f"{label} -> transport error {type(e).__name__} (fail-closed)"
                )
                continue
            status = resp.status_code
            if status in REJECTED_STATUSES:
                continue
            if status in _INCONCLUSIVE_STATUSES:
                inconclusive.append(
                    f"{label} -> {status} (synthetic id; authz layer may not "
                    "have been reached — inconclusive, not verified rejected)"
                )
                continue
            failures.append(
                f"{label} -> {status} (expected 401/403/405; write path open)"
            )
    else:
        # dav_root/username weren't supplied — don't silently claim
        # coverage we never attempted. See module docstring.
        for verb, note, _req in side_channel_probes(
            dav_root or "<unset>", username or "<unset>"
        ):
            skipped.append(
                f"{verb} ({note}) -> skipped (dav_root/username not supplied; "
                "side channels unverified)"
            )

    return RoProbeResult(
        ok=not failures,
        failures=failures,
        inconclusive=inconclusive,
        skipped=skipped,
    )


def _parse_version_tuple(value: Any) -> tuple[int, int, int] | None:
    """Parse a dotted version out of a capabilities value.

    Defensive by design (module docstring / spec note): the value may be
    a plain ``"20.1.2"`` string, a dict with a ``"version"`` key, or
    simply absent — different Nextcloud versions expose the groupfolders
    app version under ``capabilities.groupfolders`` inconsistently.
    Anything that doesn't cleanly parse to three ints returns ``None``
    (caller treats that as fail-closed "unverifiable").
    """
    if isinstance(value, dict):
        value = value.get("version")
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) < 3:
        return None
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _parse_server_version(version_obj: Any) -> tuple[int, int, int] | None:
    """Parse ``ocs.data.version.{major,minor,micro}`` into a tuple."""
    if not isinstance(version_obj, dict):
        return None
    try:
        return (
            int(version_obj["major"]),
            int(version_obj["minor"]),
            int(version_obj["micro"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _capabilities_origin(base_url: str) -> str:
    """Derive the server origin from a WebDAV base URL.

    Splits before ``/remote.php`` (the OCS capabilities endpoint lives at
    the server root, not under the DAV path); if ``/remote.php`` isn't
    present, the whole ``base_url`` is assumed to already be the origin.
    """
    stripped = base_url.rstrip("/")
    marker = "/remote.php"
    idx = stripped.find(marker)
    if idx == -1:
        return stripped
    return stripped[:idx]


async def check_version_floors(
    client, base_url: str, *, backend: str
) -> RoProbeResult:
    """Runtime-check the version floors that fixed the RO-bypass CVEs.

    docs/design/cloud_access_unification.md §3.3 (verbatim): "Version
    floors: Nextcloud server >= 28.0.3, groupfolders >= 20.1.2. The probe
    converts version-assumption risk into a runtime check."

    The spec defines floors **only for Nextcloud** — OpenCloud/oCIS has
    no equivalent CVE-fix floor documented here, so ``backend="opencloud"``
    always returns ``ok=True`` with no failures; this function is a no-op
    gate for that backend, not a claim that OpenCloud has been verified
    against anything.

    For ``backend="nextcloud"``: GETs
    ``{origin}/ocs/v2.php/cloud/capabilities?format=json`` (origin derived
    by ``_capabilities_origin``) with ``OCS-APIRequest: true``, and parses
    ``ocs.data.version.{major,minor,micro}`` for the server floor and
    ``ocs.data.capabilities.groupfolders`` for the groupfolders floor.
    Fail-closed throughout: a transport error, a non-2xx response, an
    unparseable JSON body, an unparseable server version, or an
    unparseable/absent groupfolders version are all failures — "unknown"
    is never treated as "ok". An absent/unparseable groupfolders version
    specifically records
    ``"groupfolders version unverifiable via capabilities (fail-closed)"``.
    """
    if backend != "nextcloud":
        return RoProbeResult(ok=True, failures=[])

    url = f"{_capabilities_origin(base_url)}/ocs/v2.php/cloud/capabilities?format=json"

    try:
        resp = await client.request("GET", url, headers={"OCS-APIRequest": "true"})
    except Exception as e:
        return RoProbeResult(
            ok=False,
            failures=[
                f"capabilities -> transport error {type(e).__name__} (fail-closed)"
            ],
        )

    if not (200 <= resp.status_code < 300):
        return RoProbeResult(
            ok=False,
            failures=[f"capabilities -> {resp.status_code} (expected 2xx)"],
        )

    try:
        data = resp.json()
    except Exception as e:
        return RoProbeResult(
            ok=False,
            failures=[
                "capabilities response unparseable as JSON "
                f"({type(e).__name__}) (fail-closed)"
            ],
        )

    failures: list[str] = []

    try:
        ocs_data = data["ocs"]["data"]
    except (KeyError, TypeError):
        ocs_data = {}

    server_tuple = _parse_server_version(ocs_data.get("version"))
    if server_tuple is None:
        failures.append(
            "nextcloud server version unparseable via capabilities (fail-closed)"
        )
    else:
        floor = VERSION_FLOORS["nextcloud"]
        if server_tuple < floor:
            failures.append(
                f"nextcloud server {'.'.join(map(str, server_tuple))} below "
                f"floor {'.'.join(map(str, floor))} (GHSA-5mq8-738w-5942)"
            )

    caps = ocs_data.get("capabilities")
    groupfolders_value = caps.get("groupfolders") if isinstance(caps, dict) else None
    groupfolders_tuple = (
        _parse_version_tuple(groupfolders_value)
        if groupfolders_value is not None
        else None
    )
    if groupfolders_tuple is None:
        failures.append(
            "groupfolders version unverifiable via capabilities (fail-closed)"
        )
    else:
        floor = VERSION_FLOORS["groupfolders"]
        if groupfolders_tuple < floor:
            failures.append(
                f"groupfolders {'.'.join(map(str, groupfolders_tuple))} below "
                f"floor {'.'.join(map(str, floor))} (GHSA-2vrq-fhmf-c49m)"
            )

    return RoProbeResult(ok=not failures, failures=failures)
