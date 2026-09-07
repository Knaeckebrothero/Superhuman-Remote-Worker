"""HTTP contract of the existing public/internal job creation funnel.

Only external identity/storage/provisioning collaborators are controlled. The
actual routes, Pydantic ingress, scope resolution and response redaction run.
No application startup, dispatch, provider or database connection is started.
"""

import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from orchestrator import main
from orchestrator.services.default_experts import ExpertSelection


USER = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
JOB = "33333333-3333-4333-8333-333333333333"
EXPERT = "44444444-4444-4444-8444-444444444444"
PARENT = "55555555-5555-4555-8555-555555555555"
CONNECTOR = "66666666-6666-4666-8666-666666666666"
STAMP = datetime(2026, 9, 6, 8, 0, tzinfo=timezone.utc)
THREAD = "77777777-7777-4777-8777-777777777777"
PATH = "/api/jobs"
PROJECT_PATH = f"/api/projects/{PROJECT}/jobs"


@pytest.fixture
def wire(monkeypatch):
    user = {"id": USER, "is_admin": False, "is_approved": True}

    async def approved(request, _db):
        if not request.headers.get("x-test-user"):
            raise HTTPException(401, "Authentication required")
        return user

    async def insert(**kwargs):
        return {
            "id": UUID(JOB),
            "description": kwargs["description"],
            "config_name": kwargs["config_name"],
            "status": "created",
            "created_at": STAMP,
            "assigned_agent_id": None,
            "user_id": UUID(kwargs["user_id"]),
            "project_id": UUID(kwargs["project_id"]),
            "context": copy.deepcopy(kwargs["context"]),
            "config_override": copy.deepcopy(kwargs["config_override"]),
            "workspace_contract": {"state": "unassigned"},
            # A real create returns its row, not a filtered ID acknowledgement.
            "existing_extension": {"nullable": None},
        }

    db = SimpleNamespace(
        create_job=AsyncMock(side_effect=insert),
        get_user=AsyncMock(return_value=user),
        get_project=AsyncMock(return_value={"id": PROJECT}),
        get_user_role_in_project=AsyncMock(return_value="editor"),
        get_job=AsyncMock(
            side_effect=lambda job_id: (
                {"id": PARENT, "user_id": USER, "project_id": PROJECT}
                if str(job_id) == PARENT
                else None
            )
        ),
        resolve_datasources_for_thread=AsyncMock(return_value=[]),
    )
    defaults = AsyncMock(return_value=([CONNECTOR], {}))
    authorize = AsyncMock(side_effect=lambda _actor, ids, **kw: (list(ids), {}))
    expert = AsyncMock(
        return_value=ExpertSelection(
            expert={"id": EXPERT, "expert_type": "worker", "owner_id": None},
            source="application",
        )
    )
    provision, dispatch = AsyncMock(), Mock()
    monkeypatch.setattr(main, "postgres_db", db)
    monkeypatch.setattr(main, "require_approved_user", approved)
    monkeypatch.setattr(main, "require_project_member", AsyncMock())
    monkeypatch.setattr(
        main,
        "is_internal_call",
        lambda request: bool(request.headers.get("x-test-internal")),
    )
    monkeypatch.setattr(main, "_enforce_readiness_gate", AsyncMock())
    monkeypatch.setattr(main, "_is_experts_db_enabled", lambda: True)
    monkeypatch.setattr(main, "_user_experts_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "resolve_root_expert", expert)
    monkeypatch.setattr(main, "_experts_cache", [SimpleNamespace(id="developer")])
    monkeypatch.setattr(main, "_authorize_thread_datasource_selection", authorize)
    monkeypatch.setattr(main, "_datasource_defaults_on_omission", lambda: False)
    monkeypatch.setattr(main, "_enforce_job_create_grants", AsyncMock())
    monkeypatch.setattr(main, "STATELESS_WORKER_DEFAULT_ENABLED", False)
    monkeypatch.setattr(main, "_spawn_scholar_subjob", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "_trigger_dispatch", dispatch)
    monkeypatch.setattr(
        "orchestrator.services.datasource_policy.default_datasource_selection", defaults
    )
    monkeypatch.setattr(
        "orchestrator.services.job_provisioning.provision_job_repo", provision
    )
    app = FastAPI()
    app.add_api_route(PATH, main.create_job, methods=["POST"])
    app.add_api_route(
        "/api/projects/{project_id}/jobs", main.create_project_job, methods=["POST"]
    )
    return SimpleNamespace(
        app=app,
        db=db,
        defaults=defaults,
        authorize=authorize,
        expert=expert,
        provision=provision,
        dispatch=dispatch,
    )


async def submit(wire, payload, path=PATH, **headers):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wire.app), base_url="http://create.test"
    ) as client:
        return await client.post(
            path, json=payload, headers={"x-test-user": USER, **headers}
        )


