"""The subagent thread accessors on BOTH pools (U3 WP3, plan B.1 / D).

Strict asyncpg seams in the ``test_save_thread_message_columns.py`` idiom:
``PostgresDB.__new__`` + a mocked ``acquire``, asserting the SQL columns and
the ``$n`` argument order — because a column the SQL names but the arguments
do not carry (or vice versa) is exactly what a fake pool never notices.

Orchestrator side: ``create_subagent_thread`` (the row derived from the
job), ``list_subagent_threads``, ``get_subagent_thread_by_call``, the
always-on ``kind = 'session'`` gate on ``list_threads``, and ``delete_job``
ending live children before the cascade. Agent side:
``update_subagent_thread`` (the ``kind = 'subagent'`` guard) and
``get_subagent_thread_by_call``.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from orchestrator.database.postgres import PostgresDB as OrchestratorDB
from src.database.postgres_db import PostgresDB as AgentDB

JOB = UUID("aaaaaaaa-1111-4222-8333-444444444444")
CHILD = UUID("bbbbbbbb-1111-4222-8333-444444444444")
PARENT_THREAD = UUID("cccccccc-1111-4222-8333-444444444444")


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def _conn() -> AsyncMock:
    conn = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__ = AsyncMock(return_value=transaction)
    transaction.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=transaction)
    return conn


def _orchestrator_db(conn) -> OrchestratorDB:
    db = OrchestratorDB.__new__(OrchestratorDB)
    db._pool = MagicMock()
    db._connection_string = "test"
    db._queries = {}

    @asynccontextmanager
    async def acquire():
        yield conn

    db.acquire = acquire
    return db


def _agent_db(conn) -> AgentDB:
    db = AgentDB.__new__(AgentDB)
    db._pool = MagicMock()
    db._queries = {}

    @asynccontextmanager
    async def acquire():
        yield conn

    db.acquire = acquire
    return db


# ---------------------------------------------------------------------------
# Orchestrator: create_subagent_thread
# ---------------------------------------------------------------------------


class TestCreateSubagentThread:
    @pytest.mark.asyncio
    async def test_the_row_is_derived_from_the_job_in_one_insert(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(return_value={"id": CHILD})
        db = _orchestrator_db(conn)

        created = await db.create_subagent_thread(
            parent_job_id=str(JOB),
            thread_id=str(CHILD),
            handle="implementer-7f3a",
            subagent_type="implementer",
            parent_tool_call_id="call-1",
            parent_thread_id=str(PARENT_THREAD),
            isolation="worktree",
            write_policy="owned_paths",
            brief_description="implement   the parser",
            parent_iteration=12,
            fork=True,
        )

        assert created == str(CHILD)
        sql = _compact(conn.fetchrow.call_args[0][0])
        args = conn.fetchrow.call_args[0][1:]
        assert sql.startswith("INSERT INTO threads (")
        # The column list and the SELECT list must line up: every identity
        # fact the row needs, none from the caller that the job owns.
        assert (
            "INSERT INTO threads ( id, kind, user_id, project_id, title, status, "
            "permission_mode, narration_mode, execution_lane, metadata, "
            "parent_job_id, parent_thread_id, parent_tool_call_id, "
            "subagent_handle, subagent_type, subagent_status )" in sql
        )
        assert (
            "SELECT $1, 'subagent', j.user_id, j.project_id, $3, 'active', "
            "'autonomous', 'silent', 'pinned', $4::jsonb, j.id, $5, $6, $7, $8, "
            "'running' FROM jobs j WHERE j.id = $2" in sql
        )
        assert "ON CONFLICT (id) DO NOTHING RETURNING id" in sql
        assert args[0] == CHILD and args[1] == JOB
        assert args[2] == "implementer-7f3a: implement the parser"
        metadata = json.loads(args[3])
        assert metadata["datasource_ids"] == []
        assert metadata["subagent"] == {
            "type": "implementer",
            "handle": "implementer-7f3a",
            "isolation": "worktree",
            "write_policy": "owned_paths",
            "brief_description": "implement the parser",
            "parent_iteration": 12,
            "fork": True,
        }
        assert args[4] == PARENT_THREAD
        assert args[5] == "call-1"
        assert args[6] == "implementer-7f3a"
        assert args[7] == "implementer"
        conn.fetchval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_fresh_id_is_minted_when_none_is_given(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(side_effect=lambda sql, *a: {"id": a[0]})
        db = _orchestrator_db(conn)
        created = await db.create_subagent_thread(
            parent_job_id=str(JOB), handle="h-0001", subagent_type="explorer"
        )
        assert UUID(created)  # a valid uuid the caller did not choose
        args = conn.fetchrow.call_args[0][1:]
        assert args[2] == "subagent h-0001"  # no brief → the handle titles it
        assert args[4] is None and args[5] is None

    @pytest.mark.asyncio
    async def test_a_missing_job_yields_none_after_one_probe(self):
        """No row back means either no job or an already-created id — the
        follow-up SELECT tells them apart."""
        conn = _conn()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=None)
        db = _orchestrator_db(conn)
        assert (
            await db.create_subagent_thread(
                parent_job_id=str(JOB),
                thread_id=str(CHILD),
                handle="h-0001",
                subagent_type="explorer",
            )
            is None
        )
        probe = _compact(conn.fetchval.call_args[0][0])
        assert probe == "SELECT id FROM threads WHERE id = $1 AND kind = 'subagent'"
        assert conn.fetchval.call_args[0][1] == CHILD

    @pytest.mark.asyncio
    async def test_a_retried_create_returns_the_existing_id(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetchval = AsyncMock(return_value=CHILD)
        db = _orchestrator_db(conn)
        assert await db.create_subagent_thread(
            parent_job_id=str(JOB),
            thread_id=str(CHILD),
            handle="h-0001",
            subagent_type="explorer",
        ) == str(CHILD)

    @pytest.mark.asyncio
    async def test_malformed_ids_never_reach_sql(self):
        conn = _conn()
        db = _orchestrator_db(conn)
        assert (
            await db.create_subagent_thread(
                parent_job_id="not-a-uuid", handle="h", subagent_type="explorer"
            )
            is None
        )
        assert (
            await db.create_subagent_thread(
                parent_job_id=str(JOB),
                thread_id="nope",
                handle="h",
                subagent_type="explorer",
            )
            is None
        )
        assert (
            await db.create_subagent_thread(
                parent_job_id=str(JOB),
                parent_thread_id="nope",
                handle="h",
                subagent_type="explorer",
            )
            is None
        )
        conn.fetchrow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_handle_and_a_type_are_required(self):
        db = _orchestrator_db(_conn())
        with pytest.raises(ValueError):
            await db.create_subagent_thread(
                parent_job_id=str(JOB), handle="", subagent_type="explorer"
            )
        with pytest.raises(ValueError):
            await db.create_subagent_thread(
                parent_job_id=str(JOB), handle="h-0001", subagent_type="  "
            )


# ---------------------------------------------------------------------------
# Orchestrator: the reads
# ---------------------------------------------------------------------------


class TestOrchestratorReads:
    @pytest.mark.asyncio
    async def test_list_subagent_threads_walks_the_job_in_spawn_order(self):
        conn = _conn()
        conn.fetch = AsyncMock(return_value=[{"id": CHILD, "subagent_handle": "h"}])
        db = _orchestrator_db(conn)
        rows = await db.list_subagent_threads(str(JOB))
        assert rows == [{"id": CHILD, "subagent_handle": "h"}]
        sql = _compact(conn.fetch.call_args[0][0])
        assert "FROM threads WHERE kind = 'subagent' AND parent_job_id = $1" in sql
        assert "ORDER BY created_at, id" in sql
        for column in (
            "subagent_handle",
            "subagent_type",
            "subagent_status",
            "subagent_outcome",
            "subagent_error",
            "report_path",
            "parent_tool_call_id",
            "total_turns",
            "total_tokens",
            "metadata",
            "created_at",
            "ended_at",
        ):
            assert column in sql, column
        assert conn.fetch.call_args[0][1] == JOB

    @pytest.mark.asyncio
    async def test_list_subagent_threads_refuses_a_bad_id_without_sql(self):
        conn = _conn()
        db = _orchestrator_db(conn)
        assert await db.list_subagent_threads("job-42") == []
        conn.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_by_call_is_keyed_on_job_and_call_newest_first(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(return_value={"id": CHILD})
        db = _orchestrator_db(conn)
        assert await db.get_subagent_thread_by_call(str(JOB), " call-1 ") == {
            "id": CHILD
        }
        sql = _compact(conn.fetchrow.call_args[0][0])
        assert (
            "WHERE kind = 'subagent' AND parent_job_id = $1 AND "
            "parent_tool_call_id = $2 ORDER BY created_at DESC, id DESC LIMIT 1" in sql
        )
        assert conn.fetchrow.call_args[0][1:] == (JOB, "call-1")

    @pytest.mark.asyncio
    async def test_get_by_call_needs_both_keys(self):
        conn = _conn()
        db = _orchestrator_db(conn)
        assert await db.get_subagent_thread_by_call(str(JOB), "") is None
        assert await db.get_subagent_thread_by_call("nope", "call-1") is None
        conn.fetchrow.assert_not_awaited()


class TestListThreadsSessionGate:
    """``list_threads`` is the sessions page's query; child rows must never
    reach it, whatever the caller filters on."""

    @pytest.mark.asyncio
    async def test_the_kind_gate_is_the_only_condition_without_filters(self):
        conn = _conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _orchestrator_db(conn)
        await db.list_threads()
        sql = _compact(conn.fetch.call_args[0][0])
        assert "FROM threads WHERE kind = 'session' ORDER BY created_at DESC" in sql
        assert conn.fetch.call_args[0][1:] == ()

    @pytest.mark.asyncio
    async def test_the_kind_gate_precedes_every_caller_filter(self):
        conn = _conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _orchestrator_db(conn)
        await db.list_threads(user_id="u1", project_id="p1", status="active")
        sql = _compact(conn.fetch.call_args[0][0])
        assert (
            "WHERE kind = 'session' AND (user_id = $1 OR user_id IS NULL) "
            "AND project_id = $2 AND status = $3" in sql
        )
        assert conn.fetch.call_args[0][1:] == ("u1", "p1", "active")


class TestDeleteJobEndsLiveChildren:
    @pytest.mark.asyncio
    async def test_children_are_ended_in_the_transaction_before_the_delete(self):
        """The cascade fires the pinned delete authority on every child row,
        and only an ended, authority-free row may go — so a child a hard kill
        left 'active' would otherwise abort the whole delete."""
        conn = _conn()
        executed: list[str] = []

        async def execute(sql, *args):
            text = _compact(sql)
            executed.append(text)
            if text.startswith("DELETE FROM jobs"):
                return "DELETE 1"
            return "UPDATE 0"

        conn.execute = AsyncMock(side_effect=execute)
        conn.fetchrow = AsyncMock(
            return_value={"status": "processing", "completion_outcome_kind": None}
        )
        conn.fetchval = AsyncMock(return_value=None)
        db = _orchestrator_db(conn)

        assert await db.delete_job(str(JOB)) is True

        ending = [s for s in executed if s.startswith("UPDATE threads")]
        assert len(ending) == 1
        (end_children,) = ending
        assert "SET status = 'ended'" in end_children
        assert "ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)" in end_children
        assert "ELSE 'cancelled'" in end_children
        assert (
            "COALESCE( subagent_outcome, 'cancelled:parent_deleted' )" in end_children
        )
        assert (
            "WHERE kind = 'subagent' AND parent_job_id = $1 AND status <> 'ended'"
            in end_children
        )
        assert executed.index(end_children) < executed.index(
            "DELETE FROM jobs WHERE id = $1"
        )
        update_call = next(
            c
            for c in conn.execute.await_args_list
            if _compact(c.args[0]).startswith("UPDATE threads")
        )
        assert update_call.args[1] == JOB


# ---------------------------------------------------------------------------
# Agent side
# ---------------------------------------------------------------------------


class TestUpdateSubagentThread:
    @pytest.mark.asyncio
    async def test_every_field_lands_on_its_column_under_the_kind_guard(self):
        conn = _conn()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        db = _agent_db(conn)

        assert await db.update_subagent_thread(
            str(CHILD),
            status="ended",
            subagent_status="capped",
            outcome="capped:turns",
            turns=40,
            tokens=200_000,
            report_path=".subagents/explorer-0001/report.md",
            error=None,
            ended=True,
        )
        sql = _compact(conn.execute.call_args[0][0])
        args = conn.execute.call_args[0][1:]
        assert sql.startswith("UPDATE threads SET")
        assert "status = COALESCE($2::text, status)" in sql
        assert "subagent_status = COALESCE($3::text, subagent_status)" in sql
        assert "subagent_outcome = COALESCE($4::text, subagent_outcome)" in sql
        assert "total_turns = COALESCE($5::integer, total_turns)" in sql
        assert "total_tokens = COALESCE($6::integer, total_tokens)" in sql
        assert "report_path = COALESCE($7::text, report_path)" in sql
        assert "subagent_error = COALESCE($8::text, subagent_error)" in sql
        assert (
            "ended_at = CASE WHEN $9::boolean THEN COALESCE(ended_at, "
            "CURRENT_TIMESTAMP) ELSE ended_at END" in sql
        )
        assert "last_activity = CURRENT_TIMESTAMP" in sql
        assert sql.endswith("WHERE id = $1::uuid AND kind = 'subagent'")
        assert args == (
            str(CHILD),
            "ended",
            "capped",
            "capped:turns",
            40,
            200_000,
            ".subagents/explorer-0001/report.md",
            None,
            True,
        )

    @pytest.mark.asyncio
    async def test_omitted_fields_are_none_so_coalesce_keeps_the_column(self):
        conn = _conn()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        db = _agent_db(conn)
        await db.update_subagent_thread(str(CHILD), status="active")
        args = conn.execute.call_args[0][1:]
        assert args == (str(CHILD), "active", None, None, None, None, None, None, False)

    @pytest.mark.asyncio
    async def test_a_session_row_or_unknown_id_reports_false(self):
        conn = _conn()
        conn.execute = AsyncMock(return_value="UPDATE 0")
        db = _agent_db(conn)
        assert not await db.update_subagent_thread(str(CHILD), status="ended")


class TestAgentGetByCall:
    @pytest.mark.asyncio
    async def test_the_lookup_matches_the_orchestrators(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(
            return_value={"id": CHILD, "subagent_status": "completed"}
        )
        db = _agent_db(conn)
        row = await db.get_subagent_thread_by_call(str(JOB), "call-1")
        assert row == {"id": CHILD, "subagent_status": "completed"}
        sql = _compact(conn.fetchrow.call_args[0][0])
        assert (
            "FROM threads WHERE kind = 'subagent' AND parent_job_id = $1::uuid AND "
            "parent_tool_call_id = $2 ORDER BY created_at DESC, id DESC LIMIT 1" in sql
        )
        for column in (
            "subagent_handle",
            "subagent_type",
            "subagent_status",
            "subagent_outcome",
            "subagent_error",
            "report_path",
            "total_turns",
            "total_tokens",
            "created_at",
            "ended_at",
        ):
            assert column in sql, column
        assert conn.fetchrow.call_args[0][1:] == (str(JOB), "call-1")

    @pytest.mark.asyncio
    async def test_an_empty_key_is_none_without_sql(self):
        conn = _conn()
        db = _agent_db(conn)
        assert await db.get_subagent_thread_by_call(str(JOB), "   ") is None
        assert await db.get_subagent_thread_by_call("", "call-1") is None
        # A non-uuid parent (a test host's "parent-job") is "no row", never a
        # DataError out of the uuid bind — the orchestrator's guard, mirrored.
        assert await db.get_subagent_thread_by_call("parent-job", "call-1") is None
        conn.fetchrow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_row_is_none(self):
        conn = _conn()
        conn.fetchrow = AsyncMock(return_value=None)
        db = _agent_db(conn)
        assert await db.get_subagent_thread_by_call(str(JOB), "call-1") is None


def test_both_pools_read_the_same_columns():
    """A replayed envelope (agent pool) and a cockpit row (orchestrator pool)
    are built from the same facts."""
    assert AgentDB._SUBAGENT_THREAD_COLUMNS == OrchestratorDB._SUBAGENT_THREAD_COLUMNS
    assert uuid4()  # keep the import honest for the fixtures above
