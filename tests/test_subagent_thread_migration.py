"""Migrations 0206 / 0207 / 0208 — the subagent child identity on ``threads``
(U3 WP3, plan B.1).

Structural assertions in the repo's migration-test idiom (read the file,
assert on the comment-stripped SQL — ``test_ssh_handle_migration.py``), the
snapshot artifact, and one real-Postgres proof that replays the chain onto a
seeded database and then drives the child lifecycle through the REAL
accessors on both pools: the orchestrator creates the row from the job, the
agent-side pool writes the transcript and the terminal update, the roster
and replay reads see it, the sessions list never does, and a job delete
cascades — including the trap this lane found (a live child row blocks the
cascade through the pinned delete authority, so ``delete_job`` ends the
children first). The behavioural test skips without a container runtime.
"""

from __future__ import annotations

import json
import pathlib
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest

from orchestrator.database.migrate import discover, run_migrations
from orchestrator.database.postgres import PostgresDB as OrchestratorDB
from src.database.postgres_db import PostgresDB as AgentDB

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "orchestrator" / "database" / "migrations" / "app"
COLUMNS = "0206_threads_subagent_kind.sql"
INDEX = "0207_threads_parent_job_idx.notx.sql"
VALIDATE = "0208_threads_subagent_validate.sql"
PREDECESSOR = "0205_experts_role_tags_backfill.sql"
FENCE_MIGRATION = "0185_thread_runtime_generation_retirement.sql"
SSH_HANDLE_MIGRATION = "0202_threads_ssh_handle.sql"

NEW_COLUMNS = (
    "kind",
    "parent_job_id",
    "parent_thread_id",
    "parent_tool_call_id",
    "subagent_handle",
    "subagent_type",
    "subagent_status",
    "subagent_outcome",
    "subagent_error",
    "report_path",
)
CONSTRAINTS = (
    "threads_kind_check",
    "threads_parent_job_id_fkey",
    "threads_parent_thread_id_fkey",
)


def _read(name: str) -> str:
    return (MIGRATIONS / name).read_text()


def _statements(name: str) -> str:
    """The migration's SQL with comment lines stripped (the 0202 idiom: the
    headers explain what the DDL deliberately does NOT do)."""
    return "\n".join(
        line for line in _read(name).splitlines() if not line.lstrip().startswith("--")
    )


def _compact(sql: str) -> str:
    return " ".join(sql.split())


# ---------------------------------------------------------------------------
# The three files
# ---------------------------------------------------------------------------


class TestFiles:
    def test_all_three_exist_at_their_numbers(self):
        for name, number in ((COLUMNS, 206), (INDEX, 207), (VALIDATE, 208)):
            path = MIGRATIONS / name
            assert path.exists(), f"{name} missing"
            assert int(path.name[:4]) == number
            assert path.read_text().startswith(f"-- migration:     {name}")

    def test_headers_declare_the_depends_on_chain_and_transactionality(self):
        columns = _read(COLUMNS)
        assert SSH_HANDLE_MIGRATION in columns, "0206 follows 0202's ADD COLUMN shape"
        assert FENCE_MIGRATION in columns, "0206 drains 0185's deferred fence"
        assert "-- transactional: yes" in columns
        index = _read(INDEX)
        assert COLUMNS in index
        assert "-- transactional: no" in index
        validate = _read(VALIDATE)
        assert COLUMNS in validate
        assert "-- transactional: yes" in validate

    def test_discover_orders_the_lane_right_after_0205(self):
        """Assert against the real rule (discover() is what boot runs and it
        raises on a duplicate prefix), not a reimplementation of it."""
        names = [path.name for path in discover(MIGRATIONS)]
        for name in (COLUMNS, INDEX, VALIDATE):
            assert names.count(name) == 1
        assert names.index(COLUMNS) == names.index(PREDECESSOR) + 1
        assert names.index(INDEX) == names.index(COLUMNS) + 1
        assert names.index(VALIDATE) == names.index(INDEX) + 1


# ---------------------------------------------------------------------------
# 0206 — the columns and the NOT VALID constraints
# ---------------------------------------------------------------------------


