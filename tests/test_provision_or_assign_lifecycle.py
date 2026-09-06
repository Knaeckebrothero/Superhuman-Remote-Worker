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
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from shared.pinned_session_identity import PinnedSessionBinding


THREAD_ID = "11111111-1111-4111-8111-111111111111"
RUNTIME_GENERATION = "22222222-2222-4222-8222-222222222222"
AGENT_ID = "33333333-3333-4333-8333-333333333333"
ATTACH_TOKEN = "44444444-4444-4444-8444-444444444444"
POD_UID = "55555555-5555-4555-8555-555555555555"


def _thread_row(
    *,
    lane: str = "pinned",
    status: str = "created",
    agent_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    return {
        "id": THREAD_ID,
        "execution_lane": lane,
        "status": status,
        "agent_id": agent_id,
        "runtime_generation": RUNTIME_GENERATION,
        "runtime_attach_token": ATTACH_TOKEN if agent_id else None,
        "runtime_retirement_token": None,
        "metadata": metadata or {},
    }


def _binding(
    *,
    status: str = "session",
    hostname: str = "srw-agent-pool-1",
    pod_uid: str = POD_UID,
    pod_ip: str = "10.0.0.5",
    pod_port: int = 8001,
) -> PinnedSessionBinding:
    return PinnedSessionBinding(
        thread_id=THREAD_ID,
        runtime_generation=RUNTIME_GENERATION,
        agent_id=AGENT_ID,
        runtime_attach_token=ATTACH_TOKEN,
        agent_hostname=hostname,
        pod_namespace="srw",
        pod_uid=pod_uid,
        pod_ip=pod_ip,
        pod_port=pod_port,
        agent_status=status,
    )


def _sequence_then_repeat(*rows):
    iterator = iter(rows)
    last = rows[-1]

    def _next(*_args, **_kwargs):
        nonlocal last
        try:
            last = next(iterator)
        except StopIteration:
            pass
        return last

    return _next


def _install_fake_main(monkeypatch, **overrides) -> types.ModuleType:
    """Inject a stub ``main`` module into sys.modules.

    The function under test does a late ``from main import …`` of a handful
    of singletons. We provide a stub so the import resolves without
    triggering the real ``main.py`` side-effect chain (license gate, agent
    provisioner connect, etc.).
    """
    stub = types.ModuleType("orchestrator.main")

    # Defaults — individual tests override via `overrides`.
    fake_db = MagicMock()
    fake_db.get_thread = AsyncMock(return_value=_thread_row())
    fake_db.resolve_datasources_for_thread = AsyncMock(return_value=[])
    fake_db.get_agent = AsyncMock(
        return_value={"id": AGENT_ID, "pod_ip": "10.0.0.5", "pod_port": 8001}
    )
    fake_db.get_pinned_session_binding = AsyncMock(return_value=_binding())
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
    # Pre-flight model-role transport check — same default; the endpoint-denied
    # test overrides it.
    stub._session_endpoint_violations = AsyncMock(return_value=[])
    stub._endpoint_violations_detail = (
        lambda v: "session cannot start — unusable model transport: " + "; ".join(v)
    )

    # Backend extraction + VM-aware readiness budget (session_create_on_vm.md).
    # Simple stand-ins — the real ones live in main.py behind the side-effect
    # chain this stub deliberately avoids importing.
    def _backend_from_override(co):
        if not isinstance(co, dict):
            return None
        ws = co.get("workspace")
        return ws.get("backend") if isinstance(ws, dict) else None

    stub._backend_from_override = _backend_from_override
    stub._session_ready_timeout_s = lambda backend: 960 if backend == "vm" else 180
    stub._await_protected_cloud_runtime_ready = AsyncMock(return_value=True)
    stub._thread_accepts_runtime = lambda row: bool(
        isinstance(row, dict)
        and row.get("status") in {"created", "active", "awaiting_user", "suspended"}
    )

    for k, v in overrides.items():
        setattr(stub, k, v)

    monkeypatch.setitem(sys.modules, "orchestrator.main", stub)
    return stub


def _install_fake_lifecycle_module(monkeypatch, emit_calls: list[dict]):
    """Stub ``services.session_lifecycle`` so the function-under-test's late
    import resolves to our capture helpers."""
    stub = types.ModuleType("orchestrator.services.session_lifecycle")

    def _capture_emit(user_id, thread_id, state, **extra):
        emit_calls.append(
            {"user_id": user_id, "thread_id": thread_id, "state": state, **extra}
        )

    async def _bound(*a, **k):
        return True

    stub.emit = _capture_emit
    stub.wait_for_ready = AsyncMock(return_value=True)
    stub.wait_for_binding = _bound
    monkeypatch.setitem(sys.modules, "orchestrator.services.session_lifecycle", stub)
    return stub


@pytest.mark.asyncio
async def test_create_path_refetch_treats_stateless_as_ready_without_lifecycle_error(
    monkeypatch,
):
    """A legitimate lane change neither provisions nor emits a false failure."""
    fake_main = _install_fake_main(monkeypatch)
    fake_main.postgres_db.get_thread = AsyncMock(
        return_value=_thread_row(lane="stateless")
    )
    fake_main._find_idle_persistent_agent = AsyncMock()
    fake_main._send_session_attach = AsyncMock()
    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        "u1",
        THREAD_ID,
        "session_base",
        {},
        [],
        None,
        runtime_generation=RUNTIME_GENERATION,
    )

    assert emit_calls == []
    fake_main._session_grant_violations.assert_not_awaited()
    fake_main._session_endpoint_violations.assert_not_awaited()
    fake_main._find_idle_persistent_agent.assert_not_awaited()
    fake_main._send_session_attach.assert_not_awaited()
    fake_main.agent_provisioner.provision_agent.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "thread_row",
    [
        None,
        _thread_row(lane="future-lane"),
    ],
)
async def test_create_path_refetch_fails_closed_for_missing_or_unknown_lane(
    monkeypatch, thread_row
):
    fake_main = _install_fake_main(monkeypatch)
    fake_main.postgres_db.get_thread = AsyncMock(return_value=thread_row)
    fake_main._find_idle_persistent_agent = AsyncMock()
    fake_main._send_session_attach = AsyncMock()
    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        "u1",
        THREAD_ID,
        "session_base",
        {},
        [],
        None,
        runtime_generation=RUNTIME_GENERATION,
    )

    if thread_row is None:
        # Unknown/missing lifecycle is not an authority for a new SSE frame.
        assert emit_calls == []
    else:
        assert [call["state"] for call in emit_calls] == ["failed"]
        assert "pinned provisioning" in emit_calls[0]["reason"]
    fake_main._session_grant_violations.assert_not_awaited()
    fake_main._session_endpoint_violations.assert_not_awaited()
    fake_main._find_idle_persistent_agent.assert_not_awaited()
    fake_main._send_session_attach.assert_not_awaited()
    fake_main.agent_provisioner.provision_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_pool_reservation_refetches_lane_before_pod_fallback(monkeypatch):
    idle_agent = {
        "id": AGENT_ID,
        "hostname": "srw-agent-pool-1",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
    }
    fake_main = _install_fake_main(monkeypatch)
    fake_main.postgres_db.get_thread = AsyncMock(
        side_effect=_sequence_then_repeat(
            _thread_row(),
            _thread_row(),
            _thread_row(),
            _thread_row(),
            _thread_row(),
            _thread_row(lane="stateless"),
        )
    )
    fake_main._find_idle_persistent_agent = AsyncMock(return_value=idle_agent)
    fake_main._send_session_attach = AsyncMock(return_value=False)
    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        "u1",
        THREAD_ID,
        "session_base",
        {},
        [],
        None,
        runtime_generation=RUNTIME_GENERATION,
    )

    assert [call["state"] for call in emit_calls] == ["provisioning"]
    fake_main._send_session_attach.assert_awaited_once()
    fake_main.agent_provisioner.provision_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_pool_attach_emits_provisioning_booting_ready(monkeypatch):
    """Warm pool fast-path: agent attaches instantly, /ready flips fast."""
    idle_agent = {
        "id": AGENT_ID,
        "hostname": "srw-agent-pool-1",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
    }

    async def _find_idle():
        return idle_agent

    fake_main = _install_fake_main(monkeypatch, _find_idle_persistent_agent=_find_idle)
    # First get_thread inside the lock — no prior binding.
    fake_main.postgres_db.get_thread = AsyncMock(return_value=_thread_row())

    emit_calls: list[dict] = []
    lifecycle = _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid=THREAD_ID,
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
        runtime_generation=RUNTIME_GENERATION,
    )

    states = [c["state"] for c in emit_calls]
    assert states == ["provisioning", "booting", "ready"], (
        f"expected provisioning→booting→ready, got {states}"
    )
    assert all(c["user_id"] == "u1" and c["thread_id"] == THREAD_ID for c in emit_calls)
    lifecycle.wait_for_ready.assert_awaited_once_with(
        "10.0.0.5",
        8001,
        180,
        require_protected_cloud=False,
        expected_session_identity_fingerprint=_binding().session_identity_fingerprint,
    )


