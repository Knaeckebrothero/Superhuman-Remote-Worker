import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.database.postgres import PostgresDB
from orchestrator.services.vm_readiness import VMReadinessService
from orchestrator.services.ssh_helpers import orchestrator_can_reach


GENERATION = "11111111-1111-4111-8111-111111111111"


def test_same_cluster_reachability_ignores_address_class(monkeypatch):
    monkeypatch.setenv("VM_MODE", "same-cluster")
    assert orchestrator_can_reach("100.64.23.180") is True


def candidate(entity_id="11111111-1111-4111-8111-111111111112", **vm):
    return {
        "entity_id": entity_id,
        "user_id": "11111111-1111-4111-8111-111111111113",
        "vm": {
            "status": "created",
            "provision_generation": GENERATION,
            **vm,
        },
    }


class FakeDB:
    def __init__(self, jobs=(), threads=(), ready_jobs=(), ready_threads=()):
        self.jobs = list(jobs)
        self.threads = list(threads)
        self.ready_jobs = list(ready_jobs)
        self.ready_threads = list(ready_threads)
        self.calls = []

    async def list_job_vm_readiness_candidates(self, *, ready=False):
        self.calls.append(("job", ready))
        return list(self.ready_jobs if ready else self.jobs)

    async def list_thread_vm_readiness_candidates(self, *, ready=False):
        self.calls.append(("thread", ready))
        return list(self.ready_threads if ready else self.threads)


class FakeProvisioner:
    def __init__(self, status, *, write_result=True):
        self.status = status
        self.write_result = write_result
        self.writes = []
        self.queries = []

    async def query_status(self, entity_id, *, entity_type="job"):
        self.queries.append((entity_type, entity_id))
        value = (
            self.status(entity_id, entity_type)
            if callable(self.status)
            else self.status
        )
        if asyncio.iscoroutine(value):
            value = await value
        return value

    async def _set_context_if_generation(
        self,
        entity_type,
        entity_id,
        generation,
        updates,
        *,
        require_status_not_ready=False,
    ):
        self.writes.append(
            (entity_type, entity_id, generation, updates, require_status_not_ready)
        )
        return self.write_result


@pytest.fixture
def successful_ssh(monkeypatch):
    tcp = AsyncMock(return_value=True)
    auth = AsyncMock(return_value=(True, 1, ""))
    seed = AsyncMock()
    monkeypatch.setattr("orchestrator.services.vm_readiness.probe_workspace_ssh", tcp)
    monkeypatch.setattr("orchestrator.services.vm_readiness.wait_for_agent_ssh", auth)
    monkeypatch.setattr(
        "orchestrator.services.vm_readiness.seed_ide_config_for_user", seed
    )
    monkeypatch.setattr(
        "orchestrator.services.vm_readiness.resolve_ssh_key_path", lambda: "/key"
    )
    return tcp, auth, seed


@pytest.mark.asyncio
async def test_first_probe_success_promotes_once_with_cas(successful_ssh):
    row = candidate()
    db = FakeDB(jobs=[row])
    provisioner = FakeProvisioner(
        {
            "ready": True,
            "pod_ip": "10.42.0.10",
            "phase": "Running",
            "active_pod_uid": "pod-1",
        }
    )
    trigger = MagicMock()
    service = VMReadinessService(db, provisioner, trigger_dispatch=trigger)

    await service.run_cycle()

    entity_type, _, generation, updates, cas = provisioner.writes[-1]
    assert (entity_type, generation, cas) == ("job", GENERATION, True)
    assert updates["status"] == "ready"
    assert updates["ssh_host"] == updates["pod_ip"] == "10.42.0.10"
    assert updates["ssh_port"] == 22
    assert updates["active_pod_uid"] == "pod-1"
    assert updates["ssh_ready_source"] == "provisioner_probe"
    assert updates["ssh_registration_id"]
    assert updates["ssh_probe_error"] is None
    assert updates["recovering"] is False
    successful_ssh[2].assert_awaited_once()
    trigger.assert_called_once()
    assert len(provisioner.writes) == 1


@pytest.mark.asyncio
async def test_transient_failure_records_pending_attempt(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.services.vm_readiness.probe_workspace_ssh",
        AsyncMock(return_value=False),
    )
    row = candidate(ssh_probe_attempts=2)
    provisioner = FakeProvisioner(
        {
            "ready": True,
            "pod_ip": "10.42.0.11",
            "active_pod_uid": "pod-transient",
            "phase": "Running",
        }
    )
    await VMReadinessService(
        FakeDB(jobs=[row]), provisioner, trigger_dispatch=lambda: None
    ).run_cycle()
    updates = provisioner.writes[-1][3]
    assert updates["status"] == "ssh_pending"
    assert updates["ssh_probe_attempts"] == 3
    assert "TCP" in updates["ssh_probe_error"]
    assert provisioner.writes[-1][4] is True


