"""Live adoption of pre-contract Kubernetes job workspace contexts.

The immediately previous release published ready Kubernetes *job* endpoints
without the Pod UID that now forms execution authority.  The pure workspace
resolver deliberately continues to reject those rows: a database endpoint is
not evidence that the same Pod still exists.  This service is the sole bridge.
It re-derives the exact job workspace owner, performs the existing Kubernetes
and SSH attestation, and writes that evidence only through a snapshot CAS.

No migration fabricates provenance.  Failed/unavailable attestation leaves the
row untouched and retryable, while a server-owned adoption marker keeps the
rare adopted row on live-attestation checks at every later network boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from services.container_provisioner import (
    WorkspaceRuntimeAttestation,
)
from services.workspace_lifecycle import WorkspaceOwner
from src.shared.workspace_contract import (
    LEGACY_K8S_RUNTIME_ADOPTION_KEY,
    WORKSPACE_CONTRACT_CONTEXT_KEY,
    WorkspaceContractError,
    configured_workspace_backend,
    normalize_workspace_backend,
    resolve_workspace_contract,
)

_ADOPTABLE_JOB_STATUSES = frozenset(
    {
        "created",
        "processing",
        "failed",
        "pending_review",
        "paused",
        "reviewing",
        "waiting",
        "waiting_for_reply",
    }
)


class LegacyK8sAdoptionOutcome(str, Enum):
    NOT_NEEDED = "not_needed"
    ADOPTED = "adopted"
    CONVERGED = "converged"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class LegacyK8sAdoptionResult:
    outcome: LegacyK8sAdoptionOutcome
    owner: WorkspaceOwner | None
    authority_job: dict[str, Any] | None
    reason: str | None = None

    @property
    def retryable(self) -> bool:
        return self.outcome is LegacyK8sAdoptionOutcome.RETRY


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _inherits_parent_workspace(job: Mapping[str, Any]) -> bool:
    value = _object(job.get("context")).get("inherits_parent_workspace")
    return value is True or value == "true"


def _sandbox_assignment_for_adoption(job: Mapping[str, Any]) -> bool:
    """Validate assignment evidence without trusting the ready endpoint.

    ``resolve_workspace_contract`` intentionally refuses a UID-less *legacy*
    ready runtime.  For adoption only, reproduce its assignment checks while
    withholding runtime authority.  Stamped contracts can use the normal
    parser because it already ignores runtime readiness.
    """

    context = _object(job.get("context"))
    configured = configured_workspace_backend(job.get("config_override"))
    raw_contract = context.get(WORKSPACE_CONTRACT_CONTEXT_KEY)
    contract_is_stamped = raw_contract is not None
    if contract_is_stamped:
        if not isinstance(raw_contract, Mapping):
            raise WorkspaceContractError(
                "workspace_contract_invalid", "Workspace contract is malformed"
            )
        assigned = resolve_workspace_contract(job).assigned_backend
    else:
        assigned = configured or "sandbox"

    # A coherent VM assignment is outside this sandbox-only adoption bridge.
    # Its VM context is not evidence of both-tier ambiguity by itself.
    if assigned != "sandbox":
        return False

    vm = context.get("vm")
    if not contract_is_stamped and (
        (isinstance(vm, Mapping) and bool(vm))
        or (vm is not None and not isinstance(vm, Mapping))
    ):
        raise WorkspaceContractError(
            "legacy_workspace_ambiguous",
            "Job has both-tier workspace history",
        )

    if "workspace_backend" in context:
        legacy_hint = normalize_workspace_backend(
            context.get("workspace_backend"), field="legacy workspace backend"
        )
        if legacy_hint != assigned:
            raise WorkspaceContractError(
                "legacy_workspace_ambiguous",
                "Legacy workspace request disagrees with job configuration",
            )
    if configured is not None and configured != assigned:
        raise WorkspaceContractError(
            "workspace_contract_config_mismatch",
            "Persisted workspace assignment disagrees with job configuration",
        )
    return assigned == "sandbox"


def legacy_k8s_job_runtime_adoption_candidate(job: Mapping[str, Any]) -> bool:
    """Whether ``job`` has the exact genuine pre-0175 ready context shape."""

    if str(job.get("status") or "") not in _ADOPTABLE_JOB_STATUSES:
        return False
    if not _sandbox_assignment_for_adoption(job):
        return False
    workspace = _object(_object(job.get("context")).get("workspace_container"))
    # A marker records how one previously-ready Pod was adopted; it is not a
    # lease on adoption itself.  Once lifecycle state says that Pod is gone or
    # not ready, ordinary workspace recovery must be free to delete/recreate it
    # instead of waiting forever for an attestation that cannot succeed.
    if not (
        workspace.get("status") == "ready"
        and workspace.get("provisioner") == "k8s"
        and (workspace.get("host") or workspace.get("pod_ip"))
    ):
        return False
    if LEGACY_K8S_RUNTIME_ADOPTION_KEY in workspace:
        return True
    # An explicitly present but malformed incarnation is not historical
    # absence and must never be repaired by guessing what its writer meant.
    if "_runtime_incarnation" in workspace:
        return False
    return True


# Adoption is convergent repair, not a competitive lease: every replica that
# meets the same historical row claims the *same* durable generation, and the
# snapshot CAS below decides which one actually stamps it.  A per-process
# claimant would make two replicas mint two generations for one Pod.
_ADOPTION_CLAIMANT = "legacy-k8s-workspace-adoption"
_ADOPTION_SCOPE = "workspace_container"
# The identity fields the previous release published.  They are what the
# reservation's manifest digest is *about*: one durable generation per exact
# historical runtime plan, so a lease expiry can never silently re-point an
# unfinished adoption at a different endpoint.
_ADOPTION_IDENTITY_FIELDS = (
    "status",
    "provisioner",
    "host",
    "pod_ip",
    "port",
    "pod_name",
    "container_name",
    "namespace",
    "restore_type",
)


def _adoption_manifest_digest(
    owner: WorkspaceOwner, workspace: Mapping[str, Any]
) -> str:
    payload = {
        "version": 1,
        "operation": "adopt",
        "owner_kind": owner.kind,
        "owner_id": owner.id,
        "scope": _ADOPTION_SCOPE,
        "runtime": {
            field: workspace.get(field)
            for field in _ADOPTION_IDENTITY_FIELDS
            if field in workspace
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _adoption_marker(attestation: WorkspaceRuntimeAttestation) -> dict[str, Any]:
    return {
        "version": 1,
        "runtime_incarnation": attestation.runtime_incarnation,
        "workspace_generation": attestation.workspace_generation,
        "ssh_host_key_fingerprint": attestation.ssh_host_key_fingerprint,
    }


def _attestation_matches_workspace(
    workspace: Mapping[str, Any], attestation: WorkspaceRuntimeAttestation
) -> bool:
    marker = workspace.get(LEGACY_K8S_RUNTIME_ADOPTION_KEY)
    if not isinstance(marker, Mapping) or marker.get("version") != 1:
        return False
    try:
        port = int(workspace.get("port"))
    except (TypeError, ValueError):
        return False
    return bool(
        workspace.get("status") == "ready"
        and workspace.get("provisioner") == "k8s"
        and workspace.get("_runtime_incarnation") == attestation.runtime_incarnation
        and workspace.get("host") == attestation.host
        and workspace.get("pod_ip") == attestation.pod_ip
        and port == attestation.port
        and dict(marker) == _adoption_marker(attestation)
    )


async def _authority_job(
    db: Any, job: Mapping[str, Any]
) -> tuple[WorkspaceOwner | None, dict[str, Any] | None]:
    if _inherits_parent_workspace(job):
        parent_id = job.get("parent_job_id")
        if not parent_id or not _sandbox_assignment_for_adoption(job):
            return None, None
        parent = await db.get_job(str(parent_id))
        if parent is None or not _sandbox_assignment_for_adoption(parent):
            return None, parent
        return WorkspaceOwner.job(str(parent_id)), parent
    job_id = job.get("id")
    if not job_id:
        return None, None
    return WorkspaceOwner.job(str(job_id)), dict(job)


async def ensure_legacy_k8s_job_runtime_authority(
    db: Any,
    provisioner: Any,
    job: Mapping[str, Any],
) -> LegacyK8sAdoptionResult:
    """Adopt or revalidate one exact historical Kubernetes job runtime.

    Two equal full attestations precede the one database CAS.  The attester
    itself brackets SSH host-key verification with Kubernetes UID/label/
    readiness reads; the second call catches a same-name replacement after
    the first completed.  The durable CAS then fences status, tier evidence,
    parent/lane and the exact workspace snapshot. A third attestation confirms
    the tentative stamp after commit; failure exact-reverse-CASes that stamp.
    A later pre-network check re-attests every marker-bearing row against any
    replacement after adoption.
    """

    try:
        owner, authority_job = await _authority_job(db, job)
    except WorkspaceContractError:
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.NOT_NEEDED, None, None, "authority_ambiguous"
        )
    if owner is None or authority_job is None:
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.NOT_NEEDED,
            owner,
            authority_job,
            "authority_unavailable",
        )

    try:
        candidate = legacy_k8s_job_runtime_adoption_candidate(authority_job)
    except WorkspaceContractError:
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.NOT_NEEDED,
            owner,
            authority_job,
            "authority_ambiguous",
        )
    if not candidate:
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.NOT_NEEDED, owner, authority_job
        )

    # Always refresh a candidate before the non-database attestation. A stale
    # dispatcher scan is not the snapshot that the CAS is allowed to bless.
    current = await db.get_job(owner.id)
    if current is None:
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.NOT_NEEDED,
            owner,
            None,
            "authority_unavailable",
        )
    try:
        if not legacy_k8s_job_runtime_adoption_candidate(current):
            return LegacyK8sAdoptionResult(
                LegacyK8sAdoptionOutcome.NOT_NEEDED, owner, current
            )
    except WorkspaceContractError:
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.NOT_NEEDED,
            owner,
            current,
            "authority_ambiguous",
        )

    workspace = _object(_object(current.get("context")).get("workspace_container"))
    marker = workspace.get(LEGACY_K8S_RUNTIME_ADOPTION_KEY)
    # An adopted row that ordinary provisioning has since replaced is no longer
    # adopted: the successor Pod was created by this server under a durable
    # reservation and needs no adoption marker. Leaving the predecessor's
    # marker there would make every later pre-network check re-attest a Pod
    # that is gone and refuse delivery for good, so retire it -- the marker is
    # not part of the 0198 authority envelope, and nothing else moves.
    current_runtime = workspace.get("_runtime_incarnation")
    if (
        isinstance(marker, Mapping)
        and current_runtime
        and marker.get("runtime_incarnation") != current_runtime
    ):
        replaced = dict(workspace)
        replaced.pop(LEGACY_K8S_RUNTIME_ADOPTION_KEY, None)
        await db.adopt_legacy_k8s_job_workspace_runtime(
            owner.id,
            expected_status=str(current.get("status") or ""),
            expected_execution_lane=current.get("execution_lane"),
            expected_parent_job_id=(
                str(current["parent_job_id"]) if current.get("parent_job_id") else None
            ),
            expected_contract=_object(current.get("context")).get(
                WORKSPACE_CONTRACT_CONTEXT_KEY
            ),
            expected_legacy_backend=_object(current.get("context")).get(
                "workspace_backend"
            ),
            expected_workspace_config=_object(current.get("config_override")).get(
                "workspace"
            ),
            expected_workspace=workspace,
            adopted_workspace=replaced,
        )
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.NOT_NEEDED,
            owner,
            await db.get_job(owner.id),
            "runtime_replaced_by_creation",
        )
    try:
        first = await provisioner.attest_workspace_runtime(owner)
        if isinstance(marker, Mapping) and _attestation_matches_workspace(
            workspace, first
        ):
            return LegacyK8sAdoptionResult(
                LegacyK8sAdoptionOutcome.CONVERGED, owner, current
            )
        confirmed = await provisioner.attest_workspace_runtime(owner)
    except Exception:
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.RETRY,
            owner,
            current,
            "kubernetes_attestation_unavailable",
        )
    if first != confirmed:
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.RETRY,
            owner,
            current,
            "kubernetes_runtime_changed",
        )

    # Open the exact durable generation the create fence asks for *before*
    # the owner row is stamped.  0198 authorises a new Pod UID only against a
    # runtime-bound reservation, so an adoption that skipped this would have
    # to be waved through by the trigger -- which would equally wave through
    # any writer that invented a UID.  The reservation is refused outright
    # unless the row still carries the genuine UID-less historical shape.
    reservation_owner_kind = "job" if owner.kind == "job" else "thread"
    reservation = await db.reserve_managed_repository_workspace_creation(
        owner.id,
        owner_kind=reservation_owner_kind,
        scope=_ADOPTION_SCOPE,
        claimant=_ADOPTION_CLAIMANT,
        operation_kind="adopt",
        desired_manifest_digest=_adoption_manifest_digest(owner, workspace),
    )
    if reservation is None:
        # The ledger refuses a generation over anything but the exact
        # historical shape, so a refusal usually means the row moved on while
        # the external attestation ran.  Re-read before reporting: a tier or
        # lifecycle transition that won is authority, not a transient error.
        moved_on = await db.get_job(owner.id)
        try:
            still_a_candidate = moved_on is not None and (
                legacy_k8s_job_runtime_adoption_candidate(moved_on)
            )
        except WorkspaceContractError:
            still_a_candidate = False
        if not still_a_candidate:
            return LegacyK8sAdoptionResult(
                LegacyK8sAdoptionOutcome.NOT_NEEDED,
                owner,
                moved_on,
                "workspace_snapshot_changed",
            )
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.RETRY,
            owner,
            moved_on,
            "adoption_reservation_unavailable",
        )
    reservation_generation = int(reservation["reservation_generation"])
    claim_token = int(reservation["claim_token"])
    reservation_fence = {
        "owner_kind": reservation_owner_kind,
        "scope": _ADOPTION_SCOPE,
        "reservation_generation": reservation_generation,
        "claimant": _ADOPTION_CLAIMANT,
        "claim_token": claim_token,
    }
    # Publish the observed UID onto the generation. Adoption never creates a
    # Pod, so this crosses no external-effect edge and the generation stays
    # abortable if the confirmation below fails.
    if not await db.authorize_managed_repository_workspace_creation_runtime(
        owner.id,
        **reservation_fence,
        runtime_incarnation=confirmed.runtime_incarnation,
    ):
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.RETRY,
            owner,
            current,
            "adoption_runtime_authority_unavailable",
        )

    adopted_workspace = dict(workspace)
    adopted_workspace.update(
        {
            "status": "ready",
            "provisioner": "k8s",
            "host": confirmed.host,
            "pod_ip": confirmed.pod_ip,
            "port": confirmed.port,
            "_runtime_incarnation": confirmed.runtime_incarnation,
            "_creation_reservation_id": str(reservation["id"]),
            "_creation_claim_token": str(claim_token),
            LEGACY_K8S_RUNTIME_ADOPTION_KEY: _adoption_marker(confirmed),
        }
    )
    cas_fence = {
        "expected_status": str(current.get("status") or ""),
        "expected_execution_lane": current.get("execution_lane"),
        "expected_parent_job_id": (
            str(current["parent_job_id"]) if current.get("parent_job_id") else None
        ),
        "expected_contract": _object(current.get("context")).get(
            WORKSPACE_CONTRACT_CONTEXT_KEY
        ),
        "expected_legacy_backend": _object(current.get("context")).get(
            "workspace_backend"
        ),
        "expected_workspace_config": _object(current.get("config_override")).get(
            "workspace"
        ),
    }
    won = await db.adopt_legacy_k8s_job_workspace_runtime(
        owner.id,
        **cas_fence,
        expected_workspace=workspace,
        adopted_workspace=adopted_workspace,
    )
    fresh = await db.get_job(owner.id)
    if won:
        # Confirm after persistence as well as before it. If the Pod changed in
        # the cross-store interval, remove only our exact tentative stamp. A
        # concurrent status/tier/runtime writer makes this reverse CAS lose and
        # remains authoritative. No caller receives ADOPTED without this proof.
        try:
            settled = await provisioner.attest_workspace_runtime(owner)
        except Exception:
            settled = None
        fresh_workspace = _object(
            _object((fresh or {}).get("context")).get("workspace_container")
        )
        if settled != confirmed or not _attestation_matches_workspace(
            fresh_workspace, confirmed
        ):
            reversed_stamp = await db.adopt_legacy_k8s_job_workspace_runtime(
                owner.id,
                **cas_fence,
                expected_workspace=adopted_workspace,
                adopted_workspace=workspace,
            )
            if reversed_stamp:
                # Only a generation whose owner projection no longer points at
                # it may be discarded; a lost reverse CAS leaves the ledger
                # entry to expire instead of stranding a live stamp.
                await db.abort_managed_repository_workspace_creation_reservation(
                    owner.id, **reservation_fence
                )
            return LegacyK8sAdoptionResult(
                LegacyK8sAdoptionOutcome.RETRY,
                owner,
                await db.get_job(owner.id),
                "kubernetes_runtime_changed_after_persistence",
            )
        if not await db.settle_managed_repository_workspace_creation_reservation(
            owner.id,
            **reservation_fence,
            runtime_incarnation=confirmed.runtime_incarnation,
        ):
            # The stamp is durable and proven, but the generation is still
            # open. Report retry so the caller comes back and closes it rather
            # than treating an unsettled fence as a finished adoption.
            return LegacyK8sAdoptionResult(
                LegacyK8sAdoptionOutcome.RETRY,
                owner,
                fresh,
                "adoption_reservation_unsettled",
            )
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.ADOPTED, owner, await db.get_job(owner.id)
        )
    if fresh is not None:
        fresh_workspace = _object(
            _object(fresh.get("context")).get("workspace_container")
        )
        if _attestation_matches_workspace(fresh_workspace, confirmed):
            return LegacyK8sAdoptionResult(
                LegacyK8sAdoptionOutcome.CONVERGED, owner, fresh
            )
        try:
            still_requires_adoption = legacy_k8s_job_runtime_adoption_candidate(fresh)
        except WorkspaceContractError:
            still_requires_adoption = False
        if not still_requires_adoption:
            # A concurrent status, tier, or authoritative-runtime transition
            # won the snapshot CAS.  Return its reread row to the caller so the
            # ordinary resolver applies that newer authority; do not make the
            # stale adoption attempt hold it in a retry loop.
            return LegacyK8sAdoptionResult(
                LegacyK8sAdoptionOutcome.NOT_NEEDED,
                owner,
                fresh,
                "workspace_snapshot_changed",
            )
    return LegacyK8sAdoptionResult(
        LegacyK8sAdoptionOutcome.RETRY,
        owner,
        fresh,
        "workspace_snapshot_changed",
    )


async def verify_adopted_k8s_runtime_before_delivery(
    db: Any, provisioner: Any, job: Mapping[str, Any]
) -> bool:
    """Re-attest a marker-bearing runtime at the last network boundary."""

    try:
        owner, authority_job = await _authority_job(db, job)
        if owner is None or authority_job is None:
            return not _inherits_parent_workspace(job)
        # For an inherited job, never trust the overlaid child snapshot as the
        # durable authority. Re-read the parent that owns the Kubernetes labels.
        if owner.id != str(job.get("id")):
            authority_job = await db.get_job(owner.id)
            if authority_job is None:
                return False
        workspace = _object(
            _object(authority_job.get("context")).get("workspace_container")
        )
        marker = workspace.get(LEGACY_K8S_RUNTIME_ADOPTION_KEY)
        if not isinstance(marker, Mapping):
            return True
        if not _sandbox_assignment_for_adoption(authority_job):
            return False
        attestation = await provisioner.attest_workspace_runtime(owner)
        return _attestation_matches_workspace(workspace, attestation)
    except Exception:
        return False


__all__ = [
    "LegacyK8sAdoptionOutcome",
    "LegacyK8sAdoptionResult",
    "ensure_legacy_k8s_job_runtime_authority",
    "legacy_k8s_job_runtime_adoption_candidate",
    "verify_adopted_k8s_runtime_before_delivery",
]
