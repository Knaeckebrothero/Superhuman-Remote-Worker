"""Canonical, non-secret identity for a protected Nextcloud source.

The database row that selects a mount is replaceable, and runtime/reader
authority rotates across Resume.  Staged bytes instead bind to the logical
cloud destination: project, workspace path, and provider-owned folder handle.
This module is deliberately pure so engage, stage, review, and Apply can all
hash and compare exactly the same object before any external effect.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from ..cloud.handles import ProjectFolderHandle


PROTECTED_SOURCE_BINDING_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BINDING_KEYS = frozenset(
    {
        "version",
        "backend",
        "backend_instance_id",
        "mount_kind",
        "source_kind",
        "source_ref",
        "target_path",
        "handle",
    }
)
_HANDLE_KEYS = frozenset({"native_id", "mountpoint"})
_SERIALIZED_HANDLE_KEYS = frozenset({"backend", "native_id", "vendor_meta"})
_NEXTCLOUD_FOLDER_ID_RE = re.compile(r"^[1-9][0-9]*$")


def _required_text(value: Any, *, single_segment: bool = False) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("protected source text is missing or malformed")
    if single_segment and (value in {".", ".."} or "/" in value or "\\" in value):
        raise ValueError("protected source segment contains a path separator")
    return value


def _canonical_uuid(value: Any, *, coordinate: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"protected source {coordinate} is not a canonical UUID")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(
            f"protected source {coordinate} is not a canonical UUID"
        ) from exc
    canonical = str(parsed)
    if not parsed.int or value != canonical:
        raise ValueError(f"protected source {coordinate} is not a canonical UUID")
    return canonical


def _canonical_target_path(value: Any) -> str:
    path = _required_text(value)
    if path.startswith("/") or "\\" in path:
        raise ValueError("protected source target path is not workspace-relative")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("protected source target path is not canonical")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class ProtectedMountSourceIdentity:
    """One canonical logical source, stable across row/G/reader replacement."""

    backend_instance_id: str
    source_ref: str
    target_path: str
    native_id: str
    mountpoint: str
    version: int = PROTECTED_SOURCE_BINDING_VERSION
    backend: str = "nextcloud"
    mount_kind: str = "project"
    source_kind: str = "project_folder"

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version != PROTECTED_SOURCE_BINDING_VERSION
        ):
            raise ValueError("protected source binding version is unsupported")
        if not isinstance(self.backend, str) or self.backend != "nextcloud":
            raise ValueError("protected source backend is unsupported")
        object.__setattr__(
            self,
            "backend_instance_id",
            _canonical_uuid(
                self.backend_instance_id,
                coordinate="backend instance",
            ),
        )
        if (
            not isinstance(self.mount_kind, str)
            or not isinstance(self.source_kind, str)
            or self.mount_kind != "project"
            or self.source_kind != "project_folder"
        ):
            raise ValueError("protected source mount shape is unsupported")
        object.__setattr__(
            self,
            "source_ref",
            _canonical_uuid(self.source_ref, coordinate="source reference"),
        )
        object.__setattr__(
            self,
            "target_path",
            _canonical_target_path(self.target_path),
        )
        native_id = _required_text(self.native_id)
        if not _NEXTCLOUD_FOLDER_ID_RE.fullmatch(native_id):
            raise ValueError("protected source folder id is not canonical")
        object.__setattr__(self, "native_id", native_id)
        object.__setattr__(
            self,
            "mountpoint",
            _required_text(self.mountpoint, single_segment=True),
        )

    @classmethod
    def from_mount_row(
        cls, row: Mapping[str, Any] | None
    ) -> ProtectedMountSourceIdentity | None:
        """Parse an eligible selected mount without adopting malformed handles."""

        if not isinstance(row, Mapping):
            return None
        if (
            row.get("backend_id") != "nextcloud"
            or row.get("mount_kind") != "project"
            or row.get("source_kind") != "project_folder"
        ):
            return None
        serialized_handle = row.get("cloud_handle")
        if not isinstance(serialized_handle, str) or not serialized_handle:
            return None
        # ProjectFolderHandle.from_db intentionally defaults/stringifies
        # malformed legacy values. Protected authority cannot adopt them: it
        # needs the provider discriminator and exact native coordinates that
        # the producer wrote.
        try:
            decoded = json.loads(serialized_handle)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, dict) or set(decoded) != _SERIALIZED_HANDLE_KEYS:
            return None
        if decoded.get("backend") != "nextcloud":
            return None
        native_id = decoded.get("native_id")
        vendor_meta = decoded.get("vendor_meta")
        if not isinstance(native_id, str) or not isinstance(vendor_meta, dict):
            return None
        mountpoint = vendor_meta.get("mountpoint")
        if not isinstance(mountpoint, str):
            return None
        try:
            return cls(
                backend_instance_id=str(row.get("backend_instance_id") or ""),
                source_ref=str(row.get("source_ref") or ""),
                target_path=row.get("target_path"),
                native_id=native_id,
                mountpoint=mountpoint,
                backend=decoded["backend"],
            )
        except (TypeError, ValueError, AttributeError):
            return None

    @classmethod
    def from_binding(
        cls,
        binding: Mapping[str, Any] | None,
        *,
        expected_sha256: str,
    ) -> ProtectedMountSourceIdentity | None:
        """Validate a persisted JSONB binding and its exact canonical digest."""

        if (
            not isinstance(binding, Mapping)
            or set(binding) != _BINDING_KEYS
            or not isinstance(expected_sha256, str)
            or not _SHA256_RE.fullmatch(expected_sha256)
        ):
            return None
        handle = binding.get("handle")
        if not isinstance(handle, Mapping) or set(handle) != _HANDLE_KEYS:
            return None
        try:
            parsed = cls(
                version=binding.get("version"),
                backend=binding.get("backend"),
                backend_instance_id=binding.get("backend_instance_id"),
                mount_kind=binding.get("mount_kind"),
                source_kind=binding.get("source_kind"),
                source_ref=binding.get("source_ref"),
                target_path=binding.get("target_path"),
                native_id=handle.get("native_id"),
                mountpoint=handle.get("mountpoint"),
            )
        except (TypeError, ValueError, AttributeError):
            return None
        try:
            persisted_canonical_json = json.dumps(
                binding,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except (TypeError, ValueError):
            return None
        if persisted_canonical_json != parsed.canonical_json:
            return None
        return parsed if parsed.sha256 == expected_sha256 else None

    def to_project_folder_handle(self) -> ProjectFolderHandle:
        """Rebuild the immutable staged Nextcloud handle centrally."""

        from ..cloud.handles import ProjectFolderHandle

        return ProjectFolderHandle(
            backend=self.backend,
            native_id=self.native_id,
            vendor_meta={"mountpoint": self.mountpoint},
        )

    @property
    def binding(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "backend": self.backend,
            "backend_instance_id": self.backend_instance_id,
            "mount_kind": self.mount_kind,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "target_path": self.target_path,
            "handle": {
                "native_id": self.native_id,
                "mountpoint": self.mountpoint,
            },
        }

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.binding,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()
