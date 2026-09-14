"""Advancing a loop: the campaign step, the rotation, and the atomic handoff.

Extracted verbatim from ``orchestrator.main`` (R1.B07 lane L, census group
``J_project_loops``; loop_unified_engine.md, loop_campaign_scheduling.md). The
upper half of the loop engine — every decision about what runs next. It uses
:mod:`orchestrator.services.project_loop_spawn` for creating and recording
members and shares that module's :class:`ProjectLoopDependencies`.

The invariants that travel with it, none of them re-derived:

* **The barrier is the exactly-once gate.** ``claim_project_loop_stage_barrier``
  drains the turn and returns True to exactly ONE caller — the member that goes
  terminal last. Only that caller aggregates the turn, checks the stop axes and
  rotates; every earlier finisher records its own outcome and backs off.
* **Membership is the idempotency guard**: a stale or re-delivered completion
  hook for a job outside ``current_stage_jobs`` is a no-op.
* **Persist before spawn.** A campaign is written in its own transaction before
  its first member is raised; a tear heals through the ``plan_job_id`` re-run.
  The reverse order would strand a member with no campaign to join.
* **A turn whose every member failed re-runs its own stage** rather than handing
  the next role nothing to work from, and a retried stage is NOT a cycle wrap —
  ageing KB notes on a cycle that never completed would re-verify them early.
* **The handoff replays from committed successor ids only**, under a durable
  claim with a heartbeat; ``authority_check`` is refreshed before every further
  consequence, so a lost lease stops the tail instead of duplicating effects.
* **The KB TTL decrement commits its ledger row and its update in one vector
  transaction**, and a replay whose identity does not match fails closed.
* **A command-owned descriptor routes to its exact finalizer first**; only a
  genuinely command-less (or already terminal legacy) descriptor is executed by
  the reconciler, so no parallel copy of the tail ever runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

from orchestrator.services.project_loop_spawn import (
    WB_UNSET,
    ProjectLoopDependencies,
    loop_deadline_passed,
    loop_stop_reason,
    notify_loop_event,
    notify_loop_user_questions,
    prepare_atomic_loop_spawn_blocks,
    record_loop_job_outcome,
    spawn_loop_stage,
    writeback_loop_stage,
)
from orchestrator.services.session_wake import notify_officer

logger = logging.getLogger(__name__)


async def spawn_campaign_member(
    loop: dict[str, Any],
    *,
    campaign: dict[str, Any],
    stage_index: int,
    execution_slot: int,
    base_total: int,
    next_remaining: int | None,
    consecutive: int,
    last_error: str | None,
    actions: list[str],
    park_until: datetime | None = None,
    dependencies: ProjectLoopDependencies,
) -> bool:
    """Spawn ONE campaign stage and point the loop at it (planner mode).

    The member is stamped with ``loop_campaign_id`` / ``loop_campaign_index``
    (spawn-time truth: the advance derives "next stage" from the completed
    member's stamp, so a lost write-back heals through the existing re-point
    path without double-spawning). The passed ``campaign`` already carries the
    post-spawn cursor and rides the same row update as the stage pointer.
    On a spawn failure the loop is marked failed — same policy as the
    rotation path (a running loop with nothing in flight would never advance).
    Always returns True: the completed advance is fully handled either way.
    """
    loop_id = str(loop["id"])
    stages = campaign.get("stages") or []
    entry = stages[stage_index]
    role = str(entry.get("role") if isinstance(entry, dict) else entry)
    label = campaign.get("title") or campaign.get("initiative_note_id") or "?"

    loop_for_spawn = dict(loop)
    loop_for_spawn["remaining_iterations"] = next_remaining
    # The member's kickoff (campaign context block) must describe the campaign
    # being spawned for — not the loop row's possibly-stale pre-advance state.
    loop_for_spawn["campaign"] = campaign
    try:
        jobs, new_total = await spawn_loop_stage(
            loop_for_spawn,
            stage=role,
            seq_index=execution_slot,
            base_total=base_total,
            remaining=next_remaining,
            extra_context={
                "loop_campaign_id": str(campaign["id"]),
                "loop_campaign_index": int(stage_index),
            },
            park_until=park_until,
            dependencies=dependencies,
        )
    except Exception as e:
        logger.exception(
            "project loop %s: failed to spawn campaign stage %d", loop_id, stage_index
        )
        await dependencies.store.update_project_loop(
            loop_id,
            status="failed",
            remaining_iterations=next_remaining,
            consecutive_failures=consecutive,
            last_error=f"campaign spawn failed: {e}",
            stop_reason="failures",
            current_job_id=None,
            current_stage_jobs=[],
            campaign=campaign,
        )
        actions.append(f"project loop {loop_id[:8]} stopped (campaign spawn failed)")
        return True

    await writeback_loop_stage(
        loop_id,
        jobs=jobs,
        seq_index=execution_slot,
        remaining=next_remaining,
        total=new_total,
        consecutive=consecutive,
        last_error=last_error,
        campaign=campaign,
        dependencies=dependencies,
    )
    actions.append(
        f"project loop {loop_id[:8]} → campaign '{label}' stage "
        f"{stage_index + 1}/{len(stages)} ({role} job {str(jobs[0]['id'])[:8]})"
    )
    return True


async def advance_planner_campaign(
    loop: dict[str, Any],
    *,
    completed_job: dict[str, Any],
    completed_ctx: dict[str, Any],
    completed_failed: bool,
    base_total: int,
    next_remaining: int | None,
    consecutive: int,
    last_error: str | None,
    actions: list[str],
    park_until: datetime | None = None,
    dependencies: ProjectLoopDependencies,
) -> tuple[bool, Any]:
    """Planner-mode campaign step for a completed single-role loop job.

    Returns ``(handled, campaign_update)``:

      * ``(True, …)`` — a campaign stage was spawned (or the loop was marked
        failed on a spawn error); the caller must NOT rotate.
      * ``(False, campaign_update)`` — fall through to normal rotation;
        ``campaign_update`` (unless ``WB_UNSET``) is a campaign mutation
        (complete → ``review``, abort → ``aborted``) that must ride the
        rotation's write-back so status and pointer can't tear apart.

    Everything here is idempotent under the sweeper's re-point-and-re-advance
    recovery: member steps derive "next stage" from the completed member's
    spawn-time stamps, and plan application is guarded on
    ``campaign.plan_job_id`` (a healed re-run of the same critic job resumes
    at the persisted cursor instead of re-applying the plan).
    knowledge-base/knowledge/features/loop_campaign_scheduling.md (P0).
    """
    from orchestrator.services.project_loops import (
        LOOP_CAMPAIGN_HISTORY_LIMIT,
        planner_slots,
        resolve_campaign_caps,
        validate_loop_plan,
    )

    loop_id = str(loop["id"])
    try:
        critic_slot, execution_slot = planner_slots(loop.get("role_sequence") or [])
    except ValueError:
        # Malformed planner template (should be rejected at start) — degrade
        # to plain rotation rather than wedging the loop.
        logger.warning(
            "project loop %s: planner scheduling with invalid role_sequence — "
            "falling back to rotation",
            loop_id[:8],
        )
        return False, WB_UNSET

    campaign = loop.get("campaign") or None
    caps = resolve_campaign_caps(loop)

    # ---- A completed CAMPAIGN MEMBER: continue / finish / abort the queue.
    member_campaign_id = completed_ctx.get("loop_campaign_id")
    if member_campaign_id:
        if not campaign or str(campaign.get("id")) != str(member_campaign_id):
            return False, WB_UNSET  # member of an already-disposed campaign
        try:
            member_index = int(completed_ctx.get("loop_campaign_index"))
        except (TypeError, ValueError):
            return False, WB_UNSET
        stages = campaign.get("stages") or []
        stages_done = max(int(campaign.get("stages_done") or 0), member_index + 1)
        member_failures = (
            (int(campaign.get("member_failures") or 0) + 1) if completed_failed else 0
        )
        label = campaign.get("title") or campaign.get("initiative_note_id") or "?"

        if completed_failed and member_failures >= caps["abort_failures"]:
            actions.append(
                f"project loop {loop_id[:8]}: campaign '{label}' ABORTED after "
                f"{member_failures} consecutive member failures — returning to "
                "the critic checkpoint"
            )
            await notify_loop_event(
                loop,
                job_id=str(completed_job["id"]),
                event_type="loop_campaign_disposition",
                subject=f"Loop campaign aborted: {label}",
                message=(
                    f"Campaign '{label}' aborted after {member_failures} "
                    f"consecutive stage failures ({stages_done} of "
                    f"{len(stages)} stages done). The loop is returning to "
                    "the critic checkpoint for a disposition."
                ),
                dependencies=dependencies,
            )
            return False, {
                **campaign,
                "status": "aborted",
                "member_failures": member_failures,
                "stages_done": stages_done,
            }

        next_index = member_index + 1
        if next_index >= len(stages):
            actions.append(
                f"project loop {loop_id[:8]}: campaign '{label}' complete "
                f"({len(stages)} stages) — awaiting critic review"
            )
            return False, {
                **campaign,
                "status": "review",
                "member_failures": member_failures,
                "stages_done": len(stages),
                "cursor": len(stages),
            }

        handled = await spawn_campaign_member(
            loop,
            campaign={
                **campaign,
                "member_failures": member_failures,
                "stages_done": stages_done,
                "cursor": next_index + 1,
            },
            stage_index=next_index,
            execution_slot=execution_slot,
            base_total=base_total,
            next_remaining=next_remaining,
            consecutive=consecutive,
            last_error=last_error,
            actions=actions,
            park_until=park_until,
            dependencies=dependencies,
        )
        return handled, WB_UNSET

    # ---- The CHECKPOINT CRITIC completed: apply its filed plan (if any).
    stamped_seq = completed_ctx.get("loop_seq_index")
    is_checkpoint_critic = completed_ctx.get("loop_role") == "critic" and (
        stamped_seq is None or int(stamped_seq) == critic_slot
    )
    if not is_checkpoint_critic:
        return False, WB_UNSET

    plan = completed_ctx.get("loop_plan")
    if not isinstance(plan, dict):
        # No plan filed → implicit K=1 rotation fallback. Legal — but if a
        # campaign is awaiting disposition, the skip must be loud: silent
        # fallbacks are how a campaign parks in review forever while its
        # verdict lives only in a KB note the engine cannot read.
        if campaign and campaign.get("status") in ("review", "aborted"):
            skipped_label = (
                campaign.get("title") or campaign.get("initiative_note_id") or "?"
            )
            logger.warning(
                "project loop %s: checkpoint critic %s filed no plan while "
                "campaign '%s' awaits disposition — campaign stays parked",
                loop_id[:8],
                str(completed_job["id"])[:8],
                skipped_label,
            )
            actions.append(
                f"project loop {loop_id[:8]}: campaign '{skipped_label}' still "
                "awaits disposition — checkpoint critic filed no plan; "
                "dispose-only filing is allowed (disposition without stages)"
            )
            await notify_loop_event(
                loop,
                job_id=str(completed_job["id"]),
                event_type="loop_campaign_review_skipped",
                subject=f"Loop campaign review skipped: {skipped_label}",
                message=(
                    f"The checkpoint critic completed without disposing campaign "
                    f"'{skipped_label}' (status {campaign.get('status')}, "
                    f"{campaign.get('stages_done', '?')} of "
                    f"{len(campaign.get('stages') or [])} stages done). The "
                    "campaign stays parked until a critic files a disposition — "
                    "ship/kill may be filed without opening a new campaign."
                ),
                dependencies=dependencies,
            )
        return False, WB_UNSET

    # Idempotency (healed re-run of the same critic advance): the plan was
    # already applied — resume spawning at the persisted cursor instead.
    if campaign and str(campaign.get("plan_job_id")) == str(completed_job["id"]):
        cursor = int(campaign.get("cursor") or 0)
        stages = campaign.get("stages") or []
        if cursor >= len(stages):
            return False, WB_UNSET
        handled = await spawn_campaign_member(
            loop,
            campaign={**campaign, "cursor": cursor + 1},
            stage_index=cursor,
            execution_slot=execution_slot,
            base_total=base_total,
            next_remaining=next_remaining,
            consecutive=consecutive,
            last_error=last_error,
            actions=actions,
            park_until=park_until,
            dependencies=dependencies,
        )
        return handled, WB_UNSET

    # Re-validate at apply time — never trust stored input, and the budget may
    # have moved since intake. A rejected plan degrades to rotation (K=1).
    try:
        normalized = validate_loop_plan(plan, loop)
    except ValueError as e:
        logger.warning(
            "project loop %s: filed plan rejected at apply time: %s", loop_id[:8], e
        )
        actions.append(
            f"project loop {loop_id[:8]}: filed plan rejected at apply time "
            f"({e}) — falling back to rotation"
        )
        return False, WB_UNSET

    # Dispose the finished/aborted campaign (validated present when required).
    history = list(loop.get("campaign_history") or [])
    extensions_used = 0
    disposition = normalized.get("disposition")
    if campaign and disposition:
        outcome = disposition["outcome"]
        if outcome == "extend":
            extensions_used = int(campaign.get("extensions_used") or 0) + 1
        history.append(
            {
                "id": campaign.get("id"),
                "initiative_note_id": campaign.get("initiative_note_id"),
                "title": campaign.get("title"),
                "stages_total": len(campaign.get("stages") or []),
                "stages_done": campaign.get("stages_done"),
                "extensions_used": campaign.get("extensions_used"),
                "status_at_close": campaign.get("status"),
                "outcome": outcome,
                "notes": disposition.get("notes"),
                "disposed_by": str(completed_job["id"]),
            }
        )
        history = history[-LOOP_CAMPAIGN_HISTORY_LIMIT:]
        disposed_label = (
            campaign.get("title") or campaign.get("initiative_note_id") or "?"
        )
        actions.append(
            f"project loop {loop_id[:8]}: campaign '{disposed_label}' "
            f"disposed ({outcome})"
        )
        await notify_loop_event(
            loop,
            job_id=str(completed_job["id"]),
            event_type="loop_campaign_disposition",
            subject=f"Loop campaign {outcome}: {disposed_label}",
            message=(
                f"The critic disposed campaign '{disposed_label}' as "
                f"{outcome.upper()} ({campaign.get('stages_done', '?')} of "
                f"{len(campaign.get('stages') or [])} stages done"
                f"{', extending it' if outcome == 'extend' else ''})."
                + (
                    f" Notes: {disposition.get('notes')}"
                    if disposition.get("notes")
                    else ""
                )
            ),
            dependencies=dependencies,
        )

        # Mirror the verdict onto the ticket. ship → resolved, kill → archived;
        # extend leaves it active because the continuing campaign still owns it.
        ticket_status = {"ship": "resolved", "kill": "archived"}.get(outcome)
        ticket_id = campaign.get("initiative_note_id")
        if ticket_status and ticket_id and dependencies.vector_store is not None:
            from orchestrator.services.project_backlog import close_backlog_ticket

            closed = await close_backlog_ticket(
                dependencies.vector_store,
                dependencies.gitea_client,
                str(loop.get("project_id")),
                str(ticket_id),
                ticket_status,
                postgres_db=dependencies.store,
            )
            if not closed:
                logger.warning(
                    "project loop %s: close_backlog_ticket reported failure "
                    "for ticket %s → %s — the durable (file) mirror did not "
                    "land; see its own logs for the cause",
                    loop_id[:8],
                    str(ticket_id),
                    ticket_status,
                )

    if not normalized["stages"]:
        # Dispose-only filing: the campaign was closed above; open nothing and
        # fall back to plain rotation for the next turn. Persisted in its own
        # write like the plan-apply path (persist-before-spawn). A healed
        # re-run is safe: with the campaign already cleared, re-validation
        # rejects the stored dispose-only plan (nothing awaiting review) and
        # the advance degrades to the same rotation fallback.
        await dependencies.store.update_project_loop(
            loop_id, campaign=None, campaign_history=history
        )
        actions.append(
            f"project loop {loop_id[:8]}: no successor campaign opened — "
            "returning to rotation"
        )
        # None, not WB_UNSET: the campaign really was just cleared (above),
        # and the caller's loop_for_spawn is a snapshot of `loop` taken
        # BEFORE this call — without a real value here, the very next
        # spawn's kickoff still shows the just-disposed campaign as "IN
        # PROGRESS" (fix: M7). WB_UNSET means "unchanged"; that isn't true.
        return False, None

    new_campaign = {
        # Deterministic id = the plan job — a healed re-run recreates the SAME
        # campaign and is caught by the plan_job_id guard above.
        "id": str(completed_job["id"]),
        "plan_job_id": str(completed_job["id"]),
        "initiative_note_id": normalized["initiative"]["kb_note_id"],
        "title": normalized["initiative"]["title"],
        "stages": normalized["stages"],
        "acceptance": normalized["acceptance"],
        "cursor": 0,
        "stages_done": 0,
        "member_failures": 0,
        "extensions_used": extensions_used,
        "status": "active",
    }
    # Persist the campaign BEFORE spawning (own transaction): a tear between
    # this write and the spawn heals via the plan_job_id re-run above — the
    # reverse order would strand a spawned member with no campaign to join.
    await dependencies.store.update_project_loop(
        loop_id, campaign=new_campaign, campaign_history=history
    )

    handled = await spawn_campaign_member(
        loop,
        campaign={**new_campaign, "cursor": 1},
        stage_index=0,
        execution_slot=execution_slot,
        base_total=base_total,
        next_remaining=next_remaining,
        consecutive=consecutive,
        last_error=last_error,
        actions=actions,
        park_until=park_until,
        dependencies=dependencies,
    )
    return handled, WB_UNSET


async def rotate_loop_to_next_stage(
    loop: dict[str, Any],
    *,
    seq_index_completed: int,
    base_total: int,
    next_remaining: int | None,
    consecutive: int,
    last_error: str | None,
    actions: list[str],
    completed_job: dict[str, Any] | None = None,
    completed_ctx: dict[str, Any] | None = None,
    completed_failed: bool = False,
    turn_all_failed: bool = False,
    park_until: datetime | None = None,
    dependencies: ProjectLoopDependencies,
) -> None:
    """Rotate a loop past the just-finished stage and spawn the next one.

    Called by the barrier winner (``advance_loop_member``): ticks the
    KB-convergence TTL on a cycle wrap, spawns the next stage (1 or N jobs),
    and points the loop at it (``current_job_id`` or ``current_stage_jobs``).
    On a spawn failure the loop is marked failed — a running loop with no
    in-flight job/stage would never advance.

    Planner-scheduled loops (knowledge-base/knowledge/features/loop_campaign_scheduling.md) get a
    campaign step first: a checkpoint critic's filed plan expands the execution
    slot into a stage queue, and a completed campaign member spawns its
    successor instead of rotating. When the campaign step falls through
    (no plan / queue drained / abort), rotation proceeds as always — with any
    campaign status mutation riding the same write-back as the pointer.
    """
    from orchestrator.services.project_loops import next_stage_index, normalize_stage

    loop_id = str(loop["id"])
    roles = loop.get("role_sequence") or ["scholar", "critic", "developer"]

    campaign_update: Any = WB_UNSET
    if (loop.get("scheduling") or "standard") == "campaign" and completed_job:
        handled, campaign_update = await advance_planner_campaign(
            loop,
            completed_job=completed_job,
            completed_ctx=completed_ctx or {},
            completed_failed=completed_failed,
            base_total=base_total,
            next_remaining=next_remaining,
            consecutive=consecutive,
            last_error=last_error,
            actions=actions,
            park_until=park_until,
            dependencies=dependencies,
        )
        if handled:
            return

    # A turn whose every member failed re-runs its own stage rather than
    # handing the next role nothing to work from — the failed-critic case,
    # where the developer would otherwise build on a stale verdict the engine
    # cannot see. Bounded by the consecutive-failure stop evaluated above.
    # knowledge-base/knowledge/features/better_resavio_restart_status.md §6c.
    next_index, cycle_wrapped = next_stage_index(
        seq_index_completed=int(seq_index_completed),
        stage_count=len(roles),
        turn_all_failed=turn_all_failed,
    )
    if turn_all_failed:
        logger.warning(
            "project loop %s: every member of stage %s failed — re-running "
            "that stage instead of advancing (attempt %s)",
            loop_id[:8],
            next_index,
            consecutive + 1,
        )
        actions.append(
            f"project loop {loop_id[:8]}: stage {next_index} "
            f"({'/'.join(normalize_stage(roles[next_index])) if roles else '?'}) "
            f"produced nothing — re-running it rather than advancing "
            f"(attempt {consecutive + 1})"
        )

    # KB convergence (knowledge-base/knowledge/features/kb_convergence_ttl_reverification.md, F13): a
    # full cycle completed when the rotation wraps back to the first stage. Tick
    # the per-note cycle TTL down once; notes that reach <= 0 become the stale
    # queue the next job's knowledge-assembler pass re-verifies. Mirrors
    # KnowledgeStore.decrement_ttl (run inline — the orchestrator can't import
    # src/). Non-fatal. A retried stage is NOT a wrap: ageing notes on the
    # strength of a cycle that never completed would re-verify them early.
    project_id_for_ttl = loop.get("project_id")
    if cycle_wrapped and project_id_for_ttl:
        try:
            async with dependencies.vector_store.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE knowledge_index
                    SET remaining_cycles = remaining_cycles - 1
                    WHERE project_id = $1::uuid
                      AND remaining_cycles IS NOT NULL
                      AND status = 'active'
                    """,
                    str(project_id_for_ttl),
                )
        except Exception:
            logger.exception(
                "project loop %s: KB TTL decrement failed (non-fatal)", loop_id
            )

    # Reflect the decremented budget in the kickoff the next stage sees — and
    # any campaign mutation from the planner step (a review/abort flip must be
    # visible to the very next spawn: with a two-stage template the checkpoint
    # critic spawns in the SAME rotation that flips its campaign to review).
    loop_for_spawn = dict(loop)
    loop_for_spawn["remaining_iterations"] = next_remaining
    if campaign_update is not WB_UNSET:
        loop_for_spawn["campaign"] = campaign_update

    try:
        jobs, new_total = await spawn_loop_stage(
            loop_for_spawn,
            stage=roles[next_index],
            seq_index=next_index,
            base_total=base_total,
            remaining=next_remaining,
            park_until=park_until,
            dependencies=dependencies,
        )
    except Exception as e:
        logger.exception("project loop %s: failed to spawn next stage", loop_id)
        fail_fields: dict[str, Any] = dict(
            status="failed",
            remaining_iterations=next_remaining,
            consecutive_failures=consecutive,
            last_error=f"spawn failed: {e}",
            stop_reason="failures",
            current_job_id=None,
            current_stage_jobs=[],
        )
        if campaign_update is not WB_UNSET:
            fail_fields["campaign"] = campaign_update
        await dependencies.store.update_project_loop(str(loop_id), **fail_fields)
        actions.append(f"project loop {str(loop_id)[:8]} stopped (spawn failed)")
        return

    await writeback_loop_stage(
        str(loop_id),
        jobs=jobs,
        seq_index=next_index,
        remaining=next_remaining,
        total=new_total,
        consecutive=consecutive,
        last_error=last_error,
        campaign=campaign_update,
        dependencies=dependencies,
    )
    if len(jobs) == 1:
        actions.append(
            f"project loop {str(loop_id)[:8]} → {roles[next_index]} "
            f"job {str(jobs[0]['id'])[:8]}"
        )
    else:
        stage_roles = "+".join(normalize_stage(roles[next_index]))
        actions.append(
            f"project loop {str(loop_id)[:8]} → parallel stage "
            f"[{stage_roles}] ({len(jobs)} jobs)"
        )


