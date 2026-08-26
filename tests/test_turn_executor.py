"""Mock-based tests for the M3 stateless turn executor (turn_executor.py).

The run_queue substrate itself is covered by the real-Postgres suite
(tests/test_run_queue.py); everything here fakes the queue functions and the
persistent_app machinery and asserts the DRIVER's behavior: the claim →
bundle → attach → inject → complete flow, skip-if-answered, the error/release
paths, the lease-lost polite abort, the strip-restored-pending helper, the
scrub-on-claim acceptance (§5.6), soft affinity, and the fenced persist in
postgres_db.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest

import src.api.persistent_app as pa
import src.api.turn_executor as te
from src.api.lease_context import (
    LeaseHandle,
    LeaseLostError,
    current_lease,
    get_current_lease,
)
from src.api.orchestrator_client import ClaimBundleError
from src.shared.run_queue import ClaimedUnit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_claim(
    unit_id=None,
    token: int = 3,
    input_seq: Optional[int] = 10,
    consumed_seq: Optional[int] = None,
    control_input_seq: int = 0,
    control_consumed_seq: int = 0,
    attempts: int = 1,
) -> ClaimedUnit:
    return ClaimedUnit(
        unit_id=unit_id or uuid4(),
        unit_kind="session_turn",
        fair_key=None,
        lease_token=token,
        input_seq=input_seq,
        consumed_seq=consumed_seq,
        attempts_since_completion=attempts,
        leased_until=datetime.now(timezone.utc),
        control_input_seq=control_input_seq,
        control_consumed_seq=control_consumed_seq,
    )


class FakeDB:
    """Stands in for the agent's PostgresDB pool wrapper."""

    def __init__(self, pending_rows: Optional[List[Dict[str, Any]]] = None):
        self.pending_rows = list(pending_rows or [])
        self.fetch_calls: List[tuple] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        next_turn = int(getattr(pa._session, "turn_count", 0) or 0) + 1
        return [
            {**row, "turn_number": row.get("turn_number", next_turn)}
            for row in self.pending_rows
        ]

    async def fetchval(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        if sql == te._PENDING_EVENT_EXISTS_SQL:
            return any(row.get("delivery_id") for row in self.pending_rows)
        return None


class FakeSession:
    def __init__(
        self,
        shell_owner_tokens: Optional[List[int]] = None,
        *,
        stateless_warm_reuse_safe: bool = True,
    ):
        self.messages: List[Any] = []
        self.postgres_conn = None
        self._shell_owner_tokens = shell_owner_tokens
        self.shell_owner_tokens: List[int] = []
        self.stateless_warm_reuse_safe = stateless_warm_reuse_safe
        self.turn_count = 0

    def set_shell_owner_token(self, token: int) -> None:
        self.shell_owner_tokens.append(token)
        if self._shell_owner_tokens is not None:
            self._shell_owner_tokens.append(token)


class Harness:
    """Wires fake run_queue functions + fake persistent_app machinery."""

    def __init__(self, monkeypatch, pending_rows=None):
        self.calls: Dict[str, List[Dict[str, Any]]] = {
            "claim": [],
            "complete": [],
            "heartbeat": [],
            "release": [],
            "bundle": [],
            "attach": [],
            "terminate": [],
            "control_start": [],
            "control_stop": [],
            "interrupt_start": [],
            "interrupt_open": [],
            "interrupt_close": [],
            "interrupt_stop": [],
            "interrupt_drain": [],
            "interrupt_stale": [],
            "shell_owner_token": [],
        }
        self.consumed: List[Dict[str, Any]] = []
        self.interrupt_order: List[str] = []
        self.db = FakeDB(pending_rows)
        self.loop_behavior = "complete"  # complete | hang | die
        self.stale_result = (0, None)
        self.restored_turn_count = 0
        self.heartbeat_result: Any = datetime.now(timezone.utc)
        self.bundle_error: Optional[Exception] = None
        self.attach_error: Optional[Exception] = None
        self.stateless_warm_reuse_safe = True
        self.sessions: List[FakeSession] = []
        self._fake_loop_tasks: List[asyncio.Task] = []

        pa._agent = SimpleNamespace(postgres_conn=self.db)
        pa._session = None
        pa._thread_id = None
        pa._loop_user_queue = None
        pa._loop_task = None
        pa._turn_start_external_hook = None
        pa._turn_complete_external_hook = None
        pa._interrupt_watcher_task = None
        pa._interrupt_watcher_stop = None
        pa._interrupt_owner_lease_token = None
        pa._interrupt_owner_turn_id = None
        pa._tool_inflight = False
        pa._loop_interrupt_flag = None
        pa._hard_interrupt_event = asyncio.Event()
        pa._pending_cloud_push_task = None

        harness = self

        async def fake_complete(db, *, unit_id, lease_token, consumed_seq):
            harness.calls["complete"].append(
                {
                    "unit_id": unit_id,
                    "lease_token": lease_token,
                    "consumed_seq": consumed_seq,
                }
            )
            return "done"

        async def fake_release(
            db, *, unit_id, lease_token, backoff_seconds=0.0, error=False
        ):
            harness.calls["release"].append(
                {
                    "unit_id": unit_id,
                    "lease_token": lease_token,
                    "backoff_seconds": backoff_seconds,
                    "error": error,
                }
            )
            return "queued"

        async def fake_heartbeat(db, *, unit_id, lease_token, lease_ttl_seconds=60.0):
            harness.calls["heartbeat"].append(
                {"unit_id": unit_id, "lease_token": lease_token}
            )
            return harness.heartbeat_result

        monkeypatch.setattr(te, "complete_unit", fake_complete)
        monkeypatch.setattr(te, "release_unit", fake_release)
        monkeypatch.setattr(te, "heartbeat_unit", fake_heartbeat)

        async def fake_open_interrupt(db, *, unit_id, lease_token, turn_id):
            harness.interrupt_order.append("open")
            harness.calls["interrupt_open"].append(
                {
                    "unit_id": unit_id,
                    "lease_token": lease_token,
                    "turn_id": turn_id,
                }
            )
            return True

        async def fake_close_interrupt(
            db, *, unit_id, lease_token, turn_id, completed_input_seq=None
        ):
            harness.interrupt_order.append("close")
            harness.calls["interrupt_close"].append(
                {
                    "unit_id": unit_id,
                    "lease_token": lease_token,
                    "turn_id": turn_id,
                    "completed_input_seq": completed_input_seq,
                }
            )
            return True

        monkeypatch.setattr(te, "open_interrupt_admission", fake_open_interrupt)
        monkeypatch.setattr(te, "close_interrupt_admission", fake_close_interrupt)

        # --- fake orchestrator client -------------------------------------
        class FakeClient:
            async def get_claim_bundle(self, unit_id, lease_token):
                harness.calls["bundle"].append(
                    {"unit_id": unit_id, "lease_token": lease_token}
                )
                if harness.bundle_error is not None:
                    raise harness.bundle_error
                return {
                    "unit_id": unit_id,
                    "thread_id": unit_id,
                    "unit_kind": "session_turn",
                    "execution_lane": "stateless",
                    "watermarks": {"input_seq": None, "consumed_seq": None},
                    "attach": harness.attach_for(unit_id),
                }

        pa._orchestrator_client = FakeClient()
        self._attach_overrides: Dict[str, Dict[str, Any]] = {}

        # --- fake persistent_app machinery --------------------------------
        async def fake_attach(**kwargs):
            harness.calls["attach"].append(kwargs)
            if harness.attach_error is not None:
                raise harness.attach_error
            pa._session = FakeSession(
                harness.calls["shell_owner_token"],
                stateless_warm_reuse_safe=harness.stateless_warm_reuse_safe,
            )
            pa._session.turn_count = harness.restored_turn_count
            harness.sessions.append(pa._session)
            pa._thread_id = kwargs.get("thread_id")
            pa._loop_user_queue = asyncio.Queue()

        async def fake_terminate(
            reason,
            *,
            mark_thread=True,
            preserve_shell=None,
            preserve_workspace_daemons=False,
        ):
            harness.calls["terminate"].append(
                {
                    "reason": reason,
                    "mark_thread": mark_thread,
                    "preserve_shell": preserve_shell,
                    "preserve_workspace_daemons": preserve_workspace_daemons,
                }
            )
            task = pa._loop_task
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            pa._loop_task = None
            pa._session = None
            pa._thread_id = None
            pa._loop_user_queue = None

        def fake_ensure(source, client_id=None):
            if pa._loop_user_queue is None:
                return False
            if pa._loop_task is None or pa._loop_task.done():
                pa._loop_task = asyncio.create_task(harness._fake_loop())
                harness._fake_loop_tasks.append(pa._loop_task)
            return True

        async def fake_start_control_watcher(*, lease_token=None, agent_id=None):
            harness.calls["control_start"].append(
                {"lease_token": lease_token, "agent_id": agent_id}
            )
            return 0

        async def fake_stop_control_watcher():
            harness.calls["control_stop"].append({})

        async def fake_start_interrupt_watcher(*, lease_token, target_turn_id):
            harness.interrupt_order.append("start")
            harness.calls["interrupt_start"].append(
                {"lease_token": lease_token, "target_turn_id": target_turn_id}
            )
            pa._interrupt_owner_lease_token = lease_token
            pa._interrupt_owner_turn_id = target_turn_id
            return 0

        async def fake_stop_interrupt_watcher():
            harness.interrupt_order.append("stop")
            harness.calls["interrupt_stop"].append({})
            pa._interrupt_owner_lease_token = None
            pa._interrupt_owner_turn_id = None

        async def fake_drain_interrupts(*, lease_token, target_turn_id):
            harness.interrupt_order.append("drain")
            harness.calls["interrupt_drain"].append(
                {"lease_token": lease_token, "target_turn_id": target_turn_id}
            )
            return 0

        async def fake_reconcile_stale_interrupts(*, lease_token):
            harness.interrupt_order.append("stale")
            harness.calls["interrupt_stale"].append({"lease_token": lease_token})
            return harness.stale_result

        monkeypatch.setattr(pa, "_attach_session", fake_attach)
        monkeypatch.setattr(pa, "_terminate_session", fake_terminate)
        monkeypatch.setattr(pa, "_ensure_persistent_loop_started", fake_ensure)
        monkeypatch.setattr(
            pa, "_start_thread_control_watcher", fake_start_control_watcher
        )
        monkeypatch.setattr(
            pa, "_stop_thread_control_watcher", fake_stop_control_watcher
        )
        monkeypatch.setattr(
            pa, "_start_thread_interrupt_watcher", fake_start_interrupt_watcher
        )
        monkeypatch.setattr(
            pa, "_stop_thread_interrupt_watcher", fake_stop_interrupt_watcher
        )
        monkeypatch.setattr(pa, "_drain_thread_interrupts", fake_drain_interrupts)
        monkeypatch.setattr(
            pa,
            "_reconcile_stale_thread_interrupts",
            fake_reconcile_stale_interrupts,
        )

        self.executor = te.StatelessTurnExecutor(
            pod_name="test-pod", abort_grace_seconds=0.05
        )

    def attach_for(self, unit_id: str) -> Dict[str, Any]:
        return self._attach_overrides.get(
            unit_id,
            {
                "thread_id": unit_id,
                "config_override": None,
                "resolved_config": {"agent": {"llm": {"model": "m"}}},
                "project_ids": [],
                "datasources": None,
                "config_name": "session_base",
            },
        )

    def set_attach(self, unit_id: str, attach: Dict[str, Any]) -> None:
        self._attach_overrides[str(unit_id)] = attach

    async def _fake_loop(self):
        while True:
            item = await pa._loop_user_queue.get()
            self.consumed.append(item)
            turn_id = int(pa._session.turn_count) + 1
            pa._session.turn_count = turn_id
            start_hook = pa._turn_start_external_hook
            if start_hook is not None:
                await start_hook(turn_id)
            if self.loop_behavior == "hang":
                await asyncio.sleep(3600)
            elif self.loop_behavior == "die":
                raise RuntimeError("scripted turn failure")
            else:
                hook = pa._turn_complete_external_hook
                if hook is not None:
                    hook(turn_id)

    async def cleanup(self):
        for task in self._fake_loop_tasks:
            if not task.done():
                task.cancel()
            # Await even already-done tasks so a scripted failure's exception
            # is retrieved (no "Task exception was never retrieved" noise).
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


_PA_SAVED_ATTRS = (
    "_session",
    "_thread_id",
    "_loop_user_queue",
    "_loop_task",
    "_turn_start_external_hook",
    "_turn_complete_external_hook",
    "_interrupt_watcher_task",
    "_interrupt_watcher_stop",
    "_interrupt_owner_lease_token",
    "_interrupt_owner_turn_id",
    "_agent",
    "_orchestrator_client",
    "_tool_inflight",
    "_loop_interrupt_flag",
    "_hard_interrupt_event",
    "_pending_cloud_push_task",
)


@pytest.fixture
def harness(monkeypatch):
    saved = {name: getattr(pa, name) for name in _PA_SAVED_ATTRS}
    h = Harness(monkeypatch)
    try:
        yield h
    finally:
        for name, value in saved.items():
            setattr(pa, name, value)


async def _finish(h: Harness):
    await h.cleanup()


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_claim_bundle_attach_inject_complete(self, harness):
        unit = uuid4()
        row_id = str(uuid4())
        harness.db.pending_rows = [{"id": row_id, "seq": 5, "content": "hello"}]
        claim = make_claim(unit_id=unit, token=7, input_seq=5, consumed_seq=None)

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        # Bundle fetched with the claim's identity.
        assert harness.calls["bundle"] == [{"unit_id": str(unit), "lease_token": 7}]
        # Attach fed the bundle's attach object unchanged.
        assert len(harness.calls["attach"]) == 1
        assert harness.calls["attach"][0]["thread_id"] == str(unit)
        assert harness.calls["attach"][0]["config_name"] == "session_base"
        # The injected item carried the DB row id + content (no re-persist).
        assert harness.consumed == [{"content": "hello", "id": row_id}]
        # Completion carried the injected message's seq.
        assert harness.calls["complete"] == [
            {"unit_id": unit, "lease_token": 7, "consumed_seq": 5}
        ]
        assert not harness.calls["release"]
        # Affinity hint updated for the next poll.
        assert harness.executor._prefer_unit_id == unit
        # Lease handle points at this claim (fenced writers read it live).
        assert get_current_lease() is None  # var only set inside run()
        assert harness.executor._lease.unit_id == str(unit)
        assert harness.executor._lease.lease_token == 7
        assert harness.calls["shell_owner_token"] == [7]
        assert harness.interrupt_order == [
            "stale",
            "start",
            "open",
            "drain",
            "close",
            "stop",
            "drain",
            "stop",  # idempotent claim-finally belt
        ]

    @pytest.mark.asyncio
    async def test_event_input_keeps_role_and_stable_delivery_identity(self, harness):
        unit = uuid4()
        row_id = str(uuid4())
        delivery_id = str(uuid4())
        harness.db.pending_rows = [
            {
                "id": row_id,
                "seq": 5,
                "content": "[wake] inspect the job",
                "turn_number": 1,
                "role": "event",
                "delivery_id": delivery_id,
            }
        ]
        claim_delivery = AsyncMock(
            return_value={
                "message_id": row_id,
                "seq": 5,
                "claim_generation": 9,
            }
        )
        harness.db.claim_stateless_input_delivery = claim_delivery

        await harness.executor._serve_claim(
            # A pre-0185 wake can sit below the old human-only watermark. The
            # ledger identity, not seq>consumed alone, keeps it executable.
            make_claim(unit_id=unit, token=7, input_seq=5, consumed_seq=5)
        )
        await _finish(harness)

        claim_delivery.assert_awaited_once_with(
            thread_id=str(unit),
            delivery_id=delivery_id,
            lease_token=7,
            executor_id="test-pod",
            pod_uid=harness.executor._pod_uid,
        )
        assert harness.consumed == [
            {
                "content": "[wake] inspect the job",
                "id": row_id,
                "role": "event",
                "delivery_id": delivery_id,
                "claim_generation": 9,
            }
        ]
        assert harness.calls["complete"] == [
            {"unit_id": unit, "lease_token": 7, "consumed_seq": 5}
        ]

    @pytest.mark.asyncio
    async def test_recovered_interrupted_input_is_never_injected(self, harness):
        unit = uuid4()
        stopped_id = str(uuid4())
        newer_id = str(uuid4())
        claim = make_claim(unit_id=unit, token=10, input_seq=9, consumed_seq=1)
        harness.stale_result = (1, 5)
        fetch_pending = AsyncMock(
            return_value=[
                {
                    "id": newer_id,
                    "seq": 9,
                    "content": "newer",
                    "turn_number": 1,
                }
            ]
        )

        with patch.object(harness.executor, "_fetch_pending_rows", fetch_pending):
            await harness.executor._serve_claim(claim)
        await _finish(harness)

        fetch_pending.assert_awaited_once_with(str(unit), 5)
        assert harness.consumed == [{"content": "newer", "id": newer_id}]
        assert all(item["id"] != stopped_id for item in harness.consumed)
        assert harness.calls["complete"] == [
            {"unit_id": unit, "lease_token": 10, "consumed_seq": 9}
        ]

    @pytest.mark.asyncio
    async def test_fresh_restore_rewinds_pending_turn_before_admission(self, harness):
        """Admission targets the durable row even if persist never runs."""

        unit = uuid4()
        row_id = str(uuid4())
        harness.restored_turn_count = 5
        harness.db.pending_rows = [
            {
                "id": row_id,
                "seq": 17,
                "content": "stop-safe",
                "turn_number": 5,
            }
        ]

        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=7, input_seq=17, consumed_seq=16)
        )
        await _finish(harness)

        assert harness.calls["interrupt_open"] == [
            {"unit_id": unit, "lease_token": 7, "turn_id": 5}
        ]
        assert harness.calls["interrupt_start"] == [
            {"lease_token": 7, "target_turn_id": 5}
        ]
        # The harness loop deliberately never calls persist_message: this is
        # the admission-before-persist crash window itself.
        assert harness.sessions[0].turn_count == 5

    @pytest.mark.asyncio
    async def test_failed_post_open_drain_closes_stops_and_final_drains(self, harness):
        unit = uuid4()
        claim = make_claim(unit_id=unit, token=12, input_seq=1)
        harness.executor._lease.update(unit, 12)
        drain = AsyncMock(side_effect=[RuntimeError("first drain failed"), 0])

        with patch.object(pa, "_drain_thread_interrupts", drain):
            with pytest.raises(RuntimeError, match="first drain failed"):
                await harness.executor._arm_interrupt_window(
                    pa,
                    claim,
                    target_turn_id=3,
                )

        assert harness.interrupt_order == [
            "start",
            "open",
            "close",
            "stop",
        ]
        assert drain.await_args_list == [
            call(lease_token=12, target_turn_id=3),
            call(lease_token=12, target_turn_id=3),
        ]
        assert not harness.executor._lease.lost.is_set()

    @pytest.mark.asyncio
    async def test_failed_final_drain_forces_no_release(self, harness):
        unit = uuid4()
        claim = make_claim(unit_id=unit, token=12, input_seq=1)
        harness.executor._lease.update(unit, 12)
        drain = AsyncMock(side_effect=RuntimeError("database unavailable"))

        with patch.object(pa, "_drain_thread_interrupts", drain):
            with pytest.raises(RuntimeError, match="database unavailable"):
                await harness.executor._arm_interrupt_window(
                    pa,
                    claim,
                    target_turn_id=3,
                )

        assert harness.executor._lease.lost.is_set()
        await harness.executor._release(claim, reason="arm_failed")
        assert not harness.calls["release"]

    @pytest.mark.asyncio
    async def test_no_pending_input_completes_with_fallback(self, harness):
        unit = uuid4()
        harness.db.pending_rows = []
        claim = make_claim(unit_id=unit, token=2, input_seq=9, consumed_seq=4)

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        # COALESCE(input_seq, consumed_seq, 0) → input_seq.
        assert harness.calls["complete"] == [
            {"unit_id": unit, "lease_token": 2, "consumed_seq": 9}
        ]
        assert not harness.calls["release"]


