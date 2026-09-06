"""Dark, admin-only verification plans for Officer runtime-grant liveness.

The production grant protocol remains authoritative.  This module only adds a
bounded, durable test plan under the already-authoritative Officer Post row.
Every consuming hook runs while the caller holds Post -> thread -> agent ->
grant locks, so a plan can affect exactly one commissioned runtime binding and
cannot follow a stale pod across recycle/recommission.

No credential, digest, ciphertext, or caller-authored runtime identity is ever
stored in or returned from a plan.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4


PLAN_STATE_KEY = "runtime_actor_verification"
PLAN_VERSION = 1
ACTIVE_STATES = frozenset(
    {"armed", "faulted", "recovering", "redelivery_pending", "awaiting_ack"}
)
TERMINAL_STATES = frozenset(
    {"completed", "disarmed", "expired", "invalidated", "exhausted"}
)
EXERCISES = frozenset({"longevity", "response_loss", "maintenance_failure"})

MIN_PLAN_SECONDS = 120
MAX_PLAN_SECONDS = 3600
MIN_LOGICAL_WINDOW_SECONDS = 30
MAX_LOGICAL_WINDOW_SECONDS = 600
MAX_RESPONSE_LOSS_GAP_SECONDS = 300
DEFAULT_RESPONSE_LOSS_GAP_SECONDS = 125


class RuntimeVerificationPlanError(Exception):
    """Safe, closed-vocabulary admin-plan refusal."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RefreshVerificationDecision:
    """Private decision consumed inside one refresh transaction."""

    plan_id: str | None = None
    exercise: str | None = None
    force_rotation: bool = False
    block_code: str | None = None
    inject_maintenance_failure: bool = False


@dataclass(frozen=True, slots=True)
class MaintenanceVerificationDecision:
    """Private decision consumed inside one watchdog transaction."""

    force_renewal: bool = False
    inject_maintenance_failure: bool = False


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _text_id(value: Any) -> str:
    return str(value) if value is not None else ""


def _incarnation(post: Any) -> int:
    values = post.get("incarnations") or []
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (TypeError, ValueError):
            values = []
    return len(values) if isinstance(values, list) else 0


def _plan_from_post(post: Any) -> dict[str, Any]:
    return _as_dict(_as_dict(post.get("state")).get(PLAN_STATE_KEY))


def _is_expired(plan: dict[str, Any], now: datetime) -> bool:
    expiry = _as_utc(plan.get("expires_at"))
    return expiry is None or expiry <= now


def _scope_matches(
    plan: dict[str, Any],
    *,
    post: Any,
    thread: Any,
    agent: Any,
    grant: Any,
) -> bool:
    return bool(
        int(plan.get("version") or 0) == PLAN_VERSION
        and _text_id(plan.get("project_id")) == _text_id(post.get("project_id"))
        and _text_id(plan.get("thread_id")) == _text_id(thread.get("id"))
        and int(plan.get("officer_incarnation") or 0) == _incarnation(post)
        and _text_id(plan.get("agent_id")) == _text_id(agent.get("id"))
        and _text_id(plan.get("grant_id")) == _text_id(grant.get("id"))
        and int(plan.get("grant_generation") or 0)
        == int(grant.get("credential_generation") or 0)
        and _text_id(post.get("thread_id")) == _text_id(thread.get("id"))
        and _text_id(thread.get("agent_id")) == _text_id(agent.get("id"))
        and _text_id(agent.get("thread_id")) == _text_id(thread.get("id"))
        and str(grant.get("caller_kind") or "") == "officer"
        and grant.get("revoked_at") is None
    )


async def _save_plan(conn: Any, post: Any, plan: dict[str, Any]) -> None:
    await conn.execute(
        """
        UPDATE project_officers
           SET state = jsonb_set(COALESCE(state, '{}'::jsonb),
                                 '{runtime_actor_verification}', $2::jsonb, true),
               updated_at = now()
         WHERE project_id = $1
        """,
        post["project_id"],
        json.dumps(plan),
    )


