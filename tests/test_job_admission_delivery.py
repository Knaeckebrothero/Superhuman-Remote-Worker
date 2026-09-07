"""Delivery preparation preserves binding authority and refusal receipts."""

import asyncio
from datetime import datetime, timezone
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

from orchestrator.schemas.job_create import JobCreate
from orchestrator.services.deliverable_contracts import DeliveryContractConflict
from orchestrator.services.job_admission_delivery import (
    JobAdmissionDeliveryDependencies,
    prepare_job_admission_delivery,
)
from orchestrator.services.officer_admission import (
    OfficerAdmissionConflict,
    OfficerAdmissionPreparation,
)


PROJECT = "11111111-1111-4111-8111-111111111111"
THREAD = "22222222-2222-4222-8222-222222222222"
DATASOURCE = "33333333-3333-4333-8333-333333333333"
STAMP = datetime(2026, 9, 7, 8, tzinfo=timezone.utc)


def repository(**fields):
    return {
        "id": DATASOURCE,
        "name": "Widget",
        "type": "repository",
        "connection_url": "https://github.com/Acme/Widget.git",
        "read_only": False,
        "project_read_only": False,
        "config": {"forge": "github"},
        "policy_revision": 7,
        **fields,
    }


@pytest.fixture
def deps():
    return JobAdmissionDeliveryDependencies(
        store=SimpleNamespace(
            resolve_datasources_for_thread=AsyncMock(return_value=[repository()])
        ),
        record_rejected_ticket_delivery_requirement=AsyncMock(return_value={}),
    )


@pytest.fixture
def preparation():
    return OfficerAdmissionPreparation(
        project_id=PROJECT,
        thread_id=THREAD,
        requested_slot=None,
        slot_name="developers",
        slot_patch={},
        category="developer",
        config_fingerprint="snapshot",
        incarnation=1,
        owner_user_id=None,
        require_auto_pull=False,
    )


async def prepare(deps, *, required_deliverables=None, ticket=None, **fields):
    return await prepare_job_admission_delivery(
        command=JobCreate(
            description="Delivery fixture",
            required_deliverables=required_deliverables,
            ticket=ticket,
        ),
        dependencies=deps,
        **{
            "context": {},
            "datasource_ids": [DATASOURCE],
            "target_project_ids": [PROJECT],
            "officer_preparation": None,
            "ticket_ready_at": None,
            **fields,
        },
    )


def test_import_does_not_load_database_auth_or_application_startup():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from orchestrator.services.job_admission_delivery import prepare_job_admission_delivery
for prefix in ('orchestrator.main', 'orchestrator.security',
               'orchestrator.database', 'agent', 'orchestrator.services.project_backlog'):
    assert not any(n == prefix or n.startswith(prefix + '.') for n in sys.modules), prefix
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [None, []])
async def test_empty_contract_removes_context_copy_without_connector_read(
    deps, requested
):
    nested = {"keep": True}
    context = {"required_deliverables": ["untrusted"], "nested": nested}
    deps.store.resolve_datasources_for_thread.side_effect = AssertionError(
        "Optional datasource service must not be read"
    )
    result = await prepare(deps, required_deliverables=requested, context=context)
    assert result is None
    assert context == {"nested": nested}
    assert context["nested"] is nested
    deps.store.resolve_datasources_for_thread.assert_not_awaited()
    deps.record_rejected_ticket_delivery_requirement.assert_not_awaited()


