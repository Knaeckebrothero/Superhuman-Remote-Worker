"""Durable scheduling and adoption for idle stateless cloud pushes.

The generation is the effect authority. These rows only schedule execution;
they never decide whether bytes are committed. Call transaction helpers on
an acquired connection. Lock order is threads -> session queue -> task queue
-> generations, matching session admission and retirement.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from shared.cloud_sync_generations import (
    PUSH_OWNER_STALE_AFTER_SECONDS,
    _WORKSPACE_BOUND_SQL,
    _requirements,
)
from shared.run_queue import (
    UNIT_KIND_BG_TASK,
    claim_unit,
    complete_unit,
    enqueue_unit,
)
from shared.session_retirement import STATELESS_STOP_KEYS, stateless_stop_markers

TASK_KIND = "cloud_push"


def enabled() -> bool:
    import os

    return os.getenv("STATELESS_CLOUD_PUSH_RECOVERY_ENABLED", "false").lower() in {
        "true",
        "1",
        "yes",
        "on",
    }


def thread_is_eligible(thread: Any) -> bool:
    if (
        not thread
        or thread["execution_lane"] != "stateless"
        or str(thread["status"]) not in {"created", "active", "awaiting_user"}
    ):
        return False
    try:
        metadata = thread["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return not stateless_stop_markers(metadata) and metadata.get(
            "protected_cloud"
        ) in (None, False)
    except (TypeError, ValueError, RuntimeError):
        return False


_STALE_SQL = """
SELECT generation.thread_id, generation.mount_id,
       generation.required_generation, generation.workspace_generation
FROM thread_cloud_sync_generations generation
JOIN threads thread ON thread.id = generation.thread_id
JOIN run_queue session ON session.unit_id = thread.id
WHERE generation.acknowledged_generation < generation.required_generation
  AND COALESCE(generation.push_heartbeat_at, generation.required_at)
      < now() - make_interval(secs => $1::float8)
  AND session.unit_kind = 'session_turn' AND session.state = 'done'
  AND thread.execution_lane = 'stateless'
  AND thread.status IN ('created', 'active', 'awaiting_user')
  AND NOT (COALESCE(thread.metadata, '{}'::jsonb) ?| $2::text[])
  AND COALESCE(thread.metadata->'protected_cloud', 'false'::jsonb) = 'false'::jsonb
  AND NOT EXISTS (
      SELECT 1 FROM run_queue_bg_tasks task
      WHERE task.thread_id = generation.thread_id AND task.task_kind = 'cloud_push'
        AND task.detail->>'mount_id' = generation.mount_id
        AND task.detail->>'generation' = generation.required_generation::text
  )
ORDER BY COALESCE(generation.push_heartbeat_at, generation.required_at),
         generation.thread_id, generation.mount_id
