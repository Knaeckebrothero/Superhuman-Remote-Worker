"""Virtual workspace backend — explicit object-store file ops, no workspace pod.

The ``virtual`` tier (``docs/features/no_workspace_agent_mode.md`` §5) gives a
lite agent real file tools without provisioning a workspace container/VM: file
operations are explicit object-store ops against a per-job/per-thread key
prefix, so files are durable in object storage and survive pod death. There is
**no** FUSE, no mount, no privileged pod, and no shell.

This backend implements the full :class:`WorkspaceBackend` contract as prefix
math over an injected :class:`~src.core.backends.object_store.ObjectStore`
(rclone in production, in-memory in tests — the logic is identical). Because S3
is a flat key space, a few POSIX semantics are approximated; each such
"S3-ism" is called out inline and pinned by the contract tests:

- **Empty directories** don't exist in object storage. ``mkdir`` writes a
  hidden ``.keep`` marker so a created-but-empty directory is still visible to
  ``is_dir``/``list_dir`` (and the marker is filtered out of listings/search).
- **``append_file``/``edit`` are read-modify-write** with no atomicity — safe
  under the tier's single-writer-per-prefix assumption (§5).
- **``search_files`` is bounded** content search (binary extensions skipped,
  per-file byte cap) — the hydration-cost lesson from rclone_cloud_mount §8 in
  miniature.
- **Reads are size-guarded** so a huge object can't OOM the agent pod.

``supports_shell`` is False and there is no ``$HOME`` concept — ``write_home_file``
and ``resolve_home_path`` inherit the base ``NotImplementedError`` (the lite
tier excludes shell/git/repository datasources by construction; §7).
"""

import fnmatch
import logging
import posixpath

from ..workspace_backend import WorkspaceBackend
from .object_store import ObjectStore

logger = logging.getLogger(__name__)

# Hidden marker that keeps an otherwise-empty directory visible in a flat key
# space. Filtered out of list_dir() / search_files() so the agent never sees it.
_DIR_MARKER = ".keep"

# Single-object read guard (bytes). The word-count truncation lives higher up
# (file tools / max_read_words); this is the hard ceiling that stops a
# multi-GB object from being pulled into the agent pod's memory.
DEFAULT_MAX_READ_BYTES = 50 * 1024 * 1024  # 50 MiB

# search_files bounds (the hydration guard). Files larger than this are skipped
# for content search; name/path matching is unaffected.
DEFAULT_SEARCH_FILE_BYTES = 1 * 1024 * 1024  # 1 MiB

# Extensions skipped during content search (binary / non-text payloads).
_BINARY_SEARCH_EXTS = frozenset(
    {
        ".pdf",
        ".docx",
        ".doc",
        ".xlsx",
        ".pptx",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".ico",
        ".zip",
        ".gz",
        ".tar",
        ".bz2",
        ".7z",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".wav",
        ".so",
        ".bin",
        ".o",
        ".pyc",
        ".woff",
        ".woff2",
        ".ttf",
    }
)


