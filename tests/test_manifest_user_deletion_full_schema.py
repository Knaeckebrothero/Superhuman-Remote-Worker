"""Application user deletion retains history without orphaning live authority."""

from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import HTTPException
import pytest

from orchestrator.services.manifest_execution_snapshot import read_execution
from orchestrator.services.manifest_resources import ManifestResourceService
from orchestrator.services.manifest_store import ManifestStore, resource_key
from orchestrator.services.manifest_workspaces import ManifestWorkspaceService
from orchestrator.services.user_administration import delete_user
from tests import test_manifest_native_full_schema as full_schema
from tests.test_manifest_native_full_schema import (
    admit,
    assignment,
)

database = full_schema.database
postgres_url = full_schema.postgres_url


async def owner(database, name):
    user, project = await database.create_user_with_default_project(name)
    await database.execute(
        "UPDATE users SET is_approved=TRUE,is_admin=TRUE WHERE id=$1", user["id"]
    )
    return {**user, "is_approved": True, "is_admin": True}, project


async def workspace(
    database, user_id, *, status="Released", execution_id=None, pod_uid=None
):
    return await database.fetchrow(
        """INSERT INTO srw_workspace_instances(
        id,owner_id,recipe,revision,pvc_name,status,execution_id,pod_uid,ssh_ciphertext)
        VALUES($1,$2,'{}','historical-recipe','historical-volume',$3,$4,$5,'test-only-ciphertext')
        RETURNING *""",
        uuid4(),
        user_id,
        status,
        execution_id,
        pod_uid,
    )


async def secret(database, user_id, scope_kind, scope_id, name="private"):
    return await database.fetchval(
        """INSERT INTO srw_resource_secrets(scope_kind,scope_name,name,owner_id,ciphertext,keys)
        VALUES($1,$2,$3,$4,'test-only-ciphertext',ARRAY['token']) RETURNING id""",
        scope_kind,
        str(scope_id),
        name,
        user_id,
    )


async def personal_expert(database, user):
    document = {
        "apiVersion": "srw/v1alpha1",
        "kind": "Expert",
        "metadata": {"name": "personal-helper"},
        "spec": {
            "runtime": {
                "image": "example.invalid/helper:v1",
                "config": {"private": None},
            }
        },
    }
    result = await ManifestResourceService(database).apply(
        json.dumps(document), user, format="json"
    )
    return await ManifestStore(database).by_id(result["resources"][0]["uid"])


