"""Trusted bench submission revalidates creators and captures app dependencies."""

import asyncio
from copy import deepcopy
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
import pytest
from starlette.requests import Request

from orchestrator.routers import bench
from orchestrator.schemas.job_create import JobCreate
from orchestrator.security import auth
from orchestrator.services.job_admission_creator import authenticate_job_creator


USER = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"
PROJECT = "33333333-3333-4333-8333-333333333333"
ARM_PROJECT = "44444444-4444-4444-8444-444444444444"
EXPERT = "55555555-5555-4555-8555-555555555555"
APPROVAL_DETAIL = (
    "Account pending approval. An administrator must approve your account."
)


def fixtures():
    return (
        {"id": RUN, "created_by": UUID(USER), "spec": {"project_id": PROJECT}},
        {
            "id": "delivery",
            "description": "Produce the bench artifact",
            "config_name": "defaults",
            "required_deliverables": ["./output/result.md", "output/result.md"],
            "config_override": {
                "llm": {"model": "task-model", "temperature": 0.2},
                "extra": {"task": True, "winner": "task"},
            },
            "priority": 3,
        },
        {
            "name": "treatment",
            "model": "arm-model",
            "execution_lane": "stateless",
            "project_id": ARM_PROJECT,
            "config_override": {"extra": {"arm": True, "winner": "arm"}},
        },
    )


def test_creator_authentication_import_is_independent_of_transport_and_startup():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from orchestrator.services.job_admission_creator import authenticate_job_creator
for prefix in ('orchestrator.main', 'orchestrator.security.auth',
               'orchestrator.security.oidc', 'orchestrator.security.kc_client',
               'orchestrator.database', 'agent', 'shared.runtime'):
    assert not any(n == prefix or n.startswith(prefix + '.') for n in sys.modules), prefix
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("is_admin", [False, True, None])
async def test_current_creator_principal_retains_mcp_shape_without_scope_or_shadow(
    monkeypatch, is_admin
):
    monkeypatch.delenv("MCP_INTERNAL_KEY", raising=False)
    row = {
        "id": USER,
        "is_approved": True,
        "is_admin": is_admin,
        "default_project_id": PROJECT,
        "auth_method": "pat",
        "scopes": [f"project:{ARM_PROJECT}"],
        "real_is_admin": "stale",
        "display_name": "Creator",
    }
    store = SimpleNamespace(get_user=AsyncMock(return_value=row))
    principal, scoped_project = await authenticate_job_creator(USER, store)
    store.get_user.assert_awaited_once_with(USER)
    assert principal == {
        **row,
        "auth_method": "mcp",
        "scopes": [],
        "real_is_admin": bool(is_admin),
    }
    assert principal["is_admin"] is is_admin
    assert principal["default_project_id"] == PROJECT
    assert principal is not row
    assert scoped_project is None


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, {}])
async def test_deleted_creator_is_unauthenticated(row):
    store = SimpleNamespace(get_user=AsyncMock(return_value=row))
    with pytest.raises(HTTPException) as caught:
        await authenticate_job_creator(USER, store)
    assert caught.value.status_code == 401
    assert caught.value.detail == "Not authenticated"
    store.get_user.assert_awaited_once_with(USER)


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", [False, None, 0])
async def test_suspended_creator_is_refused_even_if_admin(approval):
    store = SimpleNamespace(
        get_user=AsyncMock(
            return_value={"id": USER, "is_approved": approval, "is_admin": True}
        )
    )
    with pytest.raises(HTTPException) as caught:
        await authenticate_job_creator(USER, store)
    assert caught.value.status_code == 403
    assert caught.value.detail == APPROVAL_DETAIL


