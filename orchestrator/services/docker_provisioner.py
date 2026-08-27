"""Docker Compose Provisioner — Static workspace pool for non-Kubernetes deployments.

When the orchestrator detects that Kubernetes is unavailable, it uses this
provisioner instead of ContainerProvisioner.  Rather than creating/deleting
pods on demand, it works with pre-existing workspace containers defined in
the Docker Compose file and assigned via environment variable.

Workspace lifecycle:
  1. assign_workspace(job_id)  — pick a free host, mark it in-use in DB
  2. release_workspace(job_id) — snapshot, then quarantine pending recreation
  3. get_workspace_status(job_id) — read assignment from DB

Static containers are one-use by default because deleting workspace files is
not a tenant boundary. Explicit single-user development mode may reuse a host
after a pinned-SSH convenience cleanup.

Selection logic:
  - WORKSPACE_HOSTS env var set + non-empty → Docker provisioning enabled
  - Empty / unset → disabled
"""

import asyncio
import json
import logging
import os
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

try:
    import asyncssh
except ImportError:  # pragma: no cover - deployment dependency guard
    asyncssh = None  # type: ignore[assignment]

from services.workspace_binding import CANVAS_WORKSPACE_GENERATION_KEY

logger = logging.getLogger(__name__)

# Truthy, explicit empty AsyncSSH KnownHostsResult. Falsy values can trigger a
# fallback to ~/.ssh/known_hosts and bypass the exact provisioner fingerprint.
EMPTY_KNOWN_HOSTS = ((), (), (), (), (), (), ())


if asyncssh is not None:

    class _PinnedResetSSHClient(asyncssh.SSHClient):
        """Accept exactly one provisioner-owned Ed25519 host identity."""

        def __init__(self, expected_fingerprint: str):
            self._expected_fingerprint = expected_fingerprint

        def validate_host_public_key(self, host, addr, port, key):  # noqa: ANN001
            del host, addr, port
            return secrets.compare_digest(
                key.get_fingerprint("sha256"), self._expected_fingerprint
            )


