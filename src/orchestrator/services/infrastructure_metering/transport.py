"""Authenticated wire helpers for the dedicated inventory collector.

The collector intentionally does not receive database credentials or the
agent-wide ``MCP_INTERNAL_KEY``.  It signs each bounded request with a separate
metering key.  The orchestrator persists the nonce before acting on a request;
these helpers only validate the cryptographic and time-bound portion of that
contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import time
from typing import Any, Mapping
from uuid import UUID, uuid4

_COLLECTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SIGNATURE = re.compile(r"^[0-9a-f]{64}$")

COLLECTOR_HEADER = "X-SRW-Metering-Collector"
TIMESTAMP_HEADER = "X-SRW-Metering-Timestamp"
NONCE_HEADER = "X-SRW-Metering-Nonce"
BODY_SHA256_HEADER = "X-SRW-Metering-Content-SHA256"
SIGNATURE_HEADER = "X-SRW-Metering-Signature"


class TransportAuthError(ValueError):
    """A request failed the collector transport authentication contract."""


@dataclass(frozen=True)
class AuthenticatedTransportRequest:
    collector_id: str
    timestamp: int
    nonce: UUID
    body_sha256: str


def canonical_json_bytes(value: Any) -> bytes:
    """Return the single JSON representation used for request signatures."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("metering payload is not canonical JSON") from exc
    return encoded.encode("utf-8")


def _key_bytes(key: str | bytes) -> bytes:
    encoded = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    if len(encoded) < 32:
        raise ValueError("infrastructure metering ingestion key must be 32+ bytes")
    return encoded


def _canonical_request(
    *,
    method: str,
    path: str,
    collector_id: str,
    timestamp: int,
    nonce: str,
    body_sha256: str,
) -> bytes:
    return (
        "srw-infrastructure-metering-v1\n"
        f"{method.upper()}\n{path}\n{collector_id}\n{timestamp}\n{nonce}\n"
        f"{body_sha256}"
    ).encode("utf-8")


def sign_transport_request(
    *,
    method: str,
    path: str,
    collector_id: str,
    body: bytes,
    key: str | bytes,
    timestamp: int | None = None,
    nonce: UUID | None = None,
) -> dict[str, str]:
    """Build time-bound HMAC headers for one exact method/path/body tuple."""
    if not _COLLECTOR_ID.fullmatch(collector_id):
        raise ValueError("invalid infrastructure metering collector id")
    request_timestamp = int(time.time()) if timestamp is None else int(timestamp)
    request_nonce = nonce or uuid4()
    body_sha256 = hashlib.sha256(body).hexdigest()
    canonical = _canonical_request(
        method=method,
        path=path,
        collector_id=collector_id,
        timestamp=request_timestamp,
        nonce=str(request_nonce),
        body_sha256=body_sha256,
    )
    signature = hmac.new(_key_bytes(key), canonical, hashlib.sha256).hexdigest()
    return {
        COLLECTOR_HEADER: collector_id,
        TIMESTAMP_HEADER: str(request_timestamp),
        NONCE_HEADER: str(request_nonce),
        BODY_SHA256_HEADER: body_sha256,
        SIGNATURE_HEADER: signature,
        "Content-Type": "application/json",
    }


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    return str(value or "").strip()


def verify_transport_headers(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    key: str | bytes,
    now: int | None = None,
    max_clock_skew_seconds: int = 60,
) -> AuthenticatedTransportRequest:
    """Authenticate signed metadata before allocating for the request body."""

    collector_id = _header(headers, COLLECTOR_HEADER)
    raw_timestamp = _header(headers, TIMESTAMP_HEADER)
    raw_nonce = _header(headers, NONCE_HEADER)
    body_sha256 = _header(headers, BODY_SHA256_HEADER)
    signature = _header(headers, SIGNATURE_HEADER)
    if not _COLLECTOR_ID.fullmatch(collector_id):
        raise TransportAuthError("invalid collector authentication")
    try:
        request_timestamp = int(raw_timestamp)
        request_nonce = UUID(raw_nonce)
    except (TypeError, ValueError) as exc:
        raise TransportAuthError("invalid collector authentication") from exc
    current = int(time.time()) if now is None else int(now)
    if abs(current - request_timestamp) > max_clock_skew_seconds:
        raise TransportAuthError("expired collector authentication")
    if not _SIGNATURE.fullmatch(signature) or not _SIGNATURE.fullmatch(body_sha256):
        raise TransportAuthError("invalid collector authentication")
    canonical = _canonical_request(
        method=method,
        path=path,
        collector_id=collector_id,
        timestamp=request_timestamp,
        nonce=str(request_nonce),
        body_sha256=body_sha256,
    )
    expected = hmac.new(_key_bytes(key), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise TransportAuthError("invalid collector authentication")
    return AuthenticatedTransportRequest(
        collector_id=collector_id,
        timestamp=request_timestamp,
        nonce=request_nonce,
        body_sha256=body_sha256,
    )


def verify_transport_request(
    *,
    method: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    key: str | bytes,
    now: int | None = None,
    max_clock_skew_seconds: int = 60,
) -> AuthenticatedTransportRequest:
    """Verify signed metadata and its exact body; DB owns durable replay checks."""

    authenticated = verify_transport_headers(
        method=method,
        path=path,
        headers=headers,
        key=key,
        now=now,
        max_clock_skew_seconds=max_clock_skew_seconds,
    )
    actual_body_sha256 = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(authenticated.body_sha256, actual_body_sha256):
        raise TransportAuthError("invalid collector authentication")
    return authenticated


__all__ = [
    "AuthenticatedTransportRequest",
    "BODY_SHA256_HEADER",
    "COLLECTOR_HEADER",
    "NONCE_HEADER",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "TransportAuthError",
    "canonical_json_bytes",
    "sign_transport_request",
    "verify_transport_headers",
    "verify_transport_request",
]
