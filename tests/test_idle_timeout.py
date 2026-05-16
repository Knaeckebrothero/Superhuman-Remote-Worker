"""Tests for idle session timeout + session status lifecycle.

Covers:
  - IdleTimeoutError exception in persistent_graph.py
  - Idle timeout in get_user_input (asyncio.wait_for)
  - _handle_idle_archive (memory extraction, title, status, git)
  - _update_thread_status helper (REST-first, DB fallback)
  - Thread status transitions (active on startup, ended on shutdown)
  - mark_orphaned_threads_ended (postgres.py)
  - update_thread_status REST client (orchestrator_client.py)
  - PUT /api/agents/threads/{id}/status endpoint
  - Stale detector orphaned thread sweep
  - DELETE /api/persistent/threads/{id}?permanent=true
  - delete_thread (postgres.py)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.persistent_graph import IdleTimeoutError


# =============================================================================
# IdleTimeoutError
# =============================================================================


class TestIdleTimeoutError:
    def test_is_exception(self):
        assert issubclass(IdleTimeoutError, Exception)

    def test_carries_message(self):
        err = IdleTimeoutError("Idle timeout after 1800s")
        assert "1800s" in str(err)

    def test_not_cancelled_error(self):
        """IdleTimeoutError must not be caught by CancelledError handlers."""
        assert not issubclass(IdleTimeoutError, asyncio.CancelledError)


# =============================================================================
# Idle timeout in get_user_input callback
# =============================================================================


class TestIdleTimeoutCallback:
    """Tests for the asyncio.wait_for timeout wrapping user_queue.get()."""

    @pytest.mark.asyncio
    async def test_returns_message_before_timeout(self):
        """User sends a message within the timeout window → no error."""
        queue = asyncio.Queue()
        await queue.put("hello")

        async def get_input():
            return await asyncio.wait_for(queue.get(), timeout=5.0)

        result = await get_input()
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_raises_timeout_when_idle(self):
        """No message within timeout → asyncio.TimeoutError."""
        queue = asyncio.Queue()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)

    @pytest.mark.asyncio
    async def test_timeout_does_not_consume_queued_message(self):
        """Message arriving after timeout should stay in queue for resume."""
        queue = asyncio.Queue()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)

        # Message arrives after timeout
        await queue.put("late message")

        # Should be available on next get
        result = queue.get_nowait()
        assert result == "late message"

    @pytest.mark.asyncio
    async def test_timeout_resets_each_turn(self):
        """Each call to wait_for gets a fresh timeout window."""
        queue = asyncio.Queue()

        # Turn 1: message arrives in time
        await queue.put("msg1")
        r1 = await asyncio.wait_for(queue.get(), timeout=0.1)
        assert r1 == "msg1"

        # Turn 2: no message → timeout
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)

    @pytest.mark.asyncio
    async def test_disabled_when_timeout_is_none(self):
        """When idle_timeout_seconds is None/0, queue.get() blocks normally."""
        queue = asyncio.Queue()
        await queue.put("msg")

        idle_timeout_seconds = None
        if idle_timeout_seconds:
            result = await asyncio.wait_for(queue.get(), timeout=idle_timeout_seconds)
        else:
            result = await queue.get()
        assert result == "msg"


# =============================================================================
# _handle_idle_archive (replicated logic)
# =============================================================================


async def _handle_idle_archive(
    session,
    orchestrator_client,
    thread_id,
    generate_title_fn=None,
):
    """Replicated idle archive logic from persistent_app.py."""
    if not session:
        return

    # 1. Extract memories
    recall_store = (
        getattr(session.tool_context, "recall_store", None)
        if session.tool_context
        else None
    )
    if recall_store and session.auxiliary_llm and session.messages:
        await recall_store.extract(session.messages)

    # 2. Generate title if untitled
    if session.postgres_conn:
        thread = await session.postgres_conn.get_thread(thread_id)
        if thread and thread.get("title") in (None, "Untitled Session", ""):
            if generate_title_fn:
                title = await generate_title_fn(session.messages, session.auxiliary_llm)
                if title:
                    async with session.postgres_conn.acquire() as conn:
                        await conn.execute(
                            "UPDATE threads SET title = $2 WHERE id = $1",
                            thread_id,
                            title,
                        )

    # 3. Set thread to 'ended' (resumable; idle status was collapsed into ended).
    if orchestrator_client and thread_id:
        await orchestrator_client.update_thread_status(thread_id, "ended")
    elif session.postgres_conn and thread_id:
        await session.postgres_conn.update_thread_status(thread_id, "ended")

    # 4. Git commit + push
    if session.workspace_manager:
        git_mgr = getattr(session.workspace_manager, "git_manager", None)
        if git_mgr and git_mgr.is_active:
            if git_mgr.has_uncommitted_changes():
                git_mgr.commit(f"Idle timeout: thread {thread_id}")
            git_mgr.push()


def _mock_session(
    has_recall=False,
    has_messages=True,
    has_postgres=True,
    has_workspace=True,
    has_git=True,
    thread_title="Untitled Session",
):
    session = MagicMock()
    session.messages = [MagicMock()] if has_messages else []
    session.auxiliary_llm = MagicMock() if has_messages else None

    if has_recall:
        session.tool_context.recall_store.extract = AsyncMock()
    else:
        session.tool_context = None

    if has_postgres:
        conn_ctx = AsyncMock()
        conn_ctx.__aenter__ = AsyncMock(return_value=AsyncMock())
        conn_ctx.__aexit__ = AsyncMock(return_value=False)
        session.postgres_conn.acquire.return_value = conn_ctx
        session.postgres_conn.get_thread = AsyncMock(
            return_value={"title": thread_title}
        )
        session.postgres_conn.update_thread_status = AsyncMock()
    else:
        session.postgres_conn = None

    if has_workspace and has_git:
        session.workspace_manager.git_manager.is_active = True
        session.workspace_manager.git_manager.has_uncommitted_changes.return_value = (
            True
        )
        session.workspace_manager.git_manager.commit = MagicMock()
        session.workspace_manager.git_manager.push = MagicMock()
    elif has_workspace:
        session.workspace_manager.git_manager = None
    else:
        session.workspace_manager = None

    return session


class TestHandleIdleArchive:
    @pytest.mark.asyncio
    async def test_sets_thread_to_ended_via_orchestrator(self):
        session = _mock_session()
        orch = AsyncMock()
        await _handle_idle_archive(session, orch, "tid-1")
        orch.update_thread_status.assert_awaited_once_with("tid-1", "ended")

    @pytest.mark.asyncio
    async def test_falls_back_to_direct_db(self):
        session = _mock_session()
        await _handle_idle_archive(session, None, "tid-1")
        session.postgres_conn.update_thread_status.assert_awaited_once_with(
            "tid-1", "ended"
        )

    @pytest.mark.asyncio
    async def test_generates_title_when_untitled(self):
        session = _mock_session(thread_title="Untitled Session")
        gen_title = AsyncMock(return_value="My Chat Title")
        await _handle_idle_archive(session, AsyncMock(), "tid-1", gen_title)
        gen_title.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_title_when_already_titled(self):
        session = _mock_session(thread_title="Existing Title")
        gen_title = AsyncMock(return_value="New Title")
        await _handle_idle_archive(session, AsyncMock(), "tid-1", gen_title)
        gen_title.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_git_commit_and_push(self):
        session = _mock_session(has_git=True)
        await _handle_idle_archive(session, AsyncMock(), "tid-1")
        gm = session.workspace_manager.git_manager
        gm.commit.assert_called_once()
        gm.push.assert_called_once()
        assert "Idle timeout" in gm.commit.call_args[0][0]

    @pytest.mark.asyncio
    async def test_skips_git_when_no_changes(self):
        session = _mock_session(has_git=True)
        session.workspace_manager.git_manager.has_uncommitted_changes.return_value = (
            False
        )
        await _handle_idle_archive(session, AsyncMock(), "tid-1")
        session.workspace_manager.git_manager.commit.assert_not_called()
        session.workspace_manager.git_manager.push.assert_called_once()

    @pytest.mark.asyncio
    async def test_extracts_memories_when_available(self):
        session = _mock_session(has_recall=True)
        await _handle_idle_archive(session, AsyncMock(), "tid-1")
        session.tool_context.recall_store.extract.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_noop_when_session_is_none(self):
        await _handle_idle_archive(None, AsyncMock(), "tid-1")
        # No exception raised


# =============================================================================
# _update_thread_status helper (replicated logic)
# =============================================================================


async def _update_thread_status(
    orchestrator_client,
    session,
    thread_id,
    status,
):
    """Replicated from persistent_app.py."""
    if orchestrator_client and thread_id:
        try:
            await orchestrator_client.update_thread_status(thread_id, status)
            return
        except Exception:
            pass
    if session and session.postgres_conn and thread_id:
        await session.postgres_conn.update_thread_status(thread_id, status)


class TestUpdateThreadStatus:
    @pytest.mark.asyncio
    async def test_prefers_orchestrator_rest(self):
        orch = AsyncMock()
        session = _mock_session()
        await _update_thread_status(orch, session, "tid-1", "active")
        orch.update_thread_status.assert_awaited_once_with("tid-1", "active")
        session.postgres_conn.update_thread_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_db_on_rest_failure(self):
        orch = AsyncMock()
        orch.update_thread_status.side_effect = Exception("connection refused")
        session = _mock_session()
        await _update_thread_status(orch, session, "tid-1", "ended")
        session.postgres_conn.update_thread_status.assert_awaited_once_with(
            "tid-1", "ended"
        )

    @pytest.mark.asyncio
    async def test_uses_db_when_no_orchestrator(self):
        session = _mock_session()
        await _update_thread_status(None, session, "tid-1", "ended")
        session.postgres_conn.update_thread_status.assert_awaited_once_with(
            "tid-1", "ended"
        )

    @pytest.mark.asyncio
    async def test_noop_when_no_thread_id(self):
        orch = AsyncMock()
        session = _mock_session()
        await _update_thread_status(orch, session, None, "active")
        orch.update_thread_status.assert_not_awaited()
        session.postgres_conn.update_thread_status.assert_not_awaited()


# =============================================================================
# mark_orphaned_threads_ended (replicated SQL logic)
# =============================================================================


async def mark_orphaned_threads_ended(db):
    """Replicated from postgres.py."""
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE threads
            SET status        = 'ended',
                ended_at      = CURRENT_TIMESTAMP,
                last_activity = CURRENT_TIMESTAMP
            WHERE status IN ('created', 'active')
              AND agent_id IS NOT NULL
              AND agent_id IN (SELECT id
                               FROM agents
                               WHERE status = 'offline')
            RETURNING id
            """
        )
    return [str(row["id"]) for row in rows]


