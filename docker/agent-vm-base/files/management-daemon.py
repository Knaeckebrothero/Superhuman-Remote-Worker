#!/usr/bin/env python3
"""
SRW Management Daemon — runs inside KubeVirt agent VMs.

Bridges the in-VM agent process to the orchestrator via NATS:
  - Registers the VM (IP, hostname) so the orchestrator can inject SSH config
  - Sends periodic heartbeats with resource usage
  - Relays control commands (freeze/resume/terminate) to the agent process
  - Reports agent process exit status

Configuration (written by cloud-init at VM creation):
  /etc/default/management-daemon  — NATS_URL, JOB_ID (sourced by systemd)
  /run/agent/job-config.json      — full job config (job_id, agent_config, etc.)

See docs/features/nats.md for the NATS subject design.
"""

import asyncio
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("management-daemon")

# Paths
AGENT_PID_FILE = Path("/run/agent/agent.pid")
AGENT_EXIT_CODE_FILE = Path("/run/agent/agent.exit_code")
JOB_CONFIG_FILE = Path("/run/agent/job-config.json")

# Intervals
HEARTBEAT_INTERVAL = 30  # seconds
AGENT_POLL_INTERVAL = 10  # seconds
NATS_RETRY_INTERVAL = 5  # seconds


def load_config() -> dict:
    """Load configuration from environment and job config file."""
    nats_url = os.environ.get("NATS_URL")
    job_id = os.environ.get("JOB_ID")

    if not nats_url or not job_id:
        log.error("NATS_URL and JOB_ID must be set (via /etc/default/management-daemon)")
        sys.exit(1)

    config = {"nats_url": nats_url, "job_id": job_id}

    if JOB_CONFIG_FILE.exists():
        try:
            with open(JOB_CONFIG_FILE) as f:
                config.update(json.load(f))
        except Exception as e:
            log.warning("Failed to read %s: %s", JOB_CONFIG_FILE, e)

    return config


def detect_ip() -> str:
    """Detect the VM's routable IPv4 address.

    Tries UDP connect trick first (works without actual network traffic),
    falls back to iterating interfaces.
    """
    # UDP connect trick: connect to a non-routable address to find local IP
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass

    # Fallback: resolve own hostname
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    log.warning("Could not detect routable IP, using hostname")
    return socket.gethostname()


def read_agent_pid() -> int | None:
    """Read the agent process PID from the PID file."""
    try:
        if AGENT_PID_FILE.exists():
            pid = int(AGENT_PID_FILE.read_text().strip())
            # Check if process is alive
            os.kill(pid, 0)
            return pid
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    return None


def get_system_metrics() -> dict:
    """Collect CPU, memory, and disk usage."""
    try:
        import psutil
        return {
            "cpu_percent": psutil.cpu_percent(interval=0),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": psutil.disk_usage("/").percent,
        }
    except ImportError:
        return {"cpu_percent": 0.0, "memory_percent": 0.0, "disk_percent": 0.0}
    except Exception as e:
        log.debug("Failed to collect metrics: %s", e)
        return {"cpu_percent": 0.0, "memory_percent": 0.0, "disk_percent": 0.0}


