"""One public spawn projection shared by job, session and internal child readers."""

import json
from typing import Any


def subagent_thread_payload(row: dict[str, Any]) -> dict[str, Any]:
    """One ``threads`` row of ``kind='subagent'`` as the roster publishes it.

    ``status`` is the child's lifecycle kind (``subagent_status``: running,
    completed, parked, interrupted, capped, error, cancelled) — the value a
    reader wants first; the thread's own ``active``/``ended`` rides along as
    ``thread_status``. The spawn facts the agent stamped at creation
    (isolation, write policy, the brief, the parent turn) come out of
    ``metadata.subagent``; ``metadata`` itself never leaves (it is the
    thread's internal envelope, redacted elsewhere for a reason).
    """
    metadata = row.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    spawn = metadata.get("subagent")
    if not isinstance(spawn, dict):
        spawn = {}
    payload = {
        "thread_id": str(row["id"]),
        "parent_job_id": (
            str(row["parent_job_id"]) if row.get("parent_job_id") else None
        ),
        "runtime_generation": (
            str(row["runtime_generation"])
            if row.get("runtime_generation") is not None
            else None
        ),
        "handle": row.get("subagent_handle"),
        "subagent_type": row.get("subagent_type"),
        "status": row.get("subagent_status"),
        "thread_status": row.get("status"),
        "outcome": row.get("subagent_outcome"),
        "error": row.get("subagent_error"),
        "turns": int(row.get("total_turns") or 0),
        "tokens": int(row.get("total_tokens") or 0),
        "report_path": row.get("report_path"),
        "parent_tool_call_id": row.get("parent_tool_call_id"),
        "parent_thread_id": (
            str(row["parent_thread_id"]) if row.get("parent_thread_id") else None
        ),
        "description": spawn.get("brief_description") or "",
        "isolation": spawn.get("isolation"),
        "write_policy": spawn.get("write_policy"),
        "owned_paths": list(spawn.get("owned_paths") or []),
        "parent_iteration": spawn.get("parent_iteration"),
        "parent_input_message_id": spawn.get("parent_input_message_id"),
        "parent_ai_message_id": spawn.get("parent_ai_message_id"),
        "fork": bool(spawn.get("fork", False)),
        "run_in_background": bool(spawn.get("run_in_background", False)),
        "started_at": row.get("created_at"),
        "ended_at": row.get("ended_at"),
        "last_activity": row.get("last_activity"),
    }
    if row.get("recovery_kind") is not None:
        payload["recovery_kind"] = row.get("recovery_kind")
    return payload
