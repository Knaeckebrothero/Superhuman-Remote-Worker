"""Cache/lifecycle invariants under observed API failures and competing work."""

from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from kubernetes import client

from shared.workspace_preparation import preparation_request
from shared.workspace_preparation_settings import PreparationSettings
from vm_controller.preparation_store import PreparationConflict, Record, now
from vm_controller.workspace_preparation import VMWorkspacePreparation, allocation_name

BASE = "registry.example/base@sha256:" + "1" * 64
BUILDER = "registry.example/builder@sha256:" + "2" * 64
SCOPE = str(uuid4())


def request(
    *, cache="Reuse", pull="IfNotPresent", scope=SCOPE, owner="job", runtime=None
):
    return preparation_request(
        {
            "image": "registry.example/base:latest",
            "cache": cache,
            "pullPolicy": pull,
            "prepare": [{"command": ["touch", "/opt/prepared"]}],
        },
        scope_kind="Project",
        scope_uid=scope,
        allocation_id=str(uuid4()),
        owner_kind=owner,
        runtime_generation=runtime,
    )


class MemoryStore:
    """API observation fixture: disk clones finish; builders finish only on signal."""

    namespace = "test"

    def __init__(self):
        self.data, self.disks, self.pods = {}, {}, {}
        self.created_pods = []
        self.core = NS(
            create_namespaced_pod=self.create_pod,
            read_namespaced_pod=lambda name: self.pods.get(name),
            delete_namespaced_pod=self.delete_pod,
            patch_namespaced_pod=self.patch_pod,
        )

    async def call(self, method, **kwargs):
        return method(**kwargs)

    async def ensure(self, name, kind, request, state):
        if name not in self.data:
            self.data[name] = Record(
                name, str(uuid4()), "1", now(), kind, deepcopy(request), deepcopy(state)
            )
        row = self.data[name]
        if row.request != request or row.kind != kind:
            raise PreparationConflict("Conflicting durable request")
        return deepcopy(row)

    async def get(self, name):
        return deepcopy(self.data.get(name))

    async def records(self, kind, *, artifact_uid=None):
        return [
            deepcopy(row)
            for row in self.data.values()
            if row.kind == kind
            and (not artifact_uid or row.state.get("artifact_uid") == artifact_uid)
        ]

    async def save(self, record, state):
        current = self.data[record.name]
        assert current.uid == record.uid and current.version == record.version
        record.state, record.version = deepcopy(state), str(int(record.version) + 1)
        self.data[record.name] = deepcopy(record)
        return record

    async def delete_record(self, record):
        assert self.data[record.name].uid == record.uid
        del self.data[record.name]

    async def ensure_disk(self, name, *, owner_uid, scope, source, size):
        if name not in self.disks:
            self.disks[name] = {
                "dv": {
                    "metadata": {"uid": str(uuid4())},
                    "spec": {"source": deepcopy(source)},
                    "status": {"phase": "Succeeded"},
                },
                "pvc": NS(metadata=NS(uid=str(uuid4()))),
                "owner": owner_uid,
            }
        return deepcopy(self.disks[name]["dv"])

    async def dv(self, name):
        return deepcopy(self.disks.get(name, {}).get("dv"))

    async def pvc(self, name):
        return deepcopy(self.disks.get(name, {}).get("pvc"))

    async def disk_identity(self, name, *, owner_uid, expected=None):
        disk = self.disks.get(name)
        if (
            disk is None
            or disk["owner"] != owner_uid
            or expected
            and disk["pvc"].metadata.uid != expected
        ):
            raise PreparationConflict("PVC identity changed")
        return disk["pvc"].metadata.uid

    async def unused(self, name, *, ignore_cdi_owner=None):
        return not any(
            v.persistent_volume_claim and v.persistent_volume_claim.claim_name == name
            for pod in self.pods.values()
            for v in pod.spec.volumes
        )

    async def reap_disk_pods(self, name, pvc_uid):
        pass

    async def delete_disk(
        self, name, *, owner_uid, pvc_uid, dv_uid, retire_import=False
    ):
        assert await self.unused(name)
        if name not in self.disks:
            return True
        disk = self.disks[name]
        assert (
            disk["owner"] == owner_uid
            and disk["pvc"].metadata.uid == pvc_uid
            and disk["dv"]["metadata"]["uid"] == dv_uid
        )
        del self.disks[name]
        return True

    def create_pod(self, body):
        self.created_pods.append(deepcopy(body))
        raw = deepcopy(body)
        raw["metadata"]["uid"] = str(uuid4())
        raw["status"] = {"phase": "Running"}
        pod = client.ApiClient()._ApiClient__deserialize(raw, "V1Pod")
        self.pods[pod.metadata.name] = pod
        return pod

    def delete_pod(self, name, body):
        assert self.pods[name].metadata.uid == body["preconditions"]["uid"]
        del self.pods[name]

    def patch_pod(self, name, body):
        assert body["metadata"]["uid"] == self.pods[name].metadata.uid
        assert body["spec"]["activeDeadlineSeconds"] == 1
        self.finish(name, code=143)

    def finish(self, name, *, code=0, receipt_changes=None):
        pod = self.pods[name]
        inputs = self.data[name].request
        receipt = {
            key: inputs[key] for key in ("version", "buildUid", "pvcUid", "cacheKey")
        }
        receipt.update(phase="Succeeded", diskSha256="a" * 64, diskBytes=1024)
        receipt.update(receipt_changes or {})
        pod.status.phase = "Succeeded" if code == 0 else "Failed"
        pod.status.container_statuses = [
            NS(state=NS(terminated=NS(exit_code=code, message=json.dumps(receipt))))
        ]


