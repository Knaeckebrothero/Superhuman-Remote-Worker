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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from services import resolve_ssh_key_path
from services.container_provisioner import WORKSPACE_RUNTIME_INCARNATION_KEY
from services.ssh_helpers import (
    EXTRACT_HOME_REMOTE_CMD,
    EXTRACT_REMOTE_CMD,
    stream_extract_snapshot,
)
from services.vm_provisioner import vm_persistent_rootdisk_enabled
from services.workspace_binding import CANVAS_WORKSPACE_GENERATION_KEY
from services.workspace_lifecycle import WorkspaceOwner

logger = logging.getLogger(__name__)


# Durable thread-workspace intent.  A restore can fail after creating a fresh
# emptyDir/PVC-backed pod; a later reconcile must retry snapshot extraction,
# not fall through to generic create and publish that empty/partial workspace as
# Ready.  The marker is set before every thread restore attempt and cleared only
# after the snapshot/reattach path has completed successfully.
WORKSPACE_SNAPSHOT_RESTORE_REQUIRED_KEY = "_snapshot_restore_required"


@dataclass(frozen=True, slots=True)
class _StrictSessionRestoreAuthority:
    """Exact post-create authority retained across a terminal snapshot extract."""

    workspace_generation: str
    runtime_incarnation: str
    endpoint_generation: str
    backing_id: str
    host_key_fingerprint: str
    ssh_host: str
    ssh_port: int
    workspace_status: str


def _thread_metadata(thread: dict[str, Any]) -> dict[str, Any]:
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("thread workspace metadata is malformed") from exc
    if not isinstance(metadata, dict):
        raise RuntimeError("thread workspace metadata is malformed")
    return metadata


def _canonical_uuid(value: Any, *, label: str) -> str:
    try:
        canonical = str(UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"strict restore {label} is unavailable") from exc
    if value != canonical:
        raise RuntimeError(f"strict restore {label} is not canonical")
    return canonical


def _strict_session_restore_authority(
    thread: dict[str, Any],
) -> _StrictSessionRestoreAuthority:
    """Parse the exact provisioner-attested tuple used by a strict restore."""

    metadata = _thread_metadata(thread)
    workspace = metadata.get("workspace_container")
    binding = metadata.get("_workspace_binding")
    if not isinstance(workspace, dict) or not isinstance(binding, dict):
        raise RuntimeError("strict restore workspace authority is malformed")
    if workspace.get(WORKSPACE_SNAPSHOT_RESTORE_REQUIRED_KEY) is not True:
        raise RuntimeError("strict restore intent is not armed")
    workspace_status = workspace.get("status")
    if not isinstance(workspace_status, str) or workspace_status not in {
        "ready",
        "failed",
        "restoring",
        "deleted",
        "suspended",
        "created",
        "creating",
        "pending",
    }:
        raise RuntimeError("strict restore endpoint status is not quarantined")
    if binding.get("kind") != "remote":
        raise RuntimeError("strict restore backing is not remote")

    workspace_generation = _canonical_uuid(
        binding.get("generation"), label="workspace generation"
    )
    endpoint_generation = _canonical_uuid(
        workspace.get(CANVAS_WORKSPACE_GENERATION_KEY),
        label="endpoint generation",
    )
    if endpoint_generation != workspace_generation:
        raise RuntimeError("strict restore endpoint generation changed")
    runtime_incarnation = _canonical_uuid(
        workspace.get(WORKSPACE_RUNTIME_INCARNATION_KEY),
        label="runtime incarnation",
    )

    backing_id = binding.get("backing_id")
    if not isinstance(backing_id, str) or not backing_id.startswith(
        ("k8s-pod:", "k8s-pvc:")
    ):
        raise RuntimeError("strict restore backing identity is unavailable")
    fingerprint = binding.get("ssh_host_key_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or not fingerprint.startswith("SHA256:")
        or len(fingerprint) > 128
        or any(char.isspace() for char in fingerprint)
    ):
        raise RuntimeError("strict restore SSH host-key fingerprint is unavailable")
    ssh_host = workspace.get("pod_ip")
    if (
        not isinstance(ssh_host, str)
        or not ssh_host
        or any(ord(char) < 33 or ord(char) == 127 for char in ssh_host)
    ):
        raise RuntimeError("strict restore SSH endpoint is unavailable")
    ssh_port_raw = workspace.get("port")
    if type(ssh_port_raw) is not int or not 1 <= ssh_port_raw <= 65535:
        raise RuntimeError("strict restore SSH endpoint port is invalid")

    return _StrictSessionRestoreAuthority(
        workspace_generation=workspace_generation,
        runtime_incarnation=runtime_incarnation,
        endpoint_generation=endpoint_generation,
        backing_id=backing_id,
        host_key_fingerprint=fingerprint,
        ssh_host=ssh_host,
        ssh_port=ssh_port_raw,
        workspace_status=workspace_status,
    )


