"""Completion-domain timeout and backoff recovery sweep bodies.

Application lifespan code owns leader election, task creation and shutdown.
This module owns only the callable sweep operations and loops over an injected
shutdown event, persistence authority and completion callbacks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any, Protocol

from orchestrator.services.completion import _parse_context, evaluate_llm_outage
from shared.runtime.core.loader import canonical_config_name

logger = logging.getLogger(__name__)


class CompletionRecoveryStore(Protocol):
    def acquire(self) -> Any: ...

    async def get_delegation_children(
        self, parent_job_id: str
    ) -> list[dict[str, Any]]: ...

    async def cancel_stateless_job(
        self, job_id: str, **guards: Any
    ) -> tuple[bool, bool]: ...

    async def cancel_job(self, job_id: str) -> bool: ...

    async def queue_stateless_job_for_resume(
        self, job_id: str, context: dict[str, Any], **options: Any
    ) -> bool: ...

    async def merge_job_context(self, job_id: str, updates: dict[str, Any]) -> Any: ...

    async def claim_delegation_resume(self, parent_job_id: str) -> bool: ...

    async def list_due_llm_outage_jobs(
        self, *, limit: int, completion_commands_enabled: bool
    ) -> list[dict[str, Any]]: ...

    async def fail_llm_outage_job(
        self, job_id: str, reason: str, *, completion_commands_enabled: bool
    ) -> bool: ...

    async def claim_llm_outage_redispatch(
        self, job_id: str, *, completion_commands_enabled: bool
    ) -> bool: ...

    async def list_due_backoff_jobs(
        self,
        freeze_type: str,
        *,
        limit: int,
        completion_commands_enabled: bool,
    ) -> list[dict[str, Any]]: ...

    async def claim_backoff_redispatch(
        self,
        job_id: str,
        freeze_type: str,
        *,
        completion_commands_enabled: bool,
    ) -> bool: ...


@dataclass(frozen=True)
class CompletionRecoveryDependencies:
    store: CompletionRecoveryStore
    completion_commands_enabled: Callable[[], bool]
    trigger_dispatch: Callable[[], None]
    completion_resume_guard_kwargs: Callable[[], dict[str, Any]]
    completion_dispatch_guard_kwargs: Callable[[], dict[str, Any]]
    wait_for_stateless_cancel_settle: Callable[[str], Awaitable[bool]]
    notify_operator_freeze: Callable[..., Awaitable[Any]]
    handle_scholar_completion: Callable[[dict[str, Any], list[str]], Awaitable[None]]
    handle_delegation_child_completion: Callable[
        [dict[str, Any], list[str]], Awaitable[None]
    ]


def latest_delegation_outage_wake(
    children: list[dict[str, Any]],
) -> datetime | None:
    """Return the latest persisted outage wake across delegation children."""
    latest: datetime | None = None
    for child in children:
        ctx = child.get("context") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except (json.JSONDecodeError, ValueError):
                continue
        wake_raw = ((ctx or {}).get("llm_outage") or {}).get("next_retry_at")
        if not isinstance(wake_raw, str):
            continue
        try:
            wake = datetime.fromisoformat(wake_raw)
        except ValueError:
            continue
        if wake.tzinfo is None:
            wake = wake.replace(tzinfo=timezone.utc)
        if latest is None or wake > latest:
            latest = wake
    return latest


async def check_delegation_timeouts(
    *, dependencies: CompletionRecoveryDependencies
) -> int:
    """Cancel expired compatibility children and resume their parent."""
    store = dependencies.store
    handled = 0
    try:
        async with store.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, freeze_data, config_override, context,
                       execution_lane, priority, user_id
                FROM jobs
                WHERE status = 'waiting'
                  AND freeze_data IS NOT NULL
                """,
            )

        for row in rows:
            job_id = str(row["id"])
            freeze = row["freeze_data"]
            if isinstance(freeze, str):
                try:
                    freeze = json.loads(freeze)
                except (json.JSONDecodeError, ValueError):
                    continue
            if not freeze or freeze.get("freeze_type") != "delegation":
                continue

            timestamp_str = freeze.get("timestamp")
            timeout = freeze.get("timeout", 7200)
            if not timestamp_str:
                continue
            try:
                delegation_start = datetime.fromisoformat(timestamp_str)
                if delegation_start.tzinfo is None:
                    delegation_start = delegation_start.replace(tzinfo=timezone.utc)
                elapsed = (
                    datetime.now(timezone.utc) - delegation_start
                ).total_seconds()
            except (ValueError, TypeError):
                continue
            if elapsed < timeout:
                continue

            children = await store.get_delegation_children(job_id)
            wake = latest_delegation_outage_wake(children)
            if wake is not None and wake > delegation_start:
                elapsed = (datetime.now(timezone.utc) - wake).total_seconds()
                if elapsed < timeout:
                    logger.info(
                        "Delegation timeout for job %s suspended: child outage "
                        "wake at %s re-anchors the deadline (%.0fs of %ss consumed)",
                        job_id,
                        wake.isoformat(),
                        elapsed,
                        timeout,
                    )
                    continue

            logger.warning(
                "Delegation timeout for job %s: %.0fs elapsed > %ss limit",
                job_id,
                elapsed,
                timeout,
            )
            cancelled_count = 0
            children_settled = True
            for child in children:
                child_status = child.get("status", "")
                if child_status in ("completed", "failed"):
                    continue
                child_id = str(child["id"])
                try:
                    if child.get("execution_lane") == "stateless":
                        if child_status == "cancelled":
                            child_context = child.get("context") or {}
                            if isinstance(child_context, str):
                                try:
                                    child_context = json.loads(child_context)
                                except (TypeError, ValueError):
                                    child_context = {}
                            settled = (
                                child_context.get("_stateless_cancel_cleanup_pending")
                                is not True
                                or await dependencies.wait_for_stateless_cancel_settle(
                                    child_id
                                )
                            )
                        else:
                            cancelled, _queue_closed = await store.cancel_stateless_job(
                                child_id,
                                **dependencies.completion_dispatch_guard_kwargs(),
                            )
                            settled = bool(
                                cancelled
                                and await dependencies.wait_for_stateless_cancel_settle(
                                    child_id
                                )
                            )
                            if cancelled:
                                cancelled_count += 1
                        if not settled:
                            children_settled = False
                            logger.error(
                                "Delegation timeout cannot resume parent %s: "
                                "stateless child %s still owns its worker lease",
                                job_id,
                                child_id,
                            )
                            continue
                    elif child_status != "cancelled":
                        cancelled = await store.cancel_job(child_id)
                        if cancelled:
                            cancelled_count += 1
                except Exception as exc:
                    children_settled = False
                    logger.warning(
                        "Failed to cancel timed-out child %s: %s", child_id, exc
                    )

            if not children_settled:
                continue

            child_results = []
            refreshed_children = await store.get_delegation_children(job_id)
            for child in refreshed_children:
                child_id = str(child["id"])
                freeze_child = child.get("freeze_data")
                if isinstance(freeze_child, str):
                    try:
                        freeze_child = json.loads(freeze_child)
                    except (json.JSONDecodeError, ValueError):
                        freeze_child = {}
                freeze_child = freeze_child or {}
                child_ctx = child.get("context") or {}
                if isinstance(child_ctx, str):
                    try:
                        child_ctx = json.loads(child_ctx)
                    except (json.JSONDecodeError, ValueError):
                        child_ctx = {}
                child_results.append(
                    {
                        "job_id": child_id,
                        "description": child.get("description", ""),
                        "status": child.get("status", "unknown"),
                        "config_name": canonical_config_name(
                            child.get("config_name") or "worker_base"
                        ),
                        "output_path": (child_ctx or {}).get("graft_output_path"),
                        "creation_order": child.get("creation_order"),
                        "branch_name": child.get("branch_name"),
                        "summary": freeze_child.get("summary", ""),
                        "confidence": freeze_child.get("confidence", 0.0),
                        "timed_out": child.get("status") == "cancelled",
                    }
                )

            delegation_context = {
                "delegation_results": child_results,
                "delegation_timed_out": True,
            }
            if row.get("execution_lane") == "stateless":
                claimed = await store.queue_stateless_job_for_resume(
                    job_id,
                    delegation_context,
                    priority=int(row.get("priority") or 0),
                    fair_key=(str(row["user_id"]) if row.get("user_id") else None),
                    expected_status="waiting",
                    **dependencies.completion_resume_guard_kwargs(),
                )
            else:
                await store.merge_job_context(job_id, delegation_context)
                claimed = await store.claim_delegation_resume(job_id)
                if claimed:
                    dependencies.trigger_dispatch()
            if not claimed:
                logger.debug(
                    "Delegation timeout for %s already handled by another "
                    "sweeper; skipping resume",
                    job_id,
                )
                continue
            logger.info(
                "Delegation timeout handled for %s: cancelled %d children, "
                "parent re-queued",
                job_id,
                cancelled_count,
            )
            handled += 1
    except Exception as exc:
        logger.error("Error checking delegation timeouts: %s", exc, exc_info=True)
    return handled


