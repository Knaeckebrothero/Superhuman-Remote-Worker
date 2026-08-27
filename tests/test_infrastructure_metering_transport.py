from uuid import UUID

import pytest

from orchestrator.services.infrastructure_metering.transport import (
    BODY_SHA256_HEADER,
    TransportAuthError,
    canonical_json_bytes,
    sign_transport_request,
    verify_transport_headers,
    verify_transport_request,
)


KEY = "metering-test-key-that-is-at-least-thirty-two-bytes"
PATH = "/api/internal/infrastructure-metering/v1/tickets"


def test_transport_signature_binds_method_path_body_and_identity():
    body = canonical_json_bytes({"namespace": "srw", "kind": "pods"})
    headers = sign_transport_request(
        method="POST",
        path=PATH,
        collector_id="kubernetes-pods",
        body=body,
        key=KEY,
        timestamp=1_700_000_000,
        nonce=UUID("11111111-1111-4111-8111-111111111111"),
    )

    verified = verify_transport_request(
        method="POST",
        path=PATH,
        headers=headers,
        body=body,
        key=KEY,
        now=1_700_000_030,
    )

    assert verified.collector_id == "kubernetes-pods"
    assert str(verified.nonce) == "11111111-1111-4111-8111-111111111111"
    assert (
        verify_transport_headers(
            method="POST",
            path=PATH,
            headers=headers,
            key=KEY,
            now=1_700_000_030,
        ).body_sha256
        == headers[BODY_SHA256_HEADER]
    )

    for changed in (
        {"method": "PUT", "path": PATH, "body": body},
        {"method": "POST", "path": f"{PATH}/other", "body": body},
        {"method": "POST", "path": PATH, "body": body + b" "},
    ):
        with pytest.raises(TransportAuthError, match="invalid"):
            verify_transport_request(
                headers=headers,
                key=KEY,
                now=1_700_000_030,
                **changed,
            )

    changed_digest = dict(headers)
    changed_digest[BODY_SHA256_HEADER] = "0" * 64
    with pytest.raises(TransportAuthError, match="invalid"):
        verify_transport_headers(
            method="POST",
            path=PATH,
            headers=changed_digest,
            key=KEY,
            now=1_700_000_030,
        )


def test_transport_signature_rejects_expired_or_wrong_key():
    body = b"{}"
    headers = sign_transport_request(
        method="POST",
        path=PATH,
        collector_id="collector-a",
        body=body,
        key=KEY,
        timestamp=100,
    )
    with pytest.raises(TransportAuthError, match="expired"):
        verify_transport_request(
            method="POST",
            path=PATH,
            headers=headers,
            body=body,
            key=KEY,
            now=161,
        )
    with pytest.raises(TransportAuthError, match="invalid"):
        verify_transport_request(
            method="POST",
            path=PATH,
            headers=headers,
            body=body,
            key="different-metering-key-that-is-also-long-enough",
            now=100,
        )


def test_transport_requires_strong_key_and_canonical_finite_json():
    with pytest.raises(ValueError, match=r"32\+"):
        sign_transport_request(
            method="POST",
            path=PATH,
            collector_id="collector-a",
            body=b"{}",
            key="short",
        )
    with pytest.raises(ValueError, match="canonical JSON"):
        canonical_json_bytes({"quantity": float("nan")})
