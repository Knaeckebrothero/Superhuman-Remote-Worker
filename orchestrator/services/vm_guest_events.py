"""Shared persistence for VM guest registration and heartbeat transports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any
from uuid import uuid4

from security.vm_guest import VmGuestIdentity
from services.ssh_helpers import is_tailnet_addr
from services.workspace_binding import CANVAS_WORKSPACE_GENERATION_KEY

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegisterResult:
    status: str | None
    ssh_host: str | None
    ssh_port: int
    ready_source: str | None
    merged: bool


def _object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


async def resolve_vm_entity(db: Any, entity_id: str) -> VmGuestIdentity | None:
    """Resolve thread first, then job, and bind the current VM generation."""

    try:
        thread = await db.get_thread(entity_id)
        if thread:
            generation = _object(_object(thread.get("metadata")).get("vm")).get(
                "provision_generation"
            )
            if not isinstance(generation, str):
                generation = await db.get_vm_provision_generation(
                    entity_id, is_thread=True
                )
            return (
                VmGuestIdentity("thread", entity_id, generation)
                if isinstance(generation, str)
                else None
            )
        job = await db.get_job(entity_id)
        if job:
            generation = _object(_object(job.get("context")).get("vm")).get(
                "provision_generation"
            )
            if not isinstance(generation, str):
                generation = await db.get_vm_provision_generation(
                    entity_id, is_thread=False
                )
            return (
                VmGuestIdentity("job", entity_id, generation)
                if isinstance(generation, str)
                else None
            )
    except Exception:
        logger.exception("Could not resolve VM entity %s", entity_id)
    return None


async def _merge_vm(
    db: Any, identity: VmGuestIdentity, updates: dict[str, Any]
) -> bool:
    method = (
        db.merge_thread_vm_context_if_provision_generation
        if identity.entity_type == "thread"
        else db.merge_vm_context_if_provision_generation
    )
    return bool(
        await method(identity.entity_id, identity.provision_generation, updates)
    )


async def record_register(
    db: Any,
    identity: VmGuestIdentity,
    payload: Mapping[str, Any],
    *,
    authoritative: bool,
    on_ready: Callable[[VmGuestIdentity, str, int], Awaitable[None] | None]
    | None = None,
) -> RegisterResult:
    """Record one guest registration, with readiness limited to external mode."""

    registered_at = datetime.now(timezone.utc).isoformat()
    ssh_host = payload.get("ip") or payload.get("hostname")
    ssh_host = ssh_host if isinstance(ssh_host, str) and ssh_host else None
    ssh_port = 22
    if not authoritative:
        merged = await _merge_vm(
            db,
            identity,
            {
                "hostname": payload.get("hostname"),
                "daemon_pid": payload.get("pid"),
                "reported_ip": payload.get("ip"),
                "registered_at": registered_at,
            },
        )
        return RegisterResult(None, ssh_host, ssh_port, None, merged)

    ssh_ready = payload.get("ssh_ready")
    if not ssh_host:
        vm_status = "ssh_unreachable"
        ready_source = None
        not_ready_reason = "daemon register did not include an SSH host"
    elif ssh_ready is True:
        vm_status = "ready"
        ready_source = "daemon"
        not_ready_reason = None
    elif ssh_ready is False:
        vm_status = "ssh_pending"
        ready_source = None
        not_ready_reason = (
            "daemon reports SSH not ready yet (sshd or tailnet IP pending); "
            "waiting for re-register"
        )
    elif is_tailnet_addr(ssh_host):
        vm_status = "ready"
        ready_source = "legacy_tailnet_ip"
        not_ready_reason = None
    else:
        vm_status = "ssh_pending"
        ready_source = None
        not_ready_reason = (
            f"registered address {ssh_host} is not a tailnet IP; "
            "waiting for tailscale re-register"
        )

    updates = {
        "status": vm_status,
        "ssh_host": ssh_host,
        "ssh_port": ssh_port,
        "hostname": payload.get("hostname"),
        "daemon_pid": payload.get("pid"),
        "recovering": False,
        "registered_at": registered_at,
        "ssh_registration_id": uuid4().hex,
        "ssh_ready_source": ready_source,
        "ssh_verified_at": registered_at if vm_status == "ready" else None,
        "ssh_probe_error": not_ready_reason,
    }
    if identity.entity_type == "thread":
        updates[CANVAS_WORKSPACE_GENERATION_KEY] = None
    merged = await _merge_vm(db, identity, updates)
    if merged and vm_status == "ready" and ssh_host and on_ready:
        result = on_ready(identity, ssh_host, ssh_port)
        if result is not None:
            await result
    return RegisterResult(vm_status, ssh_host, ssh_port, ready_source, merged)


async def record_heartbeat(
    db: Any, identity: VmGuestIdentity, payload: Mapping[str, Any]
) -> bool:
    """Record heartbeat liveness and thread-aware IDE connection activity."""

    now = datetime.now(timezone.utc).isoformat()
    merged = await _merge_vm(db, identity, {"last_heartbeat": now})
    connections = payload.get("code_server_connections")
    if connections is not None:
        updates = {"code_server_connections": connections}
        if connections > 0:
            updates.update({"last_activity": now, "status": "active"})
        else:
            updates["status"] = "idle"
        method = (
            db.merge_thread_ide_session_context
            if identity.entity_type == "thread"
            else db.merge_ide_session_context
        )
        try:
            await method(identity.entity_id, updates)
        except Exception:
            logger.warning(
                "Could not update IDE session heartbeat for %s %s",
                identity.entity_type,
                identity.entity_id,
                exc_info=True,
            )
    return merged
