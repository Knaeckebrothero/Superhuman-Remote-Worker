"""Fail-closed RO engage gate for protected cloud mode (design §3.3, §11.4).

Provisions the per-user reader + per-mount grant, then verifies — as the
reader identity — that the mount is genuinely read-only before it may be used:
version floors (CVE side-channel patches) AND a live probe of every mutating
verb, with a positive read control so a dead credential cannot pass by 401ing
everywhere. On ANY failure it revokes the grant and raises RoEngageRefused;
only an ``ok`` probe persists the cloud_ro_mounts row.

This is the first (and only) caller of ``ro_probe`` — the module shipped in
Phase 0 with unit coverage but no wiring. The canary side channels still target
synthetic ids until the §11.4 live-validation step supplies real version/trash
ids, so a real run today lands ``inconclusive`` and — correctly — REFUSES.
"""
from __future__ import annotations

import logging

from . import ro_probe
from .base import RoReaderGrant

logger = logging.getLogger(__name__)


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

    ``http_client_factory(credentials)`` returns an httpx-like client
    authenticated AS THE READER (so the probe exercises the reader's real
    rights). On success the ``cloud_ro_mounts`` row is persisted and the grant
    returned; on any failure the grant is revoked and ``RoEngageRefused`` is
    raised.
    """
    await backend.ensure_ro_reader(user_key=user_key)
    grant = await backend.mint_ro_grant(handle, user_key=user_key, grant_key=thread_id)
    canary = None
    try:
        canary = await backend.seed_canary_fixture(handle)
        # Probe AS THE READER using its freshly minted credential.
        client = http_client_factory(grant.credentials)

        floors = await ro_probe.check_version_floors(
            client, grant.webdav_url, backend=backend.backend_id
        )
        if not floors.ok:
            raise RoEngageRefused(
                f"version floor check failed: "
                f"{floors.failures or floors.inconclusive}"
            )

        # probe_read_only(client, base_url, path, *, dav_root=None, username=None)
        # — `path` is the positional target (the canary file); the reader's
        # WebDAV URL is both base_url and dav_root (ro_probe.py:240).
        result = await ro_probe.probe_read_only(
            client,
            grant.webdav_url,
            canary.path,
            dav_root=grant.webdav_url,
            username=grant.reader_id,
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
        return grant
    except Exception:
        # Fail closed: roll the grant back so no partial RO access lingers.
        try:
            await backend.revoke_ro_grant(grant.grant_handle, user_key=user_key)
        except Exception:  # pragma: no cover - best effort
            logger.exception("failed to revoke RO grant during engage rollback")
        raise
    finally:
        if canary is not None:
            try:
                await backend.remove_canary_fixture(handle, canary)
            except Exception:  # pragma: no cover
                logger.exception("failed to remove canary fixture")
