"""A selected development VM survives template edits and execution recovery."""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

from orchestrator.services.manifest_execution_snapshot import (
    prepare_srw_session_patch,
    read_execution,
    srw_snapshot_config,
)
from orchestrator.services.manifest_resources import ManifestResourceService
from orchestrator.services.manifest_store import ManifestStore
from orchestrator.services.manifest_workspace_selection import (
    select_execution_workspace,
    srw_workspace_config,
)
from orchestrator.services.vm_workspace_config import vm_provisioning_options
from shared.manifests import preview_documents
from tests import test_manifest_native_full_schema as full_schema

actor = full_schema.actor
database = full_schema.database
postgres_url = full_schema.postgres_url

IMAGE = "registry.example/dev-vm@sha256:" + "a" * 64
VM = {"image": IMAGE, "cpu_cores": 12, "memory": "24Gi", "disk_size": "120Gi"}
OPTIONS = {"vm_image": IMAGE, "cpu_cores": 12, "memory": "24Gi", "disk_size": "120Gi"}


def template():
    return {
        "apiVersion": "srw/v1alpha1",
        "kind": "WorkspaceTemplate",
        "metadata": {"name": "development"},
        "spec": {
            "backend": "vm",
            "resources": {"cpu": 12, "memory": "24Gi", "storage": "120Gi"},
            "environment": {"image": IMAGE},
        },
    }


def test_resolved_prebuilt_vm_preserves_allocation_without_changing_the_source():
    document = template()
    before = deepcopy(document)
    resolved = preview_documents(
        [document], default_scope={"kind": "Catalog", "name": "shared"}
    )["resolved"][0]
    assert srw_workspace_config({"template": {"inline": resolved["spec"]}}) == {
        "backend": "vm",
        "vm": VM,
    }
    assert document == before


@pytest.mark.parametrize(
    "changes",
    [
        {"backend": "sandbox"},
        {"resources": {"cpu": 1.5}},
        {"resources": {"cpu": True}},
        {"environment": {"image": "bad\nmanifest: injected"}},
        {"environment": {"image": IMAGE, "prepare": []}},
        {"environment": {"image": IMAGE, "pullPolicy": "Always"}},
        {"environment": {"image": IMAGE, "pullPolicy": "Never"}},
        {"environment": {"image": IMAGE, "cache": "Rebuild"}},
        {"initialize": []},
        {"retention": "Retain"},
    ],
)
def test_unsupported_vm_recipes_are_refused_instead_of_partially_applied(changes):
    spec = {**template()["spec"], **changes}
    with pytest.raises(HTTPException) as denied:
        srw_workspace_config({"template": {"inline": spec}})
    assert denied.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["pinned", "stateless"])
async def test_job_dispatch_keeps_selected_image_and_size_after_template_edit(
    database, actor, lane
):
    service = ManifestResourceService(database)
    document = template()
    await service.apply(json.dumps(document), actor, format="json")
    job = full_schema.assignment(adapter="srw/v1", mode="Reported")
    job["spec"]["execution"]["workspace"] = {
        "template": {"ref": {"name": "development"}}
    }
    _, _, _, _, work_id, snapshot = await full_schema.admit(database, actor, job)
    _, policy = srw_snapshot_config(snapshot)
    assert policy["workspace"]["vm"] == VM
    await database.execute(
        "UPDATE jobs SET execution_lane=$2 WHERE id=$1::uuid", work_id, lane
    )
    row = await ManifestStore(database).by_name(
        "WorkspaceTemplate",
        {"kind": "Account", "name": str(actor["id"])},
        "development",
    )
    document["spec"]["environment"]["image"] = "registry.example/other:v2"
    document["spec"]["resources"]["cpu"] = 2
    await service.apply(
        json.dumps(document),
        actor,
        format="json",
        expected_versions={
            f"WorkspaceTemplate/Account/{actor['id']}/development": row[
                "resource_version"
            ]
        },
    )
    candidates = await (
        database.get_dispatchable_jobs()
        if lane == "pinned"
        else database.get_admittable_stateless_jobs()
    )
    candidate = next(row for row in candidates if str(row["id"]) == work_id)
    assert candidate["execution_harness_adapter"] == "srw/v1"
    assert (
        await vm_provisioning_options(
            database,
            "Job",
            candidate,
            fallback={"workspace": {"vm": {"image": "mutable-projection:v2"}}},
        )
        == OPTIONS
    )
    assert await read_execution(database, "Job", work_id) == snapshot


