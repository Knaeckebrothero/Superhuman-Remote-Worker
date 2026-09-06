"""Cached cloud-identity resolution.

``resolve_user_identity`` against a main-cloud backend costs up to two
sequential user-search HTTP calls (~2.3 s measured on Nextcloud OCS), and its
result is a stable fact — the user's cloud account id. This module fronts it
with a per-backend cache persisted on ``users.cloud_identity`` (migration
0051) so the price is paid once per user per backend, not per page view.

Caching rules (knowledge-base/knowledge/issues/project_page_open_blocks_on_cloud_heal.md part 2):

* **Positive results persist indefinitely** — a cloud account id is stable.
  A backend switch is a cache miss (entries are keyed by ``backend_id``),
  never a wrong answer.
* **Negative results are never persisted** — "user hasn't logged into the
  cloud yet" is a valid transient state (see
  ``NextcloudBackend.resolve_user_identity``); the next caller retries.

The helpers take a plain user dict needing only ``id`` + ``email`` /
``display_name`` (a full ``users`` row or a ``project_members`` row both
qualify) and read the cache with a dedicated single-column query, so no
``SELECT`` list anywhere needs widening.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from orchestrator.services.cloud.base import MainCloudBackend
from orchestrator.services.cloud.handles import UserId

logger = logging.getLogger(__name__)


def _cache_entry(identity: dict[str, Any], backend_id: str) -> dict[str, Any]:
    entry = identity.get(backend_id)
    return entry if isinstance(entry, dict) else {}


async def resolve_user_identity_cached(
    db: Any,
    user: dict[str, Any],
    backend: MainCloudBackend,
) -> Optional[UserId]:
    """Resolve a user's cloud account id, cache-first.

    ``db`` is the app ``Database``; ``user`` needs ``id`` and, for the miss
    path, ``email`` / ``display_name``.
    """
    if not backend.is_initialized or not user.get("id"):
        return None
    user_id = str(user["id"])

    identity = await db.get_user_cloud_identity(user_id)
    cached = _cache_entry(identity, backend.backend_id).get("user_id")
    if cached:
        return UserId(cached)

    resolved = await backend.resolve_user_identity(
        user.get("email"), (user.get("display_name") or "").lower()
    )
    if resolved:
        await db.merge_user_cloud_identity(
            user_id,
            backend.backend_id,
            {
                "user_id": str(resolved),
                "resolved_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    return resolved


async def peek_home_browser_url(
    db: Any, user_id: str, backend_id: str
) -> Optional[str]:
    """Cache-only home-URL lookup — pure DB, never calls the backend."""
    identity = await db.get_user_cloud_identity(user_id)
    return _cache_entry(identity, backend_id).get("home_browser_url") or None


async def get_home_browser_url_cached(
    db: Any,
    user: dict[str, Any],
    backend: MainCloudBackend,
) -> Optional[str]:
    """Resolve a user's personal home-Space browser URL, cache-first.

    On a miss this blocks on the backend (identity resolution + drive
    lookup) — callers on a latency-sensitive path should use
    ``peek_home_browser_url`` and run this in the background instead.
    """
    if not backend.is_initialized or not user.get("id"):
        return None
    user_id = str(user["id"])

    cached = await peek_home_browser_url(db, user_id, backend.backend_id)
    if cached:
        return cached

    resolved = await resolve_user_identity_cached(db, user, backend)
    if not resolved:
        return None
    home = await backend.get_user_home(resolved)
    if not (home and home.browser_url):
        return None
    await db.merge_user_cloud_identity(
        user_id,
        backend.backend_id,
        {"home_browser_url": home.browser_url},
    )
    return home.browser_url
