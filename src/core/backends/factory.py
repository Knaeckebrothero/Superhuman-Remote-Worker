"""Factory for the lite (no-workspace-pod) backends, keyed on ``workspace.backend``.

The two new tiers (``knowledge-base/knowledge/features/no_workspace_agent_mode.md`` §4) are
selected by the existing ``workspace.backend`` config value, exactly like the
legacy ``sandbox``/``vm`` aliases:

- ``virtual`` -> :class:`VirtualWorkspaceBackend` over an object store built
  from the dispatch payload's ``mounts`` (the §4 ``rclone_spec``).
- ``none`` -> :class:`ScratchBackend` (disposable agent-local tmpdir).

The ``sandbox``/``vm`` (``RemoteBackend`` over SSH) construction stays at its
existing bootstrap seams — those carry site-specific SSH/retry behaviour, so
rather than fold them in here, the seams check :data:`LITE_BACKENDS` first and
call :func:`create_lite_backend` only for the new, shell-less tiers. Lite
backends need no SSH, no readiness-wait, and no privileged/workspace pod.
"""

from typing import Any

from ..workspace_backend import WorkspaceBackend

# Backends that run with no workspace container/VM (the agent pod is the only
# pod). Shell-less by construction; the seams branch on this set.
LITE_BACKENDS = frozenset({"virtual", "none"})

# Backends whose workspace is a KubeVirt VM (``threads.metadata.vm``, owned by
# vm_provisioner) rather than a sandbox container
# (``threads.metadata.workspace_container``). ``remote`` is the legacy alias for
# ``vm`` — kept in the set so old stored overrides resolve identically (see the
# inline ``("vm", "remote")`` checks in orchestrator/main.py).
VM_BACKENDS = frozenset({"vm", "remote"})


def is_lite_backend(backend: str) -> bool:
    """True if ``backend`` is a no-workspace-pod tier (``virtual``/``none``)."""
    return backend in LITE_BACKENDS


def is_vm_backend(backend: str) -> bool:
    """True if ``backend`` is VM-tier (``vm``, or its legacy ``remote`` alias).

    Callers use this to keep container-provisioning paths off a VM-tier session:
    its workspace already exists as a VM, and provisioning a sandbox alongside it
    makes the session silently attach to the wrong tier
    (knowledge-base/knowledge/issues/session_vm_backend_never_attaches.md).
    """
    return backend in VM_BACKENDS


def create_lite_backend(workspace_config: Any, *, job_id: str) -> WorkspaceBackend:
    """Construct the lite backend for ``virtual``/``none``.

    Args:
        workspace_config: The resolved workspace config (needs ``.backend`` and,
            for ``virtual``, ``.mounts``).
        job_id: Job/thread id — names the scratch tmpdir and provides the
            default object-store key prefix.

    Raises:
        ValueError: If called for a non-lite backend, or ``virtual`` has no
            mount spec.
    """
    backend = workspace_config.backend

    if backend == "none":
        from .scratch import ScratchBackend

        return ScratchBackend(job_id=job_id)

    if backend == "virtual":
        from .rclone import object_store_from_spec
        from .virtual import VirtualWorkspaceBackend

        mounts = getattr(workspace_config, "mounts", None) or []
        if not mounts:
            raise ValueError(
                "workspace.backend='virtual' requires workspace.mounts (the "
                "object-store spec from dispatch); none was provided"
            )
        mount = mounts[0]
        store = object_store_from_spec(mount)
        prefix = mount.get("prefix") or f"jobs/{job_id}/"
        return VirtualWorkspaceBackend(store, prefix=prefix)

    raise ValueError(
        f"create_lite_backend called with non-lite backend {backend!r}; "
        f"expected one of {sorted(LITE_BACKENDS)}"
    )
