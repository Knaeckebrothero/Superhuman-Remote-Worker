from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

import orchestrator.services.managed_repository_reconciliation as reconciliation
from orchestrator.services.managed_repository_reconciliation import (
    LegacyRepositoryCandidate,
    classify_managed_repository_legacy_candidate,
    legacy_reconciliation_retry_delay,
    reconcile_managed_repository_legacy_once,
    serialize_legacy_reconciliation_report,
)


def _candidate(**overrides):
    source_id = str(overrides.pop("source_id", uuid4()))
    defaults = {
        "source_kind": "job",
        "source_id": source_id,
        "project_id": None,
        "observed_url": (f"http://admin:secret@gitea:3000/srw/job-{source_id[:8]}.git"),
        "repo_name": f"job-{source_id[:8]}",
        "role": None,
        "read_only": None,
        "is_managed": None,
        "project_status": None,
        "source_status": "created",
        "completion_outcome_kind": None,
        "parent_job_id": None,
        "branch_name": "job/test",
        "execution_lane": "pinned",
        "source_metadata": {},
        "current_officer": False,
    }
    defaults.update(overrides)
    return LegacyRepositoryCandidate(**defaults)


def _gitea():
    client = MagicMock()
    client.repository_owner = "srw"
    client.clean_repo_url = MagicMock(
        side_effect=lambda name: f"http://gitea:3000/srw/{name}.git"
    )
    return client


def _job_db(candidate):
    db = MagicMock()
    db.get_job = AsyncMock(
        return_value={
            "id": candidate.source_id,
            "project_id": candidate.project_id,
            "parent_job_id": candidate.parent_job_id,
            "repo_name": candidate.repo_name,
        }
    )
    db.managed_repository_scope_is_unambiguous = AsyncMock(return_value=True)
    return db


def _settled_retirement(*, permanent: bool) -> dict:
    return {
        "_stateless_workspace_retirement_settled": {
            "terminal_token": 8,
            "cleanup_complete": True,
            "permanent": permanent,
            "backing_id": None,
            "runtime_incarnation": None,
            "snapshot_restore_required": False,
        }
    }


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [(1, 60), (2, 120), (3, 240), (4, 480), (5, 900), (99, 900)],
)
def test_legacy_reconciliation_backoff_is_bounded(attempts, expected):
    assert legacy_reconciliation_retry_delay(attempts) == expected