class TestMarkOrphanedThreadsEnded:
    @pytest.mark.asyncio
    async def test_returns_ids_from_update(self):
        conn = AsyncMock()
        conn.fetch.return_value = [
            {"id": "t-1"},
            {"id": "t-2"},
            {"id": "t-3"},
        ]
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        ids = await mark_orphaned_threads_ended(db)
        assert ids == ["t-1", "t-2", "t-3"]

    @pytest.mark.asyncio
    async def test_returns_empty_when_none_affected(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        ids = await mark_orphaned_threads_ended(db)
        assert ids == []

    @pytest.mark.asyncio
    async def test_sql_filters_created_and_active(self):
        conn = AsyncMock()
        conn.fetch.return_value = []
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        await mark_orphaned_threads_ended(db)
        sql = conn.fetch.call_args[0][0]
        assert "'created'" in sql
        assert "'active'" in sql
        assert "'ended'" in sql
        assert "ended_at" in sql
        # Fresh threads (agent_id IS NULL) are intentionally skipped — they
        # cover legitimate transient states the dispatcher / WS proxy own.
        assert "agent_id IS NOT NULL" in sql
        assert "agent_id IS NULL" not in sql
        assert "'offline'" in sql
        # The caller needs the affected IDs to drive workspace + agent-pod
        # teardown — otherwise resources leak even when the row flips ended.
        assert "RETURNING id" in sql


# =============================================================================
# PUT /api/agents/threads/{id}/status endpoint (replicated logic)
# =============================================================================


async def agent_update_thread_status(db, thread_id, status):
    """Replicated from orchestrator/main.py."""
    valid = {"active", "ended"}
    if status not in valid:
        return {"error": 400, "detail": f"Status must be one of: {valid}"}
    if status == "ended":
        await db.end_thread(thread_id)
    else:
        await db.update_thread_status(thread_id, status)
    return {"status": status}


class TestAgentUpdateThreadStatus:
    @pytest.mark.asyncio
    async def test_accepts_active(self):
        db = AsyncMock()
        result = await agent_update_thread_status(db, "tid-1", "active")
        assert result == {"status": "active"}
        db.update_thread_status.assert_awaited_once_with("tid-1", "active")
        db.end_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_accepts_ended_routes_through_end_thread(self):
        db = AsyncMock()
        result = await agent_update_thread_status(db, "tid-1", "ended")
        assert result == {"status": "ended"}
        # end_thread is invoked so ended_at gets stamped.
        db.end_thread.assert_awaited_once_with("tid-1")
        db.update_thread_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_idle(self):
        db = AsyncMock()
        result = await agent_update_thread_status(db, "tid-1", "idle")
        assert result["error"] == 400
        db.update_thread_status.assert_not_awaited()
        db.end_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_created(self):
        db = AsyncMock()
        result = await agent_update_thread_status(db, "tid-1", "created")
        assert result["error"] == 400

    @pytest.mark.asyncio
    async def test_rejects_arbitrary_string(self):
        db = AsyncMock()
        result = await agent_update_thread_status(db, "tid-1", "bogus")
        assert result["error"] == 400


# =============================================================================
# Stale detector thread sweep (replicated logic)
# =============================================================================


async def stale_detector_sweep(db):
    """Replicated stale agent detector logic, full sequence."""
    count = await db.mark_stale_agents_offline(timeout_minutes=3)
    stuck_working = await db.mark_stuck_working_agents_ready()
    stuck_session = await db.mark_stuck_session_agents_ready()
    ended_ids = await db.mark_orphaned_threads_ended()
    recovered = await db.recover_orphaned_jobs()
    gc_count = await db.gc_offline_agents(retention_hours=24)
    return {
        "stale_agents": count,
        "stuck_working": stuck_working,
        "stuck_session": stuck_session,
        "ended_threads": ended_ids,
        "recovered_jobs": recovered,
        "gc_offline": gc_count,
    }


class TestStaleDetectorSweep:
    @pytest.mark.asyncio
    async def test_marks_threads_ended_after_agents_offline(self):
        db = AsyncMock()
        db.mark_stale_agents_offline.return_value = 2
        db.mark_stuck_working_agents_ready.return_value = 0
        db.mark_stuck_session_agents_ready.return_value = 0
        db.mark_orphaned_threads_ended.return_value = ["thread-a"]
        db.recover_orphaned_jobs.return_value = 0
        db.gc_offline_agents.return_value = 0

        result = await stale_detector_sweep(db)
        assert result["stale_agents"] == 2
        assert result["ended_threads"] == ["thread-a"]

        # Ensure thread sweep is called AFTER agent marking
        calls = db.method_calls
        agent_idx = next(
            i for i, c in enumerate(calls) if c[0] == "mark_stale_agents_offline"
        )
        thread_idx = next(
            i for i, c in enumerate(calls) if c[0] == "mark_orphaned_threads_ended"
        )
        assert agent_idx < thread_idx

    @pytest.mark.asyncio
    async def test_zero_ended_when_no_orphans(self):
        db = AsyncMock()
        db.mark_stale_agents_offline.return_value = 0
        db.mark_stuck_working_agents_ready.return_value = 0
        db.mark_stuck_session_agents_ready.return_value = 0
        db.mark_orphaned_threads_ended.return_value = []
        db.recover_orphaned_jobs.return_value = 0
        db.gc_offline_agents.return_value = 0

        result = await stale_detector_sweep(db)
        assert result["ended_threads"] == []

    @pytest.mark.asyncio
    async def test_consistency_sweeps_run_before_propagation(self):
        # The zombie sweeps (working/session → ready) need to land BEFORE the
        # thread/job propagation sweeps so freshly-released slots are visible
        # to the dispatcher within the same tick.
        db = AsyncMock()
        db.mark_stale_agents_offline.return_value = 0
        db.mark_stuck_working_agents_ready.return_value = 1
        db.mark_stuck_session_agents_ready.return_value = 1
        db.mark_orphaned_threads_ended.return_value = []
        db.recover_orphaned_jobs.return_value = 0
        db.gc_offline_agents.return_value = 0

        await stale_detector_sweep(db)
        names = [c[0] for c in db.method_calls]
        assert names.index("mark_stuck_working_agents_ready") < names.index(
            "mark_orphaned_threads_ended"
        )
        assert names.index("mark_stuck_session_agents_ready") < names.index(
            "recover_orphaned_jobs"
        )

    @pytest.mark.asyncio
    async def test_gc_runs_last(self):
        # GC must run after every other sweep — the earlier ones may still
        # need to read offline agent rows that GC would delete.
        db = AsyncMock()
        db.mark_stale_agents_offline.return_value = 0
        db.mark_stuck_working_agents_ready.return_value = 0
        db.mark_stuck_session_agents_ready.return_value = 0
        db.mark_orphaned_threads_ended.return_value = []
        db.recover_orphaned_jobs.return_value = 0
        db.gc_offline_agents.return_value = 3

        await stale_detector_sweep(db)
        names = [c[0] for c in db.method_calls]
        assert names[-1] == "gc_offline_agents"


# =============================================================================
# mark_stuck_working_agents_ready (replicated SQL logic)
# =============================================================================


async def mark_stuck_working_agents_ready(db):
    """Replicated from postgres.py."""
    async with db.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE agents
            SET status = 'ready'
            WHERE status = 'working'
              AND current_job_id IS NULL
            """
        )
    if result.startswith("UPDATE "):
        return int(result.split()[1])
    return 0


class TestMarkStuckWorkingAgentsReady:
    @pytest.mark.asyncio
    async def test_returns_count(self):
        conn = AsyncMock()
        conn.execute.return_value = "UPDATE 4"
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        assert await mark_stuck_working_agents_ready(db) == 4

    @pytest.mark.asyncio
    async def test_sql_filters_working_with_no_job(self):
        conn = AsyncMock()
        conn.execute.return_value = "UPDATE 0"
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        await mark_stuck_working_agents_ready(db)
        sql = conn.execute.call_args[0][0]
        assert "status = 'ready'" in sql
        assert "status = 'working'" in sql
        assert "current_job_id IS NULL" in sql


# =============================================================================
# mark_stuck_session_agents_ready (replicated SQL logic)
# =============================================================================


async def mark_stuck_session_agents_ready(db):
    """Replicated from postgres.py."""
    async with db.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE agents
            SET status    = 'ready',
                thread_id = NULL
            WHERE status = 'session'
              AND thread_id IS NOT NULL
              AND thread_id IN (SELECT id
                                FROM threads
                                WHERE status = 'ended'
                                  AND ended_at < NOW() - INTERVAL '2 minutes')
            """
        )
    if result.startswith("UPDATE "):
        return int(result.split()[1])
    return 0