@pytest.mark.asyncio
async def test_new_account_deletion_keeps_completed_manifest_history(database):
    user, project = await owner(database, "Departing owner")
    expert = await personal_expert(database, user)
    account_secret = await secret(database, user["id"], "Account", user["id"])
    await database.execute(
        "INSERT INTO capability_grants(scope_kind,scope_id,key,value_json) VALUES('user',$1,'shell_tools','true')",
        user["id"],
    )
    execution, runtime, _, _, work_id, snapshot = await admit(
        database, user, assignment()
    )
    await execution.reconcile_one(str(snapshot["id"]))
    runtime.exit()
    await execution.reconcile_one(str(snapshot["id"]))
    assert (await database.get_job(work_id))["status"] == "completed"
    thread_id = await database.create_thread(
        user_id=str(user["id"]),
        authority_user_id=str(user["id"]),
        initial_metadata={
            "config_override": {
                "llm": {"model": "gpt-4o"},
                "workspace": {"backend": "none"},
            }
        },
    )
    await database.execute(
        "UPDATE threads SET status='ended' WHERE id=$1", UUID(thread_id)
    )
    session = await read_execution(database, "Session", thread_id)
    receipt = await workspace(database, user["id"])
    await database.execute(
        "INSERT INTO srw_execution_workspace_bindings(execution_id,instance_id) VALUES($1,$2)",
        snapshot["id"],
        receipt["id"],
    )
    histories = await database.fetch(
        "SELECT * FROM srw_execution_spec_revisions ORDER BY execution_id,generation"
    )
    expert_revisions = await database.fetch(
        "SELECT * FROM srw_resource_revisions WHERE resource_id=$1", expert["id"]
    )

    result = await delete_user(
        user_id=str(user["id"]), dependencies=SimpleNamespace(store=database)
    )
    assert result == {"status": "deleted"}
    assert await database.get_user(str(user["id"])) is None
    assert await database.get_expert_by_id(str(expert["linked_id"])) is None
    retired = await database.fetchrow(
        "SELECT * FROM srw_resources WHERE id=$1", expert["id"]
    )
    assert retired["deleted_at"] is not None
    assert retired["owner_id"] is None
    assert (
        await database.fetch(
            "SELECT * FROM srw_resource_revisions WHERE resource_id=$1", expert["id"]
        )
        == expert_revisions
    )
    assert (
        await database.fetch(
            "SELECT * FROM srw_execution_spec_revisions ORDER BY execution_id,generation"
        )
        == histories
    )
    for kind, identity, original in [
        ("Job", work_id, snapshot),
        ("Session", thread_id, session),
    ]:
        kept = await read_execution(database, kind, identity)
        assert kept["owner_id"] is None
        assert kept["resolved"] == original["resolved"]
        assert kept["revision"] == original["revision"]
    assert (await database.get_job(work_id))["user_id"] is None
    assert (await database.get_thread(thread_id))["user_id"] is None
    kept_receipt = await database.fetchrow(
        "SELECT * FROM srw_workspace_instances WHERE id=$1", receipt["id"]
    )
    assert kept_receipt["owner_id"] is None
    assert kept_receipt["ssh_ciphertext"] is None
    assert (
        await database.fetchval(
            "SELECT instance_id FROM srw_execution_workspace_bindings WHERE execution_id=$1",
            snapshot["id"],
        )
        == receipt["id"]
    )
    assert (
        await database.fetchval(
            "SELECT count(*) FROM srw_execution_attempts WHERE execution_id=$1 AND cleaned_at IS NOT NULL",
            snapshot["id"],
        )
        == 1
    )
    assert (
        await database.fetchval(
            "SELECT count(*) FROM srw_manifest_operations WHERE owner_id=$1", user["id"]
        )
        == 0
    )
    assert (
        await database.fetchval(
            "SELECT count(*) FROM srw_resource_secrets WHERE id=$1", account_secret
        )
        == 0
    )
    assert (
        await database.fetchval(
            "SELECT count(*) FROM capability_grants WHERE scope_kind='user' AND scope_id=$1",
            user["id"],
        )
        == 0
    )
    # Deleting an application user historically leaves its Project row behind.
    project_resource = await ManifestStore(database).by_link("Project", project["id"])
    assert project_resource["owner_id"] is None
    assert project_resource["deleted_at"] is None

    administrator, _ = await owner(database, "History administrator")
    service = ManifestWorkspaceService(
        database, None, namespace="test", default_image="test.invalid/workspace:v1"
    )
    assert (await service.read(str(receipt["id"]), administrator))["id"] == receipt[
        "id"
    ]
    with pytest.raises(HTTPException) as denied:
        await service.read(str(receipt["id"]), {**administrator, "is_admin": False})
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_personal_default_project_can_be_removed_after_account_deletion(database):
    user, project = await owner(database, "New user")
    resource = await ManifestStore(database).by_link("Project", project["id"])
    assert await database.delete_user(str(user["id"])) is True
    assert await database.delete_user(str(user["id"])) is False
    assert await database.delete_project(str(project["id"])) is True
    retired = await database.fetchrow(
        "SELECT * FROM srw_resources WHERE id=$1", resource["id"]
    )
    assert retired["deleted_at"] is not None
    assert retired["project_id"] is None
    assert (
        await database.fetchval(
            "SELECT count(*) FROM srw_resource_revisions WHERE resource_id=$1",
            resource["id"],
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,pod_uid",
    [("Reserved", None), ("Detached", None), ("Released", "unfenced-pod")],
)
async def test_workspace_refusal_rolls_back_the_entire_account_deletion(
    database, status, pod_uid
):
    user, project = await owner(database, "Workspace owner")
    expert = await personal_expert(database, user)
    private = await secret(database, user["id"], "Account", user["id"])
    receipt = await workspace(database, user["id"], status=status, pod_uid=pod_uid)
    with pytest.raises(HTTPException) as denied:
        await database.delete_user(str(user["id"]))
    assert denied.value.status_code == 409
    assert "workspace" in denied.value.detail
    assert await database.get_user(str(user["id"])) is not None
    assert (await ManifestStore(database).by_id(expert["id"]))[
        "resource_version"
    ] == expert["resource_version"]
    assert (
        await database.fetchval(
            "SELECT owner_id FROM srw_workspace_instances WHERE id=$1", receipt["id"]
        )
        == user["id"]
    )
    assert (
        await database.fetchval(
            "SELECT owner_id FROM srw_resource_secrets WHERE id=$1", private
        )
        == user["id"]
    )
    assert (
        await database.get_user_role_in_project(str(project["id"]), str(user["id"]))
        == "owner"
    )


@pytest.mark.asyncio
async def test_unfinished_manifest_execution_refuses_user_deletion(database):
    user, _ = await owner(database, "Execution owner")
    _, _, _, _, work_id, snapshot = await admit(database, user, assignment())
    with pytest.raises(HTTPException) as denied:
        await database.delete_user(str(user["id"]))
    assert denied.value.status_code == 409
    assert "manifest work" in denied.value.detail
    assert (await read_execution(database, "Job", work_id))["owner_id"] == user["id"]
    assert await ManifestStore(database).by_id(snapshot["resource_id"]) is not None


@pytest.mark.asyncio
async def test_user_delete_rolls_back_manifest_retirement_if_final_cleanup_fails(
    database, monkeypatch
):
    user, _ = await owner(database, "Transactional owner")
    expert = await personal_expert(database, user)
    private = await secret(database, user["id"], "Account", user["id"])
    receipt = await workspace(database, user["id"])
    operations = await database.fetch(
        "SELECT * FROM srw_manifest_operations WHERE owner_id=$1", user["id"]
    )

    async def final_cleanup_failure(*args, **kwargs):
        raise RuntimeError("injected final account cleanup failure")

    monkeypatch.setattr(database, "delete_grants_for_scope", final_cleanup_failure)
    with pytest.raises(RuntimeError, match="injected final account cleanup failure"):
        await database.delete_user(str(user["id"]))
    assert await database.get_user(str(user["id"])) is not None
    assert await ManifestStore(database).by_id(expert["id"]) == expert
    assert (
        await database.fetchrow(
            "SELECT * FROM srw_workspace_instances WHERE id=$1", receipt["id"]
        )
        == receipt
    )
    assert (
        await database.fetchval(
            "SELECT owner_id FROM srw_resource_secrets WHERE id=$1", private
        )
        == user["id"]
    )
    assert (
        await database.fetch(
            "SELECT * FROM srw_manifest_operations WHERE owner_id=$1", user["id"]
        )
        == operations
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["user", "project"])
async def test_personal_expert_retirement_cannot_cascade_external_defaults(
    database, target
):
    departing, _ = await owner(database, "Expert creator")
    remaining, project = await owner(database, "Default owner")
    expert = await personal_expert(database, departing)
    if target == "project":
        await database.set_project_default_expert(
            project_id=str(project["id"]),
            expert_id=str(expert["linked_id"]),
            expert_type="worker",
            actor_user_id=str(remaining["id"]),
        )
    else:
        # Preserve even a historical pointer predating the personal-default
        # ownership guard; user deletion must not silently cascade another row.
        await database.execute(
            "INSERT INTO user_expert_defaults(user_id,expert_type,expert_id) VALUES($1,'worker',$2)",
            remaining["id"],
            expert["linked_id"],
        )
    with pytest.raises(HTTPException) as denied:
        await database.delete_user(str(departing["id"]))
    assert denied.value.status_code == 409
    assert "defaults" in denied.value.detail
    assert await ManifestStore(database).by_id(expert["id"]) == expert
    if target == "project":
        assert (
            await database.get_project_default_expert(
                project_id=str(project["id"]), expert_type="worker"
            )
        )["id"] == str(expert["linked_id"])
    else:
        assert (
            await database.fetchval(
                "SELECT expert_id FROM user_expert_defaults WHERE user_id=$1",
                remaining["id"],
            )
            == expert["linked_id"]
        )


@pytest.mark.asyncio
async def test_shared_project_and_expert_keep_their_existing_owner_authority(database):
    departing, legacy_project = await owner(database, "Project creator")
    remaining, remaining_project = await owner(database, "Remaining owner")
    await database.add_project_member(
        str(legacy_project["id"]), str(remaining["id"]), "owner"
    )
    legacy_resource = await ManifestStore(database).by_link(
        "Project", legacy_project["id"]
    )
    document = {
        "apiVersion": "srw/v1alpha1",
        "kind": "Project",
        "metadata": {"name": "shared-team"},
        "spec": {
            "resources": {
                "experts": {
                    "developer": {
                        "inline": {"runtime": {"image": "example.invalid/helper:v1"}}
                    }
                }
            },
            "defaults": {"expert": "developer"},
        },
    }
    service = ManifestResourceService(database)
    created = await service.apply(json.dumps(document), departing, format="json")
    project_view = next(
        item for item in created["resources"] if item["resource"]["kind"] == "Project"
    )
    project = await ManifestStore(database).by_id(project_view["uid"])
    project_id = project["linked_id"]
    await database.add_project_member(str(project_id), str(remaining["id"]), "owner")
    shared_secret = await secret(database, departing["id"], "Project", project_id)
    other_account_secret = await secret(
        database, departing["id"], "Account", remaining["id"]
    )
    default = await database.get_project_default_expert(
        project_id=str(project_id), expert_type="worker"
    )
    unrelated = await ManifestStore(database).by_link(
        "Project", remaining_project["id"]
    )

    assert await database.delete_user(str(departing["id"])) is True
    kept = await database.get_project_default_expert(
        project_id=str(project_id), expert_type="worker"
    )
    assert kept["id"] == default["id"]
    assert kept["owner_id"] == remaining["id"]
    assert kept["manifest_revision"] == default["manifest_revision"]
    assert (
        await database.get_user_role_in_project(str(project_id), str(remaining["id"]))
        == "owner"
    )
    assert (
        await database.fetchval(
            "SELECT owner_id FROM srw_resource_secrets WHERE id=$1", shared_secret
        )
        is None
    )
    assert (
        await database.fetchval(
            "SELECT owner_id FROM srw_resource_secrets WHERE id=$1",
            other_account_secret,
        )
        == remaining["id"]
    )
    assert (
        await ManifestStore(database).by_link("Project", remaining_project["id"])
        == unrelated
    )
    # The old Account-shaped Project identity still authorizes by real Project
    # membership. Its remaining owner can update it without becoming admin.
    change = deepcopy(project["document"])
    change["spec"]["description"] = "Maintained by its remaining owner"
    await service.apply(
        json.dumps(change),
        {**remaining, "is_admin": False},
        format="json",
        expected_versions={resource_key(change): project["resource_version"]},
    )
    changed = await ManifestStore(database).by_link("Project", project_id)
    assert changed["id"] == project["id"]
    assert (
        changed["document"]["metadata"]["scope"]
        == project["document"]["metadata"]["scope"]
    )
    assert changed["document"]["spec"]["description"] == change["spec"]["description"]
    await database.update_project(
        str(legacy_project["id"]),
        default_config_override={"settings": {"shared": True}},
    )
    legacy_changed = await ManifestStore(database).by_link(
        "Project", legacy_project["id"]
    )
    assert legacy_changed["id"] == legacy_resource["id"]
    assert (
        legacy_changed["document"]["metadata"]["scope"]
        == legacy_resource["document"]["metadata"]["scope"]
    )


@pytest.mark.asyncio
async def test_shared_expert_without_successor_does_not_grant_a_viewer_ownership(
    database,
):
    departing, project = await owner(database, "Sole owner")
    viewer, _ = await owner(database, "Viewer")
    await database.add_project_member(str(project["id"]), str(viewer["id"]), "viewer")
    document = {
        "apiVersion": "srw/v1alpha1",
        "kind": "Expert",
        "metadata": {
            "name": "project-helper",
            "scope": {"kind": "Project", "name": str(project["id"])},
        },
        "spec": {"runtime": {"image": "example.invalid/helper:v1"}},
    }
    result = await ManifestResourceService(database).apply(
        json.dumps(document), departing, format="json"
    )
    expert = await ManifestStore(database).by_id(result["resources"][0]["uid"])
    with pytest.raises(HTTPException) as denied:
        await database.delete_user(str(departing["id"]))
    assert denied.value.status_code == 409
    assert "authorized owner" in denied.value.detail
    assert (await ManifestStore(database).by_id(expert["id"]))["owner_id"] == departing[
        "id"
    ]
    assert (
        await database.get_user_role_in_project(str(project["id"]), str(viewer["id"]))
        == "viewer"
    )