async def prepare_atomic_project_loop_advance(
    job: dict[str, Any],
    result: dict[str, Any],
    *,
    completion_command_id: str | None = None,
    dependencies: ProjectLoopDependencies,
) -> dict[str, Any] | None:
    """Build S32's exact world expectation and pure mutation plan.

    No external work occurs after this returns until the DB transaction has
    committed. Vector/history reads needed to render successor kickoffs are
    materialized here and become inert transaction inputs.
    """

    from orchestrator.services.project_loop_atomic import (
        LoopAdvanceExpectation,
        bounded_replay_diagnostic,
        plan_loop_advance,
    )

    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    if not isinstance(ctx, Mapping) or not ctx.get("loop_id"):
        return None
    loop_id = str(ctx["loop_id"])
    loop = await dependencies.store.get_project_loop(loop_id)
    if not loop or loop.get("status") != "running":
        return None
    stage_ids = [str(value) for value in (loop.get("current_stage_jobs") or [])]
    if str(job["id"]) not in stage_ids:
        return None

    statuses = await dependencies.store.get_loop_stage_member_statuses(stage_ids)
    expectation = LoopAdvanceExpectation.from_rows(loop, statuses)
    failed = bool(result.get("error")) or job.get("status") == "failed"
    raw_error = result.get("error")
    if isinstance(raw_error, Mapping):
        raw_error = raw_error.get("message") or str(dict(raw_error))
    member_error = (str(raw_error) if raw_error else "job failed") if failed else None
    replay_error, replay_error_truncation = bounded_replay_diagnostic(member_error)

    all_survivors_terminal = all(
        status in ("completed", "failed", "cancelled") for status in statuses.values()
    )
    if not all_survivors_terminal:
        return {
            "kind": "member_only",
            "loop": loop,
            "job_context": dict(ctx),
            "output": {
                "applicable": True,
                "won": True,
                "reason": "turn_incomplete",
                "loop_id": loop_id,
                "completed_member_id": str(job["id"]),
                "spawned_job_ids": [],
                "spawned_roles": [],
                "replay": {
                    "record_member": {
                        "failed": failed,
                        "last_error": replay_error,
                        **(
                            {"last_error_truncation": replay_error_truncation}
                            if replay_error_truncation is not None
                            else {}
                        ),
                    },
                    "notify_user_questions": True,
                    "notifications": [],
                    "close_ticket": None,
                    "kb_ttl_decrement": False,
                    "officer": None,
                    "action": {"kind": "turn_incomplete"},
                },
            },
        }

    park_until = await loop_cooldown_park_until(
        job,
        result,
        stage_ids=stage_ids,
        statuses=statuses,
        dependencies=dependencies,
    )
    mutation = plan_loop_advance(
        loop,
        completed_job=job,
        completed_context=ctx,
        member_states=statuses,
        failed=failed,
        member_error=member_error,
        deadline_passed=loop_deadline_passed(loop.get("run_until")),
        park_until=park_until,
    )
    successor_identity = {
        **dict(mutation.extra_context),
        "_loop_advance_origin_job_id": str(job["id"]),
    }
    if completion_command_id is not None:
        successor_identity["_loop_advance_completion_command_id"] = str(
            completion_command_id
        )
    mutation = replace(mutation, extra_context=successor_identity)
    backlog_block: str | None = None
    history_block: str | None = None
    if mutation.stage is not None:
        backlog_block, history_block = await prepare_atomic_loop_spawn_blocks(
            loop, dependencies=dependencies
        )
    return {
        "kind": "mutation",
        "loop": loop,
        "job_context": dict(ctx),
        "expectation": expectation,
        "mutation": mutation,
        "backlog_block": backlog_block,
        "history_block": history_block,
    }


