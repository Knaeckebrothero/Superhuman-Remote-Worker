"""Inventory or explicitly reconcile legacy managed repository authority.

This module deliberately lives below ``orchestrator/`` because both production
orchestrator images copy that directory's contents directly into ``/app``.  The
supported deployed invocation is therefore::

    python -m operator_cli.managed_repository_reconciliation

Dry-run is the default and performs no database or forge mutation.  ``--apply``
persists secret-free intents and drains only currently due leased work.  An
exhausted exact intent can only be re-armed through ``--rearm-failed`` with its
full source UUID, a full actor UUID, and a non-blank audit reason.

Output contains aggregate counts and opaque UUIDs only.  It never contains
repository URLs/names, credentials, key material, ciphertext, transport
coordinates, or the operator's audit reason.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import UUID

_SOURCE_KINDS = ("job", "thread", "project_repository")
_REARM_SUCCESS = frozenset({"rearmed", "replayed"})
_REASON_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}\Z")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _canonical_uuid(value: str) -> str:
    text = str(value).strip()
    try:
        parsed = UUID(text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a full UUID") from exc
    if str(parsed) != text.lower():
        raise argparse.ArgumentTypeError("must be a canonical full UUID")
    return str(parsed)


def _audit_reason(value: str) -> str:
    reason = str(value).strip()
    if not _REASON_CODE.fullmatch(reason):
        raise argparse.ArgumentTypeError(
            "must be a 1-100 character lowercase machine reason code"
        )
    return reason


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only by default: inventory legacy managed repository scopes. "
            "Pass --apply explicitly to persist leased reconciliation intents "
            "and process currently due work."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="perform bounded durable reconciliation (default: dry-run only)",
    )
    mode.add_argument(
        "--rearm-failed",
        action="store_true",
        help="re-arm one exact exhausted intent; requires all re-arm fields",
    )
    parser.add_argument("--page-size", type=_positive_int, default=100)
    parser.add_argument("--concurrency", type=_positive_int, default=4)
    parser.add_argument("--lease-seconds", type=_positive_int, default=300)
    parser.add_argument("--max-attempts", type=_positive_int, default=8)
    parser.add_argument("--source-kind", choices=_SOURCE_KINDS)
    parser.add_argument("--source-id", type=_canonical_uuid)
    parser.add_argument("--actor-id", type=_canonical_uuid)
    parser.add_argument(
        "--reason",
        type=_audit_reason,
        help=(
            "secret-free lowercase machine reason code (letters, digits, dot, "
            "underscore, or hyphen; maximum 100 characters)"
        ),
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    rearm_values = (args.source_kind, args.source_id, args.actor_id, args.reason)
    if args.rearm_failed and any(value is None for value in rearm_values):
        parser.error(
            "--rearm-failed requires --source-kind, --source-id, --actor-id, "
            "and --reason"
        )
    if not args.rearm_failed and any(value is not None for value in rearm_values):
        parser.error(
            "--source-kind, --source-id, --actor-id, and --reason may only be "
            "used with --rearm-failed"
        )
    return args


def _active_authority_is_expected(
    row: Mapping[str, Any],
    *,
    settled_retirement_parser: Callable[[Any], Mapping[str, Any] | None],
    expected_job_authority_record_ids: frozenset[str] = frozenset(),
) -> bool:
    """Match one active authority to the exact durable scope that needs it."""

    kind = str(row.get("authority_kind") or "")
    authority_id = str(row.get("authority_id") or "")
    project_id = str(row.get("project_id") or "")
    repo_name = str(row.get("repo_name") or "")
    access_mode = str(row.get("access_mode") or "")
    if kind == "job":
        return str(row.get("id") or "") in expected_job_authority_record_ids
    if kind == "thread":
        if not (
            row.get("thread_exists")
            and str(row.get("thread_id") or "") == authority_id
            and str(row.get("thread_project_id") or "") == project_id
            and str(row.get("thread_repo_name") or "") == repo_name
            and access_mode == "write"
        ):
            return False
        if (
            row.get("thread_execution_lane") == "stateless"
            and row.get("thread_status") == "ended"
        ):
            try:
                settled = settled_retirement_parser(row.get("thread_metadata"))
            except RuntimeError:
                # Malformed lifecycle evidence cannot authorize key removal.
                return True
            if settled is not None and settled.get("permanent") is True:
                return False
        return True
    if kind == "project_repository":
        if not (
            row.get("repository_exists")
            and str(row.get("repository_id") or "") == authority_id
            and str(row.get("repository_project_id") or "") == project_id
            and str(row.get("repository_name") or "") == repo_name
            and row.get("repository_is_managed") is True
        ):
            return False
        role = str(row.get("repository_role") or "")
        if role == "knowledge":
            return False
        expected_mode = (
            "read"
            if role == "reference" or row.get("repository_read_only") is True
            else "write"
        )
        return access_mode == expected_mode
    return False


def _job_is_absorbing(row: Mapping[str, Any]) -> bool:
    status = str(row.get("status") or "")
    if (
        row.get("completion_outcome_kind") == "blocked_undelivered"
        and status != "cancelled"
    ):
        return False
    return status == "completed" or (
        status == "cancelled"
        and row.get("completion_outcome_kind") == "blocked_undelivered"
    )


def _job_lineage_authority_inventory(
    job_rows: Sequence[Mapping[str, Any]],
    active_authorities: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve full job trees and their active isolated/shared authority.

    The public resume contract applies to every job row, not just the root.  A
    completed root can therefore still own the repository key required by a
    failed/paused child.  Corrupt trees remain unresolved and retain any key
    that might still serve a resumable member; they never become cleanup proof.
    """

    jobs = {str(row["id"]): row for row in job_rows}
    adjacency: dict[str, set[str]] = {job_id: set() for job_id in jobs}
    dangling_parent: dict[str, str] = {}
    for job_id, row in jobs.items():
        parent_id = str(row.get("parent_job_id") or "")
        if not parent_id:
            continue
        if parent_id in jobs:
            adjacency[job_id].add(parent_id)
            adjacency[parent_id].add(job_id)
        else:
            dangling_parent[job_id] = parent_id

    components: list[dict[str, Any]] = []
    components_by_authority_id: dict[str, list[dict[str, Any]]] = {}
    unseen = set(jobs)
    while unseen:
        start = min(unseen)
        stack = [start]
        members: set[str] = set()
        while stack:
            current = stack.pop()
            if current in members:
                continue
            members.add(current)
            unseen.discard(current)
            stack.extend(adjacency[current] - members)
        roots = [
            member
            for member in members
            if not str(jobs[member].get("parent_job_id") or "")
        ]
        missing_parents = {
            dangling_parent[member] for member in members if member in dangling_parent
        }
        root_id = roots[0] if len(roots) == 1 and not missing_parents else None
        root = jobs[root_id] if root_id else None
        projects = {str(jobs[member].get("project_id") or "") for member in members}
        repo_names = {
            str(jobs[member].get("repo_name") or "")
            for member in members
            if str(jobs[member].get("repo_name") or "")
        }
        root_repo = str(root.get("repo_name") or "") if root else ""
        malformed = bool(
            root_id is None
            or len(projects) != 1
            or len(repo_names) > 1
            or (repo_names and root_repo not in repo_names)
            or any(
                jobs[member].get("completion_outcome_kind") == "blocked_undelivered"
                and jobs[member].get("status") != "cancelled"
                for member in members
            )
        )
        component = {
            "members": members,
            "missing_parents": missing_parents,
            "root_id": root_id,
            "root": root,
            "malformed": malformed,
            "resumable": any(not _job_is_absorbing(jobs[item]) for item in members),
        }
        components.append(component)
        for member in members | missing_parents:
            components_by_authority_id.setdefault(member, []).append(component)

    active_job_authorities = [
        row for row in active_authorities if row.get("authority_kind") == "job"
    ]
    active_shared_authorities = [
        row
        for row in active_authorities
        if row.get("authority_kind") == "project_repository"
        and row.get("repository_exists")
        and row.get("repository_is_managed") is True
        and row.get("repository_role") == "jobs"
        and row.get("repository_read_only") is not True
        and row.get("access_mode") == "write"
    ]
    anomalies: set[str] = set()
    expected_record_ids: set[str] = set()
    for component in components:
        if component["malformed"]:
            anomalies.update(component["members"])

    for authority in active_job_authorities:
        authority_id = str(authority.get("authority_id") or "")
        associated = components_by_authority_id.get(authority_id, [])
        resumable_components = [item for item in associated if item["resumable"]]
        if not resumable_components:
            continue
        component = resumable_components[0]
        root = component["root"]
        exact_root_authority = bool(
            len(associated) == 1
            and not component["malformed"]
            and component["root_id"] == authority_id
            and root is not None
            and str(authority.get("project_id") or "")
            == str(root.get("project_id") or "")
            and str(authority.get("repo_name") or "")
            == str(root.get("repo_name") or "")
            and authority.get("access_mode") == "write"
        )
        if not exact_root_authority:
            for item in associated:
                anomalies.update(item["members"])
        # A malformed binding may still be serving resumable work. Keep it
        # fenced as expected until the separately surfaced anomaly is resolved.
        expected_record_ids.add(str(authority["id"]))

    missing_authorities = 0
    for component in components:
        if component["malformed"] or not component["resumable"]:
            continue
        root = component["root"]
        assert root is not None
        root_repo = str(root.get("repo_name") or "")
        if not root_repo:
            continue
        root_id = str(component["root_id"])
        project_id = str(root.get("project_id") or "")
        exact_job_authority = any(
            str(authority.get("authority_id") or "") == root_id
            and str(authority.get("project_id") or "") == project_id
            and str(authority.get("repo_name") or "") == root_repo
            and authority.get("access_mode") == "write"
            for authority in active_job_authorities
        )
        exact_shared_authority = any(
            str(authority.get("repository_project_id") or "") == project_id
            and str(authority.get("repository_name") or "") == root_repo
            and str(authority.get("project_id") or "") == project_id
            and str(authority.get("repo_name") or "") == root_repo
            for authority in active_shared_authorities
        )
        if not (exact_job_authority or exact_shared_authority):
            missing_authorities += 1

    return {
        "missing_authorities": missing_authorities,
        "anomaly_ids": sorted(anomalies),
        "expected_job_authority_record_ids": frozenset(expected_record_ids),
    }


