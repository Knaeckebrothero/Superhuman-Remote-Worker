"""Workspace Container Idle Suspension — S3 snapshot + pod lifecycle.

Detects idle workspace containers (pods for paused/frozen jobs), captures
their environment to S3 via SnapshotService, deletes the pod, and restores
on demand when the job needs to run again.

Requires both ContainerProvisioner (K8s) and SnapshotService (S3) to be
available. Gracefully degrades: when S3 is unavailable, containers stay alive.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any, Optional

from services import resolve_ssh_key_path
from services.ssh_helpers import stream_extract_snapshot
from services.workspace_lifecycle import WorkspaceOwner

logger = logging.getLogger(__name__)


def _resolve_ssh_port(ws_ctx: dict, vm_ctx: dict) -> int:
    """Resolve the snapshot SSH port by workspace kind.

    Container/pod workspaces run sshd on 30022; only true VM contexts use the
    VM ssh_port (default 22). Previously both fell through to a VM-shaped 22
    default, which silently broke pod snapshots when ``port`` was absent from
    the stored context (the cause of the dev-cluster leaked-pod incident).
    """
    if ws_ctx:
        return int(ws_ctx.get("port", 30022))
    return int(vm_ctx.get("ssh_port", 22))


class WorkspaceSuspensionService:
    """Coordinates idle suspension between SnapshotService and ContainerProvisioner.

    Status transitions for workspace_container.status:
        ready → suspending → suspended → restoring → ready
    """

    def __init__(self):
        self._db: Optional[Any] = None
        self._snapshot_service: Optional[Any] = None
        self._container_provisioner: Optional[Any] = None
        self._docker_provisioner: Optional[Any] = None
        self._vm_provisioner: Optional[Any] = None
        self._agent_provisioner: Optional[Any] = None

    def connect(
        self,
        db: Any,
        snapshot_service: Any,
        container_provisioner: Any,
        docker_provisioner: Any = None,
        vm_provisioner: Any = None,
        agent_provisioner: Any = None,
    ) -> None:
        self._db = db
        self._snapshot_service = snapshot_service
        self._container_provisioner = container_provisioner
        self._docker_provisioner = docker_provisioner
        self._vm_provisioner = vm_provisioner
        self._agent_provisioner = agent_provisioner

        if self.is_enabled:
            backends = []
            if self._container_provisioner and self._container_provisioner.is_available:
                backends.append("k8s")
            if self._docker_provisioner and self._docker_provisioner.is_available:
                backends.append("docker")
            if self._vm_provisioner and self._vm_provisioner.is_available:
                backends.append("vm")
            logger.info(
                "Workspace suspension enabled (idle_timeout=%dm, backends=%s)",
                self.idle_timeout_minutes,
                ",".join(backends),
            )
        else:
            logger.info("Workspace suspension disabled (S3 or provisioner unavailable)")

    @property
    def is_enabled(self) -> bool:
        """True if S3 snapshots and at least one provisioner are available."""
        if not self._snapshot_service or not self._snapshot_service.is_available:
            return False
        return (
            (
                self._container_provisioner is not None
                and self._container_provisioner.is_available
            )
            or (
                self._docker_provisioner is not None
                and self._docker_provisioner.is_available
            )
            or (self._vm_provisioner is not None and self._vm_provisioner.is_available)
        )

    @property
    def idle_timeout_minutes(self) -> int:
        return int(os.environ.get("WORKSPACE_IDLE_TIMEOUT", "30"))

    # =========================================================================
    # Suspend: snapshot → delete pod
    # =========================================================================

    async def suspend_workspace(self, job_id: str) -> bool:
        """Capture snapshot to S3, then tear down the workspace.

        Dispatches to the correct provisioner based on workspace metadata:
        - K8s container: snapshot → delete pod
        - Docker Compose: snapshot → SSH reset (container stays alive)
        - VM: snapshot → delete VM

        Returns True if snapshot + teardown succeeded.
        On failure, reverts status to 'ready' and keeps the workspace alive.
        """
        if not self.is_enabled or not self._db:
            return False

        job = await self._db.get_job(job_id)
        if not job:
            return False

        ctx = job.get("context") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except (json.JSONDecodeError, TypeError):
                ctx = {}

        ws_ctx = ctx.get("workspace_container", {})
        vm_ctx = ctx.get("vm", {})
        provisioner_type = ws_ctx.get("provisioner")

        # Determine SSH host for snapshot
        ssh_host = ws_ctx.get("pod_ip") or ws_ctx.get("host") or vm_ctx.get("ssh_host")
        ssh_port = _resolve_ssh_port(ws_ctx, vm_ctx)

        if not ssh_host:
            return False

        ws_status = ws_ctx.get("status") if ws_ctx else vm_ctx.get("status")
        if ws_status != "ready":
            return False

        # Opportunistic final code-server config pull while the workspace is
        # still alive — shrinks the worst-case IDE-settings loss window below the
        # sweeper interval on a clean suspend/teardown. Best-effort.
        try:
            user_id = job.get("user_id")
            if user_id:
                from services.ide_settings import IdeSettingsStore, pull_ide_config

                store = IdeSettingsStore(self._db)
                pulled = await pull_ide_config(ssh_host, int(ssh_port))
                if pulled:
                    await store.apply_pulled_files(str(user_id), pulled)
                # Capture license/globalStorage + bytes extensions to S3 (Phase B),
                # signature-gated. Shrinks the state loss window on clean suspend.
                if self._snapshot_service and self._snapshot_service.is_available:
                    from services.ide_profile_store import IdeProfileStore
                    from services.ide_settings import capture_ide_profile

                    profile = IdeProfileStore(
                        self._snapshot_service._s3, self._snapshot_service._bucket
                    )
                    await capture_ide_profile(
                        store, str(user_id), ssh_host, int(ssh_port), profile
                    )
        except Exception:
            logger.debug(
                "ide settings teardown pull failed for job %s", job_id, exc_info=True
            )

        # Mark as suspending (prevents re-entry from sweeper)
        if ws_ctx:
            await self._db.merge_workspace_container_context(
                job_id, {"status": "suspending"}
            )
        elif vm_ctx:
            await self._db.merge_vm_context(job_id, {"status": "suspending"})

        try:
            # Capture environment to S3
            source_type = "vm" if vm_ctx and not ws_ctx else "pod"
            ok = await self._snapshot_service.capture_vm_snapshot(
                job_id=job_id,
                ssh_host=ssh_host,
                ssh_port=int(ssh_port),
                source_type=source_type,
            )
            if not ok:
                logger.warning(
                    "Snapshot capture failed for job %s — keeping workspace alive",
                    job_id,
                )
                if ws_ctx:
                    await self._db.merge_workspace_container_context(
                        job_id, {"status": "ready"}
                    )
                elif vm_ctx:
                    await self._db.merge_vm_context(job_id, {"status": "ready"})
                return False

            # Tear down based on provisioner type
            suspended_ctx: dict[str, Any] = {
                "status": "suspended",
                "suspended_at": datetime.now(timezone.utc).isoformat(),
            }

            if provisioner_type == "docker" and self._docker_provisioner:
                await self._docker_provisioner._reset_workspace_via_ssh(
                    ws_ctx.get("host", ""), int(ws_ctx.get("port", 22))
                )
                suspended_ctx.update({"pod_ip": None, "pod_name": None})
                await self._db.merge_workspace_container_context(job_id, suspended_ctx)
            elif vm_ctx and self._vm_provisioner and self._vm_provisioner.is_available:
                await self._vm_provisioner.delete_vm(job_id)
                await self._db.merge_vm_context(job_id, suspended_ctx)
            else:
                # K8s container (default)
                await self._container_provisioner.delete_workspace(
                    WorkspaceOwner.job(job_id)
                )
                suspended_ctx.update({"pod_ip": None, "pod_name": None})
                await self._db.merge_workspace_container_context(job_id, suspended_ctx)

            logger.info("Workspace suspended to S3 for job %s", job_id)
            return True

        except Exception:
            logger.exception("Failed to suspend workspace for job %s", job_id)
            try:
                if ws_ctx:
                    await self._db.merge_workspace_container_context(
                        job_id, {"status": "ready"}
                    )
                elif vm_ctx:
                    await self._db.merge_vm_context(job_id, {"status": "ready"})
            except Exception:
                pass
            return False

    # =========================================================================
    # Restore: create pod → extract snapshot
    # =========================================================================

    async def restore_workspace(self, job_id: str) -> bool:
        """Provision a fresh workspace and extract the S3 snapshot into it.

        Dispatches to the correct provisioner based on workspace metadata:
        - K8s container: create pod → SSH extract
        - Docker Compose: container already running → SSH extract
        - VM: create VM → SSH extract

        Returns True if provisioning + snapshot extraction succeeded.
        """
        if not self.is_enabled or not self._db:
            return False

        job = await self._db.get_job(job_id)
        if not job:
            return False

        ctx = job.get("context") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except (json.JSONDecodeError, TypeError):
                ctx = {}

        ws_ctx = ctx.get("workspace_container", {})
        vm_ctx = ctx.get("vm", {})
        provisioner_type = ws_ctx.get("provisioner")

        if ws_ctx:
            await self._db.merge_workspace_container_context(
                job_id, {"status": "restoring"}
            )
        elif vm_ctx:
            await self._db.merge_vm_context(job_id, {"status": "restoring"})

        try:
            ssh_host = None

            if provisioner_type == "docker" and self._docker_provisioner:
                # Docker Compose: container is already running, just get its host
                ssh_host = ws_ctx.get("host")
                if not ssh_host:
                    # Need to re-assign a workspace slot
                    result = await self._docker_provisioner.assign_workspace(job_id)
                    if not result:
                        await self._db.merge_workspace_container_context(
                            job_id,
                            {
                                "status": "failed",
                                "error": "no workspace available for restore",
                            },
                        )
                        return False
                    ssh_host = result["host"]

            elif vm_ctx and self._vm_provisioner and self._vm_provisioner.is_available:
                # VM: create a fresh VM
                ok = await self._vm_provisioner.create_vm(job_id)
                if not ok:
                    logger.error("Failed to create VM for restore of job %s", job_id)
                    await self._db.merge_vm_context(
                        job_id,
                        {"status": "failed", "error": "VM creation failed on restore"},
                    )
                    return False

                # Re-read context to get SSH coordinates
                job = await self._db.get_job(job_id)
                vm_ctx = (job.get("context") or {}).get("vm", {})
                ssh_host = vm_ctx.get("ssh_host")

            else:
                # K8s container (default): create a fresh pod
                ok = await self._container_provisioner.create_workspace(
                    WorkspaceOwner.job(job_id)
                )
                if not ok:
                    logger.error("Failed to create pod for restore of job %s", job_id)
                    await self._db.merge_workspace_container_context(
                        job_id,
                        {
                            "status": "failed",
                            "error": "pod creation failed on restore",
                        },
                    )
                    return False

                job = await self._db.get_job(job_id)
                ws_ctx = (job.get("context") or {}).get("workspace_container", {})
                ssh_host = ws_ctx.get("pod_ip")

            if not ssh_host:
                error_msg = "no SSH host after provisioning for restore"
                logger.error("%s (job %s)", error_msg, job_id)
                if ws_ctx:
                    await self._db.merge_workspace_container_context(
                        job_id, {"status": "failed", "error": error_msg}
                    )
                elif vm_ctx:
                    await self._db.merge_vm_context(
                        job_id, {"status": "failed", "error": error_msg}
                    )
                return False

            # Extract snapshot into the workspace
            ssh_port = _resolve_ssh_port(ws_ctx, vm_ctx)
            await self._extract_snapshot(job_id, ssh_host, ssh_port=ssh_port)

            # Mark as ready
            restored_ctx = {
                "status": "ready",
                "restored_at": datetime.now(timezone.utc).isoformat(),
            }
            if ws_ctx:
                await self._db.merge_workspace_container_context(job_id, restored_ctx)
            elif vm_ctx:
                await self._db.merge_vm_context(job_id, restored_ctx)

            logger.info(
                "Workspace restored from S3 for job %s (ssh_host=%s)",
                job_id,
                ssh_host,
            )
            return True

        except Exception:
            logger.exception("Failed to restore workspace for job %s", job_id)
            if ws_ctx:
                await self._db.merge_workspace_container_context(
                    job_id, {"status": "failed", "error": "restore exception"}
                )
            elif vm_ctx:
                await self._db.merge_vm_context(
                    job_id, {"status": "failed", "error": "restore exception"}
                )
            return False

    async def _extract_snapshot(
        self,
        entity_id: str,
        ssh_host: str,
        ssh_port: int = 22,
        entity_type: str = "jobs",
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

            key_path = resolve_ssh_key_path()
            if not key_path:
                logger.warning(
                    "No SSH key available for snapshot extraction (%s %s)",
                    entity_type.rstrip("s"),
                    entity_id,
                )
            rc, stderr = await stream_extract_snapshot(
                ssh_host, ssh_port, tar_path, key_path=key_path
            )

            if rc != 0:
                logger.warning(
                    "Snapshot extraction had errors for %s %s (rc=%d): %s",
                    entity_type.rstrip("s"),
                    entity_id,
                    rc,
                    stderr.decode(errors="replace")[:500],
                )

    async def restore(self, owner: WorkspaceOwner) -> bool:
        """Owner-keyed restore: job -> restore_workspace, session -> restore_thread_workspace."""
        if owner.kind == "job":
            return await self.restore_workspace(owner.id)
        return await self.restore_thread_workspace(owner.id)

    # =========================================================================
    # Thread suspension (mirrors job suspension for persistent agent threads)
    # =========================================================================

    async def suspend_thread_workspace(self, thread_id: str) -> bool:
        """Capture thread workspace snapshot to S3, then tear down.

        Dispatches to the correct provisioner based on workspace metadata.

        Returns True if snapshot + teardown succeeded.
        On failure, reverts status to 'ready' and keeps the workspace alive.
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
        vm_ctx = metadata.get("vm", {})
        provisioner_type = ws_ctx.get("provisioner")

        ssh_host = ws_ctx.get("pod_ip") or ws_ctx.get("host") or vm_ctx.get("ssh_host")
        ssh_port = _resolve_ssh_port(ws_ctx, vm_ctx)

        if not ssh_host:
            return False

        ws_status = ws_ctx.get("status") if ws_ctx else vm_ctx.get("status")
        if ws_status != "ready":
            return False

        if ws_ctx:
            await self._db.merge_thread_workspace_context(
                thread_id, {"status": "suspending"}
            )
        elif vm_ctx:
            await self._db.merge_thread_vm_context(thread_id, {"status": "suspending"})

        try:
            source_type = "vm" if vm_ctx and not ws_ctx else "pod"
            ok = await self._snapshot_service.capture_vm_snapshot(
                job_id=thread_id,
                ssh_host=ssh_host,
                ssh_port=int(ssh_port),
                source_type=source_type,
                entity_type="threads",
            )
            if not ok:
                logger.warning(
                    "Snapshot capture failed for thread %s — keeping workspace alive",
                    thread_id,
                )
                if ws_ctx:
                    await self._db.merge_thread_workspace_context(
                        thread_id, {"status": "ready"}
                    )
                elif vm_ctx:
                    await self._db.merge_thread_vm_context(
                        thread_id, {"status": "ready"}
                    )
                return False

            # Tear down based on provisioner type
            suspended_ctx: dict[str, Any] = {
                "status": "suspended",
                "suspended_at": datetime.now(timezone.utc).isoformat(),
            }

            if provisioner_type == "docker" and self._docker_provisioner:
                await self._docker_provisioner._reset_workspace_via_ssh(
                    ws_ctx.get("host", ""), int(ws_ctx.get("port", 22))
                )
                suspended_ctx.update({"pod_ip": None, "pod_name": None})
                await self._db.merge_thread_workspace_context(thread_id, suspended_ctx)
            elif vm_ctx and self._vm_provisioner and self._vm_provisioner.is_available:
                await self._vm_provisioner.delete_thread_vm(thread_id)
                await self._db.merge_thread_vm_context(thread_id, suspended_ctx)
            else:
                await self._container_provisioner.delete_workspace(
                    WorkspaceOwner.session(thread_id)
                )
                suspended_ctx.update({"pod_ip": None, "pod_name": None})
                await self._db.merge_thread_workspace_context(thread_id, suspended_ctx)

            # Also delete the agent pod (it's stateless, state is in the workspace)
            if self._agent_provisioner and self._agent_provisioner.is_available:
                await self._agent_provisioner.delete_agent_pod_by_thread(thread_id)

            logger.info("Workspace suspended to S3 for thread %s", thread_id)
            return True

        except Exception:
            logger.exception("Failed to suspend workspace for thread %s", thread_id)
            try:
                if ws_ctx:
                    await self._db.merge_thread_workspace_context(
                        thread_id, {"status": "ready"}
                    )
                elif vm_ctx:
                    await self._db.merge_thread_vm_context(
                        thread_id, {"status": "ready"}
                    )
            except Exception:
                pass
            return False

    async def restore_thread_workspace(self, thread_id: str) -> bool:
        """Provision a fresh workspace and extract the S3 snapshot into it.

        Dispatches to the correct provisioner based on workspace metadata.

        Returns True if provisioning + snapshot extraction succeeded.
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
        vm_ctx = metadata.get("vm", {})
        provisioner_type = ws_ctx.get("provisioner")

        if ws_ctx:
            await self._db.merge_thread_workspace_context(
                thread_id, {"status": "restoring"}
            )
        elif vm_ctx:
            await self._db.merge_thread_vm_context(thread_id, {"status": "restoring"})

        try:
            ssh_host = None

            if provisioner_type == "docker" and self._docker_provisioner:
                # Docker: container already running, get its host
                ssh_host = ws_ctx.get("host")
                if not ssh_host:
                    result = await self._docker_provisioner.assign_thread_workspace(
                        thread_id
                    )
                    if not result:
                        await self._db.merge_thread_workspace_context(
                            thread_id,
                            {
                                "status": "failed",
                                "error": "no workspace available for restore",
                            },
                        )
                        return False
                    ssh_host = result["host"]

            elif vm_ctx and self._vm_provisioner and self._vm_provisioner.is_available:
                ok = await self._vm_provisioner.create_thread_vm(thread_id)
                if not ok:
                    logger.error(
                        "Failed to create VM for restore of thread %s", thread_id
                    )
                    await self._db.merge_thread_vm_context(
                        thread_id,
                        {
                            "status": "failed",
                            "error": "VM creation failed on restore",
                        },
                    )
                    return False

                thread = await self._db.get_thread(thread_id)
                metadata = thread.get("metadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (ValueError, TypeError):
                        metadata = {}
                vm_ctx = metadata.get("vm", {})
                ssh_host = vm_ctx.get("ssh_host")

            else:
                # K8s container (default)
                ok = await self._container_provisioner.create_workspace(
                    WorkspaceOwner.session(thread_id)
                )
                if not ok:
                    logger.error(
                        "Failed to create pod for restore of thread %s", thread_id
                    )
                    await self._db.merge_thread_workspace_context(
                        thread_id,
                        {
                            "status": "failed",
                            "error": "pod creation failed on restore",
                        },
                    )
                    return False

                thread = await self._db.get_thread(thread_id)
                metadata = thread.get("metadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (ValueError, TypeError):
                        metadata = {}
                ws_ctx = metadata.get("workspace_container", {})
                ssh_host = ws_ctx.get("pod_ip")

            if not ssh_host:
                error_msg = "no SSH host after provisioning for restore"
                logger.error("%s (thread %s)", error_msg, thread_id)
                if ws_ctx:
                    await self._db.merge_thread_workspace_context(
                        thread_id, {"status": "failed", "error": error_msg}
                    )
                elif vm_ctx:
                    await self._db.merge_thread_vm_context(
                        thread_id, {"status": "failed", "error": error_msg}
                    )
                return False

            # Extract snapshot into the workspace
            ssh_port = _resolve_ssh_port(ws_ctx, vm_ctx)
            await self._extract_snapshot(
                thread_id, ssh_host, ssh_port=ssh_port, entity_type="threads"
            )

            restored_ctx = {
                "status": "ready",
                "restored_at": datetime.now(timezone.utc).isoformat(),
            }
            if ws_ctx:
                await self._db.merge_thread_workspace_context(thread_id, restored_ctx)
            elif vm_ctx:
                await self._db.merge_thread_vm_context(thread_id, restored_ctx)

            logger.info(
                "Workspace restored from S3 for thread %s (ssh_host=%s)",
                thread_id,
                ssh_host,
            )
            return True

        except Exception:
            logger.exception("Failed to restore workspace for thread %s", thread_id)
            if ws_ctx:
                await self._db.merge_thread_workspace_context(
                    thread_id, {"status": "failed", "error": "restore exception"}
                )
            elif vm_ctx:
                await self._db.merge_thread_vm_context(
                    thread_id, {"status": "failed", "error": "restore exception"}
                )
            return False

    async def check_idle_threads(self) -> int:
        """Sweep idle thread workspaces (containers + VMs) and suspend them.

        A thread workspace is idle if:
        - Thread status is 'ended' (no active WebSocket; agent detached or orphaned)
        - Workspace or VM status is 'ready'
        - last_activity is older than idle_timeout_minutes

        Returns the count of workspaces suspended.
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
                    WHERE status = 'ended'
                      AND (
                          metadata->'workspace_container'->>'status' = 'ready'
                          OR metadata->'vm'->>'status' = 'ready'
                      )
                    """,
                )
        except Exception:
            logger.exception("Failed to query ended thread workspace containers")
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
        """Sweep all ready workspaces (containers + VMs) and suspend idle ones.

        A workspace is idle if:
        - The job is in paused/pending_review/waiting_for_reply status
        - last_activity (or updated_at) is older than idle_timeout_minutes

        Returns the count of workspaces suspended.
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
                    WHERE status IN ('paused', 'pending_review', 'waiting_for_reply')
                      AND (
                          context->'workspace_container'->>'status' = 'ready'
                          OR context->'vm'->>'status' = 'ready'
                      )
                    """,
                )
        except Exception:
            logger.exception("Failed to query idle workspaces")
            return 0

        for row in rows:
            job_id = str(row["id"])
            ctx = row.get("context") or {}
            if isinstance(ctx, str):
                try:
                    ctx = json.loads(ctx)
                except (ValueError, TypeError):
                    ctx = {}
            if not isinstance(ctx, dict):
                ctx = {}

            # Check both workspace_container and vm contexts
            ws_ctx = ctx.get("workspace_container", {})
            vm_ctx = ctx.get("vm", {})
            last_activity_str = ws_ctx.get("last_activity") or vm_ctx.get(
                "last_activity"
            )

            if last_activity_str:
                try:
                    last_activity = datetime.fromisoformat(last_activity_str)
                except (ValueError, TypeError):
                    last_activity = row.get("updated_at", now)
            else:
                last_activity = row.get("updated_at", now)

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