async def materialize_prepared_project_loop_advance(
    prepared: Mapping[str, Any] | None,
    job: Mapping[str, Any],
    *,
    dependencies: ProjectLoopDependencies,
) -> dict[str, Any]:
    """Run only S32's app-DB work; safe inside ``run_transactional``."""

    if prepared is None:
        return {
            "applicable": False,
            "won": True,
            "reason": "not_a_running_loop_member",
            "loop_id": None,
            "completed_member_id": str(job["id"]),
            "spawned_job_ids": [],
            "spawned_roles": [],
            "replay": {},
        }
    if prepared.get("kind") == "member_only":
        return dict(prepared["output"])

    from orchestrator.services.project_loop_atomic import (
        materialize_loop_advance_atomic,
    )

    output = await materialize_loop_advance_atomic(
        dependencies.store,
        loop_id=str(prepared["loop"]["id"]),
        member_job_id=str(job["id"]),
        expected=prepared["expectation"],
        mutation=prepared["mutation"],
        backlog_block=prepared.get("backlog_block"),
        history_block=prepared.get("history_block"),
    )
    return {"applicable": True, **output}


async def decrement_project_loop_kb_ttl_once(
    *,
    loop_id: str,
    project_id: str,
    completed_member_id: str,
    total_jobs_run: int,
    dependencies: ProjectLoopDependencies,
) -> bool:
    """Apply one cycle decrement under an immutable vector-DB turn identity.

    Both the ledger INSERT and ``knowledge_index`` UPDATE commit in one vector
    transaction. A response-lost handoff retry therefore either observes the
    exact ledger identity and skips, or finds no ledger and applies both. A key
    collision with different project/member identity fails closed.
    """

    if dependencies.vector_store is None:
        return False
    async with dependencies.vector_store.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchrow(
                """
                INSERT INTO project_loop_ttl_effects (
                    loop_id, total_jobs_run, completed_member_id, project_id
                ) VALUES ($1::uuid, $2::int, $3::uuid, $4::uuid)
                ON CONFLICT (loop_id, total_jobs_run) DO NOTHING
                RETURNING completed_member_id, project_id
                """,
                str(loop_id),
                int(total_jobs_run),
                str(completed_member_id),
                str(project_id),
            )
            if inserted is not None:
                await conn.execute(
                    """
                    UPDATE knowledge_index
                    SET remaining_cycles = remaining_cycles - 1
                    WHERE project_id = $1::uuid
                      AND remaining_cycles IS NOT NULL
                      AND status = 'active'
                    """,
                    str(project_id),
                )
                return True

            # Separate statement takes a fresh READ COMMITTED snapshot after a
            # concurrent ON CONFLICT waiter and validates immutable identity.
            existing = await conn.fetchrow(
                """
                SELECT completed_member_id, project_id
                FROM project_loop_ttl_effects
                WHERE loop_id = $1::uuid AND total_jobs_run = $2::int
                """,
                str(loop_id),
                int(total_jobs_run),
            )
            if (
                existing is None
                or str(existing["completed_member_id"]) != str(completed_member_id)
                or str(existing["project_id"]) != str(project_id)
            ):
                raise RuntimeError(
                    "project-loop KB TTL replay identity matched a different turn"
                )
            return False


