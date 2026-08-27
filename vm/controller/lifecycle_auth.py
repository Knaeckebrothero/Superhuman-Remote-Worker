"""Versioned HMAC envelope for VM lifecycle commands and responses.

Keep this small module behaviorally identical to
``orchestrator/services/vm_lifecycle_auth.py``.  The controller is built from
``vm/controller`` as a standalone image, so it cannot import orchestrator
code at runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
import json
import os
import time
from typing import Any, Literal
from uuid import UUID, uuid4


AUTH_FIELD = "_lifecycle_auth"
AUTH_VERSION = "hmac-sha256-v1"
MIN_SECRET_BYTES = 32
MAX_MESSAGE_AGE_SECONDS = 60
MAX_FUTURE_SKEW_SECONDS = 10

Direction = Literal["request", "response"]


class LifecycleAuthConfigurationError(ValueError):
    """The lifecycle authentication secret is present but unsafe."""


def configured_secret(source: Mapping[str, str] | None = None) -> bytes | None:
    env = os.environ if source is None else source
    value = env.get("VM_LIFECYCLE_HMAC_SECRET", "")
    if not value:
        return None
    secret = value.encode("utf-8")
    if len(secret) < MIN_SECRET_BYTES:
        raise LifecycleAuthConfigurationError(
            f"VM_LIFECYCLE_HMAC_SECRET must be at least {MIN_SECRET_BYTES} bytes"
        )
    return secret


def guest_token(
    secret: bytes,
    entity_type: str,
    entity_id: str,
    provision_generation: str,
) -> str:
    """Derive the generation-scoped bearer token installed in one VM guest."""

    guest_key = hmac.new(
        secret,
        b"srw-kdf|vm-guest-token|v1",
        hashlib.sha256,
    ).digest()
    message = (
        f"srw.vm.guest.v1\n{entity_type}\n{entity_id}\n{provision_generation}\n"
    ).encode()
    return hmac.new(guest_key, message, hashlib.sha256).hexdigest()


def _unsigned_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != AUTH_FIELD}


def signature(
    payload: Mapping[str, Any],
    *,
    direction: Direction,
    operation: str,
    secret: bytes,
    issued_at: int,
    request_id: str,
    correlation_id: str | None,
) -> str:
    canonical = json.dumps(
        {
            "payload": _unsigned_payload(payload),
            "auth": {
                "version": AUTH_VERSION,
                "direction": direction,
                "operation": operation,
                "issued_at": issued_at,
                "request_id": request_id,
                "correlation_id": correlation_id,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    domain = f"srw.vm.lifecycle.{AUTH_VERSION}\n{direction}\n{operation}\n".encode()
    return hmac.new(secret, domain + canonical, hashlib.sha256).hexdigest()


def sign_payload(
    payload: Mapping[str, Any],
    *,
    direction: Direction,
    operation: str,
    secret: bytes | None,
    issued_at: int | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    result = _unsigned_payload(payload)
    if secret is None:
        return result
    timestamp = int(time.time()) if issued_at is None else issued_at
    nonce = str(uuid4()) if request_id is None else request_id
    result[AUTH_FIELD] = {
        "version": AUTH_VERSION,
        "direction": direction,
        "operation": operation,
        "issued_at": timestamp,
        "request_id": nonce,
        "correlation_id": correlation_id,
        "signature": signature(
            result,
            direction=direction,
            operation=operation,
            secret=secret,
            issued_at=timestamp,
            request_id=nonce,
            correlation_id=correlation_id,
        ),
    }
    return result


def verify_payload(
    payload: Mapping[str, Any],
    *,
    direction: Direction,
    operation: str,
    secret: bytes | None,
    now: int | None = None,
    max_age_seconds: int = MAX_MESSAGE_AGE_SECONDS,
    max_future_skew_seconds: int = MAX_FUTURE_SKEW_SECONDS,
    expected_correlation_id: str | None = None,
) -> bool:
    if secret is None:
        return True
    auth = payload.get(AUTH_FIELD)
    if not isinstance(auth, Mapping):
        return False
    if (
        auth.get("version") != AUTH_VERSION
        or auth.get("direction") != direction
        or auth.get("operation") != operation
        or isinstance(auth.get("issued_at"), bool)
        or not isinstance(auth.get("issued_at"), int)
        or not isinstance(auth.get("request_id"), str)
        or (
            auth.get("correlation_id") is not None
            and not isinstance(auth.get("correlation_id"), str)
        )
        or not isinstance(auth.get("signature"), str)
    ):
        return False
    try:
        parsed_request_id = UUID(auth["request_id"])
    except (AttributeError, TypeError, ValueError):
        return False
    if str(parsed_request_id) != auth["request_id"]:
        return False
    correlation_id = auth.get("correlation_id")
    if correlation_id is not None:
        try:
            parsed_correlation_id = UUID(correlation_id)
        except (AttributeError, TypeError, ValueError):
            return False
        if str(parsed_correlation_id) != correlation_id:
            return False
    if (
        expected_correlation_id is not None
        and correlation_id != expected_correlation_id
    ):
        return False
    current_time = int(time.time()) if now is None else now
    issued_at = auth["issued_at"]
    if issued_at < current_time - max_age_seconds:
        return False
    if issued_at > current_time + max_future_skew_seconds:
        return False
    expected = signature(
        payload,
        direction=direction,
        operation=operation,
        secret=secret,
        issued_at=issued_at,
        request_id=auth["request_id"],
        correlation_id=correlation_id,
    )
    return hmac.compare_digest(auth["signature"], expected)


def unsigned_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _unsigned_payload(payload)
