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
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ...core.backends.overlay import unwrap_backend

if TYPE_CHECKING:
    from ...core.workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)

SYNC_IGNORE_PATTERNS = [
    ".git/",
    "repos/",
    "projects/",
    "tools/",
    "todos.yaml",
    "archive/",
    "chunks/",
    "candidates/",
    "requirements/",
    "workspace.md",
    "datasources.md",
    "spec_lock.md",
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


def _normalize_dav_listing(raw: list, webdav_base_path: str) -> list[dict]:
    """Shape a webdav3 ``list(get_info=True)`` result for the sync algorithm.

    Shared by the Nextcloud and OpenCloud transports (it was duplicated).
    Strips the server-absolute prefix down to mount-relative paths and
    coerces ``size`` (webdav3 reports it as a string, absent for
    collections) to ``int | None``.
    """
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path", "")
        if raw_path.startswith(webdav_base_path):
            rel = raw_path[len(webdav_base_path) :]
        else:
            rel = raw_path.strip("/")
        size = item.get("size")
        try:
            size_int: Optional[int] = int(size) if size not in (None, "") else None
        except (TypeError, ValueError):
            size_int = None
        out.append(
            {
                "path": rel,
                "etag": item.get("etag", "") or "",
                "isdir": bool(item.get("isdir")),
                "size": size_int,
            }
        )
    return out


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
        mount_subdir: str = "",
    ) -> None:
        self._workspace_path = Path(workspace_path)
        self._poll_interval = poll_interval
        # Unwrap the virtual overlay once, here, so no method below can reach a
        # virtual path. Cloud sync is not a tool-layer consumer: virtual
        # prefixes (tools/, contacts/, instructions.md) are framework
        # projection, not user data. Walking the overlay merged contacts/ into
        # the root listing and uploaded contact names, emails and phone numbers
        # to the user's cloud folder — SYNC_IGNORE_PATTERNS lists tools/ but
        # nothing else — and the return pull then raised VirtualPathError,
        # which the coordinator's strict=True turned into "cloud sync disabled
        # for the rest of the session". Doing it structurally, rather than by
        # extending SYNC_IGNORE_PATTERNS, means a future provider cannot
        # reintroduce either failure.
        # See docs/features/virtual_directories.md.
        self._backend = unwrap_backend(workspace_backend) if workspace_backend else None
        # Workspace-relative path of this mount inside the backend's filesystem.
        # Empty string means "the whole workspace" (legacy single-folder mount).
        # For project mounts, e.g. "projects/alpha". No leading/trailing slash.
        self._mount_subdir = mount_subdir.strip("/")

        self._local_state: dict[str, float] = {}
        self._remote_state: dict[str, str] = {}
        self._remote_dirs: set[str] = set()
        self._pushed_sizes: dict[str, int] = {}
        # One-shot guard for priming the dedup dicts from the remote tree.
        # All state above is process memory: a fresh agent pod used to
        # re-upload (and re-download) the whole mount because it had no way
        # to know the cloud already held identical content. Set by the first
        # pull (whose listing doubles as the seed) or by _seed_remote_state
        # on a push that runs before any pull.
        # docs/issues/session_turn_end_cloud_push_blocks_queued_input.md
        self._remote_seeded: bool = False

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
    async def _list_remote_files(self, rel_dir: str = "") -> list[dict]:
        """List ONE remote directory level (``rel_dir`` is mount-relative).

        Returns a list of dicts shaped
        ``{"path": <rel>, "etag": <str>, "isdir": <bool>, "size": <int|None>}``
        where ``path`` is relative to the MOUNT ROOT, not to ``rel_dir``.
        WebDAV ``list`` is a Depth-1 PROPFIND, so this is a single level;
        recursion lives in ``_list_remote_tree``. Implementations returning
        more than the requested level (test doubles list recursively) are
        fine — the tree walk dedups.
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

    def _backend_path(self, mount_rel_path: str) -> str:
        """Translate a mount-relative path to a workspace-backend path."""
        if not self._mount_subdir:
            return mount_rel_path
        return (
            f"{self._mount_subdir}/{mount_rel_path}"
            if mount_rel_path
            else self._mount_subdir
        )

    async def _list_remote_tree_fast(self) -> Optional[list[dict]]:
        """Optional transport primitive: the WHOLE mount tree in one shot.

        Return the complete recursive listing (files may include ignored
        paths and directory entries — the caller filters), or ``None`` when
        the transport has no such capability (permanently or for this
        server). The default has none. Motivation: the per-directory walk
        costs one ``_list_remote_files`` round trip per directory, measured
        at ~2.5s/dir via webdav3 (2026-08-08 baseline: 39.9s to list 16
        files); Nextcloud serves the same tree in ONE ``Depth: infinity``
        PROPFIND (~1s).
        """
        return None

    async def _list_remote_tree(self) -> list[dict]:
        """Recursively list the mount via per-directory ``_list_remote_files``.

        Tries the transport's one-shot ``_list_remote_tree_fast`` first;
        falls back to the walk when the transport reports no capability.
        Fast results pass through the same shaping the walk applies (files
        only, ignored subtrees dropped, deduped) — ``_should_ignore``'s
        directory patterns match by path prefix, so per-file filtering
        subsumes the walk's descend-time pruning.

        The old single ``_list_remote_files()`` call was documented as
        recursive but backed by a Depth-1 PROPFIND, which silently limited
        pull (and now dedup seeding) to ROOT-LEVEL files — cloud-side edits
        under ``output/`` etc. never reached the agent. Ignored subtrees are
        pruned before descending; the visited set drops entries echoing the
        listed collection itself (WebDAV servers include it) and keeps
        fully-recursive test doubles from double-listing.
        """
        fast = await self._list_remote_tree_fast()
        if fast is not None:
            out: list[dict] = []
            seen_fast: set[str] = set()
            for item in fast:
                path = (item.get("path") or "").strip("/")
                if not path or item.get("isdir") or _should_ignore(path):
                    continue
                if path not in seen_fast:
                    seen_fast.add(path)
                    out.append({**item, "path": path})
            return out
        out = []
        seen_files: set[str] = set()
        visited: set[str] = set()
        stack: list[str] = [""]
        while stack:
            rel_dir = stack.pop()
            if rel_dir in visited:
                continue
            visited.add(rel_dir)
            for item in await self._list_remote_files(rel_dir):
                path = (item.get("path") or "").strip("/")
                if not path or path == rel_dir:
                    continue
                if item.get("isdir"):
                    if not _should_ignore(f"{path}/") and path not in visited:
                        stack.append(path)
                    continue
                if path not in seen_files:
                    seen_files.add(path)
                    out.append({**item, "path": path})
        return out

    async def _seed_remote_state(self) -> None:
        """One-shot: prime push dedup from the remote tree's sizes.

        A fresh agent pod starts with empty in-memory dedup state and used
        to re-upload the entire mount on its first push — minutes of serial
        WebDAV round-trips. Recording each remote file's size as the
        last-pushed size makes the first push skip everything the cloud
        already holds at the same size: the documented size-dedup tradeoff
        (a same-size, different-content change is missed until the size
        moves), extended across pod recycles instead of newly invented.

        Normally the attach/turn-start pull has already seeded (shared
        ``_remote_seeded`` flag); this covers a push that runs before any
        pull succeeded. Fail-soft and single-shot: on listing failure the
        push simply runs unseeded, which is the old behavior.
        """
        if self._remote_seeded:
            return
        self._remote_seeded = True
        try:
            items = await self._list_remote_tree()
        except Exception as e:
            logger.debug("Remote dedup seeding skipped: %s", e)
            return
        seeded = 0
        for item in items:
            path = item.get("path", "")
            if not path or _should_ignore(path):
                continue
            size = item.get("size")
            if path not in self._pushed_sizes and isinstance(size, int) and size >= 0:
                self._pushed_sizes[path] = size
                seeded += 1
        if seeded:
            logger.info("Seeded push dedup from %d remote file size(s)", seeded)

    async def _local_size(self, rel_path: str) -> Optional[int]:
        """Byte size of the workspace copy of ``rel_path``, or None if absent.

        Backend ``stat()`` reports 0 for missing paths, so a 0 answer gets an
        ``is_file`` confirmation — otherwise an empty remote file would be
        recorded as in-sync against a local copy that does not exist.
        """
        if self._backend:
            backend_path = self._backend_path(rel_path)
            try:
                size = await asyncio.to_thread(self._backend.stat, backend_path)
                if size == 0 and not await asyncio.to_thread(
                    self._backend.is_file, backend_path
                ):
                    return None
                return size
            except Exception:
                return None
        local_path = self._workspace_path / rel_path
        try:
            return local_path.stat().st_size if local_path.is_file() else None
        except OSError:
            return None

    def _walk_backend_files(self) -> list[str]:
        """Recursively list files from the remote workspace backend.

        Returns *mount-relative* paths — i.e. with ``mount_subdir`` stripped
        so the rest of the algorithm operates in the mount's coordinate
        system, matching what the cloud-side ``webdav_url`` expects.
        """
        results: list[str] = []
        stack = [self._mount_subdir]
        prefix = f"{self._mount_subdir}/" if self._mount_subdir else ""
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
                    if prefix and entry.startswith(prefix):
                        results.append(entry[len(prefix) :])
                    elif not prefix:
                        results.append(entry)
        return results

    async def _walk_backend_files_async(self) -> list[str]:
        """Level-parallel ``_walk_backend_files`` (same semantics, same
        skip-dir-on-error behavior). One ``list_dir`` per directory is a
        backend round trip — S3 LIST for virtual workspaces, measured 5.5s
        serial for a 16-file tree — so directories of a level are listed
        concurrently under a small semaphore.
        """
        results: list[str] = []
        level = [self._mount_subdir]
        prefix = f"{self._mount_subdir}/" if self._mount_subdir else ""
        sem = asyncio.Semaphore(8)

        async def _list(dir_path: str) -> list[str]:
            async with sem:
                try:
                    return await asyncio.to_thread(
                        self._backend.list_dir,  # type: ignore[union-attr]
                        dir_path,
                    )
                except Exception:
                    return []

        while level:
            listings = await asyncio.gather(*[_list(d) for d in level])
            level = []
            for entries in listings:
                for entry in entries:
                    if entry.endswith("/"):
                        level.append(entry.rstrip("/"))
                    elif prefix and entry.startswith(prefix):
                        results.append(entry[len(prefix) :])
                    elif not prefix:
                        results.append(entry)
        return results

    async def _backend_file_index(self) -> Optional[dict[str, int]]:
        """Mount-relative path → size for every file under the mount, in ONE
        backend round trip — when the backend can do that.

        Capability probe by duck typing: :class:`VirtualWorkspaceBackend`
        exposes ``list_files_with_sizes`` (one recursive object-store list;
        every individual op there is an rclone subprocess spawn, measured
        ~0.3–0.6s each — a per-file stat pass costs seconds, this costs one
        spawn). SSH/remote backends have no such bulk primitive and answer
        ``None``, which keeps them on the per-file path. ``None`` on failure
        too — the caller's fallback re-runs the ordinary walk/stat path and
        surfaces the real error under its own strict rules.
        """
        lister = getattr(self._backend, "list_files_with_sizes", None)
        if lister is None:
            return None
        try:
            raw = await asyncio.to_thread(lister, self._mount_subdir)
        except Exception as e:
            logger.debug("Bulk file index failed (%s); per-file path", e)
            return None
        prefix = f"{self._mount_subdir}/" if self._mount_subdir else ""
        out: dict[str, int] = {}
        for path, size in raw:
            if prefix and path.startswith(prefix):
                out[path[len(prefix) :]] = size
            elif not prefix:
                out[path] = size
        return out

    async def _local_sizes(self, paths: list[str]) -> dict[str, Optional[int]]:
        """``_local_size`` for many paths with bounded concurrency.

        ``_local_size`` never raises (it answers None for anything it cannot
        stat), so the gather needs no exception shaping.
        """
        if not paths:
            return {}
        sem = asyncio.Semaphore(8)

        async def _one(path: str) -> tuple[str, Optional[int]]:
            async with sem:
                return path, await self._local_size(path)

        return dict(await asyncio.gather(*[_one(p) for p in paths]))

    async def _backend_stats(self, paths: list[str]) -> dict[str, object]:
        """Backend ``stat`` for many paths with bounded concurrency.

        Failures are returned as the exception object (not raised) so the
        caller can honor its own strict/lenient per-file policy.
        """
        if not paths:
            return {}
        sem = asyncio.Semaphore(8)

        async def _one(path: str) -> tuple[str, object]:
            async with sem:
                try:
                    return path, await asyncio.to_thread(
                        self._backend.stat,  # type: ignore[union-attr]
                        self._backend_path(path),
                    )
                except Exception as e:
                    return path, e

        return dict(await asyncio.gather(*[_one(p) for p in paths]))

    async def push(self, *, strict: bool = False) -> list[str]:
        """Push locally modified/new files to the cloud.

        When ``strict`` is True, transport-level and per-file errors raise
        instead of being logged and swallowed. This is what the
        ``WorkspaceSyncCoordinator`` uses to enforce the raise-and-block
        policy at turn boundaries.
        """
        pushed: list[str] = []
        try:
            await self._ensure_ready()
            await self._seed_remote_state()
            if self._backend:
                pushed = await self._push_via_backend(strict=strict)
            else:
                pushed = await self._push_local(strict=strict)
            if pushed:
                logger.info("Synced %d file(s) to cloud", len(pushed))
        except Exception as e:
            if strict:
                raise
            logger.warning("Push sync failed: %s", e)
        return pushed

    async def _push_via_backend(self, *, strict: bool = False) -> list[str]:
        """Push files from a remote workspace backend. Uses size-based dedup."""
        pushed: list[str] = []
        _t0 = _time.perf_counter()
        # One bulk walk-with-sizes when the backend supports it (virtual/S3:
        # a single rclone spawn replaces the per-dir walk AND every per-file
        # size check). Fallback: level-parallel walk + batched stats.
        index = await self._backend_file_index()
        if index is not None:
            files = list(index)
            stat_results: dict[str, object] = dict(index)
            _n_stats = 0
        else:
            files = await self._walk_backend_files_async()
            # stat() is one round-trip with no payload — read the (possibly
            # large) file only when its size moved. All size-checks run up
            # front with bounded concurrency (measured 2.2s serial for 16
            # files on the virtual/S3 backend). stat() reports 0 for
            # vanished paths, which mismatches any tracked nonzero size and
            # falls through to the read; that read's failure is the per-file
            # skip below. A failed stat re-raises inside the per-file try so
            # the strict/lenient policy stays exactly per-file.
            tracked = [
                p
                for p in files
                if not _should_ignore(p) and self._pushed_sizes.get(p) is not None
            ]
            stat_results = await self._backend_stats(tracked)
            _n_stats = len(tracked)
        _t_walk = _time.perf_counter() - _t0

        for rel_path in files:
            if _should_ignore(rel_path):
                continue
            try:
                prev_size = self._pushed_sizes.get(rel_path)
                if prev_size is not None:
                    cur_size = stat_results.get(rel_path)
                    if isinstance(cur_size, Exception):
                        raise cur_size
                    if cur_size == prev_size:
                        continue
                content = await asyncio.to_thread(
                    self._backend.read_file,  # type: ignore[union-attr]
                    self._backend_path(rel_path),
                    True,
                )
                if isinstance(content, str):
                    content = content.encode("utf-8")

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
                if strict:
                    raise
                logger.debug("Push failed for %s: %s", rel_path, e)
        logger.info(
            "push detail: mount=%s walk=%.2fs files=%d stats=%d uploads=%d total=%.2fs",
            self._mount_subdir or "<root>",
            _t_walk,
            len(files),
            _n_stats,
            len(pushed),
            _time.perf_counter() - _t0,
        )
        return pushed

    async def _push_local(self, *, strict: bool = False) -> list[str]:
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
                if prev_mtime is None:
                    # First sighting by this instance. A seeded size match
                    # means the cloud already holds this content — record
                    # the mtime so the normal dedup takes over, skip the
                    # upload (fresh-pod full re-upload fix, local mode).
                    seeded = self._pushed_sizes.get(rel_path)
                    if seeded is not None and local_path.stat().st_size == seeded:
                        self._local_state[rel_path] = mtime
                        continue

                try:
                    remote_dir = str(Path(rel_path).parent)
                    await self._ensure_remote_dirs(remote_dir)
                    await self._upload_file(rel_path, str(local_path))
                    self._local_state[rel_path] = mtime
                    pushed.append(rel_path)
                except Exception as e:
                    if strict:
                        raise
                    logger.debug("Push failed for %s: %s", rel_path, e)
        return pushed

    async def pull(self, *, strict: bool = False) -> list[str]:
        """Pull remotely modified/new files from the cloud into the workspace.

        When ``strict`` is True, transport-level and per-file errors raise
        instead of being logged and swallowed. Used by the coordinator at
        turn boundaries.
        """
        pulled: list[str] = []
        _t0 = _time.perf_counter()
        _t_list = 0.0
        _n_stats = 0
        try:
            await self._ensure_ready()
            try:
                remote_files = await self._list_remote_tree()
            except Exception:
                if strict:
                    raise
                # Folder doesn't exist yet — nothing to pull
                return pulled
            _t_list = _time.perf_counter() - _t0
            # A successful listing doubles as the dedup seed (the reconcile
            # below records sizes/etags), so a later push needn't re-list.
            self._remote_seeded = True

            # First-sighting local size checks are one backend round trip
            # each (an rclone subprocess spawn on virtual workspaces;
            # measured 1.9s serial / 4.0s under thread contention for 16
            # files) — prefer ONE bulk walk-with-sizes, else resolve them
            # with bounded concurrency.
            _stat_paths = [
                item.get("path", "")
                for item in remote_files
                if item.get("path")
                and not item.get("isdir")
                and not _should_ignore(item.get("path", ""))
                and not self._remote_state.get(item.get("path", ""))
                and isinstance(item.get("size"), int)
                and item.get("size", -1) >= 0
            ]
            _index = await self._backend_file_index() if _stat_paths else None
            if _index is not None:
                _local_sizes: dict[str, Optional[int]] = {
                    p: _index.get(p) for p in _stat_paths
                }
                _n_stats = 1
            else:
                _local_sizes = await self._local_sizes(_stat_paths)
                _n_stats = len(_stat_paths)

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

                if not prev_etag:
                    # First sighting by this instance (fresh pod). If the
                    # workspace copy already matches by size, record it as
                    # in-sync instead of re-downloading the whole mount on
                    # every pod recycle — which also clobbered any unpushed
                    # local edit with a stale same-size cloud copy. Size is
                    # the same heuristic push dedup has always used.
                    size = item.get("size")
                    if isinstance(size, int) and size >= 0:
                        local_size = _local_sizes.get(path)
                        if local_size == size:
                            self._remote_state[path] = etag
                            self._pushed_sizes[path] = size
                            if not self._backend:
                                try:
                                    self._local_state[path] = (
                                        (self._workspace_path / path).stat().st_mtime
                                    )
                                except OSError:
                                    pass
                            continue

                try:
                    if self._backend:
                        await self._pull_file_to_backend(path, etag)
                    else:
                        await self._pull_file_local(path, etag)
                    pulled.append(path)
                except Exception as e:
                    if strict:
                        raise
                    logger.debug("Pull failed for %s: %s", path, e)

            if pulled:
                logger.info("Pulled %d file(s) from cloud", len(pulled))
            logger.info(
                "pull detail: mount=%s list=%.2fs files=%d reconcile=%.2fs "
                "stats=%d downloads=%d total=%.2fs",
                self._mount_subdir or "<root>",
                _t_list,
                len(remote_files),
                _time.perf_counter() - _t0 - _t_list,
                _n_stats,
                len(pulled),
                _time.perf_counter() - _t0,
            )
        except Exception as e:
            if strict:
                raise
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
                self._backend_path(path),
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

    async def full_sync(self, *, strict: bool = False) -> tuple[list[str], list[str]]:
        """Push first, then pull."""
        pushed = await self.push(strict=strict)
        pulled = await self.pull(strict=strict)
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
