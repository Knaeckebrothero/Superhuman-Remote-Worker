"""Tests for the VM Provisioner module (orchestrator/services/vm_provisioner.py).

Tests cover:
1. Backend selection logic (NATS vs direct K8s vs disabled)
2. Template loading and rendering with variable substitution
3. create_vm() for both NATS and direct backends
4. delete_vm() for both NATS and direct backends
5. query_status() for both NATS and direct backends
6. send_control() (NATS-only)
7. Error handling (K8s failures, NATS failures, missing template)
8. _set_vm_context() context mutation
9. Edge cases: missing env vars, malformed templates, timeout scenarios
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
import yaml

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

PROVISION_GENERATION = "00000000-0000-4000-8000-000000000001"


# =============================================================================
# Sample template for tests
# =============================================================================

SAMPLE_VM_TEMPLATE = """\
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: agent-vm-${JOB_ID}
  namespace: agent-vms
  labels:
    app: agent-vm
    job-id: "${JOB_ID}"
spec:
  running: true
  dataVolumeTemplates:
    - metadata:
        name: agent-vm-${JOB_ID}-rootdisk
      spec:
        storage:
          accessModes:
            - ReadWriteOnce
          storageClassName: ${VM_STORAGE_CLASS}
          resources:
            requests:
              storage: ${VM_DISK_SIZE}
        source:
          registry:
            url: "docker://${VM_IMAGE}"
  template:
    metadata:
      labels:
        app: agent-vm
        job-id: "${JOB_ID}"
    spec:
      domain:
        cpu:
          cores: ${CPU_CORES}
        resources:
          requests:
            memory: "${MEMORY}"
      volumes:
        - name: rootdisk
          dataVolume:
            name: agent-vm-${JOB_ID}-rootdisk
      nodeSelector: {}
  config:
    agentConfig: "${AGENT_CONFIG}"
    natsUrl: "${NATS_URL}"
    description: "${DESCRIPTION}"
"""

SAMPLE_RENDERED = yaml.safe_load(
    SAMPLE_VM_TEMPLATE.replace("${JOB_ID}", "test-job-123")
    .replace("${AGENT_CONFIG}", "defaults")
    .replace("${VM_IMAGE}", "ghcr.io/example/image:latest")
    .replace("${CPU_CORES}", "2")
    .replace("${MEMORY}", "4Gi")
    .replace("${NATS_URL}", "nats://nats:4222")
    .replace("${DESCRIPTION}", "Test task")
    .replace("${VM_STORAGE_CLASS}", "local-path")
    .replace("${VM_DISK_SIZE}", "20Gi")
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_nats_bridge():
    """Create a mock nats_bridge with controllable is_available."""
    bridge = MagicMock()
    bridge.is_available = False
    bridge.request_vm_create = AsyncMock(return_value=True)
    bridge.request_vm_delete = AsyncMock(return_value=True)
    bridge.query_vm_status = AsyncMock(
        return_value={"job_id": "test", "status": "running"}
    )
    bridge.send_control = AsyncMock(return_value=True)
    return bridge


@pytest.fixture
def mock_db():
    """Create a mock PostgresDB instance."""
    db = MagicMock()
    db.merge_vm_context = AsyncMock()
    db.merge_thread_vm_context = AsyncMock()
    db.merge_vm_context_if_provision_generation = AsyncMock(return_value=True)
    db.merge_thread_vm_context_if_provision_generation = AsyncMock(return_value=True)
    db.get_job = AsyncMock(
        return_value={"context": {"vm": {"provision_generation": PROVISION_GENERATION}}}
    )
    db.get_thread = AsyncMock(
        return_value={
            "metadata": {"vm": {"provision_generation": PROVISION_GENERATION}}
        }
    )
    return db


@pytest.fixture
def mock_k8s_api():
    """Create a mock Kubernetes CustomObjectsApi."""
    api = MagicMock()

    def _admit_object(**kwargs):
        body = kwargs["body"]
        return {
            **body,
            "metadata": {
                **body.get("metadata", {}),
                "uid": "direct-admitted-vm-uid",
            },
        }

    api.create_namespaced_custom_object = MagicMock(side_effect=_admit_object)
    api.delete_namespaced_custom_object = MagicMock(return_value={"status": "Success"})
    api.get_namespaced_custom_object = MagicMock(
        return_value={
            "metadata": {
                "name": "agent-vm-test-job-123",
                "uid": "queried-vm-uid",
                "annotations": {
                    "srw.io/provision-generation": PROVISION_GENERATION,
                },
            },
            "status": {
                "printableStatus": "Running",
                "created": True,
                "conditions": [
                    {"type": "Ready", "status": "True"},
                ],
            },
        }
    )
    return api


@pytest.fixture
def template_file(tmp_path):
    """Write a sample VM template to a temp file and return its path."""
    template_path = tmp_path / "vm-template.yaml"
    template_path.write_text(SAMPLE_VM_TEMPLATE)
    return str(template_path)


@pytest.fixture
def provisioner_with_k8s(mock_k8s_api, template_file, mock_db):
    """Create a VMProvisioner configured for direct K8s mode."""
    with patch.dict(
        os.environ, {"VM_TEMPLATE_PATH": template_file, "VM_NAMESPACE": "test-ns"}
    ):
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            # Import fresh to get clean singleton behavior
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            prov._k8s = mock_k8s_api
            prov._core_k8s = MagicMock()

            def _read_pvc(**kwargs):
                name = kwargs["name"]
                owner_id = name[len("agent-vm-") : -len("-rootdisk")]
                return {
                    "metadata": {
                        "name": name,
                        "uid": f"direct-root-pvc-uid-{owner_id}",
                        "labels": {
                            "srw.io/owner-kind": "job",
                            "srw.io/owner-id": owner_id,
                        },
                    }
                }

            prov._core_k8s.read_namespaced_persistent_volume_claim.side_effect = (
                _read_pvc
            )
            prov._k8s_available = True
            prov._db = mock_db
            prov._vm_namespace = "test-ns"
            with open(template_file) as f:
                prov._template_text = f.read()
            yield prov


@pytest.fixture
def provisioner_with_nats(mock_nats_bridge, mock_db):
    """Create a VMProvisioner configured for NATS mode."""
    with patch("orchestrator.services.vm_provisioner.nats_bridge", mock_nats_bridge):
        mock_nats_bridge.is_available = True
        from orchestrator.services.vm_provisioner import VMProvisioner

        prov = VMProvisioner()
        prov._db = mock_db
        yield prov


@pytest.fixture
def provisioner_disabled():
    """Create a VMProvisioner with no backend available."""
    with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
        nb.is_available = False
        from orchestrator.services.vm_provisioner import VMProvisioner

        prov = VMProvisioner()
        prov._k8s_available = False
        prov._k8s = None
        prov._db = None
        yield prov


# =============================================================================
# Test: Backend Selection Logic
# =============================================================================


class TestBackendSelection:
    """Tests for is_available and mode properties."""

    def test_mode_nats_when_nats_available(self, mock_nats_bridge):
        """Mode returns 'nats' when NATS bridge is available."""
        with patch(
            "orchestrator.services.vm_provisioner.nats_bridge", mock_nats_bridge
        ):
            mock_nats_bridge.is_available = True
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            assert prov.mode == "nats"
            assert prov.is_available is True

    def test_mode_direct_when_k8s_available(self):
        """Mode returns 'direct' when K8s is available and NATS is not."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            prov._k8s_available = True
            assert prov.mode == "direct"
            assert prov.is_available is True

    def test_mode_none_when_nothing_available(self, provisioner_disabled):
        """Mode returns None when no backend is available."""
        assert provisioner_disabled.mode is None
        assert provisioner_disabled.is_available is False

    def test_nats_takes_priority_over_k8s(self, mock_nats_bridge):
        """NATS is preferred when both backends are available."""
        with patch(
            "orchestrator.services.vm_provisioner.nats_bridge", mock_nats_bridge
        ):
            mock_nats_bridge.is_available = True
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            prov._k8s_available = True
            assert prov.mode == "nats"

    def test_is_available_false_by_default(self):
        """Fresh provisioner is not available without initialization."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            assert prov.is_available is False


# =============================================================================
# Test: Connect and K8s Initialization
# =============================================================================


class TestConnect:
    """Tests for the connect() and _init_k8s() lifecycle methods."""

    def test_connect_stores_db(self, mock_db):
        """connect() stores the db reference."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            with patch.object(prov, "_init_k8s"):
                prov.connect(mock_db)
            assert prov._db is mock_db

    def test_connect_calls_init_k8s(self, mock_db):
        """connect() calls _init_k8s()."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            with patch.object(prov, "_init_k8s") as mock_init:
                prov.connect(mock_db)
            mock_init.assert_called_once()

    def test_init_k8s_skips_when_no_k8s_module(self):
        """_init_k8s() does nothing when kubernetes package is not installed."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            with patch("orchestrator.services.vm_provisioner.K8S_AVAILABLE", False):
                from orchestrator.services.vm_provisioner import VMProvisioner

                prov = VMProvisioner()
                prov._init_k8s()
                assert prov._k8s is None
                assert prov._k8s_available is False

    def test_init_k8s_loads_incluster_config(self, template_file):
        """_init_k8s() tries in-cluster config first."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            with patch("orchestrator.services.vm_provisioner.K8S_AVAILABLE", True):
                mock_k8s_config = MagicMock()
                mock_k8s_client = MagicMock()
                with (
                    patch(
                        "orchestrator.services.vm_provisioner.k8s_config",
                        mock_k8s_config,
                    ),
                    patch(
                        "orchestrator.services.vm_provisioner.k8s_client",
                        mock_k8s_client,
                    ),
                    patch.dict(os.environ, {"VM_TEMPLATE_PATH": template_file}),
                ):
                    from orchestrator.services.vm_provisioner import VMProvisioner

                    prov = VMProvisioner()
                    prov._init_k8s()
                    mock_k8s_config.load_incluster_config.assert_called_once()
                    assert prov._k8s_available is True

    def test_init_k8s_falls_back_to_kubeconfig(self, template_file):
        """_init_k8s() falls back to kubeconfig when in-cluster fails."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            with patch("orchestrator.services.vm_provisioner.K8S_AVAILABLE", True):
                mock_k8s_config = MagicMock()
                mock_k8s_config.ConfigException = type(
                    "ConfigException", (Exception,), {}
                )
                mock_k8s_config.load_incluster_config.side_effect = (
                    mock_k8s_config.ConfigException("no cluster")
                )
                mock_k8s_client = MagicMock()
                with (
                    patch(
                        "orchestrator.services.vm_provisioner.k8s_config",
                        mock_k8s_config,
                    ),
                    patch(
                        "orchestrator.services.vm_provisioner.k8s_client",
                        mock_k8s_client,
                    ),
                    patch.dict(os.environ, {"VM_TEMPLATE_PATH": template_file}),
                ):
                    from orchestrator.services.vm_provisioner import VMProvisioner

                    prov = VMProvisioner()
                    prov._init_k8s()
                    mock_k8s_config.load_kube_config.assert_called_once()
                    assert prov._k8s_available is True

    def test_init_k8s_disabled_when_no_config(self):
        """_init_k8s() disables K8s when neither in-cluster nor kubeconfig works."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            with patch("orchestrator.services.vm_provisioner.K8S_AVAILABLE", True):
                mock_k8s_config = MagicMock()
                mock_k8s_config.ConfigException = type(
                    "ConfigException", (Exception,), {}
                )
                mock_k8s_config.load_incluster_config.side_effect = (
                    mock_k8s_config.ConfigException("no cluster")
                )
                mock_k8s_config.load_kube_config.side_effect = (
                    mock_k8s_config.ConfigException("no kubeconfig")
                )
                mock_k8s_client = MagicMock()
                with (
                    patch(
                        "orchestrator.services.vm_provisioner.k8s_config",
                        mock_k8s_config,
                    ),
                    patch(
                        "orchestrator.services.vm_provisioner.k8s_client",
                        mock_k8s_client,
                    ),
                ):
                    from orchestrator.services.vm_provisioner import VMProvisioner

                    prov = VMProvisioner()
                    prov._init_k8s()
                    assert prov._k8s is None
                    assert prov._k8s_available is False

    def test_init_k8s_handles_unexpected_exception(self):
        """_init_k8s() catches arbitrary exceptions and disables K8s."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            with patch("orchestrator.services.vm_provisioner.K8S_AVAILABLE", True):
                mock_k8s_config = MagicMock()
                mock_k8s_config.load_incluster_config.side_effect = RuntimeError(
                    "unexpected"
                )
                with patch(
                    "orchestrator.services.vm_provisioner.k8s_config", mock_k8s_config
                ):
                    from orchestrator.services.vm_provisioner import VMProvisioner

                    prov = VMProvisioner()
                    prov._init_k8s()
                    assert prov._k8s is None
                    assert prov._k8s_available is False