class ManagementDaemon:
    """Manages communication between an in-VM agent and the orchestrator."""

    def __init__(self, config: dict):
        self.config = config
        self.job_id = config["job_id"]
        self.nats_url = config["nats_url"]
        self.nc = None
        self._shutdown = asyncio.Event()
        self._agent_exit_reported = False

    # =========================================================================
    # NATS connection
    # =========================================================================

    async def connect_nats(self):
        """Connect to NATS with retry loop."""
        import nats

        while not self._shutdown.is_set():
            try:
                self.nc = await nats.connect(
                    self.nats_url,
                    error_cb=self._on_error,
                    disconnected_cb=self._on_disconnect,
                    reconnected_cb=self._on_reconnect,
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=2,
                )
                log.info("Connected to NATS at %s", self.nats_url)
                return
            except Exception as e:
                log.warning("NATS connection failed (%s), retrying in %ds...", e, NATS_RETRY_INTERVAL)
                try:
                    await asyncio.wait_for(self._shutdown.wait(), timeout=NATS_RETRY_INTERVAL)
                    return  # Shutdown requested during retry wait
                except asyncio.TimeoutError:
                    pass

    async def _on_error(self, e):
        log.error("NATS error: %s", e)

    async def _on_disconnect(self):
        log.warning("NATS disconnected")

    async def _on_reconnect(self):
        log.info("NATS reconnected")

    # =========================================================================
    # Registration
    # =========================================================================

    async def register(self):
        """Announce this VM to the orchestrator."""
        ip = detect_ip()
        hostname = socket.gethostname()

        payload = {
            "job_id": self.job_id,
            "hostname": hostname,
            "ip": ip,
            "pid": os.getpid(),
        }

        subject = f"agent.vm.{self.job_id}.register"
        await self.nc.publish(subject, json.dumps(payload).encode())
        log.info("Registered: subject=%s, ip=%s, hostname=%s", subject, ip, hostname)

    # =========================================================================
    # Control commands
    # =========================================================================

    async def _on_control(self, msg):
        """Handle control commands from the orchestrator."""
        try:
            data = json.loads(msg.data.decode())
            action = data.get("action")
            log.info("Control command received: %s", action)

            pid = read_agent_pid()
            if not pid:
                log.warning("Cannot execute '%s': no agent process found", action)
                return

            if action == "freeze":
                os.kill(pid, signal.SIGSTOP)
                log.info("Sent SIGSTOP to agent (pid %d)", pid)
            elif action == "resume":
                os.kill(pid, signal.SIGCONT)
                log.info("Sent SIGCONT to agent (pid %d)", pid)
            elif action == "terminate":
                os.kill(pid, signal.SIGTERM)
                log.info("Sent SIGTERM to agent (pid %d)", pid)
                # Give it time to shut down gracefully, then force kill
                await asyncio.sleep(10)
                try:
                    os.kill(pid, 0)  # Check if still alive
                    os.kill(pid, signal.SIGKILL)
                    log.info("Sent SIGKILL to agent (pid %d)", pid)
                except ProcessLookupError:
                    pass
            else:
                log.warning("Unknown control action: %s", action)
        except Exception:
            log.exception("Error handling control command")

    # =========================================================================
    # Heartbeat
    # =========================================================================

    async def heartbeat_loop(self):
        """Publish periodic heartbeats with resource usage."""
        subject = f"agent.vm.{self.job_id}.heartbeat"

        while not self._shutdown.is_set():
            try:
                pid = read_agent_pid()
                metrics = get_system_metrics()

                payload = {
                    "job_id": self.job_id,
                    "agent_pid": pid,
                    "agent_running": pid is not None,
                    **metrics,
                }

                if self.nc and self.nc.is_connected:
                    await self.nc.publish(subject, json.dumps(payload).encode())
                    log.debug("Heartbeat sent: agent_running=%s", pid is not None)
            except Exception:
                log.exception("Heartbeat error")

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=HEARTBEAT_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass

    # =========================================================================
    # Agent process monitor
    # =========================================================================

    async def agent_monitor_loop(self):
        """Monitor the agent process and report exit status."""
        subject = f"agent.vm.{self.job_id}.status"
        agent_was_running = False

        while not self._shutdown.is_set():
            try:
                pid = read_agent_pid()

                if pid is not None:
                    agent_was_running = True
                elif agent_was_running and not self._agent_exit_reported:
                    # Agent was running but is now gone — report exit
                    exit_code = 1  # Default to failure
                    try:
                        if AGENT_EXIT_CODE_FILE.exists():
                            exit_code = int(AGENT_EXIT_CODE_FILE.read_text().strip())
                    except (ValueError, OSError):
                        pass

                    status = "completed" if exit_code == 0 else "failed"
                    payload = {
                        "job_id": self.job_id,
                        "status": status,
                        "exit_code": exit_code,
                    }

                    if self.nc and self.nc.is_connected:
                        await self.nc.publish(subject, json.dumps(payload).encode())
                        log.info("Agent exited: status=%s, exit_code=%d", status, exit_code)

                    self._agent_exit_reported = True
            except Exception:
                log.exception("Agent monitor error")

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=AGENT_POLL_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass

    # =========================================================================
    # Main
    # =========================================================================

    async def run(self):
        """Main entry point."""
        log.info("Management daemon starting (job_id=%s)", self.job_id)

        await self.connect_nats()
        if self._shutdown.is_set() or not self.nc:
            return

        # Register with orchestrator
        await self.register()

        # Subscribe to control commands
        control_subject = f"agent.vm.{self.job_id}.control"
        await self.nc.subscribe(control_subject, cb=self._on_control)
        log.info("Subscribed to %s", control_subject)

        # Start background tasks
        tasks = [
            asyncio.create_task(self.heartbeat_loop()),
            asyncio.create_task(self.agent_monitor_loop()),
        ]

        # Wait for shutdown signal
        await self._shutdown.wait()

        # Cancel background tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        # Drain NATS
        if self.nc and self.nc.is_connected:
            try:
                await self.nc.drain()
            except Exception:
                pass

        log.info("Management daemon stopped")

    def request_shutdown(self):
        """Signal the daemon to shut down gracefully."""
        self._shutdown.set()


def main():
    config = load_config()
    daemon = ManagementDaemon(config)

    def signal_handler(sig, _frame):
        log.info("Received signal %d, requesting shutdown", sig)
        daemon.request_shutdown()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