# ---------------------------------------------------------------------------
# 2. Skip-if-answered
# ---------------------------------------------------------------------------


class TestSkipIfAnswered:
    @pytest.mark.asyncio
    async def test_consumed_at_or_past_input_completes_immediately(self, harness):
        unit = uuid4()
        claim = make_claim(unit_id=unit, token=4, input_seq=7, consumed_seq=7)

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        assert harness.calls["complete"] == [
            {"unit_id": unit, "lease_token": 4, "consumed_seq": 7}
        ]
        # No LLM path: no bundle fetch, no attach, no injection.
        assert not harness.calls["bundle"]
        assert not harness.calls["attach"]
        assert not harness.consumed
        # There is no attached session to keep warm on this no-LLM path.
        assert harness.executor._prefer_unit_id is None

    @pytest.mark.asyncio
    async def test_pending_control_bypasses_skip_and_claims_without_human_input(
        self, harness
    ):
        unit = uuid4()
        harness.db.pending_rows = []
        claim = make_claim(
            unit_id=unit,
            token=5,
            input_seq=7,
            consumed_seq=7,
            control_input_seq=2,
            control_consumed_seq=1,
        )

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        assert harness.calls["bundle"]
        assert harness.calls["attach"]
        assert harness.calls["control_start"] == [{"lease_token": 5, "agent_id": None}]
        assert harness.calls["complete"] == [
            {"unit_id": unit, "lease_token": 5, "consumed_seq": 7}
        ]


