"""Real-Postgres proof of the generalized completion ownership invariant.

The invariant is intentionally job-status agnostic.  An unfinished completion
command, or a stateless terminal-queue owner gap, has exactly one of four
durable authority domains:

* the accepted agent term while the command is still pending;
* one claimed finalizer command (including that command's effect leases);
* one current-attempt completion-sweep action; or
* the durable operator-review marker for a rescued stateless owner gap.

This test builds the M1--M3 collision families plus the terminal/absent queue
gap and derives the actor count from persisted fences.  Scenario labels are
used only to state the expected owner; they are not inputs to the census.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB, _completion_control_active_sql
from orchestrator.services.completion_lifecycle import CompletionLifecycleOwnership
from orchestrator.services.completion_sweep_router import (
    STATELESS_OWNER_GAP_CODE,
    CompletionSweepRouter,
)
from orchestrator.services.job_completion_commands import (
    COMPLETION_CODE_VERSION,
    accept_completion_command,
)
from orchestrator.services.project_loop_sweeper import _heal_wedged_loop
from shared.worker_queue import claim_worker_batch


SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)

_UNFINISHED_STATES = ("pending", "finalizing", "parked")

# An agent's old jobs-row assignment is not authority after a finalizer claims
# the command.  Restricting agent terms to pending commands models that durable
# handoff.  A finalizer command and all of its live effect rows are one actor,
# so finalizer_terms is distinct by command rather than by effect.
_OWNERSHIP_CENSUS_SQL = f"""
WITH unfinished AS (
    SELECT command.id AS command_id,
           command.job_id,
           command.state,
           command.attempts,
           command.accepted_agent_id,
           command.accepted_lease_token,
           command.finalizing_by,
           command.lease_expires_at
    FROM job_completion_commands AS command
    WHERE command.state = ANY($1::text[])
),
owner_gaps AS (
    SELECT job.id AS job_id,
           CASE WHEN job.status='processing'
                THEN 'active'
                ELSE 'operator_parked'
           END AS gap_state
    FROM jobs AS job
    LEFT JOIN run_queue AS queue ON queue.unit_id=job.id
    WHERE (
        job.status='processing'
        AND job.execution_lane='stateless'
        AND job.assigned_agent_id IS NULL
        AND (
            job.lease_expires_at IS NULL
            OR job.lease_expires_at <= clock_timestamp()
        )
        AND NOT ({_completion_control_active_sql("job.context")})
        AND (
            queue.unit_id IS NULL
            OR (queue.unit_kind='worker_batch' AND queue.state='done')
        )
        AND NOT EXISTS (
            SELECT 1
            FROM job_completion_commands AS command
            WHERE command.job_id=job.id
              AND command.state=ANY($1::text[])
        )
    ) OR (
        job.status='pending_review'
        AND job.execution_lane='stateless'
        AND job.error_details->>'code'=$2::text
    )
),
subject_jobs AS (
    SELECT DISTINCT job_id FROM unfinished
    UNION
    SELECT job_id FROM owner_gaps
),
agent_terms AS (
    SELECT DISTINCT unfinished.job_id,
           'agent:pinned:' || agent.id::text AS term
    FROM unfinished
    JOIN jobs AS job ON job.id = unfinished.job_id
    JOIN agents AS agent
      ON agent.id = unfinished.accepted_agent_id
     AND agent.id = job.assigned_agent_id
     AND agent.current_job_id = job.id
    WHERE unfinished.state = 'pending'
      AND job.execution_lane = 'pinned'
      AND job.lease_expires_at > now()
      AND agent.status IN ('working', 'draining')

    UNION

    SELECT DISTINCT unfinished.job_id,
           'agent:stateless:' || queue.lease_token::text AS term
    FROM unfinished
    JOIN jobs AS job ON job.id = unfinished.job_id
    JOIN run_queue AS queue
      ON queue.unit_id = unfinished.job_id
     AND queue.lease_token = unfinished.accepted_lease_token
    WHERE unfinished.state = 'pending'
      AND job.execution_lane = 'stateless'
      AND queue.unit_kind = 'worker_batch'
      AND queue.state = 'leased'
      AND queue.leased_by IS NOT NULL
      AND queue.leased_until > now()
),
finalizer_terms AS (
    SELECT DISTINCT unfinished.job_id,
           'finalizer:' || unfinished.command_id::text AS term
    FROM unfinished
    WHERE (
        unfinished.state = 'finalizing'
        AND unfinished.finalizing_by IS NOT NULL
        AND unfinished.lease_expires_at > now()
    ) OR EXISTS (
        SELECT 1
        FROM completion_effects AS effect
        WHERE effect.producer_kind = 'job_completion'
          AND effect.producer_id = unfinished.command_id
          AND effect.state = 'pending'
          AND effect.complete_by > now()
    )
),
sweep_terms AS (
    SELECT DISTINCT unfinished.job_id,
           'sweep:' || action.job_id::text || ':' || action.attempt::text AS term
    FROM unfinished
    JOIN job_completion_sweep_actions AS action
      ON action.command_id = unfinished.command_id
     AND action.command_attempt = unfinished.attempts
    WHERE action.state IN ('pending', 'claimed')
),
operator_terms AS (
    SELECT owner_gaps.job_id,
           'operator:stateless-owner-gap'::text AS term
    FROM owner_gaps
    WHERE owner_gaps.gap_state='operator_parked'
),
actor_terms AS (
    SELECT job_id, term, 'agent'::text AS actor_kind FROM agent_terms
    UNION ALL
    SELECT job_id, term, 'finalizer'::text AS actor_kind FROM finalizer_terms
    UNION ALL
    SELECT job_id, term, 'sweep'::text AS actor_kind FROM sweep_terms
    UNION ALL
    SELECT job_id, term, 'operator'::text AS actor_kind FROM operator_terms
)
SELECT job.id,
       job.description,
       job.status::text AS status,
       count(actor_terms.term)::int AS owner_count,
       count(actor_terms.term) FILTER (
           WHERE actor_terms.actor_kind = 'agent'
       )::int AS agent_count,
       count(actor_terms.term) FILTER (
           WHERE actor_terms.actor_kind = 'finalizer'
       )::int AS finalizer_count,
       count(actor_terms.term) FILTER (
           WHERE actor_terms.actor_kind = 'sweep'
       )::int AS sweep_count,
       count(actor_terms.term) FILTER (
           WHERE actor_terms.actor_kind = 'operator'
       )::int AS operator_count,
       COALESCE(
           array_agg(actor_terms.term ORDER BY actor_terms.term)
               FILTER (WHERE actor_terms.term IS NOT NULL),
           ARRAY[]::text[]
       ) AS actor_terms
