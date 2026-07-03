"""HF-2 — the turn-complete reconcile batch against a real Postgres.

The reconcile switched from a per-message save_thread_message loop to one
pipelined save_thread_messages. These tests pin the resume-critical invariants
the batch must preserve, verified on a real pg (testcontainers):

  * ON CONFLICT (id) dedup — reconciling the incrementally-written rows updates
    them in place, never duplicates;
  * seq stability — seq is assigned once on first insert and preserved across
    the reconcile update (it is the resume cursor);
  * idempotency — running the batch twice is a no-op on the row set + seqs;
  * the threads activity/turn bump still happens, once.
"""

import uuid

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from src.database.postgres_db import PostgresDB, _coerce_row_id

_TID = "aaaaaaaa-0000-0000-0000-000000000001"


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def db(pg_dsn):
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS threads (
                id uuid PRIMARY KEY,
                last_activity timestamptz,
                total_turns int DEFAULT 0
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_messages (
                id uuid PRIMARY KEY,
                thread_id uuid,
                role text,
                content text,
                tool_calls jsonb,
                turn_number int,
                metrics jsonb,
                tool_call_id text,
                thinking text,
                reasoning jsonb,
                tool_results jsonb,
                provider text,
                provider_raw jsonb,
                additional_kwargs jsonb,
                response_metadata jsonb,
                seq bigserial,
                created_at timestamptz DEFAULT now()
            )
            """
        )
        await conn.execute("TRUNCATE thread_messages")
        await conn.execute(
            "INSERT INTO threads (id, total_turns) VALUES ($1, 0) "
            "ON CONFLICT (id) DO UPDATE SET total_turns = 0, last_activity = NULL",
            uuid.UUID(_TID),
        )
    yield d
    await d.close()


def _row(mid, role, content, turn, **extra):
    base = {
        "id": mid,
        "role": role,
        "content": content,
        "tool_calls": None,
        "turn_number": turn,
        "metrics": None,
        "tool_call_id": None,
        "thinking": None,
    }
    base.update(extra)
    return base


async def _seqs_by_id(db):
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, seq, content, metrics FROM thread_messages "
            "WHERE thread_id = $1 ORDER BY seq",
            uuid.UUID(_TID),
        )
    return {str(r["id"]): r for r in rows}


@pytest.mark.asyncio
async def test_reconcile_dedups_and_preserves_seq(db):
    # Incremental mid-turn writes land 3 rows (each returns its assigned seq).
    ids = ["msg_a", "msg_b", "msg_c"]
    first = {}
    for i, mid in enumerate(ids):
        r = await db.save_thread_message(
            thread_id=_TID, role="ai", content=f"draft-{mid}", turn_number=2, id=mid
        )
        first[mid] = r["seq"]

    # Turn-complete reconcile: same ids, updated content + metrics, batched.
    rows = [
        _row(mid, "ai", f"final-{mid}", 2, metrics={"tokens": i})
        for i, mid in enumerate(ids)
    ]
    await db.save_thread_messages(_TID, rows)

    after = await _seqs_by_id(db)
    # No duplicates — one row per id.
    assert len(after) == 3
    for mid in ids:
        row_id = _coerce_row_id(mid)
        assert row_id in after, f"{mid} row missing after reconcile"
        # seq preserved across the upsert (the resume cursor stays stable).
        assert after[row_id]["seq"] == first[mid], f"seq moved for {mid}"
        # content updated to the reconciled value.
        assert after[row_id]["content"] == f"final-{mid}"


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(db):
    rows = [_row("only", "ai", "hello", 1)]
    await db.save_thread_messages(_TID, rows)
    once = await _seqs_by_id(db)
    await db.save_thread_messages(_TID, rows)
    twice = await _seqs_by_id(db)
    assert list(once) == list(twice), "row set changed on re-run"
    for k in once:
        assert once[k]["seq"] == twice[k]["seq"], "seq changed on re-run"


@pytest.mark.asyncio
async def test_reconcile_bumps_thread_turn_count(db):
    await db.save_thread_messages(
        _TID,
        [_row("t_hi", "ai", "x", 7), _row("t_lo", "tool", "y", 5)],
    )
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT total_turns, last_activity FROM threads WHERE id = $1",
            uuid.UUID(_TID),
        )
    # GREATEST(existing, max turn_number in the batch) => 7.
    assert row["total_turns"] == 7
    assert row["last_activity"] is not None
