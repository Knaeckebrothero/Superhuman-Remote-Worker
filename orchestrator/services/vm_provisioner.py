"""VM Provisioner — Unified VM lifecycle management.

Two backends, auto-selected based on available infrastructure:

1. **NATS bridge** (cross-cluster): Publishes to NATS, VM Controller on remote
   agent cluster handles KubeVirt API calls. Used when NATS_URL is configured.

2. **Direct K8s API** (same-cluster): Orchestrator calls KubeVirt API directly.
   Used when both orchestrator and VMs run on the same cluster.

Selection logic:
  - NATS_URL set and nats-py installed → NATS mode
  - No NATS, but kubernetes client available + VM template found → direct mode
  - Neither → VM features disabled

The VM endpoints and lifecycle hooks in main.py use this provisioner exclusively.
"""

import asyncio
import logging
import os
from typing import Any, Optional

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


class VMProvisioner:
    """Unified VM provisioner with automatic backend selection.

    Uses NATS bridge when available (cross-cluster), falls back to direct
    Kubernetes API calls (same-cluster). Control commands (freeze/resume/
    terminate) require NATS since they target the management daemon inside
    the VM.
    """

    def __init__(self):
        self._db: Optional[Any] = None
        self._k8s: Optional[Any] = None
        self._k8s_available: bool = False
        self._template_text: str = ""
        self._vm_namespace: str = os.environ.get("VM_NAMESPACE", "agent-vms")
        self._default_vm_image: str = os.environ.get(
            "DEFAULT_VM_IMAGE",
            "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent-vm-base:latest",
        )

    @property
    def is_available(self) -> bool:
        """True if any VM provisioning backend is available."""
        return nats_bridge.is_available or self._k8s_available

    @property
    def mode(self) -> Optional[str]:
        """Current provisioning mode: 'nats', 'direct', or None."""
        if nats_bridge.is_available:
            return "nats"
        if self._k8s_available:
            return "direct"
        return None

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def connect(self, db: Any) -> None:
        """Initialize the provisioner.

        Args:
            db: PostgresDB instance for job context updates.
        """
        self._db = db
        self._init_k8s()

        if self._k8s_available:
            logger.info(
                "VM provisioner ready: direct K8s mode (namespace=%s)",
                self._vm_namespace,
            )
        elif nats_bridge.is_available:
            logger.info("VM provisioner ready: NATS mode")
        else:
            logger.info(
                "VM provisioner: no backend available (NATS and K8s both unavailable)"
            )

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

        Uses NATS (cross-cluster) or direct K8s API (same-cluster).

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

        if self._k8s_available:
            return await self._create_direct(
                job_id=job_id,
                agent_config=agent_config,
                vm_image=vm_image,
                cpu_cores=cpu_cores,
                memory=memory,
                description=description,
            )

        return False

    async def delete_vm(self, job_id: str) -> bool:
        """Delete a VM for a job.

        Returns:
            True if the request was accepted, False otherwise.
        """
        if nats_bridge.is_available:
            return await nats_bridge.request_vm_delete(job_id)

        if self._k8s_available:
            return await self._delete_direct(job_id)

        return False

    async def query_status(self, job_id: str, timeout: float = 5.0) -> Optional[dict]:
        """Query live VM status.

        NATS mode: request/reply to VM controller.
        Direct mode: query KubeVirt API.

        Returns:
            Status dict or None if unavailable.
        """
        if nats_bridge.is_available:
            return await nats_bridge.query_vm_status(job_id, timeout)

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
            logger.error(
                "Failed to create thread VM for %s: %s", thread_id, e
            )
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
            logger.error(
                "Failed to delete thread VM for %s: %s", thread_id, e
            )
            return False

    async def _set_thread_vm_context(self, thread_id: str, updates: dict) -> None:
        """Atomically merge updates into thread's metadata.vm key."""
        if not self._db:
            return

        try:
            await self._db.merge_thread_vm_context(thread_id, updates)
        except Exception:
            logger.exception(
                "Failed to update thread VM context for %s", thread_id
            )


# Module-level singleton
vm_provisioner = VMProvisioner()
