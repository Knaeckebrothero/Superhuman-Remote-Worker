"""Tests for Phase 3 of headless persistent sessions: DB-backed permission
gates with LISTEN/NOTIFY. The real round-trip (INSERT → UPDATE → trigger →
NOTIFY → wake) needs a live Postgres and lives in integration tests; here
we cover the data-flow and contract surface with mocks.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helper: a fake postgres_conn that returns a context-managed asyncpg-like
# connection object with the methods _loop_permission_check uses.
# ---------------------------------------------------------------------------


def _make_postgres_conn(
    *,
    insert_returns: str = "11111111-1111-1111-1111-111111111111",
    select_status_sequence: list[str] = None,
    update_returns: dict | None = None,
    add_listener_callback=None,
):
    """Build a MagicMock postgres_conn whose .acquire() yields a connection
    with stubbed fetchval/fetchrow/execute/add_listener/remove_listener."""

    select_status_sequence = list(select_status_sequence or ["pending"])

    fake_conn = MagicMock()
    fake_conn.fetchval = AsyncMock()
    # Default to None (asyncpg's "no row" return) so callers without an
    # explicit update_returns don't accidentally trip Phase 5's
    # SELECT-first-by-tool_call_id guard in _loop_permission_check.
    fake_conn.fetchrow = AsyncMock(return_value=None)
    fake_conn.execute = AsyncMock()
    fake_conn.add_listener = AsyncMock()
    fake_conn.remove_listener = AsyncMock()

    # fetchval routes:
    #   - INSERT ... RETURNING id   -> insert_returns
    #   - SELECT status ... WHERE id = $1  -> next item from sequence
    async def _fetchval(sql, *args, **kwargs):
        if "INSERT" in sql:
            return insert_returns
        if "SELECT status" in sql:
            return (
                select_status_sequence.pop(0) if select_status_sequence else "pending"
            )
        if "MAX(seq)" in sql or "COALESCE(MAX" in sql:
            return 0
        if "events_epoch" in sql:
            return 0
        return None

    fake_conn.fetchval.side_effect = _fetchval

    if update_returns is not None:
        fake_conn.fetchrow.return_value = update_returns

    if add_listener_callback is not None:
        fake_conn.add_listener.side_effect = add_listener_callback

    class _Acquire:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    postgres_conn = MagicMock()
    postgres_conn.acquire = lambda: _Acquire()
    postgres_conn._fake_conn = fake_conn
    return postgres_conn


def _install_session(postgres_conn=None, permission_mode="supervised"):
    """Install a minimal _session into the persistent_app module globals.

    Note: call AFTER create_persistent_app() if you also need _thread_id
    set — create_persistent_app overwrites _thread_id from its arg.
    """
    import src.api.persistent_app as mod

    session = MagicMock()
    session.permission_mode = permission_mode
    session.tool_decisions = {}
    session.postgres_conn = postgres_conn
    mod._session = session
    mod._thread_id = "thread-test"
    return session


# ---------------------------------------------------------------------------
# Section 1 — Early returns: autonomous / auto_accept / no session
# ---------------------------------------------------------------------------


class TestPermissionCheckEarlyReturns:
    def setup_method(self):
        import src.api.persistent_app as mod

        mod._session = None
        mod._thread_id = None

    def teardown_method(self):
        import src.api.persistent_app as mod

        mod._session = None
        mod._thread_id = None

    @pytest.mark.asyncio
    async def test_no_session_denies(self):
        import src.api.persistent_app as mod

        result = await mod._loop_permission_check("read_file", {}, "tc1")
        assert result is False

    @pytest.mark.asyncio
    async def test_autonomous_approves_without_db(self):
        import src.api.persistent_app as mod

        _install_session(postgres_conn=None, permission_mode="autonomous")
        result = await mod._loop_permission_check("run_command", {}, "tc1")
        assert result is True

    @pytest.mark.asyncio
    async def test_auto_accept_approves_non_shell(self):
        import src.api.persistent_app as mod

        _install_session(postgres_conn=None, permission_mode="auto_accept")
        result = await mod._loop_permission_check("read_file", {}, "tc1")
        assert result is True

    @pytest.mark.asyncio
    async def test_auto_accept_falls_through_for_shell(self):
        """Shell tools under auto_accept go through the DB path; with no
        postgres_conn the INSERT fails and we deny conservatively."""
        import src.api.persistent_app as mod

        session = _install_session(postgres_conn=None, permission_mode="auto_accept")
        result = await mod._loop_permission_check("run_command", {}, "tc1")
        assert result is False
        assert session.tool_decisions["tc1"] == "denied"


# ---------------------------------------------------------------------------
# Section 2 — _insert_permission_request behavior
# ---------------------------------------------------------------------------


class TestInsertPermissionRequest:
    def setup_method(self):
        import src.api.persistent_app as mod

        mod._session = None
        mod._thread_id = None

    def teardown_method(self):
        import src.api.persistent_app as mod

        mod._session = None
        mod._thread_id = None

    @pytest.mark.asyncio
    async def test_returns_uuid_string(self):
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn(insert_returns="aaaaaaaa-1111")
        _install_session(postgres_conn=postgres)
        rid = await mod._insert_permission_request("tc1", "read_file", {"path": "/x"})
        assert rid == "aaaaaaaa-1111"
        # INSERT was called via fetchval.
        sql = postgres._fake_conn.fetchval.await_args_list[0].args[0]
        assert "INSERT INTO thread_permission_requests" in sql

    @pytest.mark.asyncio
    async def test_returns_none_when_session_missing(self):
        import src.api.persistent_app as mod

        # No _session set.
        rid = await mod._insert_permission_request("tc1", "x", {})
        assert rid is None

    @pytest.mark.asyncio
    async def test_returns_none_on_db_error(self):
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn()
        postgres._fake_conn.fetchval.side_effect = RuntimeError("db down")
        _install_session(postgres_conn=postgres)
        rid = await mod._insert_permission_request("tc1", "x", {})
        assert rid is None


# ---------------------------------------------------------------------------
# Section 3 — _resolve_pending_permission UPDATEs the right row
# ---------------------------------------------------------------------------


class TestResolvePendingPermission:
    def setup_method(self):
        import src.api.persistent_app as mod

        mod._session = None
        mod._thread_id = None

    def teardown_method(self):
        import src.api.persistent_app as mod

        mod._session = None
        mod._thread_id = None

    @pytest.mark.asyncio
    async def test_resolves_by_explicit_id(self):
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn(
            update_returns={
                "id": "abc-123",
                "status": "approved",
                "tool_call_id": "tc1",
                "thread_id": "thread-test",
            }
        )
        _install_session(postgres_conn=postgres)
        result = await mod._resolve_pending_permission(
            "approved", approval_id="abc-123"
        )
        assert result is not None
        assert result["status"] == "approved"
        # UPDATE issued via fetchrow with the explicit id.
        sql = postgres._fake_conn.fetchrow.await_args.args[0]
        assert "UPDATE thread_permission_requests" in sql
        assert "WHERE id = $1" in sql

    @pytest.mark.asyncio
    async def test_resolves_most_recent_when_no_id(self):
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn(
            update_returns={
                "id": "abc-456",
                "status": "denied",
                "tool_call_id": "tc2",
                "thread_id": "thread-test",
            }
        )
        _install_session(postgres_conn=postgres)
        result = await mod._resolve_pending_permission("denied")
        assert result is not None
        assert result["id"] == "abc-456"
        sql = postgres._fake_conn.fetchrow.await_args.args[0]
        # The fallback selects the most-recent pending by thread.
        assert "ORDER BY requested_at DESC LIMIT 1" in sql

    @pytest.mark.asyncio
    async def test_rejects_invalid_decision(self):
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn()
        _install_session(postgres_conn=postgres)
        # Garbage decision → return None, no UPDATE.
        result = await mod._resolve_pending_permission("maybe?")
        assert result is None
        postgres._fake_conn.fetchrow.assert_not_called()


# ---------------------------------------------------------------------------
# Section 4 — _wait_for_permission_resolution: race-safe SELECT-first
# ---------------------------------------------------------------------------


class TestWaitForPermissionResolution:
    def setup_method(self):
        import src.api.persistent_app as mod

        mod._session = None
        mod._thread_id = None

    def teardown_method(self):
        import src.api.persistent_app as mod

        mod._session = None
        mod._thread_id = None

    @pytest.mark.asyncio
    async def test_returns_immediately_if_already_approved(self):
        """SELECT-first guard: if the UPDATE happened between INSERT and
        add_listener, the pre-wait status check picks it up without waiting."""
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn(
            select_status_sequence=["approved"],
        )
        _install_session(postgres_conn=postgres)
        result = await mod._wait_for_permission_resolution("rid-1", timeout=1.0)
        assert result == "approved"
        # add_listener registered then immediately removed.
        postgres._fake_conn.add_listener.assert_awaited_once()
        postgres._fake_conn.remove_listener.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_denied_when_already_denied(self):
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn(
            select_status_sequence=["denied"],
        )
        _install_session(postgres_conn=postgres)
        result = await mod._wait_for_permission_resolution("rid-2", timeout=1.0)
        assert result == "denied"

    @pytest.mark.asyncio
    async def test_timeout_marks_expired_and_returns_expired(self):
        """When the listener never fires and the wait times out, the
        function UPDATEs the row to expired (CAS-style) and re-reads."""
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn(
            # First SELECT: still pending → enter wait.
            # Second SELECT (post-timeout, post-UPDATE): expired.
            select_status_sequence=["pending", "expired"],
        )
        _install_session(postgres_conn=postgres)
        result = await mod._wait_for_permission_resolution("rid-3", timeout=0.1)
        assert result == "expired"
        # The expire-UPDATE was issued.
        update_sql = postgres._fake_conn.execute.await_args.args[0]
        assert "UPDATE thread_permission_requests" in update_sql
        assert "SET status = 'expired'" in update_sql

    @pytest.mark.asyncio
    async def test_returns_denied_on_db_error(self):
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn()
        postgres._fake_conn.add_listener.side_effect = RuntimeError("listener fail")
        _install_session(postgres_conn=postgres)
        result = await mod._wait_for_permission_resolution("rid-4", timeout=0.1)
        assert result == "denied"

    @pytest.mark.asyncio
    async def test_notify_callback_wakes_the_wait(self):
        """When the NOTIFY callback fires for our id, the wait returns
        the final status from the post-wake SELECT."""
        import src.api.persistent_app as mod

        target_id = "rid-5"

        captured_callbacks = []

        async def _capture_listener(channel, callback):
            captured_callbacks.append(callback)

        postgres = _make_postgres_conn(
            # Pre-wait SELECT: pending. Post-wake SELECT: approved.
            select_status_sequence=["pending", "approved"],
        )
        postgres._fake_conn.add_listener.side_effect = _capture_listener
        _install_session(postgres_conn=postgres)

        async def _trigger_notify():
            # Wait for the listener to register, then fire a NOTIFY
            # carrying our id.
            for _ in range(50):
                if captured_callbacks:
                    break
                await asyncio.sleep(0.01)
            assert captured_callbacks, "listener never registered"
            import json as _json

            captured_callbacks[0](
                MagicMock(),
                12345,
                "thread_permission_updates",
                _json.dumps({"id": target_id, "status": "approved"}),
            )

        trigger_task = asyncio.create_task(_trigger_notify())
        try:
            result = await mod._wait_for_permission_resolution(target_id, timeout=2.0)
        finally:
            await trigger_task
        assert result == "approved"


# ---------------------------------------------------------------------------
# Section 5 — _loop_permission_check end-to-end (mocked)
# ---------------------------------------------------------------------------


class TestLoopPermissionCheckDBPath:
    def setup_method(self):
        import src.api.persistent_app as mod

        mod._session = None
        mod._thread_id = None
        mod._subscribers.clear()
        mod._events_epoch = 0
        mod._next_seq = 0

    def teardown_method(self):
        import src.api.persistent_app as mod

        mod._session = None
        mod._thread_id = None
        mod._subscribers.clear()

    @pytest.mark.asyncio
    async def test_denies_when_insert_fails(self):
        import src.api.persistent_app as mod

        session = _install_session(postgres_conn=None, permission_mode="supervised")
        result = await mod._loop_permission_check("run_command", {}, "tc1")
        assert result is False
        assert session.tool_decisions["tc1"] == "denied"

    @pytest.mark.asyncio
    async def test_approved_path_returns_true_and_records_decision(self):
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn(
            insert_returns="rid-approve",
            select_status_sequence=["approved"],  # SELECT-first picks it up
        )
        session = _install_session(postgres_conn=postgres, permission_mode="supervised")
        result = await mod._loop_permission_check("run_command", {"cmd": "ls"}, "tc1")
        assert result is True
        assert session.tool_decisions["tc1"] == "approved"

    @pytest.mark.asyncio
    async def test_denied_path_returns_false_and_records_decision(self):
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn(
            insert_returns="rid-deny",
            select_status_sequence=["denied"],
        )
        session = _install_session(postgres_conn=postgres, permission_mode="supervised")
        result = await mod._loop_permission_check(
            "run_command", {"cmd": "rm -rf /"}, "tc1"
        )
        assert result is False
        assert session.tool_decisions["tc1"] == "denied"

    @pytest.mark.asyncio
    async def test_broadcast_carries_approval_id(self):
        """The permission.request frame must include approval_id so
        clients can refer back via either tool_call_id or approval_id."""
        import src.api.persistent_app as mod

        postgres = _make_postgres_conn(
            insert_returns="rid-broadcast",
            select_status_sequence=["approved"],
        )
        _install_session(postgres_conn=postgres, permission_mode="supervised")
        q = mod._subscribe("client-X")
        await mod._loop_permission_check("run_command", {"cmd": "ls"}, "tc1")
        # First broadcast on the queue should be permission.request.
        frame = q.get_nowait()
        assert frame["method"] == "permission.request"
        assert frame["params"]["approval_id"] == "rid-broadcast"
        assert frame["params"]["id"] == "tc1"
        assert frame["params"]["tool"] == "run_command"


# ---------------------------------------------------------------------------
# Section 6 — Agent REST /api/approve hits the DB path
# ---------------------------------------------------------------------------


class TestApiApproveRestEndpoint:
    def setup_method(self):
        import src.api.persistent_app as mod

        mod._session = None

    def teardown_method(self):
        import src.api.persistent_app as mod

        mod._session = None

    def test_resolves_via_db_helper_with_explicit_id(self):
        from fastapi.testclient import TestClient

        import src.api.persistent_app as mod
        from src.api.persistent_app import create_persistent_app

        postgres = _make_postgres_conn(
            update_returns={
                "id": "rid-X",
                "status": "approved",
                "tool_call_id": "tc-abc",
                "thread_id": "thread-test",
            }
        )
        # create_persistent_app overwrites _thread_id — install AFTER.
        app = create_persistent_app("interactive")
        _install_session(postgres_conn=postgres)
        try:
            client = TestClient(app)
            resp = client.post(
                "/api/approve",
                json={"decision": "approve", "approval_id": "rid-X"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["accepted"] is True
            assert body["approval_id"] == "rid-X"
            assert body["tool_call_id"] == "tc-abc"
        finally:
            mod._session = None

    def test_returns_404_when_no_pending(self):
        from fastapi.testclient import TestClient

        import src.api.persistent_app as mod
        from src.api.persistent_app import create_persistent_app

        postgres = _make_postgres_conn(update_returns=None)
        app = create_persistent_app("interactive")
        _install_session(postgres_conn=postgres)
        try:
            client = TestClient(app)
            resp = client.post(
                "/api/approve",
                json={"decision": "approve"},  # no id, no pending
            )
            assert resp.status_code == 404
        finally:
            mod._session = None

    def test_rejects_invalid_decision(self):
        from fastapi.testclient import TestClient

        import src.api.persistent_app as mod
        from src.api.persistent_app import create_persistent_app

        postgres = _make_postgres_conn()
        app = create_persistent_app("interactive")
        _install_session(postgres_conn=postgres)
        try:
            client = TestClient(app)
            resp = client.post(
                "/api/approve",
                json={"decision": "maybe"},
            )
            assert resp.status_code == 400
        finally:
            mod._session = None


# ---------------------------------------------------------------------------
# Section 7 — Imports of removed APPROVE/DENY sentinels stay clean
# ---------------------------------------------------------------------------


def test_persistent_app_no_longer_imports_sentinels():
    """Phase 3 removed the APPROVE_SENTINEL/DENY_SENTINEL imports because
    the queue-based approval path was replaced with the DB-backed one.
    They still live in persistent_graph for dual_app's worker-mode flow."""
    import src.api.persistent_app as mod

    assert not hasattr(mod, "APPROVE_SENTINEL")
    assert not hasattr(mod, "DENY_SENTINEL")
