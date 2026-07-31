"""Batch tool-call approval — announce step.

docs/superpowers/specs/2026-08-01-batch-tool-approval-design.md
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.api.persistent_app as pa
from src.persistent_graph import PermissionOutcome


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


def _conn_with(fetchrow_result):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    session = _mock_session()
    session.postgres_conn = MagicMock()
    session.postgres_conn.acquire = MagicMock(return_value=acquire_ctx)
    return session, conn


class TestClaimsAnnouncedRow:
    @pytest.mark.asyncio
    async def test_claims_pending_row_without_inserting_again(self):
        """No unique constraint on (thread_id, tool_call_id): inserting again
        would orphan a card nobody waits on."""
        session, _ = _conn_with({"id": "rid-announced", "status": "pending"})
        insert = AsyncMock()
        waited = {}

        async def _wait(request_id, *a, **kw):
            waited["id"] = request_id
            return "approved"

        with (
            patch.object(pa, "_session", session),
            patch.object(pa, "_thread_id", "tid"),
            patch.object(pa, "_subscribers", {"c1": MagicMock()}),
            patch.object(pa, "_insert_permission_request", insert),
            patch.object(pa, "_wait_for_permission_resolution", _wait),
            patch.object(pa, "_broadcast", MagicMock()),
        ):
            outcome = await pa._loop_permission_check("web_search", {}, "tc_0")

        insert.assert_not_awaited()
        assert waited["id"] == "rid-announced"
        assert outcome is PermissionOutcome.APPROVED

    @pytest.mark.asyncio
    async def test_claimed_row_does_not_rebroadcast_request(self):
        """The batch frame already told the client — a second
        permission.request would duplicate the entry."""
        session, _ = _conn_with({"id": "rid-announced", "status": "pending"})
        bcast = MagicMock()

        with (
            patch.object(pa, "_session", session),
            patch.object(pa, "_thread_id", "tid"),
            patch.object(pa, "_subscribers", {"c1": MagicMock()}),
            patch.object(pa, "_insert_permission_request", AsyncMock()),
            patch.object(
                pa, "_wait_for_permission_resolution", AsyncMock(return_value="approved")
            ),
            patch.object(pa, "_broadcast", bcast),
        ):
            await pa._loop_permission_check("web_search", {}, "tc_0")

        events = [c.args[0] for c in bcast.call_args_list]
        assert "permission.request" not in events

    @pytest.mark.asyncio
    async def test_no_announced_row_still_inserts_and_broadcasts(self):
        """Single-gate turns keep today's behaviour exactly."""
        session, _ = _conn_with(None)
        insert = AsyncMock(return_value="rid-fresh")
        bcast = MagicMock()

        with (
            patch.object(pa, "_session", session),
            patch.object(pa, "_thread_id", "tid"),
            patch.object(pa, "_subscribers", {"c1": MagicMock()}),
            patch.object(pa, "_insert_permission_request", insert),
            patch.object(
                pa, "_wait_for_permission_resolution", AsyncMock(return_value="approved")
            ),
            patch.object(pa, "_broadcast", bcast),
        ):
            outcome = await pa._loop_permission_check("web_search", {}, "tc_9")

        insert.assert_awaited_once()
        assert "permission.request" in [c.args[0] for c in bcast.call_args_list]
        assert outcome is PermissionOutcome.APPROVED


class TestSharedShellToolSet:
    """``_gate_needed`` and ``_loop_permission_check`` must consult the SAME
    shell-tool set. Two independently-maintained copies can silently drift:
    a tool added to only one leaves a gated call with no announced row — a
    permanently stuck approval card.

    Patches ``_SHELL_TOOLS`` itself (rather than asserting against its
    current real contents) so the test exercises the actual wiring — that
    both call sites read the one shared constant — instead of a coincidence
    that would hold even if _loop_permission_check still carried its own
    hardcoded copy with today's same three names.
    """

    @pytest.mark.asyncio
    async def test_permission_check_and_gate_needed_agree_for_every_shell_tool(self):
        fake_shell_tools = {"synthetic_shell_tool_for_drift_test"}
        with patch.object(pa, "_SHELL_TOOLS", fake_shell_tools):
            for tool_name in pa._SHELL_TOOLS:
                assert pa._gate_needed("auto_accept", tool_name) is True

                session = _mock_session("auto_accept")
                session.postgres_conn = None  # deterministic DECLINE if gated
                with patch.object(pa, "_session", session):
                    outcome = await pa._loop_permission_check(tool_name, {}, "tc")

                # A shell tool must never be auto-approved here — it should
                # fall through to the gate (and only DECLINE because
                # postgres_conn is None in this test, not because it was
                # waved through as a non-shell tool).
                assert outcome is not PermissionOutcome.APPROVED, (
                    f"{tool_name!r} was auto-approved though _gate_needed "
                    "says it requires a gate — the two paths disagree"
                )
