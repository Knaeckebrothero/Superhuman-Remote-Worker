"""Fail-closed RO engage gate for protected cloud mode (design §3.3, §11.4).

Provisions the per-user reader + per-mount grant, then verifies — as the
reader identity — that the mount is genuinely read-only before it may be used:
version floors (CVE side-channel patches) AND a live probe of every mutating
verb, with a positive read control so a dead credential cannot pass by 401ing
everywhere. On ANY failure it revokes the grant and raises RoEngageRefused;
only an ``ok`` probe persists the cloud_ro_mounts row.

This is the first (and only) caller of ``ro_probe``. The write identity's
``seed_canary_fixture`` (nextcloud.py) discovers real version/trash ids when
the server exposes them for the canary, and this module passes them through
to ``probe_read_only`` as ``version_ref``/``trash_ref`` — so the
versions-restore/trash-restore side channels target ids the server actually
knows, turning ``inconclusive`` into a verified ``403`` rejection on a
correctly-RO reader. The uploads-finalize side channel is cured differently
(it can't be by an injectable ref — its URL is reader-namespaced):
``probe_read_only`` self-provisions a REAL reader-owned upload session via
``MKCOL`` before probing it. Remaining inconclusives — the canary's server
exposed no version/trash id, or the uploads MKCOL failed — still correctly
REFUSE: fail-closed, not a config bug.
"""

from __future__ import annotations

import logging

from . import ro_probe
from .base import RoReaderGrant

logger = logging.getLogger(__name__)


def _dav_root_from_webdav_url(url: str) -> str:
    """Derive the true DAV root from a reader's files-namespace WebDAV URL.

    ``grant.webdav_url`` (Slice A ``mint_ro_grant``) is the files-namespace
    URL — ``{origin}/remote.php/dav/files/{reader}/{mount}/`` — which is NOT
    the root the versions/trashbin/uploads side channels live under (they
    hang directly off ``{origin}/remote.php/dav``, not under ``files/...``).
    Passing the files URL as ``dav_root`` builds nonsense side-channel URLs
    like ``.../files/<reader>/<mount>/versions/...``. Splitting at the
    ``/remote.php/dav`` marker and keeping the prefix up to and including it
    recovers the real root. If the marker is absent (unexpected URL shape),
    fall back to the url rstripped of a trailing slash — defensive, not a
    silent success path: a malformed root still only produces requests that
    fail closed (404/409 inconclusive, or a transport error) under the
    strict engage gate, never a false "verified rejected".
    """
    marker = "/remote.php/dav"
    idx = url.find(marker)
    if idx == -1:
        return url.rstrip("/")
    return url[: idx + len(marker)]


class RoEngageRefused(Exception):
    """The RO gate refused to engage — protected mode must NOT mount."""


async def engage_ro_mount(
    *,
    backend,
    handle,
    user_key: str,
    thread_id: str,
    user_id: str,
    postgres_db,
    http_client_factory,
) -> RoReaderGrant:
    """Provision + verify a read-only mount grant, fail-closed.

    ``http_client_factory(credentials, reader_id)`` returns an httpx-like
    client authenticated AS THE READER (so the probe exercises the reader's
    real rights) — ``reader_id`` is ``grant.reader_id`` itself, never
    re-derived by the caller. On success the ``cloud_ro_mounts`` row is
    persisted, the etag baseline (design §3.4) is captured and persisted
    against it, and the grant is returned; on any failure (including a
    baseline capture/persist failure — without one neither the staged-diff
    manifest nor the apply conflict gate can classify writes) the grant is
    revoked — along with the ``cloud_ro_mounts`` row, if it already
    persisted — and ``RoEngageRefused`` is raised.
    """
    await backend.ensure_ro_reader(user_key=user_key)
    grant = await backend.mint_ro_grant(handle, user_key=user_key, grant_key=thread_id)
    canary = None
    row_id: str | None = None  # set once persisted, so the rollback is row-aware
    try:
        canary = await backend.seed_canary_fixture(handle)
        # Probe AS THE READER using its freshly minted credential.
        client = http_client_factory(grant.credentials, grant.reader_id)

        floors = await ro_probe.check_version_floors(
            client, grant.webdav_url, backend=backend.backend_id
        )
        if not floors.ok:
            raise RoEngageRefused(
                f"version floor check failed: {floors.failures or floors.inconclusive}"
            )

        # probe_read_only(client, base_url, path, *, dav_root=None,
        # username=None, version_ref=None, trash_ref=None) — `path` is the
        # positional target (the canary file); `base_url` stays the
        # reader's files-namespace WebDAV URL (that's the mount being
        # verified read-only), but `dav_root` must be the TRUE DAV root
        # (versions/trashbin/uploads live under it, not under
        # `files/<reader>/<mount>/`) — see `_dav_root_from_webdav_url`.
        # The canary's discovered refs (None if the server exposed none)
        # pass straight through so the side channels target real ids.
        result = await ro_probe.probe_read_only(
            client,
            grant.webdav_url,
            canary.path,
            dav_root=_dav_root_from_webdav_url(grant.webdav_url),
            username=grant.reader_id,
            version_ref=canary.version_ref,
            trash_ref=canary.trash_ref,
        )
        if not result.ok:
            raise RoEngageRefused(
                "read-only probe did not pass: "
                f"failures={result.failures} inconclusive={result.inconclusive} "
                f"skipped={result.skipped}"
            )

        row_id = await postgres_db.create_ro_mount(
            thread_id=thread_id,
            user_id=user_id,
            backend=backend.backend_id,
            reader_id=grant.reader_id,
            grant_handle=grant.grant_handle,
            credentials=grant.credentials,
            webdav_url=grant.webdav_url,
            auth_kind=grant.auth_kind,
        )
        logger.info("RO mount engaged for thread %s (row %s)", thread_id, row_id)

        # Etag baseline (design §3.4): without it neither the staged-diff
        # manifest nor the apply conflict gate can classify writes, so a
        # capture failure here is itself an engage refusal — the outer
        # except below revokes the grant AND (row-aware, since the row has
        # already persisted at this point) marks the row revoked, just like
        # every other refusal in this function (no second cleanup path).
        try:
            baseline = await backend.capture_etag_baseline(handle)
        except Exception as e:
            raise RoEngageRefused(f"etag baseline capture failed: {e}") from e
        if not await postgres_db.update_ro_mount_baseline(row_id, baseline):
            # False = the row is no longer active (e.g. the reconciler
            # revoked it mid-engage) — the baseline did NOT persist, so
            # engage must not report success.
            raise RoEngageRefused(
                "etag baseline persist failed: cloud_ro_mounts row no longer active"
            )

        return grant
    except Exception:
        # Fail closed: roll the grant back so no partial RO access lingers —
        # and if the row already persisted (baseline-stage refusals fire
        # after create_ro_mount), mark it revoked too, so no status='active'
        # row with dead credentials and a NULL baseline survives.
        try:
            await backend.revoke_ro_grant(grant.grant_handle, user_key=user_key)
        except Exception:  # pragma: no cover - best effort
            logger.exception("failed to revoke RO grant during engage rollback")
        if row_id is not None:
            try:
                await postgres_db.mark_ro_mount_revoked(row_id)
            except Exception:  # pragma: no cover - best effort
                logger.exception(
                    "failed to mark RO mount row revoked during engage rollback"
                )
        raise
    finally:
        if canary is not None:
            try:
                await backend.remove_canary_fixture(handle, canary)
            except Exception:  # pragma: no cover
                logger.exception("failed to remove canary fixture")