class TestColumnsMigration:
    def test_is_wrapped_and_bounded(self):
        sql = _statements(COLUMNS)
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        assert statements[0].upper() == "BEGIN"
        assert statements[-1].upper() == "COMMIT"
        assert "SET LOCAL lock_timeout" in sql
        assert "SET LOCAL statement_timeout" in sql
        assert "SET LOCAL idle_in_transaction_session_timeout" in sql

    def test_adds_every_column_nullable_or_with_a_constant_default(self):
        sql = _compact(_statements(COLUMNS))
        for column in NEW_COLUMNS:
            assert f"ADD COLUMN IF NOT EXISTS {column} " in sql, column
        # kind is the only NOT NULL column, and it carries the constant
        # default that makes the ADD COLUMN an in-place catalog change.
        assert "ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'session'" in sql
        assert sql.count("NOT NULL") == 1, (
            "only kind may be NOT NULL — every other column is nullable, and a "
            "later SET NOT NULL is exactly what this assertion exists to catch"
        )
        assert "UPDATE threads" not in sql and "UPDATE public.threads" not in sql

    def test_constraints_are_added_not_valid(self):
        sql = _compact(_statements(COLUMNS))
        assert (
            "ADD CONSTRAINT threads_kind_check CHECK (kind IN ('session', "
            "'subagent')) NOT VALID" in sql
        )
        assert (
            "ADD CONSTRAINT threads_parent_job_id_fkey FOREIGN KEY (parent_job_id) "
            "REFERENCES public.jobs (id) ON DELETE CASCADE NOT VALID" in sql
        )
        assert (
            "ADD CONSTRAINT threads_parent_thread_id_fkey FOREIGN KEY "
            "(parent_thread_id) REFERENCES public.threads (id) ON DELETE CASCADE "
            "NOT VALID" in sql
        )
        assert sql.count("NOT VALID") == 3

    def test_constraint_adds_are_guarded_on_the_catalog(self):
        """ADD CONSTRAINT has no IF NOT EXISTS; a rerun after a partial
        failure must still be idempotent, like the columns."""
        sql = _compact(_statements(COLUMNS))
        for name in CONSTRAINTS:
            assert f"WHERE conname = '{name}'" in sql, name

    def test_the_closed_status_vocabulary_is_untouched(self):
        """B.1: child rows use active -> ended; the outcome is a sibling
        column, never a new valid_thread_status value."""
        sql = _statements(COLUMNS)
        assert "valid_thread_status" not in sql
        assert "DROP CONSTRAINT" not in sql
        assert "ADD COLUMN IF NOT EXISTS subagent_status text" in sql
        # Open set by design: no CHECK on subagent_status, and the COMMENT
        # names the vocabulary the ledger writes.
        assert "CHECK (subagent_status" not in _compact(sql)
        comment = re.search(
            r"COMMENT ON COLUMN public\.threads\.subagent_status IS(.*?);",
            sql,
            re.S,
        )
        assert comment is not None
        for word in (
            "running",
            "completed",
            "parked",
            "interrupted",
            "capped",
            "error",
            "cancelled",
        ):
            assert word in comment.group(1), word

    def test_every_new_column_is_commented(self):
        sql = _statements(COLUMNS)
        for column in NEW_COLUMNS:
            assert f"COMMENT ON COLUMN public.threads.{column} IS" in sql, column

    def test_retries_the_lock_and_drains_the_deferred_fence_around_the_alter(self):
        """The 0202 shape: lock_timeout with a retry loop, and 0185's
        reciprocity fence fired early (scoped, never SET CONSTRAINTS ALL)
        before the ALTER and restored to DEFERRED after it."""
        sql = _statements(COLUMNS)
        assert "lock_timeout" in sql
        assert "EXCEPTION WHEN lock_not_available" in sql
        drain = "SET CONSTRAINTS public.threads_agent_reciprocity_fence IMMEDIATE"
        restore = "SET CONSTRAINTS public.threads_agent_reciprocity_fence DEFERRED"
        alter = "ALTER TABLE public.threads"
        assert drain in sql and restore in sql
        assert sql.index(drain) < sql.index(alter) < sql.index(restore)
        assert "SET CONSTRAINTS ALL" not in sql


# ---------------------------------------------------------------------------
# 0207 — the concurrent partial index
# ---------------------------------------------------------------------------


