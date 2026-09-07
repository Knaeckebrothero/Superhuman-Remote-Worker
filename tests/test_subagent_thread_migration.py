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

import asyncio
import json
import pathlib
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest

from orchestrator.database.migrate import discover, run_migrations
from orchestrator.database.postgres import (
    PostgresDB as OrchestratorDB,
    SessionParentAuthorityRefused,
)
from agent.api.turn_executor import _PENDING_INPUT_SQL
from agent.database.postgres_db import PostgresDB as AgentDB
from shared.persistent_input_delivery import (
    InputDeliveryConflict,
    claim_stateless_input_delivery,
    persist_input_delivery,
    transition_input_delivery,
    transition_stateless_input_delivery,
)
from shared.session_subagent_authority import session_subagent_delivery_id
from shared.subagent_parent_authority import (
    ParentExecutionAuthority,
    ParentExecutionAuthorityRefused,
    require_parent_execution_authority,
)
from shared.run_queue import (
    UNIT_KIND_SESSION_TURN,
    UNIT_KIND_WORKER_BATCH,
    claim_unit,
    reap_expired,
    record_input_seq,
)
from shared.worker_queue import claim_worker_batch, enqueue_worker_batch

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "src" / "orchestrator" / "database" / "migrations" / "app"
COLUMNS = "0206_threads_subagent_kind.sql"
INDEX = "0207_threads_parent_job_idx.notx.sql"
VALIDATE = "0208_threads_subagent_validate.sql"
PREDECESSOR = "0205_experts_role_tags_backfill.sql"
FENCE_MIGRATION = "0185_thread_runtime_generation_retirement.sql"
SSH_HANDLE_MIGRATION = "0202_threads_ssh_handle.sql"
SESSION_PARENT_SHAPE = "0214_threads_subagent_parent_shape.sql"
SESSION_PARENT_INDEX = "0215_threads_parent_thread_idx.notx.sql"
SESSION_PARENT_VALIDATE = "0216_threads_subagent_parent_shape_validate.notx.sql"
SESSION_PARENT_CALL_UNIQUE = "0217_threads_session_parent_tool_call_unique.notx.sql"
JOB_PARENT_CALL_DEDUPE = "0218_threads_job_parent_tool_call_dedupe.sql"
JOB_PARENT_CALL_UNIQUE = "0219_threads_job_parent_tool_call_unique.notx.sql"
STATELESS_RECOVERY = "0220_stateless_subagent_recovery_events.sql"

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


