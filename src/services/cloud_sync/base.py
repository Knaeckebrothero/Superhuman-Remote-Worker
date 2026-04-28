"""Base class for cloud-agnostic workspace sync.

The sync algorithm (walk workspace, diff against last-known state, push
modified files, poll for remote changes, honor ignore patterns) lives here
exactly once. Per-backend subclasses implement four transport primitives —
mkdir, upload, list, download — so the same algorithm runs for Nextcloud
WebDAV + basic auth, OpenCloud WebDAV + bearer token, or any future
transport (e.g. MS Graph for OneDrive).
"""

from __future__ import annotations

import abc
import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ...core.workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)

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
    """Return True if ``rel_path`` matches any ignore pattern."""
    for pattern in SYNC_IGNORE_PATTERNS:
        if pattern.endswith("/"):
            if rel_path.startswith(pattern) or f"/{pattern}" in f"/{rel_path}":
                return True
        elif rel_path == pattern or rel_path.endswith(f"/{pattern}"):
            return True
    return False


class WorkspaceSyncBase(abc.ABC):
    """Bidirectional workspace ↔ cloud-folder sync.

    Subclasses implement transport primitives; this class owns the
    algorithm and background polling lifecycle.
    """

    def __init__(
        self,
        workspace_path: Path,
        *,
        poll_interval: int = 15,
        workspace_backend: Optional["WorkspaceBackend"] = None,
    ) -> None:
        self._workspace_path = Path(workspace_path)
        self._poll_interval = poll_interval
        self._backend = workspace_backend

        self._local_state: dict[str, float] = {}
        self._remote_state: dict[str, str] = {}
        self._remote_dirs: set[str] = set()
        self._pushed_sizes: dict[str, int] = {}

        self._poll_task: Optional[asyncio.Task] = None
        self._running = False

    # --------------------------------------------------------------- Primitives

    @abc.abstractmethod
    async def _ensure_ready(self) -> None:
        """Verify transport is ready (lazy client init, token fetch, etc.)."""

    @abc.abstractmethod
    async def _ensure_remote_dir(self, rel_dir: str) -> None:
        """Create a single remote directory. Idempotent; swallow "exists"."""

    @abc.abstractmethod
    async def _upload_file(self, rel_path: str, local_path: str) -> None:
        """Upload ``local_path`` to remote ``rel_path``. Parent dirs already exist."""

    @abc.abstractmethod
    async def _list_remote_files(self) -> list[dict]:
        """List the session folder recursively.

        Returns a list of dicts shaped
        ``{"path": <rel>, "etag": <str>, "isdir": <bool>}``.
        """

    @abc.abstractmethod
    async def _download_file(self, rel_path: str, local_path: str) -> None:
        """Download remote ``rel_path`` into ``local_path``."""

    async def aclose(self) -> None:
        """Release per-instance resources. Default: nothing to release."""
        return None

    # --------------------------------------------------------------- Algorithm

    async def _ensure_remote_dirs(self, rel_dir: str) -> None:
        """Recursively create remote directories, caching known dirs."""
        if not rel_dir or rel_dir == "." or rel_dir in self._remote_dirs:
            return
        parent = str(Path(rel_dir).parent)
        if parent and parent != rel_dir:
            await self._ensure_remote_dirs(parent)
        try:
            await self._ensure_remote_dir(rel_dir)
        except Exception:
            # Already exists or created by parent — not worth distinguishing
            pass
        self._remote_dirs.add(rel_dir)

    def _walk_backend_files(self) -> list[str]:
        """Recursively list files from the remote workspace backend."""
        results: list[str] = []
        stack = [""]
        while stack:
            dir_path = stack.pop()
            try:
                entries = self._backend.list_dir(dir_path)  # type: ignore[union-attr]
            except Exception:
                continue
            for entry in entries:
                if entry.endswith("/"):
                    stack.append(entry.rstrip("/"))
                else:
                    results.append(entry)
        return results

    async def push(self) -> list[str]:
        """Push locally modified/new files to the cloud."""
        pushed: list[str] = []
        try:
            await self._ensure_ready()
            if self._backend:
                pushed = await self._push_via_backend()
            else:
                pushed = await self._push_local()
            if pushed:
                logger.info("Synced %d file(s) to cloud", len(pushed))
        except Exception as e:
            logger.warning("Push sync failed: %s", e)
        return pushed

    async def _push_via_backend(self) -> list[str]:
        """Push files from a remote workspace backend. Uses size-based dedup."""
        pushed: list[str] = []
        files = await asyncio.to_thread(self._walk_backend_files)

        for rel_path in files:
            if _should_ignore(rel_path):
                continue
            try:
                content = await asyncio.to_thread(
                    self._backend.read_file,  # type: ignore[union-attr]
                    rel_path,
                    True,
                )
                if isinstance(content, str):
                    content = content.encode("utf-8")

                prev_size = self._pushed_sizes.get(rel_path)
                if prev_size is not None and len(content) == prev_size:
                    continue

                fd, tmp_path = tempfile.mkstemp()
                try:
                    os.write(fd, content)
                    os.close(fd)

                    remote_dir = str(Path(rel_path).parent)
                    await self._ensure_remote_dirs(remote_dir)
                    await self._upload_file(rel_path, tmp_path)
                    self._pushed_sizes[rel_path] = len(content)
                    pushed.append(rel_path)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            except Exception as e:
                logger.debug("Push failed for %s: %s", rel_path, e)
        return pushed

    async def _push_local(self) -> list[str]:
        """Push files from the local filesystem. Uses mtime-based dedup."""
        pushed: list[str] = []
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

                try:
                    remote_dir = str(Path(rel_path).parent)
                    await self._ensure_remote_dirs(remote_dir)
                    await self._upload_file(rel_path, str(local_path))
                    self._local_state[rel_path] = mtime
                    pushed.append(rel_path)
                except Exception as e:
                    logger.debug("Push failed for %s: %s", rel_path, e)
        return pushed

    async def pull(self) -> list[str]:
        """Pull remotely modified/new files from the cloud into the workspace."""
        pulled: list[str] = []
        try:
            await self._ensure_ready()
            try:
                remote_files = await self._list_remote_files()
            except Exception:
                # Folder doesn't exist yet — nothing to pull
                return pulled

            for item in remote_files:
                path = item.get("path", "")
                if not path or item.get("isdir"):
                    continue
                if _should_ignore(path):
                    continue

                etag = item.get("etag", "")
                prev_etag = self._remote_state.get(path)
                if prev_etag and etag == prev_etag:
                    continue

                try:
                    if self._backend:
                        await self._pull_file_to_backend(path, etag)
                    else:
                        await self._pull_file_local(path, etag)
                    pulled.append(path)
                except Exception as e:
                    logger.debug("Pull failed for %s: %s", path, e)

            if pulled:
                logger.info("Pulled %d file(s) from cloud", len(pulled))
        except Exception as e:
            logger.warning("Pull sync failed: %s", e)
        return pulled

    async def _pull_file_to_backend(self, path: str, etag: str) -> None:
        fd, tmp_path = tempfile.mkstemp()
        os.close(fd)
        try:
            await self._download_file(path, tmp_path)
            with open(tmp_path, "rb") as f:
                content = f.read()
            await asyncio.to_thread(
                self._backend.write_file,  # type: ignore[union-attr]
                path,
                content,
            )
            self._remote_state[path] = etag
            self._pushed_sizes[path] = len(content)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    async def _pull_file_local(self, path: str, etag: str) -> None:
        local_path = self._workspace_path / path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        await self._download_file(path, str(local_path))
        self._remote_state[path] = etag
        self._local_state[path] = local_path.stat().st_mtime

    async def full_sync(self) -> tuple[list[str], list[str]]:
        """Push first, then pull."""
        pushed = await self.push()
        pulled = await self.pull()
        return pushed, pulled

    # ------------------------------------------------------------ Poll lifecycle

    async def start_background_poll(self) -> None:
        """Start the background pull loop."""
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Started cloud sync polling (interval=%ss)", self._poll_interval)

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                if not self._running:
                    break
                await self.pull()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Sync poll error: %s", e)

    async def stop(self) -> None:
        """Stop the background pull loop."""
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("Stopped cloud sync polling")