async def _lock_current_plan_scope(
    conn: Any, *, post: Any, plan: dict[str, Any], now: datetime
) -> bool:
    """Revalidate Post -> thread -> agent -> grant for an admin transition."""

    if _text_id(post.get("thread_id")) != _text_id(plan.get("thread_id")):
        return False
    thread = await conn.fetchrow(
        "SELECT id, project_id, user_id, status, metadata, execution_lane, "
        "runtime_generation, runtime_attach_token, runtime_retirement_token, "
        "agent_id FROM threads WHERE id = $1::uuid FOR UPDATE",
        str(plan.get("thread_id")),
    )
    if thread is None or thread.get("agent_id") is None:
        return False
    agent = await conn.fetchrow(
        "SELECT id, thread_id, status, last_heartbeat FROM agents "
        "WHERE id = $1 FOR UPDATE",
        thread["agent_id"],
    )
    if agent is None:
        return False
    from orchestrator.services.runtime_actor import (
        lock_current_officer_runtime_grant,
    )

    grant = await lock_current_officer_runtime_grant(
        conn, post=post, thread=thread, agent=agent, now=now
    )
    if grant is None:
        return False
    grant = await conn.fetchrow(
        "SELECT id, caller_kind, credential_generation, revoked_at "
        "FROM runtime_actor_grants WHERE id = $1 FOR UPDATE",
        grant["id"],
    )
    return bool(
        grant
        and _scope_matches(plan, post=post, thread=thread, agent=agent, grant=grant)
    )


def _terminalize(
    plan: dict[str, Any], state: str, now: datetime, *, reason: str | None = None
) -> dict[str, Any]:
    next_plan = dict(plan)
    next_plan["state"] = state
    next_plan["finished_at"] = now.isoformat()
    next_plan["last_transition_at"] = now.isoformat()
    if reason:
        next_plan["finish_reason"] = reason[:128]
    return next_plan


def _request_digest(
    *,
    project_id: str,
    exercise: str,
    created_by: str,
    expires_in_seconds: int,
    parameters: dict[str, Any],
) -> str:
    """Fingerprint the normalized, non-secret create request."""

    canonical = {
        "version": PLAN_VERSION,
        "project_id": project_id,
        "exercise": exercise,
        "created_by": created_by,
        "expires_in_seconds": int(expires_in_seconds),
        "parameters": parameters,
    }
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_projection(plan: dict[str, Any], *, replayed: bool = False) -> dict[str, Any]:
    """Return the operator view; the stored shape contains no credentials."""

    if not plan:
        return {"configured": False}
    return {
        "configured": True,
        "plan_id": plan.get("plan_id"),
        "idempotency_key": plan.get("idempotency_key"),
        "request_digest": plan.get("request_digest"),
        "exercise": plan.get("exercise"),
        "state": plan.get("state"),
        "project_id": plan.get("project_id"),
        "thread_id": plan.get("thread_id"),
        "officer_incarnation": plan.get("officer_incarnation"),
        "agent_id": plan.get("agent_id"),
        "grant_generation": plan.get("grant_generation"),
        "created_by": plan.get("created_by"),
        "created_at": plan.get("created_at"),
        "recovery_requested_by": plan.get("recovery_requested_by"),
        "recovery_requested_at": plan.get("recovery_requested_at"),
        "disarmed_by": plan.get("disarmed_by"),
        "disarmed_at": plan.get("disarmed_at"),
        "expires_at": plan.get("expires_at"),
        "finished_at": plan.get("finished_at"),
        "last_transition_at": plan.get("last_transition_at"),
        "finish_reason": plan.get("finish_reason"),
        "attempt_count": int(plan.get("attempt_count") or 0),
        "max_attempts": int(plan.get("max_attempts") or 0),
        "parameters": _as_dict(plan.get("parameters")),
        "progress": _as_dict(plan.get("progress")),
        "replayed": replayed,
    }


