"""Tests for ``services.provision_or_assign`` lifecycle emission.

This is the create-thread binding path. It must emit the same
``session.lifecycle`` sequence (``provisioning`` → ``booting`` →
``ready``) that ``routers/sessions._do_prepare`` does, so the cockpit's
startup card renders live counters regardless of which path bound the
agent. See the warm-pool regression on dev cluster thread ``68acde8d``
(2026-05-23) and the unified-signal-source plan.
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest


def _install_fake_main(monkeypatch, **overrides) -> types.ModuleType:
    """Inject a stub ``main`` module into sys.modules.

    The function under test does a late ``from main import …`` of a handful
    of singletons. We provide a stub so the import resolves without
    triggering the real ``main.py`` side-effect chain (license gate, agent
    provisioner connect, etc.).
    """
    stub = types.ModuleType("main")

    # Defaults — individual tests override via `overrides`.
    fake_db = MagicMock()
    fake_db.get_thread = AsyncMock(return_value={"id": "t1", "agent_id": None})
    fake_db.resolve_datasources_for_thread = AsyncMock(return_value=[])
    fake_db.get_agent = AsyncMock(
        return_value={"id": "a1", "pod_ip": "10.0.0.5", "pod_port": 8001}
    )
    lock_cm = AsyncMock()
    lock_cm.__aenter__.return_value = None
    lock_cm.__aexit__.return_value = False
    fake_db.thread_advisory_lock = MagicMock(return_value=lock_cm)
    stub.postgres_db = fake_db

    async def _no_idle():
        return None

    stub._find_idle_persistent_agent = _no_idle

    async def _attach(*args, **kwargs):
        return True

    stub._send_session_attach = _attach
    stub._build_datasources_payload = lambda _ds: []
    stub._build_datasource_tool_override = lambda _ds, _co: _co

    fake_provisioner = MagicMock()
    fake_provisioner.provision_agent = AsyncMock(return_value="srw-agent-s-new")
    stub.agent_provisioner = fake_provisioner

    # Pre-flight capability-grant check — defaults to "no violations" so the
    # happy-path tests proceed; the grant-denied test overrides it.
    stub._session_grant_violations = AsyncMock(return_value=[])
    stub._grant_violations_detail = (
        lambda v: "config exceeds your capability grants: " + "; ".join(v)
    )

    for k, v in overrides.items():
        setattr(stub, k, v)

    monkeypatch.setitem(sys.modules, "main", stub)
    return stub


def _install_fake_lifecycle_module(monkeypatch, emit_calls: list[dict]):
    """Stub ``services.session_lifecycle`` so the function-under-test's late
    import resolves to our capture helpers."""
    stub = types.ModuleType("services.session_lifecycle")

    def _capture_emit(user_id, thread_id, state, **extra):
        emit_calls.append(
            {"user_id": user_id, "thread_id": thread_id, "state": state, **extra}
        )

    async def _ready_ok(pod_ip, pod_port, timeout_s):
        return True

    async def _bound(*a, **k):
        return True

    stub.emit = _capture_emit
    stub.wait_for_ready = _ready_ok
    stub.wait_for_binding = _bound
    monkeypatch.setitem(sys.modules, "services.session_lifecycle", stub)
    return stub


@pytest.mark.asyncio
async def test_idle_pool_attach_emits_provisioning_booting_ready(monkeypatch):
    """Warm pool fast-path: agent attaches instantly, /ready flips fast."""
    idle_agent = {
        "id": "a1",
        "hostname": "srw-agent-pool-1",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
    }

    async def _find_idle():
        return idle_agent

    fake_main = _install_fake_main(monkeypatch, _find_idle_persistent_agent=_find_idle)
    # First get_thread inside the lock — no prior binding.
    fake_main.postgres_db.get_thread = AsyncMock(
        return_value={"id": "t1", "agent_id": None}
    )

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid="t1",
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
    )

    states = [c["state"] for c in emit_calls]
    assert states == ["provisioning", "booting", "ready"], (
        f"expected provisioning→booting→ready, got {states}"
    )
    assert all(c["user_id"] == "u1" and c["thread_id"] == "t1" for c in emit_calls)


@pytest.mark.asyncio
async def test_fresh_pod_path_emits_full_sequence(monkeypatch):
    """No idle agent — provision a fresh pod, wait for binding, emit phases."""
    fake_main = _install_fake_main(monkeypatch)
    # No idle agent (default already None). After fresh-pod, the second
    # get_thread sees the binding.
    fake_main.postgres_db.get_thread = AsyncMock(
        side_effect=[
            {"id": "t1", "agent_id": None},
            {"id": "t1", "agent_id": "a-new"},
        ]
    )
    fake_main.postgres_db.get_agent = AsyncMock(
        return_value={"id": "a-new", "pod_ip": "10.0.0.9", "pod_port": 8001}
    )

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid="t1",
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
    )

    states = [c["state"] for c in emit_calls]
    assert states == ["provisioning", "booting", "ready"]


@pytest.mark.asyncio
async def test_fresh_pod_path_waits_when_agent_pod_marker_in_flight(monkeypatch):
    """A sibling prepare/create path may already have created the pod but not
    yet received the agent registration. Do not create a duplicate pod."""
    fake_main = _install_fake_main(monkeypatch)
    marker = {
        "status": "created",
        "pod_name": "srw-agent-s-existing",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    fake_main.postgres_db.get_thread = AsyncMock(
        side_effect=[
            {"id": "t1", "agent_id": None, "metadata": {"agent_pod": marker}},
            {"id": "t1", "agent_id": "a-existing"},
        ]
    )
    fake_main.postgres_db.get_agent = AsyncMock(
        return_value={"id": "a-existing", "pod_ip": "10.0.0.9", "pod_port": 8001}
    )
    fake_main._find_idle_persistent_agent = AsyncMock(return_value=None)

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid="t1",
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
    )

    fake_main._find_idle_persistent_agent.assert_not_awaited()
    fake_main.agent_provisioner.provision_agent.assert_not_awaited()
    states = [c["state"] for c in emit_calls]
    assert states == ["provisioning", "booting", "ready"]


@pytest.mark.asyncio
async def test_no_idle_and_provision_fails_emits_failed(monkeypatch):
    """No idle pool agent, fresh-pod creation also fails — emit ``failed``."""
    fake_main = _install_fake_main(monkeypatch)
    fake_main.agent_provisioner.provision_agent = AsyncMock(return_value=None)

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid="t1",
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
    )

    states = [c["state"] for c in emit_calls]
    assert states[0] == "provisioning"
    assert "failed" in states
    failed_emit = next(c for c in emit_calls if c["state"] == "failed")
    assert "reason" in failed_emit and failed_emit["reason"]


@pytest.mark.asyncio
async def test_grant_denied_fails_fast_without_pool_or_pod(monkeypatch):
    """A session whose resolved config exceeds the user's capability grants must
    fail fast: emit provisioning→failed carrying the violation, and attach NO
    pool agent / spawn NO dedicated pod. Otherwise a doomed pod boots, 403s at
    the workspace endpoint, exits, and the cockpit polls /connection until its
    ~5m40s ready timeout.
    docs/issues/session_permission_mode_grant_denied_ready_timeout.md
    """
    fake_main = _install_fake_main(monkeypatch)
    fake_main.postgres_db.get_thread = AsyncMock(
        return_value={"id": "t1", "agent_id": None, "user_id": "u1"}
    )
    fake_main._session_grant_violations = AsyncMock(
        return_value=["permission_mode: 'autonomous' exceeds the ceiling"]
    )
    # Spy that neither provisioning path is taken.
    fake_main._find_idle_persistent_agent = AsyncMock(return_value=None)

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid="t1",
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
    )

    states = [c["state"] for c in emit_calls]
    assert states == ["provisioning", "failed"], states
    failed = next(c for c in emit_calls if c["state"] == "failed")
    assert "capability grants" in failed["reason"]
    assert "autonomous" in failed["reason"]
    fake_main._find_idle_persistent_agent.assert_not_awaited()
    fake_main.agent_provisioner.provision_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_bound_exits_without_emitting_booting_or_ready(monkeypatch):
    """Race with /prepare or /resume: the other path owns the rest of the
    lifecycle, so this path emits only the up-front ``provisioning`` and
    returns without ``booting``/``ready`` duplicates."""
    fake_main = _install_fake_main(monkeypatch)
    # Already bound — duplicate-provision guard fires.
    fake_main.postgres_db.get_thread = AsyncMock(
        return_value={"id": "t1", "agent_id": "previously-bound"}
    )

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid="t1",
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
    )

    states = [c["state"] for c in emit_calls]
    assert states == ["provisioning"]