@pytest.mark.asyncio
async def test_completed_job_scrubs_without_repository_authority():
    candidate = _candidate(source_status="completed")
    db = _job_db(candidate)
    plan = await classify_managed_repository_legacy_candidate(db, _gitea(), candidate)
    assert plan.classification == "terminal_historical"
    assert plan.authority_id == candidate.source_id
    assert plan.access_mode == "write"
    db.get_job.assert_awaited_once_with(candidate.source_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_explicitly_resumable_job_statuses_adopt_repository_authority(status):
    candidate = _candidate(source_status=status)
    plan = await classify_managed_repository_legacy_candidate(
        _job_db(candidate), _gitea(), candidate
    )
    assert plan.classification == "runnable_job"
    assert plan.authority_id == candidate.source_id
    assert plan.access_mode == "write"


@pytest.mark.asyncio
async def test_blocked_undelivered_cancelled_job_scrubs_without_authority():
    candidate = _candidate(
        source_status="cancelled",
        completion_outcome_kind="blocked_undelivered",
    )
    plan = await classify_managed_repository_legacy_candidate(
        _job_db(candidate), _gitea(), candidate
    )
    assert plan.classification == "terminal_historical"


@pytest.mark.asyncio
async def test_blocked_undelivered_on_non_cancelled_job_fails_closed():
    candidate = _candidate(
        source_status="failed",
        completion_outcome_kind="blocked_undelivered",
    )
    plan = await classify_managed_repository_legacy_candidate(
        _job_db(candidate), _gitea(), candidate
    )
    assert plan.classification == "ambiguous"
    assert plan.reason_code == "job_completion_outcome_lifecycle_conflict"


@pytest.mark.asyncio
async def test_permanently_retired_stateless_thread_scrubs_without_key():
    thread_id = str(uuid4())
    repo_name = f"thread-{thread_id[:8]}"
    candidate = _candidate(
        source_kind="thread",
        source_id=thread_id,
        repo_name=repo_name,
        observed_url=f"http://admin:secret@gitea:3000/srw/{repo_name}.git",
        source_status="ended",
        execution_lane="stateless",
        source_metadata=_settled_retirement(permanent=True),
    )
    db = MagicMock()
    db.managed_repository_scope_is_unambiguous = AsyncMock(return_value=True)
    plan = await classify_managed_repository_legacy_candidate(db, _gitea(), candidate)
    assert plan.classification == "terminal_historical"
    assert plan.authority_kind == "thread"


@pytest.mark.asyncio
async def test_soft_retired_stateless_thread_keeps_resume_authority():
    thread_id = str(uuid4())
    repo_name = f"thread-{thread_id[:8]}"
    candidate = _candidate(
        source_kind="thread",
        source_id=thread_id,
        repo_name=repo_name,
        observed_url=f"http://admin:secret@gitea:3000/srw/{repo_name}.git",
        source_status="ended",
        execution_lane="stateless",
        source_metadata=_settled_retirement(permanent=False),
    )
    db = MagicMock()
    db.managed_repository_scope_is_unambiguous = AsyncMock(return_value=True)

    plan = await classify_managed_repository_legacy_candidate(db, _gitea(), candidate)

    assert plan.classification == "resumable_thread"
    assert plan.authority_id == thread_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_metadata",
    [
        {
            "_stateless_workspace_retirement_settled": {
                "terminal_token": 8,
                "permanent": True,
                "snapshot_restore_required": False,
            }
        },
        {
            **_settled_retirement(permanent=True),
            "_stateless_workspace_retirement_pending": True,
            "_stateless_claim_retirement": {"permanent": True},
        },
    ],
    ids=["malformed-settled-proof", "settled-pending-overlap"],
)
async def test_invalid_stateless_retirement_proof_fails_closed(source_metadata):
    thread_id = str(uuid4())
    repo_name = f"thread-{thread_id[:8]}"
    candidate = _candidate(
        source_kind="thread",
        source_id=thread_id,
        repo_name=repo_name,
        observed_url=f"http://admin:secret@gitea:3000/srw/{repo_name}.git",
        source_status="ended",
        execution_lane="stateless",
        source_metadata=source_metadata,
    )
    db = MagicMock()
    db.managed_repository_scope_is_unambiguous = AsyncMock(return_value=True)

    plan = await classify_managed_repository_legacy_candidate(db, _gitea(), candidate)

    assert plan.classification == "ambiguous"
    assert plan.reason_code == "stateless_retirement_authority_malformed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_metadata",
    [
        {
            "workspace_container": {
                "status": "suspended",
                "_snapshot_restore_required": True,
            },
            "last_memory_archive_at": "2026-08-25T00:00:00Z",
        },
        {
            "workspace_container": {
                "status": "ready",
                "provisioner": "k8s",
            },
            "config_override": {},
        },
    ],
    ids=["idle-ended", "orphan-ended"],
)
async def test_ended_persistent_thread_adopts_resume_authority(source_metadata):
    thread_id = str(uuid4())
    repo_name = f"thread-{thread_id[:8]}"
    candidate = _candidate(
        source_kind="thread",
        source_id=thread_id,
        repo_name=repo_name,
        observed_url=f"http://admin:secret@gitea:3000/srw/{repo_name}.git",
        source_status="ended",
        execution_lane="pinned",
        source_metadata=source_metadata,
    )
    db = MagicMock()
    db.managed_repository_scope_is_unambiguous = AsyncMock(return_value=True)
    plan = await classify_managed_repository_legacy_candidate(db, _gitea(), candidate)
    assert plan.classification == "resumable_thread"
    assert plan.authority_kind == "thread"
    assert plan.authority_id == thread_id