async def create_plan(
    db: Any,
    *,
    enabled: bool,
    project_id: str,
    idempotency_key: str,
    exercise: str,
    created_by: str,
    expires_in_seconds: int,
    logical_window_seconds: int | None = None,
    response_losses: int | None = None,
    response_loss_gap_seconds: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one exact current-binding plan under the durable Post lock."""

    if not enabled:
        raise RuntimeVerificationPlanError(
            "verification_disabled",
            "Officer runtime verification is disabled for this deployment.",
            status_code=404,
        )
    try:
        normalized_project_id = str(UUID(str(project_id)))
        normalized_idempotency_key = str(UUID(str(idempotency_key)))
        normalized_created_by = str(UUID(str(created_by)))
    except ValueError as exc:
        raise RuntimeVerificationPlanError(
            "invalid_identity",
            "Verification identities must be UUIDs.",
            status_code=400,
        ) from exc
    if exercise not in EXERCISES:
        raise RuntimeVerificationPlanError(
            "invalid_exercise", "Unsupported verification exercise.", status_code=400
        )
    if not MIN_PLAN_SECONDS <= int(expires_in_seconds) <= MAX_PLAN_SECONDS:
        raise RuntimeVerificationPlanError(
            "invalid_expiry",
            f"Plan expiry must be {MIN_PLAN_SECONDS}-{MAX_PLAN_SECONDS} seconds.",
            status_code=400,
        )

    parameters: dict[str, Any]
    max_attempts: int
    if exercise == "longevity":
        window = int(60 if logical_window_seconds is None else logical_window_seconds)
        if not MIN_LOGICAL_WINDOW_SECONDS <= window <= MAX_LOGICAL_WINDOW_SECONDS:
            raise RuntimeVerificationPlanError(
                "invalid_logical_window",
                "Logical renewal window is outside the supported bound.",
                status_code=400,
            )
        if expires_in_seconds <= (2 * window):
            raise RuntimeVerificationPlanError(
                "invalid_expiry",
                "Plan expiry must outlast both logical renewal windows.",
                status_code=400,
            )
        parameters = {"logical_window_seconds": window, "windows_required": 2}
        max_attempts = 20
    elif exercise == "response_loss":
        losses = int(1 if response_losses is None else response_losses)
        gap = int(
            DEFAULT_RESPONSE_LOSS_GAP_SECONDS
            if response_loss_gap_seconds is None
            else response_loss_gap_seconds
        )
        if losses not in {1, 2}:
            raise RuntimeVerificationPlanError(
                "invalid_response_loss_count",
                "Response-loss verification supports one or two losses.",
                status_code=400,
            )
        if not 0 <= gap <= MAX_RESPONSE_LOSS_GAP_SECONDS:
            raise RuntimeVerificationPlanError(
                "invalid_response_loss_gap",
                "Response-loss spacing is outside the supported bound.",
                status_code=400,
            )
        if losses == 2 and expires_in_seconds <= gap:
            raise RuntimeVerificationPlanError(
                "invalid_expiry",
                "Plan expiry must outlast the response-loss spacing.",
                status_code=400,
            )
        parameters = {
            "response_losses": losses,
            "response_loss_gap_seconds": gap,
        }
        max_attempts = 20
    else:
        parameters = {"fault_attempts": 1}
        max_attempts = 20

    request_digest = _request_digest(
        project_id=normalized_project_id,
        exercise=exercise,
        created_by=normalized_created_by,
        expires_in_seconds=int(expires_in_seconds),
        parameters=parameters,
    )

    observed_at = now or datetime.now(timezone.utc)
    async with db.acquire() as conn:
        async with conn.transaction():
            post = await conn.fetchrow(
                "SELECT project_id, thread_id, incarnations, state "
                "FROM project_officers WHERE project_id = $1::uuid FOR UPDATE",
                normalized_project_id,
            )
            if post is None or post.get("thread_id") is None:
                raise RuntimeVerificationPlanError(
                    "officer_not_commissioned", "No commissioned Officer is current."
                )
            existing = _plan_from_post(post)
            if (
                existing
                and existing.get("idempotency_key") == normalized_idempotency_key
            ):
                if existing.get("request_digest") != request_digest:
                    raise RuntimeVerificationPlanError(
                        "idempotency_conflict",
                        "The idempotency key is already bound to a different request.",
                    )
                return _safe_projection(existing, replayed=True)
            if (
                existing
                and str(existing.get("state")) in ACTIVE_STATES
                and not _is_expired(existing, observed_at)
            ):
                raise RuntimeVerificationPlanError(
                    "plan_already_active",
                    "Another verification plan is active for this Officer Post.",
                )

            thread = await conn.fetchrow(
                "SELECT id, project_id, user_id, status, metadata, execution_lane, "
                "runtime_generation, runtime_attach_token, "
                "runtime_retirement_token, agent_id FROM threads "
                "WHERE id = $1 FOR UPDATE",
                post["thread_id"],
            )
            agent = (
                await conn.fetchrow(
                    "SELECT id, thread_id, status, last_heartbeat FROM agents "
                    "WHERE id = $1 FOR UPDATE",
                    thread.get("agent_id") if thread else None,
                )
                if thread and thread.get("agent_id")
                else None
            )
            if thread is None or agent is None:
                raise RuntimeVerificationPlanError(
                    "runtime_not_current", "The commissioned runtime is not live."
                )
            # Lazy import avoids a module cycle: runtime_actor invokes the hook
            # only after this module has loaded.
            from orchestrator.services.runtime_actor import (
                lock_current_officer_runtime_grant,
            )

            grant = await lock_current_officer_runtime_grant(
                conn, post=post, thread=thread, agent=agent, now=observed_at
            )
            if grant is None:
                raise RuntimeVerificationPlanError(
                    "runtime_not_current",
                    "The commissioned runtime has no exact live Officer grant.",
                )
            grant = await conn.fetchrow(
                "SELECT id, caller_kind, credential_generation, "
                "refresh_handoff_ciphertext, revoked_at FROM runtime_actor_grants "
                "WHERE id = $1 FOR UPDATE",
                grant["id"],
            )
            if grant is None:
                raise RuntimeVerificationPlanError(
                    "runtime_not_current", "The Officer grant changed."
                )
            if exercise == "response_loss" and grant.get("refresh_handoff_ciphertext"):
                raise RuntimeVerificationPlanError(
                    "rotation_already_pending",
                    "A refresh handoff is already pending acknowledgement.",
                )

            plan_id = str(uuid4())
            expires_at = observed_at + timedelta(seconds=int(expires_in_seconds))
            progress: dict[str, Any] = {}
            if exercise == "longevity":
                progress = {
                    "windows_completed": 0,
                    "next_window_at": (
                        observed_at
                        + timedelta(seconds=parameters["logical_window_seconds"])
                    ).isoformat(),
                    "authority_refresh_observed": False,
                }
            elif exercise == "response_loss":
                progress = {
                    "losses_committed": 0,
                    "next_loss_not_before": observed_at.isoformat(),
                    "redelivery_observed": False,
                    "acknowledgement_observed": False,
                }
            else:
                progress = {
                    "fault_committed": False,
                    "blocked_maintenance_calls": 0,
                    "recovery_requested_at": None,
                    "recovery_observed": False,
                }
            plan = {
                "version": PLAN_VERSION,
                "plan_id": plan_id,
                "idempotency_key": normalized_idempotency_key,
                "request_digest": request_digest,
                "exercise": exercise,
                "state": "armed",
                "project_id": str(post["project_id"]),
                "thread_id": str(thread["id"]),
                "officer_incarnation": _incarnation(post),
                "agent_id": str(agent["id"]),
                "grant_id": str(grant["id"]),
                "grant_generation": int(grant["credential_generation"]),
                "created_by": normalized_created_by,
                "created_at": observed_at.isoformat(),
                "expires_at": expires_at.isoformat(),
                "last_transition_at": observed_at.isoformat(),
                "attempt_count": 0,
                "max_attempts": max_attempts,
                "parameters": parameters,
                "progress": progress,
            }
            await _save_plan(conn, post, plan)
            return _safe_projection(plan)


async def get_plan(
    db: Any, *, enabled: bool, project_id: str, now: datetime | None = None
) -> dict[str, Any]:
    if not enabled:
        raise RuntimeVerificationPlanError(
            "verification_disabled",
            "Officer runtime verification is disabled for this deployment.",
            status_code=404,
        )
    observed_at = now or datetime.now(timezone.utc)
    async with db.acquire() as conn:
        async with conn.transaction():
            post = await conn.fetchrow(
                "SELECT project_id, thread_id, incarnations, state "
                "FROM project_officers WHERE project_id = $1::uuid FOR UPDATE",
                str(project_id),
            )
            if post is None:
                return {"configured": False}
            plan = _plan_from_post(post)
            if (
                plan
                and str(plan.get("state")) in ACTIVE_STATES
                and _is_expired(plan, observed_at)
            ):
                plan = _terminalize(plan, "expired", observed_at, reason="plan_expired")
                await _save_plan(conn, post, plan)
            elif (
                plan
                and str(plan.get("state")) in ACTIVE_STATES
                and not await _lock_current_plan_scope(
                    conn, post=post, plan=plan, now=observed_at
                )
            ):
                plan = _terminalize(
                    plan, "invalidated", observed_at, reason="binding_changed"
                )
                await _save_plan(conn, post, plan)
            return _safe_projection(plan)


async def transition_plan(
    db: Any,
    *,
    enabled: bool,
    project_id: str,
    plan_id: str,
    action: str,
    actor_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recover or disarm one exact durable plan idempotently."""

    if not enabled:
        raise RuntimeVerificationPlanError(
            "verification_disabled",
            "Officer runtime verification is disabled for this deployment.",
            status_code=404,
        )
    if action not in {"recover", "disarm"}:
        raise RuntimeVerificationPlanError(
            "invalid_transition", "Unsupported plan transition.", status_code=400
        )
    try:
        normalized_project_id = str(UUID(str(project_id)))
        normalized_actor_id = str(UUID(str(actor_id)))
    except ValueError as exc:
        raise RuntimeVerificationPlanError(
            "invalid_identity",
            "Verification identities must be UUIDs.",
            status_code=400,
        ) from exc
    observed_at = now or datetime.now(timezone.utc)
    async with db.acquire() as conn:
        async with conn.transaction():
            post = await conn.fetchrow(
                "SELECT project_id, thread_id, incarnations, state "
                "FROM project_officers WHERE project_id = $1::uuid FOR UPDATE",
                normalized_project_id,
            )
            plan = _plan_from_post(post) if post else {}
            if not plan or str(plan.get("plan_id")) != str(plan_id):
                raise RuntimeVerificationPlanError(
                    "plan_not_found",
                    "Verification plan was not found.",
                    status_code=404,
                )
            if str(plan.get("state")) in TERMINAL_STATES:
                return _safe_projection(plan, replayed=True)
            if _is_expired(plan, observed_at):
                plan = _terminalize(plan, "expired", observed_at, reason="plan_expired")
            elif action == "recover" and plan.get("state") == "recovering":
                # A committed transition whose HTTP response was lost must be
                # replayable without changing its first actor or timestamp.
                return _safe_projection(plan, replayed=True)
            elif action == "disarm":
                disarmed_at = observed_at.isoformat()
                plan = _terminalize(
                    {
                        **plan,
                        "disarmed_by": normalized_actor_id,
                        "disarmed_at": disarmed_at,
                    },
                    "disarmed",
                    observed_at,
                    reason="operator_disarmed",
                )
            elif (
                plan.get("exercise") != "maintenance_failure"
                or plan.get("state") != "faulted"
            ):
                raise RuntimeVerificationPlanError(
                    "recovery_not_available",
                    "Only a faulted maintenance plan can be recovered.",
                )
            elif not await _lock_current_plan_scope(
                conn, post=post, plan=plan, now=observed_at
            ):
                plan = _terminalize(
                    plan, "invalidated", observed_at, reason="binding_changed"
                )
            else:
                progress = _as_dict(plan.get("progress"))
                recovery_requested_at = observed_at.isoformat()
                progress["recovery_requested_at"] = recovery_requested_at
                plan = {
                    **plan,
                    "state": "recovering",
                    "recovery_requested_by": normalized_actor_id,
                    "recovery_requested_at": recovery_requested_at,
                    "progress": progress,
                    "last_transition_at": observed_at.isoformat(),
                }
            assert post is not None
            await _save_plan(conn, post, plan)
            return _safe_projection(plan)


async def prepare_refresh_on_conn(
    conn: Any,
    *,
    post: Any,
    thread: Any,
    agent: Any,
    grant: Any,
    pre_turn: bool,
    now: datetime,
) -> RefreshVerificationDecision:
    """Consume the pre-refresh portion of one exact active plan."""

    plan = _plan_from_post(post)
    if not plan or str(plan.get("state")) not in ACTIVE_STATES:
        return RefreshVerificationDecision()
    if _is_expired(plan, now):
        await _save_plan(
            conn, post, _terminalize(plan, "expired", now, reason="plan_expired")
        )
        return RefreshVerificationDecision()
    if not _scope_matches(plan, post=post, thread=thread, agent=agent, grant=grant):
        await _save_plan(
            conn,
            post,
            _terminalize(plan, "invalidated", now, reason="binding_changed"),
        )
        return RefreshVerificationDecision()
    # All credential/lifecycle authority above is still re-derived even when
    # this is the periodic heartbeat path. Verification itself waits for the
    # persistent loop's post-persistence, pre-provider callback so a failure
    # cannot be consumed early and misrepresented as the requested no-spend
    # turn proof. The phase assertion selects timing only, never identity.
    if not pre_turn:
        return RefreshVerificationDecision()

    attempts = int(plan.get("attempt_count") or 0) + 1
    if attempts > int(plan.get("max_attempts") or 0):
        await _save_plan(
            conn,
            post,
            _terminalize(plan, "exhausted", now, reason="attempt_budget_exhausted"),
        )
        return RefreshVerificationDecision()
    plan["attempt_count"] = attempts
    plan["last_transition_at"] = now.isoformat()
    exercise = str(plan.get("exercise"))
    progress = _as_dict(plan.get("progress"))

    if exercise == "maintenance_failure":
        if plan.get("state") in {"armed", "faulted"}:
            progress["fault_committed"] = True
            progress["blocked_maintenance_calls"] = (
                int(progress.get("blocked_maintenance_calls") or 0) + 1
            )
            plan["state"] = "faulted"
            plan["progress"] = progress
            await _save_plan(conn, post, plan)
            return RefreshVerificationDecision(
                plan_id=str(plan["plan_id"]),
                exercise=exercise,
                inject_maintenance_failure=True,
            )
        await _save_plan(conn, post, plan)
        return RefreshVerificationDecision(
            plan_id=str(plan["plan_id"]), exercise=exercise
        )

    if exercise == "response_loss":
        required = int(_as_dict(plan.get("parameters")).get("response_losses") or 1)
        committed = int(progress.get("losses_committed") or 0)
        if committed < required:
            not_before = _as_utc(progress.get("next_loss_not_before")) or now
            if not_before > now:
                plan["progress"] = progress
                await _save_plan(conn, post, plan)
                return RefreshVerificationDecision(
                    plan_id=str(plan["plan_id"]),
                    exercise=exercise,
                    block_code="verification_response_loss_spacing",
                )
            await _save_plan(conn, post, plan)
            return RefreshVerificationDecision(
                plan_id=str(plan["plan_id"]),
                exercise=exercise,
                force_rotation=committed == 0,
            )

    await _save_plan(conn, post, plan)
    return RefreshVerificationDecision(plan_id=str(plan["plan_id"]), exercise=exercise)


async def finish_refresh_on_conn(
    conn: Any,
    *,
    post: Any,
    thread: Any,
    agent: Any,
    grant: Any,
    decision: RefreshVerificationDecision,
    resulting_generation: int,
    using_previous: bool,
    now: datetime,
) -> bool:
    """Record the committed exchange and decide whether to hide its bearer."""

    if not decision.plan_id:
        return False
    plan = _plan_from_post(post)
    # ``post`` is the originally fetched record. Another helper may have
    # updated the JSONB column, so reload the exact key while retaining the
    # already-held Post lock.
    value = await conn.fetchval(
        "SELECT state->'runtime_actor_verification' FROM project_officers "
        "WHERE project_id = $1",
        post["project_id"],
    )
    plan = _as_dict(value) or plan
    if str(plan.get("plan_id")) != decision.plan_id:
        return False
    exercise = str(plan.get("exercise"))
    progress = _as_dict(plan.get("progress"))

    if exercise == "response_loss":
        required = int(_as_dict(plan.get("parameters")).get("response_losses") or 1)
        committed = int(progress.get("losses_committed") or 0)
        if committed < required:
            committed += 1
            progress["losses_committed"] = committed
            progress["last_loss_committed_at"] = now.isoformat()
            gap = int(
                _as_dict(plan.get("parameters")).get("response_loss_gap_seconds") or 0
            )
            progress["next_loss_not_before"] = (
                now + timedelta(seconds=gap)
            ).isoformat()
            plan["grant_generation"] = int(resulting_generation)
            plan["state"] = "redelivery_pending" if committed >= required else "armed"
            plan["progress"] = progress
            plan["last_transition_at"] = now.isoformat()
            await _save_plan(conn, post, plan)
            return True
        if plan.get("state") == "redelivery_pending":
            progress["redelivery_observed"] = True
            plan["state"] = "awaiting_ack"
        elif plan.get("state") == "awaiting_ack" and not using_previous:
            progress["acknowledgement_observed"] = True
            plan = _terminalize(
                {**plan, "progress": progress},
                "completed",
                now,
                reason="rotation_acknowledged",
            )
            await _save_plan(conn, post, plan)
            return False
        plan["progress"] = progress
        plan["last_transition_at"] = now.isoformat()
        await _save_plan(conn, post, plan)
        return False

    if exercise == "maintenance_failure" and plan.get("state") == "recovering":
        progress["recovery_observed"] = True
        plan = _terminalize(
            {**plan, "progress": progress},
            "completed",
            now,
            reason="runtime_maintenance_recovered",
        )
        await _save_plan(conn, post, plan)
    elif exercise == "longevity" and plan.get("state") == "awaiting_ack":
        progress["authority_refresh_observed"] = True
        plan = _terminalize(
            {**plan, "progress": progress},
            "completed",
            now,
            reason="post_window_authority_refreshed",
        )
        await _save_plan(conn, post, plan)
    return False


async def observe_maintenance_on_conn(
    conn: Any,
    *,
    post: Any,
    thread: Any,
    agent: Any,
    grant: Any,
    now: datetime,
) -> MaintenanceVerificationDecision:
    """Advance one logical longevity window during real watchdog maintenance."""

    plan = _plan_from_post(post)
    if not plan or str(plan.get("state")) not in ACTIVE_STATES:
        return MaintenanceVerificationDecision()
    if _is_expired(plan, now):
        await _save_plan(
            conn, post, _terminalize(plan, "expired", now, reason="plan_expired")
        )
        return MaintenanceVerificationDecision()
    if not _scope_matches(plan, post=post, thread=thread, agent=agent, grant=grant):
        await _save_plan(
            conn,
            post,
            _terminalize(plan, "invalidated", now, reason="binding_changed"),
        )
        return MaintenanceVerificationDecision()
    if plan.get("exercise") == "maintenance_failure" and plan.get("state") == "faulted":
        return MaintenanceVerificationDecision(inject_maintenance_failure=True)
    if plan.get("exercise") != "longevity" or plan.get("state") != "armed":
        return MaintenanceVerificationDecision()
    progress = _as_dict(plan.get("progress"))
    next_window = _as_utc(progress.get("next_window_at"))
    if next_window is None or next_window > now:
        return MaintenanceVerificationDecision()
    completed = int(progress.get("windows_completed") or 0) + 1
    required = int(_as_dict(plan.get("parameters")).get("windows_required") or 2)
    progress["windows_completed"] = completed
    progress["last_window_at"] = now.isoformat()
    window = int(_as_dict(plan.get("parameters")).get("logical_window_seconds") or 60)
    progress["next_window_at"] = (now + timedelta(seconds=window)).isoformat()
    plan["progress"] = progress
    plan["last_transition_at"] = now.isoformat()
    if completed >= required:
        # The next credential-bearing maintenance must still succeed before
        # the plan completes. This produces the requested post-window proof
        # without making list/read tools aware of the verification seam.
        plan["state"] = "awaiting_ack"
    await _save_plan(conn, post, plan)
    return MaintenanceVerificationDecision(force_renewal=True)
