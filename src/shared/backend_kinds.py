"""Workspace backend vocabulary shared by dispatch and runtime construction."""

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