@pytest.mark.asyncio
async def test_canonical_pr_binding_uses_exact_selection_and_independent_record(deps):
    selection = [DATASOURCE]
    projects = [PROJECT]
    context = {"required_deliverables": ["untrusted"], "keep": True}
    result = await prepare(
        deps,
        required_deliverables=[
            " PR:Acme/Widget ",
            "pr:acme/widget",
            "repo/output/a.md",
        ],
        context=context,
        datasource_ids=selection,
        target_project_ids=projects,
    )
    assert result == {
        "deliverables": ["pr:acme/widget", "output/a.md"],
        "pr_repositories": ["acme/widget"],
        "pr_bindings": [
            {
                "repository": "acme/widget",
                "datasource_id": DATASOURCE,
                "forge": "github",
                "policy_revision": 7,
            }
        ],
        "digest": "8ec60620175faa51f0322a3116591245d58e95171e5dcb9c9ab9671424300409",
    }
    assert context == {
        "required_deliverables": ["pr:acme/widget", "output/a.md"],
        "keep": True,
    }
    args = deps.store.resolve_datasources_for_thread.await_args.args
    assert args[0] is selection and args[1] is projects
    result["deliverables"].append("kb:separate-record")
    assert context["required_deliverables"] == ["pr:acme/widget", "output/a.md"]
    deps.record_rejected_ticket_delivery_requirement.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_repository_contract_still_performs_declared_contract_lookup(deps):
    deps.store.resolve_datasources_for_thread.return_value = []
    context = {}
    result = await prepare(
        deps,
        required_deliverables=["repo/output/a.md", "output/a.md", "kb:findings"],
        context=context,
        datasource_ids=[],
        target_project_ids=[],
    )
    deps.store.resolve_datasources_for_thread.assert_awaited_once_with([], [])
    assert result["deliverables"] == ["output/a.md", "kb:findings"]
    assert result["pr_repositories"] == []
    assert result["pr_bindings"] == []
    assert context["required_deliverables"] == result["deliverables"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested,rows,code,fields",
    [
        (
            ["pr:acme/other"],
            [repository()],
            "pr_deliverable_not_attached",
            {"repository": "acme/other"},
        ),
        (
            ["pr:acme/widget"],
            [repository(read_only=True)],
            "pr_deliverable_read_only",
            {"repository": "acme/widget"},
        ),
        (
            ["pr:acme/widget"],
            [repository(project_read_only=True)],
            "pr_deliverable_read_only",
            {"repository": "acme/widget"},
        ),
        (
            ["pr:acme/widget", "pr:acme/other"],
            [repository()],
            "multiple_pr_deliverables_unsupported",
            {},
        ),
        (
            ["repos/Widget/output/a.md"],
            [],
            "external_repository_contract_unresolvable",
            {},
        ),
    ],
)
async def test_other_contract_refusals_never_record_ticket_receipt(
    deps, preparation, requested, rows, code, fields
):
    deps.store.resolve_datasources_for_thread.return_value = rows
    context = {"required_deliverables": ["keep-on-error"]}
    with pytest.raises(HTTPException) as caught:
        await prepare(
            deps,
            required_deliverables=requested,
            ticket="feature-widget",
            officer_preparation=preparation,
            ticket_ready_at=STAMP,
            context=context,
        )
    assert caught.value.status_code == 409
    cause = caught.value.__cause__
    assert isinstance(cause, DeliveryContractConflict)
    assert caught.value.detail == {"code": code, "message": cause.message, **fields}
    assert context == {"required_deliverables": ["keep-on-error"]}
    deps.record_rejected_ticket_delivery_requirement.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_repository_refusal_awaits_authoritative_receipt(
    deps, preparation
):
    receipt_started = asyncio.Event()
    release_receipt = asyncio.Event()

    async def record(**_kwargs):
        receipt_started.set()
        await release_receipt.wait()
        return {"required_pr_repositories": ["ignored-return-value"]}

    deps.record_rejected_ticket_delivery_requirement.side_effect = record
    context = {"required_deliverables": ["keep-on-error"]}
    task = asyncio.create_task(
        prepare(
            deps,
            required_deliverables=["./repos/Widget/output/a.md"],
            ticket="feature-widget",
            officer_preparation=preparation,
            ticket_ready_at=STAMP,
            context=context,
        )
    )
    await asyncio.wait_for(receipt_started.wait(), timeout=5)
    assert not task.done()
    assert context == {"required_deliverables": ["keep-on-error"]}
    release_receipt.set()
    with pytest.raises(HTTPException) as caught:
        await task
    assert caught.value.status_code == 409
    cause = caught.value.__cause__
    assert isinstance(cause, DeliveryContractConflict)
    assert caught.value.detail == {
        "code": "external_repository_requires_pr",
        "message": cause.message,
        "required_pr_deliverables": ["pr:acme/widget"],
        "required_pr_repositories": ["acme/widget"],
    }
    deps.record_rejected_ticket_delivery_requirement.assert_awaited_once_with(
        preparation=preparation,
        ticket_note_id="feature-widget",
        ticket_ready_at=STAMP,
        required_pr_repositories=["acme/widget"],
    )
    args = deps.record_rejected_ticket_delivery_requirement.await_args.kwargs
    assert args["preparation"] is preparation
    assert args["ticket_ready_at"] is STAMP
    assert context == {"required_deliverables": ["keep-on-error"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing", ["preparation", "ticket", "empty_ticket", "timestamp"]
)
async def test_external_refusal_without_complete_officer_authority_does_not_write(
    deps, preparation, missing
):
    with pytest.raises(HTTPException) as caught:
        await prepare(
            deps,
            required_deliverables=["repos/Widget/output/a.md"],
            ticket=(
                None
                if missing == "ticket"
                else ""
                if missing == "empty_ticket"
                else "feature-widget"
            ),
            officer_preparation=None if missing == "preparation" else preparation,
            ticket_ready_at=None if missing == "timestamp" else STAMP,
        )
    assert caught.value.detail["code"] == "external_repository_requires_pr"
    deps.record_rejected_ticket_delivery_requirement.assert_not_awaited()


@pytest.mark.asyncio
async def test_receipt_admission_conflict_takes_precedence_over_contract_refusal(
    deps, preparation
):
    failure = OfficerAdmissionConflict(
        "officer_held", "The Officer Post is held.", state="held", incarnation=2
    )
    deps.record_rejected_ticket_delivery_requirement.side_effect = failure
    context = {"required_deliverables": ["keep-on-error"]}
    with pytest.raises(HTTPException) as caught:
        await prepare(
            deps,
            required_deliverables=["repos/Widget/output/a.md"],
            ticket="feature-widget",
            officer_preparation=preparation,
            ticket_ready_at=STAMP,
            context=context,
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "officer_held",
        "message": "The Officer Post is held.",
        "state": "held",
        "incarnation": 2,
    }
    assert caught.value.__cause__ is failure
    assert context == {"required_deliverables": ["keep-on-error"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connector", "receipt"])
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_collaborator_failures_and_cancellation_propagate_unchanged(
    deps, preparation, stage, failure_type
):
    failure = failure_type("fixture failure")
    collaborator = (
        deps.store.resolve_datasources_for_thread
        if stage == "connector"
        else deps.record_rejected_ticket_delivery_requirement
    )
    collaborator.side_effect = failure
    context = {"required_deliverables": ["keep-on-error"]}
    with pytest.raises(failure_type) as caught:
        await prepare(
            deps,
            required_deliverables=["repos/Widget/output/a.md"],
            ticket="feature-widget",
            officer_preparation=preparation,
            ticket_ready_at=STAMP,
            context=context,
        )
    assert caught.value is failure
    assert context == {"required_deliverables": ["keep-on-error"]}
    if stage == "connector":
        deps.record_rejected_ticket_delivery_requirement.assert_not_awaited()


@pytest.mark.asyncio
async def test_suspended_bindings_keep_request_selections_and_contexts_separate():
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def resolve(datasource_ids, project_ids):
        assert datasource_ids == project_ids
        if datasource_ids == ["first"]:
            first_started.set()
            await release_first.wait()
        return []

    deps = JobAdmissionDeliveryDependencies(
        store=SimpleNamespace(resolve_datasources_for_thread=resolve),
        record_rejected_ticket_delivery_requirement=AsyncMock(),
    )
    first_context = {"required_deliverables": ["old-first"]}
    second_context = {"required_deliverables": ["old-second"]}
    first = asyncio.create_task(
        prepare(
            deps,
            required_deliverables=["kb:first"],
            context=first_context,
            datasource_ids=["first"],
            target_project_ids=["first"],
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=5)
    second_record = await prepare(
        deps,
        required_deliverables=["kb:second"],
        context=second_context,
        datasource_ids=["second"],
        target_project_ids=["second"],
    )
    assert first_context == {"required_deliverables": ["old-first"]}
    assert second_context == {"required_deliverables": ["kb:second"]}
    release_first.set()
    first_record = await first
    assert first_context == {"required_deliverables": ["kb:first"]}
    assert first_record["deliverables"] == ["kb:first"]
    assert second_record["deliverables"] == ["kb:second"]
    assert first_record["digest"] != second_record["digest"]