class TestIndexMigration:
    def test_is_one_concurrent_statement_outside_any_transaction(self):
        sql = _statements(INDEX)
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        assert len(statements) == 1, "a .notx file is ONE statement"
        assert statements[0].startswith(
            "CREATE INDEX CONCURRENTLY idx_threads_parent_job"
        )
        assert "BEGIN" not in sql and "COMMIT" not in sql
        assert "SET " not in sql, "no in-file timeouts (require-timeout-settings)"

    def test_is_partial_on_the_parent_job(self):
        sql = _compact(_statements(INDEX))
        assert (
            "ON public.threads (parent_job_id) WHERE parent_job_id IS NOT NULL" in sql
        )

    def test_refuses_if_not_exists_and_says_why_to_squawk(self):
        """0132 / 0203 runbook: IF NOT EXISTS would report success against
        the INVALID shell a failed concurrent build leaves behind. The
        trade-off is acknowledged to the linter on the statement itself."""
        assert "IF NOT EXISTS" not in _statements(INDEX)
        raw = _read(INDEX)
        assert "-- squawk-ignore prefer-robust-stmts" in raw
        marker = raw.index("-- squawk-ignore prefer-robust-stmts")
        assert raw.index("CREATE INDEX CONCURRENTLY") > marker


# ---------------------------------------------------------------------------
# 0208 — the validation
# ---------------------------------------------------------------------------


class TestValidateMigration:
    def test_is_wrapped_and_bounded(self):
        sql = _statements(VALIDATE)
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        assert statements[0].upper() == "BEGIN"
        assert statements[-1].upper() == "COMMIT"
        assert "SET LOCAL lock_timeout" in sql
        assert "SET LOCAL statement_timeout" in sql

    def test_validates_exactly_the_three_constraints_0206_added(self):
        sql = _compact(_statements(VALIDATE))
        for name in CONSTRAINTS:
            assert f"VALIDATE CONSTRAINT {name}" in sql, name
        assert sql.count("VALIDATE CONSTRAINT") == 3
        assert "ALTER TABLE public.threads VALIDATE CONSTRAINT" in sql
        assert not re.search(r"\b(ADD|DROP|CREATE|UPDATE|INSERT|DELETE)\b", sql)


# ---------------------------------------------------------------------------
# The schema of record (regenerated with the migration)
# ---------------------------------------------------------------------------


class TestSchemaArtifact:
    def test_schema_current_carries_the_lane(self):
        schema = (ROOT / "orchestrator/database/schema_current.sql").read_text()
        start = schema.index("CREATE TABLE public.threads (")
        block = schema[start : schema.index(");", start)]
        assert "kind text DEFAULT 'session'::text NOT NULL" in block
        for column in NEW_COLUMNS[1:]:
            assert f"    {column} " in block, column
        assert (
            "CONSTRAINT threads_kind_check CHECK ((kind = ANY (ARRAY['session'::text, "
            "'subagent'::text])))" in block
        )
        assert (
            "CREATE INDEX idx_threads_parent_job ON public.threads USING btree "
            "(parent_job_id) WHERE (parent_job_id IS NOT NULL);" in schema
        )
        assert (
            "ADD CONSTRAINT threads_parent_job_id_fkey FOREIGN KEY (parent_job_id) "
            "REFERENCES public.jobs(id) ON DELETE CASCADE;" in schema
        )
        assert (
            "ADD CONSTRAINT threads_parent_thread_id_fkey FOREIGN KEY "
            "(parent_thread_id) REFERENCES public.threads(id) ON DELETE CASCADE;"
            in schema
        )
        # The status vocabulary the child rows ride on, unchanged.
        assert (
            "CONSTRAINT valid_thread_status CHECK (((status)::text = ANY "
            "((ARRAY['created'::character varying, 'active'::character varying, "
            "'idle'::character varying, 'awaiting_user'::character varying, "
            "'suspended'::character varying, 'ended'::character varying])::text[])))"
            in block
        )

    def test_the_other_two_artifacts_are_not_involved(self):
        for name in ("vector_schema_current.sql", "audit_schema_current.sql"):
            text = (ROOT / "orchestrator/database" / name).read_text()
            assert "subagent_status" not in text
            assert "idx_threads_parent_job" not in text


