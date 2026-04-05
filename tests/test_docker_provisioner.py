"""Tests for the Docker Compose provisioner service."""

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestDockerProvisionerInit:
    """Tests for DockerProvisioner initialization."""

    def test_not_available_without_hosts(self):
        """Provisioner reports unavailable when WORKSPACE_HOSTS is not set."""
        from orchestrator.services.docker_provisioner import DockerProvisioner

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(DockerProvisioner, "_REPO_ROOT", Path("/nonexistent")),
        ):
            os.environ.pop("WORKSPACE_HOSTS", None)
            os.environ.pop("VM_HOSTS", None)

            provisioner = DockerProvisioner()
            db = MagicMock()
            provisioner.connect(db=db)
            assert provisioner.is_available is False
            assert provisioner.workspace_hosts == []
            assert provisioner.vm_hosts == []

    def test_available_with_hosts(self):
        """Provisioner reports available when WORKSPACE_HOSTS is set."""
        with patch.dict(
            os.environ,
            {"WORKSPACE_HOSTS": "workspace-1,workspace-2", "VM_HOSTS": ""},
        ):
            from orchestrator.services.docker_provisioner import DockerProvisioner

            provisioner = DockerProvisioner()
            db = MagicMock()
            provisioner.connect(db=db)
            assert provisioner.is_available is True
            assert provisioner.workspace_hosts == [
                "workspace-1:22",
                "workspace-2:22",
            ]
            assert provisioner.vm_hosts == []

    def test_vm_hosts_parsed(self):
        """VM hosts are parsed from VM_HOSTS env var."""
        with patch.dict(
            os.environ,
            {
                "WORKSPACE_HOSTS": "workspace-1",
                "VM_HOSTS": "agent-vm-1,agent-vm-2",
            },
        ):
            from orchestrator.services.docker_provisioner import DockerProvisioner

            provisioner = DockerProvisioner()
            provisioner.connect(db=MagicMock())
            assert provisioner.vm_hosts == ["agent-vm-1:22", "agent-vm-2:22"]

    def test_whitespace_in_hosts_stripped(self):
        """Leading/trailing whitespace in hostnames is stripped."""
        with patch.dict(
            os.environ,
            {"WORKSPACE_HOSTS": " workspace-1 , workspace-2 ,, "},
        ):
            from orchestrator.services.docker_provisioner import DockerProvisioner

            provisioner = DockerProvisioner()
            provisioner.connect(db=MagicMock())
            assert provisioner.workspace_hosts == [
                "workspace-1:22",
                "workspace-2:22",
            ]

    def test_host_port_format_parsed(self):
        """Host:port entries are parsed correctly."""
        with patch.dict(
            os.environ,
            {"WORKSPACE_HOSTS": "localhost:2201,localhost:2202"},
        ):
            from orchestrator.services.docker_provisioner import DockerProvisioner

            provisioner = DockerProvisioner()
            provisioner.connect(db=MagicMock())
            assert provisioner.workspace_hosts == [
                "localhost:2201",
                "localhost:2202",
            ]

    def test_auto_detect_dev_compose(self, tmp_path):
        """Auto-detects dev compose when .dev/ssh-keys/id_ed25519 exists."""
        from orchestrator.services.docker_provisioner import DockerProvisioner

        key_dir = tmp_path / ".dev" / "ssh-keys"
        key_dir.mkdir(parents=True)
        (key_dir / "id_ed25519").touch()

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(DockerProvisioner, "_REPO_ROOT", tmp_path),
        ):
            os.environ.pop("WORKSPACE_HOSTS", None)
            os.environ.pop("SSH_KEY_PATH", None)
            os.environ.pop("VM_HOSTS", None)

            provisioner = DockerProvisioner()
            provisioner.connect(db=MagicMock())
            assert provisioner.is_available is True
            assert len(provisioner.workspace_hosts) == 3
            assert os.environ.get("SSH_KEY_PATH") == str(
                tmp_path / ".dev" / "ssh-keys" / "id_ed25519"
            )