LIMIT $3::int
"""


async def stale_pending_pushes(conn: Any, *, limit: int = 50) -> list[dict]:
    return [
        dict(row)
        for row in await conn.fetch(
            _STALE_SQL,
            PUSH_OWNER_STALE_AFTER_SECONDS,
            sorted(STATELESS_STOP_KEYS),
            limit,
        )
    ]


async def enqueue_cloud_push(conn: Any, candidate: dict) -> str | None:
    """Recheck under the thread lock; one bounded retry history per generation.

    The queue's general dedup contract stays queued-only. The periodic sweep
    is not a fresh signal: it must not reset attempts by minting another unit
    after the same generation has exhausted its retries.
    """
    thread_id = candidate["thread_id"]
    async with conn.transaction():
        thread = await conn.fetchrow(
            "SELECT * FROM threads WHERE id=$1::uuid FOR UPDATE", thread_id
        )
        if not thread_is_eligible(thread):
            return None
        session = await conn.fetchrow(
            "SELECT state FROM run_queue WHERE unit_id=$1::uuid FOR UPDATE", thread_id
        )
        if session is None or session["state"] != "done":
            return None
        current = await conn.fetchval(
            """
            SELECT 1 FROM thread_cloud_sync_generations
            WHERE thread_id=$1::uuid AND mount_id=$2::text
              AND required_generation=$3::bigint
              AND workspace_generation=$4::text
              AND acknowledged_generation<required_generation
              AND COALESCE(push_heartbeat_at, required_at)
                  < now()-make_interval(secs=>$5::float8)
            FOR UPDATE
        """,
            thread_id,
            candidate["mount_id"],
            candidate["required_generation"],
            candidate["workspace_generation"],
            PUSH_OWNER_STALE_AFTER_SECONDS,
        )
        if not current:
            return None
        exists = await conn.fetchval(
            """
            SELECT 1 FROM run_queue_bg_tasks WHERE thread_id=$1::uuid
              AND task_kind='cloud_push' AND detail->>'mount_id'=$2::text
              AND detail->>'generation'=$3::text LIMIT 1
        """,
            thread_id,
            candidate["mount_id"],
            str(candidate["required_generation"]),
        )
        if exists:
            return None
        unit_id = uuid4()
        detail = {
            "mount_id": candidate["mount_id"],
            "generation": candidate["required_generation"],
            "workspace_generation": candidate["workspace_generation"],
        }
        result = await enqueue_unit(
            conn,
            unit_id=unit_id,
            unit_kind=UNIT_KIND_BG_TASK,
            fair_key=str(thread["user_id"] or thread_id),
            priority=-100,
            dedup_key=f"cloud_push:{thread_id}:{candidate['mount_id']}:{candidate['required_generation']}",
        )
        if result.status == "deduped":
            return None
        await conn.execute(
            """
            INSERT INTO run_queue_bg_tasks (unit_id,thread_id,task_kind,detail)
            VALUES ($1,$2,'cloud_push',$3::jsonb)
        """,
            unit_id,
            thread_id,
            json.dumps(detail),
        )
        return str(unit_id)


async def claim_bg_task(conn: Any, *, pod_name: str):
    return await claim_unit(conn, unit_kind=UNIT_KIND_BG_TASK, pod_name=pod_name)


async def load_bg_task(
    conn: Any, *, unit_id, lease_token: int, pod_name: str
) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT task.* FROM run_queue_bg_tasks task JOIN run_queue queue USING(unit_id)
        WHERE task.unit_id=$1::uuid AND queue.unit_kind='bg_task'
          AND queue.state='leased' AND queue.lease_token=$2::bigint
          AND queue.leased_by=$3::text AND queue.leased_until>now()
    """,
        UUID(str(unit_id)),
        lease_token,
        pod_name,
    )
    if row is None:
        return None
    task = dict(row)
    if isinstance(task["detail"], str):
        task["detail"] = json.loads(task["detail"])
    return task


async def task_is_pending(conn: Any, task: dict) -> bool:
    detail = task["detail"]
    return bool(
        await conn.fetchval(
            """
        SELECT 1 FROM thread_cloud_sync_generations
        WHERE thread_id=$1::uuid AND mount_id=$2::text
          AND required_generation=$3::bigint AND workspace_generation=$4::text
          AND acknowledged_generation<required_generation
    """,
            task["thread_id"],
            detail["mount_id"],
            int(detail["generation"]),
            detail["workspace_generation"],
        )
    )


class PushAdoptionDeferred(RuntimeError):
    """Another current writer owns the thread; keep this task retryable."""


async def adopt_bg_push(
    conn: Any,
    *,
    unit_id,
    lease_token: int,
    pod_name: str,
    pod_uid: str,
    workspace_generation: str,
):
    """CAS an idle thread's stale pushes under the exact background lease.

    Adopt all pending mounts together because the push fence uses a single
    token across the thread. A session claim takes the same queue-row lock;
    after it claims, its normal adoption fences this writer before pulling.
    """
    async with conn.transaction():
        task = await load_bg_task(
            conn, unit_id=unit_id, lease_token=lease_token, pod_name=pod_name
        )
        if task is None or task["task_kind"] != TASK_KIND:
            raise RuntimeError("background push lease is no longer current")
        thread_id = task["thread_id"]
        thread = await conn.fetchrow(
            "SELECT * FROM threads WHERE id=$1::uuid FOR UPDATE", thread_id
        )
        if not thread_is_eligible(thread):
            raise RuntimeError("background push thread authority unavailable")
        session = await conn.fetchrow(
            "SELECT state FROM run_queue WHERE unit_id=$1::uuid FOR UPDATE", thread_id
        )
        await conn.fetchrow(
            "SELECT unit_id FROM run_queue WHERE unit_id=$1::uuid FOR UPDATE",
            UUID(str(unit_id)),
        )
        if (
            await load_bg_task(
                conn, unit_id=unit_id, lease_token=lease_token, pod_name=pod_name
            )
            is None
        ):
            raise RuntimeError("background push lease is no longer current")
        if not await task_is_pending(conn, task):
            return {}
        if session is None or session["state"] != "done":
            raise PushAdoptionDeferred("session has priority over background push")
        if workspace_generation != task["detail"]["workspace_generation"]:
            raise RuntimeError("background push workspace changed")
        bound = await conn.fetchval(
            "SELECT 1 FROM threads thread WHERE thread.id=$1::uuid AND "
            + _WORKSPACE_BOUND_SQL.format(param="$2"),
            thread_id,
            workspace_generation,
        )
        if not bound:
            raise RuntimeError("background push workspace authority unavailable")
        rows = await conn.fetch(
            """
            SELECT *, COALESCE(push_heartbeat_at,required_at)
                >= now()-make_interval(secs=>$2::float8) AS owner_alive
            FROM thread_cloud_sync_generations WHERE thread_id=$1::uuid
            ORDER BY mount_id FOR UPDATE
        """,
            thread_id,
            PUSH_OWNER_STALE_AFTER_SECONDS,
        )
        pending = [
            row
            for row in rows
            if row["acknowledged_generation"] < row["required_generation"]
        ]
        if any(row["owner_alive"] for row in pending):
            raise PushAdoptionDeferred("a live push owner still holds the thread")
        if any(row["workspace_generation"] != workspace_generation for row in pending):
            raise RuntimeError("pending push belongs to another workspace")
        token = max(int(row["push_owner_token"]) for row in rows) + 1
        adopted = await conn.fetch(
            """
            UPDATE thread_cloud_sync_generations SET push_owner_token=$2::bigint,
                push_owner_pod=$3::text,push_owner_pod_uid=$4::text,
                push_heartbeat_at=now(),push_failed_at=NULL,push_error=NULL,
                push_started_at=COALESCE(push_started_at,now())
            WHERE thread_id=$1::uuid AND acknowledged_generation<required_generation
            RETURNING *
        """,
            thread_id,
            token,
            pod_name,
            pod_uid,
        )
        return _requirements(adopted)