# ---------------------------------------------------------------------------
# 3. Bundle failures
# ---------------------------------------------------------------------------


class TestBundleFailures:
    @pytest.mark.asyncio
    async def test_bundle_403_releases_with_error_and_continues(self, harness):
        harness.bundle_error = ClaimBundleError(403, "token mismatch")
        claim = make_claim(token=3)

        await harness.executor._serve_claim(claim)  # must not raise
        await _finish(harness)

        assert len(harness.calls["release"]) == 1
        assert harness.calls["release"][0]["error"] is True
        assert not harness.calls["attach"]
        assert not harness.calls["complete"]

    @pytest.mark.asyncio
    async def test_bundle_404_drops_without_release(self, harness):
        harness.bundle_error = ClaimBundleError(404, "gone")
        claim = make_claim()

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        assert not harness.calls["release"]
        assert not harness.calls["attach"]
        assert not harness.calls["complete"]

    @pytest.mark.asyncio
    async def test_bundle_network_error_releases(self, harness):
        harness.bundle_error = ConnectionError("orchestrator down")
        claim = make_claim()

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        assert len(harness.calls["release"]) == 1
        assert harness.calls["release"][0]["error"] is True


# ---------------------------------------------------------------------------
# 4. Turn error → release + loop continues
# ---------------------------------------------------------------------------


