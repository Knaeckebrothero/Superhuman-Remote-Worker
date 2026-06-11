"""rclone-subprocess :class:`ObjectStore` — the production virtual-tier transport.

Chosen for provider continuity with ``RcloneMountSpec``
(``docs/features/no_workspace_agent_mode.md`` §5): the same ``{type, config}``
remote descriptor that drives FUSE mounts on full workspaces describes the
explicit-op remote here, so S3, WebDAV, Drive, etc. all work from one spec.

This is a fixed-destination typed client in the agent pod (§3.1) — it shells
out to ``rclone`` but only ever against the one remote it was constructed for.
The remote is configured via ``RCLONE_CONFIG_<NAME>_<KEY>`` environment
variables passed to each child process, so credentials never appear in
``argv`` (no ``ps`` leak) and no config file is written to disk.

Object keys map to rclone paths as ``<name>:<root>/<key>`` where ``root`` is
the bucket (or bucket/subpath) from the spec and ``key`` is the full,
prefix-inclusive key the backend hands us. Requires rclone ≥ 1.59 for
``lsjson --stat``.
"""

import json
import logging
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from .object_store import ObjectInfo, ObjectStore, ObjectStoreError

logger = logging.getLogger(__name__)

# Default per-operation subprocess timeouts (seconds).
_DEFAULT_TRANSFER_TIMEOUT = 300  # get/put — may move large objects
_DEFAULT_META_TIMEOUT = 60  # head/list/delete/copy — metadata ops

# rclone signals "the thing isn't there" with various wordings across backends;
# we only need to tell a missing object apart from a real transport error.
_NOT_FOUND_RE = re.compile(
    r"not found|doesn't exist|does not exist|no such|not exist", re.IGNORECASE
)

_DEFAULT_REMOTE_NAME = "srw"


def _env_key(remote_name: str, option: str) -> str:
    """rclone env-var name for one remote option (e.g. RCLONE_CONFIG_SRW_TYPE)."""
    safe = re.sub(r"[^A-Za-z0-9]", "_", option).upper()
    return f"RCLONE_CONFIG_{remote_name.upper()}_{safe}"