@pytest.mark.asyncio
async def test_reprobe_controller_blip_preserves_ready_row(successful_ssh):
    row = candidate(status="ready", pod_ip="10.42.0.20", active_pod_uid="pod-old")
    provisioner = FakeProvisioner(None)

    await VMReadinessService(
        FakeDB(ready_jobs=[row]), provisioner, trigger_dispatch=lambda: None
    ).run_cycle()

    assert provisioner.writes == []
    successful_ssh[0].assert_not_awaited()


@pytest.mark.asyncio
async def test_reprobe_probe_failure_preserves_ready_row(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.services.vm_readiness.probe_workspace_ssh",
        AsyncMock(return_value=False),
    )
    row = candidate(status="ready", pod_ip="10.42.0.20", active_pod_uid="pod-old")
    provisioner = FakeProvisioner(
        {
            "ready": True,
            "pod_ip": "10.42.0.21",
            "active_pod_uid": "pod-new",
            "phase": "Running",
        }
    )

    await VMReadinessService(
        FakeDB(ready_jobs=[row]), provisioner, trigger_dispatch=lambda: None
    ).run_cycle()

    assert provisioner.writes == []


@pytest.mark.asyncio
async def test_not_found_marks_ready_vm_unreachable(successful_ssh):
    row = candidate(status="ready", pod_ip="10.42.0.20", active_pod_uid="pod-old")
    provisioner = FakeProvisioner({"status": "not_found"})

    await VMReadinessService(
        FakeDB(ready_jobs=[row]), provisioner, trigger_dispatch=lambda: None
    ).run_cycle()

    assert provisioner.writes[-1][3] == {
        "status": "ssh_unreachable",
        "ssh_probe_error": "vm not found",
    }


@pytest.mark.asyncio
async def test_stopped_guest_becomes_unreachable(monkeypatch):
    tcp = AsyncMock()
    monkeypatch.setattr("orchestrator.services.vm_readiness.probe_workspace_ssh", tcp)
    provisioner = FakeProvisioner(
        {"ready": False, "phase": "Succeeded", "pod_ip": "10.42.0.12"}
    )
    await VMReadinessService(
        FakeDB(jobs=[candidate()]), provisioner, trigger_dispatch=lambda: None
    ).run_cycle()
    assert provisioner.writes[-1][3] == {
        "status": "ssh_unreachable",
        "ssh_probe_error": "vm stopped",
    }
    tcp.assert_not_awaited()


@pytest.mark.asyncio
async def test_ip_change_reprobes_ready_vm(successful_ssh):
    row = candidate(status="ready", pod_ip="10.42.0.20", active_pod_uid="pod-old")
    provisioner = FakeProvisioner(
        {
            "ready": True,
            "pod_ip": "10.42.0.21",
            "phase": "Running",
            "active_pod_uid": "pod-new",
        }
    )
    service = VMReadinessService(
        FakeDB(ready_jobs=[row]), provisioner, trigger_dispatch=lambda: None
    )
    await service.run_cycle()
    assert provisioner.writes[-1][3]["ssh_host"] == "10.42.0.21"
    assert provisioner.writes[-1][4] is False


@pytest.mark.asyncio
async def test_reprobe_unchanged_identity_is_noop(successful_ssh):
    row = candidate(status="ready", pod_ip="10.42.0.20", active_pod_uid="pod-old")
    provisioner = FakeProvisioner(
        {
            "ready": True,
            "pod_ip": "10.42.0.20",
            "phase": "Running",
            "active_pod_uid": "pod-old",
        }
    )

    await VMReadinessService(
        FakeDB(ready_jobs=[row]), provisioner, trigger_dispatch=lambda: None
    ).run_cycle()

    assert provisioner.writes == []
    successful_ssh[0].assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_controller_generation_is_ignored(successful_ssh):
    provisioner = FakeProvisioner(
        {
            "ready": True,
            "pod_ip": "10.42.0.21",
            "active_pod_uid": "pod-new",
            "provision_generation": "33333333-3333-4333-8333-333333333333",
        }
    )

    await VMReadinessService(
        FakeDB(jobs=[candidate()]), provisioner, trigger_dispatch=lambda: None
    ).run_cycle()

    assert provisioner.writes == []
    successful_ssh[0].assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_promotion_suppresses_seed_and_dispatch(successful_ssh):
    provisioner = FakeProvisioner(
        {
            "ready": True,
            "pod_ip": "10.42.0.22",
            "phase": "Running",
            "active_pod_uid": "pod-new",
        },
        write_result=False,
    )
    trigger = MagicMock()

    await VMReadinessService(
        FakeDB(jobs=[candidate()]), provisioner, trigger_dispatch=trigger
    ).run_cycle()

    assert len(provisioner.writes) == 1
    successful_ssh[2].assert_not_awaited()
    trigger.assert_not_called()