class TestTurnError:
    @pytest.mark.asyncio
    async def test_physical_session_detaches_before_queue_release(self, harness):
        unit = uuid4()
        claim = make_claim(unit_id=unit, token=31, input_seq=1)
        pa._session = FakeSession(stateless_warm_reuse_safe=False)
        order = []

        async def detach(reason):
            order.append(("detach", reason))
            pa._session = None

        async def release(*_args, **_kwargs):
            order.append(("release", None))
            return "queued"

        with (
            patch.object(
                harness.executor, "_detach_cached_session", side_effect=detach
            ),
            patch.object(te, "release_unit", side_effect=release),
        ):
            await harness.executor._release(claim, reason="turn_error")

        assert order == [
            ("detach", "release_turn_error"),
            ("release", None),
        ]

    @pytest.mark.asyncio
    async def test_failed_terminate_keeps_claimant_attached_and_blocks_transition(
        self, harness
    ):
        backend = MagicMock()
        session = SimpleNamespace(
            stateless_warm_reuse_safe=False,
            cleanup=AsyncMock(),
            retire_shell_owner=MagicMock(),
            _unwrapped_backend=MagicMock(return_value=backend),
        )
        pa._session = session
        pa._thread_id = "physical-thread"

        with patch.object(
            pa,
            "_terminate_session",
            new=AsyncMock(side_effect=RuntimeError("journal drain failed")),
        ):
            with pytest.raises(RuntimeError, match="journal drain failed"):
                await harness.executor._detach_cached_session("turn_error")

        session.cleanup.assert_not_awaited()
        session.retire_shell_owner.assert_not_called()
        backend.retire.assert_not_called()
        assert pa._session is session
        assert pa._thread_id == "physical-thread"

    @pytest.mark.asyncio
    async def test_loop_death_releases_with_error(self, harness):
        harness.loop_behavior = "die"
        unit = uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 3, "content": "x"}]
        claim = make_claim(unit_id=unit, token=6, input_seq=3)

        await harness.executor._serve_claim(claim)  # must not raise
        await _finish(harness)

        assert len(harness.calls["release"]) == 1
        assert harness.calls["release"][0] == {
            "unit_id": unit,
            "lease_token": 6,
            "backoff_seconds": 0.0,
            "error": True,
        }
        assert not harness.calls["complete"]
        # Broken session was detached, never marking the thread.
        assert harness.calls["terminate"]
        assert all(t["mark_thread"] is False for t in harness.calls["terminate"])
        assert all(t["preserve_shell"] is True for t in harness.calls["terminate"])


