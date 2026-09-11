"""Provision VMs from captured execution settings, independently of Experts."""

from copy import deepcopy

from fastapi import HTTPException

from orchestrator.services.manifest_execution_snapshot import (
    object_value,
    read_execution,
    srw_snapshot_config,
)


async def vm_provisioning_options(store, work_kind: str, work: dict, *, fallback=None):
    """Use the immutable policy for current work; keep historical inputs working.

    The typed harness configuration deliberately excludes VM allocation fields.
    The captured policy retains execution-owned infrastructure after Expert
    configuration has been stripped of allocation authority. The adapter marker
    comes from the database's execution join, never a caller's context.
    """
    configuration = fallback
    if work.get("execution_harness_adapter") is not None:
        snapshot = await read_execution(store, work_kind, str(work["id"]))
        if snapshot is None:
            raise HTTPException(409, "VM execution configuration is unavailable.")
        _, configuration = srw_snapshot_config(snapshot)
    workspace = object_value(object_value(configuration).get("workspace"))
    vm = object_value(workspace.get("vm"))
    return {
        target: deepcopy(vm[source])
        for source, target in (
            ("image", "vm_image"),
            ("cpu_cores", "cpu_cores"),
            ("memory", "memory"),
            ("disk_size", "disk_size"),
        )
        if vm.get(source) is not None
    }