class DockerProvisioner:
    """Workspace assignment for Docker Compose mode.

    Unlike ContainerProvisioner (which creates/deletes k8s pods on demand),
    this provisioner works with pre-existing containers defined in the
    compose file. It assigns available workspaces to jobs and quarantines them
    after use until a controller recreates and attests the container. Optional
    trusted single-user development reuse is a convenience, not tenant reset.

    Assignment state is tracked in PostgreSQL (same ``jobs.context
    .workspace_container`` JSONB field used by ContainerProvisioner) so
    the dispatch logic in ``main.py`` can use either provisioner
    interchangeably.

    Host entries support ``host:port`` format for dev mode where workspace
    SSH ports are published to the host (e.g. ``localhost:2201``). Plain
    workspace hostnames default to the container SSH port 30022; VM inventory
    retains the conventional port 22 default.
    """

    def __init__(self) -> None:
        self._db: Optional[Any] = None
        self._snapshot_service: Optional[Any] = None
        self._workspace_hosts: list[tuple[str, int]] = []
        self._vm_hosts: list[tuple[str, int]] = []
        # Maps SSH "host:port" → IDE "host:port" for code-server access
        self._ide_hosts: dict[str, tuple[str, int]] = {}
        self._workspace_fingerprints: dict[str, str] = {}
        self._bootstrap_attested_endpoints: set[str] = set()
        self._trusted_dev_reuse = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """True if at least one workspace host is configured."""
        return len(self._workspace_hosts) > 0

    @property
    def workspace_hosts(self) -> list[str]:
        """Human-readable list of workspace host entries."""
        return [self._host_key(h, p) for h, p in self._workspace_hosts]

    @property
    def vm_hosts(self) -> list[str]:
        """Human-readable list of VM host entries."""
        return [self._host_key(h, p) for h, p in self._vm_hosts]

    @staticmethod
    def _host_key(host: str, port: int) -> str:
        """Canonical key for occupancy tracking (``host:port``)."""
        return f"{host}:{port}"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # Default workspace ports for dev compose (published SSH ports on localhost)
    _DEV_COMPOSE_DEFAULTS = "localhost:2201,localhost:2202,localhost:2203"
    _DEV_COMPOSE_IDE_DEFAULTS = "localhost:18081,localhost:18082,localhost:18083"
    _DEV_COMPOSE_SSH_KEY_REL = ".dev/ssh-keys/id_ed25519"

    # Repo root: orchestrator/services/docker_provisioner.py → ../../
    _REPO_ROOT = Path(__file__).resolve().parent.parent.parent

    def connect(
        self,
        db: Any,
        snapshot_service: Optional[Any] = None,
    ) -> None:
        """Initialise the provisioner.

        Args:
            db: PostgresDB instance for job context updates.
            snapshot_service: Optional SnapshotService for workspace recycling.
        """
        self._db = db
        self._snapshot_service = snapshot_service

        # Auto-detect dev compose: if WORKSPACE_HOSTS is not set but the
        # dev compose SSH key exists on disk, apply dev defaults so the
        # developer doesn't need to export env vars manually.
        ssh_key_path = self._REPO_ROOT / self._DEV_COMPOSE_SSH_KEY_REL
        if not os.environ.get("WORKSPACE_HOSTS", "").strip():
            if ssh_key_path.exists():
                logger.info(
                    "Docker provisioner: detected dev compose "
                    "(found %s) — applying dev defaults",
                    ssh_key_path,
                )
                os.environ.setdefault("WORKSPACE_HOSTS", self._DEV_COMPOSE_DEFAULTS)
                os.environ.setdefault(
                    "WORKSPACE_IDE_HOSTS", self._DEV_COMPOSE_IDE_DEFAULTS
                )
                os.environ.setdefault("SSH_KEY_PATH", str(ssh_key_path))

        self._workspace_hosts = self._parse_hosts("WORKSPACE_HOSTS", default_port=30022)
        self._vm_hosts = self._parse_hosts("VM_HOSTS")
        self._workspace_fingerprints = self._parse_host_fingerprints(
            "WORKSPACE_HOST_KEY_FINGERPRINTS"
        )
        requested_bootstrap = {
            self._host_key(host, port)
            for host, port in self._parse_hosts(
                "DOCKER_WORKSPACE_BOOTSTRAP_ATTESTED_ENDPOINTS",
                default_port=30022,
            )
        }
        configured_endpoints = {
            self._host_key(host, port) for host, port in self._workspace_hosts
        }
        unknown_bootstrap = requested_bootstrap - configured_endpoints
        if unknown_bootstrap:
            logger.warning(
                "Docker provisioner: ignoring bootstrap attestations for "
                "unconfigured endpoints: %s",
                ",".join(sorted(unknown_bootstrap)),
            )
        missing_fingerprints = {
            key
            for key in requested_bootstrap & configured_endpoints
            if key not in self._workspace_fingerprints
        }
        if missing_fingerprints:
            logger.error(
                "Docker provisioner: bootstrap attestation requires an exact "
                "host fingerprint for: %s",
                ",".join(sorted(missing_fingerprints)),
            )
        self._bootstrap_attested_endpoints = (
            requested_bootstrap
            & configured_endpoints
            & self._workspace_fingerprints.keys()
        )
        self._trusted_dev_reuse = os.environ.get(
            "DOCKER_WORKSPACE_TRUSTED_DEV_REUSE", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}

        # Build SSH→IDE host mapping (positional: 1st SSH ↔ 1st IDE, etc.)
        ide_hosts = self._parse_hosts("WORKSPACE_IDE_HOSTS")
        for i, (ws_host, ws_port) in enumerate(self._workspace_hosts):
            ws_key = self._host_key(ws_host, ws_port)
            if i < len(ide_hosts):
                self._ide_hosts[ws_key] = ide_hosts[i]

        if self._workspace_hosts:
            logger.info(
                "Docker provisioner ready (workspaces=%s, vms=%s)",
                ",".join(self.workspace_hosts),
                ",".join(self.vm_hosts) if self._vm_hosts else "none",
            )
        else:
            logger.info("Docker provisioner: not available (WORKSPACE_HOSTS not set)")

    @staticmethod
    def _parse_hosts(env_var: str, *, default_port: int = 22) -> list[tuple[str, int]]:
        """Parse comma-separated ``host[:port]`` entries from an env var.

        Examples::

            workspace-1,workspace-2       → [("workspace-1", 22), ("workspace-2", 22)]
            localhost:2201,localhost:2202  → [("localhost", 2201), ("localhost", 2202)]
        """
        raw = os.environ.get(env_var, "")
        result: list[tuple[str, int]] = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" in entry:
                host, port_str = entry.rsplit(":", 1)
                try:
                    result.append((host.strip(), int(port_str.strip())))
                except ValueError:
                    # Not a valid port — treat entire string as hostname
                    result.append((entry, default_port))
            else:
                result.append((entry, default_port))
        return result

    @staticmethod
    def _parse_host_fingerprints(env_var: str) -> dict[str, str]:
        """Parse ``host:port=SHA256:...`` trusted inventory entries."""

        result: dict[str, str] = {}
        for entry in os.environ.get(env_var, "").split(","):
            target, separator, fingerprint = entry.strip().partition("=")
            if (
                separator
                and target
                and fingerprint.startswith("SHA256:")
                and len(fingerprint) <= 128
                and not any(char.isspace() for char in fingerprint)
            ):
                result[target] = fingerprint
        return result

    # ------------------------------------------------------------------
    # Workspace assignment
    # ------------------------------------------------------------------

    def _lease_candidates(self) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for host, port in self._workspace_hosts:
            key = self._host_key(host, port)
            candidate: dict[str, Any] = {
                "host": host,
                "port": port,
                "bootstrap_attested": key in self._bootstrap_attested_endpoints,
                "trusted_dev_reuse": self._trusted_dev_reuse,
            }
            fingerprint = self._workspace_fingerprints.get(key)
            if fingerprint is not None:
                candidate["host_key_fingerprint"] = fingerprint
            ide_entry = self._ide_hosts.get(key)
            if ide_entry:
                candidate["ide_host"], candidate["ide_port"] = ide_entry
            candidates.append(candidate)
        return candidates

    async def assign_workspace(self, job_id: str) -> Optional[dict]:
        """Find a free workspace and assign it to this job.

        Checks ``jobs.context.workspace_container`` across all active jobs
        to determine which host:port pairs are currently in use.  Returns
        the first free one.

        Returns:
            ``{"host": "workspace-1", "port": 22, "status": "ready"}``
            or ``None`` if all workspaces are occupied.
        """
        if not self._db or not self._workspace_hosts:
            return None

        ctx = await self._db.acquire_docker_workspace_lease(
            owner_kind="job",
            owner_id=job_id,
            candidates=self._lease_candidates(),
        )
        if not isinstance(ctx, dict) or ctx.get("status") != "ready":
            logger.warning(
                "Docker provisioner: no reusable workspace is available for job %s",
                job_id,
            )
            return None
        logger.info(
            "Docker provisioner: assigned workspace %s to job %s",
            self._host_key(str(ctx["host"]), int(ctx.get("port", 22))),
            job_id,
        )
        return ctx

    async def release_workspace(self, job_id: str) -> bool:
        """End a job lease without crossing the workspace trust boundary.

        Production/default behavior quarantines the container until a future
        controller recreates and attests it. The optional trusted single-user
        development mode may perform a convenience SSH cleanup and reuse the
        same container, but that cleanup is not a security reset.

        Returns:
            True if released successfully, False otherwise.
        """
        if not self._db:
            return False

        ctx = await self._get_job_workspace_context(job_id)
        if not ctx or ctx.get("provisioner") != "docker":
            return False

        releasing = await self._db.transition_docker_workspace_lease(
            owner_kind="job",
            owner_id=job_id,
            expected_lease_id=ctx.get("_docker_workspace_lease_id"),
            expected_statuses={"ready"},
            updates={"status": "releasing"},
        )
        if releasing is None:
            return False
        # Owner JSON is only a lease reference. The inventory row returned by
        # the release CAS is authoritative for every endpoint side effect.
        host = str(releasing.get("host") or "")
        port = int(releasing.get("port", 22))
        lease_id = str(releasing["_docker_workspace_lease_id"])

        # Snapshot to S3 before release (best-effort)
        if self._snapshot_service and self._snapshot_service.is_available:
            try:
                await self._snapshot_service.capture_vm_snapshot(
                    job_id=job_id,
                    ssh_host=host,
                    ssh_port=port,
                    source_type="container",
                )
                logger.info(
                    "Docker provisioner: captured snapshot for job %s "
                    "from %s before release",
                    job_id,
                    host,
                )
            except Exception:
                logger.exception(
                    "Docker provisioner: snapshot failed for job %s "
                    "on %s — releasing anyway",
                    job_id,
                    host,
                )

        if not self._trusted_dev_reuse:
            await self._db.transition_docker_workspace_lease(
                owner_kind="job",
                owner_id=job_id,
                expected_lease_id=lease_id,
                expected_statuses={"releasing"},
                updates={
                    "status": "quarantined",
                    "quarantine_reason": "container_recreation_required",
                },
            )
            logger.warning(
                "Docker provisioner: quarantined %s:%d; safe reuse requires "
                "controller-attested container recreation",
                host,
                port,
            )
            return False

        try:
            cleaned = await self._reset_workspace_via_ssh(host, port)
        except Exception:
            logger.exception(
                "Docker provisioner: dev cleanup raised on %s:%d", host, port
            )
            cleaned = False

        final_status = "released" if cleaned else "quarantined"
        finalized = await self._db.transition_docker_workspace_lease(
            owner_kind="job",
            owner_id=job_id,
            expected_lease_id=lease_id,
            expected_statuses={"releasing"},
            updates={
                "status": final_status,
                "quarantine_reason": None if cleaned else "dev_cleanup_failed",
                **(
                    {
                        "_docker_workspace_trust_mode": "trusted_dev",
                        "_docker_workspace_attested": False,
                    }
                    if cleaned
                    else {}
                ),
            },
        )
        if finalized is None or not cleaned:
            logger.error(
                "Docker provisioner: workspace %s:%d remains unavailable after "
                "dev cleanup failure",
                host,
                port,
            )
            return False
        logger.info(
            "Docker provisioner: released workspace %s from job %s",
            host,
            job_id,
        )
        return True

    async def get_workspace_status(self, job_id: str) -> Optional[dict]:
        """Return the workspace assignment for a job, or None."""
        return await self._get_job_workspace_context(job_id)

    # ------------------------------------------------------------------
    # Thread (persistent session) workspace assignment
    # ------------------------------------------------------------------

    async def assign_thread_workspace(self, thread_id: str) -> Optional[dict]:
        """Assign a free workspace to a persistent agent thread.

        Same logic as ``assign_workspace`` but writes to
        ``threads.metadata.workspace_container``.
        """
        if not self._db or not self._workspace_hosts:
            return None

        ctx = await self._db.acquire_docker_workspace_lease(
            owner_kind="thread",
            owner_id=thread_id,
            candidates=self._lease_candidates(),
        )
        if not isinstance(ctx, dict) or ctx.get("status") != "ready":
            logger.warning(
                "Docker provisioner: no reusable workspace is available for thread %s",
                thread_id,
            )
            return None

        host = str(ctx["host"])
        port = int(ctx.get("port", 22))
        key = self._host_key(host, port)
        lease_id = str(ctx["_docker_workspace_lease_id"])
        fingerprint = self._workspace_fingerprints.get(key)
        inventory_attested = ctx.get("_docker_workspace_attested") is True
        if fingerprint is None or not inventory_attested:
            logger.warning(
                "Docker workspace %s lacks a matching production inventory "
                "attestation; Canvas file serving is disabled for thread %s",
                key,
                thread_id,
            )
            try:
                canvas_disabled = await self._db.transition_docker_workspace_lease(
                    owner_kind="thread",
                    owner_id=thread_id,
                    expected_lease_id=lease_id,
                    expected_statuses={"ready"},
                    updates={
                        "status": "ready",
                        CANVAS_WORKSPACE_GENERATION_KEY: None,
                    },
                )
            except Exception:
                logger.exception(
                    "Docker provisioner: failed to withdraw Canvas capability "
                    "from unattested thread lease %s",
                    thread_id,
                )
                return None
            if canvas_disabled is None:
                logger.warning(
                    "Docker provisioner: unattested thread lease changed before "
                    "Canvas capability could be withdrawn for %s",
                    thread_id,
                )
                return None
            ctx.update(canvas_disabled)
            return ctx
        try:
            binding = await self._db.bind_thread_workspace_backing(
                thread_id,
                backing_kind="remote",
                backing_id=f"docker:{key}:{lease_id}",
                ssh_host_key_fingerprint=fingerprint,
            )
            canvas_generation = str(
                UUID(str((binding or {}).get("workspace_generation")))
            )
        except Exception:
            logger.exception(
                "Failed to bind trusted Canvas SSH identity for Docker thread %s",
                thread_id,
            )
            await self._quarantine_unpaired_thread_lease(thread_id, ctx)
            return None

        try:
            paired = await self._db.transition_docker_workspace_lease(
                owner_kind="thread",
                owner_id=thread_id,
                expected_lease_id=lease_id,
                expected_statuses={"ready"},
                updates={
                    "status": "ready",
                    CANVAS_WORKSPACE_GENERATION_KEY: canvas_generation,
                },
            )
        except Exception:
            logger.exception(
                "Docker provisioner: failed to CAS-pair Canvas endpoint for thread %s",
                thread_id,
            )
            await self._quarantine_unpaired_thread_lease(thread_id, ctx)
            return None
        if paired is None:
            logger.error(
                "Docker provisioner: lease changed before Canvas endpoint pairing "
                "for thread %s",
                thread_id,
            )
            await self._quarantine_unpaired_thread_lease(thread_id, ctx)
            return None
        ctx.update(paired)
        logger.info(
            "Docker provisioner: assigned workspace %s to thread %s", key, thread_id
        )
        return ctx

    async def _quarantine_unpaired_thread_lease(
        self, thread_id: str, ctx: dict[str, Any]
    ) -> None:
        """Keep a leased host unavailable if Canvas endpoint pairing failed."""

        try:
            quarantined = await self._db.transition_docker_workspace_lease(
                owner_kind="thread",
                owner_id=thread_id,
                expected_lease_id=str(ctx["_docker_workspace_lease_id"]),
                expected_statuses={"ready"},
                updates={
                    "status": "quarantined",
                    CANVAS_WORKSPACE_GENERATION_KEY: None,
                },
            )
        except Exception:
            logger.exception(
                "Docker provisioner: failed to quarantine unpaired thread lease %s",
                thread_id,
            )
            return
        if quarantined is None:
            logger.error(
                "Docker provisioner: unpaired thread lease %s could not be quarantined",
                thread_id,
            )

    async def release_thread_workspace(
        self,
        thread_id: str,
        *,
        expected_lease_id: str | None = None,
        force_quarantine: bool = False,
    ) -> bool:
        """Release a workspace assigned to a persistent thread."""
        if not self._db:
            return False

        # Read thread metadata for workspace context
        try:
            thread = await self._db.get_thread(thread_id)
            if not thread:
                return False
            metadata = thread.get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            ctx = metadata.get("workspace_container") or {}
        except Exception:
            logger.exception(
                "Docker provisioner: failed to read thread %s metadata",
                thread_id,
            )
            return False

        if ctx.get("provisioner") != "docker":
            return False
        if expected_lease_id is not None and str(
            ctx.get("_docker_workspace_lease_id") or ""
        ) != str(expected_lease_id):
            return False
        current_status = str(ctx.get("status") or "")
        if current_status in {"released", "quarantined"}:
            if force_quarantine and current_status == "released":
                # A released endpoint may already have been allocated under a
                # new lease. Never relabel it quarantined by stale owner JSON.
                return False
            terminal = await self._db.transition_docker_workspace_lease(
                owner_kind="thread",
                owner_id=thread_id,
                expected_lease_id=ctx.get("_docker_workspace_lease_id"),
                expected_statuses={current_status},
                updates={"status": current_status},
            )
            return terminal is not None and str(terminal.get("status") or "") == (
                current_status
            )

        # Claim the release before snapshot/reset. Only one replica can move a
        # concrete lease from ready to releasing, and Canvas is revoked in the
        # same DB update before the static host is touched.
        if current_status not in {"ready", "releasing"}:
            return False
        releasing = await self._db.transition_docker_workspace_lease(
            owner_kind="thread",
            owner_id=thread_id,
            expected_lease_id=ctx.get("_docker_workspace_lease_id"),
            expected_statuses={current_status},
            updates={
                "status": "releasing",
                CANVAS_WORKSPACE_GENERATION_KEY: None,
            },
        )
        if releasing is None:
            logger.error(
                "Docker provisioner: thread %s workspace release was not claimed",
                thread_id,
            )
            return False
        # Owner JSON is only a lease reference. The inventory row returned by
        # the release CAS is authoritative for every endpoint side effect.
        host = str(releasing.get("host") or "")
        port = int(releasing.get("port", 22))
        lease_id = str(releasing["_docker_workspace_lease_id"])

        # Snapshot before release (best-effort)
        if self._snapshot_service and self._snapshot_service.is_available:
            try:
                await self._snapshot_service.capture_vm_snapshot(
                    job_id=thread_id,
                    ssh_host=host,
                    ssh_port=port,
                    source_type="container",
                    entity_type="threads",
                )
            except Exception:
                logger.exception(
                    "Docker provisioner: snapshot failed for thread %s",
                    thread_id,
                )

        if force_quarantine or not self._trusted_dev_reuse:
            quarantined = await self._db.transition_docker_workspace_lease(
                owner_kind="thread",
                owner_id=thread_id,
                expected_lease_id=lease_id,
                expected_statuses={"releasing"},
                updates={
                    "status": "quarantined",
                    "quarantine_reason": "container_recreation_required",
                    CANVAS_WORKSPACE_GENERATION_KEY: None,
                },
            )
            if quarantined is None or str(quarantined.get("status") or "") != (
                "quarantined"
            ):
                logger.error(
                    "Docker provisioner: exact quarantine did not commit for %s",
                    thread_id,
                )
                return False
            logger.warning(
                "Docker provisioner: quarantined %s:%d; safe reuse requires "
                "controller-attested container recreation",
                host,
                port,
            )
            # Quarantine removes the host from allocation and is the only safe
            # terminal disposition after protected strict UID-zero (which also
            # kills code-server). Trusted-dev file reset may never revive or
            # reassign that same container. It is not a cleanup failure.
            return True

        try:
            cleaned = await self._reset_workspace_via_ssh(host, port)
        except Exception:
            logger.exception(
                "Docker provisioner: dev cleanup raised on %s:%d", host, port
            )
            cleaned = False
        final_status = "released" if cleaned else "quarantined"
        finalized = await self._db.transition_docker_workspace_lease(
            owner_kind="thread",
            owner_id=thread_id,
            expected_lease_id=lease_id,
            expected_statuses={"releasing"},
            updates={
                "status": final_status,
                "quarantine_reason": None if cleaned else "dev_cleanup_failed",
                CANVAS_WORKSPACE_GENERATION_KEY: None,
                **(
                    {
                        "_docker_workspace_trust_mode": "trusted_dev",
                        "_docker_workspace_attested": False,
                    }
                    if cleaned
                    else {}
                ),
            },
        )
        if finalized is None or not cleaned:
            logger.error(
                "Docker provisioner: workspace %s:%d remains unavailable after "
                "dev cleanup failure",
                host,
                port,
            )
            return False
        logger.info(
            "Docker provisioner: released workspace %s from thread %s",
            host,
            thread_id,
        )
        return True

    async def fence_thread_workspace_lease(
        self, thread_id: str, *, expected_lease_id: str
    ) -> bool:
        """Make one exact Docker lease non-reallocatable before process zero."""

        if not self._db:
            return False
        try:
            thread = await self._db.get_thread(thread_id)
            metadata = (thread or {}).get("metadata") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            ctx = metadata.get("workspace_container") or {}
        except Exception:
            logger.exception("Failed to read Docker lease fence for %s", thread_id)
            return False
        if (
            not isinstance(ctx, dict)
            or ctx.get("provisioner") != "docker"
            or str(ctx.get("_docker_workspace_lease_id") or "")
            != str(expected_lease_id)
        ):
            return False
        current_status = str(ctx.get("status") or "")
        if current_status not in {"ready", "releasing"}:
            return False
        fenced = await self._db.transition_docker_workspace_lease(
            owner_kind="thread",
            owner_id=thread_id,
            expected_lease_id=expected_lease_id,
            expected_statuses={current_status},
            updates={
                "status": "releasing",
                CANVAS_WORKSPACE_GENERATION_KEY: None,
            },
        )
        return fenced is not None and str(fenced.get("status") or "") == "releasing"

    # ------------------------------------------------------------------
    # VM assignment (QEMU-in-Docker, Phase 4)
    # ------------------------------------------------------------------

    async def assign_vm(self, job_id: str) -> Optional[dict]:
        """Assign a free QEMU-in-Docker VM to a job.

        Returns:
            ``{"ssh_host": "agent-vm-1", "ssh_port": 22, "status": "ready"}``
            or ``None`` if no VMs available.
        """
        if not self._db or not self._vm_hosts:
            return None

        in_use = await self._get_occupied_vm_keys()
        for host, port in self._vm_hosts:
            key = self._host_key(host, port)
            if key not in in_use:
                ctx = {
                    "status": "ready",
                    "ssh_host": host,
                    "ssh_port": port,
                    "provisioner": "docker",
                }
                await self._db.merge_vm_context(job_id, ctx)
                logger.info(
                    "Docker provisioner: assigned VM %s to job %s",
                    key,
                    job_id,
                )
                return ctx

        logger.warning(
            "Docker provisioner: all VMs occupied — job %s must wait",
            job_id,
        )
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _reset_workspace_via_ssh(self, host: str, port: int) -> bool:
        """Best-effort cleanup for explicit single-user development reuse.

        This only removes files from the fixed workspace root. It cannot attest
        the rest of a previously agent-controlled container and therefore is
        never used by the production/default release path.

        Returns:
            True if the reset succeeded, False otherwise.
        """
        key = self._host_key(host, port)
        fingerprint = self._workspace_fingerprints.get(key)
        ssh_key = os.environ.get("SSH_KEY_PATH", "").strip()
        if asyncssh is None:
            logger.error("Docker provisioner: pinned SSH transport is unavailable")
            return False
        if not ssh_key:
            logger.warning(
                "Docker provisioner: SSH_KEY_PATH not set — cannot run dev cleanup "
                "on %s:%d",
                host,
                port,
            )
            return False
        if not fingerprint:
            logger.error(
                "Docker provisioner: no pinned host identity for dev cleanup target %s",
                key,
            )
            return False

        # Replacing the fixed root itself also removes dot-prefixed content and
        # safely recovers if an untrusted workspace replaced the root with a
        # symlink. The final assertions make a partial cleanup fail closed.
        command = (
            "set -eu; "
            "if test -d /home/agent-host/.ssh/srw-managed/sockets; then "
            "for socket in /home/agent-host/.ssh/srw-managed/sockets/*.sock; do "
            'if test -S "$socket"; then '
            'SSH_AUTH_SOCK="$socket" ssh-add -D >/dev/null 2>&1 || true; '
            "fi; done; fi; "
            "rm -rf -- /home/agent-host/.ssh/srw-managed; "
            "rm -rf -- /home/agent-host/workspace; "
            "install -d -m 700 /home/agent-host/workspace; "
            "test ! -e /home/agent-host/.ssh/srw-managed; "
            "test -d /home/agent-host/workspace; "
            "test ! -L /home/agent-host/workspace; "
            'test -z "$(find /home/agent-host/workspace -mindepth 1 '
            '-maxdepth 1 -print -quit)"'
        )

        connection = None
        try:
            async with asyncio.timeout(30):
                connection = await asyncssh.connect(
                    host,
                    port=port,
                    username="agent-host",
                    client_keys=[ssh_key],
                    known_hosts=EMPTY_KNOWN_HOSTS,
                    client_factory=lambda: _PinnedResetSSHClient(fingerprint),
                    server_host_key_algs=["ssh-ed25519"],
                    connect_timeout=10,
                    login_timeout=15,
                )
                result = await connection.run(command, check=False, timeout=20)
            if result.exit_status == 0:
                logger.info(
                    "Docker provisioner: dev workspace cleanup completed on %s:%d",
                    host,
                    port,
                )
                return True
            logger.warning(
                "Docker provisioner: dev workspace cleanup failed on %s:%d (rc=%d)",
                host,
                port,
                result.exit_status,
            )
            return False
        except TimeoutError:
            logger.warning(
                "Docker provisioner: dev workspace cleanup timed out on %s:%d",
                host,
                port,
            )
            return False
        except Exception:
            logger.exception(
                "Docker provisioner: dev workspace cleanup error on %s:%d", host, port
            )
            return False
        finally:
            if connection is not None:
                connection.close()
                with suppress(Exception):
                    await connection.wait_closed()

    async def _get_occupied_vm_keys(self) -> set[str]:
        """Return ``host:port`` keys for VMs currently assigned to active jobs."""
        occupied: set[str] = set()
        if not self._db:
            return occupied

        try:
            rows = await self._db.fetch(
                """
                SELECT context->'vm'->>'ssh_host' AS host,
                       COALESCE(
                           (context->'vm'->>'ssh_port')::int, 22
                       ) AS port
                FROM jobs
                WHERE status NOT IN ('completed', 'failed', 'cancelled')
                  AND context->'vm'->>'provisioner' = 'docker'
                  AND context->'vm'->>'status' = 'ready'
                """,
            )
            for row in rows:
                if row["host"]:
                    occupied.add(self._host_key(row["host"], row["port"]))
        except Exception:
            logger.exception("Docker provisioner: failed to query occupied VMs")

        return occupied

    async def _get_job_workspace_context(self, job_id: str) -> Optional[dict]:
        """Read workspace_container context from a job row."""
        if not self._db:
            return None
        try:
            job = await self._db.get_job(job_id)
            if not job:
                return None
            ctx = job.get("context") or {}
            if isinstance(ctx, str):
                ctx = json.loads(ctx)
            return ctx.get("workspace_container")
        except Exception:
            logger.exception(
                "Docker provisioner: failed to read job %s context", job_id
            )
            return None


# Module-level singleton
docker_provisioner = DockerProvisioner()