def engine(store=None, **settings):
    resolver = NS(permitted=lambda image: None, resolve=AsyncMock(return_value=BASE))
    instance = VMWorkspacePreparation(
        NS(core_api=None, k8s_client=None),
        namespace="test",
        storage_class="local-path",
        settings=PreparationSettings(enabled=True, builder_image=BUILDER, **settings),
        resolver=resolver,
    )
    instance.store = store or MemoryStore()
    return instance


async def complete(service, value, **finish):
    result, wait = await service.prepare(value)
    assert result is None and wait["preparation"]["phase"] == "Building"
    pod = next(
        p
        for p in service.store.pods
        if service.store.data[p].request["buildUid"] == wait["preparation"]["uid"]
    )
    service.store.finish(pod, **finish)
    for _ in range(3):
        result, wait = await service.prepare(value)
        if result or wait["status"] == "failed":
            return result, wait
    raise AssertionError("Build did not retire")


@pytest.mark.asyncio
async def test_restart_coalesces_same_scope_build_and_publishes_only_after_writer_removal():
    service, first, second = engine(), request(), request()
    _, initial = await service.prepare(first)
    restarted = engine(service.store)
    _, attached = await restarted.prepare(second)
    assert initial["preparation"]["uid"] == attached["preparation"]["uid"]
    assert len(service.store.created_pods) == 1
    pod = next(iter(service.store.pods))
    service.store.finish(pod)
    result, waiting = await restarted.prepare(second)
    assert result is None and waiting["preparation"]["phase"] == "Releasing"
    result, _ = await restarted.prepare(second)
    assert result["preparation"]["phase"] == "Succeeded"
    third, _ = await restarted.prepare(request())
    assert third["pvc_uid"] == result["pvc_uid"] and third["preparation"]["cacheHit"]
    assert len(service.store.created_pods) == 1


@pytest.mark.asyncio
async def test_cache_is_scoped_and_rebuild_retry_does_not_create_another_writer():
    service = engine()
    first, _ = await complete(service, request())
    other = request(scope=str(uuid4()))
    second, _ = await complete(service, other)
    assert first["pvc_uid"] != second["pvc_uid"]
    rebuilt = request(cache="Rebuild")
    third, _ = await complete(service, rebuilt)
    assert third["pvc_uid"] not in {first["pvc_uid"], second["pvc_uid"]}
    again, _ = await service.prepare(rebuilt)
    assert again["pvc_uid"] == third["pvc_uid"]
    assert len(service.store.created_pods) == 3


@pytest.mark.asyncio
async def test_never_miss_does_not_resolve_base_or_create_disks():
    service = engine()
    result, failure = await service.prepare(request(pull="Never"))
    assert result is None and failure["status"] == "failed"
    service.resolver.resolve.assert_not_awaited()
    assert not service.store.disks and not service.store.created_pods


@pytest.mark.asyncio
async def test_if_not_present_and_never_keep_cached_digest_while_always_resolves_again():
    service = engine()
    first, _ = await complete(service, request())
    calls = service.resolver.resolve.await_count
    for policy in ("IfNotPresent", "Never"):
        ready, _ = await service.prepare(request(pull=policy))
        assert ready["pvc_uid"] == first["pvc_uid"]
        assert service.resolver.resolve.await_count == calls
    service.resolver.resolve.return_value = BASE.replace("1" * 64, "3" * 64)
    _, waiting = await service.prepare(request(pull="Always"))
    assert waiting["preparation"]["baseImage"] != first["preparation"]["baseImage"]
    assert service.resolver.resolve.await_count == calls + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        {"code": 17},
        {"receipt_changes": {"pvcUid": str(uuid4())}},
        {"receipt_changes": {"diskSha256": "invalid"}},
    ],
)
async def test_failure_or_receipt_mismatch_never_publishes(failure):
    service = engine()
    ready, failed = await complete(service, request(), **failure)
    assert ready is None and failed["status"] == "failed"
    assert not service.store.pods


