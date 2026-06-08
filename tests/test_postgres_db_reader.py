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


# ---------------------------------------------------------------------------
# Phase 3: seq-cursor resume (seq_gt) + boundary resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_seq_gt_filters_and_orders_by_seq():
    """Resume on the seq cursor: seq > S, ordered by seq ASC (insertion order)."""
    db = PostgresDB.__new__(PostgresDB)
    db.fetch = AsyncMock(return_value=[])
    await db.get_thread_messages_history("t1", limit=None, seq_gt=42)
    sql = " ".join(db.fetch.call_args[0][0].split())
    assert "seq >" in sql, "seq_gt must add a seq filter"
    assert "ORDER BY seq ASC" in sql, "seq cursor must order by seq, not turn"
    assert "role <> 'summary'" in sql, "summary exclusion preserved"
    assert 42 in db.fetch.call_args[0][1:], "boundary seq must be a bound param"


@pytest.mark.asyncio
async def test_history_without_seq_gt_keeps_turn_ordering():
    """Back-compat: no seq_gt → original turn/created_at ordering, no seq filter."""
    db = PostgresDB.__new__(PostgresDB)
    db.fetch = AsyncMock(return_value=[])
    await db.get_thread_messages_history("t1")
    sql = " ".join(db.fetch.call_args[0][0].split())
    assert "seq >" not in sql
    assert "ORDER BY turn_number ASC" in sql


@pytest.mark.asyncio
async def test_get_seq_for_message_id_coerces_and_returns_seq():
    import uuid

    from src.database.postgres_db import _THREAD_MSG_ID_NS

    db = PostgresDB.__new__(PostgresDB)
    db.fetchrow = AsyncMock(return_value={"seq": 99})
    seq = await db.get_seq_for_message_id("t1", "chatcmpl-xyz")
    assert seq == 99
    sql = " ".join(db.fetchrow.call_args[0][0].split())
    assert "SELECT seq FROM thread_messages" in sql
    # The provider id is coerced to its UUID row id for the lookup.
    assert db.fetchrow.call_args[0][2] == str(
        uuid.uuid5(_THREAD_MSG_ID_NS, "chatcmpl-xyz")
    )


@pytest.mark.asyncio
async def test_get_seq_for_message_id_returns_none_when_absent():
    """Unpersisted boundary (transient injection / fresh resume id) → None."""
    db = PostgresDB.__new__(PostgresDB)
    db.fetchrow = AsyncMock(return_value=None)
    assert await db.get_seq_for_message_id("t1", "m_x") is None


@pytest.mark.asyncio
async def test_checkpoint_surfaces_boundary_seq():
    db = PostgresDB.__new__(PostgresDB)
    db.fetchrow = AsyncMock(
        return_value={
            "content": "summary",
            "metrics": '{"boundary_turn": 7, "boundary_seq": 780}',
            "turn_number": 8,
        }
    )
    result = await db.get_latest_compaction_checkpoint("t1")
    assert result["boundary_seq"] == 780
    assert result["boundary_turn"] == 7


# ---------------------------------------------------------------------------
# Phase 4: resume floor (newest_first)
# ---------------------------------------------------------------------------


def _full_row(mid, turn):
    """A thread_messages row with every column the reader maps."""
    return {
        "id": mid,
        "role": "ai",
        "content": f"c{mid}",
        "tool_calls": None,
        "turn_number": turn,
        "metrics": None,
        "tool_call_id": None,
        "thinking": None,
        "reasoning": None,
        "tool_results": None,
        "provider": None,
        "provider_raw": None,
        "additional_kwargs": None,
        "response_metadata": None,
        "created_at": None,
    }


@pytest.mark.asyncio
async def test_newest_first_selects_seq_desc_and_returns_chronological():
    """The resume floor takes the NEWEST N (seq DESC + LIMIT) but hands them
    back oldest→newest so the restored context reads chronologically."""
    db = PostgresDB.__new__(PostgresDB)
    # The DB returns rows newest-first under ORDER BY seq DESC.
    db.fetch = AsyncMock(
        return_value=[_full_row("n3", 3), _full_row("n2", 2), _full_row("n1", 1)]
    )
    rows = await db.get_thread_messages_history("t1", limit=1000, newest_first=True)
    sql = " ".join(db.fetch.call_args[0][0].split())
    assert "ORDER BY seq DESC" in sql, "newest_first must select by seq DESC"
    assert "LIMIT" in sql, "the floor must cap the row count"
    assert [r["id"] for r in rows] == ["n1", "n2", "n3"], "result must be chronological"


@pytest.mark.asyncio
async def test_newest_first_composes_with_seq_cursor():
    """The floor and the boundary_seq cursor combine: newest N of seq > S."""
    db = PostgresDB.__new__(PostgresDB)
    db.fetch = AsyncMock(return_value=[])
    await db.get_thread_messages_history("t1", limit=500, seq_gt=42, newest_first=True)
    sql = " ".join(db.fetch.call_args[0][0].split())
    assert "seq >" in sql and "ORDER BY seq DESC" in sql
    args = db.fetch.call_args[0]
    assert 42 in args[1:] and 500 in args[1:]
