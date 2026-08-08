"""Unit tests for IDE session restore routing and container clone behavior."""

import logging
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
async def test_restore_session_uses_snapshot_container_for_pod_snapshot(
    service_factory,
):
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
async def test_restore_session_falls_back_to_gitea_when_pod_snapshot_restore_fails(
    service_factory,
):
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
async def test_restore_k8s_ide_container_fails_clone_marks_session_failed(
    service_factory,
):
    """Gitea clone failures on the IDE pod should fail the IDE session."""
    svc = service_factory
    svc._container_provisioner.create_ide_pod = AsyncMock(return_value="10.0.0.10")

    proc = AsyncMock()
    proc.returncode = 2
    proc.communicate = AsyncMock(return_value=(b"", b"permission denied"))

    with (
        patch(
            "orchestrator.services.ide_session.build_agent_ssh_cmd",
            return_value=["ssh", "-i", "k", "agent-host@10.0.0.10", "clone"],
        ) as mock_build_ssh,
        patch(
            "orchestrator.services.ide_session.resolve_ssh_key_path", return_value="k"
        ),
    ):
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
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


@pytest.mark.asyncio
async def test_extract_snapshot_uses_home_scoped_command(service_factory):
    """IDE pod extraction must scope to home/agent-host, not the full tarball."""
    from orchestrator.services.ide_session import EXTRACT_HOME_REMOTE_CMD

    svc = service_factory
    svc._snapshot_service.is_available = True
    svc._snapshot_service.download_snapshot = AsyncMock(return_value=True)

    with (
        patch(
            "orchestrator.services.ide_session.stream_extract_snapshot",
            AsyncMock(return_value=(0, b"")),
        ) as mock_extract,
        patch(
            "orchestrator.services.ide_session.resolve_ssh_key_path", return_value="k"
        ),
    ):
        ok = await svc._extract_snapshot_to_k8s_pod("job-0008", "10.0.0.10", 30022)

    assert ok is True
    assert mock_extract.await_args.kwargs["remote_cmd"] == EXTRACT_HOME_REMOTE_CMD


@pytest.mark.asyncio
async def test_extract_rc_nonzero_with_populated_workspace_continues(service_factory):
    """tar noise (rc=2) must not fail the restore when the workspace landed."""
    svc = service_factory
    svc._snapshot_service.is_available = True
    svc._snapshot_service.download_snapshot = AsyncMock(return_value=True)
    svc._workspace_populated = AsyncMock(return_value=True)

    with (
        patch(
            "orchestrator.services.ide_session.stream_extract_snapshot",
            AsyncMock(return_value=(2, b"tar: usr/local/bin/x: Cannot open")),
        ),
        patch(
            "orchestrator.services.ide_session.resolve_ssh_key_path", return_value="k"
        ),
    ):
        ok = await svc._extract_snapshot_to_k8s_pod("job-0009", "10.0.0.10", 30022)

    assert ok is True
    for call in svc._db.merge_ide_session_context.await_args_list:
        assert call.args[1].get("status") != "failed"


@pytest.mark.asyncio
async def test_extract_rc_nonzero_with_empty_workspace_fails(service_factory):
    """A genuinely failed extraction (empty workspace) still fails the session."""
    svc = service_factory
    svc._snapshot_service.is_available = True
    svc._snapshot_service.download_snapshot = AsyncMock(return_value=True)
    svc._workspace_populated = AsyncMock(return_value=False)

    with (
        patch(
            "orchestrator.services.ide_session.stream_extract_snapshot",
            AsyncMock(return_value=(2, b"zstd: corrupt input")),
        ),
        patch(
            "orchestrator.services.ide_session.resolve_ssh_key_path", return_value="k"
        ),
    ):
        ok = await svc._extract_snapshot_to_k8s_pod("job-0010", "10.0.0.10", 30022)

    assert ok is False
    last_ctx = svc._db.merge_ide_session_context.await_args_list[-1]
    assert last_ctx.args[1]["status"] == "failed"


@pytest.mark.asyncio
async def test_gitea_clone_chain_is_idempotent(service_factory):
    """Retries against a pre-populated workspace must not die on remote add."""
    svc = service_factory
    svc._container_provisioner.create_ide_pod = AsyncMock(return_value="10.0.0.10")

    proc = AsyncMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))

    with (
        patch(
            "orchestrator.services.ide_session.build_agent_ssh_cmd",
            return_value=["ssh", "cmd"],
        ) as mock_build_ssh,
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
        patch(
            "orchestrator.services.ide_session.IdeSessionService._wait_for_code_server",
            AsyncMock(return_value=True),
        ),
    ):
        ok = await svc._restore_k8s_ide_container(
            "job-0011",
            {"branch_name": "main", "repo_name": "demo/repo"},
            "demo/repo",
            "main",
        )

    assert ok is True
    clone_cmd = mock_build_ssh.call_args_list[0].args[2]
    assert "git remote set-url origin" in clone_cmd


