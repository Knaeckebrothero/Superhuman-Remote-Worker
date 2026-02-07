"""Gitea client for workspace delivery.

Provides a GiteaClient that bootstraps an admin user on a Gitea instance
and creates per-job repositories. Agents push workspace contents to these
repos so users can browse deliverables via Gitea's web UI.

Gracefully degrades — if Gitea is unavailable, all methods return safe
defaults and the system continues without workspace delivery.
"""

import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class GiteaClient:
    """Async HTTP client for Gitea API.

    Reads configuration from environment variables:
        GITEA_URL: Base URL of the Gitea instance (e.g. http://gitea:3000)
        GITEA_ADMIN_USER: Admin username to create/use
        GITEA_ADMIN_PASSWORD: Admin password
    """

    def __init__(self) -> None:
        self._url = os.environ.get("GITEA_URL", "").rstrip("/")
        self._user = os.environ.get("GITEA_ADMIN_USER", "graphrag")
        self._password = os.environ.get("GITEA_ADMIN_PASSWORD", "graphrag_gitea")
        self._initialized = False
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def is_configured(self) -> bool:
        """True if GITEA_URL is set."""
        return bool(self._url)

    @property
    def is_initialized(self) -> bool:
        """True if admin user and access are verified."""
        return self._initialized

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                auth=(self._user, self._password),
            )
        return self._client

    async def ensure_initialized(self) -> bool:
        """Bootstrap admin user and verify access.

        Creates the admin user via Gitea's sign-up API (first user becomes
        admin). If the user already exists, verifies credentials work.

        Returns:
            True if Gitea is ready, False if unavailable or setup failed.
        """
        if not self.is_configured:
            logger.info("Gitea not configured (GITEA_URL not set), workspace delivery disabled")
            return False

        client = self._get_client()

        # Check if Gitea is reachable
        try:
            resp = await client.get(f"{self._url}/api/v1/version")
            if resp.status_code != 200:
                logger.warning(f"Gitea not reachable (status {resp.status_code})")
                return False
            logger.info(f"Gitea reachable: {resp.json().get('version', 'unknown')}")
        except httpx.HTTPError as e:
            logger.warning(f"Gitea not reachable: {e}")
            return False

        # Try to authenticate with existing user
        try:
            resp = await client.get(f"{self._url}/api/v1/user")
            if resp.status_code == 200:
                logger.info(f"Gitea admin user '{self._user}' authenticated")
                self._initialized = True
                return True
        except httpx.HTTPError:
            pass

        # User doesn't exist — create via sign-up (first user = admin)
        try:
            signup_data = {
                "username": self._user,
                "email": f"{self._user}@graphrag.local",
                "password": self._password,
                "must_change_password": False,
                "send_notify": False,
            }
            # Use unauthenticated request for sign-up
            async with httpx.AsyncClient(timeout=30.0) as anon_client:
                resp = await anon_client.post(
                    f"{self._url}/api/v1/admin/users",
                    json=signup_data,
                    auth=(self._user, self._password),
                )

                if resp.status_code in (201, 422):
                    # 201 = created, 422 = already exists
                    if resp.status_code == 422:
                        logger.info("Admin user creation returned 422 (may already exist)")
                    else:
                        logger.info(f"Created Gitea admin user '{self._user}'")
                else:
                    # Try user registration endpoint as fallback
                    resp = await anon_client.post(
                        f"{self._url}/user/sign_up",
                        data={
                            "user_name": self._user,
                            "email": f"{self._user}@graphrag.local",
                            "password": self._password,
                            "retype": self._password,
                        },
                    )
                    if resp.status_code in (200, 302, 303):
                        logger.info(f"Registered Gitea user '{self._user}' via sign-up form")
                    else:
                        logger.warning(
                            f"Failed to create Gitea user (status {resp.status_code}): "
                            f"{resp.text[:200]}"
                        )
                        return False

        except httpx.HTTPError as e:
            logger.warning(f"Failed to create Gitea admin user: {e}")
            return False

        # Verify access after creation
        try:
            resp = await client.get(f"{self._url}/api/v1/user")
            if resp.status_code == 200:
                self._initialized = True
                logger.info("Gitea workspace delivery initialized")
                return True
            else:
                logger.warning(f"Gitea auth verification failed (status {resp.status_code})")
                return False
        except httpx.HTTPError as e:
            logger.warning(f"Gitea auth verification failed: {e}")
            return False

    async def create_repo(self, name: str) -> Optional[str]:
        """Create a repository and return the authenticated clone URL.

        Args:
            name: Repository name (e.g. "job-abc123")

        Returns:
            Authenticated clone URL (http://user:pass@host/user/repo.git)
            or None if creation failed.
        """
        if not self._initialized:
            return None

        client = self._get_client()

        try:
            resp = await client.post(
                f"{self._url}/api/v1/user/repos",
                json={
                    "name": name,
                    "private": True,
                    "auto_init": False,
                    "description": f"Workspace for {name}",
                },
            )

            if resp.status_code == 409:
                # Already exists — return the URL anyway
                logger.debug(f"Gitea repo '{name}' already exists")
            elif resp.status_code not in (200, 201):
                logger.warning(
                    f"Failed to create Gitea repo '{name}' "
                    f"(status {resp.status_code}): {resp.text[:200]}"
                )
                return None

            # Build authenticated clone URL
            return self._build_clone_url(name)

        except httpx.HTTPError as e:
            logger.warning(f"Failed to create Gitea repo '{name}': {e}")
            return None

    async def delete_repo(self, name: str) -> bool:
        """Delete a repository.

        Args:
            name: Repository name

        Returns:
            True if deleted, False on failure.
        """
        if not self._initialized:
            return False

        client = self._get_client()

        try:
            resp = await client.delete(
                f"{self._url}/api/v1/repos/{self._user}/{name}"
            )
            if resp.status_code == 204:
                logger.info(f"Deleted Gitea repo '{name}'")
                return True
            elif resp.status_code == 404:
                logger.debug(f"Gitea repo '{name}' not found (already deleted)")
                return True
            else:
                logger.warning(
                    f"Failed to delete Gitea repo '{name}' (status {resp.status_code})"
                )
                return False
        except httpx.HTTPError as e:
            logger.warning(f"Failed to delete Gitea repo '{name}': {e}")
            return False

    def _build_clone_url(self, repo_name: str) -> str:
        """Build an authenticated clone URL.

        Embeds credentials in the URL for git push from agents.
        Internal network only — acceptable for this use case.
        """
        # Parse URL to inject credentials
        # http://gitea:3000 -> http://user:pass@gitea:3000/user/repo.git
        url = self._url
        if "://" in url:
            scheme, rest = url.split("://", 1)
            return f"{scheme}://{self._user}:{self._password}@{rest}/{self._user}/{repo_name}.git"
        return f"http://{self._user}:{self._password}@{url}/{self._user}/{repo_name}.git"

    @staticmethod
    def mask_credentials(url: str) -> str:
        """Mask credentials in a URL for safe logging.

        Args:
            url: URL potentially containing user:pass@

        Returns:
            URL with password replaced by ***
        """
        return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", url)

    async def close(self) -> None:
        """Close the httpx client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
