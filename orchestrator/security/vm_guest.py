"""Per-VM bearer authentication for guest-to-orchestrator HTTP routes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hmac
import json
import os
import re
from typing import Any

from fastapi import HTTPException, Request, status

from security.access import log_security_event
from services.vm_lifecycle_auth import (
    LifecycleAuthConfigurationError,
    configured_secret,
    guest_token,
)

_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_INACTIVE_VM_STATUSES = frozenset({"deleted", "failed"})


@dataclass(frozen=True, slots=True)
class VmGuestIdentity:
    entity_type: str
    entity_id: str
    provision_generation: str


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


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not _TOKEN_RE.fullmatch(token):
        return None
    return token


def _configured_guest_secrets() -> tuple[bytes, ...]:
    primary = configured_secret()
    if primary is None:
        return ()
    previous_value = os.getenv("VM_LIFECYCLE_HMAC_SECRET_PREVIOUS", "")
    previous = (
        configured_secret({"VM_LIFECYCLE_HMAC_SECRET": previous_value})
        if previous_value
        else None
    )
    return (primary,) if previous is None else (primary, previous)


async def require_vm_guest(
    request: Request, db: Any, entity_id: str
) -> VmGuestIdentity:
    """Authenticate a current job/thread VM without revealing entity existence."""

    supplied = _bearer_token(request)
    if supplied is None:
        raise _unauthorized()

    thread = await db.get_thread(entity_id)
    if thread:
        entity_type = "thread"
        vm = _object(_object(thread.get("metadata")).get("vm"))
    else:
        job = await db.get_job(entity_id)
        if not job:
            raise _unauthorized()
        entity_type = "job"
        vm = _object(_object(job.get("context")).get("vm"))

    generation = vm.get("provision_generation")
    if (
        not isinstance(generation, str)
        or not generation
        or vm.get("status") in _INACTIVE_VM_STATUSES
    ):
        raise _unauthorized()

    try:
        secrets = _configured_guest_secrets()
    except LifecycleAuthConfigurationError:
        raise _unauthorized() from None
    if any(
        hmac.compare_digest(
            supplied,
            guest_token(secret, entity_type, entity_id, generation),
        )
        for secret in secrets
    ):
        return VmGuestIdentity(entity_type, entity_id, generation)

    await log_security_event(
        db,
        resource_type="vm_guest",
        event_type="vm_guest_auth_mismatch",
        resource_id=entity_id,
        detail="VM guest bearer MAC mismatch",
        request=request,
    )
    raise _unauthorized()
