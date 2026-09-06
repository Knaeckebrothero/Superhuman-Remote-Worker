"""Mount-time etag-baseline capture (design §3.4, §11.5).

Protected cloud mode has no Mode-A Gitea seed, so the path→etag map that
``detect_external_mods`` compares against live cloud state must be captured by
a PROPFIND walk when the overlay mounts. Sequential Depth:1 BFS does not scale
past ~50-100 directories, so this tries one ``Depth: infinity`` PROPFIND first
(one request on Nextcloud) and falls back to a bounded-concurrency BFS only
when the backend rejects infinity (OpenCloud 400s it — opencloud.py:763).

Pure orchestration: the caller injects the two PROPFIND primitives so this
module stays backend- and auth-agnostic and unit-testable without httpx.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from orchestrator.services.cloud.handles import ProjectFolderEntry


class PropfindError(Exception):
    """A PROPFIND attempt failed or the depth is unsupported.

    Raised by ``list_tree`` to signal "infinity is not available, fall back to
    BFS". A ``list_children`` raising this aborts the capture (a mid-walk
    failure means an incomplete baseline, which must not be trusted).
    """


def _files_to_map(entries: list[ProjectFolderEntry]) -> dict[str, str]:
    # Files only: dir etags churn on any descendant change and the conflict
    # gate compares file content, matching Mode A's entries_map.
    return {e.path: e.etag for e in entries if not e.is_dir}


async def capture_etag_baseline(
    *,
    root_subpath: str,
    list_children: Callable[[str], Awaitable[list[ProjectFolderEntry]]],
    list_tree: Callable[[], Awaitable[list[ProjectFolderEntry]]],
    concurrency: int = 8,
) -> dict[str, str]:
    """Return ``{path: etag}`` for every file under ``root_subpath``.

    Tries ``list_tree()`` (one whole-tree read) first; on ``PropfindError``
    falls back to a bounded-concurrency breadth-first walk via
    ``list_children``.
    """
    try:
        return _files_to_map(await list_tree())
    except PropfindError:
        pass  # infinity unsupported — BFS below

    out: dict[str, str] = {}
    seen: set[str] = set()
    frontier = [root_subpath.strip("/")]
    sem = asyncio.Semaphore(concurrency)

    async def expand(sub: str) -> list[str]:
        async with sem:
            children = await list_children(sub)
        next_dirs: list[str] = []
        for e in children:
            if e.is_dir:
                if e.path not in seen:
                    next_dirs.append(e.path)
            else:
                out[e.path] = e.etag
        return next_dirs

    while frontier:
        level = [s for s in frontier if s not in seen]
        seen.update(level)
        results = await asyncio.gather(*(expand(s) for s in level))
        frontier = [d for group in results for d in group]
    return out
