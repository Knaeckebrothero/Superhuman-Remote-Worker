"""Durable execution authority for pinned persistent-session input.

``thread_messages`` is the transcript, not an inbox: restore presents those
rows as context and never schedules them.  This module keeps the smallest
separate state needed to make a persisted input reclaimable and to prove when
its one paid turn crossed provider admission.

All mutators use the repository lock order ``thread -> agent -> delivery``.
The agent and pod identities are server-issued observations; no model-visible
tool schema contains them.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid5


_THREAD_MESSAGE_ID_NAMESPACE = UUID("4b9d8f7e-2c3a-5d6b-8e1f-0a1b2c3d4e5f")


class InputDeliveryAuthorityLost(RuntimeError):
    """The calling runtime is not the exact current pinned thread owner."""


class InputDeliveryConflict(RuntimeError):
    """A stable delivery identity was reused for different input."""


def raw_message_id(delivery_id: str | UUID) -> str:
    return f"msg_delivery_{UUID(str(delivery_id)).hex}"


def message_row_id(delivery_id: str | UUID) -> UUID:
    return uuid5(_THREAD_MESSAGE_ID_NAMESPACE, raw_message_id(delivery_id))


def _dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


async def lock_runtime_authority(
    conn: Any,
    *,
    thread_id: str | UUID,
    agent_id: str | UUID,
    pod_uid: str,
) -> None:
    """Lock and prove exact reciprocal thread/agent/pod authority."""

    thread_uuid = UUID(str(thread_id))
    agent_uuid = UUID(str(agent_id))
    pod = str(pod_uid or "").strip()
    if not pod:
        raise InputDeliveryAuthorityLost("runtime pod identity is unavailable")

    thread = await conn.fetchrow(
        "SELECT id, agent_id, status, execution_lane FROM threads "
        "WHERE id = $1 FOR UPDATE",
        thread_uuid,
    )
    if (
        thread is None
        or str(thread["agent_id"] or "") != str(agent_uuid)
        or str(thread["execution_lane"] or "pinned") == "stateless"
        or str(thread["status"] or "") in {"ended", "suspended"}
    ):
        raise InputDeliveryAuthorityLost("thread runtime authority was lost")

    agent = await conn.fetchrow(
        "SELECT id, thread_id, pod_uid, status FROM agents WHERE id = $1 FOR SHARE",
        agent_uuid,
    )
    if (
        agent is None
        or str(agent["thread_id"] or "") != str(thread_uuid)
        or str(agent["pod_uid"] or "") != pod
        or str(agent["status"] or "") in {"offline", "deleted"}
    ):
        raise InputDeliveryAuthorityLost("agent runtime authority was lost")


async def persist_input_delivery(
    conn: Any,
    *,
    thread_id: str | UUID,
    delivery_id: str | UUID,
    role: str,
    content: str,
    source: str,
    turn_number: int | None,
    agent_id: str | UUID | None = None,
    pod_uid: str | None = None,
    runtime_generation: str | UUID | None = None,
) -> dict[str, Any]:
    """Atomically persist one transcript row and optionally claim execution.

    With no runtime identity this is an orchestrator-side durable persist only.
    With all three identity fields it also establishes/recovers the exact
    process claim.  Partial identity is always rejected.
    """

    delivery_uuid = UUID(str(delivery_id))
    thread_uuid = UUID(str(thread_id))
    row_id = message_row_id(delivery_uuid)
    source_value = str(source or "unknown")[:80]
    identity = (agent_id, pod_uid, runtime_generation)
    has_identity = all(value is not None for value in identity)
    if any(value is not None for value in identity) and not has_identity:
        raise InputDeliveryAuthorityLost("incomplete runtime identity")

    if has_identity:
        await lock_runtime_authority(
            conn,
            thread_id=thread_uuid,
            agent_id=str(agent_id),
            pod_uid=str(pod_uid),
        )
    else:
        # Transcript FK + activity updates follow the same parent-first order.
        exists = await conn.fetchval(
            "SELECT id FROM threads WHERE id = $1 FOR UPDATE", thread_uuid
        )
        if exists is None:
            raise InputDeliveryAuthorityLost("thread no longer exists")

    inserted = await conn.fetchrow(
        """
        INSERT INTO thread_messages
            (id, thread_id, role, content, turn_number)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (id) DO NOTHING
        RETURNING id, seq, thread_id, role, content
        """,
        row_id,
        thread_uuid,
        str(role),
        str(content),
        turn_number,
    )
    transcript_inserted = inserted is not None
    message = inserted or await conn.fetchrow(
        "SELECT id, seq, thread_id, role, content FROM thread_messages WHERE id = $1",
        row_id,
    )
    if (
        message is None
        or str(message["thread_id"]) != str(thread_uuid)
        or str(message["role"]) != str(role)
    ):
        raise InputDeliveryConflict("stable input identity conflicts with transcript")

    if transcript_inserted:
        await conn.execute(
            "UPDATE threads SET last_activity = CURRENT_TIMESTAMP, "
            "total_turns = GREATEST(total_turns, COALESCE($2, 0)) WHERE id = $1",
            thread_uuid,
            turn_number,
        )

    await conn.execute(
        """
        INSERT INTO thread_input_deliveries
            (delivery_id, thread_id, message_id, source)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (delivery_id) DO NOTHING
        """,
        delivery_uuid,
        thread_uuid,
        row_id,
        source_value,
    )
    delivery = await conn.fetchrow(
        "SELECT * FROM thread_input_deliveries WHERE delivery_id = $1 FOR UPDATE",
        delivery_uuid,
    )
    if (
        delivery is None
        or str(delivery["thread_id"]) != str(thread_uuid)
        or str(delivery["message_id"]) != str(row_id)
        or str(delivery["source"]) != source_value
    ):
        raise InputDeliveryConflict("stable input identity conflicts with delivery")

    # A wake renderer may observe newer project state on an ambiguous-response
    # retry. The first committed transcript payload is canonical for that
    # server-owned delivery identity; replay it rather than rejecting the retry
    # or queueing different text under one identity. Human delivery identities
    # retain the stricter invariant so internal misuse cannot alias two inputs.
    stored_content = str(message["content"] or "")
    if stored_content != str(content) and source_value != "officer_wake":
        raise InputDeliveryConflict("stable input identity conflicts with transcript")

    if has_identity and str(delivery["state"]) not in {"admitted", "settled"}:
        same_runtime = (
            str(delivery["owner_agent_id"] or "") == str(agent_id)
            and str(delivery["owner_pod_uid"] or "") == str(pod_uid)
            and str(delivery["owner_runtime_generation"] or "")
            == str(runtime_generation)
        )
        if not same_runtime or str(delivery["state"]) == "deferred":
            delivery = await conn.fetchrow(
                """
                UPDATE thread_input_deliveries
                   SET state = 'owned',
                       claim_generation = claim_generation + 1,
                       owner_agent_id = $2,
                       owner_pod_uid = $3,
                       owner_runtime_generation = $4,
                       owned_at = statement_timestamp(),
                       queued_at = NULL,
                       deferred_reason = NULL,
                       deferred_at = NULL,
                       updated_at = statement_timestamp()
                 WHERE delivery_id = $1
                   AND state IN ('persisted', 'owned', 'queued', 'deferred')
                RETURNING *
                """,
                delivery_uuid,
                UUID(str(agent_id)),
                str(pod_uid),
                UUID(str(runtime_generation)),
            )
            if delivery is None:
                raise InputDeliveryAuthorityLost("delivery claim was lost")

    result = _dict(delivery)
    result.update(
        {
            "message_id": str(row_id),
            "message_row_id": str(row_id),
            "seq": int(message["seq"]),
            "transcript_inserted": transcript_inserted,
            "content": stored_content,
            "role": str(role),
        }
    )
    return result


async def claim_pending_input_deliveries(
    conn: Any,
    *,
    thread_id: str | UUID,
    agent_id: str | UUID,
    pod_uid: str,
    runtime_generation: str | UUID,
) -> list[dict[str, Any]]:
    """Claim every persisted/unadmitted input for one attached runtime."""

    thread_uuid = UUID(str(thread_id))
    agent_uuid = UUID(str(agent_id))
    runtime_uuid = UUID(str(runtime_generation))
    await lock_runtime_authority(
        conn, thread_id=thread_uuid, agent_id=agent_uuid, pod_uid=pod_uid
    )
    rows = await conn.fetch(
        """
        SELECT delivery.*, message.seq, message.role, message.content
          FROM thread_input_deliveries AS delivery
          JOIN thread_messages AS message ON message.id = delivery.message_id
         WHERE delivery.thread_id = $1
           AND delivery.state IN ('persisted', 'owned', 'queued', 'deferred')
         ORDER BY message.seq, delivery.delivery_id
         FOR UPDATE OF delivery
        """,
        thread_uuid,
    )
    result: list[dict[str, Any]] = []
    for raw in rows:
        row = _dict(raw)
        same_runtime = (
            str(row.get("owner_agent_id") or "") == str(agent_uuid)
            and str(row.get("owner_pod_uid") or "") == str(pod_uid)
            and str(row.get("owner_runtime_generation") or "") == str(runtime_uuid)
        )
        if not same_runtime or str(row.get("state")) == "deferred":
            updated = await conn.fetchrow(
                """
                UPDATE thread_input_deliveries
                   SET state = 'owned', claim_generation = claim_generation + 1,
                       owner_agent_id = $2, owner_pod_uid = $3,
                       owner_runtime_generation = $4,
                       owned_at = statement_timestamp(), queued_at = NULL,
                       deferred_reason = NULL, deferred_at = NULL,
                       updated_at = statement_timestamp()
                 WHERE delivery_id = $1
                   AND state IN ('persisted', 'owned', 'queued', 'deferred')
                RETURNING *
                """,
                row["delivery_id"],
                agent_uuid,
                str(pod_uid),
                runtime_uuid,
            )
            if updated is None:
                continue
            preserved = {
                "seq": row["seq"],
                "role": row["role"],
                "content": row["content"],
            }
            row = {**_dict(updated), **preserved}
        row["message_id"] = str(row["message_id"])
        result.append(row)
    return result


async def mark_input_delivery_queued(
    conn: Any,
    *,
    delivery_id: str | UUID,
    agent_id: str | UUID,
    pod_uid: str,
    runtime_generation: str | UUID,
    claim_generation: int,
) -> bool:
    row = await conn.fetchrow(
        """
        UPDATE thread_input_deliveries delivery
           SET state = 'queued', queued_at = COALESCE(queued_at, statement_timestamp()),
               updated_at = statement_timestamp()
          FROM threads thread, agents agent
         WHERE delivery.delivery_id = $1
           AND delivery.state IN ('owned', 'queued')
           AND delivery.claim_generation = $2
           AND delivery.owner_agent_id = $3
           AND delivery.owner_pod_uid = $4
           AND delivery.owner_runtime_generation = $5
           AND thread.id = delivery.thread_id
           AND thread.agent_id = delivery.owner_agent_id
           AND agent.id = delivery.owner_agent_id
           AND agent.thread_id = thread.id
           AND agent.pod_uid = delivery.owner_pod_uid
           AND agent.status NOT IN ('offline', 'deleted')
        RETURNING delivery.delivery_id
        """,
        UUID(str(delivery_id)),
        int(claim_generation),
        UUID(str(agent_id)),
        str(pod_uid),
        UUID(str(runtime_generation)),
    )
    return row is not None


async def transition_input_delivery(
    conn: Any,
    *,
    delivery_id: str | UUID,
    agent_id: str | UUID,
    pod_uid: str,
    runtime_generation: str | UUID,
    claim_generation: int,
    transition: str,
    turn_number: int | None = None,
    reason: str | None = None,
) -> bool:
    """CAS one exact owner through admitted, settled, or deferred."""

    if transition == "admitted":
        states = ("owned", "queued")
        assignments = (
            "state = 'admitted', admitted_at = statement_timestamp(), "
            "admitted_turn_number = $6, deferred_reason = NULL, "
            "deferred_at = NULL, updated_at = statement_timestamp()"
        )
    elif transition == "settled":
        states = ("admitted",)
        assignments = (
            "state = 'settled', settled_at = statement_timestamp(), "
            "updated_at = statement_timestamp()"
        )
    elif transition == "deferred":
        states = ("owned", "queued")
        assignments = (
            "state = 'deferred', deferred_reason = LEFT(COALESCE($7, 'retryable'), 120), "
            "deferred_at = statement_timestamp(), queued_at = NULL, "
            "updated_at = statement_timestamp()"
        )
    elif transition == "unadmit":
        states = ("admitted",)
        assignments = (
            "state = 'deferred', admitted_at = NULL, admitted_turn_number = NULL, "
            "deferred_reason = LEFT(COALESCE($7, 'provider_not_started'), 120), "
            "deferred_at = statement_timestamp(), queued_at = NULL, "
            "updated_at = statement_timestamp()"
        )
    else:  # pragma: no cover - caller contract
        raise ValueError(f"unsupported input delivery transition: {transition}")

    row = await conn.fetchrow(
        f"""
        UPDATE thread_input_deliveries delivery
           SET {assignments}
          FROM threads thread, agents agent
         WHERE delivery.delivery_id = $1
           AND delivery.state = ANY($2::text[])
           AND delivery.claim_generation = $3
           AND delivery.owner_agent_id = $4
           AND delivery.owner_pod_uid = $5
           AND delivery.owner_runtime_generation = $8
           AND thread.id = delivery.thread_id
           AND thread.agent_id = delivery.owner_agent_id
           AND agent.id = delivery.owner_agent_id
           AND agent.thread_id = thread.id
           AND agent.pod_uid = delivery.owner_pod_uid
           AND agent.status NOT IN ('offline', 'deleted')
           AND ($6::bigint IS NULL OR TRUE)
           AND ($7::text IS NULL OR TRUE)
        RETURNING delivery.delivery_id
        """,
        UUID(str(delivery_id)),
        list(states),
        int(claim_generation),
        UUID(str(agent_id)),
        str(pod_uid),
        turn_number,
        reason,
        UUID(str(runtime_generation)),
    )
    return row is not None


async def get_input_delivery(
    conn: Any, delivery_id: str | UUID
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT * FROM thread_input_deliveries WHERE delivery_id = $1",
        UUID(str(delivery_id)),
    )
    return _dict(row) if row is not None else None