@pytest.mark.asyncio
async def test_pending_permanent_stateless_retirement_still_adopts_authority():
    thread_id = str(uuid4())
    repo_name = f"thread-{thread_id[:8]}"
    candidate = _candidate(
        source_kind="thread",
        source_id=thread_id,
        repo_name=repo_name,
        observed_url=f"http://admin:secret@gitea:3000/srw/{repo_name}.git",
        source_status="ended",
        execution_lane="stateless",
        source_metadata={
            "_stateless_workspace_retirement_pending": True,
            "_stateless_claim_retirement": {
                "permanent": True,
                "terminal_token": 8,
            },
        },
    )
    db = MagicMock()
    db.managed_repository_scope_is_unambiguous = AsyncMock(return_value=True)

    plan = await classify_managed_repository_legacy_candidate(db, _gitea(), candidate)

    assert plan.classification == "resumable_thread"
    assert plan.authority_id == thread_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_kind", "access_mode"),
    [("job", "write"), ("thread", "write"), ("project_repository", "read")],
)
async def test_exact_orphan_authority_is_contained(source_kind, access_mode):
    source_id = str(uuid4())
    project_id = str(uuid4())
    authority_id = str(uuid4())
    authority = {
        "id": authority_id,
        "status": "active",
        "authority_kind": source_kind,
        "authority_id": source_id,
        "project_id": project_id,
        "repository_owner": "srw",
        "repo_name": "legacy-repository",
        "access_mode": access_mode,
        "generation": 1,
        "public_key_fingerprint": "SHA256:exact",
        "forge_key_id": 91,
    }
    db = MagicMock()
    db.get_managed_repository_authority = AsyncMock(return_value=authority)
    db.managed_repository_legacy_reconciliation_claim_is_current = AsyncMock(
        return_value=True
    )
    db.claim_managed_repository_authority_revoke_exact = AsyncMock(
        return_value={**authority, "status": "revoking"}
    )
    db.finish_managed_repository_authority_revoke = AsyncMock(return_value=True)
    gitea = _gitea()
    gitea.delete_repo_deploy_key = AsyncMock(return_value=True)

    await reconciliation._contain_exact_terminal_or_orphan_authority(
        db,
        gitea,
        source_kind=source_kind,
        source_id=source_id,
        project_id=project_id,
        authority_kind=source_kind,
        authority_id=source_id,
        authority_record_id=authority_id,
        authority_generation=1,
        repository_owner="srw",
        repo_name="legacy-repository",
        access_mode=access_mode,
        source_absent_or_changed=True,
        reconciliation_id=str(uuid4()),
        claim_token=1,
    )

    db.claim_managed_repository_authority_revoke_exact.assert_awaited_once()
    gitea.delete_repo_deploy_key.assert_awaited_once_with("legacy-repository", 91)
    db.finish_managed_repository_authority_revoke.assert_awaited_once_with(authority_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["provisioning", "revoking"])
async def test_exact_containment_recovers_unrecorded_forge_key(status):
    source_id = str(uuid4())
    authority = {
        "id": str(uuid4()),
        "status": status,
        "authority_kind": "job",
        "authority_id": source_id,
        "project_id": None,
        "repository_owner": "srw",
        "repo_name": "job-unrecorded",
        "access_mode": "write",
        "generation": 1,
        "public_key": "ssh-ed25519 exact-public-key",
        "public_key_fingerprint": "SHA256:unrecorded",
        "forge_key_id": None,
    }
    recorded = {**authority, "forge_key_id": 91}
    claimed = {**recorded, "status": "revoking"}
    db = MagicMock()
    db.get_managed_repository_authority = AsyncMock(return_value=authority)
    db.managed_repository_legacy_reconciliation_claim_is_current = AsyncMock(
        return_value=True
    )
    db.record_managed_repository_authority_forge_key = AsyncMock(return_value=recorded)
    db.claim_managed_repository_authority_revoke_exact = AsyncMock(return_value=claimed)
    db.finish_managed_repository_authority_revoke = AsyncMock(return_value=True)
    gitea = _gitea()
    gitea.ensure_repo_deploy_key = AsyncMock(return_value=91)
    gitea.delete_repo_deploy_key = AsyncMock(return_value=True)

    await reconciliation._contain_exact_terminal_or_orphan_authority(
        db,
        gitea,
        source_kind="job",
        source_id=source_id,
        project_id=None,
        authority_kind="job",
        authority_id=source_id,
        authority_record_id=authority["id"],
        authority_generation=1,
        repository_owner="srw",
        repo_name="job-unrecorded",
        access_mode="write",
        source_absent_or_changed=True,
        reconciliation_id=str(uuid4()),
        claim_token=1,
    )

    gitea.ensure_repo_deploy_key.assert_awaited_once_with(
        "job-unrecorded",
        title=f"srw-managed-{authority['id']}-write",
        public_key=authority["public_key"],
        access_mode="write",
    )
    db.record_managed_repository_authority_forge_key.assert_awaited_once()
    gitea.delete_repo_deploy_key.assert_awaited_once_with("job-unrecorded", 91)
    db.finish_managed_repository_authority_revoke.assert_awaited_once_with(
        authority["id"]
    )


