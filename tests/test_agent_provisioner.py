"""Tests for orchestrator/services/agent_provisioner.py.

Covers the new pool management and reservation-aware provisioning:
  - reap_pods(): unified GC of completed / stale / unstartable pods
  - scale_down_idle(): terminate excess idle pods above MIN_AGENTS
  - _try_evict_for_reservation(): evict idle other-purpose pod at capacity
  - provision_agent(): reservation-aware capacity check with eviction
  - Pool fallback paths are tested indirectly via the provisioner methods
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.agent_provisioner import AgentProvisioner


# =============================================================================
# Helpers
# =============================================================================


def _make_provisioner(
    max_agents=10,
    min_agents=2,
    reserved_session_slots=0,
    reserved_job_slots=0,
    k8s_available=True,
):
    """Create an AgentProvisioner with mocked K8s API and DB."""
    p = AgentProvisioner()
    p._k8s_available = k8s_available
    p._namespace = "test-ns"
    p._max_agents = max_agents
    p._min_agents = min_agents
    p._agent_buffer = 0
    p._reserved_session_slots = reserved_session_slots
    p._reserved_job_slots = reserved_job_slots
    p._core_api = MagicMock()
    p._agent_image = "test-image:latest"
    p._configmap_name = "srw-config"
    p._secret_name = "srw-secrets"

    p._db = AsyncMock()
    p._db.acquire = MagicMock()
    mock_conn = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    p._db.acquire.return_value = mock_ctx
    return p, mock_conn


def _make_pod(
    name,
    phase="Running",
    purpose="job",
    thread_id=None,
    age_seconds=10,
    waiting_reason=None,
    agent_terminated_at=None,
):
    """Create a mock K8s pod object.

    ``age_seconds`` controls ``metadata.creation_timestamp`` (now minus that).
    ``waiting_reason`` attaches a single container_status with
    ``state.waiting.reason``. When None, container_statuses is empty — which
    matches the shape of pods that haven't progressed to container creation.
    ``agent_terminated_at`` attaches a container_status named ``"agent"``
    with ``state.terminated.finished_at`` set to (now - that many seconds),
    simulating an agent container that has crashed while a sidecar keeps
    the pod in phase=Running.
    """
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.labels = {
        "srw/managed-by": "agent-provisioner",
        "srw/purpose": purpose,
    }
    if thread_id:
        pod.metadata.labels["srw/thread-id"] = thread_id[:12]
    pod.metadata.creation_timestamp = datetime.now(timezone.utc) - timedelta(
        seconds=age_seconds
    )
    pod.status.phase = phase
    pod.status.pod_ip = "10.0.0.1"
    statuses = []
    if waiting_reason is not None:
        cs = MagicMock()
        cs.name = "agent"
        cs.state.waiting.reason = waiting_reason
        cs.state.terminated = None
        statuses.append(cs)
    if agent_terminated_at is not None:
        cs = MagicMock()
        cs.name = "agent"
        cs.state.waiting = None
        cs.state.terminated.finished_at = datetime.now(timezone.utc) - timedelta(
            seconds=agent_terminated_at
        )
        statuses.append(cs)
    pod.status.container_statuses = statuses
    pod.status.init_container_statuses = []
    return pod


async def _fake_to_thread(fn, *args, **kwargs):
    """Execute a function synchronously (replaces asyncio.to_thread in tests)."""
    return fn(*args, **kwargs)


# =============================================================================
# TestReapPods
# =============================================================================


class TestReapPods:
    """Tests for reap_pods() — unified completed/stale/unstartable dispatcher."""

    @pytest.mark.asyncio
    async def test_noop_when_k8s_not_available(self):
        p, _ = _make_provisioner(k8s_available=False)
        result = await p.reap_pods()
        assert result == {"completed": 0, "crashed": 0, "stale": 0, "unstartable": 0}

    @pytest.mark.asyncio
    async def test_noop_when_no_reapable_pods(self):
        p, conn = _make_provisioner()
        conn.fetch.return_value = []
        pods_result = MagicMock()
        pods_result.items = [_make_pod("srw-agent-j-healthy")]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result == {"completed": 0, "crashed": 0, "stale": 0, "unstartable": 0}
        assert p._core_api.delete_namespaced_pod.call_count == 0

    @pytest.mark.asyncio
    async def test_reaps_completed_pods(self):
        p, conn = _make_provisioner()
        conn.fetch.return_value = []
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod("srw-agent-j-ok", phase="Succeeded"),
            _make_pod("srw-agent-j-bad", phase="Failed"),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result["completed"] == 2
        assert result["stale"] == 0
        assert result["unstartable"] == 0
        assert p._core_api.delete_namespaced_pod.call_count == 2

    @pytest.mark.asyncio
    async def test_reaps_stale_running_pods(self):
        p, conn = _make_provisioner()
        conn.fetch.return_value = [
            {"hostname": "srw-agent-j-stale"},
        ]
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod("srw-agent-j-stale"),
            _make_pod("srw-agent-j-healthy"),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result["stale"] == 1
        assert result["completed"] == 0
        assert result["unstartable"] == 0
        assert p._core_api.delete_namespaced_pod.call_count == 1

    @pytest.mark.asyncio
    async def test_reaps_unstartable_pending_pods_past_grace(self):
        p, conn = _make_provisioner()
        conn.fetch.return_value = []
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod(
                "srw-agent-j-stuck",
                phase="Pending",
                age_seconds=600,
                waiting_reason="CreateContainerConfigError",
            ),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result["unstartable"] == 1
        assert p._core_api.delete_namespaced_pod.call_count == 1

    @pytest.mark.asyncio
    async def test_preserves_unstartable_pending_pods_within_grace(self):
        p, conn = _make_provisioner()
        conn.fetch.return_value = []
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod(
                "srw-agent-j-young",
                phase="Pending",
                age_seconds=30,
                waiting_reason="CreateContainerConfigError",
            ),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result["unstartable"] == 0
        assert p._core_api.delete_namespaced_pod.call_count == 0

    @pytest.mark.asyncio
    async def test_preserves_pending_pods_with_benign_waiting_reason(self):
        p, conn = _make_provisioner()
        conn.fetch.return_value = []
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod(
                "srw-agent-j-pulling",
                phase="Pending",
                age_seconds=600,
                waiting_reason="ContainerCreating",
            ),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result == {"completed": 0, "crashed": 0, "stale": 0, "unstartable": 0}
        assert p._core_api.delete_namespaced_pod.call_count == 0

    @pytest.mark.asyncio
    async def test_reaps_crashed_running_pod_past_grace(self):
        # Sidecar keeps the pod in phase=Running even though agent died —
        # this is the case the original sidecar-pinned bug exposed.
        p, conn = _make_provisioner()
        conn.fetch.return_value = []
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod(
                "srw-agent-j-crashed",
                phase="Running",
                agent_terminated_at=120,
            ),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result["crashed"] == 1
        assert p._core_api.delete_namespaced_pod.call_count == 1

    @pytest.mark.asyncio
    async def test_preserves_crashed_pod_within_grace(self):
        p, conn = _make_provisioner()
        conn.fetch.return_value = []
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod(
                "srw-agent-j-just-crashed",
                phase="Running",
                agent_terminated_at=10,
            ),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result["crashed"] == 0
        assert p._core_api.delete_namespaced_pod.call_count == 0

    @pytest.mark.asyncio
    async def test_mixed_pod_set_reaps_all_three_categories(self):
        p, conn = _make_provisioner()
        conn.fetch.return_value = [{"hostname": "srw-agent-j-zombie"}]
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod("srw-agent-j-done", phase="Succeeded"),
            _make_pod("srw-agent-j-zombie"),  # Running, in offline DB list
            _make_pod(
                "srw-agent-j-stuck",
                phase="Pending",
                age_seconds=600,
                waiting_reason="ImagePullBackOff",
            ),
            _make_pod("srw-agent-j-fine"),  # Running, healthy — skip
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.reap_pods()
        assert result == {"completed": 1, "crashed": 0, "stale": 1, "unstartable": 1}
        assert p._core_api.delete_namespaced_pod.call_count == 3


# =============================================================================
# TestScaleDownIdle
# =============================================================================


class TestScaleDownIdle:
    """Tests for scale_down_idle()."""

    @pytest.mark.asyncio
    async def test_noop_when_k8s_not_available(self):
        p, _ = _make_provisioner(k8s_available=False)
        assert await p.scale_down_idle() == 0

    @pytest.mark.asyncio
    async def test_noop_when_min_agents_zero(self):
        p, _ = _make_provisioner(min_agents=0)
        assert await p.scale_down_idle() == 0

    @pytest.mark.asyncio
    async def test_noop_when_at_or_below_min(self):
        p, conn = _make_provisioner(min_agents=2)

        # active_count returns 2 (at floor)
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod("pod-1"),
            _make_pod("pod-2"),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.scale_down_idle()

        assert result == 0

    @pytest.mark.asyncio
    async def test_scales_down_excess_idle(self):
        p, conn = _make_provisioner(min_agents=2)

        # active_count returns 6 (4 excess)
        pods_result = MagicMock()
        pods_result.items = [_make_pod(f"pod-{i}") for i in range(6)]
        p._core_api.list_namespaced_pod.return_value = pods_result

        # DB has 4 idle agents
        conn.fetch.return_value = [
            {"id": f"agent-{i}", "hostname": f"pod-{i}"} for i in range(4)
        ]

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.scale_down_idle(max_terminate=2)

        # Should terminate max 2 (max_terminate cap)
        assert result == 2
        assert p._core_api.delete_namespaced_pod.call_count == 2

    @pytest.mark.asyncio
    async def test_respects_max_terminate(self):
        p, conn = _make_provisioner(min_agents=2)

        pods_result = MagicMock()
        pods_result.items = [_make_pod(f"pod-{i}") for i in range(10)]
        p._core_api.list_namespaced_pod.return_value = pods_result

        conn.fetch.return_value = [
            {"id": f"agent-{i}", "hostname": f"pod-{i}"} for i in range(8)
        ]

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.scale_down_idle(max_terminate=1)

        assert result == 1

    @pytest.mark.asyncio
    async def test_noop_when_no_idle_agents(self):
        p, conn = _make_provisioner(min_agents=2)

        # 6 active pods but no idle agents in DB
        pods_result = MagicMock()
        pods_result.items = [_make_pod(f"pod-{i}") for i in range(6)]
        p._core_api.list_namespaced_pod.return_value = pods_result
        conn.fetch.return_value = []

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.scale_down_idle()

        assert result == 0


# =============================================================================
# TestTryEvictForReservation
# =============================================================================


class TestTryEvictForReservation:
    """Tests for _try_evict_for_reservation()."""

    @pytest.mark.asyncio
    async def test_returns_false_without_reservation(self):
        p, conn = _make_provisioner(reserved_session_slots=0, reserved_job_slots=0)
        result = await p._try_evict_for_reservation(
            "session", {"job": 8, "session": 2, "total": 10}
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_evicts_idle_job_for_session(self):
        p, conn = _make_provisioner(reserved_session_slots=2)

        # DB: one idle job agent
        conn.fetch.return_value = [
            {"id": "agent-idle-job", "hostname": "srw-agent-j-idle"},
        ]

        # K8s: one job pod matching the idle agent
        pods_result = MagicMock()
        pods_result.items = [_make_pod("srw-agent-j-idle", purpose="job")]
        p._core_api.list_namespaced_pod.return_value = pods_result

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p._try_evict_for_reservation(
                "session", {"job": 8, "session": 2, "total": 10}
            )

        assert result is True
        p._core_api.delete_namespaced_pod.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_when_no_idle_other_type(self):
        p, conn = _make_provisioner(reserved_session_slots=2)

        # No idle agents in DB
        conn.fetch.return_value = []

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p._try_evict_for_reservation(
                "session", {"job": 8, "session": 2, "total": 10}
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_evicts_idle_session_for_job(self):
        p, conn = _make_provisioner(reserved_job_slots=1)

        conn.fetch.return_value = [
            {"id": "agent-idle-session", "hostname": "srw-agent-s-idle"},
        ]

        pods_result = MagicMock()
        pods_result.items = [_make_pod("srw-agent-s-idle", purpose="session")]
        p._core_api.list_namespaced_pod.return_value = pods_result

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p._try_evict_for_reservation(
                "job", {"job": 2, "session": 8, "total": 10}
            )

        assert result is True


# =============================================================================
# TestProvisionWithEviction
# =============================================================================


class TestProvisionWithEviction:
    """Tests for provision_agent() with reservation-aware eviction."""

    @pytest.mark.asyncio
    async def test_at_capacity_evicts_for_session(self):
        p, conn = _make_provisioner(max_agents=10, reserved_session_slots=2)

        # active_counts: at capacity (10/10)
        pods_list = MagicMock()
        pods_list.items = [_make_pod(f"pod-{i}") for i in range(10)]

        # For eviction: one idle job agent matching a pod
        conn.fetch.side_effect = [
            # First fetch: idle agents for eviction
            [{"id": "agent-idle", "hostname": "pod-0"}],
        ]

        # After eviction, need to list pods again for eviction check
        eviction_pods = MagicMock()
        eviction_pods.items = [_make_pod("pod-0", purpose="job")]

        def list_pods_side_effect(**kwargs):
            selector = kwargs.get("label_selector", "")
            if "srw/purpose=job" in selector:
                return eviction_pods
            return pods_list

        p._core_api.list_namespaced_pod.side_effect = list_pods_side_effect
        p._core_api.create_namespaced_pod.return_value = MagicMock()

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.provision_agent(
                purpose="session", thread_id="test-thread-id"
            )

        # Should have evicted one pod and created a new one
        assert result is not None
        p._core_api.delete_namespaced_pod.assert_called_once()
        p._core_api.create_namespaced_pod.assert_called_once()

    @pytest.mark.asyncio
    async def test_at_capacity_no_eviction_without_reservation(self):
        p, conn = _make_provisioner(max_agents=10, reserved_session_slots=0)

        pods_list = MagicMock()
        pods_list.items = [_make_pod(f"pod-{i}") for i in range(10)]
        p._core_api.list_namespaced_pod.return_value = pods_list

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.provision_agent(
                purpose="session", thread_id="test-thread-id"
            )

        assert result is None
        p._core_api.create_namespaced_pod.assert_not_called()

    @pytest.mark.asyncio
    async def test_under_capacity_no_eviction(self):
        p, conn = _make_provisioner(max_agents=10, reserved_session_slots=2)

        # Only 5 active pods — well under capacity
        pods_list = MagicMock()
        pods_list.items = [_make_pod(f"pod-{i}") for i in range(5)]
        p._core_api.list_namespaced_pod.return_value = pods_list
        p._core_api.create_namespaced_pod.return_value = MagicMock()

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.provision_agent(
                purpose="session", thread_id="test-thread-id"
            )

        assert result is not None
        # delete should NOT have been called (no eviction needed)
        p._core_api.delete_namespaced_pod.assert_not_called()


# =============================================================================
# TestProvisionBasic
# =============================================================================


class TestProvisionBasic:
    """Tests for basic provision_agent() behaviour."""

    @pytest.mark.asyncio
    async def test_returns_none_when_k8s_not_available(self):
        p, _ = _make_provisioner(k8s_available=False)
        result = await p.provision_agent(purpose="job")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_pod_name_on_success(self):
        p, conn = _make_provisioner()

        pods_list = MagicMock()
        pods_list.items = []
        p._core_api.list_namespaced_pod.return_value = pods_list
        p._core_api.create_namespaced_pod.return_value = MagicMock()

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.provision_agent(purpose="job")

        assert result is not None
        assert result.startswith("srw-agent-j-")

    @pytest.mark.asyncio
    async def test_reservation_blocks_job_at_ceiling(self):
        p, conn = _make_provisioner(max_agents=10, reserved_session_slots=2)

        # 8 job pods, 0 session → job at ceiling (10-2=8)
        pods_list = MagicMock()
        pods_list.items = [_make_pod(f"pod-{i}", purpose="job") for i in range(8)]
        p._core_api.list_namespaced_pod.return_value = pods_list

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.provision_agent(purpose="job")

        assert result is None

    @pytest.mark.asyncio
    async def test_handles_409_conflict(self):
        p, conn = _make_provisioner()

        pods_list = MagicMock()
        pods_list.items = []
        p._core_api.list_namespaced_pod.return_value = pods_list

        exc = Exception("Conflict")
        exc.status = 409
        p._core_api.create_namespaced_pod.side_effect = exc

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.provision_agent(purpose="job")

        # 409 is treated as success (pod already exists)
        assert result is not None


# =============================================================================
# TestActiveCountsByPurpose
# =============================================================================


class TestActiveCountsByPurpose:
    """Tests for active_counts_by_purpose()."""

    @pytest.mark.asyncio
    async def test_counts_running_pods_by_purpose(self):
        p, _ = _make_provisioner()

        pods_result = MagicMock()
        pods_result.items = [
            _make_pod("j1", purpose="job"),
            _make_pod("j2", purpose="job"),
            _make_pod("s1", purpose="session"),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.active_counts_by_purpose()

        assert result == {"job": 2, "session": 1, "total": 3}

    @pytest.mark.asyncio
    async def test_excludes_succeeded_and_failed(self):
        p, _ = _make_provisioner()

        pods_result = MagicMock()
        pods_result.items = [
            _make_pod("j1", purpose="job"),
            _make_pod("j2", phase="Succeeded", purpose="job"),
            _make_pod("s1", phase="Failed", purpose="session"),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result

        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.active_counts_by_purpose()

        assert result == {"job": 1, "session": 0, "total": 1}

    @pytest.mark.asyncio
    async def test_returns_zeros_when_k8s_not_available(self):
        p, _ = _make_provisioner(k8s_available=False)
        result = await p.active_counts_by_purpose()
        assert result == {"job": 0, "session": 0, "total": 0}

    @pytest.mark.asyncio
    async def test_excludes_pods_with_terminated_agent_container(self):
        # Sidecar-pinned crashed pods stay in phase=Running but must not
        # count against MAX_AGENTS, otherwise the ceiling locks up.
        p, _ = _make_provisioner()
        pods_result = MagicMock()
        pods_result.items = [
            _make_pod("j1", purpose="job"),
            _make_pod("j2", purpose="job", agent_terminated_at=5),
            _make_pod("s1", purpose="session", agent_terminated_at=200),
        ]
        p._core_api.list_namespaced_pod.return_value = pods_result
        with patch(
            "orchestrator.services.agent_provisioner.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            result = await p.active_counts_by_purpose()
        assert result == {"job": 1, "session": 0, "total": 1}