class TestAssignWorkspace:
    """Tests for workspace assignment."""

    @pytest.fixture
    def provisioner(self):
        """Create a provisioner with 3 workspace hosts."""
        with patch.dict(
            os.environ,
            {"WORKSPACE_HOSTS": "workspace-1,workspace-2,workspace-3"},
        ):
            from orchestrator.services.docker_provisioner import DockerProvisioner

            p = DockerProvisioner()
            db = AsyncMock()
            db.fetch = AsyncMock(return_value=[])
            db.merge_workspace_container_context = AsyncMock(return_value=True)
            p.connect(db=db)
            return p

    @pytest.mark.asyncio
    async def test_assign_first_available(self, provisioner):
        """Assigns first workspace when all are free."""
        result = await provisioner.assign_workspace("job-1")
        assert result is not None
        assert result["host"] == "workspace-1"
        assert result["port"] == 22
        assert result["status"] == "ready"
        assert result["provisioner"] == "docker"

    @pytest.mark.asyncio
    async def test_assign_skips_occupied(self, provisioner):
        """Skips workspaces already assigned to active jobs."""
        provisioner._db.fetch = AsyncMock(
            return_value=[{"host": "workspace-1", "port": 22}]
        )
        result = await provisioner.assign_workspace("job-2")
        assert result is not None
        assert result["host"] == "workspace-2"

    @pytest.mark.asyncio
    async def test_assign_returns_none_when_full(self, provisioner):
        """Returns None when all workspaces are occupied."""
        provisioner._db.fetch = AsyncMock(
            return_value=[
                {"host": "workspace-1", "port": 22},
                {"host": "workspace-2", "port": 22},
                {"host": "workspace-3", "port": 22},
            ]
        )
        result = await provisioner.assign_workspace("job-4")
        assert result is None

    @pytest.mark.asyncio
    async def test_assign_writes_context_to_db(self, provisioner):
        """Assignment writes workspace context to the job in DB."""
        await provisioner.assign_workspace("job-1")
        provisioner._db.merge_workspace_container_context.assert_called_once_with(
            "job-1",
            {
                "status": "ready",
                "host": "workspace-1",
                "port": 22,
                "provisioner": "docker",
            },
        )

    @pytest.mark.asyncio
    async def test_assign_without_db_returns_none(self):
        """Returns None if DB is not connected."""
        from orchestrator.services.docker_provisioner import DockerProvisioner

        with patch.dict(os.environ, {"WORKSPACE_HOSTS": "workspace-1"}):
            p = DockerProvisioner()
            # Don't call connect — _db is None
            result = await p.assign_workspace("job-1")
            assert result is None


class TestAssignWorkspaceWithPorts:
    """Tests for workspace assignment with host:port format (dev compose)."""

    @pytest.fixture
    def provisioner(self):
        """Create a provisioner with localhost:port workspace hosts."""
        with patch.dict(
            os.environ,
            {"WORKSPACE_HOSTS": "localhost:2201,localhost:2202"},
        ):
            from orchestrator.services.docker_provisioner import DockerProvisioner

            p = DockerProvisioner()
            db = AsyncMock()
            db.fetch = AsyncMock(return_value=[])
            db.merge_workspace_container_context = AsyncMock(return_value=True)
            p.connect(db=db)
            return p

    @pytest.mark.asyncio
    async def test_assign_uses_correct_port(self, provisioner):
        """Assignment includes the parsed port, not default 22."""
        result = await provisioner.assign_workspace("job-1")
        assert result is not None
        assert result["host"] == "localhost"
        assert result["port"] == 2201

    @pytest.mark.asyncio
    async def test_assign_skips_occupied_by_port(self, provisioner):
        """Same host with different port is correctly distinguished."""
        provisioner._db.fetch = AsyncMock(
            return_value=[{"host": "localhost", "port": 2201}]
        )
        result = await provisioner.assign_workspace("job-2")
        assert result is not None
        assert result["host"] == "localhost"
        assert result["port"] == 2202

    @pytest.mark.asyncio
    async def test_assign_all_occupied(self, provisioner):
        """All host:port pairs occupied returns None."""
        provisioner._db.fetch = AsyncMock(
            return_value=[
                {"host": "localhost", "port": 2201},
                {"host": "localhost", "port": 2202},
            ]
        )
        result = await provisioner.assign_workspace("job-3")
        assert result is None


