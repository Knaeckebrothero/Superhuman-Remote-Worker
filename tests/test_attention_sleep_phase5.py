"""Tests for Phase 5 of headless persistent sessions: attention-sleep watchdog,
awaiting_user state transitions in the agent, magic-link wake on suspended
thread, and the /magic/extend endpoint.

Live DB round-trips (UPDATE → trigger → NOTIFY → wake) are covered by the
smoke runbook at docs/tests/headless_sessions_smoke.md. Here we mock the
postgres pool and the workspace_suspension_service to exercise the
contract surface of each new code path.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers: fake postgres pool + acquire() context manager
# ---------------------------------------------------------------------------


def _make_db(*, fetchval=None, fetchrow=None, fetch=None, execute=None):
    """Build a MagicMock db with .acquire() yielding a connection whose
    fetchval/fetchrow/fetch/execute can be stubbed independently.
    """
    fake_conn = MagicMock()

    def _wrap(val):
        if val is None:
            return AsyncMock(return_value=None)
        if isinstance(val, AsyncMock):
            return val
        if callable(val):
            return AsyncMock(side_effect=val)
        return AsyncMock(return_value=val)

    fake_conn.fetchval = _wrap(fetchval)
    fake_conn.fetchrow = _wrap(fetchrow)
    fake_conn.fetch = _wrap(fetch)
    fake_conn.execute = _wrap(execute)
    fake_conn.add_listener = AsyncMock()
    fake_conn.remove_listener = AsyncMock()

    class _Acquire:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    db = MagicMock()
    db.acquire = lambda: _Acquire()
    db._fake_conn = fake_conn
    return db


# ===========================================================================
# Section 1 — Agent: awaiting_user transitions in persistent_app
# ===========================================================================
#
# _loop_get_user_input flips to awaiting_user when about to block AND no
# subscribers AND turn_count > 0. _subscribe reverts to active on the
# 0→1 subscriber transition.


def _reset_agent_globals():
    import src.api.persistent_app as mod

    mod._session = None
    mod._thread_id = None
    mod._orchestrator_client = None
    mod._subscribers.clear()
    mod._loop_user_queue = None


def _install_agent_session(*, turn_count: int = 0):
    """Minimal session + orchestrator_client wiring for transition tests."""
    import src.api.persistent_app as mod

    session = MagicMock()
    session.turn_count = turn_count
    session.permission_mode = "supervised"
    session.tool_decisions = {}
    session.postgres_conn = None
    session.config = MagicMock()
    session.config.interactive.idle_timeout_minutes = 0
    mod._session = session
    mod._thread_id = "thread-test-uuid"
    client = AsyncMock()
    client.update_thread_status = AsyncMock(return_value=True)
    mod._orchestrator_client = client
    mod._loop_user_queue = asyncio.Queue()
    return session, client


class TestSubscribeRevertsAwaitingUser:
    def setup_method(self):
        _reset_agent_globals()

    def teardown_method(self):
        _reset_agent_globals()

    @pytest.mark.asyncio
    async def test_first_subscriber_schedules_revert(self):
        import src.api.persistent_app as mod

        _install_agent_session(turn_count=2)

        _ = mod._subscribe("client-1")

        # _safe_set_thread_status runs as a task — give it a tick.
        await asyncio.sleep(0)
        mod._orchestrator_client.update_thread_status.assert_awaited_with(
            "thread-test-uuid", "active"
        )

    @pytest.mark.asyncio
    async def test_second_subscriber_does_not_schedule_revert(self):
        import src.api.persistent_app as mod

        _install_agent_session(turn_count=2)
        _ = mod._subscribe("client-1")
        await asyncio.sleep(0)
        mod._orchestrator_client.update_thread_status.reset_mock()

        _ = mod._subscribe("client-2")
        await asyncio.sleep(0)
        mod._orchestrator_client.update_thread_status.assert_not_called()


class TestLoopGetUserInputAwaitingUserFlip:
    def setup_method(self):
        _reset_agent_globals()

    def teardown_method(self):
        _reset_agent_globals()

    @pytest.mark.asyncio
    async def test_flips_when_untethered_after_turn(self):
        import src.api.persistent_app as mod

        _, client = _install_agent_session(turn_count=3)
        mod._loop_user_queue.put_nowait("hi")  # unblock the get()

        await mod._loop_get_user_input()

        # Task is fire-and-forget — yield to event loop.
        await asyncio.sleep(0)
        client.update_thread_status.assert_awaited_with(
            "thread-test-uuid", "awaiting_user"
        )

    @pytest.mark.asyncio
    async def test_skips_flip_on_first_call_before_any_turn(self):
        import src.api.persistent_app as mod

        _, client = _install_agent_session(turn_count=0)
        mod._loop_user_queue.put_nowait("hi")

        await mod._loop_get_user_input()
        await asyncio.sleep(0)

        client.update_thread_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_flip_when_subscriber_present(self):
        import src.api.persistent_app as mod

        _, client = _install_agent_session(turn_count=3)
        mod._subscribers["ws-client"] = asyncio.Queue()
        await asyncio.sleep(0)
        client.update_thread_status.reset_mock()

        mod._loop_user_queue.put_nowait("hi")
        await mod._loop_get_user_input()
        await asyncio.sleep(0)

        # Only "active" from the subscribe wasn't called this time either;
        # critically no awaiting_user was scheduled.
        for call in client.update_thread_status.await_args_list:
            assert call.args[1] != "awaiting_user"


# ===========================================================================
# Section 2 — Agent: permission_check wake-path select-first guard
# ===========================================================================
#
# After a magic-link click while the agent is suspended, the workspace and
# agent pod are restored from snapshot. The agent's LangGraph checkpoint
# replays the same tool_call_id. permission_check must SELECT for an
# existing terminal decision before INSERTing a fresh request — otherwise
# the user would have to click approve a second time.


class TestPermissionCheckWakePathGuard:
    def setup_method(self):
        _reset_agent_globals()

    def teardown_method(self):
        _reset_agent_globals()

    @pytest.mark.asyncio
    async def test_reuses_prior_approved_decision(self):
        import src.api.persistent_app as mod

        session, _ = _install_agent_session(turn_count=1)
        db = _make_db(fetchrow={"status": "approved"})
        session.postgres_conn = db
        mod._session = session

        result = await mod._loop_permission_check("run_command", {}, "tc-1")
        assert result is True
        assert session.tool_decisions.get("tc-1") == "approved"
        # We never reached the INSERT step.
        db._fake_conn.fetchval.assert_not_called()

    @pytest.mark.asyncio
    async def test_reuses_prior_denied_decision(self):
        import src.api.persistent_app as mod

        session, _ = _install_agent_session(turn_count=1)
        db = _make_db(fetchrow={"status": "denied"})
        session.postgres_conn = db
        mod._session = session

        result = await mod._loop_permission_check("run_command", {}, "tc-2")
        assert result is False
        assert session.tool_decisions.get("tc-2") == "denied"

    @pytest.mark.asyncio
    async def test_no_prior_decision_falls_through_to_insert(self):
        import src.api.persistent_app as mod

        session, _ = _install_agent_session(turn_count=1)
        # fetchrow=None means no prior decision; fetchval drives INSERT/SELECT.
        db = _make_db(
            fetchrow=None,
            fetchval=lambda sql, *a: (
                "11111111-1111-1111-1111-111111111111"
                if "INSERT" in sql
                else "approved"
                if "SELECT status" in sql
                else 0
            ),
        )
        session.postgres_conn = db
        mod._session = session

        result = await mod._loop_permission_check("run_command", {}, "tc-3")
        # Fell through to insert + wait, picked up the (mocked) "approved"
        # status on the race-check select.
        assert result is True
        assert db._fake_conn.fetchval.await_count >= 1


# ===========================================================================
# Section 3 — Attention-sleep watchdog
# ===========================================================================
#
# attention_sleep_sweeper runs every 60s, selects stale awaiting_user
# threads, calls workspace_suspension_service.suspend_thread_workspace,
# and CAS-UPDATEs status to 'suspended'.


class TestAttentionSleepSweeper:
    def setup_method(self):
        import orchestrator.main as om

        # Save the originals so each test restores cleanly.
        self._orig_db = om.postgres_db
        self._orig_svc = om.workspace_suspension_service

    def teardown_method(self):
        import orchestrator.main as om

        om.postgres_db = self._orig_db
        om.workspace_suspension_service = self._orig_svc

    @pytest.mark.asyncio
    async def test_suspends_stale_awaiting_user(self):
        import orchestrator.main as om

        # Stub the DB: SELECT returns one stale row; UPDATE returns its id.
        db = _make_db(
            fetch=[{"id": "thread-abc"}],
            fetchval="thread-abc",
        )
        svc = MagicMock()
        svc.is_enabled = True
        svc.suspend_thread_workspace = AsyncMock(return_value=True)

        om.postgres_db = db
        om.workspace_suspension_service = svc

        shutdown = asyncio.Event()
        # Run one tick: schedule the sweeper, give it a moment, signal shutdown.
        task = asyncio.create_task(om.attention_sleep_sweeper(shutdown))
        await asyncio.sleep(0.05)
        shutdown.set()
        await task

        svc.suspend_thread_workspace.assert_awaited_with("thread-abc")
        # The CAS UPDATE → fetchval should also have fired.
        update_calls = [
            c
            for c in db._fake_conn.fetchval.await_args_list
            if "UPDATE threads" in c.args[0]
        ]
        assert len(update_calls) >= 1

    @pytest.mark.asyncio
    async def test_skips_when_service_disabled(self):
        import orchestrator.main as om

        db = _make_db(fetch=[{"id": "thread-abc"}])
        svc = MagicMock()
        svc.is_enabled = False
        svc.suspend_thread_workspace = AsyncMock(return_value=True)

        om.postgres_db = db
        om.workspace_suspension_service = svc

        shutdown = asyncio.Event()
        task = asyncio.create_task(om.attention_sleep_sweeper(shutdown))
        await asyncio.sleep(0.05)
        shutdown.set()
        await task

        svc.suspend_thread_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_suspend_failure_without_crashing(self):
        import orchestrator.main as om

        db = _make_db(fetch=[{"id": "thread-abc"}])
        svc = MagicMock()
        svc.is_enabled = True
        svc.suspend_thread_workspace = AsyncMock(return_value=False)

        om.postgres_db = db
        om.workspace_suspension_service = svc

        shutdown = asyncio.Event()
        task = asyncio.create_task(om.attention_sleep_sweeper(shutdown))
        await asyncio.sleep(0.05)
        shutdown.set()
        await task

        # suspend was tried but did not produce an UPDATE.
        svc.suspend_thread_workspace.assert_awaited_once()


# ===========================================================================
# Section 4 — Magic-link extend window
# ===========================================================================
#
# magic_link_extend validates the token (without consuming), bumps
# awaiting_user_since, increments extend_count, respects the cap.


class TestMagicLinkExtendCap:
    """The cap-enforcement logic lives inside the UPDATE WHERE clause:
    `extend_count < $cap`. We exercise the three observable banner states:
    'extended', 'cap_reached', 'not_awaiting'.
    """

    def setup_method(self):
        import orchestrator.main as om

        self._orig_db = om.postgres_db
        self._orig_email = om.email_service
        self._orig_hn = om.headless_notifications

    def teardown_method(self):
        import orchestrator.main as om

        om.postgres_db = self._orig_db
        om.email_service = self._orig_email
        om.headless_notifications = self._orig_hn

    @pytest.mark.asyncio
    async def test_extended_banner_when_under_cap(self):
        import orchestrator.main as om

        # Token valid; UPDATE returns a new extend_count of 1; the second
        # fetchrow loads the permission row for re-render.
        rows = iter(
            [
                {"extend_count": 1},  # UPDATE returns
                {  # permission_row load
                    "tool_name": "run_command",
                    "tool_args": '{"command":"ls"}',
                    "status": "pending",
                },
            ]
        )
        db = _make_db(fetchrow=lambda *a, **kw: next(rows))
        om.postgres_db = db

        # Stub the email_service so cockpit_url resolves.
        om.email_service = MagicMock()
        om.email_service.cockpit_url = "http://localhost:4200"

        # Stub validate_magic_link to return a valid token row.
        om.headless_notifications = MagicMock()
        om.headless_notifications.validate_magic_link = AsyncMock(
            return_value={
                "id": "tok-1",
                "approval_id": "appr-1",
                "thread_id": "thread-abc",
                "intended_decision": "approved",
            }
        )

        resp = await om.magic_link_extend("raw-token")
        body = resp.body.decode()
        assert "extended by 60 minutes" in body.lower() or "Window extended" in body
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cap_reached_banner_when_update_misses(self):
        import orchestrator.main as om

        # UPDATE returns None (cap blocked the WHERE clause). Then SELECT
        # reveals extend_count >= cap.
        rows = iter(
            [
                None,  # UPDATE failed (cap)
                {  # SELECT status, extend_count
                    "status": "awaiting_user",
                    "extend_count": 4,
                },
                {  # permission_row load
                    "tool_name": "run_command",
                    "tool_args": "{}",
                    "status": "pending",
                },
            ]
        )
        db = _make_db(fetchrow=lambda *a, **kw: next(rows))
        om.postgres_db = db
        om.email_service = MagicMock()
        om.email_service.cockpit_url = "http://localhost:4200"
        om.headless_notifications = MagicMock()
        om.headless_notifications.validate_magic_link = AsyncMock(
            return_value={
                "id": "tok-1",
                "approval_id": "appr-1",
                "thread_id": "thread-abc",
                "intended_decision": "approved",
            }
        )

        resp = await om.magic_link_extend("raw-token")
        body = resp.body.decode()
        assert "Extend limit reached" in body
        assert "disabled" in body  # extend button disabled

    @pytest.mark.asyncio
    async def test_not_awaiting_banner_when_thread_already_active(self):
        import orchestrator.main as om

        rows = iter(
            [
                None,  # UPDATE failed (not in awaiting_user)
                {  # SELECT shows thread is now 'active'
                    "status": "active",
                    "extend_count": 0,
                },
                {  # permission_row load
                    "tool_name": "run_command",
                    "tool_args": "{}",
                    "status": "pending",
                },
            ]
        )
        db = _make_db(fetchrow=lambda *a, **kw: next(rows))
        om.postgres_db = db
        om.email_service = MagicMock()
        om.email_service.cockpit_url = "http://localhost:4200"
        om.headless_notifications = MagicMock()
        om.headless_notifications.validate_magic_link = AsyncMock(
            return_value={
                "id": "tok-1",
                "approval_id": "appr-1",
                "thread_id": "thread-abc",
                "intended_decision": "approved",
            }
        )

        resp = await om.magic_link_extend("raw-token")
        body = resp.body.decode()
        assert "No extend needed" in body or "already active" in body

    @pytest.mark.asyncio
    async def test_invalid_token_returns_404(self):
        import orchestrator.main as om

        om.postgres_db = _make_db()
        om.email_service = MagicMock()
        om.email_service.cockpit_url = "http://localhost:4200"
        om.headless_notifications = MagicMock()
        om.headless_notifications.validate_magic_link = AsyncMock(return_value=None)

        resp = await om.magic_link_extend("bad-token")
        assert resp.status_code == 404


# ===========================================================================
# Section 5 — Magic-link approve: suspended-thread wake task
# ===========================================================================
#
# magic_link_post fires a fire-and-forget task that restores the
# workspace if it's suspended. We test the helper directly because the
# full POST handler depends on the consume CAS path already covered by
# Phase 4 tests.


class TestPhase5WakeIfSuspended:
    def setup_method(self):
        import orchestrator.main as om

        self._orig_db = om.postgres_db
        self._orig_svc = om.workspace_suspension_service
        self._orig_prov = om.persistent_provisioner

    def teardown_method(self):
        import orchestrator.main as om

        om.postgres_db = self._orig_db
        om.workspace_suspension_service = self._orig_svc
        om.persistent_provisioner = self._orig_prov

    @pytest.mark.asyncio
    async def test_restores_when_workspace_suspended(self):
        import orchestrator.main as om

        # get_thread returns metadata with suspended workspace.
        thread = {
            "id": "thread-abc",
            "agent_id": None,
            "config_name": "persistent_defaults",
            "metadata": {"workspace_container": {"status": "suspended"}},
        }
        db = MagicMock()
        db.get_thread = AsyncMock(return_value=thread)
        db.acquire = lambda: _make_db()._Acquire if False else _make_db().acquire()
        # Easier: build a proper db helper
        db_inner = _make_db()
        db.acquire = db_inner.acquire
        om.postgres_db = db

        svc = MagicMock()
        svc.is_enabled = True
        svc.restore_thread_workspace = AsyncMock(return_value=True)
        om.workspace_suspension_service = svc

        prov = MagicMock()
        prov.create_agent_pod = AsyncMock(return_value=True)
        om.persistent_provisioner = prov

        await om._phase5_wake_if_suspended("thread-abc")
        # Give the inner create_task room.
        await asyncio.sleep(0)

        svc.restore_thread_workspace.assert_awaited_with("thread-abc")
        prov.create_agent_pod.assert_called_with(
            "thread-abc", config_name="persistent_defaults"
        )

    @pytest.mark.asyncio
    async def test_skips_when_workspace_not_suspended(self):
        import orchestrator.main as om

        thread = {
            "id": "thread-abc",
            "agent_id": "agent-1",
            "metadata": {"workspace_container": {"status": "ready"}},
        }
        db = MagicMock()
        db.get_thread = AsyncMock(return_value=thread)
        db_inner = _make_db()
        db.acquire = db_inner.acquire
        om.postgres_db = db

        svc = MagicMock()
        svc.is_enabled = True
        svc.restore_thread_workspace = AsyncMock()
        om.workspace_suspension_service = svc

        prov = MagicMock()
        prov.create_agent_pod = AsyncMock()
        om.persistent_provisioner = prov

        await om._phase5_wake_if_suspended("thread-abc")
        await asyncio.sleep(0)

        svc.restore_thread_workspace.assert_not_called()
        # agent_id is already bound; no need to re-create.
        prov.create_agent_pod.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_missing_thread_gracefully(self):
        import orchestrator.main as om

        db = MagicMock()
        db.get_thread = AsyncMock(return_value=None)
        db.acquire = _make_db().acquire
        om.postgres_db = db
        om.workspace_suspension_service = MagicMock()
        om.persistent_provisioner = MagicMock()

        # Should not raise.
        await om._phase5_wake_if_suspended("missing-thread")
