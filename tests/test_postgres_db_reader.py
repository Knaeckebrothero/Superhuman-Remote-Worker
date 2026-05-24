import pytest
from unittest.mock import AsyncMock
from src.database.postgres_db import PostgresDB


@pytest.mark.asyncio
async def test_history_includes_components_and_tool_link():
    db = PostgresDB.__new__(PostgresDB)  # bypass __init__/connection
    db.fetch = AsyncMock(return_value=[{
        "id": "11111111-1111-1111-1111-111111111111",
        "role": "ai", "content": "hi", "tool_calls": None,
        "turn_number": 1, "metrics": None,
        "tool_call_id": None, "thinking": "legacy reasoning",
        "reasoning": None, "tool_results": None,
        "provider": "openai-chat", "provider_raw": None,
        "additional_kwargs": None, "response_metadata": None,
        "created_at": None,
    }])
    rows = await db.get_thread_messages_history("t1")
    row = rows[0]
    for key in ("tool_call_id", "thinking", "reasoning", "tool_results",
                "provider", "provider_raw", "additional_kwargs", "response_metadata"):
        assert key in row, f"reader dropped {key}"
    assert row["provider"] == "openai-chat"
    assert row["thinking"] == "legacy reasoning"
