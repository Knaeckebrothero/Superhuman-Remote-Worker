"""
Headscale API Client — Pre-auth key generation and node management.

Used by the VM controller to:
  1. Generate single-use ephemeral pre-auth keys for new VMs (tag:vm)
  2. Delete nodes when VMs are torn down

Requires HEADSCALE_URL and HEADSCALE_API_KEY environment variables.
When not configured, all methods return None/False (graceful degradation).

See knowledge-base/knowledge/features/headscale_mesh.md for the full design.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

log = logging.getLogger("vm-controller")

HEADSCALE_URL = os.environ.get("HEADSCALE_URL", "")
HEADSCALE_API_KEY = os.environ.get("HEADSCALE_API_KEY", "")
HEADSCALE_USER = os.environ.get("HEADSCALE_USER", "srw")

# Pre-auth key expiry: short-lived since it's single-use
AUTH_KEY_EXPIRY_MINUTES = int(os.environ.get("HEADSCALE_KEY_EXPIRY_MINUTES", "10"))

# On-demand user re-resolution backoff. Creates are infrequent, but the
# dispatcher polls a 'waiting_headscale' job every tick — throttle so an
# outage can't turn that poll into a hot loop against Headscale.
RETRY_BASE_S = float(os.environ.get("HEADSCALE_RETRY_BASE_S", "5"))
RETRY_MAX_S = float(os.environ.get("HEADSCALE_RETRY_MAX_S", "60"))


class HeadscaleClient:
    """Async HTTP client for the Headscale REST API."""

    def __init__(self):
        self._configured = bool(HEADSCALE_URL and HEADSCALE_API_KEY)
        self._user_id: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._last_error: Optional[str] = None
        self._retry_delay = RETRY_BASE_S
        self._next_retry_at = 0.0  # time.monotonic() deadline

        if not self._configured:
            log.info(
                "Headscale not configured (HEADSCALE_URL=%s, API_KEY=%s). "
                "Mesh VPN features disabled.",
                "set" if HEADSCALE_URL else "unset",
                "set" if HEADSCALE_API_KEY else "unset",
            )

    @property
    def is_available(self) -> bool:
        """Whether Headscale is in play for this deployment.

        Reflects *configuration only*, and deliberately stays True while
        Headscale is unreachable — see ``is_ready`` for a health signal. A
        transient outage must never permanently disable mesh networking:
        that is exactly the latch that made every VM boot keyless and
        unreachable (knowledge-base/knowledge/issues/
        vm_controller_headscale_latch_kills_provisioning.md).
        """
        return self._configured

    @property
    def is_ready(self) -> bool:
        """Whether a pre-auth key can be minted right now (user resolved)."""
        return self._configured and self._user_id is not None

    @property
    def last_error(self) -> Optional[str]:
        """Why the last Headscale interaction failed, for caller telemetry."""
        return self._last_error

    async def init(self) -> None:
        """Initialize the HTTP client and resolve the user ID.

        A failure here is NOT fatal and NOT sticky: the user ID is re-resolved
        on demand by ``create_auth_key``. The controller routinely starts
        before Headscale after a host reboot, where pod start order is a coin
        flip.
        """
        if not self._configured:
            return

        self._client = httpx.AsyncClient(
            base_url=HEADSCALE_URL,
            headers={
                "Authorization": f"Bearer {HEADSCALE_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )

        await self._ensure_user_id()

    async def _ensure_user_id(self) -> Optional[str]:
        """Return the cached user ID, resolving it on demand with backoff.

        Returns None while Headscale is unreachable or the user is missing,
        leaving the client configured so the next call retries.
        """
        if self._user_id:
            return self._user_id
        if not self._configured or self._client is None:
            return None

        now = time.monotonic()
        if now < self._next_retry_at:
            return None

        user_id = await self._resolve_user_id(HEADSCALE_USER)
        if user_id:
            self._user_id = user_id
            self._last_error = None
            self._retry_delay = RETRY_BASE_S
            self._next_retry_at = 0.0
            log.info(
                "Headscale client initialized (url=%s, user=%s, id=%s)",
                HEADSCALE_URL,
                HEADSCALE_USER,
                user_id,
            )
            return user_id

        if self._last_error is None:
            self._last_error = (
                f"Headscale user '{HEADSCALE_USER}' not found "
                f"(create it with: headscale users create {HEADSCALE_USER})"
            )
        self._next_retry_at = now + self._retry_delay
        log.warning(
            "Headscale unavailable (%s) — retrying in %.0fs; "
            "VM creates are deferred until it resolves",
            self._last_error,
            self._retry_delay,
        )
        self._retry_delay = min(self._retry_delay * 2, RETRY_MAX_S)
        return None

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
        if not self._configured or self._client is None:
            return None

        # Re-resolve on demand: the controller may have started before
        # Headscale was serving, and that must not doom every later VM.
        user_id = await self._ensure_user_id()
        if not user_id:
            return None

        expiry = (
            datetime.now(timezone.utc) + timedelta(minutes=AUTH_KEY_EXPIRY_MINUTES)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        payload = {
            "user": user_id,
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
            self._last_error = f"{type(e).__name__}: {e}"
            log.error(
                "Failed to create Headscale auth key for job %s: %s",
                job_id,
                self._last_error,
            )
            return None

    async def delete_node(self, job_id: str) -> bool:
        """Remove the VM's node from Headscale.

        Looks up the node by hostname (vm-{job_id}) and deletes it.
        Ephemeral nodes auto-expire, but explicit cleanup is faster.

        Returns:
            True if deleted, False if not found or unavailable.
        """
        if not self._configured or self._client is None:
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
            self._last_error = None
            resp = await self._client.get("/api/v1/user")
            resp.raise_for_status()
            users = resp.json().get("users", [])
            for user in users:
                if user.get("name") == username:
                    return str(user.get("id"))
        except Exception as e:
            # Include the exception type: httpx connect/timeout errors often
            # str() to '', which rendered the original outage's log line as a
            # bare "Failed to list Headscale users:" with no cause.
            self._last_error = f"{type(e).__name__}: {e}"
            log.error("Failed to list Headscale users: %s", self._last_error)

        return None
