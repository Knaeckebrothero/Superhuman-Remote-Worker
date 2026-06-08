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
from .coordinator import CloudSyncError, MountSync, WorkspaceSyncCoordinator
from .nextcloud_sync import NextcloudWorkspaceSync
from .opencloud_sync import OpenCloudWorkspaceSync

if TYPE_CHECKING:
    from ...core.workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)


def build_workspace_sync(
    *,
    workspace_path: Path,
    cloud_cfg: Optional[dict[str, Any]],
    workspace_backend: Optional["WorkspaceBackend"] = None,
    poll_interval: int = 15,
    mount_subdir: str = "",
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
          } | {
            "type": "keycloak_user_impersonation",
            "issuer": str, "client_id": str, "client_secret": str,
            "target_user_sub": str,     # Keycloak sub of the user to impersonate
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
            mount_subdir=mount_subdir,
        )
    if backend == "opencloud" and auth_type in (
        "keycloak_client_credentials",
        "keycloak_user_impersonation",
    ):
        target_user_sub = (
            auth.get("target_user_sub")
            if auth_type == "keycloak_user_impersonation"
            else None
        )
        if auth_type == "keycloak_user_impersonation" and not target_user_sub:
            logger.warning(
                "cloud_sync: keycloak_user_impersonation auth missing "
                "target_user_sub — skipping mount"
            )
            return None
        return OpenCloudWorkspaceSync(
            workspace_path=workspace_path,
            webdav_base_url=webdav_url,
            keycloak_issuer=auth["issuer"],
            client_id=auth["client_id"],
            client_secret=auth["client_secret"],
            target_user_sub=target_user_sub,
            poll_interval=poll_interval,
            workspace_backend=workspace_backend,
            mount_subdir=mount_subdir,
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
    "MountSync",
    "WorkspaceSyncCoordinator",
    "CloudSyncError",
    "build_workspace_sync",
]
