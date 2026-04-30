"""VM Provisioner — Unified VM lifecycle management.

Four backends, auto-selected based on available infrastructure:

1. **NATS bridge** (cross-cluster): Publishes to NATS, VM Controller on remote
   agent cluster handles KubeVirt API calls. Used when NATS_URL is configured.

2. **HTTP controller** (same-cluster): POSTs to a co-located VM Controller
   over HTTP. The controller still owns KubeVirt RBAC and the VM template;
   only the transport changes. Used when VM_CONTROLLER_URL is configured.

3. **Direct K8s API** (same-cluster, no controller): Orchestrator calls
   KubeVirt API directly. Requires VM_TEMPLATE_PATH + kubevirt.io RBAC on
   the orchestrator's ServiceAccount.

4. **Docker** (compose): QEMU-in-Docker pool, see docker_provisioner.py.

Selection priority:
  NATS_URL set       → NATS mode
  VM_CONTROLLER_URL  → HTTP mode
  template + k8s     → direct mode
  docker hosts       → docker mode
  none               → VM features disabled

The VM endpoints and lifecycle hooks in main.py use this provisioner exclusively.
"""

import asyncio
import logging
import os
from typing import Any, Optional

import httpx
import yaml

from .nats_bridge import nats_bridge

try:
    from kubernetes import client as k8s_client, config as k8s_config

    K8S_AVAILABLE = True
except ImportError:
    k8s_client = None
    k8s_config = None
    K8S_AVAILABLE = False

logger = logging.getLogger(__name__)

# KubeVirt API coordinates (same as vm/controller/controller.py)
KUBEVIRT_GROUP = "kubevirt.io"
KUBEVIRT_VERSION = "v1"
KUBEVIRT_PLURAL = "virtualmachines"


def _extract_vm_context(job: dict) -> dict:
    """Extract the vm sub-dict from a job's context."""
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        import json

        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    return ctx.get("vm", {})