@pytest.mark.asyncio
async def test_expired_reconciliation_claim_cannot_start_external_containment():
    source_id = str(uuid4())
    db = MagicMock()
    db.get_managed_repository_authority = AsyncMock(
        return_value={
            "id": str(uuid4()),
            "status": "active",
            "authority_kind": "job",
            "authority_id": source_id,
            "project_id": None,
            "repository_owner": "srw",
            "repo_name": "job-expired",
            "access_mode": "write",
            "generation": 1,
            "public_key_fingerprint": "SHA256:expired",
            "forge_key_id": 91,
        }
    )
    db.managed_repository_legacy_reconciliation_claim_is_current = AsyncMock(
        return_value=False
    )
    db.claim_managed_repository_authority_revoke_exact = AsyncMock()
    gitea = _gitea()
    gitea.delete_repo_deploy_key = AsyncMock()

    with pytest.raises(reconciliation.ManagedRepositoryAuthorityError) as exc:
        await reconciliation._contain_exact_terminal_or_orphan_authority(
            db,
            gitea,
            source_kind="job",
            source_id=source_id,
            project_id=None,
            authority_kind="job",
            authority_id=source_id,
            authority_record_id=None,
            authority_generation=None,
            repository_owner="srw",
            repo_name="job-expired",
            access_mode="write",
            source_absent_or_changed=True,
            reconciliation_id=str(uuid4()),
            claim_token=4,
        )

    assert exc.value.code == "reconciliation_claim_lost"
    db.claim_managed_repository_authority_revoke_exact.assert_not_awaited()
    gitea.delete_repo_deploy_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_expiring_after_exact_revoke_claim_blocks_forge_deletion():
    source_id = str(uuid4())
    authority = {
        "id": str(uuid4()),
        "status": "active",
        "authority_kind": "job",
        "authority_id": source_id,
        "project_id": None,
        "repository_owner": "srw",
        "repo_name": "job-expired-after-claim",
        "access_mode": "write",
        "generation": 1,
        "public_key_fingerprint": "SHA256:expired-after-claim",
        "forge_key_id": 91,
    }
    db = MagicMock()
    db.get_managed_repository_authority = AsyncMock(return_value=authority)
    db.managed_repository_legacy_reconciliation_claim_is_current = AsyncMock(
        side_effect=[True, True, False]
    )
    db.claim_managed_repository_authority_revoke_exact = AsyncMock(
        return_value={**authority, "status": "revoking"}
    )
    gitea = _gitea()
    gitea.delete_repo_deploy_key = AsyncMock()

    with pytest.raises(reconciliation.ManagedRepositoryAuthorityError) as exc:
        await reconciliation._contain_exact_terminal_or_orphan_authority(
            db,
            gitea,
            source_kind="job",
            source_id=source_id,
            project_id=None,
            authority_kind="job",
            authority_id=source_id,
            authority_record_id=authority["id"],
            authority_generation=1,
            repository_owner="srw",
            repo_name="job-expired-after-claim",
            access_mode="write",
            source_absent_or_changed=True,
            reconciliation_id=str(uuid4()),
            claim_token=5,
        )

    assert exc.value.code == "reconciliation_claim_lost"
    db.claim_managed_repository_authority_revoke_exact.assert_awaited_once()
    gitea.delete_repo_deploy_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_stored_owner_drift_fails_closed_before_forge_mutation():
    source_id = str(uuid4())
    authority = {
        "id": str(uuid4()),
        "status": "active",
        "authority_kind": "job",
        "authority_id": source_id,
        "project_id": None,
        "repository_owner": "legacy-owner",
        "repo_name": "job-owner-drift",
        "access_mode": "write",
        "generation": 1,
        "public_key_fingerprint": "SHA256:owner-drift",
        "forge_key_id": 91,
    }
    db = MagicMock()
    db.get_managed_repository_authority = AsyncMock(return_value=authority)
    db.managed_repository_legacy_reconciliation_claim_is_current = AsyncMock()
    db.claim_managed_repository_authority_revoke_exact = AsyncMock()
    gitea = _gitea()
    gitea.repository_owner = "srw"
    gitea.delete_repo_deploy_key = AsyncMock()

    with pytest.raises(reconciliation.ManagedRepositoryAuthorityError) as exc:
        await reconciliation._contain_exact_terminal_or_orphan_authority(
            db,
            gitea,
            source_kind="job",
            source_id=source_id,
            project_id=None,
            authority_kind="job",
            authority_id=source_id,
            authority_record_id=authority["id"],
            authority_generation=1,
            repository_owner="legacy-owner",
            repo_name="job-owner-drift",
            access_mode="write",
            source_absent_or_changed=True,
            reconciliation_id=str(uuid4()),
            claim_token=6,
        )

    assert exc.value.code == "repository_owner_mismatch"
    db.managed_repository_legacy_reconciliation_claim_is_current.assert_not_awaited()
    db.claim_managed_repository_authority_revoke_exact.assert_not_awaited()
    gitea.delete_repo_deploy_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_job_never_revokes_shared_project_repository_authority():
    db = MagicMock()
    db.get_managed_repository_authority = AsyncMock()

    await reconciliation._contain_exact_terminal_or_orphan_authority(
        db,
        _gitea(),
        source_kind="job",
        source_id=str(uuid4()),
        project_id=str(uuid4()),
        authority_kind="project_repository",
        authority_id=str(uuid4()),
        authority_record_id=None,
        authority_generation=None,
        repository_owner="srw",
        repo_name="shared-jobs-repository",
        access_mode="write",
        source_absent_or_changed=True,
    )

    db.get_managed_repository_authority.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_clean_source_never_revokes_successor_authority():
    source_id = str(uuid4())
    reconciliation_id = str(uuid4())
    repo_name = f"job-{source_id[:8]}"
    claim = {
        "id": reconciliation_id,
        "claim_token": 1,
        "attempts": 1,
        "source_kind": "job",
        "source_id": source_id,
        "project_id": None,
        "classification": "runnable_job",
        "authority_kind": "job",
        "authority_id": source_id,
        "repository_owner": "srw",
        "repo_name": repo_name,
        "access_mode": "write",
    }
    db = MagicMock()
    db.get_managed_repository_legacy_candidate = AsyncMock(return_value=None)
    db.get_managed_repository_authority = AsyncMock(
        return_value={"id": str(uuid4()), "status": "active"}
    )
    db.finish_managed_repository_legacy_reconciliation = AsyncMock(return_value=None)
    db.get_job = AsyncMock(
        return_value={
            "id": source_id,
            "repo_name": repo_name,
            "context": {"git_remote_url": f"http://gitea:3000/srw/{repo_name}.git"},
        }
    )
    db.retry_managed_repository_legacy_reconciliation = AsyncMock(return_value=False)
    db.mark_managed_repository_legacy_reconciliation_ambiguous = AsyncMock()
    gitea = _gitea()
    gitea.delete_repo_deploy_key = AsyncMock(return_value=True)

    result = await reconciliation._process_claim(db, gitea, claim, max_attempts=8)

    assert result == "deferred"
    gitea.delete_repo_deploy_key.assert_not_awaited()
    db.mark_managed_repository_legacy_reconciliation_ambiguous.assert_not_awaited()