async def handoff_atomic_project_loop_advance(
    job: dict[str, Any],
    atomic_output: Mapping[str, Any],
    *,
    authority_check: Callable[[], Awaitable[None]] | None = None,
    dependencies: ProjectLoopDependencies,
) -> dict[str, Any]:
    """Replay S32's external tail from committed successor IDs only."""

    if authority_check is not None:
        await authority_check()
    loop_id = atomic_output.get("loop_id")
    if (
        not atomic_output.get("applicable")
        or not atomic_output.get("won")
        or not loop_id
    ):
        return {"actions": []}
    loop = await dependencies.store.get_project_loop(str(loop_id))
    if authority_check is not None:
        await authority_check()
    if not loop:
        raise RuntimeError(f"project loop {loop_id} disappeared before S32 handoff")
    replay = atomic_output.get("replay") or {}
    if not isinstance(replay, Mapping):
        raise RuntimeError("project-loop advance replay payload is not an object")
    handoff_actions: list[str] = []
    from orchestrator.services.project_loop_atomic import bounded_replay_text

    turn_identity = (
        f"{atomic_output.get('completed_member_id')}:"
        f"{int(atomic_output.get('total_jobs_run') or 0)}"
    )

    record = replay.get("record_member") or {}
    job_context = job.get("context") or {}
    if isinstance(job_context, str):
        try:
            job_context = json.loads(job_context)
        except (json.JSONDecodeError, TypeError):
            job_context = {}
    await record_loop_job_outcome(
        job,
        ctx=(dict(job_context) if isinstance(job_context, Mapping) else {}),
        loop=loop,
        loop_id=str(loop_id),
        actions=handoff_actions,
        failed=bool(record.get("failed")),
        last_error=(str(record["last_error"]) if record.get("last_error") else None),
        durable=True,
        authority_check=authority_check,
        dependencies=dependencies,
    )
    if authority_check is not None:
        await authority_check()
    if replay.get("notify_user_questions"):
        await notify_loop_user_questions(
            loop,
            job,
            dedup_turn_identity=turn_identity,
            durable=True,
            authority_check=authority_check,
            dependencies=dependencies,
        )
        if authority_check is not None:
            await authority_check()

    from orchestrator.services.job_provisioning import provision_job_repo

    spawned_ids = [str(value) for value in atomic_output.get("spawned_job_ids") or []]
    for spawned_id in spawned_ids:
        if authority_check is not None:
            await authority_check()
        spawned = await dependencies.store.get_job(spawned_id)
        if authority_check is not None:
            await authority_check()
        if not spawned:
            raise RuntimeError(
                f"atomic loop successor {spawned_id} disappeared before provisioning"
            )
        await provision_job_repo(
            job_row=spawned,
            gitea_client=dependencies.gitea_client,
            postgres_db=dependencies.store,
            main_cloud_router=dependencies.main_cloud_router,
            loop_floor=True,
            authority_check=authority_check,
        )
        if authority_check is not None:
            await authority_check()

    if replay.get("kb_ttl_decrement") and loop.get("project_id"):
        if authority_check is not None:
            await authority_check()
        await decrement_project_loop_kb_ttl_once(
            loop_id=str(loop_id),
            project_id=str(loop["project_id"]),
            completed_member_id=str(atomic_output["completed_member_id"]),
            total_jobs_run=int(atomic_output.get("total_jobs_run") or 0),
            dependencies=dependencies,
        )
        if authority_check is not None:
            await authority_check()

    close_ticket = replay.get("close_ticket")
    if isinstance(close_ticket, Mapping) and dependencies.vector_store is not None:
        from orchestrator.services.project_backlog import close_backlog_ticket

        if authority_check is not None:
            await authority_check()
        if not await close_backlog_ticket(
            dependencies.vector_store,
            dependencies.gitea_client,
            str(loop.get("project_id")),
            str(close_ticket["note_id"]),
            str(close_ticket["status"]),
            postgres_db=dependencies.store,
            authority_check=authority_check,
        ):
            raise RuntimeError(
                "project loop "
                f"{loop_id}: could not durably mirror ticket "
                f"{close_ticket['note_id']} -> {close_ticket['status']}"
            )
        if authority_check is not None:
            await authority_check()

    for notification_index, notification in enumerate(
        replay.get("notifications") or []
    ):
        if not isinstance(notification, Mapping):
            continue
        await notify_loop_event(
            loop,
            job_id=str(job["id"]),
            event_type=str(notification["event_type"]),
            subject=str(notification["subject"]),
            message=str(notification["message"]),
            dedup_turn_identity=turn_identity,
            note_id=f"planned:{notification_index}",
            authority_check=authority_check,
            dependencies=dependencies,
        )
        if authority_check is not None:
            await authority_check()

    officer = replay.get("officer")
    if isinstance(officer, Mapping):
        project_id = str(loop.get("project_id") or "")
        if authority_check is not None:
            await authority_check()
        officer_thread = await dependencies.store.get_officer_thread_for_project(
            project_id
        )
        if authority_check is not None:
            await authority_check()
        if officer_thread:
            await dependencies.store.enqueue_session_wake_event(
                str(officer_thread["id"]),
                source="loop",
                dedup_key=str(officer["dedup_key"]),
                project_id=project_id,
                payload={
                    "loop_id": str(loop_id),
                    "turn_all_failed": bool(officer.get("turn_all_failed")),
                    "consecutive_failures": int(
                        officer.get("consecutive_failures") or 0
                    ),
                    "summary": (
                        "loop turn concluded — scheduling='officer': the next "
                        "dispatch is yours (nothing was auto-created)"
                    ),
                },
            )
            if authority_check is not None:
                await authority_check()
        if authority_check is not None:
            await authority_check()
        dependencies.kick_officer_event_drain(dependencies.store)

    for pre_action in replay.get("pre_actions") or []:
        if not isinstance(pre_action, Mapping):
            continue
        pre_kind = pre_action.get("kind")
        if pre_kind == "cooldown_park":
            handoff_actions.append(
                f"project loop {str(loop_id)[:8]}: model cooldown — next member "
                f"parked until {pre_action.get('park_until')}"
            )
        elif pre_kind == "campaign_aborted":
            handoff_actions.append(
                f"project loop {str(loop_id)[:8]}: campaign "
                f"'{pre_action.get('label')}' ABORTED after "
                f"{pre_action.get('member_failures')} consecutive member failures — "
                "returning to the critic checkpoint"
            )
        elif pre_kind == "campaign_complete":
            handoff_actions.append(
                f"project loop {str(loop_id)[:8]}: campaign "
                f"'{pre_action.get('label')}' complete "
                f"({pre_action.get('stage_count')} stages) — awaiting critic review"
            )
        elif pre_kind == "campaign_review_skipped":
            handoff_actions.append(
                f"project loop {str(loop_id)[:8]}: campaign "
                f"'{pre_action.get('label')}' still awaits disposition — checkpoint "
                "critic filed no plan; dispose-only filing is allowed"
            )
        elif pre_kind == "campaign_disposed":
            handoff_actions.append(
                f"project loop {str(loop_id)[:8]}: campaign "
                f"'{pre_action.get('label')}' disposed ({pre_action.get('outcome')})"
            )
        elif pre_kind == "campaign_dispose_only":
            handoff_actions.append(
                f"project loop {str(loop_id)[:8]}: no successor campaign opened — "
                "returning to rotation"
            )
        elif pre_kind == "plan_rejected":
            handoff_actions.append(
                f"project loop {str(loop_id)[:8]}: filed plan rejected at apply "
                f"time ({pre_action.get('error')}) — falling back to rotation"
            )

    action = replay.get("action") or {}
    action_kind = action.get("kind") if isinstance(action, Mapping) else None
    if action_kind == "stop":
        handoff_actions.append(
            f"project loop {str(loop_id)[:8]} stopped ({action.get('reason')})"
        )
    elif action_kind == "officer":
        handoff_actions.append(
            f"project loop {str(loop_id)[:8]} turn concluded — "
            "officer-scheduled, no auto-advance"
        )
    elif action_kind == "campaign_member" and spawned_ids:
        handoff_actions.append(
            f"project loop {str(loop_id)[:8]} → campaign "
            f"'{action.get('label')}' stage {int(action.get('stage_index') or 0) + 1}/"
            f"{action.get('stage_count')} ({action.get('role')} job "
            f"{spawned_ids[0][:8]})"
        )
    elif action_kind == "rotation" and spawned_ids:
        stage = action.get("stage")
        if len(spawned_ids) == 1:
            handoff_actions.append(
                f"project loop {str(loop_id)[:8]} → {stage} job {spawned_ids[0][:8]}"
            )
        else:
            from orchestrator.services.project_loops import normalize_stage

            handoff_actions.append(
                f"project loop {str(loop_id)[:8]} → parallel stage "
                f"[{'+'.join(normalize_stage(stage))}] ({len(spawned_ids)} jobs)"
            )

    if spawned_ids:
        if authority_check is not None:
            await authority_check()
        dependencies.trigger_dispatch()
    return {
        "actions": [
            bounded_replay_text(action_text, limit_bytes=768)
            for action_text in handoff_actions
        ]
    }


