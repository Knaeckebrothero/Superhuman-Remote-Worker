"""Reconcile orphaned protected-mode RO grants (design §8.1.4).

Revoke-on-teardown can be skipped by a crash, a killed pod, or a lost teardown
path — so a leader-gated sweep independently revokes any active cloud_ro_mounts
grant whose thread is gone or ended, and marks the row revoked. Modeled on the
existing periodic reconcilers (lifecycle/reconciler, project_loop_sweeper). The
leader-gated periodic wrapper lives in ``orchestrator/main.py`` so it can close
over the ``postgres_db`` / ``main_cloud_router`` module globals.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_DEAD_THREAD_STATUSES = {"ended", "deleted"}


async def reconcile_orphaned_ro_mounts(*, postgres_db, router) -> int:
    """Revoke every active RO grant whose thread is gone/ended. Returns the
    number of grants revoked. Best-effort per row — one failure does not abort
    the sweep."""
    revoked = 0
    for row in await postgres_db.list_active_ro_mounts():
        thread = await postgres_db.get_thread(row["thread_id"])
        is_orphan = thread is None or thread.get("status") in _DEAD_THREAD_STATUSES
        if not is_orphan:
            continue
        try:
            data = json.loads(row["grant_handle"])
            user_key = _user_key_from_grant(data)
            backend = router.for_backend(row["backend"])
            await backend.revoke_ro_grant(row["grant_handle"], user_key=user_key)
            await postgres_db.mark_ro_mount_revoked(row["id"])
            revoked += 1
        except Exception:
            logger.exception("failed to revoke orphaned RO mount %s", row["id"])
    if revoked:
        logger.info("RO reader reconciler revoked %d orphaned grant(s)", revoked)
    return revoked


def _user_key_from_grant(data: dict) -> str:
    """Recover the reader's user_key. Newer grants carry it explicitly; older
    ones only carry ``reader_id`` (``srw-reader-<user_key>``) — strip the prefix.
    (``revoke_ro_grant`` ignores ``user_key`` on both backends, so a best-effort
    value is sufficient for logging/compatibility.)"""
    if data.get("user_key"):
        return str(data["user_key"])
    reader_id = data.get("reader_id", "")
    return reader_id.removeprefix("srw-reader-")
