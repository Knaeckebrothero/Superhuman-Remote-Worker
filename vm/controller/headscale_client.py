"""
Headscale API Client — Pre-auth key generation and node management.

Used by the VM controller to:
  1. Generate single-use ephemeral pre-auth keys for new VMs (tag:vm)
  2. Delete nodes when VMs are torn down

Requires HEADSCALE_URL and HEADSCALE_API_KEY environment variables.
When not configured, all methods return None/False (graceful degradation).

See docs/features/headscale_mesh.md for the full design.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

log = logging.getLogger("vm-controller")

HEADSCALE_URL = os.environ.get("HEADSCALE_URL", "")
HEADSCALE_API_KEY = os.environ.get("HEADSCALE_API_KEY", "")
HEADSCALE_USER = os.environ.get("HEADSCALE_USER", "srw")

# Pre-auth key expiry: short-lived since it's single-use
AUTH_KEY_EXPIRY_MINUTES = int(os.environ.get("HEADSCALE_KEY_EXPIRY_MINUTES", "10"))


class HeadscaleClient:
    """Async HTTP client for the Headscale REST API."""

    def __init__(self):
        self._available = bool(HEADSCALE_URL and HEADSCALE_API_KEY)
        self._user_id: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

        if not self._available:
            log.info(
                "Headscale not configured (HEADSCALE_URL=%s, API_KEY=%s). "
                "Mesh VPN features disabled.",
                "set" if HEADSCALE_URL else "unset",
                "set" if HEADSCALE_API_KEY else "unset",
            )

    @property
    def is_available(self) -> bool:
        return self._available

    async def init(self) -> None:
        """Initialize the HTTP client and resolve the user ID."""
        if not self._available:
            return

        self._client = httpx.AsyncClient(
            base_url=HEADSCALE_URL,
            headers={
                "Authorization": f"Bearer {HEADSCALE_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )

        # Resolve user name -> user ID (API requires numeric ID)
        self._user_id = await self._resolve_user_id(HEADSCALE_USER)
        if self._user_id:
            log.info(
                "Headscale client initialized (url=%s, user=%s, id=%s)",
                HEADSCALE_URL,
                HEADSCALE_USER,
                self._user_id,
            )
        else:
            log.warning(
                "Headscale user '%s' not found. Create it with: "
                "headscale users create %s",
                HEADSCALE_USER,
                HEADSCALE_USER,
            )
            self._available = False

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def create_auth_key(self, job_id: str) -> Optional[str]:
        """Generate a single-use ephemeral pre-auth key tagged with tag:vm.

        Args:
            job_id: Job UUID (for logging).

        Returns:
            The auth key string, or None if unavailable.
        """
        if not self._available or not self._client:
            return None

        expiry = (
            datetime.now(timezone.utc) + timedelta(minutes=AUTH_KEY_EXPIRY_MINUTES)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        payload = {
            "user": self._user_id,
            "reusable": False,
            "ephemeral": True,
            "expiration": expiry,
            "aclTags": ["tag:vm"],
        }

        try:
            resp = await self._client.post("/api/v1/preauthkey", json=payload)
            resp.raise_for_status()
            data = resp.json()
            key = data.get("preAuthKey", {}).get("key")
            if key:
                log.info(
                    "Created Headscale auth key for job %s (expires %s)",
                    job_id,
                    expiry,
                )
                return key
            else:
                log.error("Headscale returned no key for job %s: %s", job_id, data)
                return None
        except Exception as e:
            log.error("Failed to create Headscale auth key for job %s: %s", job_id, e)
            return None

    async def delete_node(self, job_id: str) -> bool:
        """Remove the VM's node from Headscale.

        Looks up the node by hostname (vm-{job_id}) and deletes it.
        Ephemeral nodes auto-expire, but explicit cleanup is faster.

        Returns:
            True if deleted, False if not found or unavailable.
        """
        if not self._available or not self._client:
            return False

        hostname = f"vm-{job_id}"
        try:
            # List nodes and find by hostname
            resp = await self._client.get("/api/v1/node")
            resp.raise_for_status()
            nodes = resp.json().get("nodes", [])

            node_id = None
            for node in nodes:
                if node.get("givenName") == hostname or node.get("name") == hostname:
                    node_id = node.get("id")
                    break

            if not node_id:
                log.debug("No Headscale node found for hostname %s", hostname)
                return False

            # Delete the node
            resp = await self._client.delete(f"/api/v1/node/{node_id}")
            resp.raise_for_status()
            log.info(
                "Deleted Headscale node %s (id=%s) for job %s",
                hostname,
                node_id,
                job_id,
            )
            return True
        except Exception as e:
            log.warning("Failed to delete Headscale node for job %s: %s", job_id, e)
            return False

    async def _resolve_user_id(self, username: str) -> Optional[str]:
        """Look up a Headscale user by name and return its ID."""
        if not self._client:
            return None

        try:
            resp = await self._client.get("/api/v1/user")
            resp.raise_for_status()
            users = resp.json().get("users", [])
            for user in users:
                if user.get("name") == username:
                    return str(user.get("id"))
        except Exception as e:
            log.error("Failed to list Headscale users: %s", e)

        return None