def body(**fields):
    return {"description": "controlled HTTP fixture", "project_id": PROJECT, **fields}


@pytest.fixture
def officer_wire(wire, monkeypatch):
    """Run real Officer snapshot/slot policy; control reads and the final write."""
    metadata = {"config_override": {"officer": {"enabled": True}}}
    thread = {
        "id": THREAD,
        "user_id": USER,
        "project_id": PROJECT,
        "status": "active",
        "metadata": metadata,
    }
    wire.db.get_thread = AsyncMock(return_value=thread)
    wire.db.get_project_officer_lineage = AsyncMock(return_value=[THREAD])
    snapshot = {
        "project_id": PROJECT,
        "thread_id": THREAD,
        "config_override": {},
        "incarnations": [],
        "post_updated_at": STAMP,
        "current_thread_id": THREAD,
        "thread_project_id": PROJECT,
        "thread_status": "active",
        "thread_metadata": metadata,
        "thread_user_id": USER,
        "thread_created_at": STAMP,
    }
    read_snapshot = AsyncMock(return_value=snapshot)

    @asynccontextmanager
    async def acquire():
        # No transaction or write methods: preparation must only read.
        yield SimpleNamespace(fetchrow=read_snapshot)

    wire.db.acquire = acquire
    monkeypatch.setattr(main, "_thread_project_ids", AsyncMock(return_value=[PROJECT]))
    monkeypatch.setattr(
        main, "_revalidate_thread_project_ids", AsyncMock(return_value=[PROJECT])
    )
    ticket = AsyncMock(
        return_value={
            "project_id": PROJECT,
            "status": "active",
            "note_type": "feature",
            "tags": ["ready", "category:researcher"],
            "ready_at": STAMP,
        }
    )
    monkeypatch.setattr(
        "orchestrator.services.project_backlog.fetch_ticket_state", ticket
    )

    async def insert(_db, *, job_kwargs, **kwargs):
        return await wire.db.create_job(**job_kwargs)

    admit = AsyncMock(side_effect=insert)
    monkeypatch.setattr(
        "orchestrator.services.officer_admission.admit_and_create_job", admit
    )
    preflight = AsyncMock(
        return_value=SimpleNamespace(
            activated=True, state="ready", retryable=False, phase="ready", error=None
        )
    )
    monkeypatch.setattr(
        "orchestrator.services.officer_preflight.ensure_officer_job_activated",
        preflight,
    )
    wire.officer = SimpleNamespace(
        thread=thread,
        snapshot=snapshot,
        read_snapshot=read_snapshot,
        ticket=ticket,
        admit=admit,
        preflight=preflight,
    )
    return wire