# =============================================================================
# Test: Template Loading
# =============================================================================


class TestTemplateLoading:
    """Tests for _load_template() and template discovery."""

    def test_load_template_from_env_var(self, template_file):
        """Template is loaded from VM_TEMPLATE_PATH env var."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            with patch.dict(os.environ, {"VM_TEMPLATE_PATH": template_file}):
                from orchestrator.services.vm_provisioner import VMProvisioner

                prov = VMProvisioner()
                prov._k8s = MagicMock()  # Pretend K8s is initialized
                prov._load_template()
                assert prov._k8s_available is True
                assert "${JOB_ID}" in prov._template_text

    def test_load_template_disables_when_file_missing(self):
        """K8s is disabled when template file does not exist."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            with patch.dict(
                os.environ, {"VM_TEMPLATE_PATH": "/nonexistent/template.yaml"}
            ):
                from orchestrator.services.vm_provisioner import VMProvisioner

                prov = VMProvisioner()
                prov._k8s = MagicMock()
                prov._load_template()
                assert prov._k8s_available is False
                assert prov._k8s is None

    def test_load_template_disables_when_no_path_configured(self):
        """K8s is disabled when VM_TEMPLATE_PATH is empty and no candidate exists."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            env = dict(os.environ)
            env.pop("VM_TEMPLATE_PATH", None)
            with (
                patch.dict(os.environ, env, clear=True),
                patch("os.path.exists", return_value=False),
            ):
                from orchestrator.services.vm_provisioner import VMProvisioner

                prov = VMProvisioner()
                prov._k8s = MagicMock()
                prov._load_template()
                assert prov._k8s_available is False

    def test_load_template_candidate_paths(self, tmp_path):
        """Template discovery tries candidate paths when VM_TEMPLATE_PATH is unset."""
        # Create one of the candidate paths
        candidate = tmp_path / "config" / "vm-template.yaml"
        candidate.parent.mkdir(parents=True)
        candidate.write_text(SAMPLE_VM_TEMPLATE)

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            env = dict(os.environ)
            env.pop("VM_TEMPLATE_PATH", None)
            with patch.dict(os.environ, env, clear=True):
                from orchestrator.services.vm_provisioner import VMProvisioner

                prov = VMProvisioner()
                prov._k8s = MagicMock()

                # Patch os.path.exists to match the candidate
                orig_exists = os.path.exists

                def custom_exists(p):
                    if p == "/config/vm-template.yaml":
                        return True
                    return orig_exists(p)

                with (
                    patch("os.path.exists", side_effect=custom_exists),
                    patch("builtins.open", create=True) as mock_open,
                ):
                    mock_open.return_value.__enter__ = lambda s: s
                    mock_open.return_value.__exit__ = MagicMock(return_value=False)
                    mock_open.return_value.read = MagicMock(
                        return_value=SAMPLE_VM_TEMPLATE
                    )
                    prov._load_template()
                    assert prov._k8s_available is True


# =============================================================================
# Test: Template Rendering
# =============================================================================


class TestTemplateRendering:
    """Tests for _render_template() variable substitution."""

    def test_render_template_basic(self, provisioner_with_k8s):
        """Template renders with all required fields."""
        job_config = {
            "job_id": "abc-123",
            "agent_config": "scholar",
            "vm_image": "ghcr.io/example/vm:v1",
            "cpu_cores": 4,
            "memory": "8Gi",
            "nats_url": "nats://nats:4222",
            "description": "Research task",
        }
        result = provisioner_with_k8s._render_template(job_config)

        assert result["metadata"]["name"] == "agent-vm-abc-123"
        assert result["metadata"]["labels"]["job-id"] == "abc-123"
        assert result["metadata"]["labels"]["srw.io/owner-kind"] == "job"
        assert result["metadata"]["labels"]["srw.io/owner-id"] == "abc-123"
        vmi_labels = result["spec"]["template"]["metadata"]["labels"]
        assert vmi_labels["srw.io/owner-kind"] == "job"
        assert vmi_labels["srw.io/owner-id"] == "abc-123"
        data_volume_labels = result["spec"]["dataVolumeTemplates"][0]["metadata"][
            "labels"
        ]
        assert data_volume_labels["srw.io/owner-kind"] == "job"
        assert data_volume_labels["srw.io/owner-id"] == "abc-123"
        assert result["spec"]["template"]["spec"]["domain"]["cpu"]["cores"] == 4
        assert (
            result["spec"]["template"]["spec"]["domain"]["resources"]["requests"][
                "memory"
            ]
            == "8Gi"
        )
        assert (
            result["spec"]["dataVolumeTemplates"][0]["spec"]["source"]["registry"][
                "url"
            ]
            == "docker://ghcr.io/example/vm:v1"
        )
        assert result["spec"]["config"]["agentConfig"] == "scholar"
        assert result["spec"]["config"]["natsUrl"] == "nats://nats:4222"
        assert result["spec"]["config"]["description"] == "Research task"

    def test_render_template_defaults(self, provisioner_with_k8s):
        """Template renders with default values when optionals are omitted."""
        job_config = {
            "job_id": "test-456",
        }
        result = provisioner_with_k8s._render_template(job_config)

        assert result["metadata"]["name"] == "agent-vm-test-456"
        # Defaults: agent_config=defaults, cpu=8, memory=16Gi
        assert result["spec"]["config"]["agentConfig"] == "worker_base"
        assert result["spec"]["template"]["spec"]["domain"]["cpu"]["cores"] == 8
        assert (
            result["spec"]["template"]["spec"]["domain"]["resources"]["requests"][
                "memory"
            ]
            == "16Gi"
        )

    def test_render_template_uses_default_vm_image(self, provisioner_with_k8s):
        """Template uses DEFAULT_VM_IMAGE when vm_image is None."""
        job_config = {
            "job_id": "test-789",
            "vm_image": None,
        }
        result = provisioner_with_k8s._render_template(job_config)

        # Should use the default image from the provisioner
        image_url = result["spec"]["dataVolumeTemplates"][0]["spec"]["source"][
            "registry"
        ]["url"]
        assert image_url == f"docker://{provisioner_with_k8s._default_vm_image}"

    def test_render_template_custom_vm_image(self, provisioner_with_k8s):
        """Template uses explicit vm_image when provided."""
        custom_image = "registry.example.com/my-vm:v2"
        job_config = {
            "job_id": "test-img",
            "vm_image": custom_image,
        }
        result = provisioner_with_k8s._render_template(job_config)

        image_url = result["spec"]["dataVolumeTemplates"][0]["spec"]["source"][
            "registry"
        ]["url"]
        assert image_url == f"docker://{custom_image}"

    def test_render_template_cpu_cores_as_int(self, provisioner_with_k8s):
        """CPU cores are rendered as integers in YAML."""
        job_config = {
            "job_id": "test-cpu",
            "cpu_cores": 8,
        }
        result = provisioner_with_k8s._render_template(job_config)
        assert result["spec"]["template"]["spec"]["domain"]["cpu"]["cores"] == 8

    def test_render_template_empty_description(self, provisioner_with_k8s):
        """Template handles empty description gracefully."""
        job_config = {
            "job_id": "test-empty-desc",
            "description": "",
        }
        result = provisioner_with_k8s._render_template(job_config)
        assert result["spec"]["config"]["description"] == ""

    def test_render_template_special_characters_in_description(
        self, provisioner_with_k8s
    ):
        """Template handles descriptions with special YAML characters."""
        job_config = {
            "job_id": "test-special",
            "description": "Analyze data: find patterns & relationships",
        }
        # Should not raise
        result = provisioner_with_k8s._render_template(job_config)
        assert "Analyze data" in result["spec"]["config"]["description"]

    def test_render_template_stamps_full_thread_identity(self, provisioner_with_k8s):
        result = provisioner_with_k8s._render_template(
            {"job_id": "thread-123", "entity_type": "thread"}
        )

        for labels in (
            result["metadata"]["labels"],
            result["spec"]["template"]["metadata"]["labels"],
            result["spec"]["dataVolumeTemplates"][0]["metadata"]["labels"],
        ):
            assert labels["srw.io/owner-kind"] == "thread"
            assert labels["srw.io/owner-id"] == "thread-123"


# =============================================================================
# Test: create_vm()
# =============================================================================


class TestCreateVm:
    """Tests for create_vm() across both backends."""

    @pytest.mark.asyncio
    async def test_create_vm_nats_backend(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        """create_vm() delegates to nats_bridge when NATS is available."""
        result = await provisioner_with_nats.create_vm(
            job_id="job-001",
            agent_config="developer",
            vm_image="my-image:v1",
            cpu_cores=4,
            memory="8Gi",
            description="Build feature",
        )
        assert result is True
        mock_nats_bridge.request_vm_create.assert_awaited_once_with(
            job_id="job-001",
            agent_config="developer",
            vm_image="my-image:v1",
            cpu_cores=4,
            memory="8Gi",
            description="Build feature",
            entity_type="job",
            set_provisioning=True,
            provision_generation=ANY,
        )

    @pytest.mark.asyncio
    async def test_create_vm_direct_backend(
        self, provisioner_with_k8s, mock_k8s_api, mock_db
    ):
        """create_vm() calls K8s API directly when NATS is unavailable."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.create_vm(
                job_id="job-002",
                agent_config="defaults",
                cpu_cores=2,
                memory="4Gi",
                description="Test task",
            )
        assert result is True
        mock_k8s_api.create_namespaced_custom_object.assert_called_once()
        call_kwargs = mock_k8s_api.create_namespaced_custom_object.call_args
        assert call_kwargs[1]["group"] == "kubevirt.io"
        assert call_kwargs[1]["version"] == "v1"
        assert call_kwargs[1]["namespace"] == "test-ns"
        assert call_kwargs[1]["plural"] == "virtualmachines"

    @pytest.mark.asyncio
    async def test_http_create_carries_thread_owner_kind(self, provisioner_disabled):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {
            "status": "created",
            "vm_name": "agent-vm-thread-1",
            "vm_uid": "http-admitted-vm-uid",
            "rootdisk_pvc_uid": "http-root-pvc-uid",
            "namespace": "agent-vms",
        }
        provisioner_disabled._set_thread_vm_context = AsyncMock()
        provisioner_disabled._http_client = MagicMock()
        provisioner_disabled._http_client.post = AsyncMock(return_value=response)

        assert await provisioner_disabled._create_http(
            job_id="thread-1",
            agent_config="worker_base",
            vm_image=None,
            cpu_cores=8,
            memory="16Gi",
            description="",
            entity_type="thread",
        )

        payload = provisioner_disabled._http_client.post.await_args.kwargs["json"]
        assert payload["job_id"] == "thread-1"
        assert payload["entity_type"] == "thread"
        # An unsigned legacy controller response remains operational telemetry,
        # but its reusable names/UID strings are not authoritative identity.
        created_updates = provisioner_disabled._set_thread_vm_context.await_args_list[
            -1
        ].args[1]
        assert created_updates["status"] == "created"
        assert "vm_uid" not in created_updates
        assert "rootdisk_pvc_uid" not in created_updates

    @pytest.mark.asyncio
    async def test_create_vm_direct_updates_context_on_success(
        self, provisioner_with_k8s, mock_db
    ):
        """Successful direct create_vm() updates job context with created status."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.create_vm(
                job_id="job-003",
                agent_config="defaults",
            )
        mock_db.merge_vm_context_if_provision_generation.assert_awaited()
        # Find the "created" context update
        calls = mock_db.merge_vm_context_if_provision_generation.await_args_list
        created_call = [c for c in calls if c.args[2].get("status") == "created"]
        assert len(created_call) == 1
        ctx = created_call[0].args[2]
        assert ctx["provisioned_by"] == "direct"
        assert "vm_name" in ctx
        assert ctx["vm_uid"] == "direct-admitted-vm-uid"
        assert ctx["rootdisk_pvc_uid"] == "direct-root-pvc-uid-job-003"

    @pytest.mark.asyncio
    async def test_create_vm_direct_updates_context_on_failure(
        self, provisioner_with_k8s, mock_k8s_api, mock_db
    ):
        """Failed direct create_vm() updates job context with failed status."""
        mock_k8s_api.create_namespaced_custom_object.side_effect = Exception(
            "API server unreachable"
        )

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.create_vm(
                job_id="job-004",
                agent_config="defaults",
            )
        assert result is False
        calls = mock_db.merge_vm_context_if_provision_generation.await_args_list
        failed_call = [c for c in calls if c.args[2].get("status") == "failed"]
        assert len(failed_call) == 1
        ctx = failed_call[0].args[2]
        assert "API server unreachable" in ctx["error"]
        assert ctx["provisioned_by"] == "direct"

    @pytest.mark.asyncio
    async def test_create_vm_returns_false_when_disabled(self, provisioner_disabled):
        """create_vm() returns False when no backend is available."""
        result = await provisioner_disabled.create_vm(job_id="job-005")
        assert result is False

    @pytest.mark.asyncio
    async def test_create_vm_nats_failure(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        """create_vm() returns False when NATS publish fails."""
        mock_nats_bridge.request_vm_create = AsyncMock(return_value=False)
        result = await provisioner_with_nats.create_vm(job_id="job-006")
        assert result is False

    @pytest.mark.asyncio
    async def test_create_vm_direct_renders_correct_manifest(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """Direct create_vm() renders template with correct job parameters."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.create_vm(
                job_id="render-test",
                agent_config="critic",
                vm_image="my-custom-image:v3",
                cpu_cores=8,
                memory="16Gi",
                description="Heavy analysis",
            )
        call_kwargs = mock_k8s_api.create_namespaced_custom_object.call_args
        manifest = call_kwargs[1]["body"]
        assert manifest["metadata"]["name"] == "agent-vm-render-test"
        assert manifest["spec"]["config"]["agentConfig"] == "critic"
        assert manifest["spec"]["template"]["spec"]["domain"]["cpu"]["cores"] == 8
        assert (
            manifest["spec"]["template"]["spec"]["domain"]["resources"]["requests"][
                "memory"
            ]
            == "16Gi"
        )

    @pytest.mark.asyncio
    async def test_create_vm_prefers_nats_over_direct(
        self, mock_nats_bridge, mock_db, mock_k8s_api, template_file
    ):
        """create_vm() prefers NATS over direct K8s when both are available."""
        with patch(
            "orchestrator.services.vm_provisioner.nats_bridge", mock_nats_bridge
        ):
            mock_nats_bridge.is_available = True
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            prov._k8s = mock_k8s_api
            prov._k8s_available = True
            prov._db = mock_db
            with open(template_file) as f:
                prov._template_text = f.read()

            await prov.create_vm(job_id="priority-test")

        mock_nats_bridge.request_vm_create.assert_awaited_once()
        mock_k8s_api.create_namespaced_custom_object.assert_not_called()