@pytest.mark.asyncio
async def test_session_snapshot_keeps_vm_allocation_across_unrelated_patch(
    database, actor
):
    workspace, receipt = await select_execution_workspace(
        database,
        actor,
        role="session",
        project_id=None,
        supplied=True,
        workspace={"template": {"inline": template()["spec"]}},
    )
    thread_id = await database.create_thread(
        user_id=str(actor["id"]),
        datasource_ids=[],
        initial_metadata={"config_override": {"workspace": workspace}},
        workspace_selection=receipt,
    )
    current = await read_execution(database, "Session", thread_id)
    thread = await database.get_thread(thread_id)
    metadata = json.loads(thread["metadata"])
    prepared, _ = await prepare_srw_session_patch(
        database,
        current,
        thread,
        metadata,
        [],
        {"llm": {"temperature": 0.2}},
    )
    _, policy = srw_snapshot_config(prepared)
    assert policy["workspace"]["vm"] == VM
    assert prepared["resolved"]["spec"]["execution"]["workspace"] == receipt["resolved"]
    assert await vm_provisioning_options(database, "Session", thread) == OPTIONS
    with pytest.raises(HTTPException) as denied:
        await prepare_srw_session_patch(
            database,
            current,
            thread,
            metadata,
            [],
            {"workspace": {"vm": {"image": "registry.example/other:v2"}}},
        )
    assert denied.value.status_code == 422
    assert await read_execution(database, "Session", thread_id) == current


@pytest.mark.asyncio
async def test_project_default_selects_vm_and_explicit_none_suppresses_it(
    database, actor
):
    service = ManifestResourceService(database)
    project = {
        "apiVersion": "srw/v1alpha1",
        "kind": "Project",
        "metadata": {"name": "development-team"},
        "spec": {
            "resources": {
                "workspaces": {"development": {"inline": template()["spec"]}}
            },
            "defaults": {"workspace": "development"},
        },
    }
    applied = await service.apply(json.dumps(project), actor, format="json")
    entry = next(x for x in applied["resources"] if x["resource"]["kind"] == "Project")
    row = await ManifestStore(database).by_id(entry["uid"])
    project_id = str(row["linked_id"])
    selected, receipt = await select_execution_workspace(
        database,
        actor,
        role="worker",
        project_id=project_id,
    )
    assert selected == {"backend": "vm", "vm": VM}
    assert receipt["project_revision"] == row["revision"]
    selected, _ = await select_execution_workspace(
        database,
        actor,
        role="worker",
        project_id=project_id,
        supplied=True,
        workspace=None,
    )
    assert selected == {"backend": "none"}


@pytest.mark.asyncio
async def test_marked_execution_never_falls_back_when_snapshot_is_missing():
    db = SimpleNamespace(fetchrow=AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as denied:
        await vm_provisioning_options(
            db,
            "Job",
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "execution_harness_adapter": "srw/v1",
            },
            fallback={"workspace": {"vm": VM}},
        )
    assert denied.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_manifest_vm_admission_obeys_the_existing_operator_gate(
    database, actor, monkeypatch, admin
):
    user = {**actor, "is_admin": admin}
    monkeypatch.setattr(database, "user_can_use_vm", AsyncMock(return_value=False))
    monkeypatch.setattr(
        database,
        "get_system_setting",
        AsyncMock(return_value={"value": {"enabled": not admin}}),
    )
    job = full_schema.assignment(adapter="srw/v1", mode="Reported")
    job["spec"]["execution"]["workspace"] = {"template": {"inline": template()["spec"]}}
    with pytest.raises(HTTPException) as denied:
        await full_schema.admit(database, user, job)
    assert denied.value.status_code == 403
    assert await database.fetchval("SELECT count(*) FROM jobs") == 0
