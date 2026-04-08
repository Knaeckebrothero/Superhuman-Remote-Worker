"""Bidirectional sync between a local workspace and a Nextcloud session folder.

Runs inside the persistent agent process. Push syncs workspace changes to
Nextcloud after each turn. Pull syncs user uploads from Nextcloud to the
workspace on a configurable polling interval.

Uses webdav3.client.Client for WebDAV operations.

Supports both local and remote workspace backends:
- Local backend: uses os.walk() for file discovery
- Remote backend: uses workspace backend's list_dir()/read_file()/write_file()
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

if TYPE_CHECKING:
    from ..core.workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)

# Internal workspace files excluded from sync
SYNC_IGNORE_PATTERNS = [
    ".git/",
    "repos/",
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

    When a workspace_backend is provided (remote workspace), file operations
    go through the backend instead of direct local filesystem access.
    """

    def __init__(
        self,
        workspace_path: Path,
        webdav_url: str,
        webdav_user: str,
        webdav_password: str,
        poll_interval: int = 15,
        workspace_backend: Optional["WorkspaceBackend"] = None,
    ):
        self._workspace_path = Path(workspace_path)
        self._webdav_url = webdav_url
        self._webdav_user = webdav_user
        self._webdav_password = webdav_password
        self._poll_interval = poll_interval
        self._backend = workspace_backend

        # Track local file mtimes for push (local backend only)
        self._local_state: dict[str, float] = {}
        # Track remote file etags for pull
        self._remote_state: dict[str, str] = {}
        # Track remote dirs confirmed to exist (avoids redundant mkdir calls)
        self._remote_dirs: set[str] = set()
        # Track pushed files for remote backend (by content length)
        self._pushed_sizes: dict[str, int] = {}

        self._poll_task: Optional[asyncio.Task] = None
        self._running = False
        self._client = None

        # Base path on the WebDAV server — used to convert absolute server
        # paths returned by client.list(get_info=True) into relative paths.
        self._webdav_base_path = urlparse(webdav_url).path.rstrip("/") + "/"

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

    async def _ensure_remote_dirs(self, client, remote_dir: str) -> None:
        """Recursively create remote directories, caching known dirs."""
        if not remote_dir or remote_dir == "." or remote_dir in self._remote_dirs:
            return
        # Create parents first
        parent = str(Path(remote_dir).parent)
        if parent != remote_dir:
            await self._ensure_remote_dirs(client, parent)
        try:
            await asyncio.to_thread(client.mkdir, remote_dir)
        except Exception:
            pass  # Already exists or created by parent call
        self._remote_dirs.add(remote_dir)

    def _walk_backend_files(self) -> list[str]:
        """Recursively list all files from the workspace backend.

        Returns list of relative paths.
        """
        results = []
        stack = [""]
        while stack:
            dir_path = stack.pop()
            try:
                entries = self._backend.list_dir(dir_path)
            except Exception:
                continue
            for entry in entries:
                if entry.endswith("/"):
                    stack.append(entry.rstrip("/"))
                else:
                    results.append(entry)
        return results

    async def push(self) -> list[str]:
        """Push locally modified/new files to Nextcloud.

        For local backends: compares workspace mtimes against last-known state.
        For remote backends: pushes all non-ignored files each time.

        Returns:
            List of pushed file paths (relative to workspace).
        """
        pushed = []
        try:
            client = self._get_client()

            if self._backend:
                pushed = await self._push_via_backend(client)
            else:
                pushed = await self._push_local(client)

            if pushed:
                logger.info(f"Synced {len(pushed)} file(s) to Nextcloud")
        except Exception as e:
            logger.warning(f"Push sync failed: {e}")
        return pushed

    async def _push_via_backend(self, client) -> list[str]:
        """Push files from a remote workspace backend to Nextcloud."""
        pushed = []
        files = await asyncio.to_thread(self._walk_backend_files)

        for rel_path in files:
            if _should_ignore(rel_path):
                continue
            try:
                content = await asyncio.to_thread(
                    self._backend.read_file, rel_path, True
                )
                if isinstance(content, str):
                    content = content.encode("utf-8")

                # Skip if size unchanged since last push
                prev_size = self._pushed_sizes.get(rel_path)
                if prev_size is not None and len(content) == prev_size:
                    continue

                # Write to temp file for webdav upload
                fd, tmp_path = tempfile.mkstemp()
                try:
                    os.write(fd, content)
                    os.close(fd)

                    remote_dir = str(Path(rel_path).parent)
                    await self._ensure_remote_dirs(client, remote_dir)
                    await asyncio.to_thread(
                        client.upload_sync,
                        remote_path=rel_path,
                        local_path=tmp_path,
                    )
                    self._pushed_sizes[rel_path] = len(content)
                    pushed.append(rel_path)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            except Exception as e:
                logger.debug(f"Push failed for {rel_path}: {e}")
        return pushed

    async def _push_local(self, client) -> list[str]:
        """Push files from local filesystem to Nextcloud (original logic)."""
        pushed = []
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
                    remote_dir = str(Path(remote_path).parent)
                    await self._ensure_remote_dirs(client, remote_dir)

                    await asyncio.to_thread(
                        client.upload_sync,
                        remote_path=remote_path,
                        local_path=str(local_path),
                    )
                    self._local_state[rel_path] = mtime
                    pushed.append(rel_path)
                except Exception as e:
                    logger.debug(f"Push failed for {rel_path}: {e}")
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

            # List remote files (may fail if folder is empty / not yet created)
            try:
                remote_files = await asyncio.to_thread(client.list, "/", get_info=True)
            except Exception:
                # Folder doesn't exist yet — nothing to pull
                return pulled

            for item in remote_files:
                if not isinstance(item, dict):
                    continue
                raw_path = item.get("path", "")
                # client.list(get_info=True) returns full server paths like
                # /remote.php/dav/files/user/folder/file.txt — strip the
                # base prefix to get a path relative to the session folder.
                if raw_path.startswith(self._webdav_base_path):
                    path = raw_path[len(self._webdav_base_path) :]
                else:
                    path = raw_path.strip("/")
                if not path or item.get("isdir"):
                    continue
                if _should_ignore(path):
                    continue

                etag = item.get("etag", "")
                prev_etag = self._remote_state.get(path)
                if prev_etag and etag == prev_etag:
                    continue

                # Download
                try:
                    if self._backend:
                        await self._pull_file_to_backend(client, path, etag)
                    else:
                        await self._pull_file_local(client, path, etag)
                    pulled.append(path)
                except Exception as e:
                    logger.debug(f"Pull failed for {path}: {e}")

            if pulled:
                logger.info(f"Pulled {len(pulled)} file(s) from Nextcloud")
        except Exception as e:
            logger.warning(f"Pull sync failed: {e}")
        return pulled

    async def _pull_file_to_backend(self, client, path: str, etag: str) -> None:
        """Download a file from Nextcloud and write to remote backend."""
        fd, tmp_path = tempfile.mkstemp()
        os.close(fd)
        try:
            await asyncio.to_thread(
                client.download_sync,
                remote_path=path,
                local_path=tmp_path,
            )
            with open(tmp_path, "rb") as f:
                content = f.read()

            await asyncio.to_thread(self._backend.write_file, path, content)
            self._remote_state[path] = etag
            # Track size so push doesn't re-upload
            self._pushed_sizes[path] = len(content)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def _pull_file_local(self, client, path: str, etag: str) -> None:
        """Download a file from Nextcloud to local filesystem."""
        local_path = self._workspace_path / path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            client.download_sync,
            remote_path=path,
            local_path=str(local_path),
        )
        self._remote_state[path] = etag
        # Update local state so push doesn't re-upload
        self._local_state[path] = local_path.stat().st_mtime

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
