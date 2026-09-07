#!/usr/bin/env python3
"""Bounded local-k3d gate for managed-repository lifecycle residues.

The gate is deliberately local-only and dry-run by default.  It creates one
disposable job row plus one production-shaped workspace Pod, PVC, and headless
Service in ``k3d-srw/srw``.  The Pod is held by the production process-zero
finalizer while the gate exercises the real managed ssh-agent scripts and the
bounded cloud-mount shell boundary.  The owner-scoped PVC and Service are kept
across the predecessor/successor Pod handoff so a stale cleanup replay can be
proved harmless to all three current resources.

The script never prints child-process output.  In particular, private key
material is sent only on ``kubectl exec -i`` stdin and is neither included in
an argument nor copied into the report.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Sequence
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5


LOCAL_CONTEXT = "k3d-srw"
LOCAL_NAMESPACE = "srw"
LOCAL_CONFIRMATION = "LOCAL-K3D-DISPOSABLE"
PROCESS_ZERO_FINALIZER = "lifecycle.srw.dev/stateless-process-zero"
CREATION_RESERVATION_ANNOTATION = "srw.io/workspace-creation-reservation"
GATE_LABEL = "srw.io/local-managed-repository-residue-gate"
DEFAULT_ORCHESTRATOR_DEPLOYMENT = "srw-orchestrator"
DEFAULT_STATELESS_DEPLOYMENT = "srw-agent-stateless"
_GATE_ID_RE = re.compile(r"srw-mr-residue-[0-9a-f]{12}\Z")
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")
_IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,510}\Z")


class SafetyError(RuntimeError):
    """The requested run is outside the hard-coded disposable boundary."""


class GateFailure(RuntimeError):
    """A redacted product-gate failure."""


@dataclass(frozen=True)
class GateConfig:
    context: str
    namespace: str
    orchestrator_deployment: str
    stateless_deployment: str
    workspace_image: str | None
    gate_id: str
    owner_id: str
    run: bool
    cleanup_only: bool
    confirmation: str | None
    timeout_seconds: int

    @property
    def pod_name(self) -> str:
        return self.gate_id

    @property
    def pvc_name(self) -> str:
        # Match ContainerProvisioner._pvc_name_for(WorkspaceOwner.job(...)).
        return f"pvc-workspace-{self.owner_id[:12]}"

    @property
    def home_path(self) -> str:
        # Keep the ssh-agent socket below Linux's short AF_UNIX path limit.
        return f"/tmp/srw-mr-{self.gate_id.rsplit('-', 1)[-1]}"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes


@dataclass(frozen=True)
class CreationEnvelope:
    """Opaque server-issued authority for one disposable Pod creation."""

    reservation_id: str
    generation: int
    claim_token: int
    settled: bool

    @classmethod
    def from_payload(cls, payload: str) -> CreationEnvelope:
        try:
            value = json.loads(payload)
            reservation_id = str(UUID(value["reservation_id"]))
            generation = value["generation"]
            claim_token = value["claim_token"]
            settled = value["settled"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GateFailure("creation reservation response was malformed") from exc
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
            or isinstance(claim_token, bool)
            or not isinstance(claim_token, int)
            or claim_token <= 0
            or not isinstance(settled, bool)
        ):
            raise GateFailure("creation reservation response was malformed")
        return cls(reservation_id, generation, claim_token, settled)


@dataclass(frozen=True)
class SharedResourceEnvelope:
    """Exact owner-scoped resource identities recorded by the reservation."""

    pvc_name: str
    pvc_uid: str
    service_name: str
    service_uid: str

    @classmethod
    def from_payload(cls, payload: str) -> SharedResourceEnvelope:
        try:
            value = json.loads(payload)
            pvc_name = value["pvc_name"]
            pvc_uid = str(UUID(value["pvc_uid"]))
            service_name = value["service_name"]
            service_uid = str(UUID(value["service_uid"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GateFailure("shared-resource response was malformed") from exc
        if not all(
            isinstance(item, str) and _DNS_LABEL_RE.fullmatch(item)
            for item in (pvc_name, service_name)
        ):
            raise GateFailure("shared-resource response was malformed")
        return cls(pvc_name, pvc_uid, service_name, service_uid)


class KubectlRunner:
    """Exact-context kubectl runner whose exceptions never include output."""

    def __init__(self, config: GateConfig) -> None:
        self._config = config

    def argv(self, arguments: Sequence[str]) -> list[str]:
        return [
            "kubectl",
            "--context",
            self._config.context,
            "--namespace",
            self._config.namespace,
            *arguments,
        ]

    def run(
        self,
        arguments: Sequence[str],
        *,
        operation: str,
        input_data: bytes | None = None,
        timeout: int | None = None,
        expect_success: bool = True,
    ) -> CommandResult:
        completed = subprocess.run(
            self.argv(arguments),
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout or self._config.timeout_seconds,
        )
        if expect_success and completed.returncode != 0:
            raise GateFailure(f"{operation} failed (rc={completed.returncode})")
        if not expect_success and completed.returncode == 0:
            raise GateFailure(f"{operation} unexpectedly succeeded")
        return CommandResult(completed.returncode, completed.stdout)


@dataclass
class SafeReport:
    gate_id: str
    mode: str
    phases: list[dict[str, str]] = field(default_factory=list)
    cleanup: str = "not_started"
    vm: str = "not_checked"

    def pass_phase(self, name: str) -> None:
        self.phases.append({"name": name, "result": "pass"})

    def fail_phase(self, name: str) -> None:
        self.phases.append({"name": name, "result": "fail"})

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "mode": self.mode,
            "phases": self.phases,
            "cleanup": self.cleanup,
            "vm": self.vm,
        }


def _validate_image(value: str) -> str:
    digest_suffix = value.rsplit("@", 1)[-1] if "@" in value else None
    if (
        not _IMAGE_RE.fullmatch(value)
        or "://" in value
        or (
            digest_suffix is not None
            and re.fullmatch(r"sha256:[0-9a-f]{64}", digest_suffix) is None
        )
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise SafetyError("workspace image reference is malformed")
    return value


def validate_config(args: argparse.Namespace) -> GateConfig:
    if args.context != LOCAL_CONTEXT or args.namespace != LOCAL_NAMESPACE:
        raise SafetyError("this harness is restricted to k3d-srw/srw")
    if not _DNS_LABEL_RE.fullmatch(args.orchestrator_deployment):
        raise SafetyError("orchestrator deployment name is malformed")
    if not _DNS_LABEL_RE.fullmatch(args.stateless_deployment):
        raise SafetyError("stateless deployment name is malformed")
    if not 60 <= args.timeout_seconds <= 600:
        raise SafetyError("timeout must be between 60 and 600 seconds")
    if args.run and args.cleanup_only:
        raise SafetyError("--run and --cleanup-only are mutually exclusive")
    mutating = bool(args.run or args.cleanup_only)
    if mutating and args.confirm != LOCAL_CONFIRMATION:
        raise SafetyError(
            f"--run/--cleanup-only requires --confirm {LOCAL_CONFIRMATION}"
        )
    if not mutating and args.confirm is not None:
        raise SafetyError("--confirm is accepted only with a mutating mode")
    if args.cleanup_only and not args.gate_id:
        raise SafetyError("--cleanup-only requires an explicit --gate-id")
    gate_id = args.gate_id or f"srw-mr-residue-{secrets.token_hex(6)}"
    if not _GATE_ID_RE.fullmatch(gate_id):
        raise SafetyError("gate id must be srw-mr-residue followed by 12 hex digits")
    image = _validate_image(args.workspace_image) if args.workspace_image else None
    owner_id = str(uuid5(NAMESPACE_URL, f"local-k3d:{gate_id}"))
    return GateConfig(
        context=args.context,
        namespace=args.namespace,
        orchestrator_deployment=args.orchestrator_deployment,
        stateless_deployment=args.stateless_deployment,
        workspace_image=image,
        gate_id=gate_id,
        owner_id=owner_id,
        run=bool(args.run),
        cleanup_only=bool(args.cleanup_only),
        confirmation=args.confirm,
        timeout_seconds=args.timeout_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default=LOCAL_CONTEXT)
    parser.add_argument("--namespace", default=LOCAL_NAMESPACE)
    parser.add_argument(
        "--orchestrator-deployment", default=DEFAULT_ORCHESTRATOR_DEPLOYMENT
    )
    parser.add_argument("--stateless-deployment", default=DEFAULT_STATELESS_DEPLOYMENT)
    parser.add_argument("--workspace-image")
    parser.add_argument("--gate-id")
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--confirm")
    return parser


def build_workspace_pod_manifest(
    config: GateConfig,
    image: str,
    *,
    creation_reservation_id: str,
) -> dict[str, Any]:
    """Return the one exact, secret-free disposable Pod manifest."""

    _validate_image(image)
    try:
        reservation_id = str(UUID(creation_reservation_id))
    except (TypeError, ValueError) as exc:
        raise SafetyError("creation reservation id is malformed") from exc
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": config.pod_name,
            "namespace": config.namespace,
            "labels": {
                "app": "srw-workspace",
                "srw/component": "workspace",
                "srw.io/component": "agent-workspace",
                "srw.io/network-tier": "internet-only",
                "srw/job-id": config.owner_id,
                GATE_LABEL: config.gate_id,
            },
            "annotations": {
                "srw.io/managed-by": "lifecycle-reconciler",
                CREATION_RESERVATION_ANNOTATION: reservation_id,
            },
            "finalizers": [PROCESS_ZERO_FINALIZER],
        },
        "spec": {
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 10,
            "containers": [
                {
                    "name": "workspace",
                    "image": image,
                    "imagePullPolicy": "IfNotPresent",
                    "command": [
                        "bash",
                        "-c",
                        "trap 'exit 0' TERM INT; while :; do sleep 1; done",
                    ],
                    "volumeMounts": [
                        {
                            "name": "workspace-data",
                            "mountPath": "/home/agent-host",
                        }
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "workspace-data",
                    "persistentVolumeClaim": {"claimName": config.pvc_name},
                }
            ],
        },
    }


_CONTROLLER_PROGRAM = r"""
import asyncio
import json
import os
import sys
from uuid import UUID

from orchestrator.database.postgres import PostgresDB
from orchestrator.services.container_provisioner import (
    ContainerProvisioner,
    STATELESS_WORKSPACE_PROCESS_ZERO_FINALIZER,
    WORKSPACE_CREATION_RESERVATION_ANNOTATION,
    _pvc_name_for,
    _pod_has_exact_process_zero,
)
from orchestrator.services.workspace_lifecycle import WorkspaceOwner


GATE_LABEL = "srw.io/local-managed-repository-residue-gate"