# ---------------------------------------------------------------------------
# Real-Postgres proof
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scratch_pg_dsn() -> str:
    testcontainers = pytest.importorskip("testcontainers.postgres")
    try:
        container = testcontainers.PostgresContainer("postgres:16")
        container.start()
    except Exception as exc:  # no container runtime on this box
        pytest.skip(f"no container runtime for the 0206-0208 replay: {exc}")
    try:
        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )
    finally:
        container.stop()


def _swap_db(dsn: str, dbname: str) -> str:
    head, _, tail = dsn.rpartition("/")
    query = "?" + tail.split("?", 1)[1] if "?" in tail else ""
    return f"{head}/{dbname}{query}"


def _orchestrator_db(pool: asyncpg.Pool) -> OrchestratorDB:
    db = OrchestratorDB.__new__(OrchestratorDB)
    db._pool = pool
    db._connection_string = "test"
    db._queries = {}

    @asynccontextmanager
    async def acquire():
        async with pool.acquire() as conn:
            yield conn

    db.acquire = acquire
    return db


def _agent_db(pool: asyncpg.Pool) -> AgentDB:
    db = AgentDB.__new__(AgentDB)
    db._pool = pool
    db._queries = {}
    return db


def _metadata(value):
    return json.loads(value) if isinstance(value, str) else value


