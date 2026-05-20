"""Opaque handles for main-cloud backend resources.

Every main-cloud backend returns handles that round-trip through the database
as a single TEXT column. The serialized form is either:

* A bare string — the legacy/backfill format, treated as ``native_id`` with an
  empty ``vendor_meta``. The row's ``main_cloud_backend`` column supplies the
  backend discriminator.
* A JSON object ``{"backend": ..., "native_id": ..., "vendor_meta": {...}}`` —
  used whenever an adapter needs to stash per-resource context (mountpoint
  names, drive ids, etc.) alongside the primary id.

Callers in ``orchestrator/main.py`` MUST NOT parse ``native_id`` or
``vendor_meta``; only the owning backend may do so. This keeps the interface
forward-compatible with non-WebDAV backends (see §4.1.4 of the design doc).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, NewType

UserId = NewType("UserId", str)
GroupId = NewType("GroupId", str)


def _serialize(backend: str, native_id: str, vendor_meta: dict[str, Any]) -> str:
    if not vendor_meta:
        return native_id
    return json.dumps(
        {"backend": backend, "native_id": native_id, "vendor_meta": vendor_meta},
        sort_keys=True,
    )


def _deserialize(s: str, *, backend: str) -> tuple[str, str, dict[str, Any]]:
    if s and s.startswith("{"):
        try:
            data = json.loads(s)
            return (
                str(data.get("backend", backend)),
                str(data.get("native_id", "")),
                dict(data.get("vendor_meta", {})),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return backend, s or "", {}


@dataclass(frozen=True, slots=True)
class ProjectFolderHandle:
    """Opaque handle for a shared project folder."""

    backend: str
    native_id: str
    vendor_meta: dict[str, Any] = field(default_factory=dict)

    def to_db(self) -> str:
        return _serialize(self.backend, self.native_id, self.vendor_meta)

    @classmethod
    def from_db(cls, s: str, *, backend: str) -> "ProjectFolderHandle":
        b, nid, meta = _deserialize(s, backend=backend)
        return cls(backend=b, native_id=nid, vendor_meta=meta)


@dataclass(frozen=True, slots=True)
class SessionFolderHandle:
    """Opaque handle for a per-thread session folder."""

    backend: str
    native_id: str
    vendor_meta: dict[str, Any] = field(default_factory=dict)

    def to_db(self) -> str:
        return _serialize(self.backend, self.native_id, self.vendor_meta)

    @classmethod
    def from_db(cls, s: str, *, backend: str) -> "SessionFolderHandle":
        b, nid, meta = _deserialize(s, backend=backend)
        return cls(backend=b, native_id=nid, vendor_meta=meta)


@dataclass(frozen=True, slots=True)
class ShareHandle:
    """Opaque handle for a user-folder grant record."""

    backend: str
    native_id: str
    vendor_meta: dict[str, Any] = field(default_factory=dict)

    def to_db(self) -> str:
        return _serialize(self.backend, self.native_id, self.vendor_meta)

    @classmethod
    def from_db(cls, s: str, *, backend: str) -> "ShareHandle":
        b, nid, meta = _deserialize(s, backend=backend)
        return cls(backend=b, native_id=nid, vendor_meta=meta)


@dataclass(frozen=True, slots=True)
class ProjectFolderEntry:
    """One file or directory beneath a project folder, as returned by
    ``MainCloudBackend.list_project_folder``.

    ``path`` is slash-separated, relative to the folder root, no leading
    slash, no trailing slash for directories. ``etag`` may be empty if
    the backend does not expose one.
    """

    path: str
    is_dir: bool
    size: int = 0
    etag: str = ""
    content_type: str = ""
