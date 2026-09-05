"""M4 — the leader-gated worker-message route reconciler (fake clock).

Logic-level proofs against a mocked DB: SLA escalation delivery, total-timeout
resume with the freeze-generation fence, terminal-job audit-only handling, the
crash-repair leg, and the dead-wake fallback. The FOR UPDATE SKIP LOCKED /
CAS races run against a real Postgres in
tests/test_officer_message_routing_real_postgres.py.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from orchestrator.services import message_route_reconciler as reconciler
from orchestrator.services import message_routing as routing


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _db(**overrides):
    db = MagicMock()
    db.close_message_routes_for_terminal_jobs = AsyncMock(return_value=[])
    db.claim_officer_sla_escalations = AsyncMock(return_value=[])
    db.claim_total_timeout_routes = AsyncMock(return_value=[])
    db.list_timed_out_routes_still_frozen = AsyncMock(return_value=[])
    db.list_routes_needing_user_redelivery = AsyncMock(return_value=[])
    db.list_stale_pending_officer_routes = AsyncMock(return_value=[])
    db.transition_message_route = AsyncMock(return_value=None)
    db.get_job = AsyncMock(return_value=None)
    for key, value in overrides.items():
        setattr(db, key, value)
    return db


def _route(**overrides):
    route = {
        "route_id": str(uuid4()),
        "job_id": str(uuid4()),
        "project_id": str(uuid4()),
        "thread_id": "abc123",
        "state": "pending_officer",
        "blocking": True,
        "officer_thread_id": str(uuid4()),
        "policy_snapshot": {"worker_messages": "officer_first"},
        "officer_deadline": NOW - timedelta(minutes=1),
        "total_deadline": NOW + timedelta(hours=23),
    }
    route.update(overrides)
    return route


def _frozen_job(route, **overrides):
    job = {
        "id": route["job_id"],
        "status": "waiting_for_reply",
        "execution_lane": "pinned",
        "freeze_data": {
            "thread_id": route["thread_id"],
            "route_id": route["route_id"],
            "subject": "Need input",
            "freeze_type": "blocking_message",
        },
        "resolved_config": {"agent": {"communication": {"blocking_timeout_hours": 24}}},
    }
    job.update(overrides)
    return job


class TestTerminalAutoCloseLeg:
    @pytest.mark.asyncio
    async def test_open_routes_of_terminal_jobs_are_closed(self):
        """Leg 0: the backstop for terminal paths with no hook — direct
        status writes (cascade cancels, sweepers) get their open routes
        closed here one tick later."""
        closed = _route(state="closed")
        db = _db(
            close_message_routes_for_terminal_jobs=AsyncMock(return_value=[closed])
        )
        counts = await reconciler.reconcile_message_routes_once(db, now=NOW)
        assert counts["closed_terminal"] == 1
        db.close_message_routes_for_terminal_jobs.assert_awaited_once_with(limit=20)

    @pytest.mark.asyncio
    async def test_close_sweep_failure_is_isolated(self):
        """A failing close sweep must not cost the deadline legs their tick."""
        route = _route()
        db = _db(
            close_message_routes_for_terminal_jobs=AsyncMock(
                side_effect=RuntimeError("x")
            ),
            claim_officer_sla_escalations=AsyncMock(return_value=[route]),
        )
        with patch.object(
            routing, "deliver_route_to_user", AsyncMock(return_value=True)
        ):
            counts = await reconciler.reconcile_message_routes_once(db, now=NOW)
        assert counts["closed_terminal"] == 0
        assert counts["sla_escalated"] == 1


class TestOfficerSlaLeg:
    @pytest.mark.asyncio
    async def test_expired_sla_escalates_and_delivers(self):
        route = _route()
        db = _db(claim_officer_sla_escalations=AsyncMock(return_value=[route]))
        deliver = AsyncMock(return_value=True)
        with patch.object(routing, "deliver_route_to_user", deliver):
            counts = await reconciler.reconcile_message_routes_once(db, now=NOW)
        assert counts["sla_escalated"] == 1
        assert counts["sla_delivered"] == 1
        db.claim_officer_sla_escalations.assert_awaited_once_with(now=NOW, limit=20)
        assert deliver.await_args.kwargs["reason"] == "officer_sla_expired"

    @pytest.mark.asyncio
    async def test_failed_delivery_leaves_redelivery_to_next_tick(self):
        route = _route()
        db = _db(claim_officer_sla_escalations=AsyncMock(return_value=[route]))
        with patch.object(
            routing, "deliver_route_to_user", AsyncMock(return_value=False)
        ):
            counts = await reconciler.reconcile_message_routes_once(db, now=NOW)
        assert counts["sla_escalated"] == 1
        assert counts["sla_delivered"] == 0

    @pytest.mark.asyncio
    async def test_fake_clock_before_deadline_claims_nothing(self):
        """The claim query owns the deadline comparison; the reconciler only
        hands it the clock. Prove the clock is passed through verbatim."""
        db = _db()
        early = NOW - timedelta(minutes=30)
        await reconciler.reconcile_message_routes_once(db, now=early)
        assert db.claim_officer_sla_escalations.await_args.kwargs["now"] == early
        assert db.claim_total_timeout_routes.await_args.kwargs["now"] == early


class TestTotalTimeoutLeg:
    @pytest.mark.asyncio
    async def test_timed_out_route_resumes_with_no_answer_reply(self):
        route = _route(state="user_direct", total_deadline=NOW - timedelta(minutes=1))
        job = _frozen_job(route)
        db = _db(
            claim_total_timeout_routes=AsyncMock(return_value=[route]),
            get_job=AsyncMock(return_value=job),
        )
        resume = AsyncMock(return_value=True)
        counts = await reconciler.reconcile_message_routes_once(
            db, now=NOW, resume_job=resume
        )
        assert counts["timed_out"] == 1
        assert counts["resumed"] == 1
        kwargs = resume.await_args.kwargs
        assert kwargs["expected_status"] == "waiting_for_reply"
        assert "no answer arrived" in kwargs["feedback"].lower()
        assert route["thread_id"] in kwargs["feedback"]
        assert "conservative" in kwargs["feedback"]

    @pytest.mark.asyncio
    async def test_terminal_job_gets_only_the_route_audit_transition(self):
        route = _route()
        job = _frozen_job(route, status="cancelled")
        db = _db(
            claim_total_timeout_routes=AsyncMock(return_value=[route]),
            get_job=AsyncMock(return_value=job),
        )
        resume = AsyncMock(return_value=True)
        counts = await reconciler.reconcile_message_routes_once(
            db, now=NOW, resume_job=resume
        )
        assert counts["timed_out"] == 1
        assert counts["resumed"] == 0
        resume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generation_fence_skips_a_refrozen_job(self):
        """A job re-frozen by a LATER blocking message must not be resumed
        against the old route's deadline."""
        route = _route()
        job = _frozen_job(route)
        job["freeze_data"]["route_id"] = str(uuid4())  # a newer generation
        db = _db(
            claim_total_timeout_routes=AsyncMock(return_value=[route]),
            get_job=AsyncMock(return_value=job),
        )
        resume = AsyncMock(return_value=True)
        counts = await reconciler.reconcile_message_routes_once(
            db, now=NOW, resume_job=resume
        )
        assert counts["timed_out"] == 1
        assert counts["resumed"] == 0
        resume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_answer_racing_the_deadline_unblocks_once(self):
        """The answer won the job CAS first: the job is no longer frozen, so
        the reconciler records the timeout and touches nothing else."""
        route = _route()
        job = _frozen_job(route, status="processing", freeze_data=None)
        db = _db(
            claim_total_timeout_routes=AsyncMock(return_value=[route]),
            get_job=AsyncMock(return_value=job),
        )
        resume = AsyncMock(return_value=True)
        counts = await reconciler.reconcile_message_routes_once(
            db, now=NOW, resume_job=resume
        )
        assert counts["resumed"] == 0
        resume.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lost_resume_cas_is_not_counted_resumed(self):
        route = _route()
        db = _db(
            claim_total_timeout_routes=AsyncMock(return_value=[route]),
            get_job=AsyncMock(return_value=_frozen_job(route)),
        )
        resume = AsyncMock(return_value=False)
        counts = await reconciler.reconcile_message_routes_once(
            db, now=NOW, resume_job=resume
        )
        assert counts["timed_out"] == 1
        assert counts["resumed"] == 0

    @pytest.mark.asyncio
    async def test_string_freeze_data_is_parsed(self):
        route = _route()
        job = _frozen_job(route)
        job["freeze_data"] = json.dumps(job["freeze_data"])
        db = _db(
            claim_total_timeout_routes=AsyncMock(return_value=[route]),
            get_job=AsyncMock(return_value=job),
        )
        resume = AsyncMock(return_value=True)
        counts = await reconciler.reconcile_message_routes_once(
            db, now=NOW, resume_job=resume
        )
        assert counts["resumed"] == 1