# =============================================================================
# Test: fresh-provision reset (C — reap counter / stale endpoint hygiene)
# =============================================================================


class TestFreshProvisionReset:
    """Every (re)provision must reset stale VM lifecycle/probe state."""

    def test_fresh_provision_ctx_shape(self):
        from orchestrator.services.vm_provisioner import VMProvisioner

        ctx = VMProvisioner._fresh_provision_ctx()
        assert ctx["snapshot_attempts"] == 0
        assert ctx["ssh_host"] is None
        assert ctx["ssh_port"] is None
        assert ctx["ssh_registration_id"] is None
        assert ctx["registered_at"] is None
        assert ctx["ssh_verified_at"] is None
        assert ctx["ssh_probe_attempts"] == 0
        assert ctx["ssh_probe_error"] is None
        assert ctx["ssh_probe_failed_at"] is None
        assert ctx["vm_uid"] is None
        assert ctx["rootdisk_pvc_uid"] is None
        assert ctx["golden_wait_started_at"] is None
        # A stale teardown anchor would make the next incarnation read as
        # instantly-stuck in 'deleting' and be recycled on sight.
        assert ctx["deleting_started_at"] is None
        assert isinstance(ctx["provisioned_at"], float)

    @pytest.mark.asyncio
    async def test_create_vm_nats_resets_before_dispatch(
        self, provisioner_with_nats, mock_nats_bridge, mock_db
    ):
        await provisioner_with_nats.create_vm(job_id="reset-nats")
        # First context write is the reset — before nats_bridge writes
        # 'provisioning' — so it can't clobber the live provisioning status.
        first = mock_db.merge_vm_context.await_args_list[0]
        assert first[0][0] == "reset-nats"
        ctx = first[0][1]
        assert ctx["snapshot_attempts"] == 0
        assert ctx["ssh_host"] is None
        assert "provisioned_at" in ctx

    @pytest.mark.asyncio
    async def test_create_vm_direct_resets(self, provisioner_with_k8s, mock_db):
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.create_vm(job_id="reset-direct")
        reset = [
            c
            for c in mock_db.merge_vm_context.await_args_list
            if c[0][1].get("snapshot_attempts") == 0
        ]
        assert len(reset) == 1
        assert reset[0][0][1]["ssh_host"] is None
        assert "provisioned_at" in reset[0][0][1]

    @pytest.mark.asyncio
    async def test_create_thread_vm_resets(self, mock_nats_bridge, mock_db):
        mock_db.merge_thread_vm_context = AsyncMock()
        with patch(
            "orchestrator.services.vm_provisioner.nats_bridge", mock_nats_bridge
        ):
            mock_nats_bridge.is_available = True
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            prov._db = mock_db
            await prov.create_thread_vm(thread_id="reset-thread")
        first = mock_db.merge_thread_vm_context.await_args_list[0]
        assert first[0][0] == "reset-thread"
        assert first[0][1]["snapshot_attempts"] == 0
        assert first[0][1]["ssh_host"] is None
        assert mock_nats_bridge.request_vm_create.await_args.kwargs["entity_type"] == (
            "thread"
        )


