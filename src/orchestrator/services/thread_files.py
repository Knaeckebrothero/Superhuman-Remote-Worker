"""Exact-runtime authority capture for pinned thread file operations.

Extracted verbatim from ``orchestrator.main`` (R1.B04 lane W). These helpers
are what stop an upload or delete from landing in *a* workspace instead of
*this* workspace. Two properties are load-bearing and move unchanged:

* **The snapshot is captured once and re-proved, not re-derived.** The K8s
  helper freezes an exact tuple (agent, generations, backing id, host-key
  fingerprint, pod IP, resolved SSH target) before any bytes move, and its
  returned probe re-reads the row *before* and *after* the provisioner
  attestation — a replacement that commits mid-operation is caught by the
  second read.
* **Unknown is not the same as replaced.** Every exception path that cannot
  answer the question returns ``"unknown"`` rather than ``"exact_live"``; only
  a positive mismatch reports ``"replacement"``.

The VM helper claims a durable remote-operation lease and settles it as
``replaced`` the moment the endpoint it was handed disagrees with the lease
identity, so a stale endpoint never survives as a live claim.

Collaborators (``store``, provisioners, the backend reader) arrive as explicit
arguments; this module never reaches for application globals.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from orchestrator.services.stateless_workspace_gate import thread_metadata_object
from orchestrator.services.workspace_lifecycle import WorkspaceOwner


def _required_thread_upload_uuid(value: Any, *, label: str) -> str:
    from orchestrator.services.thread_uploads import ThreadUploadError

    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ThreadUploadError(
            status_code=409,
            detail=f"Pinned workspace {label} is unavailable",
        ) from exc
    if parsed.int == 0:
        raise ThreadUploadError(
            status_code=409,
            detail=f"Pinned workspace {label} is unavailable",
        )
    return str(parsed)


async def _prepare_pinned_k8s_thread_upload(
    thread_id: str,
    thread: dict[str, Any],
    destination: Any,
    *,
    store: Any,
    container_provisioner: Any,
) -> tuple[str, str, str, Callable[[], Awaitable[str]]]:
    """Capture and repeatedly prove one pinned Kubernetes workspace target."""

    from orchestrator.services.thread_uploads import (
        ThreadUploadError,
        _SshTarget,
        is_kubernetes_thread_upload_destination,
        resolve_thread_upload_destination,
    )

    live_statuses = {"created", "active", "idle", "awaiting_user"}
    live_agent_statuses = {"ready", "working", "session"}

    def _thread_snapshot(row: dict[str, Any] | None) -> tuple[Any, ...] | None:
        if (
            not isinstance(row, dict)
            or str(row.get("id") or "") != thread_id
            or row.get("execution_lane") != "pinned"
            or row.get("status") not in live_statuses
            or row.get("runtime_retirement_token") is not None
            or not is_kubernetes_thread_upload_destination(row)
        ):
            return None
        metadata = thread_metadata_object(row)
        workspace = metadata.get("workspace_container")
        binding = metadata.get("_workspace_binding")
        if not isinstance(workspace, dict) or not isinstance(binding, dict):
            return None
        backing_id = binding.get("backing_id")
        fingerprint = binding.get("ssh_host_key_fingerprint")
        if not (
            workspace.get("status") == "ready"
            and workspace.get("provisioner") in {"k8s", "kubernetes"}
            and binding.get("kind") == "remote"
            and isinstance(backing_id, str)
            and backing_id.startswith(("k8s-pod:", "k8s-pvc:"))
            and isinstance(fingerprint, str)
            and fingerprint.startswith("SHA256:")
            and not any(char.isspace() for char in fingerprint)
            and (
                "_snapshot_restore_required" not in workspace
                or workspace.get("_snapshot_restore_required") is False
            )
        ):
            return None
        try:
            runtime_generation = _required_thread_upload_uuid(
                row.get("runtime_generation"), label="session generation"
            )
            generation = _required_thread_upload_uuid(
                binding.get("generation"), label="generation"
            )
            endpoint_generation = _required_thread_upload_uuid(
                workspace.get("_canvas_workspace_generation"),
                label="endpoint generation",
            )
            runtime_incarnation = _required_thread_upload_uuid(
                workspace.get("_runtime_incarnation"),
                label="runtime incarnation",
            )
        except ThreadUploadError:
            return None
        if generation != endpoint_generation:
            return None
        try:
            resolved = resolve_thread_upload_destination(row)
        except ThreadUploadError:
            return None
        if not isinstance(resolved, _SshTarget):
            return None
        return (
            str(row.get("agent_id") or ""),
            runtime_generation,
            generation,
            runtime_incarnation,
            backing_id,
            fingerprint,
            str(workspace.get("pod_ip") or ""),
            resolved,
        )

    expected_thread = _thread_snapshot(thread)
    if expected_thread is None or expected_thread[-1] != destination:
        raise ThreadUploadError(
            status_code=409,
            detail="Pinned Kubernetes workspace authority is unavailable",
        )
    (
        expected_agent_id,
        session_generation,
        generation,
        runtime_incarnation,
        backing_id,
        fingerprint,
        pod_ip,
        _expected_destination,
    ) = expected_thread
    if not expected_agent_id or not pod_ip:
        raise ThreadUploadError(
            status_code=409,
            detail="Pinned session binding is unavailable",
        )

    async def _read_session_binding() -> Any | None:
        try:
            binding = await store.get_pinned_session_binding(
                thread_id,
                expected_runtime_generation=session_generation,
            )
        except Exception:
            return None
        if not (
            binding is not None
            and binding.agent_id == expected_agent_id
            and binding.agent_status in live_agent_statuses
        ):
            return None
        return binding

    expected_binding = await _read_session_binding()
    if expected_binding is None:
        raise ThreadUploadError(
            status_code=409,
            detail="Pinned session agent authority is unavailable",
        )
    expected_binding_target = expected_binding.target_key

    async def _probe_runtime() -> str:
        try:
            before = await store.get_thread(thread_id)
        except Exception:
            return "unknown"
        if _thread_snapshot(before) != expected_thread:
            return "replacement"
        current_binding = await _read_session_binding()
        if (
            current_binding is None
            or current_binding.target_key != expected_binding_target
        ):
            return "replacement"
        try:
            attestation = await container_provisioner.attest_workspace_runtime(
                WorkspaceOwner.session(thread_id)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return "unknown"
        if not (
            attestation.runtime_incarnation == runtime_incarnation
            and attestation.workspace_generation == generation
            and attestation.backing_id == backing_id
            and attestation.ssh_host_key_fingerprint == fingerprint
            and attestation.pod_ip == pod_ip
            and attestation.host == destination.host
            and int(attestation.port) == int(destination.port)
        ):
            return "replacement"
        try:
            after = await store.get_thread(thread_id)
        except Exception:
            return "unknown"
        if _thread_snapshot(after) != expected_thread:
            return "replacement"
        current_binding = await _read_session_binding()
        if (
            current_binding is None
            or current_binding.target_key != expected_binding_target
        ):
            return "replacement"
        return "exact_live"

    return generation, runtime_incarnation, fingerprint, _probe_runtime


async def _prepare_pinned_vm_thread_operation(
    thread_id: str,
    thread: dict[str, Any],
    destination: Any,
    *,
    operation_kind: str,
    store: Any,
    vm_provisioner: Any,
    thread_workspace_backend: Callable[[Any], Any],
) -> Any:
    from orchestrator.services.thread_uploads import ThreadUploadError, _SshTarget
    from orchestrator.services.vm_remote_operation import (
        VMRemoteOperationUnavailable,
        claim_vm_remote_operation,
    )

    if (
        thread.get("execution_lane") != "pinned"
        or thread_workspace_backend(thread) != "vm"
        or not isinstance(destination, _SshTarget)
    ):
        raise ThreadUploadError(409, "Pinned VM workspace authority is unavailable")
    try:
        lease = await claim_vm_remote_operation(
            db=store,
            provisioner=vm_provisioner,
            owner_id=thread_id,
            owner_kind="thread",
            operation_kind=operation_kind,
        )
    except VMRemoteOperationUnavailable as exc:
        raise ThreadUploadError(
            503, "VM workspace runtime authority could not be verified"
        ) from exc
    if (destination.host, int(destination.port)) != (
        lease.identity.ssh_host,
        lease.identity.ssh_port,
    ):
        await store.settle_vm_remote_operation(
            str(lease.receipt["id"]),
            claim_token=int(lease.receipt["claim_token"]),
            claimant=lease.claimant,
            result_kind="replaced",
        )
        raise ThreadUploadError(409, "VM endpoint changed before operation")
    return lease