def _reclaim_on_idle_enabled() -> bool:
    """Opt-in gate for dropping a session's hot-cache PVC on idle-suspend.

    Default OFF: today's retain-on-idle behavior (snapshot + delete pod, keep
    the PVC) is unchanged unless an operator turns this on. When enabled, the
    PVC is dropped only after ``verify_snapshot`` confirms the S3 archive is
    restorable — see the call site in ``suspend_thread_workspace``.
    Truthy-token parsing mirrors ``canvas_snapshots.snapshots_enabled()``.
    See knowledge-base/knowledge/features/workspace_durability_tiering.md §D3.
    """
    return os.environ.get("WORKSPACE_RECLAIM_ON_IDLE", "false").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


def _thread_is_vm_tier(metadata: dict, ws_ctx: dict, vm_ctx: dict) -> bool:
    """Is this THREAD's workspace a VM rather than a container?

    Ask the resolved tier; never infer it from which metadata keys exist.
    ``metadata.workspace_container`` is present on *every* thread because
    ``_setup_gitea`` writes ``git_remote_url``/``repo_name`` there for all tiers,
    so "the key exists" does NOT mean a container was provisioned. Reading
    presence made every VM session look pod-tier, which made
    ``suspend_thread_workspace`` bail before doing anything — VM sessions could
    never be suspended and their VMs ran until the session ended.
    knowledge-base/knowledge/issues/workspace_suspension_infers_tier_from_metadata_presence.md

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


# ``ContainerProvisioner._trusted_pod_ssh_identity`` mints the thread's
# ``_workspace_binding.backing_id`` from the PVC when — and only when — the
# workspace pod mounts one, as ``k8s-pvc:<namespace>:<pvc-uid>``; an emptyDir
# pod is bound to ``k8s-pod:<namespace>:<pod-uid>`` instead. That makes the
# string the authoritative reattach signal: the PVC's UID is stable across every
# pod recreate that reattaches the SAME volume, and changes the moment a new
# volume is minted (first PVC-backed create, a rollback to emptyDir, or the
# single-replica node-loss fallback discarding a wedged PVC).
_K8S_PVC_BACKING_PREFIX = "k8s-pvc:"


def _workspace_backing_id(metadata: dict) -> str:
    """The thread's currently bound workspace backing id ("" when unbound)."""
    binding = metadata.get("_workspace_binding")
    if not isinstance(binding, dict):
        return ""
    backing_id = binding.get("backing_id")
    return backing_id if isinstance(backing_id, str) else ""


