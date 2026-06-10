"""The ``MainCloudBackend`` Protocol — the single contract every main-cloud
backend must satisfy.

Phase 1.5 tightened the contract:

* ``ensure_*`` methods return non-Optional handles and raise
  ``CloudBackendError`` on hard failure. Callers catch the exception —
  the "returns None on failure" convention from Phase 1 is gone.
* ``resolve_user_identity`` and ``get_user_home`` still return
  ``Optional[...]`` because "user not found" is a valid state, not an
  error.
* URL constructors stay sync and may return ``None`` (missing mountpoint,
  uninitialized backend, non-WebDAV backend, etc.).
* ``delete_*`` methods still accept ``if_exists=True`` and swallow
  ``NOT_FOUND`` in that case — the Shrine rule from §4.8.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Protocol, runtime_checkable

from .handles import (
    GroupId,
    ProjectFolderEntry,
    ProjectFolderHandle,
    SessionFolderHandle,
    ShareHandle,
    UserId,
)


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Cheap health-check result returned by ``health_check``."""

    ok: bool
    latency_ms: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class UserHome:
    """A user's home directory on the main cloud.

    ``webdav_url`` may be ``None`` for backends that do not speak WebDAV
    (Microsoft Graph). ``browser_url`` is always populated.
    """

    handle: ProjectFolderHandle
    browser_url: str
    webdav_url: Optional[str]


@dataclass(frozen=True, slots=True)
class CloudMountSubject:
    """Identity context for a user-scoped cloud mount.

    Backends use this only when the remote they are exposing belongs to a
    specific user rather than to a service/project space.
    """

    user_id: Optional[str] = None
    user_sub: Optional[str] = None
    username: Optional[str] = None


@dataclass(frozen=True, slots=True)
class RcloneMountSpec:
    """Provider-owned description of one rclone remote.

    The orchestrator serializes this into the agent's ``cloud_mount`` payload.
    The agent-side mount manager is intentionally generic: it writes the
    rclone config and starts the mount without knowing provider business rules.
    """

    source_type: str
    source_config: dict[str, Any]
    auth: dict[str, Any] = field(default_factory=dict)
    root: str = ""
    provider_flags: list[str] = field(default_factory=list)
    cache: dict[str, Any] = field(default_factory=dict)
    required_capabilities: list[str] = field(
        default_factory=lambda: ["rclone", "fuse", "rc"]
    )
    # Lowest rclone release the runtime may use for this remote (e.g. the
    # webdav `infinitescale` vendor only exists from 1.70.0). Empty = any.
    min_rclone_version: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "type": self.source_type,
            "config": dict(self.source_config),
        }
        if self.root:
            payload["root"] = self.root
        out = {
            "source": payload,
            "auth": dict(self.auth),
            "provider_flags": list(self.provider_flags),
            "cache": dict(self.cache),
            "required_capabilities": list(self.required_capabilities),
        }
        if self.min_rclone_version:
            out["min_rclone_version"] = self.min_rclone_version
        return out


