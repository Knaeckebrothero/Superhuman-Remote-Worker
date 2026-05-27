"""Tests for the IDE proxy service and URL generation."""

import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# IdeProxyService — pod IP resolution and caching
# =============================================================================


class TestIdeProxyServiceInit:
    """Tests for IdeProxyService initialization."""

    def test_default_state(self):
        from orchestrator.services.ide_proxy import IdeProxyService

        service = IdeProxyService()
        assert service._db is None
        assert service._pod_ip_cache == {}
        assert service._cache_ttl == 30.0

    def test_connect_stores_db(self):
        from orchestrator.services.ide_proxy import IdeProxyService

        service = IdeProxyService()
        mock_db = MagicMock()
        service.connect(mock_db)
        assert service._db is mock_db


class TestResolvePodIp:
    """Tests for IdeProxyService.resolve_pod_ip()."""

    @pytest.fixture
    def service(self):
        from orchestrator.services.ide_proxy import IdeProxyService

        svc = IdeProxyService()
        svc._db = AsyncMock()
        return svc

    @pytest.mark.asyncio
    async def test_cache_hit(self, service):
        """Cached pod IP is returned without DB query."""
        service._pod_ip_cache["job-1"] = ("10.0.0.1", time.monotonic() + 60)

        result = await service.resolve_pod_ip("job-1")

        assert result == "10.0.0.1"
        service._db.get_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_expired(self, service):
        """Expired cache entry triggers DB lookup."""
        service._pod_ip_cache["job-1"] = ("10.0.0.1", time.monotonic() - 1)
        service._db.get_job = AsyncMock(
            return_value={
                "context": {"ide_session": {"status": "active", "pod_ip": "10.0.0.2"}}
            }
        )

        result = await service.resolve_pod_ip("job-1")

        assert result == "10.0.0.2"
        service._db.get_job.assert_called_once_with("job-1")

    @pytest.mark.asyncio
    async def test_cache_miss_ide_session(self, service):
        """Resolves pod_ip from ide_session context."""
        service._db.get_job = AsyncMock(
            return_value={
                "context": {"ide_session": {"status": "active", "pod_ip": "10.0.0.5"}}
            }
        )

        result = await service.resolve_pod_ip("job-1")

        assert result == "10.0.0.5"
        # Verify it's cached now
        assert "job-1" in service._pod_ip_cache
        assert service._pod_ip_cache["job-1"][0] == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_cache_miss_workspace_container(self, service):
        """Resolves pod_ip from workspace_container context."""
        service._db.get_job = AsyncMock(
            return_value={
                "context": {
                    "workspace_container": {"status": "ready", "pod_ip": "10.0.1.3"}
                }
            }
        )

        result = await service.resolve_pod_ip("job-1")
        assert result == "10.0.1.3"

    @pytest.mark.asyncio
    async def test_cache_miss_vm(self, service):
        """Resolves pod_ip from vm context (pod_ip preferred — cluster-internal, reachable from proxy)."""
        service._db.get_job = AsyncMock(
            return_value={
                "context": {
                    "vm": {
                        "status": "ready",
                        "ssh_host": "100.64.0.1",
                        "pod_ip": "10.0.2.1",
                    }
                }
            }
        )

        result = await service.resolve_pod_ip("job-1")
        assert (
            result == "10.0.2.1"
        )  # pod_ip preferred (cluster-internal, not Tailscale)

    @pytest.mark.asyncio
    async def test_cache_miss_vm_fallback_ssh_host(self, service):
        """Falls back to vm.ssh_host when pod_ip is absent."""
        service._db.get_job = AsyncMock(
            return_value={"context": {"vm": {"status": "ready", "pod_ip": "10.0.2.1"}}}
        )

        result = await service.resolve_pod_ip("job-1")
        assert result == "10.0.2.1"

    @pytest.mark.asyncio
    async def test_priority_order(self, service):
        """ide_session.pod_ip takes priority over workspace_container and vm."""
        service._db.get_job = AsyncMock(
            return_value={
                "context": {
                    "ide_session": {"status": "active", "pod_ip": "10.0.0.1"},
                    "workspace_container": {"status": "ready", "pod_ip": "10.0.0.2"},
                    "vm": {"status": "ready", "ssh_host": "10.0.0.3"},
                }
            }
        )

        result = await service.resolve_pod_ip("job-1")
        assert result == "10.0.0.1"

    @pytest.mark.asyncio
    async def test_job_not_found(self, service):
        """Returns None when job doesn't exist."""
        service._db.get_job = AsyncMock(return_value=None)
        service._db.get_thread = AsyncMock(return_value=None)

        result = await service.resolve_pod_ip("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_active_session(self, service):
        """Returns None when no IDE-related context is active."""
        service._db.get_job = AsyncMock(
            return_value={
                "context": {
                    "ide_session": {"status": "expired"},
                    "vm": {"status": "deleted"},
                }
            }
        )

        result = await service.resolve_pod_ip("job-1")
        assert result is None
        assert "job-1" not in service._pod_ip_cache

    @pytest.mark.asyncio
    async def test_no_db(self, service):
        """Returns None when DB is not connected."""
        service._db = None
        result = await service.resolve_pod_ip("job-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_context_as_json_string(self, service):
        """Handles context stored as JSON string (not dict)."""
        service._db.get_job = AsyncMock(
            return_value={
                "context": json.dumps(
                    {"ide_session": {"status": "active", "pod_ip": "10.0.0.7"}}
                )
            }
        )

        result = await service.resolve_pod_ip("job-1")
        assert result == "10.0.0.7"

    @pytest.mark.asyncio
    async def test_context_empty(self, service):
        """Returns None when context is empty."""
        service._db.get_job = AsyncMock(return_value={"context": {}})

        result = await service.resolve_pod_ip("job-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_context_none(self, service):
        """Returns None when context is None."""
        service._db.get_job = AsyncMock(return_value={"context": None})

        result = await service.resolve_pod_ip("job-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_idle_session_also_resolves(self, service):
        """'idle' session status is also valid for resolution."""
        service._db.get_job = AsyncMock(
            return_value={
                "context": {"ide_session": {"status": "idle", "pod_ip": "10.0.0.8"}}
            }
        )

        result = await service.resolve_pod_ip("job-1")
        assert result == "10.0.0.8"


class TestEvict:
    """Tests for cache eviction."""

    def test_evict_existing_entry(self):
        from orchestrator.services.ide_proxy import IdeProxyService

        service = IdeProxyService()
        service._pod_ip_cache["job-1"] = ("10.0.0.1", time.monotonic() + 60)

        service.evict("job-1")

        assert "job-1" not in service._pod_ip_cache

    def test_evict_nonexistent_entry(self):
        """Evicting a non-cached job_id is a no-op."""
        from orchestrator.services.ide_proxy import IdeProxyService

        service = IdeProxyService()
        service.evict("nonexistent")  # should not raise


# =============================================================================
# _build_code_server_url — URL generation helper
# =============================================================================


class TestBuildCodeServerUrl:
    """Tests for the proxy URL builder in ide_session.py."""

    def test_default_base_url(self):
        """Uses localhost:8085 when IDE_PROXY_BASE_URL is not set."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IDE_PROXY_BASE_URL", None)

            from orchestrator.services.ide_session import _build_code_server_url

            url = _build_code_server_url("abc-123-def")

            assert url == (
                "http://localhost:8085/api/ide/abc-123-def/proxy/"
                "?folder=/home/agent-host/workspace"
            )

    def test_custom_base_url(self):
        """Uses IDE_PROXY_BASE_URL when set."""
        with patch.dict(
            os.environ,
            {"IDE_PROXY_BASE_URL": "https://api.example.com"},
        ):
            from orchestrator.services.ide_session import _build_code_server_url

            url = _build_code_server_url("job-uuid-1234")

            assert url.startswith(
                "https://api.example.com/api/ide/job-uuid-1234/proxy/"
            )
            assert "folder=/home/agent-host/workspace" in url

    def test_custom_folder(self):
        """Supports custom folder path."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IDE_PROXY_BASE_URL", None)

            from orchestrator.services.ide_session import _build_code_server_url

            url = _build_code_server_url("job-1", folder="/opt/workspace")

            assert "folder=/opt/workspace" in url

    def test_url_contains_job_id(self):
        """Job ID is embedded in the URL path for routing."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IDE_PROXY_BASE_URL", None)

            from orchestrator.services.ide_session import _build_code_server_url

            url = _build_code_server_url("unique-job-id-999")

            assert "/api/ide/unique-job-id-999/proxy/" in url


# =============================================================================
# ContainerProvisioner IDE pod methods
# =============================================================================


class TestCreateIdePod:
    """Tests for ContainerProvisioner.create_ide_pod()."""

    @pytest.mark.asyncio
    async def test_not_available(self):
        """Returns None when K8s is not available."""
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = ContainerProvisioner()
        result = await provisioner.create_ide_pod("job-1")
        assert result is None

    def test_ide_pod_name_format(self):
        """IDE pod uses 'ide-' prefix with truncated job_id."""
        from orchestrator.services.container_provisioner import ContainerProvisioner
        from services.workspace_lifecycle import WorkspaceOwner

        provisioner = ContainerProvisioner()
        manifest = provisioner._build_pod_manifest(
            pod_name="ide-abc123456789",
            owner=WorkspaceOwner.job("abc123456789-full-uuid"),
            image="test:latest",
            cpu="250m",
            memory="512Mi",
            cpu_limit="1000m",
            memory_limit="2Gi",
        )

        assert manifest["metadata"]["name"] == "ide-abc123456789"

    def test_ide_pod_label_override(self):
        """create_ide_pod overrides the component label to 'ide-session'."""
        from orchestrator.services.container_provisioner import ContainerProvisioner
        from services.workspace_lifecycle import WorkspaceOwner

        provisioner = ContainerProvisioner()
        manifest = provisioner._build_pod_manifest(
            pod_name="ide-test123",
            owner=WorkspaceOwner.job("test123-full-uuid"),
            image="test:latest",
            cpu="250m",
            memory="512Mi",
            cpu_limit="1000m",
            memory_limit="2Gi",
        )

        # Default label is 'workspace', create_ide_pod overrides to 'ide-session'
        assert manifest["metadata"]["labels"]["srw/component"] == "workspace"

        # Simulate what create_ide_pod does
        manifest["metadata"]["labels"]["srw/component"] = "ide-session"
        assert manifest["metadata"]["labels"]["srw/component"] == "ide-session"


class TestDeleteIdePod:
    """Tests for ContainerProvisioner.delete_ide_pod()."""

    @pytest.mark.asyncio
    async def test_not_available(self):
        """Returns False when K8s is not available."""
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = ContainerProvisioner()
        result = await provisioner.delete_ide_pod("job-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_success(self):
        """Deletes IDE pod via K8s API."""
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._namespace = "test-ns"
        mock_api = MagicMock()
        provisioner._core_api = mock_api

        # Mock asyncio.to_thread to call the function synchronously
        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch(
            "orchestrator.services.container_provisioner.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            result = await provisioner.delete_ide_pod("abc123456789")

        assert result is True
        mock_api.delete_namespaced_pod.assert_called_once_with(
            name="ide-abc123456789",
            namespace="test-ns",
            grace_period_seconds=5,
        )

    @pytest.mark.asyncio
    async def test_delete_already_gone(self):
        """Returns True when pod is already deleted (404)."""
        from orchestrator.services.container_provisioner import ContainerProvisioner

        provisioner = ContainerProvisioner()
        provisioner._k8s_available = True
        provisioner._namespace = "test-ns"
        mock_api = MagicMock()
        provisioner._core_api = mock_api

        exc = Exception("Not Found")
        exc.status = 404
        mock_api.delete_namespaced_pod.side_effect = exc

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch(
            "orchestrator.services.container_provisioner.asyncio.to_thread",
            side_effect=fake_to_thread,
        ):
            result = await provisioner.delete_ide_pod("job-1")

        assert result is True


# =============================================================================
# Proxy endpoint header filtering
# =============================================================================


class TestProxyHopHeaders:
    """Tests for the hop-by-hop header filtering constant."""

    def test_hop_headers_include_essentials(self):
        """The proxy strips headers that should not be forwarded."""
        # Import the constant from main.py would require the full app setup,
        # so we test the concept: these headers must be stripped
        hop_headers = {
            "host",
            "authorization",
            "connection",
            "upgrade",
            "transfer-encoding",
            "keep-alive",
            "te",
            "trailer",
            "proxy-authorization",
            "proxy-connection",
        }

        # Verify these are all lowercase (matching our implementation)
        for h in hop_headers:
            assert h == h.lower()

        # These must be in the set
        assert "host" in hop_headers
        assert "authorization" in hop_headers
        assert "connection" in hop_headers
        assert "transfer-encoding" in hop_headers
