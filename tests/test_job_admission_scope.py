"""Authority preparation can run with isolated dependencies and no startup.

These tests cover the extraction boundary; real HTTP tests separately preserve
authentication, later admission gates, response JSON and side-effect ordering.
"""

import asyncio
import copy
from dataclasses import replace
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
import pytest

from orchestrator.schemas.job_create import JobCreate
from orchestrator.services.job_admission_scope import (
    JobAdmissionActor,
    JobAdmissionScopeDependencies,
    prepare_job_admission_scope,
)


USER = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
PARENT = "33333333-3333-4333-8333-333333333333"
THREAD = "44444444-4444-4444-8444-444444444444"
OTHER = "55555555-5555-4555-8555-555555555555"


@pytest.fixture
def dependencies():
    principal = {"id": USER, "is_approved": True, "is_admin": False}
    return JobAdmissionScopeDependencies(
        store=SimpleNamespace(
            get_thread=AsyncMock(
                return_value={"id": THREAD, "user_id": USER, "project_id": PROJECT}
            ),
            get_job=AsyncMock(
                return_value={"id": PARENT, "user_id": USER, "project_id": PROJECT}
            ),
            get_user=AsyncMock(return_value=principal),
        ),
        thread_project_ids=AsyncMock(return_value=[]),
        revalidate_thread_project_ids=AsyncMock(side_effect=lambda _, ids: ids),
        authenticate_forwarded_user=AsyncMock(return_value=(principal, None)),
        authorize_upload_reference=Mock(return_value={}),
    )


async def internal(dependencies, *, actor=None, **fields):
    return await prepare_job_admission_scope(
        command=JobCreate(description="scope fixture", **fields),
        actor=actor or JobAdmissionActor(),
        origin="internal_rest",
        dependencies=dependencies,
    )


def test_scope_import_does_not_load_auth_database_or_application_startup():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from orchestrator.services.job_admission_scope import prepare_job_admission_scope
for prefix in ('orchestrator.main', 'orchestrator.security.auth',
               'orchestrator.database', 'agent', 'shared.runtime'):
    assert not any(name == prefix or name.startswith(prefix + '.') for name in sys.modules), prefix
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr


@pytest.mark.asyncio
async def test_context_overlay_uses_actor_owner_and_authorizes_all_uploads(
    dependencies,
):
    context = {
        "upload_id": "context-document",
        "config_upload_id": "context-config",
        "instructions_upload_id": "context-instructions",
        "instructions": "old",
        "keep": {"nested": True},
    }
    command = JobCreate(
        description="prepare",
        user_id=OTHER,
        project_id=PROJECT,
        context=context,
        upload_id="body-document",
        instructions="new",
        kickoff_message="begin",
        required_deliverables=["output/proof.txt", "output/proof.txt"],
    )
    before = copy.deepcopy(command.model_dump())
    actor = JobAdmissionActor(principal={"id": USER})
    scope = await prepare_job_admission_scope(
        command=command, actor=actor, origin="user_rest", dependencies=dependencies
    )
    assert scope.user_id == USER
    assert scope.project_id == PROJECT
    assert scope.origin_bound is False
    assert scope.context == {
        **context,
        "upload_id": "body-document",
        "instructions": "new",
        "kickoff_message": "begin",
        "required_deliverables": ["output/proof.txt"],
    }
    assert command.model_dump() == before
    assert scope.context is not command.context
    assert [
        call.args for call in dependencies.authorize_upload_reference.call_args_list
    ] == [
        (actor.principal, "body-document"),
        (actor.principal, "context-config"),
        (actor.principal, "context-instructions"),
    ]
    assert all(
        call.kwargs == {"internal": False}
        for call in dependencies.authorize_upload_reference.call_args_list
    )
    dependencies.store.get_user.assert_not_awaited()
    dependencies.authenticate_forwarded_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_deliverable_precedes_origin_reads_and_forwarded_auth(
    dependencies,
):
    # Internal Python callers may construct a command without Pydantic ingress.
    command = JobCreate.model_construct(
        description="invalid", required_deliverables=["../escape.txt"], thread_id=THREAD
    )
    with pytest.raises(ValueError):
        await prepare_job_admission_scope(
            command=command,
            actor=JobAdmissionActor(forwarded_user_id=USER),
            origin="internal_rest",
            dependencies=dependencies,
        )
    dependencies.store.get_thread.assert_not_awaited()
    dependencies.authenticate_forwarded_user.assert_not_awaited()
    dependencies.authorize_upload_reference.assert_not_called()


@pytest.mark.asyncio
async def test_thread_column_scope_is_revalidated_before_parent_owner_and_upload(
    dependencies,
):
    order = Mock()
    for name, collaborator in (
        ("thread", dependencies.store.get_thread),
        ("projects", dependencies.thread_project_ids),
        ("revalidate", dependencies.revalidate_thread_project_ids),
        ("parent", dependencies.store.get_job),
        ("owner", dependencies.store.get_user),
        ("upload", dependencies.authorize_upload_reference),
    ):
        order.attach_mock(collaborator, name)
    scope = await internal(
        dependencies,
        thread_id=THREAD,
        parent_job_id=PARENT,
        upload_id="owned",
        actor=JobAdmissionActor(forwarded_user_id=USER),
    )
    assert scope.user_id == USER and scope.project_id == PROJECT and scope.origin_bound
    assert [call[0] for call in order.mock_calls] == [
        "thread",
        "projects",
        "revalidate",
        "parent",
        "owner",
        "upload",
    ]
    assert dependencies.revalidate_thread_project_ids.await_args.args[1] == [PROJECT]
    dependencies.authenticate_forwarded_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [HTTPException(403, "private cause"), RuntimeError("offline")]
)
async def test_scope_read_failures_hide_reason_and_never_reach_upload(
    dependencies, failure
):
    dependencies.revalidate_thread_project_ids.side_effect = failure
    with pytest.raises(HTTPException) as exc:
        await internal(
            dependencies, thread_id=THREAD, parent_job_id=PARENT, upload_id="x"
        )
    assert (exc.value.status_code, exc.value.detail) == (
        403,
        "Internal job origin scope is unavailable",
    )
    dependencies.store.get_job.assert_not_awaited()
    dependencies.authenticate_forwarded_user.assert_not_awaited()
    dependencies.authorize_upload_reference.assert_not_called()


