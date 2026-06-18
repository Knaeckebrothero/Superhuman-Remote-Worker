# orchestrator/services/grants_service.py
"""Async resolution of a principal's effective capability grants. Pure logic in
src/core/capability_grants.py; this is the DB glue (mirrors config_resolver's
pure-core / async split)."""

from __future__ import annotations

from typing import Any

from src.core.capability_grants import resolve_grants


async def resolve_grants_for(
    postgres_db, *, user_id: str | None, project_ids: list[str]
) -> dict[str, Any]:
    scoped = await postgres_db.list_grants_for_scopes(
        user_id=user_id, project_ids=project_ids or []
    )
    return resolve_grants(
        user_rows=scoped["user"],
        project_rows=scoped["project"],
        global_rows=scoped["global"],
    )