async def complete_bg_task(conn: Any, claim) -> str | None:
    return await complete_unit(
        conn, unit_id=claim.unit_id, lease_token=claim.lease_token, consumed_seq=None
    )


async def bg_push_lease_is_current(
    conn: Any, *, unit_id, lease_token: int, pod_name: str
) -> bool:
    return bool(
        await conn.fetchval(
            """
        SELECT 1 FROM run_queue queue JOIN run_queue_bg_tasks task USING(unit_id)
        JOIN threads thread ON thread.id=task.thread_id
        WHERE queue.unit_id=$1::uuid AND queue.unit_kind='bg_task'
          AND queue.state='leased' AND queue.lease_token=$2::bigint
          AND queue.leased_by=$3::text AND queue.leased_until>now()
          AND thread.execution_lane='stateless'
          AND thread.status IN ('created','active','awaiting_user')
          AND NOT (COALESCE(thread.metadata,'{}'::jsonb) ?| $4::text[])
          AND COALESCE(thread.metadata->'protected_cloud','false'::jsonb)='false'::jsonb
    """,
            UUID(str(unit_id)),
            lease_token,
            pod_name,
            sorted(STATELESS_STOP_KEYS),
        )
    )


async def abandon_bg_push(conn: Any, *, thread_id, push_owner_token: int) -> None:
    """Retire this exact writer before releasing its task lease."""
    await conn.execute(
        """
        UPDATE thread_cloud_sync_generations SET push_owner_token=push_owner_token+1,
            push_owner_pod=NULL,push_owner_pod_uid=NULL,
            push_heartbeat_at=now()-interval '91 seconds'
        WHERE thread_id=$1::uuid AND push_owner_token=$2::bigint
          AND acknowledged_generation<required_generation
    """,
        UUID(str(thread_id)),
        push_owner_token,
    )


async def fail_bg_task(
    conn: Any, claim, *, error: str, deferred: bool = False
) -> str | None:
    """Bound actual failures; contention does not spend the failure budget."""
    return await conn.fetchval(
        """
        UPDATE run_queue SET
            state=CASE WHEN NOT $4::boolean AND attempts_since_completion>=max_attempts THEN 'parked' ELSE 'queued' END,
            park_reason=CASE WHEN NOT $4::boolean AND attempts_since_completion>=max_attempts THEN 'cloud_push_failed' ELSE NULL END,
            parked_at=CASE WHEN NOT $4::boolean AND attempts_since_completion>=max_attempts THEN now() ELSE NULL END,
            attempts_since_completion=GREATEST(0,attempts_since_completion-CASE WHEN $4::boolean THEN 1 ELSE 0 END),
            last_error=$3::text, leased_by=NULL,leased_until=NULL,
            queued_at=now(),run_after=now()+make_interval(secs=>CASE WHEN $4::boolean THEN 15.0 ELSE LEAST(300.0,5.0*power(3.0,LEAST(attempts_since_completion-1,4))) END)
        WHERE unit_id=$1::uuid AND unit_kind='bg_task' AND state='leased' AND lease_token=$2::bigint
        RETURNING state
    """,
        claim.unit_id,
        claim.lease_token,
        error[:1000],
        deferred,
    )