class TestMarkStuckSessionAgentsReady:
    @pytest.mark.asyncio
    async def test_returns_count(self):
        conn = AsyncMock()
        conn.execute.return_value = "UPDATE 6"
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        assert await mark_stuck_session_agents_ready(db) == 6

    @pytest.mark.asyncio
    async def test_sql_filters_session_on_ended_thread_with_grace(self):
        conn = AsyncMock()
        conn.execute.return_value = "UPDATE 0"
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        await mark_stuck_session_agents_ready(db)
        sql = conn.execute.call_args[0][0]
        assert "status    = 'ready'" in sql
        assert "thread_id = NULL" in sql
        assert "status = 'session'" in sql
        assert "status = 'ended'" in sql
        # Grace is on thread.ended_at — heartbeat freshness is the wrong
        # signal here because zombies heartbeat normally.
        assert "ended_at < NOW() - INTERVAL '2 minutes'" in sql
        assert "last_heartbeat" not in sql


# =============================================================================
# gc_offline_agents (replicated SQL logic)
# =============================================================================


async def gc_offline_agents(db, retention_hours=24):
    """Replicated from postgres.py."""
    async with db.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM agents
            WHERE status = 'offline'
              AND last_heartbeat < NOW() - ($1 || ' hours')::INTERVAL
            """,
            str(retention_hours),
        )
    if result.startswith("DELETE "):
        return int(result.split()[1])
    return 0


class TestGcOfflineAgents:
    @pytest.mark.asyncio
    async def test_returns_count(self):
        conn = AsyncMock()
        conn.execute.return_value = "DELETE 12"
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        assert await gc_offline_agents(db) == 12

    @pytest.mark.asyncio
    async def test_default_retention_24h(self):
        conn = AsyncMock()
        conn.execute.return_value = "DELETE 0"
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        await gc_offline_agents(db)
        # Retention is parameterised — passed as a string for the
        # `($1 || ' hours')::INTERVAL` cast.
        assert conn.execute.call_args[0][1] == "24"

    @pytest.mark.asyncio
    async def test_custom_retention(self):
        conn = AsyncMock()
        conn.execute.return_value = "DELETE 0"
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        await gc_offline_agents(db, retention_hours=72)
        assert conn.execute.call_args[0][1] == "72"

    @pytest.mark.asyncio
    async def test_sql_targets_only_offline_agents(self):
        conn = AsyncMock()
        conn.execute.return_value = "DELETE 0"
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        await gc_offline_agents(db)
        sql = conn.execute.call_args[0][0]
        assert "DELETE FROM agents" in sql
        assert "status = 'offline'" in sql
        assert "last_heartbeat" in sql


# =============================================================================
# DELETE /api/persistent/threads/{id}?permanent=true (replicated logic)
# =============================================================================


def _mock_db_for_delete():
    db = AsyncMock()
    db.end_thread = AsyncMock()
    db.delete_thread = AsyncMock()
    return db


async def end_or_delete_thread(db, thread_id, permanent=False):
    """Replicated from orchestrator/main.py (post-cleanup portion)."""
    if permanent:
        await db.delete_thread(thread_id)
        return {"status": "deleted"}
    await db.end_thread(thread_id)
    return {"status": "ended"}


class TestPermanentDelete:
    @pytest.mark.asyncio
    async def test_permanent_true_calls_delete(self):
        db = _mock_db_for_delete()
        result = await end_or_delete_thread(db, "tid-1", permanent=True)
        assert result == {"status": "deleted"}
        db.delete_thread.assert_awaited_once_with("tid-1")
        db.end_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_permanent_false_calls_end(self):
        db = _mock_db_for_delete()
        result = await end_or_delete_thread(db, "tid-1", permanent=False)
        assert result == {"status": "ended"}
        db.end_thread.assert_awaited_once_with("tid-1")
        db.delete_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_is_end_not_delete(self):
        db = _mock_db_for_delete()
        result = await end_or_delete_thread(db, "tid-1")
        assert result == {"status": "ended"}
        db.delete_thread.assert_not_awaited()


# =============================================================================
# delete_thread SQL (replicated logic)
# =============================================================================


async def delete_thread(db, thread_id):
    """Replicated from postgres.py."""
    async with db.acquire() as conn:
        await conn.execute(
            "DELETE FROM thread_messages WHERE thread_id = $1",
            thread_id,
        )
        await conn.execute(
            "DELETE FROM threads WHERE id = $1",
            thread_id,
        )


class TestDeleteThread:
    @pytest.mark.asyncio
    async def test_deletes_messages_before_thread(self):
        conn = AsyncMock()
        db = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db.acquire.return_value = ctx

        await delete_thread(db, "tid-1")

        calls = conn.execute.call_args_list
        assert len(calls) == 2
        # Messages deleted first
        assert "thread_messages" in calls[0][0][0]
        assert calls[0][0][1] == "tid-1"
        # Then thread row
        assert "threads" in calls[1][0][0]
        assert calls[1][0][1] == "tid-1"


# =============================================================================
# OrchestratorClient.update_thread_status (replicated logic)
# =============================================================================


async def client_update_thread_status(client, orchestrator_url, thread_id, status):
    """Replicated from orchestrator_client.py."""
    if not client:
        return False
    url = f"{orchestrator_url}/api/agents/threads/{thread_id}/status"
    try:
        r = await client.put(url, json={"status": status})
        return r.status_code == 200
    except Exception:
        return False


class TestOrchestratorClientUpdateStatus:
    @pytest.mark.asyncio
    async def test_sends_put_with_status(self):
        client = AsyncMock()
        client.put.return_value = MagicMock(status_code=200)
        result = await client_update_thread_status(
            client, "http://orch:8085", "tid-1", "active"
        )
        assert result is True
        client.put.assert_awaited_once_with(
            "http://orch:8085/api/agents/threads/tid-1/status",
            json={"status": "active"},
        )

    @pytest.mark.asyncio
    async def test_returns_false_on_error_status(self):
        client = AsyncMock()
        client.put.return_value = MagicMock(status_code=400)
        result = await client_update_thread_status(
            client, "http://orch:8085", "tid-1", "bogus"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        client = AsyncMock()
        client.put.side_effect = ConnectionError("refused")
        result = await client_update_thread_status(
            client, "http://orch:8085", "tid-1", "active"
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_client(self):
        result = await client_update_thread_status(
            None, "http://orch:8085", "tid-1", "active"
        )
        assert result is False


# =============================================================================
# Config defaults
# =============================================================================


class TestIdleTimeoutConfig:
    def test_interactive_config_has_idle_timeout(self):
        from src.core.loader import InteractiveConfig

        ic = InteractiveConfig()
        assert hasattr(ic, "idle_timeout_minutes")
        assert isinstance(ic.idle_timeout_minutes, int)
        assert ic.idle_timeout_minutes > 0

    def test_idle_timeout_zero_disables(self):
        """idle_timeout_minutes=0 should be treated as disabled."""
        timeout = 0
        idle_seconds = timeout * 60 if timeout > 0 else None
        assert idle_seconds is None

    def test_idle_timeout_positive_converts_to_seconds(self):
        timeout = 30
        idle_seconds = timeout * 60 if timeout > 0 else None
        assert idle_seconds == 1800