@pytest.mark.asyncio
async def test_ended_thread_linked_as_current_officer_fails_closed():
    thread_id = str(uuid4())
    repo_name = f"thread-{thread_id[:8]}"
    candidate = _candidate(
        source_kind="thread",
        source_id=thread_id,
        repo_name=repo_name,
        observed_url=f"http://admin:secret@gitea:3000/srw/{repo_name}.git",
        source_status="ended",
        execution_lane="pinned",
        current_officer=True,
    )
    db = MagicMock()
    db.managed_repository_scope_is_unambiguous = AsyncMock(return_value=True)

    plan = await classify_managed_repository_legacy_candidate(db, _gitea(), candidate)

    assert plan.classification == "ambiguous"
    assert plan.reason_code == "officer_thread_lifecycle_conflict"


@pytest.mark.asyncio
async def test_active_current_officer_thread_gets_exact_write_authority():
    thread_id = str(uuid4())
    repo_name = f"thread-{thread_id[:8]}"
    candidate = _candidate(
        source_kind="thread",
        source_id=thread_id,
        repo_name=repo_name,
        observed_url=f"http://admin:secret@gitea:3000/srw/{repo_name}.git",
        source_status="active",
        execution_lane="pinned",
        current_officer=True,
    )
    db = MagicMock()
    db.managed_repository_scope_is_unambiguous = AsyncMock(return_value=True)

    plan = await classify_managed_repository_legacy_candidate(db, _gitea(), candidate)

    assert plan.classification == "current_officer_thread"
    assert plan.authority_id == thread_id
    assert plan.access_mode == "write"


