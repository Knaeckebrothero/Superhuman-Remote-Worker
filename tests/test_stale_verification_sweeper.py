"""Unit tests for the stale verification-subjob sweeper (D2).

Covers the pure tick, the shutdown-aware loop, and the PostgresDB helper's
wire-level contract (mocked connection — the SQL's runtime behavior is verified
on the dev cluster, there is no test DB here, matching test_admin_providers_db.py
and test_postgres_advisory_lock.py). See
knowledge-history/done/preemption_before_first_checkpoint_replays_job_opening.md.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.database.postgres import PostgresDB
from orchestrator.services.stale_verification_sweeper import (
    _sweep_tick,
    stale_verification_sweeper_loop,
)


def _make_db(mock_conn):
    """Build a PostgresDB whose acquire() yields a mocked connection."""
    db = PostgresDB.__new__(PostgresDB)
    db._pool = MagicMock()
    db._connection_string = "test"
    db._queries = {}

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    db.acquire = _acquire
    return db


class TestSweepTick:
    @pytest.mark.asyncio
    async def test_runs_both_steps_and_returns_counts(self):
        db = AsyncMock()
        db.cancel_stale_verification_subjobs = AsyncMock(return_value=3)
        db.unstick_reviewing_parents = AsyncMock(return_value=[])
        notifier = AsyncMock()

        cancelled, unstuck = await _sweep_tick(
            db, stale_hours=6, grace_minutes=30, notifier=notifier
        )

        assert (cancelled, unstuck) == (3, 0)
        db.cancel_stale_verification_subjobs.assert_awaited_once_with(6)
        db.unstick_reviewing_parents.assert_awaited_once_with(30)
        notifier.record_review_returned.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_routes_stateless_candidates_through_queue_aware_callback(self):
        db = AsyncMock()
        db.cancel_stale_verification_subjobs = AsyncMock(return_value=2)
        db.list_stale_stateless_verification_subjobs = AsyncMock(
            return_value=["critic-a", "critic-b"]
        )
        db.unstick_reviewing_parents = AsyncMock(return_value=[])
        cancel_stateless = AsyncMock(side_effect=(True, False))

        cancelled, unstuck = await _sweep_tick(
            db,
            stale_hours=9,
            grace_minutes=30,
            notifier=AsyncMock(),
            stateless_cancel_fn=cancel_stateless,
        )

        assert (cancelled, unstuck) == (3, 0)
        db.list_stale_stateless_verification_subjobs.assert_awaited_once_with(9)
        assert cancel_stateless.await_args_list[0].args == ("critic-a",)
        assert cancel_stateless.await_args_list[0].kwargs == {"stale_hours": 9}
        assert cancel_stateless.await_args_list[1].args == ("critic-b",)

    @pytest.mark.asyncio
    async def test_notifies_each_unstuck_parent(self):
        db = AsyncMock()
        db.cancel_stale_verification_subjobs = AsyncMock(return_value=0)
        db.unstick_reviewing_parents = AsyncMock(
            return_value=[
                {"id": "p1", "user_id": "u1", "config_name": "scholar"},
                {"id": "p2", "user_id": "u2", "config_name": "developer"},
            ]
        )
        notifier = AsyncMock()

        cancelled, unstuck = await _sweep_tick(
            db, stale_hours=6, grace_minutes=30, notifier=notifier
        )

        assert (cancelled, unstuck) == (0, 2)
        assert notifier.record_review_returned.await_count == 2
        first = notifier.record_review_returned.await_args_list[0].kwargs
        assert first == {
            "user_id": "u1",
            "job_id": "p1",
            "config_name": "scholar",
        }

    @pytest.mark.asyncio
    async def test_notify_failure_does_not_abort_tick(self):
        db = AsyncMock()
        db.cancel_stale_verification_subjobs = AsyncMock(return_value=0)
        db.unstick_reviewing_parents = AsyncMock(
            return_value=[{"id": "p1", "user_id": "u1", "config_name": "scholar"}]
        )
        notifier = AsyncMock()
        notifier.record_review_returned = AsyncMock(
            side_effect=RuntimeError("smtp down")
        )

        # Must swallow the notify error and still report the un-stuck count.
        cancelled, unstuck = await _sweep_tick(
            db, stale_hours=6, grace_minutes=30, notifier=notifier
        )
        assert (cancelled, unstuck) == (0, 1)


class TestSweeperLoop:
    @pytest.mark.asyncio
    async def test_no_tick_when_shutdown_preset(self):
        db = AsyncMock()
        db.cancel_stale_verification_subjobs = AsyncMock(return_value=0)
        db.unstick_reviewing_parents = AsyncMock(return_value=[])
        shutdown = asyncio.Event()
        shutdown.set()

        await asyncio.wait_for(
            stale_verification_sweeper_loop(db, shutdown), timeout=1.0
        )
        db.cancel_stale_verification_subjobs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_one_tick_then_exits_on_shutdown(self):
        db = AsyncMock()
        shutdown = asyncio.Event()

        def _count_then_stop(*_args):
            shutdown.set()  # stop the loop after this single tick
            return 2

        db.cancel_stale_verification_subjobs = AsyncMock(side_effect=_count_then_stop)
        db.unstick_reviewing_parents = AsyncMock(return_value=[])

        await asyncio.wait_for(
            stale_verification_sweeper_loop(db, shutdown), timeout=1.0
        )
        db.cancel_stale_verification_subjobs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tick_exception_does_not_kill_loop(self):
        db = AsyncMock()
        shutdown = asyncio.Event()

        def _raise_then_stop(*_args):
            shutdown.set()  # ensure the loop exits after the failed tick
            raise RuntimeError("boom")

        db.cancel_stale_verification_subjobs = AsyncMock(side_effect=_raise_then_stop)
        db.unstick_reviewing_parents = AsyncMock(return_value=[])

        # Must swallow the tick error and exit cleanly on shutdown, not raise.
        await asyncio.wait_for(
            stale_verification_sweeper_loop(db, shutdown), timeout=1.0
        )


class TestCancelStaleVerificationSubjobs:
    @pytest.mark.asyncio
    async def test_executes_update_with_stale_hours_and_parses_count(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 3")
        db = _make_db(conn)

        count = await db.cancel_stale_verification_subjobs(stale_hours=12)

        assert count == 3
        conn.execute.assert_awaited_once()
        args = conn.execute.await_args.args
        sql = args[0]
        # The discriminator + the staleness fallback must be in the predicate,
        # and stale_hours must bind as $1.
        assert "verification_target" in sql
        assert "make_interval" in sql
        assert "parent.status IN" in sql
        assert args[1] == 12

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_rows(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 0")
        db = _make_db(conn)

        assert await db.cancel_stale_verification_subjobs() == 0


class TestUnstickReviewingParents:
    @pytest.mark.asyncio
    async def test_executes_update_with_grace_and_returns_rows(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(
            return_value=[
                {"id": "p1", "user_id": "u1", "config_name": "scholar"},
            ]
        )
        db = _make_db(conn)

        rows = await db.unstick_reviewing_parents(grace_minutes=30)

        assert rows == [{"id": "p1", "user_id": "u1", "config_name": "scholar"}]
        conn.fetch.assert_awaited_once()
        args = conn.fetch.await_args.args
        sql = args[0]
        # Flips reviewing → pending_review, gated by the grace floor and the
        # ledger-aware "no live critic + newest round unrecorded" clause;
        # grace binds as $1. The old "every child failed/cancelled" clause
        # is gone (a `completed` critic is now normal — see
        # test_unstick_no_longer_requires_all_children_failed) — replaced by
        # a check against the durable verification_rounds ledger, not
        # dropped outright.
        assert "status = 'pending_review'" in sql
        assert "p.status = 'reviewing'" in sql
        assert "make_interval" in sql
        assert "verification_target" in sql
        assert "status NOT IN ('failed', 'cancelled')" not in sql
        assert "verification_rounds" in sql
        assert "RETURNING" in sql
        assert args[1] == 30

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_rows(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db(conn)

        assert await db.unstick_reviewing_parents() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("wallclock", [False, True])
    async def test_commands_flag_defers_only_to_live_finalizer_route(self, wallclock):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db(conn)

        if wallclock:
            await db.unstick_reviewing_parents_wallclock(
                60, completion_commands_enabled=True
            )
        else:
            await db.unstick_reviewing_parents(30, completion_commands_enabled=True)

        sql = conn.fetch.await_args.args[0]
        assert "job_completion_sweep_exclusions" in sql
        assert "completion_route.route = 'stand_down'" in sql
        assert "_completion_control_claim" in sql


def test_unstick_no_longer_requires_all_children_failed():
    """Every round now leaves a `completed` critic behind (Task 8 froze both
    verdicts as ordinary `completed` subjobs instead of parking a 'returned'
    critic in 'waiting'), so the old "all children failed/cancelled" condition
    could never be met again after round 1 — a target whose round-2 critic
    died would sit in `reviewing` forever.
    """
    from orchestrator.database.postgres import PostgresDB

    sql = PostgresDB._UNSTICK_REVIEWING_SQL
    assert "status NOT IN ('failed', 'cancelled')" not in sql
    assert "verification_rounds" in sql
    assert "_stateless_cancel_cleanup_pending" in sql


class TestUnstickReviewingParentsWallclock:
    """The wall-clock arm — fix direction 2 of
    knowledge-history/done/rejected_verdict_livelocks_critic_and_wedges_parent.md.
    """

    @pytest.mark.asyncio
    async def test_sql_targets_live_critics_with_distinct_message(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(
            return_value=[{"id": "p1", "user_id": "u1", "config_name": "scholar"}]
        )
        db = _make_db(conn)

        rows = await db.unstick_reviewing_parents_wallclock(60)

        assert rows == [{"id": "p1", "user_id": "u1", "config_name": "scholar"}]
        args = conn.fetch.await_args.args
        sql = args[0]
        # The complement of the dead-critic arm: requires a LIVE critic
        # (EXISTS, not NOT EXISTS) so the two arms stay disjoint, and carries
        # the distinct did-not-render-a-verdict message.
        assert "status = 'pending_review'" in sql
        assert "p.status = 'reviewing'" in sql
        assert "make_interval" in sql
        assert "AND EXISTS" in sql
        assert "NOT EXISTS" not in sql
        assert "critic did not render a verdict in" in sql
        assert "RETURNING" in sql
        assert args[1] == 60

    def test_both_arms_share_the_live_critic_status_set(self):
        """The EXISTS/NOT EXISTS predicates must agree on what "live" means,
        or a critic status could fall through both arms (or into both)."""
        from orchestrator.database.postgres import PostgresDB

        live_set = "'created', 'processing', 'paused', 'waiting',"
        assert live_set in PostgresDB._UNSTICK_REVIEWING_SQL
        assert live_set in PostgresDB._UNSTICK_REVIEWING_WALLCLOCK_SQL
        assert "_stateless_cancel_cleanup_pending" in PostgresDB._UNSTICK_REVIEWING_SQL
        assert (
            "_stateless_cancel_cleanup_pending"
            in PostgresDB._UNSTICK_REVIEWING_WALLCLOCK_SQL
        )

    @pytest.mark.asyncio
    async def test_tick_runs_wallclock_arm_when_enabled(self):
        db = AsyncMock()
        db.cancel_stale_verification_subjobs = AsyncMock(return_value=0)
        db.unstick_reviewing_parents = AsyncMock(return_value=[])
        db.unstick_reviewing_parents_wallclock = AsyncMock(
            return_value=[{"id": "p9", "user_id": "u9", "config_name": "scholar"}]
        )
        notifier = AsyncMock()

        cancelled, unstuck = await _sweep_tick(
            db,
            stale_hours=6,
            grace_minutes=30,
            notifier=notifier,
            wallclock_minutes=60,
        )

        assert (cancelled, unstuck) == (0, 1)
        db.unstick_reviewing_parents_wallclock.assert_awaited_once_with(60)
        notifier.record_review_returned.assert_awaited_once_with(
            user_id="u9", job_id="p9", config_name="scholar"
        )

    @pytest.mark.asyncio
    async def test_tick_skips_wallclock_arm_by_default_and_when_disabled(self):
        for wallclock in (None, 0):
            db = AsyncMock()
            db.cancel_stale_verification_subjobs = AsyncMock(return_value=0)
            db.unstick_reviewing_parents = AsyncMock(return_value=[])
            notifier = AsyncMock()

            kwargs = {}
            if wallclock is not None:
                kwargs["wallclock_minutes"] = wallclock
            cancelled, unstuck = await _sweep_tick(
                db, stale_hours=6, grace_minutes=30, notifier=notifier, **kwargs
            )

            assert (cancelled, unstuck) == (0, 0)
            db.unstick_reviewing_parents_wallclock.assert_not_awaited()


def test_sweeper_reaps_waiting_critics_and_still_reaps_paused():
    """`waiting` critics are orphans of the retired inter-round parking
    mechanism (knowledge-base/knowledge/issues/stale_critic_waiting_status_escapes_reaper.md) and
    must now be reaped.

    Deviation from the task-10 brief's literal example test: the brief
    asserted ``"'paused'" not in sql`` (i.e. remove 'paused' entirely). That
    contradicts this plan's own explicit constraint — "Never add 'paused'...
    Removing paused from the existing set is equally wrong" — and it would
    silently gut the LLM-outage/cooldown exemption a few lines below in the
    same query, which tests specifically for ``j.status = 'paused'``. Orphan
    recovery legitimately re-dispatches critics through `paused`, and "paused
    too long ⇒ dead" was evaluated and rejected as a standalone signal in a
    prior design (knowledge-base/knowledge/features/llm_outage_subjob_resilience.md) — there is no
    positive deadness signal for `paused` the way there now is for `waiting`
    (a status nothing legitimately parks in any more). So `paused` must
    survive in the predicate, and this test asserts presence, not absence.
    """
    from orchestrator.database.postgres import PostgresDB

    sql = PostgresDB._CANCEL_STALE_VERIFICATION_SQL
    assert "'waiting'" in sql
    assert "'paused'" in sql
    assert "execution_lane = 'pinned'" in sql
