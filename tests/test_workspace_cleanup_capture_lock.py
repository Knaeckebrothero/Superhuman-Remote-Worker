"""Capture pending cleanup without reacquiring its physical mutation lock."""

import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from kubernetes.client.exceptions import ApiException

from orchestrator.services.workspace_lifecycle import WorkspaceOwner
from tests.test_non_pinned_workspace_lifecycle_real_postgres import (
    _create_settled_authoritative_runtime,
    _schema_applied,  # noqa: F401
    db as _postgres_db_fixture,
    pg_dsn,  # noqa: F401
)
from tests.test_workspace_cleanup_retry_real_postgres import _absent_pod_provisioner

db = _postgres_db_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_kind", ["job", "thread"])
async def test_reconcile_uncaptured_intent_keeps_one_database_lock(db, owner_kind):
    owner_id, runtime, reservation, state = await _create_settled_authoritative_runtime(
        db, owner_kind=owner_kind, scope="workspace_container", settle=False
    )
    owner = (
        WorkspaceOwner.job(str(owner_id))
        if owner_kind == "job"
        else WorkspaceOwner.session(str(owner_id))
    )
    # Finish the creator's initial projection with the actual deterministic name.
    # Capture must still refuse an unrelated name, even on the same Pod UID.
    state["workspace_container"]["pod_name"] = owner.pod_name
    table, column = (
        ("jobs", "context") if owner_kind == "job" else ("threads", "metadata")
    )
    async with db.acquire() as conn:
        await conn.execute(
            f"UPDATE {table} SET {column}=$2::jsonb WHERE id=$1",
            owner_id,
            json.dumps(state),
        )
    assert await db.settle_managed_repository_workspace_creation_reservation(
        str(owner_id),
        owner_kind=owner_kind,
        scope="workspace_container",
        reservation_generation=int(reservation["reservation_generation"]),
        claimant="authority-envelope-creator",
        claim_token=int(reservation["claim_token"]),
        runtime_incarnation=runtime,
    )
    assert await db.record_managed_repository_workspace_process_zero(
        str(owner_id),
        owner_kind=owner_kind,
        scope="workspace_container",
        provisioner="k8s",
        runtime_incarnation=runtime,
    )
    intent = await db.prepare_managed_repository_workspace_cleanup_intent(
        str(owner_id),
        owner_kind=owner_kind,
        scope="workspace_container",
        runtime_incarnation=runtime,
        target_disposition="deleted",
        reclaim_shared_resources=False,
        resources_captured=False,
    )
    assert intent is not None and intent["resources_captured_at"] is None
    provisioner, _resources = _absent_pod_provisioner(
        db, owner, {"pvc_uid": uuid4(), "service_uid": uuid4()}
    )
    capture = provisioner.capture_workspace_teardown_identity
    observed = []

    async def capture_with_lock_check(*args, **kwargs):
        async with db.workspace_runtime_mutation_lock(
            str(owner_id),
            owner_kind=owner_kind,
            scope="workspace_container",
            wait=False,
        ) as acquired:
            assert not acquired, "another DB connection entered during capture"
        observed.append(True)
        return await capture(*args, **kwargs)

    provisioner.capture_workspace_teardown_identity = capture_with_lock_check
    await asyncio.wait_for(
        provisioner.reconcile_workspace_cleanup_intent(
            owner,
            expected_runtime_incarnation=runtime,
            intent_generation=int(intent["intent_generation"]),
        ),
        timeout=5,
    )
    saved = await db.get_managed_repository_workspace_cleanup_intent(
        str(owner_id),
        owner_kind=owner_kind,
        scope="workspace_container",
        runtime_incarnation=runtime,
    )
    assert observed == [True]
    assert saved["id"] == intent["id"] and saved["resources_captured_at"] is not None
    async with db.workspace_runtime_mutation_lock(
        str(owner_id), owner_kind=owner_kind, scope="workspace_container", wait=False
    ) as acquired:
        assert acquired


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["finalizer", "delete"])
async def test_guarded_terminal_entry_keeps_capture_serialized(db, entry):
    from tests.test_container_provisioner import (
        TestStrictStatelessWorkspaceCreation as Fixture,
        _StrictCreationDB,
    )

    class Ledger(_StrictCreationDB):
        @asynccontextmanager
        async def workspace_runtime_mutation_lock(self, *args, **kwargs):
            async with db.workspace_runtime_mutation_lock(*args, **kwargs) as held:
                yield held

    ledger = Ledger()
    provisioner = Fixture._provisioner(db=ledger)
    owner, pod = Fixture._owner(), Fixture._pod()
    pod.metadata.deletion_timestamp = "now"
    pod.metadata.finalizers = ["lifecycle.srw.dev/stateless-process-zero"]
    pod.status.phase = "Failed"
    pod.status.container_statuses[0].state.terminated = SimpleNamespace(exit_code=0)
    provisioner._core_api.read_namespaced_pod.return_value = pod

    def release_finalizer(**kwargs):
        assert kwargs["body"][0] == {
            "op": "test",
            "path": "/metadata/uid",
            "value": Fixture.RUNTIME,
        }
        # Model Kubernetes removing the terminating Pod after its finalizer.
        provisioner._core_api.read_namespaced_pod.side_effect = ApiException(status=404)

    provisioner._core_api.patch_namespaced_pod.side_effect = release_finalizer
    capture = provisioner.capture_workspace_teardown_identity
    seen = []

    async def capture_under_lock(*args, **kwargs):
        async with db.workspace_runtime_mutation_lock(
            owner.id, owner_kind="thread", scope="workspace_container", wait=False
        ) as held:
            assert not held
        seen.append(True)
        return await capture(*args, **kwargs)

    provisioner.capture_workspace_teardown_identity = capture_under_lock
    if entry == "finalizer":
        result = await asyncio.wait_for(
            provisioner.release_stateless_workspace_process_zero_finalizer(
                owner, expected_runtime_incarnation=Fixture.RUNTIME
            ),
            timeout=5,
        )
        assert result is True
    else:
        ledger.process_zero_recorded = True
        ledger.process_zero_uid = Fixture.RUNTIME
        result = await asyncio.wait_for(
            provisioner.delete_workspace_with_outcome(
                owner, expected_runtime_incarnation=Fixture.RUNTIME
            ),
            timeout=5,
        )
        assert result.current_deleted
    assert seen
    async with db.workspace_runtime_mutation_lock(
        owner.id, owner_kind="thread", scope="workspace_container", wait=False
    ) as held:
        assert held