@pytest.mark.asyncio
async def test_server_only_knowledge_repository_never_gets_runtime_key():
    candidate = _candidate(
        source_kind="project_repository",
        role="knowledge",
        is_managed=True,
        read_only=False,
        repo_name="project-knowledge",
        observed_url=("http://admin:secret@gitea:3000/srw/project-knowledge.git"),
    )
    plan = await classify_managed_repository_legacy_candidate(
        MagicMock(), _gitea(), candidate
    )
    assert plan.classification == "server_only_repository"
    assert plan.authority_id == candidate.source_id
    assert plan.access_mode == "none"


@pytest.mark.asyncio
async def test_foreign_coordinate_is_ambiguous_before_forge_work():
    job_id = str(uuid4())
    repo_name = f"job-{job_id[:8]}"
    candidate = _candidate(
        source_id=job_id,
        repo_name=repo_name,
        observed_url="http://admin:secret@gitea:3000/srw/foreign.git",
    )
    db = MagicMock()
    db.get_job = AsyncMock(
        return_value={
            "id": job_id,
            "project_id": None,
            "parent_job_id": None,
            "repo_name": repo_name,
        }
    )
    db.managed_repository_scope_is_unambiguous = AsyncMock(return_value=True)
    plan = await classify_managed_repository_legacy_candidate(db, _gitea(), candidate)
    assert plan.classification == "ambiguous"
    assert plan.reason_code == "repository_coordinate_mismatch"


@pytest.mark.asyncio
async def test_terminal_foreign_coordinate_is_not_scrubbed_as_managed_history():
    candidate = _candidate(
        source_status="completed",
        observed_url="http://admin:secret@foreign.invalid/owner/repository.git",
    )
    plan = await classify_managed_repository_legacy_candidate(
        _job_db(candidate), _gitea(), candidate
    )
    assert plan.classification == "ambiguous"
    assert plan.reason_code == "repository_coordinate_mismatch"


