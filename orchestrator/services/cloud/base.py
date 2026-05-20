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

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from .handles import (
    GroupId,
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
        docs/features/job_cloud_export.md) to copy a completed job's
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
