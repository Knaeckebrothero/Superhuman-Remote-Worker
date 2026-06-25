"""Unit tests for checkpoint retention (D3).

``delete_checkpoint_thread`` prunes the 3 LangGraph tables for a terminal job,
gated on ``CHECKPOINTER_BACKEND=postgres``; ``update_job_status`` triggers it on
terminal status only. Mocked connection (no real DB), mirroring
tests/test_admin_providers_db.py. See
docs/issues/cross_pod_resume_cold_starts_checkpoint_not_replicated.md.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.database.postgres import PostgresDB

_JOB_ID = "11111111-1111-1111-1111-111111111111"


def _make_db(mock_conn):
    db = PostgresDB.__new__(PostgresDB)
    db._pool = MagicMock()
    db._connection_string = "test"
    db._queries = {}

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    db.acquire = _acquire
    return db


class TestDeleteCheckpointThread:
    @pytest.mark.asyncio
    async def test_noop_when_backend_not_postgres(self, monkeypatch):
        monkeypatch.delenv("CHECKPOINTER_BACKEND", raising=False)  # default sqlite
        conn = AsyncMock()
        db = _make_db(conn)

        assert await db.delete_checkpoint_thread("job-1") == 0
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deletes_three_tables_when_postgres(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="DELETE 2")
        db = _make_db(conn)

        deleted = await db.delete_checkpoint_thread("job-1")

        assert deleted == 6  # 3 tables × 2 rows
        assert conn.execute.await_count == 3
        tables = {
            call.args[0].split("FROM ")[1].split()[0]
            for call in conn.execute.await_args_list
        }
        assert tables == {"checkpoint_writes", "checkpoint_blobs", "checkpoints"}
        for call in conn.execute.await_args_list:
            assert call.args[1] == "job-1"  # thread_id bound as $1

    @pytest.mark.asyncio
    async def test_missing_table_is_nonfatal(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=Exception("relation does not exist"))
        db = _make_db(conn)

        assert await db.delete_checkpoint_thread("job-1") == 0  # swallowed


class TestUpdateJobStatusPrunes:
    @pytest.mark.asyncio
    async def test_terminal_status_triggers_prune(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
        conn = AsyncMock()
        # first execute = the UPDATE, next three = the checkpoint DELETEs
        conn.execute = AsyncMock(
            side_effect=["UPDATE 1", "DELETE 0", "DELETE 0", "DELETE 0"]
        )
        db = _make_db(conn)

        ok = await db.update_job_status(_JOB_ID, status="completed")

        assert ok is True
        assert conn.execute.await_count == 4  # UPDATE + 3 DELETEs
        assert conn.execute.await_args_list[0].args[0].startswith("UPDATE jobs")

    @pytest.mark.asyncio
    async def test_non_terminal_status_does_not_prune(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        db = _make_db(conn)

        ok = await db.update_job_status(_JOB_ID, status="paused")

        assert ok is True
        assert conn.execute.await_count == 1  # only the UPDATE — no prune
