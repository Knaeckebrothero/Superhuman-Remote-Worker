"""Cross-image contract tests for VM lifecycle HMAC envelopes."""

import hashlib
import hmac
from uuid import uuid4

import pytest

from orchestrator.services import vm_lifecycle_auth as orchestrator_auth
from vm.controller import lifecycle_auth as controller_auth


SECRET = b"a-dedicated-lifecycle-secret-at-least-32-bytes"
ISSUED_AT = 1_800_000_000
REQUEST_ID = "00000000-0000-4000-8000-000000000001"


def test_guest_token_matches_cross_image_formula() -> None:
    secret = b"0123456789abcdef0123456789abcdef"
    entity_type = "job"
    entity_id = "11111111-1111-4111-8111-111111111111"
    generation = "22222222-2222-4222-8222-222222222222"
    guest_key = hmac.new(secret, b"srw-kdf|vm-guest-token|v1", hashlib.sha256).digest()
    expected = hmac.new(
        guest_key,
        (f"srw.vm.guest.v1\n{entity_type}\n{entity_id}\n{generation}\n").encode(),
        hashlib.sha256,
    ).hexdigest()

    assert (
        orchestrator_auth.guest_token(secret, entity_type, entity_id, generation)
        == expected
    )


def _signed_by_orchestrator(operation: str = "create") -> dict:
    return orchestrator_auth.sign_payload(
        {
            "job_id": str(uuid4()),
            "provision_generation": "00000000-0000-4000-8000-000000000002",
        },
        direction="request",
        operation=operation,
        secret=SECRET,
        issued_at=ISSUED_AT,
        request_id=REQUEST_ID,
    )


def test_orchestrator_and_standalone_controller_envelopes_interoperate() -> None:
    request = _signed_by_orchestrator()
    assert controller_auth.verify_payload(
        request,
        direction="request",
        operation="create",
        secret=SECRET,
        now=ISSUED_AT,
    )

    response = controller_auth.sign_payload(
        {
            "job_id": request["job_id"],
            "vm_uid": "admitted-uid",
            "provision_generation": request["provision_generation"],
        },
        direction="response",
        operation="create",
        secret=SECRET,
        issued_at=ISSUED_AT,
        request_id=REQUEST_ID,
    )
    assert orchestrator_auth.verify_payload(
        response,
        direction="response",
        operation="create",
        secret=SECRET,
        now=ISSUED_AT,
    )


def test_tampered_or_cross_operation_envelope_is_rejected() -> None:
    request = _signed_by_orchestrator()
    request["job_id"] = str(uuid4())
    assert not controller_auth.verify_payload(
        request,
        direction="request",
        operation="create",
        secret=SECRET,
        now=ISSUED_AT,
    )

    untampered = _signed_by_orchestrator()
    assert not controller_auth.verify_payload(
        untampered,
        direction="request",
        operation="delete",
        secret=SECRET,
        now=ISSUED_AT,
    )


@pytest.mark.parametrize("issued_at", [ISSUED_AT - 61, ISSUED_AT + 11])
def test_expired_or_future_envelope_is_rejected(issued_at: int) -> None:
    request = orchestrator_auth.sign_payload(
        {"job_id": "job-one"},
        direction="request",
        operation="status",
        secret=SECRET,
        issued_at=issued_at,
        request_id=REQUEST_ID,
    )
    assert not controller_auth.verify_payload(
        request,
        direction="request",
        operation="status",
        secret=SECRET,
        now=ISSUED_AT,
    )


def test_unsigned_payload_is_legacy_only() -> None:
    payload = {"job_id": "job-one"}
    assert orchestrator_auth.verify_payload(
        payload,
        direction="request",
        operation="create",
        secret=None,
        now=ISSUED_AT,
    )
    assert not orchestrator_auth.verify_payload(
        payload,
        direction="request",
        operation="create",
        secret=SECRET,
        now=ISSUED_AT,
    )


def test_present_short_key_never_downgrades_to_legacy() -> None:
    with pytest.raises(
        orchestrator_auth.LifecycleAuthConfigurationError,
        match="at least 32 bytes",
    ):
        orchestrator_auth.configured_secret({"VM_LIFECYCLE_HMAC_SECRET": "short"})


def test_guest_token_known_vector() -> None:
    assert (
        controller_auth.guest_token(
            SECRET,
            "job",
            "11111111-2222-4333-8444-555555555555",
            "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        )
        == "42e877b1219d604802f241eca001f27ec0ebc337bd8e05e9fffe22a8b46e3f33"
    )


def test_response_must_correlate_to_the_exact_request() -> None:
    correlation_id = "00000000-0000-4000-8000-000000000010"
    response = controller_auth.sign_payload(
        {"job_id": "job-one", "status": "created"},
        direction="response",
        operation="create",
        secret=SECRET,
        issued_at=ISSUED_AT,
        request_id=REQUEST_ID,
        correlation_id=correlation_id,
    )

    assert orchestrator_auth.verify_payload(
        response,
        direction="response",
        operation="create",
        secret=SECRET,
        now=ISSUED_AT,
        expected_correlation_id=correlation_id,
    )
    assert not orchestrator_auth.verify_payload(
        response,
        direction="response",
        operation="create",
        secret=SECRET,
        now=ISSUED_AT,
        expected_correlation_id="00000000-0000-4000-8000-000000000011",
    )
