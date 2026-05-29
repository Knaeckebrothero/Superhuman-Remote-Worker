import pytest
from unittest.mock import AsyncMock
from src.database.postgres_db import PostgresDB


@pytest.mark.asyncio
async def test_history_includes_components_and_tool_link():
    db = PostgresDB.__new__(PostgresDB)  # bypass __init__/connection
    db.fetch = AsyncMock(
        return_value=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "role": "ai",
                "content": "hi",
                "tool_calls": None,
                "turn_number": 1,
                "metrics": None,
                "tool_call_id": None,
                "thinking": "legacy reasoning",
                "reasoning": None,
                "tool_results": None,
                "provider": "openai-chat",
                "provider_raw": None,
                "additional_kwargs": None,
                "response_metadata": None,
                "created_at": None,
            }
        ]
    )
    rows = await db.get_thread_messages_history("t1")
    row = rows[0]
    for key in (
        "tool_call_id",
        "thinking",
        "reasoning",
        "tool_results",
        "provider",
        "provider_raw",
        "additional_kwargs",
        "response_metadata",
    ):
        assert key in row, f"reader dropped {key}"
    assert row["provider"] == "openai-chat"
    assert row["thinking"] == "legacy reasoning"


@pytest.mark.asyncio
async def test_history_excludes_summary_marker_rows():
    """Display-only role='summary' compaction markers must never re-enter the
    agent's resumed LLM context (they'd double-count its own summary)."""
    db = PostgresDB.__new__(PostgresDB)
    db.fetch = AsyncMock(return_value=[])
    await db.get_thread_messages_history("t1")
    sql = " ".join(db.fetch.call_args[0][0].split())
    assert "role <> 'summary'" in sql


@pytest.mark.asyncio
async def test_history_supports_since_turn_for_checkpoint_resume():
    """Resume-from-checkpoint loads only the tail (turn > B), skipping rows
    the persisted summary already covers. Without ``since_turn`` the query is
    unchanged."""
    db = PostgresDB.__new__(PostgresDB)
    db.fetch = AsyncMock(return_value=[])

    await db.get_thread_messages_history("t1", since_turn=5)
    sql = " ".join(db.fetch.call_args[0][0].split())
    assert "turn_number >" in sql, "since_turn must add a turn_number filter"
    assert "role <> 'summary'" in sql, "since_turn must preserve summary exclusion"
    assert "ORDER BY turn_number ASC" in sql, "ordering must be preserved"
    # The boundary value is bound as a parameter, not interpolated.
    args = db.fetch.call_args[0]
    assert 5 in args[1:], f"boundary 5 must be a bound param, got args={args}"

    db.fetch = AsyncMock(return_value=[])
    await db.get_thread_messages_history("t1")
    sql_no_since = " ".join(db.fetch.call_args[0][0].split())
    assert "turn_number >" not in sql_no_since, (
        "default call (since_turn=None) must NOT filter by turn_number — "
        "back-compat with existing callers"
    )


@pytest.mark.asyncio
async def test_get_latest_compaction_checkpoint_parses_metrics():
    """Returns the latest summary row's content + boundary_turn from metrics."""
    db = PostgresDB.__new__(PostgresDB)
    db.fetchrow = AsyncMock(
        return_value={
            "content": "summary of prior work",
            # metrics may arrive as JSON string from asyncpg if codec isn't registered;
            # the method must handle both dict and str/bytes.
            "metrics": '{"before": 100, "after": 11, "trigger": "auto", "boundary_turn": 7}',
            "turn_number": 8,
        }
    )

    result = await db.get_latest_compaction_checkpoint("t1")

    assert result is not None
    assert result["summary"] == "summary of prior work"
    assert result["boundary_turn"] == 7
    assert result["turn_number"] == 8

    # SQL must select only summary rows, ordered newest-first.
    sql = " ".join(db.fetchrow.call_args[0][0].split())
    assert "role = 'summary'" in sql
    assert "ORDER BY turn_number DESC" in sql
    assert "LIMIT 1" in sql


@pytest.mark.asyncio
async def test_get_latest_compaction_checkpoint_returns_none_when_absent():
    """No summary row → None → restore falls back to Path B (full load)."""
    db = PostgresDB.__new__(PostgresDB)
    db.fetchrow = AsyncMock(return_value=None)
    assert await db.get_latest_compaction_checkpoint("t1") is None


@pytest.mark.asyncio
async def test_get_latest_compaction_checkpoint_handles_missing_boundary_turn():
    """Back-compat: rows written before this feature lack ``boundary_turn``."""
    db = PostgresDB.__new__(PostgresDB)
    db.fetchrow = AsyncMock(
        return_value={
            "content": "old phase-3 summary",
            "metrics": {"before": 50, "after": 8, "trigger": "manual"},
            "turn_number": 3,
        }
    )

    result = await db.get_latest_compaction_checkpoint("t1")
    assert result is not None
    assert result["boundary_turn"] is None, (
        "absent boundary_turn must surface as None so restore falls back to Path B"
    )
