"""Real-Postgres tests for the atomic ``jobs.context`` helpers (HF-3, Phase 4).

Spins up an ephemeral PostgreSQL via testcontainers and exercises the
single-statement context writers added to retire the read-modify-write
full-dict rewrites:

    - ``append_queued_reply``          (jsonb_set + ``|| $1`` array append)
    - ``delete_job_context_keys``      (``context - $1::text[]``)
    - ``merge_job_context``            (``|| $1`` top-level merge, NULL guard)

The point these tests defend that a mock cannot: under real concurrency the
atomic single-statement form is lost-update-immune (READ COMMITTED /
EvalPlanQual re-reads the row after a concurrent commit), so N racing writers
all land. The old full-dict rewrite lost all but one of a racing batch.

Skips cleanly without ``testcontainers[postgres]`` (requirements-dev.txt) or a
container runtime — same pattern as tests/test_audit_writer.py.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.postgres")

from orchestrator.database.postgres import PostgresDB  # noqa: E402

PG_IMAGE = "postgres:15"


def _swap_db(dsn: str, dbname: str) -> str:
    head, _, tail = dsn.rpartition("/")
    query = "?" + tail.split("?", 1)[1] if "?" in tail else ""
    return f"{head}/{dbname}{query}"


@pytest.fixture(scope="module")
def pg_dsn():
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(PG_IMAGE)
    try:
        container.start()
    except Exception as exc:  # pragma: no cover - env without a runtime
        pytest.skip(f"no container runtime for testcontainers: {exc}")
    try:
        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )
    finally:
        container.stop()


@pytest_asyncio.fixture
async def db(pg_dsn):
    """A fresh database with a minimal ``jobs`` table + a connected PostgresDB."""
    dbname = f"jobs_ctx_{uuid4().hex[:12]}"

    admin = await asyncpg.connect(pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()

    database = PostgresDB(
        _swap_db(pg_dsn, dbname), min_connections=2, max_connections=12
    )
    await database.connect()
    async with database.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE jobs (
                id uuid PRIMARY KEY,
                context jsonb,
                status text,
                execution_lane text NOT NULL DEFAULT 'pinned',
                freeze_data jsonb,
                assigned_agent_id text,
                updated_at timestamptz DEFAULT now()
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE checkpoints (
                thread_id text NOT NULL,
                checkpoint_ns text NOT NULL DEFAULT '',
                checkpoint_id text NOT NULL,
                metadata jsonb NOT NULL DEFAULT '{}',
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )
            """
        )
    try:
        yield database
    finally:
        await database.disconnect()
        admin = await asyncpg.connect(pg_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()


async def _insert_job(database, job_id, context="__unset__"):
    """Insert a job. ``context=None`` writes a SQL NULL column (the COALESCE case)."""
    payload = (
        None
        if context is None
        else json.dumps(context if context != "__unset__" else {})
    )
    async with database.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, context, status) VALUES ($1, $2::jsonb, 'processing')",
            uuid.UUID(job_id),
            payload,
        )


async def _read_ctx(database, job_id):
    async with database.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT context FROM jobs WHERE id = $1", uuid.UUID(job_id)
        )
    ctx = row["context"]
    return json.loads(ctx) if isinstance(ctx, str) else ctx


# ---------------------------------------------------------------------------
# append_queued_reply
# ---------------------------------------------------------------------------


