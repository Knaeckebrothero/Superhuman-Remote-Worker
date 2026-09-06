"""HTTP contract of the existing public/internal job creation funnel.

Only external identity/storage/provisioning collaborators are controlled. The
actual routes, Pydantic ingress, scope resolution and response redaction run.
No application startup, dispatch, provider or database connection is started.
"""

import copy
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
