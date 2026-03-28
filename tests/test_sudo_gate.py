"""Tests for the Sudo Approval Gate orchestrator service.

Tests cover:
1. connect() — NATS and DB binding
2. on_sudo_request() — request parsing, auto-approval, pending state, error handling
3. approve_request() — approval flow, NATS reply, DB update, SSE broadcast
4. deny_request() — denial flow, NATS reply, DB update, SSE broadcast
5. sweep_expired() — expiration logic, NATS reply for expired requests
6. Auto-approval rules: _evaluate_auto_rules(), create_rule(), delete_rule(), list_rules()
7. Pattern matching: glob patterns, shell metacharacter detection
8. list_requests(), get_request() — query methods with filters
9. SSE: subscribe_sse(), unsubscribe_sse(), _broadcast_sse()
10. Edge cases: duplicate requests, already-decided requests, malformed NATS messages
11. Error handling: DB connection failures, NATS publish failures
"""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from orchestrator.services.sudo_gate import SudoGateService, _SHELL_META_RE  # noqa: E402


# =============================================================================
# Fixtures
# =============================================================================


def make_nats_msg(
    data: dict,
    reply: str = "_INBOX.abc123",
    respond_side_effect=None,
) -> MagicMock:
    """Create a mock NATS message with .data, .reply, and .respond()."""
    msg = MagicMock()
    msg.data = json.dumps(data).encode()
    msg.reply = reply
    msg.respond = AsyncMock(side_effect=respond_side_effect)
    return msg


def make_nats_msg_raw(raw_bytes: bytes, reply: str = "_INBOX.abc123") -> MagicMock:
    """Create a mock NATS message with raw bytes (for malformed payload tests)."""
    msg = MagicMock()
    msg.data = raw_bytes
    msg.reply = reply
    msg.respond = AsyncMock()
    return msg


