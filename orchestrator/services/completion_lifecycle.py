"""Completion-aware ownership for workspace and VM lifecycle actions.

The generic lifecycle reconciler and the completion finalizer are deliberately
separate leadership domains.  A read-before-delete veto therefore cannot make
their external work mutually exclusive: a completion command can be accepted
after the read and before the delete.

This module uses the jobs row as the shared linearization point.  While holding
that lock it consumes the authoritative completion routing view.  An unfinished
command is routed to the existing durable sweep action; otherwise lifecycle
installs the same fixed-shape, leased marker used by human controls.  Completion
acceptance, dispatch, successor claims, and controls already refuse that marker.

Feature gating belongs to callers.  No default-off caller should construct this
service, which preserves the legacy path's zero reads of Gate-3 relations and
zero reserved context metadata.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal, Mapping
from uuid import uuid4

from database.postgres import (
    _completion_control_active_sql,
    _completion_control_owned_active_sql,
)
from services.completion_control import (
    COMPLETION_CONTROL_CLAIM_KEY,
    COMPLETION_CONTROL_CLAIM_VERSION,
)

logger = logging.getLogger(__name__)


# Lifecycle snapshots are bounded well below the completion finalizer's
# 15-minute S36 timeout.  Keep a generous first term and renew it while the
# process is alive; a crash retains the marker only until this bounded expiry.
LIFECYCLE_ACTION_LEASE_SECONDS = 2 * 60 * 60
LIFECYCLE_ACTION_HEARTBEAT_SECONDS = 60
LIFECYCLE_EXTERNAL_TIMEOUT_SECONDS = 15 * 60

LifecycleDisposition = Literal[
    "legacy",
    "claimed",
    "stand_down",
    "routed",
    "missing_job",
    "unknown",
]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _bounded(value: Any, *, label: str, limit: int) -> str:
    text = str(value).strip()
    if not text or len(text) > limit:
        raise ValueError(f"{label} must contain 1-{limit} characters")
    return text


@dataclass(frozen=True, slots=True)
class LifecycleActionClaim:
    """One exact, DB-clock-leased lifecycle ownership term."""

    job_id: str
    claim_id: str
    source: str
    resource_kind: str
    resource_identity: str
    expected_status: str
    expected_lane: str
    job_context: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LifecycleRouteDecision:
    """List/action-time result from the shared completion route."""

    job_id: str
    disposition: LifecycleDisposition
    route: str | None = None
    command_id: str | None = None
    claim: LifecycleActionClaim | None = None
    reason: str | None = None

    @property
    def local(self) -> bool:
        return self.disposition in {"legacy", "claimed", "missing_job"}

    @property
    def deferred(self) -> bool:
        return not self.local


class LifecycleActionPermit:
    """Mutable completion signal yielded around one external action section."""

    __slots__ = ("decision", "completed", "lost", "skip_reason")

    def __init__(self, decision: LifecycleRouteDecision) -> None:
        self.decision = decision
        self.completed = False
        self.lost = asyncio.Event()
        self.skip_reason: str | None = None

    @property
    def local(self) -> bool:
        return (
            self.decision.local and self.skip_reason is None and not self.lost.is_set()
        )

    @property
    def claim(self) -> LifecycleActionClaim | None:
        return self.decision.claim

    def complete(self) -> None:
        """Declare that the claimed external section settled conclusively."""

        self.completed = True

    def skip(self, reason: str, *, settled: bool = False) -> None:
        """Suppress local I/O, optionally releasing a conclusively safe claim."""

        self.skip_reason = _bounded(reason, label="lifecycle skip reason", limit=128)
        if settled:
            self.completed = True


class CompletionLifecycleOwnership:
    """Route unfinished commands and claim command-free lifecycle work."""

    def __init__(
        self,
        db: Any,
        router: Any,
        *,
        lease_seconds: float = LIFECYCLE_ACTION_LEASE_SECONDS,
        heartbeat_seconds: float = LIFECYCLE_ACTION_HEARTBEAT_SECONDS,
    ) -> None:
        if router is None:
            raise ValueError("completion lifecycle routing requires a router")
        if float(lease_seconds) <= 0:
            raise ValueError("lifecycle action lease must be positive")
        if float(heartbeat_seconds) <= 0 or float(heartbeat_seconds) >= float(
            lease_seconds
        ):
            raise ValueError("lifecycle heartbeat must be positive and below its lease")
        self.db = db
        self.router = router
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)

    @staticmethod
    def _route_decision(job_id: str, row: Any) -> LifecycleRouteDecision:
        route = str(row["route"])
        return LifecycleRouteDecision(
            job_id=job_id,
            disposition="stand_down" if route == "stand_down" else "routed",
            route=route,
            command_id=str(row["command_id"]),
        )

    async def _enqueue(self, decision: LifecycleRouteDecision, *, source: str) -> None:
        if decision.disposition == "routed":
            await self.router.enqueue_job(decision.job_id, source=source)

    async def classify(self, job_id: str, *, source: str) -> LifecycleRouteDecision:
        """Classify one list-time candidate and nudge actionable command routes.

        This is deliberately not action authority.  :meth:`action` repeats the
        classification under the jobs lock and either routes or installs the
        marker immediately before external work.
        """

        canonical = str(job_id)
        clean_source = _bounded(source, label="lifecycle source", limit=64)
        try:
            async with self.db.acquire() as conn:
                async with conn.transaction():
                    await conn.fetchrow(
                        "SELECT unit_id FROM run_queue "
                        "WHERE unit_id=$1::uuid FOR UPDATE",
                        canonical,
                    )
                    row = await conn.fetchrow(
                        f"""
                        SELECT status::text AS status, execution_lane, context,
                               ({_completion_control_active_sql("context")})
                                   AS control_active
                        FROM jobs
                        WHERE id=$1::uuid
                        FOR UPDATE
                        """,
                        canonical,
                    )
                    if row is None:
                        return LifecycleRouteDecision(canonical, "missing_job")
                    route = await conn.fetchrow(
                        """
                        SELECT command_id, route
                        FROM job_completion_sweep_exclusions
                        WHERE job_id=$1::uuid
                        """,
                        canonical,
                    )
                    if route is not None:
                        decision = self._route_decision(canonical, route)
                    elif bool(row["control_active"]):
                        decision = LifecycleRouteDecision(
                            canonical,
                            "stand_down",
                            reason="active_control_claim",
                        )
                    else:
                        decision = LifecycleRouteDecision(canonical, "legacy")
        except Exception:
            logger.exception(
                "Completion lifecycle classification failed for job %s; "
                "preserving resource",
                canonical,
            )
            return LifecycleRouteDecision(
                canonical, "unknown", reason="classification_failed"
            )
        await self._enqueue(decision, source=clean_source)
        return decision

    async def _claim(
        self,
        job_id: str,
        *,
        source: str,
        resource_kind: str,
        resource_identity: str,
        expected_status: str,
        expected_lane: str,
    ) -> LifecycleRouteDecision:
        canonical = str(job_id)
        claim_id = str(uuid4())
        async with self.db.acquire() as conn:
            async with conn.transaction():
                # Global lock order shared with command accept and controls.
                await conn.fetchrow(
                    "SELECT unit_id FROM run_queue WHERE unit_id=$1::uuid FOR UPDATE",
                    canonical,
                )
                job = await conn.fetchrow(
                    f"""
                    SELECT status::text AS status, execution_lane, context,
                           ({_completion_control_active_sql("context")})
                               AS control_active
                    FROM jobs
                    WHERE id=$1::uuid
                    FOR UPDATE
                    """,
                    canonical,
                )
                if job is None:
                    # A deleted row cannot accept a new command.  Its detached
                    # resources remain the legacy orphan sweep's responsibility.
                    return LifecycleRouteDecision(canonical, "missing_job")
                if (
                    str(job["status"]) != expected_status
                    or str(job["execution_lane"] or "pinned") != expected_lane
                ):
                    return LifecycleRouteDecision(
                        canonical, "stand_down", reason="job_world_state_changed"
                    )

                # Command routing is decided before considering a new lifecycle
                # claim.  This is the critical expired-lease row: enqueue the
                # exact command attempt and never redispatch or tear down here.
                route = await conn.fetchrow(
                    """
                    SELECT command_id, route
                    FROM job_completion_sweep_exclusions
                    WHERE job_id=$1::uuid
                    """,
                    canonical,
                )
                if route is not None:
                    return self._route_decision(canonical, route)
                if bool(job["control_active"]):
                    return LifecycleRouteDecision(
                        canonical, "stand_down", reason="active_control_claim"
                    )

                updated = await conn.fetchrow(
                    f"""
                    UPDATE jobs
                    SET context=jsonb_set(
                            COALESCE(context, '{{}}'::jsonb),
                            '{{{COMPLETION_CONTROL_CLAIM_KEY}}}',
                            jsonb_build_object(
                                'version', $2::int,
                                'claim_id', $3::text,
                                'source', $4::text,
                                'expected_status', $5::text,
                                'expected_lane', $6::text,
                                'fence_kind', $7::text,
                                'fence_value', $8::text,
                                'claimed_at', to_jsonb(now()),
                                'expires_epoch', to_jsonb(
                                    extract(epoch FROM now()) + $9::float8
                                )
                            ),
                            true
                        ),
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=$1::uuid
                      AND status::text=$5::text
                      AND COALESCE(execution_lane, 'pinned')=$6::text
                      AND NOT ({_completion_control_active_sql("context")})
                      AND NOT EXISTS (
                          SELECT 1
                          FROM job_completion_sweep_exclusions AS route
                          WHERE route.job_id=jobs.id
                      )
                    RETURNING context
                    """,
                    canonical,
                    COMPLETION_CONTROL_CLAIM_VERSION,
                    claim_id,
                    source,
                    expected_status,
                    expected_lane,
                    resource_kind,
                    resource_identity,
                    self.lease_seconds,
                )
                if updated is None:
                    return LifecycleRouteDecision(
                        canonical, "stand_down", reason="claim_race_lost"
                    )
                claim = LifecycleActionClaim(
                    job_id=canonical,
                    claim_id=claim_id,
                    source=source,
                    resource_kind=resource_kind,
                    resource_identity=resource_identity,
                    expected_status=expected_status,
                    expected_lane=expected_lane,
                    job_context=_json_object(job["context"]),
                )
                return LifecycleRouteDecision(canonical, "claimed", claim=claim)

    async def _renew(self, claim: LifecycleActionClaim) -> bool:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE jobs
                SET context=jsonb_set(
                        context,
                        '{{{COMPLETION_CONTROL_CLAIM_KEY},expires_epoch}}',
                        to_jsonb(
                            extract(epoch FROM now()) + $3::float8
                        ),
                        false
                    ),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=$1::uuid
                  AND ({_completion_control_owned_active_sql("context", "$2")})
                  AND status::text=$4::text
                  AND COALESCE(execution_lane, 'pinned')=$5::text
                RETURNING id
                """,
                claim.job_id,
                claim.claim_id,
                self.lease_seconds,
                claim.expected_status,
                claim.expected_lane,
            )
        return row is not None

    async def _heartbeat(
        self,
        permit: LifecycleActionPermit,
        stopped: asyncio.Event,
    ) -> None:
        claim = permit.claim
        assert claim is not None
        while not stopped.is_set() and not permit.lost.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=self.heartbeat_seconds)
                return
            except TimeoutError:
                pass
            try:
                renewed = await self._renew(claim)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Lifecycle claim heartbeat failed job=%s claim=%s; "
                    "external result authority lost",
                    claim.job_id,
                    claim.claim_id,
                )
                permit.lost.set()
                return
            if not renewed:
                logger.warning(
                    "Lifecycle claim heartbeat lost exact term job=%s claim=%s",
                    claim.job_id,
                    claim.claim_id,
                )
                permit.lost.set()
                return

    async def _clear(self, claim: LifecycleActionClaim) -> bool:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE jobs
                SET context=COALESCE(context, '{{}}'::jsonb)
                            - '{COMPLETION_CONTROL_CLAIM_KEY}',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=$1::uuid
                  AND ({_completion_control_owned_active_sql("context", "$2")})
                RETURNING id
                """,
                claim.job_id,
                claim.claim_id,
            )
        return row is not None

    async def refresh(self, permit: LifecycleActionPermit) -> bool:
        """Renew and prove the exact term immediately before external I/O."""

        claim = permit.claim
        if claim is None:
            return permit.local
        if permit.lost.is_set():
            return False
        try:
            renewed = await self._renew(claim)
        except Exception:
            logger.exception(
                "Lifecycle pre-I/O term refresh failed job=%s claim=%s",
                claim.job_id,
                claim.claim_id,
            )
            permit.lost.set()
            return False
        if not renewed:
            permit.lost.set()
            return False
        return True

    @asynccontextmanager
    async def action(
        self,
        job_id: str,
        *,
        source: str,
        resource_kind: str,
        resource_identity: str,
        expected_status: str,
        expected_lane: str,
    ) -> AsyncIterator[LifecycleActionPermit]:
        """Route or lease one complete external lifecycle action section.

        A successful caller must call :meth:`LifecycleActionPermit.complete`.
        Exceptions, cancellation, ambiguous I/O, and a missing completion signal
        intentionally retain the exact marker until its bounded expiry.
        """

        clean_source = _bounded(source, label="lifecycle source", limit=64)
        clean_kind = _bounded(resource_kind, label="lifecycle resource kind", limit=32)
        clean_identity = _bounded(
            resource_identity, label="lifecycle resource identity", limit=256
        )
        clean_status = _bounded(
            expected_status, label="lifecycle expected status", limit=64
        )
        if expected_lane not in {"pinned", "stateless"}:
            raise ValueError("lifecycle expected lane must be pinned or stateless")

        try:
            decision = await self._claim(
                str(job_id),
                source=clean_source,
                resource_kind=clean_kind,
                resource_identity=clean_identity,
                expected_status=clean_status,
                expected_lane=expected_lane,
            )
        except Exception:
            logger.exception(
                "Completion lifecycle claim failed for job %s; preserving resource",
                job_id,
            )
            decision = LifecycleRouteDecision(
                str(job_id), "unknown", reason="claim_failed"
            )
        await self._enqueue(decision, source=clean_source)
        permit = LifecycleActionPermit(decision)
        claim = permit.claim
        if claim is None:
            yield permit
            return

        stopped = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(permit, stopped))
        try:
            yield permit
        finally:
            stopped.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if permit.completed and not permit.lost.is_set():
                try:
                    if not await self._clear(claim):
                        permit.lost.set()
                        logger.warning(
                            "Lifecycle action completed after losing exact clear "
                            "authority job=%s claim=%s",
                            claim.job_id,
                            claim.claim_id,
                        )
                except Exception:
                    logger.exception(
                        "Lifecycle exact claim clear failed job=%s claim=%s; "
                        "bounded marker retained",
                        claim.job_id,
                        claim.claim_id,
                    )
            else:
                logger.warning(
                    "Lifecycle action did not settle conclusively; retaining "
                    "bounded claim job=%s claim=%s source=%s",
                    claim.job_id,
                    claim.claim_id,
                    claim.source,
                )


__all__ = [
    "CompletionLifecycleOwnership",
    "LIFECYCLE_ACTION_HEARTBEAT_SECONDS",
    "LIFECYCLE_ACTION_LEASE_SECONDS",
    "LIFECYCLE_EXTERNAL_TIMEOUT_SECONDS",
    "LifecycleActionClaim",
    "LifecycleActionPermit",
    "LifecycleRouteDecision",
]
