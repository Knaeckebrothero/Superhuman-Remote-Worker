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
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from services.workspace_binding import remote_canvas_presentation_available
from src.shared.backend_kinds import LITE_BACKENDS

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
_WORKSPACE_RUNTIME_CREATION_KEY = "_runtime_creation"
_WORKSPACE_RUNTIME_CREATION_FIELDS = frozenset(
    {"generation", "mode", "attempted", "replaces_uid"}
)


def thread_metadata_object(thread: Any) -> dict[str, Any]:
    if isinstance(thread, Mapping):
        metadata = thread.get("metadata") or {}
    else:
        # ``asyncpg.Record`` deliberately exposes the Mapping API without
        # registering as ``collections.abc.Mapping``.  Use its real protocol
        # rather than converting the whole row or rejecting authoritative DB
        # snapshots at Canvas/attach boundaries.
        getter = getattr(thread, "get", None)
        if not callable(getter):
            return {}
        try:
            metadata = getter("metadata") or {}
        except (KeyError, TypeError, ValueError):
            return {}
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


def stateless_session_class_refusal(thread: Any) -> str | None:
    """Refuse materialized session classes that still require pinned owners.

    Officer and conference sessions have background wake/hold protocols tied
    to their pinned process.  External workspace writers must use the same
    class authority as turn admission; otherwise a legacy/direct-DB stateless
    row could bypass the turn gate through Browser or Canvas SSH.
    """

    metadata = thread_metadata_object(thread)
    config_override = metadata.get("config_override")
    if config_override is None:
        return None
    if not isinstance(config_override, dict):
        return "session_config_malformed"
    officer = config_override.get("officer")
    if officer is None:
        return None
    if not isinstance(officer, dict):
        return "session_class_malformed"
    for field, reason in (
        ("conference", "conference_requires_pinned"),
        ("enabled", "officer_requires_pinned"),
    ):
        if field not in officer or officer[field] is False:
            continue
        if officer[field] is True:
            return reason
        return "session_class_malformed"
    return None


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


def _canonical_uuid_string(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, str):
        return False
    try:
        return value == str(UUID(value))
    except (TypeError, ValueError):
        return False


def _runtime_creation_marker_valid(
    marker: Any,
    *,
    restore_required: bool,
) -> bool:
    if (
        not isinstance(marker, dict)
        or set(marker) != _WORKSPACE_RUNTIME_CREATION_FIELDS
    ):
        return False
    if not _canonical_uuid_string(marker.get("generation")):
        return False
    if marker.get("mode") not in {"create", "restore"}:
        return False
    if type(marker.get("attempted")) is not bool:
        return False
    replaces_uid = marker.get("replaces_uid")
    if replaces_uid is not None and not _canonical_uuid_string(replaces_uid):
        return False
    return (marker["mode"] == "restore") is restore_required


def _sandbox_workspace_refusal(metadata: dict[str, Any]) -> str | None:
    workspace_context = metadata.get("workspace_container")
    if not isinstance(workspace_context, dict) or not workspace_context:
        return "workspace_context_missing"
    if workspace_context.get("provisioner") != "k8s":
        return "workspace_not_k8s"

    status = workspace_context.get("status")
    if status not in _K8S_LIFECYCLE_STATUSES:
        return "workspace_status_unavailable"

    # Restore intent is an orchestrator-owned exact boolean. ``True`` is a
    # valid in-progress authority (credential delivery suppresses it), while
    # absent/``False`` means no restore is pending. Every other present JSON
    # value is corruption and must fail closed.
    if "_snapshot_restore_required" in workspace_context:
        restore_required = workspace_context["_snapshot_restore_required"]
        if restore_required is not True and restore_required is not False:
            return "workspace_restore_marker_malformed"
    else:
        restore_required = False

    if _WORKSPACE_RUNTIME_CREATION_KEY in workspace_context:
        if not _runtime_creation_marker_valid(
            workspace_context[_WORKSPACE_RUNTIME_CREATION_KEY],
            restore_required=restore_required is True,
        ):
            return "workspace_creation_marker_malformed"
        if status == "ready":
            # Ready is credential-bearing. The marker is removed only by the
            # exact UID+generation binding/endpoint CAS, so its presence proves
            # publication is incomplete even if stale Ready fields remain.
            return "workspace_creation_in_progress"

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
    evidence deliberately fail closed. Protected-cloud sessions remain on the
    pinned execution plane: their turn-end overlay staging is not yet bound to
    an exact stateless lease/runtime acknowledgement, so admitting one here
    could release a claimant while mutable workspace bytes are still staging.
    """
    metadata = thread_metadata_object(thread)
    backend = declared_thread_workspace_backend(thread)

    # ``protected_cloud`` is an orchestrator-owned boolean marker. Accept only
    # its exact disabled/absent forms; malformed legacy/operator values fail
    # closed with the enabled form. This classifier is also called from the
    # locked input/control boundaries and the final credential-attach path, so
    # one decision protects already-materialized rows as well as new admission.
    if "protected_cloud" in metadata and metadata["protected_cloud"] is not False:
        return backend, "protected_cloud_requires_pinned"

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


def stateless_session_workspace_check(thread: Any) -> tuple[str | None, str | None]:
    """Apply pinned-only session-class and workspace-tier admission together."""

    backend = declared_thread_workspace_backend(thread)
    class_refusal = stateless_session_class_refusal(thread)
    if class_refusal is not None:
        return backend, class_refusal
    return stateless_workspace_check(thread)


# Compatibility alias for the S1/S2 callers while they move to the accurately
# named helper. Keeping it also prevents a partially rolled deployment from
# failing imports while orchestrator replicas are replaced.
stateless_lite_workspace_check = stateless_workspace_check


__all__ = [
    "declared_thread_workspace_backend",
    "stateless_session_class_refusal",
    "stateless_session_workspace_check",
    "stateless_workspace_check",
    "stateless_lite_workspace_check",
    "thread_metadata_object",
]
