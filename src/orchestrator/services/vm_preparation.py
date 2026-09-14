"""Execution-owned VM preparation inputs and installation capability checks."""

import os
import asyncio
import json
import logging
from uuid import UUID

from fastapi import HTTPException

from shared.workspace_preparation import preparation_request
from shared.workspace_preparation_settings import PreparationSettings


def validate_environment(environment, resources):
    try:
        settings = PreparationSettings.from_environment()
        if not settings.enabled or os.getenv("VM_MODE") != "same-cluster":
            raise ValueError(
                "VM workspace preparation requires enabled same-cluster hosting."
            )
        if not os.getenv("VM_LIFECYCLE_HMAC_SECRET"):
            raise ValueError(
                "VM preparation requires authenticated lifecycle transport."
            )
        if os.getenv("VM_PERSISTENT_ROOTDISK", "false").lower() != "true":
            raise ValueError("VM preparation requires persistent rootdisk hosting.")
        from shared.workspace_preparation import image_reference

        host, _, _ = image_reference(environment["image"])
        if host not in settings.registry_hosts:
            raise ValueError("Image registry is not enabled for workspace preparation.")
        # Validate argv and policies without allocating or resolving an image.
        preparation_request(
            environment,
            scope_kind="Account",
            scope_uid=UUID(int=0),
            allocation_id=UUID(int=0),
            owner_kind="job",
        )
        from shared.workspace_preparation_settings import disk_bytes

        requested = resources.get("storage")
        if requested and disk_bytes(requested) < disk_bytes(settings.disk_size):
            raise ValueError(
                "Workspace storage is smaller than the installed preparation disk size."
            )
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return settings


def execution_request(snapshot, environment, *, runtime_generation=None):
    scope = snapshot["resolved"]["metadata"]["scope"]
    return preparation_request(
        environment,
        scope_kind=scope["kind"],
        scope_uid=scope["name"],
        allocation_id=snapshot["work_id"],
        owner_kind="job" if snapshot["work_kind"] == "Job" else "session",
        runtime_generation=runtime_generation,
    )


async def reconcile_cancellations(db, provisioner):
    from shared.workspace_preparation import validate_request

    for row in await db.list_vm_preparation_cancellations():
        try:
            vm = row["vm"]
            if isinstance(vm, str):
                vm = json.loads(vm)
            request = validate_request(vm["preparation_request"])
            if request["allocationId"] != row["entity_id"] or request["ownerKind"] != (
                "job" if row["entity_type"] == "job" else "session"
            ):
                raise ValueError("Preparation cancellation ownership is inconsistent")
            result = await provisioner.preparation_operation(
                "cancel", {"preparation": request}
            )
            if result.get("cancelled") is True:
                await db.acknowledge_vm_preparation_cancelled(
                    row["entity_type"], row["entity_id"], request
                )
        except Exception:
            logging.getLogger(__name__).exception(
                "Preparation cancellation failed for %s %s",
                row["entity_type"],
                row["entity_id"],
            )


async def cancellation_loop(shutdown_event, *, db, provisioner):
    while not shutdown_event.is_set():
        try:
            await reconcile_cancellations(db, provisioner)
        except Exception:
            logging.getLogger(__name__).exception(
                "VM preparation cancellation reconciliation failed"
            )
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
