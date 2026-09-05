"""Fail-closed read-only verification for a protected-mount identity.

Given credentials for the dedicated RO account (NOT agent-service — see
knowledge-base/knowledge/design/cloud_access_unification.md §3.3), attempt every mutating
WebDAV verb AND the version/trash side channels that had real RO-bypass
CVEs (Nextcloud GHSA-5mq8-738w-5942 / GHSA-2vrq-fhmf-c49m). Protected
cloud mode must refuse to engage unless every one is rejected.

This module is a permanent Phase-1 artifact (not spike-only code), so it
is held to fail-closed standards throughout:

* **A positive read control gates every verdict** (whole-branch-review
  Finding 1). Before any mutating verb is probed, ``probe_read_only``
  issues an authenticated ``PROPFIND`` with ``Depth: 0`` against the
  target and requires a 2xx (200/207) response. Without this, a
  wrong/expired RO credential 401s on every mutating verb too — every
  verb comes back "rejected", ``ok`` reads True, and the probe has
  verified nothing (it never proved the credential could even
  authenticate, let alone read). A non-2xx or transport error on this
  check is appended to ``failures``, the same fail-closed bucket as any
  other unrecoverable check. The rejection set for the verb/side-channel
  loops is ``{403, 405}`` only — **401 is deliberately excluded**: once
  the read control has proven the credential live, a genuine RO identity
  should only ever see 403/405 on a write attempt, so a 401 at that point
  is anomalous and must fail closed rather than count as "rejected".
* **Transport errors fail closed.** A timeout or connection error while
  probing a verb is recorded as a failure, not swallowed — an
  unreachable server must never be read as "read-only verified".
* **Version floors are enforced, not assumed.** ``check_version_floors``
  turns the CVE-fix versions from the spec (Nextcloud server >= 28.0.3,
  groupfolders per-branch >= ``GROUPFOLDERS_PATCHED``) into a runtime
  capabilities check instead of a documentation-only assumption.
* **Side channels are real requests, not decoys.** ``side_channel_probes``
  builds the actual DAV MOVE requests a real restore/finalize operation
  would issue (dedicated ``versions``/``trashbin``/``uploads`` namespaces),
  not a second POST to the same generic folder URL every other verb
  already exercises — the latter can 405 for reasons that have nothing to
  do with restore-permission enforcement, giving false assurance against
  the very CVE class this module exists to catch. Each side channel aims
  at the PROTECTED mount (or verifies its effect there): the
  uploads-finalize ``Destination`` lands inside the mount, and
  trash-restore checks the item stayed trashed. A write that only reaches
  the reader's own home/namespace is a legitimate reader capability, not a
  bypass, and must not be probed as one (live tuning 2026-07-12).

Gate semantics — ``RoProbeResult.ok`` is the engage gate, strictly. It
is True only when *every* check was attempted AND verified rejected:
no ``failures``, no ``skipped``, no ``inconclusive`` (spec §3.3
verbatim: the side channels "all must 403/405 or protected mode refuses
to engage"). The three lists distinguish *why* a refusal happened and
how it is cured:

* ``failures`` — a write path is demonstrably open, or a check could
  not be trusted (transport error, unparseable response). Not curable
  by configuration; the deployment is unsafe for protected mode.
* ``skipped`` — the side channels were never attempted because
  ``dav_root``/``username`` weren't supplied to ``probe_read_only``.
  Curable: supply both.
* ``inconclusive`` — a side channel came back 404/409. ``side_channel_
  probes``/``probe_read_only`` accept ``version_ref``/``trash_ref``
  kwargs; when supplied (the orchestrator-side write identity's
  ``seed_canary_fixture`` discovers them — see ``nextcloud.py``) the
  versions-restore/trash-restore probes target those REAL ids instead of
  the ``_SYNTHETIC_*`` placeholders, turning a 404 into a verified ``403``
  rejection. When a ref is ``None`` (not supplied, or the server exposed
  no real id for this canary) that probe falls back to the synthetic
  placeholder and stays inconclusive — fail-closed, curable by supplying
  the ref and/or the design's §6.4 live-probe run
  (knowledge-base/knowledge/design/cloud_access_unification.md §6 item 4 — "Live RO probe")
  tuning the status map from observed behavior. The uploads-finalize
  side channel is different: it can never be cured by an injectable ref
  (the URL is reader-namespaced), so ``probe_read_only`` instead
  self-provisions a REAL reader-owned upload session via ``MKCOL`` before
  probing it; only an MKCOL failure leaves it on the synthetic id
  (still inconclusive, fail-closed).

Phase 1 must call BOTH ``check_version_floors`` and ``probe_read_only``
and require ``ok`` from each — passing one gate does not imply the
other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

# Whole-branch-review Finding 1: 401 was dropped from this set. Without a
# positive read control, a wrong/expired RO credential 401s on every
# mutating verb too, so the old {401, 403, 405} treated "the credential
# doesn't even work" as "verified read-only". Once ``probe_read_only``'s
# positive read control has proven the credential is LIVE and can READ
# the target (see below), a genuine RO identity should only ever see
# 403/405 on a mutation attempt — a 401 at that point is anomalous
# (unauthenticated on a write, after successfully authenticating on a
# read moments earlier) and must fail closed, not count as rejected.
REJECTED_STATUSES = frozenset({403, 405})

# Status codes for side-channel probes that mean "the request didn't reach
# far enough to prove anything" (synthetic ids are expected to 404/409 on a
# server that never heard of them) — recorded in ``inconclusive``, which
# refuses the engage gate like everything else that isn't a verified
# rejection (see the module docstring for the cure).
_INCONCLUSIVE_STATUSES = frozenset({404, 409})

# Version floors from knowledge-base/knowledge/design/cloud_access_unification.md §3.3: the
# releases that fixed the RO-bypass CVEs this module probes for.
#   nextcloud    >= 28.0.3  (GHSA-5mq8-738w-5942, versions-restore)
#   groupfolders per-branch (GHSA-2vrq-fhmf-c49m, trash-restore) — see
#   GROUPFOLDERS_PATCHED below.
VERSION_FLOORS: dict[str, tuple[int, int, int]] = {
    "nextcloud": (28, 0, 3),
}

# groupfolders ships one release branch per Nextcloud major (18.x -> NC 30,
# 19.x -> NC 31, 20.x -> NC 32, ...; each branch's info.xml pins
# min-version = max-version), and GHSA-2vrq-fhmf-c49m was fixed in EVERY
# maintained branch, not only 20.x. A single (20, 1, 2) floor is therefore
# branch-blind: NC 31 can never run groupfolders 20.x, so a fully patched
# 19.1.x install would be refused forever (caught live on k3d NC 31.0.14 /
# groupfolders 19.1.18, 2026-07-12). First patched release per branch,
# verbatim from the advisory's patched-versions list. Branches NEWER than
# the table were created after the fix and carry it from their first
# release; branches OLDER than the table have no known-safe release and
# refuse (fail-closed).
GROUPFOLDERS_PATCHED: dict[int, tuple[int, int, int]] = {
    14: (14, 0, 11),
    15: (15, 3, 12),
    16: (16, 0, 15),
    17: (17, 0, 14),
    18: (18, 1, 8),
    19: (19, 1, 8),
    20: (20, 1, 2),
}


def groupfolders_floor(major: int) -> tuple[int, int, int] | None:
    """CVE floor for the groupfolders release branch ``major``.

    Returns the first GHSA-2vrq-fhmf-c49m-patched release of that branch,
    ``(major, 0, 0)`` for branches born after the fix, or ``None`` for
    branches older than the advisory's patched set — there is no known-safe
    release on those, so the caller must refuse.
    """
    if major in GROUPFOLDERS_PATCHED:
        return GROUPFOLDERS_PATCHED[major]
    if major > max(GROUPFOLDERS_PATCHED):
        return (major, 0, 0)
    return None


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
# Filename the uploads-finalize probe tries to land INSIDE the protected
# mount. The whole point of the CVE class is a write reaching the RO folder,
# so the finalize Destination must be inside the mount — never the reader's
# own home (a reader can always write its own home, which would false-positive
# the probe; proven live on NC 31, 2026-07-12).
_MOUNT_FINALIZE_TARGET = "srw-ro-probe-finalize.txt"


def side_channel_probes(
    dav_root: str,
    username: str,
    *,
    version_ref: str | None = None,
    trash_ref: str | None = None,
    mount_url: str | None = None,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Build the real restore/finalize requests for the RO-bypass CVE class.

    Returns a list of ``(verb, note, request_kwargs)`` where
    ``request_kwargs`` always has a ``"url"`` key plus any ``"headers"``
    needed, ready to be passed to ``client.request(verb, **request_kwargs)``
    (after popping ``"url"`` for the positional slot — see
    ``probe_read_only``).

    ``version_ref`` (shape ``"{fileid}/{versionid}"``) and ``trash_ref``
    (shape ``"{trashed_item_name}"``), when supplied, replace the
    ``_SYNTHETIC_FILEID``/``_SYNTHETIC_VERSIONID`` and
    ``_SYNTHETIC_TRASH_ITEM`` placeholders in the versions-restore and
    trash-restore requests (``trash_ref`` replaces the placeholder in BOTH
    the URL and the ``Destination`` header) with ids the server actually
    knows — turning a 404 ``inconclusive`` into a verified rejection on a
    correctly-RO reader. ``None`` (the default) keeps the synthetic
    placeholder, so that side channel stays inconclusive, fail-closed.

    ``mount_url`` is the reader's files-namespace URL for the PROTECTED
    mount (``{origin}/remote.php/dav/files/{reader}/{mount}``). The CVE
    class is a write that reaches the read-only folder, so the
    uploads-finalize ``Destination`` MUST land inside the mount — NOT the
    reader's own home. A reader is a real Nextcloud user who can always
    write its own home, so a home-targeted finalize returns 201/204 and
    false-positives the gate (proven live on NC 31, 2026-07-12: home
    Destination → 201, mount Destination → 403). When ``mount_url`` is
    None (the label-only "skipped" path), the finalize falls back to a
    home Destination — harmless there because the request is never sent.

    Three real operations against the protected mount / dedicated DAV
    namespaces:

    * **versions-restore** (GHSA-5mq8-738w-5942): ``MOVE`` a version into
      the restore target, under the dedicated ``versions`` DAV namespace.
    * **trash-restore** (GHSA-2vrq-fhmf-c49m): ``MOVE`` a trashed item
      back into the reader's ``trashbin`` restore endpoint — for a
      groupfolder member this exercises the team-folder trash-restore the
      CVE covers. Nextcloud signals an RO denial here with a 500, not a
      clean 403, so ``probe_read_only`` verifies the *effect* (the item
      stays trashed), not the status code alone.
    * **chunked-upload finalize** (the spec's "chunked-upload/TUS
      finalize"): ``MOVE`` an in-progress upload chunk collection's
      ``.file`` marker — assembled in the reader's own ``uploads``
      namespace, a legitimate reader capability — with a ``Destination``
      INSIDE the protected mount. This is the only step of a chunked/TUS
      upload that touches the mount, so it is THE test for that write
      path; a standalone "start a TUS session" POST is not (it only ever
      hits the reader's own namespace and can never demonstrate a mount
      bypass, so it was removed after the live run — 2026-07-12).

    Synthetic ids (see the ``_SYNTHETIC_*`` constants) stand in for real
    file/version/transfer ids so no real cloud state needs to exist for
    the probe to run.
    """
    root = dav_root.rstrip("/")
    version_segment = (
        version_ref
        if version_ref is not None
        else f"{_SYNTHETIC_FILEID}/{_SYNTHETIC_VERSIONID}"
    )
    trash_segment = trash_ref if trash_ref is not None else _SYNTHETIC_TRASH_ITEM
    finalize_dest = (
        f"{mount_url.rstrip('/')}/{_MOUNT_FINALIZE_TARGET}"
        if mount_url
        else f"{root}/files/{username}/{_MOUNT_FINALIZE_TARGET}"
    )
    return [
        (
            "MOVE",
            "versions-restore",
            {
                "url": f"{root}/versions/{username}/versions/{version_segment}",
                "headers": {
                    "Destination": f"{root}/versions/{username}/restore/target"
                },
            },
        ),
        (
            "MOVE",
            "trash-restore",
            {
                "url": f"{root}/trashbin/{username}/trash/{trash_segment}",
                "headers": {
                    "Destination": (
                        f"{root}/trashbin/{username}/restore/{trash_segment}"
                    )
                },
            },
        ),
        (
            "MOVE",
            "uploads-finalize",
            {
                "url": f"{root}/uploads/{username}/{_SYNTHETIC_TRANSFER_ID}/.file",
                "headers": {"Destination": finalize_dest},
            },
        ),
    ]


