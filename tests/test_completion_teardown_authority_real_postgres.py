"""Real-Postgres proofs for the Gate-3 S36/admission linearization."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.services.completion_finalizer import CompletionEffectRunner
from orchestrator.services.job_completion_commands import (
    CompletionInProgress,
    CompletionTeardownInProgress,
    accept_completion_command,
)
from orchestrator.services.completion_teardown_authority import (
    WORKSPACE_TEARDOWN_EFFECT,
    active_workspace_teardown_authorization,
    authorize_workspace_teardown,
    workspace_teardown_handoff,
)


SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)


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
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=8, timeout=10)
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE completion_effects, completion_finalizer_leases, "
            "job_completion_commands, run_queue, jobs, agents CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


async def _job(
    pg,
    *,
    completion_seq_hwm: int,
    status: str = "completed",
) -> UUID:
    async with pg.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO jobs (
                description, status, execution_lane, completion_seq_hwm
            ) VALUES ('S36 authority test', $2, 'pinned', $1)
            RETURNING id
            """,
            completion_seq_hwm,
            status,
        )


async def _command(
    pg,
    *,
    job_id: UUID,
    report_seq: int,
    state: str = "finalizing",
    owner: str | None = None,
) -> UUID:
    owner = owner or f"owner-{report_seq}"
    async with pg.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO job_completion_commands (
                job_id, report_seq, client_report_id, payload, payload_digest,
                accepted_lease_token, origin, requested_by, state, attempts,
                lease_expires_at, deadline_at, finalizing_by, code_version,
                outcome, finalized_at
            ) VALUES (
                $1, $2, $3, '{}'::jsonb, $4, $2, 'agent', 'S36 test',
                $5, 1,
                CASE WHEN $5 = 'finalizing'
                     THEN now() + interval '10 minutes' END,
                now() + interval '1 day',
                CASE WHEN $5 = 'finalizing' THEN $6 END,
                'job-completion-v1',
                CASE WHEN $5 = 'done'
                     THEN '{"status":"handled"}'::jsonb END,
                CASE WHEN $5 = 'done' THEN now() END
            )
            RETURNING id
            """,
            job_id,
            report_seq,
            uuid4(),
            f"digest-{report_seq}",
            state,
            owner,
        )


async def _effect(
    pg,
    *,
    command_id: UUID,
    job_id: UUID,
    state: str = "pending",
    detail: dict | None = None,
) -> None:
    async with pg.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO completion_effects (
                producer_kind, producer_id, scope_id, effect_name,
                effect_group, state, attempts, intent_at, complete_by,
                completed_at, detail
            ) VALUES (
                'job_completion', $1, $2, $3, 'workspace_teardown', $4,
                1, now(),
                CASE WHEN $4 = 'pending'
                     THEN now() + interval '9 minutes' END,
                CASE WHEN $4 = 'done' THEN now() END,
                $5::jsonb
            )
            """,
            command_id,
            job_id,
            WORKSPACE_TEARDOWN_EFFECT,
            state,
            json.dumps(detail or {}),
        )