async def delegation_timeout_sweeper(
    shutdown_event: asyncio.Event,
    *,
    dependencies: CompletionRecoveryDependencies,
    interval_seconds: float,
) -> None:
    """Run compatibility delegation timeout checks until shutdown."""
    logger.info("Delegation timeout sweeper started")
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
            break
        except asyncio.TimeoutError:
            pass
        try:
            handled = await check_delegation_timeouts(dependencies=dependencies)
            if handled:
                logger.info("Delegation timeout sweeper: handled %d timeouts", handled)
        except Exception as exc:
            logger.error("Delegation timeout sweeper error: %s", exc, exc_info=True)
    logger.info("Delegation timeout sweeper stopped")


async def llm_outage_sweep_once(
    *, dependencies: CompletionRecoveryDependencies
) -> tuple[int, int]:
    """Re-dispatch due outage jobs and fail jobs beyond the outage ceiling."""
    store = dependencies.store
    commands_enabled = dependencies.completion_commands_enabled()
    due = await store.list_due_llm_outage_jobs(
        limit=50,
        completion_commands_enabled=commands_enabled,
    )
    if not due:
        return (0, 0)

    now = datetime.now(timezone.utc)
    redispatched = 0
    failed = 0
    for job in due:
        job_id = str(job["id"])
        evaluation = evaluate_llm_outage(_parse_context(job), now)
        if evaluation["over_ceiling"]:
            reason = (
                "LLM endpoint unavailable past the give-up ceiling "
                f"({evaluation['ceiling_reason']}, {evaluation['attempt']} attempts) "
                "— failed by the outage sweeper. Check the model "
                "endpoint/provider (Admin → Models)."
            )
            if await store.fail_llm_outage_job(
                job_id,
                reason,
                completion_commands_enabled=commands_enabled,
            ):
                failed += 1
                logger.error("LLM-outage sweeper: job %s FAILED — %s", job_id, reason)
                freeze_data = job.get("freeze_data")
                if isinstance(freeze_data, str):
                    try:
                        freeze_data = json.loads(freeze_data)
                    except (ValueError, TypeError):
                        freeze_data = {}
                try:
                    await dependencies.notify_operator_freeze(
                        job,
                        job_id,
                        "llm_unavailable",
                        freeze_data or {},
                        dedup_key=f"llm_unavailable:sweeper:{job_id}",
                    )
                except Exception as exc:
                    logger.warning("give-up alert failed for %s: %s", job_id, exc)
                if job.get("parent_job_id") is not None:
                    failed_job = {**job, "status": "failed"}
                    unblock_actions: list[str] = []
                    try:
                        await dependencies.handle_scholar_completion(
                            failed_job, unblock_actions
                        )
                        await dependencies.handle_delegation_child_completion(
                            failed_job, unblock_actions
                        )
                    except Exception as exc:
                        logger.warning(
                            "sweep-fail parent unblock for %s failed: %s", job_id, exc
                        )
            continue
        if await store.claim_llm_outage_redispatch(
            job_id,
            completion_commands_enabled=commands_enabled,
        ):
            redispatched += 1

    if redispatched:
        dependencies.trigger_dispatch()
    if redispatched or failed:
        logger.info(
            "LLM-outage sweeper: re-dispatched %d, failed %d (of %d due)",
            redispatched,
            failed,
            len(due),
        )
    return (redispatched, failed)


