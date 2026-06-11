"""Object-store seam for the ``virtual`` workspace backend.

``VirtualWorkspaceBackend`` (the no-workspace ``virtual`` tier — see
``docs/features/no_workspace_agent_mode.md`` §5) translates the
``WorkspaceBackend`` file contract into flat object-store key operations.
This module defines that narrow seam so the backend's logic is
transport-agnostic:

- unit tests run the backend over :class:`InMemoryObjectStore`;
- production runs it over ``RcloneObjectStore`` (``rclone.py``), chosen for
  provider continuity with ``RcloneMountSpec`` (§5).

Keys are flat, posix-style strings (e.g. ``jobs/<id>/documents/x.md``). There
is **no** directory concept — "directories" are emergent from shared key
prefixes, exactly as in S3. All path/prefix math lives in the backend; the
store only moves bytes by key. This is deliberately the smallest surface that
``VirtualWorkspaceBackend`` needs, so swapping rclone for boto3 later (the v1.1
revisit in §10) is a single new ``ObjectStore`` subclass.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class ObjectInfo:
    """One object in the store.

    Attributes:
        key: The full, prefix-inclusive object key (not relative to any
            workspace prefix — the backend strips its own prefix).
        size: Object size in bytes.
    """

    key: str
    size: int


class ObjectStoreError(Exception):
    """Transport-level failure talking to the object store.

    Distinct from ``FileNotFoundError`` (a missing object, which is a normal
    control-flow signal): this means the store itself could not be reached or
    returned an unexpected error.
    """


class ObjectStore(ABC):
    """Narrow flat key/value blob store the virtual backend builds file ops on.

    Implementations move opaque bytes addressed by a flat string key. They do
    not interpret keys, enforce boundaries, or model directories — that is the
    backend's job. ``get``/``head`` use ``FileNotFoundError`` to signal a
    missing object; genuine transport failures raise :class:`ObjectStoreError`.
    """

    @abstractmethod
    def get(self, key: str) -> bytes:
        """Return the object's bytes.

        Raises:
            FileNotFoundError: If no object exists at ``key``.
            ObjectStoreError: On transport failure.
        """
        ...

    @abstractmethod
    def put(self, key: str, data: bytes) -> None:
        """Create or overwrite the object at ``key`` with ``data`` (bytes)."""
        ...

    @abstractmethod
    def head(self, key: str) -> Optional[int]:
        """Return the object's size in bytes, or ``None`` if it doesn't exist.

        Never raises ``FileNotFoundError`` — absence is reported as ``None`` so
        callers can probe existence in one call.
        """
        ...

    @abstractmethod
    def list(self, prefix: str) -> List[ObjectInfo]:
        """Return all objects whose key starts with ``prefix`` (recursive).

        An empty ``prefix`` lists the whole store. Order is not guaranteed by
        the contract, but implementations sort by key for determinism.
        """
        ...

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete the object at ``key``.

        Returns:
            True if an object existed and was deleted, False if it was absent.
            Idempotent: deleting a missing key is not an error.
        """
        ...

    # --- optional, overridable for server-side efficiency ---

    def copy(self, src: str, dst: str) -> None:
        """Copy one object from ``src`` to ``dst``.

        Default implementation is a client-side read+write; transports with a
        server-side copy (S3 ``CopyObject``, ``rclone copyto``) should override.

        Raises:
            FileNotFoundError: If ``src`` does not exist.
        """
        self.put(dst, self.get(src))

    # --- lifecycle (default no-op) ---

    def connect(self) -> None:
        """Establish or verify the transport. Default no-op."""

    def close(self) -> None:
        """Release transport resources. Default no-op."""


_MISSING = object()


class InMemoryObjectStore(ObjectStore):
    """Process-local, dict-backed :class:`ObjectStore`.

    Used by the backend's contract tests and viable for lightweight dev, but
    **not a production transport**: it is per-process and non-durable, which
    defeats the "workspace survives pod death" property the ``virtual`` tier is
    built for (§5). Production uses ``RcloneObjectStore``.
    """

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def get(self, key: str) -> bytes:
        try:
            return self._data[key]
        except KeyError:
            raise FileNotFoundError(key) from None

    def put(self, key: str, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(
                f"ObjectStore.put requires bytes, got {type(data).__name__}"
            )
        self._data[key] = bytes(data)

    def head(self, key: str) -> Optional[int]:
        value = self._data.get(key, _MISSING)
        return None if value is _MISSING else len(value)

    def list(self, prefix: str) -> List[ObjectInfo]:
        return sorted(
            (
                ObjectInfo(key=k, size=len(v))
                for k, v in self._data.items()
                if k.startswith(prefix)
            ),
            key=lambda info: info.key,
        )

    def delete(self, key: str) -> bool:
        return self._data.pop(key, _MISSING) is not _MISSING
