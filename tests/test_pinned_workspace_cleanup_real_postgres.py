"""Exercise pinned End through the real workspace provisioner and PostgreSQL."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from kubernetes.client.exceptions import ApiException

from orchestrator import main
from orchestrator.services.container_provisioner import (
    STATELESS_WORKSPACE_PROCESS_ZERO_FINALIZER,
)
from orchestrator.services.workspace_lifecycle import WorkspaceOwner
from tests import test_persistent_recycler_real_postgres as authority
from tests.test_workspace_cleanup_retry_real_postgres import _absent_pod_provisioner


db = authority.db
pg_dsn = authority.pg_dsn
_schema_applied = authority._schema_applied


async def _scenario(db, monkeypatch):
    ids = await authority._seed(db, protected_agent_pod=True, workspace_claim=False)
    thread = await db.get_thread(ids["thread"])
    generation = str(thread["runtime_generation"])
    metadata = authority._json(thread["metadata"])
    metadata["config_override"]["workspace"]["backend"] = "sandbox"
    metadata["config_override"]["officer"]["enabled"] = False
    await db.execute(
        "UPDATE threads SET metadata=$2::jsonb WHERE id=$1::uuid",
        ids["thread"],
        json.dumps(metadata),
    )
    owner = WorkspaceOwner.session(ids["thread"])
    attempt, pod_uid, pvc_uid, service_uid = (str(uuid4()) for _ in range(4))
    assert await db.reserve_pinned_thread_workspace_provision_intent(
        owner.id,
        expected_runtime_generation=generation,
        expected_agent_id=ids["agent"],
        expected_attach_token=ids["attach_token"],
        expected_workspace_context=None,
        expected_binding_context=None,
        attempt_id=attempt,
        namespace="agent-workspaces",
        pod_name=owner.pod_name,
        pvc_name="pvc-" + owner.pod_name,
        seed_configmap_name=None,
        service_name=owner.pod_name,
        retained_service_uid=None,
        network_tier="internet-only",
        manifest_fingerprint="a" * 64,
    )
    for resource, uid in (("pod", pod_uid), ("pvc", pvc_uid), ("service", service_uid)):
        assert await db.publish_pinned_thread_workspace_provision_resource(
            owner.id,
            expected_runtime_generation=generation,
            attempt_id=attempt,
            resource=resource,
            resource_uid=uid,
        )
    assert await db.complete_pinned_thread_workspace_provision_intent(
        owner.id,
        expected_runtime_generation=generation,
        attempt_id=attempt,
        expected_pod_uid=pod_uid,
        expected_pvc_uid=pvc_uid,
        expected_seed_configmap_uid=None,
        expected_service_uid=service_uid,
        pod_ip="10.0.0.8",
        ssh_host_key_fingerprint="SHA256:" + "A" * 43,
    )
    p, resources = _absent_pod_provisioner(
        db, owner, {"pvc_uid": pvc_uid, "service_uid": service_uid}
    )
    pod = NS(
        metadata=NS(
            name=owner.pod_name,
            namespace=p._namespace,
            uid=pod_uid,
            resource_version="7",
            deletion_timestamp=None,
            finalizers=[STATELESS_WORKSPACE_PROCESS_ZERO_FINALIZER],
            labels={
                "app": "srw-workspace",
                "srw/component": owner.component_label,
                "srw.io/component": "agent-workspace",
                owner.label_key: owner.id,
            },
        ),
        spec=NS(
            containers=[
                NS(
                    name="workspace",
                    volume_mounts=[
                        NS(name="workspace-data", mount_path="/home/agent-host")
                    ],
                )
            ],
            init_containers=[],
            ephemeral_containers=[],
            volumes=[
                NS(
                    name="workspace-data",
                    persistent_volume_claim=NS(claim_name="pvc-" + owner.pod_name),
                )
            ],
        ),
        status=NS(
            phase="Running",
            pod_ip="10.0.0.8",
            init_container_statuses=[],
            ephemeral_container_statuses=[],
            container_statuses=[
                NS(name="workspace", ready=True, state=NS(terminated=None))
            ],
        ),
    )
    resources["pod"] = pod
    effects = []

    def read_pod(**kwargs):
        if "pod" not in resources:
            raise ApiException(status=404)
        return resources["pod"]

    def delete_pod(**kwargs):
        current = read_pod()
        assert kwargs["body"]["preconditions"]["uid"] == current.metadata.uid
        current.metadata.deletion_timestamp = "now"
        current.status.phase = "Succeeded"
        current.status.container_statuses[0].state.terminated = NS(exit_code=0)
        effects.append(("delete", current.metadata.uid))

    def patch_pod(**kwargs):
        current = read_pod()
        body = kwargs["body"]
        assert {"op": "test", "path": "/metadata/uid", "value": pod_uid} in body
        assert any(x["op"] == "remove" and "finalizers" in x["path"] for x in body)
        assert current.status.container_statuses[0].state.terminated is not None
        effects.append(("finalizer", current.metadata.uid))
        del resources["pod"]
        return current

    p._core_api.read_namespaced_pod.side_effect = read_pod
    p._core_api.delete_namespaced_pod.side_effect = delete_pod
    p._core_api.patch_namespaced_pod.side_effect = patch_pod
    monkeypatch.setattr(main, "postgres_db", db)
    monkeypatch.setattr(main, "container_provisioner", p)
    monkeypatch.setattr(
        main.session_router, "teardown_route", AsyncMock(return_value=True)
    )
    return ids, owner, p, resources, effects


async def _begin(db, ids, permanent):
    retirement = await db.begin_pinned_thread_retirement(
        ids["thread"], permanent=permanent
    )
    await authority._authorize_and_ack(db, ids, retirement)
    return retirement


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent_first", [False, True])
async def test_pinned_workspace_end_and_permanent_delete(
    db, monkeypatch, permanent_first
):
    ids, owner, p, resources, effects = await _scenario(db, monkeypatch)
    pvc_uid = resources["pvc"].metadata.uid
    pod_uid = resources["pod"].metadata.uid
    retirement = await _begin(db, ids, permanent_first)
    await main._cleanup_pinned_thread_retirement(retirement, cleanup_agent_pod=False)
    assert effects == [("delete", pod_uid), ("finalizer", pod_uid)]
    assert (
        await db.fetchval(
            "SELECT count(*) FROM managed_repository_process_zero_receipts "
            "WHERE owner_id=$1::uuid AND scope='workspace_container' "
            "AND runtime_incarnation=$2",
            owner.id,
            pod_uid,
        )
        == 1
    )
    assert (
        await db.fetchval(
            "SELECT count(*) FROM managed_repository_workspace_cleanup_intents "
            "WHERE owner_id=$1::uuid",
            owner.id,
        )
        == 0
    )
    if not permanent_first:
        assert set(resources) == {"pvc"}
        assert resources["pvc"].metadata.uid == pvc_uid
        assert await db.settle_pinned_thread_retirement(
            owner.id,
            token=retirement["token"],
            generation=retirement["generation"],
            final_status="ended",
        )
        retirement = await db.begin_pinned_thread_retirement(owner.id, permanent=True)
        assert retirement["context"]["retained_soft_workspace"]["pvc_uid"] == pvc_uid
        assert await db.authorize_pinned_thread_retirement(
            owner.id,
            token=retirement["token"],
            generation=retirement["generation"],
            settle_status="ended",
        )
        await main._cleanup_pinned_thread_retirement(
            retirement, cleanup_agent_pod=False
        )
    assert not resources
    await db.delete_thread(
        owner.id,
        expected_runtime_retirement_token=retirement["token"],
        expected_runtime_generation=retirement["generation"],
    )
    assert await db.get_thread(owner.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "unauthorized",
        "missing_ack",
        "stale_token",
        "altered_context",
        "pod_replacement",
        "pvc_replacement",
        "absent_without_receipt",
    ],
)
async def test_pinned_workspace_refuses_incomplete_cleanup_authority(
    db, monkeypatch, fault
):
    ids, owner, p, resources, effects = await _scenario(db, monkeypatch)
    retirement = await db.begin_pinned_thread_retirement(owner.id, permanent=True)
    if fault != "unauthorized":
        assert await db.authorize_pinned_thread_retirement(
            owner.id,
            token=retirement["token"],
            generation=retirement["generation"],
            settle_status="ended",
        )
    if fault not in {"unauthorized", "missing_ack"}:
        await authority._authorize_and_ack(db, ids, retirement)
    if fault == "stale_token":
        retirement = {**retirement, "token": str(uuid4())}
    elif fault == "altered_context":
        retirement = {
            **retirement,
            "context": {**retirement["context"], "entry_status": "ended"},
        }
    elif fault in {"pod_replacement", "pvc_replacement"}:
        resources[fault.split("_")[0]].metadata.uid = str(uuid4())
    elif fault == "absent_without_receipt":
        del resources["pod"]
    with pytest.raises(RuntimeError):
        await main._cleanup_pinned_thread_retirement(
            retirement, cleanup_agent_pod=False
        )
    assert effects == []
    assert "pvc" in resources and "service" in resources
    assert not p._core_api.delete_namespaced_persistent_volume_claim.called
    assert not p._core_api.delete_namespaced_service.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["pod_ack", "service_ack", "projection_ack", "retained_pvc_ack"]
)
async def test_pinned_workspace_replays_lost_responses(db, monkeypatch, fault):
    ids, owner, p, resources, effects = await _scenario(db, monkeypatch)
    retirement = await _begin(db, ids, False)
    if fault == "retained_pvc_ack":
        await main._cleanup_pinned_thread_retirement(
            retirement, cleanup_agent_pod=False
        )
        assert await db.settle_pinned_thread_retirement(
            owner.id,
            token=retirement["token"],
            generation=retirement["generation"],
            final_status="ended",
        )
        retirement = await db.begin_pinned_thread_retirement(owner.id, permanent=True)
        assert await db.authorize_pinned_thread_retirement(
            owner.id,
            token=retirement["token"],
            generation=retirement["generation"],
            settle_status="ended",
        )
    target = {
        "pod_ack": p._core_api.patch_namespaced_pod,
        "service_ack": p._core_api.delete_namespaced_service,
        "retained_pvc_ack": p._core_api.delete_namespaced_persistent_volume_claim,
    }.get(fault)
    if target is not None:
        effect = target.side_effect
        lost = False

        def lose_once(**kwargs):
            nonlocal lost
            result = effect(**kwargs)
            if not lost:
                lost = True
                raise TimeoutError("accepted response lost")
            return result

        target.side_effect = lose_once
    else:
        original = p._set_context
        lost = False

        async def lose_projection(*args, **kwargs):
            nonlocal lost
            result = await original(*args, **kwargs)
            if not lost:
                lost = True
                return False
            return result

        monkeypatch.setattr(p, "_set_context", lose_projection)
    with pytest.raises(RuntimeError):
        await main._cleanup_pinned_thread_retirement(
            retirement, cleanup_agent_pod=False
        )
    assert lost
    await main._cleanup_pinned_thread_retirement(retirement, cleanup_agent_pod=False)
    assert len(effects) == 2
    assert set(resources) == (set() if fault == "retained_pvc_ack" else {"pvc"})
    current = await db.get_thread(owner.id)
    assert (
        authority._json(current["metadata"])["workspace_container"]["status"]
        == "deleted"
    )