class TestRepairLegs:
    @pytest.mark.asyncio
    async def test_crash_between_cas_and_resume_is_healed(self):
        """Leg 2b: a timed_out route whose job is STILL frozen on the same
        generation gets its resume retried until it lands."""
        route = _route(state="timed_out")
        db = _db(
            list_timed_out_routes_still_frozen=AsyncMock(return_value=[route]),
            get_job=AsyncMock(return_value=_frozen_job(route)),
        )
        resume = AsyncMock(return_value=True)
        counts = await reconciler.reconcile_message_routes_once(
            db, now=NOW, resume_job=resume
        )
        assert counts["repair_resumed"] == 1
        resume.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_owed_user_delivery_is_redelivered(self):
        escalated = _route(state="escalated_to_user")
        direct = _route(state="user_direct")
        db = _db(
            list_routes_needing_user_redelivery=AsyncMock(
                return_value=[escalated, direct]
            )
        )
        deliver = AsyncMock(return_value=True)
        with patch.object(routing, "deliver_route_to_user", deliver):
            counts = await reconciler.reconcile_message_routes_once(db, now=NOW)
        assert counts["redelivered"] == 2
        reasons = [call.kwargs["reason"] for call in deliver.await_args_list]
        assert reasons == ["officer_sla_expired", "delivery_repair"]

    @pytest.mark.asyncio
    async def test_dead_wake_intent_falls_back_to_user(self):
        """§5.1: a lost officer wake ⇒ delivery_failed ⇒ immediate user
        fallback through the escalation path."""
        route = _route()
        failed = {**route, "state": "delivery_failed"}
        db = _db(
            list_stale_pending_officer_routes=AsyncMock(return_value=[route]),
            transition_message_route=AsyncMock(return_value=failed),
        )
        escalate = AsyncMock(return_value={"escalated": True, "delivered": True})
        with patch.object(routing, "escalate_route", escalate):
            counts = await reconciler.reconcile_message_routes_once(db, now=NOW)
        assert counts["delivery_failed"] == 1
        cas = db.transition_message_route.await_args.kwargs
        assert cas["to_state"] == "delivery_failed"
        assert cas["expected_states"] == ["pending_officer"]
        assert escalate.await_args.kwargs["reason"] == "officer_delivery_failed"
        assert escalate.await_args.kwargs["expected_states"] == ("delivery_failed",)

    @pytest.mark.asyncio
    async def test_lost_delivery_failed_cas_does_not_escalate(self):
        route = _route()
        db = _db(
            list_stale_pending_officer_routes=AsyncMock(return_value=[route]),
            transition_message_route=AsyncMock(return_value=None),
        )
        escalate = AsyncMock()
        with patch.object(routing, "escalate_route", escalate):
            counts = await reconciler.reconcile_message_routes_once(db, now=NOW)
        assert counts["delivery_failed"] == 0
        escalate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_leg_failures_are_isolated(self):
        """One leg raising must not cost the others their tick."""
        route = _route(state="timed_out")
        db = _db(
            claim_officer_sla_escalations=AsyncMock(side_effect=RuntimeError("x")),
            list_timed_out_routes_still_frozen=AsyncMock(return_value=[route]),
            get_job=AsyncMock(return_value=_frozen_job(route)),
        )
        resume = AsyncMock(return_value=True)
        counts = await reconciler.reconcile_message_routes_once(
            db, now=NOW, resume_job=resume
        )
        assert counts["repair_resumed"] == 1


class TestLoop:
    @pytest.mark.asyncio
    async def test_loop_ticks_and_stops_on_shutdown(self):
        db = _db()
        shutdown = asyncio.Event()

        async def _stop_soon():
            await asyncio.sleep(0.05)
            shutdown.set()

        with patch.object(reconciler, "TICK_SECONDS", 0.01):
            await asyncio.gather(
                reconciler.message_route_reconciler_loop(db, shutdown),
                _stop_soon(),
            )
        assert db.claim_officer_sla_escalations.await_count >= 1
