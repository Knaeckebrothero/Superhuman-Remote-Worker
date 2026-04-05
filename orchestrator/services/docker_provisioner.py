"""Docker Compose Provisioner — Static workspace pool for non-Kubernetes deployments.

When the orchestrator detects that Kubernetes is unavailable, it uses this
provisioner instead of ContainerProvisioner.  Rather than creating/deleting
pods on demand, it works with pre-existing workspace containers defined in
the Docker Compose file and assigned via environment variable.

Workspace lifecycle:
  1. assign_workspace(job_id)  — pick a free host, mark it in-use in DB
  2. release_workspace(job_id) — snapshot to S3, restart container, free slot
  3. get_workspace_status(job_id) — read assignment from DB

Selection logic:
  - WORKSPACE_HOSTS env var set + non-empty → Docker provisioning enabled
  - Empty / unset → disabled
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class DockerProvisioner:
    """Workspace assignment for Docker Compose mode.

    Unlike ContainerProvisioner (which creates/deletes k8s pods on demand),
    this provisioner works with pre-existing containers defined in the
    compose file.  It assigns available workspaces to jobs and recycles
    them after job completion.

    Assignment state is tracked in PostgreSQL (same ``jobs.context
    .workspace_container`` JSONB field used by ContainerProvisioner) so
    the dispatch logic in ``main.py`` can use either provisioner
    interchangeably.

    Host entries support ``host:port`` format for dev mode where workspace
    SSH ports are published to the host (e.g. ``localhost:2201``).  Plain
    hostnames default to port 22 (Docker-internal networking).
    """

    def __init__(self) -> None:
        self._db: Optional[Any] = None
        self._snapshot_service: Optional[Any] = None
        self._workspace_hosts: list[tuple[str, int]] = []
        self._vm_hosts: list[tuple[str, int]] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True if at least one workspace host is configured."""
        return len(self._workspace_hosts) > 0

    @property
    def workspace_hosts(self) -> list[str]:
        """Human-readable list of workspace host entries."""
        return [self._host_key(h, p) for h, p in self._workspace_hosts]

    @property
    def vm_hosts(self) -> list[str]:
        """Human-readable list of VM host entries."""
        return [self._host_key(h, p) for h, p in self._vm_hosts]

    @staticmethod
    def _host_key(host: str, port: int) -> str:
        """Canonical key for occupancy tracking (``host:port``)."""
        return f"{host}:{port}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # Default workspace ports for dev compose (published SSH ports on localhost)
    _DEV_COMPOSE_DEFAULTS = "localhost:2201,localhost:2202,localhost:2203"
    _DEV_COMPOSE_SSH_KEY_REL = ".dev/ssh-keys/id_ed25519"

    # Repo root: orchestrator/services/docker_provisioner.py → ../../
    _REPO_ROOT = Path(__file__).resolve().parent.parent.parent

    def connect(
        self,
        db: Any,
        snapshot_service: Optional[Any] = None,
    ) -> None:
        """Initialise the provisioner.

        Args:
            db: PostgresDB instance for job context updates.
            snapshot_service: Optional SnapshotService for workspace recycling.
        """
        self._db = db
        self._snapshot_service = snapshot_service

        # Auto-detect dev compose: if WORKSPACE_HOSTS is not set but the
        # dev compose SSH key exists on disk, apply dev defaults so the
        # developer doesn't need to export env vars manually.
        ssh_key_path = self._REPO_ROOT / self._DEV_COMPOSE_SSH_KEY_REL
        if not os.environ.get("WORKSPACE_HOSTS", "").strip():
            if ssh_key_path.exists():
                logger.info(
                    "Docker provisioner: detected dev compose "
                    "(found %s) — applying dev defaults",
                    ssh_key_path,
                )
                os.environ.setdefault("WORKSPACE_HOSTS", self._DEV_COMPOSE_DEFAULTS)
                os.environ.setdefault("SSH_KEY_PATH", str(ssh_key_path))

        self._workspace_hosts = self._parse_hosts("WORKSPACE_HOSTS")
        self._vm_hosts = self._parse_hosts("VM_HOSTS")

        if self._workspace_hosts:
            logger.info(
                "Docker provisioner ready (workspaces=%s, vms=%s)",
                ",".join(self.workspace_hosts),
                ",".join(self.vm_hosts) if self._vm_hosts else "none",
            )
        else:
            logger.info("Docker provisioner: not available (WORKSPACE_HOSTS not set)")

    @staticmethod
    def _parse_hosts(env_var: str) -> list[tuple[str, int]]:
        """Parse comma-separated ``host[:port]`` entries from an env var.

        Examples::

            workspace-1,workspace-2       → [("workspace-1", 22), ("workspace-2", 22)]
            localhost:2201,localhost:2202  → [("localhost", 2201), ("localhost", 2202)]
        """
        raw = os.environ.get(env_var, "")
        result: list[tuple[str, int]] = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" in entry:
                host, port_str = entry.rsplit(":", 1)
                try:
                    result.append((host.strip(), int(port_str.strip())))
                except ValueError:
                    # Not a valid port — treat entire string as hostname
                    result.append((entry, 22))
            else:
                result.append((entry, 22))
        return result

    # ------------------------------------------------------------------
    # Workspace assignment
    # ------------------------------------------------------------------

    async def assign_workspace(self, job_id: str) -> Optional[dict]:
        """Find a free workspace and assign it to this job.

        Checks ``jobs.context.workspace_container`` across all active jobs
        to determine which host:port pairs are currently in use.  Returns
        the first free one.

        Returns:
            ``{"host": "workspace-1", "port": 22, "status": "ready"}``
            or ``None`` if all workspaces are occupied.
        """
        if not self._db or not self._workspace_hosts:
            return None

        in_use = await self._get_occupied_keys()
        for host, port in self._workspace_hosts:
            key = self._host_key(host, port)
            if key not in in_use:
                ctx = {
                    "status": "ready",
                    "host": host,
                    "port": port,
                    "provisioner": "docker",
                }
                await self._db.merge_workspace_container_context(job_id, ctx)
                logger.info(
                    "Docker provisioner: assigned workspace %s to job %s",
                    key,
                    job_id,
                )
                return ctx

        logger.warning(
            "Docker provisioner: all workspaces occupied (%d/%d) — job %s must wait",
            len(in_use),
            len(self._workspace_hosts),
            job_id,
        )
        return None

    async def release_workspace(self, job_id: str) -> bool:
        """Release a workspace back to the pool after job completion.

        Steps:
          1. Read the current workspace assignment from DB.
          2. Snapshot workspace to S3 (if snapshot service available).
          3. Clear the ``workspace_container`` context from the job.

        The actual container restart (to get a clean filesystem) is
        handled separately — either by the orchestrator sending a
        restart signal or by the next job's setup phase.

        Returns:
            True if released successfully, False otherwise.
        """
        if not self._db:
            return False

        ctx = await self._get_job_workspace_context(job_id)
        if not ctx or ctx.get("provisioner") != "docker":
            return False

        host = ctx.get("host", "unknown")

        # Snapshot to S3 before release (best-effort)
        if self._snapshot_service and self._snapshot_service.is_available:
            try:
                await self._snapshot_service.capture_vm_snapshot(
                    job_id=job_id,
                    ssh_host=host,
                    ssh_port=ctx.get("port", 22),
                    source_type="container",
                )
                logger.info(
                    "Docker provisioner: captured snapshot for job %s "
                    "from %s before release",
                    job_id,
                    host,
                )
            except Exception:
                logger.exception(
                    "Docker provisioner: snapshot failed for job %s "
                    "on %s — releasing anyway",
                    job_id,
                    host,
                )

        # Mark workspace as released in DB
        await self._db.merge_workspace_container_context(
            job_id, {"status": "released", "host": host}
        )
        logger.info(
            "Docker provisioner: released workspace %s from job %s",
            host,
            job_id,
        )
        return True

    async def get_workspace_status(self, job_id: str) -> Optional[dict]:
        """Return the workspace assignment for a job, or None."""
        return await self._get_job_workspace_context(job_id)

    # ------------------------------------------------------------------
    # Thread (persistent session) workspace assignment
    # ------------------------------------------------------------------

    async def assign_thread_workspace(self, thread_id: str) -> Optional[dict]:
        """Assign a free workspace to a persistent agent thread.

        Same logic as ``assign_workspace`` but writes to
        ``threads.metadata.workspace_container``.
        """
        if not self._db or not self._workspace_hosts:
            return None

        in_use = await self._get_occupied_keys(include_threads=True)
        for host, port in self._workspace_hosts:
            key = self._host_key(host, port)
            if key not in in_use:
                ctx = {
                    "status": "ready",
                    "host": host,
                    "port": port,
                    "provisioner": "docker",
                }
                await self._db.merge_thread_workspace_context(thread_id, ctx)
                logger.info(
                    "Docker provisioner: assigned workspace %s to thread %s",
                    key,
                    thread_id,
                )
                return ctx

        logger.warning(
            "Docker provisioner: all workspaces occupied — thread %s must wait",
            thread_id,
        )
        return None

    async def release_thread_workspace(self, thread_id: str) -> bool:
        """Release a workspace assigned to a persistent thread."""
        if not self._db:
            return False

        # Read thread metadata for workspace context
        try:
            thread = await self._db.get_thread(thread_id)
            if not thread:
                return False
            metadata = thread.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            ctx = metadata.get("workspace_container") or {}
        except Exception:
            logger.exception(
                "Docker provisioner: failed to read thread %s metadata",
                thread_id,
            )
            return False

        if ctx.get("provisioner") != "docker":
            return False

        host = ctx.get("host", "unknown")

        # Snapshot before release (best-effort)
        if self._snapshot_service and self._snapshot_service.is_available:
            try:
                await self._snapshot_service.capture_vm_snapshot(
                    job_id=thread_id,
                    ssh_host=host,
                    ssh_port=ctx.get("port", 22),
                    source_type="container",
                    entity_type="threads",
                )
            except Exception:
                logger.exception(
                    "Docker provisioner: snapshot failed for thread %s",
                    thread_id,
                )

        await self._db.merge_thread_workspace_context(
            thread_id, {"status": "released", "host": host}
        )
        logger.info(
            "Docker provisioner: released workspace %s from thread %s",
            host,
            thread_id,
        )
        return True

    # ------------------------------------------------------------------
    # VM assignment (QEMU-in-Docker, Phase 4)
    # ------------------------------------------------------------------

    async def assign_vm(self, job_id: str) -> Optional[dict]:
        """Assign a free QEMU-in-Docker VM to a job.

        Returns:
            ``{"ssh_host": "agent-vm-1", "ssh_port": 22, "status": "ready"}``
            or ``None`` if no VMs available.
        """
        if not self._db or not self._vm_hosts:
            return None

        in_use = await self._get_occupied_vm_keys()
        for host, port in self._vm_hosts:
            key = self._host_key(host, port)
            if key not in in_use:
                ctx = {
                    "status": "ready",
                    "ssh_host": host,
                    "ssh_port": port,
                    "provisioner": "docker",
                }
                await self._db.merge_vm_context(job_id, ctx)
                logger.info(
                    "Docker provisioner: assigned VM %s to job %s",
                    key,
                    job_id,
                )
                return ctx

        logger.warning(
            "Docker provisioner: all VMs occupied — job %s must wait",
            job_id,
        )
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_occupied_keys(self, include_threads: bool = False) -> set[str]:
        """Return ``host:port`` keys for workspaces currently assigned to
        active jobs (and optionally threads).
        """
        occupied: set[str] = set()
        if not self._db:
            return occupied

        try:
            rows = await self._db.fetch(
                """
                SELECT context->'workspace_container'->>'host' AS host,
                       COALESCE(
                           (context->'workspace_container'->>'port')::int, 22
                       ) AS port
                FROM jobs
                WHERE status NOT IN ('completed', 'failed', 'cancelled')
                  AND context->'workspace_container'->>'provisioner' = 'docker'
                  AND context->'workspace_container'->>'status' = 'ready'
                """,
            )
            for row in rows:
                if row["host"]:
                    occupied.add(self._host_key(row["host"], row["port"]))

            if include_threads:
                thread_rows = await self._db.fetch(
                    """
                    SELECT metadata->'workspace_container'->>'host' AS host,
                           COALESCE(
                               (metadata->'workspace_container'->>'port')::int, 22
                           ) AS port
                    FROM threads
                    WHERE status = 'active'
                      AND metadata->'workspace_container'->>'provisioner' = 'docker'
                      AND metadata->'workspace_container'->>'status' = 'ready'
                    """,
                )
                for row in thread_rows:
                    if row["host"]:
                        occupied.add(self._host_key(row["host"], row["port"]))
        except Exception:
            logger.exception("Docker provisioner: failed to query occupied workspaces")

        return occupied

    async def _get_occupied_vm_keys(self) -> set[str]:
        """Return ``host:port`` keys for VMs currently assigned to active jobs."""
        occupied: set[str] = set()
        if not self._db:
            return occupied

        try:
            rows = await self._db.fetch(
                """
                SELECT context->'vm'->>'ssh_host' AS host,
                       COALESCE(
                           (context->'vm'->>'ssh_port')::int, 22
                       ) AS port
                FROM jobs
                WHERE status NOT IN ('completed', 'failed', 'cancelled')
                  AND context->'vm'->>'provisioner' = 'docker'
                  AND context->'vm'->>'status' = 'ready'
                """,
            )
            for row in rows:
                if row["host"]:
                    occupied.add(self._host_key(row["host"], row["port"]))
        except Exception:
            logger.exception("Docker provisioner: failed to query occupied VMs")

        return occupied

    async def _get_job_workspace_context(self, job_id: str) -> Optional[dict]:
        """Read workspace_container context from a job row."""
        if not self._db:
            return None
        try:
            job = await self._db.get_job(job_id)
            if not job:
                return None
            ctx = job.get("context") or {}
            if isinstance(ctx, str):
                ctx = json.loads(ctx)
            return ctx.get("workspace_container")
        except Exception:
            logger.exception(
                "Docker provisioner: failed to read job %s context", job_id
            )
            return None


# Module-level singleton
docker_provisioner = DockerProvisioner()
