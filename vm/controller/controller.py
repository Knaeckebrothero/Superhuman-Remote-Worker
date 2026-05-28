"""
VM Controller — KubeVirt VM Lifecycle Manager

Manages KubeVirt VirtualMachine resources on behalf of the orchestrator.
Two transports, selected by TRANSPORT env (nats|http|both):

  nats  — cross-cluster: subscribe to vm.lifecycle.{create,delete,get},
          publish results on vm.lifecycle.status. Default for the
          deployment-vms/ Fleet bundle.
  http  — same-cluster: serve POST /vms, DELETE /vms/{id}, GET /vms/{id}
          on LISTEN_PORT (default 8080). Returns the result synchronously
          so the orchestrator's HTTP client can update job context itself
          — no separate status channel needed for lifecycle events.
  both  — run both. Useful when migrating, or when the in-VM management
          daemon still uses NATS while the orchestrator dials HTTP.

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
# Per-orchestrator scope for vm.lifecycle.* subjects. Required when the
# controller and its orchestrator share a NATS hub with other SRW
# installations; without it the controller would receive every orchestrator's
# vm.lifecycle.create and provision duplicate VMs.
ORCHESTRATOR_ID = os.environ.get("ORCHESTRATOR_ID", "").strip()
VM_TEMPLATE_PATH = os.environ.get("VM_TEMPLATE_PATH", "/config/vm-template.yaml")
VM_NAMESPACE = os.environ.get("VM_NAMESPACE", "agent-vms")
DEFAULT_VM_IMAGE = os.environ.get(
    "DEFAULT_VM_IMAGE",
    "ghcr.io/knaeckebrothero/superhuman-remote-worker-agent-vm-base:latest",
)
DEFAULT_CPU = int(os.environ.get("DEFAULT_CPU", "2"))
DEFAULT_MEMORY = os.environ.get("DEFAULT_MEMORY", "4Gi")
VM_STORAGE_CLASS = os.environ.get("VM_STORAGE_CLASS", "local-path")
VM_DISK_SIZE = os.environ.get("VM_DISK_SIZE", "20Gi")

# Transport selection: nats | http | both. Defaults to nats so existing
# deployment-vms/ Fleet bundles keep working without overrides.
TRANSPORT = os.environ.get("TRANSPORT", "nats").lower()
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))

# KubeVirt API coordinates
KUBEVIRT_GROUP = "kubevirt.io"
KUBEVIRT_VERSION = "v1"
KUBEVIRT_PLURAL = "virtualmachines"


class VMController:
    """Manages KubeVirt VM lifecycle via NATS commands."""

    def __init__(self):
        self.nc = None  # NATS client (when transport includes nats)
        self.http_runner = None  # aiohttp AppRunner (when transport includes http)
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
            # Per-orchestrator scope for the management-daemon + sudo-gated
            # NATS subjects inside the VM. Burned into /etc/default by
            # cloud-init so the in-VM publishers reach this orchestrator's
            # scoped subscribe wildcards.
            "${ORCHESTRATOR_ID}": ORCHESTRATOR_ID,
            "${DESCRIPTION}": job_config.get("description", ""),
            # CDI DataVolume storage
            "${VM_STORAGE_CLASS}": VM_STORAGE_CLASS,
            "${VM_DISK_SIZE}": VM_DISK_SIZE,
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

    # =========================================================================
    # Transport-agnostic core
    #
    # Each `_do_*` method takes a plain dict, performs the K8s work, and
    # returns a result dict shaped the same as the historical NATS status
    # payload. Both NATS and HTTP transports wrap these.
    # =========================================================================

    async def _do_create(self, job_config: dict) -> dict:
        """Create a KubeVirt VirtualMachine for a job."""
        from kubernetes.client.exceptions import ApiException

        job_id = job_config.get("job_id", "unknown")
        log.info("Creating VM for job %s", job_id)

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
                break
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
        return {
            "job_id": job_id,
            "status": "created",
            "vm_name": vm_name,
            "namespace": VM_NAMESPACE,
        }

    async def _do_delete(self, job_id: str) -> dict:
        """Delete a KubeVirt VirtualMachine for a job."""
        from kubernetes.client.exceptions import ApiException

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

        if self.headscale.is_available:
            await self.headscale.delete_node(job_id)

        log.info("VM deleted: %s (job %s)", vm_name, job_id)
        return {"job_id": job_id, "status": "deleted", "vm_name": vm_name}

    async def _do_status(self, job_id: str) -> dict:
        """Query KubeVirt for a VM's current status."""
        vm_name = f"agent-vm-{job_id}"
        vm = self.k8s_client.get_namespaced_custom_object(
            group=KUBEVIRT_GROUP,
            version=KUBEVIRT_VERSION,
            namespace=VM_NAMESPACE,
            plural=KUBEVIRT_PLURAL,
            name=vm_name,
        )
        status = vm.get("status", {})
        conditions = status.get("conditions", [])
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
        )
        return {
            "job_id": job_id,
            "vm_name": vm_name,
            "ready": ready,
            "phase": status.get("printableStatus", "Unknown"),
            "created": status.get("created", False),
        }

    # =========================================================================
    # NATS transport
    # =========================================================================

    async def handle_create(self, msg):
        """vm.lifecycle.create → _do_create + publish vm.lifecycle.status."""
        try:
            job_config = json.loads(msg.data.decode())
            result = await self._do_create(job_config)
            await self._publish_status(result["job_id"], result)
        except Exception as e:
            job_id = _safe_job_id(msg.data)
            log.exception("Failed to create VM for job %s", job_id)
            await self._publish_status(
                job_id, {"job_id": job_id, "status": "failed", "error": str(e)}
            )

    async def handle_delete(self, msg):
        """vm.lifecycle.delete → _do_delete + publish vm.lifecycle.status."""
        try:
            data = json.loads(msg.data.decode())
            result = await self._do_delete(data["job_id"])
            await self._publish_status(result["job_id"], result)
        except Exception as e:
            job_id = _safe_job_id(msg.data)
            log.exception("Failed to delete VM for job %s", job_id)
            await self._publish_status(
                job_id,
                {"job_id": job_id, "status": "delete_failed", "error": str(e)},
            )

    async def handle_status_query(self, msg):
        """vm.lifecycle.get → _do_status (request/reply or status publish)."""
        try:
            data = json.loads(msg.data.decode())
            response = await self._do_status(data["job_id"])
            if msg.reply:
                await self.nc.publish(msg.reply, json.dumps(response).encode())
            else:
                await self._publish_status(response["job_id"], response)
        except Exception as e:
            job_id = _safe_job_id(msg.data)
            error_response = {
                "job_id": job_id,
                "status": "query_failed",
                "error": str(e),
            }
            if msg.reply:
                await self.nc.publish(msg.reply, json.dumps(error_response).encode())
            else:
                await self._publish_status(job_id, error_response)

    # =========================================================================
    # HTTP transport (aiohttp)
    # =========================================================================

    async def http_create(self, request):
        """POST /vms — body is the create payload, returns the result dict."""
        from aiohttp import web

        try:
            payload = await request.json()
        except Exception as e:
            return web.json_response({"error": f"invalid json: {e}"}, status=400)

        if not payload.get("job_id"):
            return web.json_response({"error": "job_id required"}, status=400)

        try:
            result = await self._do_create(payload)
            return web.json_response(result, status=200)
        except Exception as e:
            log.exception("HTTP create failed for job %s", payload.get("job_id"))
            return web.json_response(
                {
                    "job_id": payload.get("job_id", "unknown"),
                    "status": "failed",
                    "error": str(e),
                },
                status=500,
            )

    async def http_delete(self, request):
        """DELETE /vms/{job_id} — returns the result dict."""
        from aiohttp import web

        job_id = request.match_info.get("job_id")
        if not job_id:
            return web.json_response({"error": "job_id required"}, status=400)

        try:
            result = await self._do_delete(job_id)
            return web.json_response(result, status=200)
        except Exception as e:
            log.exception("HTTP delete failed for job %s", job_id)
            return web.json_response(
                {"job_id": job_id, "status": "delete_failed", "error": str(e)},
                status=500,
            )

    async def http_status(self, request):
        """GET /vms/{job_id} — returns the result dict."""
        from aiohttp import web

        job_id = request.match_info.get("job_id")
        if not job_id:
            return web.json_response({"error": "job_id required"}, status=400)

        try:
            result = await self._do_status(job_id)
            return web.json_response(result, status=200)
        except Exception as e:
            from kubernetes.client.exceptions import ApiException

            if isinstance(e, ApiException) and e.status == 404:
                return web.json_response(
                    {"job_id": job_id, "status": "not_found"}, status=404
                )
            log.debug("HTTP status query failed for job %s: %s", job_id, e)
            return web.json_response(
                {"job_id": job_id, "status": "query_failed", "error": str(e)},
                status=500,
            )

    async def http_health(self, _request):
        """GET /healthz — liveness probe target."""
        from aiohttp import web

        return web.json_response({"status": "ok"})

    async def _publish_status(self, job_id: str, payload: dict):
        """Publish a status message on vm.lifecycle.status.{ORCHESTRATOR_ID}
        (NATS only)."""
        if not self.nc:
            return
        try:
            await self.nc.publish(
                f"vm.lifecycle.status.{ORCHESTRATOR_ID}",
                json.dumps(payload).encode(),
            )
        except Exception:
            log.exception("Failed to publish status for job %s", job_id)

    async def start_http_server(self) -> None:
        """Start the aiohttp HTTP server. Runs alongside other transports."""
        from aiohttp import web

        app = web.Application()
        app.router.add_post("/vms", self.http_create)
        app.router.add_delete("/vms/{job_id}", self.http_delete)
        app.router.add_get("/vms/{job_id}", self.http_status)
        app.router.add_get("/healthz", self.http_health)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, LISTEN_HOST, LISTEN_PORT)
        await site.start()
        self.http_runner = runner
        log.info("HTTP server listening on %s:%d", LISTEN_HOST, LISTEN_PORT)

    async def run(self):
        """Main entry point — connect transports, wait for shutdown."""
        log.info("VM Controller starting (transport=%s)", TRANSPORT)

        if TRANSPORT not in ("nats", "http", "both"):
            log.error("Invalid TRANSPORT=%s (expected nats|http|both)", TRANSPORT)
            sys.exit(1)

        self.load_template()
        self.init_k8s()
        await self.headscale.init()

        if TRANSPORT in ("nats", "both"):
            if not ORCHESTRATOR_ID:
                log.error(
                    "ORCHESTRATOR_ID is required for NATS transport — refusing to "
                    "subscribe to flat vm.lifecycle.* (would cross-talk on shared hub)"
                )
                sys.exit(1)
            await self.connect_nats()
            suffix = f".{ORCHESTRATOR_ID}"
            await self.nc.subscribe(
                f"vm.lifecycle.create{suffix}", cb=self.handle_create
            )
            await self.nc.subscribe(
                f"vm.lifecycle.delete{suffix}", cb=self.handle_delete
            )
            await self.nc.subscribe(
                f"vm.lifecycle.get{suffix}", cb=self.handle_status_query
            )
            log.info(
                "Subscribed to vm.lifecycle.{create,delete,get}.%s — waiting for NATS requests",
                ORCHESTRATOR_ID,
            )

        if TRANSPORT in ("http", "both"):
            await self.start_http_server()

        # Wait for shutdown signal
        await self._shutdown.wait()

        log.info("Shutting down...")
        if self.nc and self.nc.is_connected:
            await self.nc.drain()
        if self.http_runner is not None:
            await self.http_runner.cleanup()
        await self.headscale.close()

        log.info("VM Controller stopped")

    def request_shutdown(self):
        """Signal the controller to shut down gracefully."""
        self._shutdown.set()


def _safe_job_id(data: bytes) -> str:
    """Extract job_id from a NATS payload without raising."""
    try:
        return json.loads(data.decode()).get("job_id", "unknown")
    except Exception:
        return "unknown"


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
