"""Batch tool-call approval — announce step.

docs/superpowers/specs/2026-08-01-batch-tool-approval-design.md
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.api.persistent_app as pa


def _mock_session(permission_mode: str = "supervised"):
    session = MagicMock()
    session.permission_mode = permission_mode
    session.tool_decisions = {}
    session.postgres_conn = MagicMock()
    return session


CALLS = [
    {"name": "web_search", "args": {"query": "france"}, "id": "tc_0"},
    {"name": "web_search", "args": {"query": "japan"}, "id": "tc_1"},
]


class TestAnnounceBatch:
    @pytest.mark.asyncio
    async def test_inserts_a_row_per_call_and_broadcasts_once(self):
        inserts = []

        async def _insert(tool_call_id, tool_name, tool_args):
            inserts.append(tool_call_id)
            return f"rid-{tool_call_id}"

        bcast = MagicMock()
        with (
            patch.object(pa, "_session", _mock_session()),
            patch.object(pa, "_thread_id", "tid"),
            patch.object(pa, "_insert_permission_request", _insert),
            patch.object(pa, "_broadcast", bcast),
        ):
            await pa._loop_announce_permission_batch(CALLS)

        assert inserts == ["tc_0", "tc_1"]
        assert bcast.call_count == 1
        event, payload = bcast.call_args.args
        assert event == "permission.request_batch"
        assert [r["id"] for r in payload["requests"]] == ["tc_0", "tc_1"]
        assert payload["requests"][0]["approval_id"] == "rid-tc_0"
        assert payload["requests"][0]["tool"] == "web_search"
        assert payload["requests"][0]["args"] == {"query": "france"}

    @pytest.mark.asyncio
    async def test_autonomous_mode_announces_nothing(self):
        bcast = MagicMock()
        insert = AsyncMock()
        with (
            patch.object(pa, "_session", _mock_session("autonomous")),
            patch.object(pa, "_thread_id", "tid"),
            patch.object(pa, "_insert_permission_request", insert),
            patch.object(pa, "_broadcast", bcast),
        ):
            await pa._loop_announce_permission_batch(CALLS)

        insert.assert_not_awaited()
        bcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_accept_announces_only_shell_calls(self):
        """Under auto_accept only shell tools are gated — the card must not
        imply the auto-approved calls need a decision."""
        mixed = CALLS + [{"name": "run_command", "args": {"cmd": "ls"}, "id": "tc_2"}]
        inserts = []

        async def _insert(tool_call_id, tool_name, tool_args):
            inserts.append(tool_call_id)
            return f"rid-{tool_call_id}"

        bcast = MagicMock()
        with (
            patch.object(pa, "_session", _mock_session("auto_accept")),
            patch.object(pa, "_thread_id", "tid"),
            patch.object(pa, "_insert_permission_request", _insert),
            patch.object(pa, "_broadcast", bcast),
        ):
            await pa._loop_announce_permission_batch(mixed)

        assert inserts == ["tc_2"]
        assert [r["id"] for r in bcast.call_args.args[1]["requests"]] == ["tc_2"]

    @pytest.mark.asyncio
    async def test_insert_failure_is_soft_and_skips_that_entry(self):
        async def _insert(tool_call_id, tool_name, tool_args):
            return None if tool_call_id == "tc_0" else "rid-tc_1"

        bcast = MagicMock()
        with (
            patch.object(pa, "_session", _mock_session()),
            patch.object(pa, "_thread_id", "tid"),
            patch.object(pa, "_insert_permission_request", _insert),
            patch.object(pa, "_broadcast", bcast),
        ):
            await pa._loop_announce_permission_batch(CALLS)

        # tc_0 has no durable row, so the per-call gate path will handle it.
        assert [r["id"] for r in bcast.call_args.args[1]["requests"]] == ["tc_1"]

    @pytest.mark.asyncio
    async def test_no_session_is_a_noop(self):
        bcast = MagicMock()
        with (
            patch.object(pa, "_session", None),
            patch.object(pa, "_broadcast", bcast),
        ):
            await pa._loop_announce_permission_batch(CALLS)
        bcast.assert_not_called()
