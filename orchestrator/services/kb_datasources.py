"""Control-plane helpers for external OKF Knowledge Base datasources."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from .kb_git_source import RemoteKnowledgeGitSource, validate_git_remote_url
from .kb_reindex import knowledge_blob_map, reindex_kb


def _external_reindex_limit() -> int:
    """Bound distinct-repository clone/embed work in one orchestrator process."""
    try:
        configured = int(os.getenv("KB_EXTERNAL_REINDEX_CONCURRENCY", "2"))
    except ValueError:
        configured = 2
    return max(1, min(configured, 16))


_external_reindex_semaphore = asyncio.Semaphore(_external_reindex_limit())


def kb_source_from_datasource(datasource: dict[str, Any]) -> RemoteKnowledgeGitSource:
    """Build the credential-owning source; callers must keep it orchestrator-side."""
    config = datasource.get("config") or {}
    connection_url = validate_git_remote_url(
        str(datasource.get("connection_url") or ""), allow_local=False
    )
    return RemoteKnowledgeGitSource(
        connection_url,
        branch=datasource.get("default_branch") or None,
        credentials=datasource.get("credentials") or {},
        root_path=str(config.get("root_path") or ""),
        label=f"datasource:{datasource['id']}",
    )


async def reindex_kb_datasource(
    datasource: dict[str, Any],
    *,
    store: Any,
    embedding_service: Any,
    force_full: bool = False,
    is_active: Callable[[], Awaitable[bool]],
    reindex_fn: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Index one external datasource under its stable datasource UUID."""
    if str(datasource.get("type") or "").lower() != "kb":
        raise ValueError("Datasource is not an OKF Knowledge Base")
    kb_id = uuid.UUID(str(datasource["id"]))
    config = datasource.get("config") or {}
    # Per-KB locks prevent duplicate writers, but create/update/manual requests
    # can target many distinct repositories at once. Bound the expensive Git +
    # embedding section so one account cannot fan out unbounded clone/API work;
    # queued/pending sources remain recoverable by the periodic sweep.
    async with _external_reindex_semaphore:
        runner = reindex_fn or reindex_kb
        return await runner(
            source=kb_source_from_datasource(datasource),
            store=store,
            embedding_service=embedding_service,
            kb_id=kb_id,
            root_path=str(config.get("root_path") or ""),
            source_label=f"datasource:{kb_id}",
            branch=datasource.get("default_branch") or None,
            force_full=force_full,
            is_active=is_active,
        )


async def test_kb_datasource(datasource: dict[str, Any]) -> dict[str, Any]:
    """Resolve and inspect an immutable snapshot without embedding or writes."""
    async with _external_reindex_semaphore:
        source = kb_source_from_datasource(datasource)
        head = await source.get_head()
        if not head:
            return {
                "status": "error",
                "message": "Repository branch has no resolvable HEAD",
                "head": None,
                "note_count": 0,
            }
        async with source.snapshot(head) as snapshot:
            notes = await snapshot.list_tree()
    root_path = str((datasource.get("config") or {}).get("root_path") or "")
    note_count = len(knowledge_blob_map(notes, root_path))
    if not note_count:
        location = f" under '{root_path}'" if root_path else ""
        return {
            "status": "error",
            "message": f"No Markdown notes found{location}",
            "head": head,
            "note_count": 0,
        }
    return {
        "status": "ok",
        "message": f"Found {note_count} Markdown note(s)",
        "head": head,
        "note_count": note_count,
    }


def index_status_payload(
    datasource_id: str, watermark: Optional[Any]
) -> dict[str, Any]:
    """Serialize credential-free operational status for REST/Cockpit."""
    if watermark is None:
        return {
            "datasource_id": datasource_id,
            "status": "pending",
            "source_head": None,
            "indexed_commit": None,
            "pipeline_version": None,
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
            "notes_done": None,
            "notes_total": None,
        }
    raw = asdict(watermark)
    result: dict[str, Any] = {"datasource_id": datasource_id}
    for key in (
        "status",
        "source_head",
        "indexed_commit",
        "pipeline_version",
        "last_attempt_at",
        "last_success_at",
        "last_error",
        "notes_done",
        "notes_total",
    ):
        value = raw.get(key)
        if isinstance(value, datetime):
            value = value.isoformat()
        result[key] = value
    return result


__all__ = [
    "index_status_payload",
    "kb_source_from_datasource",
    "reindex_kb_datasource",
    "test_kb_datasource",
]