async def llm_outage_redispatch_sweeper(
    shutdown_event: asyncio.Event,
    *,
    dependencies: CompletionRecoveryDependencies,
    interval_seconds: float,
) -> None:
    """Run LLM outage recovery ticks until application shutdown."""
    logger.info("LLM-outage re-dispatch sweeper started (tick=%.0fs)", interval_seconds)
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
            break
        except asyncio.TimeoutError:
            pass
        try:
            await llm_outage_sweep_once(dependencies=dependencies)
        except Exception as exc:
            logger.error("LLM-outage re-dispatch sweeper error: %s", exc, exc_info=True)
    logger.info("LLM-outage re-dispatch sweeper stopped")


async def infra_transient_sweep_once(
    *, dependencies: CompletionRecoveryDependencies
) -> tuple[int, int]:
    """Release jobs whose transient-infrastructure backoff is due."""
    store = dependencies.store
    commands_enabled = dependencies.completion_commands_enabled()
    due = await store.list_due_backoff_jobs(
        "infra_transient",
        limit=50,
        completion_commands_enabled=commands_enabled,
    )
    redispatched = 0
    for row in due:
        job_id = str(row["id"])
        try:
            if await store.claim_backoff_redispatch(
                job_id,
                "infra_transient",
                completion_commands_enabled=commands_enabled,
            ):
                redispatched += 1
                logger.info(
                    "Job %s: transient-infra backoff elapsed — released for "
                    "re-dispatch (workspace was kept, agent will reattach)",
                    job_id,
                )
        except Exception as exc:
            logger.error(
                "infra_transient sweeper: failed to release job %s: %s",
                job_id,
                exc,
            )
    if redispatched:
        dependencies.trigger_dispatch()
    return len(due), redispatched


async def infra_transient_redispatch_sweeper(
    shutdown_event: asyncio.Event,
    *,
    dependencies: CompletionRecoveryDependencies,
    interval_seconds: float,
) -> None:
    """Run transient-infrastructure recovery ticks until shutdown."""
    logger.info(
        "Transient-infra re-dispatch sweeper started (tick=%.0fs)", interval_seconds
    )
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
            break
        except asyncio.TimeoutError:
            pass
        try:
            await infra_transient_sweep_once(dependencies=dependencies)
        except Exception as exc:
            logger.error(
                "Transient-infra re-dispatch sweeper error: %s", exc, exc_info=True
            )
    logger.info("Transient-infra re-dispatch sweeper stopped")
