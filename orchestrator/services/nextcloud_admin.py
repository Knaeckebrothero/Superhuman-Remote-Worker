"""Nextcloud Admin API client for project cloud folder provisioning.

Manages Nextcloud Group Folders and group membership so that every SRW project
automatically gets a shared cloud storage folder accessible via WebDAV.

Uses two Nextcloud APIs:
    - Group Folders API:  /index.php/apps/groupfolders/folders
    - Provisioning API:   /ocs/v2.php/cloud/

Gracefully degrades — if Nextcloud is unavailable, the groupfolders app is not
installed, or credentials are missing, all methods log warnings and the system
continues without cloud folder provisioning.
"""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Nextcloud permission bitmask: read(1) + update(2) + create(4) + delete(8) + share(16)
_ALL_PERMISSIONS = 31


class NextcloudAdmin:
    """Manages Nextcloud Group Folders and groups for project integration.

    Reads configuration from environment variables:
        NEXTCLOUD_URL:            Internal base URL (default: http://localhost:8800)
        NEXTCLOUD_PUBLIC_URL:     Browser-facing URL for deep-links (default: same as NEXTCLOUD_URL)
        NEXTCLOUD_ADMIN_USER:     Admin username (default: admin)
        NEXTCLOUD_ADMIN_PASSWORD: Admin password (default: admin)
        NEXTCLOUD_AGENT_USER:     Service account username (default: agent-service)
        NEXTCLOUD_AGENT_PASSWORD: Service account password (default: agent-service-dev)
    """

    def __init__(self) -> None:
        self._base_url = os.getenv("NEXTCLOUD_URL", "http://localhost:8800").rstrip("/")
        self._public_url = os.getenv("NEXTCLOUD_PUBLIC_URL", self._base_url).rstrip("/")
        self._admin_user = os.getenv("NEXTCLOUD_ADMIN_USER", "admin")
        self._admin_password = os.getenv("NEXTCLOUD_ADMIN_PASSWORD", "admin")
        self._agent_user = os.getenv("NEXTCLOUD_AGENT_USER", "agent-service")
        self._agent_password = os.getenv(
            "NEXTCLOUD_AGENT_PASSWORD", "agent-service-dev"
        )
        self._initialized = False
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def is_configured(self) -> bool:
        """True if admin credentials are set."""
        return bool(self._admin_user and self._admin_password)

    @property
    def is_initialized(self) -> bool:
        """True if Nextcloud is reachable and groupfolders app is available."""
        return self._initialized

    @property
    def agent_user(self) -> str:
        return self._agent_user

    @property
    def agent_password(self) -> str:
        return self._agent_password

    async def ensure_initialized(self) -> bool:
        """Verify Nextcloud is reachable and the groupfolders app is installed.

        Returns:
            True if Nextcloud is ready, False if unavailable or unconfigured.
        """
        if not self.is_configured:
            logger.info(
                "Nextcloud admin not configured (NEXTCLOUD_ADMIN_USER/PASSWORD not set)"
            )
            return False

        try:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                auth=(self._admin_user, self._admin_password),
                headers={"OCS-APIRequest": "true"},
                timeout=15.0,
            )

            # Check Nextcloud is up
            resp = await self._client.get("/status.php")
            resp.raise_for_status()

            # Check groupfolders app is available
            resp = await self._client.get(
                "/index.php/apps/groupfolders/folders",
                params={"format": "json"},
            )
            if resp.status_code == 404:
                logger.warning(
                    "Nextcloud groupfolders app not installed — "
                    "cloud folder provisioning disabled"
                )
                await self._client.aclose()
                self._client = None
                return False
            resp.raise_for_status()

            self._initialized = True
            logger.info("Nextcloud admin initialized (groupfolders available)")
            return True
        except Exception as e:
            logger.warning(f"Nextcloud admin init failed: {e}")
            if self._client:
                await self._client.aclose()
                self._client = None
            return False

    # =========================================================================
    # Group Management (Provisioning API)
    # =========================================================================

    async def ensure_group(self, group_id: str) -> bool:
        """Create a Nextcloud group if it doesn't exist.

        Idempotent — returns True even if the group already exists.

        Args:
            group_id: Group name (e.g. "project-{uuid}")
        """
        if not self._initialized:
            return False
        try:
            resp = await self._client.post(
                "/ocs/v2.php/cloud/groups",
                params={"format": "json"},
                data={"groupid": group_id},
            )
            # 200 = created, 400 = already exists — both are fine
            if resp.status_code in (200, 400):
                return True
            logger.warning(
                f"Unexpected status creating group '{group_id}': {resp.status_code}"
            )
            return False
        except Exception as e:
            logger.warning(f"Failed to ensure Nextcloud group '{group_id}': {e}")
            return False

    async def resolve_nc_username(self, *candidates: str) -> str | None:
        """Find the first candidate that exists as a Nextcloud user.

        Tries two strategies:
        1. Exact user-ID lookup for each candidate (fast, covers the common
           case where the NC username *is* the email or display name).
        2. Search API with each candidate as query — catches OIDC setups
           where NC stores a UUID as user-ID but the email is searchable.
        """
        if not self._initialized:
            return None
        # Pass 1: exact user-ID match
        for name in candidates:
            if not name:
                continue
            try:
                resp = await self._client.get(
                    f"/ocs/v2.php/cloud/users/{name}",
                    params={"format": "json"},
                )
                if resp.status_code == 200:
                    return name
            except Exception:
                continue
        # Pass 2: search (matches email, display name, etc.)
        for name in candidates:
            if not name:
                continue
            try:
                resp = await self._client.get(
                    "/ocs/v2.php/cloud/users",
                    params={"format": "json", "search": name},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    users = data.get("ocs", {}).get("data", {}).get("users", [])
                    if len(users) == 1:
                        return users[0]
            except Exception:
                continue
        return None

    async def add_user_to_group(self, user_id: str, group_id: str) -> bool:
        """Add a user to a Nextcloud group (immediate, bypasses OIDC-only sync).

        Args:
            user_id: Nextcloud username (typically the user's email)
            group_id: Group name
        """
        if not self._initialized:
            return False
        try:
            resp = await self._client.post(
                f"/ocs/v2.php/cloud/users/{user_id}/groups",
                params={"format": "json"},
                data={"groupid": group_id},
            )
            if resp.status_code == 200:
                logger.debug(f"Added user '{user_id}' to Nextcloud group '{group_id}'")
                return True
            logger.warning(
                f"Failed to add user '{user_id}' to group '{group_id}': "
                f"{resp.status_code}"
            )
            return False
        except Exception as e:
            logger.warning(f"Failed to add user '{user_id}' to group '{group_id}': {e}")
            return False

    async def remove_user_from_group(self, user_id: str, group_id: str) -> bool:
        """Remove a user from a Nextcloud group.

        Args:
            user_id: Nextcloud username
            group_id: Group name
        """
        if not self._initialized:
            return False
        try:
            resp = await self._client.delete(
                f"/ocs/v2.php/cloud/users/{user_id}/groups",
                params={"format": "json"},
                data={"groupid": group_id},
            )
            if resp.status_code == 200:
                logger.debug(
                    f"Removed user '{user_id}' from Nextcloud group '{group_id}'"
                )
                return True
            logger.warning(
                f"Failed to remove user '{user_id}' from group '{group_id}': "
                f"{resp.status_code}"
            )
            return False
        except Exception as e:
            logger.warning(
                f"Failed to remove user '{user_id}' from group '{group_id}': {e}"
            )
            return False

    # =========================================================================
    # Group Folder Management (Group Folders API)
    # =========================================================================

    async def create_project_folder(
        self, project_name: str, group_id: str
    ) -> Optional[int]:
        """Create a Group Folder and grant access to the project group + srw-agents.

        Args:
            project_name: Folder mount point name (displayed in Nextcloud)
            group_id: Nextcloud group name to grant access (e.g. "project-{uuid}")

        Returns:
            Nextcloud Group Folder ID, or None on failure.
        """
        if not self._initialized:
            return None
        try:
            # 1. Create the folder
            resp = await self._client.post(
                "/index.php/apps/groupfolders/folders",
                params={"format": "json"},
                data={"mountpoint": project_name},
            )
            resp.raise_for_status()
            data = resp.json()
            folder_id = data.get("ocs", {}).get("data", {}).get("id")
            if not folder_id:
                logger.warning(
                    f"Group folder creation returned unexpected response: {data}"
                )
                return None

            # 2. Grant project group access with full permissions
            await self._grant_group_access(folder_id, group_id, _ALL_PERMISSIONS)

            # 3. Grant srw-agents group access with full permissions
            await self._grant_group_access(folder_id, "srw-agents", _ALL_PERMISSIONS)

            logger.info(
                f"Created Nextcloud Group Folder '{project_name}' "
                f"(id={folder_id}) for group '{group_id}'"
            )
            return int(folder_id)
        except Exception as e:
            logger.warning(f"Failed to create Group Folder '{project_name}': {e}")
            return None

    async def delete_project_folder(self, folder_id: int) -> bool:
        """Delete a Group Folder.

        Args:
            folder_id: Nextcloud Group Folder ID
        """
        if not self._initialized:
            return False
        try:
            resp = await self._client.delete(
                f"/index.php/apps/groupfolders/folders/{folder_id}",
                params={"format": "json"},
            )
            if resp.status_code == 200:
                logger.info(f"Deleted Nextcloud Group Folder id={folder_id}")
                return True
            logger.warning(
                f"Failed to delete Group Folder id={folder_id}: {resp.status_code}"
            )
            return False
        except Exception as e:
            logger.warning(f"Failed to delete Group Folder id={folder_id}: {e}")
            return False

    async def refresh_group_folder_access(
        self, folder_id: int, group_id: str, permissions: int = _ALL_PERMISSIONS
    ) -> bool:
        """Workaround for NC server#57445 — re-grant group access to propagate
        to new members.

        Removes and re-adds the group to the folder, which forces Nextcloud to
        recalculate share propagation.

        Args:
            folder_id: Nextcloud Group Folder ID
            group_id: Group to refresh
            permissions: Permission bitmask (default: all)
        """
        if not self._initialized:
            return False
        try:
            # Remove group access
            await self._client.delete(
                f"/index.php/apps/groupfolders/folders/{folder_id}/groups/{group_id}",
                params={"format": "json"},
            )
            # Re-grant access
            await self._grant_group_access(folder_id, group_id, permissions)
            logger.debug(
                f"Refreshed group folder access: folder={folder_id}, group={group_id}"
            )
            return True
        except Exception as e:
            logger.warning(
                f"Failed to refresh folder access (folder={folder_id}, "
                f"group={group_id}): {e}"
            )
            return False

    # =========================================================================
    # Session Folder Management (WebDAV + OCS Share API)
    # =========================================================================

    async def create_session_folder(self, folder_path: str) -> bool:
        """Create a folder in the agent-service user's home via WebDAV MKCOL.

        Args:
            folder_path: Path relative to agent-service home (e.g. "sessions/abc123")
        """
        if not self._initialized:
            return False
        try:
            # Ensure parent directories exist
            parts = folder_path.strip("/").split("/")
            for i in range(len(parts)):
                partial = "/".join(parts[: i + 1])
                url = f"/remote.php/dav/files/{self._agent_user}/{partial}"
                resp = await self._client.request(
                    "MKCOL",
                    url,
                    auth=(self._agent_user, self._agent_password),
                )
                # 201 = created, 405 = already exists — both fine
                if resp.status_code not in (201, 405):
                    logger.warning(f"MKCOL {partial} returned {resp.status_code}")

            logger.info(f"Created session folder: {folder_path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to create session folder '{folder_path}': {e}")
            return False

    async def share_folder_with_user(
        self, folder_path: str, nc_username: str
    ) -> Optional[int]:
        """Share a folder with a user via OCS Share API.

        Args:
            folder_path: Path relative to agent-service home
            nc_username: Nextcloud username to share with

        Returns:
            Share ID, or None on failure.
        """
        if not self._initialized:
            return None
        try:
            # Use agent-service credentials for sharing (folder owner)
            resp = await self._client.post(
                "/ocs/v2.php/apps/files_sharing/api/v1/shares",
                params={"format": "json"},
                data={
                    "path": f"/{folder_path}",
                    "shareType": "0",  # 0 = user share
                    "shareWith": nc_username,
                    "permissions": "15",  # read + update + create + delete (no reshare)
                },
                auth=(self._agent_user, self._agent_password),
            )
            if resp.status_code != 200:
                logger.warning(
                    f"Share folder '{folder_path}' with '{nc_username}' "
                    f"returned {resp.status_code}: {resp.text[:200]}"
                )
                return None
            data = resp.json()
            share_id = data.get("ocs", {}).get("data", {}).get("id")
            logger.info(
                f"Shared session folder '{folder_path}' with user '{nc_username}' "
                f"(share_id={share_id})"
            )
            return int(share_id) if share_id else None
        except Exception as e:
            logger.warning(
                f"Failed to share folder '{folder_path}' with '{nc_username}': {e}"
            )
            return None

    async def delete_folder(self, folder_path: str) -> bool:
        """Delete a folder from agent-service's home via WebDAV DELETE.

        Args:
            folder_path: Path relative to agent-service home
        """
        if not self._initialized:
            return False
        try:
            url = f"/remote.php/dav/files/{self._agent_user}/{folder_path}"
            resp = await self._client.delete(
                url, auth=(self._agent_user, self._agent_password)
            )
            if resp.status_code in (204, 404):
                # 204 = deleted, 404 = already gone
                logger.info(f"Deleted session folder: {folder_path}")
                return True
            logger.warning(f"Delete folder '{folder_path}' returned {resp.status_code}")
            return False
        except Exception as e:
            logger.warning(f"Failed to delete folder '{folder_path}': {e}")
            return False

    # =========================================================================
    # URL Generation
    # =========================================================================

    def get_session_folder_webdav_url(self, folder_path: str) -> str:
        """Return the internal WebDAV URL for agent access to a session folder."""
        return (
            f"{self._base_url}/remote.php/dav/files/{self._agent_user}/{folder_path}/"
        )

    def get_session_folder_browser_url(self, folder_path: str) -> str:
        """Return the browser URL for a user to view a shared session folder.

        The shared folder appears in the user's file tree under the folder name.
        """
        folder_name = folder_path.rstrip("/").split("/")[-1]
        return f"{self._public_url}/apps/files/?dir=/{folder_name}"

    def get_folder_webdav_url(self, folder_name: str) -> str:
        """Return the internal WebDAV URL for agent access to a Group Folder.

        Uses the dedicated groupfolders WebDAV endpoint so agent-service only
        sees Group Folders, not its personal files.
        """
        return (
            f"{self._base_url}/remote.php/dav/groupfolders/"
            f"{self._agent_user}/{folder_name}/"
        )

    def get_folder_browser_url(self, folder_name: str) -> str:
        """Return the browser URL for deep-linking to a Group Folder.

        Group Folders appear at the root of the user's file tree in Nextcloud.
        """
        return f"{self._public_url}/apps/files/?dir=/{folder_name}"

    def get_user_home_webdav_url(self, username: str) -> str:
        """Return the internal WebDAV URL for a user's home directory.

        Used for personal/default project cloud storage.
        """
        return f"{self._base_url}/remote.php/dav/files/{username}/"

    def get_user_home_browser_url(self) -> str:
        """Return the browser URL for the user's Nextcloud home."""
        return f"{self._public_url}/apps/files/"

    # =========================================================================
    # Internal helpers
    # =========================================================================

    async def _grant_group_access(
        self, folder_id: int, group_id: str, permissions: int
    ) -> None:
        """Grant a group access to a folder with the given permissions."""
        # Add group to folder
        resp = await self._client.post(
            f"/index.php/apps/groupfolders/folders/{folder_id}/groups",
            params={"format": "json"},
            data={"group": group_id},
        )
        resp.raise_for_status()

        # Set permissions
        resp = await self._client.post(
            f"/index.php/apps/groupfolders/folders/{folder_id}/groups/{group_id}",
            params={"format": "json"},
            data={"permissions": str(permissions)},
        )
        resp.raise_for_status()