class TestReleaseWorkspace:
    """Tests for workspace release."""

    @pytest.fixture
    def provisioner(self):
        with patch.dict(
            os.environ,
            {"WORKSPACE_HOSTS": "workspace-1,workspace-2"},
        ):
            from orchestrator.services.docker_provisioner import DockerProvisioner

            p = DockerProvisioner()
            db = AsyncMock()
            db.get_job = AsyncMock(
                return_value={
                    "context": json.dumps(
                        {
                            "workspace_container": {
                                "status": "ready",
                                "host": "workspace-1",
                                "port": 22,
                                "provisioner": "docker",
                            }
                        }
                    )
                }
            )
            db.merge_workspace_container_context = AsyncMock(return_value=True)
            p.connect(db=db, snapshot_service=None)
            return p

    @pytest.mark.asyncio
    async def test_release_marks_released(self, provisioner):
        """Release updates DB status to 'released'."""
        ok = await provisioner.release_workspace("job-1")
        assert ok is True
        provisioner._db.merge_workspace_container_context.assert_called_once_with(
            "job-1",
            {"status": "released", "host": "workspace-1"},
        )

    @pytest.mark.asyncio
    async def test_release_with_snapshot(self, provisioner):
        """Release captures S3 snapshot if snapshot service available."""
        snap = AsyncMock()
        snap.is_available = True
        snap.capture_vm_snapshot = AsyncMock(return_value=True)
        provisioner._snapshot_service = snap

        ok = await provisioner.release_workspace("job-1")
        assert ok is True
        snap.capture_vm_snapshot.assert_called_once_with(
            job_id="job-1",
            ssh_host="workspace-1",
            ssh_port=22,
            source_type="container",
        )

    @pytest.mark.asyncio
    async def test_release_non_docker_returns_false(self):
        """Release returns False for non-docker provisioned workspaces."""
        from orchestrator.services.docker_provisioner import DockerProvisioner

        with patch.dict(os.environ, {"WORKSPACE_HOSTS": "workspace-1"}):
            p = DockerProvisioner()
            db = AsyncMock()
            db.get_job = AsyncMock(
                return_value={
                    "context": json.dumps(
                        {
                            "workspace_container": {
                                "status": "ready",
                                "pod_ip": "10.42.0.1",
                                # No "provisioner": "docker"
                            }
                        }
                    )
                }
            )
            p.connect(db=db)
            ok = await p.release_workspace("job-1")
            assert ok is False


class TestVMAssignment:
    """Tests for QEMU-in-Docker VM assignment."""

    @pytest.mark.asyncio
    async def test_assign_vm_returns_none_without_hosts(self):
        """Returns None when VM_HOSTS is empty."""
        from orchestrator.services.docker_provisioner import DockerProvisioner

        with patch.dict(os.environ, {"WORKSPACE_HOSTS": "w-1", "VM_HOSTS": ""}):
            p = DockerProvisioner()
            db = AsyncMock()
            p.connect(db=db)
            result = await p.assign_vm("job-1")
            assert result is None

    @pytest.mark.asyncio
    async def test_assign_vm_success(self):
        """Assigns first free VM."""
        from orchestrator.services.docker_provisioner import DockerProvisioner

        with patch.dict(
            os.environ,
            {"WORKSPACE_HOSTS": "w-1", "VM_HOSTS": "agent-vm-1"},
        ):
            p = DockerProvisioner()
            db = AsyncMock()
            db.fetch = AsyncMock(return_value=[])
            db.merge_vm_context = AsyncMock(return_value=True)
            p.connect(db=db)
            result = await p.assign_vm("job-1")
            assert result is not None
            assert result["ssh_host"] == "agent-vm-1"
            assert result["status"] == "ready"
            assert result["provisioner"] == "docker"


class TestModuleSingleton:
    """Test module-level singleton."""

    def test_singleton_exists(self):
        from orchestrator.services.docker_provisioner import docker_provisioner

        assert docker_provisioner is not None
        assert docker_provisioner.is_available is False  # Not connected yet
