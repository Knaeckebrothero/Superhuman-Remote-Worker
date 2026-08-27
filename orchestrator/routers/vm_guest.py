"""Authenticated VM guest register, heartbeat, and sudo HTTP bridge."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, UUID4

from security.vm_guest import require_vm_guest
from services.sudo_gate import SudoEntityUnavailable, SudoRequestConflict
from services.vm_guest_events import record_heartbeat, record_register

router = APIRouter(prefix="/api/internal/vm", include_in_schema=False)


def _get_db() -> Any:
    from main import postgres_db  # type: ignore

    return postgres_db


def _get_sudo_gate() -> Any:
    from services.sudo_gate import sudo_gate

    return sudo_gate


class RegisterBody(BaseModel):
    hostname: str = Field(min_length=1, max_length=255)
    ip: str = Field(min_length=1, max_length=255)
    pid: int = Field(ge=1)


class HeartbeatBody(BaseModel):
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    code_server_connections: int = Field(ge=0)


class SudoCreateBody(BaseModel):
    request_id: UUID4
    command: str = Field(min_length=1, max_length=4096)
    argv: list[str] = Field(max_length=256)
    runas_user: str = Field(min_length=1, max_length=255)
    user: str = Field(min_length=1, max_length=255)
    host: str = Field(max_length=255)
    tty: str = Field(max_length=255)
    cwd: str = Field(max_length=4096)
    pid: int = Field(ge=1)


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class _GuestRateLimits:
    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str, str], _Bucket] = {}
        self.waiters = 0
        self.waiter_lock = asyncio.Lock()

    def allow(self, action: str, entity_type: str, entity_id: str, limit: int) -> bool:
        now = time.monotonic()
        key = (action, entity_type, entity_id)
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _Bucket(float(limit - 1), now)
            return True
        bucket.tokens = min(
            float(limit), bucket.tokens + (now - bucket.updated_at) * limit / 60.0
        )
        bucket.updated_at = now
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True

    def reset(self) -> None:
        self._buckets.clear()
        self.waiters = 0


_rate_limits = _GuestRateLimits()


def _limited(retry_after: int = 10) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Rate limit exceeded",
        headers={"Retry-After": str(retry_after)},
    )


def _expires_at(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value is not None else ""


@router.post("/{entity_id}/register")
async def register_guest(
    entity_id: str, request: Request, body: RegisterBody
) -> dict[str, bool]:
    db = _get_db()
    identity = await require_vm_guest(request, db, entity_id)
    await record_register(
        db, identity, body.model_dump(mode="json"), authoritative=False
    )
    return {"ok": True}


@router.post("/{entity_id}/heartbeat")
async def heartbeat_guest(
    entity_id: str, request: Request, body: HeartbeatBody
) -> dict[str, bool]:
    db = _get_db()
    identity = await require_vm_guest(request, db, entity_id)
    if not _rate_limits.allow("heartbeat", identity.entity_type, entity_id, 12):
        raise _limited(5)
    await record_heartbeat(db, identity, body.model_dump(mode="json"))
    return {"ok": True}


@router.post("/{entity_id}/sudo")
async def create_sudo_request(
    entity_id: str,
    request: Request,
    response: Response,
    body: SudoCreateBody,
) -> dict[str, Any]:
    db = _get_db()
    identity = await require_vm_guest(request, db, entity_id)
    if not _rate_limits.allow("sudo_create", identity.entity_type, entity_id, 6):
        raise _limited()
    gate = _get_sudo_gate()
    request_id = str(body.request_id)
    try:
        existing = await gate.http_request_exists(identity, request_id)
    except SudoRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not existing and await gate.count_pending_for_entity(identity) >= 2:
        raise _limited(5)
    try:
        result = await gate.open_request(identity, body)
    except SudoEntityUnavailable as exc:
        raise HTTPException(status_code=401, detail="Unauthorized") from exc
    except SudoRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response.status_code = 201 if result.created else 200
    return {
        "request_id": result.request_id,
        "status": result.status,
        "reason": result.reason,
        "expires_at": _expires_at(result.expires_at),
    }


@router.get("/{entity_id}/sudo/{request_id}")
async def wait_for_sudo_decision(
    entity_id: str,
    request_id: UUID,
    request: Request,
    wait: Annotated[float, Query(ge=0)] = 0,
) -> dict[str, Any]:
    db = _get_db()
    identity = await require_vm_guest(request, db, entity_id)
    if not _rate_limits.allow("sudo_wait", identity.entity_type, entity_id, 30):
        raise _limited(2)
    async with _rate_limits.waiter_lock:
        if _rate_limits.waiters >= 200:
            raise HTTPException(
                status_code=503,
                detail="Sudo wait capacity exhausted",
                headers={"Retry-After": "5"},
            )
        _rate_limits.waiters += 1
    try:
        result = await _get_sudo_gate().wait_for_decision(
            str(request_id),
            min(wait, 30),
            entity_type=identity.entity_type,
            entity_id=identity.entity_id,
            provision_generation=identity.provision_generation,
        )
    finally:
        async with _rate_limits.waiter_lock:
            _rate_limits.waiters -= 1
    if result is None:
        raise HTTPException(status_code=404, detail="Sudo request not found")
    return result