async def _status_effect(
    pg,
    *,
    command_id: UUID,
    job_id: UUID,
    new_status: str = "completed",
) -> None:
    """Record the S17 provenance S36 must match under the jobs lock."""

    async with pg.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO completion_effects (
                producer_kind, producer_id, scope_id, effect_name,
                effect_group, state, attempts, intent_at, completed_at, detail
            ) VALUES (
                'job_completion', $1, $2, 'main_status_write',
                'job_disposition', 'done', 1, now(), now(), $3::jsonb
            )
            """,
            command_id,
            job_id,
            json.dumps({"output": {"new_status": new_status}}),
        )


async def _admit_if_s36_clear(conn, *, job_id: UUID) -> tuple[int | None, object]:
    """Model the accept-side sequence while the caller owns jobs FOR UPDATE."""

    async with conn.transaction():
        await conn.fetchrow(
            "SELECT completion_seq_hwm FROM jobs WHERE id=$1 FOR UPDATE", job_id
        )
        active = await active_workspace_teardown_authorization(conn, job_id=str(job_id))
        if active is not None:
            return None, active
        report_seq = await conn.fetchval(
            """
            UPDATE jobs
            SET completion_seq_hwm = completion_seq_hwm + 1
            WHERE id = $1
            RETURNING completion_seq_hwm
            """,
            job_id,
        )
        await conn.execute(
            """
            INSERT INTO job_completion_commands (
                job_id, report_seq, client_report_id, payload, payload_digest,
                accepted_lease_token, origin, requested_by, state,
                deadline_at, code_version
            ) VALUES (
                $1, $2, $3, '{}'::jsonb, $4, 999,
                'agent', 'simulated fresh accept', 'pending',
                now() + interval '1 day', 'job-completion-v1'
            )
            """,
            job_id,
            report_seq,
            uuid4(),
            f"digest-{report_seq}",
        )
        return int(report_seq), None


@pytest.mark.asyncio
async def test_accept_lock_wins_and_old_s36_defers_without_marker(pg):
    job_id = await _job(pg, completion_seq_hwm=1)
    command_id = await _command(pg, job_id=job_id, report_seq=1)
    await _effect(pg, command_id=command_id, job_id=job_id)
    await _status_effect(pg, command_id=command_id, job_id=job_id)

    # Hold admission's jobs-row lock while the S36 authorizer starts.  It must
    # wait, then observe the committed higher HWM instead of installing a
    # marker and performing external work.
    async with pg.acquire() as accept_conn:
        transaction = accept_conn.transaction()
        await transaction.start()
        transaction_open = True
        try:
            await accept_conn.fetchrow(
                "SELECT completion_seq_hwm FROM jobs WHERE id=$1 FOR UPDATE", job_id
            )
            authorization = asyncio.create_task(
                authorize_workspace_teardown(
                    pg,
                    job_id=str(job_id),
                    command_id=str(command_id),
                    owner="owner-1",
                )
            )
            await asyncio.sleep(0.05)
            assert not authorization.done()
            await accept_conn.execute(
                "UPDATE jobs SET completion_seq_hwm=2 WHERE id=$1", job_id
            )
            await accept_conn.execute(
                """
                INSERT INTO job_completion_commands (
                    job_id, report_seq, client_report_id, payload,
                    payload_digest, accepted_lease_token, origin,
                    requested_by, state, deadline_at, code_version
                ) VALUES (
                    $1, 2, $2, '{}'::jsonb, 'digest-2', 2,
                    'agent', 'higher report', 'pending',
                    now() + interval '1 day', 'job-completion-v1'
                )
                """,
                job_id,
                uuid4(),
            )
            await transaction.commit()
            transaction_open = False
        except BaseException:
            if transaction_open:
                await transaction.rollback()
            raise

    decision = await asyncio.wait_for(authorization, timeout=2)
    assert not decision.authorized
    assert decision.report_seq == 1
    assert decision.higher_report_seq == 2
    async with pg.acquire() as conn:
        detail = await conn.fetchval(
            "SELECT detail FROM completion_effects "
            "WHERE producer_id=$1 AND effect_name=$2",
            command_id,
            WORKSPACE_TEARDOWN_EFFECT,
        )
    assert json.loads(detail) == {}


@pytest.mark.asyncio
async def test_authorized_marker_blocks_fresh_accept_after_every_clock_expires(pg):
    job_id = await _job(pg, completion_seq_hwm=1)
    command_id = await _command(pg, job_id=job_id, report_seq=1)
    await _effect(pg, command_id=command_id, job_id=job_id)
    await _status_effect(pg, command_id=command_id, job_id=job_id)

    decision = await authorize_workspace_teardown(
        pg,
        job_id=str(job_id),
        command_id=str(command_id),
        owner="owner-1",
    )
    assert decision.authorized

    async with pg.acquire() as conn:
        report_seq, active = await _admit_if_s36_clear(conn, job_id=job_id)
    assert report_seq is None
    assert active.command_id == str(command_id)
    assert active.marker_report_seq == 1

    # A stopped external callback can outlive every ordinary finalizer clock.
    # Parking/expiry therefore must not make admission consider it safe.
    async with pg.acquire() as conn:
        await conn.execute(
            """
            UPDATE job_completion_commands
            SET state='parked', error_code='operator_hold',
                finalizing_by=NULL, lease_expires_at=NULL,
                deadline_at=now()-interval '1 second'
            WHERE id=$1
            """,
            command_id,
        )
        await conn.execute(
            "UPDATE completion_effects "
            "SET complete_by=now()-interval '1 second' WHERE producer_id=$1",
            command_id,
        )
        report_seq, active = await _admit_if_s36_clear(conn, job_id=job_id)
        hwm = await conn.fetchval(
            "SELECT completion_seq_hwm FROM jobs WHERE id=$1", job_id
        )
    assert report_seq is None
    assert active.command_id == str(command_id)
    assert hwm == 1


@pytest.mark.asyncio
async def test_only_settling_s36_releases_the_active_admission_barrier(pg):
    job_id = await _job(pg, completion_seq_hwm=1)
    command_id = await _command(pg, job_id=job_id, report_seq=1)
    await _effect(pg, command_id=command_id, job_id=job_id)
    await _status_effect(pg, command_id=command_id, job_id=job_id)
    assert (
        await authorize_workspace_teardown(
            pg,
            job_id=str(job_id),
            command_id=str(command_id),
            owner="owner-1",
        )
    ).authorized

    async with pg.acquire() as conn:
        # Completion may drop the marker from detail because state='done' is
        # committed by the same statement, after the external callback has
        # returned. Merely expiring or parking never has this authority.
        await conn.execute(
            """
            UPDATE completion_effects
            SET state='done', complete_by=NULL, completed_at=now(),
                detail='{"output":{"teardown_disposition":"completed"}}'::jsonb
            WHERE producer_id=$1 AND effect_name=$2
            """,
            command_id,
            WORKSPACE_TEARDOWN_EFFECT,
        )
        report_seq, active = await _admit_if_s36_clear(conn, job_id=job_id)
    assert active is None
    assert report_seq == 2


@pytest.mark.asyncio
async def test_real_admission_replays_exact_key_but_rejects_fresh_higher_report(pg):
    async with pg.acquire() as conn:
        agent_id = await conn.fetchval(
            "INSERT INTO agents (config_name, hostname, status) "
            "VALUES ('developer', $1, 'working') RETURNING id",
            f"S36-admission-{uuid4().hex[:8]}",
        )
        job_id = await conn.fetchval(
            """
            INSERT INTO jobs (
                description, status, execution_lane, assigned_agent_id
            ) VALUES ('S36 admission test', 'processing', 'pinned', $1)
            RETURNING id
            """,
            agent_id,
        )
    payload = {
        "should_stop": True,
        "goal_achieved": True,
        "error": None,
        "freeze_data": None,
    }
    report_id = uuid4()
    accepted = await accept_completion_command(
        pg,
        job_id=str(job_id),
        payload=payload,
        lease_token=None,
        agent_id=str(agent_id),
        client_report_id=str(report_id),
        requested_by="S36 real admission test",
    )
    async with pg.acquire() as conn:
        await conn.execute(
            """
            UPDATE job_completion_commands
            SET state='finalizing', attempts=1, finalizing_by='owner-1',
                lease_expires_at=now()+interval '10 minutes'
            WHERE id=$1
            """,
            UUID(accepted.command_id),
        )
    await _effect(
        pg,
        command_id=UUID(accepted.command_id),
        job_id=job_id,
    )
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status='completed' WHERE id=$1",
            job_id,
        )
    await _status_effect(
        pg,
        command_id=UUID(accepted.command_id),
        job_id=job_id,
    )
    assert (
        await authorize_workspace_teardown(
            pg,
            job_id=str(job_id),
            command_id=accepted.command_id,
            owner="owner-1",
        )
    ).authorized

    # The exact-key lookup deliberately precedes the S36 fresh-admission
    # barrier, preserving the established retry response for this command.
    with pytest.raises(CompletionInProgress):
        await accept_completion_command(
            pg,
            job_id=str(job_id),
            payload=payload,
            lease_token=None,
            agent_id=str(agent_id),
            client_report_id=str(report_id),
            requested_by="S36 exact replay",
        )

    with pytest.raises(CompletionTeardownInProgress) as blocked:
        await accept_completion_command(
            pg,
            job_id=str(job_id),
            payload=payload,
            lease_token=None,
            agent_id=str(agent_id),
            client_report_id=str(uuid4()),
            requested_by="S36 fresh higher report",
        )
    assert blocked.value.command_id == accepted.command_id
    assert blocked.value.report_seq == 1
    async with pg.acquire() as conn:
        hwm, count = await conn.fetchrow(
            """
            SELECT job.completion_seq_hwm,
                   (SELECT count(*) FROM job_completion_commands command
                    WHERE command.job_id=job.id) AS command_count
            FROM jobs job WHERE job.id=$1
            """,
            job_id,
        )
    assert hwm == 1
    assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["kubernetes", "vm", "docker"])
async def test_authorization_marker_is_backend_neutral_and_preserves_intent(
    pg, backend
):
    job_id = await _job(pg, completion_seq_hwm=1)
    command_id = await _command(pg, job_id=job_id, report_seq=1)
    intent = {"kind": backend, "identity": f"{backend}-identity"}
    await _effect(
        pg,
        command_id=command_id,
        job_id=job_id,
        detail={"intent": intent},
    )
    await _status_effect(pg, command_id=command_id, job_id=job_id)

    assert (
        await authorize_workspace_teardown(
            pg,
            job_id=str(job_id),
            command_id=str(command_id),
            owner="owner-1",
        )
    ).authorized
    async with pg.acquire() as conn:
        detail = await conn.fetchval(
            "SELECT detail FROM completion_effects "
            "WHERE producer_id=$1 AND effect_name=$2",
            command_id,
            WORKSPACE_TEARDOWN_EFFECT,
        )
    assert json.loads(detail) == {
        "intent": intent,
        "teardown_authorization": {"active": True, "report_seq": 1},
    }


@pytest.mark.asyncio
async def test_retryable_s36_keeps_active_authorization_while_pending(pg):
    job_id = await _job(pg, completion_seq_hwm=1)
    command_id = await _command(pg, job_id=job_id, report_seq=1)
    await _status_effect(pg, command_id=command_id, job_id=job_id)
    runner = CompletionEffectRunner(
        pg,
        command={"id": str(command_id), "job_id": str(job_id)},
        owner="owner-1",
        random_source=lambda: 0.0,
    )

    async def retryable_teardown():
        decision = await authorize_workspace_teardown(
            pg,
            job_id=str(job_id),
            command_id=str(command_id),
            owner="owner-1",
        )
        assert decision.authorized
        return {
            "teardown_disposition": "retry_pending",
            "error": "UID-fenced release outcome is ambiguous",
        }

    output = await runner.run(
        name=WORKSPACE_TEARDOWN_EFFECT,
        group="workspace_teardown",
        callback=retryable_teardown,
        retry_if=lambda result: bool(result.get("error")),
    )
    assert output["teardown_disposition"] == "retry_pending"

    async with pg.acquire() as conn:
        effect = await conn.fetchrow(
            "SELECT state, complete_by, detail FROM completion_effects "
            "WHERE producer_id=$1 AND effect_name=$2",
            command_id,
            WORKSPACE_TEARDOWN_EFFECT,
        )
        active = await active_workspace_teardown_authorization(conn, job_id=str(job_id))
    assert effect["state"] == "pending"
    assert effect["complete_by"] is None
    assert json.loads(effect["detail"])["teardown_authorization"] == {
        "active": True,
        "report_seq": 1,
    }
    assert active is not None
    assert active.command_id == str(command_id)


@pytest.mark.asyncio
async def test_cancel_after_s17_supersedes_before_s36_without_release_or_done(pg):
    job_id = await _job(pg, completion_seq_hwm=1)
    command_id = await _command(pg, job_id=job_id, report_seq=1)
    await _effect(pg, command_id=command_id, job_id=job_id)
    await _status_effect(pg, command_id=command_id, job_id=job_id)
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status='cancelled' WHERE id=$1",
            job_id,
        )

    release = AsyncMock()
    decision = await authorize_workspace_teardown(
        pg,
        job_id=str(job_id),
        command_id=str(command_id),
        owner="owner-1",
    )
    if decision.authorized:
        await release()

    assert not decision.authorized
    assert decision.superseded
    assert not decision.operator_hold
    assert decision.observed_status == "cancelled"
    assert decision.expected_status == "completed"
    release.assert_not_awaited()
    async with pg.acquire() as conn:
        effect = await conn.fetchrow(
            "SELECT state, detail FROM completion_effects "
            "WHERE producer_id=$1 AND effect_name=$2",
            command_id,
            WORKSPACE_TEARDOWN_EFFECT,
        )
        active = await active_workspace_teardown_authorization(conn, job_id=str(job_id))
    assert effect["state"] == "pending"
    assert json.loads(effect["detail"]) == {}
    assert active is None


@pytest.mark.asyncio
async def test_active_marker_status_drift_holds_for_operator_and_stays_pending(pg):
    job_id = await _job(pg, completion_seq_hwm=1)
    command_id = await _command(pg, job_id=job_id, report_seq=1)
    await _effect(pg, command_id=command_id, job_id=job_id)
    await _status_effect(pg, command_id=command_id, job_id=job_id)
    first = await authorize_workspace_teardown(
        pg,
        job_id=str(job_id),
        command_id=str(command_id),
        owner="owner-1",
    )
    assert first.authorized

    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status='cancelled' WHERE id=$1",
            job_id,
        )
    second = await authorize_workspace_teardown(
        pg,
        job_id=str(job_id),
        command_id=str(command_id),
        owner="owner-1",
    )

    assert not second.authorized
    assert not second.superseded
    assert second.operator_hold
    assert second.observed_status == "cancelled"
    assert second.expected_status == "completed"
    async with pg.acquire() as conn:
        effect = await conn.fetchrow(
            "SELECT state, detail FROM completion_effects "
            "WHERE producer_id=$1 AND effect_name=$2",
            command_id,
            WORKSPACE_TEARDOWN_EFFECT,
        )
        active = await active_workspace_teardown_authorization(conn, job_id=str(job_id))
    assert effect["state"] == "pending"
    assert json.loads(effect["detail"])["teardown_authorization"] == {
        "active": True,
        "report_seq": 1,
    }
    assert active is not None
    assert active.command_id == str(command_id)


@pytest.mark.asyncio
async def test_newest_lower_s36_disposition_controls_multi_report_handoff(pg):
    job_id = await _job(pg, completion_seq_hwm=3)
    first = await _command(pg, job_id=job_id, report_seq=1, state="done")
    second = await _command(pg, job_id=job_id, report_seq=2, state="done")
    await _effect(
        pg,
        command_id=first,
        job_id=job_id,
        state="done",
        detail={
            "output": {
                "teardown_disposition": "deferred",
                "higher_report_seq": 2,
            }
        },
    )
    await _effect(
        pg,
        command_id=second,
        job_id=job_id,
        state="done",
        detail={
            "output": {
                "teardown_disposition": "deferred",
                "higher_report_seq": 3,
            }
        },
    )

    for before, source in ((2, 1), (3, 2)):
        handoff = await workspace_teardown_handoff(
            pg, job_id=str(job_id), before_report_seq=before
        )
        assert handoff.required
        assert handoff.source_report_seq == source
        assert handoff.disposition == "deferred"

    # Querying only deferred rows would incorrectly find report 1 here and
    # repeat teardown forever. The immediate predecessor is the authority, and
    # report 2's completion closes the handoff chain.
    async with pg.acquire() as conn:
        await conn.execute(
            """
            UPDATE completion_effects
            SET detail='{"output":{"teardown_disposition":"completed"}}'::jsonb
            WHERE producer_id=$1 AND effect_name=$2
            """,
            second,
            WORKSPACE_TEARDOWN_EFFECT,
        )
    handoff = await workspace_teardown_handoff(
        pg, job_id=str(job_id), before_report_seq=3
    )
    assert not handoff.required
    assert handoff.source_report_seq == 2
    assert handoff.disposition == "completed"


@pytest.mark.asyncio
async def test_intervening_command_without_s36_closes_older_handoff(pg):
    job_id = await _job(pg, completion_seq_hwm=3)
    first = await _command(pg, job_id=job_id, report_seq=1, state="done")
    await _command(pg, job_id=job_id, report_seq=2, state="done")
    await _effect(
        pg,
        command_id=first,
        job_id=job_id,
        state="done",
        detail={
            "output": {
                "teardown_disposition": "deferred",
                "higher_report_seq": 2,
            }
        },
    )

    handoff = await workspace_teardown_handoff(
        pg, job_id=str(job_id), before_report_seq=3
    )
    assert not handoff.required
    assert handoff.source_report_seq == 2
    assert handoff.disposition is None