@dataclass
class RoProbeResult:
    """Outcome of one probe run. ``ok`` is the engage gate, strictly.

    ``ok`` is True only when ``failures``, ``skipped``, AND
    ``inconclusive`` are all empty — every check attempted, every check
    verified rejected. It is derived from the three lists (a property,
    so it cannot drift out of sync with them); see the module docstring
    for what each list means and how its refusal is cured.
    """

    # A write path is demonstrably open, or a check couldn't be trusted
    # (transport error, unparseable response). Not curable by config.
    failures: list[str] = field(default_factory=list)
    # 404/409 on a side channel: the request round-tripped but synthetic
    # ids mean we can't tell whether the authz layer was even reached.
    # Refuses the gate; cure = real fixture ids / §6.4 live-run tuning.
    inconclusive: list[str] = field(default_factory=list)
    # Side channels never attempted (``dav_root``/``username`` not
    # supplied to ``probe_read_only``). Refuses the gate; cure = supply
    # both.
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.failures or self.skipped or self.inconclusive)


async def _trash_item_still_present(
    client, dav_root: str, username: str, item_name: str
) -> bool:
    """Whether ``item_name`` is still in ``username``'s trashbin.

    Used by the trash-restore side channel to verify a restore had NO
    effect (the item stayed trashed) rather than trusting Nextcloud's
    status code, which is a 500 — not a clean 403 — when an RO groupfolder
    member is denied. Fail-closed: any error, a non-207 response, or an
    unparseable body returns False, so the caller records a failure and the
    gate refuses rather than reading an unverifiable state as "rejected".
    """
    url = f"{dav_root.rstrip('/')}/trashbin/{username}/trash"
    try:
        resp = await client.request("PROPFIND", url, headers={"Depth": "1"})
    except Exception:
        return False
    if getattr(resp, "status_code", None) != 207:
        return False
    hrefs = re.findall(
        r"<d:href>([^<]+)</d:href>", getattr(resp, "text", ""), flags=re.IGNORECASE
    )
    leaves = {unquote(h.rstrip("/").split("/")[-1]) for h in hrefs}
    return item_name in leaves