# =============================================================================
# Test: golden-poll re-issue (fresh=False)
# (knowledge-history/done/golden_image_cold_import_fails_inflight_vm_jobs.md)
# =============================================================================


class TestGoldenPollCreate:
    """create_vm(fresh=False) — the dispatcher's waiting_golden poll.

    A poll must NOT reset the provision context (golden_wait_started_at
    anchors the golden budget across polls) and must not flip the context
    status to 'provisioning' (it must stay waiting_golden so the decision
    logic keeps polling). Only provisioned_at rolls forward, so the boot
    budget starts ≈ when the golden completes and the VM is actually built.
    """

    @pytest.mark.asyncio
    async def test_poll_rolls_only_provisioned_at(
        self, provisioner_with_nats, mock_nats_bridge, mock_db
    ):
        await provisioner_with_nats.create_vm(job_id="poll-1", fresh=False)
        first = mock_db.merge_vm_context.await_args_list[0]
        assert first[0][0] == "poll-1"
        ctx = first[0][1]
        assert set(ctx.keys()) == {"provisioned_at"}

    @pytest.mark.asyncio
    async def test_poll_passes_set_provisioning_false(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        await provisioner_with_nats.create_vm(job_id="poll-2", fresh=False)
        kwargs = mock_nats_bridge.request_vm_create.await_args.kwargs
        assert kwargs["set_provisioning"] is False

    @pytest.mark.asyncio
    async def test_fresh_default_resets_and_sets_provisioning(
        self, provisioner_with_nats, mock_nats_bridge, mock_db
    ):
        await provisioner_with_nats.create_vm(job_id="fresh-1")
        ctx = mock_db.merge_vm_context.await_args_list[0][0][1]
        assert ctx["snapshot_attempts"] == 0
        assert ctx["golden_wait_started_at"] is None
        kwargs = mock_nats_bridge.request_vm_create.await_args.kwargs
        assert kwargs["set_provisioning"] is True


# =============================================================================
# Test: delete_vm()
# =============================================================================


class TestDeleteVm:
    """Tests for delete_vm() across both backends."""

    @pytest.mark.asyncio
    async def test_delete_vm_nats_backend(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        """delete_vm() delegates to nats_bridge when NATS is available."""
        result = await provisioner_with_nats.delete_vm(job_id="job-del-001")
        assert result is True
        mock_nats_bridge.request_vm_delete.assert_awaited_once_with(
            "job-del-001",
            purge_disk=True,
            provision_generation=PROVISION_GENERATION,
            entity_type="job",
        )

    @pytest.mark.asyncio
    async def test_delete_vm_direct_backend(
        self, provisioner_with_k8s, mock_k8s_api, mock_db
    ):
        """delete_vm() calls K8s delete API directly when NATS is unavailable."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.delete_vm(job_id="job-del-002")
        assert result is True
        mock_k8s_api.delete_namespaced_custom_object.assert_called_once()
        call_kwargs = mock_k8s_api.delete_namespaced_custom_object.call_args
        assert call_kwargs[1]["name"] == "agent-vm-job-del-002"
        assert call_kwargs[1]["namespace"] == "test-ns"
        assert call_kwargs[1]["body"]["preconditions"] == {"uid": "queried-vm-uid"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entity_type", ["job", "thread"])
    async def test_direct_delete_without_durable_generation_fails_closed(
        self,
        entity_type,
        provisioner_with_k8s,
        mock_k8s_api,
        mock_db,
    ):
        if entity_type == "thread":
            mock_db.get_thread.return_value = {"metadata": {"vm": {}}}
        else:
            mock_db.get_job.return_value = {"context": {"vm": {}}}

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            if entity_type == "thread":
                result = await provisioner_with_k8s.delete_thread_vm("legacy")
            else:
                result = await provisioner_with_k8s.delete_vm("legacy")

        assert result is False
        mock_k8s_api.get_namespaced_custom_object.assert_not_called()
        mock_k8s_api.delete_namespaced_custom_object.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entity_type", ["job", "thread"])
    async def test_direct_delete_generation_mismatch_fails_closed(
        self,
        entity_type,
        provisioner_with_k8s,
        mock_k8s_api,
    ):
        mock_k8s_api.get_namespaced_custom_object.return_value["metadata"][
            "annotations"
        ]["srw.io/provision-generation"] = "00000000-0000-4000-8000-000000000099"

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            if entity_type == "thread":
                result = await provisioner_with_k8s.delete_thread_vm("reused")
            else:
                result = await provisioner_with_k8s.delete_vm("reused")

        assert result is False
        mock_k8s_api.delete_namespaced_custom_object.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_vm_direct_updates_context(
        self, provisioner_with_k8s, mock_db
    ):
        """Successful direct delete_vm() updates job context with deleted status."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.delete_vm(job_id="job-del-003")
        mock_db.merge_vm_context_if_provision_generation.assert_awaited_once_with(
            "job-del-003", PROVISION_GENERATION, {"status": "deleted"}
        )

    @pytest.mark.asyncio
    async def test_delete_vm_direct_failure(self, provisioner_with_k8s, mock_k8s_api):
        """Failed direct delete_vm() returns False."""
        mock_k8s_api.delete_namespaced_custom_object.side_effect = Exception(
            "404 Not Found"
        )

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.delete_vm(job_id="job-del-404")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_vm_returns_false_when_disabled(self, provisioner_disabled):
        """delete_vm() returns False when no backend is available."""
        result = await provisioner_disabled.delete_vm(job_id="job-del-none")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_vm_nats_failure(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        """delete_vm() returns False when NATS publish fails."""
        mock_nats_bridge.request_vm_delete = AsyncMock(return_value=False)
        result = await provisioner_with_nats.delete_vm(job_id="job-del-fail")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_vm_direct_constructs_correct_vm_name(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """Direct delete uses 'agent-vm-{job_id}' naming convention."""
        job_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.delete_vm(job_id=job_id)
        call_kwargs = mock_k8s_api.delete_namespaced_custom_object.call_args
        assert call_kwargs[1]["name"] == f"agent-vm-{job_id}"


# =============================================================================
# Test: query_status()
# =============================================================================


class TestQueryStatus:
    """Tests for query_status() across both backends."""

    @pytest.mark.asyncio
    async def test_query_status_nats_backend(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        """query_status() delegates to nats_bridge when NATS is available."""
        expected = {"job_id": "status-001", "status": "running", "ready": True}
        mock_nats_bridge.query_vm_status = AsyncMock(return_value=expected)

        result = await provisioner_with_nats.query_status(
            job_id="status-001", timeout=3.0
        )
        assert result == expected
        mock_nats_bridge.query_vm_status.assert_awaited_once_with(
            "status-001", 3.0, provision_generation=PROVISION_GENERATION
        )

    @pytest.mark.asyncio
    async def test_nats_status_persists_late_rootdisk_identity(
        self, provisioner_with_nats, mock_nats_bridge, mock_db
    ):
        mock_nats_bridge.query_vm_status.return_value = {
            "job_id": "late-nats",
            "vm_name": "agent-vm-late-nats",
            "namespace": "agent-vms",
            "vm_uid": "late-nats-vm-uid",
            "rootdisk_pvc_uid": "late-nats-root-pvc-uid",
            "provision_generation": PROVISION_GENERATION,
            "_identity_authenticated": True,
        }

        result = await provisioner_with_nats.query_status("late-nats")

        assert result is not None
        assert "_identity_authenticated" not in result
        updates = mock_db.merge_vm_context_if_provision_generation.await_args.args[2]
        assert updates["vm_uid"] == "late-nats-vm-uid"
        assert updates["rootdisk_pvc_uid"] == "late-nats-root-pvc-uid"
        assert updates["identity_authenticated"] is True

    @pytest.mark.asyncio
    async def test_http_status_persists_late_thread_rootdisk_identity(
        self, provisioner_disabled, mock_db
    ):
        from orchestrator.services.vm_lifecycle_auth import sign_payload

        secret = b"http-lifecycle-status-test-secret-at-least-32-bytes"
        provisioner_disabled._db = mock_db
        provisioner_disabled._controller_url = "http://vm-controller:8080"
        provisioner_disabled._lifecycle_hmac_secret = secret
        client = MagicMock()

        async def _get(_path, *, params, timeout):
            response = MagicMock(status_code=200)
            response.raise_for_status = MagicMock()
            response.json.return_value = sign_payload(
                {
                    "job_id": "late-http-thread",
                    "vm_name": "agent-vm-late-http-thread",
                    "namespace": "agent-vms",
                    "vm_uid": "late-http-vm-uid",
                    "rootdisk_pvc_uid": "late-http-root-pvc-uid",
                    "provision_generation": PROVISION_GENERATION,
                },
                direction="response",
                operation="status",
                secret=secret,
                correlation_id=params["lifecycle_auth_request_id"],
            )
            return response

        client.get = AsyncMock(side_effect=_get)
        provisioner_disabled._http_client = client

        result = await provisioner_disabled.query_status(
            "late-http-thread", entity_type="thread"
        )

        assert result is not None
        updates = (
            mock_db.merge_thread_vm_context_if_provision_generation.await_args.args[2]
        )
        assert updates["vm_uid"] == "late-http-vm-uid"
        assert updates["rootdisk_pvc_uid"] == "late-http-root-pvc-uid"

    @pytest.mark.asyncio
    async def test_direct_status_persists_late_rootdisk_identity(
        self, provisioner_with_k8s, mock_db
    ):
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.query_status("test-job-123")

        assert result is not None
        updates = mock_db.merge_vm_context_if_provision_generation.await_args.args[2]
        assert updates["vm_uid"] == "queried-vm-uid"
        assert updates["rootdisk_pvc_uid"] == ("direct-root-pvc-uid-test-job-123")
        assert updates["identity_authenticated"] is True

    @pytest.mark.asyncio
    async def test_query_status_direct_backend_ready(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """Direct query_status() returns correct status when VM is Ready."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.query_status(job_id="test-job-123")

        assert result is not None
        assert result["job_id"] == "test-job-123"
        assert result["vm_name"] == "agent-vm-test-job-123"
        assert result["ready"] is True
        assert result["phase"] == "Running"
        assert result["created"] is True

    @pytest.mark.asyncio
    async def test_query_status_direct_backend_not_ready(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """Direct query_status() detects VM that is not Ready."""
        mock_k8s_api.get_namespaced_custom_object.return_value = {
            "metadata": {"name": "agent-vm-test"},
            "status": {
                "printableStatus": "Provisioning",
                "created": False,
                "conditions": [
                    {"type": "Ready", "status": "False"},
                ],
            },
        }

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.query_status(job_id="test")

        assert result["ready"] is False
        assert result["phase"] == "Provisioning"
        assert result["created"] is False

    @pytest.mark.asyncio
    async def test_query_status_direct_no_conditions(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """Direct query_status() handles missing conditions gracefully."""
        mock_k8s_api.get_namespaced_custom_object.return_value = {
            "metadata": {"name": "agent-vm-test"},
            "status": {},
        }

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.query_status(job_id="no-conditions")

        assert result["ready"] is False
        assert result["phase"] == "Unknown"

    @pytest.mark.asyncio
    async def test_query_status_direct_failure(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """Direct query_status() returns None on K8s API failure."""
        mock_k8s_api.get_namespaced_custom_object.side_effect = Exception(
            "connection refused"
        )

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.query_status(job_id="fail-query")

        assert result is None

    @pytest.mark.asyncio
    async def test_query_status_returns_none_when_disabled(self, provisioner_disabled):
        """query_status() returns None when no backend is available."""
        result = await provisioner_disabled.query_status(job_id="no-backend")
        assert result is None

    @pytest.mark.asyncio
    async def test_query_status_nats_timeout(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        """query_status() returns None when NATS request times out."""
        mock_nats_bridge.query_vm_status = AsyncMock(return_value=None)
        result = await provisioner_with_nats.query_status(
            job_id="timeout-test", timeout=1.0
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_query_status_default_timeout(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        """query_status() uses default 5.0s timeout when not specified."""
        await provisioner_with_nats.query_status(job_id="default-timeout")
        mock_nats_bridge.query_vm_status.assert_awaited_once_with(
            "default-timeout", 5.0, provision_generation=PROVISION_GENERATION
        )

    @pytest.mark.asyncio
    async def test_query_status_direct_multiple_conditions(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """Direct query_status() finds Ready among multiple conditions."""
        mock_k8s_api.get_namespaced_custom_object.return_value = {
            "metadata": {"name": "agent-vm-multi"},
            "status": {
                "printableStatus": "Running",
                "created": True,
                "conditions": [
                    {"type": "AgentConnected", "status": "True"},
                    {"type": "Ready", "status": "True"},
                    {"type": "Synchronized", "status": "False"},
                ],
            },
        }

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.query_status(job_id="multi-cond")

        assert result["ready"] is True


# =============================================================================
# Test: send_control()
# =============================================================================


class TestSendControl:
    """Tests for send_control() (NATS-only operation)."""

    @pytest.mark.asyncio
    async def test_send_control_freeze(self, provisioner_with_nats, mock_nats_bridge):
        """send_control() sends freeze command via NATS."""
        result = await provisioner_with_nats.send_control(
            job_id="ctrl-001", action="freeze"
        )
        assert result is True
        mock_nats_bridge.send_control.assert_awaited_once_with("ctrl-001", "freeze")

    @pytest.mark.asyncio
    async def test_send_control_resume(self, provisioner_with_nats, mock_nats_bridge):
        """send_control() sends resume command via NATS."""
        result = await provisioner_with_nats.send_control(
            job_id="ctrl-002", action="resume"
        )
        assert result is True
        mock_nats_bridge.send_control.assert_awaited_once_with("ctrl-002", "resume")

    @pytest.mark.asyncio
    async def test_send_control_terminate(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        """send_control() sends terminate command via NATS."""
        result = await provisioner_with_nats.send_control(
            job_id="ctrl-003", action="terminate"
        )
        assert result is True
        mock_nats_bridge.send_control.assert_awaited_once_with("ctrl-003", "terminate")

    @pytest.mark.asyncio
    async def test_send_control_returns_false_without_nats(self, provisioner_disabled):
        """send_control() returns False when NATS is unavailable."""
        result = await provisioner_disabled.send_control(
            job_id="ctrl-no-nats", action="freeze"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_send_control_returns_false_with_direct_only(
        self, provisioner_with_k8s
    ):
        """send_control() returns False in direct-K8s mode (needs NATS daemon)."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.send_control(
                job_id="ctrl-direct", action="freeze"
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_send_control_nats_failure(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        """send_control() returns False when NATS publish fails."""
        mock_nats_bridge.send_control = AsyncMock(return_value=False)
        result = await provisioner_with_nats.send_control(
            job_id="ctrl-fail", action="terminate"
        )
        assert result is False


# =============================================================================
# Test: _set_vm_context()
# =============================================================================


class TestSetVmContext:
    """Tests for _set_vm_context() helper."""

    @pytest.mark.asyncio
    async def test_set_vm_context_calls_db(self, provisioner_with_k8s, mock_db):
        """_set_vm_context() delegates to db.merge_vm_context."""
        updates = {"status": "created", "vm_name": "agent-vm-test"}
        await provisioner_with_k8s._set_vm_context("job-ctx-001", updates)
        mock_db.merge_vm_context.assert_awaited_once_with("job-ctx-001", updates)

    @pytest.mark.asyncio
    async def test_set_vm_context_no_db(self):
        """_set_vm_context() is a no-op when db is not set."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            prov._db = None
            # Should not raise
            await prov._set_vm_context("job-no-db", {"status": "test"})

    @pytest.mark.asyncio
    async def test_set_vm_context_handles_db_error(self, provisioner_with_k8s, mock_db):
        """_set_vm_context() catches and logs db exceptions."""
        mock_db.merge_vm_context = AsyncMock(
            side_effect=Exception("DB connection lost")
        )
        # Should not raise
        await provisioner_with_k8s._set_vm_context("job-db-err", {"status": "test"})
        mock_db.merge_vm_context.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_vm_context_passes_arbitrary_updates(
        self, provisioner_with_k8s, mock_db
    ):
        """_set_vm_context() passes the exact updates dict to the DB."""
        updates = {
            "status": "ready",
            "ssh_host": "10.0.0.5",
            "ssh_port": 22,
            "custom_key": "custom_value",
        }
        await provisioner_with_k8s._set_vm_context("job-arb", updates)
        mock_db.merge_vm_context.assert_awaited_once_with("job-arb", updates)


# =============================================================================
# Test: Environment Variable Configuration
# =============================================================================


class TestEnvironmentConfiguration:
    """Tests for environment variable handling during __init__."""

    def test_default_namespace(self):
        """Default VM namespace is 'agent-vms'."""
        env = dict(os.environ)
        env.pop("VM_NAMESPACE", None)
        with patch.dict(os.environ, env, clear=True):
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            assert prov._vm_namespace == "agent-vms"

    def test_custom_namespace(self):
        """VM_NAMESPACE env var overrides the default."""
        with patch.dict(os.environ, {"VM_NAMESPACE": "custom-ns"}):
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            assert prov._vm_namespace == "custom-ns"

    def test_default_vm_image(self):
        """Default VM image is the ghcr.io image."""
        env = dict(os.environ)
        env.pop("DEFAULT_VM_IMAGE", None)
        with patch.dict(os.environ, env, clear=True):
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            assert "ghcr.io" in prov._default_vm_image
            assert "agent-vm-base" in prov._default_vm_image

    def test_custom_vm_image(self):
        """DEFAULT_VM_IMAGE env var overrides the default."""
        with patch.dict(
            os.environ, {"DEFAULT_VM_IMAGE": "my-registry.io/custom-vm:v5"}
        ):
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            assert prov._default_vm_image == "my-registry.io/custom-vm:v5"


# =============================================================================
# Test: Error Handling and Edge Cases
# =============================================================================


class TestErrorHandling:
    """Tests for error handling and edge cases."""

    @pytest.mark.asyncio
    async def test_create_direct_k8s_api_error(
        self, provisioner_with_k8s, mock_k8s_api, mock_db
    ):
        """_create_direct() handles K8s API errors gracefully."""
        mock_k8s_api.create_namespaced_custom_object.side_effect = Exception(
            "forbidden: not authorized"
        )

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.create_vm(
                job_id="err-001",
                agent_config="defaults",
            )
        assert result is False
        # Context should be updated with failure
        calls = mock_db.merge_vm_context_if_provision_generation.await_args_list
        assert any(c.args[2].get("status") == "failed" for c in calls)

    @pytest.mark.asyncio
    async def test_delete_direct_k8s_404(self, provisioner_with_k8s, mock_k8s_api):
        """_delete_direct() handles K8s 404 (VM already gone)."""
        mock_k8s_api.delete_namespaced_custom_object.side_effect = Exception(
            "Not Found: virtualmachines.kubevirt.io 'agent-vm-gone' not found"
        )

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.delete_vm(job_id="gone")
        assert result is False

    @pytest.mark.asyncio
    async def test_query_direct_k8s_connection_error(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """_query_direct() handles connection errors."""
        mock_k8s_api.get_namespaced_custom_object.side_effect = ConnectionError(
            "Connection refused"
        )

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.query_status(job_id="conn-err")
        assert result is None

    @pytest.mark.asyncio
    async def test_create_direct_template_rendering_error(self, mock_db):
        """_create_direct() handles template rendering errors."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            prov._k8s = MagicMock()
            prov._k8s_available = True
            prov._db = mock_db
            # Set malformed template that will cause yaml.safe_load to fail
            prov._template_text = "{{invalid yaml: [[[}"

            result = await prov.create_vm(
                job_id="bad-template", agent_config="defaults"
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_create_direct_with_nats_url_env(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """_create_direct() includes NATS_URL from environment in template."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            with patch.dict(os.environ, {"NATS_URL": "nats://custom:4222"}):
                await provisioner_with_k8s.create_vm(job_id="nats-env-test")

        call_kwargs = mock_k8s_api.create_namespaced_custom_object.call_args
        manifest = call_kwargs[1]["body"]
        assert manifest["spec"]["config"]["natsUrl"] == "nats://custom:4222"

    @pytest.mark.asyncio
    async def test_delete_direct_does_not_update_context_on_failure(
        self, provisioner_with_k8s, mock_k8s_api, mock_db
    ):
        """_delete_direct() does not call _set_vm_context on failure."""
        mock_k8s_api.delete_namespaced_custom_object.side_effect = Exception(
            "API error"
        )

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.delete_vm(job_id="no-ctx-update")

        # No lifecycle state is written when Kubernetes rejects the delete.
        mock_db.merge_vm_context_if_provision_generation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_query_status_direct_empty_status(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """Direct query handles VM with completely empty status."""
        mock_k8s_api.get_namespaced_custom_object.return_value = {
            "metadata": {"name": "agent-vm-empty"},
        }

        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            result = await provisioner_with_k8s.query_status(job_id="empty-status")

        assert result is not None
        assert result["ready"] is False
        assert result["phase"] == "Unknown"
        assert result["created"] is False


# =============================================================================
# Test: KubeVirt API Constants
# =============================================================================


class TestKubeVirtConstants:
    """Tests for the KubeVirt API coordinate constants."""

    def test_kubevirt_group(self):
        """KubeVirt group is kubevirt.io."""
        from orchestrator.services.vm_provisioner import KUBEVIRT_GROUP

        assert KUBEVIRT_GROUP == "kubevirt.io"

    def test_kubevirt_version(self):
        """KubeVirt version is v1."""
        from orchestrator.services.vm_provisioner import KUBEVIRT_VERSION

        assert KUBEVIRT_VERSION == "v1"

    def test_kubevirt_plural(self):
        """KubeVirt plural is virtualmachines."""
        from orchestrator.services.vm_provisioner import KUBEVIRT_PLURAL

        assert KUBEVIRT_PLURAL == "virtualmachines"


# =============================================================================
# Test: Module-Level Singleton
# =============================================================================


class TestSingleton:
    """Tests for the module-level vm_provisioner singleton."""

    def test_singleton_exists(self):
        """Module exports a vm_provisioner singleton."""
        from orchestrator.services.vm_provisioner import vm_provisioner

        assert vm_provisioner is not None

    def test_singleton_is_vm_provisioner(self):
        """Singleton is an instance of VMProvisioner."""
        from orchestrator.services.vm_provisioner import VMProvisioner, vm_provisioner

        assert isinstance(vm_provisioner, VMProvisioner)


# =============================================================================
# Test: Direct Backend K8s API Parameters
# =============================================================================


class TestDirectBackendApiParams:
    """Tests that direct backend passes correct K8s API parameters."""

    @pytest.mark.asyncio
    async def test_create_uses_kubevirt_coordinates(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """_create_direct() uses correct KubeVirt API group/version/plural."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.create_vm(job_id="api-test")

        call_kwargs = mock_k8s_api.create_namespaced_custom_object.call_args[1]
        assert call_kwargs["group"] == "kubevirt.io"
        assert call_kwargs["version"] == "v1"
        assert call_kwargs["plural"] == "virtualmachines"

    @pytest.mark.asyncio
    async def test_delete_uses_kubevirt_coordinates(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """_delete_direct() uses correct KubeVirt API group/version/plural."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.delete_vm(job_id="del-api-test")

        call_kwargs = mock_k8s_api.delete_namespaced_custom_object.call_args[1]
        assert call_kwargs["group"] == "kubevirt.io"
        assert call_kwargs["version"] == "v1"
        assert call_kwargs["plural"] == "virtualmachines"

    @pytest.mark.asyncio
    async def test_query_uses_kubevirt_coordinates(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """_query_direct() uses correct KubeVirt API group/version/plural."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.query_status(job_id="query-api-test")

        call_kwargs = mock_k8s_api.get_namespaced_custom_object.call_args[1]
        assert call_kwargs["group"] == "kubevirt.io"
        assert call_kwargs["version"] == "v1"
        assert call_kwargs["plural"] == "virtualmachines"

    @pytest.mark.asyncio
    async def test_create_uses_configured_namespace(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """_create_direct() uses the configured VM namespace."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.create_vm(job_id="ns-test")

        call_kwargs = mock_k8s_api.create_namespaced_custom_object.call_args[1]
        assert call_kwargs["namespace"] == "test-ns"

    @pytest.mark.asyncio
    async def test_delete_uses_configured_namespace(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """_delete_direct() uses the configured VM namespace."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.delete_vm(job_id="ns-del-test")

        call_kwargs = mock_k8s_api.delete_namespaced_custom_object.call_args[1]
        assert call_kwargs["namespace"] == "test-ns"

    @pytest.mark.asyncio
    async def test_query_uses_configured_namespace(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        """_query_direct() uses the configured VM namespace."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            await provisioner_with_k8s.query_status(job_id="ns-query-test")

        call_kwargs = mock_k8s_api.get_namespaced_custom_object.call_args[1]
        assert call_kwargs["namespace"] == "test-ns"


# =============================================================================
# Test: Concurrent Operations
# =============================================================================


class TestConcurrentOperations:
    """Tests for concurrent VM operations."""

    @pytest.mark.asyncio
    async def test_multiple_creates_in_parallel(self, mock_nats_bridge, mock_db):
        """Multiple create_vm() calls can run concurrently via NATS."""
        with patch(
            "orchestrator.services.vm_provisioner.nats_bridge", mock_nats_bridge
        ):
            mock_nats_bridge.is_available = True
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            prov._db = mock_db

            results = await asyncio.gather(
                prov.create_vm(job_id="par-001"),
                prov.create_vm(job_id="par-002"),
                prov.create_vm(job_id="par-003"),
            )

        assert all(r is True for r in results)
        assert mock_nats_bridge.request_vm_create.await_count == 3

    @pytest.mark.asyncio
    async def test_create_and_query_in_parallel(self, mock_nats_bridge, mock_db):
        """create_vm() and query_status() can run concurrently."""
        with patch(
            "orchestrator.services.vm_provisioner.nats_bridge", mock_nats_bridge
        ):
            mock_nats_bridge.is_available = True
            from orchestrator.services.vm_provisioner import VMProvisioner

            prov = VMProvisioner()
            prov._db = mock_db

            create_result, status_result = await asyncio.gather(
                prov.create_vm(job_id="mix-001"),
                prov.query_status(job_id="mix-002"),
            )

        assert create_result is True
        assert status_result is not None


# =============================================================================
# Test: asyncio.to_thread Usage in Direct Backend
# =============================================================================


class TestAsyncToThread:
    """Tests that direct backend properly offloads sync K8s calls to threads."""

    @pytest.mark.asyncio
    async def test_create_runs_in_thread(self, provisioner_with_k8s, mock_k8s_api):
        """_create_direct() uses asyncio.to_thread for K8s API call."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            with patch(
                "orchestrator.services.vm_provisioner.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_to_thread:

                def _thread_result(callable_, *args, **kwargs):
                    if callable_ == mock_k8s_api.create_namespaced_custom_object:
                        body = kwargs["body"]
                        return {
                            **body,
                            "metadata": {
                                **body.get("metadata", {}),
                                "name": "agent-vm-thread-create",
                                "uid": "threaded-admitted-vm-uid",
                            },
                        }
                    return {
                        "metadata": {
                            "name": "agent-vm-thread-create-rootdisk",
                            "uid": "threaded-root-pvc-uid",
                            "labels": {
                                "srw.io/owner-kind": "job",
                                "srw.io/owner-id": "thread-create",
                            },
                        }
                    }

                mock_to_thread.side_effect = _thread_result
                await provisioner_with_k8s.create_vm(job_id="thread-create")

        assert mock_to_thread.await_count == 2
        # First arg should be the K8s API method
        first_arg = mock_to_thread.await_args_list[0].args[0]
        assert first_arg == mock_k8s_api.create_namespaced_custom_object

    @pytest.mark.asyncio
    async def test_delete_runs_in_thread(self, provisioner_with_k8s, mock_k8s_api):
        """_delete_direct() uses asyncio.to_thread for K8s API call."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            with patch(
                "orchestrator.services.vm_provisioner.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_to_thread:
                mock_to_thread.side_effect = [
                    {
                        "metadata": {
                            "uid": "thread-delete-vm-uid",
                            "annotations": {
                                "srw.io/provision-generation": PROVISION_GENERATION,
                            },
                        }
                    },
                    {},
                ]
                await provisioner_with_k8s.delete_vm(job_id="thread-delete")

        assert mock_to_thread.await_count == 2
        assert (
            mock_to_thread.await_args_list[0].args[0]
            == mock_k8s_api.get_namespaced_custom_object
        )
        assert (
            mock_to_thread.await_args_list[1].args[0]
            == mock_k8s_api.delete_namespaced_custom_object
        )

    @pytest.mark.asyncio
    async def test_query_runs_in_thread(self, provisioner_with_k8s, mock_k8s_api):
        """_query_direct() uses asyncio.to_thread for K8s API call."""
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            with patch(
                "orchestrator.services.vm_provisioner.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as mock_to_thread:
                mock_to_thread.return_value = {
                    "status": {
                        "conditions": [],
                        "printableStatus": "Running",
                        "created": True,
                    }
                }
                await provisioner_with_k8s.query_status(job_id="thread-query")

        assert mock_to_thread.await_count == 2
        first_arg = mock_to_thread.await_args_list[0].args[0]
        assert first_arg == mock_k8s_api.get_namespaced_custom_object


# =============================================================================
# Test: list_vms() — orphan-sweep inventory across backends
# =============================================================================


class TestListVms:
    @pytest.mark.asyncio
    async def test_list_nats_backend(self, provisioner_with_nats, mock_nats_bridge):
        expected = [{"vm_name": "agent-vm-j1", "entity_id": "j1"}]
        mock_nats_bridge.request_vm_list = AsyncMock(return_value=expected)
        assert await provisioner_with_nats.list_vms() == expected

    @pytest.mark.asyncio
    async def test_list_direct_backend_filters_names(
        self, provisioner_with_k8s, mock_k8s_api
    ):
        mock_k8s_api.list_namespaced_custom_object = MagicMock(
            return_value={
                "items": [
                    {
                        "metadata": {
                            "name": "agent-vm-j1",
                            "creationTimestamp": "2026-07-09T09:00:00Z",
                        },
                        "status": {"printableStatus": "Running"},
                    },
                    {"metadata": {"name": "agent-vm-golden-abc"}},
                    {"metadata": {"name": "unrelated"}},
                ]
            }
        )
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            vms = await provisioner_with_k8s.list_vms()
        assert vms == [
            {
                "vm_name": "agent-vm-j1",
                "entity_id": "j1",
                "created_at": "2026-07-09T09:00:00Z",
                "phase": "Running",
            }
        ]

    @pytest.mark.asyncio
    async def test_list_http_backend(self, provisioner_disabled):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "vms": [{"vm_name": "agent-vm-j2", "entity_id": "j2"}]
        }
        provisioner_disabled._controller_url = "http://vm-controller:8080"
        provisioner_disabled._http_client = MagicMock()
        provisioner_disabled._http_client.get = AsyncMock(return_value=resp)
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            vms = await provisioner_disabled.list_vms()
        assert vms == [{"vm_name": "agent-vm-j2", "entity_id": "j2"}]

    @pytest.mark.asyncio
    async def test_list_http_404_means_unknown_not_empty(self, provisioner_disabled):
        # An old controller without the list op must yield None (unknown),
        # never [] — the sweep would otherwise treat it as "no VMs".
        resp = MagicMock()
        resp.status_code = 404
        provisioner_disabled._controller_url = "http://vm-controller:8080"
        provisioner_disabled._http_client = MagicMock()
        provisioner_disabled._http_client.get = AsyncMock(return_value=resp)
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            assert await provisioner_disabled.list_vms() is None

    @pytest.mark.asyncio
    async def test_list_no_backend_returns_none(self, provisioner_disabled):
        with patch("orchestrator.services.vm_provisioner.nats_bridge") as nb:
            nb.is_available = False
            assert await provisioner_disabled.list_vms() is None


# =============================================================================
# release_vm / release_thread_vm — snapshot outcome must be reported truthfully
# knowledge-base/knowledge/issues/vm_workspace_snapshot_unreachable_from_orchestrator.md
# =============================================================================


def _snapshot_service(*, captured: bool):
    """Snapshot service whose capture SUCCEEDS or is SKIPPED.

    capture_vm_snapshot returns False for an unroutable tailnet target — it does
    not raise — so a try/except around it cannot see the failure.
    """
    svc = MagicMock()
    svc.is_available = True
    svc.capture_vm_snapshot = AsyncMock(return_value=captured)
    return svc


class TestReleaseReportsSnapshotOutcome:
    """A VM workspace lives on the tailnet, which the orchestrator cannot reach,
    so capture_vm_snapshot returns False for every VM. Release deletes the VM
    regardless (by design, non-fatal) — but it must not claim it captured a
    snapshot it did not, or the logs actively mislead whoever investigates the
    missing workspace state."""

    @pytest.mark.asyncio
    async def test_thread_release_does_not_claim_capture_when_skipped(
        self, provisioner_with_nats, mock_nats_bridge, caplog
    ):
        provisioner_with_nats._snapshot_service = _snapshot_service(captured=False)
        provisioner_with_nats._db.get_thread = AsyncMock(
            return_value={
                "metadata": {"vm": {"ssh_host": "100.64.1.6", "ssh_port": 22}}
            }
        )

        with caplog.at_level("INFO"):
            ok = await provisioner_with_nats.release_thread_vm("tid-1")

        assert ok is True  # deletion still proceeds — non-fatal by design
        text = caplog.text
        assert "snapshot captured" not in text.lower(), (
            "must not report a capture that returned False"
        )
        assert "tid-1" in text

    @pytest.mark.asyncio
    async def test_thread_release_still_reports_a_real_capture(
        self, provisioner_with_nats, mock_nats_bridge, caplog
    ):
        provisioner_with_nats._snapshot_service = _snapshot_service(captured=True)
        provisioner_with_nats._db.get_thread = AsyncMock(
            return_value={"metadata": {"vm": {"ssh_host": "10.0.0.9", "ssh_port": 22}}}
        )

        with caplog.at_level("INFO"):
            await provisioner_with_nats.release_thread_vm("tid-2")

        assert "snapshot captured" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_job_release_does_not_claim_capture_when_skipped(
        self, provisioner_with_nats, mock_nats_bridge, caplog
    ):
        """release_vm carries the identical pattern for jobs."""
        provisioner_with_nats._snapshot_service = _snapshot_service(captured=False)
        provisioner_with_nats._db.get_job = AsyncMock(
            return_value={"context": {"vm": {"ssh_host": "100.64.1.9", "ssh_port": 22}}}
        )

        with caplog.at_level("INFO"):
            ok = await provisioner_with_nats.release_vm(
                "job-1", ssh_host="100.64.1.9", ssh_port=22
            )

        assert ok is True
        assert "snapshot captured" not in caplog.text.lower()


class TestPurgeDiskIntent:
    """``purge_disk`` tells the controller whether a delete is terminal.

    Default True everywhere, so a call site that says nothing keeps today's
    semantics; False means "a recreate is coming, keep the rootdisk".
    knowledge-base/knowledge/features/vm_persistent_rootdisk.md D2.
    """

    @pytest.mark.asyncio
    async def test_job_delete_defaults_to_purge(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        await provisioner_with_nats.delete_vm("job-1")
        mock_nats_bridge.request_vm_delete.assert_awaited_once_with(
            "job-1",
            purge_disk=True,
            provision_generation=PROVISION_GENERATION,
            entity_type="job",
        )

    @pytest.mark.asyncio
    async def test_job_delete_can_keep_the_disk(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        await provisioner_with_nats.delete_vm("job-1", purge_disk=False)
        mock_nats_bridge.request_vm_delete.assert_awaited_once_with(
            "job-1",
            purge_disk=False,
            provision_generation=PROVISION_GENERATION,
            entity_type="job",
        )

    @pytest.mark.asyncio
    async def test_thread_delete_can_keep_the_disk(
        self, provisioner_with_nats, mock_nats_bridge
    ):
        await provisioner_with_nats.delete_thread_vm("tid-1", purge_disk=False)
        mock_nats_bridge.request_vm_delete.assert_awaited_once_with(
            "tid-1",
            purge_disk=False,
            provision_generation=PROVISION_GENERATION,
            entity_type="thread",
        )

    @pytest.mark.asyncio
    async def test_release_purges(self, provisioner_with_nats, mock_nats_bridge):
        """Release is terminal for the entity — the disk goes with it."""
        provisioner_with_nats._snapshot_service = None
        await provisioner_with_nats.release_vm("job-1")
        mock_nats_bridge.request_vm_delete.assert_awaited_once_with(
            "job-1",
            purge_disk=True,
            provision_generation=PROVISION_GENERATION,
            entity_type="job",
        )

    @pytest.mark.asyncio
    async def test_thread_release_purges(self, provisioner_with_nats, mock_nats_bridge):
        provisioner_with_nats._snapshot_service = None
        await provisioner_with_nats.release_thread_vm("tid-1")
        mock_nats_bridge.request_vm_delete.assert_awaited_once_with(
            "tid-1",
            purge_disk=True,
            provision_generation=PROVISION_GENERATION,
            entity_type="thread",
        )


class TestCapturedVmTeardown:
    @staticmethod
    def _identity():
        from orchestrator.services.vm_provisioner import VMTeardownIdentity

        return VMTeardownIdentity(
            provision_generation=PROVISION_GENERATION,
            vm_uid="captured-vm-uid",
            rootdisk_pvc_uid="captured-root-uid",
        )

    @staticmethod
    def _probe(disposition: str, *, vm_uid=None, root_uid=None, known=True):
        from orchestrator.services.vm_provisioner import (
            VMTeardownIdentity,
            _VMTeardownProbe,
        )

        return _VMTeardownProbe(
            disposition,
            VMTeardownIdentity(PROVISION_GENERATION, vm_uid, root_uid),
            rootdisk_identity_known=known,
        )

    @pytest.mark.asyncio
    async def test_capture_probes_authenticated_late_rootdisk_uid(
        self, provisioner_with_k8s, mock_db
    ):
        mock_db.get_job.return_value = {
            "context": {
                "vm": {
                    "provision_generation": PROVISION_GENERATION,
                    "identity_provision_generation": PROVISION_GENERATION,
                    "identity_authenticated": True,
                    "vm_uid": "captured-vm-uid",
                }
            }
        }
        provisioner_with_k8s._probe_vm_teardown_identity = AsyncMock(
            return_value=self._probe(
                "present",
                vm_uid="captured-vm-uid",
                root_uid="late-root-uid",
                known=True,
            )
        )

        identity = await provisioner_with_k8s.capture_vm_teardown_identity("job-1")

        assert identity.vm_uid == "captured-vm-uid"
        assert identity.rootdisk_pvc_uid == "late-root-uid"

    @pytest.mark.asyncio
    async def test_delete_acceptance_waits_for_exact_vm_and_rootdisk_absence(
        self, provisioner_with_k8s
    ):
        present = self._probe(
            "present",
            vm_uid="captured-vm-uid",
            root_uid="captured-root-uid",
        )
        provisioner_with_k8s._probe_vm_teardown_identity = AsyncMock(
            side_effect=[present, present]
        )
        provisioner_with_k8s._delete_vm_with_identity = AsyncMock(return_value=True)

        outcome = await provisioner_with_k8s.delete_vm_captured(
            "job-1", self._identity()
        )

        assert outcome.disposition == "retry_pending"
        assert outcome.deleted is False

    @pytest.mark.asyncio
    async def test_response_loss_converges_with_stale_db_context_after_exact_absence(
        self, provisioner_with_k8s
    ):
        absent = self._probe("absent", vm_uid=None, root_uid=None, known=True)
        provisioner_with_k8s._probe_vm_teardown_identity = AsyncMock(
            return_value=absent
        )
        provisioner_with_k8s._delete_vm_with_identity = AsyncMock()

        outcome = await provisioner_with_k8s.delete_vm_captured(
            "job-1", self._identity()
        )

        assert outcome.disposition == "completed"
        assert outcome.deleted is True
        provisioner_with_k8s._delete_vm_with_identity.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("vm_uid", "root_uid"),
        [
            ("replacement-vm-uid", "captured-root-uid"),
            ("captured-vm-uid", "replacement-root-uid"),
        ],
    )
    async def test_proven_replacement_identity_supersedes_without_delete(
        self, provisioner_with_k8s, vm_uid, root_uid
    ):
        provisioner_with_k8s._probe_vm_teardown_identity = AsyncMock(
            return_value=self._probe(
                "present", vm_uid=vm_uid, root_uid=root_uid, known=True
            )
        )
        provisioner_with_k8s._delete_vm_with_identity = AsyncMock()

        outcome = await provisioner_with_k8s.delete_vm_captured(
            "job-1", self._identity()
        )

        assert outcome.disposition == "identity_superseded"
        provisioner_with_k8s._delete_vm_with_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transport_false_with_unchanged_identity_retries(
        self, provisioner_with_k8s
    ):
        present = self._probe(
            "present",
            vm_uid="captured-vm-uid",
            root_uid="captured-root-uid",
        )
        provisioner_with_k8s._probe_vm_teardown_identity = AsyncMock(
            side_effect=[present, present]
        )
        provisioner_with_k8s._delete_vm_with_identity = AsyncMock(return_value=False)

        outcome = await provisioner_with_k8s.delete_vm_captured(
            "job-1", self._identity()
        )

        assert outcome.disposition == "retry_pending"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("root_known", [False, True])
    async def test_active_vm_without_captured_rootdisk_uid_never_purges(
        self, provisioner_with_k8s, root_known
    ):
        identity = self._identity()
        identity = type(identity)(identity.provision_generation, identity.vm_uid, None)
        provisioner_with_k8s._probe_vm_teardown_identity = AsyncMock(
            return_value=self._probe(
                "present",
                vm_uid="captured-vm-uid",
                root_uid=None,
                known=root_known,
            )
        )
        provisioner_with_k8s._delete_vm_with_identity = AsyncMock()

        outcome = await provisioner_with_k8s.delete_vm_captured("job-1", identity)

        assert outcome.disposition == "identity_unknown"
        provisioner_with_k8s._delete_vm_with_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_orphan_purge_requires_captured_rootdisk_uid(
        self, provisioner_with_k8s
    ):
        identity = self._identity()
        identity = type(identity)(identity.provision_generation, identity.vm_uid, None)
        provisioner_with_k8s._delete_vm_with_identity = AsyncMock()

        outcome = await provisioner_with_k8s.delete_orphan_vm_captured(
            "job-1", identity, purge_disk=True
        )

        assert outcome.disposition == "identity_unknown"
        provisioner_with_k8s._delete_vm_with_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_orphan_delete_acceptance_waits_for_exact_absence(
        self, provisioner_with_k8s
    ):
        present = self._probe(
            "present",
            vm_uid="captured-vm-uid",
            root_uid="captured-root-uid",
        )
        provisioner_with_k8s._probe_vm_teardown_identity = AsyncMock(
            side_effect=[present, present]
        )
        provisioner_with_k8s._delete_vm_with_identity = AsyncMock(return_value=True)

        outcome = await provisioner_with_k8s.delete_orphan_vm_captured(
            "job-1", self._identity(), purge_disk=True
        )

        assert (outcome.disposition, outcome.deleted) == ("retry_pending", False)

    @pytest.mark.asyncio
    async def test_orphan_response_loss_converges_on_exact_absence(
        self, provisioner_with_k8s
    ):
        present = self._probe(
            "present",
            vm_uid="captured-vm-uid",
            root_uid="captured-root-uid",
        )
        absent = self._probe("absent", vm_uid=None, root_uid=None, known=True)
        provisioner_with_k8s._probe_vm_teardown_identity = AsyncMock(
            side_effect=[present, absent]
        )
        provisioner_with_k8s._delete_vm_with_identity = AsyncMock(return_value=False)

        outcome = await provisioner_with_k8s.delete_orphan_vm_captured(
            "job-1", self._identity(), purge_disk=True
        )

        assert (outcome.disposition, outcome.deleted) == ("completed", True)

    @pytest.mark.asyncio
    async def test_orphan_replacement_supersedes_without_delete(
        self, provisioner_with_k8s
    ):
        provisioner_with_k8s._probe_vm_teardown_identity = AsyncMock(
            return_value=self._probe(
                "present",
                vm_uid="replacement-vm-uid",
                root_uid="captured-root-uid",
            )
        )
        provisioner_with_k8s._delete_vm_with_identity = AsyncMock()

        outcome = await provisioner_with_k8s.delete_orphan_vm_captured(
            "job-1", self._identity(), purge_disk=True
        )

        assert (outcome.disposition, outcome.deleted) == (
            "identity_superseded",
            False,
        )
        provisioner_with_k8s._delete_vm_with_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_http_delete_sends_purge_disk_query_param(
        self, provisioner_with_nats
    ):
        client = AsyncMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "deleted"}
        response.raise_for_status = MagicMock()
        client.delete = AsyncMock(return_value=response)
        provisioner_with_nats._http_client = client

        await provisioner_with_nats._delete_http("job-1", purge_disk=False)

        assert client.delete.await_args.args[0] == "/vms/job-1"
        assert client.delete.await_args.kwargs["params"] == {"purge_disk": "false"}

    @pytest.mark.asyncio
    async def test_http_delete_omits_the_param_when_purging(
        self, provisioner_with_nats
    ):
        """Keeps the request byte-identical against an un-upgraded controller."""
        client = AsyncMock()
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "deleted"}
        response.raise_for_status = MagicMock()
        client.delete = AsyncMock(return_value=response)
        provisioner_with_nats._http_client = client

        await provisioner_with_nats._delete_http("job-1")

        assert client.delete.await_args.args[0] == "/vms/job-1"
        assert client.delete.await_args.kwargs["params"] == {}

    @pytest.mark.asyncio
    async def test_direct_mode_warns_that_keep_is_not_honoured(
        self, provisioner_with_k8s, caplog
    ):
        """Direct K8s mode has no controller, so the disk is still templated
        and cascade-deletes. Say so rather than silently purging."""
        with caplog.at_level("WARNING"):
            await provisioner_with_k8s.delete_vm("job-1", purge_disk=False)

        assert "purge_disk=False" in caplog.text