def _volume_survived_teardown(
    prior_backing_id: str, current_backing_id: str
) -> tuple[bool, str]:
    """Did the restored pod come back on the volume it already had?

    The container-tier twin of the VM path's ``rootdisk == "kept"``: when the
    answer is yes, the disk already holds the workspace and extracting the older
    S3 tarball over it would replace newer files with older ones.

    Returns ``(reattached, reason)`` — the reason is log copy for the skip.
    """
    if not prior_backing_id.startswith(_K8S_PVC_BACKING_PREFIX):
        # emptyDir (or never bound at all): storage died with the pod, so the
        # S3 snapshot is the only copy and must be unrolled. This is also the
        # mixed-fleet upgrade case — a session suspended before PVCs were
        # enabled restores onto a brand-new empty volume.
        return False, ""
    if not current_backing_id:
        # The post-create rebind is best-effort (create_workspace logs and
        # continues when it raises), so its absence is ambiguous rather than
        # informative. The tie goes to the volume: the generation we came in
        # with was PVC-backed, a PVC survives every non-permanent teardown, and
        # unrolling a stale tarball over live files is the unrecoverable
        # mistake — a skipped extract is not.
        return True, "binding unavailable, prior generation was PVC-backed"
    if current_backing_id == prior_backing_id:
        return True, "same PVC reattached"
    # A different backing id means a different volume (or an emptyDir pod after
    # a flag rollback): whatever the pod is mounting now, it is not the tree we
    # suspended, so restore it from S3.
    return False, ""


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
                deleted = await self._vm_provisioner.delete_vm(
                    job_id, purge_disk=not disk_survives_teardown
                )
                if not deleted:
                    raise RuntimeError(
                        "VM suspension process-zero retirement is incomplete"
                    )
                if disk_survives_teardown:
                    suspended_ctx["rootdisk"] = "kept"
                await self._db.merge_vm_context(job_id, suspended_ctx)
            else:
                # K8s container (default)
                deleted = await self._container_provisioner.delete_workspace(
                    WorkspaceOwner.job(job_id)
                )
                if not deleted:
                    raise RuntimeError(
                        "workspace suspension process-zero retirement is incomplete"
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
            # Pod vs VM decides the extract scope below: a pod extract runs as
            # the unprivileged agent-host user and must NOT try to overwrite the
            # image-provided, root-owned /usr/local (scoped_home), while a VM
            # extract (root, the snapshot IS the disk) restores the whole tree.
            restoring_vm = bool(
                vm_ctx and self._vm_provisioner and self._vm_provisioner.is_available
            )

            if restoring_vm:
                # VM: create a fresh VM
                ok = await self._vm_provisioner.create_vm(job_id)
                if not ok:
                    logger.error("Failed to create VM for restore of job %s", job_id)
                    await self._db.merge_vm_context(
                        job_id,
                        {"status": "failed", "error": "VM creation failed on restore"},
                    )
                    return False
                if vm_ctx.get("rootdisk") == "kept":
                    logger.info(
                        "VM restore for job %s is reusing its kept rootdisk; "
                        "readiness is owned by the VM prober",
                        job_id,
                    )
                    return True
                error_msg = (
                    "VM restore without a kept rootdisk requires a post-readiness "
                    "snapshot extract (unsupported in same-cluster mode v1)"
                )
                logger.error("%s (job %s)", error_msg, job_id)
                await self._db.merge_vm_context(
                    job_id, {"status": "failed", "error": error_msg}
                )
                return False

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
                await self._db.merge_workspace_container_context(
                    job_id, {"status": "failed", "error": error_msg}
                )
                return False

            # Extract snapshot into the workspace. A failed extract is a failed
            # restore: the workspace is empty or half-populated, and stamping
            # 'ready' over it hands the dispatcher a blank tree that looks
            # healthy. Fail visibly instead and let the caller decide.
            ssh_port = _resolve_ssh_port(ws_ctx, {})
            if not await self._extract_snapshot(
                job_id, ssh_host, ssh_port=ssh_port, scoped_home=True
            ):
                error_msg = "snapshot extraction failed on restore"
                logger.error("%s (job %s)", error_msg, job_id)
                await self._db.merge_workspace_container_context(
                    job_id, {"status": "failed", "error": error_msg}
                )
                return False

            # Mark as ready
            restored_ctx = {
                "status": "ready",
                "restored_at": datetime.now(timezone.utc).isoformat(),
            }
            await self._db.merge_workspace_container_context(job_id, restored_ctx)

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

    async def _commit_strict_thread_restore_ready(
        self,
        thread_id: str,
        expected: _StrictSessionRestoreAuthority,
    ) -> bool:
        """CAS one exact restored runtime from quarantined to Ready.

        The Kubernetes UID probe happens immediately before this call. The row
        lock then re-reads the complete binding/runtime/endpoint tuple, and the
        guarded UPDATE clears restore intent in the same transaction which
        publishes Ready. A concurrent replacement or lifecycle transition
        therefore wins cleanly and this restore remains quarantined.
        """

        restored_at = datetime.now(timezone.utc).isoformat()
        async with self._db.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT id::text AS id, status::text AS status, "
                    "execution_lane, metadata FROM threads "
                    "WHERE id = $1::uuid FOR UPDATE",
                    thread_id,
                )
                if (
                    row is None
                    or str(row.get("execution_lane") or "") != "stateless"
                    or str(row.get("status") or "")
                    not in {"created", "active", "awaiting_user"}
                ):
                    return False
                try:
                    current = _strict_session_restore_authority(dict(row))
                except RuntimeError:
                    return False
                if current != expected:
                    return False

                updated = await conn.fetchval(
                    f"""
                    UPDATE threads
                    SET metadata = jsonb_set(
                            metadata,
                            '{{workspace_container}}',
                            (metadata->'workspace_container') ||
                                jsonb_build_object(
                                    'status', 'ready',
                                    'restored_at', $9::text,
                                    '{WORKSPACE_SNAPSHOT_RESTORE_REQUIRED_KEY}',
                                    false
                                ),
                            false
                        ),
                        last_activity = CURRENT_TIMESTAMP
                    WHERE id = $1::uuid
                      AND execution_lane = 'stateless'
                      AND status IN ('created', 'active', 'awaiting_user')
                      AND metadata #>
                            '{{workspace_container,{WORKSPACE_SNAPSHOT_RESTORE_REQUIRED_KEY}}}'
                            = 'true'::jsonb
                      AND metadata #>> '{{_workspace_binding,kind}}' = 'remote'
                      AND metadata #>> '{{_workspace_binding,generation}}' = $2::text
                      AND metadata #>> '{{workspace_container,{CANVAS_WORKSPACE_GENERATION_KEY}}}'
                            = $3::text
                      AND metadata #>> '{{workspace_container,{WORKSPACE_RUNTIME_INCARNATION_KEY}}}'
                            = $4::text
                      AND metadata #>> '{{_workspace_binding,backing_id}}' = $5::text
                      AND metadata #>>
                            '{{_workspace_binding,ssh_host_key_fingerprint}}'
                            = $6::text
                      AND metadata #>> '{{workspace_container,pod_ip}}' = $7::text
                      AND metadata #> '{{workspace_container,port}}'
                            = to_jsonb($8::integer)
                      AND metadata #>> '{{workspace_container,status}}' = $10::text
                    RETURNING id
                    """,
                    thread_id,
                    expected.workspace_generation,
                    expected.endpoint_generation,
                    expected.runtime_incarnation,
                    expected.backing_id,
                    expected.host_key_fingerprint,
                    expected.ssh_host,
                    expected.ssh_port,
                    restored_at,
                    expected.workspace_status,
                )
                return updated is not None

    async def _extract_snapshot(
        self,
        entity_id: str,
        ssh_host: str,
        ssh_port: int = 22,
        entity_type: str = "jobs",
        *,
        scoped_home: Optional[bool] = None,
        expected_host_key_fingerprint: Optional[str] = None,
        remote_cmd: Optional[str] = None,
        require_pipefail: bool = False,
    ) -> bool:
        """Download snapshot from S3 and extract into the pod via SSH.

        Mirrors ide_session.py:_extract_snapshot_to_vm (lines 782-836).

        ``scoped_home`` selects the extract command. A snapshot carries both
        ``/home/agent-host`` and ``/usr/local``; for a **container/pod** target
        the extract runs as the unprivileged ``agent-host`` user, which cannot
        overwrite the image-provided, root-owned ``/usr/local`` files — the full
        extract then exits rc=2 and the restore is (correctly) reported failed,
        so pod restore-from-S3 silently never worked. Pods pass
        ``scoped_home=True`` to extract only ``home/agent-host`` (the pod image
        already supplies ``/usr/local``), matching the proven
        ``ide_session`` k8s-pod path. VMs (root, the snapshot IS the disk) keep
        the full extract. See knowledge-base/knowledge/features/workspace_durability_tiering.md §C1.

        Returns:
            True when the snapshot was downloaded AND unpacked cleanly; False
            when the download failed or tar exited non-zero. Callers MUST NOT
            report the workspace as restored on False — this used to return
            None on every path, so a failed restore still stamped
            ``status: "ready"`` and the agent went to work on an empty or
            half-populated tree.
        """
        with tempfile.NamedTemporaryFile(
            suffix=".tar.zst", delete=True, prefix=f"restore_{entity_id[:8]}_"
        ) as tmp:
            tar_path = tmp.name

            ok = await self._snapshot_service.download_snapshot(
                entity_id,
                tar_path,
                entity_type=entity_type,
                require_strict_terminal=require_pipefail,
            )
            if not ok:
                logger.warning(
                    "Failed to download snapshot for %s %s",
                    entity_type.rstrip("s"),
                    entity_id,
                )
                return False

            key_path = resolve_ssh_key_path()
            if not key_path:
                logger.warning(
                    "No SSH key available for snapshot extraction (%s %s)",
                    entity_type.rstrip("s"),
                    entity_id,
                )
            extract_kwargs: dict[str, Any] = {
                "key_path": key_path,
                "require_pipefail": require_pipefail,
            }
            if expected_host_key_fingerprint is not None:
                extract_kwargs.update(
                    {
                        "expected_host_key_fingerprint": (
                            expected_host_key_fingerprint
                        ),
                    }
                )
            if remote_cmd is not None:
                extract_kwargs["remote_cmd"] = remote_cmd
            elif scoped_home is not None:
                extract_kwargs["remote_cmd"] = (
                    EXTRACT_HOME_REMOTE_CMD if scoped_home else EXTRACT_REMOTE_CMD
                )
            rc, stderr = await stream_extract_snapshot(
                ssh_host,
                ssh_port,
                tar_path,
                **extract_kwargs,
            )

            if rc != 0:
                logger.warning(
                    "Snapshot extraction had errors for %s %s (rc=%d): %s",
                    entity_type.rstrip("s"),
                    entity_id,
                    rc,
                    stderr.decode(errors="replace")[:500],
                )
                return False

            return True

    async def restore(
        self,
        owner: WorkspaceOwner,
        *,
        expected_runtime_incarnation: str | None = None,
        stateless_creation_generation: str | None = None,
        allow_stateless_create: bool = False,
    ) -> bool:
        """Owner-keyed restore: job -> restore_workspace, session -> restore_thread_workspace."""
        if owner.kind == "job":
            if (
                expected_runtime_incarnation is not None
                or stateless_creation_generation is not None
                or allow_stateless_create
            ):
                return False
            return await self.restore_workspace(owner.id)
        if (
            expected_runtime_incarnation is None
            and stateless_creation_generation is None
            and not allow_stateless_create
        ):
            return await self.restore_thread_workspace(owner.id)
        if stateless_creation_generation is None and not allow_stateless_create:
            return await self.restore_thread_workspace(
                owner.id,
                expected_runtime_incarnation=expected_runtime_incarnation,
            )
        return await self.restore_thread_workspace(
            owner.id,
            expected_runtime_incarnation=expected_runtime_incarnation,
            stateless_creation_generation=stateless_creation_generation,
            allow_stateless_create=allow_stateless_create,
        )

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

        # Stateless session teardown is owned by the acknowledged retirement
        # protocol.  The legacy idle-suspension path has no claim/incarnation
        # authority and must never race that protocol by snapshotting or
        # deleting its physical workspace (including an already-ended thread
        # whose retirement marker is still pending).
        if thread.get("execution_lane") == "stateless":
            logger.info(
                "Legacy workspace suspension refused for stateless thread %s",
                thread_id,
            )
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
            # (knowledge-base/knowledge/issues/session_silent_failure_audit.md #13).
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
            if not is_vm:
                suspended_ctx[WORKSPACE_SNAPSHOT_RESTORE_REQUIRED_KEY] = True

            if is_vm and self._vm_provisioner and self._vm_provisioner.is_available:
                deleted = await self._vm_provisioner.delete_thread_vm(
                    thread_id, purge_disk=not disk_survives_teardown
                )
                if not deleted:
                    raise RuntimeError(
                        "thread VM suspension process-zero retirement is incomplete"
                    )
                if disk_survives_teardown:
                    # Optimistic; the vm.lifecycle.status handler overwrites it
                    # with what the controller actually did. Restore reads it to
                    # decide whether to extract a snapshot over the disk.
                    suspended_ctx["rootdisk"] = "kept"
                await self._db.merge_thread_vm_context(thread_id, suspended_ctx)
            else:
                owner = WorkspaceOwner.session(thread_id)
                deleted = await self._container_provisioner.delete_workspace(owner)
                if not deleted:
                    raise RuntimeError(
                        "thread workspace suspension process-zero retirement is incomplete"
                    )
                # Reclaim-on-idle (opt-in, fail-safe): drop the hot-cache PVC
                # once the snapshot is confirmed restorable, so idle sessions
                # stop pinning volumes (knowledge-base/knowledge/features/
                # workspace_durability_tiering.md §D3). If the archive can't
                # be verified, KEEP the PVC — deleting it would risk the only
                # copy of the live working tree. A delete failure (return
                # False, or a raised exception from the provisioner) must
                # not fail the suspend either: the session stays resumable
                # off the retained volume either way.
                #
                # This does NOT reclaim the session's separate AGENT-pod PVC
                # (`pvc-agent-s-<id>`, created by AgentProvisioner): the type
                # actually wired into ``self._agent_provisioner`` (see
                # orchestrator/main.py) exposes no PVC-delete method at all.
                # Only the different, unwired ``PersistentProvisioner`` class
                # has ``delete_agent_pvc``, and it manages a differently-named
                # legacy claim (`pvc-persistent-<id>`). Follow-up, not in
                # scope here — the workspace PVC is the primary/larger
                # consumer.
                if _reclaim_on_idle_enabled() and self._snapshot_service:
                    v_ok, reason = await self._snapshot_service.verify_snapshot(
                        thread_id, entity_type="threads"
                    )
                    if v_ok:
                        try:
                            reclaimed = (
                                await self._container_provisioner.delete_workspace_pvc(
                                    owner
                                )
                            )
                        except Exception:
                            logger.exception(
                                "Reclaim-on-idle: PVC delete raised for thread "
                                "%s — keeping session resumable off the "
                                "retained volume",
                                thread_id,
                            )
                            reclaimed = False
                        if reclaimed:
                            suspended_ctx["volume_reclaimed"] = True
                            logger.info(
                                "Reclaim-on-idle: snapshot verified, PVC "
                                "reclaimed for thread %s",
                                thread_id,
                            )
                        else:
                            logger.warning(
                                "Reclaim-on-idle: PVC delete did not succeed "
                                "for thread %s — keeping PVC",
                                thread_id,
                            )
                    else:
                        logger.warning(
                            "Reclaim-on-idle: snapshot unverified (%s) — "
                            "keeping PVC for thread %s",
                            reason,
                            thread_id,
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

    async def restore_thread_workspace(
        self,
        thread_id: str,
        *,
        expected_runtime_incarnation: str | None = None,
        stateless_creation_generation: str | None = None,
        allow_stateless_create: bool = False,
    ) -> bool:
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

        # Same "read it first" rule, container tier: create_workspace rebinds
        # _workspace_binding to whatever the new pod mounts, so the identity of
        # the volume we are coming BACK to only exists before that call.
        prior_backing_id = _workspace_backing_id(metadata)

        # A stateless sandbox resume after terminal retirement is a stricter
        # protocol than legacy idle-suspension restore. The exact-true marker
        # is its durable proof that an S3 extract (or same-PVC reattach) is
        # required before this endpoint may be published. Do not manufacture
        # that proof from truthiness or from entering this method.
        strict_terminal_snapshot = bool(
            thread.get("execution_lane") == "stateless" and not is_vm
        )
        if strict_terminal_snapshot:
            raw_restore_required = ws_ctx.get(WORKSPACE_SNAPSHOT_RESTORE_REQUIRED_KEY)
            if raw_restore_required is not True:
                logger.error(
                    "Strict terminal restore refused without exact snapshot "
                    "intent (thread %s)",
                    thread_id,
                )
                return False
        elif expected_runtime_incarnation is not None:
            # Exact-runtime reuse is a stateless terminal-restore primitive;
            # legacy pinned/VM callers must not silently change semantics.
            return False
        if stateless_creation_generation is not None and not strict_terminal_snapshot:
            return False
        if allow_stateless_create and stateless_creation_generation is None:
            return False

        reuse_existing_runtime = expected_runtime_incarnation is not None
        if (
            strict_terminal_snapshot
            and not reuse_existing_runtime
            and stateless_creation_generation is None
        ):
            logger.error(
                "Strict terminal restore refused without durable create authority "
                "(thread %s)",
                thread_id,
            )
            return False
        if reuse_existing_runtime:
            current_runtime = ws_ctx.get(WORKSPACE_RUNTIME_INCARNATION_KEY)
            if current_runtime != expected_runtime_incarnation:
                logger.error(
                    "Strict restore cached runtime changed for thread %s",
                    thread_id,
                )
                return False
            authority_probe = getattr(
                self._container_provisioner, "workspace_pod_authority", None
            )
            if not callable(authority_probe):
                return False
            try:
                authority = await authority_probe(
                    WorkspaceOwner.session(thread_id),
                    expected_runtime_incarnation=expected_runtime_incarnation,
                )
            except Exception:
                logger.exception(
                    "Strict restore cached runtime probe failed for thread %s",
                    thread_id,
                )
                return False
            if authority != "exact_live":
                logger.error(
                    "Strict restore cached runtime is no longer exact-live for "
                    "thread %s",
                    thread_id,
                )
                return False

        if is_vm:
            await self._db.merge_thread_vm_context(
                thread_id,
                {"status": "restoring"},
            )
        elif not reuse_existing_runtime:
            await self._db.merge_thread_workspace_context(
                thread_id,
                {
                    "status": "restoring",
                    WORKSPACE_SNAPSHOT_RESTORE_REQUIRED_KEY: True,
                },
            )

        try:
            ssh_host = None
            strict_authority: Optional[_StrictSessionRestoreAuthority] = None
            # Container tier only; a VM restore that reaches the extract below
            # never rides a kept disk (rootdisk_kept returned early).
            volume_reattached = False
            reattach_reason = ""

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

                # Kept rootdisk: the restore IS the create. The reattached disk
                # already holds the workspace (extracting a snapshot over it
                # would replace newer files with older ones), and there is no
                # SSH to wait for — VM creation is async over NATS, so
                # ssh coordinates arrive minutes later via the daemon's
                # register, which also supplies the real 'ready'. The
                # container-era tail below demanded an ssh_host synchronously
                # and read its absence as failure, stamping a transient
                # vm.status='failed' that a declared-vm thread's attach poll
                # treats as fatal (live-gate finding, thread a1240add).
                if rootdisk_kept:
                    logger.info(
                        "VM restore for thread %s rides the kept rootdisk — no "
                        "snapshot extract; readiness arrives via the daemon",
                        thread_id,
                    )
                    return True

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
                if not reuse_existing_runtime:
                    create_kwargs: dict[str, Any] = {}
                    if stateless_creation_generation is not None:
                        create_kwargs = {
                            "stateless_creation_generation": (
                                stateless_creation_generation
                            ),
                            "allow_stateless_create": allow_stateless_create,
                        }
                    ok = await self._container_provisioner.create_workspace(
                        WorkspaceOwner.session(thread_id),
                        **create_kwargs,
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
                if (
                    reuse_existing_runtime
                    and ws_ctx.get(WORKSPACE_RUNTIME_INCARNATION_KEY)
                    != expected_runtime_incarnation
                ):
                    logger.error(
                        "Strict restore runtime changed before extraction for "
                        "thread %s",
                        thread_id,
                    )
                    return False
                ssh_host = ws_ctx.get("pod_ip")
                volume_reattached, reattach_reason = _volume_survived_teardown(
                    prior_backing_id, _workspace_backing_id(metadata)
                )
                if strict_terminal_snapshot:
                    try:
                        strict_authority = _strict_session_restore_authority(thread)
                    except RuntimeError as exc:
                        logger.error(
                            "Strict terminal restore has no exact post-create "
                            "authority for thread %s: %s",
                            thread_id,
                            exc,
                        )
                        return False
                    ssh_host = strict_authority.ssh_host

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

            # Extract snapshot into the workspace — unless the storage that came
            # back already holds the live tree. Kept-rootdisk VM restores never
            # reach this point (they returned right after the create above), and
            # a PVC-backed session pod that REATTACHED its volume is that exact
            # situation one tier down: the volume survived the teardown, so
            # unrolling the older S3 tarball over it would replace newer files
            # with older ones. Everything else — emptyDir pods, a freshly minted
            # (empty) PVC, a volume discarded by the single-replica node-loss
            # fallback — has the S3 snapshot as its only copy and must extract.
            ssh_port = (
                strict_authority.ssh_port
                if strict_authority is not None
                else _resolve_ssh_port(ws_ctx, vm_ctx, is_vm=is_vm)
            )
            if volume_reattached:
                logger.info(
                    "Workspace restore for thread %s rides the reattached "
                    "volume (%s) — no snapshot extract",
                    thread_id,
                    reattach_reason,
                )
            else:
                extract_kwargs: dict[str, Any] = {"scoped_home": not is_vm}
                if strict_authority is not None:
                    extract_kwargs = {
                        "expected_host_key_fingerprint": (
                            strict_authority.host_key_fingerprint
                        ),
                        # The workspace image owns /usr/local as root. The
                        # stateless SSH principal restores only its durable
                        # home tree; VM/legacy restores retain the full-root
                        # command below.
                        "remote_cmd": EXTRACT_HOME_REMOTE_CMD,
                        "require_pipefail": True,
                    }
                extracted = await self._extract_snapshot(
                    thread_id,
                    ssh_host,
                    ssh_port=ssh_port,
                    entity_type="threads",
                    **extract_kwargs,
                )
                if not extracted:
                    # Same reasoning as the job path: a workspace that failed
                    # to restore must not advertise itself as ready. A strict
                    # restore leaves the exact-true intent armed; the internal
                    # credential gate consequently keeps this ready-looking
                    # provisioner row quarantined and the next ensure retries.
                    error_msg = "snapshot extraction failed on restore"
                    logger.error("%s (thread %s)", error_msg, thread_id)
                    if strict_terminal_snapshot:
                        return False
                    if is_vm:
                        await self._db.merge_thread_vm_context(
                            thread_id, {"status": "failed", "error": error_msg}
                        )
                    else:
                        await self._db.merge_thread_workspace_context(
                            thread_id, {"status": "failed", "error": error_msg}
                        )
                    return False

            if strict_authority is not None:
                # Extraction success alone is not authority: the deterministic
                # Pod name may have been deleted/replaced while bytes streamed.
                # Re-probe the exact UID, then CAS the same binding/runtime/
                # endpoint tuple under the thread row lock before clearing the
                # restore marker and publishing Ready.
                probe = getattr(self._container_provisioner, "workspace_pod_live", None)
                if not callable(probe):
                    logger.error(
                        "Strict terminal restore cannot verify runtime UID for "
                        "thread %s",
                        thread_id,
                    )
                    return False
                try:
                    exact_live = await probe(
                        WorkspaceOwner.session(thread_id),
                        expected_runtime_incarnation=(
                            strict_authority.runtime_incarnation
                        ),
                    )
                except Exception:
                    logger.exception(
                        "Strict terminal restore runtime re-probe failed for thread %s",
                        thread_id,
                    )
                    return False
                if exact_live is not True:
                    logger.error(
                        "Strict terminal restore runtime changed or is ambiguous "
                        "for thread %s",
                        thread_id,
                    )
                    return False
                if not await self._commit_strict_thread_restore_ready(
                    thread_id,
                    strict_authority,
                ):
                    logger.error(
                        "Strict terminal restore authority changed before Ready "
                        "commit for thread %s",
                        thread_id,
                    )
                    return False

                logger.info(
                    "Strict terminal workspace restored for thread %s from %s "
                    "(runtime=%s)",
                    thread_id,
                    "its own volume" if volume_reattached else "S3",
                    strict_authority.runtime_incarnation,
                )
                return True

            # Legacy job/VM/pinned-session semantics remain below. Strict
            # terminal restores returned only after the transactional CAS.
            restored_ctx = {
                "status": "ready",
                "restored_at": datetime.now(timezone.utc).isoformat(),
            }
            if not is_vm:
                restored_ctx[WORKSPACE_SNAPSHOT_RESTORE_REQUIRED_KEY] = False
            if is_vm:
                await self._db.merge_thread_vm_context(thread_id, restored_ctx)
            else:
                await self._db.merge_thread_workspace_context(thread_id, restored_ctx)

            logger.info(
                "Workspace restored for thread %s from %s (ssh_host=%s)",
                thread_id,
                "its own volume" if volume_reattached else "S3",
                ssh_host,
            )
            return True

        except Exception:
            logger.exception("Failed to restore workspace for thread %s", thread_id)
            if strict_terminal_snapshot:
                # Keep the exact-true marker armed. A racing replacement must
                # not be stamped failed (or Ready) by this stale restore.
                return False
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
