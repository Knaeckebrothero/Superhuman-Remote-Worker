"""Agent-side cloud workspace sync.

One base class + one subclass per backend, mirroring the orchestrator-side
``MainCloudBackend`` pattern. The factory inspects the ``cloud_sync`` dict
returned by ``GET /api/agents/threads/{id}/workspace`` and builds the right
subclass; callers don't need to know which backend is active.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .base import SYNC_IGNORE_PATTERNS, WorkspaceSyncBase
from .nextcloud import NextcloudWorkspaceSync
from .opencloud import OpenCloudWorkspaceSync

if TYPE_CHECKING:
    from ...core.workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)


def build_workspace_sync(
    *,
    workspace_path: Path,
    cloud_cfg: Optional[dict[str, Any]],
    workspace_backend: Optional["WorkspaceBackend"] = None,
    poll_interval: int = 15,
) -> Optional[WorkspaceSyncBase]:
    """Construct the right sync implementation for a session, or None.

    ``cloud_cfg`` shape::

        {
          "backend": "nextcloud" | "opencloud",
          "webdav_url": str,            # full URL to the session folder
          "auth": {
            "type": "basic",
            "username": str, "password": str,
          } | {
            "type": "keycloak_client_credentials",
            "issuer": str, "client_id": str, "client_secret": str,
          },
        }

    Returns ``None`` for missing / unsupported configs; logs a warning in
    that case (never logging the auth payload).
    """
    if not cloud_cfg:
        return None
    backend = cloud_cfg.get("backend")
    webdav_url = cloud_cfg.get("webdav_url")
    auth = cloud_cfg.get("auth") or {}
    auth_type = auth.get("type")

    if not backend or not webdav_url:
        logger.warning(
            "cloud_sync: missing backend or webdav_url (got backend=%s)",
            backend,
        )
        return None

    if backend == "nextcloud" and auth_type == "basic":
        return NextcloudWorkspaceSync(
            workspace_path=workspace_path,
            webdav_url=webdav_url,
            webdav_user=auth["username"],
            webdav_password=auth["password"],
            poll_interval=poll_interval,
            workspace_backend=workspace_backend,
        )
    if backend == "opencloud" and auth_type == "keycloak_client_credentials":
        return OpenCloudWorkspaceSync(
            workspace_path=workspace_path,
            webdav_base_url=webdav_url,
            keycloak_issuer=auth["issuer"],
            client_id=auth["client_id"],
            client_secret=auth["client_secret"],
            poll_interval=poll_interval,
            workspace_backend=workspace_backend,
        )
    logger.warning(
        "cloud_sync: unsupported backend/auth combo (backend=%s auth.type=%s)",
        backend,
        auth_type,
    )
    return None


__all__ = [
    "SYNC_IGNORE_PATTERNS",
    "WorkspaceSyncBase",
    "NextcloudWorkspaceSync",
    "OpenCloudWorkspaceSync",
    "build_workspace_sync",
]
