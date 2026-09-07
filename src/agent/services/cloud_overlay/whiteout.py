"""Engine-agnostic overlay-upperdir → diff enumerator.

Walks an overlay upperdir (kernel overlayfs OR fuse-overlayfs — the two
produce interchangeable-enough markers, see knowledge-base/knowledge/design/cloud_access_
unification.md §3.1) and yields one DiffEntry per changed path.

Deletions surface as EITHER a char(0,0) device node at the deleted name
OR a `.wh.<name>` regular file. Directory replacement is an "opaque"
dir, marked by any of three xattrs or a `.wh..wh..opq` sentinel file.
A char(0,0) node whose OWN name starts with `.wh.` (e.g. `.wh..opq`,
observed from fuse-overlayfs after a directory rename) is engine
bookkeeping rather than a deletion, and is skipped rather than surfaced.
Everything else is a real added/modified path (reported as "present";
add-vs-modify is resolved by the caller against the lower to avoid an
rclone round-trip per file).

Phase-0-spike finding (knowledge-base/knowledge/design/cloud_access_unification.md §11.1 /
§11.6): a ``"deleted"`` ``DiffEntry`` for a directory does NOT imply the
directory existed in the lower. fuse-overlayfs marks EVERY directory
created through the merged view opaque — including brand-new,
never-in-the-lower directories — so a freshly ADDED tree surfaces as a
"deleted" opaque-dir entry paired with "present" entries for its
contents (a DELETE+PUT pair once the caller resolves add-vs-modify), and
an added *empty* directory surfaces as a bare "deleted" entry with no
paired "present" entry at all. The caller resolving `present` into
`added`/`modified`, and applying `"deleted"` entries as deletions, MUST
check lower existence before treating an opaque-marked directory as an
actual deletion — this walker reports the raw overlay signal only, it
does not consult the lower itself.

Contract note: lower/user files whose own names start with ``.wh.`` are
OUT OF CONTRACT — kernel overlayfs does not reserve that namespace, so a
real lower file named ``.wh.foo`` deleted through a kernel overlay leaves
a char(0,0) node named ``.wh.foo`` that this walker skips (a missed
deletion), and an ADDED regular ``.wh.foo`` misreads as a whiteout of
``foo`` — both pre-existing, accepted limitations. A user-created regular
``.wh..wh.x`` under kernel overlayfs hard-fails via the reserved-name
RuntimeError guard below (fail-loud by design).

Raises:
    ValueError: a malformed whiteout marker — the bare prefix ``.wh.``
        with an empty remainder (names no target, so it cannot be
        resolved to a real deleted path).
    RuntimeError: the final invariant guard below — any overlay-reserved
        name (basename starting with ``.wh.``) that leaks into the
        result. Under kernel overlayfs this is out-of-contract user input
        (a real file/dir whose own basename starts with ``.wh.``, e.g.
        ``.wh..wh.x``); the guard fails loudly rather than letting engine
        bookkeeping masquerade as a user-visible change.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from typing import Literal

Status = Literal["present", "deleted"]

_WH_PREFIX = ".wh."
_OPAQUE_SENTINEL = ".wh..wh..opq"
_OPAQUE_XATTRS = (
    "trusted.overlay.opaque",
    "user.overlay.opaque",
    "user.fuseoverlayfs.opaque",
)


@dataclass(frozen=True)
class DiffEntry:
    path: str
    status: Status


def _is_char_whiteout(st: os.stat_result) -> bool:
    return stat.S_ISCHR(st.st_mode) and st.st_rdev == os.makedev(0, 0)


def is_whiteout(name: str) -> bool:
    """Whether ``name`` — a bare basename, not a path — is a whiteout marker.

    The bare prefix ``.wh.`` (empty remainder) names no target and is NOT a
    valid whiteout; neither is the opaque sentinel.
    """
    return (
        name.startswith(_WH_PREFIX) and name != _OPAQUE_SENTINEL and name != _WH_PREFIX
    )


def is_opaque_dir(dirpath: str) -> bool:
    """Whether ``dirpath`` — a real, stat-able directory path — is opaque
    (reads the sentinel file / xattrs from disk)."""
    if os.path.exists(os.path.join(dirpath, _OPAQUE_SENTINEL)):
        return True
    for attr in _OPAQUE_XATTRS:
        try:
            if os.getxattr(dirpath, attr) == b"y":
                return True
        except OSError:
            continue
    return False


def enumerate_diff(upperdir: str) -> list[DiffEntry]:
    out: list[DiffEntry] = []
    root = os.path.abspath(upperdir)

    def rel(p: str) -> str:
        return os.path.relpath(p, root).replace(os.sep, "/")

    def walk(dirpath: str) -> None:
        opaque = is_opaque_dir(dirpath)
        if opaque and dirpath != root:
            out.append(DiffEntry(rel(dirpath), "deleted"))
        with os.scandir(dirpath) as it:
            for entry in it:
                name = entry.name
                if name == _OPAQUE_SENTINEL:
                    continue
                st = entry.stat(follow_symlinks=False)
                if _is_char_whiteout(st):
                    if name.startswith(_WH_PREFIX):
                        # Engine bookkeeping, not a deletion: e.g.
                        # fuse-overlayfs leaves a char(0,0) node named
                        # `.wh..opq` (a whiteout OF the opaque-sentinel
                        # name) in a renamed dir's upperdir after `mv
                        # lowerdir newdir`. Overlay-reserved names can
                        # never be real user files/dirs in a merged view,
                        # so this can only be internal bookkeeping —
                        # skip silently rather than emit a phantom
                        # deletion of the reserved name itself.
                        continue
                    out.append(DiffEntry(rel(entry.path), "deleted"))
                    continue
                if name.startswith(_WH_PREFIX):
                    # Prefix first, then validate the remainder: a bare
                    # `.wh.` must fail loudly, not alias to the parent dir
                    # or slip through as a "present" file.
                    remainder = name[len(_WH_PREFIX) :]
                    if not remainder:
                        raise ValueError(f"malformed whiteout marker: {entry.path!r}")
                    real = os.path.join(dirpath, remainder)
                    out.append(DiffEntry(rel(real), "deleted"))
                    continue
                if stat.S_ISDIR(st.st_mode):
                    walk(entry.path)
                else:
                    out.append(DiffEntry(rel(entry.path), "present"))

    walk(root)
    result = sorted(out, key=lambda e: e.path)
    # Defensive final guarantee: overlay-reserved names (anything whose
    # basename starts with `.wh.`) can never be a legitimate diff entry —
    # if any branch above ever leaks one, fail loudly instead of letting
    # engine bookkeeping masquerade as a user-visible change.
    for e in result:
        if os.path.basename(e.path).startswith(_WH_PREFIX):
            raise RuntimeError(
                f"invariant violated: overlay-reserved name leaked into "
                f"diff output: {e.path!r}"
            )
    return result