async def probe_read_only(
    client,
    base_url: str,
    path: str,
    *,
    dav_root: str | None = None,
    username: str | None = None,
    version_ref: str | None = None,
    trash_ref: str | None = None,
) -> RoProbeResult:
    """Verify a mount identity is read-only against ``base_url``+``path``.

    Whole-branch-review Finding 1: before probing a single mutating verb,
    run a POSITIVE READ CONTROL — an authenticated ``PROPFIND`` with
    ``Depth: 0`` on the target path, requiring a 2xx (200/207) response.
    This proves the credential is LIVE and can actually READ the target.
    Without it, a wrong/expired RO credential 401s on every mutating verb
    too, and every verb would come back "rejected" while the probe had
    authenticated nothing — "read-only verified" for a credential that
    cannot read at all. A read-control failure (non-2xx, or a transport
    error) is appended to ``failures`` — not a separate list — so the
    existing ``RoProbeResult.ok`` property already refuses the gate on it;
    see the module docstring for why ``failures`` (not curable by config)
    is the right bucket.

    The read control runs FIRST but does not short-circuit: the verb loop
    and side-channel probes still run afterward regardless of its outcome,
    and everything aggregates into one ``RoProbeResult``.

    Rejection set: ``REJECTED_STATUSES = {403, 405}`` (401 excluded — see
    the constant's comment). A verb/side-channel response of 401 therefore
    now falls through to the generic "write path open" failure branch,
    same as any other unexpected status; it is never classified as
    ``inconclusive`` (401 is not in ``_INCONCLUSIVE_STATUSES``) or silently
    dropped.

    ``version_ref``/``trash_ref`` are passed straight through to
    ``side_channel_probes`` (real ids in place of the synthetic
    placeholders — see that function's docstring). Separately, when
    ``dav_root``/``username`` are supplied, this function first attempts to
    self-provision a REAL reader-owned upload session (``MKCOL
    {dav_root}/uploads/{username}/srw-ro-probe``) before probing
    uploads-finalize: a 201 retargets that probe at the real session so a
    403/405 is a verified rejection instead of a synthetic-id 404; any
    other MKCOL outcome (including a transport error, which is NOT itself
    recorded as a probe failure — it's setup, not a verb probe) falls back
    to the synthetic transfer id, same as before. The provisioned session
    is best-effort deleted after the side-channel loop.
    """
    target = base_url.rstrip("/") + "/" + path.lstrip("/")
    failures: list[str] = []
    inconclusive: list[str] = []
    skipped: list[str] = []

    try:
        read_resp = await client.request("PROPFIND", target, headers={"Depth": "0"})
    except Exception as e:
        failures.append(
            "read control failed: PROPFIND Depth:0 -> transport error "
            f"{type(e).__name__} (credential not live/authorized; cannot "
            "verify read-only)"
        )
    else:
        if not (200 <= read_resp.status_code < 300):
            failures.append(
                "read control failed: PROPFIND Depth:0 -> "
                f"{read_resp.status_code} (credential not live/authorized; "
                "cannot verify read-only)"
            )

    for verb, note, body in MUTATING_VERBS:
        label = verb if not note else f"{verb} ({note})"
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["content"] = body
        # MOVE and COPY are WebDAV write verbs that REQUIRE a Destination
        # header; without it Nextcloud returns 400 (malformed) before the
        # authz layer, masking the real 403 (live k3d validation 2026-07-09).
        if verb in ("MOVE", "COPY"):
            kwargs["headers"] = {"Destination": target + ".moved"}
        try:
            resp = await client.request(verb, target, **kwargs)
        except Exception as e:
            # A transport failure (timeout, connection error, ...) must
            # refuse, not crash the probe or silently skip the verb.
            # Continue probing the remaining verbs regardless.
            failures.append(
                f"{label} -> transport error {type(e).__name__} (fail-closed)"
            )
            continue
        if verb == "PROPPATCH" and resp.status_code == 207:
            # PROPPATCH ALWAYS returns a 207 multistatus envelope; the real
            # authz decision is the inner propstat status. A read-only reader
            # gets an inner 4xx (denied); a 2xx inner status means a property
            # was actually written — a real RO bypass (live k3d validation
            # 2026-07-09 saw inner 403 on a correctly-RO reader).
            if _proppatch_all_denied(getattr(resp, "text", "")):
                continue
            failures.append(
                f"{label} -> 207 with a non-4xx inner status (property write open)"
            )
            continue
        if resp.status_code not in REJECTED_STATUSES:
            failures.append(f"{label} -> {resp.status_code} (expected 403/405)")

    if dav_root is not None and username is not None:
        root = dav_root.rstrip("/")
        # Reuses _SYNTHETIC_TRANSFER_ID as the session name whether or not
        # MKCOL succeeds — the URL shape is identical either way; what
        # differs is whether a real session exists behind it (tracked by
        # `real_upload_session` below), not the name itself.
        upload_session_url = f"{root}/uploads/{username}/{_SYNTHETIC_TRANSFER_ID}"
        real_upload_session = False
        try:
            mkcol_resp = await client.request("MKCOL", upload_session_url)
        except Exception:
            # A transport error on this provisioning step is NOT a probe
            # result — it's setup, not a verb probe. Fall back to the
            # synthetic transfer id below (that side channel stays
            # inconclusive, fail-closed) rather than recording a failure.
            mkcol_resp = None
        if mkcol_resp is not None and mkcol_resp.status_code == 201:
            real_upload_session = True

        probes = side_channel_probes(
            dav_root,
            username,
            version_ref=version_ref,
            trash_ref=trash_ref,
            mount_url=base_url,
        )
        if real_upload_session:
            # A real reader-owned session exists — retarget uploads-finalize's
            # SOURCE at it so a 403/405 is a verified rejection, not a
            # synthetic-id 404. MKCOL in one's own uploads namespace is a
            # legitimate reader capability, not a folder write, so this
            # doesn't contaminate the write-verb probes above. The
            # Destination stays INSIDE the protected mount (from
            # side_channel_probes(mount_url=...)) — that is the byte that
            # must be denied; a reader's own home would accept it (201) and
            # false-positive the gate.
            probes = [
                (verb, note, req)
                if not (verb == "MOVE" and note == "uploads-finalize")
                else (
                    "MOVE",
                    "uploads-finalize",
                    {
                        "url": f"{upload_session_url}/.file",
                        "headers": {
                            "Destination": (
                                f"{base_url.rstrip('/')}/{_MOUNT_FINALIZE_TARGET}"
                            )
                        },
                    },
                )
                for verb, note, req in probes
            ]

        for verb, note, req in probes:
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
            if (
                note == "trash-restore"
                and trash_ref is not None
                and status not in _INCONCLUSIVE_STATUSES
            ):
                # Nextcloud does NOT return a clean 403 when an RO groupfolder
                # member is denied a trash restore — it 500s (proven live on
                # NC 31, 2026-07-12: agent restore -> 201, reader -> 500, and
                # the file STAYS trashed). Trust the effect, not the status:
                # if the item is still in the reader's trashbin, the restore
                # had no effect => RO held. Only a restore that actually moved
                # the item out of trash (incl. any 2xx) is a real bypass. The
                # effect check fails closed — an unverifiable trashbin read is
                # treated as "not still present" => recorded as a failure.
                if await _trash_item_still_present(client, root, username, trash_ref):
                    continue
                failures.append(
                    f"{label} -> {status} (item no longer in trash; restore "
                    "may have succeeded — write path open)"
                )
                continue
            if status in _INCONCLUSIVE_STATUSES:
                inconclusive.append(
                    f"{label} -> {status} (id not recognized by the server; "
                    "authz layer may not have been reached — inconclusive, "
                    "not verified rejected)"
                )
                continue
            # 401 falls through to here too (no longer in REJECTED_STATUSES
            # or _INCONCLUSIVE_STATUSES): a mutating side channel 401'ing
            # against a credential the read control just proved live is
            # anomalous, and must fail closed rather than land in
            # `inconclusive` or go unclassified.
            failures.append(f"{label} -> {status} (expected 403/405; write path open)")

        if real_upload_session:
            # Best-effort cleanup of the session this probe provisioned;
            # errors here are swallowed (not a probe result, and the probe's
            # verdict is already fully determined by the loop above).
            try:
                await client.request("DELETE", upload_session_url)
            except Exception:
                pass
    else:
        # dav_root/username weren't supplied — the side channels were
        # never attempted, so the gate refuses until they are (see
        # module docstring; the refusal is curable, not an error).
        for verb, note, _req in side_channel_probes(
            dav_root or "<unset>", username or "<unset>"
        ):
            skipped.append(
                f"{verb} ({note}) -> skipped (dav_root/username not supplied; "
                "side channels unverified)"
            )

    return RoProbeResult(
        failures=failures,
        inconclusive=inconclusive,
        skipped=skipped,
    )


