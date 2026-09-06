"""Live adoption of pre-contract Kubernetes session workspace projections.

The thread twin of :mod:`services.job_workspace_adoption`.  The immediately
previous release published ready Kubernetes *session* workspaces without the
Pod UID that now forms execution authority, and named its one-shot create
marker differently.  Neither is evidence that the same Pod still exists, so
the pure resolver keeps rejecting those rows and this service is the sole
bridge: it opens an exact ``adopt`` generation on the 0198 reservation ledger,
proves the Pod against Kubernetes twice, publishes that observed UID onto the
generation, and only then CASes the exact owner snapshot.

Adoption creates nothing.  A failed or unavailable attestation leaves the row
untouched and retryable, and a confirmation that no longer matches withdraws
the tentative stamp rather than leaving a durable claim on a foreign Pod.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from orchestrator.services.job_workspace_adoption import (
    LegacyK8sAdoptionOutcome,
    LegacyK8sAdoptionResult,
)
from orchestrator.services.workspace_lifecycle import WorkspaceOwner
from shared.workspace_contract import LEGACY_K8S_RUNTIME_ADOPTION_KEY

_ADOPTION_CLAIMANT = "legacy-k8s-session-workspace-adoption"
_ADOPTION_SCOPE = "workspace_container"
_ADOPTABLE_THREAD_STATUSES = frozenset({"created", "active", "awaiting_user"})
# The marker name the previous release used for the same one-shot create
# authority.  A row still carrying it was mid-creation when the tranche
# landed; the reader in the database layer already refuses to progress on it,
# and adoption is the only path that is allowed to look at it at all.
LEGACY_CREATION_MARKER_KEY = "_stateless_runtime_creation"
CREATION_MARKER_KEY = "_runtime_creation"
# Lifecycle markers that mean this thread is on its way out.  A retiring or
# claim-losing session must never acquire fresh runtime authority.
_RETIREMENT_MARKERS = (
    "_stateless_workspace_retirement_pending",
    "_stateless_claim_retirement",
    "_stateless_claim_loss_hold",
    "_stateless_claim_losses",
)
_IDENTITY_FIELDS = (
    "status",
    "provisioner",
    "host",
    "pod_ip",
    "port",
    "pod_name",
    "container_name",
    "namespace",
)


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


def _workspace(thread: Mapping[str, Any]) -> dict[str, Any]:
    return _object(_object(thread.get("metadata")).get("workspace_container"))


def legacy_k8s_thread_runtime_adoption_candidate(thread: Mapping[str, Any]) -> bool:
    """Whether ``thread`` has the exact genuine pre-0197 ready session shape."""

    if str(thread.get("execution_lane") or "") != "stateless":
        return False
    if str(thread.get("status") or "") not in _ADOPTABLE_THREAD_STATUSES:
        return False
    if not thread.get("runtime_generation"):
        return False
    metadata = _object(thread.get("metadata"))
    if any(marker in metadata for marker in _RETIREMENT_MARKERS):
        return False
    if "protected_cloud" in metadata and metadata["protected_cloud"] is not False:
        return False
    workspace = _workspace(thread)
    if not (
        workspace.get("status") == "ready"
        and workspace.get("provisioner") == "k8s"
        and (workspace.get("host") or workspace.get("pod_ip"))
    ):
        return False
    if LEGACY_K8S_RUNTIME_ADOPTION_KEY in workspace:
        return True
    # A row that already carries current authority, an explicitly present but
    # malformed incarnation, or a post-tranche reservation is not historical
    # absence and must never be repaired by guessing what its writer meant.
    if "_runtime_incarnation" in workspace:
        return False
    if "_creation_reservation_id" in workspace or "_creation_claim_token" in workspace:
        return False
    # Both marker names at once is a contradiction, not a legacy row.
    if CREATION_MARKER_KEY in workspace and LEGACY_CREATION_MARKER_KEY in workspace:
        return False
    return True


def _adoption_marker(attestation: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "runtime_incarnation": attestation.runtime_incarnation,
        "workspace_generation": attestation.workspace_generation,
        "ssh_host_key_fingerprint": attestation.ssh_host_key_fingerprint,
    }


def _attestation_matches_workspace(
    workspace: Mapping[str, Any], attestation: Any
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


def _manifest_digest(thread_id: str, workspace: Mapping[str, Any]) -> str:
    payload = {
        "version": 1,
        "operation": "adopt",
        "owner_kind": "thread",
        "owner_id": thread_id,
        "scope": _ADOPTION_SCOPE,
        "runtime": {
            field: workspace.get(field)
            for field in _IDENTITY_FIELDS
            if field in workspace
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def ensure_legacy_k8s_thread_runtime_authority(
    db: Any,
    provisioner: Any,
    thread: Mapping[str, Any],
) -> LegacyK8sAdoptionResult:
    """Adopt or revalidate one exact historical Kubernetes session runtime."""

    thread_id = str(thread.get("id") or "")
    if not thread_id:
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.NOT_NEEDED, None, None, "authority_unavailable"
        )
    owner = WorkspaceOwner.session(thread_id)
    if not legacy_k8s_thread_runtime_adoption_candidate(thread):
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.NOT_NEEDED, owner, dict(thread)
        )

    # A stale scan is not the snapshot the CAS may bless.
    current = await db.get_thread(thread_id)
    if current is None or not legacy_k8s_thread_runtime_adoption_candidate(current):
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.NOT_NEEDED, owner, current
        )

    workspace = _workspace(current)
    marker = workspace.get(LEGACY_K8S_RUNTIME_ADOPTION_KEY)
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

    reservation = await db.reserve_managed_repository_workspace_creation(
        thread_id,
        owner_kind="thread",
        scope=_ADOPTION_SCOPE,
        claimant=_ADOPTION_CLAIMANT,
        operation_kind="adopt",
        desired_manifest_digest=_manifest_digest(thread_id, workspace),
    )
    if reservation is None:
        moved_on = await db.get_thread(thread_id)
        # Another adopter may have completed the exact same generation while
        # this caller was attesting Kubernetes.  The reservation ledger then
        # correctly refuses a second adoption over the now-UID-bearing row;
        # classify that exact stamp as convergence rather than asking the
        # caller to retry an adoption that is already complete.
        if moved_on is not None and _attestation_matches_workspace(
            _workspace(moved_on), confirmed
        ):
            return LegacyK8sAdoptionResult(
                LegacyK8sAdoptionOutcome.CONVERGED, owner, moved_on
            )
        if moved_on is None or not legacy_k8s_thread_runtime_adoption_candidate(
            moved_on
        ):
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
    reservation_fence = {
        "owner_kind": "thread",
        "scope": _ADOPTION_SCOPE,
        "reservation_generation": int(reservation["reservation_generation"]),
        "claimant": _ADOPTION_CLAIMANT,
        "claim_token": int(reservation["claim_token"]),
    }
    if not await db.authorize_managed_repository_workspace_creation_runtime(
        thread_id,
        **reservation_fence,
        runtime_incarnation=confirmed.runtime_incarnation,
    ):
        # Both adopters can acquire the same claimant/token before either one
        # stamps the owner.  If the winner settles the reservation before this
        # authorization runs, the update is deliberately refused; re-read the
        # owner so that the loser's result still converges on the winner.
        moved_on = await db.get_thread(thread_id)
        if moved_on is not None and _attestation_matches_workspace(
            _workspace(moved_on), confirmed
        ):
            return LegacyK8sAdoptionResult(
                LegacyK8sAdoptionOutcome.CONVERGED, owner, moved_on
            )
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.RETRY,
            owner,
            current,
            "adoption_runtime_authority_unavailable",
        )

    # The historical create marker is retired by conversion, never carried
    # forward: it was minted with no reservation behind it, and leaving it
    # would let an old replica read it as live create authority for an
    # already-adopted generation.  A withdrawal returns to this same
    # marker-less projection, which is what the reversal predicate admits.
    withdrawn_workspace = {
        key: value
        for key, value in workspace.items()
        if key != LEGACY_CREATION_MARKER_KEY
    }
    adopted_workspace = dict(withdrawn_workspace)
    adopted_workspace.update(
        {
            "status": "ready",
            "provisioner": "k8s",
            "host": confirmed.host,
            "pod_ip": confirmed.pod_ip,
            "port": confirmed.port,
            "_runtime_incarnation": confirmed.runtime_incarnation,
            "_creation_reservation_id": str(reservation["id"]),
            "_creation_claim_token": str(reservation_fence["claim_token"]),
            LEGACY_K8S_RUNTIME_ADOPTION_KEY: _adoption_marker(confirmed),
        }
    )
    cas_fence = {
        "expected_status": str(current.get("status") or ""),
        "expected_execution_lane": str(current.get("execution_lane") or ""),
        "expected_runtime_generation": str(current.get("runtime_generation") or ""),
    }
    won = await db.adopt_legacy_k8s_thread_workspace_runtime(
        thread_id,
        **cas_fence,
        expected_workspace=workspace,
        adopted_workspace=adopted_workspace,
    )
    fresh = await db.get_thread(thread_id)
    if won:
        try:
            settled = await provisioner.attest_workspace_runtime(owner)
        except Exception:
            settled = None
        if settled != confirmed or not _attestation_matches_workspace(
            _workspace(fresh or {}), confirmed
        ):
            reversed_stamp = await db.adopt_legacy_k8s_thread_workspace_runtime(
                thread_id,
                **cas_fence,
                expected_workspace=adopted_workspace,
                adopted_workspace=withdrawn_workspace,
            )
            if reversed_stamp:
                await db.abort_managed_repository_workspace_creation_reservation(
                    thread_id, **reservation_fence
                )
            return LegacyK8sAdoptionResult(
                LegacyK8sAdoptionOutcome.RETRY,
                owner,
                await db.get_thread(thread_id),
                "kubernetes_runtime_changed_after_persistence",
            )
        if not await db.settle_managed_repository_workspace_creation_reservation(
            thread_id,
            **reservation_fence,
            runtime_incarnation=confirmed.runtime_incarnation,
        ):
            return LegacyK8sAdoptionResult(
                LegacyK8sAdoptionOutcome.RETRY,
                owner,
                fresh,
                "adoption_reservation_unsettled",
            )
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.ADOPTED, owner, await db.get_thread(thread_id)
        )
    if fresh is not None and _attestation_matches_workspace(
        _workspace(fresh), confirmed
    ):
        return LegacyK8sAdoptionResult(LegacyK8sAdoptionOutcome.CONVERGED, owner, fresh)
    if fresh is not None and not legacy_k8s_thread_runtime_adoption_candidate(fresh):
        return LegacyK8sAdoptionResult(
            LegacyK8sAdoptionOutcome.NOT_NEEDED,
            owner,
            fresh,
            "workspace_snapshot_changed",
        )
    return LegacyK8sAdoptionResult(
        LegacyK8sAdoptionOutcome.RETRY, owner, fresh, "workspace_snapshot_changed"
    )


async def verify_adopted_k8s_session_runtime_before_delivery(
    db: Any, provisioner: Any, thread: Mapping[str, Any]
) -> bool:
    """Re-attest a marker-bearing session runtime at the last network edge."""

    try:
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            return False
        authority = await db.get_thread(thread_id)
        if authority is None:
            return False
        workspace = _workspace(authority)
        if not isinstance(workspace.get(LEGACY_K8S_RUNTIME_ADOPTION_KEY), Mapping):
            return True
        if str(authority.get("execution_lane") or "") != "stateless":
            return False
        attestation = await provisioner.attest_workspace_runtime(
            WorkspaceOwner.session(thread_id)
        )
        return _attestation_matches_workspace(workspace, attestation)
    except Exception:
        return False


__all__ = [
    "ensure_legacy_k8s_thread_runtime_authority",
    "legacy_k8s_thread_runtime_adoption_candidate",
    "verify_adopted_k8s_session_runtime_before_delivery",
]