@pytest.mark.parametrize(
    "changed_binding",
    [
        replace(_binding(), agent_hostname="successor-agent"),
        replace(_binding(), pod_uid="successor-pod-uid"),
        replace(_binding(), pod_ip="10.0.0.99"),
        replace(_binding(), pod_port=9001),
        replace(_binding(), agent_id="66666666-6666-4666-8666-666666666666"),
        replace(
            _binding(),
            runtime_attach_token="77777777-7777-4777-8777-777777777777",
        ),
    ],
    ids=["hostname", "pod_uid", "pod_ip", "pod_port", "agent_id", "attach"],
)
@pytest.mark.asyncio
async def test_create_path_never_emits_ready_for_a_changed_binding(
    monkeypatch,
    changed_binding,
):
    """A readiness result for physical target A cannot label B ready."""

    idle_agent = {
        "id": AGENT_ID,
        "hostname": "srw-agent-pool-1",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
    }
    fake_main = _install_fake_main(
        monkeypatch,
        _find_idle_persistent_agent=AsyncMock(return_value=idle_agent),
    )
    fake_main.postgres_db.get_thread = AsyncMock(return_value=_thread_row())
    original = _binding()
    fake_main.postgres_db.get_pinned_session_binding.side_effect = [
        original,
        changed_binding,
    ]
    emit_calls: list[dict] = []
    lifecycle = _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid=THREAD_ID,
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
        runtime_generation=RUNTIME_GENERATION,
    )

    assert [call["state"] for call in emit_calls] == ["provisioning", "booting"]
    lifecycle.wait_for_ready.assert_awaited_once_with(
        original.pod_ip,
        original.pod_port,
        180,
        require_protected_cloud=False,
        expected_session_identity_fingerprint=(original.session_identity_fingerprint),
    )