_PROPSTAT_STATUS_RE = re.compile(r"HTTP/\d\.\d\s+(\d{3})", re.IGNORECASE)


def _proppatch_all_denied(body: str) -> bool:
    """Whether a PROPPATCH 207 multistatus body denied every property.

    PROPPATCH returns a 207 envelope wrapping one ``<d:status>HTTP/1.1 NNN``
    per property. Read-only => every inner status is 4xx. Returns True only
    when at least one inner status was found AND all are 4xx; an empty/
    unparseable body returns False (fail-closed — the write can't be
    verified as rejected).
    """
    codes = [int(c) for c in _PROPSTAT_STATUS_RE.findall(body or "")]
    if not codes:
        return False
    return all(400 <= c < 500 for c in codes)


def _parse_version_tuple(value: Any) -> tuple[int, int, int] | None:
    """Parse a dotted version out of a capabilities value.

    Defensive by design (module docstring / spec note): the value may be
    a plain ``"20.1.2"`` string, or a dict — different Nextcloud versions
    expose the groupfolders app version under ``capabilities.groupfolders``
    inconsistently: real NC 31 uses ``{"appVersion": "20.1.2", ...}`` (live
    k3d validation 2026-07-09), older shapes used ``{"version": "..."}``.
    Both keys are honored. Anything that doesn't cleanly parse to three ints
    returns ``None`` (caller treats that as fail-closed "unverifiable").
    """
    if isinstance(value, dict):
        value = value.get("version") or value.get("appVersion")
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


