"""HTTP client for orchestrator communication.

Handles agent registration, heartbeats, and job management with the orchestrator.
"""

import asyncio
import logging
import os
import socket
from typing import Any, Callable, Optional

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class UploadedFileInfo(BaseModel):
    """Metadata for a single uploaded file."""

    name: str
    size: int
    mime_type: str


class UploadInfo(BaseModel):
    """Information about an upload."""

    upload_id: str
    upload_type: str
    files: list[UploadedFileInfo]
    created_at: str


def get_agent_ip() -> str:
    """Auto-detect agent IP address.

    First checks AGENT_POD_IP environment variable, then falls back to
    socket-based detection.

    Returns:
        IP address as string
    """
    # Check environment variable first
    env_ip = os.getenv("AGENT_POD_IP")
    if env_ip:
        return env_ip

    # Fall back to socket-based detection
    try:
        # Create a socket and connect to an external address to determine local IP
        # This doesn't actually send data, just determines the route
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        # Final fallback to localhost
        return "127.0.0.1"


def get_hostname() -> str:
    """Get hostname for agent identification.

    Returns AGENT_HOSTNAME env var if set, otherwise system hostname.
    """
    return os.getenv("AGENT_HOSTNAME") or socket.gethostname()


class OrchestratorClient:
    """HTTP client for communication with the orchestrator service.

    Handles:
    - Agent registration on startup
    - Periodic heartbeats to report status
    - Graceful deregistration on shutdown
    """

    def __init__(
        self,
        orchestrator_url: str,
        pod_ip: str,
        pod_port: int,
        hostname: str,
        config_name: str,
        pid: Optional[int] = None,
    ):
        """Initialize the orchestrator client.

        Args:
            orchestrator_url: Base URL of orchestrator (e.g., http://localhost:8085)
            pod_ip: IP address where this agent can be reached
            pod_port: Port where this agent's API is running
            hostname: Hostname for identification
            config_name: Agent configuration name (e.g., "creator", "validator")
            pid: Optional process ID
        """
        self.orchestrator_url = orchestrator_url.rstrip("/")
        self.pod_ip = pod_ip
        self.pod_port = pod_port
        self.hostname = hostname
        self.config_name = config_name
        self.pid = pid or os.getpid()

        self.agent_id: Optional[str] = None
        self.heartbeat_interval: int = 60  # Default, may be updated by orchestrator

        self._client: Optional[httpx.AsyncClient] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._stop_heartbeat = asyncio.Event()

    async def connect(self) -> None:
        """Initialize the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def register(self) -> bool:
        """Register this agent with the orchestrator.

        Returns:
            True if registration succeeded, False otherwise
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/agents/register"
        payload = {
            "config_name": self.config_name,
            "pod_ip": self.pod_ip,
            "pod_port": self.pod_port,
            "hostname": self.hostname,
            "pid": self.pid,
        }

        try:
            response = await self._client.post(url, json=payload)

            if response.status_code == 200:
                data = response.json()
                self.agent_id = data.get("agent_id")
                self.heartbeat_interval = data.get("heartbeat_interval_seconds", 60)
                logger.info(
                    f"Registered with orchestrator as agent {self.agent_id}, "
                    f"heartbeat interval: {self.heartbeat_interval}s"
                )
                return True
            else:
                logger.error(
                    f"Failed to register with orchestrator: {response.status_code} - {response.text}"
                )
                return False

        except httpx.RequestError as e:
            logger.error(f"Failed to connect to orchestrator for registration: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during registration: {e}")
            return False

    async def deregister(self) -> bool:
        """Deregister this agent from the orchestrator.

        Returns:
            True if deregistration succeeded, False otherwise
        """
        if not self.agent_id:
            logger.warning("Cannot deregister: agent_id not set")
            return False

        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/agents/{self.agent_id}"

        try:
            response = await self._client.delete(url)

            if response.status_code == 200:
                logger.info(f"Deregistered agent {self.agent_id} from orchestrator")
                self.agent_id = None
                return True
            else:
                logger.error(
                    f"Failed to deregister from orchestrator: {response.status_code} - {response.text}"
                )
                return False

        except httpx.RequestError as e:
            logger.error(f"Failed to connect to orchestrator for deregistration: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during deregistration: {e}")
            return False

    async def heartbeat(
        self,
        status: str,
        job_id: Optional[str] = None,
        metrics: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Send a heartbeat to the orchestrator.

        Args:
            status: Agent status (booting, ready, working, completed, failed)
            job_id: Current job ID if working
            metrics: Optional metrics dict (memory_mb, cpu_percent, tokens_processed)

        Returns:
            True if heartbeat succeeded, False otherwise
        """
        if not self.agent_id:
            logger.warning("Cannot send heartbeat: agent_id not set")
            return False

        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/agents/{self.agent_id}/heartbeat"
        payload: dict[str, Any] = {"status": status}

        if job_id:
            payload["current_job_id"] = job_id
        if metrics:
            payload["metrics"] = metrics

        try:
            response = await self._client.post(url, json=payload)

            if response.status_code == 200:
                logger.debug(f"Heartbeat sent: status={status}, job_id={job_id}")
                return True
            elif response.status_code == 404:
                # Agent not found - might have been cleaned up, try to re-register
                logger.warning("Agent not found during heartbeat, attempting re-registration")
                if await self.register():
                    # Retry heartbeat after re-registration
                    return await self.heartbeat(status, job_id, metrics)
                return False
            else:
                logger.error(
                    f"Failed to send heartbeat: {response.status_code} - {response.text}"
                )
                return False

        except httpx.RequestError as e:
            logger.error(f"Failed to connect to orchestrator for heartbeat: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during heartbeat: {e}")
            return False

    async def run_heartbeat_loop(
        self,
        get_status: Callable[[], str],
        get_job_id: Callable[[], Optional[str]],
        get_metrics: Callable[[], Optional[dict[str, Any]]],
    ) -> None:
        """Run the heartbeat loop.

        Sends heartbeats at the configured interval until stopped.
        If not yet registered, attempts registration each interval before
        switching to heartbeat mode. This makes startup order irrelevant
        and recovers from transient registration failures.

        Args:
            get_status: Callback that returns current agent status
            get_job_id: Callback that returns current job ID or None
            get_metrics: Callback that returns metrics dict or None
        """
        logger.info(f"Starting heartbeat loop (interval: {self.heartbeat_interval}s)")
        self._stop_heartbeat.clear()

        while not self._stop_heartbeat.is_set():
            try:
                if not self.agent_id:
                    # Not registered yet — keep trying
                    if await self.register():
                        logger.info("Registration succeeded, switching to heartbeat mode")
                    else:
                        logger.warning("Registration attempt failed, will retry next interval")
                else:
                    status = get_status()
                    job_id = get_job_id()
                    metrics = get_metrics()

                    await self.heartbeat(status, job_id, metrics)

            except Exception as e:
                logger.error(f"Error in heartbeat loop: {e}")

            # Wait for interval or stop signal
            try:
                await asyncio.wait_for(
                    self._stop_heartbeat.wait(),
                    timeout=self.heartbeat_interval,
                )
                break  # Stop signal received
            except asyncio.TimeoutError:
                pass  # Continue loop

        logger.info("Heartbeat loop stopped")

    def stop_heartbeat(self) -> None:
        """Signal the heartbeat loop to stop."""
        self._stop_heartbeat.set()

    async def get_upload_info(self, upload_id: str) -> Optional[UploadInfo]:
        """Get information about an upload from the orchestrator.

        Args:
            upload_id: Upload identifier

        Returns:
            UploadInfo with file list and metadata, or None if not found/error
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/uploads/{upload_id}"

        try:
            response = await self._client.get(url)

            if response.status_code == 200:
                data = response.json()
                return UploadInfo(**data)
            elif response.status_code == 404:
                logger.debug(f"Upload not found on orchestrator: {upload_id}")
                return None
            else:
                logger.warning(
                    f"Failed to get upload info: {response.status_code} - {response.text}"
                )
                return None

        except httpx.RequestError as e:
            logger.warning(f"Failed to connect to orchestrator for upload info: {e}")
            return None
        except Exception as e:
            logger.warning(f"Unexpected error getting upload info: {e}")
            return None

    async def download_file(self, upload_id: str, filename: str) -> Optional[bytes]:
        """Download a file from an upload on the orchestrator.

        Uses streaming to handle large files efficiently.

        Args:
            upload_id: Upload identifier
            filename: Name of the file to download

        Returns:
            File contents as bytes, or None if not found/error
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/uploads/{upload_id}/files/{filename}"

        try:
            # Use streaming for large file support
            async with self._client.stream("GET", url) as response:
                if response.status_code == 200:
                    chunks = []
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    logger.debug(
                        f"Downloaded {filename} from {upload_id} ({len(content)} bytes)"
                    )
                    return content
                elif response.status_code == 404:
                    logger.debug(f"File not found on orchestrator: {upload_id}/{filename}")
                    return None
                else:
                    logger.warning(
                        f"Failed to download file: {response.status_code}"
                    )
                    return None

        except httpx.RequestError as e:
            logger.warning(f"Failed to connect to orchestrator for file download: {e}")
            return None
        except Exception as e:
            logger.warning(f"Unexpected error downloading file: {e}")
            return None


def create_orchestrator_client_from_env(config_name: str) -> OrchestratorClient:
    """Create an OrchestratorClient from environment variables.

    Optional environment variables:
        ORCHESTRATOR_URL: Base URL of orchestrator service (default: http://localhost:8085)
        AGENT_POD_IP: IP address (auto-detected if not set)
        AGENT_POD_PORT: API port (default 8001)
        AGENT_HOSTNAME: Hostname (auto-detected if not set)

    Args:
        config_name: Agent configuration name

    Returns:
        OrchestratorClient configured from environment
    """
    orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8085")

    pod_ip = get_agent_ip()
    pod_port = int(os.getenv("AGENT_POD_PORT", "8001"))
    hostname = get_hostname()

    logger.info(
        f"Creating orchestrator client: url={orchestrator_url}, "
        f"pod_ip={pod_ip}, port={pod_port}, hostname={hostname}"
    )

    return OrchestratorClient(
        orchestrator_url=orchestrator_url,
        pod_ip=pod_ip,
        pod_port=pod_port,
        hostname=hostname,
        config_name=config_name,
    )