class VMProvisioner:
    """Unified VM provisioner with automatic backend selection.

    Uses NATS bridge when available (cross-cluster), falls back to direct
    Kubernetes API calls (same-cluster). Control commands (freeze/resume/
    terminate) require NATS since they target the management daemon inside
    the VM.
    """

    def __init__(self):
        self._db: Optional[Any] = None
        self._snapshot_service: Optional[Any] = None
        self._k8s: Optional[Any] = None
        self._k8s_available: bool = False
        self._template_text: str = ""
        self._vm_namespace: str = os.environ.get("VM_NAMESPACE", "agent-vms")
        self._default_vm_image: str = os.environ.get(
            "DEFAULT_VM_IMAGE",
            "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent-vm-base:latest",
        )
        # HTTP controller transport (same-cluster, no NATS).
        self._controller_url: str = os.environ.get("VM_CONTROLLER_URL", "").rstrip("/")
        self._http_client: Optional[httpx.AsyncClient] = None
        self._http_timeout: float = float(os.environ.get("VM_CONTROLLER_TIMEOUT", "30"))

    @property
    def is_available(self) -> bool:
        """True if any VM provisioning backend is available."""
        return (
            nats_bridge.is_available
            or self._http_available
            or self._k8s_available
            or self._docker_available
        )

    @property
    def _http_available(self) -> bool:
        """True if a co-located VM controller HTTP endpoint is configured."""
        return bool(self._controller_url)

    @property
    def _docker_available(self) -> bool:
        """True if QEMU-in-Docker VMs are configured."""
        from .docker_provisioner import docker_provisioner

        return len(docker_provisioner.vm_hosts) > 0

    @property
    def mode(self) -> Optional[str]:
        """Current provisioning mode: 'nats', 'http', 'direct', 'docker', or None."""
        if nats_bridge.is_available:
            return "nats"
        if self._http_available:
            return "http"
        if self._k8s_available:
            return "direct"
        if self._docker_available:
            return "docker"
        return None

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def connect(
        self,
        db: Any,
        snapshot_service: Optional[Any] = None,
    ) -> None:
        """Initialize the provisioner.

        Args:
            db: PostgresDB instance for job context updates.
            snapshot_service: Optional SnapshotService for archival before deletion.
        """
        self._db = db
        self._snapshot_service = snapshot_service
        self._init_k8s()

        if self._http_available:
            self._http_client = httpx.AsyncClient(
                base_url=self._controller_url,
                timeout=self._http_timeout,
            )

        if nats_bridge.is_available:
            logger.info("VM provisioner ready: NATS mode")
        elif self._http_available:
            logger.info(
                "VM provisioner ready: HTTP mode (controller=%s)",
                self._controller_url,
            )
        elif self._k8s_available:
            logger.info(
                "VM provisioner ready: direct K8s mode (namespace=%s)",
                self._vm_namespace,
            )
        else:
            logger.info(
                "VM provisioner: no backend available "
                "(NATS, controller URL, and K8s all unavailable)"
            )

    async def disconnect(self) -> None:
        """Close the HTTP client (if any)."""
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                logger.debug("Error closing VM controller HTTP client", exc_info=True)
            self._http_client = None

    def _init_k8s(self) -> None:
        """Try to initialize the Kubernetes client for direct provisioning."""
        if not K8S_AVAILABLE:
            return

        try:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                try:
                    k8s_config.load_kube_config()
                except k8s_config.ConfigException:
                    logger.debug(
                        "Kubernetes not available (no in-cluster or kubeconfig)"
                    )
                    return

            self._k8s = k8s_client.CustomObjectsApi()
            self._load_template()
        except Exception as e:
            logger.debug("Direct K8s VM provisioning not available: %s", e)
            self._k8s = None
            self._k8s_available = False

    def _load_template(self) -> None:
        """Load the KubeVirt VM template for direct provisioning."""
        path = os.environ.get("VM_TEMPLATE_PATH", "")
        if not path:
            for candidate in [
                "/config/vm-template.yaml",
                "deployment/harvester/vm-template.yaml",
            ]:
                if os.path.exists(candidate):
                    path = candidate
                    break

        if not path or not os.path.exists(path):
            logger.info("VM template not found — direct K8s provisioning disabled")
            self._k8s = None
            self._k8s_available = False
            return

        with open(path) as f:
            self._template_text = f.read()

        self._k8s_available = True
        logger.info("Loaded VM template from %s", path)

    def _render_template(self, job_config: dict) -> dict:
        """Render the VM template with job-specific values.

        Same string substitution as vm/controller/controller.py.
        """
        replacements = {
            "${JOB_ID}": job_config["job_id"],
            "${AGENT_CONFIG}": job_config.get("agent_config", "defaults"),
            "${VM_IMAGE}": job_config.get("vm_image") or self._default_vm_image,
            "${CPU_CORES}": str(job_config.get("cpu_cores", 2)),
            "${MEMORY}": job_config.get("memory", "4Gi"),
            "${NATS_URL}": job_config.get("nats_url", ""),
            "${DESCRIPTION}": job_config.get("description", ""),
            # CDI DataVolume storage
            "${VM_STORAGE_CLASS}": os.environ.get("VM_STORAGE_CLASS", "local-path"),
            "${VM_DISK_SIZE}": os.environ.get("VM_DISK_SIZE", "20Gi"),
        }

        rendered = self._template_text
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)

        return yaml.safe_load(rendered)

    # =========================================================================
    # Public API
    # =========================================================================

    async def create_vm(
        self,
        job_id: str,
        agent_config: str = "defaults",
        vm_image: Optional[str] = None,
        cpu_cores: int = 2,
        memory: str = "4Gi",
        description: str = "",
    ) -> bool:
        """Create a VM for a job.

        Picks the highest-priority transport that is available
        (NATS > HTTP > direct > docker).

        Returns:
            True if the request was accepted, False otherwise.
        """
        if nats_bridge.is_available:
            return await nats_bridge.request_vm_create(
                job_id=job_id,
                agent_config=agent_config,
                vm_image=vm_image,
                cpu_cores=cpu_cores,
                memory=memory,
                description=description,
            )

        if self._http_available:
            return await self._create_http(
                job_id=job_id,
                agent_config=agent_config,
                vm_image=vm_image,
                cpu_cores=cpu_cores,
                memory=memory,
                description=description,
                entity_type="job",
            )

        if self._k8s_available:
            return await self._create_direct(
                job_id=job_id,
                agent_config=agent_config,
                vm_image=vm_image,
                cpu_cores=cpu_cores,
                memory=memory,
                description=description,
            )

        # Docker Compose mode: assign from QEMU-in-Docker pool
        if self._docker_available:
            from .docker_provisioner import docker_provisioner

            result = await docker_provisioner.assign_vm(job_id)
            return result is not None

        return False

    async def delete_vm(self, job_id: str) -> bool:
        """Delete a VM for a job.

        Returns:
            True if the request was accepted, False otherwise.
        """
        if nats_bridge.is_available:
            return await nats_bridge.request_vm_delete(job_id)

        if self._http_available:
            return await self._delete_http(job_id, entity_type="job")

        if self._k8s_available:
            return await self._delete_direct(job_id)

        return False

    async def release_vm(
        self,
        job_id: str,
        ssh_host: Optional[str] = None,
        ssh_port: Optional[int] = None,
    ) -> bool:
        """Snapshot a job VM to S3, then delete it.

        Args:
            job_id: Job UUID.
            ssh_host: SSH host for snapshot (read from DB context if omitted).
            ssh_port: SSH port for snapshot (read from DB context if omitted).

        Returns:
            True if deletion succeeded (snapshot failure is non-fatal).
        """
        # Resolve SSH coordinates from DB if not provided
        if not ssh_host and self._db:
            try:
                job = await self._db.get_job(job_id)
                if job:
                    vm_ctx = _extract_vm_context(job)
                    ssh_host = ssh_host or vm_ctx.get("ssh_host")
                    ssh_port = ssh_port or vm_ctx.get("ssh_port")
            except Exception:
                logger.debug("Could not read VM context for job %s", job_id)

        # Snapshot before delete (best-effort)
        if (
            self._snapshot_service
            and self._snapshot_service.is_available
            and ssh_host
            and ssh_port
        ):
            try:
                await self._snapshot_service.capture_vm_snapshot(
                    job_id=job_id,
                    ssh_host=ssh_host,
                    ssh_port=int(ssh_port),
                    source_type="vm",
                )
                logger.info("VM snapshot captured for job %s before release", job_id)
            except Exception:
                logger.exception(
                    "VM snapshot failed for job %s — deleting anyway", job_id
                )

        return await self.delete_vm(job_id)

    async def release_thread_vm(
        self,
        thread_id: str,
        ssh_host: Optional[str] = None,
        ssh_port: Optional[int] = None,
    ) -> bool:
        """Snapshot a thread VM to S3, then delete it.

        Args:
            thread_id: Thread UUID.
            ssh_host: SSH host for snapshot (read from DB if omitted).
            ssh_port: SSH port for snapshot (read from DB if omitted).

        Returns:
            True if deletion succeeded (snapshot failure is non-fatal).
        """
        # Resolve SSH coordinates from DB if not provided
        if not ssh_host and self._db:
            try:
                thread = await self._db.get_thread(thread_id)
                if thread:
                    metadata = thread.get("metadata") or {}
                    if isinstance(metadata, str):
                        import json

                        metadata = json.loads(metadata)
                    vm_ctx = metadata.get("vm") or {}
                    ssh_host = ssh_host or vm_ctx.get("ssh_host")
                    ssh_port = ssh_port or vm_ctx.get("ssh_port")
            except Exception:
                logger.debug("Could not read VM context for thread %s", thread_id)

        # Snapshot before delete (best-effort)
        if (
            self._snapshot_service
            and self._snapshot_service.is_available
            and ssh_host
            and ssh_port
        ):
            try:
                await self._snapshot_service.capture_vm_snapshot(
                    job_id=thread_id,
                    ssh_host=ssh_host,
                    ssh_port=int(ssh_port),
                    source_type="vm",
                    entity_type="threads",
                )
                logger.info(
                    "VM snapshot captured for thread %s before release", thread_id
                )
            except Exception:
                logger.exception(
                    "VM snapshot failed for thread %s — deleting anyway", thread_id
                )

        return await self.delete_thread_vm(thread_id)

    async def query_status(self, job_id: str, timeout: float = 5.0) -> Optional[dict]:
        """Query live VM status.

        NATS mode: request/reply to VM controller.
        HTTP mode: GET /vms/{job_id} on the controller.
        Direct mode: query KubeVirt API.

        Returns:
            Status dict or None if unavailable.
        """
        if nats_bridge.is_available:
            return await nats_bridge.query_vm_status(job_id, timeout)

        if self._http_available:
            return await self._query_http(job_id, timeout)

        if self._k8s_available:
            return await self._query_direct(job_id)

        return None

    async def send_control(self, job_id: str, action: str) -> bool:
        """Send a control command to the management daemon in a VM.

        Only works via NATS (the daemon listens on NATS subjects).
        Returns False if NATS is unavailable.

        Args:
            job_id: Job UUID.
            action: One of "freeze", "resume", "terminate".
        """
        if nats_bridge.is_available:
            return await nats_bridge.send_control(job_id, action)
        return False

    # =========================================================================
    # Direct K8s backend (same-cluster)
    # =========================================================================

    async def _create_direct(
        self,
        job_id: str,
        agent_config: str,
        vm_image: Optional[str],
        cpu_cores: int,
        memory: str,
        description: str,
    ) -> bool:
        """Create a VM via direct Kubernetes API call."""
        job_config = {
            "job_id": job_id,
            "agent_config": agent_config,
            "vm_image": vm_image,
            "cpu_cores": cpu_cores,
            "memory": memory,
            "description": description,
            "nats_url": os.environ.get("NATS_URL", ""),
        }

        try:
            manifest = self._render_template(job_config)
            vm_name = manifest["metadata"]["name"]

            # K8s client is synchronous — run in thread pool
            await asyncio.to_thread(
                self._k8s.create_namespaced_custom_object,
                group=KUBEVIRT_GROUP,
                version=KUBEVIRT_VERSION,
                namespace=self._vm_namespace,
                plural=KUBEVIRT_PLURAL,
                body=manifest,
            )

            logger.info("VM created (direct): %s (job %s)", vm_name, job_id)
            await self._set_vm_context(
                job_id,
                {
                    "status": "created",
                    "vm_name": vm_name,
                    "namespace": self._vm_namespace,
                    "provisioned_by": "direct",
                },
            )
            return True
        except Exception as e:
            logger.error("Failed to create VM for job %s: %s", job_id, e)
            await self._set_vm_context(
                job_id,
                {
                    "status": "failed",
                    "error": str(e),
                    "provisioned_by": "direct",
                },
            )
            return False

    async def _delete_direct(self, job_id: str) -> bool:
        """Delete a VM via direct Kubernetes API call."""
        vm_name = f"agent-vm-{job_id}"

        try:
            await asyncio.to_thread(
                self._k8s.delete_namespaced_custom_object,
                group=KUBEVIRT_GROUP,
                version=KUBEVIRT_VERSION,
                namespace=self._vm_namespace,
                plural=KUBEVIRT_PLURAL,
                name=vm_name,
            )

            logger.info("VM deleted (direct): %s (job %s)", vm_name, job_id)
            await self._set_vm_context(job_id, {"status": "deleted"})
            return True
        except Exception as e:
            logger.error("Failed to delete VM for job %s: %s", job_id, e)
            return False

    async def _query_direct(self, job_id: str) -> Optional[dict]:
        """Query VM status via direct Kubernetes API call."""
        vm_name = f"agent-vm-{job_id}"

        try:
            vm = await asyncio.to_thread(
                self._k8s.get_namespaced_custom_object,
                group=KUBEVIRT_GROUP,
                version=KUBEVIRT_VERSION,
                namespace=self._vm_namespace,
                plural=KUBEVIRT_PLURAL,
                name=vm_name,
            )

            status = vm.get("status", {})
            conditions = status.get("conditions", [])
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in conditions
            )

            return {
                "job_id": job_id,
                "vm_name": vm_name,
                "ready": ready,
                "phase": status.get("printableStatus", "Unknown"),
                "created": status.get("created", False),
            }
        except Exception as e:
            logger.debug("VM status query failed for job %s: %s", job_id, e)
            return None

    # =========================================================================
    # HTTP controller backend (same-cluster, controller-mediated)
    #
    # The controller still owns KubeVirt RBAC and the VM template; this
    # transport just swaps NATS for HTTP. Status updates that NATS pushed
    # asynchronously now arrive synchronously in the response body, so
    # context updates happen here in the orchestrator instead of via the
    # vm.lifecycle.status subscription.
    #
    # Caveat: in-VM daemon events (register/heartbeat/freeze/resume) are
    # NOT carried by this transport. Same-cluster deployments that need
    # those still need NATS (or a future HTTP webhook from the daemon).
    # =========================================================================

    async def _create_http(
        self,
        job_id: str,
        agent_config: str,
        vm_image: Optional[str],
        cpu_cores: int,
        memory: str,
        description: str,
        entity_type: str = "job",
    ) -> bool:
        """Create a VM by POSTing to the co-located VM controller."""
        if self._http_client is None:
            return False

        payload: dict[str, Any] = {
            "job_id": job_id,
            "agent_config": agent_config,
            "cpu_cores": cpu_cores,
            "memory": memory,
            "description": description,
            "nats_url": "",  # No NATS in same-cluster mode
        }
        if vm_image:
            payload["vm_image"] = vm_image

        try:
            await self._set_context(entity_type, job_id, {"status": "provisioning"})
            resp = await self._http_client.post("/vms", json=payload)
            resp.raise_for_status()
            data = resp.json()

            await self._set_context(
                entity_type,
                job_id,
                {
                    "status": data.get("status", "created"),
                    "vm_name": data.get("vm_name"),
                    "namespace": data.get("namespace"),
                    "provisioned_by": "http",
                },
            )
            logger.info(
                "VM created (http): %s (%s %s)",
                data.get("vm_name"),
                entity_type,
                job_id,
            )
            return True
        except httpx.HTTPStatusError as e:
            error = _extract_http_error(e.response)
            logger.error(
                "VM controller rejected create for %s %s: %s",
                entity_type,
                job_id,
                error,
            )
            await self._set_context(
                entity_type,
                job_id,
                {"status": "failed", "error": error, "provisioned_by": "http"},
            )
            return False
        except Exception as e:
            logger.error("HTTP create failed for %s %s: %s", entity_type, job_id, e)
            await self._set_context(
                entity_type,
                job_id,
                {"status": "failed", "error": str(e), "provisioned_by": "http"},
            )
            return False

    async def _delete_http(self, job_id: str, entity_type: str = "job") -> bool:
        """Delete a VM by sending DELETE to the co-located VM controller."""
        if self._http_client is None:
            return False

        try:
            resp = await self._http_client.delete(f"/vms/{job_id}")
            # 404 from the controller means already gone — treat as success.
            if resp.status_code == 404:
                await self._set_context(entity_type, job_id, {"status": "deleted"})
                return True
            resp.raise_for_status()
            await self._set_context(entity_type, job_id, {"status": "deleted"})
            logger.info("VM deleted (http): %s %s", entity_type, job_id)
            return True
        except httpx.HTTPStatusError as e:
            error = _extract_http_error(e.response)
            logger.error(
                "VM controller rejected delete for %s %s: %s",
                entity_type,
                job_id,
                error,
            )
            return False
        except Exception as e:
            logger.error("HTTP delete failed for %s %s: %s", entity_type, job_id, e)
            return False

    async def _query_http(self, job_id: str, timeout: float = 5.0) -> Optional[dict]:
        """Query VM status via the co-located VM controller."""
        if self._http_client is None:
            return None

        try:
            resp = await self._http_client.get(f"/vms/{job_id}", timeout=timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.debug("HTTP status query failed for job %s: %s", job_id, e)
            return None

    async def _set_context(
        self, entity_type: str, entity_id: str, updates: dict
    ) -> None:
        """Route context updates to the right table based on entity type."""
        if entity_type == "thread":
            await self._set_thread_vm_context(entity_id, updates)
        else:
            await self._set_vm_context(entity_id, updates)

    # =========================================================================
    # Helpers
    # =========================================================================

    async def _set_vm_context(self, job_id: str, updates: dict) -> None:
        """Atomically merge updates into the job's context.vm key."""
        if not self._db:
            return

        try:
            await self._db.merge_vm_context(job_id, updates)
        except Exception:
            logger.exception("Failed to update VM context for job %s", job_id)

    # =========================================================================
    # Thread VM support (persistent agent sessions)
    # =========================================================================

    async def create_thread_vm(
        self,
        thread_id: str,
        agent_config: str = "defaults",
        vm_image: Optional[str] = None,
        cpu_cores: int = 2,
        memory: str = "4Gi",
        description: str = "",
    ) -> bool:
        """Create a VM for a persistent thread.

        Mirrors create_vm() but routes context to threads.metadata.vm
        and uses entity_type="thread" for NATS bridge routing.

        Returns:
            True if the request was accepted, False otherwise.
        """
        if nats_bridge.is_available:
            return await nats_bridge.request_vm_create(
                job_id=thread_id,
                agent_config=agent_config,
                vm_image=vm_image,
                cpu_cores=cpu_cores,
                memory=memory,
                description=description,
                entity_type="thread",
            )

        if self._http_available:
            return await self._create_http(
                job_id=thread_id,
                agent_config=agent_config,
                vm_image=vm_image,
                cpu_cores=cpu_cores,
                memory=memory,
                description=description,
                entity_type="thread",
            )

        if self._k8s_available:
            return await self._create_thread_vm_direct(
                thread_id=thread_id,
                agent_config=agent_config,
                vm_image=vm_image,
                cpu_cores=cpu_cores,
                memory=memory,
                description=description,
            )

        return False

    async def delete_thread_vm(self, thread_id: str) -> bool:
        """Delete a VM for a persistent thread.

        Returns:
            True if the request was accepted, False otherwise.
        """
        if nats_bridge.is_available:
            return await nats_bridge.request_vm_delete(thread_id)

        if self._http_available:
            return await self._delete_http(thread_id, entity_type="thread")

        if self._k8s_available:
            return await self._delete_thread_vm_direct(thread_id)

        return False

    async def _create_thread_vm_direct(
        self,
        thread_id: str,
        agent_config: str,
        vm_image: Optional[str],
        cpu_cores: int,
        memory: str,
        description: str,
    ) -> bool:
        """Create a thread VM via direct Kubernetes API call."""
        job_config = {
            "job_id": thread_id,  # Template uses ${JOB_ID}
            "agent_config": agent_config,
            "vm_image": vm_image,
            "cpu_cores": cpu_cores,
            "memory": memory,
            "description": description,
            "nats_url": os.environ.get("NATS_URL", ""),
        }

        try:
            manifest = self._render_template(job_config)
            vm_name = manifest["metadata"]["name"]

            await asyncio.to_thread(
                self._k8s.create_namespaced_custom_object,
                group=KUBEVIRT_GROUP,
                version=KUBEVIRT_VERSION,
                namespace=self._vm_namespace,
                plural=KUBEVIRT_PLURAL,
                body=manifest,
            )

            logger.info(
                "Thread VM created (direct): %s (thread %s)", vm_name, thread_id
            )
            await self._set_thread_vm_context(
                thread_id,
                {
                    "status": "created",
                    "vm_name": vm_name,
                    "namespace": self._vm_namespace,
                    "provisioned_by": "direct",
                },
            )
            return True
        except Exception as e:
            logger.error("Failed to create thread VM for %s: %s", thread_id, e)
            await self._set_thread_vm_context(
                thread_id,
                {
                    "status": "failed",
                    "error": str(e),
                    "provisioned_by": "direct",
                },
            )
            return False

    async def _delete_thread_vm_direct(self, thread_id: str) -> bool:
        """Delete a thread VM via direct Kubernetes API call."""
        vm_name = f"agent-vm-{thread_id}"

        try:
            await asyncio.to_thread(
                self._k8s.delete_namespaced_custom_object,
                group=KUBEVIRT_GROUP,
                version=KUBEVIRT_VERSION,
                namespace=self._vm_namespace,
                plural=KUBEVIRT_PLURAL,
                name=vm_name,
            )

            logger.info(
                "Thread VM deleted (direct): %s (thread %s)", vm_name, thread_id
            )
            await self._set_thread_vm_context(thread_id, {"status": "deleted"})
            return True
        except Exception as e:
            if hasattr(e, "status") and e.status == 404:
                logger.debug(
                    "Thread VM already deleted: %s (thread %s)",
                    vm_name,
                    thread_id,
                )
                return True
            logger.error("Failed to delete thread VM for %s: %s", thread_id, e)
            return False

    async def _set_thread_vm_context(self, thread_id: str, updates: dict) -> None:
        """Atomically merge updates into thread's metadata.vm key."""
        if not self._db:
            return

        try:
            await self._db.merge_thread_vm_context(thread_id, updates)
        except Exception:
            logger.exception("Failed to update thread VM context for %s", thread_id)


def _extract_http_error(response: httpx.Response) -> str:
    """Pull a useful error message out of a controller HTTP error response."""
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    except Exception:
        pass
    text = (response.text or "").strip()
    return text or f"HTTP {response.status_code}"


# Module-level singleton
vm_provisioner = VMProvisioner()