@pytest.mark.asyncio
async def test_archived_refusal_preserves_exception_and_stops_before_parent(
    dependencies,
):
    refusal = HTTPException(409, "Project is archived")
    dependencies.revalidate_thread_project_ids.side_effect = refusal
    with pytest.raises(HTTPException) as exc:
        await internal(dependencies, thread_id=THREAD, parent_job_id=PARENT)
    assert exc.value is refusal
    dependencies.store.get_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict",
    [
        "owners",
        "forwarded",
        "body-owner",
        "project",
        "unscoped-parent",
        "revoked-owner",
        "missing-thread",
        "missing-parent",
    ],
)
async def test_internal_authority_conflicts_never_reach_upload(dependencies, conflict):
    fields = {"thread_id": THREAD, "parent_job_id": PARENT, "upload_id": "x"}
    if conflict == "owners":
        dependencies.store.get_job.return_value["user_id"] = OTHER
    elif conflict == "forwarded":
        fields["actor"] = JobAdmissionActor(forwarded_user_id=OTHER)
    elif conflict == "body-owner":
        fields["user_id"] = OTHER
    elif conflict == "project":
        fields["project_id"] = OTHER
    elif conflict == "unscoped-parent":
        dependencies.store.get_job.return_value["project_id"] = None
    elif conflict == "revoked-owner":
        dependencies.store.get_user.return_value = None
    elif conflict == "missing-thread":
        dependencies.store.get_thread.return_value = None
    else:
        dependencies.store.get_job.return_value = None
    with pytest.raises(HTTPException) as exc:
        await internal(dependencies, **fields)
    assert (exc.value.status_code, exc.value.detail) == (
        403,
        "Internal job origin scope is unavailable",
    )
    dependencies.authorize_upload_reference.assert_not_called()
    dependencies.authenticate_forwarded_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [USER, None])
async def test_unscoped_parent_remains_bound_and_only_ownerless_child_bypasses_upload_owner(
    dependencies, owner
):
    dependencies.store.get_job.return_value.update(user_id=owner, project_id=None)
    scope = await internal(dependencies, parent_job_id=PARENT, upload_id="x")
    assert scope.user_id == owner and scope.project_id is None and scope.origin_bound
    assert dependencies.authorize_upload_reference.call_args.kwargs == {
        "internal": owner is None
    }
    assert dependencies.store.get_user.await_count == (owner is not None)


@pytest.mark.asyncio
async def test_forwarded_principal_is_fresh_and_project_scope_is_enforced(dependencies):
    principal = {"id": USER, "is_approved": True}
    dependencies.authenticate_forwarded_user.return_value = principal, PROJECT
    actor = JobAdmissionActor(forwarded_user_id=USER)
    scope = await internal(dependencies, actor=actor)
    assert scope.principal is principal and scope.project_id == PROJECT
    assert scope.origin_bound is False
    with pytest.raises(HTTPException) as exc:
        await internal(dependencies, actor=actor, project_id=OTHER, upload_id="x")
    assert exc.value.status_code == 403
    refusal = HTTPException(403, "Account pending approval")
    dependencies.authenticate_forwarded_user.side_effect = refusal
    with pytest.raises(HTTPException) as exc:
        await internal(dependencies, actor=actor, upload_id="x")
    assert exc.value is refusal
    assert dependencies.authenticate_forwarded_user.await_count == 3
    dependencies.authorize_upload_reference.assert_not_called()


@pytest.mark.asyncio
async def test_bare_internal_identity_cannot_bypass_upload_owner(dependencies):
    with pytest.raises(HTTPException) as exc:
        await internal(dependencies, user_id=USER, upload_id="x")
    assert exc.value.status_code == 403
    dependencies.authenticate_forwarded_user.assert_not_awaited()
    dependencies.authorize_upload_reference.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_operations_keep_dependencies_and_principals_separate(
    dependencies,
):
    # Interleave at the first authority read. A cached singleton/callback would
    # bind one invocation to the other app's store, principal or upload gate.
    both_entered = asyncio.Event()
    entered = 0

    def make_store(owner):
        async def parent(_job_id):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=2)
            return {"user_id": owner, "project_id": None}

        return SimpleNamespace(
            get_job=parent, get_user=AsyncMock(return_value={"id": owner})
        )

    first = replace(
        dependencies, store=make_store(USER), authorize_upload_reference=Mock()
    )
    second = replace(
        dependencies, store=make_store(OTHER), authorize_upload_reference=Mock()
    )
    scopes = await asyncio.gather(
        internal(first, parent_job_id=PARENT, upload_id="first"),
        internal(second, parent_job_id=PARENT, upload_id="second"),
    )
    assert [scope.user_id for scope in scopes] == [USER, OTHER]
    first.store.get_user.assert_awaited_once_with(USER)
    second.store.get_user.assert_awaited_once_with(OTHER)
    first.authorize_upload_reference.assert_called_once_with(
        {"id": USER}, "first", internal=False
    )
    second.authorize_upload_reference.assert_called_once_with(
        {"id": OTHER}, "second", internal=False
    )