def assert_no_admission_effects(wire):
    wire.authorize.assert_not_awaited()
    main._enforce_job_create_grants.assert_not_awaited()
    wire.db.create_job.assert_not_awaited()
    wire.officer.admit.assert_not_awaited()
    wire.officer.preflight.assert_not_awaited()
    wire.provision.assert_not_awaited()
    wire.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_officer_dependency_factory_captures_stores_without_reading_them(
    wire, monkeypatch
):
    from orchestrator.services import officer_admission, project_backlog

    snapshot = AsyncMock()
    ticket = AsyncMock()
    monkeypatch.setattr(officer_admission, "prepare_officer_admission", snapshot)
    monkeypatch.setattr(project_backlog, "fetch_ticket_state", ticket)
    first_store, first_vector = object(), object()
    second_store, second_vector = object(), object()
    monkeypatch.setattr(main, "postgres_db", first_store)
    monkeypatch.setattr(main, "vector_db", first_vector)
    first = main._job_admission_officer_dependencies()
    monkeypatch.setattr(main, "postgres_db", second_store)
    monkeypatch.setattr(main, "vector_db", second_vector)
    second = main._job_admission_officer_dependencies()
    snapshot.assert_not_called()
    ticket.assert_not_called()
    assert first.store is first_store and second.store is second_store
    kwargs = dict(
        project_id=PROJECT,
        thread_id=THREAD,
        requested_slot=None,
        requested_config_override=None,
    )
    await first.prepare_officer(**kwargs)
    snapshot.assert_awaited_once_with(first_store, **kwargs)
    await first.fetch_ticket(PROJECT, "first")
    ticket.assert_awaited_once_with(first_vector, PROJECT, "first")
    await second.prepare_officer(**kwargs)
    assert snapshot.await_args.args == (second_store,)
    await second.fetch_ticket(PROJECT, "second")
    assert ticket.await_args.args == (second_vector, PROJECT, "second")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,code",
    [
        ("retired", "officer_disabled"),
        ("orphan", "post_missing"),
        ("duplicate", "stale_incarnation"),
        ("held", "officer_held"),
        ("wrong_project", "project_mismatch"),
    ],
)
async def test_officer_candidate_refusals_precede_ticket_and_all_effects(
    officer_wire, failure, code
):
    wire = officer_wire
    meta = wire.officer.thread["metadata"]["config_override"]["officer"]
    if failure == "retired":
        meta["enabled"] = False
    elif failure == "orphan":
        wire.db.get_project_officer_lineage.return_value = []
        wire.officer.read_snapshot.return_value = None
    elif failure == "duplicate":
        wire.db.get_project_officer_lineage.return_value = []
        wire.officer.snapshot["thread_id"] = PARENT
    elif failure == "held":
        meta["hold"] = {"reason": "conference"}
    else:
        wire.officer.snapshot["thread_project_id"] = PARENT
    response = await submit(
        wire, body(thread_id=THREAD, ticket="fixture"), **{"x-test-internal": "1"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == code
    wire.officer.ticket.assert_not_awaited()
    assert_no_admission_effects(wire)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["thread", "lineage", "both", "neither"])
async def test_ordinary_session_candidate_lookup_failures_keep_existing_behavior(
    officer_wire, failure
):
    wire = officer_wire
    thread = wire.officer.thread
    thread["metadata"]["config_override"]["officer"]["enabled"] = False
    # The first thread read is scope authorization; only the later Officer
    # candidate read has historical best-effort behavior.
    wire.db.get_thread.side_effect = [
        thread,
        RuntimeError("unavailable") if failure in {"thread", "both"} else thread,
        thread,  # the later datasource-inheritance read
    ]
    wire.db.get_project_officer_lineage.return_value = []
    if failure in {"lineage", "both"}:
        wire.db.get_project_officer_lineage.side_effect = RuntimeError("unavailable")
    response = await submit(wire, body(thread_id=THREAD), **{"x-test-internal": "1"})
    assert response.status_code == 200, response.text
    wire.officer.read_snapshot.assert_not_awaited()
    wire.officer.ticket.assert_not_awaited()
    wire.officer.admit.assert_not_awaited()
    wire.db.create_job.assert_awaited_once()
    wire.provision.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["thread", "lineage"])
