"""Operator verbs over the stateless run queue and completion commands.

R1.B06, root lane. Four admin operations that were inline in
``orchestrator.main``. Two properties travelled with them and are the reason
this is a service rather than four router bodies:

* **Unpark is the only path out of ``parked``.** Neither enqueue nor input
  recording revives a parked unit (stateless_turn_resilience §5.1), so this is
  the single writer of that edge — and it refuses while the claimant has not
  quiesced. A stopped stateless unit answers ``409 awaiting claimant
  quiescence``, and an unreadable metadata blob is treated as *stopped*, not as
  permission: ``stateless_stop_markers`` raising is a refusal, never a pass.
  The thread row is read ``FOR UPDATE`` in the same transaction that unparks.
* **The completion-command verbs are disabled-by-default.** With the feature
  off they answer ``404``, not ``403`` — the endpoint does not exist rather
  than existing and refusing, so nothing enumerates a disabled surface.

The flag arrives as a callable (port contract P1). Reading it at import time
would freeze whatever value the application happened to hold when this module
was first imported, which is precisely the bug that rule exists to prevent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import UUID

from fastapi import HTTPException

from shared.session_retirement import stateless_stop_markers

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunQueueAdminDependencies:
    """Collaborators resolved per invocation from the handling application."""

    db: Any
    #: The admin guard; returns the acting administrator's row.
    require_admin: Callable[..., Awaitable[Any]]
    #: B11 feature gate, as a callable — never a captured value.
    completion_commands_enabled: Callable[[], bool]
    #: B08 owns the resolution authority; B06 only calls it.
    get_completion_command_resolution: Callable[[], Any]


def completion_operator_result(result: Any) -> dict[str, Any]:
    """Serialize the bounded dataclass returned by the resolution service."""

    from dataclasses import fields, is_dataclass

    if not is_dataclass(result):
        raise RuntimeError("completion operator service returned an invalid result")
    return {field.name: getattr(result, field.name) for field in fields(result)}


async def read_run_queue_model(
    *, dependencies: RunQueueAdminDependencies
) -> dict[str, Any]:
    """Operator read model for the stateless run_queue.

    ``src/shared/run_queue.list_active`` passthrough: current leases (with
    ``lease_remaining_seconds`` — negative means expired, awaiting the reaper)
    and parked units (the unpark worklist). Diagnostics only; never an input to
    correctness decisions.
    """
    from shared.run_queue import list_active

    async with dependencies.db.acquire() as conn:
        return await list_active(conn)


async def unpark_run_queue_unit(
    unit_id: str, *, dependencies: RunQueueAdminDependencies
) -> dict[str, Any]:
    """parked → queued, attempts reset, runnable now.

    404 when the unit is not currently parked; 409 while a stateless claimant
    has not quiesced.
    """
    from shared.run_queue import unpark_unit

    try:
        UUID(str(unit_id))
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Unit is not parked") from None
    async with dependencies.db.acquire() as conn:
        async with conn.transaction():
            authority = await conn.fetchrow(
                "SELECT execution_lane, metadata FROM threads "
                "WHERE id = $1::uuid FOR UPDATE",
                unit_id,
            )
            if (
                authority is not None
                and str(authority["execution_lane"] or "") == "stateless"
            ):
                metadata = authority["metadata"]
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (TypeError, ValueError):
                        metadata = None
                try:
                    stopped = bool(stateless_stop_markers(metadata))
                except RuntimeError:
                    stopped = True
                if stopped:
                    raise HTTPException(
                        status_code=409,
                        detail="Unit is awaiting claimant quiescence",
                    )
            ok = await unpark_unit(conn, unit_id=unit_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Unit is not parked")
    logger.info("run_queue unpark: unit=%s", unit_id)
    return {"unit_id": unit_id, "state": "queued"}


async def unpark_completion_command(
    command_id: str, *, admin: Any, dependencies: RunQueueAdminDependencies
) -> dict[str, Any]:
    """Rearm one exact parked completion command and its pending effects."""

    if not dependencies.completion_commands_enabled():
        raise HTTPException(status_code=404, detail="Completion commands are disabled")
    from orchestrator.services.completion_command_resolution import (
        CompletionResolutionConflict,
        CompletionResolutionNotFound,
    )

    try:
        command_uuid = UUID(str(command_id))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=404, detail="Completion command not found"
        ) from None
    try:
        result = await dependencies.get_completion_command_resolution().unpark(
            command_uuid,
            actor=str(admin["id"]),
        )
    except CompletionResolutionNotFound as exc:
        raise HTTPException(
            status_code=404, detail="Completion command not found"
        ) from exc
    except CompletionResolutionConflict as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    return completion_operator_result(result)


async def force_resolve_completion_command(
    command_id: str,
    *,
    expected_state: str,
    terminal_status: str,
    reason: str,
    admin: Any,
    dependencies: RunQueueAdminDependencies,
) -> dict[str, Any]:
    """Abandon a quiescent tail and write an operator-selected terminal state."""

    if not dependencies.completion_commands_enabled():
        raise HTTPException(status_code=404, detail="Completion commands are disabled")
    from orchestrator.services.completion_command_resolution import (
        CompletionResolutionConflict,
        CompletionResolutionNotFound,
    )

    try:
        command_uuid = UUID(str(command_id))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=404, detail="Completion command not found"
        ) from None
    try:
        result = await dependencies.get_completion_command_resolution().force_resolve(
            command_uuid,
            expected_state=expected_state,
            terminal_status=terminal_status,
            actor=str(admin["id"]),
            reason=reason,
        )
    except CompletionResolutionNotFound as exc:
        raise HTTPException(
            status_code=404, detail="Completion command not found"
        ) from exc
    except CompletionResolutionConflict as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc

    # The durable jobs/command/effect transaction is authoritative. Checkpoint
    # pruning is the same non-fatal hygiene used by ordinary terminal writes.
    try:
        await dependencies.db.delete_checkpoint_thread(result.job_id)
    except Exception:
        logger.warning(
            "completion force-resolve checkpoint prune failed for job %s",
            result.job_id,
            exc_info=True,
        )
    return completion_operator_result(result)