async def _safe_inventory_counts(db: Any) -> dict[str, object]:
    """Return only coordinate-free rollout gates from authoritative storage."""

    from src.shared.session_retirement import (
        stateless_settled_retirement_authority,
    )

    async with db.acquire() as conn:
        table_present = bool(
            await conn.fetchval(
                "SELECT to_regclass("
                "'public.managed_repository_legacy_reconciliations') IS NOT NULL"
            )
        )
        credentialed_legacy_rows = int(
            await conn.fetchval(
                """
                SELECT (
                    SELECT count(*) FROM project_repositories
                     WHERE public.managed_repository_url_has_userinfo(repo_url)
                ) + (
                    SELECT count(*) FROM jobs
                     WHERE public.managed_repository_url_has_userinfo(
                         context->>'git_remote_url'
                     )
                ) + (
                    SELECT count(*) FROM threads
                     WHERE public.managed_repository_url_has_userinfo(
                         metadata->'workspace_container'->>'git_remote_url'
                     )
                )
                """
            )
            or 0
        )
        incomplete_creation_intents = int(
            await conn.fetchval(
                "SELECT count(*) FROM managed_repository_creation_intents "
                "WHERE status IN ('pending', 'deleting') "
                "OR failure_class IS NOT NULL"
            )
            or 0
        )
        modes = await conn.fetch(
            "SELECT access_mode, status, count(*) AS count "
            "FROM managed_repository_authorities "
            "GROUP BY access_mode, status ORDER BY access_mode, status"
        )
        unproven_project_repositories = int(
            await conn.fetchval(
                """
                WITH scopes AS (
                    SELECT 'project_repository'::text AS kind,
                           repository.id AS scope_id, repository.project_id,
                           repository.name AS repo_name,
                           CASE WHEN repository.role='reference'
                                      OR repository.read_only
                                THEN 'read' ELSE 'write' END AS access_mode
                      FROM project_repositories AS repository
                     WHERE repository.is_managed
                       AND repository.role <> 'knowledge'
                       AND EXISTS (
                           SELECT 1 FROM jobs
                            WHERE jobs.project_id=repository.project_id
                              AND jobs.status <> 'completed'
                              AND NOT (
                                  jobs.status='cancelled'
                                  AND jobs.completion_outcome_kind=
                                      'blocked_undelivered'
                              )
                       )
                )
                SELECT count(*)
                  FROM scopes
                 WHERE NOT EXISTS (
                     SELECT 1
                       FROM managed_repository_authorities AS authority
                      WHERE authority.status='active'
                        AND authority.repo_name=scopes.repo_name
                        AND authority.access_mode=scopes.access_mode
                        AND (
                            (
                                authority.authority_kind=scopes.kind
                                AND authority.authority_id=scopes.scope_id
                            )
                        )
                 )
                """
            )
            or 0
        )
        thread_rows = await conn.fetch(
            """
            SELECT thread.id, thread.status::text AS status,
                   thread.execution_lane, thread.metadata,
                   EXISTS (
                       SELECT 1
                         FROM managed_repository_authorities AS authority
                        WHERE authority.status='active'
                          AND authority.authority_kind='thread'
                          AND authority.authority_id=thread.id
                          AND authority.project_id IS NOT DISTINCT FROM
                              thread.project_id
                          AND authority.repo_name=
                              thread.metadata->'workspace_container'->>'repo_name'
                          AND authority.access_mode='write'
                   ) AS has_active_authority
              FROM threads AS thread
             WHERE thread.metadata->'workspace_container'->>'repo_name'
                   IS NOT NULL
            """
        )
        unproven_threads = 0
        for thread in thread_rows:
            permanently_retired = False
            if thread["execution_lane"] == "stateless" and thread["status"] == "ended":
                try:
                    settled = stateless_settled_retirement_authority(thread["metadata"])
                except RuntimeError:
                    settled = None
                permanently_retired = bool(
                    settled is not None and settled.get("permanent") is True
                )
            if not permanently_retired and not thread["has_active_authority"]:
                unproven_threads += 1
        unproven_runnable = unproven_project_repositories + unproven_threads
        historical_shared_jobs = int(
            await conn.fetchval(
                """
                WITH RECURSIVE lineage AS (
                    SELECT root.id AS root_id, root.id AS member_id
                      FROM jobs AS root
                     WHERE root.parent_job_id IS NULL
                    UNION ALL
                    SELECT lineage.root_id, child.id
                      FROM lineage
                      JOIN jobs AS child
                        ON child.parent_job_id=lineage.member_id
                )
                SELECT count(DISTINCT root.id)
                  FROM lineage
                  JOIN jobs AS root ON root.id=lineage.root_id
                  JOIN jobs AS member ON member.id=lineage.member_id
                 WHERE root.project_id IS NOT NULL
                   AND root.repo_name IS NULL
                   AND root.branch_name IS NOT NULL
                   AND member.project_id=root.project_id
                   AND member.status <> 'completed'
                   AND NOT (
                       member.status='cancelled'
                       AND member.completion_outcome_kind='blocked_undelivered'
                   )
                   AND EXISTS (
                       SELECT 1 FROM project_repositories AS repository
                        WHERE repository.project_id=root.project_id
                          AND repository.role='jobs'
                          AND repository.is_managed
                   )
                """
            )
            or 0
        )
        active_authorities = await conn.fetch(
            """
            SELECT authority.id, authority.authority_kind,
                   authority.authority_id, authority.project_id,
                   authority.repo_name, authority.access_mode,
                   job.id IS NOT NULL AS job_exists,
                   job.id AS job_id, job.project_id AS job_project_id,
                   job.parent_job_id AS job_parent_id,
                   job.repo_name AS job_repo_name,
                   job.status::text AS job_status,
                   job.completion_outcome_kind AS job_completion_outcome_kind,
                   thread.id IS NOT NULL AS thread_exists,
                   thread.id AS thread_id,
                   thread.project_id AS thread_project_id,
                   thread.status::text AS thread_status,
                   thread.execution_lane AS thread_execution_lane,
                   thread.metadata AS thread_metadata,
                   thread.metadata->'workspace_container'->>'repo_name'
                       AS thread_repo_name,
                   repository.id IS NOT NULL AS repository_exists,
                   repository.id AS repository_id,
                   repository.project_id AS repository_project_id,
                   repository.name AS repository_name,
                   repository.role AS repository_role,
                   repository.read_only AS repository_read_only,
                   repository.is_managed AS repository_is_managed
              FROM managed_repository_authorities AS authority
              LEFT JOIN jobs AS job
                ON authority.authority_kind='job'
               AND job.id=authority.authority_id
              LEFT JOIN threads AS thread
                ON authority.authority_kind='thread'
               AND thread.id=authority.authority_id
              LEFT JOIN project_repositories AS repository
                ON authority.authority_kind='project_repository'
               AND repository.id=authority.authority_id
             WHERE authority.status='active'
             ORDER BY authority.id
            """
        )
        job_rows = await conn.fetch(
            """
            SELECT id, parent_job_id, project_id, repo_name, branch_name,
                   status::text AS status, completion_outcome_kind
              FROM jobs
             ORDER BY id
            """
        )
        job_lineages = _job_lineage_authority_inventory(job_rows, active_authorities)
        unproven_runnable += int(job_lineages["missing_authorities"])
        unexpected_active_ids = [
            str(row["id"])
            for row in active_authorities
            if not _active_authority_is_expected(
                row,
                settled_retirement_parser=stateless_settled_retirement_authority,
                expected_job_authority_record_ids=job_lineages[
                    "expected_job_authority_record_ids"
                ],
            )
        ]
        incomplete_authorities = await conn.fetch(
            """
            SELECT id
              FROM managed_repository_authorities
             WHERE status NOT IN ('active', 'revoked')
             ORDER BY id
             LIMIT 100
            """
        )
        incomplete_authority_count = int(
            await conn.fetchval(
                "SELECT count(*) FROM managed_repository_authorities "
                "WHERE status NOT IN ('active', 'revoked')"
            )
            or 0
        )
        failed_rearms: list[dict[str, str]] = []
        if table_present:
            rows = await conn.fetch(
                """
                SELECT source_kind, source_id
                  FROM managed_repository_legacy_reconciliations
                 WHERE state='failed'
                 ORDER BY source_kind, source_id
                 LIMIT 100
                """
            )
            failed_rearms = [
                {
                    "source_kind": str(row["source_kind"]),
                    "source_id": str(row["source_id"]),
                }
                for row in rows
            ]
            failed_rearm_count = int(
                await conn.fetchval(
                    "SELECT count(*) "
                    "FROM managed_repository_legacy_reconciliations "
                    "WHERE state='failed'"
                )
                or 0
            )
        else:
            failed_rearm_count = 0
    return {
        "reconciliation_table_present": table_present,
        "credentialed_legacy_rows": credentialed_legacy_rows,
        "incomplete_creation_intents": incomplete_creation_intents,
        "managed_scopes_without_active_authority": unproven_runnable,
        "job_lineage_anomalies": len(job_lineages["anomaly_ids"]),
        "job_lineage_anomaly_ids": job_lineages["anomaly_ids"][:100],
        "historical_shared_jobs_candidates": historical_shared_jobs,
        "unexpected_active_authorities": len(unexpected_active_ids),
        "unexpected_active_authority_ids": unexpected_active_ids[:100],
        "incomplete_managed_authorities": incomplete_authority_count,
        "incomplete_managed_authority_ids": [
            str(row["id"]) for row in incomplete_authorities
        ],
        "failed_rearm_required": failed_rearm_count,
        "failed_rearm_scopes": failed_rearms,
        "authority_access_modes": [dict(row) for row in modes],
    }


