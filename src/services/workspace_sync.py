"""Bidirectional sync between a local workspace and a Nextcloud session folder.

Runs inside the persistent agent process. Push syncs workspace changes to
Nextcloud after each turn. Pull syncs user uploads from Nextcloud to the
workspace on a configurable polling interval.

Uses webdav3.client.Client for WebDAV operations.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Internal workspace files excluded from sync
SYNC_IGNORE_PATTERNS = [
    ".git/",
    "tools/",
    "todos.yaml",
    "archive/",
    "chunks/",
    "candidates/",
    "requirements/",
    "workspace.md",
    "plan.md",
]


def _should_ignore(rel_path: str) -> bool:
    """Check if a relative path matches any ignore pattern."""
    for pattern in SYNC_IGNORE_PATTERNS:
        if pattern.endswith("/"):
            # Directory pattern — match if path starts with it or contains it
            if rel_path.startswith(pattern) or f"/{pattern}" in f"/{rel_path}":
                return True
        elif rel_path == pattern or rel_path.endswith(f"/{pattern}"):
            return True
    return False


class WorkspaceSyncService:
    """Bidirectional sync between local workspace and Nextcloud folder.

    Sync is triggered:
    - After each agent turn (push): upload new/modified workspace files
    - On a polling interval (pull): download new/modified user uploads
    - On explicit call: full_sync()
    """

    def __init__(
        self,
        workspace_path: Path,
        webdav_url: str,
        webdav_user: str,
        webdav_password: str,
        poll_interval: int = 15,
    ):
        self._workspace_path = Path(workspace_path)
        self._webdav_url = webdav_url
        self._webdav_user = webdav_user
        self._webdav_password = webdav_password
        self._poll_interval = poll_interval

        # Track local file mtimes for push
        self._local_state: dict[str, float] = {}
        # Track remote file etags for pull
        self._remote_state: dict[str, str] = {}

        self._poll_task: Optional[asyncio.Task] = None
        self._running = False
        self._client = None

    def _get_client(self):
        """Lazy-init the WebDAV client."""
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

    async def push(self) -> list[str]:
        """Push locally modified/new files to Nextcloud.

        Compares workspace mtimes against last-known state. Uploads
        new/modified files, skipping ignored patterns.

        Returns:
            List of pushed file paths (relative to workspace).
        """
        pushed = []
        try:
            client = self._get_client()
            for root, _dirs, files in os.walk(self._workspace_path):
                for filename in files:
                    local_path = Path(root) / filename
                    rel_path = str(local_path.relative_to(self._workspace_path))

                    if _should_ignore(rel_path):
                        continue

                    mtime = local_path.stat().st_mtime
                    prev_mtime = self._local_state.get(rel_path)
                    if prev_mtime is not None and mtime <= prev_mtime:
                        continue

                    # Upload
                    remote_path = rel_path
                    try:
                        # Ensure remote parent dirs exist
                        remote_dir = str(Path(remote_path).parent)
                        if remote_dir and remote_dir != ".":
                            await asyncio.to_thread(client.mkdir, remote_dir)

                        await asyncio.to_thread(
                            client.upload_sync,
                            remote_path=remote_path,
                            local_path=str(local_path),
                        )
                        self._local_state[rel_path] = mtime
                        pushed.append(rel_path)
                    except Exception as e:
                        logger.debug(f"Push failed for {rel_path}: {e}")

            if pushed:
                logger.info(f"Synced {len(pushed)} file(s) to Nextcloud")
        except Exception as e:
            logger.warning(f"Push sync failed: {e}")
        return pushed

    async def pull(self) -> list[str]:
        """Pull remotely modified/new files from Nextcloud to workspace.

        Uses webdav list to detect new/modified files. Downloads them
        into the workspace, preserving the folder structure.

        Returns:
            List of pulled file paths (relative to workspace).
        """
        pulled = []
        try:
            client = self._get_client()

            # List remote files recursively
            remote_files = await asyncio.to_thread(client.list, "/", get_info=True)

            for item in remote_files:
                if not isinstance(item, dict):
                    continue
                path = item.get("path", "").strip("/")
                if not path or item.get("isdir"):
                    continue
                if _should_ignore(path):
                    continue

                etag = item.get("etag", "")
                prev_etag = self._remote_state.get(path)
                if prev_etag and etag == prev_etag:
                    continue

                # Download
                local_path = self._workspace_path / path
                try:
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(
                        client.download_sync,
                        remote_path=path,
                        local_path=str(local_path),
                    )
                    self._remote_state[path] = etag
                    # Update local state so push doesn't re-upload
                    self._local_state[path] = local_path.stat().st_mtime
                    pulled.append(path)
                except Exception as e:
                    logger.debug(f"Pull failed for {path}: {e}")

            if pulled:
                logger.info(f"Pulled {len(pulled)} file(s) from Nextcloud")
        except Exception as e:
            logger.warning(f"Pull sync failed: {e}")
        return pulled

    async def full_sync(self) -> tuple[list[str], list[str]]:
        """Bidirectional sync. Push first, then pull.

        Returns:
            Tuple of (pushed paths, pulled paths).
        """
        pushed = await self.push()
        pulled = await self.pull()
        return pushed, pulled

    async def start_background_poll(self) -> None:
        """Start background polling loop for user uploads."""
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(f"Started Nextcloud sync polling (interval={self._poll_interval}s)")

    async def _poll_loop(self) -> None:
        """Background loop that pulls from Nextcloud on interval."""
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                if not self._running:
                    break
                await self.pull()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Sync poll error: {e}")

    async def stop(self) -> None:
        """Stop background polling."""
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("Stopped Nextcloud sync polling")
