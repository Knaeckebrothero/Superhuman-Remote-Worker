"""Tests for orchestrator/database/postgres.py heartbeat handler.

Phase 0 stopgap: an orchestrator-set 'draining' status must survive an
agent-reported status on the next heartbeat. Phase 1 replaces this with
a separate intent column, but in the meantime this guard plus the
'drained' reaper category are what make `_drain_stale_image_agents`
actually delete pods rather than flicker a status field nothing reads.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.database.postgres import PostgresDB


def _make_db_with_mocked_acquire(prev_status: str, update_result: str = "UPDATE 1"):
    """Build a real PostgresDB with acquire() replaced by a mocked conn.

    The replacement uses an `asynccontextmanager` so the production
    `async with db.acquire() as conn:` shape still works.
    """
    with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}):
        db = PostgresDB()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"status": prev_status})
    conn.execute = AsyncMock(return_value=update_result)

    @asynccontextmanager
    async def fake_acquire():
        yield conn

    db.acquire = fake_acquire
    return db, conn


class TestHeartbeatPreservesDraining:
    """Heartbeat must not let an agent overwrite an orchestrator-set drain."""

    @pytest.mark.asyncio
    async def test_draining_preserved_when_agent_reports_ready(self):
        db, _ = _make_db_with_mocked_acquire(prev_status="draining")
        result = await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
        )
        assert result is not None
        assert result["previous_status"] == "draining"
        assert result["effective_status"] == "draining"

    @pytest.mark.asyncio
    async def test_draining_preserved_when_agent_reports_working(self):
        # Future-proof: even if a working agent's drain bit gets set, the
        # next heartbeat (still reporting working) must not flip it back.
        db, _ = _make_db_with_mocked_acquire(prev_status="draining")
        result = await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="working",
            current_job_id="11111111-1111-1111-1111-111111111111",
        )
        assert result["effective_status"] == "draining"

    @pytest.mark.asyncio
    async def test_normal_transition_unaffected(self):
        db, _ = _make_db_with_mocked_acquire(prev_status="working")
        result = await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
        )
        assert result["previous_status"] == "working"
        assert result["effective_status"] == "ready"

    @pytest.mark.asyncio
    async def test_sql_uses_case_expression(self):
        # Belt-and-braces: assert the CASE is in the issued SQL so a
        # future refactor that drops it triggers a test failure rather
        # than a silent regression.
        db, conn = _make_db_with_mocked_acquire(prev_status="working")
        await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
        )
        sql = conn.execute.call_args[0][0]
        assert "CASE WHEN status = 'draining'" in sql

    @pytest.mark.asyncio
    async def test_returns_none_when_agent_missing(self):
        db, conn = _make_db_with_mocked_acquire(prev_status="ready")
        conn.fetchrow.return_value = None
        result = await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
        )
        assert result is None