@pytest.mark.asyncio
async def test_each_submission_observes_current_creator_admission_and_project():
    store = SimpleNamespace(
        get_user=AsyncMock(
            side_effect=[
                {"id": USER, "is_approved": True, "default_project_id": PROJECT},
                {"id": USER, "is_approved": True, "default_project_id": ARM_PROJECT},
                {"id": USER, "is_approved": False},
                None,
            ]
        )
    )
    first, _ = await authenticate_job_creator(USER, store)
    second, _ = await authenticate_job_creator(USER, store)
    assert first["default_project_id"] == PROJECT
    assert second["default_project_id"] == ARM_PROJECT
    for status in [403, 401]:
        with pytest.raises(HTTPException) as caught:
            await authenticate_job_creator(USER, store)
        assert caught.value.status_code == status
    assert store.get_user.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_creator_lookup_failure_propagates_unchanged(failure_type):
    failure = failure_type("fixture lookup failure")
    store = SimpleNamespace(get_user=AsyncMock(side_effect=failure))
    with pytest.raises(failure_type) as caught:
        await authenticate_job_creator(USER, store)
    assert caught.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("is_admin", [False, True])
@pytest.mark.parametrize("view_as", ["", "UsEr"])
async def test_http_approval_retains_existing_admin_shadow(
    monkeypatch, is_admin, view_as
):
    row = {"id": USER, "is_approved": True, "is_admin": is_admin}
    resolve = AsyncMock(return_value=row)
    monkeypatch.setattr(auth, "get_current_user", resolve)
    request = Request(
        {"type": "http", "headers": [(b"x-admin-view-as", view_as.encode())]}
    )
    store = object()
    result = await auth.require_approved_user(request, store)
    resolve.assert_awaited_once_with(request, store)
    assert result == {
        **row,
        "is_admin": False if view_as and is_admin else is_admin,
        "real_is_admin": is_admin,
    }
    assert row == {"id": USER, "is_approved": True, "is_admin": is_admin}


@pytest.mark.asyncio
async def test_http_approval_refusal_precedes_shadow_header_access(monkeypatch):
    row = {"id": USER, "is_admin": True}
    resolve = AsyncMock(return_value=row)
    monkeypatch.setattr(auth, "get_current_user", resolve)

    class NoHeaders:
        @property
        def headers(self):
            raise AssertionError("Approval must run before admin-shadow handling")

    with pytest.raises(HTTPException) as caught:
        await auth.require_approved_user(NoHeaders(), object())
    assert caught.value.status_code == 403
    assert caught.value.detail == APPROVAL_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize("expert_id", [None, EXPERT])
async def test_bench_adapter_passes_frozen_payload_and_actual_creator(
    monkeypatch, expert_id
):
    monkeypatch.delenv("MCP_INTERNAL_KEY", raising=False)
    monkeypatch.setattr(
        bench, "Request", Mock(side_effect=AssertionError("No fabricated request"))
    )
    run, task, arm = fixtures()
    if expert_id:
        arm["expert_id"] = expert_id
    originals = deepcopy((run, task, arm))
    result = {"id": "created-job"}
    create = AsyncMock(return_value=result)
    actual = await bench._create_job_through_admission(
        run, task, arm, 2, create_job=create
    )
    assert actual is result
    create.assert_awaited_once()
    creator_id, command = create.await_args.args
    assert creator_id == USER
    assert isinstance(command, JobCreate)
    assert str(command.user_id) == USER
    assert str(command.project_id) == ARM_PROJECT
    assert command.description == "Produce the bench artifact"
    assert command.config_name == "worker_base"
    assert command.expert_id == expert_id
    assert command.config_override == {
        "llm": {"model": "arm-model", "temperature": 0.2},
        "extra": {"task": True, "arm": True, "winner": "arm"},
        "autonomy": "full",
    }
    assert command.required_deliverables == ["output/result.md"]
    assert command.datasource_ids == []
    assert command.execution_lane == "stateless"
    assert command.priority == 3
    assert command.context == {
        "bench": {
            "run_id": RUN,
            "task": "delivery",
            "arm": "treatment",
            "replicate": 2,
        }
    }
    assert (run, task, arm) == originals