async def test_either_candidate_signal_still_requires_authoritative_snapshot(
    officer_wire, lookup
):
    wire = officer_wire
    if lookup == "thread":
        wire.db.get_thread.side_effect = [
            wire.officer.thread,
            RuntimeError("unavailable"),
        ]
    else:
        wire.db.get_project_officer_lineage.side_effect = RuntimeError("unavailable")
    wire.officer.read_snapshot.return_value = None
    response = await submit(wire, body(thread_id=THREAD), **{"x-test-internal": "1"})
    assert response.status_code == 409, response.text
    assert response.json() == {
        "detail": {"code": "post_missing", "message": "Officer Post does not exist."}
    }
    wire.officer.read_snapshot.assert_awaited_once()
    assert_no_admission_effects(wire)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ticket_change,status,detail",
    [
        (
            "unavailable",
            503,
            "Backlog ticket authority is unavailable; no claim or job was created.",
        ),
        (
            None,
            409,
            "Backlog ticket 'fixture' does not exist in this Officer Post's project.",
        ),
        ({"project_id": PARENT}, 409, "Backlog ticket belongs to a different project."),
        (
            {"status": "archived"},
            409,
            "Backlog ticket 'fixture' is not an active backlog ticket.",
        ),
        (
            {"note_type": "reference"},
            409,
            "Backlog ticket 'fixture' is not an active backlog ticket.",
        ),
        (
            {"tags": ["category:researcher"]},
            409,
            "Backlog ticket 'fixture' is not ready with trusted Officer provenance.",
        ),
        (
            {"ready_at": STAMP.isoformat()},
            409,
            "Backlog ticket 'fixture' is not ready with trusted Officer provenance.",
        ),
    ],
)
async def test_officer_ticket_error_json_and_no_effects(
    officer_wire, ticket_change, status, detail
):
    wire = officer_wire
    if ticket_change == "unavailable":
        wire.officer.ticket.side_effect = RuntimeError("unavailable")
    elif ticket_change is None:
        wire.officer.ticket.return_value = None
    else:
        wire.officer.ticket.return_value.update(ticket_change)
    response = await submit(
        wire, body(thread_id=THREAD, ticket="fixture"), **{"x-test-internal": "1"}
    )
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    assert_no_admission_effects(wire)


@pytest.mark.asyncio
async def test_non_officer_ticket_refused_without_vector_or_write(officer_wire):
    wire = officer_wire
    wire.officer.thread["metadata"]["config_override"]["officer"]["enabled"] = False
    wire.db.get_project_officer_lineage.return_value = []
    response = await submit(
        wire, body(thread_id=THREAD, ticket="fixture"), **{"x-test-internal": "1"}
    )
    assert response.status_code == 409, response.text
    assert response.json() == {
        "detail": "Backlog ticket claims require the exact current commissioned Officer Post incarnation. Ad-hoc jobs must omit ticket."
    }
    wire.officer.ticket.assert_not_awaited()
    assert_no_admission_effects(wire)