@pytest.mark.asyncio
async def test_backoff_skips_candidate(successful_ssh):
    row = candidate()
    provisioner = FakeProvisioner({"ready": False})
    service = VMReadinessService(
        FakeDB(jobs=[row]), provisioner, trigger_dispatch=lambda: None
    )
    key = ("job", row["entity_id"], GENERATION)
    service._retry_after[key] = asyncio.get_running_loop().time() + 60

    await service.run_cycle()

    assert provisioner.queries == []


@pytest.mark.asyncio
async def test_inflight_key_deduplicates_candidate(successful_ssh):
    row = candidate()
    provisioner = FakeProvisioner({"ready": False})
    service = VMReadinessService(
        FakeDB(jobs=[row]), provisioner, trigger_dispatch=lambda: None
    )
    service._inflight.add(("job", row["entity_id"], GENERATION))

    await service.run_cycle()

    assert provisioner.queries == []


@pytest.mark.asyncio
async def test_retry_state_pruned_for_non_candidates(successful_ssh):
    service = VMReadinessService(
        FakeDB(), FakeProvisioner({"ready": False}), trigger_dispatch=lambda: None
    )
    stale = ("job", "11111111-1111-4111-8111-111111111199", GENERATION)
    service._failures[stale] = 4
    service._retry_after[stale] = 999999999.0

    await service.run_cycle()

    assert service._failures == {}
    assert service._retry_after == {}


@pytest.mark.asyncio
async def test_new_leader_rearms_from_db_rows(successful_ssh):
    db = FakeDB(jobs=[candidate()])
    status = {
        "ready": True,
        "pod_ip": "10.42.0.30",
        "phase": "Running",
        "active_pod_uid": "pod",
    }
    first = FakeProvisioner(status)
    second = FakeProvisioner(status)
    await VMReadinessService(db, first, trigger_dispatch=lambda: None).run_cycle()
    await VMReadinessService(db, second, trigger_dispatch=lambda: None).run_cycle()
    assert first.writes and second.writes
    assert db.calls.count(("job", False)) == 2


@pytest.mark.asyncio
async def test_concurrency_cap(successful_ssh):
    active = 0
    peak = 0

    async def status(_entity_id, _entity_type):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"ready": False, "phase": "Starting"}

    rows = [candidate(f"11111111-1111-4111-8111-{index:012d}") for index in range(10)]
    service = VMReadinessService(
        FakeDB(jobs=rows),
        FakeProvisioner(status),
        trigger_dispatch=lambda: None,
        max_inflight=3,
    )
    await service.run_cycle()
    assert peak == 3


@pytest.mark.asyncio
async def test_thread_entity_promotes_without_dispatch(successful_ssh):
    provisioner = FakeProvisioner(
        {
            "ready": True,
            "pod_ip": "10.42.0.40",
            "phase": "Running",
            "active_pod_uid": "pod",
        }
    )
    trigger = MagicMock()
    await VMReadinessService(
        FakeDB(threads=[candidate()]), provisioner, trigger_dispatch=trigger
    ).run_cycle()
    assert provisioner.writes[-1][0] == "thread"
    trigger.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "status_expression"),
    [
        ("merge_vm_context_if_provision_generation", "context->'vm'->>'status'"),
        (
            "merge_thread_vm_context_if_provision_generation",
            "metadata->'vm'->>'status'",
        ),
    ],
)
async def test_database_generation_merge_includes_ready_cas(
    method_name, status_expression
):
    executed = []

    class Connection:
        async def execute(self, query, *args):
            executed.append((query, args))
            return "UPDATE 1"

    db = PostgresDB("postgresql://unused")

    @asynccontextmanager
    async def acquire():
        yield Connection()

    db.acquire = acquire
    method = getattr(db, method_name)
    assert await method(
        "11111111-1111-4111-8111-111111111112",
        GENERATION,
        {"status": "ready"},
        require_status_not_ready=True,
    )
    query, args = executed[0]
    assert status_expression in query
    assert "IS DISTINCT FROM 'ready'" in query
    assert args[-1] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "terminal_predicate"),
    [
        (
            "list_job_vm_readiness_candidates",
            "jobs.status NOT IN ('completed','failed','cancelled')",
        ),
        (
            "list_thread_vm_readiness_candidates",
            "threads.status <> 'ended' AND threads.ended_at IS NULL",
        ),
    ],
)
async def test_readiness_queries_filter_finished_fake_row(
    method_name, terminal_predicate
):
    stale_row = candidate(status="created")

    class Connection:
        async def fetch(self, query):
            return [] if terminal_predicate in query else [stale_row]

    db = PostgresDB("postgresql://unused")

    @asynccontextmanager
    async def acquire():
        yield Connection()

    db.acquire = acquire
    assert await getattr(db, method_name)() == []
