"""Import historical frozen execution configurations without launching work.

Run ``python -m orchestrator.operator_cli.manifest_execution_migration`` inside
the configured orchestrator environment. The default reports a bounded batch;
``--apply`` writes only rows with an existing rendered snapshot. Missing source
snapshots are reported explicitly and never reconstructed from current experts.
Output contains identities and reason codes, never private settings or secrets.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from typing import Any
from uuid import UUID

from orchestrator.services.manifest_execution_snapshot import (
    object_value,
    rendered_srw_snapshot,
)
from orchestrator.services.manifest_store import ManifestStore


def historical_snapshot(row: dict, *, work_kind: str, image: str) -> dict | None:
    """Preserve recorded prompts/models; no loader or live Expert read occurs."""
    metadata = (
        object_value(row.get("metadata"))
        if work_kind == "Session"
        else object_value(row.get("context"))
    )
    source = (
        row.get("resolved_config")
        if work_kind == "Job"
        else metadata.get("resolved_config")
    )
    if source is None:
        return None
    blob = object_value(source)
    if not isinstance(blob.get("agent"), dict) or not isinstance(
        blob.get("prompts"), dict
    ):
        raise ValueError("invalid-frozen-config")
    projects = [str(row["project_id"])] if row.get("project_id") else []
    selection = object_value(metadata.get("datasource_selection"))
    selected = selection.get("datasource_ids", metadata.get("datasource_ids", []))
    if not isinstance(selected, list):
        raise ValueError("invalid-connector-selection")
    selected = [str(UUID(str(value))) for value in selected]
    revisions = object_value(selection.get("policy_revisions"))
    result = rendered_srw_snapshot(
        blob,
        blob["agent"],
        work_kind=work_kind,
        work_id=str(row["id"]),
        owner_id=str(row["user_id"]) if row.get("user_id") else None,
        project_ids=projects,
        config_name=row.get("config_name")
        or ("worker_base" if work_kind == "Job" else "session_base"),
        description=row.get("description") or row.get("title") or "Imported execution",
        datasource_ids=selected,
        policy_revisions=revisions,
        image=image,
        dependencies=[],
    )
    result["document"]["metadata"]["annotations"]["srw.io/import-source"] = (
        "historical-resolved-config"
    )
    result["resolved"]["metadata"]["annotations"]["srw.io/import-source"] = (
        "historical-resolved-config"
    )
    return result


async def migrate_batch(
    db: Any,
    *,
    apply: bool,
    limit: int = 100,
    kind: str = "all",
    work_ids: list[str] | None = None,
    after: str | None = None,
) -> dict:
    """Lock and copy each exact source snapshot; never mutate lifecycle rows."""
    from orchestrator.services.manifest_experts import installed_srw_image

    if not 1 <= limit <= 500 or kind not in {"all", "Job", "Session"}:
        raise ValueError("Invalid migration batch")
    requested = [UUID(value) for value in work_ids] if work_ids else None
    cursor_kind = cursor_id = None
    if after is not None:
        cursor_kind, raw_id = after.split(":", 1)
        if cursor_kind not in {"Job", "Session"}:
            raise ValueError("Invalid migration cursor")
        cursor_id = UUID(raw_id)
    candidates = []
    for work_kind, table in (("Job", "jobs"), ("Session", "threads")):
        if kind not in {"all", work_kind} or len(candidates) >= limit:
            continue
        if cursor_kind == "Session" and work_kind == "Job":
            continue
        # Identifiers come only from the two fixed tuples above.
        session_filter = "AND source.kind='session'" if work_kind == "Session" else ""
        rows = await db.fetch(
            f"""SELECT source.id FROM {table} source
            WHERE ($1::uuid[] IS NULL OR source.id=ANY($1)) {session_filter}
            AND ($4::uuid IS NULL OR source.id>$4)
            AND NOT EXISTS(SELECT 1 FROM srw_execution_specs execution
                WHERE execution.work_kind=$2 AND execution.work_id=source.id)
            ORDER BY source.id LIMIT $3""",
            requested,
            work_kind,
            limit - len(candidates),
            cursor_id if cursor_kind == work_kind else None,
        )
        candidates.extend((work_kind, table, row["id"]) for row in rows)
    results = []
    for work_kind, table, work_id in candidates:
        async with db.transaction_scope() as conn:
            row = await conn.fetchrow(
                f"SELECT * FROM {table} WHERE id=$1 FOR UPDATE", work_id
            )
            if row is None:
                state = "source-disappeared"
            elif await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM srw_execution_specs WHERE work_kind=$1 AND work_id=$2)",
                work_kind,
                work_id,
            ):
                state = "already-canonical"
            else:
                try:
                    prepared = historical_snapshot(
                        dict(row),
                        work_kind=work_kind,
                        image=getattr(db, "manifest_runtime_image", None)
                        or installed_srw_image(),
                    )
                except (TypeError, ValueError, KeyError):
                    prepared = None
                    state = "invalid-frozen-config"
                else:
                    state = "ready" if prepared is not None else "requires-resolution"
                if prepared is not None and apply:
                    await ManifestStore(db).freeze_execution(
                        work_kind=work_kind,
                        work_id=str(work_id),
                        owner_id=str(row["user_id"]) if row.get("user_id") else None,
                        project_ids=[str(row["project_id"])]
                        if row.get("project_id")
                        else [],
                        conn=conn,
                        **prepared,
                    )
                    state = "imported"
            results.append(
                {"workKind": work_kind, "workId": str(work_id), "state": state}
            )
    next_cursor = (
        f"{candidates[-1][0]}:{candidates[-1][2]}" if len(candidates) == limit else None
    )
    return {
        "operation": "apply" if apply else "preview",
        "limit": limit,
        "nextCursor": next_cursor,
        "counts": dict(Counter(item["state"] for item in results)),
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--kind", choices=["all", "Job", "Session"], default="all")
    parser.add_argument("--work-id", action="append", default=[])
    parser.add_argument("--after", help="nextCursor from the previous bounded batch")
    return parser


async def run(args: argparse.Namespace) -> dict:
    from orchestrator.database.postgres import PostgresDB

    if not 1 <= args.limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    ids = [str(UUID(value)) for value in args.work_id]
    db = PostgresDB(min_connections=1, max_connections=2)
    await db.connect()
    try:
        return await migrate_batch(
            db,
            apply=args.apply,
            limit=args.limit,
            kind=args.kind,
            work_ids=ids,
            after=args.after,
        )
    finally:
        await db.close()


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        # Driver/configuration exceptions may contain transport coordinates.
        print(json.dumps({"error": "migration-failed", "category": type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