def _runtime_dependencies() -> tuple[type[Any], type[Any], Callable[..., Any], Any]:
    """Import dependencies using the flattened production-image namespace."""

    from database.postgres import PostgresDB
    from services.gitea import GiteaClient
    from services.managed_repository_reconciliation import (
        reconcile_managed_repository_legacy_once,
        serialize_legacy_reconciliation_report,
    )

    return (
        PostgresDB,
        GiteaClient,
        reconcile_managed_repository_legacy_once,
        serialize_legacy_reconciliation_report,
    )


def _safe_rearm_report(result: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist the exact coordinate-free re-arm response fields."""

    report: dict[str, Any] = {
        "mode": "rearm-failed",
        "status": str(result.get("status") or "not_found"),
        "contains_urls_or_credentials": False,
    }
    for field in ("source_kind", "source_id", "rearm_generation", "state"):
        value = result.get(field)
        if value is not None:
            report[field] = int(value) if field == "rearm_generation" else str(value)
    return report


async def execute(
    args: argparse.Namespace,
    *,
    db_factory: Callable[[], Any] | None = None,
    gitea_factory: Callable[[], Any] | None = None,
    reconcile: Callable[..., Any] | None = None,
    serializer: Callable[..., Any] | None = None,
) -> int:
    if any(
        dependency is None
        for dependency in (db_factory, gitea_factory, reconcile, serializer)
    ):
        defaults = _runtime_dependencies()
        db_factory = db_factory or defaults[0]
        gitea_factory = gitea_factory or defaults[1]
        reconcile = reconcile or defaults[2]
        serializer = serializer or defaults[3]

    assert db_factory is not None
    assert gitea_factory is not None
    assert reconcile is not None
    assert serializer is not None
    db = db_factory()
    gitea = gitea_factory()
    await db.connect()
    try:
        if args.rearm_failed:
            result = await db.rearm_managed_repository_legacy_reconciliation(
                args.source_kind,
                args.source_id,
                actor_id=args.actor_id,
                reason=args.reason,
            )
            safe_report = _safe_rearm_report(result)
            print(json.dumps(safe_report, indent=2, sort_keys=True))
            return 0 if safe_report["status"] in _REARM_SUCCESS else 2

        if args.apply and not await gitea.ensure_initialized():
            print(
                json.dumps(
                    {
                        "mode": "apply",
                        "error": "managed_repository_forge_unavailable",
                        "contains_urls_or_credentials": False,
                    },
                    sort_keys=True,
                )
            )
            return 3
        stats, details = await reconcile(
            db,
            gitea,
            apply=bool(args.apply),
            page_size=args.page_size,
            concurrency=args.concurrency,
            lease_seconds=args.lease_seconds,
            max_attempts=args.max_attempts,
        )
        report = serializer(stats, details)
        report.update(await _safe_inventory_counts(db))
        report["ambiguous_repository_scopes"] = stats.ambiguous
        progress_counts = report.get("progress", {}).get("counts", [])
        pending_or_claimed = sum(
            int(row.get("count") or 0)
            for row in progress_counts
            if row.get("state") in {"pending", "retry", "claimed"}
        )
        report["pending_or_claimed_reconciliations"] = pending_or_claimed
        print(json.dumps(report, indent=2, sort_keys=True))
        unresolved = bool(
            stats.deferred
            or stats.failed
            or stats.ambiguous
            or not report["reconciliation_table_present"]
            or report["managed_scopes_without_active_authority"]
            or report["job_lineage_anomalies"]
            or report["credentialed_legacy_rows"]
            or report["incomplete_creation_intents"]
            or report["unexpected_active_authorities"]
            or report["incomplete_managed_authorities"]
            or report["failed_rearm_required"]
            or pending_or_claimed
        )
        return 2 if unresolved else 0
    finally:
        await gitea.close()
        await db.disconnect()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(execute(args))
    except KeyboardInterrupt:
        raise
    except Exception:
        # This is an operator/security boundary, so an unhandled client or
        # transport exception must not render a DSN, forge coordinate, URL,
        # or credential through Python's default traceback. Detailed internal
        # diagnostics remain in the normal redacted service logs.
        mode = (
            "rearm-failed"
            if args.rearm_failed
            else ("apply" if args.apply else "dry-run")
        )
        print(
            json.dumps(
                {
                    "mode": mode,
                    "error": "managed_repository_reconciliation_failed",
                    "contains_urls_or_credentials": False,
                },
                sort_keys=True,
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "_safe_inventory_counts",
    "_active_authority_is_expected",
    "_job_lineage_authority_inventory",
    "build_parser",
    "execute",
    "main",
    "parse_args",
]
