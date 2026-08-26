"""Shipped operator boundary for managed-repository legacy reconciliation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from orchestrator.operator_cli.managed_repository_reconciliation import (
    _active_authority_is_expected,
    _job_lineage_authority_inventory,
    _safe_inventory_counts,
    execute,
    main,
    parse_args,
)
from orchestrator.services.managed_repository_reconciliation import (
    LegacyReconciliationStats,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = "operator_cli.managed_repository_reconciliation"


def _rearm_args(*extra: str):
    source_id = str(uuid4())
    actor_id = str(uuid4())
    return parse_args(
        [
            "--rearm-failed",
            "--source-kind",
            "thread",
            "--source-id",
            source_id,
            "--actor-id",
            actor_id,
            "--reason",
            "forge_outage_resolved",
            *extra,
        ]
    )


def test_cli_defaults_to_read_only_inventory():
    args = parse_args([])
    assert args.apply is False
    assert args.rearm_failed is False


@pytest.mark.asyncio
async def test_inventory_excludes_only_exact_permanent_stateless_retirement():
    settled = {
        "workspace_container": {"repo_name": "thread-settled"},
        "_stateless_workspace_retirement_settled": {
            "terminal_token": 8,
            "cleanup_complete": True,
            "permanent": True,
            "snapshot_restore_required": False,
            "backing_id": None,
            "runtime_incarnation": None,
        },
    }
    thread_rows = [
        {
            "id": uuid4(),
            "status": "ended",
            "execution_lane": "stateless",
            "metadata": settled,
            "has_active_authority": False,
        },
        {
            "id": uuid4(),
            "status": "ended",
            "execution_lane": "stateless",
            "metadata": {
                "workspace_container": {"repo_name": "thread-pending"},
                "_stateless_workspace_retirement_pending": True,
                "_stateless_claim_retirement": {"permanent": True},
            },
            "has_active_authority": False,
        },
        {
            "id": uuid4(),
            "status": "ended",
            "execution_lane": "stateless",
            "metadata": {
                **settled,
                "_stateless_workspace_retirement_pending": True,
            },
            "has_active_authority": False,
        },
        {
            "id": uuid4(),
            "status": "ended",
            "execution_lane": "pinned",
            "metadata": {"workspace_container": {"repo_name": "thread-idle"}},
            "has_active_authority": False,
        },
    ]
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[True, 4, 0, 0, 0, 0, 0])
    conn.fetch = AsyncMock(side_effect=[[], thread_rows, [], [], [], []])
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=conn)
    acquire.__aexit__ = AsyncMock(return_value=False)
    db = MagicMock()
    db.acquire.return_value = acquire

    report = await _safe_inventory_counts(db)

    assert report["managed_scopes_without_active_authority"] == 3
    assert report["credentialed_legacy_rows"] == 4


def test_job_authority_classifier_consumes_full_lineage_whitelist():
    parser = MagicMock(return_value=None)
    base = {
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "authority_kind": "job",
        "authority_id": "11111111-1111-4111-8111-111111111111",
        "project_id": "22222222-2222-4222-8222-222222222222",
        "repo_name": "job-11111111",
        "access_mode": "write",
        "job_exists": True,
        "job_id": "11111111-1111-4111-8111-111111111111",
        "job_project_id": "22222222-2222-4222-8222-222222222222",
        "job_parent_id": None,
        "job_repo_name": "job-11111111",
        "job_status": "failed",
        "job_completion_outcome_kind": None,
    }
    assert _active_authority_is_expected(
        base,
        settled_retirement_parser=parser,
        expected_job_authority_record_ids=frozenset({base["id"]}),
    )
    assert not _active_authority_is_expected(
        base,
        settled_retirement_parser=parser,
    )


def _job_row(
    job_id: str,
    *,
    parent_id: str | None = None,
    project_id: str = "22222222-2222-4222-8222-222222222222",
    repo_name: str | None = "job-root0000",
    status: str = "completed",
    outcome: str | None = None,
):
    return {
        "id": job_id,
        "parent_job_id": parent_id,
        "project_id": project_id,
        "repo_name": repo_name,
        "branch_name": "main",
        "status": status,
        "completion_outcome_kind": outcome,
    }


def _job_authority(
    root_id: str,
    *,
    record_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    project_id: str = "22222222-2222-4222-8222-222222222222",
    repo_name: str = "job-root0000",
):
    return {
        "id": record_id,
        "authority_kind": "job",
        "authority_id": root_id,
        "project_id": project_id,
        "repo_name": repo_name,
        "access_mode": "write",
    }


def test_lineage_keeps_root_key_while_completed_root_has_resumable_child():
    root_id = "11111111-1111-4111-8111-111111111111"
    child_id = "33333333-3333-4333-8333-333333333333"
    authority = _job_authority(root_id)
    report = _job_lineage_authority_inventory(
        [
            _job_row(root_id, status="completed"),
            _job_row(child_id, parent_id=root_id, status="failed"),
        ],
        [authority],
    )
    assert report["missing_authorities"] == 0
    assert report["anomaly_ids"] == []
    assert report["expected_job_authority_record_ids"] == frozenset({authority["id"]})


def test_lineage_requires_one_authority_until_every_member_is_absorbing():
    root_id = "11111111-1111-4111-8111-111111111111"
    child_id = "33333333-3333-4333-8333-333333333333"
    rows = [
        _job_row(root_id, status="completed"),
        _job_row(child_id, parent_id=root_id, status="paused"),
    ]
    missing = _job_lineage_authority_inventory(rows, [])
    assert missing["missing_authorities"] == 1

    authority = _job_authority(root_id)
    absorbing = _job_lineage_authority_inventory(
        [
            _job_row(root_id, status="completed"),
            _job_row(
                child_id,
                parent_id=root_id,
                status="cancelled",
                outcome="blocked_undelivered",
            ),
        ],
        [authority],
    )
    assert absorbing["missing_authorities"] == 0
    assert absorbing["expected_job_authority_record_ids"] == frozenset()


def test_invalid_blocked_outcome_is_anomaly_not_absorbing_cleanup_proof():
    root_id = "11111111-1111-4111-8111-111111111111"
    authority = _job_authority(root_id)
    report = _job_lineage_authority_inventory(
        [
            _job_row(
                root_id,
                status="completed",
                outcome="blocked_undelivered",
            )
        ],
        [authority],
    )
    assert report["anomaly_ids"] == [root_id]
    assert report["expected_job_authority_record_ids"] == frozenset({authority["id"]})


@pytest.mark.parametrize("drift", ["cycle", "project", "repository", "missing"])
def test_corrupt_resumable_lineage_is_bounded_and_retains_possible_key(drift):
    root_id = "11111111-1111-4111-8111-111111111111"
    child_id = "33333333-3333-4333-8333-333333333333"
    missing_id = "44444444-4444-4444-8444-444444444444"
    root = _job_row(root_id, status="created")
    child = _job_row(child_id, parent_id=root_id, status="failed")
    authority_id = root_id
    rows = [root, child]
    if drift == "cycle":
        root["parent_job_id"] = child_id
    elif drift == "project":
        child["project_id"] = "55555555-5555-4555-8555-555555555555"
    elif drift == "repository":
        child["repo_name"] = "job-different"
    else:
        child["parent_job_id"] = missing_id
        authority_id = missing_id
        rows = [child]
    authority = _job_authority(authority_id)

    report = _job_lineage_authority_inventory(rows, [authority])

    assert report["missing_authorities"] == 0
    assert report["anomaly_ids"]
    assert report["expected_job_authority_record_ids"] == frozenset({authority["id"]})


def test_shared_missing_parent_keeps_key_if_any_orphan_child_can_resume():
    missing_id = "44444444-4444-4444-8444-444444444444"
    first = _job_row(
        "11111111-1111-4111-8111-111111111111",
        parent_id=missing_id,
        status="failed",
    )
    second = _job_row(
        "33333333-3333-4333-8333-333333333333",
        parent_id=missing_id,
        status="completed",
    )
    authority = _job_authority(missing_id)

    report = _job_lineage_authority_inventory([first, second], [authority])

    assert report["expected_job_authority_record_ids"] == frozenset({authority["id"]})
    assert set(report["anomaly_ids"]) == {first["id"], second["id"]}


def test_unexpected_authority_classifier_preserves_intended_shared_repository():
    row = {
        "authority_kind": "project_repository",
        "authority_id": "11111111-1111-4111-8111-111111111111",
        "project_id": "22222222-2222-4222-8222-222222222222",
        "repo_name": "shared-jobs",
        "access_mode": "write",
        "repository_exists": True,
        "repository_id": "11111111-1111-4111-8111-111111111111",
        "repository_project_id": "22222222-2222-4222-8222-222222222222",
        "repository_name": "shared-jobs",
        "repository_role": "jobs",
        "repository_read_only": False,
        "repository_is_managed": True,
    }
    parser = MagicMock(return_value=None)
    assert _active_authority_is_expected(row, settled_retirement_parser=parser)
    assert not _active_authority_is_expected(
        {**row, "repository_exists": False}, settled_retirement_parser=parser
    )
    assert not _active_authority_is_expected(
        {**row, "repository_role": "knowledge"},
        settled_retirement_parser=parser,
    )


def test_unexpected_authority_classifier_retires_only_canonical_thread_proof():
    row = {
        "authority_kind": "thread",
        "authority_id": "11111111-1111-4111-8111-111111111111",
        "project_id": "22222222-2222-4222-8222-222222222222",
        "repo_name": "thread-11111111",
        "access_mode": "write",
        "thread_exists": True,
        "thread_id": "11111111-1111-4111-8111-111111111111",
        "thread_project_id": "22222222-2222-4222-8222-222222222222",
        "thread_repo_name": "thread-11111111",
        "thread_execution_lane": "stateless",
        "thread_status": "ended",
        "thread_metadata": {},
    }
    assert not _active_authority_is_expected(
        row,
        settled_retirement_parser=MagicMock(return_value={"permanent": True}),
    )
    assert _active_authority_is_expected(
        row,
        settled_retirement_parser=MagicMock(return_value={"permanent": False}),
    )
    parser = MagicMock(side_effect=RuntimeError("malformed"))
    assert _active_authority_is_expected(row, settled_retirement_parser=parser)


@pytest.mark.parametrize(
    "argv",
    [
        ["--apply", "--rearm-failed"],
        ["--rearm-failed"],
        ["--rearm-failed", "--source-kind", "job"],
        ["--source-kind", "job"],
        [
            "--rearm-failed",
            "--source-kind",
            "job",
            "--source-id",
            "not-a-uuid",
            "--actor-id",
            "11111111-1111-4111-8111-111111111111",
            "--reason",
            "retry",
        ],
        [
            "--rearm-failed",
            "--source-kind",
            "job",
            "--source-id",
            "11111111-1111-4111-8111-111111111111",
            "--actor-id",
            "22222222-2222-4222-8222-222222222222",
            "--reason",
            "   ",
        ],
        [
            "--rearm-failed",
            "--source-kind",
            "job",
            "--source-id",
            "11111111-1111-4111-8111-111111111111",
            "--actor-id",
            "22222222-2222-4222-8222-222222222222",
            "--reason",
            "https://credential-bearing-coordinate.invalid/retry",
        ],
    ],
)
def test_cli_rejects_incomplete_or_ambiguous_mutation_arguments(argv):
    with pytest.raises(SystemExit) as exc:
        parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_exact_rearm_calls_database_and_redacts_reason(capsys):
    args = _rearm_args()
    result = {
        "status": "rearmed",
        "source_kind": args.source_kind,
        "source_id": args.source_id,
        "rearm_generation": 4,
        "state": "pending",
        "reason": args.reason,
        "repo_name": "must-not-escape",
    }
    db = MagicMock()
    db.connect = AsyncMock()
    db.disconnect = AsyncMock()
    db.rearm_managed_repository_legacy_reconciliation = AsyncMock(return_value=result)
    gitea = MagicMock()
    gitea.close = AsyncMock()

    status = await execute(
        args,
        db_factory=lambda: db,
        gitea_factory=lambda: gitea,
        reconcile=AsyncMock(),
        serializer=MagicMock(),
    )

    assert status == 0
    db.rearm_managed_repository_legacy_reconciliation.assert_awaited_once_with(
        args.source_kind,
        args.source_id,
        actor_id=args.actor_id,
        reason=args.reason,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "contains_urls_or_credentials": False,
        "mode": "rearm-failed",
        "rearm_generation": 4,
        "source_id": args.source_id,
        "source_kind": "thread",
        "state": "pending",
        "status": "rearmed",
    }
    rendered = json.dumps(payload)
    assert args.reason not in rendered
    assert "must-not-escape" not in rendered
    gitea.close.assert_awaited_once()
    db.disconnect.assert_awaited_once()


@pytest.mark.parametrize(
    ("db_status", "exit_status"),
    [
        ("replayed", 0),
        ("not_found", 2),
        ("not_failed", 2),
        ("idempotency_conflict", 2),
    ],
)
@pytest.mark.asyncio
async def test_rearm_exit_status_is_fail_closed(db_status, exit_status, capsys):
    args = _rearm_args()
    db = MagicMock()
    db.connect = AsyncMock()
    db.disconnect = AsyncMock()
    db.rearm_managed_repository_legacy_reconciliation = AsyncMock(
        return_value={"status": db_status}
    )
    gitea = MagicMock()
    gitea.close = AsyncMock()

    status = await execute(
        args,
        db_factory=lambda: db,
        gitea_factory=lambda: gitea,
        reconcile=AsyncMock(),
        serializer=MagicMock(),
    )

    assert status == exit_status
    assert json.loads(capsys.readouterr().out)["status"] == db_status


@pytest.mark.asyncio
async def test_apply_exits_unresolved_for_durable_retry_even_when_inventory_is_clean(
    monkeypatch, capsys
):
    args = parse_args(["--apply"])
    db = MagicMock()
    db.connect = AsyncMock()
    db.disconnect = AsyncMock()
    gitea = MagicMock()
    gitea.ensure_initialized = AsyncMock(return_value=True)
    gitea.close = AsyncMock()
    inventory = {
        "reconciliation_table_present": True,
        "credentialed_legacy_rows": 0,
        "incomplete_creation_intents": 0,
        "managed_scopes_without_active_authority": 0,
        "job_lineage_anomalies": 0,
        "job_lineage_anomaly_ids": [],
        "historical_shared_jobs_candidates": 0,
        "unexpected_active_authorities": 0,
        "unexpected_active_authority_ids": [],
        "incomplete_managed_authorities": 0,
        "incomplete_managed_authority_ids": [],
        "failed_rearm_required": 0,
        "failed_rearm_scopes": [],
        "authority_access_modes": [],
    }
    monkeypatch.setattr(
        "orchestrator.operator_cli.managed_repository_reconciliation._safe_inventory_counts",
        AsyncMock(return_value=inventory),
    )
    details = {
        "dry_run": False,
        "progress": {
            "counts": [
                {
                    "state": "retry",
                    "classification": "terminal_historical",
                    "result_kind": None,
                    "count": 1,
                }
            ],
            "oldest_pending_or_retry_at": None,
            "ambiguous": [],
            "failure_reasons": [],
            "rearm_required": [],
        },
    }

    def serializer(stats, raw):
        return {
            "mode": "apply",
            "deferred": stats.deferred,
            "failed": stats.failed,
            "ambiguous": stats.ambiguous,
            **raw,
            "contains_urls_or_credentials": False,
        }

    status = await execute(
        args,
        db_factory=lambda: db,
        gitea_factory=lambda: gitea,
        reconcile=AsyncMock(
            return_value=(LegacyReconciliationStats(scanned=0), details)
        ),
        serializer=serializer,
    )

    assert status == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending_or_claimed_reconciliations"] == 1


def test_operator_module_runs_from_flattened_image_layout(tmp_path):
    shutil.copytree(ROOT / "orchestrator" / "operator_cli", tmp_path / "operator_cli")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", MODULE, "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--apply" in result.stdout
    assert "--rearm-failed" in result.stdout


def test_production_image_build_checks_deployed_module_help():
    dockerfile = (ROOT / "docker" / "Dockerfile.orchestrator").read_text()
    assert f"RUN python -m {MODULE} --help >/dev/null" in dockerfile


def test_compatibility_script_is_a_thin_wrapper():
    wrapper = (
        ROOT / "scripts" / "inventory-managed-repository-authority.py"
    ).read_text()
    assert "operator_cli.managed_repository_reconciliation" in wrapper
    assert "SELECT " not in wrapper
    assert "GiteaClient" not in wrapper
    assert "PostgresDB" not in wrapper


def test_top_level_failure_is_redacted(monkeypatch, capsys):
    secret_shaped_error = (
        "postgresql://operator:password@internal-db:5432/srw "
        "https://admin:password@gitea.internal/private-repo"
    )

    async def fail(_args):
        raise RuntimeError(secret_shaped_error)

    monkeypatch.setattr(
        "orchestrator.operator_cli.managed_repository_reconciliation.execute",
        fail,
    )

    assert main(["--apply"]) == 3
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload == {
        "contains_urls_or_credentials": False,
        "error": "managed_repository_reconciliation_failed",
        "mode": "apply",
    }
    assert secret_shaped_error not in captured.out
