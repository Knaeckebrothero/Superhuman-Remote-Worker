"""Nextcloud workspace sync — WebDAV + HTTP basic auth."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

from .base import WorkspaceSyncBase

if TYPE_CHECKING:
    from ...core.workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)


class NextcloudWorkspaceSync(WorkspaceSyncBase):
    """Nextcloud sync client — agent-service user + password, classic WebDAV."""

    def __init__(
        self,
        workspace_path: Path,
        *,
        webdav_url: str,
        webdav_user: str,
        webdav_password: str,
        poll_interval: int = 15,
        workspace_backend: Optional["WorkspaceBackend"] = None,
        mount_subdir: str = "",
    ) -> None:
        super().__init__(
            workspace_path,
            poll_interval=poll_interval,
            workspace_backend=workspace_backend,
            mount_subdir=mount_subdir,
        )
        self._webdav_url = webdav_url
        self._webdav_user = webdav_user
        self._webdav_password = webdav_password
        self._client = None
        # client.list(get_info=True) returns absolute server paths
        # (e.g. /remote.php/dav/files/user/folder/file.txt) — we strip
        # this prefix to normalize to paths relative to the session folder.
        self._webdav_base_path = urlparse(webdav_url).path.rstrip("/") + "/"

    def _get_client(self):
        if self._client is None:
            from webdav3.client import Client

            self._client = Client(
                {
                    "webdav_hostname": self._webdav_url.rstrip("/"),
                    "webdav_login": self._webdav_user,
                    "webdav_password": self._webdav_password,
                }
            )
        return self._client

    async def _ensure_ready(self) -> None:
        self._get_client()

    async def _ensure_remote_dir(self, rel_dir: str) -> None:
        client = self._get_client()
        await asyncio.to_thread(client.mkdir, rel_dir)

    async def _upload_file(self, rel_path: str, local_path: str) -> None:
        client = self._get_client()
        await asyncio.to_thread(
            client.upload_sync,
            remote_path=rel_path,
            local_path=local_path,
        )

    async def _list_remote_files(self) -> list[dict]:
        client = self._get_client()
        raw = await asyncio.to_thread(client.list, "/", get_info=True)
        out: list[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            raw_path = item.get("path", "")
            if raw_path.startswith(self._webdav_base_path):
                rel = raw_path[len(self._webdav_base_path) :]
            else:
                rel = raw_path.strip("/")
            out.append(
                {
                    "path": rel,
                    "etag": item.get("etag", "") or "",
                    "isdir": bool(item.get("isdir")),
                }
            )
        return out

    async def _download_file(self, rel_path: str, local_path: str) -> None:
        client = self._get_client()
        await asyncio.to_thread(
            client.download_sync,
            remote_path=rel_path,
            local_path=local_path,
        )

    async def aclose(self) -> None:
        # webdav3 client has no explicit close; drop the reference so GC reclaims.
        self._client = None
