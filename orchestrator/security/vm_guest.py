"""Per-VM bearer authentication for guest-to-orchestrator HTTP routes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hmac
import json
import os
import re
import time
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
_DUMMY_SECRET = b"\x00" * 32
_DUMMY_GENERATION = "00000000-0000-4000-8000-000000000000"
_PREAUTH_REQUESTS_PER_MINUTE = 30
_SECURITY_LOG_INTERVAL_SECONDS = 60.0


@dataclass(slots=True)
class _TokenBucket:
    tokens: float
    updated_at: float


class _PreAuthRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _TokenBucket] = {}

    def allow(self, client_ip: str, entity_id: str) -> bool:
        now = time.monotonic()
        key = (client_ip, entity_id)
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= 4096:
                oldest = min(
                    self._buckets, key=lambda item: self._buckets[item].updated_at
                )
                self._buckets.pop(oldest, None)
            self._buckets[key] = _TokenBucket(
                float(_PREAUTH_REQUESTS_PER_MINUTE - 1), now
            )
            return True
        bucket.tokens = min(
            float(_PREAUTH_REQUESTS_PER_MINUTE),
            bucket.tokens
            + (now - bucket.updated_at) * _PREAUTH_REQUESTS_PER_MINUTE / 60.0,
        )
        bucket.updated_at = now
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True

    def reset(self) -> None:
        self._buckets.clear()


_preauth_rate_limiter = _PreAuthRateLimiter()
_security_log_last_at: dict[str, float] = {}


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


def _client_ip(request: Request) -> str:
    host = getattr(getattr(request, "client", None), "host", None)
    return host if isinstance(host, str) and host else "unknown"


def _should_log_mismatch(entity_id: str) -> bool:
    now = time.monotonic()
    last = _security_log_last_at.get(entity_id)
    if last is not None and now - last < _SECURITY_LOG_INTERVAL_SECONDS:
        return False
    _security_log_last_at[entity_id] = now
    return True


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

    if not _preauth_rate_limiter.allow(_client_ip(request), entity_id):
        raise _unauthorized()

    supplied = _bearer_token(request)
    if supplied is None:
        raise _unauthorized()

    thread = await db.get_thread(entity_id)
    known_entity = False
    if thread:
        known_entity = True
        entity_type = "thread"
        vm = _object(_object(thread.get("metadata")).get("vm"))
    else:
        job = await db.get_job(entity_id)
        entity_type = "job"
        if job:
            known_entity = True
            vm = _object(_object(job.get("context")).get("vm"))
        else:
            vm = {}

    generation = vm.get("provision_generation")
    eligible = (
        known_entity
        and isinstance(generation, str)
        and bool(generation)
        and vm.get("status") not in _INACTIVE_VM_STATUSES
    )
    comparison_generation = generation if isinstance(generation, str) else None
    comparison_generation = comparison_generation or _DUMMY_GENERATION

    try:
        secrets = _configured_guest_secrets()
    except LifecycleAuthConfigurationError:
        secrets = ()
    comparison_secrets = secrets or (_DUMMY_SECRET,)
    matches = [
        hmac.compare_digest(
            supplied,
            guest_token(secret, entity_type, entity_id, comparison_generation),
        )
        for secret in comparison_secrets
    ]
    if eligible and secrets and any(matches):
        return VmGuestIdentity(entity_type, entity_id, generation)

    if known_entity and _should_log_mismatch(entity_id):
        await log_security_event(
            db,
            resource_type="vm_guest",
            event_type="vm_guest_auth_mismatch",
            resource_id=entity_id,
            detail="VM guest bearer MAC mismatch",
            request=request,
        )
    raise _unauthorized()
