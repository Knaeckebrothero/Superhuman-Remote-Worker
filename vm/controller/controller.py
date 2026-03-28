"""
VM Controller — KubeVirt VM Lifecycle Manager

Runs on the agent cluster. Subscribes to NATS subjects for VM lifecycle
management and creates/deletes KubeVirt VirtualMachine resources via the
Kubernetes API.

Subjects:
  vm.lifecycle.create   Orchestrator requests a new VM for a job
  vm.lifecycle.delete   Orchestrator requests VM teardown
  vm.lifecycle.status   Controller publishes creation/deletion results

SSH connectivity uses a Headscale mesh VPN (self-hosted Tailscale). The
controller generates short-lived auth keys via the Headscale API and injects
them into cloud-init so VMs join the tailnet on boot. Agent pods run a
Tailscale sidecar and route directly to VMs via 100.64.x.y addresses.

See docs/features/headscale_mesh.md for the mesh VPN design.
See docs/features/vm_backend.md (Phase 3) and docs/features/nats.md.
"""

import asyncio
import json
import logging
import os
import signal
import sys

import yaml

from headscale_client import HeadscaleClient

_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
if _log_level == "DEBUG" and not os.environ.get("DEBUG_ALL"):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("vm-controller").setLevel(logging.DEBUG)
else:
    logging.basicConfig(
        level=_log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
log = logging.getLogger("vm-controller")

# Configuration from environment
NATS_URL = os.environ.get("NATS_URL", "nats://nats-leaf.nats.svc.cluster.local:4222")
VM_TEMPLATE_PATH = os.environ.get("VM_TEMPLATE_PATH", "/config/vm-template.yaml")
VM_NAMESPACE = os.environ.get("VM_NAMESPACE", "agent-vms")
DEFAULT_VM_IMAGE = os.environ.get(
    "DEFAULT_VM_IMAGE",
    "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent-vm-base:latest",
)
DEFAULT_CPU = int(os.environ.get("DEFAULT_CPU", "2"))
DEFAULT_MEMORY = os.environ.get("DEFAULT_MEMORY", "4Gi")

# KubeVirt API coordinates
KUBEVIRT_GROUP = "kubevirt.io"
KUBEVIRT_VERSION = "v1"
KUBEVIRT_PLURAL = "virtualmachines"


class VMController:
    """Manages KubeVirt VM lifecycle via NATS commands."""

    def __init__(self):
        self.nc = None  # NATS client
        self.k8s_client = None  # kubernetes CustomObjectsApi
        self.template_text: str = ""  # Raw YAML template (for string substitution)
        self.headscale = HeadscaleClient()
        self._shutdown = asyncio.Event()

    def load_template(self):
        """Load the VM template as raw text for placeholder substitution."""
        path = VM_TEMPLATE_PATH
        if not os.path.exists(path):
            log.error("VM template not found at %s", path)
            sys.exit(1)

        with open(path) as f:
            self.template_text = f.read()

        log.info("Loaded VM template from %s", path)

    def render_template(self, job_config: dict, tailscale_auth_key: str = "") -> dict:
        """Render the VM template with job-specific values.

        Performs string substitution on the raw YAML text, then parses
        the result. This handles placeholders in both string and numeric
        contexts (e.g., cores: ${CPU_CORES} becomes cores: 2).

        Args:
            job_config: Dict with keys: job_id, agent_config, vm_image,
                        cpu_cores, memory, nats_url, description.
            tailscale_auth_key: Headscale pre-auth key for the VM to join
                                the tailnet. Empty string if Headscale unavailable.

        Returns:
            Parsed YAML dict ready for the Kubernetes API.
        """
        headscale_url = os.environ.get("HEADSCALE_URL", "")

        replacements = {
            "${JOB_ID}": job_config["job_id"],
            "${AGENT_CONFIG}": job_config.get("agent_config", "defaults"),
            "${VM_IMAGE}": job_config.get("vm_image", DEFAULT_VM_IMAGE),
            "${CPU_CORES}": str(job_config.get("cpu_cores", DEFAULT_CPU)),
            "${MEMORY}": job_config.get("memory", DEFAULT_MEMORY),
            # Always use the local leaf node URL — the VM runs on this cluster,
            # not the orchestrator's cluster where the job's nats_url points.
            "${NATS_URL}": NATS_URL,
            "${DESCRIPTION}": job_config.get("description", ""),
            # Headscale mesh VPN — VM joins tailnet on boot
            "${TAILSCALE_AUTH_KEY}": tailscale_auth_key,
            "${HEADSCALE_URL}": headscale_url,
        }

        rendered = self.template_text
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)

        return yaml.safe_load(rendered)

    def init_k8s(self):
        """Initialize the Kubernetes client using in-cluster config."""
        from kubernetes import client, config

        config.load_incluster_config()
        self.k8s_client = client.CustomObjectsApi()
        log.info("Kubernetes client initialized (in-cluster)")

    async def connect_nats(self):
        """Connect to the NATS leaf node on the agent cluster."""
        import nats

        async def error_handler(e):
            log.error("NATS error: %s", e)

        async def disconnected_handler():
            log.warning("NATS disconnected")

        async def reconnected_handler():
            log.info("NATS reconnected")

        self.nc = await nats.connect(
            NATS_URL,
            error_cb=error_handler,
            disconnected_cb=disconnected_handler,
            reconnected_cb=reconnected_handler,
            max_reconnect_attempts=-1,  # Reconnect indefinitely
            reconnect_time_wait=2,
        )
        log.info("Connected to NATS at %s", NATS_URL)

    async def handle_create(self, msg):
        """Handle vm.lifecycle.create — create a KubeVirt VirtualMachine.

        Expected payload:
        {
            "job_id": "uuid",
            "agent_config": "developer",
            "vm_image": "ghcr.io/.../agent-vm-base:latest",
            "cpu_cores": 2,
            "memory": "4Gi",
            "nats_url": "nats://...",
            "description": "Task description"
        }
        """
        from kubernetes.client.exceptions import ApiException

        try:
            job_config = json.loads(msg.data.decode())
            job_id = job_config.get("job_id", "unknown")
            log.info("Creating VM for job %s", job_id)

            # Generate a Headscale pre-auth key so the VM joins the tailnet
            tailscale_auth_key = ""
            if self.headscale.is_available:
                tailscale_auth_key = await self.headscale.create_auth_key(job_id) or ""
                if not tailscale_auth_key:
                    log.warning(
                        "Failed to get Headscale auth key for job %s — "
                        "VM will boot without mesh VPN",
                        job_id,
                    )

            manifest = self.render_template(job_config, tailscale_auth_key)
            vm_name = manifest["metadata"]["name"]

            # Retry loop: if the old VM is still being deleted (409 Conflict),
            # wait for the finalizer to clear before creating the new one.
            max_retries = 12  # ~60s total
            for attempt in range(max_retries + 1):
                try:
                    self.k8s_client.create_namespaced_custom_object(
                        group=KUBEVIRT_GROUP,
                        version=KUBEVIRT_VERSION,
                        namespace=VM_NAMESPACE,
                        plural=KUBEVIRT_PLURAL,
                        body=manifest,
                    )
                    break  # Success
                except ApiException as e:
                    if e.status == 409 and "is being deleted" in (e.body or ""):
                        if attempt < max_retries:
                            log.info(
                                "VM %s still being deleted, waiting... (attempt %d/%d)",
                                vm_name,
                                attempt + 1,
                                max_retries,
                            )
                            await asyncio.sleep(5)
                            continue
                        log.error(
                            "VM %s still being deleted after %d retries, giving up",
                            vm_name,
                            max_retries,
                        )
                    raise

            log.info("VM created: %s (job %s)", vm_name, job_id)

            await self._publish_status(
                job_id,
                {
                    "job_id": job_id,
                    "status": "created",
                    "vm_name": vm_name,
                    "namespace": VM_NAMESPACE,
                },
            )

        except Exception as e:
            job_id = "unknown"
            try:
                job_id = json.loads(msg.data.decode()).get("job_id", "unknown")
            except Exception:
                pass

            log.exception("Failed to create VM for job %s", job_id)
            await self._publish_status(
                job_id,
                {
                    "job_id": job_id,
                    "status": "failed",
                    "error": str(e),
                },
            )

    async def handle_delete(self, msg):
        """Handle vm.lifecycle.delete — delete a KubeVirt VirtualMachine.

        Expected payload:
        {
            "job_id": "uuid"
        }
        """
        from kubernetes.client.exceptions import ApiException

        try:
            data = json.loads(msg.data.decode())
            job_id = data["job_id"]
            vm_name = f"agent-vm-{job_id}"
            log.info("Deleting VM %s (job %s)", vm_name, job_id)

            try:
                self.k8s_client.delete_namespaced_custom_object(
                    group=KUBEVIRT_GROUP,
                    version=KUBEVIRT_VERSION,
                    namespace=VM_NAMESPACE,
                    plural=KUBEVIRT_PLURAL,
                    name=vm_name,
                )
            except ApiException as e:
                if e.status == 404:
                    log.info("VM %s already gone (404), treating as deleted", vm_name)
                else:
                    raise

            # Remove the VM's node from Headscale (ephemeral nodes auto-expire,
            # but explicit cleanup is faster and avoids stale entries)
            if self.headscale.is_available:
                await self.headscale.delete_node(job_id)

            log.info("VM deleted: %s (job %s)", vm_name, job_id)

            await self._publish_status(
                job_id,
                {
                    "job_id": job_id,
                    "status": "deleted",
                    "vm_name": vm_name,
                },
            )

        except Exception as e:
            job_id = "unknown"
            try:
                job_id = json.loads(msg.data.decode()).get("job_id", "unknown")
            except Exception:
                pass

            log.exception("Failed to delete VM for job %s", job_id)
            await self._publish_status(
                job_id,
                {
                    "job_id": job_id,
                    "status": "delete_failed",
                    "error": str(e),
                },
            )

    async def handle_status_query(self, msg):
        """Handle vm.lifecycle.get — query the status of a VM.

        Expected payload:
        {
            "job_id": "uuid"
        }

        Responds with the VirtualMachine's status from the Kubernetes API.
        """
        try:
            data = json.loads(msg.data.decode())
            job_id = data["job_id"]
            vm_name = f"agent-vm-{job_id}"

            vm = self.k8s_client.get_namespaced_custom_object(
                group=KUBEVIRT_GROUP,
                version=KUBEVIRT_VERSION,
                namespace=VM_NAMESPACE,
                plural=KUBEVIRT_PLURAL,
                name=vm_name,
            )

            # Extract status fields
            status = vm.get("status", {})
            conditions = status.get("conditions", [])
            ready = any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in conditions
            )

            response = {
                "job_id": job_id,
                "vm_name": vm_name,
                "ready": ready,
                "phase": status.get("printableStatus", "Unknown"),
                "created": status.get("created", False),
            }

            # Use request-reply if available
            if msg.reply:
                await self.nc.publish(msg.reply, json.dumps(response).encode())
            else:
                await self._publish_status(job_id, response)

        except Exception as e:
            job_id = "unknown"
            try:
                job_id = json.loads(msg.data.decode()).get("job_id", "unknown")
            except Exception:
                pass

            error_response = {
                "job_id": job_id,
                "status": "query_failed",
                "error": str(e),
            }
            if msg.reply:
                await self.nc.publish(msg.reply, json.dumps(error_response).encode())
            else:
                await self._publish_status(job_id, error_response)

    async def _publish_status(self, job_id: str, payload: dict):
        """Publish a status message on vm.lifecycle.status."""
        try:
            await self.nc.publish(
                "vm.lifecycle.status",
                json.dumps(payload).encode(),
            )
        except Exception:
            log.exception("Failed to publish status for job %s", job_id)

    async def run(self):
        """Main entry point — connect, subscribe, wait for shutdown."""
        log.info("VM Controller starting")

        self.load_template()
        self.init_k8s()
        await self.headscale.init()
        await self.connect_nats()

        # Subscribe to lifecycle subjects
        await self.nc.subscribe("vm.lifecycle.create", cb=self.handle_create)
        await self.nc.subscribe("vm.lifecycle.delete", cb=self.handle_delete)
        await self.nc.subscribe("vm.lifecycle.get", cb=self.handle_status_query)
        log.info(
            "Subscribed to vm.lifecycle.{create,delete,get} — waiting for requests"
        )

        # Wait for shutdown signal
        await self._shutdown.wait()

        # Drain and disconnect
        log.info("Shutting down...")
        if self.nc and self.nc.is_connected:
            await self.nc.drain()
        await self.headscale.close()

        log.info("VM Controller stopped")

    def request_shutdown(self):
        """Signal the controller to shut down gracefully."""
        self._shutdown.set()


def main():
    controller = VMController()

    def signal_handler(sig, _frame):
        log.info("Received signal %d, requesting shutdown", sig)
        controller.request_shutdown()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    asyncio.run(controller.run())


if __name__ == "__main__":
    main()
