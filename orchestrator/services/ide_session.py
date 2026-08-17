"""IDE Session Service — On-demand code-server sessions from S3 snapshots.

Manages the lifecycle of restored IDE sessions:
  1. Check availability (snapshot or Gitea repo exists)
  2. Start session (provision VM, extract snapshot, boot code-server)
  3. Track session state (restoring → active → idle → expired)
  4. TTL enforcement (idle timeout + max lifetime)
  5. Manual teardown

Session state is stored in ``jobs.context.ide_session`` via atomic JSONB
merges. The cockpit polls ``GET /api/jobs/{job_id}/ide`` to drive the
IDE button UI.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from services import resolve_ssh_key_path
from services.ssh_helpers import (
    EXTRACT_HOME_REMOTE_CMD,
    build_agent_ssh_cmd,
    orchestrator_can_reach,
    stream_extract_snapshot,
)

logger = logging.getLogger(__name__)

_VM_READY_TIMEOUT_SECONDS = 420
_VM_RESTORE_TOPOLOGY_ERROR = "VM restore not supported on this topology"


def _build_code_server_url(
    job_id: str, folder: str = "/home/agent-host/workspace"
) -> str:
    """Build the proxy-routed code-server URL for a job."""
    proxy_base = os.environ.get("IDE_PROXY_BASE_URL", "http://localhost:8085")
    return f"{proxy_base}/api/ide/{job_id}/proxy/?folder={folder}"


class IdeSessionService:
    """Manages on-demand IDE sessions backed by S3 environment snapshots.

    Coordinates between SnapshotService (S3 download), VMProvisioner
    (VM creation), and the management daemon (code-server health).
    """

    def __init__(self) -> None:
        self._db: Any = None
        self._snapshot_service: Any = None
        self._vm_provisioner: Any = None
        self._container_provisioner: Any = None
        self._gitea_client: Any = None

    @property
    def idle_timeout_minutes(self) -> int:
        return int(os.environ.get("IDE_SESSION_IDLE_TIMEOUT", "30"))

    @property
    def max_lifetime_minutes(self) -> int:
        return int(os.environ.get("IDE_SESSION_MAX_LIFETIME", "240"))

    @property
    def max_concurrent_per_user(self) -> int:
        return int(os.environ.get("IDE_MAX_CONCURRENT_PER_USER", "2"))

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def connect(
        self,
        db: Any,
        snapshot_service: Any,
        vm_provisioner: Any,
        gitea_client: Any = None,
        container_provisioner: Any = None,
    ) -> None:
        """Initialize with dependencies.

        Args:
            db: PostgresDB instance.
            snapshot_service: SnapshotService singleton.
            vm_provisioner: VMProvisioner singleton.
            gitea_client: GiteaClient (optional, for Gitea fallback).
            container_provisioner: ContainerProvisioner (optional, for K8s IDE pods).
        """
        self._db = db
        self._snapshot_service = snapshot_service
        self._vm_provisioner = vm_provisioner
        self._container_provisioner = container_provisioner
        self._gitea_client = gitea_client
        logger.info("IDE session service initialized")

    # =========================================================================
    # Public API
    # =========================================================================

    async def get_session_status(self, job_id: str) -> dict[str, Any]:
        """Get the current IDE session status for a job.

        Combines live VM state, active session state, snapshot availability,
        and Gitea repo existence into a single status response.

        Returns:
            Dict with status, code_server_url, expires_at, etc.
        """
        job = await self._get_job(job_id)
        if not job:
            return {"status": "unavailable", "code_server_url": None}

        ctx = self._parse_context(job)
        vm_ctx = ctx.get("vm", {})
        session_ctx = ctx.get("ide_session", {})
        snapshot_ctx = ctx.get("snapshot", {})

        # 1a. Check for live VM (job is still processing)
        if vm_ctx.get("status") == "ready":
            ssh_host = vm_ctx.get("ssh_host") or vm_ctx.get("pod_ip")
            if ssh_host and orchestrator_can_reach(ssh_host):
                return {
                    "status": "active",
                    "code_server_url": _build_code_server_url(job_id),
                    "source": "live_vm",
                }
            if ssh_host:
                # Mesh VM (Tailscale ssh_host) the orchestrator cannot reach
                # directly; live-VM IDE via the agent tunnel is not wired yet
                # (knowledge-base/knowledge/features/vm_snapshots_and_ide.md, "Live-VM IDE Access
                # via the Agent"). Surface unavailable instead of advertising a
                # proxy URL that black-holes into a 504. Becomes "active" once
                # the agent-routed transport lands.
                return {
                    "status": "unavailable",
                    "code_server_url": None,
                    "error": "IDE is not yet available for VM-backed workspaces.",
                }

        # 1b. Check for live workspace container (job processing on container)
        ws_ctx = ctx.get("workspace_container", {})
        if ws_ctx.get("status") == "ready" and ws_ctx.get("pod_ip"):
            return {
                "status": "active",
                "code_server_url": _build_code_server_url(job_id),
                "source": "live_workspace",
            }

        # 2. Check for active IDE session
        session_status = session_ctx.get("status")
        if session_status in ("active", "idle"):
            url = session_ctx.get("code_server_url")
            expires_at = self._compute_expiry(session_ctx)
            return {
                "status": session_status,
                "code_server_url": url,
                "expires_at": expires_at,
                "started_at": session_ctx.get("started_at"),
                "restore_type": session_ctx.get("restore_type", "vm"),
                "source": session_ctx.get("source", "restored_snapshot"),
            }

        if session_status == "restoring":
            return {
                "status": "restoring",
                "code_server_url": None,
                "estimated_seconds": session_ctx.get("estimated_seconds", 30),
                "started_at": session_ctx.get("started_at"),
                "restore_type": session_ctx.get("restore_type", "vm"),
            }

        if session_status == "failed":
            return {
                "status": "failed",
                "code_server_url": None,
                "error": session_ctx.get("error"),
            }

        # Topology gate verdict is terminal: the snapshot may exist, but the
        # restore can never succeed here — surface the error instead of
        # re-offering a doomed retry (which would poll to the 5-min cap).
        if session_status == "unavailable":
            return {
                "status": "unavailable",
                "code_server_url": None,
                "error": session_ctx.get("error"),
            }

        # 3. Check for available snapshot
        if snapshot_ctx.get("status") == "available":
            return {
                "status": "available",
                "code_server_url": None,
                "snapshot_type": snapshot_ctx.get("source_type", "vm"),
            }

        # 4. Check for Gitea repo (fallback)
        if job.get("repo_name"):
            return {
                "status": "available",
                "code_server_url": None,
                "snapshot_type": "gitea",
            }

        # 5. Session was torn down (can restart)
        if session_status == "expired":
            if snapshot_ctx.get("status") == "available" or job.get("repo_name"):
                return {
                    "status": "expired",
                    "code_server_url": None,
                }

        return {"status": "unavailable", "code_server_url": None}

    async def start_session(
        self,
        job_id: str,
        cpu_cores: int = 8,
        memory: str = "16Gi",
        idle_timeout_minutes: Optional[int] = None,
    ) -> dict[str, Any]:
        """Start an IDE session for a job.

        Idempotent: returns existing session if already active/restoring.

        Args:
            job_id: Job UUID.
            cpu_cores: VM CPU cores for restored session.
            memory: VM memory for restored session.
            idle_timeout_minutes: Override default idle timeout.

        Returns:
            Session status dict.
        """
        # Check current state first (idempotent)
        current = await self.get_session_status(job_id)
        if current["status"] in ("active", "idle"):
            return current
        if current["status"] == "restoring":
            return current
        if current["status"] == "unavailable":
            return {
                "status": "unavailable",
                "error": current.get("error") or "No snapshot or repo available",
            }

        job = await self._get_job(job_id)
        if not job:
            return {"status": "unavailable", "error": "Job not found"}

        ctx = self._parse_context(job)
        snapshot_ctx = ctx.get("snapshot", {})
        snapshot_type = snapshot_ctx.get("source_type", "vm")

        # Determine restore method
        if snapshot_ctx.get("status") == "available":
            source = "snapshot"
            estimated_seconds = (
                30 if snapshot_type == "pod" else _VM_READY_TIMEOUT_SECONDS
            )
        elif job.get("repo_name"):
            source = "gitea"
            estimated_seconds = 45
        else:
            return {"status": "unavailable", "error": "No snapshot or repo available"}

        timeout = idle_timeout_minutes or self.idle_timeout_minutes
        now = datetime.now(timezone.utc).isoformat()

        # Mark session as restoring
        await self._set_session_context(
            job_id,
            {
                "status": "restoring",
                "source": source,
                "snapshot_type": snapshot_type if source == "snapshot" else "gitea",
                "started_at": now,
                "code_server_url": None,
                "last_activity": None,
                "idle_timeout_minutes": timeout,
                "max_lifetime_minutes": self.max_lifetime_minutes,
                "estimated_seconds": estimated_seconds,
                "cpu_cores": cpu_cores,
                "memory": memory,
            },
        )

        # Start async restore (VM provisioning + snapshot extraction)
        # This runs in the background — the cockpit polls GET /ide for updates
        import asyncio

        asyncio.create_task(
            self._restore_session(job_id, job, source, cpu_cores, memory)
        )

        return {
            "status": "restoring",
            "snapshot_type": snapshot_type if source == "snapshot" else "gitea",
            "estimated_seconds": estimated_seconds,
        }

    async def stop_session(self, job_id: str) -> dict[str, Any]:
        """Manually tear down an active IDE session.

        Returns:
            Status dict.
        """
        job = await self._get_job(job_id)
        if not job:
            return {"status": "not_found"}

        ctx = self._parse_context(job)
        session_ctx = ctx.get("ide_session", {})
        status = session_ctx.get("status")

        if status not in ("active", "idle", "restoring"):
            return {"status": "no_active_session"}

        restore_type = session_ctx.get("restore_type", "vm")

        if restore_type in ("container", "k8s_container"):
            container_name = session_ctx.get("container_name")
            if container_name:
                await self._delete_ide_container(job_id, container_name, restore_type)
        else:
            vm_name = session_ctx.get("vm_name")
            if vm_name and self._vm_provisioner:
                try:
                    await self._delete_ide_vm(job_id, vm_name)
                except Exception as e:
                    logger.warning("Failed to delete IDE VM %s: %s", vm_name, e)

        await self._set_session_context(
            job_id,
            {
                "status": "expired",
                "code_server_url": None,
                "stopped_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        return {"status": "stopped", "job_id": job_id}

    async def check_ttl_all(self) -> int:
        """Check all active IDE sessions and expire those past TTL.

        Called periodically by the background sweeper task.

        Returns:
            Number of sessions expired.
        """
        if not self._db:
            return 0

        expired_count = 0
        try:
            # Query jobs with active IDE sessions
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, context
                    FROM jobs
                    WHERE context->'ide_session'->>'status' IN ('active', 'idle')
                    """
                )

            now = datetime.now(timezone.utc)
            for row in rows:
                job_id = str(row["id"])
                ctx = (
                    row["context"]
                    if isinstance(row["context"], dict)
                    else json.loads(row["context"] or "{}")
                )
                session = ctx.get("ide_session", {})

                should_expire = False
                reason = ""

                # Check max lifetime
                started_at = session.get("started_at")
                max_lifetime = session.get(
                    "max_lifetime_minutes", self.max_lifetime_minutes
                )
                if started_at:
                    start = datetime.fromisoformat(started_at)
                    if now - start > timedelta(minutes=max_lifetime):
                        should_expire = True
                        reason = "max_lifetime"

                # Check idle timeout (only for "idle" status)
                if not should_expire and session.get("status") == "idle":
                    last_activity = session.get("last_activity")
                    idle_timeout = session.get(
                        "idle_timeout_minutes", self.idle_timeout_minutes
                    )
                    if last_activity:
                        last = datetime.fromisoformat(last_activity)
                        if now - last > timedelta(minutes=idle_timeout):
                            should_expire = True
                            reason = "idle_timeout"
                    elif started_at:
                        # No activity recorded since start — use started_at as baseline
                        start = datetime.fromisoformat(started_at)
                        if now - start > timedelta(minutes=idle_timeout):
                            should_expire = True
                            reason = "idle_timeout_no_activity"

                if should_expire:
                    logger.info(
                        "Expiring IDE session for job %s (reason: %s)", job_id, reason
                    )
                    await self.stop_session(job_id)
                    expired_count += 1

        except Exception:
            logger.exception("Error during IDE session TTL check")

        return expired_count

    # =========================================================================
    # Restore logic (runs as background task)
    # =========================================================================

    async def _restore_session(
        self,
        job_id: str,
        job: dict,
        source: str,
        cpu_cores: int,
        memory: str,
    ) -> None:
        """Background task: provision environment and start code-server.

        Two paths:
        - snapshot/vm: Provision KubeVirt VM → extract S3 snapshot → code-server
        - gitea: Spin up lightweight code-server container → git clone

        Updates context.ide_session as the restore progresses.
        """
        try:
            if source == "gitea":
                # Lightweight container path — no VM needed
                await self._restore_gitea_container(job_id, job)
            elif source == "snapshot":
                snapshot_ctx = self._parse_context(job).get("snapshot", {})
                snapshot_type = snapshot_ctx.get("source_type", "vm")

                if snapshot_type == "vm":
                    # Legacy VM snapshots remain in the VM restore flow
                    await self._restore_vm_session(
                        job_id, job, source, cpu_cores, memory
                    )
                    return

                restored = False
                if (
                    self._container_provisioner
                    and self._container_provisioner.is_available
                ):
                    restored = await self._restore_snapshot_container(job_id, job)

                if not restored:
                    if not job.get("repo_name"):
                        if source == "snapshot":
                            await self._set_session_context(
                                job_id,
                                {
                                    "status": "failed",
                                    "error": "Pod snapshot restore failed and no Gitea repo is available",
                                },
                            )
                        return

                    logger.warning(
                        "Pod snapshot restore failed for job %s; falling back to Gitea clone",
                        job_id,
                    )
                    # Clear the failed verdict before the fallback runs, or the
                    # cockpit's 3s poll sees 'failed' mid-fallback and reports
                    # an error for a session that may still come up.
                    await self._set_session_context(
                        job_id,
                        {"status": "restoring", "error": None},
                    )
                    await self._restore_gitea_container(job_id, job)
            else:
                # Full VM path — provision VM, extract snapshot
                await self._restore_vm_session(job_id, job, source, cpu_cores, memory)

        except Exception as e:
            logger.exception("IDE session restore failed for job %s", job_id)
            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": str(e),
                },
            )

    async def _restore_vm_session(
        self,
        job_id: str,
        job: dict,
        source: str,
        cpu_cores: int,
        memory: str,
    ) -> None:
        """Restore an IDE session via a full KubeVirt VM."""
        if not self._vm_provisioner or not self._vm_provisioner.is_available:
            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": "VM provisioner not available",
                },
            )
            return

        # Provision a fresh VM for the IDE session
        vm_name = f"ide-{job_id[:12]}"
        config_name = job.get("config_name") or "worker_base"

        ok = await self._vm_provisioner.create_vm(
            job_id=job_id,
            agent_config=config_name,
            cpu_cores=cpu_cores,
            memory=memory,
            description=f"IDE session for job {job_id[:8]}",
        )

        if not ok:
            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": "VM provisioning failed",
                },
            )
            return

        await self._set_session_context(
            job_id,
            {
                "vm_name": vm_name,
                "restore_type": "vm",
            },
        )

        # Wait for VM to become ready (poll context.vm.status)
        ssh_host, ssh_port = await self._wait_for_vm_ready(
            job_id, timeout=_VM_READY_TIMEOUT_SECONDS
        )
        if not ssh_host:
            current = await self._get_job(job_id)
            current_ctx = self._parse_context(current).get("vm", {}) if current else {}
            vm_host = current_ctx.get("ssh_host") or current_ctx.get("pod_ip")
            if vm_host and not orchestrator_can_reach(vm_host):
                await self._set_session_context(
                    job_id,
                    {
                        "status": "unavailable",
                        "error": _VM_RESTORE_TOPOLOGY_ERROR,
                    },
                )
                return

            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": "VM did not become ready within timeout",
                },
            )
            return

        # Extract snapshot into the VM. Best-effort by design: a populated
        # tree beats a hard error here, so the bool return is intentionally
        # ignored (mirrors _extract_snapshot_to_k8s_pod's soft policy).
        if source == "snapshot":
            await self._extract_snapshot_to_vm(job_id, ssh_host, ssh_port)
        elif source == "gitea":
            await self._clone_gitea_to_vm(job_id, job, ssh_host, ssh_port)

        await self._seed_ide_profile_for_user(job_id, job, ssh_host, ssh_port)

        # Code-server should already be running from base image
        code_server_url = _build_code_server_url(job_id)

        await self._set_session_context(
            job_id,
            {
                "status": "active",
                "code_server_url": code_server_url,
                "restore_type": "vm",
                "last_activity": datetime.now(timezone.utc).isoformat(),
            },
        )

        logger.info("IDE session active (VM) for job %s: %s", job_id, code_server_url)

    async def _restore_gitea_container(self, job_id: str, job: dict) -> bool:
        """Restore an IDE session via a lightweight code-server container.

        Spins up a code-server container, clones the Gitea repo into it,
        and returns the code-server URL. Much faster than a full VM (~15s
        vs ~45s) but provides only the workspace files, not the full
        agent environment.

        For production (K8s): creates a Pod via ContainerProvisioner.
        For local dev: falls back to podman/docker subprocess.
        """
        repo_name = job.get("repo_name")
        branch = job.get("branch_name") or "main"
        if not repo_name:
            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": "No Gitea repo available",
                },
            )
            return False

        # Route to K8s or local container path
        if self._container_provisioner and self._container_provisioner.is_available:
            return await self._restore_k8s_ide_container(job_id, job, repo_name, branch)
        else:
            return await self._restore_local_ide_container(job_id, repo_name, branch)

    async def _restore_k8s_ide_container(
        self, job_id: str, job: dict, repo_name: str, branch: str
    ) -> bool:
        """Create an IDE pod on Kubernetes, clone the repo, return code-server URL."""
        import asyncio

        clone_url = self._build_gitea_clone_url(repo_name)
        pod_name = f"ide-{job_id[:12]}"

        await self._set_session_context(
            job_id,
            {
                "container_name": pod_name,
                "restore_type": "k8s_container",
            },
        )

        # Create IDE pod via ContainerProvisioner
        pod_ip = await self._container_provisioner.create_ide_pod(job_id)
        if not pod_ip:
            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": "IDE pod did not become ready within timeout",
                },
            )
            return False

        # Clone the Gitea repo into the workspace via SSH. The fallback chain
        # must be idempotent: the workspace may be non-empty with origin
        # already configured (retry after a failed attempt, or a partial
        # snapshot extraction that landed a captured .git), and the captured
        # remote URL may embed stale credentials — hence add-or-set-url.
        clone_cmd = (
            f"git clone --branch {branch} {clone_url} /home/agent-host/workspace "
            f"2>/dev/null || "
            f"(cd /home/agent-host/workspace && git init && "
            f"(git remote add origin {clone_url} 2>/dev/null || "
            f"git remote set-url origin {clone_url}) && "
            f"git fetch origin {branch} && "
            f"git checkout FETCH_HEAD)"
        )

        try:
            async_ssh_cmd = build_agent_ssh_cmd(pod_ip, 30022, clone_cmd)
            proc = await asyncio.create_subprocess_exec(
                *async_ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = (
                    f"Git clone failed for IDE session (rc={proc.returncode}): "
                    f"{stderr.decode(errors='replace')[:300]}"
                )
                logger.error(
                    "Git clone for IDE session failed (job %s): %s",
                    job_id,
                    error_msg,
                )
                await self._set_session_context(
                    job_id,
                    {
                        "status": "failed",
                        "error": error_msg,
                        "pod_ip": pod_ip,
                    },
                )
                return False
        except Exception as e:
            error_msg = f"SSH git clone failed for IDE session: {e}"
            logger.warning("SSH git clone failed for IDE session job %s: %s", job_id, e)
            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": error_msg,
                    "pod_ip": pod_ip,
                },
            )
            return False

        # code-server is already running from the workspace entrypoint
        code_server_url = _build_code_server_url(job_id)

        # Verify code-server is responding
        ready = await self._wait_for_code_server(f"http://{pod_ip}:38080", timeout=15)
        if not ready:
            logger.warning(
                "code-server not responding on IDE pod %s — setting active anyway",
                pod_name,
            )

        await self._set_session_context(
            job_id,
            {
                "status": "active",
                "code_server_url": code_server_url,
                "restore_type": "k8s_container",
                "pod_ip": pod_ip,
                "last_activity": datetime.now(timezone.utc).isoformat(),
            },
        )

        logger.info(
            "IDE session active (K8s container) for job %s: %s",
            job_id,
            code_server_url,
        )
        return True

    async def _restore_local_ide_container(
        self, job_id: str, repo_name: str, branch: str
    ) -> bool:
        """Fallback: run code-server via local podman/docker (dev environments)."""
        import asyncio

        container_name = f"srw-ide-{job_id[:12]}"
        clone_url = self._build_gitea_clone_url(repo_name)
        host_port = await self._find_free_port()

        code_server_image = os.environ.get(
            "CODE_SERVER_IMAGE", "docker.io/codercom/code-server:latest"
        )

        await self._set_session_context(
            job_id,
            {
                "container_name": container_name,
                "restore_type": "container",
            },
        )

        try:
            runtime = await self._detect_container_runtime()

            entrypoint_script = (
                f"git clone --branch {branch} {clone_url} /home/coder/workspace && "
                f"exec code-server --bind-addr 0.0.0.0:{host_port} --auth none /home/coder/workspace"
            )

            cmd = [
                runtime,
                "run",
                "-d",
                "--name",
                container_name,
                "--network",
                "host",
                "-e",
                f"PORT={host_port}",
                "--label",
                f"srw.job_id={job_id}",
                "--label",
                "srw.type=ide-session",
                "--entrypoint",
                "sh",
                code_server_image,
                "-c",
                entrypoint_script,
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode(errors="replace")[:500]
                logger.error(
                    "Failed to start IDE container for job %s: %s",
                    job_id,
                    error_msg,
                )
                await self._set_session_context(
                    job_id,
                    {
                        "status": "failed",
                        "error": f"Container start failed: {error_msg}",
                    },
                )
                return False

            ready = await self._wait_for_code_server(
                f"http://localhost:{host_port}", timeout=30
            )
            if not ready:
                await self._set_session_context(
                    job_id,
                    {
                        "status": "failed",
                        "error": "Code-server did not start within timeout",
                    },
                )
                await self._remove_container(runtime, container_name)
                return False

            code_server_url = (
                f"http://localhost:{host_port}/?folder=/home/coder/workspace"
            )

            await self._set_session_context(
                job_id,
                {
                    "status": "active",
                    "code_server_url": code_server_url,
                    "restore_type": "container",
                    "host_port": host_port,
                    "last_activity": datetime.now(timezone.utc).isoformat(),
                },
            )

            logger.info(
                "IDE session active (container) for job %s: %s",
                job_id,
                code_server_url,
            )
            return True

        except Exception as e:
            logger.exception("Container IDE session failed for job %s", job_id)
            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": str(e),
                },
            )
            try:
                runtime = await self._detect_container_runtime()
                await self._remove_container(runtime, container_name)
            except Exception:
                pass
            return False

    async def _restore_snapshot_container(self, job_id: str, job: dict) -> bool:
        """Restore an IDE session via in-cluster pod from an in-cluster snapshot."""
        pod_name = f"ide-{job_id[:12]}"

        await self._set_session_context(
            job_id,
            {
                "container_name": pod_name,
                "restore_type": "k8s_container",
            },
        )

        if not self._container_provisioner or not getattr(
            self._container_provisioner, "is_available", True
        ):
            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": "IDE pod provisioner not available",
                },
            )
            return False

        pod_ip = await self._container_provisioner.create_ide_pod(job_id)
        if not pod_ip:
            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": "IDE pod did not become ready within timeout",
                },
            )
            return False

        extracted = await self._extract_snapshot_to_k8s_pod(job_id, pod_ip, 30022)
        if not extracted:
            return False

        await self._repair_git_after_snapshot(job_id, job, pod_ip, 30022)
        await self._seed_ide_profile_for_user(job_id, job, pod_ip, 30022)

        # code-server should already be running from the workspace entrypoint
        code_server_url = _build_code_server_url(job_id)

        # Verify code-server is responding before marking active.
        ready = await self._wait_for_code_server(f"http://{pod_ip}:38080", timeout=15)
        if not ready:
            logger.warning(
                "code-server not responding on IDE pod %s — setting active anyway",
                pod_name,
            )

        await self._set_session_context(
            job_id,
            {
                "status": "active",
                "code_server_url": code_server_url,
                "restore_type": "k8s_container",
                "pod_ip": pod_ip,
                "last_activity": datetime.now(timezone.utc).isoformat(),
            },
        )

        logger.info(
            "IDE session active (K8s container snapshot restore) for job %s: %s",
            job_id,
            code_server_url,
        )
        return True

    async def _extract_snapshot_to_k8s_pod(
        self, job_id: str, pod_ip: str, ssh_port: int = 30022
    ) -> bool:
        """Download S3 snapshot and extract into the IDE pod via SSH."""
        import tempfile

        if not self._snapshot_service:
            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": "Snapshot service not available",
                },
            )
            return False

        if not getattr(self._snapshot_service, "is_available", True):
            await self._set_session_context(
                job_id,
                {
                    "status": "failed",
                    "error": "Snapshot service is unavailable",
                },
            )
            return False

        with tempfile.NamedTemporaryFile(
            suffix=".tar.zst", delete=True, prefix=f"restore_{job_id[:8]}_"
        ) as tmp:
            tar_path = tmp.name

            ok = await self._snapshot_service.download_snapshot(job_id, tar_path)
            if not ok:
                error_msg = "Failed to download snapshot"
                logger.warning(
                    "Failed to download snapshot for job %s",
                    job_id,
                )
                await self._set_session_context(
                    job_id,
                    {
                        "status": "failed",
                        "error": error_msg,
                    },
                )
                return False

            key_path = resolve_ssh_key_path()
            if not key_path:
                logger.warning(
                    "No SSH key available for snapshot extraction for job %s",
                    job_id,
                )
            rc, stderr = await stream_extract_snapshot(
                pod_ip,
                ssh_port,
                tar_path,
                key_path=key_path,
                remote_cmd=EXTRACT_HOME_REMOTE_CMD,
            )
            if rc != 0:
                # tar exits 2 on any per-file error even when the payload
                # extracted fine (e.g. an unwritable stray path). Probe the
                # workspace before declaring failure — a populated workspace
                # beats a hard error for a browse tool.
                if await self._workspace_populated(pod_ip, ssh_port):
                    logger.warning(
                        "Snapshot extraction for job %s exited rc=%d but the "
                        "workspace is populated — continuing: %s",
                        job_id,
                        rc,
                        stderr.decode(errors="replace")[:300],
                    )
                    return True
                error_msg = (
                    f"Snapshot extraction failed for job {job_id} (rc={rc}): "
                    f"{stderr.decode(errors='replace')[:500]}"
                )
                logger.warning(error_msg)
                await self._set_session_context(
                    job_id,
                    {
                        "status": "failed",
                        "error": error_msg,
                    },
                )
                return False

        return True

    async def _workspace_populated(self, ssh_host: str, ssh_port: int) -> bool:
        """True when the pod's workspace directory exists and is non-empty."""
        import asyncio

        probe_cmd = 'test -n "$(ls -A /home/agent-host/workspace 2>/dev/null)"'
        try:
            proc = await asyncio.create_subprocess_exec(
                *build_agent_ssh_cmd(ssh_host, ssh_port, probe_cmd),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return (await proc.wait()) == 0
        except Exception:
            return False

    async def _repair_git_after_snapshot(
        self, job_id: str, job: dict, ssh_host: str, ssh_port: int
    ) -> None:
        """Best-effort: refetch git objects after a snapshot restore.

        Snapshot capture excludes ``.git/objects`` (snapshot_service's
        exclude list — "re-cloned/regenerated on restore"), so the restored
        workspace repo has config/refs/index but no objects. A fetch
        repopulates them; the embedded-credential remote URL from capture
        time may also be stale, so it is refreshed first. Failure only
        degrades the Source Control panel, never the session.
        """
        import asyncio

        repo_name = job.get("repo_name")
        if not repo_name:
            return

        clone_url = self._build_gitea_clone_url(repo_name)
        branch = job.get("branch_name") or "main"
        # --refetch skips have/want negotiation, which can die on "bad
        # object" here: local refs point at objects the capture excluded.
        # Older git (<2.36) lacks the flag — fall back to a plain fetch.
        cmd = (
            "cd /home/agent-host/workspace && "
            f"(git remote add origin {clone_url} 2>/dev/null || "
            f"git remote set-url origin {clone_url}) && "
            f"(git fetch --refetch origin {branch} 2>/dev/null || "
            f"git fetch origin {branch})"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *build_agent_ssh_cmd(ssh_host, ssh_port, cmd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                logger.info(
                    "Git repair fetch failed for job %s (rc=%d): %s",
                    job_id,
                    proc.returncode,
                    err.decode(errors="replace")[:200],
                )
        except asyncio.TimeoutError:
            logger.info("Git repair fetch timed out for job %s", job_id)
            try:
                proc.kill()
            except Exception:
                pass
        except Exception as e:
            logger.info("Git repair fetch failed for job %s: %s", job_id, e)

    async def _seed_ide_profile_for_user(
        self, job_id: str, job: dict, ssh_host: str, ssh_port: int
    ) -> None:
        """Seed IDE config and profile for restored IDE pods."""
        try:
            from services.ide_settings import seed_ide_config_for_user

            await seed_ide_config_for_user(
                self._db, job.get("user_id"), ssh_host, ssh_port
            )

            # Restore license/globalStorage + non-Open-VSX bytes into the IDE
            # workspace. Best-effort; no-ops when optional dependencies
            # are unavailable.
            user_id = job.get("user_id")
            if (
                user_id
                and self._snapshot_service
                and getattr(self._snapshot_service, "is_available", True)
            ):
                from services.ide_profile_store import IdeProfileStore
                from services.ide_settings import IdeSettingsStore, seed_ide_profile

                items = await IdeSettingsStore(self._db).get_extensions(str(user_id))
                profile = IdeProfileStore(
                    self._snapshot_service._s3, self._snapshot_service._bucket
                )
                await seed_ide_profile(
                    user_id=str(user_id),
                    ssh_host=ssh_host,
                    ssh_port=ssh_port,
                    profile_store=profile,
                    ext_items=items,
                )
        except Exception:
            logger.warning(
                "Failed to seed code-server config for IDE session %s",
                job_id,
                exc_info=True,
            )

    async def _wait_for_vm_ready(
        self, job_id: str, timeout: int = _VM_READY_TIMEOUT_SECONDS
    ) -> tuple[Optional[str], Optional[int]]:
        """Poll job context until VM is ready, return (ssh_host, ssh_port)."""
        import asyncio

        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        while datetime.now(timezone.utc) < deadline:
            job = await self._get_job(job_id)
            if not job:
                return None, None

            ctx = self._parse_context(job)
            vm_ctx = ctx.get("vm", {})
            ssh_host = vm_ctx.get("ssh_host") or vm_ctx.get("pod_ip")
            if ssh_host and not orchestrator_can_reach(ssh_host):
                return None, None

            if vm_ctx.get("status") == "ready":
                ssh_port = vm_ctx.get("ssh_port") or 22
                if ssh_host:
                    return ssh_host, int(ssh_port)

            if vm_ctx.get("status") == "failed":
                return None, None

            await asyncio.sleep(3)

        return None, None

    async def _extract_snapshot_to_vm(
        self, job_id: str, ssh_host: str, ssh_port: int
    ) -> bool:
        """Download snapshot from S3 and extract into the VM via SSH.

        Mirrors workspace_suspension.py:_extract_snapshot (lines 506-562).

        Returns:
            True when the snapshot was downloaded AND unpacked cleanly; False
            when there is no snapshot service, the download failed, or tar
            exited non-zero. Callers MUST NOT report the workspace restored
            on False — this used to return None on every path, so a failed
            restore still reported success and the agent resumed on an empty
            or half-populated tree.
        """
        import tempfile

        if not self._snapshot_service:
            return False

        with tempfile.NamedTemporaryFile(
            suffix=".tar.zst", delete=True, prefix=f"restore_{job_id[:8]}_"
        ) as tmp:
            tar_path = tmp.name

            # Download from S3
            ok = await self._snapshot_service.download_snapshot(job_id, tar_path)
            if not ok:
                logger.warning("Failed to download snapshot for job %s", job_id)
                return False

            # Extract into VM via SSH
            key_path = resolve_ssh_key_path()
            if not key_path:
                logger.warning(
                    "No SSH key available for snapshot extraction (job %s)",
                    job_id,
                )
            rc, stderr = await stream_extract_snapshot(
                ssh_host, ssh_port, tar_path, key_path=key_path
            )

            if rc != 0:
                logger.warning(
                    "Snapshot extraction had errors for job %s (rc=%d): %s",
                    job_id,
                    rc,
                    stderr.decode(errors="replace")[:500],
                )
                return False

            return True

    async def _clone_gitea_to_vm(
        self, job_id: str, job: dict, ssh_host: str, ssh_port: int
    ) -> None:
        """Clone the job's Gitea repo into the VM as a fallback."""
        import asyncio

        repo_name = job.get("repo_name")
        if not repo_name:
            return

        clone_url = self._build_gitea_clone_url(repo_name)
        branch = job.get("branch_name") or "main"

        cmd = (
            f"git clone --branch {branch} {clone_url} /home/agent-host/workspace "
            f"2>/dev/null || true"
        )

        key_path = resolve_ssh_key_path()
        ssh_cmd = [
            "ssh",
            *(["-i", key_path] if key_path else []),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(ssh_port),
            f"agent-host@{ssh_host}",
            cmd,
        ]

        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    async def _delete_ide_vm(self, job_id: str, vm_name: str) -> None:
        """Delete an IDE session VM."""
        if self._vm_provisioner and self._vm_provisioner.is_available:
            await self._vm_provisioner.delete_vm(job_id)

    async def _delete_ide_container(
        self, job_id: str, container_name: str, restore_type: str = "container"
    ) -> None:
        """Stop and remove an IDE session container (K8s pod or local container)."""
        if restore_type == "k8s_container":
            if self._container_provisioner:
                await self._container_provisioner.delete_ide_pod(job_id)
            return

        # Local dev path (podman/docker)
        try:
            runtime = await self._detect_container_runtime()
            await self._remove_container(runtime, container_name)
        except Exception as e:
            logger.warning("Failed to remove IDE container %s: %s", container_name, e)

    # =========================================================================
    # Snapshot restore for job resume
    # =========================================================================

    async def restore_snapshot_for_resume(
        self, job_id: str, ssh_host: str, ssh_port: int
    ) -> bool:
        """Restore S3 snapshot into a VM as part of job resume.

        Called by the resume endpoint after a new VM is provisioned.
        Extracts the environment snapshot so the agent picks up where
        it left off.

        Returns:
            True if snapshot was restored, False if no snapshot or failed.
        """
        if not self._snapshot_service or not self._snapshot_service.is_available:
            return False

        job = await self._get_job(job_id)
        if not job:
            return False

        ctx = self._parse_context(job)
        snapshot_ctx = ctx.get("snapshot", {})

        if snapshot_ctx.get("status") != "available":
            return False

        try:
            ok = await self._extract_snapshot_to_vm(job_id, ssh_host, ssh_port)
            if not ok:
                logger.error(
                    "Snapshot restore FAILED for resume of job %s (extract failed)",
                    job_id,
                )
                return False
            logger.info("Snapshot restored for job resume: %s", job_id)
            return True
        except Exception as e:
            logger.warning(
                "Snapshot restore failed for resume of job %s: %s", job_id, e
            )
            return False

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _get_job(self, job_id: str) -> Optional[dict]:
        """Fetch job from DB."""
        if not self._db:
            return None
        return await self._db.get_job(job_id)

    @staticmethod
    def _parse_context(job: dict) -> dict:
        """Parse job context, handling both dict and JSON string."""
        ctx = job.get("context") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except json.JSONDecodeError:
                ctx = {}
        return ctx

    async def _set_session_context(self, job_id: str, updates: dict) -> None:
        """Atomically merge updates into context.ide_session."""
        if not self._db:
            return
        try:
            await self._db.merge_ide_session_context(job_id, updates)
        except Exception:
            logger.exception("Failed to update IDE session context for job %s", job_id)

    @staticmethod
    def _compute_expiry(session_ctx: dict) -> Optional[str]:
        """Compute when the session will expire."""
        started_at = session_ctx.get("started_at")
        max_lifetime = session_ctx.get("max_lifetime_minutes", 240)
        if started_at:
            start = datetime.fromisoformat(started_at)
            expiry = start + timedelta(minutes=max_lifetime)
            return expiry.isoformat()
        return None

    # =========================================================================
    # Container helpers (Gitea fallback)
    # =========================================================================

    @staticmethod
    def _build_gitea_clone_url(repo_name: str) -> str:
        """Build a Gitea clone URL with embedded credentials.

        Uses GITEA_INTERNAL_URL (for container-to-host reachability),
        falling back to GITEA_URL. Injects GITEA_ADMIN_USER/PASSWORD
        for authentication.
        """
        from urllib.parse import urlparse, urlunparse

        gitea_url = os.environ.get("GITEA_URL", "http://localhost:3000")
        gitea_internal_url = os.environ.get("GITEA_INTERNAL_URL", gitea_url)
        user = os.environ.get("GITEA_ADMIN_USER", "srw")
        password = os.environ.get("GITEA_ADMIN_PASSWORD", "")

        parsed = urlparse(gitea_internal_url)
        if password:
            netloc = f"{user}:{password}@{parsed.hostname}"
        else:
            netloc = parsed.hostname
        if parsed.port:
            netloc += f":{parsed.port}"

        base = urlunparse(parsed._replace(netloc=netloc))
        return f"{base}/srw/{repo_name}.git"

    @staticmethod
    async def _detect_container_runtime() -> str:
        """Detect available container runtime (podman or docker)."""
        import shutil

        for runtime in ("podman", "docker"):
            if shutil.which(runtime):
                return runtime

        raise RuntimeError("No container runtime found (podman or docker)")

    @staticmethod
    async def _find_free_port() -> int:
        """Find a free TCP port on localhost."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    @staticmethod
    async def _wait_for_code_server(url: str, timeout: int = 30) -> bool:
        """Poll code-server URL until it responds."""
        import asyncio
        import httpx

        deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        async with httpx.AsyncClient(timeout=5.0) as client:
            while datetime.now(timezone.utc) < deadline:
                try:
                    resp = await client.get(url)
                    if resp.status_code < 500:
                        return True
                except (
                    httpx.ConnectError,
                    httpx.ReadTimeout,
                    httpx.RemoteProtocolError,
                ):
                    pass
                await asyncio.sleep(1)
        return False

    @staticmethod
    async def _remove_container(runtime: str, container_name: str) -> None:
        """Stop and remove a container by name."""
        import asyncio

        # Stop (ignore errors if already stopped)
        proc = await asyncio.create_subprocess_exec(
            runtime,
            "stop",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

        # Remove
        proc = await asyncio.create_subprocess_exec(
            runtime,
            "rm",
            "-f",
            container_name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()


# Module-level singleton
ide_session_service = IdeSessionService()
