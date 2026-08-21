"""P0: a terminating persistent pod cannot admit another paid turn."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import WebSocketDisconnect
from langchain_core.messages import AIMessage, ToolMessage

from services import session_lifecycle, session_wake, sitrep
from src.api import persistent_app, persistent_termination
from src.persistent_graph import PersistentLoopCallbacks, run_persistent_loop
from src.services.auxiliary import (
    AuxiliaryLLM,
    AuxiliaryProviderAdmissionClosed,
)
from src.services.memory import CaptureEvent
from src.services.memory.manager import MemoryManager


def _config() -> MagicMock:
    config = MagicMock()
    config.llm.timeout = 30
    config.memory.enabled = False
    config.memory.observer_interval = 5
    config.context_management.max_summary_length = 10_000
    config.officer.enabled = False
    return config


def _context_manager() -> MagicMock:
    manager = MagicMock()
    manager.ensure_within_limits = AsyncMock(
        side_effect=lambda messages, *_args, **_kwargs: messages
    )
    return manager


def _callbacks(inputs, **overrides) -> PersistentLoopCallbacks:
    source = iter(inputs)

    async def _input():
        try:
            return next(source)
        except StopIteration:
            raise asyncio.CancelledError from None

    values = {
        "get_user_input": _input,
        "on_token": AsyncMock(),
        "on_thinking": AsyncMock(),
        "on_tool_start": AsyncMock(),
        "on_tool_result": AsyncMock(),
        "permission_check": AsyncMock(return_value=True),
        "on_turn_start": AsyncMock(),
        "on_turn_complete": AsyncMock(),
        "on_error": AsyncMock(),
        "check_interrupt": MagicMock(return_value=False),
        "persist_message": AsyncMock(),
        "on_turn_settled": AsyncMock(),
    }
    values.update(overrides)
    return PersistentLoopCallbacks(**values)


class _InsertOnceDB:
    def __init__(self) -> None:
        self.ids: set[str] = set()
        self.rows: list[dict] = []
        self.deliveries: dict[str, dict] = {}
        self.after_persist = None

    async def persist_pinned_input_delivery(self, **row):
        delivery_id = row["delivery_id"]
        inserted = delivery_id not in self.deliveries
        if inserted:
            self.rows.append(dict(row))
            self.deliveries[delivery_id] = {
                **row,
                "state": "owned",
                "claim_generation": 1,
                "message_id": str(uuid4()),
                "owner_runtime_generation": row["runtime_generation"],
            }
        result = self.deliveries[delivery_id]
        if self.after_persist is not None:
            self.after_persist()
        return {**result, "transcript_inserted": inserted}

    async def mark_pinned_input_delivery_queued(self, **row):
        delivery = self.deliveries[row["delivery_id"]]
        if delivery["state"] not in {"owned", "queued"}:
            return False
        delivery["state"] = "queued"
        return True

    async def transition_pinned_input_delivery(self, **row):
        delivery = self.deliveries[row["delivery_id"]]
        transition = row["transition"]
        if transition == "deferred":
            delivery["state"] = "deferred"
        elif transition == "unadmit":
            delivery["state"] = "deferred"
        elif transition == "admitted":
            delivery["state"] = "admitted"
        elif transition == "settled":
            delivery["state"] = "settled"
        return True

    async def claim_pending_pinned_input_deliveries(self, **row):
        result = []
        for delivery in self.deliveries.values():
            if delivery["state"] in {"admitted", "settled"}:
                continue
            if delivery["owner_runtime_generation"] != row["runtime_generation"]:
                delivery["owner_runtime_generation"] = row["runtime_generation"]
                delivery["runtime_generation"] = row["runtime_generation"]
                delivery["claim_generation"] += 1
                delivery["state"] = "owned"
            result.append(delivery)
        return result


def _wire_input_runtime(monkeypatch, tmp_path, db, *, turn_count: int = 5):
    queue: asyncio.Queue = asyncio.Queue()
    monkeypatch.setattr(
        persistent_app,
        "_TERMINATION_SENTINEL_PATH",
        tmp_path / "terminating",
    )
    monkeypatch.setattr(persistent_app, "_termination_admission_fenced", False)
    monkeypatch.setattr(persistent_app, "_termination_fence_reason", None)
    monkeypatch.setattr(persistent_app, "_awaiting_input", False)
    monkeypatch.setattr(persistent_app, "_turn_event_open", False)
    monkeypatch.setattr(persistent_app, "_tool_inflight", False)
    monkeypatch.setattr(persistent_app, "_loop_user_queue", queue)
    monkeypatch.setattr(persistent_app, "_loop_last_user_content", [""])
    monkeypatch.setattr(persistent_app, "_thread_id", str(uuid4()))
    monkeypatch.setenv("POD_UID", "pod-uid-test")
    monkeypatch.setattr(
        persistent_app,
        "_orchestrator_client",
        SimpleNamespace(agent_id=str(uuid4())),
    )
    monkeypatch.setattr(persistent_app, "_input_runtime_generation", str(uuid4()))
    persistent_app._queued_input_claims.clear()
    monkeypatch.setattr(
        persistent_app,
        "_session",
        SimpleNamespace(postgres_conn=db, turn_count=turn_count),
    )
    monkeypatch.setattr(persistent_app, "_broadcast", MagicMock())
    return queue


async def _run_websocket_input(monkeypatch, content: str, *, before_receive=None):
    """Drive the production WS receive loop for exactly one human input."""

    session = persistent_app._session
    session.llm_with_tools = MagicMock()
    session.messages = []
    session.permission_mode = "supervised"
    session.narration_mode = "auto"
    session.config = SimpleNamespace(
        llm=SimpleNamespace(model="test-model", temperature=0.0)
    )
    session.session_task_manager = None

    receives = 0

    async def _receive_text():
        nonlocal receives
        receives += 1
        if receives == 1:
            if before_receive is not None:
                before_receive()
            return json.dumps({"method": "message", "content": content})
        raise WebSocketDisconnect()

    async def _park_pump(*_args, **_kwargs):
        await asyncio.Future()

    ws = AsyncMock()
    ws.receive_text.side_effect = _receive_text
    monkeypatch.setattr(persistent_app, "_signal_ws_connected", MagicMock())
    monkeypatch.setattr(
        persistent_app,
        "_durable_session_control_modes",
        AsyncMock(return_value=("supervised", "auto")),
    )
    monkeypatch.setattr(
        persistent_app,
        "_pending_permission_requests",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        persistent_app, "_ensure_persistent_loop_started", MagicMock(return_value=True)
    )
    subscriber_queue: asyncio.Queue = asyncio.Queue()
    monkeypatch.setattr(
        persistent_app, "_subscribe", lambda _client_id: subscriber_queue
    )
    monkeypatch.setattr(persistent_app, "_unsubscribe", MagicMock())
    monkeypatch.setattr(persistent_app, "_clear_canvas_awareness", MagicMock())
    monkeypatch.setattr(persistent_app, "_run_subscriber_pump", _park_pump)
    await persistent_app.handle_persistent_websocket(ws)
    return ws


def _ws_frames(ws, method: str) -> list[dict]:
    return [
        call.args[0]
        for call in ws.send_json.await_args_list
        if call.args[0].get("method") == method
    ]


def test_prestop_helper_publishes_sentinel_before_loopback_wait(monkeypatch, tmp_path):
    sentinel = tmp_path / "terminating"
    observed = []

    class _Response:
        status = 200

        def __enter__(self):
            observed.append(sentinel.exists())
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        persistent_termination, "TERMINATION_SENTINEL_PATH", str(sentinel)
    )
    monkeypatch.setattr(
        persistent_termination.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(),
    )

    assert persistent_termination.main() == 0
    assert observed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [({}, False), ({"durable_input_delivery": True}, True)],
)
async def test_wake_probe_requires_input_ledger_capability(
    monkeypatch, capabilities, expected
):
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ready": True, "capabilities": capabilities}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, _url):
            return _Response()

    monkeypatch.setattr(session_lifecycle.httpx, "AsyncClient", _Client)
    assert (
        await session_lifecycle.probe_ready(
            "127.0.0.1",
            8001,
            required_capability="durable_input_delivery",
        )
        is expected
    )


@pytest.mark.asyncio
async def test_retry_stable_event_is_enqueued_once_and_fence_rejects_new_wake(
    monkeypatch, tmp_path
):
    """A lost response retry cannot create a second transcript row or turn."""

    db = _InsertOnceDB()
    queue = _wire_input_runtime(monkeypatch, tmp_path, db)
    delivery_id = str(uuid4())

    first = await persistent_app._accept_user_input(
        "timer wake", role="event", delivery_id=delivery_id
    )
    retry = await persistent_app._accept_user_input(
        "timer wake", role="event", delivery_id=delivery_id
    )

    assert first.enqueued is True
    assert retry.duplicate is True and retry.enqueued is False
    assert queue.qsize() == 1
    assert len(db.rows) == 1

    assert persistent_app.activate_termination_admission_fence("test") is True
    with pytest.raises(persistent_app.TerminationAdmissionClosed):
        await persistent_app._accept_user_input(
            "must wait", role="event", delivery_id=str(uuid4())
        )
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_supplied_event_delivery_identity_requires_internal_authority(
    monkeypatch, tmp_path
):
    db = _InsertOnceDB()
    _wire_input_runtime(monkeypatch, tmp_path, db)
    monkeypatch.setenv("MCP_INTERNAL_KEY", "synthetic-internal-key")
    monkeypatch.setattr(
        persistent_app, "_ensure_persistent_loop_started", MagicMock(return_value=True)
    )
    delivery_id = str(uuid4())

    denied = await persistent_app.handle_api_input(
        SimpleNamespace(
            headers={},
            json=AsyncMock(
                return_value={
                    "content": "server wake",
                    "role": "event",
                    "delivery_id": delivery_id,
                }
            ),
        )
    )
    assert denied.status_code == 403
    assert db.rows == []

    accepted = await persistent_app.handle_api_input(
        SimpleNamespace(
            headers={"X-Internal-Key": "synthetic-internal-key"},
            json=AsyncMock(
                return_value={
                    "content": "server wake",
                    "role": "event",
                    "delivery_id": delivery_id,
                }
            ),
        )
    )
    assert accepted.status_code == 202
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_direct_input_fenced_after_persist_is_truthfully_deferred(
    monkeypatch, tmp_path
):
    """The accept/fence race retains human input instead of silently losing it."""

    db = _InsertOnceDB()
    db.after_persist = lambda: persistent_app.activate_termination_admission_fence(
        "persist_race"
    )
    queue = _wire_input_runtime(monkeypatch, tmp_path, db)

    accepted = await persistent_app._accept_user_input("please retain this")

    assert accepted.deferred is True
    assert accepted.enqueued is False
    assert len(db.rows) == 1
    assert queue.empty()


@pytest.mark.asyncio
async def test_websocket_fence_before_persist_rejects_as_retryable(
    monkeypatch, tmp_path
):
    """Only a pre-transaction termination race tells the human to retry."""

    db = _InsertOnceDB()
    _wire_input_runtime(monkeypatch, tmp_path, db)
    ws = await _run_websocket_input(
        monkeypatch,
        "not persisted",
        before_receive=lambda: persistent_app.activate_termination_admission_fence(
            "before_persist"
        ),
    )

    rejected = _ws_frames(ws, "input.rejected")
    assert len(rejected) == 1
    assert rejected[0]["params"] == {
        "error": "runtime_terminating",
        "retryable": True,
        "message": "Retry input on the replacement runtime.",
    }
    assert _ws_frames(ws, "input.accepted") == []
    assert db.rows == []
    assert db.deliveries == {}


@pytest.mark.asyncio
async def test_websocket_post_persist_fence_acknowledges_and_successor_reclaims_once(
    monkeypatch, tmp_path
):
    """A committed human input is owned work, never a retry invitation."""

    db = _InsertOnceDB()
    db.after_persist = lambda: persistent_app.activate_termination_admission_fence(
        "after_persist"
    )
    queue = _wire_input_runtime(monkeypatch, tmp_path, db)

    ws = await _run_websocket_input(monkeypatch, "persist exactly once")

    assert _ws_frames(ws, "input.rejected") == []
    acknowledged = _ws_frames(ws, "input.accepted")
    assert len(acknowledged) == 1
    params = acknowledged[0]["params"]
    assert params["accepted"] is True
    assert params["deferred"] is True
    assert params["retryable"] is False
    assert params["delivery_state"] == "deferred"
    assert params["message_id"]
    assert params["delivery_id"] in db.deliveries
    assert len(db.rows) == 1
    assert len(db.deliveries) == 1
    assert queue.empty()

    # The response contract does not ask the client to mint a second random
    # identity. A new process generation claims the existing delivery and
    # publishes exactly that one input to its queue.
    db.after_persist = None
    persistent_app._termination_admission_fenced = False
    persistent_app._input_runtime_generation = str(uuid4())
    persistent_app._queued_input_claims.clear()
    reclaimed = await persistent_app._reclaim_pending_pinned_inputs()
    assert reclaimed == {(params["delivery_id"], 2)}
    item = queue.get_nowait()
    assert item["delivery_id"] == params["delivery_id"]
    assert item["id"] == params["message_id"]
    assert queue.empty()
    assert len(db.rows) == 1
    assert len(db.deliveries) == 1


@pytest.mark.asyncio
async def test_rest_post_persist_event_defer_is_accepted_for_outbox_retry(
    monkeypatch, tmp_path
):
    """REST reports persistence while the wake outbox retains execution debt."""

    db = _InsertOnceDB()
    db.after_persist = lambda: persistent_app.activate_termination_admission_fence(
        "after_persist"
    )
    _wire_input_runtime(monkeypatch, tmp_path, db)
    monkeypatch.setenv("MCP_INTERNAL_KEY", "synthetic-internal-key")
    monkeypatch.setattr(
        persistent_app, "_ensure_persistent_loop_started", MagicMock(return_value=True)
    )
    delivery_id = str(uuid4())

    response = await persistent_app.handle_api_input(
        SimpleNamespace(
            headers={"X-Internal-Key": "synthetic-internal-key"},
            json=AsyncMock(
                return_value={
                    "content": "stable outbox wake",
                    "role": "event",
                    "delivery_id": delivery_id,
                }
            ),
        )
    )
    payload = json.loads(response.body)
    assert response.status_code == 202
    assert payload["accepted"] is True
    assert payload["deferred"] is True
    assert payload["retryable"] is False
    assert payload["delivery_id"] == delivery_id
    assert payload["delivery_state"] == "deferred"
    assert len(db.rows) == 1
    assert len(db.deliveries) == 1


@pytest.mark.asyncio
async def test_committed_insert_before_queue_is_reclaimed_by_new_process(
    monkeypatch, tmp_path
):
    db = _InsertOnceDB()
    queue = _wire_input_runtime(monkeypatch, tmp_path, db)
    delivery_id = str(uuid4())
    original_queue = persistent_app._queue_claimed_input

    async def _die_after_commit(_row):
        raise RuntimeError("process died before queue publication")

    monkeypatch.setattr(persistent_app, "_queue_claimed_input", _die_after_commit)
    with pytest.raises(RuntimeError, match="process died"):
        await persistent_app._accept_user_input(
            "persisted wake", role="event", delivery_id=delivery_id
        )
    assert len(db.rows) == 1
    assert queue.empty()

    monkeypatch.setattr(persistent_app, "_queue_claimed_input", original_queue)
    persistent_app._input_runtime_generation = str(uuid4())
    persistent_app._queued_input_claims.clear()
    assert await persistent_app._reclaim_pending_pinned_inputs() == {(delivery_id, 2)}
    item = queue.get_nowait()
    assert item["delivery_id"] == delivery_id
    assert item["claim_generation"] == 2
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_retry_before_and_after_admission_never_requeues(monkeypatch, tmp_path):
    db = _InsertOnceDB()
    queue = _wire_input_runtime(monkeypatch, tmp_path, db)
    delivery_id = str(uuid4())
    first = await persistent_app._accept_user_input(
        "wake", role="event", delivery_id=delivery_id
    )
    before_admission = await persistent_app._accept_user_input(
        "wake", role="event", delivery_id=delivery_id
    )
    assert first.delivery_state == "queued"
    assert before_admission.delivery_state == "queued"
    assert queue.qsize() == 1

    assert await db.transition_pinned_input_delivery(
        delivery_id=delivery_id, transition="admitted"
    )
    after_admission = await persistent_app._accept_user_input(
        "wake", role="event", delivery_id=delivery_id
    )
    assert after_admission.delivery_state == "admitted"
    assert after_admission.enqueued is False
    assert queue.qsize() == 1
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_delivery_attempts_publish_one_queue_item(
    monkeypatch, tmp_path
):
    entered = 0
    both_entered = asyncio.Event()
    release = asyncio.Event()

    class _RacingDB(_InsertOnceDB):
        async def mark_pinned_input_delivery_queued(self, **row):
            nonlocal entered
            entered += 1
            if entered == 2:
                both_entered.set()
            await both_entered.wait()
            await release.wait()
            return await super().mark_pinned_input_delivery_queued(**row)

    db = _RacingDB()
    queue = _wire_input_runtime(monkeypatch, tmp_path, db)
    delivery_id = str(uuid4())
    attempts = [
        asyncio.create_task(
            persistent_app._accept_user_input(
                "same wake", role="event", delivery_id=delivery_id
            )
        )
        for _ in range(2)
    ]
    await both_entered.wait()
    release.set()
    results = await asyncio.gather(*attempts)
    assert sum(result.enqueued for result in results) == 1
    assert queue.qsize() == 1
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_fence_after_loop_consumption_defers_before_first_provider():
    admission_open = True
    provider_calls = 0
    admit = AsyncMock(return_value=True)
    defer = AsyncMock(return_value=True)

    async def _authorize():
        nonlocal admission_open
        admission_open = False
        return True, "authorized"

    async def _stream(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        yield AIMessage(content="must not run")

    llm = MagicMock(reasoning=None)
    llm.astream = _stream
    callbacks = _callbacks(
        [
            {
                "id": str(uuid4()),
                "role": "event",
                "content": "queued wake",
                "delivery_id": str(uuid4()),
                "claim_generation": 7,
            }
        ],
        before_turn_authorization=_authorize,
        before_provider_admission=lambda: admission_open,
        admit_input_delivery=admit,
        defer_input_delivery=defer,
    )

    await run_persistent_loop(
        llm_with_tools=llm,
        tools=[],
        context_manager=_context_manager(),
        config=_config(),
        system_prompt="system",
        callbacks=callbacks,
        messages=[],
    )
    assert provider_calls == 0
    admit.assert_not_awaited()
    defer.assert_awaited_once()


@pytest.mark.asyncio
async def test_mid_tool_termination_settles_pair_and_starts_no_second_provider():
    """An admitted tool batch may settle; its follow-up provider call may not."""

    admission_open = True
    provider_calls = 0
    persisted: list = []

    async def _stream(_messages, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        yield AIMessage(
            content="",
            tool_calls=[{"id": "call_term", "name": "inspect", "args": {}}],
        )

    async def _tool(_args):
        nonlocal admission_open
        admission_open = False
        return "settled result"

    llm = MagicMock(reasoning=None)
    llm.astream = _stream
    tool = MagicMock()
    tool.name = "inspect"
    tool.ainvoke = AsyncMock(side_effect=_tool)
    callbacks = _callbacks(
        ["inspect"],
        persist_message=AsyncMock(side_effect=persisted.append),
        before_provider_admission=lambda: admission_open,
    )
    messages: list = []

    await run_persistent_loop(
        llm_with_tools=llm,
        tools=[tool],
        context_manager=_context_manager(),
        config=_config(),
        system_prompt="system",
        callbacks=callbacks,
        messages=messages,
    )

    assert provider_calls == 1
    call_ids = {
        call["id"]
        for message in persisted
        if isinstance(message, AIMessage)
        for call in (message.tool_calls or [])
    }
    result_ids = {
        message.tool_call_id
        for message in persisted
        if isinstance(message, ToolMessage)
    }
    assert call_ids == result_ids == {"call_term"}
    assert callbacks.on_turn_complete.await_count == 1

    replacement_inputs: list[list] = []

    async def _replacement_stream(provider_input, **_kwargs):
        replacement_inputs.append(list(provider_input))
        yield AIMessage(content="replacement continued")

    replacement = MagicMock(reasoning=None)
    replacement.astream = _replacement_stream
    await run_persistent_loop(
        llm_with_tools=replacement,
        tools=[tool],
        context_manager=_context_manager(),
        config=_config(),
        system_prompt="system",
        callbacks=_callbacks(["next wake"]),
        messages=list(messages),
    )
    restored_calls = {
        call["id"]
        for message in replacement_inputs[0]
        if isinstance(message, AIMessage)
        for call in (message.tool_calls or [])
    }
    restored_results = {
        message.tool_call_id
        for message in replacement_inputs[0]
        if isinstance(message, ToolMessage)
    }
    assert restored_calls == restored_results == {"call_term"}


@pytest.mark.asyncio
async def test_termination_fence_survives_model_and_tool_hot_swap():
    provider_calls = 0

    async def _stream(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        yield AIMessage(content="must not run")

    original = MagicMock(reasoning=None)
    original.astream = _stream
    rebuilt = MagicMock(reasoning=None)
    rebuilt.astream = _stream
    rebuilt_tool = MagicMock()
    rebuilt_tool.name = "list_jobs"
    authorization = AsyncMock(return_value=(True, "maintained"))
    callbacks = _callbacks(
        ["direct", {"id": "queued", "role": "event", "content": "wake"}],
        before_turn_authorization=authorization,
        before_provider_admission=lambda: False,
    )
    manager = _context_manager()
    current = MagicMock(return_value=(rebuilt, [rebuilt_tool]))

    await run_persistent_loop(
        llm_with_tools=original,
        tools=[],
        context_manager=manager,
        config=_config(),
        system_prompt="system",
        callbacks=callbacks,
        messages=[],
        get_current_tools=current,
    )

    assert current.call_count == 2
    assert callbacks.persist_message.await_count == 2
    assert authorization.await_count == 0
    assert manager.ensure_within_limits.await_count == 0
    assert provider_calls == 0


@pytest.mark.asyncio
async def test_precompaction_background_capture_cannot_start_provider_after_fence():
    """A task scheduled pre-fence re-checks at the actual aux invocation."""

    entered = asyncio.Event()
    release = asyncio.Event()
    gate_open = True
    provider = MagicMock()
    provider.ainvoke = AsyncMock(return_value=AIMessage(content="must not run"))
    aux = AuxiliaryLLM(provider)
    aux.set_provider_admission_gate(lambda: gate_open)

    class _Writer:
        event_kinds = frozenset({"pre_compaction"})

        async def on_event(self, _event):
            entered.set()
            await release.wait()
            await aux.ainvoke([], task_name="pre_compaction")

    manager = MemoryManager(
        SimpleNamespace(),
        writers=[("pre_compaction", _Writer())],
    )
    task = manager.capture_nowait(
        CaptureEvent(kind="pre_compaction", messages=[], phase=0)
    )
    await entered.wait()
    gate_open = False
    release.set()
    await task

    provider.ainvoke.assert_not_awaited()
    assert manager.background_tasks_inflight == 0
    assert aux.provider_calls_inflight == 0


@pytest.mark.asyncio
async def test_failed_officer_maintenance_fences_queued_auxiliary_and_rebuild(
    monkeypatch, tmp_path
):
    """A queued aux call re-checks authorization after the pre-turn failure."""

    entered = asyncio.Event()
    release = asyncio.Event()
    boot_model = MagicMock()
    boot_model.ainvoke = AsyncMock(return_value=AIMessage(content="must not run"))
    boot_aux = AuxiliaryLLM(boot_model)
    officer_config = SimpleNamespace(officer=SimpleNamespace(enabled=True))
    session = SimpleNamespace(
        config=officer_config,
        auxiliary_llm=boot_aux,
        memory_service=None,
    )
    client = SimpleNamespace(
        maintain_runtime_actor=AsyncMock(
            side_effect=[
                (False, "verification maintenance failed"),
                (True, "verification maintenance recovered"),
            ]
        )
    )
    monkeypatch.setattr(persistent_app, "_session", session)
    monkeypatch.setattr(persistent_app, "_orchestrator_client", client)
    monkeypatch.setattr(
        persistent_app, "_TERMINATION_SENTINEL_PATH", tmp_path / "terminating"
    )
    monkeypatch.setattr(persistent_app, "_termination_admission_fenced", False)
    monkeypatch.setattr(persistent_app, "_runtime_authorization_admission_open", True)
    persistent_app._wire_session_aux_archiver()

    class _Writer:
        event_kinds = frozenset({"pre_compaction"})

        async def on_event(self, _event):
            entered.set()
            await release.wait()
            await boot_aux.ainvoke([], task_name="pre_compaction")

    manager = MemoryManager(
        SimpleNamespace(),
        writers=[("pre_compaction", _Writer())],
    )
    queued = manager.capture_nowait(
        CaptureEvent(kind="pre_compaction", messages=[], phase=0)
    )
    await entered.wait()

    assert await persistent_app._loop_before_turn_authorization() == (
        False,
        "verification maintenance failed",
    )
    # The primary loop remains able to reach a later maintenance retry, while
    # background provider work is closed immediately.
    assert persistent_app._loop_provider_admission_open() is True
    assert persistent_app._loop_auxiliary_provider_admission_open() is False
    release.set()
    await queued
    boot_model.ainvoke.assert_not_awaited()

    # A live config rebuild gets the same process-owned callback rather than
    # inheriting an open provider gate from a new AuxiliaryLLM instance.
    rebuilt_model = MagicMock()
    rebuilt_model.ainvoke = AsyncMock(return_value=AIMessage(content="recovered"))
    rebuilt_aux = AuxiliaryLLM(rebuilt_model)
    session.auxiliary_llm = rebuilt_aux
    persistent_app._wire_session_aux_archiver()
    with pytest.raises(AuxiliaryProviderAdmissionClosed):
        await rebuilt_aux.ainvoke([], task_name="rebuilt_while_failed")
    rebuilt_model.ainvoke.assert_not_awaited()

    assert await persistent_app._loop_before_turn_authorization() == (
        True,
        "verification maintenance recovered",
    )
    assert persistent_app._loop_auxiliary_provider_admission_open() is True
    response = await rebuilt_aux.ainvoke([], task_name="rebuilt_after_recovery")
    assert response.content == "recovered"
    rebuilt_model.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_auxiliary_gate_survives_rebuild_and_quiescence_tracks_inflight(
    monkeypatch, tmp_path
):
    started = asyncio.Event()
    release = asyncio.Event()

    async def _invoke(_messages):
        started.set()
        await release.wait()
        return AIMessage(content="done")

    model = MagicMock()
    model.ainvoke = AsyncMock(side_effect=_invoke)
    rebuilt = AuxiliaryLLM(model)
    session = SimpleNamespace(auxiliary_llm=rebuilt, memory_service=None)
    monkeypatch.setattr(persistent_app, "_session", session)
    monkeypatch.setattr(
        persistent_app, "_TERMINATION_SENTINEL_PATH", tmp_path / "terminating"
    )
    monkeypatch.setattr(persistent_app, "_termination_admission_fenced", False)
    monkeypatch.setattr(persistent_app, "_thread_id", str(uuid4()))
    monkeypatch.setattr(persistent_app, "_tool_inflight", False)
    monkeypatch.setattr(persistent_app, "_turn_event_open", False)
    monkeypatch.setattr(persistent_app, "_loop_task", None)
    persistent_app._wire_session_aux_archiver()

    call = asyncio.create_task(rebuilt.ainvoke([], task_name="rebuilt_aux"))
    await started.wait()
    assert rebuilt.provider_calls_inflight == 1
    assert persistent_app._termination_quiescent() is False
    release.set()
    await call
    assert persistent_app._termination_quiescent() is True

    persistent_app.activate_termination_admission_fence("test")
    with pytest.raises(AuxiliaryProviderAdmissionClosed):
        await rebuilt.ainvoke([], task_name="after_fence")
    assert model.ainvoke.await_count == 1


@pytest.mark.asyncio
async def test_callback_absence_preserves_ordinary_persistent_behavior():
    calls = 0

    async def _stream(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        yield AIMessage(content="ordinary reply")

    llm = MagicMock(reasoning=None)
    llm.astream = _stream
    await run_persistent_loop(
        llm_with_tools=llm,
        tools=[],
        context_manager=_context_manager(),
        config=_config(),
        system_prompt="system",
        callbacks=_callbacks(["hello"]),
        messages=[],
    )
    assert calls == 1


@pytest.mark.asyncio
async def test_terminating_wake_rejection_and_response_loss_retry_once(
    monkeypatch,
):
    """A 503/lost response leaves the durable row claimable with one id."""

    thread_id = str(uuid4())
    event_id = 41
    delivery_id = str(uuid4())
    row = {
        "id": event_id,
        "thread_id": thread_id,
        "project_id": None,
        "source": "timer",
        "dedup_key": "timer",
        "payload": {
            "minutes": 30,
            "reason": "wake after replacement",
            "_delivery_id": delivery_id,
        },
        "delivery_id": delivery_id,
    }
    db = SimpleNamespace(
        claim_pending_session_wake_events=AsyncMock(return_value=[row]),
        assign_session_wake_delivery_groups=AsyncMock(return_value=[row]),
        get_session_wake_delivery_group=AsyncMock(return_value=[row]),
        get_thread=AsyncMock(
            return_value={
                "id": thread_id,
                "project_id": None,
                "metadata": {"config_override": {"officer": {"enabled": True}}},
            }
        ),
        finish_session_wake_events=AsyncMock(),
        release_session_wake_events=AsyncMock(),
        defer_session_wake_events=AsyncMock(),
        merge_thread_officer_state=AsyncMock(),
    )
    monkeypatch.setattr(
        session_wake,
        "_resolve_live_agent",
        AsyncMock(return_value={"pod_ip": "127.0.0.1", "pod_port": 8001}),
    )
    inject = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(session_wake, "_inject_live", inject)
    monkeypatch.setattr(
        session_wake,
        "_officer_ceiling_deferral",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        sitrep,
        "build_wake_message",
        AsyncMock(return_value=("bounded wake", {"fingerprint": "next"})),
    )

    assert await session_wake.drain_pending_event_wakes(db) == 0
    db.release_session_wake_events.assert_awaited_once_with(
        [event_id], max_attempts=session_wake._OFFICER_MAX_ATTEMPTS
    )
    db.finish_session_wake_events.assert_not_awaited()

    assert await session_wake.drain_pending_event_wakes(db) == 1
    db.finish_session_wake_events.assert_awaited_once_with([event_id])
    assert [call.kwargs["delivery_id"] for call in inject.await_args_list] == [
        delivery_id,
        delivery_id,
    ]


@pytest.mark.asyncio
async def test_orchestrator_surfaces_terminating_direct_input_as_retryable(
    monkeypatch,
):
    from fastapi import HTTPException
    from orchestrator import main as orchestrator_main

    class _Response:
        status_code = 503
        text = '{"error":"runtime_terminating"}'
        headers = {"Retry-After": "7"}

        @staticmethod
        def json():
            return {"error": "runtime_terminating", "retryable": True}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr(orchestrator_main.httpx, "AsyncClient", _Client)

    with pytest.raises(HTTPException) as caught:
        await orchestrator_main._forward_to_agent(
            {"id": str(uuid4()), "pod_ip": "127.0.0.1", "pod_port": 8001},
            "/api/input",
            {"content": "retain me"},
        )

    assert caught.value.status_code == 503
    assert caught.value.detail["error"] == "runtime_terminating"
    assert caught.value.detail["retryable"] is True
    assert caught.value.headers == {"Retry-After": "7"}