@pytest.mark.asyncio
async def test_create_path_allows_booting_status_lag_after_exact_ready_probe(
    monkeypatch,
):
    """The DB heartbeat state may lag the exact identity-bound /ready result."""

    idle_agent = {
        "id": AGENT_ID,
        "hostname": "srw-agent-pool-1",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
    }
    fake_main = _install_fake_main(
        monkeypatch,
        _find_idle_persistent_agent=AsyncMock(return_value=idle_agent),
    )
    fake_main.postgres_db.get_thread = AsyncMock(return_value=_thread_row())
    original = _binding(status="ready")
    fake_main.postgres_db.get_pinned_session_binding.side_effect = [
        original,
        replace(original, agent_status="booting"),
    ]
    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid=THREAD_ID,
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
        runtime_generation=RUNTIME_GENERATION,
    )

    assert [call["state"] for call in emit_calls] == [
        "provisioning",
        "booting",
        "ready",
    ]


@pytest.mark.asyncio
async def test_create_path_rejects_offline_status_after_exact_ready_probe(monkeypatch):
    """The final joined status gate may not publish an offline target ready."""

    idle_agent = {
        "id": AGENT_ID,
        "hostname": "srw-agent-pool-1",
        "pod_ip": "10.0.0.5",
        "pod_port": 8001,
    }
    fake_main = _install_fake_main(
        monkeypatch,
        _find_idle_persistent_agent=AsyncMock(return_value=idle_agent),
    )
    fake_main.postgres_db.get_thread = AsyncMock(return_value=_thread_row())
    original = _binding(status="ready")
    fake_main.postgres_db.get_pinned_session_binding.side_effect = [
        original,
        replace(original, agent_status="offline"),
    ]
    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid=THREAD_ID,
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
        runtime_generation=RUNTIME_GENERATION,
    )

    assert [call["state"] for call in emit_calls] == ["provisioning", "booting"]