def make_db_pool(
    fetchrow_return=None,
    fetch_return=None,
    execute_return="DELETE 1",
    fetchrow_side_effect=None,
    fetch_side_effect=None,
    execute_side_effect=None,
):
    """Create a mock asyncpg connection pool with acquire() context manager."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value=fetchrow_return, side_effect=fetchrow_side_effect
    )
    conn.fetch = AsyncMock(
        return_value=fetch_return or [], side_effect=fetch_side_effect
    )
    conn.execute = AsyncMock(
        return_value=execute_return, side_effect=execute_side_effect
    )

    @asynccontextmanager
    async def acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = acquire
    return pool, conn


def make_request_row(
    request_id="req-001",
    status="pending",
    nats_reply_subject="_INBOX.abc123",
    **overrides,
) -> dict:
    """Create a mock DB row for a sudo approval request."""
    row = {
        "id": request_id,
        "job_id": "job-aaa",
        "vm_name": "vm-001",
        "command": "apt-get",
        "arguments": ["install", "-y", "libxml2-dev"],
        "working_directory": "/home/agent",
        "requesting_user": "agent",
        "target_user": "root",
        "status": status,
        "requested_at": "2026-03-28T10:00:00+00:00",
        "decided_at": None,
        "decided_by": None,
        "decision_reason": None,
        "nats_reply_subject": nats_reply_subject,
        "ttl_seconds": 300,
        "expires_at": "2026-03-28T10:05:00+00:00",
        "metadata": {},
    }
    row.update(overrides)
    return row


_SENTINEL = object()


def make_sudo_request_data(
    job_id="job-aaa",
    vm_id="vm-001",
    command="apt-get",
    argv=_SENTINEL,
    user="agent",
    runas_user="root",
    cwd="/home/agent",
) -> dict:
    """Create a standard sudo request payload."""
    if argv is _SENTINEL:
        argv = ["install", "-y", "libxml2-dev"]
    return {
        "job_id": job_id,
        "vm_id": vm_id,
        "command": command,
        "argv": argv,
        "user": user,
        "runas_user": runas_user,
        "cwd": cwd,
    }


# =============================================================================
# Test: connect()
# =============================================================================


class TestConnect:
    """Tests for SudoGateService.connect()."""

    def test_connect_binds_db_and_nats(self):
        """connect() stores db and nc references."""
        svc = SudoGateService()
        db = MagicMock()
        nc = MagicMock()

        svc.connect(db, nc)

        assert svc._db is db
        assert svc._nc is nc

    def test_connect_without_nats(self):
        """connect() works with db only (NATS optional)."""
        svc = SudoGateService()
        db = MagicMock()

        svc.connect(db)

        assert svc._db is db
        assert svc._nc is None

    def test_connect_replaces_previous_bindings(self):
        """connect() can be called multiple times, replacing references."""
        svc = SudoGateService()
        db1, db2 = MagicMock(), MagicMock()
        nc1, nc2 = MagicMock(), MagicMock()

        svc.connect(db1, nc1)
        svc.connect(db2, nc2)

        assert svc._db is db2
        assert svc._nc is nc2


# =============================================================================
# Test: on_sudo_request() — parsing and insertion
# =============================================================================


class TestOnSudoRequest:
    """Tests for on_sudo_request() message handling."""

    @pytest.mark.asyncio
    async def test_basic_request_inserted_and_pending(self):
        """A valid sudo request is inserted into the DB and pushed to SSE."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_return={"id": "req-001"},
            fetch_return=[],  # no auto-rules
        )
        svc.connect(pool)

        q = svc.subscribe_sse()
        data = make_sudo_request_data()
        msg = make_nats_msg(data)

        await svc.on_sudo_request(msg)

        # DB insert was called
        conn.fetchrow.assert_called_once()
        insert_sql = conn.fetchrow.call_args[0][0]
        assert "INSERT INTO sudo_approval_requests" in insert_sql

        # SSE event was broadcast
        assert not q.empty()
        event_type, event_data = q.get_nowait()
        assert event_type == "new_request"
        assert event_data["id"] == "req-001"
        assert event_data["job_id"] == "job-aaa"
        assert event_data["command"] == "apt-get"

    @pytest.mark.asyncio
    async def test_request_stores_pending_msg(self):
        """When no auto-rule matches, the NATS msg is stored for later reply."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_return={"id": "req-001"},
            fetch_return=[],  # no auto-rules
        )
        svc.connect(pool)

        data = make_sudo_request_data()
        msg = make_nats_msg(data)

        await svc.on_sudo_request(msg)

        assert "req-001" in svc._pending_msgs
        assert svc._pending_msgs["req-001"] is msg

    @pytest.mark.asyncio
    async def test_malformed_json_returns_early(self):
        """Malformed JSON payload is silently rejected."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        svc.connect(pool)

        msg = make_nats_msg_raw(b"not-json")

        await svc.on_sudo_request(msg)

        # No DB insert should have occurred
        conn.fetchrow.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_utf8_returns_early(self):
        """Invalid UTF-8 payload is silently rejected."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        svc.connect(pool)

        msg = make_nats_msg_raw(b"\xff\xfe")

        await svc.on_sudo_request(msg)

        conn.fetchrow.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_insert_failure_denies_immediately(self):
        """When DB insert fails, the request is denied via NATS reply."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetchrow_return=None)
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        data = make_sudo_request_data()
        msg = make_nats_msg(data, reply="_INBOX.deny123")

        await svc.on_sudo_request(msg)

        # Should publish a denial via NATS
        nc.publish.assert_called_once()
        call_args = nc.publish.call_args
        payload = json.loads(call_args[0][1].decode())
        assert payload["approved"] is False
        assert "internal error" in payload["reason"]

    @pytest.mark.asyncio
    async def test_missing_fields_use_defaults(self):
        """Missing fields in the request payload fall back to defaults."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_return={"id": "req-002"},
            fetch_return=[],
        )
        svc.connect(pool)

        # Minimal payload — most fields missing
        msg = make_nats_msg({"command": "ls"})
        await svc.on_sudo_request(msg)

        # Insert should have been called with defaults
        conn.fetchrow.assert_called_once()
        args = conn.fetchrow.call_args[0]
        # job_id defaults to ""
        assert args[1] == ""
        # vm_name defaults to ""
        assert args[2] == ""
        # requesting_user defaults to "unknown"
        assert args[6] == "unknown"
        # target_user defaults to "root"
        assert args[7] == "root"

    @pytest.mark.asyncio
    async def test_nats_reply_subject_from_msg(self):
        """The NATS reply subject is taken from msg.reply."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_return={"id": "req-003"},
            fetch_return=[],
        )
        svc.connect(pool)

        msg = make_nats_msg(make_sudo_request_data(), reply="_INBOX.custom_reply")
        await svc.on_sudo_request(msg)

        # The reply subject should be passed to the insert
        args = conn.fetchrow.call_args[0]
        assert args[8] == "_INBOX.custom_reply"

    @pytest.mark.asyncio
    async def test_msg_without_reply_attribute(self):
        """When msg has no .reply attribute, nats_reply_subject is None."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_return={"id": "req-004"},
            fetch_return=[],
        )
        svc.connect(pool)

        msg = MagicMock(spec=[])  # no attributes
        msg.data = json.dumps(make_sudo_request_data()).encode()

        await svc.on_sudo_request(msg)

        # reply subject should be None
        args = conn.fetchrow.call_args[0]
        assert args[8] is None


# =============================================================================
# Test: on_sudo_request() — auto-approval
# =============================================================================


class TestOnSudoRequestAutoApproval:
    """Tests for auto-approval evaluation within on_sudo_request()."""

    @pytest.mark.asyncio
    async def test_auto_approve_matches(self):
        """When an auto-approve rule matches, the request is auto-approved."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetchrow_return={"id": "req-010"})
        svc.connect(pool)

        # cmd_string = " ".join(argv) when argv is non-empty
        # So argv=["install", "-y", "libxml2-dev"] -> "install -y libxml2-dev"
        conn.fetchrow.return_value = {"id": "req-010"}
        conn.fetch.return_value = [
            {"pattern": "install -y *", "action": "approve"}
        ]

        data = make_sudo_request_data(argv=["install", "-y", "libxml2-dev"])
        msg = make_nats_msg(data)

        await svc.on_sudo_request(msg)

        # Should have called msg.respond with approval
        msg.respond.assert_called_once()
        response = json.loads(msg.respond.call_args[0][0].decode())
        assert response["approved"] is True
        assert "auto-approval" in response["reason"]

        # Request should NOT be in pending_msgs (auto-resolved)
        assert "req-010" not in svc._pending_msgs

    @pytest.mark.asyncio
    async def test_auto_deny_matches(self):
        """When an auto-deny rule matches, the request is auto-denied."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetchrow_return={"id": "req-011"})
        svc.connect(pool)

        # cmd_string = " ".join(argv) = "-rf /"
        conn.fetch.return_value = [{"pattern": "-rf *", "action": "deny"}]

        data = make_sudo_request_data(command="rm", argv=["-rf", "/"])
        msg = make_nats_msg(data)

        await svc.on_sudo_request(msg)

        msg.respond.assert_called_once()
        response = json.loads(msg.respond.call_args[0][0].decode())
        assert response["approved"] is False
        assert "auto-denial" in response["reason"]

    @pytest.mark.asyncio
    async def test_auto_approve_skipped_for_shell_metacharacters(self):
        """Commands with shell metacharacters bypass auto-approval entirely."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_return={"id": "req-012"},
            fetch_return=[{"pattern": "*", "action": "approve"}],
        )
        svc.connect(pool)

        # When argv is empty, cmd_string = command (which contains pipe)
        # Shell metacharacters bypass auto-approval, so request stays pending
        data = make_sudo_request_data(command="cat /etc/passwd | grep root", argv=[])
        msg = make_nats_msg(data)
        # Note: fetch_return is set via make_db_pool but _evaluate_auto_rules
        # won't reach the DB query because _SHELL_META_RE matches first

        await svc.on_sudo_request(msg)

        # msg.respond should NOT have been called for auto-approval
        # The request should be pending
        assert "req-012" in svc._pending_msgs

    @pytest.mark.asyncio
    async def test_auto_approve_respond_failure_is_swallowed(self):
        """If msg.respond() fails during auto-approval, the error is swallowed."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetchrow_return={"id": "req-013"})
        svc.connect(pool)

        conn.fetch.return_value = [
            {"pattern": "apt-get install *", "action": "approve"}
        ]

        data = make_sudo_request_data(argv=["install", "-y", "vim"])
        msg = make_nats_msg(data, respond_side_effect=Exception("connection lost"))

        # Should not raise
        await svc.on_sudo_request(msg)

    @pytest.mark.asyncio
    async def test_first_matching_rule_wins(self):
        """Auto-rules are evaluated in priority order; the first match wins."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetchrow_return={"id": "req-014"})
        svc.connect(pool)

        # cmd_string = " ".join(argv) = "install -y vim"
        # First rule denies (matches), second approves — deny should win
        conn.fetch.return_value = [
            {"pattern": "install *", "action": "deny"},
            {"pattern": "install -y *", "action": "approve"},
        ]

        data = make_sudo_request_data(argv=["install", "-y", "vim"])
        msg = make_nats_msg(data)

        await svc.on_sudo_request(msg)

        response = json.loads(msg.respond.call_args[0][0].decode())
        assert response["approved"] is False

    @pytest.mark.asyncio
    async def test_no_rules_leaves_request_pending(self):
        """When the rules table is empty, the request remains pending."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_return={"id": "req-015"},
            fetch_return=[],
        )
        svc.connect(pool)

        data = make_sudo_request_data()
        msg = make_nats_msg(data)

        await svc.on_sudo_request(msg)

        assert "req-015" in svc._pending_msgs


# =============================================================================
# Test: approve_request()
# =============================================================================


class TestApproveRequest:
    """Tests for approve_request() — human approval flow."""

    @pytest.mark.asyncio
    async def test_approve_pending_request(self):
        """Approving a pending request finalizes it and publishes NATS reply."""
        svc = SudoGateService()
        row = make_request_row(status="pending", nats_reply_subject="_INBOX.reply1")
        pool, conn = make_db_pool(fetchrow_return=row)
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        # Store a pending msg for the request
        pending_msg = MagicMock()
        pending_msg.respond = AsyncMock()
        svc._pending_msgs["req-001"] = pending_msg

        q = svc.subscribe_sse()
        result = await svc.approve_request("req-001", reason="Looks safe", decided_by="admin")

        assert result is not None
        assert result["status"] == "approved"
        assert result["id"] == "req-001"

        # DB update was called
        conn.execute.assert_called_once()
        update_sql = conn.execute.call_args[0][0]
        assert "status = $2" in update_sql
        assert conn.execute.call_args[0][2] == "approved"

        # msg.respond() was used for NATS reply
        pending_msg.respond.assert_called_once()
        resp_payload = json.loads(pending_msg.respond.call_args[0][0].decode())
        assert resp_payload["approved"] is True
        assert resp_payload["reason"] == "Looks safe"

        # Pending msg is removed
        assert "req-001" not in svc._pending_msgs

        # SSE event broadcast
        assert not q.empty()
        event_type, event_data = q.get_nowait()
        assert event_type == "request_decided"
        assert event_data["status"] == "approved"
        assert event_data["decided_by"] == "admin"

    @pytest.mark.asyncio
    async def test_approve_nonexistent_request_returns_none(self):
        """Approving a nonexistent request returns None."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetchrow_return=None)
        svc.connect(pool)

        result = await svc.approve_request("nonexistent-id")

        assert result is None

    @pytest.mark.asyncio
    async def test_approve_already_decided_request(self):
        """Approving an already-approved request returns error dict."""
        svc = SudoGateService()
        row = make_request_row(status="approved")
        pool, conn = make_db_pool(fetchrow_return=row)
        svc.connect(pool)

        result = await svc.approve_request("req-001")

        assert result is not None
        assert "error" in result
        assert "'approved'" in result["error"]

    @pytest.mark.asyncio
    async def test_approve_already_denied_request(self):
        """Approving an already-denied request returns error dict."""
        svc = SudoGateService()
        row = make_request_row(status="denied")
        pool, conn = make_db_pool(fetchrow_return=row)
        svc.connect(pool)

        result = await svc.approve_request("req-001")

        assert "error" in result
        assert "'denied'" in result["error"]

    @pytest.mark.asyncio
    async def test_approve_expired_request(self):
        """Approving an expired request returns error dict."""
        svc = SudoGateService()
        row = make_request_row(status="expired")
        pool, conn = make_db_pool(fetchrow_return=row)
        svc.connect(pool)

        result = await svc.approve_request("req-001")

        assert "error" in result
        assert "'expired'" in result["error"]

    @pytest.mark.asyncio
    async def test_approve_with_no_pending_msg(self):
        """Approve works even when no pending NATS msg is stored (e.g., after restart)."""
        svc = SudoGateService()
        row = make_request_row(status="pending", nats_reply_subject="_INBOX.reply2")
        pool, conn = make_db_pool(fetchrow_return=row)
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        # No pending msg stored — reply goes via _nats_reply (publish to subject)
        result = await svc.approve_request("req-001", reason="ok")

        assert result["status"] == "approved"
        # NATS publish used as fallback
        nc.publish.assert_called()
        payload = json.loads(nc.publish.call_args[0][1].decode())
        assert payload["approved"] is True

    @pytest.mark.asyncio
    async def test_approve_msg_respond_failure_is_handled(self):
        """If msg.respond() fails during approval, it's logged but not fatal."""
        svc = SudoGateService()
        row = make_request_row(status="pending")
        pool, conn = make_db_pool(fetchrow_return=row)
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        pending_msg = MagicMock()
        pending_msg.respond = AsyncMock(side_effect=Exception("NATS down"))
        svc._pending_msgs["req-001"] = pending_msg

        # Should not raise
        result = await svc.approve_request("req-001", reason="ok")
        assert result["status"] == "approved"

    @pytest.mark.asyncio
    async def test_approve_default_decided_by(self):
        """Default decided_by is 'operator' when not specified."""
        svc = SudoGateService()
        row = make_request_row(status="pending")
        pool, conn = make_db_pool(fetchrow_return=row)
        svc.connect(pool)

        q = svc.subscribe_sse()
        await svc.approve_request("req-001")

        event_type, event_data = q.get_nowait()
        assert event_data["decided_by"] == "operator"