class TestShutdownCancellation:
    @pytest.mark.asyncio
    async def test_cancelled_stalled_bundle_quiesces_then_exact_releases(self, harness):
        entered = asyncio.Event()

        async def _blocked_bundle(_unit_id, _token):
            entered.set()
            await asyncio.sleep(3600)

        claim = make_claim(token=41)
        order: list[str] = []

        async def _detach(reason):
            assert reason == "shutdown_cancelled_claim"
            order.append("detach")

        async def _release(*_args, **kwargs):
            assert kwargs["lease_token"] == 41
            assert kwargs["error"] is True
            order.append("release")
            return "queued"

        with (
            patch.object(harness.executor, "_fetch_bundle", _blocked_bundle),
            patch.object(
                harness.executor,
                "_detach_physical_before_transition",
                _detach,
            ),
            patch.object(te, "release_unit", _release),
        ):
            serving = asyncio.create_task(harness.executor._serve_claim(claim))
            await asyncio.wait_for(entered.wait(), timeout=2)
            serving.cancel()
            with pytest.raises(asyncio.CancelledError):
                await serving

        assert order == ["detach", "release"]

    @pytest.mark.asyncio
    async def test_cancelled_stalled_turn_detaches_before_queue_release(self, harness):
        harness.loop_behavior = "hang"
        harness.stateless_warm_reuse_safe = False
        row_id = str(uuid4())
        claim = make_claim(token=42, input_seq=4)
        harness.db.pending_rows = [{"id": row_id, "seq": 4, "content": "blocked"}]

        serving = asyncio.create_task(harness.executor._serve_claim(claim))
        deadline = asyncio.get_running_loop().time() + 2
        while not harness.calls["interrupt_open"]:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving
        await _finish(harness)

        assert harness.calls["terminate"]
        assert harness.calls["terminate"][-1]["mark_thread"] is False
        assert harness.calls["release"] == [
            {
                "unit_id": claim.unit_id,
                "lease_token": 42,
                "backoff_seconds": 0.0,
                "error": True,
            }
        ]
        assert not harness.calls["complete"]

    @pytest.mark.asyncio
    async def test_cancelled_claim_acks_exact_loss_when_end_won_release(self, harness):
        entered = asyncio.Event()

        async def _blocked_bundle(_unit_id, _token):
            entered.set()
            await asyncio.sleep(3600)

        claim = make_claim(token=43)
        ack = AsyncMock(return_value=True)
        with (
            patch.object(harness.executor, "_fetch_bundle", _blocked_bundle),
            patch.object(
                harness.executor,
                "_detach_physical_before_transition",
                new=AsyncMock(),
            ),
            patch.object(te, "release_unit", new=AsyncMock(return_value=None)),
            patch.object(harness.executor, "_ack_terminal_claim_loss", ack),
        ):
            serving = asyncio.create_task(harness.executor._serve_claim(claim))
            await asyncio.wait_for(entered.wait(), timeout=2)
            serving.cancel()
            with pytest.raises(asyncio.CancelledError):
                await serving

        ack.assert_awaited_once_with(claim)

    @pytest.mark.asyncio
    async def test_attach_failure_releases_and_clears(self, harness):
        harness.attach_error = RuntimeError("workspace exploded")
        unit = uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "x"}]
        claim = make_claim(unit_id=unit, input_seq=1)

        await harness.executor._serve_claim(claim)
        await _finish(harness)

        assert len(harness.calls["release"]) == 1
        assert harness.calls["release"][0]["error"] is True
        assert pa._thread_id is None
        assert not harness.calls["complete"]


# ---------------------------------------------------------------------------
# 5. Heartbeat lease-lost → polite abort, NO release
# ---------------------------------------------------------------------------


class TestLeaseLost:
    @pytest.mark.asyncio
    async def test_loss_during_bundle_cannot_be_erased_by_handle_update(
        self, harness, monkeypatch
    ):
        monkeypatch.setattr(te, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
        harness.heartbeat_result = None
        prior_unit = uuid4()
        harness.executor._lease.update(prior_unit, 4)
        prior_lost_event = harness.executor._lease.lost
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _blocked_bundle(_unit_id, _token):
            entered.set()
            await release.wait()
            return {
                "attach": harness.attach_for(str(_unit_id)),
                "watermarks": {},
            }

        claim = make_claim(unit_id=uuid4(), token=9, input_seq=2)
        with patch.object(
            harness.executor, "_fetch_bundle", side_effect=_blocked_bundle
        ):
            serve = asyncio.create_task(harness.executor._serve_claim(claim))
            await asyncio.wait_for(entered.wait(), timeout=2)
            deadline = asyncio.get_running_loop().time() + 2
            while not harness.calls["heartbeat"]:
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            release.set()
            await asyncio.wait_for(serve, timeout=2)

        assert harness.executor._lease.unit_id == str(prior_unit)
        assert harness.executor._lease.lease_token == 4
        assert harness.executor._lease.lost is prior_lost_event
        assert not prior_lost_event.is_set()
        assert not harness.calls["attach"]
        assert not harness.calls["release"]
        assert not harness.calls["complete"]

    @pytest.mark.asyncio
    async def test_heartbeat_loss_aborts_without_release(self, harness, monkeypatch):
        monkeypatch.setattr(te, "HEARTBEAT_INTERVAL_SECONDS", 0.01)
        harness.heartbeat_result = None  # renewal finds no leased row
        harness.loop_behavior = "hang"  # the turn never completes on its own
        unit = uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 2, "content": "hi"}]
        claim = make_claim(unit_id=unit, token=9, input_seq=2)

        await asyncio.wait_for(harness.executor._serve_claim(claim), timeout=5.0)
        await _finish(harness)

        # Polite abort: interrupt signalled, session discarded, and neither
        # complete_unit nor release_unit was called — the lease is gone.
        assert not harness.calls["release"]
        assert not harness.calls["complete"]
        assert pa._loop_interrupt_flag in ("hard", "graceful")
        assert harness.calls["terminate"]
        assert harness.calls["terminate"][-1]["mark_thread"] is False
        assert harness.calls["terminate"][-1]["preserve_shell"] is True
        # Affinity dropped: the discarded session must not be preferred.
        assert harness.executor._prefer_unit_id is None
        assert harness.executor._attached_fingerprint is None


# ---------------------------------------------------------------------------
# 6. strip_restored_pending_humans
# ---------------------------------------------------------------------------


