"""HTTP client for orchestrator communication.

Handles agent registration, heartbeats, and job management with the orchestrator.
"""

import asyncio
import logging
import os
import socket
from typing import Any, Awaitable, Callable, Optional

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
        """Initialize the HTTP client.

        Attaches ``X-Internal-Key`` to every request when ``MCP_INTERNAL_KEY``
        is set in the agent's env. The orchestrator's Track B (P4b) gates
        check this header on agent-internal endpoints (register, heartbeat,
        job-complete, etc.) and on the dual-callable job mutation paths
        (cancel/pause/resume/approve/subjob-merge/messages-send). Without
        the key the agent's calls would be rejected as anonymous external
        traffic.
        """
        if self._client is None:
            headers: dict[str, str] = {}
            internal_key = os.getenv("MCP_INTERNAL_KEY", "")
            if internal_key:
                headers["X-Internal-Key"] = internal_key
            self._client = httpx.AsyncClient(timeout=30.0, headers=headers)

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def register(
        self,
        agent_mode: str = "worker",
        thread_id: str | None = None,
    ) -> bool:
        """Register this agent with the orchestrator.

        Args:
            agent_mode: "worker" (default, dispatch pool) or "persistent" (interactive session)
            thread_id: Thread UUID for persistent mode

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
            "agent_mode": agent_mode,
            "thread_id": thread_id,
            "build_sha": os.environ.get("BUILD_SHA", ""),
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

    async def create_thread(
        self,
        config_name: str = "persistent_defaults",
        permission_mode: str = "supervised",
        title: str = "Local Session",
    ) -> str | None:
        """Create a thread in the orchestrator DB (agent-facing, no auth).

        Returns:
            Thread UUID string, or None on failure.
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/agents/threads"
        payload = {
            "config_name": config_name,
            "permission_mode": permission_mode,
            "title": title,
        }

        try:
            response = await self._client.post(url, json=payload)
            if response.status_code == 200:
                data = response.json()
                thread_id = data.get("thread_id")
                logger.info(f"Created thread via orchestrator: {thread_id}")
                return thread_id
            else:
                logger.error(
                    f"Failed to create thread: {response.status_code} - {response.text}"
                )
                return None
        except Exception as e:
            logger.error(f"Failed to create thread: {e}")
            return None

    async def save_thread_message(
        self,
        thread_id: str,
        role: str,
        content: str | None = None,
        tool_calls: list | None = None,
        turn_number: int | None = None,
        metrics: dict | None = None,
        tool_call_id: str | None = None,
        thinking: str | None = None,
    ) -> bool:
        """Save a message to thread history via orchestrator REST. Fire-and-forget safe.

        ``tool_call_id`` is set only for role='tool' rows; ``thinking`` for
        role='ai' rows that carry reasoning content.
        """
        if not self._client:
            return False

        url = f"{self.orchestrator_url}/api/agents/threads/{thread_id}/messages"
        payload = {
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
            "turn_number": turn_number,
            "metrics": metrics,
            "tool_call_id": tool_call_id,
            "thinking": thinking,
        }

        try:
            response = await self._client.post(url, json=payload)
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Failed to save thread message (non-fatal): {e}")
            return False

    async def request_thread_vm_upgrade(
        self, thread_id: str, cpu_cores: int = 2, memory: str = "4Gi"
    ) -> bool:
        """Request VM provisioning for a persistent thread (upgrade from container).

        Returns:
            True if accepted, False on failure.
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/agents/threads/{thread_id}/upgrade-to-vm"
        payload = {"cpu_cores": cpu_cores, "memory": memory}

        try:
            response = await self._client.post(url, json=payload)
            if response.status_code == 200:
                logger.info(f"VM upgrade requested for thread {thread_id}")
                return True
            else:
                logger.error(
                    f"VM upgrade request failed: {response.status_code} - {response.text}"
                )
                return False
        except Exception as e:
            logger.error(f"VM upgrade request error: {e}")
            return False

    async def get_thread_workspace(self, thread_id: str) -> dict | None:
        """Poll workspace container status for a thread.

        Returns:
            Workspace status dict {status, pod_ip, pod_name, namespace},
            or None on failure.
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/agents/threads/{thread_id}/workspace"
        try:
            response = await self._client.get(url)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            logger.debug(f"Failed to get thread workspace: {e}")
            return None

    async def get_thread_lifecycle(self, thread_id: str) -> dict | None:
        """Fetch minimal lifecycle fields for the agent's status watchdog.

        Returns:
            ``{status, agent_id, ended_at}`` on success; ``None`` on any
            failure (caller treats that as "skip this poll cycle").
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/agents/threads/{thread_id}/lifecycle"
        try:
            response = await self._client.get(url)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            logger.debug(f"Failed to get thread lifecycle: {e}")
            return None

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

    async def update_thread_status(self, thread_id: str, status: str) -> bool:
        """Update thread status via orchestrator REST.

        Args:
            thread_id: Thread UUID
            status: New status ('active' or 'ended'). When 'ended', the
                orchestrator routes through end_thread() so ended_at is stamped.

        Returns:
            True if update succeeded, False otherwise
        """
        if not self._client:
            return False
        url = f"{self.orchestrator_url}/api/agents/threads/{thread_id}/status"
        try:
            r = await self._client.put(url, json={"status": status})
            return r.status_code == 200
        except Exception as e:
            logger.warning(f"Thread status update failed (non-fatal): {e}")
            return False

    async def release_thread_agent(self, thread_id: str) -> bool:
        """Clear threads.agent_id when this agent's session attach fails.

        Lets the orchestrator dispatch the next WS reconnect to a healthy
        agent instead of re-targeting this (now session-less) one.
        """
        if not self._client:
            return False
        url = f"{self.orchestrator_url}/api/agents/threads/{thread_id}/release-agent"
        try:
            r = await self._client.post(url)
            return r.status_code == 200
        except Exception as e:
            logger.warning(f"Thread→agent release failed (non-fatal): {e}")
            return False

    async def update_thread_config(
        self, thread_id: str, config_override: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Persist runtime config changes for a thread.

        Args:
            thread_id: Thread UUID
            config_override: Partial config dict to deep-merge
                             (e.g. ``{"llm": {"model": "..."}}``)

        Returns:
            The orchestrator-enriched ``config_override`` (with resolved
            ``base_url``/``api_key`` for endpoint-backed models) on success,
            or ``None`` on failure. Callers must use the returned dict —
            not the input — when rebuilding the LLM, otherwise custom
            endpoint requests fall through to api.openai.com.
        """
        if not self._client:
            return None
        url = f"{self.orchestrator_url}/api/agents/threads/{thread_id}/config"
        try:
            r = await self._client.patch(url, json={"config_override": config_override})
            if r.status_code != 200:
                return None
            data = r.json()
            enriched = data.get("config_override")
            return enriched if isinstance(enriched, dict) else config_override
        except Exception as e:
            logger.warning(f"Thread config update failed (non-fatal): {e}")
            return None

    async def heartbeat(
        self,
        status: str,
        job_id: Optional[str] = None,
        metrics: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Send a heartbeat to the orchestrator.

        Args:
            status: Agent status (booting, ready, working, completed, failed)
            job_id: Current job ID if working
            metrics: Optional metrics dict (memory_mb, cpu_percent, tokens_processed)

        Returns:
            The orchestrator's JSON response body on success (truthy);
            ``None`` on failure. The response carries any pending
            ``intents`` (drain, version-upgrade hints) the agent should
            react to. Callers that only care about success can still
            check the return value as truthy/falsy.
        """
        if not self.agent_id:
            logger.warning("Cannot send heartbeat: agent_id not set")
            return None

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
                try:
                    return response.json() or {}
                except Exception:
                    return {}
            elif response.status_code == 404:
                # Agent not found - might have been cleaned up, try to re-register
                logger.warning(
                    "Agent not found during heartbeat, attempting re-registration"
                )
                if await self.register():
                    # Retry heartbeat after re-registration
                    return await self.heartbeat(status, job_id, metrics)
                return None
            else:
                logger.error(
                    f"Failed to send heartbeat: {response.status_code} - {response.text}"
                )
                return None

        except httpx.RequestError as e:
            logger.error(f"Failed to connect to orchestrator for heartbeat: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during heartbeat: {e}")
            return None

    async def run_heartbeat_loop(
        self,
        get_status: Callable[[], str],
        get_job_id: Callable[[], Optional[str]],
        get_metrics: Callable[[], Optional[dict[str, Any]]],
        on_response: Optional[
            Callable[[dict[str, Any]], Optional[Awaitable[None]]]
        ] = None,
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
            on_response: Optional callback invoked with each successful
                heartbeat response body (carries ``intents``). Used by
                callers that need to react to drain / version-upgrade
                hints. Sync or async.
        """
        logger.info(f"Starting heartbeat loop (interval: {self.heartbeat_interval}s)")
        self._stop_heartbeat.clear()

        while not self._stop_heartbeat.is_set():
            try:
                if not self.agent_id:
                    # Not registered yet — keep trying
                    if await self.register():
                        logger.info(
                            "Registration succeeded, switching to heartbeat mode"
                        )
                    else:
                        logger.warning(
                            "Registration attempt failed, will retry next interval"
                        )
                else:
                    status = get_status()
                    job_id = get_job_id()
                    metrics = get_metrics()

                    response = await self.heartbeat(status, job_id, metrics)
                    if response is not None and on_response is not None:
                        try:
                            ret = on_response(response)
                            if asyncio.iscoroutine(ret):
                                await ret
                        except Exception:
                            logger.exception("on_response callback raised")

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
                    logger.debug(
                        f"File not found on orchestrator: {upload_id}/{filename}"
                    )
                    return None
                else:
                    logger.warning(f"Failed to download file: {response.status_code}")
                    return None

        except httpx.RequestError as e:
            logger.warning(f"Failed to connect to orchestrator for file download: {e}")
            return None
        except Exception as e:
            logger.warning(f"Unexpected error downloading file: {e}")
            return None

    async def resume_job(
        self,
        job_id: str,
        feedback: Optional[str] = None,
    ) -> bool:
        """Resume a job via the orchestrator API.

        Used to resume the target job with critic feedback, or to resume
        a waiting critic job for another review round.

        If no agent is available, the orchestrator queues the job for
        auto-dispatch (returns 200 with status "queued").

        Args:
            job_id: UUID of the job to resume
            feedback: Optional feedback to inject before resuming

        Returns:
            True if resume succeeded or queued, False otherwise
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/jobs/{job_id}/resume"
        payload: dict[str, Any] = {}
        if feedback:
            payload["feedback"] = feedback

        try:
            response = await self._client.post(url, json=payload)

            if response.status_code in (200, 202):
                data = (
                    response.json()
                    if response.headers.get("content-type", "").startswith(
                        "application/json"
                    )
                    else {}
                )
                status = data.get("status", "resumed")
                if status == "queued":
                    logger.info(
                        f"Job {job_id} queued for auto-dispatch (no agents available)"
                    )
                else:
                    logger.info(f"Resumed job {job_id} via orchestrator")
                return True
            else:
                logger.error(
                    f"Failed to resume job {job_id}: {response.status_code} - {response.text}"
                )
                return False

        except httpx.RequestError as e:
            logger.error(f"Failed to connect to orchestrator for resume: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error resuming job: {e}")
            return False

    async def trigger_subjob_merge(self, job_id: str) -> bool:
        """Trigger squash merge of a completed subjob via the orchestrator.

        Args:
            job_id: UUID of the completed subjob

        Returns:
            True if merge succeeded or was skipped, False on error
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/jobs/{job_id}/subjob-merge"

        try:
            response = await self._client.post(url)

            if response.status_code == 200:
                data = response.json()
                logger.info(
                    f"Subjob merge for {job_id}: {data.get('status', 'unknown')}"
                )
                return True
            else:
                logger.error(
                    f"Subjob merge failed for {job_id}: "
                    f"{response.status_code} - {response.text}"
                )
                return False

        except httpx.RequestError as e:
            logger.error(f"Failed to connect to orchestrator for subjob merge: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error triggering subjob merge: {e}")
            return False

    async def report_pause(self, job_id: str) -> bool:
        """Report that the agent is releasing a job (e.g. during graceful shutdown).

        Tells the orchestrator to set the job to 'paused' and clear its agent
        assignment so the dispatcher can reassign it.  Uses the agent-initiated
        pause endpoint which skips the agent-callback loop (since we *are* the
        agent).

        Args:
            job_id: UUID of the job to pause

        Returns:
            True if the orchestrator accepted the pause, False otherwise.
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/jobs/{job_id}/agent-release"

        try:
            response = await self._client.put(url, timeout=10.0)
            if response.status_code == 200:
                logger.info(f"Orchestrator accepted agent-release for job {job_id}")
                return True
            else:
                logger.warning(
                    f"Agent-release failed for job {job_id}: "
                    f"{response.status_code} - {response.text}"
                )
                return False
        except Exception as e:
            logger.warning(f"Failed to report agent-release for job {job_id}: {e}")
            return False

    async def report_completion(
        self,
        job_id: str,
        result: dict[str, Any],
    ) -> bool:
        """Report job completion to the orchestrator.

        The orchestrator is the single authority for DB status. It handles
        all post-completion logic: status determination, freeze_data persistence,
        critic verdict handling, verification job spawning, curation, and dispatch.

        Args:
            job_id: UUID of the completed job
            result: Final graph state (should_stop, goal_achieved, error, freeze_data)

        Returns:
            True if the orchestrator handled completion successfully.
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/jobs/{job_id}/complete"
        payload = {
            "should_stop": result.get("should_stop", False),
            "goal_achieved": result.get("goal_achieved", False),
            "error": result.get("error"),
            "freeze_data": result.get("freeze_data"),
        }

        try:
            response = await self._client.post(url, json=payload, timeout=60.0)
            if response.status_code == 200:
                resp_data = response.json()
                actions = resp_data.get("actions", [])
                logger.info(
                    f"Orchestrator handled completion for job {job_id}: "
                    f"status={resp_data.get('new_status')}, actions={actions}"
                )
                return True
            elif response.status_code == 404:
                logger.info(
                    "Orchestrator does not support /complete endpoint — "
                    "falling back to local handling"
                )
                return False
            else:
                logger.warning(
                    f"Completion report failed for job {job_id}: "
                    f"{response.status_code} - {response.text}"
                )
                return False

        except httpx.RequestError as e:
            logger.warning(f"Failed to report completion for job {job_id}: {e}")
            return False
        except Exception as e:
            logger.warning(
                f"Unexpected error reporting completion for job {job_id}: {e}"
            )
            return False

    async def create_delegation_job(
        self,
        description: str,
        config_name: str,
        parent_job_id: str,
        creation_order: int,
        delegation_context: str = "",
        config_override: dict[str, Any] | None = None,
        project_id: str | None = None,
        priority: int = 5,
    ) -> dict[str, Any] | None:
        """Create a delegation child job via the orchestrator.

        Args:
            description: Task description for the child job
            config_name: Agent config to use (e.g., "scholar", "developer")
            parent_job_id: UUID of the parent job
            creation_order: 0-based index in the sibling group
            delegation_context: Shared context string (with port range info)
            config_override: Config overrides (autonomy, delegation settings)
            project_id: Project UUID (inherited from parent)
            priority: Job priority (inherited from parent)

        Returns:
            Created job dict with id, or None on failure.
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/jobs"
        payload: dict[str, Any] = {
            "description": description,
            "config_name": config_name,
            "parent_job_id": parent_job_id,
            "creation_order": creation_order,
            "delegation_context": delegation_context,
            "priority": priority,
        }
        if config_override:
            payload["config_override"] = config_override
        if project_id:
            payload["project_id"] = project_id

        try:
            response = await self._client.post(url, json=payload, timeout=30.0)
            if response.status_code in (200, 201):
                data = response.json()
                logger.info(
                    f"Created delegation child job {data.get('id')} "
                    f"(order={creation_order}) for parent {parent_job_id}"
                )
                return data
            else:
                logger.warning(
                    f"Failed to create delegation job for parent {parent_job_id}: "
                    f"{response.status_code} - {response.text}"
                )
                return None
        except Exception as e:
            logger.warning(
                f"Error creating delegation job for parent {parent_job_id}: {e}"
            )
            return None

    async def approve_job(
        self,
        job_id: str,
        notes: str | None = None,
    ) -> bool:
        """Approve a frozen job via the orchestrator.

        Args:
            job_id: UUID of the job to approve
            notes: Optional reviewer notes

        Returns:
            True if the orchestrator approved successfully.
        """
        if not self._client:
            await self.connect()

        url = f"{self.orchestrator_url}/api/jobs/{job_id}/approve"
        payload: dict[str, Any] = {}
        if notes:
            payload["notes"] = notes

        try:
            response = await self._client.put(url, json=payload, timeout=30.0)
            if response.status_code == 200:
                logger.info(f"Job {job_id} approved via orchestrator")
                return True
            else:
                logger.warning(
                    f"Approval failed for job {job_id}: "
                    f"{response.status_code} - {response.text}"
                )
                return False
        except Exception as e:
            logger.warning(f"Failed to approve job {job_id}: {e}")
            return False

    async def create_verification_job(
        self,
        job_id: str,
        description: str,
        freeze_data: dict[str, Any],
        config_name: str,
        project_id: Optional[str] = None,
        max_rounds: int = 3,
        parent_llm_override: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Create a critic verification job for a completed job.

        Loads the verification instructions template, formats it with
        target job details, and creates a new critic job via the
        orchestrator API.

        Args:
            job_id: UUID of the target job being verified
            description: Original job description
            freeze_data: Freeze data from the target job (has summary, deliverables, confidence)
            config_name: Critic config name to use (e.g., "critic")
            project_id: Optional project UUID (inherit from parent job)
            max_rounds: Maximum feedback round-trips before auto-accepting

        Returns:
            Created job dict from orchestrator, or None on failure
        """
        if not self._client:
            await self.connect()

        # Load and format the verification instructions template
        instructions = self._format_verification_instructions(
            job_id,
            description,
            freeze_data,
            config_name,
        )
        if not instructions:
            logger.error(
                f"Failed to load verification instructions template for job {job_id}"
            )
            return None

        # Build the job creation payload
        verification_description = (
            f"Verify deliverables of job {job_id} ({config_name}). "
            f"Review output against original requirements and either approve or return with feedback."
        )

        context = {
            "verification_target": job_id,
            "original_description": description,
            "original_config": config_name,
            "deliverables": freeze_data.get("deliverables", []),
            "summary": freeze_data.get("summary", ""),
            "confidence": freeze_data.get("confidence", 0),
            "verification_round": 0,
            "max_verification_rounds": max_rounds,
        }

        payload: dict[str, Any] = {
            "description": verification_description,
            "config_name": freeze_data.get("critic_config", "critic"),
            "config_override": {
                "autonomy": "full",  # Critic must run autonomously
                "tools": {
                    "evaluation": ["approve_job", "return_job_with_feedback"],
                },
                **({"llm": parent_llm_override} if parent_llm_override else {}),
            },
            "instructions": instructions,
            "context": context,
            "parent_job_id": job_id,
            "priority": 10,  # High priority — verification preempts lower-priority work
        }
        if project_id:
            payload["project_id"] = project_id

        url = f"{self.orchestrator_url}/api/jobs"

        try:
            response = await self._client.post(url, json=payload)

            if response.status_code == 200:
                result = response.json()
                critic_job_id = result.get("id", "unknown")
                logger.info(
                    f"Created verification job {critic_job_id} for target job {job_id}"
                )
                return result
            else:
                logger.error(
                    f"Failed to create verification job: {response.status_code} - {response.text}"
                )
                return None

        except httpx.RequestError as e:
            logger.error(f"Failed to connect to orchestrator for verification job: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error creating verification job: {e}")
            return None

    @staticmethod
    def _format_verification_instructions(
        job_id: str,
        description: str,
        freeze_data: dict[str, Any],
        config_name: str,
    ) -> Optional[str]:
        """Load and format the verification instructions template.

        Args:
            job_id: Target job UUID
            description: Original job description
            freeze_data: Freeze data with summary, deliverables, confidence
            config_name: Original job's config name

        Returns:
            Formatted instructions string, or None if template not found
        """
        from pathlib import Path

        # Look for template in critic expert directory, then fall back to config/templates
        template_path = None
        search_paths = [
            Path(__file__).parent.parent.parent
            / "config"
            / "experts"
            / "critic"
            / "verification_instructions.md",
            Path(__file__).parent.parent.parent
            / "config"
            / "templates"
            / "verification_instructions.md",
        ]

        for path in search_paths:
            if path.exists():
                template_path = path
                break

        if not template_path:
            logger.error(
                f"Verification instructions template not found. "
                f"Searched: {[str(p) for p in search_paths]}"
            )
            return None

        try:
            template = template_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to read verification template {template_path}: {e}")
            return None

        # Format deliverables as a bulleted list
        deliverables = freeze_data.get("deliverables", [])
        if deliverables:
            deliverables_list = "\n".join(f"- `{d}`" for d in deliverables)
        else:
            deliverables_list = "- *(no deliverables listed)*"

        confidence = freeze_data.get("confidence", 0)
        confidence_str = (
            f"{confidence:.0%}"
            if isinstance(confidence, (int, float))
            else str(confidence)
        )

        try:
            return template.format(
                target_job_id=job_id,
                target_config=config_name,
                target_description=description,
                deliverables_list=deliverables_list,
                agent_summary=freeze_data.get("summary", "*(no summary provided)*"),
                agent_confidence=confidence_str,
            )
        except KeyError as e:
            logger.error(f"Verification template has unknown placeholder: {e}")
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
