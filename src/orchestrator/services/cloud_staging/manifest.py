"""Derive a staged-diff manifest from an upperdir tar (protected cloud Slice C).

The tar is `tar --xattrs -C /home/agent-host/.overlay upper` streamed from the
workspace pod. Classification mirrors src/agent/services/cloud_overlay/whiteout.py
(char(0,0) whiteouts, `.wh.` name markers, opaque dirs via three xattrs or the
`.wh..wh..opq` sentinel) — constants re-declared because the orchestrator image
does not ship src/. Whiteouts/opaque dirs are expanded against the etag
baseline to per-file deletes; targets never in the baseline are no-ops
(design §11.6 amendment #2). Non-regular members (symlinks/hardlinks/specials)
cannot exist on WebDAV and are surfaced in `skipped`, never silently dropped.
"""

from __future__ import annotations

import posixpath
import tarfile

_UPPER_PREFIX = "upper/"
_WH_PREFIX = ".wh."
_OPAQUE_SENTINEL = ".wh..wh..opq"
_OPAQUE_XATTR_KEYS = (
    "SCHILY.xattr.trusted.overlay.opaque",
    "SCHILY.xattr.user.overlay.opaque",
    "SCHILY.xattr.user.fuseoverlayfs.opaque",
)
_BINARY_SNIFF_BYTES = 8192
MAX_MANIFEST_ENTRIES = 100_000
MAX_TAR_MEMBERS = 200_000
MAX_PATH_BYTES = 4096
MAX_STAGED_FILE_BYTES = 100 * 1024 * 1024
MAX_MANIFEST_JSON_BYTES = 16 * 1024 * 1024


def _rel(name: str) -> str | None:
    name = name.removeprefix("./")
    if not name.startswith(_UPPER_PREFIX):
        return None
    relative = name[len(_UPPER_PREFIX) :].rstrip("/") or None
    if relative is None:
        return None
    if (
        relative.startswith("/")
        or "\x00" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or len(relative.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES
    ):
        raise ValueError("unsafe or oversized path in upperdir tar")
    return relative


def _is_opaque(member: tarfile.TarInfo) -> bool:
    for key in _OPAQUE_XATTR_KEYS:
        val = (member.pax_headers or {}).get(key)
        if val in ("y", b"y"):
            return True
    return False


def derive_manifest(
    tar_path: str, *, baseline: dict[str, str], epoch: int, staged_at: str
) -> dict:
    if len(baseline) > MAX_MANIFEST_ENTRIES or any(
        not isinstance(path, str)
        or len(path.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES
        for path in baseline
    ):
        raise ValueError("staging baseline exceeds manifest limits")
    staged: dict[str, dict] = {}  # rel -> {size, binary}
    whiteout_targets: set[str] = set()  # file-or-dir paths whited out
    opaque_dirs: set[str] = set()
    skipped: list[dict] = []

    with tarfile.open(tar_path, "r") as tf:
        for member_count, member in enumerate(tf, start=1):
            if member_count > MAX_TAR_MEMBERS:
                raise ValueError("upperdir tar has too many members")
            rel = _rel(member.name)
            if rel is None:
                continue
            base = posixpath.basename(rel)
            if member.ischr():
                if member.devmajor == 0 and member.devminor == 0:
                    if base.startswith(_WH_PREFIX):
                        continue  # engine bookkeeping (e.g. `.wh..opq` node)
                    whiteout_targets.add(rel)
                continue
            if member.isdir():
                if _is_opaque(member):
                    opaque_dirs.add(rel)
                continue
            if not member.isreg():
                # WebDAV/Nextcloud has no symlink/device concept — these can
                # never be applied to the cloud. Surface them instead of
                # silently narrowing the review.
                kind = (
                    "symlink"
                    if member.issym()
                    else "hardlink"
                    if member.islnk()
                    else "special"
                )
                skipped.append({"path": rel, "kind": kind})
                continue
            if member.size < 0 or member.size > MAX_STAGED_FILE_BYTES:
                raise ValueError("upperdir member exceeds staged file limit")
            if base == _OPAQUE_SENTINEL:
                parent = posixpath.dirname(rel)
                if parent:
                    opaque_dirs.add(parent)
                continue
            if base.startswith(_WH_PREFIX):
                remainder = base[len(_WH_PREFIX) :]
                if not remainder:
                    raise ValueError(f"bare whiteout prefix in upperdir tar: {rel!r}")
                parent = posixpath.dirname(rel)
                whiteout_targets.add(
                    posixpath.join(parent, remainder) if parent else remainder
                )
                continue
            f = tf.extractfile(member)
            head = f.read(_BINARY_SNIFF_BYTES) if f else b""
            staged[rel] = {"size": member.size, "binary": b"\0" in head}
            if len(staged) > MAX_MANIFEST_ENTRIES:
                raise ValueError("staging manifest has too many entries")

    deleted: set[str] = set()
    for target in whiteout_targets | opaque_dirs:
        if target in baseline and target not in staged:
            deleted.add(target)
        prefix = target + "/"
        for path in baseline:
            if path.startswith(prefix) and path not in staged:
                deleted.add(path)

    entries = [
        {
            "path": p,
            "status": "modified" if p in baseline else "added",
            "size": meta["size"],
            "binary": meta["binary"],
        }
        for p, meta in staged.items()
    ] + [{"path": p, "status": "deleted", "size": 0, "binary": False} for p in deleted]
    entries.sort(key=lambda e: e["path"])
    counts = {"added": 0, "modified": 0, "deleted": 0}
    for e in entries:
        counts[e["status"]] += 1
    skipped.sort(key=lambda e: e["path"])
    return {
        "epoch": epoch,
        "staged_at": staged_at,
        "counts": counts,
        "entries": entries,
        "skipped": skipped,
    }
