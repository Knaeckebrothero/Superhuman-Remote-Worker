#!/usr/bin/env python3
"""Read-only inventory for managed repository credential adoption.

The report intentionally emits no URLs, credentials, ciphertext, public keys,
SSH endpoints, or repository contents. Run it against the application database
before rollout and again after reconciliation. Exit 2 means credential-bearing
legacy rows or managed scopes without active repository authority remain.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_ORCH = _ROOT / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

from database.postgres import PostgresDB  # noqa: E402

_USERINFO_SQL = """
    value ~ '^[A-Za-z][A-Za-z0-9+.-]*://[^/@[:space:]]+@'
    OR value ~ '^[^/[:space:]]+@[^:]+:'
"""


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only inventory of credential-bearing or unproven managed "
            "repository scopes. Database connection settings come from the "
            "normal Postgres environment variables."
        )
    )
    parser.parse_args()
    db = PostgresDB()
    await db.connect()
    try:
        async with db.acquire() as conn:
            project_rows = await conn.fetch(
                f"""
                SELECT id, project_id, name, role
                  FROM project_repositories
                 WHERE is_managed
                   AND ({_USERINFO_SQL.replace("value", "repo_url")})
                 ORDER BY id
                """
            )
            job_rows = await conn.fetch(
                f"""
                SELECT id, project_id, repo_name, status
                  FROM jobs
                 WHERE ({_USERINFO_SQL.replace("value", "context->>'git_remote_url'")})
                 ORDER BY id
                """
            )
            thread_rows = await conn.fetch(
                f"""
                SELECT id, project_id, status,
                       metadata->'workspace_container'->>'repo_name' AS repo_name
                  FROM threads
                 WHERE ({_USERINFO_SQL.replace("value", "metadata->'workspace_container'->>'git_remote_url'")})
                 ORDER BY id
                """
            )
            authority_table = bool(
                await conn.fetchval(
                    "SELECT to_regclass('public.managed_repository_authorities') "
                    "IS NOT NULL"
                )
            )
            unproven: list[dict[str, str | None]] = []
            scope_rows = []
            creation_intents: list[dict[str, str | None]] = []
            historical_shared_jobs = await conn.fetch(
                """
                SELECT job.id, job.project_id, job.status,
                       repository.id AS repository_id,
                       repository.name AS repo_name
                  FROM jobs AS job
                  LEFT JOIN project_repositories AS repository
                    ON repository.project_id = job.project_id
                   AND repository.role = 'jobs'
                 WHERE job.project_id IS NOT NULL
                   AND job.parent_job_id IS NULL
                   AND job.repo_name IS NULL
                   AND job.branch_name IS NOT NULL
                   AND job.status IN (
                       'created', 'processing', 'paused', 'pending_review'
                   )
                 ORDER BY job.id
                """
            )
            if authority_table:
                scope_rows = await conn.fetch(
                    """
                    WITH scopes AS (
                        SELECT 'project_repository'::text AS kind, id AS scope_id,
                               project_id, name AS repo_name, role,
                               CASE
                                   WHEN role = 'reference' OR read_only
                                   THEN 'read'
                                   ELSE 'write'
                               END AS access_mode
                          FROM project_repositories AS repository
                         WHERE is_managed
                           AND role <> 'knowledge'
                           AND (
                               (
                                   role <> 'jobs'
                                   AND EXISTS (
                                       SELECT 1 FROM jobs AS job
                                        WHERE job.project_id =
                                              repository.project_id
                                          AND job.status IN (
                                              'created', 'processing', 'paused',
                                              'pending_review'
                                          )
                                   )
                               )
                               OR (
                                   role = 'jobs'
                                   AND EXISTS (
                                       SELECT 1 FROM jobs AS job
                                        WHERE job.project_id =
                                              repository.project_id
                                          AND job.parent_job_id IS NULL
                                          AND job.repo_name IS NULL
                                          AND job.branch_name IS NOT NULL
                                          AND job.status IN (
                                              'created', 'processing', 'paused',
                                              'pending_review'
                                          )
                                   )
                               )
                           )
                        UNION ALL
                        SELECT 'job', id, project_id, repo_name, NULL::text, 'write'
                          FROM jobs
                         WHERE parent_job_id IS NULL AND repo_name IS NOT NULL
                           AND status IN (
                               'created', 'processing', 'paused', 'pending_review'
                           )
                        UNION ALL
                        SELECT 'thread', id, project_id,
                               metadata->'workspace_container'->>'repo_name',
                               NULL::text, 'write'
                          FROM threads
                         WHERE metadata->'workspace_container'->>'repo_name'
                               IS NOT NULL
                           AND status <> 'ended'
                    )
                    SELECT scopes.kind, scopes.scope_id, scopes.project_id,
                           scopes.repo_name, scopes.role, scopes.access_mode,
                           NOT EXISTS (
                           SELECT 1
                             FROM managed_repository_authorities AS authority
                            WHERE authority.repo_name = scopes.repo_name
                              AND authority.status = 'active'
                              AND authority.access_mode = scopes.access_mode
                              AND (
                                  (
                                      authority.authority_kind = scopes.kind
                                      AND authority.authority_id = scopes.scope_id
                                  )
                                  OR (
                                      scopes.kind = 'job'
                                      AND authority.authority_kind =
                                          'project_repository'
                                      AND EXISTS (
                                          SELECT 1
                                            FROM project_repositories AS repository
                                           WHERE repository.id =
                                                 authority.authority_id
                                             AND repository.project_id =
                                                 scopes.project_id
                                             AND repository.name =
                                                 scopes.repo_name
                                             AND repository.is_managed
                                             AND repository.role = 'jobs'
                                      )
                                  )
                              )
                           ) AS is_unproven
                      FROM scopes
                    ORDER BY scopes.kind, scopes.scope_id
                    """
                )
                unproven = [
                    {
                        "kind": str(row["kind"]),
                        "scope_id": str(row["scope_id"]),
                        "project_id": (
                            str(row["project_id"])
                            if row["project_id"] is not None
                            else None
                        ),
                        "repo_name": str(row["repo_name"]),
                        "role": str(row["role"]) if row["role"] else None,
                        "access_mode": str(row["access_mode"]),
                    }
                    for row in scope_rows
                    if row["is_unproven"]
                ]
                creation_table = bool(
                    await conn.fetchval(
                        "SELECT to_regclass("
                        "'public.managed_repository_creation_intents') "
                        "IS NOT NULL"
                    )
                )
                if creation_table:
                    rows = await conn.fetch(
                        """
                        SELECT id, project_id, authority_kind, authority_id,
                               repo_name, access_mode, status, failure_class
                          FROM managed_repository_creation_intents
                         WHERE status IN ('pending', 'deleting')
                            OR failure_class IS NOT NULL
                         ORDER BY id
                        """
                    )
                    creation_intents = [
                        {
                            "intent_id": str(row["id"]),
                            "project_id": (
                                str(row["project_id"])
                                if row["project_id"] is not None
                                else None
                            ),
                            "authority_kind": str(row["authority_kind"]),
                            "authority_id": str(row["authority_id"]),
                            "repo_name": str(row["repo_name"]),
                            "access_mode": str(row["access_mode"]),
                            "status": str(row["status"]),
                            "failure_class": (
                                str(row["failure_class"])
                                if row["failure_class"] is not None
                                else None
                            ),
                        }
                        for row in rows
                    ]
    finally:
        await db.disconnect()

    def safe_rows(rows, *, kind: str) -> list[dict[str, str | None]]:
        return [
            {
                "kind": kind,
                "scope_id": str(row["id"]),
                "project_id": (
                    str(row["project_id"]) if row["project_id"] is not None else None
                ),
                "repo_name": (
                    str(
                        row["name"]
                        if kind == "project_repository"
                        else row["repo_name"]
                    )
                    if (
                        row["name"]
                        if kind == "project_repository"
                        else row["repo_name"]
                    )
                    is not None
                    else None
                ),
                "state": str(
                    row["role"] if kind == "project_repository" else row["status"]
                ),
            }
            for row in rows
        ]

    credentialed = [
        *safe_rows(project_rows, kind="project_repository"),
        *safe_rows(job_rows, kind="job"),
        *safe_rows(thread_rows, kind="thread"),
    ]
    by_repo: dict[str, list] = {}
    for row in scope_rows:
        by_repo.setdefault(str(row["repo_name"]), []).append(row)
    ambiguous: list[dict[str, object]] = []
    for repo_name, rows in sorted(by_repo.items()):
        projects = [row for row in rows if row["kind"] == "project_repository"]
        jobs = [row for row in rows if row["kind"] == "job"]
        threads = [row for row in rows if row["kind"] == "thread"]
        is_ambiguous = False
        if threads:
            is_ambiguous = len(threads) != 1 or bool(projects or jobs)
        elif projects:
            project_id = projects[0]["project_id"]
            is_ambiguous = (
                len(projects) != 1
                or (bool(jobs) and projects[0]["role"] != "jobs")
                or any(row["project_id"] != project_id for row in jobs)
            )
        elif len(jobs) > 1:
            is_ambiguous = True
        if is_ambiguous:
            ambiguous.append(
                {
                    "repo_name": repo_name,
                    "durable_scope_count": len(rows),
                    "scope_kinds": sorted({str(row["kind"]) for row in rows}),
                }
            )
    report = {
        "authority_table_present": authority_table,
        "credentialed_legacy_rows": len(credentialed),
        "managed_scopes_without_active_authority": len(unproven),
        "ambiguous_repository_names": len(ambiguous),
        "historical_shared_jobs_candidates": len(historical_shared_jobs),
        "incomplete_creation_intents": len(creation_intents),
        "legacy_rows": credentialed,
        "unproven_scopes": unproven,
        "ambiguous_scopes": ambiguous,
        "historical_shared_jobs": [
            {
                "job_id": str(row["id"]),
                "project_id": str(row["project_id"]),
                "state": str(row["status"]),
                "repository_id": (
                    str(row["repository_id"])
                    if row["repository_id"] is not None
                    else None
                ),
                "repo_name": (
                    str(row["repo_name"]) if row["repo_name"] is not None else None
                ),
            }
            for row in historical_shared_jobs
        ],
        "creation_intents_requiring_attention": creation_intents,
        "contains_urls_or_credentials": False,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if credentialed or unproven or ambiguous or creation_intents else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