async def _stamp_stateless_claim(
    conn, thread_id: UUID, *, token: int, pod: str, pod_uid: str
) -> None:
    await conn.execute(
        """
        UPDATE threads
           SET metadata = jsonb_set(
               COALESCE(metadata, '{}'::jsonb),
               '{_stateless_active_claim}',
               jsonb_build_object(
                   'lease_token', $2::bigint,
                   'pod', $3::text,
                   'pod_uid', $4::text
               ),
               true
           )
         WHERE id = $1
        """,
        thread_id,
        token,
        pod,
        pod_uid,
    )


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
        schema = (ROOT / "src/orchestrator/database/schema_current.sql").read_text()
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
            "CREATE UNIQUE INDEX idx_threads_job_parent_tool_call "
            "ON public.threads USING btree (parent_job_id, parent_tool_call_id) "
            "WHERE ((kind = 'subagent'::text) AND "
            "(parent_job_id IS NOT NULL) AND (parent_thread_id IS NULL) AND "
            "(parent_tool_call_id IS NOT NULL));" in schema
        )
        assert "CONSTRAINT threads_parent_shape_check CHECK" in block
        assert "num_nonnulls(parent_job_id, parent_thread_id) = 1" in block
        assert (
            "CREATE INDEX idx_threads_parent_thread ON public.threads USING btree "
            "(parent_thread_id) WHERE (parent_thread_id IS NOT NULL);" in schema
        )
        assert (
            "CREATE UNIQUE INDEX idx_threads_session_parent_tool_call "
            "ON public.threads USING btree (parent_thread_id, parent_tool_call_id) "
            "WHERE ((kind = 'subagent'::text) AND (parent_job_id IS NULL) AND "
            "(parent_thread_id IS NOT NULL) AND "
            "(parent_tool_call_id IS NOT NULL));" in schema
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
            text = (ROOT / "src/orchestrator/database" / name).read_text()
            assert "subagent_status" not in text
            assert "idx_threads_parent_job" not in text


class TestSessionParentHardeningMigrations:
    def test_discovery_orders_the_u5_shape_index_and_validation(self):
        names = [path.name for path in discover(MIGRATIONS)]
        assert (
            names.index(SESSION_PARENT_INDEX) == names.index(SESSION_PARENT_SHAPE) + 1
        )
        assert (
            names.index(SESSION_PARENT_VALIDATE)
            == names.index(SESSION_PARENT_INDEX) + 1
        )
        assert (
            names.index(SESSION_PARENT_CALL_UNIQUE)
            == names.index(SESSION_PARENT_VALIDATE) + 1
        )
        assert (
            names.index(JOB_PARENT_CALL_DEDUPE)
            == names.index(SESSION_PARENT_CALL_UNIQUE) + 1
        )
        assert (
            names.index(JOB_PARENT_CALL_UNIQUE)
            == names.index(JOB_PARENT_CALL_DEDUPE) + 1
        )
        assert (
            names.index(STATELESS_RECOVERY) == names.index(JOB_PARENT_CALL_UNIQUE) + 1
        )

    def test_parent_shape_is_added_not_valid_then_validated(self):
        shape = _compact(_statements(SESSION_PARENT_SHAPE))
        assert "ADD CONSTRAINT threads_parent_shape_check CHECK" in shape
        assert "kind = 'session' AND parent_job_id IS NULL" in shape
        assert (
            "kind = 'subagent' AND num_nonnulls(parent_job_id, parent_thread_id) = 1"
            in shape
        )
        assert ") NOT VALID" in shape
        assert (
            "UPDATE public.threads SET parent_thread_id = NULL WHERE kind = 'subagent' "
            "AND parent_job_id IS NOT NULL AND parent_thread_id IS NOT NULL" in shape
        )
        validate = _compact(_statements(SESSION_PARENT_VALIDATE))
        assert "VALIDATE CONSTRAINT threads_parent_shape_check" in validate

    def test_parent_thread_index_is_concurrent_partial_and_not_transactional(self):
        sql = _compact(_statements(SESSION_PARENT_INDEX))
        assert sql == (
            "CREATE INDEX CONCURRENTLY idx_threads_parent_thread "
            "ON public.threads (parent_thread_id) "
            "WHERE parent_thread_id IS NOT NULL;"
        )
        assert "-- transactional: no" in _read(SESSION_PARENT_INDEX)
        assert "IF NOT EXISTS" not in _statements(SESSION_PARENT_INDEX)

    def test_session_parent_tool_call_is_unique_and_partial(self):
        sql = _compact(_statements(SESSION_PARENT_CALL_UNIQUE))
        assert sql == (
            "CREATE UNIQUE INDEX CONCURRENTLY "
            "idx_threads_session_parent_tool_call ON public.threads "
            "(parent_thread_id, parent_tool_call_id) WHERE kind = 'subagent' "
            "AND parent_job_id IS NULL AND parent_thread_id IS NOT NULL "
            "AND parent_tool_call_id IS NOT NULL;"
        )
        assert "-- transactional: no" in _read(SESSION_PARENT_CALL_UNIQUE)
        assert "IF NOT EXISTS" not in _statements(SESSION_PARENT_CALL_UNIQUE)

    def test_worker_duplicate_repair_keeps_only_the_newest_replay_key(self):
        sql = _compact(_statements(JOB_PARENT_CALL_DEDUPE))
        assert "PARTITION BY parent_job_id, parent_tool_call_id" in sql
        assert "ORDER BY created_at DESC, id DESC" in sql
        assert "SET parent_tool_call_id = NULL" in sql
        assert "ranked.replay_rank > 1" in sql
        assert "BEGIN" in sql and "COMMIT" in sql

    def test_worker_parent_tool_call_is_unique_and_partial(self):
        sql = _compact(_statements(JOB_PARENT_CALL_UNIQUE))
        assert sql == (
            "CREATE UNIQUE INDEX CONCURRENTLY idx_threads_job_parent_tool_call "
            "ON public.threads (parent_job_id, parent_tool_call_id) "
            "WHERE kind = 'subagent' AND parent_job_id IS NOT NULL "
            "AND parent_thread_id IS NULL AND parent_tool_call_id IS NOT NULL;"
        )
        assert "-- transactional: no" in _read(JOB_PARENT_CALL_UNIQUE)
        assert "IF NOT EXISTS" not in _statements(JOB_PARENT_CALL_UNIQUE)

    def test_stateless_recovery_marker_is_positive_and_server_event_only(self):
        sql = _compact(_statements(STATELESS_RECOVERY))
        assert "ADD COLUMN supersedes_input_seq BIGINT" in sql
        assert "supersedes_input_seq > 0" in sql
        assert "NEW.source NOT IN ('officer_wake', 'subagent')" in sql
        assert JOB_PARENT_CALL_UNIQUE in _read(STATELESS_RECOVERY)
        assert "-- transactional: yes" in _read(STATELESS_RECOVERY)


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
    agent_id = uuid4()
    session_id = uuid4()
    pod_uid = f"pod-{uuid4()}"
    process_generation = str(uuid4())
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
                "VALUES ($1, 'parent job', $2, 'created')",
                job_id,
                owner,
            )
            await conn.execute(
                "INSERT INTO agents (id, config_name, status, current_job_id, "
                "metadata, pod_uid) VALUES ($1, 'developer', 'working', $2, "
                "$3::jsonb, $4)",
                agent_id,
                job_id,
                json.dumps({"dispatch_process_generation": process_generation}),
                pod_uid,
            )
            await conn.execute(
                "INSERT INTO threads (id, user_id, title, status) "
                "VALUES ($1, $2, 'a session', 'active')",
                session_id,
                owner,
            )
        # Use the production claim path: migration 0175 requires the
        # server-minted workspace dispatch receipt when a job becomes pinned
        # and processing.  The agent's reciprocal current_job row above makes
        # the later process-identity fence exact.
        assert await _orchestrator_db(pool).claim_job_for_agent(
            str(job_id), str(agent_id)
        )

        # --- the upgrade -------------------------------------------------
        await run_migrations(pool, MIGRATIONS)

        async with pool.acquire() as conn:
            for name in (
                COLUMNS,
                INDEX,
                VALIDATE,
                SESSION_PARENT_SHAPE,
                SESSION_PARENT_INDEX,
                SESSION_PARENT_VALIDATE,
                SESSION_PARENT_CALL_UNIQUE,
                JOB_PARENT_CALL_DEDUPE,
                JOB_PARENT_CALL_UNIQUE,
                STATELESS_RECOVERY,
            ):
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
            assert await conn.fetchval(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conrelid='public.threads'::regclass "
                "AND conname='threads_parent_shape_check'"
            )
            assert await conn.fetchval(
                "SELECT i.indisvalid FROM pg_index i JOIN pg_class c "
                "ON c.oid=i.indexrelid "
                "WHERE c.relname='idx_threads_parent_thread'"
            )
            assert await conn.fetchval(
                "SELECT i.indisunique AND i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid=i.indexrelid "
                "WHERE c.relname='idx_threads_session_parent_tool_call'"
            )
            assert await conn.fetchval(
                "SELECT i.indisunique AND i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid=i.indexrelid "
                "WHERE c.relname='idx_threads_job_parent_tool_call'"
            )

        orchestrator = _orchestrator_db(pool)
        agent = _agent_db(pool)
        authority = ParentExecutionAuthority(
            execution_lane="pinned",
            parent_job_id=job_id,
            agent_id=agent_id,
            pod_uid=pod_uid,
            dispatch_process_generation=process_generation,
        )

        # --- U5: a session is a distinct exact parent authority -----------
        session_agent_id = uuid4()
        session_pod_uid = f"session-pod-{uuid4()}"
        session_pod_name = f"session-{str(session_id)[:12]}"
        session_attempt = str(uuid4())
        async with pool.acquire() as conn:
            session_generation = await conn.fetchval(
                "SELECT runtime_generation FROM threads WHERE id=$1", session_id
            )
        assert await orchestrator.reserve_pinned_agent_pod_provision_intent(
            str(session_id),
            expected_runtime_generation=str(session_generation),
            attempt_id=session_attempt,
            pod_name=session_pod_name,
            provisioner="agent",
            namespace="agents-test",
        )
        assert await orchestrator.publish_pinned_agent_pod_provision_intent(
            str(session_id),
            expected_runtime_generation=str(session_generation),
            attempt_id=session_attempt,
            pod_name=session_pod_name,
            pod_uid=session_pod_uid,
            namespace="agents-test",
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO agents "
                    "(id, config_name, status, metadata, hostname, pod_uid) "
                    "VALUES ($1, 'session_base', 'session', '{}'::jsonb, $2, $3)",
                    session_agent_id,
                    session_pod_name,
                    session_pod_uid,
                )
                binding = await conn.fetchrow(
                    "UPDATE threads SET agent_id=$2 WHERE id=$1 "
                    "RETURNING runtime_generation, runtime_attach_token",
                    session_id,
                    session_agent_id,
                )
                await conn.execute(
                    "UPDATE agents SET thread_id=$2 WHERE id=$1",
                    session_agent_id,
                    session_id,
                )
        session_authority = {
            "version": 1,
            "execution_lane": "pinned",
            "parent_thread_id": str(session_id),
            "agent_id": str(session_agent_id),
            "pod_uid": session_pod_uid,
            "session_runtime_generation": str(binding["runtime_generation"]),
            "runtime_attach_token": str(binding["runtime_attach_token"]),
        }

        session_input_1 = uuid4()
        session_ai_1 = uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, turn_number) "
                "VALUES ($1, $2, 'human', 'start background review', 1)",
                session_input_1,
                session_id,
            )
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', '', $3::jsonb, 1)",
                session_ai_1,
                session_id,
                json.dumps(
                    [
                        {
                            "id": "session-call-1",
                            "name": "delegate_agent",
                            "args": {},
                        }
                    ]
                ),
            )

        session_child_id = str(uuid4())
        session_created = await orchestrator.create_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=session_child_id,
            handle="reviewer-0001",
            subagent_type="reviewer",
            parent_tool_call_id="session-call-1",
            parent_input_message_id=str(session_input_1),
            parent_ai_message_id=str(session_ai_1),
            parent_iteration=1,
            brief_description="review the session work",
            run_in_background=True,
            initial_status="queued",
        )
        assert session_created is not None
        assert (
            await orchestrator.create_session_subagent_thread(
                parent_thread_id=str(session_id),
                parent_authority=session_authority,
                thread_id=session_child_id,
                handle="reviewer-0001",
                subagent_type="reviewer",
                parent_tool_call_id="session-call-1",
                parent_input_message_id=str(session_input_1),
                parent_ai_message_id=str(session_ai_1),
                parent_iteration=1,
                brief_description="review the session work",
                run_in_background=True,
                initial_status="queued",
            )
            == session_created
        )
        with pytest.raises(ValueError, match="different child request"):
            await orchestrator.create_session_subagent_thread(
                parent_thread_id=str(session_id),
                parent_authority=session_authority,
                thread_id=str(uuid4()),
                handle="reviewer-0002",
                subagent_type="reviewer",
                parent_tool_call_id="session-call-1",
                parent_input_message_id=str(session_input_1),
                parent_ai_message_id=str(session_ai_1),
                parent_iteration=1,
                brief_description="review the session work",
                run_in_background=True,
                initial_status="queued",
            )
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM threads WHERE kind='subagent' "
                    "AND parent_thread_id=$1 AND parent_tool_call_id=$2",
                    session_id,
                    "session-call-1",
                )
                == 1
            )
        with pytest.raises(ValueError, match="different child request"):
            await orchestrator.create_session_subagent_thread(
                parent_thread_id=str(session_id),
                parent_authority=session_authority,
                thread_id=session_child_id,
                handle="reviewer-0001",
                subagent_type="reviewer",
                parent_tool_call_id="session-call-1",
                parent_input_message_id=str(session_input_1),
                parent_ai_message_id=str(session_ai_1),
                parent_iteration=1,
                brief_description="changed after the stable id was committed",
                run_in_background=True,
                initial_status="queued",
            )
        session_generation = session_created["runtime_generation"]
        async with pool.acquire() as conn:
            session_child = await conn.fetchrow(
                "SELECT user_id, project_id, parent_job_id, parent_thread_id, "
                "status, subagent_status FROM threads WHERE id=$1",
                UUID(session_child_id),
            )
        assert session_child["user_id"] == owner
        assert session_child["project_id"] is None
        assert session_child["parent_job_id"] is None
        assert session_child["parent_thread_id"] == session_id
        assert session_child["status"] == "created"
        assert session_child["subagent_status"] == "queued"

        found_session_child = await orchestrator.get_session_subagent_thread_by_call(
            str(session_id),
            "session-call-1",
            parent_authority=session_authority,
        )
        assert found_session_child is not None
        assert str(found_session_child["id"]) == session_child_id

        session_terminal, retry_session_terminal = await asyncio.gather(
            orchestrator.terminalize_session_subagent_thread(
                parent_thread_id=str(session_id),
                parent_authority=session_authority,
                thread_id=session_child_id,
                runtime_generation=session_generation,
                subagent_status="completed",
                message="review complete",
                outcome="completed",
                turns=2,
                tokens=50,
            ),
            orchestrator.terminalize_session_subagent_thread(
                parent_thread_id=str(session_id),
                parent_authority=session_authority,
                thread_id=session_child_id,
                runtime_generation=session_generation,
                subagent_status="completed",
                message="review complete",
                outcome="completed",
                turns=2,
                tokens=50,
            ),
        )
        assert session_terminal is not None and retry_session_terminal is not None
        assert {session_terminal["result"], retry_session_terminal["result"]} == {
            "applied",
            "idempotent",
        }
        if session_terminal["result"] != "applied":
            session_terminal, retry_session_terminal = (
                retry_session_terminal,
                session_terminal,
            )
        assert session_terminal["delivery_state"] == "owned"
        assert session_terminal["delivery"]["role"] == "event"
        assert session_terminal["delivery"]["source"] == "subagent"
        assert retry_session_terminal["result"] == "idempotent"
        assert retry_session_terminal["delivery_id"] == session_terminal["delivery_id"]
        async with pool.acquire() as conn:
            delivery = await conn.fetchrow(
                "SELECT delivery.source, delivery.state, message.role, "
                "message.content, delivery.thread_id "
                "FROM thread_input_deliveries delivery "
                "JOIN thread_messages message ON message.id=delivery.message_id "
                "WHERE delivery.delivery_id=$1",
                UUID(session_terminal["delivery_id"]),
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM thread_input_deliveries "
                    "WHERE thread_id=$1 AND source='subagent'",
                    session_id,
                )
                == 1
            )
        assert delivery["source"] == "subagent"
        assert delivery["state"] == "owned"
        assert delivery["role"] == "event"
        assert delivery["content"] == "review complete"
        assert delivery["thread_id"] == session_id

        with pytest.raises(InputDeliveryConflict):
            await orchestrator.terminalize_session_subagent_thread(
                parent_thread_id=str(session_id),
                parent_authority=session_authority,
                thread_id=session_child_id,
                runtime_generation=session_generation,
                subagent_status="completed",
                message="different evidence under the same stable id",
            )
        with pytest.raises(ValueError, match="changed its turns"):
            await orchestrator.terminalize_session_subagent_thread(
                parent_thread_id=str(session_id),
                parent_authority=session_authority,
                thread_id=session_child_id,
                runtime_generation=session_generation,
                subagent_status="completed",
                message="review complete",
                turns=999,
            )

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agents SET pod_uid='replacement-session-pod' WHERE id=$1",
                session_agent_id,
            )
        with pytest.raises(SessionParentAuthorityRefused):
            await orchestrator.get_session_subagent_thread(
                str(session_id),
                session_child_id,
                parent_authority=session_authority,
            )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agents SET pod_uid=$2 WHERE id=$1",
                session_agent_id,
                session_pod_uid,
            )

        session_input_2 = uuid4()
        session_ai_2 = uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, turn_number) "
                "VALUES ($1, $2, 'human', 'start foreground explorer', 2)",
                session_input_2,
                session_id,
            )
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', '', $3::jsonb, 2)",
                session_ai_2,
                session_id,
                json.dumps(
                    [
                        {
                            "id": "session-call-2",
                            "name": "delegate_agent",
                            "args": {},
                        }
                    ]
                ),
            )
        foreground_child_id = str(uuid4())
        foreground = await orchestrator.create_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=foreground_child_id,
            handle="explorer-0003",
            subagent_type="explorer",
            parent_tool_call_id="session-call-2",
            parent_input_message_id=str(session_input_2),
            parent_ai_message_id=str(session_ai_2),
            parent_iteration=2,
        )
        assert foreground is not None
        foreground_terminal = await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=foreground_child_id,
            runtime_generation=foreground["runtime_generation"],
            subagent_status="completed",
            outcome="completed",
        )
        assert foreground_terminal is not None
        assert foreground_terminal["delivery_id"] is None
        reopened = await orchestrator.reopen_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=foreground_child_id,
            runtime_generation=foreground["runtime_generation"],
        )
        assert reopened is not None
        assert reopened["result"] == "reopened"
        assert reopened["runtime_generation"] != foreground["runtime_generation"]
        reconciled_reopen = await orchestrator.reopen_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=foreground_child_id,
            runtime_generation=foreground["runtime_generation"],
        )
        assert reconciled_reopen is not None
        assert reconciled_reopen["result"] == "reopened"
        assert reconciled_reopen["runtime_generation"] == reopened["runtime_generation"]
        assert reconciled_reopen["reconciled"] is True
        reterminalized = await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=foreground_child_id,
            runtime_generation=reopened["runtime_generation"],
            subagent_status="completed",
        )
        assert reterminalized is not None
        assert reterminalized["result"] == "applied"

        # If the child terminal write wins but the parent crashes before its
        # matching ToolMessage lands, attach-time recovery sees that exact
        # foreground gap and converts the child transcript into one stable
        # role=event delivery. A later retry is idempotent, and a parent tool
        # result suppresses any further recovery.
        orphan_call_id = "session-call-terminal-gap"
        orphan_input_id = uuid4()
        orphan_ai_id = uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, turn_number) "
                "VALUES ($1, $2, 'human', 'run orphan child', 3)",
                orphan_input_id,
                session_id,
            )
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', '', $3::jsonb, 3)",
                orphan_ai_id,
                session_id,
                json.dumps(
                    [
                        {
                            "id": orphan_call_id,
                            "name": "delegate_agent",
                            "args": {"prompt": "inspect"},
                        }
                    ]
                ),
            )
        orphan_child_id = str(uuid4())
        orphan_child = await orchestrator.create_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=orphan_child_id,
            handle="explorer-0007",
            subagent_type="explorer",
            parent_tool_call_id=orphan_call_id,
            parent_input_message_id=str(orphan_input_id),
            parent_ai_message_id=str(orphan_ai_id),
            parent_iteration=3,
        )
        assert orphan_child is not None
        await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=orphan_child_id,
            runtime_generation=orphan_child["runtime_generation"],
            subagent_status="completed",
            outcome="completed",
        )
        candidates = await orchestrator.list_live_session_subagent_threads(
            str(session_id), parent_authority=session_authority
        )
        orphan_candidate = next(
            row for row in candidates if str(row["id"]) == orphan_child_id
        )
        assert orphan_candidate["recovery_kind"] == "terminal_foreground"
        recovered_orphan = await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=orphan_child_id,
            runtime_generation=orphan_child["runtime_generation"],
            subagent_status="completed",
            message="durable completed child evidence",
            outcome="completed",
            foreground_orphan_recovery=True,
        )
        retry_orphan = await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=orphan_child_id,
            runtime_generation=orphan_child["runtime_generation"],
            subagent_status="completed",
            message="durable completed child evidence",
            outcome="completed",
            foreground_orphan_recovery=True,
        )
        assert recovered_orphan["delivery_id"] == retry_orphan["delivery_id"]
        assert retry_orphan["result"] == "idempotent"
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM thread_input_deliveries WHERE delivery_id=$1",
                    UUID(recovered_orphan["delivery_id"]),
                )
                == 1
            )
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_call_id, turn_number) "
                "VALUES ($1, $2, 'tool', 'normal result', $3, 3)",
                uuid4(),
                session_id,
                orphan_call_id,
            )
        assert (
            await orchestrator.terminalize_session_subagent_thread(
                parent_thread_id=str(session_id),
                parent_authority=session_authority,
                thread_id=orphan_child_id,
                runtime_generation=orphan_child["runtime_generation"],
                subagent_status="completed",
                message="durable completed child evidence",
                outcome="completed",
                foreground_orphan_recovery=True,
            )
        )["result"] == "idempotent"

        # A settled source plus ToolMessage is not enough to prove the user saw
        # a final answer. If the final AI row is missing, recovery queues one
        # no-delegation continuation even though ordinary turn settlement had
        # already advanced the pinned source ledger.
        delivered_live_call = "session-call-live-already-delivered"
        delivered_input_delivery = uuid4()
        delivered_ai_id = uuid4()
        async with pool.acquire() as conn:
            delivered_source = await persist_input_delivery(
                conn,
                thread_id=session_id,
                delivery_id=delivered_input_delivery,
                role="human",
                content="run live delivered child",
                source="direct_human",
                turn_number=4,
                agent_id=session_agent_id,
                pod_uid=session_pod_uid,
                runtime_generation=binding["runtime_generation"],
                session_runtime_generation=binding["runtime_generation"],
                runtime_attach_token=binding["runtime_attach_token"],
            )
            for transition in ("admitted", "settled"):
                assert await transition_input_delivery(
                    conn,
                    delivery_id=delivered_input_delivery,
                    agent_id=session_agent_id,
                    pod_uid=session_pod_uid,
                    runtime_generation=binding["runtime_generation"],
                    session_runtime_generation=binding["runtime_generation"],
                    runtime_attach_token=binding["runtime_attach_token"],
                    claim_generation=delivered_source["claim_generation"],
                    transition=transition,
                    turn_number=4 if transition == "admitted" else None,
                )
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', '', $3::jsonb, 4)",
                delivered_ai_id,
                session_id,
                json.dumps(
                    [
                        {
                            "id": delivered_live_call,
                            "name": "delegate_agent",
                            "args": {},
                        }
                    ]
                ),
            )
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_call_id, turn_number) "
                "VALUES ($1, $2, 'tool', 'normal live result', $3, 4)",
                uuid4(),
                session_id,
                delivered_live_call,
            )
        delivered_live_id = str(uuid4())
        delivered_live = await orchestrator.create_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=delivered_live_id,
            handle="explorer-0009",
            subagent_type="explorer",
            parent_tool_call_id=delivered_live_call,
            parent_input_message_id=str(delivered_source["message_id"]),
            parent_ai_message_id=str(delivered_ai_id),
            parent_iteration=4,
        )
        delivered_live_recovery = (
            await orchestrator.terminalize_session_subagent_thread(
                parent_thread_id=str(session_id),
                parent_authority=session_authority,
                thread_id=delivered_live_id,
                runtime_generation=delivered_live["runtime_generation"],
                subagent_status="interrupted",
                message="evidence that must not be duplicated",
                outcome="interrupted:parent_restart",
                foreground_orphan_recovery=True,
            )
        )
        assert delivered_live_recovery["result"] == "applied"
        assert delivered_live_recovery["delivery_id"] is not None
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT status FROM threads WHERE id=$1", UUID(delivered_live_id)
                )
                == "ended"
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM thread_input_deliveries "
                    "WHERE thread_id=$1 AND source='subagent'",
                    session_id,
                )
                == 3
            )
            assert (
                await conn.fetchval(
                    "SELECT message.content FROM thread_input_deliveries delivery "
                    "JOIN thread_messages message ON message.id=delivery.message_id "
                    "WHERE delivery.delivery_id=$1",
                    UUID(delivered_live_recovery["delivery_id"]),
                )
                == "[subagent recovery] The original delegate_agent ToolMessage "
                "is already durable in this conversation, but the parent turn "
                "ended before its final response was recorded. Continue from that "
                "tool result and answer the original request directly. Do not "
                "delegate replacement work for this recovery turn."
            )

        # Pinned crash seam after the parent has persisted its final response:
        # the admitted source receipt plus exact ToolMessage/final AI is enough
        # to prevent a second provider turn. Post-turn memory/Git hooks are
        # healable and must not buy another answer.
        pinned_final_call = "session-call-final-response"
        pinned_final_delivery = uuid4()
        pinned_final_ai = uuid4()
        async with pool.acquire() as conn:
            async with conn.transaction():
                pinned_source = await persist_input_delivery(
                    conn,
                    thread_id=session_id,
                    delivery_id=pinned_final_delivery,
                    role="human",
                    content="delegate then answer once",
                    source="direct_human",
                    turn_number=5,
                    agent_id=session_agent_id,
                    pod_uid=session_pod_uid,
                    runtime_generation=binding["runtime_generation"],
                    session_runtime_generation=binding["runtime_generation"],
                    runtime_attach_token=binding["runtime_attach_token"],
                )
                assert await transition_input_delivery(
                    conn,
                    delivery_id=pinned_final_delivery,
                    agent_id=session_agent_id,
                    pod_uid=session_pod_uid,
                    runtime_generation=binding["runtime_generation"],
                    session_runtime_generation=binding["runtime_generation"],
                    runtime_attach_token=binding["runtime_attach_token"],
                    claim_generation=pinned_source["claim_generation"],
                    transition="admitted",
                    turn_number=5,
                )
                await conn.execute(
                    "INSERT INTO thread_messages "
                    "(id, thread_id, role, content, tool_calls, turn_number) "
                    "VALUES ($1, $2, 'ai', '', $3::jsonb, 5)",
                    pinned_final_ai,
                    session_id,
                    json.dumps(
                        [
                            {
                                "id": pinned_final_call,
                                "name": "delegate_agent",
                                "args": {},
                            }
                        ]
                    ),
                )
        pinned_final_child = await orchestrator.create_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            handle="explorer-0013",
            subagent_type="explorer",
            parent_tool_call_id=pinned_final_call,
            parent_input_message_id=str(pinned_source["message_id"]),
            parent_ai_message_id=str(pinned_final_ai),
            parent_iteration=5,
        )
        await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=pinned_final_child["thread_id"],
            runtime_generation=pinned_final_child["runtime_generation"],
            subagent_status="completed",
            outcome="completed",
        )
        # A future pinned input may be persisted while this turn is still
        # running. It is not a transcript boundary until it is admitted.
        pinned_later_delivery = uuid4()
        async with pool.acquire() as conn:
            async with conn.transaction():
                pinned_later = await persist_input_delivery(
                    conn,
                    thread_id=session_id,
                    delivery_id=pinned_later_delivery,
                    role="human",
                    content="queued behind the child turn",
                    source="direct_human",
                    turn_number=6,
                    agent_id=session_agent_id,
                    pod_uid=session_pod_uid,
                    runtime_generation=binding["runtime_generation"],
                    session_runtime_generation=binding["runtime_generation"],
                    runtime_attach_token=binding["runtime_attach_token"],
                )
        assert pinned_later["state"] == "owned"
        async with pool.acquire() as conn:
            # The final response can survive even when incremental ToolMessage
            # persistence did not. A settled source plus this exact response is
            # enough to suppress a second provider answer.
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', 'one final answer', '[]'::jsonb, 5)",
                uuid4(),
                session_id,
            )
        pinned_final_recovery = await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=pinned_final_child["thread_id"],
            runtime_generation=pinned_final_child["runtime_generation"],
            subagent_status="completed",
            outcome="completed",
            message="must not become another provider turn",
            foreground_orphan_recovery=True,
        )
        assert pinned_final_recovery["result"] == "already_delivered"
        assert pinned_final_recovery["delivery_id"] is None
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT state FROM thread_input_deliveries WHERE delivery_id=$1",
                    pinned_final_delivery,
                )
                == "settled"
            )
            assert (
                await conn.fetchval(
                    "SELECT state FROM thread_input_deliveries WHERE delivery_id=$1",
                    pinned_later_delivery,
                )
                == "owned"
            )

        session_roster = await orchestrator.list_session_subagent_threads(
            str(session_id)
        )
        assert [str(row["id"]) for row in session_roster] == [
            session_child_id,
            foreground_child_id,
            orphan_child_id,
            delivered_live_id,
            pinned_final_child["thread_id"],
        ]

        # A disposable session-turn executor may create a foreground child
        # under its exact run_queue lease, but background work is explicitly
        # refused because that lease cannot outlive the turn.
        stateless_session_id = uuid4()
        stateless_input_id = uuid4()
        stateless_ai_id = uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO threads "
                "(id, user_id, title, status, execution_lane) "
                "VALUES ($1, $2, 'stateless parent', 'active', 'stateless')",
                stateless_session_id,
                owner,
            )
            stateless_source_seq = await conn.fetchval(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, turn_number) "
                "VALUES ($1, $2, 'human', 'delegate the stateless child', 1) "
                "RETURNING seq",
                stateless_input_id,
                stateless_session_id,
            )
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', '', $3::jsonb, 1)",
                stateless_ai_id,
                stateless_session_id,
                json.dumps(
                    [
                        {
                            "id": "stateless-call-recovery",
                            "name": "delegate_agent",
                            "args": {},
                        }
                    ]
                ),
            )
            await conn.execute(
                "UPDATE threads SET total_turns=1 WHERE id=$1",
                stateless_session_id,
            )
            await record_input_seq(
                conn,
                unit_id=stateless_session_id,
                unit_kind=UNIT_KIND_SESSION_TURN,
                input_seq=int(stateless_source_seq),
                fair_key=str(owner),
            )
            stateless_claim = await claim_unit(
                conn,
                unit_kind=UNIT_KIND_SESSION_TURN,
                pod_name="stateless-executor-1",
                prefer_unit_id=stateless_session_id,
            )
            assert stateless_claim is not None
            await _stamp_stateless_claim(
                conn,
                stateless_session_id,
                token=stateless_claim.lease_token,
                pod="stateless-executor-1",
                pod_uid="stateless-executor-pod-1",
            )
        assert stateless_claim is not None
        async with pool.acquire() as conn:
            stateless_later_id = uuid4()
            stateless_later_seq = await conn.fetchval(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, turn_number) "
                "VALUES ($1, $2, 'human', 'later stateless request', 2) "
                "RETURNING seq",
                stateless_later_id,
                stateless_session_id,
            )
            await conn.execute(
                "UPDATE threads SET total_turns=2 WHERE id=$1",
                stateless_session_id,
            )
            await record_input_seq(
                conn,
                unit_id=stateless_session_id,
                unit_kind=UNIT_KIND_SESSION_TURN,
                input_seq=int(stateless_later_seq),
                fair_key=str(owner),
            )
        stateless_authority = {
            "version": 1,
            "execution_lane": "stateless",
            "parent_thread_id": str(stateless_session_id),
            "lease_token": stateless_claim.lease_token,
            "executor_id": "stateless-executor-1",
            "executor_pod_uid": "stateless-executor-pod-1",
        }
        stateless_child = await orchestrator.create_session_subagent_thread(
            parent_thread_id=str(stateless_session_id),
            parent_authority=stateless_authority,
            handle="explorer-0004",
            subagent_type="explorer",
            parent_tool_call_id="stateless-call-recovery",
            parent_input_message_id=str(stateless_input_id),
            parent_ai_message_id=str(stateless_ai_id),
            parent_iteration=1,
        )
        assert stateless_child is not None
        with pytest.raises(SessionParentAuthorityRefused) as unsupported:
            await orchestrator.create_session_subagent_thread(
                parent_thread_id=str(stateless_session_id),
                parent_authority=stateless_authority,
                handle="reviewer-0005",
                subagent_type="reviewer",
                parent_tool_call_id="stateless-call-recovery",
                parent_input_message_id=str(stateless_input_id),
                parent_ai_message_id=str(stateless_ai_id),
                parent_iteration=1,
                run_in_background=True,
                initial_status="queued",
            )
        assert unsupported.value.reason == "stateless_background_unsupported"
        stale_stateless_authority = {
            **stateless_authority,
            "lease_token": stateless_claim.lease_token + 1,
        }
        with pytest.raises(SessionParentAuthorityRefused) as stale_stateless:
            await orchestrator.get_session_subagent_thread(
                str(stateless_session_id),
                stateless_child["thread_id"],
                parent_authority=stale_stateless_authority,
            )
        assert stale_stateless.value.reason == "stateless_parent_not_current"

        # A live stateless foreground orphan uses the same stable server-event
        # path under the exact current queue lease. Migration 0220 is the DB
        # half of this admission; arbitrary stateless input remains refused.
        stateless_recovery = stateless_child
        assert stateless_recovery is not None
        stateless_event = await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(stateless_session_id),
            parent_authority=stateless_authority,
            thread_id=stateless_recovery["thread_id"],
            runtime_generation=stateless_recovery["runtime_generation"],
            subagent_status="interrupted",
            message="stateless child partial evidence",
            outcome="interrupted:parent_restart",
            foreground_orphan_recovery=True,
        )
        async with pool.acquire() as conn:
            stateless_delivery = await conn.fetchrow(
                "SELECT delivery.source, delivery.supersedes_input_seq, "
                "message.role, message.content, message.turn_number, message.seq "
                "FROM thread_input_deliveries delivery "
                "JOIN thread_messages message ON message.id=delivery.message_id "
                "WHERE delivery.delivery_id=$1",
                UUID(stateless_event["delivery_id"]),
            )
            stateless_queue_consumed = await conn.fetchval(
                "SELECT consumed_seq FROM run_queue WHERE unit_id=$1",
                stateless_session_id,
            )
            stateless_pending = await conn.fetch(
                _PENDING_INPUT_SQL,
                stateless_session_id,
                int(stateless_queue_consumed),
                10,
            )
        assert stateless_delivery["source"] == "subagent"
        assert stateless_delivery["role"] == "event"
        assert stateless_delivery["content"] == "stateless child partial evidence"
        assert stateless_delivery["turn_number"] == 1
        assert stateless_delivery["supersedes_input_seq"] == stateless_source_seq
        assert stateless_queue_consumed == stateless_source_seq
        assert [row["seq"] for row in stateless_pending[:2]] == [
            stateless_delivery["seq"],
            stateless_later_seq,
        ]

        # Keep a distinct foreground child live so the later Force-End proves
        # stateless retirement cancellation, independently of the recovered
        # generation above.
        stateless_retirement_call = "stateless-call-retirement"
        stateless_retirement_ai = uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', '', $3::jsonb, 2)",
                stateless_retirement_ai,
                stateless_session_id,
                json.dumps(
                    [
                        {
                            "id": stateless_retirement_call,
                            "name": "delegate_agent",
                            "args": {},
                        }
                    ]
                ),
            )
        stateless_child = await orchestrator.create_session_subagent_thread(
            parent_thread_id=str(stateless_session_id),
            parent_authority=stateless_authority,
            handle="explorer-0012",
            subagent_type="explorer",
            parent_tool_call_id=stateless_retirement_call,
            parent_input_message_id=str(stateless_later_id),
            parent_ai_message_id=str(stateless_retirement_ai),
            parent_iteration=2,
        )
        assert stateless_child is not None

        # A server event can itself be the abandoned parent input. Its old
        # admitted delivery is settled in the same transaction that queues
        # the child recovery; it never remains as stale-token ledger debt.
        event_parent_id = uuid4()
        event_source_delivery_id = uuid4()
        event_child_call = "stateless-event-child-call"
        event_ai_id = uuid4()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO threads "
                    "(id, user_id, title, status, execution_lane) "
                    "VALUES ($1, $2, 'event parent', 'active', 'stateless')",
                    event_parent_id,
                    owner,
                )
                event_source = await persist_input_delivery(
                    conn,
                    thread_id=event_parent_id,
                    delivery_id=event_source_delivery_id,
                    role="event",
                    content="server event that delegates",
                    source="officer_wake",
                    turn_number=1,
                )
                event_claim = await claim_unit(
                    conn,
                    unit_kind=UNIT_KIND_SESSION_TURN,
                    pod_name="stateless-event-executor",
                    prefer_unit_id=event_parent_id,
                )
                assert event_claim is not None
                await _stamp_stateless_claim(
                    conn,
                    event_parent_id,
                    token=event_claim.lease_token,
                    pod="stateless-event-executor",
                    pod_uid="stateless-event-pod",
                )
                claimed_event = await claim_stateless_input_delivery(
                    conn,
                    thread_id=event_parent_id,
                    delivery_id=event_source_delivery_id,
                    lease_token=event_claim.lease_token,
                    executor_id="stateless-event-executor",
                    pod_uid="stateless-event-pod",
                )
                assert claimed_event is not None
                assert await transition_stateless_input_delivery(
                    conn,
                    thread_id=event_parent_id,
                    delivery_id=event_source_delivery_id,
                    lease_token=event_claim.lease_token,
                    executor_id="stateless-event-executor",
                    pod_uid="stateless-event-pod",
                    claim_generation=claimed_event["claim_generation"],
                    transition="admitted",
                    turn_number=1,
                )
                await conn.execute(
                    "INSERT INTO thread_messages "
                    "(id, thread_id, role, content, tool_calls, turn_number) "
                    "VALUES ($1, $2, 'ai', '', $3::jsonb, 1)",
                    event_ai_id,
                    event_parent_id,
                    json.dumps(
                        [
                            {
                                "id": event_child_call,
                                "name": "delegate_agent",
                                "args": {},
                            }
                        ]
                    ),
                )
        event_authority = {
            "version": 1,
            "execution_lane": "stateless",
            "parent_thread_id": str(event_parent_id),
            "lease_token": event_claim.lease_token,
            "executor_id": "stateless-event-executor",
            "executor_pod_uid": "stateless-event-pod",
        }
        event_child = await orchestrator.create_session_subagent_thread(
            parent_thread_id=str(event_parent_id),
            parent_authority=event_authority,
            handle="explorer-0010",
            subagent_type="explorer",
            parent_tool_call_id=event_child_call,
            parent_input_message_id=str(event_source["message_id"]),
            parent_ai_message_id=str(event_ai_id),
            parent_iteration=1,
        )
        event_recovery = await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(event_parent_id),
            parent_authority=event_authority,
            thread_id=event_child["thread_id"],
            runtime_generation=event_child["runtime_generation"],
            subagent_status="interrupted",
            message="event child partial evidence",
            outcome="interrupted:parent_restart",
            foreground_orphan_recovery=True,
        )
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT state FROM thread_input_deliveries WHERE delivery_id=$1",
                    event_source_delivery_id,
                )
                == "settled"
            )
            assert (
                await conn.fetchval(
                    "SELECT supersedes_input_seq FROM thread_input_deliveries "
                    "WHERE delivery_id=$1",
                    UUID(event_recovery["delivery_id"]),
                )
                == event_source["seq"]
            )

        # If the final parent AI response and the authoritative stateless
        # completion effect both landed, recovery performs only the missed
        # queue checkpoint. It never asks the provider to answer twice.
        finalized_parent_id = uuid4()
        finalized_execution_id = uuid4()
        finalized_call = "stateless-finalized-child-call"
        finalized_input_id = uuid4()
        finalized_ai_id = uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO threads "
                "(id, user_id, title, status, execution_lane) "
                "VALUES ($1, $2, 'finalized parent', 'active', 'stateless')",
                finalized_parent_id,
                owner,
            )
            finalized_source_seq = await conn.fetchval(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, turn_number, turn_execution_id) "
                "VALUES ($1, $2, 'human', 'delegate then answer', 1, $3) "
                "RETURNING seq",
                finalized_input_id,
                finalized_parent_id,
                finalized_execution_id,
            )
            await conn.execute(
                "UPDATE threads SET total_turns=1 WHERE id=$1",
                finalized_parent_id,
            )
            await record_input_seq(
                conn,
                unit_id=finalized_parent_id,
                unit_kind=UNIT_KIND_SESSION_TURN,
                input_seq=int(finalized_source_seq),
                fair_key=str(owner),
            )
            finalized_claim = await claim_unit(
                conn,
                unit_kind=UNIT_KIND_SESSION_TURN,
                pod_name="stateless-finalized-executor",
                prefer_unit_id=finalized_parent_id,
            )
            assert finalized_claim is not None
            await _stamp_stateless_claim(
                conn,
                finalized_parent_id,
                token=finalized_claim.lease_token,
                pod="stateless-finalized-executor",
                pod_uid="stateless-finalized-pod",
            )
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', '', $3::jsonb, 1)",
                finalized_ai_id,
                finalized_parent_id,
                json.dumps(
                    [{"id": finalized_call, "name": "delegate_agent", "args": {}}]
                ),
            )
            finalized_later_id = uuid4()
            finalized_later_seq = await conn.fetchval(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, turn_number) "
                "VALUES ($1, $2, 'human', 'queued future input', 2) "
                "RETURNING seq",
                finalized_later_id,
                finalized_parent_id,
            )
            await record_input_seq(
                conn,
                unit_id=finalized_parent_id,
                unit_kind=UNIT_KIND_SESSION_TURN,
                input_seq=int(finalized_later_seq),
                fair_key=str(owner),
            )
            # Reconciliation may restore the missing ToolMessage after the
            # final AI row. Sequence order is physical persistence order, not
            # the logical LangGraph message order.
            finalized_end_seq = await conn.fetchval(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', 'final answer', '[]'::jsonb, 1) "
                "RETURNING seq",
                uuid4(),
                finalized_parent_id,
            )
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_call_id, turn_number) "
                "VALUES ($1, $2, 'tool', 'child result', $3, 1)",
                uuid4(),
                finalized_parent_id,
                finalized_call,
            )
            await conn.execute(
                "INSERT INTO completion_effects "
                "(producer_kind, producer_id, scope_id, effect_name, "
                "effect_group, state, detail) "
                "VALUES ('session_turn', $1, $2, 'final_memory_extraction', "
                "'memory_extraction', 'pending', $3::jsonb)",
                finalized_execution_id,
                finalized_parent_id,
                json.dumps(
                    {
                        "input_message_id": str(finalized_input_id),
                        "turn_number": 1,
                        "memory_scope_kind": "thread",
                        "memory_scope_id": str(finalized_parent_id),
                        "boundary_seq": int(finalized_source_seq),
                        "end_seq": int(finalized_end_seq),
                    }
                ),
            )
        finalized_authority = {
            "version": 1,
            "execution_lane": "stateless",
            "parent_thread_id": str(finalized_parent_id),
            "lease_token": finalized_claim.lease_token,
            "executor_id": "stateless-finalized-executor",
            "executor_pod_uid": "stateless-finalized-pod",
        }
        finalized_child = await orchestrator.create_session_subagent_thread(
            parent_thread_id=str(finalized_parent_id),
            parent_authority=finalized_authority,
            handle="explorer-0011",
            subagent_type="explorer",
            parent_tool_call_id=finalized_call,
            parent_input_message_id=str(finalized_input_id),
            parent_ai_message_id=str(finalized_ai_id),
            parent_iteration=1,
        )
        await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(finalized_parent_id),
            parent_authority=finalized_authority,
            thread_id=finalized_child["thread_id"],
            runtime_generation=finalized_child["runtime_generation"],
            subagent_status="completed",
            outcome="completed",
        )
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE completion_effects "
                "SET detail = jsonb_set(detail, '{end_seq}', $2::jsonb) "
                "WHERE producer_kind='session_turn' AND producer_id=$1",
                finalized_execution_id,
                json.dumps(int(finalized_end_seq) - 1),
            )
        with pytest.raises(ValueError, match="authoritative settled turn boundary"):
            await orchestrator.terminalize_session_subagent_thread(
                parent_thread_id=str(finalized_parent_id),
                parent_authority=finalized_authority,
                thread_id=finalized_child["thread_id"],
                runtime_generation=finalized_child["runtime_generation"],
                subagent_status="completed",
                outcome="completed",
                message="malformed effect cannot suppress provider recovery",
                foreground_orphan_recovery=True,
            )
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT consumed_seq FROM run_queue WHERE unit_id=$1",
                    finalized_parent_id,
                )
                == int(finalized_source_seq) - 1
            )
            await conn.execute(
                "UPDATE completion_effects "
                "SET detail = jsonb_set(detail, '{end_seq}', $2::jsonb) "
                "WHERE producer_kind='session_turn' AND producer_id=$1",
                finalized_execution_id,
                json.dumps(int(finalized_end_seq)),
            )
        finalized_recovery = await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(finalized_parent_id),
            parent_authority=finalized_authority,
            thread_id=finalized_child["thread_id"],
            runtime_generation=finalized_child["runtime_generation"],
            subagent_status="completed",
            outcome="completed",
            message="must not become a second provider turn",
            foreground_orphan_recovery=True,
        )
        assert finalized_recovery["result"] == "already_delivered"
        assert finalized_recovery["delivery_id"] is None
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT consumed_seq FROM run_queue WHERE unit_id=$1",
                    finalized_parent_id,
                )
                == finalized_source_seq
            )
            assert finalized_later_seq > finalized_source_seq
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM thread_input_deliveries "
                    "WHERE thread_id=$1 AND source='subagent'",
                    finalized_parent_id,
                )
                == 0
            )

        # Incremental final-AI persistence can win before the stateless batch
        # reconcile mints its completion effect. Recovery adopts that exact
        # response under the current lease instead of invoking the provider a
        # second time or wedging forever on the missing effect.
        no_effect_call = "stateless-final-without-effect"
        no_effect_ai_id = uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', '', $3::jsonb, 2)",
                no_effect_ai_id,
                finalized_parent_id,
                json.dumps(
                    [{"id": no_effect_call, "name": "delegate_agent", "args": {}}]
                ),
            )
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', 'durable incremental answer', "
                "'[]'::jsonb, 2)",
                uuid4(),
                finalized_parent_id,
            )
        no_effect_child = await orchestrator.create_session_subagent_thread(
            parent_thread_id=str(finalized_parent_id),
            parent_authority=finalized_authority,
            handle="explorer-0014",
            subagent_type="explorer",
            parent_tool_call_id=no_effect_call,
            parent_input_message_id=str(finalized_later_id),
            parent_ai_message_id=str(no_effect_ai_id),
            parent_iteration=2,
        )
        await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(finalized_parent_id),
            parent_authority=finalized_authority,
            thread_id=no_effect_child["thread_id"],
            runtime_generation=no_effect_child["runtime_generation"],
            subagent_status="completed",
            outcome="completed",
        )
        no_effect_recovery = await orchestrator.terminalize_session_subagent_thread(
            parent_thread_id=str(finalized_parent_id),
            parent_authority=finalized_authority,
            thread_id=no_effect_child["thread_id"],
            runtime_generation=no_effect_child["runtime_generation"],
            subagent_status="completed",
            outcome="completed",
            message="must not trigger a duplicate answer",
            foreground_orphan_recovery=True,
        )
        assert no_effect_recovery["result"] == "already_delivered"
        assert no_effect_recovery["delivery_id"] is None
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT consumed_seq FROM run_queue WHERE unit_id=$1",
                    finalized_parent_id,
                )
                == finalized_later_seq
            )

        # Reopen is idempotent across a committed generation rotation whose
        # response was lost: retrying G1 adopts only the pristine queued G2.
        reopened_once = await orchestrator.reopen_session_subagent_thread(
            parent_thread_id=str(finalized_parent_id),
            thread_id=no_effect_child["thread_id"],
            runtime_generation=no_effect_child["runtime_generation"],
            parent_authority=finalized_authority,
        )
        reopened_retry = await orchestrator.reopen_session_subagent_thread(
            parent_thread_id=str(finalized_parent_id),
            thread_id=no_effect_child["thread_id"],
            runtime_generation=no_effect_child["runtime_generation"],
            parent_authority=finalized_authority,
        )
        assert reopened_once["result"] == "reopened"
        assert reopened_retry["result"] == "reopened"
        assert (
            reopened_retry["runtime_generation"] == reopened_once["runtime_generation"]
        )
        assert reopened_retry["reconciled"] is True

        # Retirement and a last child terminal write serialize parent first.
        # Drive both against real row locks: either the completed result wins
        # before retirement observes the child, or retirement owns the live
        # generation and its stable cancellation evidence wins. There is no
        # deadlock, duplicate event, or mixed terminal row/delivery pair.
        race_input_id = uuid4()
        race_ai_id = uuid4()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, turn_number) "
                "VALUES ($1, $2, 'human', 'start race child', 5)",
                race_input_id,
                session_id,
            )
            await conn.execute(
                "INSERT INTO thread_messages "
                "(id, thread_id, role, content, tool_calls, turn_number) "
                "VALUES ($1, $2, 'ai', '', $3::jsonb, 5)",
                race_ai_id,
                session_id,
                json.dumps(
                    [
                        {
                            "id": "session-call-race",
                            "name": "delegate_agent",
                            "args": {},
                        }
                    ]
                ),
            )
        race_child_id = str(uuid4())
        race_child = await orchestrator.create_session_subagent_thread(
            parent_thread_id=str(session_id),
            parent_authority=session_authority,
            thread_id=race_child_id,
            handle="probe-0006",
            subagent_type="probe",
            parent_tool_call_id="session-call-race",
            parent_input_message_id=str(race_input_id),
            parent_ai_message_id=str(race_ai_id),
            parent_iteration=5,
            run_in_background=True,
            initial_status="queued",
        )
        assert race_child is not None

        async def _terminalize_race_child():
            return await orchestrator.terminalize_session_subagent_thread(
                parent_thread_id=str(session_id),
                parent_authority=session_authority,
                thread_id=race_child_id,
                runtime_generation=race_child["runtime_generation"],
                subagent_status="completed",
                message="race child complete",
                outcome="completed",
            )

        async def _retire_race_child():
            async with pool.acquire() as conn:
                async with conn.transaction():
                    return await orchestrator._terminalize_live_session_subagents_for_retirement(
                        conn,
                        parent_thread_id=session_id,
                        execution_lane="pinned",
                        disposition="ended",
                    )

        terminal_race, retirement_race = await asyncio.wait_for(
            asyncio.gather(
                _terminalize_race_child(),
                _retire_race_child(),
                return_exceptions=True,
            ),
            timeout=5,
        )
        assert not isinstance(retirement_race, BaseException)
        race_delivery_id = session_subagent_delivery_id(
            race_child_id, race_child["runtime_generation"]
        )
        async with pool.acquire() as conn:
            race_row = await conn.fetchrow(
                "SELECT status, subagent_status, subagent_outcome "
                "FROM threads WHERE id=$1",
                UUID(race_child_id),
            )
            race_delivery = await conn.fetchrow(
                "SELECT delivery.state, message.role, message.content "
                "FROM thread_input_deliveries delivery "
                "JOIN thread_messages message ON message.id=delivery.message_id "
                "WHERE delivery.delivery_id=$1",
                race_delivery_id,
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM thread_input_deliveries WHERE delivery_id=$1",
                    race_delivery_id,
                )
                == 1
            )
        assert race_row["status"] == "ended"
        assert race_delivery["role"] == "event"
        if race_row["subagent_status"] == "completed":
            assert not isinstance(terminal_race, BaseException)
            assert retirement_race == {"terminalized": 0, "deliveries": 0}
            assert race_row["subagent_outcome"] == "completed"
            assert race_delivery["state"] == "owned"
            assert race_delivery["content"] == "race child complete"
        else:
            assert race_row["subagent_status"] == "cancelled"
            assert race_row["subagent_outcome"] == "cancelled:parent_retired"
            assert isinstance(terminal_race, ValueError)
            assert retirement_race == {"terminalized": 1, "deliveries": 1}
            assert race_delivery["state"] == "persisted"
            assert "cancelled:parent_retired" in race_delivery["content"]

        # A real stateless Force-End closes the queue and cancels the foreground
        # child in the same parent-locked transaction. The one prior
        # source=subagent row is the explicit foreground-restart event above;
        # retirement itself creates no additional delivery.
        stateless_closed = (
            await orchestrator.begin_stateless_thread_workspace_retirement(
                str(stateless_session_id), force=True, permanent=False
            )
        )
        assert stateless_closed["state"] == "closed"
        async with pool.acquire() as conn:
            retired_stateless_child = await conn.fetchrow(
                "SELECT status, subagent_status, subagent_outcome "
                "FROM threads WHERE id=$1",
                UUID(stateless_child["thread_id"]),
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM thread_input_deliveries "
                    "WHERE thread_id=$1 AND source='subagent'",
                    stateless_session_id,
                )
                == 1
            )
        assert retired_stateless_child["status"] == "ended"
        assert retired_stateless_child["subagent_status"] == "cancelled"
        assert retired_stateless_child["subagent_outcome"] == "cancelled:parent_retired"

        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "INSERT INTO threads "
                    "(id, kind, status, parent_job_id, parent_thread_id, "
                    "subagent_status) VALUES "
                    "($1, 'subagent', 'created', $2, $3, 'queued')",
                    uuid4(),
                    job_id,
                    session_id,
                )

        # --- open: the row is derived from the job ------------------------
        child_id = str(uuid4())
        created = await orchestrator.create_subagent_thread(
            parent_job_id=str(job_id),
            parent_authority=authority,
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
                parent_authority=authority,
                thread_id=child_id,
                handle="explorer-0001",
                subagent_type="explorer",
                parent_tool_call_id="call-1",
                isolation="shared",
                write_policy="none",
                brief_description="  find the   secret ",
                parent_iteration=7,
            )
            == created
        )
        duplicate_worker_call = str(uuid4())
        assert (
            await orchestrator.create_subagent_thread(
                parent_job_id=str(job_id),
                parent_authority=authority,
                thread_id=duplicate_worker_call,
                handle="explorer-duplicate",
                subagent_type="explorer",
                parent_tool_call_id="call-1",
            )
            is None
        )
        async with pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM threads WHERE kind='subagent' "
                    "AND parent_job_id=$1 AND parent_tool_call_id=$2",
                    job_id,
                    "call-1",
                )
                == 1
            )
        assert (
            await orchestrator.create_subagent_thread(
                parent_job_id=str(job_id),
                parent_authority=authority,
                thread_id=child_id,
                handle="explorer-0001",
                subagent_type="explorer",
                parent_tool_call_id="call-1",
                run_in_background=True,
                initial_status="queued",
            )
            is None
        ), "a background retry cannot inherit a foreground recovery identity"
        with pytest.raises(ParentExecutionAuthorityRefused):
            await orchestrator.create_subagent_thread(
                parent_job_id=str(uuid4()),
                parent_authority=authority,
                handle="explorer-0002",
                subagent_type="explorer",
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
            "owned_paths": [],
            "brief_description": "find the secret",
            "parent_iteration": 7,
            "fork": False,
            "run_in_background": False,
        }

        # --- the transcript through the agent-side pool -------------------
        saved = await agent.save_subagent_thread_message(
            thread_id=child_id,
            parent_job_id=str(job_id),
            parent_authority=authority,
            runtime_generation=generation,
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
            parent_job_id=str(job_id),
            parent_authority=authority,
            runtime_generation=str(uuid4()),
            status="ended",
            subagent_status="completed",
            ended=True,
        ), "a stale generation cannot win the first terminal write"
        assert await agent.update_subagent_thread(
            child_id,
            parent_job_id=str(job_id),
            parent_authority=authority,
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
            parent_job_id=str(job_id),
            parent_authority=authority,
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
            parent_authority=authority,
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
            parent_job_id=str(job_id),
            parent_authority=authority,
            runtime_generation=generation,
            status="active",
            subagent_status="running",
        )

        # --- a pinned replacement blocks behind the exact parent lock -----
        replacement_generation = str(uuid4())

        async def replace_pinned_process() -> None:
            async with pool.acquire() as replacement_conn:
                await replacement_conn.execute(
                    "UPDATE agents SET metadata = jsonb_set("
                    "COALESCE(metadata, '{}'::jsonb), "
                    "'{dispatch_process_generation}', to_jsonb($2::text), true) "
                    "WHERE id = $1",
                    agent_id,
                    replacement_generation,
                )

        async with pool.acquire() as authority_conn:
            async with authority_conn.transaction():
                await require_parent_execution_authority(
                    authority_conn,
                    authority,
                    parent_job_id=job_id,
                    mutation=True,
                )
                replacement = asyncio.create_task(replace_pinned_process())
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(replacement), timeout=0.1)
            await asyncio.wait_for(replacement, timeout=2)

        # The replaced process cannot read (and therefore cannot leak) the
        # successor child generation or mutate that successor.
        with pytest.raises(ParentExecutionAuthorityRefused):
            await orchestrator.get_subagent_thread(
                str(job_id), child_id, parent_authority=authority
            )
        with pytest.raises(ParentExecutionAuthorityRefused):
            await agent.update_subagent_thread(
                child_id,
                parent_job_id=str(job_id),
                parent_authority=authority,
                runtime_generation=next_generation,
                status="active",
                subagent_status="running",
            )
        async with pool.acquire() as conn:
            still_current = await conn.fetchrow(
                "SELECT status, runtime_generation FROM threads WHERE id=$1",
                child_id,
            )
            assert still_current["status"] == "created"
            assert str(still_current["runtime_generation"]) == next_generation
            await conn.execute(
                "UPDATE agents SET metadata = jsonb_set("
                "COALESCE(metadata, '{}'::jsonb), "
                "'{dispatch_process_generation}', to_jsonb($2::text), true) "
                "WHERE id = $1",
                agent_id,
                process_generation,
            )
            await conn.execute(
                "UPDATE agents SET pod_uid=$2 WHERE id=$1", agent_id, "replacement-pod"
            )
        with pytest.raises(ParentExecutionAuthorityRefused):
            await agent.parent_execution_authority_current(authority)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agents SET pod_uid=$2 WHERE id=$1", agent_id, pod_uid
            )
        assert await agent.parent_execution_authority_current(authority)
        assert await agent.update_subagent_thread(
            child_id,
            parent_job_id=str(job_id),
            parent_authority=authority,
            runtime_generation=next_generation,
            status="active",
            subagent_status="running",
        )

        # --- terminal row + worker delivery are one idempotent operation --
        delivery_id = str(uuid4())
        terminal = await orchestrator.terminalize_subagent_thread_and_enqueue(
            parent_job_id=str(job_id),
            parent_authority=authority,
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
            parent_authority=authority,
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
            parent_authority=authority,
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
            found = await db.get_subagent_thread_by_call(
                str(job_id), "call-1", parent_authority=authority
            )
            assert found is not None and str(found["id"]) == child_id
            assert (
                await db.get_subagent_thread_by_call(
                    str(job_id), "call-9", parent_authority=authority
                )
                is None
            )
        sessions = await orchestrator.list_threads(user_id=str(owner))
        assert {str(t["id"]) for t in sessions} == {
            str(session_id),
            str(stateless_session_id),
            str(event_parent_id),
            str(finalized_parent_id),
        }
        assert session_child_id not in {str(t["id"]) for t in sessions}
        assert foreground_child_id not in {str(t["id"]) for t in sessions}
        assert {
            str(thread["id"])
            for thread in await orchestrator.list_threads(status="ended")
        } == {str(stateless_session_id)}

        # --- real stateless lease steal: queue -> job -> child ------------
        stateless_job_id = uuid4()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO jobs "
                    "(id, description, user_id, status, execution_lane) "
                    "VALUES ($1, 'stateless parent', $2, 'created', 'stateless')",
                    stateless_job_id,
                    owner,
                )
                await enqueue_worker_batch(
                    conn, job_id=stateless_job_id, fair_key="authority-test"
                )
        stateless_claim = await claim_worker_batch(
            pool, pod_name="authority-worker-a", affinity_grace_seconds=0
        )
        assert stateless_claim is not None
        assert stateless_claim.unit_id == stateless_job_id
        stateless_authority = ParentExecutionAuthority(
            execution_lane="stateless",
            parent_job_id=stateless_job_id,
            worker_lease_token=stateless_claim.lease_token,
        )
        stateless_child_id = str(uuid4())
        stateless_created = await orchestrator.create_subagent_thread(
            parent_job_id=str(stateless_job_id),
            parent_authority=stateless_authority,
            thread_id=stateless_child_id,
            handle="probe-0001",
            subagent_type="probe",
            parent_tool_call_id="stateless-call",
            initial_status="queued",
            run_in_background=True,
        )
        assert stateless_created is not None
        stateless_generation = stateless_created["runtime_generation"]
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE run_queue SET leased_until=now()-interval '1 minute' "
                "WHERE unit_id=$1",
                stateless_job_id,
            )

        async def steal_expired_worker():
            async with pool.acquire() as steal_conn:
                return await reap_expired(
                    steal_conn,
                    unit_kind=UNIT_KIND_WORKER_BATCH,
                    grace_seconds=0,
                    backoff_base_seconds=0,
                    jitter=0,
                )

        async with pool.acquire() as authority_conn:
            async with authority_conn.transaction():
                await require_parent_execution_authority(
                    authority_conn,
                    stateless_authority,
                    parent_job_id=stateless_job_id,
                    mutation=True,
                )
                steal = asyncio.create_task(steal_expired_worker())
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(steal), timeout=0.1)
            stolen = await asyncio.wait_for(steal, timeout=2)
        assert len(stolen) == 1
        assert stolen[0].previous_lease_token == stateless_claim.lease_token
        with pytest.raises(ParentExecutionAuthorityRefused):
            await orchestrator.get_subagent_thread(
                str(stateless_job_id),
                stateless_child_id,
                parent_authority=stateless_authority,
            )
        with pytest.raises(ParentExecutionAuthorityRefused):
            await agent.update_subagent_thread(
                stateless_child_id,
                parent_job_id=str(stateless_job_id),
                parent_authority=stateless_authority,
                runtime_generation=stateless_generation,
                status="active",
                subagent_status="running",
            )
        async with pool.acquire() as conn:
            stateless_row = await conn.fetchrow(
                "SELECT status, subagent_status, runtime_generation "
                "FROM threads WHERE id=$1",
                stateless_child_id,
            )
        assert stateless_row["status"] == "created"
        assert stateless_row["subagent_status"] == "queued"
        assert str(stateless_row["runtime_generation"]) == stateless_generation

        successor_claim = await claim_worker_batch(
            pool, pod_name="authority-worker-b", affinity_grace_seconds=0
        )
        assert successor_claim is not None
        assert successor_claim.unit_id == stateless_job_id
        successor_authority = ParentExecutionAuthority(
            execution_lane="stateless",
            parent_job_id=stateless_job_id,
            worker_lease_token=successor_claim.lease_token,
        )
        assert await agent.update_subagent_thread(
            stateless_child_id,
            parent_job_id=str(stateless_job_id),
            parent_authority=successor_authority,
            runtime_generation=stateless_generation,
            status="active",
            subagent_status="running",
        )

        # --- the cascade, and the live-child trap -------------------------
        live_id = str(uuid4())
        live_created = await orchestrator.create_subagent_thread(
            parent_job_id=str(job_id),
            parent_authority=authority,
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
                    "SELECT count(*) FROM threads "
                    "WHERE kind = 'subagent' AND parent_job_id=$1",
                    job_id,
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
