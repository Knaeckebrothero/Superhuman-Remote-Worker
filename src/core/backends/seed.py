"""Cross-backend workspace seeding — copy every file from one backend to another.

Used by the workspace-tier upgrade (``workspace_tier_upgrade.md`` §4.2 S3a for
sessions, §4.3 W1 for worker jobs): when a lite (``virtual``) session/job
upgrades to a real ``sandbox`` container, the agent copies the object-store
prefix down into the freshly provisioned pod while BOTH backends are live. It
is a pure in-process copy — the agent already holds the object-store
credentials (lite internal creds never leave the agent process), so no
orchestrator transfer is involved.
"""

from __future__ import annotations

import logging
import posixpath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)


def seed_workspace(src: "WorkspaceBackend", dst: "WorkspaceBackend") -> int:
    """Copy every file from ``src`` into ``dst``, preserving relative paths.

    Walks the source recursively (``src.walk()``), reads each file as bytes,
    creates the parent directory in the destination, and writes the bytes.

    Verify-before-return: after copying, every source file must exist in the
    destination. A mismatch raises ``RuntimeError`` so the caller can refuse to
    swap onto a half-seeded workspace (the upgrade then fails cleanly instead of
    silently losing files). Using the same relative path for both the write and
    the existence check makes the verification robust to per-backend path
    formatting.

    Args:
        src: Source backend (e.g. the live ``VirtualWorkspaceBackend``).
        dst: Destination backend (e.g. the freshly connected ``RemoteBackend``).

    Returns:
        The number of files written.

    Raises:
        RuntimeError: If any source file is missing from the destination after
            the copy.
    """
    files = src.walk()
    written = 0
    for rel in files:
        data = src.read_file(rel, binary=True)
        parent = posixpath.dirname(rel)
        if parent:
            dst.mkdir(parent)
        dst.write_file(rel, data)
        written += 1

    missing = [rel for rel in files if not dst.exists(rel)]
    if missing:
        raise RuntimeError(
            f"Workspace seed incomplete: {len(missing)} of {len(files)} file(s) "
            f"missing in destination (e.g. {missing[:3]})"
        )

    logger.info(
        "Seeded %d file(s) from %s to %s",
        written,
        type(src).__name__,
        type(dst).__name__,
    )
    return written
