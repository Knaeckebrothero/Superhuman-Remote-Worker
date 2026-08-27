"""Durable reconciliation of pre-0176 managed repository credentials.

Discovery is read-only and keyset-paged. Apply mode persists one secret-free
intent per exact source row, then multiple operators/replicas may safely drain
leased claims. External Gitea mutations reuse the existing idempotent deploy-key
authority protocol; only a final source-row CAS removes historical userinfo.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import UUID, uuid4

from src.shared.session_retirement import stateless_settled_retirement_authority

from .managed_repository_authority import (
    ManagedRepositoryAuthorityError,
    ensure_job_repository_authority,
    ensure_thread_repository_authority,
    project_repository_access_mode,
    repository_url_has_credentials,
    rotate_project_repository_authority,
)

# The explicit job-resume contract rejects completed work, while ordinary
# failed/cancelled rows are deliberately retryable.  ``blocked_undelivered`` is
# stored as cancelled but has its own server-owned absorbing outcome.
_TERMINAL_JOB_STATUSES = frozenset({"completed"})
_TERMINAL_COMPLETION_OUTCOMES = frozenset({"blocked_undelivered"})
_AUTHORITY_CLASSIFICATIONS = frozenset(
    {
        "runnable_job",
        "resumable_thread",
        "current_officer_thread",
        "shared_project_jobs_repository",
        "project_runtime_repository",
    }
)
_AMBIGUOUS_AUTHORITY_CODES = frozenset(
    {
        "authority_scope_invalid",
        "job_lineage_invalid",
        "job_lineage_unavailable",
        "job_repository_mismatch",
        "job_repository_project_mismatch",
        "legacy_jobs_repository_ambiguous",
        "legacy_repository_ambiguous",
        "repository_name_invalid",
        "repository_owner_invalid",
        "repository_owner_mismatch",
        "repository_scope_ambiguous",
        "repository_scope_conflict",
        "thread_repository_mismatch",
    }
)


@dataclass(frozen=True, repr=False)
class LegacyRepositoryCandidate:
    source_kind: str
    source_id: str
    project_id: str | None
    observed_url: str
    repo_name: str | None
    role: str | None
    read_only: bool | None
    is_managed: bool | None
    project_status: str | None
    source_status: str | None
    completion_outcome_kind: str | None
    parent_job_id: str | None
    branch_name: str | None
    execution_lane: str | None
    source_metadata: dict[str, Any]
    current_officer: bool

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> LegacyRepositoryCandidate:
        metadata = row.get("source_metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return cls(
            source_kind=str(row["source_kind"]),
            source_id=str(row["source_id"]),
            project_id=(str(row["project_id"]) if row.get("project_id") else None),
            observed_url=str(row.get("observed_url") or ""),
            repo_name=(str(row["repo_name"]) if row.get("repo_name") else None),
            role=(str(row["role"]) if row.get("role") else None),
            read_only=(
                bool(row["read_only"]) if row.get("read_only") is not None else None
            ),
            is_managed=(
                bool(row["is_managed"]) if row.get("is_managed") is not None else None
            ),
            project_status=(
                str(row["project_status"]) if row.get("project_status") else None
            ),
            source_status=(
                str(row["source_status"]) if row.get("source_status") else None
            ),
            completion_outcome_kind=(
                str(row["completion_outcome_kind"])
                if row.get("completion_outcome_kind")
                else None
            ),
            parent_job_id=(
                str(row["parent_job_id"]) if row.get("parent_job_id") else None
            ),
            branch_name=(str(row["branch_name"]) if row.get("branch_name") else None),
            execution_lane=(
                str(row["execution_lane"]) if row.get("execution_lane") else None
            ),
            source_metadata=metadata,
            current_officer=bool(row.get("current_officer")),
        )


@dataclass(frozen=True)
class LegacyReconciliationPlan:
    source_kind: str
    source_id: str
    project_id: str | None
    classification: str
    authority_kind: str | None = None
    authority_id: str | None = None
    authority_record_id: str | None = None
    authority_generation: int | None = None
    repository_owner: str | None = None
    repo_name: str | None = None
    access_mode: str | None = None
    reason_code: str | None = None

    @property
    def actionable(self) -> bool:
        return self.classification != "ambiguous"

    def persistence_kwargs(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "project_id": self.project_id,
            "classification": self.classification,
            "authority_kind": self.authority_kind,
            "authority_id": self.authority_id,
            "authority_record_id": self.authority_record_id,
            "authority_generation": self.authority_generation,
            "repository_owner": self.repository_owner,
            "repo_name": self.repo_name,
            "access_mode": self.access_mode,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class LegacyReconciliationStats:
    scanned: int = 0
    adopted: int = 0
    scrubbed_terminal: int = 0
    deferred: int = 0
    failed: int = 0
    ambiguous: int = 0


def legacy_reconciliation_retry_delay(attempts: int) -> int:
    """Return the required 60--900 second bounded exponential backoff."""

    exponent = max(0, min(int(attempts) - 1, 30))
    return min(900, 60 * (2**exponent))


def _safe_exception_code(exc: BaseException) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", type(exc).__name__)[:64]
    return f"reconciliation_{name or 'error'}"


def _ambiguous(
    candidate: LegacyRepositoryCandidate, code: str
) -> LegacyReconciliationPlan:
    return LegacyReconciliationPlan(
        source_kind=candidate.source_kind,
        source_id=candidate.source_id,
        project_id=candidate.project_id,
        classification="ambiguous",
        reason_code=code,
    )


def _url_matches_managed_scope(
    observed_url: str,
    *,
    expected_clean_url: str,
) -> bool:
    """Compare authority coordinates without ever formatting them for output."""

    observed = urlparse(observed_url)
    expected = urlparse(expected_clean_url)
    if (
        observed.scheme not in {"http", "https"}
        or observed.username is None
        or observed.hostname is None
        or expected.scheme not in {"http", "https"}
        or expected.hostname is None
        or observed.params
        or observed.query
        or observed.fragment
    ):
        return False
    return bool(
        observed.scheme.lower() == expected.scheme.lower()
        and observed.hostname.lower() == expected.hostname.lower()
        and observed.port == expected.port
        and observed.path.rstrip("/") == expected.path.rstrip("/")
    )


def _is_permanently_retired_stateless_thread(
    candidate: LegacyRepositoryCandidate,
) -> bool:
    """Return whether durable state proves this thread cannot be resumed.

    ``ended`` alone is deliberately insufficient: idle archive and orphan
    recovery both write that status, and the public resume path accepts it.
    A stateless permanent End is different.  Its settled tombstone is written
    only after claimant, resident, remote-shell, and workspace retirement have
    completed, and ``resume_thread`` refuses a tombstone with
    ``permanent=true``.  Pending retirement intent is not enough because the
    predecessor may still need its repository while it drains.
    """

    if candidate.execution_lane != "stateless" or candidate.source_status != "ended":
        return False
    settled = stateless_settled_retirement_authority(candidate.source_metadata)
    return bool(
        settled is not None
        and settled.get("cleanup_complete") is True
        and settled.get("permanent") is True
    )


async def _root_job(db: Any, job: Mapping[str, Any]) -> Mapping[str, Any]:
    current = job
    seen: set[str] = set()
    while current.get("parent_job_id"):
        current_id = str(current.get("id") or "")
        if current_id in seen:
            raise ManagedRepositoryAuthorityError("job_lineage_invalid")
        seen.add(current_id)
        parent = await db.get_job(str(current["parent_job_id"]))
        if parent is None:
            raise ManagedRepositoryAuthorityError("job_lineage_unavailable")
        current = parent
    return current


async def _legacy_source_exists(db: Any, source_kind: str, source_id: str) -> bool:
    """Distinguish a deleted owner from one already scrubbed by a winner."""

    if source_kind == "job":
        source = await db.get_job(source_id)
    elif source_kind == "thread":
        source = await db.get_thread(source_id)
    elif source_kind == "project_repository":
        source = await db.get_project_repository(source_id)
    else:
        return False
    return source is not None


async def _bind_existing_exact_authority(
    db: Any, plan: LegacyReconciliationPlan
) -> LegacyReconciliationPlan:
    """Bind a live exact generation without making a name-only adoption."""

    if (
        not plan.actionable
        or not plan.authority_kind
        or not plan.authority_id
        or not plan.repository_owner
        or not plan.repo_name
        or plan.access_mode not in {"read", "write"}
    ):
        return plan
    authority = await db.get_managed_repository_authority(
        plan.repo_name,
        repository_owner=plan.repository_owner,
        include_private_key=False,
        active_only=False,
    )
    if authority is None or str(authority.get("status") or "") == "revoked":
        return plan
    if (
        str(authority.get("authority_kind") or "") != plan.authority_kind
        or str(authority.get("authority_id") or "") != plan.authority_id
        or str(authority.get("project_id") or "") != str(plan.project_id or "")
        or str(authority.get("repository_owner") or "") != plan.repository_owner
        or str(authority.get("repo_name") or "") != plan.repo_name
        or str(authority.get("access_mode") or "") != plan.access_mode
    ):
        return replace(
            plan,
            classification="ambiguous",
            authority_kind=None,
            authority_id=None,
            repository_owner=None,
            repo_name=None,
            access_mode=None,
            reason_code="repository_scope_conflict",
        )
    return replace(
        plan,
        authority_record_id=str(authority["id"]),
        authority_generation=int(authority["generation"]),
    )


async def classify_managed_repository_legacy_candidate(
    db: Any,
    gitea_client: Any,
    candidate: LegacyRepositoryCandidate,
) -> LegacyReconciliationPlan:
    """Derive one exact scope exclusively from current durable authority."""

    if not repository_url_has_credentials(candidate.observed_url):
        return _ambiguous(candidate, "source_no_longer_credentialed")

    if candidate.source_kind == "job":
        job = await db.get_job(candidate.source_id)
        if not job or not candidate.repo_name:
            return _ambiguous(candidate, "job_repository_missing")
        root = await _root_job(db, job)
        root_id = str(root["id"])
        root_project = str(root.get("project_id") or "") or None
        if root_project != candidate.project_id:
            return _ambiguous(candidate, "job_lineage_project_mismatch")
        root_repo = str(root.get("repo_name") or "").strip()
        if root_repo != candidate.repo_name:
            return _ambiguous(candidate, "job_lineage_repository_mismatch")

        authority_kind = "job"
        authority_id = root_id
        if candidate.project_id:
            repositories = await db.get_project_repositories(
                candidate.project_id, role="jobs"
            )
            matching = [
                repository
                for repository in repositories
                if repository.get("is_managed")
                and str(repository.get("name") or "") == candidate.repo_name
            ]
            if len(matching) > 1:
                return _ambiguous(candidate, "legacy_jobs_repository_ambiguous")
            if matching:
                if matching[0].get("read_only"):
                    return _ambiguous(candidate, "legacy_jobs_repository_read_only")
                authority_kind = "project_repository"
                authority_id = str(matching[0]["id"])
            elif candidate.repo_name != f"job-{root_id[:8]}":
                return _ambiguous(candidate, "legacy_repository_ambiguous")
        if not await db.managed_repository_scope_is_unambiguous(
            repo_name=candidate.repo_name,
            authority_kind=authority_kind,
            authority_id=authority_id,
            project_id=candidate.project_id,
        ):
            return _ambiguous(candidate, "repository_scope_ambiguous")
        if not _url_matches_managed_scope(
            candidate.observed_url,
            expected_clean_url=gitea_client.clean_repo_url(candidate.repo_name),
        ):
            return _ambiguous(candidate, "repository_coordinate_mismatch")
        if (
            candidate.completion_outcome_kind in _TERMINAL_COMPLETION_OUTCOMES
            and candidate.source_status != "cancelled"
        ):
            return _ambiguous(candidate, "job_completion_outcome_lifecycle_conflict")
        if (
            candidate.source_status in _TERMINAL_JOB_STATUSES
            or candidate.completion_outcome_kind in _TERMINAL_COMPLETION_OUTCOMES
        ):
            return LegacyReconciliationPlan(
                source_kind="job",
                source_id=candidate.source_id,
                project_id=candidate.project_id,
                classification="terminal_historical",
                authority_kind=authority_kind,
                authority_id=authority_id,
                repository_owner=str(gitea_client.repository_owner),
                repo_name=candidate.repo_name,
                access_mode="write",
            )
        return LegacyReconciliationPlan(
            source_kind="job",
            source_id=candidate.source_id,
            project_id=candidate.project_id,
            classification="runnable_job",
            authority_kind=authority_kind,
            authority_id=authority_id,
            repository_owner=str(gitea_client.repository_owner),
            repo_name=candidate.repo_name,
            access_mode="write",
        )

    if candidate.source_kind == "thread":
        expected_name = f"thread-{candidate.source_id[:8]}"
        if candidate.repo_name != expected_name:
            return _ambiguous(candidate, "thread_repository_mismatch")
        if not await db.managed_repository_scope_is_unambiguous(
            repo_name=expected_name,
            authority_kind="thread",
            authority_id=candidate.source_id,
            project_id=candidate.project_id,
        ):
            return _ambiguous(candidate, "repository_scope_ambiguous")
        if not _url_matches_managed_scope(
            candidate.observed_url,
            expected_clean_url=gitea_client.clean_repo_url(expected_name),
        ):
            return _ambiguous(candidate, "repository_coordinate_mismatch")
        if candidate.source_status == "ended" and candidate.current_officer:
            return _ambiguous(candidate, "officer_thread_lifecycle_conflict")
        try:
            permanently_retired = _is_permanently_retired_stateless_thread(candidate)
        except RuntimeError:
            return _ambiguous(candidate, "stateless_retirement_authority_malformed")
        if permanently_retired:
            return LegacyReconciliationPlan(
                source_kind="thread",
                source_id=candidate.source_id,
                project_id=candidate.project_id,
                classification="terminal_historical",
                authority_kind="thread",
                authority_id=candidate.source_id,
                repository_owner=str(gitea_client.repository_owner),
                repo_name=expected_name,
                access_mode="write",
            )
        return LegacyReconciliationPlan(
            source_kind="thread",
            source_id=candidate.source_id,
            project_id=candidate.project_id,
            classification=(
                "current_officer_thread"
                if candidate.current_officer
                else "resumable_thread"
            ),
            authority_kind="thread",
            authority_id=candidate.source_id,
            repository_owner=str(gitea_client.repository_owner),
            repo_name=expected_name,
            access_mode="write",
        )

    if candidate.source_kind == "project_repository":
        if not candidate.is_managed or not candidate.repo_name:
            return _ambiguous(candidate, "project_repository_not_managed")
        if candidate.role == "knowledge":
            if not _url_matches_managed_scope(
                candidate.observed_url,
                expected_clean_url=gitea_client.clean_repo_url(candidate.repo_name),
            ):
                return _ambiguous(candidate, "repository_coordinate_mismatch")
            return LegacyReconciliationPlan(
                source_kind="project_repository",
                source_id=candidate.source_id,
                project_id=candidate.project_id,
                classification="server_only_repository",
                authority_kind="project_repository",
                authority_id=candidate.source_id,
                repository_owner=str(gitea_client.repository_owner),
                repo_name=candidate.repo_name,
                access_mode="none",
            )
        repository = await db.get_project_repository(candidate.source_id)
        if repository is None:
            return _ambiguous(candidate, "project_repository_missing")
        access_mode = project_repository_access_mode(repository)
        if access_mode is None:
            return _ambiguous(candidate, "project_repository_mode_unavailable")
        if not await db.managed_repository_scope_is_unambiguous(
            repo_name=candidate.repo_name,
            authority_kind="project_repository",
            authority_id=candidate.source_id,
            project_id=candidate.project_id,
        ):
            return _ambiguous(candidate, "repository_scope_ambiguous")
        if not _url_matches_managed_scope(
            candidate.observed_url,
            expected_clean_url=gitea_client.clean_repo_url(candidate.repo_name),
        ):
            return _ambiguous(candidate, "repository_coordinate_mismatch")
        return LegacyReconciliationPlan(
            source_kind="project_repository",
            source_id=candidate.source_id,
            project_id=candidate.project_id,
            classification=(
                "shared_project_jobs_repository"
                if candidate.role == "jobs"
                else "project_runtime_repository"
            ),
            authority_kind="project_repository",
            authority_id=candidate.source_id,
            repository_owner=str(gitea_client.repository_owner),
            repo_name=candidate.repo_name,
            access_mode=access_mode,
        )

    return _ambiguous(candidate, "source_kind_unsupported")


def _plan_matches_claim(
    plan: LegacyReconciliationPlan, claim: Mapping[str, Any]
) -> bool:
    return all(
        (
            plan.project_id
            == (str(claim["project_id"]) if claim.get("project_id") else None),
            plan.classification == str(claim["classification"]),
            plan.authority_kind
            == (str(claim["authority_kind"]) if claim.get("authority_kind") else None),
            plan.authority_id
            == (str(claim["authority_id"]) if claim.get("authority_id") else None),
            plan.authority_record_id
            == (
                str(claim["authority_record_id"])
                if claim.get("authority_record_id")
                else None
            ),
            plan.authority_generation
            == (
                int(claim["authority_generation"])
                if claim.get("authority_generation") is not None
                else None
            ),
            plan.repository_owner
            == (
                str(claim["repository_owner"])
                if claim.get("repository_owner")
                else None
            ),
            plan.repo_name
            == (str(claim["repo_name"]) if claim.get("repo_name") else None),
            plan.access_mode
            == (str(claim["access_mode"]) if claim.get("access_mode") else None),
        )
    )


async def _contain_exact_terminal_or_orphan_authority(
    db: Any,
    gitea_client: Any,
    *,
    source_kind: str,
    source_id: str,
    project_id: str | None,
    authority_kind: str | None,
    authority_id: str | None,
    authority_record_id: str | None,
    authority_generation: int | None,
    repository_owner: str | None,
    repo_name: str | None,
    access_mode: str | None,
    source_absent_or_changed: bool = False,
    reconciliation_id: str | None = None,
    claim_token: int | None = None,
) -> bool:
    """Key-first contain an authority owned only by this exact source.

    A root job may use a project ``jobs`` repository and a subjob may use its
    root's authority.  Neither key belongs exclusively to the source row being
    terminalized/deleted, so those shapes are intentionally no-ops here.
    Exact job and thread authorities are containable only once the database's
    locked lifecycle check proves no legitimate consumer remains (including
    every descendant of a root job). An absent/changed ``project_repository``
    source also owns its key exactly; the important prohibition is never
    revoking that shared authority for a *job* source.
    """

    containable_source_kinds = {"job", "thread"}
    if source_absent_or_changed:
        containable_source_kinds.add("project_repository")
    if (
        source_kind not in containable_source_kinds
        or authority_kind != source_kind
        or authority_id != source_id
        or not repository_owner
        or not repo_name
        or not access_mode
    ):
        return False
    authority = await db.get_managed_repository_authority(
        repo_name,
        repository_owner=repository_owner,
        include_private_key=False,
        active_only=False,
    )
    if authority is None or str(authority.get("status") or "") == "revoked":
        return False
    if (
        str(authority.get("authority_kind") or "") != authority_kind
        or str(authority.get("authority_id") or "") != authority_id
        or str(authority.get("project_id") or "") != str(project_id or "")
        or str(authority.get("repository_owner") or "") != repository_owner
        or str(authority.get("repo_name") or "") != repo_name
        or str(authority.get("access_mode") or "") != access_mode
        or (
            authority_record_id is not None
            and str(authority.get("id") or "") != authority_record_id
        )
        or (
            authority_generation is not None
            and int(authority.get("generation") or 0) != authority_generation
        )
    ):
        raise ManagedRepositoryAuthorityError("repository_scope_conflict")
    if str(getattr(gitea_client, "repository_owner", "") or "") != repository_owner:
        # Forge helpers mutate the client-configured namespace; a stored owner
        # must never be treated as a method argument that those helpers honor.
        # Otherwise a wrong-namespace 404 could be mistaken for successful key
        # containment and the database would revoke still-live authority.
        raise ManagedRepositoryAuthorityError("repository_owner_mismatch")
    if reconciliation_id is None or claim_token is None:
        raise ManagedRepositoryAuthorityError("reconciliation_claim_lost")
    if not await db.managed_repository_legacy_reconciliation_claim_is_current(
        reconciliation_id, claim_token
    ):
        raise ManagedRepositoryAuthorityError("reconciliation_claim_lost")

    forge_key_id = authority.get("forge_key_id")
    if forge_key_id is None:
        # A response/process loss may leave a durable provisioning or revoking
        # generation whose deterministic forge key exists but whose numeric ID
        # was never recorded. Re-adopt that exact public key/title, persist the
        # ID through the immutable generation CAS, then continue key-first
        # containment. No private material is needed for this recovery.
        forge_key_id = await gitea_client.ensure_repo_deploy_key(
            repo_name,
            title=(f"srw-managed-{authority['id']}-{authority['access_mode']}"),
            public_key=str(authority["public_key"]),
            access_mode=str(authority["access_mode"]),
        )
        if forge_key_id is None:
            raise ManagedRepositoryAuthorityError("repository_key_unavailable")
        authority = await db.record_managed_repository_authority_forge_key(
            str(authority["id"]),
            repository_owner=repository_owner,
            repo_name=repo_name,
            authority_kind=authority_kind,
            authority_scope_id=authority_id,
            project_id=project_id,
            generation=int(authority["generation"]),
            access_mode=access_mode,
            public_key_fingerprint=str(authority["public_key_fingerprint"]),
            forge_key_id=int(forge_key_id),
        )
        if authority is None:
            raise ManagedRepositoryAuthorityError("repository_authority_raced")

    if not await db.managed_repository_legacy_reconciliation_claim_is_current(
        reconciliation_id, claim_token
    ):
        raise ManagedRepositoryAuthorityError("reconciliation_claim_lost")
    claimed = await db.claim_managed_repository_authority_revoke_exact(
        reconciliation_id,
        claim_token,
        str(authority["id"]),
        repository_owner=repository_owner,
        repo_name=repo_name,
        authority_kind=authority_kind,
        authority_scope_id=authority_id,
        project_id=project_id,
        generation=int(authority["generation"]),
        access_mode=access_mode,
        public_key_fingerprint=str(authority["public_key_fingerprint"]),
    )
    if claimed is None:
        current = await db.get_managed_repository_authority(
            repo_name,
            repository_owner=repository_owner,
            include_private_key=False,
            active_only=False,
        )
        if current is None or str(current.get("status") or "") == "revoked":
            return False
        # The locked lifecycle check may reject containment because a root
        # still has a resumable descendant or because the source changed after
        # discovery. Do not revoke; the subsequent source CAS decides whether
        # a credential-bearing terminal row can still be safely scrubbed.
        return False
    claimed_key_id = claimed.get("forge_key_id")
    if claimed_key_id is None:
        raise ManagedRepositoryAuthorityError("repository_key_unavailable")
    if not await db.managed_repository_legacy_reconciliation_claim_is_current(
        reconciliation_id, claim_token
    ):
        raise ManagedRepositoryAuthorityError("reconciliation_claim_lost")
    revoked = await gitea_client.delete_repo_deploy_key(repo_name, int(claimed_key_id))
    if not revoked or not await db.finish_managed_repository_authority_revoke(
        str(claimed["id"])
    ):
        raise ManagedRepositoryAuthorityError("repository_authority_revocation_failed")
    return True


async def _ensure_claim_authority(
    db: Any,
    gitea_client: Any,
    candidate: LegacyRepositoryCandidate,
) -> dict[str, Any]:
    if candidate.source_kind == "job":
        source = await db.get_job(candidate.source_id)
        authority = await ensure_job_repository_authority(
            db, gitea_client, source or {}
        )
    elif candidate.source_kind == "thread":
        source = await db.get_thread(candidate.source_id)
        authority = await ensure_thread_repository_authority(
            db, gitea_client, source or {}
        )
    else:
        source = await db.get_project_repository(candidate.source_id)
        authority = await rotate_project_repository_authority(
            db, gitea_client, source or {}
        )
    if authority is None:
        raise ManagedRepositoryAuthorityError("repository_authority_unavailable")
    return authority


async def _settle_bound_terminal_claim(
    db: Any,
    gitea_client: Any,
    claim: Mapping[str, Any],
    *,
    reconciliation_id: str,
    claim_token: int,
) -> bool:
    """Contain and settle one exact active-authority lifecycle intent.

    Rediscovery can observe an owner after its URL was scrubbed, deleted, or
    changed to another repository mode. Reclassifying that current row would
    discard the exact old authority identity. Keep the durable generation
    binding authoritative and let the database repeat lifecycle/scope checks
    under locks before both revocation and settlement.
    """

    await _contain_exact_terminal_or_orphan_authority(
        db,
        gitea_client,
        source_kind=str(claim["source_kind"]),
        source_id=str(claim["source_id"]),
        project_id=(str(claim["project_id"]) if claim.get("project_id") else None),
        authority_kind=(
            str(claim["authority_kind"]) if claim.get("authority_kind") else None
        ),
        authority_id=(
            str(claim["authority_id"]) if claim.get("authority_id") else None
        ),
        authority_record_id=(
            str(claim["authority_record_id"])
            if claim.get("authority_record_id")
            else None
        ),
        authority_generation=(
            int(claim["authority_generation"])
            if claim.get("authority_generation") is not None
            else None
        ),
        repository_owner=(
            str(claim["repository_owner"]) if claim.get("repository_owner") else None
        ),
        repo_name=(str(claim["repo_name"]) if claim.get("repo_name") else None),
        access_mode=(str(claim["access_mode"]) if claim.get("access_mode") else None),
        source_absent_or_changed=True,
        reconciliation_id=reconciliation_id,
        claim_token=claim_token,
    )
    return bool(
        await db.finish_missing_or_clean_managed_repository_legacy_reconciliation(
            reconciliation_id,
            claim_token,
        )
    )


async def _process_claim(
    db: Any,
    gitea_client: Any,
    claim: Mapping[str, Any],
    *,
    max_attempts: int,
) -> str:
    reconciliation_id = str(claim["id"])
    claim_token = int(claim["claim_token"])
    attempts = int(claim.get("attempts") or 1)
    candidate_row = await db.get_managed_repository_legacy_candidate(
        str(claim["source_kind"]), str(claim["source_id"])
    )
    if str(claim["classification"]) == "terminal_historical":
        try:
            if await _settle_bound_terminal_claim(
                db,
                gitea_client,
                claim,
                reconciliation_id=reconciliation_id,
                claim_token=claim_token,
            ):
                return "scrubbed_terminal"
        except ManagedRepositoryAuthorityError as exc:
            if exc.code in _AMBIGUOUS_AUTHORITY_CODES:
                await db.mark_managed_repository_legacy_reconciliation_ambiguous(
                    reconciliation_id,
                    claim_token,
                    reason_code=exc.code,
                )
                return "ambiguous"
            await db.retry_managed_repository_legacy_reconciliation(
                reconciliation_id,
                claim_token,
                reason_code=exc.code,
                delay_seconds=legacy_reconciliation_retry_delay(attempts),
                max_attempts=max_attempts,
            )
            return "deferred"
        except Exception as exc:
            await db.retry_managed_repository_legacy_reconciliation(
                reconciliation_id,
                claim_token,
                reason_code=_safe_exception_code(exc),
                delay_seconds=legacy_reconciliation_retry_delay(attempts),
                max_attempts=max_attempts,
            )
            return "deferred"
        if candidate_row is None:
            await db.retry_managed_repository_legacy_reconciliation(
                reconciliation_id,
                claim_token,
                reason_code="source_snapshot_changed",
                delay_seconds=legacy_reconciliation_retry_delay(attempts),
                max_attempts=max_attempts,
            )
            return "deferred"
    if candidate_row is None:
        # A normal lazy attach/dispatch may have completed the same adoption
        # after this claim was leased.  A credential-free row is not enough:
        # settle only when the exact active authority and locked source facts
        # still match this durable intent.
        if str(claim["classification"]) in _AUTHORITY_CLASSIFICATIONS:
            authority = await db.get_managed_repository_authority(
                str(claim.get("repo_name") or ""),
                repository_owner=str(claim.get("repository_owner") or ""),
                include_private_key=False,
            )
            if authority is not None:
                result = await db.finish_managed_repository_legacy_reconciliation(
                    reconciliation_id,
                    claim_token,
                    observed_url=None,
                    authority_id=str(authority["id"]),
                )
                if result is not None:
                    return "adopted"
        # ``candidate_row is None`` also means a concurrent winner already
        # scrubbed the URL.  A stale/expired predecessor must never interpret
        # that successful adoption as source deletion and revoke the winner's
        # exact active key.  Let the token-fenced finish/reclaim path settle the
        # clean source; containment below is reserved for an actually absent
        # owner row.
        if await _legacy_source_exists(
            db, str(claim["source_kind"]), str(claim["source_id"])
        ):
            await db.retry_managed_repository_legacy_reconciliation(
                reconciliation_id,
                claim_token,
                reason_code="source_no_longer_credentialed",
                delay_seconds=legacy_reconciliation_retry_delay(attempts),
                max_attempts=max_attempts,
            )
            return "deferred"
        try:
            await _contain_exact_terminal_or_orphan_authority(
                db,
                gitea_client,
                source_kind=str(claim["source_kind"]),
                source_id=str(claim["source_id"]),
                project_id=(
                    str(claim["project_id"]) if claim.get("project_id") else None
                ),
                authority_kind=(
                    str(claim["authority_kind"])
                    if claim.get("authority_kind")
                    else None
                ),
                authority_id=(
                    str(claim["authority_id"]) if claim.get("authority_id") else None
                ),
                authority_record_id=(
                    str(claim["authority_record_id"])
                    if claim.get("authority_record_id")
                    else None
                ),
                authority_generation=(
                    int(claim["authority_generation"])
                    if claim.get("authority_generation") is not None
                    else None
                ),
                repository_owner=(
                    str(claim["repository_owner"])
                    if claim.get("repository_owner")
                    else None
                ),
                repo_name=(str(claim["repo_name"]) if claim.get("repo_name") else None),
                access_mode=(
                    str(claim["access_mode"]) if claim.get("access_mode") else None
                ),
                source_absent_or_changed=True,
                reconciliation_id=reconciliation_id,
                claim_token=claim_token,
            )
        except ManagedRepositoryAuthorityError as exc:
            if exc.code in _AMBIGUOUS_AUTHORITY_CODES:
                await db.mark_managed_repository_legacy_reconciliation_ambiguous(
                    reconciliation_id,
                    claim_token,
                    reason_code=exc.code,
                )
                return "ambiguous"
            await db.retry_managed_repository_legacy_reconciliation(
                reconciliation_id,
                claim_token,
                reason_code=exc.code,
                delay_seconds=legacy_reconciliation_retry_delay(attempts),
                max_attempts=max_attempts,
            )
            return "deferred"
        except Exception as exc:
            await db.retry_managed_repository_legacy_reconciliation(
                reconciliation_id,
                claim_token,
                reason_code=_safe_exception_code(exc),
                delay_seconds=legacy_reconciliation_retry_delay(attempts),
                max_attempts=max_attempts,
            )
            return "deferred"
        await db.mark_managed_repository_legacy_reconciliation_ambiguous(
            reconciliation_id,
            claim_token,
            reason_code="source_missing_or_changed",
        )
        return "ambiguous"
    candidate = LegacyRepositoryCandidate.from_row(candidate_row)
    try:
        plan = await classify_managed_repository_legacy_candidate(
            db, gitea_client, candidate
        )
        plan = await _bind_existing_exact_authority(db, plan)
        if not plan.actionable:
            await db.mark_managed_repository_legacy_reconciliation_ambiguous(
                reconciliation_id,
                claim_token,
                reason_code=plan.reason_code or "scope_ambiguous",
            )
            return "ambiguous"
        if not _plan_matches_claim(plan, claim):
            await db.retry_managed_repository_legacy_reconciliation(
                reconciliation_id,
                claim_token,
                reason_code="source_authority_changed",
                delay_seconds=60,
                max_attempts=max_attempts,
            )
            return "deferred"

        authority: dict[str, Any] | None = None
        if plan.classification in _AUTHORITY_CLASSIFICATIONS:
            authority = await _ensure_claim_authority(db, gitea_client, candidate)
            if (
                str(authority.get("authority_kind") or "") != plan.authority_kind
                or str(authority.get("authority_id") or "") != plan.authority_id
                or str(authority.get("project_id") or "") != str(plan.project_id or "")
                or str(authority.get("repo_name") or "") != plan.repo_name
                or str(authority.get("access_mode") or "") != plan.access_mode
                or str(authority.get("status") or "") != "active"
            ):
                raise ManagedRepositoryAuthorityError("repository_authority_raced")
        elif plan.classification == "terminal_historical":
            await _contain_exact_terminal_or_orphan_authority(
                db,
                gitea_client,
                source_kind=plan.source_kind,
                source_id=plan.source_id,
                project_id=plan.project_id,
                authority_kind=plan.authority_kind,
                authority_id=plan.authority_id,
                authority_record_id=plan.authority_record_id,
                authority_generation=plan.authority_generation,
                repository_owner=plan.repository_owner,
                repo_name=plan.repo_name,
                access_mode=plan.access_mode,
                reconciliation_id=reconciliation_id,
                claim_token=claim_token,
            )
        result = await db.finish_managed_repository_legacy_reconciliation(
            reconciliation_id,
            claim_token,
            observed_url=candidate.observed_url,
            authority_id=(str(authority["id"]) if authority else None),
        )
        if result is None:
            await db.retry_managed_repository_legacy_reconciliation(
                reconciliation_id,
                claim_token,
                reason_code="source_snapshot_changed",
                delay_seconds=legacy_reconciliation_retry_delay(attempts),
                max_attempts=max_attempts,
            )
            return "deferred"
        return "adopted" if result == "adopted" else "scrubbed_terminal"
    except asyncio.CancelledError:
        raise
    except ManagedRepositoryAuthorityError as exc:
        if exc.code in _AMBIGUOUS_AUTHORITY_CODES:
            await db.mark_managed_repository_legacy_reconciliation_ambiguous(
                reconciliation_id,
                claim_token,
                reason_code=exc.code,
            )
            return "ambiguous"
        await db.retry_managed_repository_legacy_reconciliation(
            reconciliation_id,
            claim_token,
            reason_code=exc.code,
            delay_seconds=legacy_reconciliation_retry_delay(attempts),
            max_attempts=max_attempts,
        )
        return "deferred"
    except Exception as exc:
        await db.retry_managed_repository_legacy_reconciliation(
            reconciliation_id,
            claim_token,
            reason_code=_safe_exception_code(exc),
            delay_seconds=legacy_reconciliation_retry_delay(attempts),
            max_attempts=max_attempts,
        )
        return "deferred"


async def scan_managed_repository_legacy_sources(
    db: Any,
    gitea_client: Any,
    *,
    apply: bool = False,
    page_size: int = 100,
) -> tuple[int, Counter[str], list[dict[str, str]]]:
    """Scan all current candidates without an in-memory correctness window."""

    scanned = 0
    classifications: Counter[str] = Counter()
    ambiguous: list[dict[str, str]] = []
    after_kind: str | None = None
    after_id: str | None = None
    while True:
        rows = await db.list_managed_repository_legacy_candidates(
            after_kind=after_kind,
            after_id=after_id,
            limit=page_size,
        )
        if not rows:
            break
        for row in rows:
            candidate = LegacyRepositoryCandidate.from_row(row)
            try:
                plan = await classify_managed_repository_legacy_candidate(
                    db, gitea_client, candidate
                )
                plan = await _bind_existing_exact_authority(db, plan)
            except ManagedRepositoryAuthorityError as exc:
                plan = _ambiguous(candidate, exc.code)
            except Exception as exc:
                plan = _ambiguous(candidate, _safe_exception_code(exc))
            scanned += 1
            classifications[plan.classification] += 1
            if plan.classification == "ambiguous" and len(ambiguous) < 100:
                ambiguous.append(
                    {
                        "source_kind": plan.source_kind,
                        "source_id": plan.source_id,
                        "reason_code": plan.reason_code or "scope_ambiguous",
                    }
                )
            if apply:
                await db.upsert_managed_repository_legacy_reconciliation(
                    **plan.persistence_kwargs()
                )
        tail = rows[-1]
        after_kind = str(tail["source_kind"])
        after_id = str(tail["source_id"])
    return scanned, classifications, ambiguous


async def _scan_managed_repository_active_authority_lifecycle(
    db: Any,
    *,
    apply: bool,
    page_size: int,
) -> tuple[int, Counter[str]]:
    """Rediscover exact keys whose clean owners later became disposable.

    Credential scanning alone cannot see an owner after successful adoption.
    The database computes this bounded candidate set from current authoritative
    lifecycle facts (including complete job lineages and canonical stateless
    retirement). Final exact-row revocation repeats those checks under the
    reconciliation token and source locks.
    """

    scanned = 0
    classifications: Counter[str] = Counter()
    after_kind: str | None = None
    after_id: str | None = None
    while True:
        rows = await db.list_managed_repository_legacy_active_authority_candidates(
            after_kind=after_kind,
            after_id=after_id,
            limit=page_size,
        )
        if not rows:
            break
        for row in rows:
            if not row.get("containment_candidate"):
                continue
            source_kind = str(row["source_kind"])
            source_id = str(row["source_id"])
            plan = LegacyReconciliationPlan(
                source_kind=source_kind,
                source_id=source_id,
                project_id=(str(row["project_id"]) if row.get("project_id") else None),
                classification="terminal_historical",
                authority_kind=source_kind,
                authority_id=source_id,
                authority_record_id=str(row["authority_record_id"]),
                authority_generation=int(row["authority_generation"]),
                repository_owner=str(row["repository_owner"]),
                repo_name=str(row["repo_name"]),
                access_mode=str(row["access_mode"]),
                reason_code=str(
                    row.get("containment_reason") or "authority_lifecycle_terminal"
                ),
            )
            scanned += 1
            classifications[plan.classification] += 1
            if apply:
                await db.upsert_managed_repository_legacy_reconciliation(
                    **plan.persistence_kwargs()
                )
        tail = rows[-1]
        after_kind = str(tail["source_kind"])
        after_id = str(tail["authority_record_id"])
    return scanned, classifications


async def reconcile_managed_repository_legacy_once(
    db: Any,
    gitea_client: Any,
    *,
    apply: bool = False,
    page_size: int = 100,
    concurrency: int = 4,
    lease_seconds: int = 300,
    max_attempts: int = 8,
    claimant_id: str | None = None,
) -> tuple[LegacyReconciliationStats, dict[str, Any]]:
    """Discover and, only with explicit apply, drain all currently due work."""

    (
        scanned,
        classifications,
        ambiguous_rows,
    ) = await scan_managed_repository_legacy_sources(
        db,
        gitea_client,
        apply=apply,
        page_size=max(1, min(int(page_size), 500)),
    )
    (
        authority_scanned,
        authority_classifications,
    ) = await _scan_managed_repository_active_authority_lifecycle(
        db,
        apply=apply,
        page_size=max(1, min(int(page_size), 500)),
    )
    scanned += authority_scanned
    classifications.update(authority_classifications)
    if not apply:
        stats = LegacyReconciliationStats(
            scanned=scanned,
            ambiguous=classifications["ambiguous"],
            deferred=scanned - classifications["ambiguous"],
        )
        return stats, {
            "dry_run": True,
            "classifications": dict(sorted(classifications.items())),
            "ambiguous": ambiguous_rows,
        }

    outcomes: Counter[str] = Counter()
    worker_id = claimant_id or str(uuid4())
    batch_size = max(1, min(int(concurrency), 16))
    await db.settle_inactive_managed_repository_legacy_reconciliations()
    while True:
        claims = await db.claim_managed_repository_legacy_reconciliations(
            claimant_id=worker_id,
            limit=batch_size,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
        if not claims:
            break
        results = await asyncio.gather(
            *(
                _process_claim(
                    db,
                    gitea_client,
                    claim,
                    max_attempts=max_attempts,
                )
                for claim in claims
            )
        )
        outcomes.update(results)
    # Sources may disappear or become credential-free after discovery but before
    # their claim is processed.  Converge those rows in this invocation instead
    # of requiring a second operator run merely to settle an inactive ledger row.
    await db.settle_inactive_managed_repository_legacy_reconciliations()
    progress = await db.get_managed_repository_legacy_reconciliation_progress()
    stats = LegacyReconciliationStats(
        scanned=scanned,
        adopted=outcomes["adopted"],
        scrubbed_terminal=outcomes["scrubbed_terminal"],
        deferred=outcomes["deferred"],
        failed=sum(
            int(row["count"]) for row in progress["counts"] if row["state"] == "failed"
        ),
        ambiguous=sum(
            int(row["count"])
            for row in progress["counts"]
            if row["state"] == "ambiguous"
        ),
    )
    return stats, {"dry_run": False, "progress": progress}


def serialize_legacy_reconciliation_report(
    stats: LegacyReconciliationStats, details: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a JSON-safe, coordinate-free operator report."""

    def encode(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, Mapping):
            return {str(key): encode(item) for key, item in value.items()}
        if isinstance(value, list):
            return [encode(item) for item in value]
        return value

    return encode(
        {
            "mode": "dry-run" if details.get("dry_run") else "apply",
            "scanned": stats.scanned,
            "adopted": stats.adopted,
            "scrubbed_terminal": stats.scrubbed_terminal,
            "deferred": stats.deferred,
            "failed": stats.failed,
            "ambiguous": stats.ambiguous,
            **details,
            "contains_urls_or_credentials": False,
        }
    )


__all__ = [
    "LegacyReconciliationPlan",
    "LegacyReconciliationStats",
    "LegacyRepositoryCandidate",
    "classify_managed_repository_legacy_candidate",
    "legacy_reconciliation_retry_delay",
    "reconcile_managed_repository_legacy_once",
    "scan_managed_repository_legacy_sources",
    "serialize_legacy_reconciliation_report",
]