async def check_version_floors(client, base_url: str, *, backend: str) -> RoProbeResult:
    """Runtime-check the version floors that fixed the RO-bypass CVEs.

    knowledge-base/knowledge/design/cloud_access_unification.md §3.3: Nextcloud server
    >= 28.0.3; groupfolders >= the first patched release of its OWN
    branch (``GROUPFOLDERS_PATCHED`` — the advisory fixed every
    maintained branch, and each branch is pinned to one NC major). "The
    probe converts version-assumption risk into a runtime check."

    Backend allowlist, fail-closed: ``"nextcloud"`` runs the floor
    checks below; ``"opencloud"`` returns ok with no failures — the spec
    defines floors **only for Nextcloud**, OpenCloud/oCIS has no
    equivalent CVE-fix floor documented here, so this is a documented
    no-op gate for that backend, not a claim that OpenCloud has been
    verified against anything. ANY other value (typo, wrong case,
    future backend) refuses with ``"unknown backend ... (fail-closed)"``
    rather than silently passing. Note this gate checks versions only —
    Phase 1 must ALSO call ``probe_read_only``; passing one gate does
    not imply the other.

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
    if backend == "opencloud":
        return RoProbeResult()
    if backend != "nextcloud":
        # Allowlist: an unrecognized backend value must refuse, not
        # silently skip the floors (fail-open would defeat the gate).
        return RoProbeResult(failures=[f"unknown backend {backend!r} (fail-closed)"])

    url = f"{_capabilities_origin(base_url)}/ocs/v2.php/cloud/capabilities?format=json"

    try:
        resp = await client.request("GET", url, headers={"OCS-APIRequest": "true"})
    except Exception as e:
        return RoProbeResult(
            failures=[
                f"capabilities -> transport error {type(e).__name__} (fail-closed)"
            ],
        )

    if not (200 <= resp.status_code < 300):
        return RoProbeResult(
            failures=[f"capabilities -> {resp.status_code} (expected 2xx)"],
        )

    try:
        data = resp.json()
    except Exception as e:
        return RoProbeResult(
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
        floor = groupfolders_floor(groupfolders_tuple[0])
        if floor is None:
            failures.append(
                f"groupfolders branch {groupfolders_tuple[0]}.x has no "
                "GHSA-2vrq-fhmf-c49m-patched release (fail-closed)"
            )
        elif groupfolders_tuple < floor:
            failures.append(
                f"groupfolders {'.'.join(map(str, groupfolders_tuple))} below "
                f"branch floor {'.'.join(map(str, floor))} (GHSA-2vrq-fhmf-c49m)"
            )

    return RoProbeResult(failures=failures)