@pytest.mark.asyncio
async def test_snapshot_restore_runs_git_repair_fetch(service_factory):
    """Successful extraction is followed by the best-effort git object refetch."""
    svc = service_factory
    svc._container_provisioner.create_ide_pod = AsyncMock(return_value="10.0.0.10")
    svc._extract_snapshot_to_k8s_pod = AsyncMock(return_value=True)
    svc._repair_git_after_snapshot = AsyncMock()
    svc._seed_ide_profile_for_user = AsyncMock()
    svc._wait_for_code_server = AsyncMock(return_value=True)

    ok = await svc._restore_snapshot_container(
        "job-0012", {"repo_name": "demo/repo", "branch_name": "job/abc"}
    )

    assert ok is True
    svc._repair_git_after_snapshot.assert_awaited_once_with(
        "job-0012", ANY, "10.0.0.10", 30022
    )


# =============================================================================
# Test: _extract_snapshot_to_vm honest return (C1a)
# =============================================================================
#
# _extract_snapshot_to_vm used to return None on every path (no-service,
# download failure, rc != 0, AND a clean extract), which made a failed
# restore indistinguishable from a successful one. Mirrors the fix already
# applied to workspace_suspension.py's _extract_snapshot.


@pytest.mark.asyncio
async def test_extract_snapshot_to_vm_returns_false_on_download_failure(
    service_factory,
):
    """A failed S3 download must not be reported as a successful extract."""
    svc = service_factory
    svc._snapshot_service.download_snapshot = AsyncMock(return_value=False)

    ok = await svc._extract_snapshot_to_vm("job-0013", "10.0.0.10", 30022)

    assert ok is False


@pytest.mark.asyncio
async def test_extract_snapshot_to_vm_returns_false_on_extract_rc_nonzero(
    service_factory,
):
    """A tar/ssh failure (rc != 0) must not be reported as a successful extract."""
    svc = service_factory
    svc._snapshot_service.download_snapshot = AsyncMock(return_value=True)

    with (
        patch(
            "orchestrator.services.ide_session.stream_extract_snapshot",
            AsyncMock(return_value=(2, b"boom")),
        ),
        patch(
            "orchestrator.services.ide_session.resolve_ssh_key_path", return_value="k"
        ),
    ):
        ok = await svc._extract_snapshot_to_vm("job-0014", "10.0.0.10", 30022)

    assert ok is False


@pytest.mark.asyncio
async def test_extract_snapshot_to_vm_returns_true_on_clean_extract(
    service_factory,
):
    """A clean download + unpack is the only path that reports success."""
    svc = service_factory
    svc._snapshot_service.download_snapshot = AsyncMock(return_value=True)

    with (
        patch(
            "orchestrator.services.ide_session.stream_extract_snapshot",
            AsyncMock(return_value=(0, b"")),
        ),
        patch(
            "orchestrator.services.ide_session.resolve_ssh_key_path", return_value="k"
        ),
    ):
        ok = await svc._extract_snapshot_to_vm("job-0015", "10.0.0.10", 30022)

    assert ok is True


# =============================================================================
# Test: restore_snapshot_for_resume gates on the extract result (C1a)
# =============================================================================
#
# restore_snapshot_for_resume used to ignore _extract_snapshot_to_vm's return
# value entirely, unconditionally logging "Snapshot restored" and returning
# True — so a resume whose extract failed still reported success and the
# agent resumed on an empty/half-populated tree.


@pytest.mark.asyncio
async def test_restore_snapshot_for_resume_returns_false_when_extract_fails(
    service_factory, caplog
):
    """A failed extract must fail the resume, not silently report success."""
    svc = service_factory
    svc._snapshot_service.is_available = True
    svc._db.get_job = AsyncMock(
        return_value={
            "id": "job-0016",
            "context": {"snapshot": {"status": "available"}},
        }
    )
    svc._extract_snapshot_to_vm = AsyncMock(return_value=False)

    with caplog.at_level(logging.INFO, logger="orchestrator.services.ide_session"):
        result = await svc.restore_snapshot_for_resume(
            "job-0016", "10.0.0.10", 30022
        )

    assert result is False
    assert "Snapshot restored for job resume" not in caplog.text


@pytest.mark.asyncio
async def test_restore_snapshot_for_resume_returns_true_when_extract_succeeds(
    service_factory,
):
    """Happy path: a clean extract still reports the resume as restored."""
    svc = service_factory
    svc._snapshot_service.is_available = True
    svc._db.get_job = AsyncMock(
        return_value={
            "id": "job-0017",
            "context": {"snapshot": {"status": "available"}},
        }
    )
    svc._extract_snapshot_to_vm = AsyncMock(return_value=True)

    result = await svc.restore_snapshot_for_resume("job-0017", "10.0.0.10", 30022)

    assert result is True