class RcloneObjectStore(ObjectStore):
    """ObjectStore backed by ``rclone`` subprocess calls against one remote.

    Args:
        remote_type: rclone backend type (``s3``, ``webdav``, ...).
        config: rclone config key/values for that backend (access_key_id,
            secret_access_key, endpoint, provider, region, ...). Passed via
            env so secrets stay out of argv. Obscure passwords upstream if the
            backend requires it (S3 access keys are plain).
        root: Base path within the remote — the bucket, or ``bucket/subpath``.
        remote_name: Internal rclone remote alias (cosmetic; default ``srw``).
        transfer_timeout / meta_timeout: Per-op subprocess timeouts.
        rclone_bin: rclone executable name/path.
    """

    def __init__(
        self,
        remote_type: str,
        config: Optional[Dict[str, Any]] = None,
        root: str = "",
        *,
        remote_name: str = _DEFAULT_REMOTE_NAME,
        transfer_timeout: int = _DEFAULT_TRANSFER_TIMEOUT,
        meta_timeout: int = _DEFAULT_META_TIMEOUT,
        rclone_bin: str = "rclone",
    ):
        if not remote_type:
            raise ValueError("RcloneObjectStore requires a remote_type")
        self._type = remote_type
        self._config = dict(config or {})
        self._root = root.strip("/")
        self._remote_name = remote_name
        self._transfer_timeout = transfer_timeout
        self._meta_timeout = meta_timeout
        self._rclone_bin = rclone_bin

        # Child-process environment: parent env is inherited at call time; here
        # we only assemble the remote-defining overlay so secrets are scoped to
        # the rclone children, never the agent's own environment.
        self._env_overlay: Dict[str, str] = {
            _env_key(remote_name, "type"): remote_type,
        }
        for key, value in self._config.items():
            self._env_overlay[_env_key(remote_name, key)] = str(value)

    # =========================================================================
    # Path mapping
    # =========================================================================

    def _remote_path(self, key: str) -> str:
        """Object key -> ``<name>:<root>/<key>`` rclone address."""
        base = f"{self._root}/" if self._root else ""
        return f"{self._remote_name}:{base}{key}"

    # =========================================================================
    # Subprocess plumbing
    # =========================================================================

    def _run(
        self,
        args: List[str],
        *,
        input_bytes: Optional[bytes] = None,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        import os

        env = dict(os.environ)
        env.update(self._env_overlay)
        try:
            return subprocess.run(
                [self._rclone_bin, *args],
                input=input_bytes,
                capture_output=True,
                env=env,
                timeout=timeout or self._meta_timeout,
            )
        except FileNotFoundError as exc:
            raise ObjectStoreError(
                f"rclone binary '{self._rclone_bin}' not found on PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ObjectStoreError(f"rclone {args[0]} timed out") from exc

    @staticmethod
    def _stderr(proc: subprocess.CompletedProcess) -> str:
        return (proc.stderr or b"").decode("utf-8", "replace")

    def _is_not_found(self, proc: subprocess.CompletedProcess) -> bool:
        return bool(_NOT_FOUND_RE.search(self._stderr(proc)))

    # =========================================================================
    # ObjectStore interface
    # =========================================================================

    def get(self, key: str) -> bytes:
        proc = self._run(
            ["cat", self._remote_path(key)], timeout=self._transfer_timeout
        )
        if proc.returncode == 0:
            return proc.stdout
        if self._is_not_found(proc):
            raise FileNotFoundError(key)
        raise ObjectStoreError(f"rclone cat failed for {key}: {self._stderr(proc)}")

    def put(self, key: str, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(
                f"ObjectStore.put requires bytes, got {type(data).__name__}"
            )
        proc = self._run(
            ["rcat", self._remote_path(key)],
            input_bytes=bytes(data),
            timeout=self._transfer_timeout,
        )
        if proc.returncode != 0:
            raise ObjectStoreError(
                f"rclone rcat failed for {key}: {self._stderr(proc)}"
            )

    def head(self, key: str) -> Optional[int]:
        # lsjson --stat describes the leaf itself: a file yields IsDir=false +
        # Size; a directory prefix yields IsDir=true (which is NOT a file, so
        # None); a missing path is a non-zero exit (None). This is how we keep
        # the file-vs-prefix distinction crisp in a flat key space.
        proc = self._run(["lsjson", "--stat", self._remote_path(key)])
        if proc.returncode != 0:
            return None
        try:
            info = json.loads(proc.stdout.decode("utf-8") or "null")
        except (ValueError, UnicodeDecodeError):
            return None
        if not info or info.get("IsDir", False):
            return None
        return int(info.get("Size", 0))

    def list(self, prefix: str) -> List[ObjectInfo]:
        proc = self._run(
            [
                "lsjson",
                "--recursive",
                "--files-only",
                "--no-modtime",
                self._remote_path(prefix),
            ]
        )
        if proc.returncode != 0:
            if self._is_not_found(proc):
                return []
            raise ObjectStoreError(
                f"rclone lsjson failed for {prefix}: {self._stderr(proc)}"
            )
        try:
            entries = json.loads(proc.stdout.decode("utf-8") or "[]")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ObjectStoreError(
                f"rclone lsjson returned unparseable JSON for {prefix}"
            ) from exc
        results = [
            # Path is relative to the listed prefix; rebuild the full key.
            ObjectInfo(key=prefix + entry["Path"], size=int(entry.get("Size", 0)))
            for entry in entries
            if not entry.get("IsDir", False)
        ]
        return sorted(results, key=lambda info: info.key)

    def delete(self, key: str) -> bool:
        proc = self._run(["deletefile", self._remote_path(key)])
        if proc.returncode == 0:
            return True
        if self._is_not_found(proc):
            return False
        raise ObjectStoreError(
            f"rclone deletefile failed for {key}: {self._stderr(proc)}"
        )

    def copy(self, src: str, dst: str) -> None:
        # Server-side where the backend supports it (S3 CopyObject); rclone
        # falls back to download+upload otherwise.
        proc = self._run(
            ["copyto", self._remote_path(src), self._remote_path(dst)],
            timeout=self._transfer_timeout,
        )
        if proc.returncode != 0:
            if self._is_not_found(proc):
                raise FileNotFoundError(src)
            raise ObjectStoreError(
                f"rclone copyto failed {src} -> {dst}: {self._stderr(proc)}"
            )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def connect(self) -> None:
        # Light check: the binary must exist. Remote reachability/credentials
        # surface on the first real operation rather than blocking startup.
        if shutil.which(self._rclone_bin) is None:
            raise ObjectStoreError(
                f"rclone binary '{self._rclone_bin}' not found on PATH; the "
                "virtual workspace tier requires rclone in the agent image"
            )

    def close(self) -> None:
        pass


def object_store_from_spec(spec: Dict[str, Any]) -> ObjectStore:
    """Build an :class:`ObjectStore` from a dispatch mount/object-store spec.

    Accepts either a §4 mount dict (with a nested ``rclone_spec``) or a bare
    rclone spec ``{type, config, root}``. The dev-only type ``memory`` returns
    an :class:`InMemoryObjectStore` (handy for local smoke tests without a real
    remote; non-durable, so never a real deployment target).

    Raises:
        ValueError: If no remote type can be determined.
    """
    spec = spec or {}
    rspec = spec.get("rclone_spec") or spec
    remote_type = rspec.get("type")
    if not remote_type:
        raise ValueError(
            "object_store_from_spec: spec has no rclone remote 'type' "
            "(expected {type, config, root} or {rclone_spec: {...}})"
        )

    if remote_type == "memory":
        from .object_store import InMemoryObjectStore

        return InMemoryObjectStore()

    config = rspec.get("config") or {}
    # Bucket/root may live on the rclone spec (`root`) or the outer mount
    # (`root`/`bucket`), mirroring cloud_mount's `source.root`.
    root = rspec.get("root") or spec.get("root") or spec.get("bucket") or ""
    return RcloneObjectStore(remote_type=remote_type, config=config, root=root)