class TestStripRestoredPending:
    def _msgs(self):
        from langchain_core.messages import AIMessage, HumanMessage

        return [
            HumanMessage(content="q1", id="a"),
            AIMessage(content="a1", id="b"),
            HumanMessage(content="pending-1", id="fresh-uuid-1"),
            HumanMessage(content="pending-2", id="fresh-uuid-2"),
        ]

    def test_removes_trailing_pending_by_content(self):
        msgs = self._msgs()
        pending = [
            {"id": "row-1", "seq": 10, "content": "pending-1"},
            {"id": "row-2", "seq": 11, "content": "pending-2"},
        ]
        removed = te.strip_restored_pending_humans(msgs, pending)
        assert removed == 2
        assert [m.content for m in msgs] == ["q1", "a1"]

    def test_removes_by_id_when_restore_preserves_ids(self):
        from langchain_core.messages import HumanMessage

        msgs = [HumanMessage(content="anything", id="row-9")]
        pending = [{"id": "row-9", "seq": 4, "content": "different text"}]
        removed = te.strip_restored_pending_humans(msgs, pending)
        assert removed == 1
        assert msgs == []

    def test_non_trailing_human_untouched(self):
        from langchain_core.messages import AIMessage, HumanMessage

        msgs = [
            HumanMessage(content="pending-1", id="x"),
            AIMessage(content="answer", id="y"),
        ]
        pending = [{"id": "row-1", "seq": 10, "content": "pending-1"}]
        removed = te.strip_restored_pending_humans(msgs, pending)
        assert removed == 0
        assert len(msgs) == 2

    def test_pending_event_excluded_from_restore_does_not_block_human_strip(self):
        from langchain_core.messages import HumanMessage

        # Unadmitted ledger rows are excluded from passive restore. The event
        # still appears in the executor's merged pending query after the human;
        # it must not become an unmatched tail sentinel that leaves the human
        # duplicated in memory when the executor injects it.
        msgs = [HumanMessage(content="pending-1", id="p1")]
        pending = [
            {"id": "row-1", "seq": 10, "content": "pending-1", "role": "human"},
            {
                "id": "event-row",
                "seq": 11,
                "content": "[wake] job finished",
                "role": "event",
                "delivery_id": "delivery",
            },
        ]
        removed = te.strip_restored_pending_humans(msgs, pending)
        assert removed == 1
        assert msgs == []

    def test_empty_inputs_are_noops(self):
        assert te.strip_restored_pending_humans([], [{"id": "a"}]) == 0
        from langchain_core.messages import HumanMessage

        msgs = [HumanMessage(content="x", id="a")]
        assert te.strip_restored_pending_humans(msgs, []) == 0
        assert len(msgs) == 1


# ---------------------------------------------------------------------------
# 7. Scrub-on-claim acceptance (§5.6 — deliverable D)
# ---------------------------------------------------------------------------


class TestScrubOnClaim:
    def test_tenant_a_then_no_env_keys_leaves_no_residue(self, monkeypatch):
        from src.services import embedding_service as emb

        # Tenant A attach: env keys land, singleton would be rebuilt lazily.
        pa._apply_session_embedding_env(
            {
                "EMBEDDING_PROVIDER": "openai",
                "EMBEDDING_MODEL": "tenant-a-model",
                "EMBEDDING_BASE_URL": "https://a.example",
                "EMBEDDING_API_KEY": "sk-tenant-a",
                "KB_EMBEDDING_MODEL": "kb-a",
                "KB_EMBEDDING_API_KEY": "sk-kb-a",
            }
        )
        assert os.environ.get("EMBEDDING_API_KEY") == "sk-tenant-a"
        assert os.environ.get("KB_EMBEDDING_MODEL") == "kb-a"
        emb._embedding_service = object()  # simulate a built singleton

        # Tenant B attach with env_keys ABSENT: no A values anywhere,
        # singleton None.
        pa._apply_session_embedding_env(None)
        for key in pa.MEMORY_EMBEDDING_ENV_KEYS:
            assert key not in os.environ, key
        for key in emb.KB_EMBEDDING_ENV_KEYS:
            assert key not in os.environ, key
        assert emb._embedding_service is None
        assert emb._kb_embedding_service is None

        # Cleanup safety: nothing to restore — the helper popped everything.

    def test_partial_override_replaces_not_merges(self, monkeypatch):
        from src.services import embedding_service as emb

        pa._apply_session_embedding_env(
            {"EMBEDDING_MODEL": "a-model", "EMBEDDING_API_KEY": "sk-a"}
        )
        # Tenant B supplies only a model: A's key must NOT survive.
        pa._apply_session_embedding_env({"EMBEDDING_MODEL": "b-model"})
        assert os.environ.get("EMBEDDING_MODEL") == "b-model"
        assert "EMBEDDING_API_KEY" not in os.environ
        assert emb._embedding_service is None
        pa._apply_session_embedding_env(None)  # cleanup

    def test_executor_scrub_clears_dual_inboxes(self, harness):
        import src.api.dual_app as dual_app

        dual_app._guidance_inbox["job-1"] = [{"id": "g1"}]
        dual_app._reply_inbox["job-1"] = [{"id": "r1"}]
        harness.executor._scrub_process_residue()
        assert dual_app._guidance_inbox == {}
        assert dual_app._reply_inbox == {}


# ---------------------------------------------------------------------------
# 8. Affinity
# ---------------------------------------------------------------------------