class TestAppendQueuedReply:
    @pytest.mark.asyncio
    async def test_creates_array_when_key_missing(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {})

        ok = await db.append_queued_reply(jid, {"thread_id": "t1", "message": "hi"})

        assert ok is True
        assert await _read_ctx(db, jid) == {
            "queued_replies": [{"thread_id": "t1", "message": "hi"}]
        }

    @pytest.mark.asyncio
    async def test_creates_array_when_context_is_null(self, db):
        # The COALESCE(context,'{}') guard: a NULL column must not null-crash.
        jid = str(uuid4())
        await _insert_job(db, jid, None)

        await db.append_queued_reply(jid, {"message": "first"})

        assert await _read_ctx(db, jid) == {"queued_replies": [{"message": "first"}]}

    @pytest.mark.asyncio
    async def test_appends_in_order_and_preserves_siblings(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {"queued_replies": [{"message": "a"}], "keep": "me"})

        await db.append_queued_reply(jid, {"message": "b"})

        ctx = await _read_ctx(db, jid)
        assert [r["message"] for r in ctx["queued_replies"]] == ["a", "b"]
        assert ctx["keep"] == "me"

    @pytest.mark.asyncio
    async def test_concurrent_appends_all_land(self, db):
        # The greenfield lost-update test: N racing appends must ALL be present.
        # The retired full-dict read-modify-write kept only one of each batch.
        jid = str(uuid4())
        await _insert_job(db, jid, {})
        n = 8

        await asyncio.gather(*(db.append_queued_reply(jid, {"i": i}) for i in range(n)))

        ctx = await _read_ctx(db, jid)
        assert len(ctx["queued_replies"]) == n
        assert sorted(r["i"] for r in ctx["queued_replies"]) == list(range(n))

    @pytest.mark.asyncio
    async def test_missing_job_returns_false(self, db):
        assert await db.append_queued_reply(str(uuid4()), {"message": "x"}) is False

    @pytest.mark.asyncio
    async def test_invalid_id_returns_false(self, db):
        assert await db.append_queued_reply("not-a-uuid", {"message": "x"}) is False


# ---------------------------------------------------------------------------
# delete_job_context_keys
# ---------------------------------------------------------------------------


class TestDeleteJobContextKeys:
    @pytest.mark.asyncio
    async def test_removes_listed_keys_only(self, db):
        jid = str(uuid4())
        await _insert_job(
            db,
            jid,
            {"queued_feedback": "fb", "delegation_results": [1, 2], "keep": "me"},
        )

        ok = await db.delete_job_context_keys(
            jid, ["queued_feedback", "delegation_results"]
        )

        assert ok is True
        assert await _read_ctx(db, jid) == {"keep": "me"}

    @pytest.mark.asyncio
    async def test_absent_key_is_noop_but_matches_row(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {"keep": "me"})

        ok = await db.delete_job_context_keys(jid, ["queued_feedback"])

        assert ok is True  # row matched
        assert await _read_ctx(db, jid) == {"keep": "me"}

    @pytest.mark.asyncio
    async def test_null_column_does_not_crash(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, None)

        ok = await db.delete_job_context_keys(jid, ["queued_feedback"])

        assert ok is True
        assert await _read_ctx(db, jid) == {}

    @pytest.mark.asyncio
    async def test_empty_keys_returns_false_and_leaves_context(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {"keep": "me"})

        ok = await db.delete_job_context_keys(jid, [])

        assert ok is False
        assert await _read_ctx(db, jid) == {"keep": "me"}

    @pytest.mark.asyncio
    async def test_missing_job_returns_false(self, db):
        assert await db.delete_job_context_keys(str(uuid4()), ["k"]) is False


# ---------------------------------------------------------------------------
# merge_job_context — NULL-column guard (used by 8 of the 13 converted sites)
# ---------------------------------------------------------------------------


class TestMergeJobContextNullGuard:
    @pytest.mark.asyncio
    async def test_merge_into_null_column(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, None)

        assert await db.merge_job_context(jid, {"a": 1}) is True
        assert await _read_ctx(db, jid) == {"a": 1}

    @pytest.mark.asyncio
    async def test_merge_preserves_and_overwrites(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {"a": 1, "b": 2})

        await db.merge_job_context(jid, {"b": 3, "c": 4})

        assert await _read_ctx(db, jid) == {"a": 1, "b": 3, "c": 4}

    @pytest.mark.asyncio
    async def test_concurrent_merges_of_distinct_keys_all_land(self, db):
        # Distinct-key merges racing on one row must all survive — this is the
        # correctness win over the full-dict rewrite that motivated HF-3.
        jid = str(uuid4())
        await _insert_job(db, jid, {})
        n = 8

        await asyncio.gather(
            *(db.merge_job_context(jid, {f"k{i}": i}) for i in range(n))
        )

        ctx = await _read_ctx(db, jid)
        assert ctx == {f"k{i}": i for i in range(n)}


# ---------------------------------------------------------------------------
# Fused context+status writes (sites 5 & 6) — the context merge and the status
# flip MUST happen in one statement, else a dispatcher window opens on
# half-written context. These run the exact SQL from orchestrator/main.py so a
# future split of the fusion is caught.
# ---------------------------------------------------------------------------

