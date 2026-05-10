"""VMInstanceManager — Phase 3 stateful manager for VM workspaces.

Wraps ``VMProvisioner`` (4-backend internal dispatch:
NATS / HTTP / direct KubeVirt / Docker pool), the snapshot service,
and the suspension service. Surfaces VMs to the unified lifecycle
reconciler so drift detection, crash recovery, and snapshot-before-
delete work the same way they do for agents and workspaces.

Unlike pods, VMs aren't enumerated via a K8s label selector — each
backend has its own listing convention, and the orchestrator already
keys VMs by job/thread id via ``jobs.context.vm`` and
``threads.metadata.vm`` JSONB. The manager iterates those rows
instead. The 4-backend dispatch stays inside ``VMProvisioner``; from
the lifecycle reconciler's perspective there is one VM kind.

Phase 3 scope: drift detection + crash recovery via the existing
``vm.status`` field. Deeper health probes (NATS daemon ping, HTTP
controller health endpoint) are a follow-up — they require new
provisioner methods, not new lifecycle scaffolding.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .types import Instance

logger = logging.getLogger(__name__)


_IDLE_JOB_STATUSES = frozenset({"paused", "pending_review", "waiting_for_reply"})
_IDLE_THREAD_STATUSES = frozenset({"ended"})


def expected_vm_shas() -> set[str]:
    """SHAs from the configured default VM image tag.

    Reads ``DEFAULT_VM_IMAGE`` and extracts the suffix from a
    ``...:sha-<hash>`` tag. Per-job ``vm_image`` overrides aren't
    surfaced here — drift is measured against the orchestrator's
    current default. Returns an empty set for ``:latest``-style tags
    so drift checks short-circuit cleanly in local dev.
    """
    shas: set[str] = set()
    tag = os.environ.get("DEFAULT_VM_IMAGE", "")
    if ":sha-" in tag:
        shas.add(tag.rsplit(":sha-", 1)[-1])
    return shas


def _extract_sha(image: str | None) -> str | None:
    if not image or ":sha-" not in image:
        return None
    return image.rsplit(":sha-", 1)[-1]


def _coerce_jsonb(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


class VMInstanceManager:
    """Lifecycle manager for the vm kind."""

    kind = "vm"

    def __init__(
        self,
        vm_provisioner: Any,
        suspension_service: Any,
        snapshot_service: Any,
        db: Any,
    ):
        self._provisioner = vm_provisioner
        self._suspension = suspension_service
        self._snapshot = snapshot_service
        self._db = db

    # -------------------------------------------------------------------------
    # Protocol implementation
    # -------------------------------------------------------------------------

    async def expected_versions(self) -> set[str]:
        return expected_vm_shas()

    async def list_instances(self) -> list[Instance]:
        if not self._provisioner_available():
            return []
        rows = await self._fetch_vm_rows()
        instances: list[Instance] = []
        for row in rows:
            inst = self._row_to_instance(row)
            if inst is not None:
                instances.append(inst)
        return instances

    async def is_healthy(self, inst: Instance) -> bool:
        # VMs report status in their own context. ``failed`` is the
        # crash signal; ``suspended`` means already drained (don't
        # delete again). Anything else (creating/ready/restoring) is
        # treated as healthy.
        status = inst.metadata.get("vm_status")
        if status == "failed":
            return False
        return True

    async def is_idle(self, inst: Instance) -> bool:
        job_status = inst.metadata.get("job_status")
        thread_status = inst.metadata.get("thread_status")
        if job_status:
            return job_status in _IDLE_JOB_STATUSES
        if thread_status:
            return thread_status in _IDLE_THREAD_STATUSES
        return False

    async def snapshot(self, inst: Instance) -> str | None:
        if self._snapshot is None or not getattr(self._snapshot, "is_available", False):
            return None
        ssh_host = inst.metadata.get("ssh_host")
        ssh_port = inst.metadata.get("ssh_port") or 22
        if not ssh_host:
            return None
        bound = inst.bound_to
        if not bound:
            return None
        try:
            ok = await self._snapshot.capture_vm_snapshot(
                job_id=bound,
                ssh_host=ssh_host,
                ssh_port=int(ssh_port),
                source_type="vm",
            )
            return bound if ok else None
        except Exception:
            logger.exception("Snapshot failed for VM %s (bound=%s)", inst.id, bound)
            return None

    async def restore(self, inst: Instance, snapshot_ref: str) -> None:
        # The suspension service dispatches to the right backend based
        # on the persisted ``provisioner`` field — same code path that
        # handles workspace restores. Phase 3 doesn't need its own
        # restore logic.
        if self._suspension is None or not getattr(
            self._suspension, "is_enabled", False
        ):
            return
        if inst.metadata.get("scope") == "thread":
            await self._suspension.restore_thread_workspace(snapshot_ref)
        else:
            await self._suspension.restore_workspace(snapshot_ref)

    async def drain(self, inst: Instance, grace_s: int) -> None:
        await self.delete(inst, grace_s)

    async def delete(self, inst: Instance, grace_s: int) -> None:
        if not self._provisioner_available():
            return
        bound = inst.bound_to
        if not bound:
            logger.debug("delete skipped: no bound id for VM %s", inst.id)
            return
        try:
            if inst.metadata.get("scope") == "thread":
                await self._provisioner.delete_thread_vm(bound)
            else:
                await self._provisioner.delete_vm(bound)
        except Exception:
            logger.exception("Failed to delete VM %s", inst.id)

    # -------------------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------------------

    def _provisioner_available(self) -> bool:
        return bool(getattr(self._provisioner, "is_available", False))

    async def _fetch_vm_rows(self) -> list[dict[str, Any]]:
        """Pull both job-bound and thread-bound VMs.

        VMs aren't a fleet-wide K8s object you can list_namespaced_pod;
        each backend (NATS / HTTP controller / KubeVirt / Docker)
        tracks them differently. The orchestrator's authoritative view
        is the ``vm`` JSONB on jobs and threads, so we read from there.
        """
        if self._db is None:
            return []
        rows: list[dict[str, Any]] = []
        try:
            async with self._db.acquire() as conn:
                job_rows = await conn.fetch(
                    """
                    SELECT id, status, context
                    FROM jobs
                    WHERE context->'vm' IS NOT NULL
                      AND context->'vm' <> '{}'::jsonb
                    """
                )
                thread_rows = await conn.fetch(
                    """
                    SELECT id, status, metadata
                    FROM threads
                    WHERE metadata->'vm' IS NOT NULL
                      AND metadata->'vm' <> '{}'::jsonb
                    """
                )
        except Exception:
            logger.exception("Failed to query VMs for lifecycle")
            return []

        for r in job_rows:
            ctx = _coerce_jsonb(r.get("context"))
            vm_ctx = _coerce_jsonb(ctx.get("vm"))
            if not vm_ctx:
                continue
            rows.append(
                {
                    "scope": "job",
                    "bound_id": str(r["id"]),
                    "owner_status": r.get("status"),
                    "vm_ctx": vm_ctx,
                }
            )
        for r in thread_rows:
            md = _coerce_jsonb(r.get("metadata"))
            vm_ctx = _coerce_jsonb(md.get("vm"))
            if not vm_ctx:
                continue
            rows.append(
                {
                    "scope": "thread",
                    "bound_id": str(r["id"]),
                    "owner_status": r.get("status"),
                    "vm_ctx": vm_ctx,
                }
            )
        return rows

    def _row_to_instance(self, row: dict[str, Any]) -> Instance | None:
        scope = row["scope"]
        bound_id = row["bound_id"]
        vm_ctx = row["vm_ctx"]
        # Identity: prefer a backend-native id when present, otherwise
        # synthesize one from scope+bound_id so the reconciler can
        # log/distinguish even before the backend assigns one.
        vm_native_id = (
            vm_ctx.get("vm_name") or vm_ctx.get("name") or vm_ctx.get("ssh_host")
        )
        inst_id = vm_native_id or f"vm-{scope}-{bound_id[:12]}"
        version = _extract_sha(vm_ctx.get("vm_image"))
        metadata: dict[str, Any] = {
            "scope": scope,
            "vm_status": vm_ctx.get("status"),
            "ssh_host": vm_ctx.get("ssh_host") or vm_ctx.get("host"),
            "ssh_port": vm_ctx.get("ssh_port") or vm_ctx.get("port"),
            "provisioner": vm_ctx.get("provisioner"),
        }
        if scope == "job":
            metadata["job_status"] = row.get("owner_status")
        else:
            metadata["thread_status"] = row.get("owner_status")
        return Instance(
            kind=self.kind,
            id=inst_id,
            version=version,
            bound_to=bound_id,
            metadata=metadata,
        )
