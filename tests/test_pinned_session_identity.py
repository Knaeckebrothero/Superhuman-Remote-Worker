from __future__ import annotations

import pytest

from shared.pinned_session_identity import (
    PINNED_SESSION_READY_IDENTITY_CONTRACT,
    PinnedSessionBinding,
    pinned_session_ready_identity_fingerprint,
)


THREAD_ID = "10000000-0000-4000-8000-000000000001"
GENERATION = "20000000-0000-4000-8000-000000000002"
AGENT_ID = "30000000-0000-4000-8000-000000000003"
ATTACH_TOKEN = "40000000-0000-4000-8000-000000000004"
POD_UID = "50000000-0000-4000-8000-000000000005"


def _fingerprint(**overrides: str) -> str | None:
    values = {
        "thread_id": THREAD_ID,
        "runtime_generation": GENERATION,
        "agent_id": AGENT_ID,
        "runtime_attach_token": ATTACH_TOKEN,
        "pod_uid": POD_UID,
    }
    values.update(overrides)
    return pinned_session_ready_identity_fingerprint(**values)


def test_pinned_ready_identity_is_canonical_non_secret_and_exact() -> None:
    assert PINNED_SESSION_READY_IDENTITY_CONTRACT == 1
    expected = _fingerprint()
    assert expected is not None and expected.startswith("sha256:")
    assert len(expected) == len("sha256:") + 64
    assert ATTACH_TOKEN not in expected
    assert _fingerprint(agent_id=AGENT_ID.upper()) == expected

    for field, replacement in (
        ("thread_id", "10000000-0000-4000-8000-000000000009"),
        ("runtime_generation", "20000000-0000-4000-8000-000000000009"),
        ("agent_id", "30000000-0000-4000-8000-000000000009"),
        ("runtime_attach_token", "40000000-0000-4000-8000-000000000009"),
        ("pod_uid", "50000000-0000-4000-8000-000000000009"),
    ):
        assert _fingerprint(**{field: replacement}) != expected


def test_pinned_ready_identity_rejects_incomplete_or_malformed_authority() -> None:
    for field in (
        "thread_id",
        "runtime_generation",
        "agent_id",
        "runtime_attach_token",
        "pod_uid",
    ):
        assert _fingerprint(**{field: ""}) is None
    assert _fingerprint(pod_uid="bad\0uid") is None
    assert _fingerprint(pod_uid=" padded") is None


def _binding(**overrides: object) -> PinnedSessionBinding:
    values: dict[str, object] = {
        "thread_id": THREAD_ID,
        "runtime_generation": GENERATION,
        "agent_id": AGENT_ID,
        "runtime_attach_token": ATTACH_TOKEN,
        "agent_hostname": "agent-pinned-1",
        "pod_namespace": "srw",
        "pod_uid": POD_UID,
        "pod_ip": "10.42.0.17",
        "pod_port": 8001,
        "agent_status": "session",
    }
    values.update(overrides)
    return PinnedSessionBinding.from_mapping(values)


def test_pinned_binding_freezes_full_route_target_without_repr_secret() -> None:
    binding = _binding(agent_id=AGENT_ID.upper(), pod_port=None)

    assert binding.agent_id == AGENT_ID
    assert binding.pod_port == 8001
    assert binding.session_identity_fingerprint == _fingerprint()
    assert binding.target_key == (
        THREAD_ID,
        GENERATION,
        AGENT_ID,
        ATTACH_TOKEN,
        "agent-pinned-1",
        "srw",
        POD_UID,
        "10.42.0.17",
        8001,
        "provisioned",
    )
    assert ATTACH_TOKEN not in repr(binding)
    assert _binding(agent_status="working").target_key == binding.target_key


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("thread_id", "bad"),
        ("runtime_generation", ""),
        ("agent_id", None),
        ("runtime_attach_token", "bad"),
        ("agent_hostname", ""),
        ("agent_hostname", " padded"),
        ("pod_namespace", ""),
        ("pod_namespace", "Invalid_Namespace"),
        ("pod_namespace", "-invalid"),
        ("pod_uid", "bad\0uid"),
        ("pod_uid", {"not": "text"}),
        ("pod_ip", ""),
        ("pod_ip", "10.42.0.17 "),
        ("pod_port", True),
        ("pod_port", "8001"),
        ("pod_port", 0),
        ("pod_port", 65_536),
        ("agent_status", ""),
        ("pod_authority_kind", "foreign"),
    ],
)
def test_pinned_binding_rejects_malformed_joined_coordinates(
    field: str, value: object
) -> None:
    with pytest.raises((TypeError, ValueError, AttributeError)):
        _binding(**{field: value})
