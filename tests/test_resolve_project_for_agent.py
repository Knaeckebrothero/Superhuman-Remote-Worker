"""Tests for orchestrator/database/postgres.py resolve_project_for_agent.

Mocks the asyncpg connection pool to test method logic without a live DB
(same pattern as tests/test_thread_db.py, tests/test_cloud_ro_mounts_db.py).

Added after a review found the thread branch queried threads.project_id
directly. That column is written only at thread creation
(postgres.py::create_thread, "INSERT INTO threads (... project_id ...)");
thread_mounts (mount_kind='project') is the actual source of truth for a
session's project attachment (migration 0013_thread_mounts.sql, and
postgres.py's own comment: "Replaces the legacy threads.metadata.project_ids
... as source of truth"). A thread that gained its project via a mount
(the normal path — see orchestrator/main.py's GET .../threads/{id},
project_ids derived from mounts) would resolve NULL from the column alone,
so the internal contacts endpoint would silently return an empty list
forever instead of erroring — the worst failure shape.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from orchestrator.database.postgres import PostgresDB

JOB_ID = "11111111-1111-1111-1111-111111111111"
THREAD_ID = "22222222-2222-2222-2222-222222222222"
PROJECT_ID = "33333333-3333-3333-3333-333333333333"


def _make_db_with_conn(mock_conn):
    """Create a PostgresDB instance with acquire() yielding mock_conn."""
    db = PostgresDB.__new__(PostgresDB)
    db._pool = MagicMock()
    db._connection_string = "test"
    db._queries = {}

    @asynccontextmanager
    async def mock_acquire():
        yield mock_conn

    db.acquire = mock_acquire
    return db


def _mock_conn():
    return AsyncMock()


class TestResolveProjectForAgent:
    """Tests for resolve_project_for_agent (contacts_registry.md agent surface)."""

    @pytest.mark.asyncio
    async def test_job_branch_queries_jobs_table(self):
        conn = _mock_conn()
        conn.fetchval = AsyncMock(return_value=UUID(PROJECT_ID))
        db = _make_db_with_conn(conn)

        result = await db.resolve_project_for_agent(job_id=JOB_ID)

        assert result == PROJECT_ID
        conn.fetchval.assert_awaited_once()
        sql = " ".join(conn.fetchval.call_args[0][0].split())
        assert "FROM jobs" in sql
        assert conn.fetchval.call_args[0][1] == UUID(JOB_ID)

    @pytest.mark.asyncio
    async def test_thread_resolved_via_mount(self):
        """thread_mounts is the source of truth and must be tried FIRST —
        before the legacy threads.project_id column."""
        conn = _mock_conn()
        conn.fetchval = AsyncMock(return_value=UUID(PROJECT_ID))
        db = _make_db_with_conn(conn)

        result = await db.resolve_project_for_agent(thread_id=THREAD_ID)

        assert result == PROJECT_ID
        # The mount query alone resolved it — no fallback query needed.
        conn.fetchval.assert_awaited_once()
        sql = " ".join(conn.fetchval.call_args[0][0].split())
        assert "thread_mounts" in sql
        assert "mount_kind = 'project'" in sql
        assert conn.fetchval.call_args[0][1] == UUID(THREAD_ID)

    @pytest.mark.asyncio
    async def test_thread_falls_back_to_column_when_unmounted(self):
        """A thread with project_id set directly and no mount row (legacy /
        edge case) must still resolve via the column fallback."""
        conn = _mock_conn()
        conn.fetchval = AsyncMock(side_effect=[None, UUID(PROJECT_ID)])
        db = _make_db_with_conn(conn)

        result = await db.resolve_project_for_agent(thread_id=THREAD_ID)

        assert result == PROJECT_ID
        assert conn.fetchval.await_count == 2
        first_sql = " ".join(conn.fetchval.call_args_list[0][0][0].split())
        second_sql = " ".join(conn.fetchval.call_args_list[1][0][0].split())
        assert "thread_mounts" in first_sql
        assert "FROM threads" in second_sql

    @pytest.mark.asyncio
    async def test_thread_with_no_project_anywhere_returns_none(self):
        """Neither a mount nor the column resolves — a genuinely
        project-less thread, not an error."""
        conn = _mock_conn()
        conn.fetchval = AsyncMock(side_effect=[None, None])
        db = _make_db_with_conn(conn)

        result = await db.resolve_project_for_agent(thread_id=THREAD_ID)

        assert result is None
        assert conn.fetchval.await_count == 2

    @pytest.mark.asyncio
    async def test_malformed_job_id_returns_none(self):
        conn = _mock_conn()
        db = _make_db_with_conn(conn)

        result = await db.resolve_project_for_agent(job_id="not-a-uuid")

        assert result is None
        conn.fetchval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_thread_id_returns_none(self):
        conn = _mock_conn()
        db = _make_db_with_conn(conn)

        result = await db.resolve_project_for_agent(thread_id="not-a-uuid")

        assert result is None
        conn.fetchval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_neither_id_returns_none(self):
        conn = _mock_conn()
        db = _make_db_with_conn(conn)

        result = await db.resolve_project_for_agent()

        assert result is None
        conn.fetchval.assert_not_awaited()
