"""The three officer tools as an MCP caller experiences them.

The client and formatter contracts are pinned elsewhere; what these tests
protect is the wiring an assistant depends on mid-task: the post read pulls
the tail of his log with it, a failed tail read never costs the whole answer,
and a tail request asks the server for the END of the log rather than paging
from turn one.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MCP_TRANSPORT", "stdio")

from mcp_server import server as _mcp_server_mod  # noqa: E402

PROJECT_ID = "a572e4a0-d97a-4103-91fd-92a980d6717d"
THREAD_ID = "6ce5bc4c-b773-4027-b47f-55d5308c92bb"

POST = {
    "commissioned": True,
    "held": None,
    "officer": {
        "thread_id": THREAD_ID,
        "status": "active",
        "title": "Centurion — Better Resavio",
        "model": "gpt-5.6-sol",
    },
    "kit": {"build": {"count": 1, "in_flight": 1}},
    "next_wake_at": "2026-08-17T09:30:00+00:00",
    "pending_events": 0,
    "backlog": {"auto_pull": False},
}

TAIL = {
    "thread_id": THREAD_ID,
    "total": 590,
    "messages": [
        {
            "role": "event",
            "created_at": "2026-08-17T06:05:00+00:00",
            "content": "[SITREP] 2026-08-17 06:05 UTC — delta since 30m ago\nJobs (2)",
        },
        {
            "role": "ai",
            "created_at": "2026-08-17T06:11:00+00:00",
            "content": "",
            "tool_calls": [{"name": "list_project_jobs"}, {"name": "sleep"}],
        },
    ],
}


@pytest.mark.asyncio
async def test_the_post_tool_brings_the_tail_of_his_log_with_it():
    client = AsyncMock()
    client.get_project_officer.return_value = POST
    client.get_persistent_thread_messages.return_value = TAIL

    with patch.object(_mcp_server_mod, "_get_client", return_value=client):
        text = await _mcp_server_mod.get_project_officer(PROJECT_ID)

    assert "Centurion — Better Resavio" in text
    assert "Recent log" in text
    assert "list_project_jobs" in text
    # The tail is a cursor read, never offset paging from the first turn.
    assert client.get_persistent_thread_messages.await_args.kwargs["before"]


@pytest.mark.asyncio
async def test_a_broken_tail_read_never_costs_the_whole_post():
    client = AsyncMock()
    client.get_project_officer.return_value = POST
    client.get_persistent_thread_messages.side_effect = RuntimeError("thread gone")

    with patch.object(_mcp_server_mod, "_get_client", return_value=client):
        text = await _mcp_server_mod.get_project_officer(PROJECT_ID)

    assert "Officer —" in text
    assert "log unavailable" in text.lower()


@pytest.mark.asyncio
async def test_a_vacant_post_is_not_chased_for_a_log():
    client = AsyncMock()
    client.get_project_officer.return_value = {"commissioned": False, "officer": {}}

    with patch.object(_mcp_server_mod, "_get_client", return_value=client):
        text = await _mcp_server_mod.get_project_officer(PROJECT_ID)

    assert "vacant" in text.lower()
    client.get_persistent_thread_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_note_tool_reports_the_delivery_it_actually_got():
    client = AsyncMock()
    client.send_officer_note.return_value = {
        "delivered": "queued",
        "thread_id": THREAD_ID,
        "next_wake_at": "2026-08-17T09:30:00+00:00",
    }

    with patch.object(_mcp_server_mod, "_get_client", return_value=client):
        text = await _mcp_server_mod.send_officer_note(
            PROJECT_ID, "Cut the theme work."
        )

    client.send_officer_note.assert_awaited_once_with(PROJECT_ID, "Cut the theme work.")
    assert "queued" in text.lower()
    assert "2026-08-17T09:30" in text


@pytest.mark.asyncio
async def test_the_roster_tool_renders_the_fleet():
    client = AsyncMock()
    client.list_officers.return_value = {"officers": [], "total": 0}

    with patch.object(_mcp_server_mod, "_get_client", return_value=client):
        text = await _mcp_server_mod.list_officers()

    assert "no officer" in text.lower()


@pytest.mark.asyncio
async def test_newest_first_reads_the_end_of_a_long_log_in_one_call():
    client = AsyncMock()
    client.get_persistent_thread_messages.return_value = TAIL

    with patch.object(_mcp_server_mod, "_get_client", return_value=client):
        text = await _mcp_server_mod.get_persistent_thread_messages(
            THREAD_ID, limit=10, newest_first=True
        )

    kwargs = client.get_persistent_thread_messages.await_args.kwargs
    assert kwargs["before"]
    assert kwargs.get("offset", 0) == 0
    assert "newest" in text.lower()