@pytest.mark.asyncio
async def test_invalid_bench_payload_still_refuses_before_admission_callback():
    run, task, arm = fixtures()
    task["priority"] = 100
    create = AsyncMock()
    with pytest.raises(ValidationError):
        await bench._create_job_through_admission(run, task, arm, 1, create_job=create)
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_bench_adapter_propagates_admission_failure(failure_type):
    failure = failure_type("fixture admission failure")
    create = AsyncMock(side_effect=failure)
    with pytest.raises(failure_type) as caught:
        await bench._create_job_through_admission(*fixtures(), 1, create_job=create)
    assert caught.value is failure


@pytest.mark.asyncio
async def test_lifespans_capture_each_app_once_and_drain_its_own_sweeper(monkeypatch):
    entered = [asyncio.Event(), asyncio.Event()]
    stopped = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    stores = [object(), object()]
    create = [AsyncMock(return_value={"app": 0}), AsyncMock(return_value={"app": 1})]
    factories = [
        Mock(
            return_value=bench.BenchDependencies(store=stores[i], create_job=create[i])
        )
        for i in range(2)
    ]
    apps = [
        SimpleNamespace(state=SimpleNamespace(bench_dependencies_factory=f))
        for f in factories
    ]
    callbacks = {}

    async def sweep(store, shutdown_event, *, create_job_fn):
        i = stores.index(store)
        callbacks[i] = create_job_fn
        entered[i].set()
        await shutdown_event.wait()
        stopped[i].set()
        await release[i].wait()

    monkeypatch.setattr(bench, "bench_sweeper_loop", sweep)
    managers = [bench._bench_lifespan(app) for app in apps]
    for i, manager in enumerate(managers):
        await manager.__aenter__()
        await asyncio.wait_for(entered[i].wait(), timeout=5)
        apps[i].state.bench_dependencies_factory = Mock(
            side_effect=AssertionError("Factory must be captured once")
        )
    for i in range(2):
        assert await callbacks[i](*fixtures(), i) == {"app": i}
        factories[i].assert_called_once_with()
        create[i].assert_awaited_once()
        assert create[i].await_args.args[1].context["bench"]["replicate"] == i
    for i, manager in enumerate(managers):
        exit_task = asyncio.create_task(manager.__aexit__(None, None, None))
        await asyncio.wait_for(stopped[i].wait(), timeout=5)
        assert not exit_task.done()
        if i == 0:
            assert not stopped[1].is_set()
        release[i].set()
        await asyncio.wait_for(exit_task, timeout=5)


@pytest.mark.asyncio
async def test_lifespan_body_failure_still_signals_and_drains(monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def sweep(_store, shutdown_event, *, create_job_fn):
        started.set()
        await shutdown_event.wait()
        stopped.set()

    monkeypatch.setattr(bench, "bench_sweeper_loop", sweep)
    factory = Mock(
        return_value=bench.BenchDependencies(store=object(), create_job=AsyncMock())
    )
    app = SimpleNamespace(state=SimpleNamespace(bench_dependencies_factory=factory))
    failure = RuntimeError("fixture lifespan body failure")
    with pytest.raises(RuntimeError) as caught:
        async with bench._bench_lifespan(app):
            await asyncio.wait_for(started.wait(), timeout=5)
            raise failure
    assert caught.value is failure
    assert stopped.is_set()
    factory.assert_called_once_with()


@pytest.mark.asyncio
async def test_lifespan_sweeper_failure_propagates_on_shutdown(monkeypatch):
    failed = asyncio.Event()
    failure = RuntimeError("fixture sweeper failure")

    async def sweep(_store, _shutdown_event, *, create_job_fn):
        failed.set()
        raise failure

    monkeypatch.setattr(bench, "bench_sweeper_loop", sweep)
    app = SimpleNamespace(
        state=SimpleNamespace(
            bench_dependencies_factory=lambda: bench.BenchDependencies(
                store=object(), create_job=AsyncMock()
            )
        )
    )
    with pytest.raises(RuntimeError) as caught:
        async with bench._bench_lifespan(app):
            await asyncio.wait_for(failed.wait(), timeout=5)
    assert caught.value is failure