class TestAffinity:
    @pytest.mark.parametrize(
        ("supports_shell", "expected"),
        ((False, True), (True, False)),
    )
    def test_persistent_session_reuse_capability_follows_backend(
        self, supports_shell, expected
    ):
        from src.api.persistent_session import PersistentSession

        session = object.__new__(PersistentSession)
        session.workspace_manager = SimpleNamespace(
            backend=SimpleNamespace(supports_shell=supports_shell)
        )

        assert session.stateless_warm_reuse_safe is expected

    def test_fingerprint_ignores_only_inbox_owned_interactive_scalars(self):
        base = {
            "thread_id": "t1",
            "resolved_config": {
                "resolved_at": "first",
                "agent": {
                    "interactive": {
                        "permission_mode": "supervised",
                        "narration_mode": "auto",
                        "idle_timeout_minutes": 30,
                    },
                    "llm": {"model": "m"},
                },
            },
        }
        changed_controls = {
            "thread_id": "t1",
            "resolved_config": {
                "resolved_at": "second",
                "agent": {
                    "interactive": {
                        "permission_mode": "autonomous",
                        "narration_mode": "verbose",
                        "idle_timeout_minutes": 30,
                    },
                    "llm": {"model": "m"},
                },
            },
        }
        changed_runtime_config = {
            **changed_controls,
            "resolved_config": {
                **changed_controls["resolved_config"],
                "agent": {
                    **changed_controls["resolved_config"]["agent"],
                    "interactive": {
                        **changed_controls["resolved_config"]["agent"]["interactive"],
                        "idle_timeout_minutes": 60,
                    },
                },
            },
        }

        assert te.attach_fingerprint(base) == te.attach_fingerprint(changed_controls)
        assert te.attach_fingerprint(base) != te.attach_fingerprint(
            changed_runtime_config
        )

    def test_fingerprint_ignores_fallback_control_scalars(self):
        first = {
            "thread_id": "t1",
            "config_override": {
                "interactive": {
                    "permission_mode": "supervised",
                    "narration_mode": "auto",
                }
            },
        }
        second = {
            "thread_id": "t1",
            "config_override": {
                "interactive": {
                    "permission_mode": "auto_accept",
                    "narration_mode": "silent",
                }
            },
        }
        assert te.attach_fingerprint(first) == te.attach_fingerprint(second)

    @pytest.mark.asyncio
    async def test_lite_same_thread_same_fingerprint_skips_reattach(self, harness):
        unit = uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "one"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=1, input_seq=1)
        )
        assert len(harness.calls["attach"]) == 1

        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 2, "content": "two"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=2, input_seq=2)
        )
        await _finish(harness)

        # Reused: no second attach, no detach in between.
        assert len(harness.calls["attach"]) == 1
        assert not harness.calls["terminate"]
        # The lease handle was repointed at the NEW claim's token.
        assert harness.executor._lease.lease_token == 2
        assert len(harness.calls["complete"]) == 2
        assert harness.calls["shell_owner_token"] == [1, 2]
        assert harness.sessions[0].shell_owner_tokens == [1, 2]
        assert [call["turn_id"] for call in harness.calls["interrupt_open"]] == [
            1,
            2,
        ]

    @pytest.mark.asyncio
    async def test_warm_attach_consumes_event_once_under_new_lease(self, harness):
        unit = uuid4()
        harness.db.pending_rows = [
            {"id": str(uuid4()), "seq": 1, "content": "human", "turn_number": 1}
        ]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=1, input_seq=1)
        )

        row_id = str(uuid4())
        delivery_id = str(uuid4())
        harness.db.pending_rows = [
            {
                "id": row_id,
                "seq": 2,
                "content": "event",
                "turn_number": 2,
                "role": "event",
                "delivery_id": delivery_id,
            }
        ]
        claim_delivery = AsyncMock(
            return_value={
                "message_id": row_id,
                "seq": 2,
                "claim_generation": 4,
            }
        )
        harness.db.claim_stateless_input_delivery = claim_delivery
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=2, input_seq=2, consumed_seq=1)
        )
        await _finish(harness)

        assert len(harness.calls["attach"]) == 1
        assert [item.get("role", "human") for item in harness.consumed] == [
            "human",
            "event",
        ]
        claim_delivery.assert_awaited_once()
        assert harness.calls["complete"][-1]["consumed_seq"] == 2

    @pytest.mark.asyncio
    async def test_warm_reuse_refuses_divergent_durable_turn_identity(self, harness):
        unit = uuid4()
        harness.db.pending_rows = [
            {
                "id": str(uuid4()),
                "seq": 1,
                "content": "one",
                "turn_number": 1,
            }
        ]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=1, input_seq=1)
        )
        assert pa._session is not None
        pa._session.turn_count = 7
        harness.db.pending_rows = [
            {
                "id": str(uuid4()),
                "seq": 2,
                "content": "must-not-run",
                "turn_number": 2,
            }
        ]

        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=2, input_seq=2, consumed_seq=1)
        )
        await _finish(harness)

        assert len(harness.consumed) == 1
        assert len(harness.calls["interrupt_open"]) == 1
        assert harness.calls["release"][-1]["lease_token"] == 2

    @pytest.mark.asyncio
    async def test_shell_backend_reattaches_before_next_lease_token(self, harness):
        unit = uuid4()
        harness.stateless_warm_reuse_safe = False
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "one"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=1, input_seq=1)
        )
        first_session = harness.sessions[0]
        # Physical ownership is retired while claim 1 is still exclusive,
        # before the queue completion transition.
        assert pa._session is None

        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 2, "content": "two"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=2, input_seq=2)
        )
        await _finish(harness)

        assert len(harness.calls["attach"]) == 2
        assert harness.calls["terminate"] == [
            {
                "reason": "turn_complete",
                "mark_thread": False,
                "preserve_shell": True,
                "preserve_workspace_daemons": True,
            },
            {
                "reason": "turn_complete",
                "mark_thread": False,
                "preserve_shell": True,
                "preserve_workspace_daemons": True,
            },
        ]
        assert first_session is harness.sessions[0]
        assert pa._session is None
        assert first_session is not harness.sessions[1]
        assert harness.sessions[0].shell_owner_tokens == [1]
        assert harness.sessions[1].shell_owner_tokens == [2]

    @pytest.mark.asyncio
    async def test_control_scalar_bundle_change_reuses_warm_session(self, harness):
        unit = uuid4()
        base = harness.attach_for(str(unit))
        first = {
            **base,
            "resolved_config": {
                **base["resolved_config"],
                "agent": {
                    **base["resolved_config"]["agent"],
                    "interactive": {
                        "permission_mode": "supervised",
                        "narration_mode": "auto",
                    },
                },
            },
        }
        harness.set_attach(str(unit), first)
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "one"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=1, input_seq=1)
        )

        second = {
            **first,
            "resolved_config": {
                **first["resolved_config"],
                "agent": {
                    **first["resolved_config"]["agent"],
                    "interactive": {
                        "permission_mode": "autonomous",
                        "narration_mode": "verbose",
                    },
                },
            },
        }
        harness.set_attach(str(unit), second)
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 2, "content": "two"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=2, input_seq=2)
        )
        await _finish(harness)

        assert len(harness.calls["attach"]) == 1
        assert not harness.calls["terminate"]

    @pytest.mark.asyncio
    async def test_different_thread_detaches_then_attaches(self, harness):
        unit_a, unit_b = uuid4(), uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "a"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit_a, token=1, input_seq=1)
        )
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "b"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit_b, token=1, input_seq=1)
        )
        await _finish(harness)

        assert len(harness.calls["attach"]) == 2
        assert harness.calls["attach"][1]["thread_id"] == str(unit_b)
        # Old session detached first, thread never marked.
        assert len(harness.calls["terminate"]) == 1
        assert harness.calls["terminate"][0]["mark_thread"] is False
        assert harness.calls["terminate"][0]["preserve_shell"] is True

    @pytest.mark.asyncio
    async def test_same_thread_changed_config_reattaches(self, harness):
        unit = uuid4()
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 1, "content": "a"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=1, input_seq=1)
        )
        harness.set_attach(
            str(unit),
            {
                "thread_id": str(unit),
                "config_override": {"llm": {"model": "different"}},
                "resolved_config": None,
                "project_ids": [],
                "datasources": None,
                "config_name": "session_base",
            },
        )
        harness.db.pending_rows = [{"id": str(uuid4()), "seq": 2, "content": "b"}]
        await harness.executor._serve_claim(
            make_claim(unit_id=unit, token=2, input_seq=2)
        )
        await _finish(harness)

        assert len(harness.calls["attach"]) == 2
        assert len(harness.calls["terminate"]) == 1


# ---------------------------------------------------------------------------
# 9. Fenced persistence (postgres_db + lease context)
# ---------------------------------------------------------------------------


