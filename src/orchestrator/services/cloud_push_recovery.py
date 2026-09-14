"""Orchestrator admission and credential boundary for cloud-only queue work."""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable
from uuid import UUID

from shared.cloud_push_tasks import (
    enabled,
    enqueue_cloud_push,
    load_bg_task,
    stale_pending_pushes,
    task_is_pending,
    thread_is_eligible,
)

logger = logging.getLogger(__name__)


async def sweep_stale_cloud_pushes(db: Any) -> int:
    if not enabled():
        return 0
    count = 0
    async with db.acquire() as conn:
        candidates = await stale_pending_pushes(conn)
        for candidate in candidates:
            unit_id = await enqueue_cloud_push(conn, candidate)
            if unit_id:
                count += 1
                logger.info(
                    "cloud push recovery enqueued: thread=%s unit=%s generation=%s",
                    candidate["thread_id"],
                    unit_id,
                    candidate["required_generation"],
                )
    return count


class CloudPushBundleRefused(RuntimeError):
    pass


def _workspace_authority(thread: dict) -> tuple:
    metadata = thread["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return metadata.get("_workspace_binding"), metadata.get("workspace_container")


async def build_cloud_push_bundle(
    db: Any,
    *,
    unit_id: str,
    lease_token: int,
    pod_name: str,
    pod_uid: str,
    resolve_workspace: Callable[[dict], Awaitable[dict]],
) -> dict:
    if not enabled():
        # During a flag rollout, old executors can still hold a task while
        # the orchestrator has disabled recovery. Return no credentials and
        # do not spend the task's failure budget on that deployment window.
        return {"unit_id": unit_id, "unit_kind": "bg_task", "deferred": True}
    if not pod_name or not pod_uid:
        raise CloudPushBundleRefused("background push lacks claimant identity")
    async with db.acquire() as conn:
        task = await load_bg_task(
            conn, unit_id=unit_id, lease_token=lease_token, pod_name=pod_name
        )
    if task is None or task["task_kind"] != "cloud_push":
        raise CloudPushBundleRefused("background push lease validation failed")
    thread_id = str(task["thread_id"])
    base = {
        "unit_id": unit_id,
        "thread_id": thread_id,
        "unit_kind": "bg_task",
        "task_kind": "cloud_push",
    }
    async with db.thread_datasource_lock(thread_id):
        thread = await db.get_thread(thread_id)
        if not thread_is_eligible(thread):
            raise CloudPushBundleRefused("background push thread authority unavailable")
        async with db.acquire() as conn:
            if not await task_is_pending(conn, task):
                return {**base, "obsolete": True}
            session_state = await conn.fetchval(
                "SELECT state FROM run_queue WHERE unit_id=$1::uuid", task["thread_id"]
            )
        if session_state != "done":
            return {**base, "deferred": True}
        snapshot = _workspace_authority(thread)
        resolved = await resolve_workspace(thread)
        workspace = resolved["workspace"]
        if (
            workspace.get("workspace_generation")
            != task["detail"]["workspace_generation"]
        ):
            raise CloudPushBundleRefused("background push workspace changed")
        if not resolved.get("cloud_sync"):
            raise CloudPushBundleRefused(
                "background push cloud destination unavailable"
            )
        # Resolution may wait on control-plane and cloud APIs. Serialize the
        # last check with End/session admission before credentials escape.
        async with db.acquire() as conn:
            async with conn.transaction():
                final = await conn.fetchrow(
                    "SELECT * FROM threads WHERE id=$1::uuid FOR UPDATE",
                    task["thread_id"],
                )
                if (
                    not thread_is_eligible(final)
                    or _workspace_authority(final) != snapshot
                ):
                    raise CloudPushBundleRefused(
                        "background push authority changed during assembly"
                    )
                session = await conn.fetchrow(
                    "SELECT state FROM run_queue WHERE unit_id=$1::uuid FOR UPDATE",
                    task["thread_id"],
                )
                await conn.fetchrow(
                    "SELECT unit_id FROM run_queue WHERE unit_id=$1::uuid FOR UPDATE",
                    UUID(unit_id),
                )
                if (
                    await load_bg_task(
                        conn,
                        unit_id=unit_id,
                        lease_token=lease_token,
                        pod_name=pod_name,
                    )
                    is None
                ):
                    raise CloudPushBundleRefused(
                        "background push lease validation failed"
                    )
                if not await task_is_pending(conn, task):
                    return {**base, "obsolete": True}
                if session is None or session["state"] != "done":
                    return {**base, "deferred": True}
        # Explicit projection: model, repository, tool and datasource secrets
        # from ordinary attach assembly never enter this response.
        return {**base, "workspace": workspace, "cloud_sync": resolved["cloud_sync"]}
