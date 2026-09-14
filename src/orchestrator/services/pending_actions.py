"""Pending-action counts for the cockpit's badge, cached 5 s per caller.

Extracted verbatim from ``orchestrator.main`` (R1.B07 lane M, census group
``J_messaging``). **P4e** — pre-fix this was anonymous and returned global
counts AND the most-urgent sudo's command string; the caller must now be
approved and a non-admin sees only their own / project-member jobs.

The cache is a parameter, not module state of this operation: the application
constructs the dict and hands it over on :class:`PendingActionsDependencies`,
so a test supplies its own instead of reaching for a global, and the operation
itself keeps nothing between calls. The key is the caller's user id (or
``"__admin__"`` for the unfiltered admin path), which is what keeps one user's
counts out of another's slot.

Stated precisely so it is not read as more than it is: `orchestrator.main`
still holds the one dict its application uses, exactly as it did before. Making
two applications in one process hold two caches would mean resolving it from
``request.app.state`` in the router, which this batch did not do — the store is
resolved through the application's factory for the same reason every other B07
router is, so that a suite rebinding ``postgres_db`` on ``main`` still steers
the read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


@dataclass
class PendingActionsDependencies:
    """The job store and the application's own 5 s count cache."""

    store: Any
    cache: dict[str, dict[str, Any]]


# Cache key is the caller's user id (or "__admin__" for the unfiltered admin
# path). 5s TTL keeps the cockpit's polling cheap without leaking other
# users' counts across cache slots.
async def get_pending_actions(
    request: Request,
    *,
    dependencies: PendingActionsDependencies,
    caller: dict[str, Any],
) -> dict[str, Any]:
    """Get counts of pending actions visible to the caller. Cached 5s per user.

    **P4e** — pre-fix this was anonymous and returned global counts AND the
    most-urgent sudo's command string. Now caller must be approved, and
    non-admins see only their own / project-member jobs.
    """
    import time

    is_admin = bool(caller.get("is_admin"))
    cache_key = "__admin__" if is_admin else str(caller["id"])

    now = time.monotonic()
    cached = dependencies.cache.get(cache_key)
    if cached and now < cached["expires_at"]:
        return cached["data"]

    try:
        if is_admin:
            data = await dependencies.store.get_pending_action_counts()
        else:
            projects = await dependencies.store.get_projects_for_user(str(caller["id"]))
            project_ids = [str(p["id"]) for p in projects]
            data = await dependencies.store.get_pending_action_counts(
                owner_user_id=str(caller["id"]),
                visible_project_ids=project_ids,
            )
        dependencies.cache[cache_key] = {
            "data": data,
            "expires_at": now + 5.0,
        }
        return data
    except Exception as e:
        logger.exception(f"Failed to get pending action counts: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


__all__ = ["PendingActionsDependencies", "get_pending_actions"]