@pytest.mark.asyncio
async def test_lost_pod_is_quarantined_without_replacement_and_counts_against_capacity():
    service = engine(max_concurrent=1)
    value = request()
    await service.prepare(value)
    service.store.pods.clear()  # Unknown node/process state, not terminal evidence.
    restarted = engine(service.store, max_concurrent=1)
    ready, failed = await restarted.prepare(value)
    assert ready is None and failed["status"] == "failed"
    assert any(
        a.state["phase"] == "Lost" for a in await service.store.records("artifact")
    )
    _, waiting = await restarted.prepare(request(cache="Rebuild"))
    assert waiting["preparation"]["phase"] == "Queued"
    assert len(service.store.created_pods) == 1


@pytest.mark.asyncio
async def test_cancel_one_holder_keeps_shared_builder_but_last_holder_stops_it():
    service, first, second = engine(), request(), request()
    await service.prepare(first)
    await service.prepare(second)
    assert await service.cancel(first)
    assert next(iter(service.store.pods.values())).status.phase == "Running"
    assert not await service.cancel(second)
    await service.reconcile()
    await service.reconcile()
    assert not service.store.pods
    assert (await service.prepare(first))[1]["status"] == "failed"
    assert all(
        a.state["phase"] == "Failed" for a in await service.store.records("artifact")
    )


@pytest.mark.asyncio
async def test_ended_session_tombstone_does_not_poison_a_new_runtime_generation():
    service = engine()
    first = request(owner="session", runtime=str(uuid4()))
    await service.cancel(first)
    assert (await service.prepare(first))[1]["status"] == "failed"
    resumed = preparation_request(
        {"image": first["image"], "prepare": first["steps"]},
        scope_kind="Project",
        scope_uid=SCOPE,
        allocation_id=first["allocationId"],
        owner_kind="session",
        runtime_generation=str(uuid4()),
    )
    assert allocation_name(first) != allocation_name(resumed)
    assert (await service.prepare(resumed))[1]["preparation"]["phase"] == "Building"


@pytest.mark.asyncio
async def test_eviction_waits_for_clone_ack_and_cannot_delete_a_replaced_pvc():
    service, value = engine(), request()
    ready, _ = await complete(service, value)
    uid = ready["preparation"]["uid"]
    assert not await service.delete_artifact(uid, value["scope"])
    root = "new-workspace"
    dv = await service.store.ensure_disk(
        root,
        owner_uid=str(uuid4()),
        scope=value["scope"],
        source={"pvc": {"namespace": "test", "name": ready["name"]}},
        size="30Gi",
    )
    assert dv["status"]["phase"] == "Succeeded"
    await service.mark_allocated(
        value, rootdisk=root, pvc_uid=(await service.store.pvc(root)).metadata.uid
    )
    service.store.disks[ready["name"]]["pvc"].metadata.uid = str(uuid4())
    with pytest.raises(
        AssertionError
    ):  # Fake API UID precondition rejects replacement.
        await service.delete_artifact(uid, value["scope"])


@pytest.mark.asyncio
async def test_idle_artifact_and_base_are_collected_after_allocation_release():
    service, value = engine(cache_ttl=60), request()
    ready, _ = await complete(service, value)
    await service.cancel(value)  # Caller retired before requesting a workspace clone.
    for row in service.store.data.values():
        row.state["last_used"] = now() - 120
    await service.reconcile()
    assert not await service.store.records("artifact")
    assert not await service.store.records("base")
    assert not service.store.disks


@pytest.mark.asyncio
async def test_cache_limit_refuses_new_build_but_keeps_existing_cache_available():
    service = engine(max_cache_entries=2)
    ready, _ = await complete(service, request())
    hit, _ = await service.prepare(request())
    assert hit["pvc_uid"] == ready["pvc_uid"]
    _, failed = await service.prepare(request(cache="Rebuild"))
    assert failed["status"] == "failed" and failed["error"] == "CacheCapacityExceeded"
    assert len(service.store.created_pods) == 1


@pytest.mark.asyncio
async def test_abandoned_allocation_expires_without_reviving_unknown_work():
    service, value = engine(), request()
    await service.prepare(value)
    allocation = service.store.data[allocation_name(value)]
    allocation.state["expires_at"] = now() - 1
    await service.reconcile()
    assert (await service.prepare(value))[1]["error"] == "AllocationExpired"
    await service.reconcile()
    assert not service.store.pods
    assert all(
        a.state["phase"] == "Failed" for a in await service.store.records("artifact")
    )


@pytest.mark.asyncio
async def test_disabling_new_builds_preserves_cancellation_and_cleanup():
    from dataclasses import replace

    service, value = engine(), request()
    await service.prepare(value)
    service.settings = replace(service.settings, enabled=False)
    await service.reconcile()
    await service.reconcile()
    assert not service.store.pods
    allocation = service.store.data[allocation_name(value)]
    assert allocation.state["error"] == "PreparationDisabled"