def project_loop_handoff_marker(job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    context = job.get("context") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (json.JSONDecodeError, TypeError):
            return None
    marker = (
        context.get("_project_loop_advance_handoff")
        if isinstance(context, Mapping)
        else None
    )
    return marker if isinstance(marker, Mapping) else None


PROJECT_LOOP_HANDOFF_LEASE_SECONDS = 120.0
PROJECT_LOOP_HANDOFF_HEARTBEAT_SECONDS = 30.0


async def execute_persisted_project_loop_handoff(
    job: dict[str, Any],
    atomic_output: Mapping[str, Any],
    *,
    dependencies: ProjectLoopDependencies,
) -> dict[str, Any]:
    """Run/replay the full external tail and settle its predecessor marker."""

    job_id = str(job["id"])
    current = await dependencies.store.get_job(job_id)
    marker = project_loop_handoff_marker(current or job)
    expected_output = dict(atomic_output)
    if marker is not None:
        if marker.get("output") != expected_output:
            raise RuntimeError(
                "project-loop handoff output differs from persisted marker"
            )
        if marker.get("state") == "done" and isinstance(marker.get("result"), Mapping):
            return dict(marker["result"])
        if marker.get("state") not in {"pending", "claimed"}:
            raise RuntimeError("project-loop handoff marker has an unknown state")

    if marker is None:
        # Member-only S32 results make no loop-world mutation and therefore
        # carry no sweeper marker; their command-owned handoff effect is enough.
        return await handoff_atomic_project_loop_advance(
            job, expected_output, dependencies=dependencies
        )

    claimant_id = f"project-loop-handoff:{uuid4()}"
    claimed = await dependencies.store.claim_project_loop_handoff(
        job_id,
        expected_output=expected_output,
        claimant_id=claimant_id,
        lease_seconds=PROJECT_LOOP_HANDOFF_LEASE_SECONDS,
    )
    if not claimed:
        # A contender can finish between our initial read and claim attempt.
        refreshed = await dependencies.store.get_job(job_id)
        refreshed_marker = project_loop_handoff_marker(refreshed or {})
        if (
            refreshed_marker is not None
            and refreshed_marker.get("output") == expected_output
            and refreshed_marker.get("state") == "done"
            and isinstance(refreshed_marker.get("result"), Mapping)
        ):
            return dict(refreshed_marker["result"])
        raise RuntimeError("project-loop handoff is owned by another live claimant")

    stopped = asyncio.Event()
    lost = asyncio.Event()

    async def _assert_authority() -> None:
        """Refresh the exact claim before starting another consequence."""

        from orchestrator.services.project_loop_atomic import (
            ProjectLoopHandoffAuthorityLost,
        )

        if lost.is_set():
            raise ProjectLoopHandoffAuthorityLost("project-loop handoff lease was lost")
        try:
            renewed = await dependencies.store.renew_project_loop_handoff(
                job_id,
                expected_output=expected_output,
                claimant_id=claimant_id,
                lease_seconds=PROJECT_LOOP_HANDOFF_LEASE_SECONDS,
            )
        except Exception as exc:
            lost.set()
            raise ProjectLoopHandoffAuthorityLost(
                "project-loop handoff lease refresh failed"
            ) from exc
        if not renewed:
            lost.set()
            raise ProjectLoopHandoffAuthorityLost("project-loop handoff lease was lost")

    async def _heartbeat() -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(
                    stopped.wait(),
                    timeout=PROJECT_LOOP_HANDOFF_HEARTBEAT_SECONDS,
                )
                return
            except TimeoutError:
                pass
            try:
                renewed = await dependencies.store.renew_project_loop_handoff(
                    job_id,
                    expected_output=expected_output,
                    claimant_id=claimant_id,
                    lease_seconds=PROJECT_LOOP_HANDOFF_LEASE_SECONDS,
                )
            except Exception:
                logger.exception(
                    "project-loop handoff heartbeat failed for predecessor %s",
                    job_id,
                )
                renewed = False
            if not renewed:
                lost.set()
                return

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        result = await handoff_atomic_project_loop_advance(
            job,
            expected_output,
            authority_check=_assert_authority,
            dependencies=dependencies,
        )
        await _assert_authority()
        return await dependencies.store.finish_project_loop_handoff(
            job_id,
            expected_output=expected_output,
            result=result,
            claimant_id=claimant_id,
        )
    finally:
        stopped.set()
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


def project_loop_handoff_error_output(exc: BaseException) -> dict[str, Any]:
    """Bound retry diagnostics before the effect runner persists them."""

    from orchestrator.services.project_loop_atomic import bounded_replay_text

    return {
        "actions": [],
        "error": bounded_replay_text(exc, limit_bytes=1024),
    }


async def reconcile_atomic_project_loop_handoff(
    *,
    limit: int = 50,
    dependencies: ProjectLoopDependencies,
) -> int:
    """Reconcile full pending handoffs, including empty/terminal loop worlds.

    The bounded descriptor is written onto the completed predecessor in the
    SAME app-DB transaction as successor rows and loop pointers. It therefore
    survives crashes before provisioning and remains discoverable after
    provisioning changes successor baselines to ``ready``, after an officer
    turn clears pointers, or after a stop makes the loop non-running.

    Command-owned descriptors route to their exact finalizer first. Only a
    genuinely command-less (or already-terminal legacy) descriptor executes
    here, through the same idempotent full-tail function used by S32.
    """

    reconciled = 0
    for origin in await dependencies.store.list_pending_project_loop_handoffs(
        limit=limit
    ):
        marker = project_loop_handoff_marker(origin)
        if marker is None or not isinstance(marker.get("output"), Mapping):
            raise RuntimeError("pending project-loop handoff descriptor is malformed")
        command_id = marker.get("command_id")
        if command_id:
            routed = await dependencies.completion_sweep_router().route_job(
                str(origin["id"]), source="project_loop_handoff"
            )
            if not routed.legacy:
                # live => stand down; expired => finalizer resumed; parked =>
                # alert-only. In all three cases this synthesizer must not run a
                # parallel copy of the command-owned tail.
                continue
        await execute_persisted_project_loop_handoff(
            origin,
            dict(marker["output"]),
            dependencies=dependencies,
        )
        reconciled += 1
    return reconciled


async def advance_project_loop(
    job: dict[str, Any],
    result: dict[str, Any],
    actions: list[str],
    *,
    dependencies: ProjectLoopDependencies,
) -> None:
    """Advance a project self-improvement loop when one of its in-flight
    turn's jobs completes.

    Every turn is a barrier-tracked set of jobs in ``current_stage_jobs``
    (width 1 included) — the engine's ONLY advance path
    (knowledge-base/knowledge/features/loop_unified_engine.md). Membership is the idempotency
    guard: a stale or re-delivered completion hook for a job outside the
    current turn is a no-op, and the atomic barrier claim inside
    ``advance_loop_member`` guarantees exactly one rotate per turn. Loop
    jobs run bare, so this is the only completion hook that fires for them.
    """
    ctx = job.get("context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            ctx = {}
    loop_id = (ctx or {}).get("loop_id")
    if not loop_id:
        return

    # The durable command path moves the barrier claim into S32's Class-C
    # transaction with successor materialization. Direct callers here are
    # safety nets, not the finalizer. A live finalizer lease owns the turn, so
    # that one route stands down. Expired/parked routes are durably nudged but
    # do NOT suppress this class-2 synthesizer: it may win the exact-world CAS,
    # in which case the resumed S32 marks itself superseded; if S32 wins first,
    # our freshly planned transaction loses benignly. The default-off path
    # below remains the historical helper/call graph byte-for-byte.
    if dependencies.completion_commands_enabled():
        routed = await dependencies.completion_sweep_router().enqueue_job(
            str(job["id"]), source="project_loop_advance"
        )
        if routed.route == "stand_down":
            return
        prepared = await prepare_atomic_project_loop_advance(
            job, result, dependencies=dependencies
        )
        output = await materialize_prepared_project_loop_advance(
            prepared, job, dependencies=dependencies
        )
        handoff = await execute_persisted_project_loop_handoff(
            job, output, dependencies=dependencies
        )
        actions.extend(handoff["actions"])
        return

    loop = await dependencies.store.get_project_loop(str(loop_id))
    if not loop or loop.get("status") != "running":
        return  # paused / stopped / terminal — leave the current job, don't advance

    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
    if str(job["id"]) not in stage_ids:
        return  # not a member of the in-flight turn
    await advance_loop_member(
        job, result, actions, loop=loop, ctx=ctx or {}, dependencies=dependencies
    )


async def loop_cooldown_park_until(
    winner_job: dict[str, Any],
    result: dict[str, Any],
    *,
    stage_ids: list[str],
    statuses: dict[str, str],
    dependencies: ProjectLoopDependencies,
) -> datetime | None:
    """When the completed turn failed on a model cooldown, the instant the
    NEXT member should wake — else None (spawn normally).

    ANY cooldown-failed member of the turn triggers the park (the loop-level
    model pin dooms the next turn regardless of sibling successes). Wake =
    ``max(reset_at)`` among cooldown-failed members, clamped to
    ``LOOP_COOLDOWN_PARK_CAP_SECONDS``; already-past resets are dropped. The
    winner's reset rides the in-flight completion payload; siblings went
    terminal earlier, so their row (``error_details``, written atomically with
    ``status='failed'``) is the truth.
    knowledge-base/knowledge/issues/loop_advances_into_active_model_cooldown.md
    """
    from orchestrator.services.project_loops import (
        LOOP_COOLDOWN_PARK_CAP_SECONDS,
        extract_cooldown_reset_at,
    )

    winner_id = str(winner_job["id"])
    resets: list[float] = []
    for mid in stage_ids:
        if statuses.get(mid) != "failed":
            continue
        if mid == winner_id:
            reset = extract_cooldown_reset_at(winner_job, result)
        else:
            row = await dependencies.store.get_job(mid)
            reset = extract_cooldown_reset_at(row or {}, {})
        if reset is not None:
            resets.append(reset)

    now_epoch = datetime.now(timezone.utc).timestamp()
    future = [r for r in resets if r > now_epoch]
    if not future:
        return None
    park = min(max(future), now_epoch + LOOP_COOLDOWN_PARK_CAP_SECONDS)
    return datetime.fromtimestamp(park, tz=timezone.utc)


async def advance_loop_member(
    job: dict[str, Any],
    result: dict[str, Any],
    actions: list[str],
    *,
    loop: dict[str, Any],
    ctx: dict[str, Any],
    dependencies: ProjectLoopDependencies,
) -> None:
    """Advance a loop when a member of its in-flight turn completes.

    Each member records its already-resolved cloud delivery immediately (its
    artifact handling is independent), then hits the barrier:
    ``claim_project_loop_stage_barrier``
    drains the turn and returns True to exactly ONE caller — the member that
    finishes last (trivially, the job itself on a width-1 turn). Only that
    caller aggregates the turn outcome (a turn counts as a failure only if
    EVERY member failed; one success resets the consecutive counter), checks
    the stop conditions, and rotates to the next stage. Every earlier
    finisher just records its outcome and backs off.

    The barrier winner's job + decoded context feed the campaign step inside
    ``rotate_loop_to_next_stage``. Campaign-relevant jobs (the checkpoint
    critic and campaign members) only ever occupy width-1 turns by planner
    grammar, so the winner IS the campaign job whenever it matters; for a
    fan-out turn the campaign step falls through as a no-op.
    knowledge-base/knowledge/features/loop_unified_engine.md (Phase 1).
    """
    loop_id = str(loop["id"])
    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]

    failed = bool(result.get("error")) or job.get("status") == "failed"
    # The agent's error may be a structured dict (e.g. the cooldown fail-fast);
    # loop last_error and the retro want the human message, not the dict.
    _err = result.get("error")
    if isinstance(_err, dict):
        _err = _err.get("message") or str(_err)
    member_error = (str(_err) if _err else "job failed") if failed else None

    # Per-member artifact handling: the cloud delivery already ran before the
    # successful terminal transition; persist its structured record and refresh
    # the independent KB. Runs before the barrier claim and is idempotent by
    # job id, while rotation remains exactly-once behind the barrier.
    await record_loop_job_outcome(
        job,
        ctx=ctx,
        loop=loop,
        loop_id=loop_id,
        actions=actions,
        failed=failed,
        last_error=member_error,
        dependencies=dependencies,
    )
    # Surface this member's `user-question` KB notes (every member passes
    # here regardless of who wins the barrier).
    await notify_loop_user_questions(loop, job, dependencies=dependencies)

    # Barrier: only the last member to go terminal claims the rotate.
    if not await dependencies.store.claim_project_loop_stage_barrier(
        loop_id, str(job["id"])
    ):
        return  # an earlier finisher, a lost co-last race, or a stray hook

    # Last out. Aggregate the turn outcome from the members' final statuses
    # (captured from the pre-drain membership snapshot).
    statuses = await dependencies.store.get_loop_stage_member_statuses(stage_ids)
    member_states = [statuses.get(mid, "failed") for mid in stage_ids]
    all_failed = bool(member_states) and all(s == "failed" for s in member_states)
    consecutive = (int(loop.get("consecutive_failures") or 0) + 1) if all_failed else 0
    # A width-1 turn keeps the member's specific error (the pre-unification
    # single-role behavior); a fan-out aggregate can only say everything failed.
    last_error = (
        (member_error if len(stage_ids) == 1 else "all stage jobs failed")
        if all_failed
        else None
    )

    if (loop.get("scheduling") or "standard") == "officer":
        # Officer-scheduled century (centurion.md §7): judgment replaces the
        # mechanical advance. The per-member merge/retro and user-question
        # notify above already ran; from here the standard path would
        # decrement iterations, evaluate stop reasons, park on cooldown and
        # rotate — all skipped: the officer decides what runs next from
        # backlog + sitrep + charter. The barrier claim above makes this
        # exactly-once per turn; empty stage pointers are the officer loop's
        # steady state (the sweeper's heal skips officer loops for the same
        # reason). The completed job itself already woke the officer via
        # maybe_wake_session's officer leg — this event marks the TURN
        # concluding, and the drain coalesces both into one sitrep.
        await dependencies.store.update_project_loop(
            loop_id,
            consecutive_failures=consecutive,
            last_error=last_error,
            current_job_id=None,
            current_stage_jobs=[],
        )
        await notify_officer(
            dependencies.store,
            str(loop.get("project_id") or (ctx or {}).get("project_id") or ""),
            source="loop",
            dedup_key=f"{loop_id[:8]}:{int(loop.get('seq_index') or 0)}",
            payload={
                "loop_id": loop_id,
                "turn_all_failed": all_failed,
                "consecutive_failures": consecutive,
                "summary": (
                    "loop turn concluded — scheduling='officer': the next "
                    "dispatch is yours (nothing was auto-created)"
                ),
            },
        )
        dependencies.kick_officer_event_drain(dependencies.store)
        actions.append(
            f"project loop {str(loop_id)[:8]} turn concluded — "
            f"officer-scheduled, no auto-advance"
        )
        return

    remaining = loop.get("remaining_iterations")
    next_remaining = (remaining - 1) if remaining is not None else None

    stop_reason = loop_stop_reason(
        loop, next_remaining=next_remaining, consecutive=consecutive
    )
    if stop_reason:
        await dependencies.store.update_project_loop(
            loop_id,
            status=("failed" if stop_reason == "failures" else "completed"),
            remaining_iterations=next_remaining,
            consecutive_failures=consecutive,
            last_error=last_error,
            stop_reason=stop_reason,
            current_job_id=None,
            current_stage_jobs=[],
        )
        actions.append(f"project loop {str(loop_id)[:8]} stopped ({stop_reason})")
        return

    # Born-parked next spawn on a model-cooldown turn failure
    # (knowledge-base/knowledge/issues/loop_advances_into_active_model_cooldown.md, Option A).
    # Strictly after the barrier claim (exactly-once per turn) and the stop
    # check (a stopping loop stops exactly as before — no park, no notify).
    park_until = await loop_cooldown_park_until(
        job,
        result,
        stage_ids=stage_ids,
        statuses=statuses,
        dependencies=dependencies,
    )
    if park_until is not None:
        park_iso = park_until.isoformat()
        actions.append(
            f"project loop {str(loop_id)[:8]}: model cooldown — "
            f"next member parked until {park_iso}"
        )
        await notify_loop_event(
            loop,
            job_id=str(job["id"]),
            event_type="loop_cooldown_park",
            subject="Loop waiting for model cooldown",
            message=(
                f"A loop member failed because model "
                f"'{loop.get('model') or 'the pinned model'}' is in a quota "
                f"cooldown. The next member was created parked and will "
                f"dispatch automatically at {park_iso}."
            ),
            dependencies=dependencies,
        )

    await rotate_loop_to_next_stage(
        loop,
        seq_index_completed=int(loop.get("seq_index") or 0),
        base_total=int(loop.get("total_jobs_run") or 0),
        next_remaining=next_remaining,
        consecutive=consecutive,
        last_error=last_error,
        actions=actions,
        completed_job=job,
        completed_ctx=ctx,
        completed_failed=failed,
        turn_all_failed=all_failed,
        park_until=park_until,
        dependencies=dependencies,
    )


async def resume_project_loop(
    loop_id: str, *, dependencies: ProjectLoopDependencies
) -> dict[str, Any] | None:
    """Resume a paused project loop.

    Sets status back to ``running``. The barrier is gated on
    ``status='running'``, so any member of the in-flight turn that went
    terminal while the loop was paused didn't advance it. Re-run the advance
    for each already-terminal member so the barrier can fire (the sweeper
    would eventually catch this too); members still running advance the loop
    naturally on completion.
    """
    loop = await dependencies.store.update_project_loop(loop_id, status="running")
    if not loop:
        return None
    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
    if stage_ids:
        for mid in stage_ids:
            mjob = await dependencies.store.get_job(mid)
            if mjob and mjob.get("status") in ("completed", "failed", "cancelled"):
                await advance_project_loop(mjob, {}, [], dependencies=dependencies)
        loop = await dependencies.store.get_project_loop(loop_id)
    return loop


__all__ = [
    "PROJECT_LOOP_HANDOFF_HEARTBEAT_SECONDS",
    "PROJECT_LOOP_HANDOFF_LEASE_SECONDS",
    "advance_loop_member",
    "advance_planner_campaign",
    "advance_project_loop",
    "decrement_project_loop_kb_ttl_once",
    "execute_persisted_project_loop_handoff",
    "handoff_atomic_project_loop_advance",
    "loop_cooldown_park_until",
    "materialize_prepared_project_loop_advance",
    "prepare_atomic_project_loop_advance",
    "project_loop_handoff_error_output",
    "project_loop_handoff_marker",
    "reconcile_atomic_project_loop_handoff",
    "resume_project_loop",
    "rotate_loop_to_next_stage",
    "spawn_campaign_member",
]