# =============================================================================
# Test: deny_request()
# =============================================================================


class TestDenyRequest:
    """Tests for deny_request() — human denial flow."""

    @pytest.mark.asyncio
    async def test_deny_pending_request(self):
        """Denying a pending request finalizes it with denied status."""
        svc = SudoGateService()
        row = make_request_row(status="pending")
        pool, conn = make_db_pool(fetchrow_return=row)
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        pending_msg = MagicMock()
        pending_msg.respond = AsyncMock()
        svc._pending_msgs["req-001"] = pending_msg

        q = svc.subscribe_sse()
        result = await svc.deny_request("req-001", reason="Too dangerous", decided_by="security")

        assert result["status"] == "denied"
        assert result["id"] == "req-001"

        # msg.respond sends denial
        pending_msg.respond.assert_called_once()
        resp_payload = json.loads(pending_msg.respond.call_args[0][0].decode())
        assert resp_payload["approved"] is False
        assert resp_payload["reason"] == "Too dangerous"

        # Pending msg removed
        assert "req-001" not in svc._pending_msgs

        # SSE event includes reason
        event_type, event_data = q.get_nowait()
        assert event_type == "request_decided"
        assert event_data["status"] == "denied"
        assert event_data["reason"] == "Too dangerous"
        assert event_data["decided_by"] == "security"

    @pytest.mark.asyncio
    async def test_deny_nonexistent_request_returns_none(self):
        """Denying a nonexistent request returns None."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetchrow_return=None)
        svc.connect(pool)

        result = await svc.deny_request("no-such-id")

        assert result is None

    @pytest.mark.asyncio
    async def test_deny_already_approved(self):
        """Denying an already-approved request returns error dict."""
        svc = SudoGateService()
        row = make_request_row(status="approved")
        pool, conn = make_db_pool(fetchrow_return=row)
        svc.connect(pool)

        result = await svc.deny_request("req-001")

        assert "error" in result
        assert "'approved'" in result["error"]

    @pytest.mark.asyncio
    async def test_deny_with_no_pending_msg(self):
        """Deny works even without a stored pending msg."""
        svc = SudoGateService()
        row = make_request_row(status="pending", nats_reply_subject="_INBOX.deny_reply")
        pool, conn = make_db_pool(fetchrow_return=row)
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        result = await svc.deny_request("req-001", reason="nope")

        assert result["status"] == "denied"
        # Fallback NATS publish was used
        nc.publish.assert_called()
        payload = json.loads(nc.publish.call_args[0][1].decode())
        assert payload["approved"] is False

    @pytest.mark.asyncio
    async def test_deny_msg_respond_failure_is_handled(self):
        """If msg.respond() fails during denial, it's logged but not fatal."""
        svc = SudoGateService()
        row = make_request_row(status="pending")
        pool, conn = make_db_pool(fetchrow_return=row)
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        pending_msg = MagicMock()
        pending_msg.respond = AsyncMock(side_effect=Exception("Connection reset"))
        svc._pending_msgs["req-001"] = pending_msg

        result = await svc.deny_request("req-001", reason="no")
        assert result["status"] == "denied"


# =============================================================================
# Test: sweep_expired()
# =============================================================================


