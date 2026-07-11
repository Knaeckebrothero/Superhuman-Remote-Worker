"""Unit tests for IDE session restore routing and container clone behavior."""

from unittest.mock import ANY, AsyncMock, patch

import pytest


@pytest.fixture
def service_factory():
    from orchestrator.services.ide_session import IdeSessionService

    db = AsyncMock()
    db.merge_ide_session_context = AsyncMock()

    container_provisioner = AsyncMock()
    container_provisioner.is_available = True

    vm_provisioner = AsyncMock()
    vm_provisioner.is_available = True

    svc = IdeSessionService()
    svc.connect(
        db=db,
        snapshot_service=AsyncMock(),
        vm_provisioner=vm_provisioner,
        gitea_client=None,
        container_provisioner=container_provisioner,
    )
    return svc


@pytest.mark.asyncio
async def test_restore_session_uses_snapshot_container_for_pod_snapshot(service_factory):
    """Pod snapshots should go through the IDE pod restore path."""
    svc = service_factory
    svc._restore_snapshot_container = AsyncMock(return_value=True)
    svc._restore_gitea_container = AsyncMock(return_value=True)
    svc._restore_vm_session = AsyncMock()

    svc._container_provisioner.is_available = True
    job = {
        "id": "job-0001",
        "context": {"snapshot": {"status": "available", "source_type": "pod"}},
    }

    await svc._restore_session("job-0001", job, "snapshot", 8, "16Gi")

    svc._restore_snapshot_container.assert_awaited_once_with("job-0001", job)
    svc._restore_vm_session.assert_not_awaited()
    svc._restore_gitea_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_session_falls_back_to_gitea_when_pod_snapshot_restore_fails(service_factory):
    """If pod snapshot restore fails, fallback to Gitea when possible."""
    svc = service_factory
    svc._restore_snapshot_container = AsyncMock(return_value=False)
    svc._restore_gitea_container = AsyncMock(return_value=True)
    svc._restore_vm_session = AsyncMock()

    svc._container_provisioner.is_available = True
    job = {
        "id": "job-0002",
        "repo_name": "demo/repo",
        "context": {"snapshot": {"status": "available", "source_type": "pod"}},
    }

    await svc._restore_session("job-0002", job, "snapshot", 8, "16Gi")

    svc._restore_snapshot_container.assert_awaited_once_with("job-0002", job)
    svc._restore_gitea_container.assert_awaited_once_with("job-0002", job)
    svc._restore_vm_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_session_routes_vm_snapshot_to_vm(service_factory):
    """VM snapshots stay on VM restore path."""
    svc = service_factory
    svc._restore_snapshot_container = AsyncMock(return_value=True)
    svc._restore_gitea_container = AsyncMock(return_value=True)
    svc._restore_vm_session = AsyncMock()

    job = {
        "id": "job-0003",
        "context": {"snapshot": {"status": "available", "source_type": "vm"}},
    }

    await svc._restore_session("job-0003", job, "snapshot", 8, "16Gi")

    svc._restore_vm_session.assert_awaited_once_with(
        "job-0003",
        job,
        "snapshot",
        8,
        "16Gi",
    )
    svc._restore_snapshot_container.assert_not_awaited()
    svc._restore_gitea_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_k8s_ide_container_fails_clone_marks_session_failed(service_factory):
    """Gitea clone failures on the IDE pod should fail the IDE session."""
    svc = service_factory
    svc._container_provisioner.create_ide_pod = AsyncMock(return_value="10.0.0.10")

    proc = AsyncMock()
    proc.returncode = 2
    proc.communicate = AsyncMock(return_value=(b"", b"permission denied"))

    with patch(
        "orchestrator.services.ide_session.build_agent_ssh_cmd",
        return_value=["ssh", "-i", "k", "agent-host@10.0.0.10", "clone"],
    ) as mock_build_ssh, patch(
        "orchestrator.services.ide_session.resolve_ssh_key_path", return_value="k"
    ):
        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=proc)
        ):
            with patch(
                "orchestrator.services.ide_session.IdeSessionService._wait_for_code_server",
                AsyncMock(return_value=True),
            ):
                ok = await svc._restore_k8s_ide_container(
                    "job-0004",
                    {"branch_name": "main", "repo_name": "demo/repo"},
                    "demo/repo",
                    "main",
                )

    assert ok is False
    mock_build_ssh.assert_called_once_with("10.0.0.10", 30022, ANY)
    last_ctx = svc._db.merge_ide_session_context.await_args_list[-1]
    assert last_ctx.kwargs == {}
    assert last_ctx.args[1]["status"] == "failed"


@pytest.mark.asyncio
async def test_restore_session_uses_long_vm_wait_estimate(service_factory):
    """VM source snapshots report the longer, realistic restore estimate."""
    svc = service_factory
    svc._db.get_job = AsyncMock(
        return_value={
            "id": "job-0005",
            "context": {"snapshot": {"status": "available", "source_type": "vm"}},
        }
    )
    svc._restore_session = AsyncMock(return_value=True)

    result = await svc.start_session("job-0005")

    assert result["status"] == "restoring"
    assert result["estimated_seconds"] == 420


@pytest.mark.asyncio
async def test_restore_vm_session_marks_unavailable_when_orchestrator_cannot_reach(
    service_factory,
):
    """Topology failures in VM restore should surface as unavailable, not timeout."""
    svc = service_factory
    svc._vm_provisioner.create_vm = AsyncMock(return_value=True)
    svc._wait_for_vm_ready = AsyncMock(return_value=(None, None))
    svc._get_job = AsyncMock(
        return_value={
            "id": "job-0006",
            "context": {"vm": {"ssh_host": "100.64.23.44"}},
        }
    )

    with patch(
        "orchestrator.services.ide_session.orchestrator_can_reach",
        return_value=False,
    ):
        await svc._restore_vm_session(
            "job-0006", {"id": "job-0006"}, "snapshot", 8, "16Gi"
        )

    last_ctx = svc._db.merge_ide_session_context.await_args_list[-1]
    assert last_ctx.kwargs == {}
    assert last_ctx.args[1]["status"] == "unavailable"
    assert last_ctx.args[1]["error"] == "VM restore not supported on this topology"


@pytest.mark.asyncio
async def test_wait_for_vm_ready_returns_none_on_unroutable_host(service_factory):
    """The wait loop should abandon VM restore as soon as host is unroutable."""
    svc = service_factory
    svc._get_job = AsyncMock(
        return_value={
            "id": "job-0007",
            "context": {"vm": {"ssh_host": "100.64.23.44", "status": "creating"}},
        }
    )

    with patch(
        "orchestrator.services.ide_session.orchestrator_can_reach",
        return_value=False,
    ):
        ssh_host, ssh_port = await svc._wait_for_vm_ready("job-0007", timeout=5)

    assert ssh_host is None
    assert ssh_port is None
