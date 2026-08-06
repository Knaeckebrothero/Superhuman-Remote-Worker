from __future__ import annotations

from uuid import UUID

import pytest
from starlette.requests import Request

from orchestrator.services.infrastructure_metering.ingestion_http import (
    IngestionRequestError,
    authenticate_ingestion_model,
)
from orchestrator.services.infrastructure_metering.ingestion_types import (
    InventoryTicketRequest,
)
from orchestrator.services.infrastructure_metering.transport import (
    canonical_json_bytes,
    sign_transport_request,
)


KEY = "k" * 32
PATH = "/api/internal/infrastructure-metering/v1/tickets"


def _request(body: bytes, *, declared: int | None = None, signed_body=None) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    signed = body if signed_body is None else signed_body
    headers = sign_transport_request(
        method="POST",
        path=PATH,
        collector_id="kubernetes-pods",
        body=signed,
        key=KEY,
    )
    headers["Content-Length"] = str(len(body) if declared is None else declared)
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": PATH,
            "raw_path": PATH.encode(),
            "query_string": b"",
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in headers.items()
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        },
        receive,
    )


def _ticket_body() -> bytes:
    return canonical_json_bytes(
        {
            "scope": {
                "source_cluster": "dev-cluster",
                "api_resource": "core/v1/pods",
                "namespace": "srw",
                "cluster_scoped": False,
            },
            "intent": "snapshot",
            "snapshot_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "starting_resource_version": None,
        }
    )


@pytest.mark.asyncio
async def test_authenticates_exact_bounded_body_before_strict_model_parse():
    result = await authenticate_ingestion_model(
        _request(_ticket_body()),
        key=KEY,
        model_type=InventoryTicketRequest,
        request_kind="snapshot-ticket",
        maximum_bytes=16_384,
    )

    assert result.collector_id == "kubernetes-pods"
    assert result.model.snapshot_id == UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    assert result.transport_claim.request_digest


@pytest.mark.asyncio
async def test_rejects_unauthenticated_headers_before_reading_body():
    async def receive():
        raise AssertionError("unauthenticated request body must not be consumed")

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": PATH,
            "raw_path": PATH.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("203.0.113.10", 1234),
            "server": ("test", 80),
        },
        receive,
    )

    with pytest.raises(
        IngestionRequestError, match="invalid collector authentication"
    ) as rejected:
        await authenticate_ingestion_model(
            request,
            key=KEY,
            model_type=InventoryTicketRequest,
            request_kind="snapshot-ticket",
            maximum_bytes=16_384,
        )
    assert rejected.value.status_code == 401


@pytest.mark.asyncio
async def test_rejects_oversize_and_content_length_mismatch_before_json_parse():
    with pytest.raises(IngestionRequestError, match="too large") as oversized:
        await authenticate_ingestion_model(
            _request(_ticket_body()),
            key=KEY,
            model_type=InventoryTicketRequest,
            request_kind="snapshot-ticket",
            maximum_bytes=10,
        )
    assert oversized.value.status_code == 413

    with pytest.raises(IngestionRequestError, match="content length mismatch"):
        await authenticate_ingestion_model(
            _request(_ticket_body(), declared=len(_ticket_body()) + 1),
            key=KEY,
            model_type=InventoryTicketRequest,
            request_kind="snapshot-ticket",
            maximum_bytes=16_384,
        )


@pytest.mark.asyncio
async def test_rejects_signature_for_different_body_without_exposing_payload():
    with pytest.raises(IngestionRequestError, match="invalid collector authentication"):
        await authenticate_ingestion_model(
            _request(_ticket_body(), signed_body=b"{}"),
            key=KEY,
            model_type=InventoryTicketRequest,
            request_kind="snapshot-ticket",
            maximum_bytes=16_384,
        )