@runtime_checkable
class MainCloudBackend(Protocol):
    """Contract every main-cloud backend must satisfy.

    * Methods that touch the wire are async. URL constructors are sync.
    * ``ensure_*`` methods are idempotent and return the handle for the
      resource, whether newly created or already present.
    * ``delete_*`` methods default to ``if_exists=True``; deleting a
      non-existent resource is a no-op, not an error.
    * Hard failures raise ``CloudBackendError`` with a mapped
      ``CloudBackendErrorKind`` value (§4.7). Callers pattern-match on
      ``.kind``; only the adapter sees raw vendor errors.
    """

    backend_id: str

    # ------------------------------------------------------------------ Lifecycle
    @property
    def is_configured(self) -> bool: ...

    @property
    def is_initialized(self) -> bool: ...

    async def health_check(self) -> HealthStatus: ...

    async def ensure_initialized(self) -> bool: ...

    async def close(self) -> None: ...

    # ------------------------------------------------------------------ Identity
    async def resolve_user_identity(
        self, email: Optional[str], display_name: Optional[str]
    ) -> Optional[UserId]: ...

    async def ensure_user(
        self,
        *,
        sub: str,
        issuer: str,
        email: Optional[str],
        display_name: Optional[str],
        preferred_username: Optional[str] = None,
    ) -> Optional[UserId]:
        """Proactively create the backend-side user record for an SSO identity.

        Runs at SRW first-login so session-folder sharing doesn't race the
        user's first browser login to the cloud. Idempotent — if the user
        already exists the existing ``UserId`` is returned. Backends that
        don't support admin user creation return ``None``; callers fall back
        to ``resolve_user_identity`` at share time.
        """
        ...

    async def get_user_home(self, user_id: UserId) -> Optional[UserHome]: ...

    def get_default_home_browser_url(self) -> Optional[str]:
        """Generic browser URL for the cloud's home/files view.

        Used for deep-links when the caller has no specific user context
        (e.g. cockpit's default-project cloud_storage_url). Equivalent to the
        legacy ``NextcloudAdmin.get_user_home_browser_url()`` accessor.
        """
        ...

    # -------------------------------------------------------------------- Groups
    async def ensure_group(self, group_id: GroupId) -> None: ...

    async def add_user_to_group(self, user_id: UserId, group_id: GroupId) -> None: ...

    async def remove_user_from_group(
        self, user_id: UserId, group_id: GroupId
    ) -> None: ...

    # ------------------------------------------------------------- Project folders
    async def ensure_project_folder(
        self,
        *,
        project_name: str,
        group_id: GroupId,
    ) -> ProjectFolderHandle: ...

    async def delete_project_folder(
        self,
        handle: ProjectFolderHandle,
        *,
        if_exists: bool = True,
    ) -> None: ...

    async def refresh_project_folder_access(
        self,
        handle: ProjectFolderHandle,
        group_id: GroupId,
    ) -> None: ...

    def get_project_folder_browser_url(
        self, handle: ProjectFolderHandle
    ) -> Optional[str]: ...

    def get_project_folder_webdav_url(
        self, handle: ProjectFolderHandle
    ) -> Optional[str]: ...

    async def list_project_folder(
        self,
        handle: ProjectFolderHandle,
        *,
        subpath: str = "",
    ) -> list[ProjectFolderEntry]:
        """Recursive inventory of a project folder via WebDAV ``PROPFIND``.

        ``subpath`` is relative to the folder root (slash-separated, no
        leading slash). Empty means "walk from root." Returned entries
        list every descendant file and directory, sorted by ``path``.

        Used by the job cloud-export Mode A baseline-seed
        (docs/done/job_cloud_export.md §3.1) to enumerate what to
        push into Gitea before the agent starts.
        """
        ...

    async def get_project_folder_file_bytes(
        self,
        handle: ProjectFolderHandle,
        *,
        path: str,
    ) -> bytes:
        """Read one file's raw bytes from a project folder.

        ``path`` is relative to the folder root. Raises
        ``CloudBackendError(NOT_FOUND)`` if the file is missing.
        Returns the file body as-is so binary content survives.
        """
        ...

    async def put_project_folder_file_bytes(
        self,
        handle: ProjectFolderHandle,
        *,
        path: str,
        content: bytes,
        content_type: Optional[str] = None,
    ) -> None:
        """Write one file into a project folder, creating parents as needed.

        ``path`` is slash-separated, relative to the project folder root,
        no leading slash. Parent collections are created on the way
        (WebDAV ``MKCOL`` is idempotent against existing collections).
        Used by the Mode A accept flow (job_cloud_export.md §3.5) to
        write the agent's accepted edits back to the cloud.
        """
        ...

    async def delete_project_folder_file(
        self,
        handle: ProjectFolderHandle,
        *,
        path: str,
        if_exists: bool = True,
    ) -> None:
        """Delete one file from a project folder.

        ``path`` is relative to the folder root. With ``if_exists=True``
        a missing file is treated as success (the goal state is "gone").
        Used by the Mode A accept flow when the agent deleted a file
        within the mounted project folder.
        """
        ...

    # ------------------------------------------------------------- Session folders
    async def ensure_session_folder(
        self, *, session_id: str
    ) -> SessionFolderHandle: ...

    async def delete_session_folder(
        self,
        handle: SessionFolderHandle,
        *,
        if_exists: bool = True,
    ) -> None: ...

    async def share_session_folder(
        self, handle: SessionFolderHandle, user_id: UserId
    ) -> ShareHandle: ...

    async def revoke_session_share(
        self, share: ShareHandle, *, if_exists: bool = True
    ) -> None: ...

    def get_session_folder_browser_url(
        self, handle: SessionFolderHandle
    ) -> Optional[str]: ...

    def get_session_folder_webdav_url(
        self, handle: SessionFolderHandle
    ) -> Optional[str]: ...

    async def put_session_file(
        self,
        handle: SessionFolderHandle,
        *,
        path: str,
        content: bytes,
        content_type: Optional[str] = None,
    ) -> None:
        """Upload one file into the session folder via WebDAV.

        ``path`` is relative to the session folder root, slash-separated,
        no leading slash. The implementation must MKCOL any missing
        parent collections before issuing the PUT. ``content_type`` is
        advisory; backends may pick a default if omitted.

        Used by the job cloud-export endpoint (Mode B in
        docs/done/job_cloud_export.md) to copy a completed job's
        output files into a freshly-allocated shared folder.
        """
        ...

    # ------------------------------------------------------------- Credentials
    @property
    def webdav_credentials(self) -> dict[str, str]:
        """Credentials the agent uses for WebDAV access, or ``{}`` if the
        backend does not speak WebDAV (e.g. Microsoft Graph).
        """
        ...


@runtime_checkable
class SupportsRcloneMount(Protocol):
    """Optional capability for backends that can expose handles via rclone."""

    async def build_rclone_mount_spec(
        self,
        *,
        handle: ProjectFolderHandle | SessionFolderHandle,
        mount_kind: str,
        target_path: str,
        access: Literal["read_only", "read_write"],
        subject: CloudMountSubject | None = None,
    ) -> RcloneMountSpec: ...
