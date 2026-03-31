"""Tests for orchestrator/database/postgres.py thread methods.

Covers section 6 of persistent_agent_tests.md:
  6.1  create_thread
  6.2  get_thread
  6.3  list_threads
  6.4  end_thread
  6.5  update_thread_status
  6.6  update_thread_agent
  6.7  save_thread_message
  6.8  get_thread_messages_history
  6.9  get_thread_message_count
  6.10 update_thread_tokens
  6.11 merge_thread_workspace_context / merge_thread_vm_context

Mocks the asyncpg connection pool to test method logic without a live DB.
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from orchestrator.database.postgres import PostgresDB


# =============================================================================
# Fixtures
# =============================================================================


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
    """Create a mock asyncpg connection."""
    conn = AsyncMock()
    return conn


# =============================================================================
# 6.1: create_thread
# =============================================================================


class TestCreateThread:
    """Tests for create_thread method."""

    @pytest.mark.asyncio
    async def test_returns_uuid_string(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(
            return_value={"id": UUID("aaaaaaaa-1111-2222-3333-444444444444")}
        )
        db = _make_db_with_conn(conn)

        result = await db.create_thread()
        assert result == "aaaaaaaa-1111-2222-3333-444444444444"
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_passes_all_params_to_query(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(
            return_value={"id": UUID("aaaaaaaa-1111-2222-3333-444444444444")}
        )
        db = _make_db_with_conn(conn)

        await db.create_thread(
            user_id="user-1",
            project_id="proj-1",
            config_name="interactive",
            permission_mode="autonomous",
            title="My Session",
        )

        call_args = conn.fetchrow.call_args
        # Positional args after the SQL query
        assert call_args[0][1] == "user-1"  # user_id
        assert call_args[0][2] == "proj-1"  # project_id
        assert call_args[0][3] == "interactive"  # config_name
        assert call_args[0][4] == "autonomous"  # permission_mode
        assert call_args[0][5] == "My Session"  # title

    @pytest.mark.asyncio
    async def test_optional_params_default_none(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(
            return_value={"id": UUID("aaaaaaaa-1111-2222-3333-444444444444")}
        )
        db = _make_db_with_conn(conn)

        await db.create_thread()

        call_args = conn.fetchrow.call_args
        assert call_args[0][1] is None  # user_id
        assert call_args[0][2] is None  # project_id

    @pytest.mark.asyncio
    async def test_default_values(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(
            return_value={"id": UUID("aaaaaaaa-1111-2222-3333-444444444444")}
        )
        db = _make_db_with_conn(conn)

        await db.create_thread()

        call_args = conn.fetchrow.call_args
        assert call_args[0][3] == "defaults"  # config_name
        assert call_args[0][4] == "supervised"  # permission_mode
        assert call_args[0][5] == "Untitled Session"  # title


# =============================================================================
# 6.2: get_thread
# =============================================================================


class TestGetThread:
    """Tests for get_thread method."""

    @pytest.mark.asyncio
    async def test_returns_dict_for_existing_thread(self):
        row = {"id": "tid-1", "user_id": "user-1", "status": "active"}
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(return_value=row)
        db = _make_db_with_conn(conn)

        result = await db.get_thread("tid-1")
        assert result == {"id": "tid-1", "user_id": "user-1", "status": "active"}

    @pytest.mark.asyncio
    async def test_returns_none_for_nonexistent(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        db = _make_db_with_conn(conn)

        result = await db.get_thread("nonexistent")
        assert result is None


# =============================================================================
# 6.3: list_threads
# =============================================================================


class TestListThreads:
    """Tests for list_threads method."""

    @pytest.mark.asyncio
    async def test_no_filters_returns_all(self):
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[{"id": "t1"}, {"id": "t2"}])
        db = _make_db_with_conn(conn)

        result = await db.list_threads()
        assert len(result) == 2
        # SQL should have no WHERE clause
        sql = conn.fetch.call_args[0][0]
        assert "WHERE" not in sql
        assert "ORDER BY created_at DESC" in sql
        assert "LIMIT 50" in sql

    @pytest.mark.asyncio
    async def test_user_id_filter_includes_null(self):
        """user_id filter includes threads with matching user_id AND user_id IS NULL."""
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db_with_conn(conn)

        await db.list_threads(user_id="user-1")
        sql = conn.fetch.call_args[0][0]
        assert "user_id = $1 OR user_id IS NULL" in sql

    @pytest.mark.asyncio
    async def test_project_id_filter(self):
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db_with_conn(conn)

        await db.list_threads(project_id="proj-1")
        sql = conn.fetch.call_args[0][0]
        assert "project_id = $1" in sql

    @pytest.mark.asyncio
    async def test_status_filter(self):
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db_with_conn(conn)

        await db.list_threads(status="ended")
        sql = conn.fetch.call_args[0][0]
        assert "status = $1" in sql

    @pytest.mark.asyncio
    async def test_multiple_filters_combined_with_and(self):
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db_with_conn(conn)

        await db.list_threads(user_id="u1", project_id="p1", status="active")
        sql = conn.fetch.call_args[0][0]
        assert "AND" in sql

    @pytest.mark.asyncio
    async def test_capped_at_50(self):
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db_with_conn(conn)

        await db.list_threads()
        sql = conn.fetch.call_args[0][0]
        assert "LIMIT 50" in sql


# =============================================================================
# 6.4: end_thread
# =============================================================================


class TestEndThread:
    """Tests for end_thread method."""

    @pytest.mark.asyncio
    async def test_sets_status_ended(self):
        conn = _mock_conn()
        db = _make_db_with_conn(conn)

        await db.end_thread("tid-1")

        sql = " ".join(conn.execute.call_args[0][0].split())
        assert "status = 'ended'" in sql
        assert "ended_at = CURRENT_TIMESTAMP" in sql
        assert conn.execute.call_args[0][1] == "tid-1"

    @pytest.mark.asyncio
    async def test_does_not_error_on_nonexistent_thread(self):
        """UPDATE affects 0 rows — no exception."""
        conn = _mock_conn()
        db = _make_db_with_conn(conn)

        # Should not raise
        await db.end_thread("nonexistent")


# =============================================================================
# 6.5: update_thread_status
# =============================================================================


class TestUpdateThreadStatus:
    """Tests for update_thread_status method."""

    @pytest.mark.asyncio
    async def test_updates_status_and_last_activity(self):
        conn = _mock_conn()
        db = _make_db_with_conn(conn)

        await db.update_thread_status("tid-1", "idle")

        sql = conn.execute.call_args[0][0]
        assert "status = $2" in sql
        assert "last_activity = CURRENT_TIMESTAMP" in sql
        assert conn.execute.call_args[0][1] == "tid-1"
        assert conn.execute.call_args[0][2] == "idle"


# =============================================================================
# 6.6: update_thread_agent
# =============================================================================


class TestUpdateThreadAgent:
    """Tests for update_thread_agent method."""

    @pytest.mark.asyncio
    async def test_binds_agent_id(self):
        conn = _mock_conn()
        db = _make_db_with_conn(conn)

        await db.update_thread_agent("tid-1", "agent-42")

        sql = conn.execute.call_args[0][0]
        assert "agent_id = $2" in sql
        assert conn.execute.call_args[0][1] == "tid-1"
        assert conn.execute.call_args[0][2] == "agent-42"


# =============================================================================
# 6.7: save_thread_message
# =============================================================================


class TestSaveThreadMessage:
    """Tests for save_thread_message method."""

    @pytest.mark.asyncio
    async def test_returns_uuid_string(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(
            return_value={"id": UUID("bbbbbbbb-1111-2222-3333-444444444444")}
        )
        db = _make_db_with_conn(conn)

        result = await db.save_thread_message("tid-1", "user", "hello")
        assert result == "bbbbbbbb-1111-2222-3333-444444444444"

    @pytest.mark.asyncio
    async def test_tool_calls_serialized_to_json(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(
            return_value={"id": UUID("bbbbbbbb-1111-2222-3333-444444444444")}
        )
        db = _make_db_with_conn(conn)

        tool_calls = [{"name": "search", "args": {"q": "test"}}]
        await db.save_thread_message("tid-1", "assistant", None, tool_calls=tool_calls)

        insert_args = conn.fetchrow.call_args[0]
        # tool_calls is 4th positional param after SQL
        serialized = insert_args[4]
        assert json.loads(serialized) == tool_calls

    @pytest.mark.asyncio
    async def test_tool_calls_none_becomes_sql_null(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(
            return_value={"id": UUID("bbbbbbbb-1111-2222-3333-444444444444")}
        )
        db = _make_db_with_conn(conn)

        await db.save_thread_message("tid-1", "user", "hi", tool_calls=None)

        insert_args = conn.fetchrow.call_args[0]
        assert insert_args[4] is None  # tool_calls

    @pytest.mark.asyncio
    async def test_updates_thread_last_activity_and_turns(self):
        conn = _mock_conn()
        conn.fetchrow = AsyncMock(
            return_value={"id": UUID("bbbbbbbb-1111-2222-3333-444444444444")}
        )
        db = _make_db_with_conn(conn)

        await db.save_thread_message("tid-1", "user", "hi", turn_number=5)

        # Second call should be the UPDATE
        update_call = conn.execute.call_args
        sql = update_call[0][0]
        assert "last_activity = CURRENT_TIMESTAMP" in sql
        assert "GREATEST(total_turns" in sql
        assert update_call[0][2] == 5  # turn_number


# =============================================================================
# 6.8: get_thread_messages_history
# =============================================================================


class TestGetThreadMessagesHistory:
    """Tests for get_thread_messages_history method."""

    @pytest.mark.asyncio
    async def test_returns_messages_asc_order(self):
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db_with_conn(conn)

        await db.get_thread_messages_history("tid-1")
        sql = conn.fetch.call_args[0][0]
        assert "ORDER BY created_at ASC" in sql

    @pytest.mark.asyncio
    async def test_supports_limit_and_offset(self):
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[])
        db = _make_db_with_conn(conn)

        await db.get_thread_messages_history("tid-1", limit=50, offset=100)
        args = conn.fetch.call_args[0]
        assert args[2] == 50  # limit
        assert args[3] == 100  # offset

    @pytest.mark.asyncio
    async def test_deserializes_tool_calls_json(self):
        ts = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)
        row = {
            "id": UUID("cccccccc-1111-2222-3333-444444444444"),
            "role": "assistant",
            "content": None,
            "tool_calls": '[{"name": "search"}]',
            "turn_number": 1,
            "metrics": None,
            "created_at": ts,
        }
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[row])
        db = _make_db_with_conn(conn)

        result = await db.get_thread_messages_history("tid-1")
        assert result[0]["tool_calls"] == [{"name": "search"}]

    @pytest.mark.asyncio
    async def test_null_tool_calls_becomes_none(self):
        row = {
            "id": UUID("cccccccc-1111-2222-3333-444444444444"),
            "role": "user",
            "content": "hi",
            "tool_calls": None,
            "turn_number": 1,
            "metrics": None,
            "created_at": datetime(2026, 3, 30, tzinfo=timezone.utc),
        }
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[row])
        db = _make_db_with_conn(conn)

        result = await db.get_thread_messages_history("tid-1")
        assert result[0]["tool_calls"] is None

    @pytest.mark.asyncio
    async def test_formats_created_at_as_iso_string(self):
        ts = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)
        row = {
            "id": UUID("cccccccc-1111-2222-3333-444444444444"),
            "role": "user",
            "content": "hi",
            "tool_calls": None,
            "turn_number": 1,
            "metrics": None,
            "created_at": ts,
        }
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[row])
        db = _make_db_with_conn(conn)

        result = await db.get_thread_messages_history("tid-1")
        assert result[0]["created_at"] == ts.isoformat()

    @pytest.mark.asyncio
    async def test_null_created_at_becomes_none(self):
        row = {
            "id": UUID("cccccccc-1111-2222-3333-444444444444"),
            "role": "user",
            "content": "hi",
            "tool_calls": None,
            "turn_number": 1,
            "metrics": None,
            "created_at": None,
        }
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[row])
        db = _make_db_with_conn(conn)

        result = await db.get_thread_messages_history("tid-1")
        assert result[0]["created_at"] is None

    @pytest.mark.asyncio
    async def test_result_dict_keys(self):
        row = {
            "id": UUID("cccccccc-1111-2222-3333-444444444444"),
            "role": "user",
            "content": "hi",
            "tool_calls": None,
            "turn_number": 3,
            "metrics": None,
            "created_at": datetime(2026, 3, 30, tzinfo=timezone.utc),
        }
        conn = _mock_conn()
        conn.fetch = AsyncMock(return_value=[row])
        db = _make_db_with_conn(conn)

        result = await db.get_thread_messages_history("tid-1")
        assert set(result[0].keys()) == {
            "id",
            "role",
            "content",
            "tool_calls",
            "turn_number",
            "metrics",
            "created_at",
        }


# =============================================================================
# 6.9: get_thread_message_count
# =============================================================================


class TestGetThreadMessageCount:
    """Tests for get_thread_message_count method."""

    @pytest.mark.asyncio
    async def test_returns_integer_count(self):
        conn = _mock_conn()
        conn.fetchval = AsyncMock(return_value=42)
        db = _make_db_with_conn(conn)

        result = await db.get_thread_message_count("tid-1")
        assert result == 42

    @pytest.mark.asyncio
    async def test_returns_zero_for_empty(self):
        conn = _mock_conn()
        conn.fetchval = AsyncMock(return_value=0)
        db = _make_db_with_conn(conn)

        result = await db.get_thread_message_count("tid-1")
        assert result == 0


# =============================================================================
# 6.10: update_thread_tokens
# =============================================================================


class TestUpdateThreadTokens:
    """Tests for update_thread_tokens method."""

    @pytest.mark.asyncio
    async def test_increments_tokens(self):
        conn = _mock_conn()
        db = _make_db_with_conn(conn)

        await db.update_thread_tokens("tid-1", 1500)

        sql = conn.execute.call_args[0][0]
        assert "total_tokens = total_tokens + $2" in sql
        assert conn.execute.call_args[0][1] == "tid-1"
        assert conn.execute.call_args[0][2] == 1500


# =============================================================================
# 6.11: merge_thread_workspace_context / merge_thread_vm_context
# =============================================================================


class TestMergeThreadWorkspaceContext:
    """Tests for merge_thread_workspace_context method."""

    @pytest.mark.asyncio
    async def test_returns_true_on_update(self):
        conn = _mock_conn()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        db = _make_db_with_conn(conn)

        result = await db.merge_thread_workspace_context(
            "aaaaaaaa-1111-2222-3333-444444444444",
            {"pod_ip": "10.0.0.5"},
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self):
        conn = _mock_conn()
        conn.execute = AsyncMock(return_value="UPDATE 0")
        db = _make_db_with_conn(conn)

        result = await db.merge_thread_workspace_context(
            "aaaaaaaa-1111-2222-3333-444444444444",
            {"pod_ip": "10.0.0.5"},
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_for_invalid_uuid(self):
        conn = _mock_conn()
        db = _make_db_with_conn(conn)

        result = await db.merge_thread_workspace_context(
            "not-a-uuid", {"pod_ip": "10.0.0.5"}
        )
        assert result is False
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_merges_into_workspace_container_path(self):
        conn = _mock_conn()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        db = _make_db_with_conn(conn)

        await db.merge_thread_workspace_context(
            "aaaaaaaa-1111-2222-3333-444444444444",
            {"status": "ready"},
        )
        sql = conn.execute.call_args[0][0]
        assert "workspace_container" in sql
        # First param is the JSON-serialized updates
        json_param = conn.execute.call_args[0][1]
        assert json.loads(json_param) == {"status": "ready"}


class TestMergeThreadVmContext:
    """Tests for merge_thread_vm_context method."""

    @pytest.mark.asyncio
    async def test_returns_true_on_update(self):
        conn = _mock_conn()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        db = _make_db_with_conn(conn)

        result = await db.merge_thread_vm_context(
            "aaaaaaaa-1111-2222-3333-444444444444",
            {"status": "ready", "ssh_host": "10.0.0.99"},
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_for_invalid_uuid(self):
        conn = _mock_conn()
        db = _make_db_with_conn(conn)

        result = await db.merge_thread_vm_context("bad-uuid", {"status": "ready"})
        assert result is False

    @pytest.mark.asyncio
    async def test_merges_into_vm_path(self):
        conn = _mock_conn()
        conn.execute = AsyncMock(return_value="UPDATE 1")
        db = _make_db_with_conn(conn)

        await db.merge_thread_vm_context(
            "aaaaaaaa-1111-2222-3333-444444444444",
            {"ssh_port": 2222},
        )
        sql = conn.execute.call_args[0][0]
        assert "'{vm}'" in sql
        json_param = conn.execute.call_args[0][1]
        assert json.loads(json_param) == {"ssh_port": 2222}
