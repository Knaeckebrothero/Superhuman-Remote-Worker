"""Test the resume re-queue write (``queue_job_for_resume``).

Locks the dispatcher-visibility contract for the HUMAN-reply resume paths:
``_internal_resume_job`` (reply to a blocking agent message, urgent interrupt,
LLM-triage interrupt) and ``resume_job``'s ``_queue_for_dispatch`` (explicit
Resume when no agent is free). Both flip a frozen job to 'paused' — and
``get_dispatchable_jobs`` requires ``freeze_data IS NULL``, so keeping the
blob left the job paused-but-invisible: the dispatcher never selected it
again and nothing else was scheduled to change its state.

For the blocking-message arm the freeze is a PRECONDITION (the reply router
only resumes when ``freeze_data.thread_id`` matches), so every reply to a
blocking message wedged its job. Mirrors tests/test_delegation_resume_claim.py.
See docs/issues/blocking_message_reply_keeps_freeze_data.md.
"""

import json
from uuid import UUID

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB

JOB = "9b760af1-a693-4652-a50c-aca46542ab48"
AGENT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

BLOCKING_FREEZE = {
    "job_id": JOB,
    "status": "waiting_for_reply",
    "subject": "BLOCKED: managed shell tab reset required",
    "thread_id": "d78144",
    "timestamp": "2026-07-18T23:24:06.901206+00:00",
    "freeze_type": "blocking_message",
}


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def db(pg_dsn):
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        # Minimal jobs table, but with every column get_dispatchable_jobs
        # touches so the dispatcher-visibility contract can be asserted here.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id uuid PRIMARY KEY,
                description text,
                status text NOT NULL,
                config_name text,
                config_override jsonb,
                assigned_agent_id uuid,
                user_id uuid,
                project_id uuid,
                parent_job_id uuid,
                priority int DEFAULT 5,
                runner_kind text,
                branch_name text,
                context jsonb,
                freeze_data jsonb,
                created_at timestamptz DEFAULT now(),
                updated_at timestamptz DEFAULT now()
            )
            """
        )
        await conn.execute("TRUNCATE jobs")
        await conn.execute(
            """
            INSERT INTO jobs (id, status, assigned_agent_id, context, freeze_data)
            VALUES ($1, 'waiting_for_reply', $2, $3::jsonb, $4::jsonb)
            """,
            UUID(JOB),
            UUID(AGENT),
            json.dumps({"loop_id": "b15dc68f", "loop_iteration": 3}),
            json.dumps(BLOCKING_FREEZE),
        )
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_reply_resume_clears_freeze_so_dispatcher_sees_job(db):
    """The re-queue must clear freeze_data, or the reply resumes nothing."""
    assert await db.queue_job_for_resume(JOB, {"queued_feedback": "fixed, resume"})

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, assigned_agent_id, freeze_data, context "
            "FROM jobs WHERE id = $1",
            UUID(JOB),
        )
    assert row["status"] == "paused"
    assert row["assigned_agent_id"] is None  # agent cleared for re-dispatch
    assert row["freeze_data"] is None

    dispatchable = await db.get_dispatchable_jobs()
    assert [str(j["id"]) for j in dispatchable] == [JOB], (
        "a job resumed by a human reply must satisfy the dispatcher's "
        "freeze_data IS NULL contract"
    )


@pytest.mark.asyncio
async def test_reply_resume_merges_feedback_and_keeps_existing_context(db):
    """Feedback lands without clobbering sibling keys (loop bookkeeping)."""
    assert await db.queue_job_for_resume(JOB, {"queued_feedback": "fixed, resume"})

    async with db.acquire() as conn:
        ctx = await db_context(conn, JOB)
    assert ctx["queued_feedback"] == "fixed, resume"
    assert ctx["loop_id"] == "b15dc68f"
    assert ctx["loop_iteration"] == 3


@pytest.mark.asyncio
async def test_reply_resume_stashes_freeze_for_observability(db):
    """The shed freeze survives in context — the row-level blob must not."""
    assert await db.queue_job_for_resume(JOB, {"queued_feedback": "fixed, resume"})

    async with db.acquire() as conn:
        ctx = await db_context(conn, JOB)
    assert ctx["last_freeze_data"]["freeze_type"] == "blocking_message"
    assert ctx["last_freeze_data"]["thread_id"] == "d78144"


@pytest.mark.asyncio
async def test_resume_without_feedback_still_sheds_freeze(db):
    """``resume_job``'s no-feedback arm must clear the freeze too."""
    assert await db.queue_job_for_resume(JOB)

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, freeze_data FROM jobs WHERE id = $1", UUID(JOB)
        )
    assert row["status"] == "paused"
    assert row["freeze_data"] is None
    assert len(await db.get_dispatchable_jobs()) == 1


@pytest.mark.asyncio
async def test_unknown_job_is_a_no_op(db):
    """Caller distinguishes 'not found' from a real re-queue (warning path)."""
    assert (
        await db.queue_job_for_resume("11111111-1111-1111-1111-111111111111") is False
    )
    assert await db.queue_job_for_resume("not-a-uuid") is False


async def db_context(conn, job_id: str) -> dict:
    """Read a job's context as a dict (asyncpg returns JSONB as a str)."""
    raw = await conn.fetchval("SELECT context FROM jobs WHERE id = $1", UUID(job_id))
    return json.loads(raw) if isinstance(raw, str) else (raw or {})