class _FenceConn:
    """Fake asyncpg connection: fence query returns None (rejected)."""

    def __init__(self, fence_row=None):
        self.fence_row = fence_row
        self.sql_log: List[str] = []

    def transaction(self):
        @contextlib.asynccontextmanager
        async def _tx():
            yield

        return _tx()

    async def fetchrow(self, sql, *args):
        self.sql_log.append(sql)
        if "FROM run_queue" in sql:
            return self.fence_row
        return {"id": args[0] if args else "x", "seq": 42}

    async def fetchval(self, sql, *args):
        self.sql_log.append(sql)
        return args[0] if args else 1

    async def execute(self, sql, *args):
        self.sql_log.append(sql)
        return "UPDATE 1"

    async def executemany(self, sql, args):
        self.sql_log.append(sql)


def _db_with_conn(conn):
    from src.database.postgres_db import PostgresDB

    db = PostgresDB(connection_string="postgresql://t:t@localhost:1/t")

    @contextlib.asynccontextmanager
    async def _acquire():
        yield conn

    db.acquire = _acquire  # type: ignore[method-assign]
    return db


class TestFencedPersistence:
    @pytest.mark.asyncio
    async def test_provider_delivery_callback_uses_exact_stateless_owner(
        self, monkeypatch
    ):
        transition = AsyncMock(return_value=True)
        db = SimpleNamespace(transition_stateless_input_delivery=transition)
        thread_id = str(uuid4())
        monkeypatch.setattr(pa, "_session", SimpleNamespace(postgres_conn=db))
        monkeypatch.setattr(pa, "_thread_id", thread_id)
        handle = LeaseHandle()
        handle.update(
            thread_id,
            17,
            executor_id="executor-a",
            pod_uid="pod-a",
        )
        token = current_lease.set(handle)
        try:
            assert await pa._transition_claimed_input(
                "0d8a40c3-8f0f-4f2b-acab-8a07660ecf5d",
                3,
                "admitted",
                turn_number=8,
            )
        finally:
            current_lease.reset(token)

        transition.assert_awaited_once_with(
            thread_id=thread_id,
            delivery_id="0d8a40c3-8f0f-4f2b-acab-8a07660ecf5d",
            lease_token=17,
            executor_id="executor-a",
            pod_uid="pod-a",
            claim_generation=3,
            transition="admitted",
            turn_number=8,
            reason=None,
        )

    @pytest.mark.asyncio
    async def test_fence_rejection_raises_and_marks_lost(self):
        conn = _FenceConn(fence_row=None)
        db = _db_with_conn(conn)
        handle = LeaseHandle()
        thread_id = str(uuid4())
        handle.update(thread_id, 5)
        token = current_lease.set(handle)
        try:
            with pytest.raises(LeaseLostError):
                await db.save_thread_message(
                    thread_id=thread_id, role="human", content="x"
                )
            assert handle.lost.is_set()
            # The fence ran and nothing else did inside the transaction.
            assert any("FROM run_queue" in s for s in conn.sql_log)
            assert not any("INSERT INTO thread_messages" in s for s in conn.sql_log)
        finally:
            current_lease.reset(token)

    @pytest.mark.asyncio
    async def test_fence_pass_writes_row(self):
        conn = _FenceConn(fence_row={"?column?": 1})
        db = _db_with_conn(conn)
        handle = LeaseHandle()
        thread_id = str(uuid4())
        handle.update(thread_id, 5)
        token = current_lease.set(handle)
        try:
            result = await db.save_thread_message(
                thread_id=thread_id, role="human", content="x"
            )
            assert result["seq"] == 42
            assert any("FROM run_queue" in s for s in conn.sql_log)
            assert any("INSERT INTO thread_messages" in s for s in conn.sql_log)
        finally:
            current_lease.reset(token)

    @pytest.mark.asyncio
    async def test_no_lease_context_keeps_pinned_behavior(self):
        conn = _FenceConn(fence_row=None)  # would reject IF consulted
        db = _db_with_conn(conn)
        assert get_current_lease() is None
        result = await db.save_thread_message(
            thread_id=str(uuid4()), role="human", content="x"
        )
        assert result["seq"] == 42
        assert not any("FROM run_queue" in s for s in conn.sql_log)

    @pytest.mark.asyncio
    async def test_batch_reconcile_fenced(self):
        conn = _FenceConn(fence_row=None)
        db = _db_with_conn(conn)
        handle = LeaseHandle()
        handle.update(str(uuid4()), 8)
        token = current_lease.set(handle)
        try:
            with pytest.raises(LeaseLostError):
                await db.save_thread_messages(
                    str(uuid4()),
                    [{"id": None, "role": "ai", "content": "x", "turn_number": 1}],
                )
        finally:
            current_lease.reset(token)


# ---------------------------------------------------------------------------
# 10. Journal writer lease fence (persistent_app._OrderedPersistentEventWriter)
# ---------------------------------------------------------------------------


class TestWriterLeaseFence:
    def _writer(self, lease):
        recorded = []

        class _Txn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        class _Conn:
            def transaction(self):
                return _Txn()

            async def fetchval(self, sql, *args):
                recorded.append((sql, args))
                return 1

        class _Acquire:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, exc_type, exc, tb):
                return None

        pool = SimpleNamespace(acquire=lambda: _Acquire())
        writer = pa._OrderedPersistentEventWriter(
            postgres_conn=pool,
            thread_id=lease.unit_id if lease is not None else "thread-1",
            epoch=0,
            on_terminal_failure=lambda events, reason: None,
            lease=lease,
        )
        return writer, recorded

    @pytest.mark.asyncio
    async def test_lease_flush_carries_fence_params_read_at_flush_time(self):
        handle = LeaseHandle()
        unit = str(uuid4())
        handle.update(unit, 3)
        writer, recorded = self._writer(handle)
        event = pa._QueuedPersistentEvent(epoch=0, seq=1, kind="token", payload={})

        await writer._write_batch([event])
        assert "FROM threads" in recorded[0][0]
        assert "FROM run_queue" in recorded[1][0]
        sql, args = recorded[2]
        assert "run_queue" in sql and "FOR SHARE" in sql
        assert args[3] == unit and args[4] == 3

        # Affinity re-claim: same writer, handle repointed → new token flows.
        handle.update(unit, 4)
        await writer._write_batch([event])
        assert recorded[5][1][4] == 4

    @pytest.mark.asyncio
    async def test_pinned_writer_keeps_four_arg_shape(self):
        writer, recorded = self._writer(None)
        event = pa._QueuedPersistentEvent(epoch=0, seq=1, kind="token", payload={})
        await writer._write_batch([event])
        sql, args = recorded[0]
        assert "run_queue" not in sql
        assert len(args) == 3  # thread_id, rows_json, epoch