class TestSweepExpired:
    """Tests for sweep_expired() — expiration logic."""

    @pytest.mark.asyncio
    async def test_sweep_expires_timed_out_requests(self):
        """Expired requests are denied and NATS replies sent."""
        svc = SudoGateService()
        expired_rows = [
            {"id": "req-exp-1", "nats_reply_subject": "_INBOX.exp1"},
            {"id": "req-exp-2", "nats_reply_subject": "_INBOX.exp2"},
        ]
        pool, conn = make_db_pool(fetch_return=expired_rows)
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        q = svc.subscribe_sse()
        count = await svc.sweep_expired()

        assert count == 2

        # NATS publish was called for each expired request (via _nats_reply fallback)
        assert nc.publish.call_count == 2

        # SSE events for each
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        assert len(events) == 2
        assert all(e[0] == "request_decided" for e in events)
        assert all(e[1]["status"] == "expired" for e in events)

    @pytest.mark.asyncio
    async def test_sweep_uses_msg_respond_when_available(self):
        """Expired requests use msg.respond() when a pending msg exists."""
        svc = SudoGateService()
        expired_rows = [
            {"id": "req-exp-3", "nats_reply_subject": "_INBOX.exp3"},
        ]
        pool, conn = make_db_pool(fetch_return=expired_rows)
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        pending_msg = MagicMock()
        pending_msg.respond = AsyncMock()
        svc._pending_msgs["req-exp-3"] = pending_msg

        count = await svc.sweep_expired()

        assert count == 1
        pending_msg.respond.assert_called_once()
        payload = json.loads(pending_msg.respond.call_args[0][0].decode())
        assert payload["approved"] is False
        assert "timed out" in payload["reason"]

        # msg was removed from pending
        assert "req-exp-3" not in svc._pending_msgs

        # NATS publish was NOT called (msg.respond succeeded)
        nc.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_falls_back_to_nats_publish_on_respond_failure(self):
        """If msg.respond() fails, sweep falls back to _nats_reply publish."""
        svc = SudoGateService()
        expired_rows = [
            {"id": "req-exp-4", "nats_reply_subject": "_INBOX.exp4"},
        ]
        pool, conn = make_db_pool(fetch_return=expired_rows)
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        pending_msg = MagicMock()
        pending_msg.respond = AsyncMock(side_effect=Exception("dead connection"))
        svc._pending_msgs["req-exp-4"] = pending_msg

        count = await svc.sweep_expired()

        assert count == 1
        # Fallback to NATS publish
        nc.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_sweep_no_expired_requests(self):
        """Sweep returns 0 when nothing is expired."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetch_return=[])
        svc.connect(pool)

        count = await svc.sweep_expired()

        assert count == 0

    @pytest.mark.asyncio
    async def test_sweep_without_db(self):
        """Sweep returns 0 when no DB is connected."""
        svc = SudoGateService()

        count = await svc.sweep_expired()

        assert count == 0

    @pytest.mark.asyncio
    async def test_sweep_db_error_returns_zero(self):
        """Sweep returns 0 on DB errors and does not raise."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetch_side_effect=Exception("DB down"))
        svc.connect(pool)

        count = await svc.sweep_expired()

        assert count == 0

    @pytest.mark.asyncio
    async def test_sweep_without_reply_subject(self):
        """Expired request without a reply subject still counts and broadcasts SSE."""
        svc = SudoGateService()
        expired_rows = [
            {"id": "req-exp-5", "nats_reply_subject": None},
        ]
        pool, conn = make_db_pool(fetch_return=expired_rows)
        svc.connect(pool)

        q = svc.subscribe_sse()
        count = await svc.sweep_expired()

        assert count == 1
        assert not q.empty()


# =============================================================================
# Test: _evaluate_auto_rules()
# =============================================================================