# Verbatim from upgrade_job_to_vm (main.py): merge into context.vm + flip status.
_UPGRADE_TO_VM_SQL = (
    "UPDATE jobs SET context = jsonb_set("
    "        COALESCE(context, '{}'::jsonb), '{vm}', "
    "        COALESCE(context->'vm', '{}'::jsonb) || $1::jsonb"
    "    ), "
    "    status = 'paused', freeze_data = NULL, "
    "    assigned_agent_id = NULL, updated_at = CURRENT_TIMESTAMP "
    "WHERE id = $2::uuid"
)

# Verbatim from _internal_resume_job (main.py): top-level merge + flip status.
_INTERNAL_RESUME_SQL = (
    "UPDATE jobs SET context = COALESCE(context, '{}'::jsonb) || $1::jsonb, "
    "status = 'paused', assigned_agent_id = NULL, "
    "updated_at = CURRENT_TIMESTAMP WHERE id = $2::uuid"
)


async def _read_row(database, job_id):
    async with database.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT context, status, freeze_data, assigned_agent_id "
            "FROM jobs WHERE id = $1",
            uuid.UUID(job_id),
        )
    ctx = row["context"]
    return {
        "context": json.loads(ctx) if isinstance(ctx, str) else ctx,
        "status": row["status"],
        "freeze_data": row["freeze_data"],
        "assigned_agent_id": row["assigned_agent_id"],
    }


class TestFusedContextStatusWrites:
    @pytest.mark.asyncio
    async def test_upgrade_to_vm_merges_vm_and_flips_status_atomically(self, db):
        jid = str(uuid4())
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO jobs (id, context, status, freeze_data, "
                "assigned_agent_id) VALUES ($1, $2::jsonb, 'pending_review', "
                "$3::jsonb, 'agent-x')",
                uuid.UUID(jid),
                json.dumps(
                    {"vm": {"golden": "g", "requested": False}, "other": "keep"}
                ),
                json.dumps({"freeze_type": "vm_upgrade_required"}),
            )
            await conn.execute(
                _UPGRADE_TO_VM_SQL,
                json.dumps(
                    {
                        "requested": True,
                        "upgrade_from": "container",
                        "upgrade_command": "sudo apt-get install libxml2-dev",
                    }
                ),
                uuid.UUID(jid),
            )

        row = await _read_row(db, jid)
        assert row["context"]["vm"]["requested"] is True  # overwritten
        assert row["context"]["vm"]["golden"] == "g"  # vm sibling preserved
        assert row["context"]["vm"]["upgrade_from"] == "container"
        assert row["context"]["other"] == "keep"  # unrelated key preserved
        assert row["status"] == "paused"  # flipped in the same statement
        assert row["freeze_data"] is None
        assert row["assigned_agent_id"] is None

    @pytest.mark.asyncio
    async def test_internal_resume_merges_feedback_and_flips_status_atomically(
        self, db
    ):
        jid = str(uuid4())
        async with db.acquire() as conn:
            await conn.execute(
                "INSERT INTO jobs (id, context, status, assigned_agent_id) "
                "VALUES ($1, $2::jsonb, 'processing', 'agent-x')",
                uuid.UUID(jid),
                json.dumps({"keep": "me"}),
            )
            await conn.execute(
                _INTERNAL_RESUME_SQL,
                json.dumps({"queued_feedback": "please redo section 3"}),
                uuid.UUID(jid),
            )

        row = await _read_row(db, jid)
        assert row["context"]["queued_feedback"] == "please redo section 3"
        assert row["context"]["keep"] == "me"  # unrelated key preserved
        assert row["status"] == "paused"  # flipped in the same statement
        assert row["assigned_agent_id"] is None


# ---------------------------------------------------------------------------
# append_verification_round
# ---------------------------------------------------------------------------


