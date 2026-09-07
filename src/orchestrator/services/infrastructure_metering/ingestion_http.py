"""Bounded authenticated HTTP boundary for inventory ingestion routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from starlette.requests import Request

from orchestrator.services.infrastructure_metering.inventory import TransportNonceClaim
from orchestrator.services.infrastructure_metering.transport import (
    TransportAuthError,
    verify_transport_headers,
    verify_transport_request,
)


T = TypeVar("T", bound=BaseModel)


class IngestionRequestError(ValueError):
    """Sanitized route error; its message is safe for an HTTP detail field."""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class AuthenticatedIngestionModel:
    model: BaseModel
    transport_claim: TransportNonceClaim
    collector_id: str
    body: bytes


async def read_bounded_body(request: Request, *, maximum_bytes: int) -> bytes:
    """Read an ASGI body incrementally and reject before unbounded buffering."""

    if maximum_bytes <= 0:
        raise ValueError("maximum_bytes must be positive")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type.casefold() != "application/json":
        raise IngestionRequestError(415, "application/json required")
    if request.headers.get("content-encoding", "identity").casefold() != "identity":
        raise IngestionRequestError(415, "content encoding is not supported")
    raw_length = request.headers.get("content-length")
    if raw_length is not None:
        try:
            declared = int(raw_length)
        except ValueError as exc:
            raise IngestionRequestError(400, "invalid content length") from exc
        if declared < 0 or declared > maximum_bytes:
            raise IngestionRequestError(413, "ingestion request is too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            raise IngestionRequestError(413, "ingestion request is too large")
        body.extend(chunk)
    if raw_length is not None and len(body) != declared:
        raise IngestionRequestError(400, "content length mismatch")
    if not body:
        raise IngestionRequestError(400, "ingestion request body is empty")
    return bytes(body)


async def authenticate_ingestion_model(
    request: Request,
    *,
    key: str | bytes,
    model_type: type[T],
    request_kind: str,
    maximum_bytes: int,
) -> AuthenticatedIngestionModel:
    """Verify the exact body/path signature before strict model validation."""

    # Authenticate the signed content digest and bounded metadata before
    # consuming a potentially multi-megabyte ASGI body. Exact body verification
    # still follows the bounded read, so a digest header cannot substitute data.
    try:
        verify_transport_headers(
            method=request.method,
            path=request.url.path,
            headers=request.headers,
            key=key,
        )
    except (TransportAuthError, ValueError) as exc:
        raise IngestionRequestError(401, "invalid collector authentication") from exc
    body = await read_bounded_body(request, maximum_bytes=maximum_bytes)
    try:
        authenticated = verify_transport_request(
            method=request.method,
            path=request.url.path,
            headers=request.headers,
            body=body,
            key=key,
        )
    except (TransportAuthError, ValueError) as exc:
        raise IngestionRequestError(401, "invalid collector authentication") from exc
    try:
        model = model_type.model_validate_json(body)
    except ValidationError as exc:
        raise IngestionRequestError(400, "invalid ingestion request") from exc
    return AuthenticatedIngestionModel(
        model=model,
        transport_claim=TransportNonceClaim(
            collector_id=authenticated.collector_id,
            request_nonce=authenticated.nonce,
            request_kind=request_kind,
            request_digest=authenticated.body_sha256,
        ),
        collector_id=authenticated.collector_id,
        body=body,
    )


__all__ = [
    "AuthenticatedIngestionModel",
    "IngestionRequestError",
    "authenticate_ingestion_model",
    "read_bounded_body",
]
