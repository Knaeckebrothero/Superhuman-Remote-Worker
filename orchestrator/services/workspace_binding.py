"""Workspace-generation binding helpers shared by lifecycle entry points."""

from __future__ import annotations

import hashlib
import json
import shutil
from typing import Any
from uuid import UUID

from services.virtual_workspace import virtual_workspace_rclone_spec

CANVAS_WORKSPACE_GENERATION_KEY = "_canvas_workspace_generation"


def remote_canvas_presentation_available(
    metadata: dict[str, Any], workspace_context: dict[str, Any]
) -> bool:
    """Return whether an agent may advertise Canvas for this SSH workspace.

    A usable SSH endpoint alone is deliberately insufficient. The provisioner
    must have atomically paired that exact endpoint generation with a trusted
    remote backing and a pinned host-key fingerprint. This small capability is
    what crosses the internal attach API; private binding material never does.
    """

    if not isinstance(metadata, dict) or not isinstance(workspace_context, dict):
        return False
    if workspace_context.get("status") != "ready":
        return False

    binding = metadata.get("_workspace_binding")
    if not isinstance(binding, dict) or binding.get("kind") != "remote":
        return False
    if not isinstance(binding.get("backing_id"), str) or not binding["backing_id"]:
        return False

    fingerprint = binding.get("ssh_host_key_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or not fingerprint.startswith("SHA256:")
        or len(fingerprint) > 128
        or any(char.isspace() for char in fingerprint)
    ):
        return False

    try:
        binding_generation = UUID(str(binding.get("generation")))
        endpoint_generation = UUID(
            str(workspace_context.get(CANVAS_WORKSPACE_GENERATION_KEY))
        )
    except (TypeError, ValueError):
        return False
    return endpoint_generation == binding_generation


def virtual_thread_backing_id(thread_id: str, spec: dict[str, Any]) -> str:
    """Canonical non-secret identity of one durable thread namespace."""

    nonsecret_identity = {
        "type": spec.get("type"),
        "root": spec.get("root"),
        "endpoint": (spec.get("config") or {}).get("endpoint"),
        "prefix": f"threads/{thread_id}/",
    }
    digest = hashlib.sha256(
        json.dumps(nonsecret_identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"rclone:{digest}"


class VirtualBackingUnavailable(Exception):
    """A thread's virtual object-store backing cannot be used right now.

    Carries a machine-readable ``reason`` rather than an HTTP status because
    callers disagree about how to report the same condition: Canvas answers
    with its own ``CanvasFileError`` codes, uploads with ``ThreadUploadError``.
    """

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def resolve_virtual_thread_backing(
    thread: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Resolve a ``virtual`` thread's current durable object-store backing.

    Read-only: this deliberately never binds or rebinds. A thread with no
    binding is a create-time misconfiguration, and silently minting one here
    would let an ordinary file write mutate workspace identity.

    Returns:
        ``(rclone_spec, key_prefix)``. ``key_prefix`` is ``threads/<id>/`` — the
        same prefix the agent's lite backend is attached with at dispatch
        (``_inject_lite_workspace_config``), so a write of ``uploads/x`` here is
        exactly what the agent's ``read_file("uploads/x")`` resolves.

    Raises:
        VirtualBackingUnavailable: ``not_configured`` (no durable object store
            on this deployment), ``backing_changed`` (thread unbound or bound to
            a different backing), or ``transport_missing`` (no ``rclone``).
    """

    thread_id = str(thread.get("id") or "")
    spec = virtual_workspace_rclone_spec()
    if not spec or spec.get("type") == "memory":
        # "memory" is process-local, so another orchestrator replica could not
        # read what this one wrote — never a real backing.
        raise VirtualBackingUnavailable(
            "not_configured",
            "Virtual workspace storage is not configured on this deployment",
        )

    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    binding = metadata.get("_workspace_binding")
    expected_backing_id = virtual_thread_backing_id(thread_id, spec)
    if (
        not isinstance(binding, dict)
        or binding.get("kind") != "virtual"
        or binding.get("backing_id") != expected_backing_id
    ):
        raise VirtualBackingUnavailable(
            "backing_changed",
            "Virtual workspace backing changed and must be rebound",
        )

    if shutil.which("rclone") is None:
        raise VirtualBackingUnavailable(
            "transport_missing",
            "Virtual workspace transport is unavailable",
        )

    return spec, f"threads/{thread_id}/"


async def ensure_virtual_thread_workspace_binding(
    db: Any, thread_id: str
) -> dict[str, Any] | None:
    """Bind the deterministic durable namespace; memory remains unavailable."""

    spec = virtual_workspace_rclone_spec()
    if not spec or spec.get("type") == "memory":
        return None
    return await db.bind_thread_workspace_backing(
        thread_id,
        backing_kind="virtual",
        backing_id=virtual_thread_backing_id(thread_id, spec),
        ssh_host_key_fingerprint=None,
    )


__all__ = [
    "CANVAS_WORKSPACE_GENERATION_KEY",
    "VirtualBackingUnavailable",
    "ensure_virtual_thread_workspace_binding",
    "remote_canvas_presentation_available",
    "resolve_virtual_thread_backing",
    "virtual_thread_backing_id",
]
