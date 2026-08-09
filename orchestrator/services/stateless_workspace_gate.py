"""Fail-closed workspace classification for stateless session admission.

The declared ``config_override.workspace.backend`` is not sufficient on its
own: live workspace upgrades provision ``metadata.workspace_container`` or
``metadata.vm`` before the agent best-effort persists the new declaration, and
older upgraded rows can retain their original lite backend forever.  Admission
must therefore consider both the declaration and physical workspace evidence.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from src.core.backends.factory import LITE_BACKENDS

# Gitea setup writes these two keys for every tier, including virtual/none.
# They describe a repository, not a provisioned SSH workspace. Every other
# non-empty workspace_container key is treated as physical/future evidence and
# fails closed until S2 is complete.
_LITE_SAFE_WORKSPACE_CONTEXT_KEYS = frozenset({"git_remote_url", "repo_name"})
_VIRTUAL_BINDING_KEYS = frozenset(
    {"generation", "kind", "backing_id", "ssh_host_key_fingerprint"}
)


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


def stateless_lite_workspace_check(thread: Any) -> tuple[str | None, str | None]:
    """Return ``(backend, refusal_reason)`` for the temporary S2 lite gate.

    ``refusal_reason is None`` means the thread has an exact lite declaration
    and no evidence that a sandbox/VM was provisioned or bound. Unknown fields
    deliberately fail closed; this gate is removed only after S2 acceptance.
    """
    metadata = thread_metadata_object(thread)
    backend = declared_thread_workspace_backend(thread)
    if backend not in LITE_BACKENDS:
        return backend, "declared_backend_not_lite"

    if "vm" in metadata and metadata["vm"] not in (None, {}):
        vm_context = metadata["vm"]
        if not isinstance(vm_context, dict):
            return backend, "vm_context_malformed"
        # Includes provisioning/ready/suspended/failed/aborted evidence. A
        # failed historical upgrade may no longer have a live VM, but refusing
        # it is safer than inferring teardown from a best-effort status field.
        return backend, "vm_context_present"

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


__all__ = [
    "declared_thread_workspace_backend",
    "stateless_lite_workspace_check",
    "thread_metadata_object",
]
