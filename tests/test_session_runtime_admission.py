import json

import pytest

from orchestrator.services.session_runtime_admission import (
    ThreadRuntimeAuthority,
    pinned_binding_invalid_detail,
    protected_cloud_marker_state,
    thread_requests_protected_cloud,
    thread_runtime_refusal_detail,
)


RUNTIME_GENERATION = "55555555-5555-4555-8555-555555555555"


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({}, False),
        ({"protected_cloud": False}, False),
        ({"protected_cloud": True}, True),
        ({"protected_cloud": "false"}, True),
        ({"protected_cloud": "true"}, True),
        ({"protected_cloud": 0}, True),
        ({"protected_cloud": 1}, True),
        ({"protected_cloud": None}, True),
    ],
)
@pytest.mark.parametrize("encoded", [False, True], ids=["mapping", "postgres-json"])
def test_protected_cloud_requirement_accepts_the_storage_encoding(
    metadata, expected, encoded
):
    stored = json.dumps(metadata) if encoded else metadata

    assert thread_requests_protected_cloud({"metadata": stored}) is expected


@pytest.mark.parametrize(
    "stored",
    ["", False, 0, [], "{malformed", "null", "false", "0", "[]", '"ordinary"'],
)
def test_invalid_stored_metadata_cannot_bypass_protected_readiness(stored):
    assert thread_requests_protected_cloud({"metadata": stored}) is True


@pytest.mark.parametrize("thread", [{}, {"metadata": None}])
def test_historical_missing_metadata_remains_an_ordinary_session(thread):
    assert thread_requests_protected_cloud(thread) is False


def test_marker_parser_remains_strict_about_its_decoded_object_contract():
    assert protected_cloud_marker_state('{"protected_cloud": false}') == "malformed"


def test_binding_refusal_is_scoped_to_the_exact_runtime_generation():
    detail = pinned_binding_invalid_detail(
        ThreadRuntimeAuthority(
            thread_id="44444444-4444-4444-8444-444444444444",
            generation=RUNTIME_GENERATION,
        )
    )

    assert detail == {
        "code": "session_binding_invalid",
        "message": "This session binding is no longer authoritative.",
        "pinned_runtime_generation_contract": 1,
        "session_runtime_generation": RUNTIME_GENERATION,
    }


def test_terminal_refusal_binds_ending_and_ended_to_exact_runtime_generation():
    ending = thread_runtime_refusal_detail(
        {
            "status": "active",
            "runtime_generation": RUNTIME_GENERATION,
            "runtime_retirement_token": "66666666-6666-4666-8666-666666666666",
            "runtime_retirement_authorized_at": "2026-08-26T14:00:00Z",
            "runtime_retirement_context": {"settle_status": "suspended"},
        }
    )
    ended = thread_runtime_refusal_detail(
        {
            "status": "ended",
            "runtime_generation": RUNTIME_GENERATION,
            "runtime_retirement_token": None,
        }
    )

    assert ending == {
        "code": "session_ending",
        "message": "This session is finishing its exact runtime cleanup.",
        "retirement_disposition": "suspended",
        "pinned_runtime_generation_contract": 1,
        "session_runtime_generation": RUNTIME_GENERATION,
    }
    assert ended == {
        "code": "session_ended",
        "message": "This session has ended. Resume it before reconnecting.",
        "pinned_runtime_generation_contract": 1,
        "session_runtime_generation": RUNTIME_GENERATION,
    }


def test_terminal_refusal_never_claims_a_malformed_runtime_generation():
    detail = thread_runtime_refusal_detail(
        {
            "status": "ended",
            "runtime_generation": "not-a-generation",
        }
    )

    assert detail == {
        "code": "session_ended",
        "message": "This session has ended. Resume it before reconnecting.",
    }
