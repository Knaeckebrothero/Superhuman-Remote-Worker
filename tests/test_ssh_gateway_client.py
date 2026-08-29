"""Tests for the SSH gateway's target-resolution client.

Builds ``GatewayConfig`` directly rather than going through ``load_config``:
``load_config`` now does real file I/O -- it opens and parses every
configured host key (rejecting anything that isn't Ed25519) and loads the
user CA key from disk (Task 1's ``load_user_ca``). ``resolve_target`` never
reads ``host_key_paths``, ``user_ca_path``, or ``allowed_origins`` -- only
``config.orchestrator_url`` and ``config.internal_key`` -- so routing every
test in this file through real key generation and parsing would make them
about ``ssh_gateway_config``'s file I/O, not about this module's HTTP
behavior. Mirrors the ``_gateway_config_kwargs`` pattern at
``tests/test_ssh_gateway_config.py:300``, which exists for the exact same
reason one field over (``GatewayConfig`` construction tests that don't want
``load_config``'s validation).

Deliberately does NOT import ``BASE_ENV`` from ``test_ssh_gateway_config``:
that fixture's real key material comes from a ``scope="session", autouse``
fixture defined in that module, which pytest only runs for tests collected
from that module.
"""

import pytest

from services import ssh_gateway_targets
from services.ssh_gateway_client import (
    REFUSAL_MESSAGES,
    SshTarget,
    TargetDenied,
    TargetUnavailable,
    resolve_target,
)
from services.ssh_gateway_config import GatewayConfig


def _gateway_config_kwargs(**overrides):
    base = dict(
        host_key_paths=("/k",),
        user_ca_path="/ca",
        orchestrator_url="http://orchestrator:8085",
        internal_key="internal",
        allowed_origins=("https://cockpit.example",),
    )
    base.update(overrides)
    return base


def _config(**overrides) -> GatewayConfig:
    return GatewayConfig(**_gateway_config_kwargs(**overrides))


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_live_target_is_returned(monkeypatch):
    async def _get(url, headers=None, params=None, timeout=None):
        assert headers["X-Internal-Key"] == "internal"
        assert params["fingerprint"].startswith("SHA256:")
        return FakeResponse(
            200,
            {
                "thread_id": "t1",
                "user_id": "u1",
                "pod_ip": "10.1.2.3",
                "pod_port": 30022,
                "host_key_fingerprint": "SHA256:xyz",
                "state": "live",
            },
        )

    import services.ssh_gateway_client as mod

    monkeypatch.setattr(mod, "_http_get", _get)
    target = await resolve_target(_config(), "s-7f3a91c2", "SHA256:abc")
    assert isinstance(target, SshTarget)
    assert target.pod_port == 30022


@pytest.mark.asyncio
async def test_404_is_denial_not_unavailability(monkeypatch):
    """Unknown handle, unknown key and unauthorized are one opaque case."""

    async def _get(url, headers=None, params=None, timeout=None):
        return FakeResponse(404, {"detail": "No such workspace"})

    import services.ssh_gateway_client as mod

    monkeypatch.setattr(mod, "_http_get", _get)
    with pytest.raises(TargetDenied):
        await resolve_target(_config(), "s-7f3a91c2", "SHA256:abc")


@pytest.mark.asyncio
async def test_non_live_state_raises_with_the_state(monkeypatch):
    async def _get(url, headers=None, params=None, timeout=None):
        return FakeResponse(
            200,
            {
                "thread_id": "t1",
                "user_id": "u1",
                "pod_ip": None,
                "pod_port": None,
                "host_key_fingerprint": None,
                "state": "suspended",
            },
        )

    import services.ssh_gateway_client as mod

    monkeypatch.setattr(mod, "_http_get", _get)
    with pytest.raises(TargetUnavailable) as excinfo:
        await resolve_target(_config(), "s-7f3a91c2", "SHA256:abc")
    assert excinfo.value.state == "suspended"


@pytest.mark.asyncio
async def test_orchestrator_failure_fails_closed(monkeypatch):
    async def _get(url, headers=None, params=None, timeout=None):
        raise OSError("connection refused")

    import services.ssh_gateway_client as mod

    monkeypatch.setattr(mod, "_http_get", _get)
    with pytest.raises(TargetUnavailable) as excinfo:
        await resolve_target(_config(), "s-7f3a91c2", "SHA256:abc")
    assert excinfo.value.state == "unreachable"


