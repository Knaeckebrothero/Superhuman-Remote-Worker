"""Tests for Phase 5 of headless persistent sessions: attention-sleep watchdog,
awaiting_user state transitions in the agent, magic-link wake on suspended
thread, and the /magic/extend endpoint.

Live DB round-trips (UPDATE → trigger → NOTIFY → wake) are covered by the
smoke runbook at knowledge-base/knowledge/tests/headless_sessions_smoke.md. Here we mock the
postgres pool and exact retirement owner to exercise the contract surface of
each new code path.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.persistent_graph import PermissionOutcome
from orchestrator.services.workspace_lifecycle import EnsureOutcome


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

    class _Transaction:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    fake_conn.transaction = lambda: _Transaction()

    class _Acquire:
        async def __aenter__(self):
            return fake_conn

        async def __aexit__(self, exc_type, exc, tb):
            return None

    db = MagicMock()
    db.acquire = lambda: _Acquire()
    db._fake_conn = fake_conn
    return db


def _thread_permissions_deps(db):
    """Build router dependencies with the test's db fake (R1.B10)."""
    from orchestrator.routers.thread_permissions import (
        ThreadPermissionsDependencies,
    )

    return ThreadPermissionsDependencies(
        store=db,
        emit_session_provisioning_failure=AsyncMock(return_value=None),
        persistent_thread_recycler=None,
    )


def _wake_kwargs(db):
    """Explicit collaborator kwargs for direct wake calls (R1.B10)."""
    return dict(
        db=db,
        emit_session_provisioning_failure=AsyncMock(return_value=None),
        persistent_thread_recycler=None,
    )


# ===========================================================================
# Section 1 — Agent: awaiting_user transitions in persistent_app
# ===========================================================================
#
# _loop_get_user_input flips to awaiting_user when about to block AND no
# subscribers AND turn_count > 0. _subscribe reverts to active on the
# 0→1 subscriber transition.


def _reset_agent_globals():
    import agent.api.persistent_app as mod

    mod._session = None
    mod._thread_id = None
    mod._pinned_status_identity_enabled = False
    mod._pinned_runtime_generation_enabled = False
    mod._session_runtime_generation = None
    mod._session_runtime_attach_token = None
    mod._orchestrator_client = None
    mod._subscribers.clear()
    mod._loop_user_queue = None


def _install_agent_session(*, turn_count: int = 0):
    """Minimal session + orchestrator_client wiring for transition tests."""
    import agent.api.persistent_app as mod

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
        import agent.api.persistent_app as mod

        _install_agent_session(turn_count=2)

        _ = mod._subscribe("client-1")

        # _safe_set_thread_status runs as a task — give it a tick.
        await asyncio.sleep(0)
        mod._orchestrator_client.update_thread_status.assert_awaited_with(
            "thread-test-uuid", "active"
        )

    @pytest.mark.asyncio
    async def test_second_subscriber_does_not_schedule_revert(self):
        import agent.api.persistent_app as mod

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
        import agent.api.persistent_app as mod

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
        import agent.api.persistent_app as mod

        _, client = _install_agent_session(turn_count=0)
        mod._loop_user_queue.put_nowait("hi")

        await mod._loop_get_user_input()
        await asyncio.sleep(0)

        client.update_thread_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_flip_when_subscriber_present(self):
        import agent.api.persistent_app as mod

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

    @pytest.mark.asyncio
    async def test_stateless_eager_uses_durable_presence_oracle(self, monkeypatch):
        import agent.api.persistent_app as mod
        from agent.api.lease_context import LeaseHandle

        session, client = _install_agent_session(turn_count=3)
        session.postgres_conn = MagicMock()
        mod._loop_user_queue.put_nowait("hi")
        monkeypatch.setenv("STATELESS_EXECUTOR", "1")
        durable_pause = AsyncMock(return_value=True)
        monkeypatch.setattr(mod, "mark_stateless_natural_pause", durable_pause)

        handle = LeaseHandle()
        handle.update("thread-test-uuid", 17)
        context_token = mod._current_lease_var.set(handle)
        try:
            await mod._loop_get_user_input()
        finally:
            mod._current_lease_var.reset(context_token)

        durable_pause.assert_awaited_once_with(
            session.postgres_conn,
            thread_id="thread-test-uuid",
            lease_token=17,
            require_untethered=True,
        )
        client.update_thread_status.assert_not_called()


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
        import agent.api.persistent_app as mod

        session, _ = _install_agent_session(turn_count=1)
        db = _make_db(fetchrow={"status": "approved"})
        session.postgres_conn = db
        mod._session = session

        result = await mod._loop_permission_check("run_command", {}, "tc-1")
        assert result is PermissionOutcome.APPROVED
        assert session.tool_decisions.get("tc-1") == "approved"
        # We never reached the INSERT step.
        db._fake_conn.fetchval.assert_not_called()

    @pytest.mark.asyncio
    async def test_reuses_prior_denied_decision(self):
        import agent.api.persistent_app as mod

        session, _ = _install_agent_session(turn_count=1)
        db = _make_db(fetchrow={"status": "denied"})
        session.postgres_conn = db
        mod._session = session

        result = await mod._loop_permission_check("run_command", {}, "tc-2")
        # A prior *explicit* deny replays as DECLINED, not as an unanswered gate.
        assert result is PermissionOutcome.DECLINED
        assert session.tool_decisions.get("tc-2") == "denied"

    @pytest.mark.asyncio
    async def test_no_prior_decision_falls_through_to_insert(self):
        import agent.api.persistent_app as mod

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
        assert result is PermissionOutcome.APPROVED
        assert db._fake_conn.fetchval.await_count >= 1