FROM subject_jobs
JOIN jobs AS job ON job.id = subject_jobs.job_id
LEFT JOIN actor_terms ON actor_terms.job_id = job.id
GROUP BY job.id, job.description, job.status
ORDER BY job.description
"""


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as postgres:
        yield postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture(scope="module")
async def _schema_applied(pg_dsn):
    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(SCHEMA_FILE.read_text())
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def pg(pg_dsn, _schema_applied):
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=12, timeout=10)
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE job_completion_sweep_actions, completion_effects, "
            "completion_finalizer_leases, job_completion_commands, "
            "run_queue, jobs, agents CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


def _db_from_pool(pool) -> PostgresDB:
    db = PostgresDB.__new__(PostgresDB)

    @asynccontextmanager
    async def acquire():
        async with pool.acquire() as conn:
            yield conn

    db.acquire = acquire
    return db


async def _agent(conn, *, label: str) -> UUID:
    return await conn.fetchval(
        "INSERT INTO agents (config_name, hostname, status) "
        "VALUES ('developer', $1, 'working') RETURNING id",
        f"ownership-{label}-{uuid4().hex[:8]}",
    )


async def _job(
    conn,
    *,
    label: str,
    status: str,
    lane: str = "pinned",
    agent_id: UUID | None = None,
    lease_seconds: float | None = None,
    context: dict | None = None,
    updated_seconds_ago: float = 0,
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO jobs (
            description, status, execution_lane, assigned_agent_id,
            lease_expires_at, context, updated_at
        ) VALUES (
            $1, $2, $3, $4,
            CASE WHEN $5::float8 IS NULL THEN NULL
                 ELSE now()+make_interval(secs => $5::float8) END,
            $6::jsonb,
            now()-make_interval(secs => $7::float8)
        )
        RETURNING id
        """,
        label,
        status,
        lane,
        agent_id,
        lease_seconds,
        json.dumps(context or {}),
        updated_seconds_ago,
    )