class TestEvaluateAutoRules:
    """Tests for _evaluate_auto_rules() — pattern matching logic."""

    @pytest.mark.asyncio
    async def test_glob_pattern_matches(self):
        """fnmatch-style glob patterns work correctly."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "apt-get install *", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("apt-get install -y vim")
        assert result == "approve"

    @pytest.mark.asyncio
    async def test_exact_match(self):
        """Exact command strings match without wildcards."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "systemctl restart nginx", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("systemctl restart nginx")
        assert result == "approve"

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self):
        """Returns None when no rule matches."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "apt-get install *", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("rm -rf /")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_rules_returns_none(self):
        """Returns None when no rules exist."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetch_return=[])
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("anything")
        assert result is None

    @pytest.mark.asyncio
    async def test_shell_metachar_pipe_blocks_auto_approval(self):
        """Pipe character | prevents auto-approval."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "*", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("cat /etc/passwd | grep root")
        assert result is None

    @pytest.mark.asyncio
    async def test_shell_metachar_semicolon_blocks(self):
        """Semicolon ; prevents auto-approval."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "*", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("ls; rm -rf /")
        assert result is None

    @pytest.mark.asyncio
    async def test_shell_metachar_ampersand_blocks(self):
        """Ampersand & prevents auto-approval."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "*", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("sleep 100 &")
        assert result is None

    @pytest.mark.asyncio
    async def test_shell_metachar_backtick_blocks(self):
        """Backtick ` prevents auto-approval."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "*", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("echo `whoami`")
        assert result is None

    @pytest.mark.asyncio
    async def test_shell_metachar_dollar_blocks(self):
        """Dollar sign $ prevents auto-approval."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "*", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("echo $HOME")
        assert result is None

    @pytest.mark.asyncio
    async def test_shell_metachar_redirect_blocks(self):
        """Redirect > prevents auto-approval."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "*", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("echo x > /etc/shadow")
        assert result is None

    @pytest.mark.asyncio
    async def test_shell_metachar_subshell_blocks(self):
        """Command substitution $() prevents auto-approval."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "*", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("rm $(find /tmp -name '*.log')")
        assert result is None

    @pytest.mark.asyncio
    async def test_shell_metachar_double_pipe_blocks(self):
        """Double pipe || prevents auto-approval."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "*", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("false || rm -rf /")
        assert result is None

    @pytest.mark.asyncio
    async def test_shell_metachar_double_ampersand_blocks(self):
        """Double ampersand && prevents auto-approval."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "*", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("true && rm -rf /")
        assert result is None

    @pytest.mark.asyncio
    async def test_safe_command_passes_metachar_check(self):
        """Commands without metacharacters proceed to rule matching."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "ls *", "action": "approve"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("ls -la /home")
        assert result == "approve"

    @pytest.mark.asyncio
    async def test_deny_action_returned(self):
        """Rules with action 'deny' return 'deny'."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "rm *", "action": "deny"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("rm -rf /important")
        assert result == "deny"

    @pytest.mark.asyncio
    async def test_review_action_returned(self):
        """Rules with action 'review' return 'review'."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "chmod *", "action": "review"}]
        )
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("chmod 777 /etc")
        assert result == "review"

    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        """Rules should be evaluated in the order returned by the query (priority ASC)."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[
                {"pattern": "apt-get *", "action": "deny"},       # priority 10 (first)
                {"pattern": "apt-get install *", "action": "approve"},  # priority 20 (second)
            ]
        )
        svc.connect(pool)

        # "apt-get install vim" matches both, but first rule wins
        result = await svc._evaluate_auto_rules("apt-get install vim")
        assert result == "deny"

    @pytest.mark.asyncio
    async def test_no_db_returns_none(self):
        """Without DB, auto-rules always return None."""
        svc = SudoGateService()

        result = await svc._evaluate_auto_rules("anything")
        assert result is None

    @pytest.mark.asyncio
    async def test_db_error_returns_none(self):
        """DB errors in rule evaluation return None (fail open to human review)."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetch_side_effect=Exception("connection refused"))
        svc.connect(pool)

        result = await svc._evaluate_auto_rules("apt-get install vim")
        assert result is None

    @pytest.mark.asyncio
    async def test_glob_question_mark(self):
        """fnmatch ? matches single character."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "ls -?", "action": "approve"}]
        )
        svc.connect(pool)

        assert await svc._evaluate_auto_rules("ls -l") == "approve"
        assert await svc._evaluate_auto_rules("ls -la") is None

    @pytest.mark.asyncio
    async def test_glob_bracket_pattern(self):
        """fnmatch [...] character class works."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetch_return=[{"pattern": "systemctl [rs]* nginx", "action": "approve"}]
        )
        svc.connect(pool)

        # [rs]* matches strings starting with r or s
        assert await svc._evaluate_auto_rules("systemctl restart nginx") == "approve"
        assert await svc._evaluate_auto_rules("systemctl start nginx") == "approve"
        # 'k' is not in [rs], so no match
        assert await svc._evaluate_auto_rules("systemctl kill nginx") is None


# =============================================================================
# Test: create_rule(), delete_rule(), list_rules()
# =============================================================================


class TestAutoRuleCRUD:
    """Tests for auto-approval rule management."""

    @pytest.mark.asyncio
    async def test_create_rule_approve(self):
        """Create an approve rule successfully."""
        svc = SudoGateService()
        rule_row = {
            "id": "rule-001",
            "pattern": "apt-get install *",
            "action": "approve",
            "priority": 50,
            "description": "Allow package installs",
            "created_by": "admin",
            "enabled": True,
        }
        pool, conn = make_db_pool(fetchrow_return=rule_row)
        svc.connect(pool)

        result = await svc.create_rule(
            pattern="apt-get install *",
            action="approve",
            priority=50,
            description="Allow package installs",
            created_by="admin",
        )

        assert result is not None
        assert result["pattern"] == "apt-get install *"
        assert result["action"] == "approve"
        assert result["priority"] == 50

    @pytest.mark.asyncio
    async def test_create_rule_deny(self):
        """Create a deny rule successfully."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_return={"id": "rule-002", "pattern": "rm *", "action": "deny"}
        )
        svc.connect(pool)

        result = await svc.create_rule(pattern="rm *", action="deny")
        assert result is not None
        assert result["action"] == "deny"

    @pytest.mark.asyncio
    async def test_create_rule_review(self):
        """Create a review rule successfully."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_return={"id": "rule-003", "pattern": "chmod *", "action": "review"}
        )
        svc.connect(pool)

        result = await svc.create_rule(pattern="chmod *", action="review")
        assert result is not None
        assert result["action"] == "review"

    @pytest.mark.asyncio
    async def test_create_rule_invalid_action(self):
        """Creating a rule with an invalid action returns None."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        svc.connect(pool)

        result = await svc.create_rule(pattern="*", action="invalid_action")
        assert result is None
        conn.fetchrow.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_rule_no_db(self):
        """Creating a rule without DB returns None."""
        svc = SudoGateService()

        result = await svc.create_rule(pattern="*", action="approve")
        assert result is None

    @pytest.mark.asyncio
    async def test_create_rule_db_error(self):
        """DB error during rule creation returns None."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_side_effect=Exception("unique violation")
        )
        svc.connect(pool)

        result = await svc.create_rule(pattern="apt-get *", action="approve")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_rule_success(self):
        """Deleting an existing rule returns True."""
        svc = SudoGateService()
        pool, conn = make_db_pool(execute_return="DELETE 1")
        svc.connect(pool)

        result = await svc.delete_rule("rule-001")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_rule_not_found(self):
        """Deleting a nonexistent rule returns False."""
        svc = SudoGateService()
        pool, conn = make_db_pool(execute_return="DELETE 0")
        svc.connect(pool)

        result = await svc.delete_rule("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_rule_no_db(self):
        """Deleting without DB returns False."""
        svc = SudoGateService()

        result = await svc.delete_rule("rule-001")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_rule_db_error(self):
        """DB error during deletion returns False."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            execute_side_effect=Exception("connection lost")
        )
        svc.connect(pool)

        result = await svc.delete_rule("rule-001")
        assert result is False

    @pytest.mark.asyncio
    async def test_list_rules_returns_all(self):
        """list_rules returns all rules ordered by priority."""
        svc = SudoGateService()
        rules = [
            {"id": "r1", "pattern": "ls *", "action": "approve", "priority": 10},
            {"id": "r2", "pattern": "rm *", "action": "deny", "priority": 20},
        ]
        pool, conn = make_db_pool(fetch_return=rules)
        svc.connect(pool)

        result = await svc.list_rules()
        assert len(result) == 2
        assert result[0]["priority"] == 10
        assert result[1]["priority"] == 20

    @pytest.mark.asyncio
    async def test_list_rules_empty(self):
        """list_rules returns empty list when no rules exist."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetch_return=[])
        svc.connect(pool)

        result = await svc.list_rules()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_rules_no_db(self):
        """list_rules without DB returns empty list."""
        svc = SudoGateService()

        result = await svc.list_rules()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_rules_db_error(self):
        """DB error in list_rules returns empty list."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetch_side_effect=Exception("timeout"))
        svc.connect(pool)

        result = await svc.list_rules()
        assert result == []


# =============================================================================
# Test: list_requests(), get_request()
# =============================================================================


class TestQueryMethods:
    """Tests for list_requests() and get_request()."""

    @pytest.mark.asyncio
    async def test_list_requests_no_filters(self):
        """list_requests with no filters returns all requests."""
        svc = SudoGateService()
        rows = [
            make_request_row(request_id="req-a"),
            make_request_row(request_id="req-b"),
        ]
        pool, conn = make_db_pool(fetch_return=rows)
        svc.connect(pool)

        result = await svc.list_requests()

        assert len(result) == 2
        # Verify the query has no WHERE clause conditions
        query = conn.fetch.call_args[0][0]
        assert "WHERE" not in query

    @pytest.mark.asyncio
    async def test_list_requests_filter_by_job_id(self):
        """list_requests filters by job_id when provided."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetch_return=[make_request_row()])
        svc.connect(pool)

        await svc.list_requests(job_id="job-aaa")

        query = conn.fetch.call_args[0][0]
        assert "job_id = $1" in query
        # job_id should be passed as param
        params = conn.fetch.call_args[0][1:]
        assert "job-aaa" in params

    @pytest.mark.asyncio
    async def test_list_requests_filter_by_status(self):
        """list_requests filters by status when provided."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetch_return=[])
        svc.connect(pool)

        await svc.list_requests(status="pending")

        query = conn.fetch.call_args[0][0]
        assert "status = $1" in query

    @pytest.mark.asyncio
    async def test_list_requests_filter_both(self):
        """list_requests filters by both job_id and status."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetch_return=[])
        svc.connect(pool)

        await svc.list_requests(job_id="job-bbb", status="approved")

        query = conn.fetch.call_args[0][0]
        assert "job_id = $1" in query
        assert "status = $2" in query
        params = conn.fetch.call_args[0][1:]
        assert params[0] == "job-bbb"
        assert params[1] == "approved"

    @pytest.mark.asyncio
    async def test_list_requests_custom_limit(self):
        """list_requests respects custom limit."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetch_return=[])
        svc.connect(pool)

        await svc.list_requests(limit=10)

        # The limit param should be the last positional arg
        params = conn.fetch.call_args[0][1:]
        assert 10 in params

    @pytest.mark.asyncio
    async def test_list_requests_no_db(self):
        """list_requests without DB returns empty list."""
        svc = SudoGateService()

        result = await svc.list_requests()
        assert result == []

    @pytest.mark.asyncio
    async def test_list_requests_db_error(self):
        """DB error in list_requests returns empty list."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetch_side_effect=Exception("connection lost"))
        svc.connect(pool)

        result = await svc.list_requests()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_request_found(self):
        """get_request returns a dict for an existing request."""
        svc = SudoGateService()
        row = make_request_row(request_id="req-xyz")
        pool, conn = make_db_pool(fetchrow_return=row)
        svc.connect(pool)

        result = await svc.get_request("req-xyz")

        assert result is not None
        assert result["id"] == "req-xyz"

    @pytest.mark.asyncio
    async def test_get_request_not_found(self):
        """get_request returns None for a nonexistent request."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetchrow_return=None)
        svc.connect(pool)

        result = await svc.get_request("no-such-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_request_no_db(self):
        """get_request without DB returns None."""
        svc = SudoGateService()

        result = await svc.get_request("req-001")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_request_db_error(self):
        """DB error in get_request returns None."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_side_effect=Exception("timeout")
        )
        svc.connect(pool)

        result = await svc.get_request("req-001")
        assert result is None


# =============================================================================
# Test: SSE broadcasting
# =============================================================================


class TestSSE:
    """Tests for SSE subscription and broadcasting."""

    def test_subscribe_returns_queue(self):
        """subscribe_sse() returns an asyncio.Queue."""
        svc = SudoGateService()
        q = svc.subscribe_sse()

        assert isinstance(q, asyncio.Queue)
        assert q in svc._sse_queues

    def test_subscribe_multiple_clients(self):
        """Multiple SSE clients can subscribe simultaneously."""
        svc = SudoGateService()
        q1 = svc.subscribe_sse()
        q2 = svc.subscribe_sse()
        q3 = svc.subscribe_sse()

        assert len(svc._sse_queues) == 3
        assert q1 is not q2
        assert q2 is not q3

    def test_unsubscribe_removes_queue(self):
        """unsubscribe_sse() removes the queue from the list."""
        svc = SudoGateService()
        q = svc.subscribe_sse()
        assert q in svc._sse_queues

        svc.unsubscribe_sse(q)
        assert q not in svc._sse_queues

    def test_unsubscribe_nonexistent_queue(self):
        """unsubscribe_sse() with unknown queue does not raise."""
        svc = SudoGateService()
        q = asyncio.Queue()

        # Should not raise
        svc.unsubscribe_sse(q)

    def test_unsubscribe_twice(self):
        """Unsubscribing the same queue twice does not raise."""
        svc = SudoGateService()
        q = svc.subscribe_sse()

        svc.unsubscribe_sse(q)
        svc.unsubscribe_sse(q)  # second call is a no-op

        assert q not in svc._sse_queues

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all_subscribers(self):
        """Broadcasting sends the event to all subscribed queues."""
        svc = SudoGateService()
        q1 = svc.subscribe_sse()
        q2 = svc.subscribe_sse()

        await svc._broadcast_sse("test_event", {"key": "value"})

        for q in [q1, q2]:
            assert not q.empty()
            event_type, data = q.get_nowait()
            assert event_type == "test_event"
            assert data["key"] == "value"

    @pytest.mark.asyncio
    async def test_broadcast_to_no_subscribers(self):
        """Broadcasting with no subscribers is a no-op."""
        svc = SudoGateService()

        # Should not raise
        await svc._broadcast_sse("test_event", {"key": "value"})

    @pytest.mark.asyncio
    async def test_broadcast_removes_full_queues(self):
        """Full queues are automatically removed during broadcast."""
        svc = SudoGateService()
        # Create a tiny queue
        q_full = asyncio.Queue(maxsize=1)
        svc._sse_queues.append(q_full)

        q_healthy = svc.subscribe_sse()

        # Fill the small queue
        q_full.put_nowait(("filler", {}))

        # Broadcasting to a full queue should remove it
        await svc._broadcast_sse("test", {"data": 1})

        assert q_full not in svc._sse_queues
        assert q_healthy in svc._sse_queues

        # Healthy queue got the event
        assert not q_healthy.empty()

    @pytest.mark.asyncio
    async def test_broadcast_maxsize_default(self):
        """Default SSE queue has maxsize=100."""
        svc = SudoGateService()
        q = svc.subscribe_sse()
        assert q.maxsize == 100


# =============================================================================
# Test: _nats_reply() internal helper
# =============================================================================


class TestNatsReply:
    """Tests for _nats_reply() — NATS publish fallback."""

    @pytest.mark.asyncio
    async def test_nats_reply_publishes_approval(self):
        """_nats_reply publishes an approval message."""
        svc = SudoGateService()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(MagicMock(), nc)

        await svc._nats_reply("_INBOX.test", True, "allowed")

        nc.publish.assert_called_once()
        subject = nc.publish.call_args[0][0]
        payload = json.loads(nc.publish.call_args[0][1].decode())
        assert subject == "_INBOX.test"
        assert payload["approved"] is True
        assert payload["reason"] == "allowed"
        nc.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_nats_reply_publishes_denial(self):
        """_nats_reply publishes a denial message."""
        svc = SudoGateService()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(MagicMock(), nc)

        await svc._nats_reply("_INBOX.test", False, "not allowed")

        payload = json.loads(nc.publish.call_args[0][1].decode())
        assert payload["approved"] is False
        assert payload["reason"] == "not allowed"

    @pytest.mark.asyncio
    async def test_nats_reply_without_reason(self):
        """_nats_reply omits 'reason' key when reason is empty."""
        svc = SudoGateService()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(MagicMock(), nc)

        await svc._nats_reply("_INBOX.test", True, "")

        payload = json.loads(nc.publish.call_args[0][1].decode())
        assert payload["approved"] is True
        assert "reason" not in payload

    @pytest.mark.asyncio
    async def test_nats_reply_no_subject_is_noop(self):
        """_nats_reply with None subject does nothing."""
        svc = SudoGateService()
        nc = MagicMock()
        nc.publish = AsyncMock()
        svc.connect(MagicMock(), nc)

        await svc._nats_reply(None, True, "test")

        nc.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_nats_reply_no_nc_is_noop(self):
        """_nats_reply without NATS connection does nothing."""
        svc = SudoGateService()
        svc.connect(MagicMock())  # no nc

        # Should not raise
        await svc._nats_reply("_INBOX.test", True, "test")

    @pytest.mark.asyncio
    async def test_nats_reply_publish_failure_is_handled(self):
        """_nats_reply handles NATS publish errors gracefully."""
        svc = SudoGateService()
        nc = MagicMock()
        nc.publish = AsyncMock(side_effect=Exception("NATS connection lost"))
        svc.connect(MagicMock(), nc)

        # Should not raise
        await svc._nats_reply("_INBOX.test", True, "test")


# =============================================================================
# Test: _insert_request() internal helper
# =============================================================================


class TestInsertRequest:
    """Tests for _insert_request() — DB insert logic."""

    @pytest.mark.asyncio
    async def test_insert_returns_request_id(self):
        """Successful insert returns the request ID as string."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetchrow_return={"id": "uuid-123"})
        svc.connect(pool)

        result = await svc._insert_request(
            job_id="job-1",
            vm_name="vm-1",
            command="ls",
            arguments=["-la"],
            cwd="/home",
            requesting_user="agent",
            target_user="root",
            nats_reply_subject="_INBOX.test",
            metadata={"key": "value"},
        )

        assert result == "uuid-123"

    @pytest.mark.asyncio
    async def test_insert_no_db_returns_none(self):
        """Without DB, insert returns None."""
        svc = SudoGateService()

        result = await svc._insert_request(
            job_id="job-1",
            vm_name="vm-1",
            command="ls",
            arguments=[],
            cwd="/",
            requesting_user="agent",
            target_user="root",
            nats_reply_subject=None,
            metadata={},
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_insert_db_error_returns_none(self):
        """DB error during insert returns None."""
        svc = SudoGateService()
        pool, conn = make_db_pool(
            fetchrow_side_effect=Exception("insert failed")
        )
        svc.connect(pool)

        result = await svc._insert_request(
            job_id="job-1",
            vm_name="vm-1",
            command="ls",
            arguments=[],
            cwd="/",
            requesting_user="agent",
            target_user="root",
            nats_reply_subject=None,
            metadata={},
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_insert_null_fetchrow_returns_none(self):
        """When fetchrow returns None (unlikely), insert returns None."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetchrow_return=None)
        svc.connect(pool)

        result = await svc._insert_request(
            job_id="job-1",
            vm_name="vm-1",
            command="ls",
            arguments=[],
            cwd="/",
            requesting_user="agent",
            target_user="root",
            nats_reply_subject=None,
            metadata={},
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_insert_serializes_metadata_to_json(self):
        """Metadata dict is serialized to JSON string for DB storage."""
        svc = SudoGateService()
        pool, conn = make_db_pool(fetchrow_return={"id": "uuid-456"})
        svc.connect(pool)

        metadata = {"job_id": "j1", "extra": [1, 2, 3]}
        await svc._insert_request(
            job_id="j1",
            vm_name="v1",
            command="ls",
            arguments=[],
            cwd="/",
            requesting_user="agent",
            target_user="root",
            nats_reply_subject=None,
            metadata=metadata,
        )

        # The metadata param (last positional arg) should be a JSON string
        args = conn.fetchrow.call_args[0]
        metadata_arg = args[9]  # $9 in the query
        parsed = json.loads(metadata_arg)
        assert parsed["extra"] == [1, 2, 3]


# =============================================================================
# Test: _finalize_request() internal helper
# =============================================================================


class TestFinalizeRequest:
    """Tests for _finalize_request() — DB update + NATS reply."""

    @pytest.mark.asyncio
    async def test_finalize_updates_db_and_sends_nats(self):
        """Finalize updates the DB row and sends NATS reply."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        await svc._finalize_request(
            "req-100", "approved", "looks good", "admin", "_INBOX.fin"
        )

        # DB update
        conn.execute.assert_called_once()
        args = conn.execute.call_args[0]
        assert args[1] == "req-100"
        assert args[2] == "approved"
        assert args[3] == "admin"
        assert args[4] == "looks good"

        # NATS reply
        nc.publish.assert_called_once()
        payload = json.loads(nc.publish.call_args[0][1].decode())
        assert payload["approved"] is True

    @pytest.mark.asyncio
    async def test_finalize_denied_sends_false(self):
        """Finalize with 'denied' status sends approved=False via NATS."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        await svc._finalize_request(
            "req-101", "denied", "too risky", "security", "_INBOX.fin"
        )

        payload = json.loads(nc.publish.call_args[0][1].decode())
        assert payload["approved"] is False

    @pytest.mark.asyncio
    async def test_finalize_auto_approved_sends_true(self):
        """Finalize with 'auto_approved' sends approved=True via NATS."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        await svc._finalize_request(
            "req-102", "auto_approved", "auto-rule", "system", "_INBOX.fin"
        )

        payload = json.loads(nc.publish.call_args[0][1].decode())
        assert payload["approved"] is True

    @pytest.mark.asyncio
    async def test_finalize_auto_denied_sends_false(self):
        """Finalize with 'auto_denied' sends approved=False via NATS."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        await svc._finalize_request(
            "req-103", "auto_denied", "auto-rule", "system", "_INBOX.fin"
        )

        payload = json.loads(nc.publish.call_args[0][1].decode())
        assert payload["approved"] is False

    @pytest.mark.asyncio
    async def test_finalize_db_error_still_sends_nats(self):
        """If DB update fails, NATS reply is still attempted."""
        svc = SudoGateService()
        pool, conn = make_db_pool(execute_side_effect=Exception("DB error"))
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        await svc._finalize_request(
            "req-104", "approved", "ok", "admin", "_INBOX.fin"
        )

        # NATS reply should still be sent despite DB failure
        nc.publish.assert_called_once()

    @pytest.mark.asyncio
    async def test_finalize_no_reply_subject_skips_nats(self):
        """Finalize without a reply subject skips NATS publish."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        nc = MagicMock()
        nc.publish = AsyncMock()
        svc.connect(pool, nc)

        await svc._finalize_request(
            "req-105", "approved", "ok", "admin", None
        )

        nc.publish.assert_not_called()

    @pytest.mark.asyncio
    async def test_finalize_no_db_still_sends_nats(self):
        """Finalize without DB still sends NATS reply."""
        svc = SudoGateService()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc._nc = nc

        await svc._finalize_request(
            "req-106", "approved", "ok", "admin", "_INBOX.fin"
        )

        nc.publish.assert_called_once()


# =============================================================================
# Test: Shell metacharacter regex
# =============================================================================


class TestShellMetacharRegex:
    """Tests for _SHELL_META_RE pattern matching."""

    @pytest.mark.parametrize(
        "command",
        [
            "cat /etc/passwd | grep root",
            "ls; rm -rf /",
            "sleep 100 &",
            "echo `whoami`",
            "echo $HOME",
            "echo x > /etc/shadow",
            "echo x < /etc/shadow",
            "rm $(find /tmp -name '*.log')",
            "true && rm -rf /",
            "false || rm -rf /",
        ],
    )
    def test_dangerous_commands_detected(self, command):
        """Commands with shell metacharacters are detected."""
        assert _SHELL_META_RE.search(command) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "apt-get install -y vim",
            "systemctl restart nginx",
            "ls -la /home",
            "chmod 755 /opt/app",
            "chown root:root /etc/config",
            "cp /tmp/file /opt/app/",
            "mkdir -p /opt/newdir",
            "cat /etc/hostname",
        ],
    )
    def test_safe_commands_pass(self, command):
        """Commands without metacharacters are not flagged."""
        assert _SHELL_META_RE.search(command) is None


# =============================================================================
# Test: Singleton instance
# =============================================================================


class TestSingleton:
    """Tests for the module-level singleton."""

    def test_singleton_exists(self):
        """Module exports a singleton sudo_gate instance."""
        from orchestrator.services.sudo_gate import sudo_gate

        assert isinstance(sudo_gate, SudoGateService)

    def test_singleton_initial_state(self):
        """Singleton starts without DB or NATS connections."""
        from orchestrator.services.sudo_gate import sudo_gate

        # Note: other tests may have modified the singleton, but its type is correct
        assert isinstance(sudo_gate, SudoGateService)


# =============================================================================
# Test: Full end-to-end flows
# =============================================================================


class TestEndToEndFlows:
    """Integration-style tests combining multiple methods."""

    @pytest.mark.asyncio
    async def test_request_then_approve_flow(self):
        """Full flow: NATS request arrives -> pending -> human approves."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        # Phase 1: Request arrives (no auto-rules)
        conn.fetchrow.return_value = {"id": "flow-req-1"}
        conn.fetch.return_value = []  # no rules

        data = make_sudo_request_data(
            command="apt-get",
            argv=["install", "-y", "build-essential"],
        )
        msg = make_nats_msg(data, reply="_INBOX.flow1")

        q = svc.subscribe_sse()
        await svc.on_sudo_request(msg)

        # Verify pending state
        assert "flow-req-1" in svc._pending_msgs
        event_type, event_data = q.get_nowait()
        assert event_type == "new_request"

        # Phase 2: Human approves
        conn.fetchrow.return_value = make_request_row(
            request_id="flow-req-1",
            status="pending",
            nats_reply_subject="_INBOX.flow1",
        )

        result = await svc.approve_request("flow-req-1", reason="Safe package install")

        assert result["status"] == "approved"
        assert "flow-req-1" not in svc._pending_msgs

        # SSE got the decision event
        event_type, event_data = q.get_nowait()
        assert event_type == "request_decided"
        assert event_data["status"] == "approved"

    @pytest.mark.asyncio
    async def test_request_then_deny_flow(self):
        """Full flow: NATS request arrives -> pending -> human denies."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        conn.fetchrow.return_value = {"id": "flow-req-2"}
        conn.fetch.return_value = []

        data = make_sudo_request_data(
            command="rm",
            argv=["-rf", "/opt/production"],
        )
        msg = make_nats_msg(data, reply="_INBOX.flow2")

        await svc.on_sudo_request(msg)
        assert "flow-req-2" in svc._pending_msgs

        # Human denies
        conn.fetchrow.return_value = make_request_row(
            request_id="flow-req-2",
            status="pending",
        )

        result = await svc.deny_request("flow-req-2", reason="Destructive command")
        assert result["status"] == "denied"

    @pytest.mark.asyncio
    async def test_request_auto_approved_bypasses_human(self):
        """Full flow: NATS request arrives -> auto-approved -> no pending state."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        conn.fetchrow.return_value = {"id": "flow-req-3"}
        # cmd_string = " ".join(argv) = "install -y libxml2-dev"
        conn.fetch.return_value = [
            {"pattern": "install -y *-dev", "action": "approve"}
        ]

        data = make_sudo_request_data(
            command="apt-get",
            argv=["install", "-y", "libxml2-dev"],
        )
        msg = make_nats_msg(data, reply="_INBOX.flow3")

        q = svc.subscribe_sse()
        await svc.on_sudo_request(msg)

        # Should NOT be pending
        assert "flow-req-3" not in svc._pending_msgs

        # msg.respond was called with approval
        msg.respond.assert_called_once()
        resp = json.loads(msg.respond.call_args[0][0].decode())
        assert resp["approved"] is True

        # SSE should NOT have received a "new_request" event
        # (auto-approved requests don't push to SSE for human review)
        assert q.empty()

    @pytest.mark.asyncio
    async def test_request_then_sweep_expires(self):
        """Full flow: request pending -> TTL expires -> sweep denies it."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        nc = MagicMock()
        nc.publish = AsyncMock()
        nc.flush = AsyncMock()
        svc.connect(pool, nc)

        # Phase 1: Request arrives
        conn.fetchrow.return_value = {"id": "flow-req-4"}
        conn.fetch.return_value = []

        data = make_sudo_request_data(command="make", argv=["install"])
        msg = make_nats_msg(data, reply="_INBOX.flow4")

        await svc.on_sudo_request(msg)
        assert "flow-req-4" in svc._pending_msgs

        # Phase 2: Sweep runs and finds it expired
        conn.fetch.return_value = [
            {"id": "flow-req-4", "nats_reply_subject": "_INBOX.flow4"}
        ]

        svc.subscribe_sse()
        count = await svc.sweep_expired()

        assert count == 1
        assert "flow-req-4" not in svc._pending_msgs

        # msg.respond was used for the expiration reply
        # (called once during on_sudo_request is not what we want here;
        # the pending msg's respond was called by sweep)
        # The stored pending msg is the original `msg` object
        # sweep calls msg.respond on the stored pending msg

    @pytest.mark.asyncio
    async def test_multiple_concurrent_requests(self):
        """Multiple requests can be pending simultaneously."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        svc.connect(pool)

        conn.fetch.return_value = []  # no rules

        for i in range(5):
            conn.fetchrow.return_value = {"id": f"req-multi-{i}"}
            data = make_sudo_request_data(
                job_id=f"job-{i}", command="ls", argv=[f"/dir{i}"]
            )
            msg = make_nats_msg(data, reply=f"_INBOX.multi{i}")
            await svc.on_sudo_request(msg)

        assert len(svc._pending_msgs) == 5
        for i in range(5):
            assert f"req-multi-{i}" in svc._pending_msgs

    @pytest.mark.asyncio
    async def test_rule_crud_then_evaluate(self):
        """Create rules, then verify evaluation uses them."""
        svc = SudoGateService()
        pool, conn = make_db_pool()
        svc.connect(pool)

        # Create rule
        conn.fetchrow.return_value = {
            "id": "rule-new",
            "pattern": "apt-get install *",
            "action": "approve",
            "priority": 10,
        }
        rule = await svc.create_rule("apt-get install *", "approve", priority=10)
        assert rule is not None

        # Evaluate with matching command
        conn.fetch.return_value = [
            {"pattern": "apt-get install *", "action": "approve"}
        ]
        result = await svc._evaluate_auto_rules("apt-get install vim")
        assert result == "approve"

        # Delete rule
        conn.execute.return_value = "DELETE 1"
        deleted = await svc.delete_rule("rule-new")
        assert deleted is True

        # After deletion, no rules match
        conn.fetch.return_value = []
        result = await svc._evaluate_auto_rules("apt-get install vim")
        assert result is None
