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
from services.vm_provisioner import vm_persistent_rootdisk_enabled
from services.workspace_lifecycle import WorkspaceOwner

logger = logging.getLogger(__name__)


def _thread_is_vm_tier(metadata: dict, ws_ctx: dict, vm_ctx: dict) -> bool:
    """Is this THREAD's workspace a VM rather than a container?

    Ask the resolved tier; never infer it from which metadata keys exist.
    ``metadata.workspace_container`` is present on *every* thread because
    ``_setup_gitea`` writes ``git_remote_url``/``repo_name`` there for all tiers,
    so "the key exists" does NOT mean a container was provisioned. Reading
    presence made every VM session look pod-tier, which made
    ``suspend_thread_workspace`` bail before doing anything — VM sessions could
    never be suspended and their VMs ran until the session ended.
    docs/issues/workspace_suspension_infers_tier_from_metadata_presence.md

    Jobs are deliberately NOT covered by this: a job's
    ``context.workspace_container`` is written only by container provisioning
    (its git_remote_url lives at the context root), so presence really does imply
    pod-tier there and the job paths keep their existing checks.
    """
    from src.core.backends.factory import is_vm_backend

    # A materialized VM beats the declared backend, because upgrade-to-VM
    # provisions metadata.vm WITHOUT rewriting config_override.workspace.backend
    # — an upgraded session still declares 'sandbox' or 'virtual' forever. Pod
    # *state* (not mere presence) is what rules a container in: a git-only
    # workspace_container has no 'status', and _setup_gitea writes one of those
    # for every tier.
    #
    # Checked FIRST. It used to sit behind `if backend: return
    # is_vm_backend(backend)`, which returned early for any non-empty string and
    # so made this branch unreachable in exactly the case it was written for —
    # every VM-upgraded thread read as pod-tier and refused to suspend
    # (live-gate finding, thread b4ae24bb).
    if vm_ctx.get("status") and not ws_ctx.get("status"):
        return True

    backend = ((metadata.get("config_override") or {}).get("workspace") or {}).get(
        "backend"
    )
    if backend:
        return is_vm_backend(backend)
    return False