async def run():
    mode = sys.argv[1]
    owner_id = sys.argv[2] if len(sys.argv) > 2 else ""
    db = PostgresDB(min_connections=1, max_connections=1)
    await db.connect()
    try:
        if mode == "preflight":
            table = await db.fetchval(
                "SELECT to_regclass('public.managed_repository_process_zero_receipts')"
            )
            creation_table = await db.fetchval(
                "SELECT to_regclass("
                "'public.managed_repository_workspace_creation_reservations')"
            )
            orphan_migration = await db.fetchval(
                "SELECT success FROM schema_migrations "
                "WHERE filename = "
                "'0193_managed_repository_process_zero_authority.sql'"
            )
            stale_runtime_migration = await db.fetchval(
                "SELECT success FROM schema_migrations "
                "WHERE filename = "
                "'0197_non_pinned_workspace_process_zero.sql'"
            )
            lifecycle_migration = await db.fetchval(
                "SELECT success FROM schema_migrations "
                "WHERE filename = "
                "'0198_non_pinned_workspace_lifecycle_authority.sql'"
            )
            dirty_migrations = await db.fetchval(
                "SELECT count(*) FROM schema_migrations WHERE success = FALSE"
            )
            post_auto_pull = await db.fetchval(
                "SELECT count(*) FROM project_officers "
                "WHERE config_override #> '{officer,auto_pull}' = 'true'::jsonb"
            )
            thread_auto_pull = await db.fetchval(
                "SELECT count(*) FROM project_officers po "
                "JOIN threads t ON t.id = po.thread_id "
                "WHERE t.metadata #> '{config_override,officer,auto_pull}' "
                "= 'true'::jsonb"
            )
            if table is None or creation_table is None or not hasattr(
                ContainerProvisioner, "_release_process_zero_finalizer"
            ) or not hasattr(
                ContainerProvisioner,
                "_managed_repository_process_zero_replay_is_current",
            ) or not hasattr(
                PostgresDB,
                "record_orphan_managed_repository_workspace_process_zero",
            ) or not hasattr(
                PostgresDB,
                "record_stale_managed_repository_workspace_process_zero",
            ) or not all(
                hasattr(PostgresDB, method)
                for method in (
                    "reserve_managed_repository_workspace_creation",
                    "mark_managed_repository_workspace_creation_started",
                    "managed_repository_workspace_creation_claim_is_current",
                    "authorize_managed_repository_workspace_creation_runtime",
                    "record_managed_repository_workspace_creation_resource",
                    "settle_managed_repository_workspace_creation_reservation",
                    "request_managed_repository_workspace_creation_cancellation",
                    "prepare_managed_repository_workspace_cleanup_intent",
                    "get_managed_repository_workspace_cleanup_intent",
                )
            ) or not hasattr(
                ContainerProvisioner,
                "request_workspace_creation_cancellation",
            ) or not hasattr(
                ContainerProvisioner,
                "prepare_workspace_cleanup_intent",
            ) or not hasattr(
                ContainerProvisioner,
                "reconcile_workspace_cleanup_intent",
            ) or orphan_migration is not True or stale_runtime_migration is not True or lifecycle_migration is not True or int(
                dirty_migrations or 0
            ) != 0 or int(post_auto_pull or 0) != 0 or int(
                thread_auto_pull or 0
            ) != 0:
                raise RuntimeError("gate artifact missing")
            print("READY")
            return
        owner_uuid = UUID(owner_id)
        if mode == "owner-inspect":
            counts = await db.fetchrow(
                "SELECT "
                "(SELECT count(*) FROM "
                "managed_repository_workspace_creation_reservations "
                "WHERE owner_kind = 'job' AND owner_id = $1 "
                "AND settled_at IS NULL) AS active_creations, "
                "(SELECT count(*) FROM "
                "managed_repository_workspace_cleanup_intents "
                "WHERE owner_kind = 'job' AND owner_id = $1 "
                "AND settled_at IS NULL) AS pending_cleanups, "
                "(SELECT count(*) FROM managed_repository_process_zero_receipts "
                "WHERE owner_kind = 'job' AND owner_id = $1) AS receipts",
                owner_uuid,
            )
            resource_rows = await db.fetch(
                "SELECT resource_kind, resource_uid::text AS resource_uid FROM ("
                "SELECT 'pod'::text AS resource_kind, pod_uid AS resource_uid "
                "FROM managed_repository_workspace_creation_reservations "
                "WHERE owner_kind = 'job' AND owner_id = $1 AND pod_uid IS NOT NULL "
                "UNION SELECT 'pvc'::text, pvc_uid "
                "FROM managed_repository_workspace_creation_reservations "
                "WHERE owner_kind = 'job' AND owner_id = $1 AND pvc_uid IS NOT NULL "
                "UNION SELECT 'service'::text, service_uid "
                "FROM managed_repository_workspace_creation_reservations "
                "WHERE owner_kind = 'job' AND owner_id = $1 "
                "AND service_uid IS NOT NULL "
                "UNION SELECT 'pod'::text, pod_uid "
                "FROM managed_repository_workspace_cleanup_intents "
                "WHERE owner_kind = 'job' AND owner_id = $1 AND pod_uid IS NOT NULL "
                "UNION SELECT 'pvc'::text, pvc_uid "
                "FROM managed_repository_workspace_cleanup_intents "
                "WHERE owner_kind = 'job' AND owner_id = $1 AND pvc_uid IS NOT NULL "
                "UNION SELECT 'service'::text, service_uid "
                "FROM managed_repository_workspace_cleanup_intents "
                "WHERE owner_kind = 'job' AND owner_id = $1 "
                "AND service_uid IS NOT NULL) AS resources "
                "ORDER BY resource_kind, resource_uid",
                owner_uuid,
            )
            resource_uids = {"pod": [], "pvc": [], "service": []}
            for resource_row in resource_rows:
                kind = str(resource_row["resource_kind"])
                resource_uids[kind].append(str(UUID(resource_row["resource_uid"])))
            active_reservation_ids = [
                str(UUID(value["id"]))
                for value in await db.fetch(
                    "SELECT id::text AS id FROM "
                    "managed_repository_workspace_creation_reservations "
                    "WHERE owner_kind = 'job' AND owner_id = $1 "
                    "AND settled_at IS NULL ORDER BY reservation_generation",
                    owner_uuid,
                )
            ]
            row = await db.fetchrow(
                "SELECT description, status::text AS status, context "
                "FROM jobs WHERE id = $1",
                owner_uuid,
            )
            if row is None:
                print(
                    "OWNER_INSPECT "
                    + json.dumps(
                        {
                            "owner_exists": False,
                            "runtime_incarnation": None,
                            "active_creations": int(counts["active_creations"]),
                            "pending_cleanups": int(counts["pending_cleanups"]),
                            "receipts": int(counts["receipts"]),
                            "owner_status": None,
                            "pod_uids": resource_uids["pod"],
                            "pvc_uids": resource_uids["pvc"],
                            "service_uids": resource_uids["service"],
                            "active_reservation_ids": active_reservation_ids,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                return
            if row.get("description") != "local managed repository residue gate":
                raise RuntimeError("owner identity refused")
            context = row.get("context")
            if isinstance(context, str):
                context = json.loads(context)
            runtime = (
                context.get("workspace_container")
                if isinstance(context, dict)
                else None
            )
            runtime_incarnation = (
                runtime.get("_runtime_incarnation")
                if isinstance(runtime, dict)
                else None
            )
            if runtime_incarnation is not None:
                runtime_incarnation = str(UUID(str(runtime_incarnation)))
            print(
                "OWNER_INSPECT "
                + json.dumps(
                    {
                        "owner_exists": True,
                        "runtime_incarnation": runtime_incarnation,
                        "active_creations": int(counts["active_creations"]),
                        "pending_cleanups": int(counts["pending_cleanups"]),
                        "receipts": int(counts["receipts"]),
                        "owner_status": str(row["status"]),
                        "pod_uids": resource_uids["pod"],
                        "pvc_uids": resource_uids["pvc"],
                        "service_uids": resource_uids["service"],
                        "active_reservation_ids": active_reservation_ids,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return
        if mode == "owner-create":
            result = await db.execute(
                "INSERT INTO jobs (id, description, status, context) "
                "VALUES ($1, 'local managed repository residue gate', "
                "'paused', '{}'::jsonb)",
                owner_uuid,
            )
            if result != "INSERT 0 1":
                raise RuntimeError("owner create refused")
            print("OWNER_CREATED")
            return
        if mode == "owner-reserve":
            creation_role = sys.argv[3]
            if creation_role not in {"predecessor", "successor"}:
                raise RuntimeError("creation role refused")
            claimant = f"k3d-residue-gate:{owner_id}:{creation_role}"
            reservation = await db.get_managed_repository_workspace_creation_result(
                owner_id,
                owner_kind="job",
                scope="workspace_container",
                claimant=claimant,
                operation_kind="create",
            )
            if not isinstance(reservation, dict) or reservation.get("settled_at") is None:
                reservation = await db.reserve_managed_repository_workspace_creation(
                    owner_id,
                    owner_kind="job",
                    scope="workspace_container",
                    claimant=claimant,
                    lease_seconds=1800,
                    operation_kind="create",
                    desired_manifest_digest="0" * 64,
                )
                if not isinstance(reservation, dict):
                    raise RuntimeError("creation reservation refused")
                reservation = await db.mark_managed_repository_workspace_creation_started(
                    owner_id,
                    owner_kind="job",
                    scope="workspace_container",
                    reservation_generation=int(
                        reservation["reservation_generation"]
                    ),
                    claimant=claimant,
                    claim_token=int(reservation["claim_token"]),
                )
                if not isinstance(reservation, dict):
                    raise RuntimeError("creation mutation edge refused")
            print(
                "OWNER_RESERVED "
                + json.dumps(
                    {
                        "reservation_id": str(reservation["id"]),
                        "generation": int(reservation["reservation_generation"]),
                        "claim_token": int(reservation["claim_token"]),
                        "settled": reservation.get("settled_at") is not None,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return
        if mode == "owner-creation-current":
            creation_role = sys.argv[3]
            if creation_role not in {"predecessor", "successor"}:
                raise RuntimeError("creation role refused")
            reservation_generation = int(sys.argv[4])
            claim_token = int(sys.argv[5])
            claimant = f"k3d-residue-gate:{owner_id}:{creation_role}"
            if not await db.managed_repository_workspace_creation_claim_is_current(
                owner_id,
                owner_kind="job",
                scope="workspace_container",
                reservation_generation=reservation_generation,
                claimant=claimant,
                claim_token=claim_token,
            ):
                raise RuntimeError("creation claim is not current")
            print("CREATION_CURRENT")
            return
        if mode == "owner-shared-resources":
            creation_role = sys.argv[3]
            if creation_role not in {"predecessor", "successor"}:
                raise RuntimeError("creation role refused")
            reservation_id = str(UUID(sys.argv[4]))
            reservation_generation = int(sys.argv[5])
            claim_token = int(sys.argv[6])
            gate_id = sys.argv[7]
            if not gate_id.startswith("srw-mr-residue-") or len(gate_id) != 27:
                raise RuntimeError("gate identity refused")
            claimant = f"k3d-residue-gate:{owner_id}:{creation_role}"
            provisioner = ContainerProvisioner()
            provisioner.connect(db)
            if not provisioner.is_available:
                raise RuntimeError("kubernetes provisioner unavailable")
            owner = WorkspaceOwner.job(owner_id)

            async def mutation_authority():
                return await db.managed_repository_workspace_creation_claim_is_current(
                    owner_id,
                    owner_kind="job",
                    scope="workspace_container",
                    reservation_generation=reservation_generation,
                    claimant=claimant,
                    claim_token=claim_token,
                )

            pvc_name = _pvc_name_for(owner)
            pvc_result = await provisioner._create_pvc(
                pvc_name,
                size="64Mi",
                labels={owner.label_key: owner.id, GATE_LABEL: gate_id},
                expected_owner=owner,
                creation_reservation_id=reservation_id,
                mutation_authority=mutation_authority,
            )
            if pvc_result not in {"created", "reused"}:
                raise RuntimeError("shared PVC creation refused")
            pvc = await asyncio.to_thread(
                provisioner._core_api.read_namespaced_persistent_volume_claim,
                name=pvc_name,
                namespace=provisioner._namespace,
            )
            pvc_uid = provisioner._require_stateless_pvc_identity(
                pvc,
                owner=owner,
                pvc_name=pvc_name,
                allow_any_storage_class=True,
            )
            pvc_labels = getattr(getattr(pvc, "metadata", None), "labels", None)
            pvc_annotations = getattr(
                getattr(pvc, "metadata", None), "annotations", None
            )
            if not isinstance(pvc_labels, dict) or pvc_labels.get(GATE_LABEL) != gate_id:
                raise RuntimeError("shared PVC gate identity refused")
            if pvc_result == "created":
                provisioner._require_workspace_creation_reservation_annotation(
                    pvc,
                    reservation_id=reservation_id,
                )
            elif not (
                isinstance(pvc_annotations, dict)
                and pvc_annotations.get(WORKSPACE_CREATION_RESERVATION_ANNOTATION)
                == reservation_id
            ) and not bool(
                await db.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM "
                    "managed_repository_workspace_creation_reservations "
                    "WHERE owner_kind = 'job' AND owner_id = $1 "
                    "AND settled_at IS NOT NULL AND pvc_uid = $2::uuid)",
                    owner_uuid,
                    pvc_uid,
                )
            ):
                raise RuntimeError("unrecorded shared PVC reuse refused")
            if not await db.record_managed_repository_workspace_creation_resource(
                owner_id,
                owner_kind="job",
                scope="workspace_container",
                reservation_generation=reservation_generation,
                claimant=claimant,
                claim_token=claim_token,
                resource_kind="pvc",
                resource_uid=pvc_uid,
            ):
                raise RuntimeError("shared PVC recording refused")

            if not await provisioner._create_service(
                owner,
                require_exact_owner=True,
                creation_reservation_id=reservation_id,
                mutation_authority=mutation_authority,
            ):
                raise RuntimeError("shared Service creation refused")
            service = await asyncio.to_thread(
                provisioner._core_api.read_namespaced_service,
                name=owner.pod_name,
                namespace=provisioner._namespace,
            )
            service_uid = provisioner._require_stateless_service_identity(
                service,
                owner=owner,
            )
            service_metadata = getattr(service, "metadata", None)
            service_labels = getattr(service_metadata, "labels", None)
            annotations = getattr(service_metadata, "annotations", None)
            if not isinstance(service_labels, dict):
                raise RuntimeError("shared Service labels refused")
            if service_labels.get(GATE_LABEL) is None:
                if (
                    creation_role != "predecessor"
                    or not isinstance(annotations, dict)
                    or annotations.get(WORKSPACE_CREATION_RESERVATION_ANNOTATION)
                    != reservation_id
                ):
                    raise RuntimeError("unlabelled shared Service reuse refused")
                resource_version = str(
                    getattr(service_metadata, "resource_version", "") or ""
                )
                if not resource_version:
                    raise RuntimeError("shared Service resource version refused")
                patched_labels = dict(service_labels)
                patched_labels[GATE_LABEL] = gate_id
                await asyncio.to_thread(
                    provisioner._core_api.patch_namespaced_service,
                    name=owner.pod_name,
                    namespace=provisioner._namespace,
                    body={
                        "metadata": {
                            "resourceVersion": resource_version,
                            "labels": patched_labels,
                        }
                    },
                )
                service = await asyncio.to_thread(
                    provisioner._core_api.read_namespaced_service,
                    name=owner.pod_name,
                    namespace=provisioner._namespace,
                )
                confirmed_uid = provisioner._require_stateless_service_identity(
                    service,
                    owner=owner,
                )
                confirmed_labels = getattr(
                    getattr(service, "metadata", None), "labels", None
                )
                if (
                    confirmed_uid != service_uid
                    or not isinstance(confirmed_labels, dict)
                    or confirmed_labels.get(GATE_LABEL) != gate_id
                ):
                    raise RuntimeError("shared Service label confirmation refused")
            elif service_labels.get(GATE_LABEL) != gate_id:
                raise RuntimeError("shared Service gate identity refused")
            elif not (
                isinstance(annotations, dict)
                and annotations.get(WORKSPACE_CREATION_RESERVATION_ANNOTATION)
                == reservation_id
            ) and not bool(
                await db.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM "
                    "managed_repository_workspace_creation_reservations "
                    "WHERE owner_kind = 'job' AND owner_id = $1 "
                    "AND settled_at IS NOT NULL AND service_uid = $2::uuid)",
                    owner_uuid,
                    service_uid,
                )
            ):
                raise RuntimeError("unrecorded shared Service reuse refused")
            if not await db.record_managed_repository_workspace_creation_resource(
                owner_id,
                owner_kind="job",
                scope="workspace_container",
                reservation_generation=reservation_generation,
                claimant=claimant,
                claim_token=claim_token,
                resource_kind="service",
                resource_uid=service_uid,
            ):
                raise RuntimeError("shared Service recording refused")
            print(
                "SHARED_RESOURCES "
                + json.dumps(
                    {
                        "pvc_name": pvc_name,
                        "pvc_uid": pvc_uid,
                        "service_name": owner.pod_name,
                        "service_uid": service_uid,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return
        if mode == "owner-bind":
            creation_role = sys.argv[3]
            if creation_role not in {"predecessor", "successor"}:
                raise RuntimeError("creation role refused")
            runtime_incarnation = str(UUID(sys.argv[4]))
            pod_name = sys.argv[5]
            namespace = sys.argv[6]
            reservation_id = str(UUID(sys.argv[7]))
            reservation_generation = int(sys.argv[8])
            claim_token = int(sys.argv[9])
            claimant = f"k3d-residue-gate:{owner_id}:{creation_role}"
            provisioner = ContainerProvisioner()
            provisioner.connect(db)
            if not provisioner.is_available or provisioner._namespace != namespace:
                raise RuntimeError("kubernetes provisioner unavailable")
            owner = WorkspaceOwner.job(owner_id)
            pod = await asyncio.to_thread(
                provisioner._core_api.read_namespaced_pod,
                name=pod_name,
                namespace=namespace,
            )
            observed = provisioner._require_workspace_pod_owner(
                pod,
                owner=owner,
                allow_owner_unlabeled=False,
                allow_terminating=False,
                expected_pod_name=pod_name,
                expected_component="workspace",
            )
            provisioner._require_workspace_creation_reservation_annotation(
                pod,
                reservation_id=reservation_id,
            )
            pod_ip = str(getattr(getattr(pod, "status", None), "pod_ip", "") or "")
            if observed != runtime_incarnation or not pod_ip:
                raise RuntimeError("workspace Pod identity refused")
            provisioner._require_stateless_pod_storage_binding(
                pod,
                owner=owner,
                expected_pvc_name=_pvc_name_for(owner),
                expected_seed_configmap=None,
                expected_pod_name=pod_name,
            )
            reservation = await db.get_managed_repository_workspace_creation_result(
                owner_id,
                owner_kind="job",
                scope="workspace_container",
                claimant=claimant,
                operation_kind="create",
            )
            if (
                not isinstance(reservation, dict)
                or str(reservation.get("id") or "") != reservation_id
                or int(reservation.get("reservation_generation") or 0)
                != reservation_generation
                or int(reservation.get("claim_token") or 0) != claim_token
            ):
                raise RuntimeError("creation reservation changed")
            already_settled = reservation.get("settled_at") is not None
            if already_settled and (
                str(reservation.get("runtime_incarnation") or "")
                != runtime_incarnation
                or str(reservation.get("result_kind") or "") != "settled"
            ):
                raise RuntimeError("settled creation runtime changed")
            if (
                not already_settled
                and not await db.authorize_managed_repository_workspace_creation_runtime(
                    owner_id,
                    owner_kind="job",
                    scope="workspace_container",
                    reservation_generation=reservation_generation,
                    claimant=claimant,
                    claim_token=claim_token,
                    runtime_incarnation=runtime_incarnation,
                )
            ):
                raise RuntimeError("creation runtime authorization refused")
            state = json.dumps(
                {
                    "provisioner": "k8s",
                    "status": "ready",
                    "pod_name": pod_name,
                    "container_name": "workspace",
                    "namespace": namespace,
                    "pod_ip": pod_ip,
                    "port": 30022,
                    "_runtime_incarnation": runtime_incarnation,
                    "_creation_reservation_id": reservation_id,
                    "_creation_claim_token": str(claim_token),
                }
            )
            existing = await db.fetchrow(
                "SELECT context FROM jobs WHERE id = $1", owner_uuid
            )
            raw_context = existing.get("context") if existing else None
            if isinstance(raw_context, str):
                raw_context = json.loads(raw_context)
            prior = (
                raw_context.get("workspace_container")
                if isinstance(raw_context, dict)
                else None
            )
            exact_replay = bool(
                isinstance(prior, dict)
                and prior.get("_runtime_incarnation") == runtime_incarnation
                and prior.get("_creation_reservation_id") == reservation_id
                and prior.get("_creation_claim_token") == str(claim_token)
                and prior.get("status") == "ready"
            )
            if not exact_replay:
                result = await db.execute(
                    "UPDATE jobs SET context = jsonb_set("
                    "COALESCE(context, '{}'::jsonb), "
                    "'{workspace_container}', $2::jsonb, true) "
                    "WHERE id = $1 AND ("
                    "NOT COALESCE(context, '{}'::jsonb) ? 'workspace_container' "
                    "OR context #>> '{workspace_container,status}' "
                    "IN ('deleted', 'suspended', 'expired'))",
                    owner_uuid,
                    state,
                )
                if result != "UPDATE 1":
                    raise RuntimeError("owner bind refused")
            if (
                not already_settled
                and not await db.settle_managed_repository_workspace_creation_reservation(
                    owner_id,
                    owner_kind="job",
                    scope="workspace_container",
                    reservation_generation=reservation_generation,
                    claimant=claimant,
                    claim_token=claim_token,
                    runtime_incarnation=runtime_incarnation,
                )
            ):
                raise RuntimeError("creation settlement refused")
            print("OWNER_BOUND")
            return
        if mode == "owner-terminal":
            async with db.acquire() as conn:
                async with conn.transaction():
                    owner = await conn.fetchrow(
                        "SELECT description, status::text AS status FROM jobs "
                        "WHERE id = $1 FOR UPDATE",
                        owner_uuid,
                    )
                    if owner is None or owner.get("description") != (
                        "local managed repository residue gate"
                    ):
                        raise RuntimeError("owner identity refused")
                    if str(owner.get("status") or "") != "cancelled":
                        result = await conn.execute(
                            "UPDATE jobs SET status = 'cancelled' WHERE id = $1",
                            owner_uuid,
                        )
                        if result != "UPDATE 1":
                            raise RuntimeError("owner terminal transition refused")
            print("OWNER_TERMINAL")
            return
        if mode == "workspace-prepare":
            expected_uid = str(UUID(sys.argv[3]))
            reclaim_shared_resources = sys.argv[4] == "terminal"
            if sys.argv[4] not in {"preserve", "terminal"}:
                raise RuntimeError("cleanup policy refused")
            provisioner = ContainerProvisioner()
            provisioner.connect(db)
            if not provisioner.is_available:
                raise RuntimeError("kubernetes provisioner unavailable")
            intent = await provisioner.prepare_workspace_cleanup_intent(
                WorkspaceOwner.job(owner_id),
                expected_runtime_incarnation=expected_uid,
                target_disposition="deleted",
                reclaim_shared_resources=reclaim_shared_resources,
            )
            if (
                not isinstance(intent, dict)
                or intent.get("resources_captured_at") is None
                or str(intent.get("pod_uid") or "") != expected_uid
                or str(intent.get("resource_policy") or "")
                != ("terminal_reclaim" if reclaim_shared_resources else "preserve")
            ):
                raise RuntimeError("workspace cleanup preparation refused")
            print(
                "WORKSPACE_PREPARED "
                + json.dumps(
                    {
                        "generation": int(intent["intent_generation"]),
                        "pvc_uid": (
                            str(intent["pvc_uid"])
                            if intent.get("pvc_uid") is not None
                            else None
                        ),
                        "service_uid": (
                            str(intent["service_uid"])
                            if intent.get("service_uid") is not None
                            else None
                        ),
                        "resource_policy": str(intent["resource_policy"]),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return
        if mode == "owner-cancel-creation":
            provisioner = ContainerProvisioner()
            provisioner.connect(db)
            if not provisioner.is_available:
                raise RuntimeError("kubernetes provisioner unavailable")
            owner = WorkspaceOwner.job(owner_id)
            terminal_owner = (
                await db.fetchval(
                    "SELECT status::text = 'cancelled' FROM jobs WHERE id = $1",
                    owner_uuid,
                )
                is True
            )
            cancellation = await provisioner.request_workspace_creation_cancellation(
                owner,
                target_disposition="deleted",
                reclaim_shared_resources=terminal_owner,
            )
            if isinstance(cancellation, dict) and cancellation.get(
                "reconciliation_outcome"
            ) == "handed_off":
                runtime_incarnation = str(
                    cancellation.get("runtime_incarnation") or ""
                )
                intent = await db.get_managed_repository_workspace_cleanup_intent(
                    owner_id,
                    owner_kind="job",
                    scope="workspace_container",
                    runtime_incarnation=runtime_incarnation,
                )
                if not isinstance(intent, dict):
                    raise RuntimeError("cancelled creation intent unavailable")
                outcome = await provisioner.reconcile_workspace_cleanup_intent(
                    owner,
                    expected_runtime_incarnation=runtime_incarnation,
                    intent_generation=int(intent["intent_generation"]),
                )
                if not (outcome.settled or outcome.superseded):
                    raise RuntimeError("cancelled creation cleanup retryable")
            active = await db.fetchval(
                "SELECT count(*) FROM "
                "managed_repository_workspace_creation_reservations "
                "WHERE owner_kind = 'job' AND owner_id = $1 "
                "AND settled_at IS NULL",
                owner_uuid,
            )
            pending = await db.fetch(
                "SELECT scope, runtime_incarnation, intent_generation FROM "
                "managed_repository_workspace_cleanup_intents "
                "WHERE owner_kind = 'job' AND owner_id = $1 "
                "AND settled_at IS NULL ORDER BY intent_generation",
                owner_uuid,
            )
            for intent in pending:
                if str(intent.get("scope") or "") != "workspace_container":
                    raise RuntimeError("unexpected disposable cleanup scope")
                outcome = await provisioner.reconcile_workspace_cleanup_intent(
                    owner,
                    expected_runtime_incarnation=str(
                        intent["runtime_incarnation"]
                    ),
                    intent_generation=int(intent["intent_generation"]),
                )
                if not (outcome.settled or outcome.superseded):
                    raise RuntimeError("disposable cleanup remains retryable")
            pending_cleanup = await db.fetchval(
                "SELECT count(*) FROM managed_repository_workspace_cleanup_intents "
                "WHERE owner_kind = 'job' AND owner_id = $1 "
                "AND settled_at IS NULL",
                owner_uuid,
            )
            if int(active or 0) or int(pending_cleanup or 0):
                raise RuntimeError("creation cancellation remains pending")
            print("CREATION_CANCELLED")
            return
        if mode in {
            "pod-delete",
            "pod-delete-stale",
            "finalizer-release",
            "finalizer-release-lost",
            "workspace-delete-current",
            "workspace-delete-replay",
            "workspace-delete-settle",
            "workspace-reconcile",
            "finalizer-diagnose",
            "receipt-diagnose",
            "teardown-diagnose",
            "teardown-release",
        }:
            expected_uid = str(UUID(sys.argv[3]))
            pod_name = sys.argv[4]
            namespace = sys.argv[5]
            provisioner = ContainerProvisioner()
            provisioner.connect(db)
            if not provisioner.is_available or provisioner._namespace != namespace:
                raise RuntimeError("kubernetes provisioner unavailable")
            owner = WorkspaceOwner.job(owner_id)
            if mode in {
                "workspace-delete-current",
                "workspace-delete-replay",
                "workspace-delete-settle",
                "workspace-reconcile",
            }:
                if mode == "workspace-reconcile":
                    intent_generation = int(sys.argv[6])
                    outcome = await provisioner.reconcile_workspace_cleanup_intent(
                        owner,
                        expected_runtime_incarnation=expected_uid,
                        intent_generation=intent_generation,
                    )
                    if not (outcome.settled or outcome.superseded):
                        raise RuntimeError("workspace cleanup reconciliation refused")
                    print(
                        "WORKSPACE_RECONCILED "
                        f"outcome={outcome.state} "
                        f"generation={outcome.intent_generation}"
                    )
                    return
                replayed = await provisioner.delete_workspace_with_outcome(
                    owner,
                    expected_runtime_incarnation=expected_uid,
                    wait_for_exact_absence=True,
                )
                accepted = (
                    replayed.current_deleted
                    if mode == "workspace-delete-current"
                    else (
                        replayed.stale_target_settled
                        if mode == "workspace-delete-replay"
                        else (
                            replayed.current_deleted
                            or replayed.stale_target_settled
                        )
                    )
                )
                if not accepted:
                    raise RuntimeError("workspace delete replay refused")
                print(
                    "WORKSPACE_DELETE_CURRENT"
                    if mode == "workspace-delete-current"
                    else (
                        "WORKSPACE_DELETE_REPLAYED"
                        if mode == "workspace-delete-replay"
                        else "WORKSPACE_DELETE_SETTLED"
                    )
                )
                return
            if mode == "teardown-diagnose":
                identity = await provisioner.capture_terminal_workspace_identity(owner)
                print(
                    "TEARDOWN_DIAGNOSE "
                    f"pod_uid={str(identity.pod_uid or 'missing')} "
                    f"pod_ip={str(bool(identity.pod_ip)).lower()} "
                    f"host_fingerprint={str(bool(identity.ssh_host_key_fingerprint)).lower()} "
                    f"pvc_uid={str(bool(identity.pvc_uid)).lower()} "
                    f"service_uid={str(bool(identity.service_uid)).lower()}"
                )
                return
            if mode == "teardown-release":
                identity = await provisioner.capture_terminal_workspace_identity(owner)
                released = await provisioner.release_workspace(
                    owner,
                    teardown_identity=identity,
                    strict=True,
                )
                print(f"TEARDOWN_RELEASE released={str(bool(released)).lower()}")
                return
            if mode == "finalizer-diagnose":
                pod = await asyncio.to_thread(
                    provisioner._core_api.read_namespaced_pod,
                    name=pod_name,
                    namespace=namespace,
                )
                observed = provisioner._require_workspace_pod_owner(
                    pod,
                    owner=owner,
                    allow_owner_unlabeled=False,
                    allow_terminating=True,
                    expected_pod_name=pod_name,
                    expected_component="workspace",
                )
                finalizers = getattr(pod.metadata, "finalizers", None) or []
                print(
                    "FINALIZER_DIAGNOSE "
                    f"uid_match={str(observed == expected_uid).lower()} "
                    f"process_zero={str(_pod_has_exact_process_zero(pod)).lower()} "
                    f"finalizer_count={finalizers.count(STATELESS_WORKSPACE_PROCESS_ZERO_FINALIZER)}"
                )
                return
            if mode == "receipt-diagnose":
                owner_row = await db.fetchrow(
                    "SELECT context FROM jobs WHERE id = $1",
                    owner_uuid,
                )
                raw_context_value = owner_row.get("context") if owner_row else None
                if isinstance(raw_context_value, str):
                    raw_context_value = json.loads(raw_context_value)
                raw_context = (
                    dict(raw_context_value)
                    if isinstance(raw_context_value, dict)
                    else {}
                )
                raw_runtime = raw_context.get("workspace_container")
                runtime = raw_runtime if isinstance(raw_runtime, dict) else {}
                stored_runtime = str(runtime.get("_runtime_incarnation") or "")
                stored_receipt = bool(
                    stored_runtime
                    and await db.fetchval(
                        "SELECT EXISTS (SELECT 1 "
                        "FROM managed_repository_process_zero_receipts "
                        "WHERE owner_kind = 'job' AND owner_id = $1 "
                        "AND scope = 'workspace_container' "
                        "AND runtime_incarnation = $2)",
                        owner_uuid,
                        stored_runtime,
                    )
                )
                claimed = await db.claim_managed_repository_workspace_retirement(
                    owner_id,
                    owner_kind="job",
                    scope="workspace_container",
                    provisioner="k8s",
                    runtime_incarnation=expected_uid,
                )
                recorded = False
                if claimed:
                    recorded = await db.record_managed_repository_workspace_process_zero(
                        owner_id,
                        owner_kind="job",
                        scope="workspace_container",
                        provisioner="k8s",
                        runtime_incarnation=expected_uid,
                    )
                print(
                    "RECEIPT_DIAGNOSE "
                    f"owner_exists={str(owner_row is not None).lower()} "
                    f"runtime_object={str(isinstance(raw_runtime, dict)).lower()} "
                    f"provisioner_k8s={str(runtime.get('provisioner') == 'k8s').lower()} "
                    f"stored_runtime={stored_runtime or 'missing'} "
                    f"stored_receipt={str(stored_receipt).lower()} "
                    f"expected_runtime={expected_uid} "
                    f"runtime_match={str(runtime.get('_runtime_incarnation') == expected_uid).lower()} "
                    f"status={str(runtime.get('status') or 'missing')} "
                    f"claimed={str(bool(claimed)).lower()} "
                    f"recorded={str(bool(recorded)).lower()}"
                )
                return
            if mode in {"pod-delete", "pod-delete-stale"}:
                pod = await asyncio.to_thread(
                    provisioner._core_api.read_namespaced_pod,
                    name=pod_name,
                    namespace=namespace,
                )
                observed = provisioner._require_workspace_pod_owner(
                    pod,
                    owner=owner,
                    allow_owner_unlabeled=False,
                    allow_terminating=True,
                    expected_pod_name=pod_name,
                    expected_component="workspace",
                )
                if observed != expected_uid:
                    raise RuntimeError("pod replacement refused")
                if mode == "pod-delete" and not await db.claim_managed_repository_workspace_retirement(
                    owner_id,
                    owner_kind="job",
                    scope="workspace_container",
                    provisioner="k8s",
                    runtime_incarnation=expected_uid,
                ):
                    raise RuntimeError("process-zero retirement claim refused")
                await asyncio.to_thread(
                    provisioner._core_api.delete_namespaced_pod,
                    name=pod_name,
                    namespace=namespace,
                    grace_period_seconds=5,
                    body={"preconditions": {"uid": expected_uid}},
                )
                print("POD_DELETE_ACCEPTED")
                return
            released = await provisioner._release_process_zero_finalizer(
                owner,
                pod_name=pod_name,
                expected_runtime_incarnation=expected_uid,
                scope="workspace_container",
                expected_component="workspace",
            )
            current = await db.managed_repository_workspace_process_zero_is_current(
                owner_id,
                owner_kind="job",
                scope="workspace_container",
                provisioner="k8s",
                runtime_incarnation=expected_uid,
            )
            if not current:
                current = await db.orphan_managed_repository_workspace_process_zero_is_current(
                    owner.id,
                    owner_kind=("thread" if owner.kind == "session" else "job"),
                    scope="workspace_container",
                    provisioner="k8s",
                    runtime_incarnation=expected_uid,
                )
            if not current:
                current = await db.stale_managed_repository_workspace_process_zero_is_current(
                    owner.id,
                    owner_kind=("thread" if owner.kind == "session" else "job"),
                    scope="workspace_container",
                    provisioner="k8s",
                    runtime_incarnation=expected_uid,
                )
            if not released or not current:
                print(
                    "FINALIZER_REFUSED "
                    f"released={str(bool(released)).lower()} "
                    f"current={str(bool(current)).lower()}"
                )
                raise RuntimeError("process-zero finalizer release refused")
            if mode == "finalizer-release-lost":
                # The finalizer patch and receipt have committed. Model a lost
                # controller response so the next attempt must reconcile Pod
                # 404 through the exact stale-runtime receipt.
                raise RuntimeError("simulated committed response loss")
            print("FINALIZER_RELEASED")
            return
        if mode == "owner-delete":
            async with db.acquire() as conn:
                async with conn.transaction():
                    owner = await conn.fetchrow(
                        "SELECT description FROM jobs WHERE id = $1 FOR UPDATE",
                        owner_uuid,
                    )
                    if owner is not None and owner.get("description") != (
                        "local managed repository residue gate"
                    ):
                        raise RuntimeError("owner identity refused")
                    result = await conn.execute(
                        "DELETE FROM jobs WHERE id = $1", owner_uuid
                    )
                    if result not in {"DELETE 1", "DELETE 0"}:
                        raise RuntimeError("owner delete refused")
                    # These ledgers intentionally outlive production owners.
                    # This disposable gate removes only its exact, fully
                    # settled audit rows after the owner-delete trigger has
                    # independently accepted the lifecycle.
                    await conn.execute(
                        "DELETE FROM managed_repository_workspace_creation_reservations "
                        "WHERE owner_kind = 'job' AND owner_id = $1 "
                        "AND settled_at IS NOT NULL",
                        owner_uuid,
                    )
                    await conn.execute(
                        "DELETE FROM managed_repository_workspace_cleanup_intents "
                        "WHERE owner_kind = 'job' AND owner_id = $1 "
                        "AND settled_at IS NOT NULL",
                        owner_uuid,
                    )
                    await conn.execute(
                        "DELETE FROM managed_repository_process_zero_receipts "
                        "WHERE owner_kind = 'job' AND owner_id = $1",
                        owner_uuid,
                    )
                    residue = await conn.fetchval(
                        "SELECT "
                        "(SELECT count(*) FROM "
                        "managed_repository_workspace_creation_reservations "
                        "WHERE owner_kind = 'job' AND owner_id = $1) + "
                        "(SELECT count(*) FROM "
                        "managed_repository_workspace_cleanup_intents "
                        "WHERE owner_kind = 'job' AND owner_id = $1) + "
                        "(SELECT count(*) FROM "
                        "managed_repository_process_zero_receipts "
                        "WHERE owner_kind = 'job' AND owner_id = $1)",
                        owner_uuid,
                    )
                    if int(residue or 0) != 0:
                        raise RuntimeError("owner ledger cleanup refused")
            print("OWNER_DELETED")
            return
        raise RuntimeError("unknown gate controller mode")
    finally:
        await db.close()


asyncio.run(run())
"""


def controller_program() -> str:
    return _CONTROLLER_PROGRAM


class ManagedRepositoryResidueGate:
    def __init__(self, config: GateConfig, runner: KubectlRunner) -> None:
        self.config = config
        self.runner = runner
        self.report = SafeReport(gate_id=config.gate_id, mode="run")
        self.orchestrator_pod: str | None = None
        self.orchestrator_pods: list[str] = []
        self.stateless_pods: list[str] = []
        self.workspace_image: str | None = config.workspace_image
        self.owner_created = False
        self.pod_uid: str | None = None
        self.owner_runtime_uid: str | None = None
        self.owner_bound = False
        self.creation_envelope: CreationEnvelope | None = None
        self.creation_role: str | None = None
        self.shared_resources: SharedResourceEnvelope | None = None
        self.predecessor_uid: str | None = None
        self.predecessor_cleanup_generation: int | None = None
        self.private_home = config.home_path

    @staticmethod
    def _decode_json(output: bytes, *, operation: str) -> dict[str, Any]:
        try:
            value = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GateFailure(f"{operation} returned malformed JSON") from exc
        if not isinstance(value, dict):
            raise GateFailure(f"{operation} returned malformed JSON")
        return value

    def _pod_json(self) -> dict[str, Any] | None:
        result = self.runner.run(
            ["get", "pod", self.config.pod_name, "-o", "json", "--ignore-not-found"],
            operation="workspace Pod inspection",
        )
        if not result.stdout.strip():
            return None
        return self._decode_json(result.stdout, operation="workspace Pod inspection")

    def _resource_json(self, kind: str, name: str) -> dict[str, Any] | None:
        if kind not in {"persistentvolumeclaim", "service", "endpoints"}:
            raise SafetyError("unsupported disposable resource kind")
        result = self.runner.run(
            ["get", kind, name, "-o", "json", "--ignore-not-found"],
            operation=f"disposable {kind} inspection",
        )
        if not result.stdout.strip():
            return None
        return self._decode_json(
            result.stdout,
            operation=f"disposable {kind} inspection",
        )

    def _exact_pvc_uid(self, claim: dict[str, Any] | None = None) -> str:
        claim = claim or self._resource_json(
            "persistentvolumeclaim", self.config.pvc_name
        )
        if claim is None:
            raise GateFailure("shared workspace PVC is unavailable")
        claim_metadata = claim.get("metadata") or {}
        claim_spec = claim.get("spec") or {}
        expected_owner_labels = {
            "app": "srw-workspace",
            "srw.io/component": "agent-workspace",
            "srw/job-id": self.config.owner_id,
            GATE_LABEL: self.config.gate_id,
        }
        claim_labels = claim_metadata.get("labels") or {}
        if (
            claim_metadata.get("name") != self.config.pvc_name
            or claim_metadata.get("namespace") != self.config.namespace
            or claim_metadata.get("deletionTimestamp") is not None
            or any(
                claim_labels.get(key) != value
                for key, value in expected_owner_labels.items()
            )
            or claim_labels.get("srw/component") != "workspace-pvc"
            or "srw/thread-id" in claim_labels
            or claim_spec.get("accessModes") != ["ReadWriteOnce"]
            or claim_spec.get("volumeMode") not in {None, "Filesystem"}
        ):
            raise GateFailure("shared workspace PVC identity changed")
        try:
            return str(UUID(str(claim_metadata.get("uid") or "")))
        except ValueError as exc:
            raise GateFailure("shared workspace PVC UID is malformed") from exc

    def _exact_service_uid(
        self,
        service: dict[str, Any] | None = None,
        *,
        allow_unlabelled_active_reservation: bool = False,
    ) -> str:
        service = service or self._resource_json("service", self.config.pod_name)
        if service is None:
            raise GateFailure("shared workspace Service is unavailable")
        service_metadata = service.get("metadata") or {}
        service_spec = service.get("spec") or {}
        expected_owner_labels = {
            "app": "srw-workspace",
            "srw.io/component": "agent-workspace",
            "srw/job-id": self.config.owner_id,
        }
        service_labels = service_metadata.get("labels") or {}
        expected_ports = {
            ("ssh", 30022, 30022, "TCP"),
            ("code-server", 38080, 38080, "TCP"),
            ("cdp", 9222, 9222, "TCP"),
        }
        observed_ports = {
            (
                item.get("name"),
                item.get("port"),
                item.get("targetPort"),
                item.get("protocol"),
            )
            for item in service_spec.get("ports") or []
            if isinstance(item, dict)
        }
        if (
            service_metadata.get("name") != self.config.pod_name
            or service_metadata.get("namespace") != self.config.namespace
            or service_metadata.get("deletionTimestamp") is not None
            or any(
                service_labels.get(key) != value
                for key, value in expected_owner_labels.items()
            )
            or (
                service_labels.get(GATE_LABEL) != self.config.gate_id
                and not (
                    allow_unlabelled_active_reservation
                    and GATE_LABEL not in service_labels
                )
            )
            or service_labels.get("srw/component") != "workspace-svc"
            or "srw/thread-id" in service_labels
            or service_spec.get("clusterIP") != "None"
            or service_spec.get("selector")
            != {"app": "srw-workspace", "srw/job-id": self.config.owner_id}
            or observed_ports != expected_ports
        ):
            raise GateFailure("shared workspace Service identity changed")
        try:
            return str(UUID(str(service_metadata.get("uid") or "")))
        except ValueError as exc:
            raise GateFailure("shared workspace Service UID is malformed") from exc

    def _exact_shared_resource_uids(self) -> tuple[str, str]:
        """Authenticate the exact gate PVC and Service and return their UIDs."""

        return self._exact_pvc_uid(), self._exact_service_uid()

    def _assert_shared_resources_current(
        self,
        *,
        expected_pod_uid: str,
        require_successor_marker: bool,
    ) -> None:
        expected = self.shared_resources
        if expected is None:
            raise GateFailure("shared workspace identities were not captured")
        pvc_uid, service_uid = self._exact_shared_resource_uids()
        if (
            expected.pvc_name != self.config.pvc_name
            or expected.service_name != self.config.pod_name
            or pvc_uid != expected.pvc_uid
            or service_uid != expected.service_uid
        ):
            raise GateFailure("shared workspace resource generation changed")
        marker = f".srw-gate-{self.config.gate_id}-predecessor"
        successor_marker = f".srw-gate-{self.config.gate_id}-successor"
        marker_check = f"test -f /home/agent-host/{marker!s}"
        if require_successor_marker:
            marker_check += f"; test -f /home/agent-host/{successor_marker!s}"
        self._pod_exec(marker_check, operation="shared PVC continuity inspection")

        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            endpoints = self._resource_json("endpoints", self.config.pod_name)
            targets = {
                str((address.get("targetRef") or {}).get("uid") or "")
                for subset in (endpoints or {}).get("subsets") or []
                if isinstance(subset, dict)
                for address in subset.get("addresses") or []
                if isinstance(address, dict)
            }
            if targets == {expected_pod_uid}:
                return
            if targets - {expected_pod_uid}:
                raise GateFailure("shared Service selected a foreign Pod generation")
            time.sleep(1)
        raise GateFailure("shared Service did not select the exact successor Pod")

    def _deployment_pods(
        self, deployment_name: str, *, container_name: str
    ) -> list[str]:
        """Require one completely converged Deployment and return every Pod."""

        deployment = self._decode_json(
            self.runner.run(
                [
                    "get",
                    "deployment",
                    deployment_name,
                    "-o",
                    "json",
                ],
                operation=f"{container_name} deployment inspection",
            ).stdout,
            operation=f"{container_name} deployment inspection",
        )
        metadata = deployment.get("metadata") or {}
        spec = deployment.get("spec") or {}
        status = deployment.get("status") or {}
        generation = metadata.get("generation")
        replicas = spec.get("replicas", 1)
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
            or isinstance(replicas, bool)
            or not isinstance(replicas, int)
            or replicas <= 0
            or int(status.get("observedGeneration") or 0) < generation
            or int(status.get("updatedReplicas") or 0) != replicas
            or int(status.get("readyReplicas") or 0) != replicas
            or int(status.get("availableReplicas") or 0) != replicas
            or int(status.get("unavailableReplicas") or 0) != 0
        ):
            raise GateFailure(f"{container_name} deployment is not converged")
        template_containers = ((spec.get("template") or {}).get("spec") or {}).get(
            "containers"
        ) or []
        desired = [
            item
            for item in template_containers
            if isinstance(item, dict) and item.get("name") == container_name
        ]
        if len(desired) != 1 or not isinstance(desired[0].get("image"), str):
            raise GateFailure(f"{container_name} deployment image is unavailable")
        desired_image = desired[0]["image"]
        selector = deployment.get("spec", {}).get("selector", {}).get("matchLabels")
        if not isinstance(selector, dict) or not selector:
            raise GateFailure(f"{container_name} deployment selector is unavailable")
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in selector.items()
        ):
            raise GateFailure(f"{container_name} deployment selector is malformed")
        selector_text = ",".join(
            f"{key}={value}" for key, value in sorted(selector.items())
        )
        pods = self._decode_json(
            self.runner.run(
                ["get", "pods", "-l", selector_text, "-o", "json"],
                operation=f"{container_name} Pod inventory",
            ).stdout,
            operation=f"{container_name} Pod inventory",
        ).get("items")
        if not isinstance(pods, list):
            raise GateFailure(f"{container_name} Pod inventory is malformed")
        candidates: list[str] = []
        template_hashes: set[str] = set()
        image_ids: set[str] = set()
        for pod in pods:
            if not isinstance(pod, dict):
                raise GateFailure(f"{container_name} Pod inventory is malformed")
            metadata = pod.get("metadata") or {}
            pod_spec = pod.get("spec") or {}
            status = pod.get("status") or {}
            statuses = status.get("containerStatuses") or []
            declared = [
                item
                for item in pod_spec.get("containers") or []
                if isinstance(item, dict) and item.get("name") == container_name
            ]
            observed = [
                item
                for item in statuses
                if isinstance(item, dict) and item.get("name") == container_name
            ]
            labels = metadata.get("labels") or {}
            template_hash = labels.get("pod-template-hash")
            if (
                metadata.get("deletionTimestamp") is None
                and status.get("phase") == "Running"
                and len(declared) == 1
                and declared[0].get("image") == desired_image
                and len(observed) == 1
                and observed[0].get("ready") is True
                and isinstance(observed[0].get("imageID"), str)
                and bool(observed[0]["imageID"])
                and isinstance(template_hash, str)
                and bool(template_hash)
                and isinstance(metadata.get("name"), str)
            ):
                candidates.append(metadata["name"])
                template_hashes.add(template_hash)
                image_ids.add(observed[0]["imageID"])
        if (
            len(pods) != replicas
            or len(candidates) != replicas
            or len(template_hashes) != 1
            or len(image_ids) != 1
        ):
            raise GateFailure(f"{container_name} Pods are not fully converged")
        return sorted(candidates)

    def _select_orchestrator(self) -> None:
        self.orchestrator_pods = self._deployment_pods(
            self.config.orchestrator_deployment,
            container_name="orchestrator",
        )
        self.orchestrator_pod = self.orchestrator_pods[0]

    def _dark_config_preflight(self) -> None:
        config_map = self._decode_json(
            self.runner.run(
                ["get", "configmap", "srw-config", "-o", "json"],
                operation="release-fence ConfigMap inspection",
            ).stdout,
            operation="release-fence ConfigMap inspection",
        )
        data = config_map.get("data") or {}
        required = {
            "WORKSPACE_CLEANUP_RECONCILIATION_ENABLED": "false",
            "WORKSPACE_REATTACH_FRESH_FALLBACK": "false",
            "OFFICER_AUTO_PULL_RELEASE_ENABLED": "false",
        }
        if any(data.get(key) != value for key, value in required.items()):
            raise GateFailure("local release fences are not dark")

    def _controller(self, mode: str, *arguments: str, marker: str) -> str:
        if self.orchestrator_pod is None:
            raise GateFailure("orchestrator Pod was not selected")
        result = self.runner.run(
            [
                "exec",
                "-i",
                self.orchestrator_pod,
                "-c",
                "orchestrator",
                "--",
                "python",
                "-",
                mode,
                *arguments,
            ],
            operation=f"gate controller {mode}",
            input_data=_CONTROLLER_PROGRAM.encode(),
        )
        lines = [
            line.strip()
            for line in result.stdout.decode(errors="replace").splitlines()
            if line.strip() == marker or line.strip().startswith(f"{marker} ")
        ]
        if len(lines) != 1:
            raise GateFailure(f"gate controller {mode} did not acknowledge")
        return lines[0][len(marker) :].strip()

    def _discover_workspace_image(self) -> None:
        if self.workspace_image is not None:
            return
        if self.orchestrator_pod is None:
            raise GateFailure("orchestrator Pod was not selected")
        result = self.runner.run(
            [
                "exec",
                self.orchestrator_pod,
                "-c",
                "orchestrator",
                "--",
                "sh",
                "-c",
                'printf "%s" "$WORKSPACE_IMAGE"',
            ],
            operation="workspace image discovery",
        )
        try:
            candidate = result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GateFailure("workspace image discovery was malformed") from exc
        self.workspace_image = _validate_image(candidate)

    def _pod_exec(
        self,
        command: str,
        *,
        operation: str,
        input_data: bytes | None = None,
        expect_success: bool = True,
        timeout: int | None = None,
    ) -> CommandResult:
        return self.runner.run(
            [
                "exec",
                "-i",
                self.config.pod_name,
                "-c",
                "workspace",
                "--",
                "bash",
                "-c",
                command,
            ],
            operation=operation,
            input_data=input_data,
            expect_success=expect_success,
            timeout=timeout,
        )

    def _runtime_artifact_preflight(self, *, include_stateless: bool = True) -> None:
        """Prove each repaired seam in the container that actually owns it."""

        orchestrator_check = (
            "import os, pathlib\n"
            "from orchestrator.database.postgres import PostgresDB\n"
            "from orchestrator.services.container_provisioner import ContainerProvisioner\n"
            "assert os.environ.get('WORKSPACE_CLEANUP_RECONCILIATION_ENABLED') == 'false'\n"
            "assert os.environ.get('WORKSPACE_REATTACH_FRESH_FALLBACK') == 'false'\n"
            "assert os.environ.get('OFFICER_AUTO_PULL_RELEASE_ENABLED') == 'false'\n"
            "assert hasattr(ContainerProvisioner, 'request_workspace_creation_cancellation')\n"
            "assert hasattr(ContainerProvisioner, 'prepare_workspace_cleanup_intent')\n"
            "assert hasattr(ContainerProvisioner, 'reconcile_workspace_cleanup_intent')\n"
            "assert hasattr(ContainerProvisioner, '_create_pvc')\n"
            "assert hasattr(ContainerProvisioner, '_create_service')\n"
            "assert hasattr(ContainerProvisioner, '_managed_repository_process_zero_replay_is_current')\n"
            "assert hasattr(PostgresDB, 'reserve_managed_repository_workspace_creation')\n"
            "assert hasattr(PostgresDB, 'record_managed_repository_workspace_creation_resource')\n"
            "assert hasattr(PostgresDB, 'prepare_managed_repository_workspace_cleanup_intent')\n"
            "assert hasattr(PostgresDB, 'record_stale_managed_repository_workspace_process_zero')\n"
            "assert pathlib.Path('/app/src/orchestrator/database/migrations/app/0193_managed_repository_process_zero_authority.sql').is_file()\n"
            "assert pathlib.Path('/app/src/orchestrator/database/migrations/app/0197_non_pinned_workspace_process_zero.sql').is_file()\n"
            "assert pathlib.Path('/app/src/orchestrator/database/migrations/app/0198_non_pinned_workspace_lifecycle_authority.sql').is_file()\n"
        ).encode()
        for pod_name in self.orchestrator_pods:
            self.runner.run(
                [
                    "exec",
                    pod_name,
                    "-c",
                    "orchestrator",
                    "--",
                    "python",
                    "-",
                ],
                operation="running orchestrator artifact inspection",
                input_data=orchestrator_check,
            )

        if not include_stateless:
            return
        self.stateless_pods = self._deployment_pods(
            self.config.stateless_deployment,
            container_name="agent",
        )
        agent_check = (
            "import inspect\n"
            "from shared.runtime.core.managed_repository import (\n"
            "    managed_repository_agent_launch_command,\n"
            "    managed_repository_agent_retirement_command,\n"
            "    managed_repository_agent_zero_command,\n"
            ")\n"
            "from shared.runtime.services.cloud_mount import RcloneMountManager\n"
            "assert '9>&-' in inspect.getsource(\n"
            "    managed_repository_agent_launch_command\n"
            ")\n"
            "assert callable(managed_repository_agent_retirement_command)\n"
            "assert callable(managed_repository_agent_zero_command)\n"
            "assert callable(RcloneMountManager._bounded_remote_script_command)\n"
        ).encode()
        for pod_name in self.stateless_pods:
            self.runner.run(
                ["exec", pod_name, "-c", "agent", "--", "python", "-"],
                operation="running stateless agent artifact inspection",
                input_data=agent_check,
            )

    def _workspace_artifact_preflight(self) -> None:
        self._pod_exec(
            "command -v ssh-agent >/dev/null\n"
            "command -v ssh-add >/dev/null\n"
            "command -v rclone >/dev/null\n"
            "command -v git >/dev/null",
            operation="running workspace artifact inspection",
        )

    def _wait_running(self) -> str:
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            pod = self._pod_json()
            if pod is not None:
                metadata = pod.get("metadata") or {}
                status = pod.get("status") or {}
                uid = metadata.get("uid")
                statuses = status.get("containerStatuses") or []
                if (
                    status.get("phase") == "Running"
                    and isinstance(uid, str)
                    and all(
                        isinstance(item, dict) and item.get("ready") is True
                        for item in statuses
                    )
                    and statuses
                ):
                    return str(UUID(uid))
                if status.get("phase") in {"Failed", "Succeeded"}:
                    raise GateFailure("workspace Pod terminated before the gate")
            time.sleep(1)
        raise GateFailure("workspace Pod did not become Ready")

    @staticmethod
    def _pod_is_exact_terminal(pod: dict[str, Any]) -> bool:
        metadata = pod.get("metadata") or {}
        spec = pod.get("spec") or {}
        status = pod.get("status") or {}
        if metadata.get("deletionTimestamp") is None:
            return False
        declared = {
            item.get("name")
            for item in spec.get("containers") or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        statuses = {
            item.get("name"): item
            for item in status.get("containerStatuses") or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if declared and all(
            isinstance(
                (statuses.get(name) or {}).get("state", {}).get("terminated"), dict
            )
            for name in declared
        ):
            return True
        # Match the production narrow never-scheduled proof.  An assigned Pod
        # with missing statuses remains deliberately ambiguous.
        return not spec.get("nodeName") and not any(
            isinstance(item.get("state", {}).get("running"), dict)
            for item in statuses.values()
        )

    def _wait_terminal(self) -> None:
        if self.pod_uid is None:
            raise GateFailure("workspace Pod UID was not captured")
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            pod = self._pod_json()
            if pod is None:
                raise GateFailure("workspace Pod disappeared before finalizer release")
            observed_uid = str((pod.get("metadata") or {}).get("uid") or "")
            if observed_uid != self.pod_uid:
                raise GateFailure("workspace Pod was replaced during termination")
            if self._pod_is_exact_terminal(pod):
                return
            time.sleep(1)
        raise GateFailure("workspace Pod did not reach exact terminal state")

    def _wait_absent(self) -> None:
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            pod = self._pod_json()
            if pod is None:
                return
            observed_uid = str((pod.get("metadata") or {}).get("uid") or "")
            if self.pod_uid is not None and observed_uid != self.pod_uid:
                raise GateFailure("same-name replacement appeared during cleanup")
            time.sleep(1)
        raise GateFailure("workspace Pod remained after finalizer release")

    def _agent_state(self, authority_id: str) -> dict[str, str]:
        slug = authority_id.replace("-", "")
        state_path = f"{self.private_home}/.ssh/srw-managed/agents/{slug}.state"
        command = (
            f"test -f {state_path!r}; "
            f'awk -F= \'$1 == "pid" || $1 == "starttime" || '
            f'$1 == "generation" || $1 == "workspace_generation" || '
            f'$1 == "runtime_incarnation" {{print $1 "=" $2}}\' {state_path!r}'
        )
        result = self._pod_exec(command, operation="ssh-agent state inspection")
        values: dict[str, str] = {}
        for line in result.stdout.decode(errors="replace").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value
        required = {
            "pid",
            "starttime",
            "generation",
            "workspace_generation",
            "runtime_incarnation",
        }
        if set(values) != required or not values["pid"].isdigit():
            raise GateFailure("ssh-agent state receipt is malformed")
        return values

    def _exercise_managed_agent(self) -> tuple[str, str, int]:
        if self.pod_uid is None:
            raise GateFailure("workspace Pod UID was not captured")
        # Imported lazily so --help/plan and safety unit tests need no crypto or
        # orchestrator runtime dependencies.
        from orchestrator.services.managed_repository_authority import _deploy_keypair
        from shared.runtime.core.managed_repository import (
            managed_repository_agent_launch_command,
            managed_repository_agent_retirement_command,
            managed_repository_agent_zero_command,
        )

        authority_id = str(uuid4())
        private_key, _public_key, _fingerprint = _deploy_keypair()
        private_buffer = bytearray(private_key.encode())
        private_key = ""
        try:
            for generation in (1, 2):
                command = managed_repository_agent_launch_command(
                    home_path=self.private_home,
                    authority_id=authority_id,
                    generation=generation,
                    preserve_existing=True,
                    workspace_generation=self.config.owner_id,
                    runtime_incarnation=self.pod_uid,
                )
                self._pod_exec(
                    command,
                    operation=f"managed ssh-agent generation {generation} launch",
                    input_data=bytes(private_buffer),
                )
                state = self._agent_state(authority_id)
                if (
                    state["generation"] != str(generation)
                    or state["workspace_generation"] != self.config.owner_id
                    or state["runtime_incarnation"] != self.pod_uid
                ):
                    raise GateFailure("managed ssh-agent receipt changed authority")
                if generation == 1:
                    first_pid = int(state["pid"])
                else:
                    second_pid = int(state["pid"])
            if first_pid == second_pid:
                raise GateFailure("managed ssh-agent generation did not rotate")

            stale = managed_repository_agent_launch_command(
                home_path=self.private_home,
                authority_id=authority_id,
                generation=1,
                preserve_existing=True,
                workspace_generation=self.config.owner_id,
                runtime_incarnation=self.pod_uid,
            )
            self._pod_exec(
                stale,
                operation="stale managed ssh-agent replacement refusal",
                input_data=bytes(private_buffer),
                expect_success=False,
            )
            stable = self._agent_state(authority_id)
            if stable["generation"] != "2" or int(stable["pid"]) != second_pid:
                raise GateFailure("stale replacement mutated managed ssh-agent")

            # A live agent must make the independent zero proof fail.  The
            # private marker scan checks that stdin was not persisted to disk.
            self._pod_exec(
                managed_repository_agent_zero_command(home_path=self.private_home),
                operation="live managed ssh-agent zero refusal",
                expect_success=False,
            )
            self._pod_exec(
                f"if grep -Rqs -- 'PRIVATE KEY' {self.private_home!r}; then exit 91; fi",
                operation="managed key persistence scan",
            )
            retirement = managed_repository_agent_retirement_command(
                home_path=self.private_home,
                authority_ids=None,
            )
            return retirement, authority_id, second_pid
        finally:
            for index in range(len(private_buffer)):
                private_buffer[index] = 0

    def _exercise_cloud_timeout(self) -> None:
        from shared.runtime.services.cloud_mount import RcloneMountManager

        marker = f"srw-cloud-{self.config.gate_id.rsplit('-', 1)[-1]}"
        script_path = f"/tmp/{marker}.sh"
        target_path = f"/tmp/{marker}-mount"
        script = f"""#!/usr/bin/env bash
set -eu
mkdir -p {target_path!r}
_srw_pid=''
cleanup() {{
  if test -n "$_srw_pid"; then
    kill "$_srw_pid" 2>/dev/null || true
    wait "$_srw_pid" 2>/dev/null || true
  fi
  rmdir {target_path!r} 2>/dev/null || true
}}
trap cleanup EXIT TERM INT
rclone rcd --rc-no-auth --rc-addr 127.0.0.1:0 --rc-baseurl /{marker} >/dev/null 2>&1 &
_srw_pid=$!
sleep 1
kill -0 "$_srw_pid"
sleep 600
"""
        self._pod_exec(
            "command -v rclone >/dev/null; command -v git >/dev/null; "
            f"umask 077; cat > {script_path!r}",
            operation="cloud timeout fixture staging",
            input_data=script.encode(),
        )
        bounded, outer_timeout = RcloneMountManager._bounded_remote_script_command(
            script_path, timeout=2
        )
        self._pod_exec(
            bounded,
            operation="bounded optional cloud timeout",
            expect_success=False,
            timeout=outer_timeout + 15,
        )
        residue_scan = f"""python3 - {marker!r} {script_path!r} {target_path!r} <<'PY'
import os
import pathlib
import sys

marker, script_path, target_path = sys.argv[1:]
if pathlib.Path(script_path).exists():
    raise SystemExit(1)
for proc in pathlib.Path('/proc').iterdir():
    if not proc.name.isdigit():
        continue
    try:
        command = (proc / 'cmdline').read_bytes()
    except OSError:
        continue
    if b'rclone\\0rcd\\0' in command and marker.encode() in command:
        raise SystemExit(2)
if os.path.ismount(target_path):
    raise SystemExit(3)
pathlib.Path(target_path).rmdir() if pathlib.Path(target_path).exists() else None
PY
git --version >/dev/null
"""
        self._pod_exec(
            residue_scan,
            operation="optional cloud timeout residue and unrelated Git check",
        )

    def _check_vm_availability(self) -> None:
        result = self.runner.run(
            ["api-resources", "--api-group=kubevirt.io", "-o", "name"],
            operation="KubeVirt availability check",
            expect_success=True,
        )
        resources = {
            line.strip() for line in result.stdout.decode(errors="replace").splitlines()
        }
        self.report.vm = (
            "available_not_mutated"
            if "virtualmachines.kubevirt.io" in resources
            else "blocked_kubevirt_unavailable"
        )

    def _create_owner(self) -> None:
        self._controller("owner-create", self.config.owner_id, marker="OWNER_CREATED")
        self.owner_created = True

    def _prepare_cleanup(self, runtime_uid: str, *, terminal: bool) -> int:
        payload = self._controller(
            "workspace-prepare",
            self.config.owner_id,
            runtime_uid,
            "terminal" if terminal else "preserve",
            marker="WORKSPACE_PREPARED",
        )
        try:
            value = json.loads(payload)
            generation = value["generation"]
            pvc_uid = str(UUID(value["pvc_uid"]))
            service_uid = str(UUID(value["service_uid"]))
            policy = value["resource_policy"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GateFailure("workspace cleanup preparation was malformed") from exc
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
            or policy != ("terminal_reclaim" if terminal else "preserve")
            or self.shared_resources is None
            or pvc_uid != self.shared_resources.pvc_uid
            or service_uid != self.shared_resources.service_uid
        ):
            raise GateFailure("workspace cleanup preparation changed resources")
        return generation

    def _reconcile_cleanup(self, runtime_uid: str, intent_generation: int) -> None:
        self._controller(
            "workspace-reconcile",
            self.config.owner_id,
            runtime_uid,
            self.config.pod_name,
            self.config.namespace,
            str(intent_generation),
            marker="WORKSPACE_RECONCILED",
        )

    def _create_pod(self, creation_role: str) -> None:
        if self.workspace_image is None:
            raise GateFailure("workspace image is unavailable")
        envelope = CreationEnvelope.from_payload(
            self._controller(
                "owner-reserve",
                self.config.owner_id,
                creation_role,
                marker="OWNER_RESERVED",
            )
        )
        self.creation_envelope = envelope
        self.creation_role = creation_role
        manifest = build_workspace_pod_manifest(
            self.config,
            self.workspace_image,
            creation_reservation_id=envelope.reservation_id,
        )
        existing = self._pod_json()
        if not envelope.settled:
            self._controller(
                "owner-creation-current",
                self.config.owner_id,
                creation_role,
                str(envelope.generation),
                str(envelope.claim_token),
                marker="CREATION_CURRENT",
            )
            observed_resources = SharedResourceEnvelope.from_payload(
                self._controller(
                    "owner-shared-resources",
                    self.config.owner_id,
                    creation_role,
                    envelope.reservation_id,
                    str(envelope.generation),
                    str(envelope.claim_token),
                    self.config.gate_id,
                    marker="SHARED_RESOURCES",
                )
            )
        else:
            pvc_uid, service_uid = self._exact_shared_resource_uids()
            observed_resources = SharedResourceEnvelope(
                self.config.pvc_name,
                pvc_uid,
                self.config.pod_name,
                service_uid,
            )
        if self.shared_resources is None:
            self.shared_resources = observed_resources
        elif observed_resources != self.shared_resources:
            raise GateFailure("successor did not reuse exact shared resources")
        if existing is None:
            self.runner.run(
                ["create", "-f", "-"],
                operation="disposable workspace Pod creation",
                input_data=json.dumps(manifest, separators=(",", ":")).encode(),
            )
        else:
            metadata = existing.get("metadata") or {}
            labels = metadata.get("labels") or {}
            annotations = metadata.get("annotations") or {}
            if (
                metadata.get("name") != self.config.pod_name
                or labels.get("srw/job-id") != self.config.owner_id
                or labels.get(GATE_LABEL) != self.config.gate_id
                or annotations.get(CREATION_RESERVATION_ANNOTATION)
                != envelope.reservation_id
            ):
                raise GateFailure("same-name workspace Pod is not this creation")
        self.pod_uid = self._wait_running()
        self._controller(
            "owner-bind",
            self.config.owner_id,
            creation_role,
            self.pod_uid,
            self.config.pod_name,
            self.config.namespace,
            envelope.reservation_id,
            str(envelope.generation),
            str(envelope.claim_token),
            marker="OWNER_BOUND",
        )
        self.owner_bound = True
        self.owner_runtime_uid = self.pod_uid
        self.creation_envelope = None
        self.creation_role = None
        marker_name = f".srw-gate-{self.config.gate_id}-{creation_role}"
        if creation_role == "successor":
            predecessor_marker = f".srw-gate-{self.config.gate_id}-predecessor"
            marker_command = (
                f"test -f /home/agent-host/{predecessor_marker}; "
                f"touch /home/agent-host/{marker_name}"
            )
        else:
            marker_command = f"touch /home/agent-host/{marker_name}"
        self._pod_exec(marker_command, operation="shared PVC continuity marker")
        self._assert_shared_resources_current(
            expected_pod_uid=self.pod_uid,
            require_successor_marker=creation_role == "successor",
        )

    def _replace_retired_predecessor_with_current_successor(self) -> None:
        """Retire A through its receipt, then authoritatively publish B.

        Replaying A after B is current proves that stale-predecessor settlement
        cannot mutate or delete the exact reserved successor.
        """

        if self.pod_uid is None or self.workspace_image is None:
            raise GateFailure("bound workspace runtime is unavailable")
        bound_uid = self.pod_uid
        cleanup_generation = self._prepare_cleanup(bound_uid, terminal=False)
        self._controller(
            "pod-delete",
            self.config.owner_id,
            bound_uid,
            self.config.pod_name,
            self.config.namespace,
            marker="POD_DELETE_ACCEPTED",
        )
        self._wait_terminal()
        self.runner.run(
            [
                "exec",
                "-i",
                self.orchestrator_pod or "",
                "-c",
                "orchestrator",
                "--",
                "python",
                "-",
                "finalizer-release-lost",
                self.config.owner_id,
                bound_uid,
                self.config.pod_name,
                self.config.namespace,
            ],
            operation="committed predecessor finalizer response loss",
            input_data=_CONTROLLER_PROGRAM.encode(),
            expect_success=False,
        )
        self._wait_absent()
        self._reconcile_cleanup(bound_uid, cleanup_generation)
        self.owner_bound = False
        self._create_pod("successor")
        successor_uid = self.pod_uid
        if self.pod_uid == bound_uid:
            raise GateFailure("workspace Pod UID did not rotate")
        self.predecessor_uid = bound_uid
        self.predecessor_cleanup_generation = cleanup_generation
        self._assert_shared_resources_current(
            expected_pod_uid=successor_uid,
            require_successor_marker=True,
        )

    def _replay_retired_predecessor_against_current_successor(
        self,
        *,
        authority_id: str,
        expected_agent_pid: int,
    ) -> None:
        """Replay settled A after B is live and prove B's whole bundle survives."""

        predecessor_uid = self.predecessor_uid
        cleanup_generation = self.predecessor_cleanup_generation
        successor_uid = self.pod_uid
        if (
            predecessor_uid is None
            or cleanup_generation is None
            or successor_uid is None
        ):
            raise GateFailure("predecessor replay authority is unavailable")
        before = self._agent_state(authority_id)
        if int(before["pid"]) != expected_agent_pid or before["generation"] != "2":
            raise GateFailure("successor managed process identity changed")
        self._reconcile_cleanup(predecessor_uid, cleanup_generation)
        current = self._pod_json()
        if (
            current is None
            or str((current.get("metadata") or {}).get("uid") or "") != successor_uid
        ):
            raise GateFailure("stale predecessor replay mutated current successor")
        self._assert_shared_resources_current(
            expected_pod_uid=successor_uid,
            require_successor_marker=True,
        )
        after = self._agent_state(authority_id)
        if after != before:
            raise GateFailure(
                "stale predecessor replay mutated current repository process"
            )

    def _delete_and_release(self) -> None:
        if self.pod_uid is None:
            raise GateFailure("workspace Pod UID was not captured")
        self._controller(
            "owner-terminal", self.config.owner_id, marker="OWNER_TERMINAL"
        )
        cleanup_generation = self._prepare_cleanup(self.pod_uid, terminal=True)
        wrong_uid = str(uuid4())
        self.runner.run(
            [
                "exec",
                "-i",
                self.orchestrator_pod or "",
                "-c",
                "orchestrator",
                "--",
                "python",
                "-",
                "pod-delete",
                self.config.owner_id,
                wrong_uid,
                self.config.pod_name,
                self.config.namespace,
            ],
            operation="replacement UID deletion refusal",
            input_data=_CONTROLLER_PROGRAM.encode(),
            expect_success=False,
        )
        pod = self._pod_json()
        if pod is None or str((pod.get("metadata") or {}).get("uid")) != self.pod_uid:
            raise GateFailure("replacement refusal did not preserve the exact Pod")
        self._controller(
            "pod-delete",
            self.config.owner_id,
            self.pod_uid,
            self.config.pod_name,
            self.config.namespace,
            marker="POD_DELETE_ACCEPTED",
        )
        self._wait_terminal()
        # A mismatched runtime must neither mint a receipt nor remove the
        # finalizer from the retained exact object.
        self.runner.run(
            [
                "exec",
                "-i",
                self.orchestrator_pod or "",
                "-c",
                "orchestrator",
                "--",
                "python",
                "-",
                "finalizer-release",
                self.config.owner_id,
                wrong_uid,
                self.config.pod_name,
                self.config.namespace,
            ],
            operation="replacement UID finalizer refusal",
            input_data=_CONTROLLER_PROGRAM.encode(),
            expect_success=False,
        )
        retained = self._pod_json()
        finalizers = (retained or {}).get("metadata", {}).get("finalizers") or []
        if (
            retained is None
            or str((retained.get("metadata") or {}).get("uid")) != self.pod_uid
            or PROCESS_ZERO_FINALIZER not in finalizers
        ):
            raise GateFailure("replacement refusal did not retain the finalizer")
        self.runner.run(
            [
                "exec",
                "-i",
                self.orchestrator_pod or "",
                "-c",
                "orchestrator",
                "--",
                "python",
                "-",
                "finalizer-release-lost",
                self.config.owner_id,
                self.pod_uid,
                self.config.pod_name,
                self.config.namespace,
            ],
            operation="committed current finalizer response loss",
            input_data=_CONTROLLER_PROGRAM.encode(),
            expect_success=False,
        )
        self._wait_absent()
        self._reconcile_cleanup(self.pod_uid, cleanup_generation)
        if (
            self._resource_json("persistentvolumeclaim", self.config.pvc_name)
            is not None
            or self._resource_json("service", self.config.pod_name) is not None
        ):
            raise GateFailure("terminal cleanup retained shared resources")

    def cleanup_exact(self, retirement_command: str | None = None) -> bool:
        """Best-effort exact cleanup; never force-remove the finalizer."""

        try:
            if self.owner_created:
                # Cleanup-only is an explicit destructive boundary for this
                # confirmed disposable owner. Terminalize first so a crashed
                # pre-Pod reservation uses its production terminal-reclaim
                # policy rather than leaking the shared PVC/Service.
                self._controller(
                    "owner-terminal",
                    self.config.owner_id,
                    marker="OWNER_TERMINAL",
                )
                # This is a no-op for a settled creation. If the harness died
                # anywhere from reserve through publication, the production
                # cancellation/reconciler owns that exact generation first.
                self._controller(
                    "owner-cancel-creation",
                    self.config.owner_id,
                    marker="CREATION_CANCELLED",
                )
            cleanup_generation: int | None = None
            if self.owner_created and self.owner_runtime_uid is not None:
                cleanup_generation = self._prepare_cleanup(
                    self.owner_runtime_uid,
                    terminal=True,
                )
            pod = self._pod_json()
            if pod is not None:
                metadata = pod.get("metadata") or {}
                observed_uid = str(metadata.get("uid") or "")
                if self.pod_uid is None:
                    self.pod_uid = str(UUID(observed_uid))
                elif observed_uid != self.pod_uid:
                    return False
                if self.owner_created and not self.owner_bound:
                    # The production cancellation path either adopts and
                    # deletes a crossed mutation or leaves it retryable. Never
                    # fabricate Ready after the reservation was cancelled.
                    return False
                if metadata.get("deletionTimestamp") is None:
                    if retirement_command is not None:
                        self._pod_exec(
                            retirement_command,
                            operation="cleanup managed ssh-agent retirement",
                        )
                    self._controller(
                        (
                            "pod-delete"
                            if self.pod_uid == self.owner_runtime_uid
                            else "pod-delete-stale"
                        ),
                        self.config.owner_id,
                        self.pod_uid,
                        self.config.pod_name,
                        self.config.namespace,
                        marker="POD_DELETE_ACCEPTED",
                    )
                self._wait_terminal()
                self._controller(
                    "finalizer-release",
                    self.config.owner_id,
                    self.pod_uid,
                    self.config.pod_name,
                    self.config.namespace,
                    marker="FINALIZER_RELEASED",
                )
                self._wait_absent()
            if (
                self.owner_created
                and self.owner_runtime_uid is not None
                and cleanup_generation is not None
            ):
                self._reconcile_cleanup(
                    self.owner_runtime_uid,
                    cleanup_generation,
                )
            if (
                self._resource_json("persistentvolumeclaim", self.config.pvc_name)
                is not None
                or self._resource_json("service", self.config.pod_name) is not None
            ):
                return False
            if self.owner_created:
                self._controller(
                    "owner-delete", self.config.owner_id, marker="OWNER_DELETED"
                )
                self.owner_created = False
            return True
        except Exception:
            return False

    def _owner_inspect(self) -> dict[str, Any]:
        payload = self._controller(
            "owner-inspect", self.config.owner_id, marker="OWNER_INSPECT"
        )
        try:
            state = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise GateFailure("owner inspection was malformed") from exc
        if not isinstance(state, dict) or not isinstance(
            state.get("owner_exists"), bool
        ):
            raise GateFailure("owner inspection was malformed")
        for key in ("active_creations", "pending_cleanups", "receipts"):
            value = state.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise GateFailure("owner inspection was malformed")
        owner_status = state.get("owner_status")
        if owner_status is not None and owner_status not in {
            "created",
            "processing",
            "completed",
            "failed",
            "cancelled",
            "pending_review",
            "paused",
        }:
            raise GateFailure("owner inspection was malformed")
        for key in ("pod_uids", "pvc_uids", "service_uids"):
            raw_uids = state.get(key)
            if not isinstance(raw_uids, list):
                raise GateFailure("owner inspection was malformed")
            try:
                canonical = [str(UUID(str(value))) for value in raw_uids]
            except (TypeError, ValueError) as exc:
                raise GateFailure("owner inspection was malformed") from exc
            if len(canonical) != len(set(canonical)):
                raise GateFailure("owner inspection was malformed")
            state[key] = canonical
        raw_reservations = state.get("active_reservation_ids")
        if not isinstance(raw_reservations, list):
            raise GateFailure("owner inspection was malformed")
        try:
            state["active_reservation_ids"] = [
                str(UUID(str(value))) for value in raw_reservations
            ]
        except (TypeError, ValueError) as exc:
            raise GateFailure("owner inspection was malformed") from exc
        runtime_incarnation = state.get("runtime_incarnation")
        if runtime_incarnation is not None:
            try:
                state["runtime_incarnation"] = str(UUID(runtime_incarnation))
            except (TypeError, ValueError) as exc:
                raise GateFailure("owner inspection was malformed") from exc
        return state

    def _adopt_cleanup_state(self) -> None:
        """Adopt only exact gate-labelled resources backed by its DB ledgers."""

        state = self._owner_inspect()
        self.owner_created = bool(state["owner_exists"])
        runtime_incarnation = state.get("runtime_incarnation")
        if isinstance(runtime_incarnation, str):
            self.owner_bound = True
            self.owner_runtime_uid = runtime_incarnation
        claim = self._resource_json("persistentvolumeclaim", self.config.pvc_name)
        service = self._resource_json("service", self.config.pod_name)
        active_reservations = set(state["active_reservation_ids"])

        def crossed_reservation(resource: dict[str, Any] | None) -> bool:
            annotations = (resource or {}).get("metadata", {}).get("annotations") or {}
            return (
                isinstance(annotations, dict)
                and annotations.get(CREATION_RESERVATION_ANNOTATION)
                in active_reservations
            )

        pvc_uid = self._exact_pvc_uid(claim) if claim is not None else None
        service_uid = (
            self._exact_service_uid(
                service,
                allow_unlabelled_active_reservation=crossed_reservation(service),
            )
            if service is not None
            else None
        )

        if (
            pvc_uid is not None
            and pvc_uid not in state["pvc_uids"]
            and not crossed_reservation(claim)
        ):
            raise GateFailure("cleanup PVC is not backed by the gate ledger")
        if (
            service_uid is not None
            and service_uid not in state["service_uids"]
            and not crossed_reservation(service)
        ):
            raise GateFailure("cleanup Service is not backed by the gate ledger")
        if pvc_uid is not None and service_uid is not None:
            self.shared_resources = SharedResourceEnvelope(
                self.config.pvc_name,
                pvc_uid,
                self.config.pod_name,
                service_uid,
            )
        elif pvc_uid is not None or service_uid is not None:
            # A crossed pre-Pod creation edge may legitimately leave only the
            # first shared resource. Production cancellation owns that partial
            # tuple; the harness never synthesizes the absent peer.
            self.shared_resources = None
        pod = self._pod_json()
        if pod is None:
            return
        metadata = pod.get("metadata") or {}
        labels = metadata.get("labels") or {}
        if (
            metadata.get("name") != self.config.pod_name
            or labels.get(GATE_LABEL) != self.config.gate_id
            or labels.get("srw/job-id") != self.config.owner_id
        ):
            raise GateFailure("cleanup target is not the exact disposable gate Pod")
        try:
            self.pod_uid = str(UUID(str(metadata.get("uid") or "")))
        except ValueError as exc:
            raise GateFailure("cleanup target Pod identity is malformed") from exc
        if self.pod_uid not in state["pod_uids"]:
            annotations = metadata.get("annotations") or {}
            if (
                not isinstance(annotations, dict)
                or annotations.get(CREATION_RESERVATION_ANNOTATION)
                not in active_reservations
            ):
                raise GateFailure("cleanup Pod is not backed by the gate ledger")
        if (
            self.owner_runtime_uid is not None
            and self.pod_uid != self.owner_runtime_uid
        ):
            raise GateFailure("cleanup Pod is not the current owner runtime")

    def _assert_zero_residue(self) -> None:
        state = self._owner_inspect()
        if state["owner_exists"] or any(
            state[key] != 0
            for key in ("active_creations", "pending_cleanups", "receipts")
        ):
            raise GateFailure("disposable database residue remains")
        if (
            self._pod_json() is not None
            or self._resource_json("persistentvolumeclaim", self.config.pvc_name)
            is not None
            or self._resource_json("service", self.config.pod_name) is not None
        ):
            raise GateFailure("exact disposable Kubernetes residue remains")
        inventory = self._decode_json(
            self.runner.run(
                [
                    "get",
                    "pods,configmaps,persistentvolumeclaims,services",
                    "-l",
                    f"{GATE_LABEL}={self.config.gate_id}",
                    "-o",
                    "json",
                ],
                operation="disposable Kubernetes residue inventory",
            ).stdout,
            operation="disposable Kubernetes residue inventory",
        )
        items = inventory.get("items")
        if not isinstance(items, list) or items:
            raise GateFailure("disposable Kubernetes residue remains")

    def cleanup_only(self) -> SafeReport:
        """Recover a prior interrupted run without recreating gate state."""

        self.report.mode = "cleanup_only"
        try:
            self._select_orchestrator()
            self._dark_config_preflight()
            self._controller("preflight", marker="READY")
            self._runtime_artifact_preflight(include_stateless=False)
            self.report.pass_phase("artifact_and_local_cluster_preflight")
            self._adopt_cleanup_state()
            if not self.cleanup_exact():
                raise GateFailure("exact cleanup remains retryable")
            # Idempotently remove settled ledgers even when a prior host died
            # after deleting the synthetic owner row.
            self._controller(
                "owner-delete", self.config.owner_id, marker="OWNER_DELETED"
            )
            self._assert_zero_residue()
            self.report.cleanup = "exact_zero"
            self.report.pass_phase("interrupted_run_exact_cleanup")
            return self.report
        except Exception:
            self.report.cleanup = "fail_closed_residue"
            self.report.fail_phase("cleanup_only")
            raise GateFailure(json.dumps(self.report.as_dict(), sort_keys=True))

    def run(self) -> SafeReport:
        retirement_command: str | None = None
        gate_succeeded = False
        try:
            self._select_orchestrator()
            self._dark_config_preflight()
            self._controller("preflight", marker="READY")
            self._runtime_artifact_preflight()
            self._discover_workspace_image()
            self.report.pass_phase("artifact_and_local_cluster_preflight")
            self._check_vm_availability()
            self._create_owner()
            self._create_pod("predecessor")
            self._replace_retired_predecessor_with_current_successor()
            self._workspace_artifact_preflight()
            self.report.pass_phase("retired_predecessor_and_current_successor")
            retirement_command, authority_id, agent_pid = self._exercise_managed_agent()
            self.report.pass_phase("managed_ssh_agent_generation_and_refusal")
            self._replay_retired_predecessor_against_current_successor(
                authority_id=authority_id,
                expected_agent_pid=agent_pid,
            )
            self.report.pass_phase("stale_cleanup_preserves_current_bundle")
            self._exercise_cloud_timeout()
            self.report.pass_phase("bounded_optional_cloud_timeout")
            self._pod_exec(
                retirement_command,
                operation="terminal managed ssh-agent retirement",
            )
            self._delete_and_release()
            self.report.pass_phase("current_successor_lost_response_and_404_replay")
            self._controller(
                "owner-delete", self.config.owner_id, marker="OWNER_DELETED"
            )
            self.owner_created = False
            self._assert_zero_residue()
            gate_succeeded = True
        except Exception:
            self.report.fail_phase("gate_execution")
        finally:
            cleaned = self.cleanup_exact(retirement_command)
            self.report.cleanup = "exact_zero" if cleaned else "fail_closed_residue"
        if not gate_succeeded or self.report.cleanup != "exact_zero":
            raise GateFailure(json.dumps(self.report.as_dict(), sort_keys=True))
        return self.report


def plan_report(config: GateConfig) -> SafeReport:
    report = SafeReport(gate_id=config.gate_id, mode="plan")
    for phase in (
        "artifact_and_local_cluster_preflight",
        "retired_predecessor_and_current_successor",
        "managed_ssh_agent_generation_and_refusal",
        "stale_cleanup_preserves_current_bundle",
        "bounded_optional_cloud_timeout",
        "current_successor_lost_response_and_404_replay",
        "exact_cleanup",
    ):
        report.phases.append({"name": phase, "result": "planned"})
    report.cleanup = "planned"
    report.vm = "preflight_only"
    return report


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = validate_config(build_parser().parse_args(argv))
        if not config.run and not config.cleanup_only:
            print(json.dumps(plan_report(config).as_dict(), sort_keys=True))
            return 0
        gate = ManagedRepositoryResidueGate(config, KubectlRunner(config))
        report = gate.cleanup_only() if config.cleanup_only else gate.run()
        print(json.dumps(report.as_dict(), sort_keys=True))
        return 0
    except SafetyError as exc:
        print(json.dumps({"mode": "refused", "error": str(exc)}, sort_keys=True))
        return 2
    except GateFailure as exc:
        # GateFailure text is constructed only from fixed operation labels or a
        # SafeReport.  It never contains subprocess stdout/stderr/arguments.
        print(json.dumps({"mode": "failed", "error": str(exc)}, sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({"mode": "failed", "error": "unexpected_gate_failure"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