@pytest.mark.asyncio
async def test_replay_onto_seeded_rows_and_the_child_lifecycle(
    scratch_pg_dsn: str, tmp_path: pathlib.Path
) -> None:
    """Replay the chain through 0205, seed an owner / a job / a session,
    apply 0206-0208 through the real runner (the .notx index included), and
    drive one child through the real accessors on both pools."""
    dbname = f"subagent_threads_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(scratch_pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()

    dsn = _swap_db(scratch_pg_dsn, dbname)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    before = tmp_path / "deployed-through-0205"
    before.mkdir()
    owner = uuid4()
    stranger = uuid4()
    job_id = uuid4()
    session_id = uuid4()
    try:
        for path in discover(MIGRATIONS):
            if path.name >= COLUMNS:
                break
            (before / path.name).write_bytes(path.read_bytes())
        await run_migrations(pool, before)

        async with pool.acquire() as conn:
            for uid, name in ((owner, "owner"), (stranger, "stranger")):
                await conn.execute(
                    "INSERT INTO users (id, display_name) VALUES ($1, $2)", uid, name
                )
            await conn.execute(
                "INSERT INTO jobs (id, description, user_id, status) "
                "VALUES ($1, 'parent job', $2, 'processing')",
                job_id,
                owner,
            )
            await conn.execute(
                "INSERT INTO threads (id, user_id, title, status) "
                "VALUES ($1, $2, 'a session', 'active')",
                session_id,
                owner,
            )

        # --- the upgrade -------------------------------------------------
        await run_migrations(pool, MIGRATIONS)

        async with pool.acquire() as conn:
            for name in (COLUMNS, INDEX, VALIDATE):
                assert await conn.fetchval(
                    "SELECT success FROM schema_migrations WHERE filename=$1", name
                ), name
            session = await conn.fetchrow(
                "SELECT * FROM threads WHERE id = $1", session_id
            )
            assert session["kind"] == "session"
            for column in NEW_COLUMNS[1:]:
                assert session[column] is None, column
            validated = {
                row["conname"]: row["convalidated"]
                for row in await conn.fetch(
                    "SELECT conname, convalidated FROM pg_constraint "
                    "WHERE conrelid = 'public.threads'::regclass "
                    "AND conname = ANY($1::text[])",
                    list(CONSTRAINTS),
                )
            }
            assert validated == dict.fromkeys(CONSTRAINTS, True)
            assert await conn.fetchval(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
                "ON c.oid = i.indexrelid WHERE c.relname = 'idx_threads_parent_job'"
            )

        orchestrator = _orchestrator_db(pool)
        agent = _agent_db(pool)

        # --- open: the row is derived from the job ------------------------
        child_id = str(uuid4())
        created = await orchestrator.create_subagent_thread(
            parent_job_id=str(job_id),
            thread_id=child_id,
            handle="explorer-0001",
            subagent_type="explorer",
            parent_tool_call_id="call-1",
            isolation="shared",
            write_policy="none",
            brief_description="  find the   secret ",
            parent_iteration=7,
        )
        assert created is not None
        assert created["thread_id"] == child_id
        generation = created["runtime_generation"]
        # Idempotent per id; an unknown job is refused before any write.
        assert (
            await orchestrator.create_subagent_thread(
                parent_job_id=str(job_id),
                thread_id=child_id,
                handle="explorer-0001",
                subagent_type="explorer",
                parent_tool_call_id="call-1",
            )
            == created
        )
        assert (
            await orchestrator.create_subagent_thread(
                parent_job_id=str(uuid4()),
                handle="explorer-0002",
                subagent_type="explorer",
            )
            is None
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM threads WHERE id = $1::uuid", child_id
            )
        assert row["kind"] == "subagent"
        assert row["user_id"] == owner and row["project_id"] is None
        assert str(row["parent_job_id"]) == str(job_id)
        assert row["parent_thread_id"] is None
        assert row["parent_tool_call_id"] == "call-1"
        assert row["subagent_handle"] == "explorer-0001"
        assert row["subagent_type"] == "explorer"
        assert row["subagent_status"] == "running"
        assert row["status"] == "active" and row["ended_at"] is None
        assert row["execution_lane"] == "pinned" and row["agent_id"] is None
        assert row["permission_mode"] == "autonomous"
        assert row["config_name"] is None and row["ssh_handle"] is None
        assert row["title"] == "explorer-0001: find the secret"
        metadata = _metadata(row["metadata"])
        assert metadata["datasource_ids"] == []
        assert metadata["subagent"] == {
            "type": "explorer",
            "handle": "explorer-0001",
            "isolation": "shared",
            "write_policy": "none",
            "brief_description": "find the secret",
            "parent_iteration": 7,
            "fork": False,
        }

        # --- the transcript through the agent-side pool -------------------
        saved = await agent.save_thread_message(
            thread_id=child_id,
            id="chatcmpl-child-1",
            role="ai",
            content="the secret is MARMALADE",
            turn_number=1,
        )
        assert saved["seq"] >= 1
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM thread_messages WHERE thread_id = $1::uuid",
                    child_id,
                )
                == 1
            )

        # --- the terminal update ------------------------------------------
        assert not await agent.update_subagent_thread(
            child_id,
            runtime_generation=str(uuid4()),
            status="ended",
            subagent_status="completed",
            ended=True,
        ), "a stale generation cannot win the first terminal write"
        assert await agent.update_subagent_thread(
            child_id,
            runtime_generation=generation,
            status="ended",
            subagent_status="completed",
            outcome="completed",
            turns=3,
            tokens=1200,
            report_path=".subagents/explorer-0001/report.md",
            ended=True,
        )
        # The guard: a session row is never touched by the subagent writer.
        assert not await agent.update_subagent_thread(
            str(session_id),
            runtime_generation=generation,
            status="ended",
            subagent_status="completed",
            ended=True,
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM threads WHERE id = $1::uuid", child_id
            )
            session = await conn.fetchrow(
                "SELECT status, ended_at FROM threads WHERE id = $1", session_id
            )
        assert row["status"] == "ended" and row["ended_at"] is not None
        assert row["subagent_status"] == "completed"
        assert row["subagent_outcome"] == "completed"
        assert row["total_turns"] == 3 and row["total_tokens"] == 1200
        assert row["report_path"] == ".subagents/explorer-0001/report.md"
        assert session["status"] == "active" and session["ended_at"] is None

        # --- revival rotates the run claim; old owners stay fenced --------
        reopened = await orchestrator.reopen_subagent_thread(
            parent_job_id=str(job_id),
            thread_id=child_id,
            runtime_generation=generation,
        )
        assert reopened is not None and reopened["result"] == "reopened"
        next_generation = reopened["runtime_generation"]
        assert next_generation != generation
        async with pool.acquire() as conn:
            revived = await conn.fetchrow(
                "SELECT status, subagent_status, runtime_generation, ended_at "
                "FROM threads WHERE id = $1::uuid",
                child_id,
            )
        assert revived["status"] == "created"
        assert revived["subagent_status"] == "queued"
        assert str(revived["runtime_generation"]) == next_generation
        assert revived["ended_at"] is None
        assert not await agent.update_subagent_thread(
            child_id,
            runtime_generation=generation,
            status="active",
            subagent_status="running",
        )
        assert await agent.update_subagent_thread(
            child_id,
            runtime_generation=next_generation,
            status="active",
            subagent_status="running",
        )

        # --- terminal row + worker delivery are one idempotent operation --
        delivery_id = str(uuid4())
        terminal = await orchestrator.terminalize_subagent_thread_and_enqueue(
            parent_job_id=str(job_id),
            thread_id=child_id,
            runtime_generation=next_generation,
            delivery_id=delivery_id,
            message="the secret is MARMALADE",
            timestamp=datetime(2026, 9, 1, 1, 2, 3, tzinfo=timezone.utc),
            subagent_status="completed",
            outcome="completed",
            turns=1,
            tokens=25,
        )
        assert terminal is not None and terminal["result"] == "applied"
        assert terminal["delivery"]["source"] == "subagent"
        retry = await orchestrator.terminalize_subagent_thread_and_enqueue(
            parent_job_id=str(job_id),
            thread_id=child_id,
            runtime_generation=next_generation,
            delivery_id=delivery_id,
            message="the secret is MARMALADE",
            timestamp=datetime(2026, 9, 1, 1, 2, 3, tzinfo=timezone.utc),
            subagent_status="completed",
        )
        assert retry is not None and retry["result"] == "idempotent"
        assert retry["delivery_state"] == "queued"
        assert (
            await orchestrator.consume_job_guidance(
                str(job_id), reply_threads=[child_id]
            )
            == 1
        )
        consumed_retry = await orchestrator.terminalize_subagent_thread_and_enqueue(
            parent_job_id=str(job_id),
            thread_id=child_id,
            runtime_generation=next_generation,
            delivery_id=delivery_id,
            message="the secret is MARMALADE",
            timestamp=datetime(2026, 9, 1, 1, 2, 3, tzinfo=timezone.utc),
            subagent_status="completed",
        )
        assert consumed_retry is not None
        assert consumed_retry["result"] == "idempotent"
        assert consumed_retry["delivery_state"] == "consumed"

        # --- the reads: roster, replay lookup on both pools, sessions list --
        roster = await orchestrator.list_subagent_threads(str(job_id))
        assert [str(r["id"]) for r in roster] == [child_id]
        assert roster[0]["subagent_status"] == "completed"
        for db in (orchestrator, agent):
            found = await db.get_subagent_thread_by_call(str(job_id), "call-1")
            assert found is not None and str(found["id"]) == child_id
            assert await db.get_subagent_thread_by_call(str(job_id), "call-9") is None
        sessions = await orchestrator.list_threads(user_id=str(owner))
        assert [str(t["id"]) for t in sessions] == [str(session_id)]
        assert await orchestrator.list_threads(status="ended") == []

        # --- the cascade, and the live-child trap -------------------------
        live_id = str(uuid4())
        live_created = await orchestrator.create_subagent_thread(
            parent_job_id=str(job_id),
            thread_id=live_id,
            handle="explorer-0002",
            subagent_type="explorer",
            parent_tool_call_id="call-2",
        )
        assert live_created is not None and live_created["thread_id"] == live_id
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.PostgresError) as blocked:
                await conn.execute("DELETE FROM jobs WHERE id = $1", job_id)
        assert "threads_pinned_delete_authority" in str(
            getattr(blocked.value, "constraint_name", "") or blocked.value
        ), "a live child row blocks the raw cascade — that is the trap"
        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM jobs WHERE id = $1", job_id
            )

        assert await orchestrator.delete_job(str(job_id)) is True
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM threads WHERE kind = 'subagent'"
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM thread_messages WHERE thread_id = $1::uuid",
                    child_id,
                )
                == 0
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM threads WHERE id = $1", session_id
                )
                == 1
            )
    finally:
        await pool.close()
        admin = await asyncpg.connect(scratch_pg_dsn)
        try:
            await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()