class TestAppendVerificationRound:
    @pytest.mark.asyncio
    async def test_appends_and_returns_length(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {})

        assert (
            await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"})
            == 1
        )
        assert (
            await db.append_verification_round(jid, {"round": 2, "critic_job_id": "c2"})
            == 2
        )
        rounds = (await _read_ctx(db, jid))["verification_rounds"]
        assert [r["critic_job_id"] for r in rounds] == ["c1", "c2"]

    @pytest.mark.asyncio
    async def test_null_column_creates_array(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, None)

        assert (
            await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"})
            == 1
        )

    @pytest.mark.asyncio
    async def test_preserves_other_keys(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {"keep": "me"})

        await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"})
        assert (await _read_ctx(db, jid))["keep"] == "me"

    @pytest.mark.asyncio
    async def test_duplicate_critic_id_is_noop(self, db):
        # A retried /complete must not append a second round for the same critic.
        jid = str(uuid4())
        await _insert_job(db, jid, {})

        assert (
            await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"})
            == 1
        )
        assert (
            await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"})
            == 0
        )
        assert len((await _read_ctx(db, jid))["verification_rounds"]) == 1

    @pytest.mark.asyncio
    async def test_concurrent_appends_all_land(self, db):
        # The property a mock cannot defend: N racing single-statement appends
        # are lost-update-immune under READ COMMITTED.
        jid = str(uuid4())
        await _insert_job(db, jid, {})
        n = 10

        await asyncio.gather(
            *(
                db.append_verification_round(
                    jid, {"round": i, "critic_job_id": f"c{i}"}
                )
                for i in range(n)
            )
        )

        assert len((await _read_ctx(db, jid))["verification_rounds"]) == n

    @pytest.mark.asyncio
    async def test_missing_job_returns_zero(self, db):
        assert (
            await db.append_verification_round(str(uuid4()), {"critic_job_id": "c1"})
            == 0
        )

    @pytest.mark.asyncio
    async def test_json_null_ledger_is_replaced_not_concatenated(self, db):
        """A jsonb `null` is NOT SQL NULL, so COALESCE leaves it in place and
        `'null'::jsonb || [x]` yields `[null, x]` — a ledger whose round
        numbers are off by one from the very first append, and a phantom
        entry the fold has to defend against."""
        jid = str(uuid4())
        await _insert_job(db, jid, {"verification_rounds": None})

        assert (
            await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"})
            == 1
        )
        rounds = (await _read_ctx(db, jid))["verification_rounds"]
        assert [r["critic_job_id"] for r in rounds] == ["c1"]

    @pytest.mark.asyncio
    async def test_non_array_ledger_is_replaced(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {"verification_rounds": "garbage"})

        assert (
            await db.append_verification_round(jid, {"round": 1, "critic_job_id": "c1"})
            == 1
        )
        assert len((await _read_ctx(db, jid))["verification_rounds"]) == 1


# ---------------------------------------------------------------------------
# append_pending_guidance + consume_job_guidance (P1-A guidance lane)
# ---------------------------------------------------------------------------


class TestAppendPendingGuidance:
    @pytest.mark.asyncio
    async def test_creates_array_and_preserves_siblings(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {"keep": "me"})

        ok = await db.append_pending_guidance(jid, {"id": "g1", "text": "steer"})

        assert ok is True
        ctx = await _read_ctx(db, jid)
        assert ctx["pending_guidance"] == [{"id": "g1", "text": "steer"}]
        assert ctx["keep"] == "me"

    @pytest.mark.asyncio
    async def test_concurrent_appends_all_land(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {})
        n = 8

        await asyncio.gather(
            *(db.append_pending_guidance(jid, {"id": f"g{i}"}) for i in range(n))
        )

        ctx = await _read_ctx(db, jid)
        assert sorted(e["id"] for e in ctx["pending_guidance"]) == sorted(
            f"g{i}" for i in range(n)
        )

    @pytest.mark.asyncio
    async def test_missing_job_and_invalid_id_return_false(self, db):
        assert await db.append_pending_guidance(str(uuid4()), {"id": "g"}) is False
        assert await db.append_pending_guidance("not-a-uuid", {"id": "g"}) is False