# ===========================================================================
# Section 3 — Attention-sleep watchdog
# ===========================================================================
#
# attention_sleep_sweeper runs every 60s, selects stale awaiting_user threads,
# and hands their exact live runtime identity to the durable retirement flow,
# which settles the lifecycle to suspended after cleanup.


def _stale_pinned_runtime_row() -> dict:
    return {
        "id": "11111111-1111-4111-8111-111111111111",
        "status": "awaiting_user",
        "execution_lane": "pinned",
        "runtime_generation": "22222222-2222-4222-8222-222222222222",
        "agent_id": "33333333-3333-4333-8333-333333333333",
        "runtime_attach_token": "44444444-4444-4444-8444-444444444444",
    }


class TestAttentionSleepSweeper:
    def setup_method(self):
        import orchestrator.services.attention_sleep as attention_sleep_module

        # R1.B10: db and the retirement factory are explicit parameters now;
        # the remaining collaborators are module globals on the owning service
        # module. Save the originals so each test restores cleanly.
        self._orig_svc = attention_sleep_module.workspace_suspension_service
        self._orig_promote = attention_sleep_module.promote_expired_stateless_pauses
        attention_sleep_module.promote_expired_stateless_pauses = AsyncMock(
            return_value=[]
        )

    def teardown_method(self):
        import orchestrator.services.attention_sleep as attention_sleep_module

        attention_sleep_module.workspace_suspension_service = self._orig_svc
        attention_sleep_module.promote_expired_stateless_pauses = self._orig_promote

    @pytest.mark.asyncio
    async def test_suspends_stale_awaiting_user(self):
        import orchestrator.services.attention_sleep as attention_sleep_module

        row = _stale_pinned_runtime_row()
        db = _make_db(fetch=[row])
        svc = MagicMock()
        svc.is_enabled = True
        end_thread_flow = AsyncMock(return_value={"status": "suspended"})

        attention_sleep_module.workspace_suspension_service = svc

        shutdown = asyncio.Event()
        # Run one tick: schedule the sweeper, give it a moment, signal shutdown.
        task = asyncio.create_task(
            attention_sleep_module.attention_sleep_sweeper(
                shutdown,
                db=db,
                thread_retirement_operations=lambda: MagicMock(
                    end_thread_flow=end_thread_flow
                ),
            )
        )
        await asyncio.sleep(0.05)
        shutdown.set()
        await task

        end_thread_flow.assert_awaited_once_with(
            str(row["id"]),
            row,
            permanent=False,
            force=False,
            expected_runtime_generation=str(row["runtime_generation"]),
            expected_agent_id=str(row["agent_id"]),
            expected_attach_token=str(row["runtime_attach_token"]),
            settle_status="suspended",
        )

    @pytest.mark.asyncio
    async def test_skips_when_service_disabled(self):
        import orchestrator.services.attention_sleep as attention_sleep_module

        db = _make_db(fetch=[{"id": "thread-abc"}])
        svc = MagicMock()
        svc.is_enabled = False
        end_thread_flow = AsyncMock(return_value={"status": "suspended"})

        attention_sleep_module.workspace_suspension_service = svc

        shutdown = asyncio.Event()
        task = asyncio.create_task(
            attention_sleep_module.attention_sleep_sweeper(
                shutdown,
                db=db,
                thread_retirement_operations=lambda: MagicMock(
                    end_thread_flow=end_thread_flow
                ),
            )
        )
        await asyncio.sleep(0.05)
        shutdown.set()
        await task

        end_thread_flow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_handles_suspend_failure_without_crashing(self):
        import orchestrator.services.attention_sleep as attention_sleep_module

        row = _stale_pinned_runtime_row()
        db = _make_db(fetch=[row])
        svc = MagicMock()
        svc.is_enabled = True
        end_thread_flow = AsyncMock(side_effect=RuntimeError("retirement unavailable"))

        attention_sleep_module.workspace_suspension_service = svc

        shutdown = asyncio.Event()
        task = asyncio.create_task(
            attention_sleep_module.attention_sleep_sweeper(
                shutdown,
                db=db,
                thread_retirement_operations=lambda: MagicMock(
                    end_thread_flow=end_thread_flow
                ),
            )
        )
        await asyncio.sleep(0.05)
        shutdown.set()
        await task

        end_thread_flow.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_presence_failure_does_not_skip_legacy_suspension(self):
        import orchestrator.services.attention_sleep as attention_sleep_module

        row = _stale_pinned_runtime_row()
        db = _make_db(fetch=[row])
        svc = MagicMock()
        svc.is_enabled = True
        end_thread_flow = AsyncMock(return_value={"status": "suspended"})
        attention_sleep_module.workspace_suspension_service = svc
        attention_sleep_module.promote_expired_stateless_pauses = AsyncMock(
            side_effect=RuntimeError("presence table unavailable")
        )

        shutdown = asyncio.Event()
        task = asyncio.create_task(
            attention_sleep_module.attention_sleep_sweeper(
                shutdown,
                db=db,
                thread_retirement_operations=lambda: MagicMock(
                    end_thread_flow=end_thread_flow
                ),
            )
        )
        await asyncio.sleep(0.05)
        shutdown.set()
        await task

        end_thread_flow.assert_awaited_once()


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
        from orchestrator.routers import thread_permissions as thread_permissions_routes

        # R1.B10: the store is an explicit dependency now; the email and
        # notification collaborators are module globals on the owning router.
        # Save the originals so each test restores cleanly.
        self._orig_email = thread_permissions_routes.email_service
        self._orig_hn = thread_permissions_routes.headless_notifications

    def teardown_method(self):
        from orchestrator.routers import thread_permissions as thread_permissions_routes

        thread_permissions_routes.email_service = self._orig_email
        thread_permissions_routes.headless_notifications = self._orig_hn

    @pytest.mark.asyncio
    async def test_extended_banner_when_under_cap(self):
        from orchestrator.routers import thread_permissions as thread_permissions_routes

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

        # Stub the email_service so cockpit_url resolves.
        thread_permissions_routes.email_service = MagicMock()
        thread_permissions_routes.email_service.cockpit_url = "http://localhost:4200"

        # Stub validate_magic_link to return a valid token row.
        thread_permissions_routes.headless_notifications = MagicMock()
        thread_permissions_routes.headless_notifications.validate_magic_link = (
            AsyncMock(
                return_value={
                    "id": "tok-1",
                    "approval_id": "appr-1",
                    "thread_id": "thread-abc",
                    "intended_decision": "approved",
                }
            )
        )

        resp = await thread_permissions_routes.magic_link_extend(
            "raw-token", dependencies=_thread_permissions_deps(db)
        )
        body = resp.body.decode()
        assert "extended by 60 minutes" in body.lower() or "Window extended" in body
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cap_reached_banner_when_update_misses(self):
        from orchestrator.routers import thread_permissions as thread_permissions_routes

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
        thread_permissions_routes.email_service = MagicMock()
        thread_permissions_routes.email_service.cockpit_url = "http://localhost:4200"
        thread_permissions_routes.headless_notifications = MagicMock()
        thread_permissions_routes.headless_notifications.validate_magic_link = (
            AsyncMock(
                return_value={
                    "id": "tok-1",
                    "approval_id": "appr-1",
                    "thread_id": "thread-abc",
                    "intended_decision": "approved",
                }
            )
        )

        resp = await thread_permissions_routes.magic_link_extend(
            "raw-token", dependencies=_thread_permissions_deps(db)
        )
        body = resp.body.decode()
        assert "Extend limit reached" in body
        assert "disabled" in body  # extend button disabled

    @pytest.mark.asyncio
    async def test_not_awaiting_banner_when_thread_already_active(self):
        from orchestrator.routers import thread_permissions as thread_permissions_routes

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
        thread_permissions_routes.email_service = MagicMock()
        thread_permissions_routes.email_service.cockpit_url = "http://localhost:4200"
        thread_permissions_routes.headless_notifications = MagicMock()
        thread_permissions_routes.headless_notifications.validate_magic_link = (
            AsyncMock(
                return_value={
                    "id": "tok-1",
                    "approval_id": "appr-1",
                    "thread_id": "thread-abc",
                    "intended_decision": "approved",
                }
            )
        )

        resp = await thread_permissions_routes.magic_link_extend(
            "raw-token", dependencies=_thread_permissions_deps(db)
        )
        body = resp.body.decode()
        assert "No extend needed" in body or "already active" in body

    @pytest.mark.asyncio
    async def test_invalid_token_returns_404(self):
        from orchestrator.routers import thread_permissions as thread_permissions_routes

        db = _make_db()
        thread_permissions_routes.email_service = MagicMock()
        thread_permissions_routes.email_service.cockpit_url = "http://localhost:4200"
        thread_permissions_routes.headless_notifications = MagicMock()
        thread_permissions_routes.headless_notifications.validate_magic_link = (
            AsyncMock(return_value=None)
        )

        resp = await thread_permissions_routes.magic_link_extend(
            "bad-token", dependencies=_thread_permissions_deps(db)
        )
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
        import orchestrator.services.thread_permissions as thread_permissions_module

        # R1.B10: db and the wake collaborators are explicit parameters now;
        # the provisioning collaborators are module globals on the owning
        # service module. Save the originals so each test restores cleanly.
        self._orig_svc = thread_permissions_module.workspace_suspension_service
        self._orig_prov = thread_permissions_module.persistent_provisioner
        self._orig_container_prov = thread_permissions_module.container_provisioner
        self._orig_ensure = thread_permissions_module.ensure_session_workspace

    def teardown_method(self):
        import orchestrator.services.thread_permissions as thread_permissions_module

        thread_permissions_module.workspace_suspension_service = self._orig_svc
        thread_permissions_module.persistent_provisioner = self._orig_prov
        thread_permissions_module.container_provisioner = self._orig_container_prov
        thread_permissions_module.ensure_session_workspace = self._orig_ensure

    @staticmethod
    def _stateless_thread(
        backend: str,
        *,
        status: str = "suspended",
        lane: str = "stateless",
        agent_id=None,
    ):
        metadata = {
            "config_override": {
                "workspace": {"backend": backend},
                "officer": {"enabled": False, "conference": False},
            }
        }
        if backend == "sandbox":
            metadata["workspace_container"] = {
                "status": "suspended",
                "provisioner": "k8s",
            }
        return {
            "id": "thread-abc",
            "execution_lane": lane,
            "agent_id": agent_id,
            "status": status,
            "metadata": metadata,
        }

    @staticmethod
    def _stateless_db(thread, queue, *, decision="approved", update="thread-abc"):
        def fetchrow(sql, *_args):
            if "FROM threads" in sql:
                return thread
            if "FROM run_queue" in sql:
                return queue
            raise AssertionError(f"unexpected fetchrow SQL: {sql}")

        def fetchval(sql, *_args):
            if "FROM thread_permission_requests" in sql:
                return decision
            if "UPDATE threads" in sql:
                return update
            raise AssertionError(f"unexpected fetchval SQL: {sql}")

        inner = _make_db(fetchrow=fetchrow, fetchval=fetchval)
        db = MagicMock()
        db.get_thread = AsyncMock(return_value=thread)
        db.acquire = inner.acquire
        return db, inner

    @pytest.mark.asyncio
    async def test_magic_post_carries_exact_permission_fence(self, monkeypatch):
        from orchestrator.routers import thread_permissions as thread_permissions_routes

        permission_row = {
            "id": "permission-1",
            "status": "approved",
            "tool_call_id": "tool-call-1",
            "tool_name": "run_command",
            "thread_id": "thread-abc",
        }
        db = _make_db(fetchrow=permission_row)
        deps = _thread_permissions_deps(db)
        notifications = MagicMock()
        notifications.validate_magic_link = AsyncMock(
            return_value={
                "id": "token-row-1",
                "approval_id": "permission-1",
                "intended_decision": "approved",
            }
        )
        notifications.consume_magic_link = AsyncMock(
            return_value={
                "approval_id": "permission-1",
                "user_id": "user-1",
            }
        )
        monkeypatch.setattr(
            thread_permissions_routes, "headless_notifications", notifications
        )
        email = MagicMock()
        email.cockpit_url = "http://localhost:4200"
        monkeypatch.setattr(thread_permissions_routes, "email_service", email)
        wake = AsyncMock()
        monkeypatch.setattr(
            thread_permissions_routes, "_phase5_wake_if_suspended", wake
        )

        response = await thread_permissions_routes.magic_link_post(
            "raw-token", dependencies=deps
        )
        await asyncio.sleep(0)

        assert response.status_code == 200
        wake.assert_awaited_once_with(
            "thread-abc",
            permission_request_id="permission-1",
            db=db,
            emit_session_provisioning_failure=deps.emit_session_provisioning_failure,
            persistent_thread_recycler=None,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", ["sandbox", "virtual", "none"])
    @pytest.mark.parametrize("decision", ["approved", "denied"])
    async def test_stateless_wake_is_queue_owned_and_topology_neutral(
        self, backend, decision
    ):
        import orchestrator.services.thread_permissions as thread_permissions_module

        thread = self._stateless_thread(backend)
        queue = {"state": "leased", "input_seq": 12, "consumed_seq": 11}
        db, inner = self._stateless_db(thread, queue, decision=decision)

        thread_permissions_module.workspace_suspension_service = MagicMock()
        thread_permissions_module.container_provisioner = MagicMock()
        ensure_workspace = AsyncMock(return_value=None)
        thread_permissions_module.ensure_session_workspace = ensure_workspace
        prov = MagicMock()
        prov.create_agent_pod = AsyncMock(return_value=True)
        thread_permissions_module.persistent_provisioner = prov

        await thread_permissions_module._phase5_wake_if_suspended(
            "thread-abc",
            permission_request_id="permission-1",
            **_wake_kwargs(db),
        )

        ensure_workspace.assert_awaited_once_with(
            "thread-abc",
            db=db,
            provisioner=thread_permissions_module.container_provisioner,
            suspension=thread_permissions_module.workspace_suspension_service,
        )
        prov.create_agent_pod.assert_not_awaited()
        calls = inner._fake_conn.fetchrow.await_args_list
        assert "FROM threads" in calls[0].args[0]
        assert "FOR UPDATE" in calls[0].args[0]
        assert "FROM run_queue" in calls[1].args[0]
        assert "FOR UPDATE" in calls[1].args[0]
        assert not any(
            "UPDATE run_queue" in call.args[0]
            for call in inner._fake_conn.fetchval.await_args_list
        )
        update_sql = next(
            call.args[0]
            for call in inner._fake_conn.fetchval.await_args_list
            if "UPDATE threads" in call.args[0]
        )
        assert "execution_lane = $2" in update_sql
        assert "agent_id IS NULL" in update_sql
        assert "control_admission_agent_id = NULL" in update_sql

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("queue", "decision"),
        [
            ({"state": "done", "input_seq": 12, "consumed_seq": 12}, "approved"),
            ({"state": "parked", "input_seq": 12, "consumed_seq": 11}, "approved"),
            (None, "approved"),
            ({"state": "queued", "input_seq": 12, "consumed_seq": 11}, None),
        ],
    )
    async def test_stateless_wake_fences_stale_queue_or_permission(
        self, queue, decision
    ):
        import orchestrator.services.thread_permissions as thread_permissions_module

        thread = self._stateless_thread("virtual")
        db, inner = self._stateless_db(thread, queue, decision=decision)
        thread_permissions_module.ensure_session_workspace = AsyncMock()
        thread_permissions_module.persistent_provisioner = MagicMock()
        thread_permissions_module.persistent_provisioner.create_agent_pod = AsyncMock()

        await thread_permissions_module._phase5_wake_if_suspended(
            "thread-abc",
            permission_request_id="permission-1",
            **_wake_kwargs(db),
        )

        thread_permissions_module.ensure_session_workspace.assert_not_awaited()
        thread_permissions_module.persistent_provisioner.create_agent_pod.assert_not_awaited()
        assert not any(
            "UPDATE threads" in call.args[0]
            for call in inner._fake_conn.fetchval.await_args_list
        )

    @pytest.mark.asyncio
    async def test_stateless_wake_refuses_lane_flip_under_thread_lock(self):
        import orchestrator.services.thread_permissions as thread_permissions_module

        initial = self._stateless_thread("virtual")
        locked = self._stateless_thread("virtual", lane="pinned")
        db, inner = self._stateless_db(
            locked,
            {"state": "queued", "input_seq": 12, "consumed_seq": 11},
        )
        db.get_thread = AsyncMock(return_value=initial)
        thread_permissions_module.ensure_session_workspace = AsyncMock()
        thread_permissions_module.persistent_provisioner = MagicMock()
        thread_permissions_module.persistent_provisioner.create_agent_pod = AsyncMock()

        await thread_permissions_module._phase5_wake_if_suspended(
            "thread-abc",
            permission_request_id="permission-1",
            **_wake_kwargs(db),
        )

        thread_permissions_module.ensure_session_workspace.assert_not_awaited()
        thread_permissions_module.persistent_provisioner.create_agent_pod.assert_not_awaited()
        assert len(inner._fake_conn.fetchrow.await_args_list) == 1

    @pytest.mark.asyncio
    async def test_stateless_wake_refuses_unfenced_direct_call(self):
        import orchestrator.services.thread_permissions as thread_permissions_module

        thread = self._stateless_thread("none")
        db, inner = self._stateless_db(
            thread,
            {"state": "queued", "input_seq": 12, "consumed_seq": 11},
        )
        thread_permissions_module.ensure_session_workspace = AsyncMock()

        await thread_permissions_module._phase5_wake_if_suspended(
            "thread-abc", **_wake_kwargs(db)
        )

        thread_permissions_module.ensure_session_workspace.assert_not_awaited()
        inner._fake_conn.fetchrow.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stateless_officer_drift_stays_on_pinned_wake_plane(self):
        import orchestrator.services.thread_permissions as thread_permissions_module

        thread = self._stateless_thread("none")
        thread["metadata"]["config_override"]["officer"]["enabled"] = True
        db, inner = self._stateless_db(
            thread,
            {"state": "queued", "input_seq": 12, "consumed_seq": 11},
        )
        thread_permissions_module.ensure_session_workspace = AsyncMock()
        thread_permissions_module.persistent_provisioner = MagicMock()
        thread_permissions_module.persistent_provisioner.create_agent_pod = AsyncMock()

        await thread_permissions_module._phase5_wake_if_suspended(
            "thread-abc",
            permission_request_id="permission-1",
            **_wake_kwargs(db),
        )

        thread_permissions_module.ensure_session_workspace.assert_not_awaited()
        thread_permissions_module.persistent_provisioner.create_agent_pod.assert_not_awaited()
        assert len(inner._fake_conn.fetchrow.await_args_list) == 1

    @pytest.mark.asyncio
    async def test_restores_when_workspace_suspended(self):
        import orchestrator.services.thread_permissions as thread_permissions_module

        thread = _stale_pinned_runtime_row()
        thread.update(
            {
                "status": "suspended",
                "agent_id": None,
                "runtime_attach_token": None,
                "config_name": "persistent_defaults",
                "metadata": {"workspace_container": {"status": "suspended"}},
            }
        )
        thread_id = str(thread["id"])
        db = MagicMock()
        db.get_thread = AsyncMock(return_value=thread)
        db_inner = _make_db(fetchval=thread_id)
        db.acquire = db_inner.acquire

        svc = MagicMock()
        svc.is_enabled = True
        thread_permissions_module.workspace_suspension_service = svc
        ensure_workspace = AsyncMock(
            return_value=MagicMock(outcome=EnsureOutcome.READY)
        )
        thread_permissions_module.ensure_session_workspace = ensure_workspace

        prov = MagicMock()
        prov.create_agent_pod = AsyncMock(return_value=MagicMock(usable=True))
        thread_permissions_module.persistent_provisioner = prov

        await thread_permissions_module._phase5_wake_if_suspended(
            thread_id, **_wake_kwargs(db)
        )
        # Give the inner create_task room.
        await asyncio.sleep(0)

        ensure_workspace.assert_awaited_once_with(
            thread_id,
            db=db,
            provisioner=thread_permissions_module.container_provisioner,
            suspension=svc,
            expected_runtime_generation=str(thread["runtime_generation"]),
        )
        prov.create_agent_pod.assert_awaited_once_with(
            thread_id,
            config_name="session_base",
            expected_runtime_generation=str(thread["runtime_generation"]),
        )
        wake_sql = " ".join(db_inner._fake_conn.fetchval.await_args.args[0].split())
        assert "status = 'active'" in wake_sql
        assert "runtime_generation=$2::uuid" in wake_sql
        assert "runtime_retirement_token IS NULL" in wake_sql
        assert "control_admission_agent_id = NULL" in wake_sql

    @pytest.mark.asyncio
    async def test_skips_when_workspace_not_suspended(self):
        import orchestrator.services.thread_permissions as thread_permissions_module

        thread = _stale_pinned_runtime_row()
        thread.update(
            {
                "status": "active",
                "metadata": {"workspace_container": {"status": "ready"}},
            }
        )
        thread_id = str(thread["id"])
        db = MagicMock()
        db.get_thread = AsyncMock(return_value=thread)
        db_inner = _make_db()
        db.acquire = db_inner.acquire

        svc = MagicMock()
        svc.is_enabled = True
        thread_permissions_module.workspace_suspension_service = svc
        ensure_workspace = AsyncMock()
        thread_permissions_module.ensure_session_workspace = ensure_workspace

        prov = MagicMock()
        prov.create_agent_pod = AsyncMock()
        thread_permissions_module.persistent_provisioner = prov

        await thread_permissions_module._phase5_wake_if_suspended(
            thread_id, **_wake_kwargs(db)
        )
        await asyncio.sleep(0)

        ensure_workspace.assert_not_awaited()
        # agent_id is already bound; no need to re-create.
        prov.create_agent_pod.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_missing_thread_gracefully(self):
        import orchestrator.services.thread_permissions as thread_permissions_module

        db = MagicMock()
        db.get_thread = AsyncMock(return_value=None)
        db.acquire = _make_db().acquire
        thread_permissions_module.workspace_suspension_service = MagicMock()
        thread_permissions_module.persistent_provisioner = MagicMock()

        # Should not raise.
        await thread_permissions_module._phase5_wake_if_suspended(
            "missing-thread", **_wake_kwargs(db)
        )
