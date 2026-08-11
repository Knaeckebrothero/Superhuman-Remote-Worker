"""Fail-closed workspace classification for stateless session admission.

The declared ``config_override.workspace.backend`` is not sufficient on its
own: live workspace upgrades provision ``metadata.workspace_container`` or
``metadata.vm`` before the agent best-effort persists the new declaration, and
older upgraded rows can retain their original lite backend forever. Admission
must therefore consider both the declaration and physical workspace evidence.

The physical tier is intentionally narrower than ``backend == 'sandbox'``:
only a Kubernetes-provisioned workspace may be reached from the stateless
executor Deployment. Docker workspaces and VMs remain on the pinned plane.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from services.workspace_binding import remote_canvas_presentation_available
from src.core.backends.factory import LITE_BACKENDS

# Gitea setup writes these two keys for every tier, including virtual/none.
# They describe a repository, not a provisioned SSH workspace. Every other
# non-empty workspace_container key is treated as physical/future evidence and
# fails closed until S2 is complete.
_LITE_SAFE_WORKSPACE_CONTEXT_KEYS = frozenset({"git_remote_url", "repo_name"})
_VIRTUAL_BINDING_KEYS = frozenset(
    {"generation", "kind", "backing_id", "ssh_host_key_fingerprint"}
)
_REMOTE_BINDING_KEYS = _VIRTUAL_BINDING_KEYS
_K8S_BACKING_PREFIXES = ("k8s-pod:", "k8s-pvc:")
_K8S_LIFECYCLE_STATUSES = frozenset(
    {
        "pending",
        "created",
        "creating",
        "restoring",
        "suspending",
        "suspended",
        "failed",
        "deleted",
        "ready",
    }
)
_WORKSPACE_RUNTIME_INCARNATION_KEY = "_runtime_incarnation"


def thread_metadata_object(thread: Any) -> dict[str, Any]:
    if not isinstance(thread, dict):
        return {}
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            return {}
    return metadata if isinstance(metadata, dict) else {}


def declared_thread_workspace_backend(thread: Any) -> str | None:
    """Return the materialized workspace backend, or ``None`` if malformed."""
    config_override = thread_metadata_object(thread).get("config_override") or {}
    if isinstance(config_override, str):
        try:
            config_override = json.loads(config_override)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(config_override, dict):
        return None
    workspace = config_override.get("workspace") or {}
    if not isinstance(workspace, dict):
        return None
    backend = workspace.get("backend")
    return backend if isinstance(backend, str) and backend else None


def _valid_remote_binding(binding: Any) -> bool:
    if not isinstance(binding, dict) or set(binding) != _REMOTE_BINDING_KEYS:
        return False
    try:
        UUID(str(binding.get("generation")))
    except (TypeError, ValueError):
        return False
    backing_id = binding.get("backing_id")
    fingerprint = binding.get("ssh_host_key_fingerprint")
    return bool(
        binding.get("kind") == "remote"
        and isinstance(backing_id, str)
        and backing_id.startswith(_K8S_BACKING_PREFIXES)
        and isinstance(fingerprint, str)
        and fingerprint.startswith("SHA256:")
        and len(fingerprint) <= 128
        and not any(char.isspace() for char in fingerprint)
    )


def _sandbox_workspace_refusal(metadata: dict[str, Any]) -> str | None:
    workspace_context = metadata.get("workspace_container")
    if not isinstance(workspace_context, dict) or not workspace_context:
        return "workspace_context_missing"
    if workspace_context.get("provisioner") != "k8s":
        return "workspace_not_k8s"

    status = workspace_context.get("status")
    if status not in _K8S_LIFECYCLE_STATUSES:
        return "workspace_status_unavailable"

    binding = metadata.get("_workspace_binding")
    if binding not in (None, {}) and not _valid_remote_binding(binding):
        return "remote_workspace_binding_malformed"

    runtime_incarnation = workspace_context.get(_WORKSPACE_RUNTIME_INCARNATION_KEY)
    if runtime_incarnation is not None:
        try:
            UUID(str(runtime_incarnation))
        except (TypeError, ValueError):
            return "workspace_runtime_incarnation_malformed"

    if status != "ready":
        # Provisioning/restoration is allowed to converge after a queue claim.
        # The attach path polls until ``ready`` and then enforces the complete
        # backing + runtime attestation before constructing RemoteBackend.
        return None

    if not _valid_remote_binding(binding):
        return "remote_workspace_binding_missing"
    if not remote_canvas_presentation_available(metadata, workspace_context):
        return "workspace_generation_mismatch"
    if runtime_incarnation is None:
        return "workspace_runtime_incarnation_missing"

    endpoint = workspace_context.get("pod_ip") or workspace_context.get("host")
    if not isinstance(endpoint, str) or not endpoint.strip() or "\x00" in endpoint:
        return "workspace_endpoint_missing"
    try:
        port = int(workspace_context.get("pod_port") or workspace_context.get("port"))
    except (TypeError, ValueError):
        return "workspace_endpoint_missing"
    if not 1 <= port <= 65535:
        return "workspace_endpoint_missing"
    pod_name = workspace_context.get("pod_name")
    namespace = workspace_context.get("namespace")
    if (
        not isinstance(pod_name, str)
        or not pod_name.strip()
        or not isinstance(namespace, str)
        or not namespace.strip()
    ):
        return "workspace_endpoint_missing"
    return None


def stateless_workspace_check(thread: Any) -> tuple[str | None, str | None]:
    """Return ``(backend, refusal_reason)`` for stateless session admission.

    ``refusal_reason is None`` means the thread has either an exact lite
    declaration with no physical evidence, or an exact sandbox declaration
    backed by the Kubernetes workspace lifecycle. Unknown/future tiers and VM
    evidence deliberately fail closed.
    """
    metadata = thread_metadata_object(thread)
    backend = declared_thread_workspace_backend(thread)

    if "vm" in metadata and metadata["vm"] not in (None, {}):
        vm_context = metadata["vm"]
        if not isinstance(vm_context, dict):
            return backend, "vm_context_malformed"
        # Includes provisioning/ready/suspended/failed/aborted evidence. A
        # failed historical upgrade may no longer have a live VM, but refusing
        # it is safer than inferring teardown from a best-effort status field.
        return backend, "vm_context_present"

    if backend == "sandbox":
        return backend, _sandbox_workspace_refusal(metadata)
    if backend not in LITE_BACKENDS:
        return backend, "declared_backend_unsupported"

    if "workspace_container" in metadata and metadata["workspace_container"] not in (
        None,
        {},
    ):
        workspace_context = metadata["workspace_container"]
        if not isinstance(workspace_context, dict):
            return backend, "workspace_context_malformed"
        if set(workspace_context) - _LITE_SAFE_WORKSPACE_CONTEXT_KEYS:
            return backend, "workspace_context_present"

    if "_workspace_binding" in metadata and metadata["_workspace_binding"] not in (
        None,
        {},
    ):
        binding = metadata["_workspace_binding"]
        # A virtual binding is the durable object-store namespace and is
        # expected for the virtual tier. A remote or unknown binding proves
        # that declared backend alone is stale/ambiguous.
        if not isinstance(binding, dict):
            return backend, "non_virtual_workspace_binding"
        if set(binding) != _VIRTUAL_BINDING_KEYS:
            return backend, "virtual_workspace_binding_malformed"
        try:
            UUID(str(binding.get("generation")))
        except (TypeError, ValueError):
            return backend, "virtual_workspace_binding_malformed"
        if (
            binding.get("kind") != "virtual"
            or not str(binding.get("backing_id") or "").startswith("rclone:")
            or binding.get("ssh_host_key_fingerprint") is not None
        ):
            return backend, "non_virtual_workspace_binding"

    return backend, None


# Compatibility alias for the S1/S2 callers while they move to the accurately
# named helper. Keeping it also prevents a partially rolled deployment from
# failing imports while orchestrator replicas are replaced.
stateless_lite_workspace_check = stateless_workspace_check


__all__ = [
    "declared_thread_workspace_backend",
    "stateless_workspace_check",
    "stateless_lite_workspace_check",
    "thread_metadata_object",
]