@pytest.mark.asyncio
async def test_live_payload_with_null_port_refuses_instead_of_crashing(monkeypatch):
    """Ruling G13: ``state: "live"`` is not proof the rest of the payload is
    well-formed. ``int(None)`` raises ``TypeError`` -- that must become a
    readable refusal, not an unhandled exception surfacing mid-authentication
    in the SSH server."""

    async def _get(url, headers=None, params=None, timeout=None):
        return FakeResponse(
            200,
            {
                "thread_id": "t1",
                "user_id": "u1",
                "pod_ip": "svc.ns.svc.cluster.local",
                "pod_port": None,
                "host_key_fingerprint": "SHA256:xyz",
                "state": "live",
            },
        )

    import services.ssh_gateway_client as mod

    monkeypatch.setattr(mod, "_http_get", _get)
    with pytest.raises(TargetUnavailable) as excinfo:
        await resolve_target(_config(), "s-7f3a91c2", "SHA256:abc")
    assert excinfo.value.state == "unreachable"


@pytest.mark.asyncio
async def test_live_payload_missing_a_field_refuses_instead_of_crashing(monkeypatch):
    """Same defect, the ``KeyError`` half: a "live" payload missing a
    required field must not surface as an unhandled ``KeyError`` either."""

    async def _get(url, headers=None, params=None, timeout=None):
        return FakeResponse(
            200,
            {
                "thread_id": "t1",
                "user_id": "u1",
                "pod_ip": "svc.ns.svc.cluster.local",
                "pod_port": 30022,
                "state": "live",
                # host_key_fingerprint is missing entirely.
            },
        )

    import services.ssh_gateway_client as mod

    monkeypatch.setattr(mod, "_http_get", _get)
    with pytest.raises(TargetUnavailable) as excinfo:
        await resolve_target(_config(), "s-7f3a91c2", "SHA256:abc")
    assert excinfo.value.state == "unreachable"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["failed", "deleted", "stale_binding"])
async def test_the_three_added_states_are_not_collapsed_to_unreachable(
    monkeypatch, state
):
    """Ruling G10: before this fix, any state absent from REFUSAL_MESSAGES
    (which "failed"/"deleted"/"stale_binding" all were) fell back to
    "unreachable" -- telling whoever reads the refusal that the gateway
    couldn't reach the control plane, when it actually reached it and got a
    specific, actionable answer. See ssh_gateway_targets.py's own comments on
    STATE_FAILED/STATE_DELETED/STATE_STALE_BINDING for why collapsing these
    "sends an operator after the wrong problem"."""

    async def _get(url, headers=None, params=None, timeout=None):
        return FakeResponse(
            200,
            {
                "thread_id": "t1",
                "user_id": "u1",
                "pod_ip": None,
                "pod_port": None,
                "host_key_fingerprint": None,
                "state": state,
            },
        )

    import services.ssh_gateway_client as mod

    monkeypatch.setattr(mod, "_http_get", _get)
    with pytest.raises(TargetUnavailable) as excinfo:
        await resolve_target(_config(), "s-7f3a91c2", "SHA256:abc")
    assert excinfo.value.state == state


def _non_live_states() -> list[str]:
    """Every ``STATE_*`` constant ``ssh_gateway_targets.py`` defines, except
    "live".

    Introspected rather than hardcoded (Ruling G10): a state added upstream
    to ``ssh_gateway_targets.py`` is picked up here automatically, so if
    ``REFUSAL_MESSAGES`` is not updated to match, this test fails instead of
    ``resolve_target`` silently degrading the new state to "unreachable".
    """
    return [
        value
        for name, value in vars(ssh_gateway_targets).items()
        if name.startswith("STATE_")
        and isinstance(value, str)
        and value != ssh_gateway_targets.STATE_LIVE
    ]


def test_every_state_has_a_message_and_exit_code():
    states = _non_live_states()
    # Sanity floor: if a future rename stopped the introspection above from
    # matching anything, the loop below would vacuously pass. Pin the three
    # states this ruling specifically added so that can't happen silently.
    assert {"failed", "deleted", "stale_binding"} <= set(states)
    for state in states:
        message, code = REFUSAL_MESSAGES[state]
        assert message and not message[0].isupper()
        assert code in (69, 75, 77)


def test_suspended_and_reclaimed_say_different_things():
    """A reclaimed volume is not the same promise as a suspended one."""
    assert REFUSAL_MESSAGES["suspended"][0] != REFUSAL_MESSAGES["reclaimed"][0]


def test_failed_deleted_and_stale_binding_say_different_things():
    """Ruling G10: these three (plus never_provisioned, the state they used
    to be misreported as inside ssh_gateway_targets.py itself) must each be
    distinct -- collapsing any pair back together reintroduces the exact
    "operator sent after the wrong problem" defect this ruling fixes."""
    messages = {
        REFUSAL_MESSAGES["failed"][0],
        REFUSAL_MESSAGES["deleted"][0],
        REFUSAL_MESSAGES["stale_binding"][0],
        REFUSAL_MESSAGES["never_provisioned"][0],
    }
    assert len(messages) == 4