class TestConsumeJobGuidance:
    @pytest.mark.asyncio
    async def test_moves_acked_guidance_to_consumed_replies(self, db):
        jid = str(uuid4())
        await _insert_job(
            db,
            jid,
            {
                "pending_guidance": [
                    {"id": "g1", "text": "rendered"},
                    {"id": "g2", "text": "still pending"},
                ],
                "keep": "me",
            },
        )

        moved = await db.consume_job_guidance(jid, guidance_ids=["g1"])

        assert moved == 1
        ctx = await _read_ctx(db, jid)
        assert [e["id"] for e in ctx["pending_guidance"]] == ["g2"]
        assert [e["id"] for e in ctx["consumed_replies"]] == ["g1"]
        assert ctx["consumed_replies"][0]["consumed_at"]
        assert ctx["keep"] == "me"

    @pytest.mark.asyncio
    async def test_moves_drained_reply_threads(self, db):
        jid = str(uuid4())
        await _insert_job(
            db,
            jid,
            {
                "queued_replies": [
                    {"thread_id": "officer", "message": "drained"},
                    {"thread_id": "other", "message": "not drained"},
                ]
            },
        )

        moved = await db.consume_job_guidance(jid, reply_threads=["officer"])

        assert moved == 1
        ctx = await _read_ctx(db, jid)
        assert [r["thread_id"] for r in ctx["queued_replies"]] == ["other"]
        assert [r["thread_id"] for r in ctx["consumed_replies"]] == ["officer"]

    @pytest.mark.asyncio
    async def test_pinned_exact_subagent_key_preserves_a_newer_run_same_thread(
        self, db
    ):
        from shared.job_steering import queued_reply_key

        jid = str(uuid4())
        child_id = str(uuid4())
        first = {
            "id": str(uuid4()),
            "source": "subagent",
            "thread_id": child_id,
            "handle": "reviewer-7f3a",
            "run_generation": str(uuid4()),
            "message": "first run",
            "timestamp": "2026-09-01T01:00:00+00:00",
        }
        successor = {
            "id": str(uuid4()),
            "source": "subagent",
            "thread_id": child_id,
            "handle": "reviewer-7f3a",
            "run_generation": str(uuid4()),
            "message": "successor run",
            "timestamp": "2026-09-01T01:01:00+00:00",
        }
        await _insert_job(db, jid, {"queued_replies": [first, successor]})

        moved = await db.consume_job_guidance(
            jid,
            reply_keys=[queued_reply_key(first)],
        )

        assert moved == 1
        ctx = await _read_ctx(db, jid)
        assert ctx["queued_replies"] == [successor]
        assert ctx["consumed_replies"][0]["id"] == first["id"]
        assert ctx["consumed_replies"][0]["source"] == "subagent"
        assert ctx["consumed_replies"][0]["run_generation"] == first["run_generation"]

    @pytest.mark.asyncio
    async def test_stateless_exact_reply_key_preserves_newer_same_thread(self, db):
        from shared.job_steering import queued_reply_key

        jid = str(uuid4())
        first = {
            "id": str(uuid4()),
            "thread_id": "officer",
            "message": "first",
            "timestamp": "2026-08-10T01:00:00+00:00",
        }
        newer = {
            "id": str(uuid4()),
            "thread_id": "officer",
            "message": "newer",
            "timestamp": "2026-08-10T01:01:00+00:00",
        }
        await _insert_job(db, jid, {"queued_replies": [first, newer]})
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET execution_lane = 'stateless' WHERE id = $1",
                uuid.UUID(jid),
            )
            await conn.execute(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, metadata) "
                "VALUES ($1, '', 'cp-7', '{\"step\": 7}'::jsonb)",
                jid,
            )

        moved = await db.consume_job_guidance(
            jid,
            reply_keys=[queued_reply_key(first)],
            checkpoint_id="cp-7",
        )

        assert moved == 1
        ctx = await _read_ctx(db, jid)
        assert ctx["queued_replies"] == [newer]
        assert ctx["consumed_replies"][0]["id"] == first["id"]
        assert ctx["consumed_replies"][0]["consumed_checkpoint_id"] == "cp-7"
        assert ctx["consumed_replies"][0]["consumed_checkpoint_step"] == 7

    @pytest.mark.asyncio
    async def test_stateless_missing_checkpoint_proof_preserves_inbox(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {"pending_guidance": [{"id": "g1"}]})
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET execution_lane = 'stateless' WHERE id = $1",
                uuid.UUID(jid),
            )

        with pytest.raises(ValueError, match="checkpoint"):
            await db.consume_job_guidance(jid, guidance_ids=["g1"])

        assert (await _read_ctx(db, jid))["pending_guidance"] == [{"id": "g1"}]

    @pytest.mark.asyncio
    async def test_stateless_unknown_checkpoint_preserves_inbox(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {"pending_guidance": [{"id": "g1"}]})
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET execution_lane = 'stateless' WHERE id = $1",
                uuid.UUID(jid),
            )

        with pytest.raises(ValueError, match="does not exist"):
            await db.consume_job_guidance(
                jid,
                guidance_ids=["g1"],
                checkpoint_id="missing",
            )

        assert (await _read_ctx(db, jid))["pending_guidance"] == [{"id": "g1"}]

    @pytest.mark.asyncio
    async def test_stateless_one_shot_ack_clears_only_exact_generation(self, db):
        from shared.job_steering import context_delivery_key

        jid = str(uuid4())
        context = {
            "queued_feedback": "try again",
            "queued_feedback_reason": "review",
            "queued_feedback_delivery_id": "feedback-new",
            "delegation_results": [{"job_id": "child-1", "status": "completed"}],
            "delegation_results_delivery_id": "delegation-new",
            "keep": "me",
        }
        await _insert_job(db, jid, context)
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET execution_lane = 'stateless' WHERE id = $1",
                uuid.UUID(jid),
            )
            await conn.execute(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, metadata) "
                "VALUES ($1, '', 'cp-8', '{\"step\": 8}'::jsonb)",
                jid,
            )

        stale_feedback_key = context_delivery_key(
            "feedback",
            "try again",
            delivery_id="feedback-old",
            companion="review",
        )
        assert (
            await db.consume_job_guidance(
                jid,
                feedback_keys=[stale_feedback_key],
                checkpoint_id="cp-8",
            )
            == 0
        )
        assert (await _read_ctx(db, jid))["queued_feedback_delivery_id"] == (
            "feedback-new"
        )

        feedback_key = context_delivery_key(
            "feedback",
            context["queued_feedback"],
            delivery_id=context["queued_feedback_delivery_id"],
            companion=context["queued_feedback_reason"],
        )
        delegation_key = context_delivery_key(
            "delegation",
            context["delegation_results"],
            delivery_id=context["delegation_results_delivery_id"],
        )
        assert (
            await db.consume_job_guidance(
                jid,
                feedback_keys=[feedback_key],
                delegation_keys=[delegation_key],
                checkpoint_id="cp-8",
            )
            == 2
        )
        ctx = await _read_ctx(db, jid)
        assert "queued_feedback" not in ctx
        assert "queued_feedback_reason" not in ctx
        assert "queued_feedback_delivery_id" not in ctx
        assert "delegation_results" not in ctx
        assert "delegation_results_delivery_id" not in ctx
        assert ctx["keep"] == "me"

    @pytest.mark.asyncio
    async def test_idempotent_reack_is_noop(self, db):
        jid = str(uuid4())
        await _insert_job(db, jid, {"pending_guidance": [{"id": "g1"}]})

        assert await db.consume_job_guidance(jid, guidance_ids=["g1"]) == 1
        assert await db.consume_job_guidance(jid, guidance_ids=["g1"]) == 0

        ctx = await _read_ctx(db, jid)
        assert ctx["pending_guidance"] == []
        assert len(ctx["consumed_replies"]) == 1

    @pytest.mark.asyncio
    async def test_concurrent_append_during_consume_is_not_lost(self, db):
        """The FOR UPDATE row lock: an atomic append racing the consume's
        read-filter-write must land, not be clobbered by the wholesale
        key rewrite."""
        jid = str(uuid4())
        await _insert_job(db, jid, {"pending_guidance": [{"id": "g1"}]})

        await asyncio.gather(
            db.consume_job_guidance(jid, guidance_ids=["g1"]),
            db.append_pending_guidance(jid, {"id": "g2"}),
        )

        ctx = await _read_ctx(db, jid)
        ids = {e["id"] for e in ctx["pending_guidance"]} | {
            e["id"] for e in ctx["consumed_replies"]
        }
        assert ids == {"g1", "g2"}

    @pytest.mark.asyncio
    async def test_missing_job_returns_none(self, db):
        assert await db.consume_job_guidance(str(uuid4()), guidance_ids=["g"]) is None
        assert await db.consume_job_guidance("not-a-uuid", guidance_ids=["g"]) is None
