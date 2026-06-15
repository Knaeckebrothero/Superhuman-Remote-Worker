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


def _make_db_with_mocked_acquire(
    prev_status: str,
    update_result: str = "UPDATE 1",
    intents: dict | None = None,
):
    """Build a real PostgresDB with acquire() replaced by a mocked conn.

    The replacement uses an `asynccontextmanager` so the production
    `async with db.acquire() as conn:` shape still works.
    """
    with patch.dict("os.environ", {"DATABASE_URL": "postgresql://test"}):
        db = PostgresDB()
    conn = AsyncMock()
    row = {"status": prev_status, "intents": intents if intents is not None else {}}
    conn.fetchrow = AsyncMock(return_value=row)
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
        assert "WHEN status = 'draining'" in sql

    @pytest.mark.asyncio
    async def test_returns_none_when_agent_missing(self):
        db, conn = _make_db_with_mocked_acquire(prev_status="ready")
        conn.fetchrow.return_value = None
        result = await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
        )
        assert result is None


class TestHeartbeatPreservesOffline:
    """Heartbeat must not let a late grace-period heartbeat resurrect an
    agent that the lifecycle reconciler has already marked offline. See
    the 2026-05-24 stale-pod regression (thread 352144ea): the dying
    pod's last in-flight heartbeat flipped its row back to ready,
    ``_find_idle_persistent_agent`` matched it (most-recent
    ``last_heartbeat`` puts it at the top of the pool query), and the
    new thread got bound to a pod that disappeared seconds later.
    """

    @pytest.mark.asyncio
    async def test_offline_preserved_when_agent_reports_ready(self):
        db, _ = _make_db_with_mocked_acquire(prev_status="offline")
        result = await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
        )
        assert result is not None
        assert result["previous_status"] == "offline"
        assert result["effective_status"] == "offline"

    @pytest.mark.asyncio
    async def test_offline_preserved_when_agent_reports_working(self):
        db, _ = _make_db_with_mocked_acquire(prev_status="offline")
        result = await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="working",
            current_job_id="11111111-1111-1111-1111-111111111111",
        )
        assert result["effective_status"] == "offline"

    @pytest.mark.asyncio
    async def test_offline_preserved_when_metrics_present(self):
        # Same guard must apply to the metrics-bearing branch — both SQL
        # paths share the CASE expression that pins offline.
        db, _ = _make_db_with_mocked_acquire(prev_status="offline")
        result = await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
            metrics={"cpu": 0.1},
        )
        assert result["effective_status"] == "offline"

    @pytest.mark.asyncio
    async def test_sql_pins_offline_in_case_expression(self):
        # Belt-and-braces: the issued SQL must include the offline branch
        # of the CASE, so a future refactor that drops it triggers a
        # test failure rather than a silent regression.
        db, conn = _make_db_with_mocked_acquire(prev_status="offline")
        await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
        )
        sql = conn.execute.call_args[0][0]
        assert "WHEN status = 'offline'" in sql


class TestHeartbeatAuxDegraded:
    """aux Phase 2: heartbeat persists the auxiliary-model degraded flag from
    the AuxHealth summary the agent carries in metrics.aux. See
    docs/issues/surface_silent_aux_failures.md.
    """

    @pytest.mark.asyncio
    async def test_true_sets_column(self):
        db, conn = _make_db_with_mocked_acquire(prev_status="ready")
        await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
            aux_degraded=True,
        )
        sql, *params = conn.execute.call_args[0]
        assert "aux_degraded = " in sql
        assert any(p is True for p in params)

    @pytest.mark.asyncio
    async def test_false_clears_column(self):
        # A recovered agent reports degraded=False — the column must be
        # actively set false, not left stale at a previous true.
        db, conn = _make_db_with_mocked_acquire(prev_status="ready")
        await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
            aux_degraded=False,
        )
        sql, *params = conn.execute.call_args[0]
        assert "aux_degraded = " in sql
        assert any(p is False for p in params)

    @pytest.mark.asyncio
    async def test_none_omits_column(self):
        # Older agent builds / not-yet-wired aux LLM omit the flag — the
        # persisted value must be left untouched (no aux_degraded clause).
        db, conn = _make_db_with_mocked_acquire(prev_status="ready")
        await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
            aux_degraded=None,
        )
        sql = conn.execute.call_args[0][0]
        assert "aux_degraded" not in sql

    @pytest.mark.asyncio
    async def test_param_indices_aligned_with_all_clauses_present(self):
        # The dynamic param builder must keep $N indices contiguous and
        # matched to positional args when the metadata merge, the
        # working→ready last_completed_at clause, and aux_degraded all fire.
        # This is the index-shuffling hazard the builder replaced.
        import re

        db, conn = _make_db_with_mocked_acquire(prev_status="working")
        await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
            current_job_id="11111111-1111-1111-1111-111111111111",
            metrics={"aux": {"degraded": True}},
            aux_degraded=True,
        )
        sql, *params = conn.execute.call_args[0]
        placeholders = {int(m) for m in re.findall(r"\$(\d+)", sql)}
        assert placeholders == set(range(1, len(params) + 1))
        assert "metadata = metadata ||" in sql
        assert "last_completed_at = CURRENT_TIMESTAMP" in sql
        assert "aux_degraded = " in sql


class TestHeartbeatReturnsIntents:
    """Phase 1c: heartbeat result includes orchestrator-set intents so
    the FastAPI handler can surface them to the agent in the response."""

    @pytest.mark.asyncio
    async def test_intents_propagate_when_present(self):
        db, _ = _make_db_with_mocked_acquire(
            prev_status="ready",
            intents={"should_drain": True, "drain_reason": "stale_image"},
        )
        result = await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
        )
        assert result["intents"] == {
            "should_drain": True,
            "drain_reason": "stale_image",
        }

    @pytest.mark.asyncio
    async def test_intents_empty_when_unset(self):
        db, _ = _make_db_with_mocked_acquire(prev_status="ready", intents={})
        result = await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
        )
        assert result["intents"] == {}

    @pytest.mark.asyncio
    async def test_intents_parsed_when_db_returns_jsonb_string(self):
        # Some asyncpg paths surface JSONB columns as raw strings.
        db, _ = _make_db_with_mocked_acquire(prev_status="ready")
        # Override the fetchrow to return the string-encoded variant.
        import json

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "status": "ready",
                "intents": json.dumps({"should_drain": True}),
            }
        )
        conn.execute = AsyncMock(return_value="UPDATE 1")

        @asynccontextmanager
        async def fake_acquire():
            yield conn

        db.acquire = fake_acquire
        result = await db.heartbeat(
            agent_id="00000000-0000-0000-0000-000000000001",
            status="ready",
        )
        assert result["intents"] == {"should_drain": True}
