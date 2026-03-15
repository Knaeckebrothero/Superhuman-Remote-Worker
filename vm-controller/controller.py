"""
VM Controller — KubeVirt VM Lifecycle Manager

Runs on the agent cluster. Subscribes to NATS subjects for VM lifecycle
management and creates/deletes KubeVirt VirtualMachine resources via the
Kubernetes API.

Subjects:
  vm.lifecycle.create   Orchestrator requests a new VM for a job
  vm.lifecycle.delete   Orchestrator requests VM teardown
  vm.lifecycle.status   Controller publishes creation/deletion results

See docs/features/vm_backend.md (Phase 3) and docs/features/nats.md.
"""

import asyncio
import json
import logging
import os
import signal
import sys

import yaml

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("vm-controller")

# Configuration from environment
NATS_URL = os.environ.get("NATS_URL", "nats://nats-leaf.nats.svc.cluster.local:4222")
VM_TEMPLATE_PATH = os.environ.get("VM_TEMPLATE_PATH", "/config/vm-template.yaml")
VM_NAMESPACE = os.environ.get("VM_NAMESPACE", "agent-vms")
DEFAULT_VM_IMAGE = os.environ.get(
    "DEFAULT_VM_IMAGE", "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent-vm-base:latest"
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

    def render_template(self, job_config: dict) -> dict:
        """Render the VM template with job-specific values.

        Performs string substitution on the raw YAML text, then parses
        the result. This handles placeholders in both string and numeric
        contexts (e.g., cores: ${CPU_CORES} becomes cores: 2).

        Args:
            job_config: Dict with keys: job_id, agent_config, vm_image,
                        cpu_cores, memory, nats_url, description.

        Returns:
            Parsed YAML dict ready for the Kubernetes API.
        """
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
        self.core_v1 = client.CoreV1Api()
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
        try:
            job_config = json.loads(msg.data.decode())
            job_id = job_config.get("job_id", "unknown")
            log.info("Creating VM for job %s", job_id)

            manifest = self.render_template(job_config)
            vm_name = manifest["metadata"]["name"]

            self.k8s_client.create_namespaced_custom_object(
                group=KUBEVIRT_GROUP,
                version=KUBEVIRT_VERSION,
                namespace=VM_NAMESPACE,
                plural=KUBEVIRT_PLURAL,
                body=manifest,
            )

            log.info("VM created: %s (job %s)", vm_name, job_id)

            # Create a NodePort Service to expose SSH (port 22) for cross-cluster access
            ssh_nodeport = self._create_ssh_service(vm_name, job_id)

            await self._publish_status(job_id, {
                "job_id": job_id,
                "status": "created",
                "vm_name": vm_name,
                "namespace": VM_NAMESPACE,
                "ssh_nodeport": ssh_nodeport,
            })

        except Exception as e:
            job_id = "unknown"
            try:
                job_id = json.loads(msg.data.decode()).get("job_id", "unknown")
            except Exception:
                pass

            log.exception("Failed to create VM for job %s", job_id)
            await self._publish_status(job_id, {
                "job_id": job_id,
                "status": "failed",
                "error": str(e),
            })

    async def handle_delete(self, msg):
        """Handle vm.lifecycle.delete — delete a KubeVirt VirtualMachine.

        Expected payload:
        {
            "job_id": "uuid"
        }
        """
        try:
            data = json.loads(msg.data.decode())
            job_id = data["job_id"]
            vm_name = f"agent-vm-{job_id}"
            log.info("Deleting VM %s (job %s)", vm_name, job_id)

            self.k8s_client.delete_namespaced_custom_object(
                group=KUBEVIRT_GROUP,
                version=KUBEVIRT_VERSION,
                namespace=VM_NAMESPACE,
                plural=KUBEVIRT_PLURAL,
                name=vm_name,
            )

            # Clean up the SSH NodePort Service
            self._delete_ssh_service(vm_name, job_id)

            log.info("VM deleted: %s (job %s)", vm_name, job_id)

            await self._publish_status(job_id, {
                "job_id": job_id,
                "status": "deleted",
                "vm_name": vm_name,
            })

        except Exception as e:
            job_id = "unknown"
            try:
                job_id = json.loads(msg.data.decode()).get("job_id", "unknown")
            except Exception:
                pass

            log.exception("Failed to delete VM for job %s", job_id)
            await self._publish_status(job_id, {
                "job_id": job_id,
                "status": "delete_failed",
                "error": str(e),
            })

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

    async def handle_pod_ip_query(self, msg):
        """Handle vm.query.pod-ip — look up the SSH endpoint for a VM.

        Returns the node IP + NodePort of the SSH service, which is
        routable from any cluster on the same LAN (cross-cluster access).
        Falls back to pod IP for same-cluster access.

        Expected payload: {"job_id": "uuid"}
        Response: {"job_id": "...", "ssh_host": "10.0.50.x", "ssh_port": 3xxxx}
                  or {"job_id": "...", "pod_ip": "10.42.x.x"} (fallback)
        """
        try:
            data = json.loads(msg.data.decode())
            job_id = data["job_id"]
            vm_name = f"agent-vm-{job_id}"
            svc_name = f"ssh-{vm_name}"

            # Try NodePort service first (cross-cluster)
            ssh_host = None
            ssh_port = None
            try:
                svc = self.core_v1.read_namespaced_service(svc_name, VM_NAMESPACE)
                nodeport = svc.spec.ports[0].node_port if svc.spec.ports else None
                if nodeport:
                    # Get a node IP (any node will work for NodePort)
                    nodes = self.core_v1.list_node()
                    for node in nodes.items:
                        for addr in (node.status.addresses or []):
                            if addr.type == "InternalIP":
                                ssh_host = addr.address
                                ssh_port = nodeport
                                break
                        if ssh_host:
                            break
            except Exception:
                log.debug("No SSH service found for %s, falling back to pod IP", vm_name)

            # Fallback: pod IP (same-cluster only)
            pod_ip = None
            if not ssh_host:
                pods = self.core_v1.list_namespaced_pod(
                    VM_NAMESPACE,
                    label_selector=f"vm.kubevirt.io/name={vm_name}",
                )
                for pod in pods.items:
                    if pod.status and pod.status.pod_ip:
                        pod_ip = pod.status.pod_ip
                        break

            response = {"job_id": job_id}
            if ssh_host and ssh_port:
                response["ssh_host"] = ssh_host
                response["ssh_port"] = ssh_port
                log.info("SSH endpoint for %s: %s:%d", vm_name, ssh_host, ssh_port)
            else:
                response["pod_ip"] = pod_ip
                log.info("Pod IP fallback for %s: %s", vm_name, pod_ip)

            if msg.reply:
                await self.nc.publish(msg.reply, json.dumps(response).encode())

        except Exception as e:
            job_id = "unknown"
            try:
                job_id = json.loads(msg.data.decode()).get("job_id", "unknown")
            except Exception:
                pass
            log.exception("Failed pod-ip query for job %s", job_id)
            if msg.reply:
                await self.nc.publish(
                    msg.reply,
                    json.dumps({"job_id": job_id, "error": str(e)}).encode(),
                )

    def _create_ssh_service(self, vm_name: str, job_id: str) -> int | None:
        """Create a NodePort Service to expose VM SSH port for cross-cluster access.

        Returns the assigned NodePort, or None on failure.
        """
        from kubernetes import client

        svc_name = f"ssh-{vm_name}"
        svc = client.V1Service(
            metadata=client.V1ObjectMeta(
                name=svc_name,
                namespace=VM_NAMESPACE,
                labels={
                    "app": "agent-vm-ssh",
                    "job-id": job_id,
                    "vm-name": vm_name,
                },
            ),
            spec=client.V1ServiceSpec(
                type="NodePort",
                selector={"vm.kubevirt.io/name": vm_name},
                ports=[
                    client.V1ServicePort(
                        name="ssh",
                        port=22,
                        target_port=22,
                        protocol="TCP",
                    ),
                ],
            ),
        )
        try:
            created = self.core_v1.create_namespaced_service(VM_NAMESPACE, svc)
            nodeport = created.spec.ports[0].node_port
            log.info("SSH service created: %s (NodePort %d)", svc_name, nodeport)
            return nodeport
        except Exception:
            log.exception("Failed to create SSH service for %s", vm_name)
            return None

    def _delete_ssh_service(self, vm_name: str, job_id: str) -> None:
        """Delete the SSH NodePort Service for a VM."""
        svc_name = f"ssh-{vm_name}"
        try:
            self.core_v1.delete_namespaced_service(svc_name, VM_NAMESPACE)
            log.info("SSH service deleted: %s", svc_name)
        except Exception:
            log.warning("Failed to delete SSH service %s (may not exist)", svc_name)

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
        await self.connect_nats()

        # Subscribe to lifecycle subjects
        await self.nc.subscribe("vm.lifecycle.create", cb=self.handle_create)
        await self.nc.subscribe("vm.lifecycle.delete", cb=self.handle_delete)
        await self.nc.subscribe("vm.lifecycle.get", cb=self.handle_status_query)
        await self.nc.subscribe("vm.query.pod-ip", cb=self.handle_pod_ip_query)
        log.info(
            "Subscribed to vm.lifecycle.{create,delete,get}, vm.query.pod-ip — waiting for requests"
        )

        # Wait for shutdown signal
        await self._shutdown.wait()

        # Drain and disconnect
        log.info("Shutting down...")
        if self.nc and self.nc.is_connected:
            await self.nc.drain()

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
