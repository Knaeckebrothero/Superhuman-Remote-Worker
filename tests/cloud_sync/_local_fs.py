"""Test double — ``WorkspaceSyncBase`` implementation backed by a tmpdir.

Lets ``WorkspaceSyncCoordinator`` integration tests round-trip files end-to-end
without standing up a WebDAV server. The "remote" side is just another
directory on disk; etags are deterministic content hashes so the pull
algorithm's "did this file change" branch is testable in CI.

Lives under ``tests/`` (not ``src/``) on purpose — production code never
points at a local FS as its workspace cloud. Mirrors the convention used
by ``tests/_fs_backend.py`` for ``WorkspaceManager``.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Optional

from src.services.cloud_sync.base import WorkspaceSyncBase


class LocalFsWorkspaceSync(WorkspaceSyncBase):
    """Cloud-side is a local directory; primitives just copy files around."""

    def __init__(
        self,
        workspace_path: Path,
        *,
        remote_root: Path,
        poll_interval: int = 15,
        workspace_backend=None,
        mount_subdir: str = "",
    ) -> None:
        super().__init__(
            workspace_path,
            poll_interval=poll_interval,
            workspace_backend=workspace_backend,
            mount_subdir=mount_subdir,
        )
        self._remote_root = Path(remote_root)
        self._remote_root.mkdir(parents=True, exist_ok=True)

    @property
    def remote_root(self) -> Path:
        return self._remote_root

    async def _ensure_ready(self) -> None:
        return None

    async def _ensure_remote_dir(self, rel_dir: str) -> None:
        if rel_dir and rel_dir != ".":
            (self._remote_root / rel_dir).mkdir(parents=True, exist_ok=True)

    async def _upload_file(self, rel_path: str, local_path: str) -> None:
        dst = self._remote_root / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dst)

    async def _list_remote_files(self) -> list[dict]:
        out: list[dict] = []
        if not self._remote_root.exists():
            return out
        for p in self._remote_root.rglob("*"):
            if p.is_dir():
                continue
            rel = str(p.relative_to(self._remote_root))
            etag = hashlib.sha256(p.read_bytes()).hexdigest()
            out.append({"path": rel, "etag": etag, "isdir": False})
        return out

    async def _download_file(self, rel_path: str, local_path: str) -> None:
        src = self._remote_root / rel_path
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, local_path)


class FailingLocalFsWorkspaceSync(LocalFsWorkspaceSync):
    """Variant that raises on either pull or push.

    Used to exercise the coordinator's raise-and-block aggregation policy.
    """

    def __init__(
        self,
        workspace_path: Path,
        *,
        remote_root: Path,
        fail_on: str = "push",
        message: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(workspace_path, remote_root=remote_root, **kwargs)
        self._fail_on = fail_on
        self._message = message or f"forced {fail_on} failure"

    async def _upload_file(self, rel_path: str, local_path: str) -> None:
        # ``_ensure_remote_dirs`` deliberately swallows mkdir errors (treats
        # them as "already exists"), so failing on dir creation won't
        # surface to the strict-mode caller. Failing on the actual upload
        # is what the algorithm propagates.
        if self._fail_on == "push":
            raise RuntimeError(self._message)
        await super()._upload_file(rel_path, local_path)

    async def _list_remote_files(self) -> list[dict]:
        if self._fail_on == "pull":
            raise RuntimeError(self._message)
        return await super()._list_remote_files()