@pytest.mark.asyncio
async def test_dry_run_keyset_pages_without_writes():
    first = _candidate(source_status="completed")
    second = _candidate(source_status="failed")
    db = MagicMock()
    db.list_managed_repository_legacy_candidates = AsyncMock(
        side_effect=[
            [
                {
                    **first.__dict__,
                    "source_rank": 2,
                }
            ],
            [{**second.__dict__, "source_rank": 2}],
            [],
        ]
    )
    db.list_managed_repository_legacy_active_authority_candidates = AsyncMock(
        return_value=[]
    )
    db.upsert_managed_repository_legacy_reconciliation = AsyncMock()
    db.get_job = AsyncMock(
        side_effect=lambda job_id: {
            "id": job_id,
            "project_id": None,
            "parent_job_id": None,
            "repo_name": next(
                item.repo_name for item in (first, second) if item.source_id == job_id
            ),
        }
    )
    db.managed_repository_scope_is_unambiguous = AsyncMock(return_value=True)
    db.get_managed_repository_authority = AsyncMock(return_value=None)

    stats, details = await reconcile_managed_repository_legacy_once(
        db, _gitea(), apply=False, page_size=1
    )

    assert stats.scanned == 2
    assert stats.deferred == 2
    assert details["dry_run"] is True
    db.upsert_managed_repository_legacy_reconciliation.assert_not_awaited()
    assert db.list_managed_repository_legacy_candidates.await_args_list[1].kwargs == {
        "after_kind": "job",
        "after_id": first.source_id,
        "limit": 1,
    }


@pytest.mark.asyncio
async def test_active_authority_lifecycle_scan_persists_only_containable_exact_row():
    healthy_id = str(uuid4())
    terminal_id = str(uuid4())
    healthy_record = str(uuid4())
    terminal_record = str(uuid4())
    common = {
        "source_kind": "job",
        "project_id": None,
        "repository_owner": "srw",
        "access_mode": "write",
        "authority_generation": 1,
        "public_key": "ssh-ed25519 omitted-from-intent",
        "public_key_fingerprint": "SHA256:safe",
        "forge_key_id": 91,
        "status": "active",
    }
    db = MagicMock()
    db.list_managed_repository_legacy_active_authority_candidates = AsyncMock(
        side_effect=[
            [
                {
                    **common,
                    "source_id": healthy_id,
                    "authority_record_id": healthy_record,
                    "repo_name": f"job-{healthy_id[:8]}",
                    "containment_candidate": False,
                    "containment_reason": None,
                },
                {
                    **common,
                    "source_id": terminal_id,
                    "authority_record_id": terminal_record,
                    "repo_name": f"job-{terminal_id[:8]}",
                    "containment_candidate": True,
                    "containment_reason": "job_lineage_terminal",
                },
            ],
            [],
        ]
    )
    db.upsert_managed_repository_legacy_reconciliation = AsyncMock()

    (
        scanned,
        classifications,
    ) = await reconciliation._scan_managed_repository_active_authority_lifecycle(
        db, apply=True, page_size=2
    )

    assert scanned == 1
    assert classifications == {"terminal_historical": 1}
    db.upsert_managed_repository_legacy_reconciliation.assert_awaited_once_with(
        source_kind="job",
        source_id=terminal_id,
        project_id=None,
        classification="terminal_historical",
        authority_kind="job",
        authority_id=terminal_id,
        authority_record_id=terminal_record,
        authority_generation=1,
        repository_owner="srw",
        repo_name=f"job-{terminal_id[:8]}",
        access_mode="write",
        reason_code="job_lineage_terminal",
    )
    assert (
        db.list_managed_repository_legacy_active_authority_candidates.await_args_list[
            1
        ].kwargs
        == {"after_kind": "job", "after_id": terminal_record, "limit": 2}
    )


def test_report_is_coordinate_and_credential_free():
    from orchestrator.services.managed_repository_reconciliation import (
        LegacyReconciliationStats,
    )

    source_id = uuid4()
    report = serialize_legacy_reconciliation_report(
        LegacyReconciliationStats(scanned=1, ambiguous=1),
        {
            "dry_run": True,
            "ambiguous": [
                {
                    "source_kind": "job",
                    "source_id": source_id,
                    "reason_code": "repository_coordinate_mismatch",
                }
            ],
        },
    )
    rendered = str(report)
    assert "http" not in rendered
    assert "secret" not in rendered
    assert report["ambiguous"][0]["source_id"] == str(source_id)
    assert isinstance(UUID(report["ambiguous"][0]["source_id"]), UUID)
    assert report["contains_urls_or_credentials"] is False
