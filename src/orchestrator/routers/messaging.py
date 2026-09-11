"""``/api/jobs/{job_id}/messages`` and ``/api/jobs/{job_id}/guidance`` — the
worker↔human message surface.

Extracted from ``orchestrator.main`` (R1.B07 lane M). Nine route declarations,
moved with their handler names, paths, methods, parameter order and docstrings
intact — the docstring is the published OpenAPI description, so it is part of
the route's identity and not editorial text.

None of the nine carried ``tags``, ``response_model``, ``status_code`` or a
``dependencies`` list, and none acquires one here. Auth is performed inside the
handler exactly as before, which is why each gate arrives through the
dependency object rather than through ``Depends`` — and why it is called in the
declaration body: ``scripts/check_endpoint_auth.py`` reads the audited gate
from the route it is declared on and does not follow a call into a service
module.

The Pydantic body of ``send_agent_message`` keeps its historical name
``request`` and the FastAPI handle its name ``req``, because those names are
part of the moved handler.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from orchestrator.schemas.messaging import (
    GuidanceAckRequest,
    MessageReplyRequest,
    MessageSendRequest,
    OfficerMessageAckRequest,
    OfficerMessageEscalateRequest,
    OfficerMessageReplyRequest,
)
from orchestrator.security.access import require_job_access
from orchestrator.services import agent_messaging, job_guidance, message_thread_reads
from orchestrator.services import officer_message_actions as officer_actions
from orchestrator.services.inbound_reply import (
    InboundReplyDependencies,
    route_inbound_reply,
)

logger = logging.getLogger(__name__)

# No `tags=` and no prefix: the nine declarations this replaces carried
# neither, and either would change the published OpenAPI operation for routes
# whose identity this batch is required to leave untouched.
router = APIRouter()


def get_agent_messaging_dependencies(
    request: Request,
) -> agent_messaging.AgentMessagingDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.agent_messaging_dependencies_factory()


def get_inbound_reply_dependencies(request: Request) -> InboundReplyDependencies:
    return request.app.state.inbound_reply_dependencies_factory()


def get_officer_message_action_dependencies(
    request: Request,
) -> officer_actions.OfficerMessageActionDependencies:
    return request.app.state.officer_message_action_dependencies_factory()


def get_job_guidance_dependencies(
    request: Request,
) -> job_guidance.JobGuidanceDependencies:
    return request.app.state.job_guidance_dependencies_factory()


def get_message_thread_read_dependencies(
    request: Request,
) -> message_thread_reads.MessageThreadReadDependencies:
    return request.app.state.message_thread_read_dependencies_factory()


@router.post("/api/jobs/{job_id}/messages/send")
async def send_agent_message(
    req: Request,
    job_id: str,
    request: MessageSendRequest,
) -> dict[str, Any]:
    """Send a message from an agent to a human. **Internal** (P4b) —
    requires ``X-Internal-Key``. Ingress strips this path.

    The Pydantic body keeps its historical name ``request`` to avoid
    churning the body of this long handler; the FastAPI Request handle
    is named ``req`` for the gate call only.

    Resolves recipient from job ownership, checks rate limits, sends
    email, and logs to message_log.
    """
    dependencies = get_agent_messaging_dependencies(req)
    await dependencies.require_internal(req)
    return await agent_messaging.send_agent_message(
        req, job_id, request, dependencies=dependencies
    )


@router.post("/api/jobs/{job_id}/messages/{thread_id}/reply")
async def reply_to_agent_message(
    request: Request,
    job_id: str,
    thread_id: str,
    body: MessageReplyRequest,
) -> dict[str, Any]:
    """Reply to an agent's message (cockpit UI or IMAP).

    If the job is in 'waiting_for_reply' status and the thread matches,
    resumes the job with the reply as feedback. ``urgent`` (and the
    immediate-interrupt user preference) delivers into the worker's next
    LLM turn via the non-destructive guidance lane
    (``delivery_strategy="guidance_next_turn"``); only a job with no live
    run is resumed to deliver an urgent message. Otherwise the reply is
    queued and injected at the next tactical→strategic phase boundary.
    """
    dependencies = get_inbound_reply_dependencies(request)
    await require_job_access(request, dependencies.store, job_id)
    try:
        delivery_strategy, sequence = await route_inbound_reply(
            job_id=job_id,
            thread_id=thread_id,
            message=body.message,
            urgent=body.urgent,
            dependencies=dependencies,
        )

        file_path = f"messages/{thread_id}/{sequence:03d}_received.md"

        return {
            "status": "delivered",
            "sequence": sequence,
            "file_path": file_path,
            "delivery_strategy": delivery_strategy,
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"Failed to deliver reply for job {job_id} thread {thread_id}: {e}"
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/jobs/{job_id}/messages/{thread_id}/officer-reply")
async def officer_reply_to_worker_message(
    request: Request,
    job_id: str,
    thread_id: str,
    body: OfficerMessageReplyRequest,
) -> dict[str, Any]:
    """Answer a worker message as the commissioned officer. **Internal** —
    requires ``X-Internal-Key``; the caller must BE the project's officer.

    Delivers through the existing reply lane [A-reply] — a blocking route's
    worker resumes exactly once (job-status CAS) — and CAS-records
    ``resolved_by_officer`` on the route. The reply is guidance, never
    authorization: no approval/ready/claim side effects, and the original
    message is never erased.
    """
    dependencies = get_officer_message_action_dependencies(request)
    await dependencies.require_internal(request)
    return await officer_actions.officer_reply_to_worker_message(
        request, job_id, thread_id, body, dependencies=dependencies
    )


@router.post("/api/jobs/{job_id}/messages/{thread_id}/officer-escalate")
async def officer_escalate_worker_message(
    request: Request,
    job_id: str,
    thread_id: str,
    body: OfficerMessageEscalateRequest,
) -> dict[str, Any]:
    """Escalate a worker message thread to the user with officer context.
    **Internal** — requires ``X-Internal-Key``; caller must BE the officer.

    Same ``thread_id`` keeps the reply/resume path: the user's answer
    resumes the worker directly. The original worker text and the officer's
    context are delivered clearly delimited (§7).
    """
    dependencies = get_officer_message_action_dependencies(request)
    await dependencies.require_internal(request)
    return await officer_actions.officer_escalate_worker_message(
        request, job_id, thread_id, body, dependencies=dependencies
    )


@router.post("/api/jobs/{job_id}/messages/{thread_id}/officer-ack")
async def officer_acknowledge_worker_message(
    request: Request,
    job_id: str,
    thread_id: str,
    body: OfficerMessageAckRequest,
) -> dict[str, Any]:
    """Close an ASYNC worker message route without a reply. **Internal** —
    requires ``X-Internal-Key``; caller must BE the officer.

    Refused for blocking routes: a frozen worker needs an answer or an
    escalation, never a silent ack pretending nobody waited.
    """
    dependencies = get_officer_message_action_dependencies(request)
    await dependencies.require_internal(request)
    return await officer_actions.officer_acknowledge_worker_message(
        request, job_id, thread_id, body, dependencies=dependencies
    )


@router.post("/api/jobs/{job_id}/guidance/ack")
async def ack_job_guidance(
    request: Request, job_id: str, body: GuidanceAckRequest
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
    dependencies = get_job_guidance_dependencies(request)
    await dependencies.require_internal(request)
    return await job_guidance.ack_job_guidance(
        request, job_id, body, dependencies=dependencies
    )


@router.get("/api/jobs/{job_id}/messages")
async def list_message_threads(request: Request, job_id: str) -> dict[str, Any]:
    """List message threads for a job."""
    dependencies = get_message_thread_read_dependencies(request)
    _, job = await require_job_access(request, dependencies.store, job_id)
    return await message_thread_reads.list_message_threads(
        request, job_id, dependencies=dependencies, job=job
    )


@router.get("/api/jobs/{job_id}/messages/{thread_id}")
async def get_thread_detail(
    request: Request, job_id: str, thread_id: str
) -> dict[str, Any]:
    """Get full ordered messages within a thread."""
    dependencies = get_message_thread_read_dependencies(request)
    _, job = await require_job_access(request, dependencies.store, job_id)
    return await message_thread_reads.get_thread_detail(
        request, job_id, thread_id, dependencies=dependencies, job=job
    )


__all__ = [
    "ack_job_guidance",
    "get_agent_messaging_dependencies",
    "get_inbound_reply_dependencies",
    "get_job_guidance_dependencies",
    "get_message_thread_read_dependencies",
    "get_officer_message_action_dependencies",
    "get_thread_detail",
    "list_message_threads",
    "officer_acknowledge_worker_message",
    "officer_escalate_worker_message",
    "officer_reply_to_worker_message",
    "reply_to_agent_message",
    "router",
    "send_agent_message",
]