@pytest.mark.asyncio
@pytest.mark.parametrize("naive", [False, True])
async def test_officer_slot_ticket_preparation_reaches_final_admission_in_order(
    officer_wire, monkeypatch, naive
):
    wire = officer_wire
    meta = wire.officer.thread["metadata"]["config_override"]["officer"]
    meta["slots"] = {
        "researchers": {
            "count": 1,
            "model": "fixture-model",
            "backend": "none",
            "category": "researcher",
        }
    }
    wire.officer.ticket.return_value["ready_at"] = (
        STAMP.replace(tzinfo=None) if naive else STAMP
    )
    order = Mock()
    vm_check = Mock(wraps=main._job_needs_vm)
    monkeypatch.setattr(main, "_job_needs_vm", vm_check)
    for name, collaborator in (
        ("snapshot", wire.officer.read_snapshot),
        ("ticket", wire.officer.ticket),
        ("vm", vm_check),
        ("datasources", wire.authorize),
        ("grants", main._enforce_job_create_grants),
        ("admit", wire.officer.admit),
        ("preflight", wire.officer.preflight),
    ):
        order.attach_mock(collaborator, name)
    response = await submit(
        wire,
        body(
            thread_id=THREAD,
            ticket="fixture",
            work_category="executor",
            context={
                "kickoff_message": "Existing brief",
                "instructions": "Keep instructions",
            },
        ),
        **{"x-test-internal": "1"},
    )
    assert response.status_code == 200, response.text
    assert [call[0] for call in order.mock_calls] == [
        "snapshot",
        "ticket",
        "vm",
        "grants",
        "datasources",
        "admit",
        "preflight",
    ]
    args = wire.officer.admit.await_args.kwargs
    assert args["ticket_ready_at"] == STAMP
    assert (
        args["ticket_claim_source"] == "manual" and args["strict_provisioning"] is True
    )
    assert args["preparation"].thread_id == THREAD
    assert args["preparation"].slot_name == "researchers"
    assert args["job_kwargs"]["config_override"]["llm"]["model"] == "fixture-model"
    context = args["job_kwargs"]["context"]
    assert (
        context["officer_slot"] == "researchers"
        and context["ticket_note_id"] == "fixture"
    )
    assert context["work_category"] == "researcher"
    assert context["instructions"] == "Keep instructions"
    assert context["kickoff_message"].startswith("Your deliverable is an ANSWER")
    assert (
        "dispatched this as executor work into the researchers slot"
        in context["kickoff_message"]
    )
    assert context["kickoff_message"].endswith("Existing brief")
    wire.officer.ticket.assert_awaited_once_with(main.vector_db, PROJECT, "fixture")
    wire.provision.assert_not_awaited()
    wire.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [PATH, PROJECT_PATH])
async def test_actual_create_json_keeps_row_nulls_extensions_and_serialization(
    wire, path
):
    response = await submit(wire, body(), path)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": JOB,
        "description": "controlled HTTP fixture",
        "config_name": "worker_base",
        "status": "created",
        "created_at": "2026-09-06T08:00:00Z",
        "assigned_agent_id": None,
        "user_id": USER,
        "project_id": PROJECT,
        "context": {"expert_selection": {"source": "application", "expert_id": EXPERT}},
        "config_override": None,
        "workspace_contract": {"state": "unassigned"},
        "existing_extension": {"nullable": None},
    }
    wire.db.create_job.assert_awaited_once()
    wire.provision.assert_awaited_once()
    wire.dispatch.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection,gate,expected,origin",
    [
        ({}, False, [], "omitted_compat"),
        ({}, True, [CONNECTOR], "default"),
        ({"datasource_ids": []}, True, [], "explicit"),
        ({"datasource_ids": [CONNECTOR]}, False, [CONNECTOR], "explicit"),
        ({"use_datasource_defaults": True}, False, [CONNECTOR], "default"),
    ],
)
async def test_datasource_wire_presence_preserves_selection_intent(
    wire,
    monkeypatch,
    selection,
    gate,
    expected,
    origin,
):
    monkeypatch.setattr(main, "_datasource_defaults_on_omission", lambda: gate)
    response = await submit(wire, body(**selection))
    assert response.status_code == 200, response.text
    args = wire.db.create_job.await_args.kwargs
    assert args["datasource_ids"] == expected
    assert args["datasource_selection_provenance"]["origin"] == origin
    assert wire.defaults.await_count == (origin == "default")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"datasource_ids": None},
        {"datasource_ids": [], "use_datasource_defaults": True},
        {"required_deliverables": ["../outside.txt"]},
        {"priority": 11},
    ],
)
async def test_validation_errors_are_json_arrays_and_never_insert(wire, fields):
    response = await submit(wire, body(**fields))
    assert response.status_code == 422, response.text
    assert isinstance(response.json()["detail"], list)
    assert response.json()["detail"][0]["loc"][0] == "body"
    wire.db.create_job.assert_not_awaited()
    wire.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector,expected_config,expected_expert",
    [
        ({"expert": "developer"}, "developer", None),
        ({"expert": EXPERT}, "worker_base", EXPERT),
        ({"config_name": "developer"}, "developer", None),
        ({"expert_id": EXPERT}, "worker_base", EXPERT),
        ({"expert": "developer", "config_name": "developer"}, "developer", None),
    ],
)
async def test_expert_aliases_reach_the_same_create_command(
    wire, selector, expected_config, expected_expert
):
    response = await submit(wire, body(**selector))
    assert response.status_code == 200, response.text
    kwargs = wire.db.create_job.await_args.kwargs
    assert (kwargs["config_name"], kwargs["expert_id"]) == (
        expected_config,
        expected_expert,
    )


