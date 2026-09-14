"""The agent's guidance-delivery ack (P1-A).

Extracted verbatim from ``orchestrator.main`` (R1.B07 lane M, census group
``J_messaging``). One operation behind one internal route. Entries move
atomically out of ``context.pending_guidance`` / ``context.queued_replies``
into ``context.consumed_replies``; a stateless worker's ``checkpoint_id`` is
verified before anything moves. At-least-once by design and idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import HTTPException, Request

from orchestrator.schemas.messaging import GuidanceAckRequest


@dataclass
class JobGuidanceDependencies:
    """The job store and the internal-key gate, resolved per invocation."""

    store: Any
    require_internal: Callable[[Request], Awaitable[None]]


async def ack_job_guidance(
    request: Request,
    job_id: str,
    body: GuidanceAckRequest,
    *,
    dependencies: JobGuidanceDependencies,
) -> dict[str, Any]:
    """Ack delivered supervisor guidance / drained queued replies.
    **Internal** (P4b) — requires ``X-Internal-Key``.

    The pinned agent calls this after entries reached LLM-visible context. A
    stateless worker calls it only after the checkpoint that absorbed those
    entries committed; ``checkpoint_id`` is verified before anything moves.
    Entries move atomically ``context.pending_guidance`` (by id) /
    ``context.queued_replies`` (by exact key for stateless workers, legacy
    thread id for pinned workers) → ``context.consumed_replies``
    with a ``consumed_at`` stamp — which stops heartbeat redelivery /
    boundary re-materialization, and lets the sender confirm delivery by
    reading job context. At-least-once by design: a lost ack just means
    the worker sees the same guidance for one more turn. Idempotent.
    """
    try:
        moved = await dependencies.store.consume_job_guidance(
            job_id,
            guidance_ids=body.guidance_ids,
            reply_threads=body.reply_threads,
            reply_keys=body.reply_keys,
            feedback_keys=body.feedback_keys,
            delegation_keys=body.delegation_keys,
            checkpoint_id=body.checkpoint_id,
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    if moved is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {"status": "ok", "consumed": moved}


__all__ = ["JobGuidanceDependencies", "ack_job_guidance"]
