"""Workspace Container Idle Suspension — S3 snapshot + pod lifecycle.

Detects idle workspace containers (pods for paused/frozen jobs), captures
their environment to S3 via SnapshotService, deletes the pod, and restores
on demand when the job needs to run again.

Requires both ContainerProvisioner (K8s) and SnapshotService (S3) to be
available. Gracefully degrades: when S3 is unavailable, containers stay alive.
"""

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


class WorkspaceSuspensionService:
    """Coordinates idle suspension between SnapshotService and ContainerProvisioner.

    Status transitions for workspace_container.status:
        ready → suspending → suspended → restoring → ready
    """

    def __init__(self):
        self._db: Optional[Any] = None
        self._snapshot_service: Optional[Any] = None
        self._container_provisioner: Optional[Any] = None
        self._persistent_provisioner: Optional[Any] = None

    def connect(
        self,
        db: Any,
        snapshot_service: Any,
        container_provisioner: Any,
        persistent_provisioner: Any = None,
    ) -> None:
        self._db = db
        self._snapshot_service = snapshot_service
        self._container_provisioner = container_provisioner
        self._persistent_provisioner = persistent_provisioner

        if self.is_enabled:
            logger.info(
                "Workspace suspension enabled (idle_timeout=%dm)",
                self.idle_timeout_minutes,
            )
        else:
            logger.info("Workspace suspension disabled (S3 or K8s unavailable)")

    @property
    def is_enabled(self) -> bool:
        """True only if container provisioner AND S3 snapshots are both available."""
        return (
            self._container_provisioner is not None
            and self._container_provisioner.is_available
            and self._snapshot_service is not None
            and self._snapshot_service.is_available
        )

    @property
    def idle_timeout_minutes(self) -> int:
        return int(os.environ.get("WORKSPACE_IDLE_TIMEOUT", "30"))

    # =========================================================================
    # Suspend: snapshot → delete pod
    # =========================================================================

    async def suspend_workspace(self, job_id: str) -> bool:
        """Capture snapshot to S3, then delete the workspace pod.

        Returns True if snapshot + deletion succeeded.
        On failure, reverts status to 'ready' and keeps the pod alive.
        """
        if not self.is_enabled or not self._db:
            return False

        # Get current container context for pod IP
        job = await self._db.get_job(job_id)
        if not job:
            return False

        ctx = job.get("context") or {}
        ws_ctx = ctx.get("workspace_container", {})
        pod_ip = ws_ctx.get("pod_ip")

        if not pod_ip or ws_ctx.get("status") != "ready":
            return False

        # Mark as suspending (prevents re-entry from sweeper)
        await self._db.merge_workspace_container_context(
            job_id, {"status": "suspending"}
        )

        try:
            # Capture environment to S3
            ok = await self._snapshot_service.capture_vm_snapshot(
                job_id=job_id,
                ssh_host=pod_ip,
                ssh_port=22,
                source_type="pod",
            )
            if not ok:
                logger.warning(
                    "Snapshot capture failed for job %s — keeping pod alive", job_id
                )
                await self._db.merge_workspace_container_context(
                    job_id, {"status": "ready"}
                )
                return False

            # Delete the pod
            await self._container_provisioner.delete_workspace(job_id)

            # Mark as suspended
            await self._db.merge_workspace_container_context(
                job_id,
                {
                    "status": "suspended",
                    "suspended_at": datetime.now(timezone.utc).isoformat(),
                    "pod_ip": None,
                    "pod_name": None,
                },
            )
            logger.info("Workspace suspended to S3 for job %s", job_id)
            return True

        except Exception:
            logger.exception("Failed to suspend workspace for job %s", job_id)
            # Try to revert to ready so the sweeper can retry
            try:
                await self._db.merge_workspace_container_context(
                    job_id, {"status": "ready"}
                )
            except Exception:
                pass
            return False

    # =========================================================================
    # Restore: create pod → extract snapshot
    # =========================================================================

    async def restore_workspace(self, job_id: str) -> bool:
        """Create a new pod and extract the S3 snapshot into it.

        Returns True if pod creation + snapshot extraction succeeded.
        """
        if not self.is_enabled or not self._db:
            return False

        await self._db.merge_workspace_container_context(
            job_id, {"status": "restoring"}
        )

        try:
            # Create a fresh pod (waits for readiness + IP)
            ok = await self._container_provisioner.create_workspace(job_id)
            if not ok:
                logger.error("Failed to create pod for restore of job %s", job_id)
                await self._db.merge_workspace_container_context(
                    job_id,
                    {"status": "failed", "error": "pod creation failed on restore"},
                )
                return False

            # Re-read context to get the new pod IP
            job = await self._db.get_job(job_id)
            ws_ctx = (job.get("context") or {}).get("workspace_container", {})
            pod_ip = ws_ctx.get("pod_ip")

            if not pod_ip:
                logger.error("No pod IP after restore creation for job %s", job_id)
                await self._db.merge_workspace_container_context(
                    job_id, {"status": "failed", "error": "no pod IP after creation"}
                )
                return False

            # Extract snapshot into the new pod
            await self._extract_snapshot(job_id, pod_ip)

            # Mark as ready
            await self._db.merge_workspace_container_context(
                job_id,
                {
                    "status": "ready",
                    "restored_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info(
                "Workspace restored from S3 for job %s (pod_ip=%s)", job_id, pod_ip
            )
            return True

        except Exception:
            logger.exception("Failed to restore workspace for job %s", job_id)
            await self._db.merge_workspace_container_context(
                job_id, {"status": "failed", "error": "restore exception"}
            )
            return False

    async def _extract_snapshot(
        self, entity_id: str, ssh_host: str, entity_type: str = "jobs"
    ) -> None:
        """Download snapshot from S3 and extract into the pod via SSH.

        Mirrors ide_session.py:_extract_snapshot_to_vm (lines 782-836).
        """
        with tempfile.NamedTemporaryFile(
            suffix=".tar.zst", delete=True, prefix=f"restore_{entity_id[:8]}_"
        ) as tmp:
            tar_path = tmp.name

            ok = await self._snapshot_service.download_snapshot(
                entity_id, tar_path, entity_type=entity_type
            )
            if not ok:
                logger.warning(
                    "Failed to download snapshot for %s %s",
                    entity_type.rstrip("s"),
                    entity_id,
                )
                return

            ssh_cmd = [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                "ConnectTimeout=10",
                "-p",
                "22",
                f"agent-host@{ssh_host}",
                "zstd -d | tar -xf - -C /",
            ]

            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            with open(tar_path, "rb") as f:
                tar_data = f.read()

            stdout, stderr = await proc.communicate(input=tar_data)

            if proc.returncode != 0:
                logger.warning(
                    "Snapshot extraction had errors for %s %s (rc=%d): %s",
                    entity_type.rstrip("s"),
                    entity_id,
                    proc.returncode,
                    stderr.decode(errors="replace")[:500],
                )

    # =========================================================================
    # Thread suspension (mirrors job suspension for persistent agent threads)
    # =========================================================================

    async def suspend_thread_workspace(self, thread_id: str) -> bool:
        """Capture thread workspace snapshot to S3, then delete the pod.

        Returns True if snapshot + deletion succeeded.
        On failure, reverts status to 'ready' and keeps the pod alive.
        """
        if not self.is_enabled or not self._db:
            return False

        thread = await self._db.get_thread(thread_id)
        if not thread:
            return False

        metadata = thread.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (ValueError, TypeError):
                metadata = {}
        ws_ctx = metadata.get("workspace_container", {})
        pod_ip = ws_ctx.get("pod_ip")

        if not pod_ip or ws_ctx.get("status") != "ready":
            return False

        await self._db.merge_thread_workspace_context(
            thread_id, {"status": "suspending"}
        )

        try:
            ok = await self._snapshot_service.capture_vm_snapshot(
                job_id=thread_id,
                ssh_host=pod_ip,
                ssh_port=22,
                source_type="pod",
                entity_type="threads",
            )
            if not ok:
                logger.warning(
                    "Snapshot capture failed for thread %s — keeping pod alive",
                    thread_id,
                )
                await self._db.merge_thread_workspace_context(
                    thread_id, {"status": "ready"}
                )
                return False

            await self._container_provisioner.delete_thread_workspace(thread_id)

            # Also delete the persistent agent pod (it's stateless, state is in the workspace)
            if (
                self._persistent_provisioner
                and self._persistent_provisioner.is_available
            ):
                await self._persistent_provisioner.delete_agent_pod(thread_id)

            await self._db.merge_thread_workspace_context(
                thread_id,
                {
                    "status": "suspended",
                    "suspended_at": datetime.now(timezone.utc).isoformat(),
                    "pod_ip": None,
                    "pod_name": None,
                },
            )
            logger.info("Workspace suspended to S3 for thread %s", thread_id)
            return True

        except Exception:
            logger.exception("Failed to suspend workspace for thread %s", thread_id)
            try:
                await self._db.merge_thread_workspace_context(
                    thread_id, {"status": "ready"}
                )
            except Exception:
                pass
            return False

    async def restore_thread_workspace(self, thread_id: str) -> bool:
        """Create a new pod and extract the S3 snapshot into it.

        Returns True if pod creation + snapshot extraction succeeded.
        """
        if not self.is_enabled or not self._db:
            return False

        await self._db.merge_thread_workspace_context(
            thread_id, {"status": "restoring"}
        )

        try:
            ok = await self._container_provisioner.create_thread_workspace(thread_id)
            if not ok:
                logger.error("Failed to create pod for restore of thread %s", thread_id)
                await self._db.merge_thread_workspace_context(
                    thread_id,
                    {"status": "failed", "error": "pod creation failed on restore"},
                )
                return False

            # Re-read to get new pod IP
            thread = await self._db.get_thread(thread_id)
            metadata = thread.get("metadata") or {}
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (ValueError, TypeError):
                    metadata = {}
            ws_ctx = metadata.get("workspace_container", {})
            pod_ip = ws_ctx.get("pod_ip")

            if not pod_ip:
                logger.error(
                    "No pod IP after restore creation for thread %s", thread_id
                )
                await self._db.merge_thread_workspace_context(
                    thread_id, {"status": "failed", "error": "no pod IP after creation"}
                )
                return False

            # Extract snapshot into the new pod
            await self._extract_snapshot(thread_id, pod_ip, entity_type="threads")

            await self._db.merge_thread_workspace_context(
                thread_id,
                {
                    "status": "ready",
                    "restored_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            logger.info(
                "Workspace restored from S3 for thread %s (pod_ip=%s)",
                thread_id,
                pod_ip,
            )
            return True

        except Exception:
            logger.exception("Failed to restore workspace for thread %s", thread_id)
            await self._db.merge_thread_workspace_context(
                thread_id, {"status": "failed", "error": "restore exception"}
            )
            return False

    async def check_idle_threads(self) -> int:
        """Sweep idle thread workspace containers and suspend them.

        A thread container is idle if:
        - Thread status is 'idle' (no active WebSocket)
        - Workspace container status is 'ready'
        - last_activity is older than idle_timeout_minutes

        Returns the count of containers suspended.
        """
        if not self.is_enabled or not self._db:
            return 0

        suspended_count = 0
        timeout = self.idle_timeout_minutes
        now = datetime.now(timezone.utc)

        try:
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, metadata, last_activity
                    FROM threads
                    WHERE metadata->'workspace_container'->>'status' = 'ready'
                      AND status = 'idle'
                    """,
                )
        except Exception:
            logger.exception("Failed to query idle thread workspace containers")
            return 0

        for row in rows:
            thread_id = str(row["id"])
            last_activity = row.get("last_activity", now)

            if last_activity and last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)

            idle_minutes = (now - last_activity).total_seconds() / 60

            if idle_minutes >= timeout:
                logger.info(
                    "Suspending idle workspace for thread %s (idle %.0fm, timeout %dm)",
                    thread_id,
                    idle_minutes,
                    timeout,
                )
                ok = await self.suspend_thread_workspace(thread_id)
                if ok:
                    suspended_count += 1

        return suspended_count

    # =========================================================================
    # Idle sweep (jobs)
    # =========================================================================

    async def check_idle_all(self) -> int:
        """Sweep all ready workspace containers and suspend idle ones.

        A container is idle if:
        - The job is in paused/pending_review/waiting_for_reply status
        - last_activity (or updated_at) is older than idle_timeout_minutes

        Returns the count of containers suspended.
        """
        if not self.is_enabled or not self._db:
            return 0

        suspended_count = 0
        timeout = self.idle_timeout_minutes
        now = datetime.now(timezone.utc)

        try:
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, context, updated_at
                    FROM jobs
                    WHERE context->'workspace_container'->>'status' = 'ready'
                      AND status IN ('paused', 'pending_review', 'waiting_for_reply')
                    """,
                )
        except Exception:
            logger.exception("Failed to query idle workspace containers")
            return 0

        for row in rows:
            job_id = str(row["id"])
            ctx = row.get("context") or {}
            if isinstance(ctx, str):
                try:
                    ctx = json.loads(ctx)
                except (ValueError, TypeError):
                    ctx = {}
            ws_ctx = ctx.get("workspace_container", {})

            # Determine last activity time
            last_activity_str = ws_ctx.get("last_activity")
            if last_activity_str:
                try:
                    last_activity = datetime.fromisoformat(last_activity_str)
                except (ValueError, TypeError):
                    last_activity = row.get("updated_at", now)
            else:
                last_activity = row.get("updated_at", now)

            # Ensure timezone-aware
            if last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)

            idle_minutes = (now - last_activity).total_seconds() / 60

            if idle_minutes >= timeout:
                logger.info(
                    "Suspending idle workspace for job %s (idle %.0fm, timeout %dm)",
                    job_id,
                    idle_minutes,
                    timeout,
                )
                ok = await self.suspend_workspace(job_id)
                if ok:
                    suspended_count += 1

        return suspended_count


# Module-level singleton
workspace_suspension_service = WorkspaceSuspensionService()
