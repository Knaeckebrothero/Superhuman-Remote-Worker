"""Direct seam tests for B09's thread lifecycle owners."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from orchestrator.routers import thread_lifecycle as routes
from orchestrator.schemas.thread_lifecycle import ThreadRewindRequest
from orchestrator.services import thread_resume, thread_retirement


def _route_app(label: str) -> tuple[FastAPI, SimpleNamespace, SimpleNamespace]:
    app = FastAPI()
    app.include_router(routes.router)
    store = SimpleNamespace(label=label)
    retirement = SimpleNamespace(end_thread_flow=AsyncMock(return_value={"end": label}))
    resume = SimpleNamespace(
        resume_thread=AsyncMock(return_value={"resume": label}),
        rewind_thread_detached=AsyncMock(return_value={"rewind": label}),
    )

    async def require_owner(request, candidate_store, thread_id):
        assert request.app is app
        assert candidate_store is store
        return {"id": f"user-{label}"}, {"id": thread_id, "owner": label}

    app.state.thread_lifecycle_dependencies_factory = lambda: (
        routes.ThreadLifecycleRouteDependencies(
            store=store,
            retirement=retirement,
            resume=resume,
            require_thread_owner=require_owner,
        )
    )
    return app, retirement, resume


def test_thread_lifecycle_routes_are_application_isolated() -> None:
    app_a, retirement_a, resume_a = _route_app("a")
    app_b, retirement_b, resume_b = _route_app("b")

    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        assert client_a.delete(
            "/api/persistent/threads/t1", params={"permanent": True, "force": True}
        ).json() == {"end": "a"}
        assert client_b.post("/api/persistent/threads/t2/resume").json() == {
            "resume": "b"
        }
        assert client_a.post(
            "/api/agents/threads/t3/rewind",
            json={"message_id": "m1", "mode": "conversation"},
        ).json() == {"rewind": "a"}

    retirement_a.end_thread_flow.assert_awaited_once_with(
        "t1", {"id": "t1", "owner": "a"}, permanent=True, force=True
    )
    retirement_b.end_thread_flow.assert_not_awaited()
    resume_b.resume_thread.assert_awaited_once_with(
        "t2", {"id": "user-b"}, {"id": "t2", "owner": "b"}, None
    )
    resume_a.rewind_thread_detached.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_cloud_registry_does_not_let_stale_callback_evict_successor() -> (
    None
):
    tasks: dict[str, asyncio.Task[None]] = {}
    dependencies = SimpleNamespace(late_cloud_setup_tasks=tasks)
    first_gate = asyncio.Event()
    second_gate = asyncio.Event()

    async def wait_on(gate: asyncio.Event) -> None:
        await gate.wait()

    first = asyncio.create_task(wait_on(first_gate))
    second = asyncio.create_task(wait_on(second_gate))
    thread_resume.register_late_cloud_setup("t1", first, dependencies=dependencies)
    thread_resume.register_late_cloud_setup("t1", second, dependencies=dependencies)

    first_gate.set()
    await first
    await asyncio.sleep(0)
    assert tasks["t1"] is second

    second_gate.set()
    await second
    await asyncio.sleep(0)
    assert "t1" not in tasks


@pytest.mark.asyncio
async def test_background_push_workspace_preserves_exact_sandbox_identity() -> None:
    payload = {
        "workspace_generation": "g1",
        "workspace_provisioner": "k8s",
        "status": "ready",
        "pod_ip": "10.0.0.2",
        "pod_port": 2222,
        "ssh_key_path": "/keys/id",
        "workspace_runtime_incarnation": "pod-uid",
        "workspace_ssh_host_key_fingerprint": "SHA256:key",
        "cloud_sync": {"driver": "sync"},
    }
    dependencies = SimpleNamespace(
        require_stateless_workspace=Mock(return_value="sandbox"),
        agent_get_thread_workspace_locked=AsyncMock(return_value=payload),
        inject_lite_workspace_config=Mock(),
    )

    result = await thread_resume.resolve_background_push_workspace(
        {"id": "t1"}, dependencies=dependencies
    )

    assert result == {
        "workspace": {
            "backend": "sandbox",
            "host": "10.0.0.2",
            "port": 2222,
            "key_path": "/keys/id",
            "workspace_generation": "g1",
            "runtime_incarnation": "pod-uid",
            "host_key_fingerprint": "SHA256:key",
        },
        "cloud_sync": {"driver": "sync"},
    }


@pytest.mark.asyncio
async def test_detached_rewind_refuses_stateless_before_database_mutation() -> None:
    store = SimpleNamespace(
        get_live_thread_message=AsyncMock(),
        apply_thread_rewind=AsyncMock(),
    )
    with pytest.raises(HTTPException) as raised:
        await thread_resume.rewind_thread_detached(
            "t1",
            {"id": "u1"},
            {"id": "t1", "execution_lane": "stateless"},
            ThreadRewindRequest(message_id="m1"),
            dependencies=SimpleNamespace(store=store),
        )

    assert getattr(raised.value, "status_code", None) == 409
    store.get_live_thread_message.assert_not_awaited()
    store.apply_thread_rewind.assert_not_awaited()


@pytest.mark.asyncio
async def test_stateless_retirement_refuses_unattested_physical_runtime() -> None:
    """A historical row without an immutable runtime UID cannot enter cleanup."""

    store = SimpleNamespace(
        get_thread=AsyncMock(
            return_value={
                "id": "t1",
                "status": "ended",
                "execution_lane": "stateless",
                "metadata": {
                    "workspace_container": {
                        "status": "ready",
                        "provisioner": "k8s",
                    },
                    "_workspace_binding": {"backing_id": "k8s-pod:workspace-t1"},
                },
            }
        )
    )

    with pytest.raises(HTTPException) as raised:
        await thread_retirement.reconcile_stateless_thread_retirement(
            "t1",
            force=True,
            permanent=False,
            dependencies=SimpleNamespace(
                store=store,
                agent_provisioner=SimpleNamespace(),
                container_provisioner=SimpleNamespace(),
                workspace_suspension_service=SimpleNamespace(),
                build_agent_cloud_mount=AsyncMock(),
                logger=Mock(),
            ),
        )

    assert raised.value.status_code == 503
    assert "exact UID" in str(raised.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("reclaim_volume", [False, True])
async def test_legacy_release_only_reclaims_persistent_volume_when_permanent(
    reclaim_volume: bool,
) -> None:
    store = SimpleNamespace(
        get_thread=AsyncMock(return_value={"id": "t1", "metadata": {}}),
    )
    agent = SimpleNamespace(
        is_available=True,
        delete_agent_pod_by_thread=AsyncMock(),
    )
    persistent = SimpleNamespace(
        is_available=True,
        delete_agent_pod=AsyncMock(),
        delete_agent_pvc=AsyncMock(),
    )
    dependencies = SimpleNamespace(
        store=store,
        agent_provisioner=agent,
        persistent_provisioner=persistent,
        container_provisioner=SimpleNamespace(is_available=False),
        vm_provisioner=SimpleNamespace(lifecycle_available=False),
        docker_provisioner=SimpleNamespace(),
        get_container_context=Mock(return_value={}),
        get_vm_context=Mock(return_value={}),
        vm_needs_release=Mock(return_value=False),
        thread_uses_pinned_execution=Mock(return_value=False),
        logger=Mock(),
    )

    await thread_retirement.release_thread_resources(
        "t1", reclaim_volume=reclaim_volume, dependencies=dependencies
    )

    agent.delete_agent_pod_by_thread.assert_awaited_once_with("t1")
    persistent.delete_agent_pod.assert_awaited_once_with("t1")
    if reclaim_volume:
        persistent.delete_agent_pvc.assert_awaited_once_with("t1")
    else:
        persistent.delete_agent_pvc.assert_not_awaited()