def _resolve_ssh_port(ws_ctx: dict, vm_ctx: dict, is_vm: Optional[bool] = None) -> int:
    """Resolve the snapshot SSH port by workspace kind.

    Container/pod workspaces run sshd on 30022; only true VM contexts use the
    VM ssh_port (default 22). Previously both fell through to a VM-shaped 22
    default, which silently broke pod snapshots when ``port`` was absent from
    the stored context (the cause of the dev-cluster leaked-pod incident).

    ``is_vm`` is the caller's resolved tier. Thread callers pass it because
    ``ws_ctx`` is truthy for every thread (git coordinates), so the presence
    fallback would hand a VM the pod port 30022. Job callers omit it and keep the
    presence behaviour, which is correct for them.
    """
    if is_vm is True:
        return int(vm_ctx.get("ssh_port", 22))
    if is_vm is False:
        return int(ws_ctx.get("port", 30022))
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
            self._container_provisioner is not None
            and self._container_provisioner.is_available
        ) or (self._vm_provisioner is not None and self._vm_provisioner.is_available)

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
        if provisioner_type == "docker":
            logger.warning(
                "Static Docker workspace suspension is disabled; safe reuse "
                "requires controller-attested container recreation (job %s)",
                job_id,
            )
            return False

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
            # Same reasoning as the thread path below: with a persistent
            # rootdisk the disk carries the workspace across the teardown, so a
            # capture that can never succeed for a VM stops being a
            # precondition. A pod has no disk to keep and stays fail-closed.
            disk_survives_teardown = (
                source_type == "vm" and vm_persistent_rootdisk_enabled()
            )

            if not ok:
                if not disk_survives_teardown:
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
                logger.info(
                    "Snapshot capture unavailable for job %s — suspending anyway; "
                    "the persistent rootdisk carries the workspace across teardown",
                    job_id,
                )

            # Tear down based on provisioner type
            suspended_ctx: dict[str, Any] = {
                "status": "suspended",
                "suspended_at": datetime.now(timezone.utc).isoformat(),
            }

            if vm_ctx and self._vm_provisioner and self._vm_provisioner.is_available:
                await self._vm_provisioner.delete_vm(
                    job_id, purge_disk=not disk_survives_teardown
                )
                if disk_survives_teardown:
                    suspended_ctx["rootdisk"] = "kept"
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
        if provisioner_type == "docker":
            logger.warning(
                "Static Docker workspace restore is disabled; safe reuse "
                "requires controller-attested container recreation (job %s)",
                job_id,
            )
            return False

        if ws_ctx:
            await self._db.merge_workspace_container_context(
                job_id, {"status": "restoring"}
            )
        elif vm_ctx:
            await self._db.merge_vm_context(job_id, {"status": "restoring"})

        try:
            ssh_host = None

            if vm_ctx and self._vm_provisioner and self._vm_provisioner.is_available:
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
        if provisioner_type == "docker":
            logger.warning(
                "Static Docker workspace suspension is disabled; safe reuse "
                "requires controller-attested container recreation (thread %s)",
                thread_id,
            )
            return False

        # Resolve the tier ONCE, explicitly. Everything below keys off this
        # instead of asking whether workspace_container happens to exist.
        is_vm = _thread_is_vm_tier(metadata, ws_ctx, vm_ctx)

        ssh_host = ws_ctx.get("pod_ip") or ws_ctx.get("host") or vm_ctx.get("ssh_host")
        ssh_port = _resolve_ssh_port(ws_ctx, vm_ctx, is_vm=is_vm)

        if not ssh_host:
            return False

        ws_status = vm_ctx.get("status") if is_vm else ws_ctx.get("status")
        if ws_status in ("suspended", "suspending"):
            # A concurrent/earlier suspend already handled (or is handling)
            # this thread. Returning False here made the caller's fallback
            # path log "suspend unavailable or failed" and delete the agent
            # pod a second time right after a successful suspend
            # (docs/issues/session_silent_failure_audit.md #13).
            logger.info(
                "Workspace for thread %s already %s — skipping duplicate suspend",
                thread_id,
                ws_status,
            )
            return True
        if ws_status != "ready":
            return False

        if is_vm:
            await self._db.merge_thread_vm_context(thread_id, {"status": "suspending"})
        else:
            await self._db.merge_thread_workspace_context(
                thread_id, {"status": "suspending"}
            )

        try:
            source_type = "vm" if is_vm else "pod"

            # Slice C (design §5): stage the protected session's upperdir
            # diff to S3 BEFORE the teardown snapshot — this is the last
            # chance to capture it while the overlay is still mounted.
            # ``metadata`` here is already the parsed dict (str-JSON handled
            # above), so reuse it rather than re-reading + re-parsing
            # ``thread["metadata"]``. Best-effort only: a staging failure
            # must never block or fail the teardown — the VM snapshot below
            # is the durable, load-bearing path and always still runs.
            if metadata.get("protected_cloud"):
                try:
                    from services.cloud_staging.stage import stage_thread_cloud_diff

                    await stage_thread_cloud_diff(
                        thread_id=thread_id,
                        postgres_db=self._db,
                        snapshot_service=self._snapshot_service,
                    )
                except Exception as e:
                    logger.warning(
                        "teardown cloud-stage failed (non-fatal) for %s: %s",
                        thread_id,
                        e,
                    )

            ok = await self._snapshot_service.capture_vm_snapshot(
                job_id=thread_id,
                ssh_host=ssh_host,
                ssh_port=int(ssh_port),
                source_type=source_type,
                entity_type="threads",
            )
            # With a persistent rootdisk the VM's disk outlives the VM, so the
            # snapshot stops being the only copy of the workspace and stops
            # being a precondition for tearing down. That matters because for a
            # VM the capture can never succeed from here: it SSHes from the
            # orchestrator, and a VM workspace is only reachable over the
            # tailnet the orchestrator has no route to. Fail-closed on a gate
            # that always fails is why VM sessions never suspended at all.
            # A pod has no disk to keep, so it stays fail-closed.
            disk_survives_teardown = is_vm and vm_persistent_rootdisk_enabled()

            if not ok:
                if not disk_survives_teardown:
                    logger.warning(
                        "Snapshot capture failed for thread %s — keeping workspace alive",
                        thread_id,
                    )
                    if is_vm:
                        await self._db.merge_thread_vm_context(
                            thread_id, {"status": "ready"}
                        )
                    else:
                        await self._db.merge_thread_workspace_context(
                            thread_id, {"status": "ready"}
                        )
                    return False
                logger.info(
                    "Snapshot capture unavailable for thread %s — suspending anyway; "
                    "the persistent rootdisk carries the workspace across teardown",
                    thread_id,
                )

            # Tear down based on provisioner type
            suspended_ctx: dict[str, Any] = {
                "status": "suspended",
                "suspended_at": datetime.now(timezone.utc).isoformat(),
            }

            if is_vm and self._vm_provisioner and self._vm_provisioner.is_available:
                await self._vm_provisioner.delete_thread_vm(
                    thread_id, purge_disk=not disk_survives_teardown
                )
                if disk_survives_teardown:
                    # Optimistic; the vm.lifecycle.status handler overwrites it
                    # with what the controller actually did. Restore reads it to
                    # decide whether to extract a snapshot over the disk.
                    suspended_ctx["rootdisk"] = "kept"
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
                if is_vm:
                    await self._db.merge_thread_vm_context(
                        thread_id, {"status": "ready"}
                    )
                else:
                    await self._db.merge_thread_workspace_context(
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
        if provisioner_type == "docker":
            logger.warning(
                "Static Docker workspace restore is disabled; safe reuse "
                "requires controller-attested container recreation (thread %s)",
                thread_id,
            )
            return False

        # Same explicit tier read as suspend — presence of workspace_container
        # says nothing about the tier (see _thread_is_vm_tier).
        is_vm = _thread_is_vm_tier(metadata, ws_ctx, vm_ctx)

        # Read BEFORE provisioning: vm_ctx is re-read from fresh metadata after
        # create_thread_vm, by which point this key reflects the new VM's
        # lifecycle rather than the suspend that put the thread here.
        rootdisk_kept = vm_ctx.get("rootdisk") == "kept"

        if is_vm:
            await self._db.merge_thread_vm_context(thread_id, {"status": "restoring"})
        else:
            await self._db.merge_thread_workspace_context(
                thread_id, {"status": "restoring"}
            )

        try:
            ssh_host = None

            if is_vm and self._vm_provisioner and self._vm_provisioner.is_available:
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
                if is_vm:
                    await self._db.merge_thread_vm_context(
                        thread_id, {"status": "failed", "error": error_msg}
                    )
                else:
                    await self._db.merge_thread_workspace_context(
                        thread_id, {"status": "failed", "error": error_msg}
                    )
                return False

            # Extract snapshot into the workspace — unless the VM reattached a
            # kept rootdisk, in which case the files are already there and are
            # *newer* than any snapshot (the disk is the live state at teardown;
            # a snapshot is the same moment at best, stale at worst). Extracting
            # over it would overwrite newer files with older ones.
            ssh_port = _resolve_ssh_port(ws_ctx, vm_ctx, is_vm=is_vm)
            if is_vm and rootdisk_kept:
                logger.info(
                    "Skipping snapshot extract for thread %s — its persistent "
                    "rootdisk was reattached and already holds the workspace",
                    thread_id,
                )
            else:
                await self._extract_snapshot(
                    thread_id, ssh_host, ssh_port=ssh_port, entity_type="threads"
                )

            restored_ctx = {
                "status": "ready",
                "restored_at": datetime.now(timezone.utc).isoformat(),
            }
            if is_vm:
                await self._db.merge_thread_vm_context(thread_id, restored_ctx)
            else:
                await self._db.merge_thread_workspace_context(thread_id, restored_ctx)

            logger.info(
                "Workspace restored from S3 for thread %s (ssh_host=%s)",
                thread_id,
                ssh_host,
            )
            return True

        except Exception:
            logger.exception("Failed to restore workspace for thread %s", thread_id)
            if is_vm:
                await self._db.merge_thread_vm_context(
                    thread_id, {"status": "failed", "error": "restore exception"}
                )
            else:
                await self._db.merge_thread_workspace_context(
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