@pytest.mark.asyncio
async def test_conflicting_alias_is_a_string_error_before_any_insert(wire):
    response = await submit(wire, body(expert="developer", expert_id=EXPERT))
    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)
    wire.db.create_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [PATH, PROJECT_PATH])
async def test_public_identity_and_authority_injection_are_stripped_without_new_rejection(
    wire, path
):
    response = await submit(
        wire,
        body(
            user_id=PARENT,
            thread_id=PARENT,
            parent_job_id=PARENT,
            creation_order=2,
            worktree_path="foreign",
            delegation_context="foreign",
            builder_session_id=PARENT,
            context={
                "keep": "yes",
                "officer_admission": {"forged": True},
                "required_deliverables": ["forged"],
                "vm": {"host": "private"},
                "nested": [{"repository_credentials": "synthetic", "keep": 1}],
            },
            config_override={
                "lifecycle_marker": "forged",
                "extra": {"repository_auth": "synthetic"},
            },
        ),
        path,
    )
    assert response.status_code == 200, response.text
    kwargs = wire.db.create_job.await_args.kwargs
    assert kwargs["user_id"] == USER
    assert all(
        kwargs[key] is None
        for key in [
            "parent_job_id",
            "creation_order",
            "worktree_path",
            "delegation_context",
            "created_by_thread_id",
        ]
    )
    assert kwargs["context"] == {
        "keep": "yes",
        "nested": [{"keep": 1}],
        "expert_selection": {"source": "application", "expert_id": EXPERT},
    }
    assert kwargs["config_override"] == {"extra": {}}


@pytest.mark.asyncio
async def test_real_internal_scope_keeps_parent_and_rejects_forged_owner(wire):
    payload = body(parent_job_id=PARENT, creation_order=2, datasource_ids=[])
    response = await submit(wire, payload, **{"x-test-internal": "1"})
    assert response.status_code == 200, response.text
    kwargs = wire.db.create_job.await_args.kwargs
    assert kwargs["parent_job_id"] == PARENT and kwargs["creation_order"] == 2
    assert kwargs["user_id"] == USER and kwargs["project_id"] == PROJECT
    wire.db.create_job.reset_mock()
    response = await submit(
        wire, {**payload, "user_id": PARENT}, **{"x-test-internal": "1"}
    )
    assert response.status_code == 403
    wire.db.create_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_originless_internal_call_and_anonymous_public_call_do_not_write(wire):
    for headers in ({"x-test-internal": "1"}, {"x-test-user": ""}):
        response = await submit(wire, body(), **headers)
        assert response.status_code in (401, 403)
    wire.db.create_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_wrapper_overrides_body_project_after_editor_check(wire):
    response = await submit(wire, body(project_id=PARENT), PROJECT_PATH)
    assert response.status_code == 200, response.text
    assert wire.db.create_job.await_args.kwargs["project_id"] == PROJECT
    assert main.require_project_member.await_args_list[0].args[2] == PROJECT
    assert main.require_project_member.await_args_list[0].kwargs == {
        "min_role": "editor",
        "allow_archived": False,
    }