async def _operator_command(
    conn,
    *,
    job_id: UUID,
    accepted_status: str,
    state: str,
    attempts: int = 0,
    lease_seconds: float | None = None,
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO job_completion_commands (
            job_id, report_seq, client_report_id, payload, payload_digest,
            accepted_job_status, origin, requested_by, state, attempts,
            max_attempts, run_after, lease_expires_at, deadline_at,
            finalizing_by, code_version, error_code
        ) VALUES (
            $1, 1, $2, '{}'::jsonb, $3,
            $4, 'operator', 'ownership-invariant', $5, $6,
            5, now()-interval '1 second',
            CASE WHEN $7::float8 IS NULL THEN NULL
                 ELSE now()+make_interval(secs => $7::float8) END,
            now()+interval '1 hour',
            CASE WHEN $5='finalizing' THEN 'ownership-finalizer' END,
            $8,
            CASE WHEN $5='parked' THEN 'operator_required' END
        )
        RETURNING id
        """,
        job_id,
        uuid4(),
        f"ownership-{uuid4()}",
        accepted_status,
        state,
        attempts,
        lease_seconds,
        COMPLETION_CODE_VERSION,
    )


async def _mark_finalizing(
    conn,
    command_id: UUID,
    *,
    owner: str,
    lease_seconds: float = 600,
) -> None:
    await conn.execute(
        """
        UPDATE job_completion_commands
        SET state='finalizing', attempts=attempts+1,
            finalizing_by=$2,
            lease_expires_at=now()+make_interval(secs => $3::float8)
        WHERE id=$1
        """,
        command_id,
        owner,
        lease_seconds,
    )


async def _live_effect(conn, *, command_id: UUID, job_id: UUID) -> None:
    await conn.execute(
        """
        INSERT INTO completion_effects (
            producer_kind, producer_id, scope_id, effect_name,
            effect_group, state, attempts, intent_at, complete_by
        ) VALUES (
            'job_completion', $1, $2, 'verification_critic_spawn',
            'verification', 'pending', 1, now(), now()+interval '5 minutes'
        )
        """,
        command_id,
        job_id,
    )


async def _accept_pinned(pg, *, job_id: UUID, agent_id: UUID) -> UUID:
    result = await accept_completion_command(
        pg,
        job_id=str(job_id),
        payload={
            "should_stop": True,
            "goal_achieved": True,
            "error": None,
            "freeze_data": None,
        },
        lease_token=None,
        agent_id=str(agent_id),
        client_report_id=str(uuid4()),
        requested_by="ownership-invariant",
    )
    return UUID(result.command_id)


async def _accept_stateless(pg, *, job_id: UUID, lease_token: int) -> UUID:
    result = await accept_completion_command(
        pg,
        job_id=str(job_id),
        payload={
            "should_stop": True,
            "goal_achieved": True,
            "error": None,
            "freeze_data": None,
        },
        lease_token=lease_token,
        agent_id=None,
        client_report_id=str(uuid4()),
        requested_by="ownership-invariant",
    )
    assert result.queue_terminalized
    return UUID(result.command_id)


async def _census(pg) -> list[dict]:
    async with pg.acquire() as conn:
        rows = await conn.fetch(
            _OWNERSHIP_CENSUS_SQL,
            list(_UNFINISHED_STATES),
            STATELESS_OWNER_GAP_CODE,
        )
    return [dict(row) for row in rows]


@pytest.mark.asyncio
async def test_m1_m3_collision_matrix_has_exactly_one_owner(pg, monkeypatch):
    """Every persisted M1--M3 collision snapshot has one authority term."""

    db = _db_from_pool(pg)
    finalizer = SimpleNamespace(finalize_command=AsyncMock())
    router = CompletionSweepRouter(
        pg,
        finalizer,
        claimant_id="ownership-invariant-router",
    )
    lifecycle = CompletionLifecycleOwnership(db, router)
    expected: dict[str, tuple[str, str]] = {}

    # M1: a just-accepted pinned report remains owned by the exact agent until
    # the command claim transfers authority to a finalizer term.
    async with pg.acquire() as conn:
        agent_id = await _agent(conn, label="m1-agent")
        agent_job = await _job(
            conn,
            label="m1-agent-live-processing",
            status="processing",
            agent_id=agent_id,
            lease_seconds=600,
        )
        await conn.execute(
            "UPDATE agents SET current_job_id=$2 WHERE id=$1",
            agent_id,
            agent_job,
        )
    await _accept_pinned(pg, job_id=agent_job, agent_id=agent_id)
    expected["m1-agent-live-processing"] = ("processing", "agent")

    # M1 live/expired/parked routing consumes the production view.  Competing
    # nudges for actionable rows must materialize one current-attempt action.
    async with pg.acquire() as conn:
        live_job = await _job(
            conn, label="m1-finalizer-live-processing", status="processing"
        )
        await _operator_command(
            conn,
            job_id=live_job,
            accepted_status="processing",
            state="finalizing",
            attempts=1,
            lease_seconds=600,
        )
        expired_job = await _job(
            conn,
            label="m1-finalizer-expired-processing",
            status="processing",
            lease_seconds=-60,
        )
        await _operator_command(
            conn,
            job_id=expired_job,
            accepted_status="processing",
            state="finalizing",
            attempts=1,
            lease_seconds=-1,
        )
        parked_job = await _job(conn, label="m1-parked-paused", status="paused")
        await _operator_command(
            conn,
            job_id=parked_job,
            accepted_status="processing",
            state="parked",
            attempts=5,
        )

    live_route = await router.enqueue_job(live_job, source="m1-live")
    assert live_route.disposition == "stand_down"
    assert live_route.route == "stand_down"
    assert not (
        await db.recover_expired_lease_jobs(completion_commands_enabled=True)
    ).changed

    expired_routes = await asyncio.gather(
        router.enqueue_job(expired_job, source="m1-expired-a"),
        router.enqueue_job(expired_job, source="m1-expired-b"),
    )
    assert {result.route for result in expired_routes} == {"resume_finalizer"}
    assert {result.disposition for result in expired_routes} == {"queued"}
    parked_routes = await asyncio.gather(
        router.enqueue_job(parked_job, source="m1-parked-a"),
        router.enqueue_job(parked_job, source="m1-parked-b"),
    )
    assert {result.route for result in parked_routes} == {"alert_only"}
    assert {result.disposition for result in parked_routes} == {"queued"}
    expected.update(
        {
            "m1-finalizer-live-processing": ("processing", "finalizer"),
            "m1-finalizer-expired-processing": ("processing", "sweep"),
            "m1-parked-paused": ("paused", "sweep"),
        }
    )

    # M2 catastrophe direction: real stateless admission consumes the old
    # queue lease.  Even if a caller illegally re-queues it during a human
    # resume, the command-aware claim predicate skips it while finalization is
    # live.  The same status-independent CAS contract covers cancel-after-
    # accept: the finalizer term remains the sole actor until it supersedes.
    async with pg.acquire() as conn:
        claim_job = await _job(
            conn,
            label="m2-claim-barrier-paused",
            status="processing",
            lane="stateless",
        )
        await conn.execute(
            """
            INSERT INTO run_queue (
                unit_id, unit_kind, state, lease_token, leased_by,
                leased_until, input_seq, consumed_seq
            ) VALUES (
                $1, 'worker_batch', 'leased', 7, 'old-worker',
                now()+interval '10 minutes', 1, 0
            )
            """,
            claim_job,
        )
    claim_command = await _accept_stateless(pg, job_id=claim_job, lease_token=7)
    async with pg.acquire() as conn:
        await _mark_finalizing(
            conn, claim_command, owner="m2-live-finalizer", lease_seconds=600
        )
        await conn.execute(
            "UPDATE jobs SET status='paused' WHERE id=$1",
            claim_job,
        )
        await conn.execute(
            "UPDATE run_queue SET state='queued', run_after=now() WHERE unit_id=$1",
            claim_job,
        )
        queue_before = await conn.fetchrow(
            "SELECT state, lease_token, leased_by, leased_until "
            "FROM run_queue WHERE unit_id=$1",
            claim_job,
        )

        cancel_job = await _job(
            conn, label="m2-cancelled-after-accept", status="cancelled"
        )
        await _operator_command(
            conn,
            job_id=cancel_job,
            accepted_status="processing",
            state="finalizing",
            attempts=1,
            lease_seconds=600,
        )

    assert (
        await claim_worker_batch(
            pg,
            pod_name="forbidden-successor",
            affinity_grace_seconds=0,
            completion_commands_enabled=True,
        )
        is None
    )
    assert (await router.enqueue_job(cancel_job, source="m2-cancel")).route == (
        "stand_down"
    )
    async with pg.acquire() as conn:
        queue_after = await conn.fetchrow(
            "SELECT state, lease_token, leased_by, leased_until "
            "FROM run_queue WHERE unit_id=$1",
            claim_job,
        )
    assert dict(queue_after) == dict(queue_before)
    expected.update(
        {
            "m2-claim-barrier-paused": ("paused", "finalizer"),
            "m2-cancelled-after-accept": ("cancelled", "finalizer"),
        }
    )

    # M3 critic deference: the production watchdog consumes only stand_down
    # from the shared route and must leave an old reviewing parent untouched.
    async with pg.acquire() as conn:
        critic_job = await _job(
            conn,
            label="m3-critic-reviewing",
            status="reviewing",
            updated_seconds_ago=7_200,
        )
        critic_command = await _operator_command(
            conn,
            job_id=critic_job,
            accepted_status="processing",
            state="finalizing",
            attempts=1,
            lease_seconds=600,
        )
        await _live_effect(conn, command_id=critic_command, job_id=critic_job)
    assert (
        await db.unstick_reviewing_parents(30, completion_commands_enabled=True) == []
    )
    expected["m3-critic-reviewing"] = ("reviewing", "finalizer")

    # M3 loop deference: drive the actual torn-loop heal helper.  A 600s-old
    # empty-barrier signature is otherwise actionable, but a live finalizer
    # owns it and the DB heal mutation is never attempted.
    loop_id = uuid4()
    async with pg.acquire() as conn:
        loop_job = await _job(
            conn,
            label="m3-loop-waiting",
            status="waiting",
            context={
                "loop_id": str(loop_id),
                "loop_role": "developer",
                "loop_iteration": 4,
                "loop_seq_index": 1,
                "loop_remaining": 3,
            },
        )
        await _operator_command(
            conn,
            job_id=loop_job,
            accepted_status="waiting",
            state="finalizing",
            attempts=1,
            lease_seconds=600,
        )
    heal = AsyncMock(return_value=True)
    monkeypatch.setattr(db, "heal_project_loop_stage", heal)
    assert (
        await _heal_wedged_loop(
            db,
            {
                "id": str(loop_id),
                "updated_at": datetime.now(UTC) - timedelta(minutes=20),
            },
            completion_commands_enabled=True,
        )
        is None
    )
    heal.assert_not_awaited()
    expected["m3-loop-waiting"] = ("waiting", "finalizer")

    # M3 lifecycle deference is action-time, under the jobs lock.  A live
    # terminal command stands down; an expired command routes the same effect
    # rows through one durable action and never grants local teardown authority.
    async with pg.acquire() as conn:
        lifecycle_live_job = await _job(
            conn, label="m3-lifecycle-completed", status="completed"
        )
        await _operator_command(
            conn,
            job_id=lifecycle_live_job,
            accepted_status="processing",
            state="finalizing",
            attempts=1,
            lease_seconds=600,
        )
        lifecycle_expired_job = await _job(
            conn, label="m3-lifecycle-failed", status="failed"
        )
        await _operator_command(
            conn,
            job_id=lifecycle_expired_job,
            accepted_status="processing",
            state="finalizing",
            attempts=1,
            lease_seconds=-1,
        )

    async with lifecycle.action(
        str(lifecycle_live_job),
        source="m3-lifecycle-live",
        resource_kind="workspace",
        resource_identity="pod-uid-live",
        expected_status="completed",
        expected_lane="pinned",
    ) as permit:
        assert not permit.local
        assert permit.decision.disposition == "stand_down"
        assert permit.decision.route == "stand_down"

    async with lifecycle.action(
        str(lifecycle_expired_job),
        source="m3-lifecycle-expired",
        resource_kind="workspace",
        resource_identity="pod-uid-expired",
        expected_status="failed",
        expected_lane="pinned",
    ) as permit:
        assert not permit.local
        assert permit.decision.disposition == "routed"
        assert permit.decision.route == "resume_finalizer"
    expected.update(
        {
            "m3-lifecycle-completed": ("completed", "finalizer"),
            "m3-lifecycle-failed": ("failed", "sweep"),
        }
    )

    # M3 owner-gap net: a terminal worker queue with no unfinished command has
    # zero authority before the rescue.  Project-loop membership deliberately
    # does not suppress the route: auto-completing would guess an outcome and
    # re-enqueueing would execute the answered turn again.  The exceptional
    # pending_review marker is instead one inert operator owner.
    async with pg.acquire() as conn:
        owner_gap_job = await _job(
            conn,
            label="m3-stateless-owner-gap-loop",
            status="processing",
            lane="stateless",
            context={"loop_id": str(uuid4()), "loop_iteration": 7},
        )
        await conn.execute(
            """
            INSERT INTO run_queue (
                unit_id, unit_kind, state, lease_token,
                input_seq, consumed_seq
            ) VALUES ($1, 'worker_batch', 'done', 4, 2, 2)
            """,
            owner_gap_job,
        )

    before_rescue = {row["description"]: row for row in await _census(pg)}
    assert before_rescue["m3-stateless-owner-gap-loop"]["owner_count"] == 0
    assert before_rescue["m3-stateless-owner-gap-loop"]["actor_terms"] == []

    rescued = await router.park_stateless_owner_gaps_once()
    assert [(row.job_id, row.queue_state) for row in rescued] == [
        (str(owner_gap_job), "done")
    ]
    expected["m3-stateless-owner-gap-loop"] = ("pending_review", "operator")

    rows = await _census(pg)
    actual = {row["description"]: row for row in rows}
    assert set(actual) == set(expected)
    assert {row["status"] for row in rows} == {
        "processing",
        "reviewing",
        "completed",
        "failed",
        "cancelled",
        "paused",
        "pending_review",
        "waiting",
    }

    zero_owner = [row["description"] for row in rows if row["owner_count"] == 0]
    double_owner = [row["description"] for row in rows if row["owner_count"] > 1]
    assert zero_owner == []
    assert double_owner == []
    for label, (status, owner_kind) in expected.items():
        row = actual[label]
        assert row["status"] == status
        assert row["owner_count"] == 1
        assert row[f"{owner_kind}_count"] == 1
        assert len(row["actor_terms"]) == 1

    async with pg.acquire() as conn:
        deduplicated = await conn.fetch(
            """
            SELECT job.description, count(*)::int AS action_count
            FROM job_completion_sweep_actions AS action
            JOIN jobs AS job ON job.id=action.job_id
            GROUP BY job.description
            ORDER BY job.description
            """
        )
    assert {row["description"]: row["action_count"] for row in deduplicated} == {
        "m1-finalizer-expired-processing": 1,
        "m1-parked-paused": 1,
        "m3-lifecycle-failed": 1,
    }
    finalizer.finalize_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_off_commandless_worker_path_makes_invariant_vacuous(pg):
    """With command admission dark, legacy claim behavior has no M5 subject."""

    async with pg.acquire() as conn:
        job_id = await _job(
            conn,
            label="flag-off-commandless",
            status="paused",
            lane="stateless",
        )
        await conn.execute(
            """
            INSERT INTO run_queue (
                unit_id, unit_kind, state, priority, run_after, input_seq
            ) VALUES ($1, 'worker_batch', 'queued', 10, now(), 1)
            """,
            job_id,
        )

    claimed = await claim_worker_batch(
        pg,
        pod_name="legacy-flag-off-worker",
        affinity_grace_seconds=0,
        completion_commands_enabled=False,
    )
    assert claimed is not None
    assert claimed.unit_id == job_id
    assert await _census(pg) == []