@pytest.mark.asyncio
async def test_fresh_pod_path_emits_full_sequence(monkeypatch):
    """No idle agent — provision a fresh pod, wait for binding, emit phases."""
    fake_main = _install_fake_main(monkeypatch)
    # No idle agent (default already None). After fresh-pod, the second
    # get_thread sees the binding.
    fake_main.postgres_db.get_thread = AsyncMock(
        side_effect=_sequence_then_repeat(
            _thread_row(),
            _thread_row(),
            _thread_row(),
            _thread_row(),
            _thread_row(),
            _thread_row(),
            _thread_row(agent_id=AGENT_ID),
        )
    )
    fake_main.postgres_db.get_pinned_session_binding = AsyncMock(
        return_value=_binding(pod_ip="10.0.0.9")
    )

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid=THREAD_ID,
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
        runtime_generation=RUNTIME_GENERATION,
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
        side_effect=_sequence_then_repeat(
            _thread_row(metadata={"agent_pod": marker}),
            _thread_row(metadata={"agent_pod": marker}),
            _thread_row(metadata={"agent_pod": marker}),
            _thread_row(agent_id=AGENT_ID),
        )
    )
    fake_main.postgres_db.get_pinned_session_binding = AsyncMock(
        return_value=_binding(pod_ip="10.0.0.9")
    )
    fake_main._find_idle_persistent_agent = AsyncMock(return_value=None)

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid=THREAD_ID,
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
        runtime_generation=RUNTIME_GENERATION,
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

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid=THREAD_ID,
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
        runtime_generation=RUNTIME_GENERATION,
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
    knowledge-base/knowledge/issues/session_permission_mode_grant_denied_ready_timeout.md
    """
    fake_main = _install_fake_main(monkeypatch)
    fake_main.postgres_db.get_thread = AsyncMock(return_value=_thread_row())
    fake_main._session_grant_violations = AsyncMock(
        return_value=["permission_mode: 'autonomous' exceeds the ceiling"]
    )
    # Spy that neither provisioning path is taken.
    fake_main._find_idle_persistent_agent = AsyncMock(return_value=None)

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid=THREAD_ID,
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
        runtime_generation=RUNTIME_GENERATION,
    )

    states = [c["state"] for c in emit_calls]
    assert states == ["provisioning", "failed"], states
    failed = next(c for c in emit_calls if c["state"] == "failed")
    assert "capability grants" in failed["reason"]
    assert "autonomous" in failed["reason"]
    fake_main._find_idle_persistent_agent.assert_not_awaited()
    fake_main.agent_provisioner.provision_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_endpoint_denied_fails_fast_without_pool_or_pod(monkeypatch):
    """A session whose resolved config has an unusable model transport (e.g. the
    memory reranker riding an unreachable embedding endpoint) must fail fast:
    emit provisioning→failed with the real reason, spawn NO pod. Otherwise the
    agent crashes at startup, the workspace is released, and the cockpit hangs
    on /connection.
    knowledge-base/knowledge/issues/openrouter_auxiliary_crashes_session_via_memory_reranker.md
    """
    fake_main = _install_fake_main(monkeypatch)
    fake_main.postgres_db.get_thread = AsyncMock(return_value=_thread_row())
    fake_main._session_endpoint_violations = AsyncMock(
        return_value=[
            "embedding model 'qwen3-embedding-8b' (local) resolved but no "
            "EMBEDDING_BASE_URL — memory, KB, and the reranker cannot reach the "
            "embedding endpoint"
        ]
    )
    fake_main._find_idle_persistent_agent = AsyncMock(return_value=None)

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid=THREAD_ID,
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
        runtime_generation=RUNTIME_GENERATION,
    )

    states = [c["state"] for c in emit_calls]
    assert states == ["provisioning", "failed"], states
    failed = next(c for c in emit_calls if c["state"] == "failed")
    assert "unusable model transport" in failed["reason"]
    assert "reranker" in failed["reason"]
    fake_main._find_idle_persistent_agent.assert_not_awaited()
    fake_main.agent_provisioner.provision_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_ok_but_endpoint_check_runs(monkeypatch):
    """The endpoint pre-flight runs even when grants pass (it's a second gate)."""
    fake_main = _install_fake_main(monkeypatch)
    fake_main.postgres_db.get_thread = AsyncMock(return_value=_thread_row())
    fake_main._find_idle_persistent_agent = AsyncMock(return_value=None)

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid=THREAD_ID,
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
        runtime_generation=RUNTIME_GENERATION,
    )

    fake_main._session_endpoint_violations.assert_awaited_once()


@pytest.mark.asyncio
async def test_already_bound_exits_without_emitting_booting_or_ready(monkeypatch):
    """Race with /prepare or /resume: the other path owns the rest of the
    lifecycle, so this path emits only the up-front ``provisioning`` and
    returns without ``booting``/``ready`` duplicates."""
    fake_main = _install_fake_main(monkeypatch)
    # Already bound — duplicate-provision guard fires.
    fake_main.postgres_db.get_thread = AsyncMock(
        return_value=_thread_row(agent_id=AGENT_ID)
    )

    emit_calls: list[dict] = []
    _install_fake_lifecycle_module(monkeypatch, emit_calls)

    from orchestrator.services.provision_or_assign import provision_or_assign

    await provision_or_assign(
        uid="u1",
        tid=THREAD_ID,
        cfg="persistent_defaults",
        co={},
        pids=[],
        ds_ids=None,
        runtime_generation=RUNTIME_GENERATION,
    )

    states = [c["state"] for c in emit_calls]
    assert states == ["provisioning"]