@pytest.mark.asyncio
async def test_project_denial_precedes_creation(wire):
    main.require_project_member.side_effect = HTTPException(
        403, "Project editor required"
    )
    response = await submit(wire, body(), PROJECT_PATH)
    assert response.status_code == 403
    wire.db.create_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_readiness_precedes_internal_authority_and_upload_checks(
    wire, monkeypatch
):
    main._enforce_readiness_gate.side_effect = HTTPException(503, "Not ready")
    upload = Mock()
    monkeypatch.setattr(main, "authorize_upload_reference", upload)
    response = await submit(
        wire,
        body(parent_job_id=PARENT, upload_id="unread"),
        **{"x-test-internal": "1"},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Not ready"}
    wire.db.get_job.assert_not_awaited()
    wire.db.get_user.assert_not_awaited()
    upload.assert_not_called()
    wire.db.create_job.assert_not_awaited()
    wire.provision.assert_not_awaited()
    wire.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_upload_refusal_precedes_project_expert_insert_and_provision(
    wire, monkeypatch
):
    upload = Mock(side_effect=HTTPException(403, "Upload belongs to another user"))
    monkeypatch.setattr(main, "authorize_upload_reference", upload)
    response = await submit(
        wire,
        body(parent_job_id=PARENT, upload_id="foreign"),
        **{"x-test-internal": "1"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Upload belongs to another user"}
    wire.db.get_job.assert_awaited_once_with(PARENT)
    wire.db.get_user.assert_awaited_once_with(USER)
    assert upload.call_args.args[0]["id"] == USER
    wire.db.get_user_role_in_project.assert_not_awaited()
    wire.db.get_project.assert_not_awaited()
    wire.expert.assert_not_awaited()
    wire.db.create_job.assert_not_awaited()
    wire.provision.assert_not_awaited()
    wire.dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal,status", [("role", 403), ("archived", 409)])
async def test_internal_project_refusal_never_reaches_expert_or_writes(
    wire, refusal, status
):
    if refusal == "role":
        wire.db.get_user_role_in_project.return_value = "viewer"
    else:
        wire.db.get_project.return_value = {"id": PROJECT, "status": "archived"}
    response = await submit(
        wire, body(parent_job_id=PARENT), **{"x-test-internal": "1"}
    )
    assert response.status_code == status, response.text
    wire.expert.assert_not_awaited()
    wire.authorize.assert_not_awaited()
    wire.db.create_job.assert_not_awaited()
    wire.provision.assert_not_awaited()
    wire.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_catalogue_stays_application_owned_and_only_scans_for_explicit_slug(
    wire, monkeypatch
):
    scan = Mock(return_value=[SimpleNamespace(id="developer")])
    monkeypatch.setattr(main, "_experts_cache", None)
    monkeypatch.setattr(main, "_scan_experts", scan)
    main._job_admission_config_dependencies()
    scan.assert_not_called()
    assert (
        await submit(wire, body(config_name="deployment/custom.yaml"))
    ).status_code == 200
    scan.assert_not_called()
    assert (await submit(wire, body(expert="developer"))).status_code == 200
    assert (await submit(wire, body(expert="developer"))).status_code == 200
    scan.assert_called_once_with()
    wire.provision.reset_mock()
    wire.db.create_job.reset_mock()
    response = await submit(wire, body(expert="absent"))
    assert response.status_code == 400 and "Unknown expert" in response.json()["detail"]
    scan.assert_called_once_with()
    wire.db.create_job.assert_not_awaited()
    wire.provision.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_and_expert_overrides_merge_before_request_at_real_insert(wire):
    wire.db.get_project.return_value = {
        "id": PROJECT,
        "default_config_override": '{"llm":{"model":"project","temperature":0.2},"extra":{"keep":true,"remove":1}}',
    }
    wire.expert.return_value = ExpertSelection(
        expert={"id": EXPERT},
        source="project",
        project_override={"llm": {"model": "expert"}, "extra": {"remove": None}},
    )
    response = await submit(wire, body(config_override={"llm": {"model": "request"}}))
    assert response.status_code == 200, response.text
    args = wire.db.create_job.await_args.kwargs
    assert args["config_override"] == {
        "llm": {"model": "request", "temperature": 0.2},
        "extra": {"keep": True},
    }
    assert args["context"]["expert_selection"] == {
        "source": "project",
        "expert_id": EXPERT,
    }
    assert args["expert_id"] == EXPERT
    wire.provision.assert_awaited_once()


@pytest.mark.asyncio
async def test_bench_adapter_revalidates_creator_and_preserves_provenance(
    wire, monkeypatch
):
    from orchestrator.routers import bench
    from orchestrator.security import access, auth

    # Exercise the retained real bench Request bridge and real MCP auth, with
    # a test-only transport key and external storage/provisioning controlled.
    monkeypatch.setenv("MCP_INTERNAL_KEY", "bench-fixture-key")
    monkeypatch.setattr(access, "_INTERNAL_KEY", "bench-fixture-key")
    monkeypatch.setattr(main, "is_internal_call", access.is_internal_call)
    monkeypatch.setattr(main, "require_approved_user", auth.require_approved_user)
    run = {"id": JOB, "created_by": USER, "spec": {"project_id": PROJECT}}
    task = {"id": "scope-test", "description": "bench admission"}
    arm = {"name": "baseline", "model": "fixture-model"}
    result = await bench._create_job_through_main(run, task, arm, 1)
    assert str(result["id"]) == JOB
    args = wire.db.create_job.await_args.kwargs
    assert args["user_id"] == USER and args["project_id"] == PROJECT
    assert args["datasource_ids"] == []
    assert args["context"]["bench"] == {
        "run_id": JOB,
        "task": "scope-test",
        "arm": "baseline",
        "replicate": 1,
    }
    assert args["datasource_selection_provenance"]["creation_path"] == "internal_rest"
    wire.db.create_job.reset_mock()
    wire.provision.reset_mock()
    wire.dispatch.reset_mock()
    wire.db.get_user.return_value["is_approved"] = False
    with pytest.raises(HTTPException) as exc:
        await bench._create_job_through_main(run, task, arm, 2)
    assert exc.value.status_code == 403
    wire.db.get_user.return_value = None
    with pytest.raises(HTTPException) as exc:
        await bench._create_job_through_main(run, task, arm, 3)
    assert exc.value.status_code == 401
    wire.db.create_job.assert_not_awaited()
    wire.provision.assert_not_awaited()
    wire.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_deliverable_contract_survives_http_validation_and_is_bound_before_insert(
    wire,
):
    response = await submit(
        wire, body(required_deliverables=["output/report.txt", "output/report.txt"])
    )
    assert response.status_code == 200, response.text
    args = wire.db.create_job.await_args.kwargs
    assert args["context"]["required_deliverables"] == ["output/report.txt"]
    assert args["delivery_contract"] is not None
    wire.db.resolve_datasources_for_thread.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_workspace_preserves_structured_error_without_writing(wire):
    response = await submit(
        wire, body(config_override={"workspace": {"backend": "unknown-tier"}})
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "invalid_workspace_backend"
    assert isinstance(response.json()["detail"]["message"], str)
    wire.db.create_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("as_string", [False, True])
async def test_success_keeps_jsonb_shape_and_redacts_private_workspace_fields(
    wire, as_string
):
    import json

    context = {"safe": "yes", "vm": {"host": "synthetic-private-host"}}
    config = {
        "llm": {"model": "fixture"},
        "workspace": {"remote": {"host": "synthetic-private-host"}},
    }
    wire.db.create_job.side_effect = None
    wire.db.create_job.return_value = {
        "id": UUID(JOB),
        "status": "created",
        "workspace_contract": {"state": "unassigned"},
        "context": json.dumps(context) if as_string else context,
        "config_override": json.dumps(config) if as_string else config,
    }
    response = await submit(wire, body())
    assert response.status_code == 200
    result = response.json()
    assert isinstance(result["context"], str if as_string else dict)
    assert isinstance(result["config_override"], str if as_string else dict)
    assert (json.loads(result["context"]) if as_string else result["context"]) == {
        "safe": "yes"
    }
    projected = (
        json.loads(result["config_override"])
        if as_string
        else result["config_override"]
    )
    assert projected == {"llm": {"model": "fixture"}, "workspace": {}}
    assert "synthetic-private-host" not in response.text