class VirtualWorkspaceBackend(WorkspaceBackend):
    """WorkspaceBackend backed by explicit object-store ops under a key prefix.

    Args:
        object_store: The blob transport (rclone in prod, in-memory in tests).
        prefix: Per-job/per-thread key prefix (e.g. ``jobs/<job_id>/``). Leading
            slashes are stripped and a trailing slash is enforced; an empty
            prefix means the whole store is the workspace (rare; for tests).
        max_read_bytes: Single-object read ceiling.
        search_file_bytes: Per-file content-search ceiling.
    """

    def __init__(
        self,
        object_store: ObjectStore,
        prefix: str = "",
        *,
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
        search_file_bytes: int = DEFAULT_SEARCH_FILE_BYTES,
    ):
        self._store = object_store
        self._prefix = self._normalize_prefix(prefix)
        self._max_read_bytes = max_read_bytes
        self._search_file_bytes = search_file_bytes
        self._connected = False

    # =========================================================================
    # Path / prefix math (escape-proof by construction — no real filesystem)
    # =========================================================================

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        prefix = (prefix or "").strip().lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        return prefix

    def _normalize_rel(self, relative_path: str) -> str:
        """Normalize a workspace-relative path; raise if it escapes the root.

        Returns "" for the workspace root. The result never starts with "/"
        or "..".
        """
        if not relative_path:
            return ""
        p = relative_path.strip()
        if p in ("", ".", "./"):
            return ""
        if p.startswith("/"):
            raise ValueError(f"Path '{relative_path}' escapes workspace boundary")
        norm = posixpath.normpath(p)
        if norm == ".":
            return ""
        if norm == ".." or norm.startswith("../"):
            raise ValueError(f"Path '{relative_path}' escapes workspace boundary")
        return norm

    def _object_key(self, relative_path: str) -> str:
        """Full object key for a file at ``relative_path`` (prefix + normalized)."""
        return self._prefix + self._normalize_rel(relative_path)

    def _dir_prefix(self, relative_path: str) -> str:
        """Key prefix that contains the children of the directory at the path."""
        norm = self._normalize_rel(relative_path)
        if norm == "":
            return self._prefix
        return self._prefix + norm + "/"

    def _strip_prefix(self, key: str) -> str:
        """Object key -> workspace-root-relative path."""
        if self._prefix and key.startswith(self._prefix):
            return key[len(self._prefix) :]
        return key

    # =========================================================================
    # Properties
    # =========================================================================

    @property
    def root(self) -> str:
        """The object-store key prefix that is this workspace's virtual root."""
        return self._prefix

    @property
    def supports_shell(self) -> bool:
        # Explicit: the virtual tier has no shell. ShellManager refuses to
        # construct without a shell-capable backend, so shell tools are absent.
        return False

    # =========================================================================
    # File operations
    # =========================================================================

    def read_file(self, path: str, binary: bool = False) -> str | bytes:
        key = self._object_key(path)
        size = self._store.head(key)
        if size is None:
            # No object at this exact key. Mirror the filesystem backend: a
            # directory path is a "Not a file" error, a truly absent path is
            # FileNotFoundError.
            if self.is_dir(path):
                raise ValueError(f"Not a file: {path}")
            raise FileNotFoundError(f"File not found: {path}")
        if size > self._max_read_bytes:
            raise ValueError(
                f"File '{path}' is {size} bytes, exceeds the read limit of "
                f"{self._max_read_bytes} bytes"
            )
        data = self._store.get(key)
        if binary:
            return data
        return data.decode("utf-8")

    def write_file(self, path: str, content: str | bytes) -> None:
        norm = self._normalize_rel(path)
        if norm == "":
            raise ValueError("Cannot write to the workspace root as a file")
        # Refuse to clobber a directory with a file (matches POSIX semantics;
        # cheap because is_dir is a single prefix probe).
        if self.is_dir(path):
            raise ValueError(f"Is a directory: {path}")
        data = content.encode("utf-8") if isinstance(content, str) else content
        self._store.put(self._prefix + norm, data)

    def append_file(self, path: str, content: str) -> None:
        # Read-modify-write: not atomic. Safe under single-writer-per-prefix.
        norm = self._normalize_rel(path)
        if norm == "":
            raise ValueError("Cannot append to the workspace root")
        key = self._prefix + norm
        addition = content.encode("utf-8") if isinstance(content, str) else content
        existing = self._store.get(key) if self._store.head(key) is not None else b""
        self._store.put(key, existing + addition)

    def exists(self, path: str) -> bool:
        return self.is_file(path) or self.is_dir(path)

    def is_file(self, path: str) -> bool:
        norm = self._normalize_rel(path)
        if norm == "":
            return False  # the root is a directory, never a file
        return self._store.head(self._prefix + norm) is not None

    def is_dir(self, path: str) -> bool:
        norm = self._normalize_rel(path)
        if norm == "":
            return True  # the workspace root always exists as a directory
        # A directory exists iff some object lives under "<prefix><norm>/".
        return bool(self._store.list(self._prefix + norm + "/"))

    def list_dir(self, path: str = "", pattern: str = "*") -> list[str]:
        norm = self._normalize_rel(path)
        if norm != "" and not self.is_dir(path):
            # Matches the filesystem backend: a file path lists itself; a
            # non-existent path lists nothing.
            return [norm] if self.is_file(path) else []

        dir_prefix = self._dir_prefix(path)
        rel_root = norm + "/" if norm else ""

        files: set[str] = set()
        dirs: set[str] = set()
        for info in self._store.list(dir_prefix):
            remainder = info.key[len(dir_prefix) :]
            if not remainder:
                continue
            head, slash, _ = remainder.partition("/")
            if slash:
                dirs.add(head)
            elif head != _DIR_MARKER:  # hide the empty-dir marker
                files.add(head)

        results: list[str] = []
        for name in files:
            if fnmatch.fnmatch(name, pattern):
                results.append(rel_root + name)
        for name in dirs:
            if fnmatch.fnmatch(name, pattern):
                results.append(rel_root + name + "/")
        return sorted(results)

    def search_files(
        self, query: str, path: str = "", case_sensitive: bool = False
    ) -> list[dict]:
        # Bounded content search (hydration guard): binary extensions skipped,
        # per-file byte cap. Name/path search is implicit via list_dir.
        if self.is_file(path):
            keys = [self._object_key(path)]
        else:
            keys = [info.key for info in self._store.list(self._dir_prefix(path))]

        needle = query if case_sensitive else query.lower()
        results: list[dict] = []
        for key in keys:
            name = posixpath.basename(key)
            if name == _DIR_MARKER:
                continue
            ext = posixpath.splitext(name)[1].lower()
            if ext in _BINARY_SEARCH_EXTS:
                continue
            size = self._store.head(key)
            if size is None or size > self._search_file_bytes:
                continue
            try:
                content = self._store.get(key).decode("utf-8")
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            rel = self._strip_prefix(key)
            for i, line in enumerate(content.splitlines(), 1):
                hay = line if case_sensitive else line.lower()
                if needle in hay:
                    results.append(
                        {"path": rel, "line_number": i, "line": line.strip()}
                    )
        return results

    def mkdir(self, path: str) -> None:
        norm = self._normalize_rel(path)
        if norm == "":
            return  # root always exists; nothing to create
        # Flat store has no real directories: drop a hidden marker so the
        # (possibly empty) directory is visible. Intermediate parents are
        # emergent from this key's prefix, so a single marker suffices.
        self._store.put(self._prefix + norm + "/" + _DIR_MARKER, b"")

    def delete_file(self, path: str) -> bool:
        if self.is_file(path):
            self._store.delete(self._object_key(path))
            return True
        if self.is_dir(path):
            # Only an empty directory (just its .keep marker) may go via
            # delete_file; a populated one is a ValueError, as on a filesystem.
            if self.list_dir(path):
                raise ValueError(f"Cannot delete non-empty directory: {path}")
            self._store.delete(self._dir_prefix(path) + _DIR_MARKER)
            return True
        return False

    def delete_directory(self, path: str) -> bool:
        norm = self._normalize_rel(path)
        if norm == "":
            raise ValueError("Cannot delete workspace root directory")
        if not self.is_dir(path):
            if self.is_file(path):
                raise ValueError(f"Not a directory: {path}")
            return False
        deleted = False
        for info in self._store.list(self._dir_prefix(path)):
            self._store.delete(info.key)
            deleted = True
        return deleted

    def move(self, src: str, dst: str) -> None:
        if not self.exists(src):
            raise FileNotFoundError(f"Source not found: {src}")
        if self.is_file(src):
            self._store.copy(self._object_key(src), self._object_key(dst))
            self._store.delete(self._object_key(src))
            return
        # Directory move: relocate the whole subtree key-by-key.
        src_prefix = self._dir_prefix(src)
        dst_prefix = self._dir_prefix(dst)
        for info in self._store.list(src_prefix):
            new_key = dst_prefix + info.key[len(src_prefix) :]
            self._store.copy(info.key, new_key)
            self._store.delete(info.key)

    def copy(self, src: str, dst: str) -> None:
        if not self.exists(src):
            raise FileNotFoundError(f"Source not found: {src}")
        if self.is_dir(src) and not self.is_file(src):
            raise ValueError(f"Cannot copy directory: {src}. Use move for directories.")
        self._store.copy(self._object_key(src), self._object_key(dst))

    def stat(self, path: str) -> int:
        key = self._object_key(path)
        size = self._store.head(key)
        if size is not None:
            return size
        total = 0
        found = False
        for info in self._store.list(self._dir_prefix(path)):
            total += info.size
            found = True
        return total if found else 0

    def resolve_path(self, relative_path: str) -> str:
        # Canonical form for a virtual workspace is the full object key.
        return self._object_key(relative_path)

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def connect(self) -> None:
        self._store.connect()
        self._connected = True

    def disconnect(self) -> None:
        try:
            self._store.close()
        finally:
            self._connected = False

    def is_connected(self) -> bool:
        return self._connected
