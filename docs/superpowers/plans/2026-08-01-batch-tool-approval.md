# Batch Tool-Call Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show every tool call in a parallel batch in one approval card, decided with one click, instead of one card at a time gated behind each tool finishing.

**Architecture:** Pre-announce the whole batch — insert all pending permission rows and broadcast one frame — *before* the tool loop starts executing. The existing per-call gate loop is left untouched; it drains instantly because `_loop_permission_check` already short-circuits on a decided row. Approved tools still execute sequentially.

**Tech Stack:** Python 3.12 (CI gate) / asyncio / asyncpg / FastAPI · Angular 20 signals + vitest · pytest

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-01-batch-tool-approval-design.md`. Predecessor (shipped, live-gated): `docs/issues/supervised_parallel_gates_timeout_fabricates_denial.md`.
- Work on `develop`. Commit per task. **Never push without asking.**
- Execution stays **sequential** — do not parallelise tool execution.
- `Approve all` covers every call in the batch, including shell/write calls. No per-tool risk classification.
- Every decision the cockpit sends **must** carry an explicit `approval_id`. `_resolve_pending_permission`'s no-id fallback resolves "most-recent-pending", which is wrong when N gates are open.
- Batch event name: `permission.request_batch`, payload `{"requests": [{id, approval_id, tool, args}, ...]}`.
- `thread_permission_requests` has **no** unique constraint on `(thread_id, tool_call_id)` — duplicate inserts are silently possible.
- Local pytest is noisy on Python 3.14; CI (3.12) is the real gate. `test_database_phase1` (needs local Postgres) and `test_endpoint_inventory` (unrelated in-flight work) fail locally already — ignore those two.
- Run `python -m ruff check src/` before each commit; `npx tsc --noEmit -p tsconfig.json` from `cockpit/` for frontend tasks.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/persistent_graph.py` | Add optional `announce_permission_batch` callback; call it once before the per-call gate loop. Gate loop itself unchanged. |
| `src/api/persistent_app.py` | New `_loop_announce_permission_batch()`; teach `_loop_permission_check` to claim an announced pending row instead of inserting a duplicate; wire the callback. |
| `cockpit/src/app/core/services/persistent-chat.service.ts` | `pendingPermission` scalar → `pendingPermissions` list; handle `permission.request_batch`; approve/deny all with explicit ids. |
| `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` | One card rendering the whole list. |
| `tests/test_persistent_graph_permission_outcomes.py` | Extend: announce is called once before gating. |
| `tests/test_persistent_app_permission_batch.py` | **New.** Announce insert/broadcast/skip rules; row claiming. |
| `cockpit/src/app/core/services/persistent-chat.service.spec.ts` | Extend: batch frame, approve-all ids, resolved removal, reload restore. |

---

## Task 1: Announce callback on the loop

**Files:**
- Modify: `src/persistent_graph.py` (callbacks dataclass ~line 441; `_execute_turn` tool loop ~line 2152)
- Test: `tests/test_persistent_graph_permission_outcomes.py`

**Interfaces:**
- Consumes: `PersistentLoopCallbacks`, `PermissionOutcome` (already exist).
- Produces: `PersistentLoopCallbacks.announce_permission_batch: Optional[Callable[[List[Dict[str, Any]]], Awaitable[None]]] = None` — receives the raw `response.tool_calls` list (each entry has `name`, `args`, `id`). Called exactly once per LLM response that carries tool calls, before any gate.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_persistent_graph_permission_outcomes.py`:

```python
class TestBatchAnnounce:
    @pytest.mark.asyncio
    async def test_announce_called_once_with_all_calls_before_gating(self):
        """The whole batch must be announced before the first gate blocks,
        so the user sees every card at once instead of one per finished tool."""
        seen = []
        gate_order = []

        announce = AsyncMock(side_effect=lambda calls: seen.append(list(calls)))

        async def _permission_check(tool_name, tool_args, tool_call_id):
            gate_order.append(tool_call_id)
            return PermissionOutcome.APPROVED

        messages: list[BaseMessage] = []
        await _run_turn_with_parallel_tools(
            permission_check=_permission_check,
            messages=messages,
            n_calls=4,
            announce_permission_batch=announce,
        )

        assert announce.await_count == 1
        assert [c["id"] for c in seen[0]] == ["tc_0", "tc_1", "tc_2", "tc_3"]
        # Announced before any gate ran.
        assert gate_order == ["tc_0", "tc_1", "tc_2", "tc_3"]

    @pytest.mark.asyncio
    async def test_no_announce_callback_still_works(self):
        """Back-compat: callers that never set the callback are unaffected."""

        async def _permission_check(tool_name, tool_args, tool_call_id):
            return PermissionOutcome.APPROVED

        messages: list[BaseMessage] = []
        tool = await _run_turn_with_parallel_tools(
            permission_check=_permission_check, messages=messages, n_calls=2
        )
        assert tool.ainvoke.await_count == 2
```

Extend the existing helper in the same file so it can pass the callback through — replace its signature and the `_make_callbacks` call:

```python
async def _run_turn_with_parallel_tools(
    *,
    permission_check,
    messages: list[BaseMessage],
    n_calls: int = 2,
    announce_permission_batch=None,
):
```

and inside it:

```python
    callbacks = _make_callbacks(
        get_user_input=_input,
        permission_check=permission_check,
        announce_permission_batch=announce_permission_batch,
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_persistent_graph_permission_outcomes.py::TestBatchAnnounce -v`
Expected: FAIL — `TypeError: PersistentLoopCallbacks.__init__() got an unexpected keyword argument 'announce_permission_batch'`

- [ ] **Step 3: Add the callback field**

In `src/persistent_graph.py`, in `PersistentLoopCallbacks`, alongside the other optional callbacks (after `on_context_compacted`):

```python
    # Announce a whole batch of tool calls for approval at once, before any
    # of them is gated. Lets the client render one card listing every call
    # instead of one card per finished tool. Optional: None ⇒ the per-call
    # gate path announces each call itself (previous behaviour).
    # See docs/superpowers/specs/2026-08-01-batch-tool-approval-design.md.
    announce_permission_batch: Optional[
        Callable[[List[Dict[str, Any]]], Awaitable[None]]
    ] = None
```

- [ ] **Step 4: Call it before the gate loop**

In `_execute_turn`, immediately after the `if not hasattr(response, "tool_calls") or not response.tool_calls: break` guard and before `for i, tool_call in enumerate(response.tool_calls):`:

```python
        # Announce the whole batch up front so the client can show every
        # pending call at once. Soft-fail: if this breaks, the per-call gate
        # path below still inserts and prompts exactly as before.
        if callbacks.announce_permission_batch is not None:
            try:
                await callbacks.announce_permission_batch(response.tool_calls)
            except Exception as e:
                logger.warning("Permission batch announce failed: %s", e)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_persistent_graph_permission_outcomes.py -v`
Expected: PASS (8 tests — 6 existing + 2 new)

- [ ] **Step 6: Verify nothing else regressed**

Run: `python -m pytest tests/test_persistent_graph.py tests/test_persistent_graph_image.py tests/test_persistent_app.py -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
python -m ruff check src/
git add src/persistent_graph.py tests/test_persistent_graph_permission_outcomes.py
git commit -m "feat(permissions): announce a tool-call batch before gating"
```

---

## Task 2: Announce implementation — insert rows + one broadcast

**Files:**
- Modify: `src/api/persistent_app.py` (near `_insert_permission_request`, ~line 4127)
- Test: `tests/test_persistent_app_permission_batch.py` (create)

**Interfaces:**
- Consumes: `_insert_permission_request(tool_call_id, tool_name, tool_args) -> Optional[str]`, `_broadcast(event, payload)`, `_safe_serialize`, module globals `_session`, `_thread_id`.
- Produces: `async def _loop_announce_permission_batch(tool_calls: List[Dict[str, Any]]) -> None` — inserts one pending row per gate-needing call and emits one `permission.request_batch` broadcast. No return value.

- [ ] **Step 1: Write the failing test**

Create `tests/test_persistent_app_permission_batch.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_persistent_app_permission_batch.py -v`
Expected: FAIL — `AttributeError: module 'src.api.persistent_app' has no attribute '_loop_announce_permission_batch'`

- [ ] **Step 3: Implement the announce**

In `src/api/persistent_app.py`, directly after `_insert_permission_request`:

```python
_SHELL_TOOLS = {"run_command", "shell_execute", "shell_read"}


def _gate_needed(mode: str, tool_name: str) -> bool:
    """Whether this call would actually hit a permission gate.

    Mirrors the early-returns in ``_loop_permission_check`` so the announce
    never creates a row for a call that auto-approves.
    """
    if mode == "autonomous":
        return False
    if mode == "auto_accept":
        return tool_name in _SHELL_TOOLS
    return True


async def _loop_announce_permission_batch(tool_calls: List[Dict[str, Any]]) -> None:
    """Insert a pending row for every gate-needing call in one batch, then
    emit a single ``permission.request_batch`` frame.

    Lets the cockpit show every pending call at once instead of one card per
    finished tool. The per-call gate path then *claims* these rows rather
    than inserting its own — see ``_loop_permission_check``.
    """
    if _session is None or _thread_id is None:
        return
    mode = _session.permission_mode
    gated = [tc for tc in tool_calls if _gate_needed(mode, tc.get("name", ""))]
    if not gated:
        return

    requests: List[Dict[str, Any]] = []
    for tc in gated:
        tool_call_id = tc.get("id") or ""
        tool_name = tc.get("name", "")
        tool_args = tc.get("args", {}) or {}
        if not tool_call_id:
            continue
        request_id = await _insert_permission_request(
            tool_call_id, tool_name, tool_args
        )
        if request_id is None:
            # DB refused this row — leave it to the per-call gate path.
            continue
        requests.append(
            {
                "id": tool_call_id,
                "approval_id": request_id,
                "tool": tool_name,
                "args": _safe_serialize(tool_args),
            }
        )

    if requests:
        _broadcast("permission.request_batch", {"requests": requests})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_persistent_app_permission_batch.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
python -m ruff check src/
git add src/api/persistent_app.py tests/test_persistent_app_permission_batch.py
git commit -m "feat(permissions): batch-announce pending tool gates in one frame"
```

---

## Task 3: Claim the announced row instead of inserting a duplicate

**Files:**
- Modify: `src/api/persistent_app.py` (`_loop_permission_check`, the pre-check block ~line 4294 and the insert ~line 4333)
- Test: `tests/test_persistent_app_permission_batch.py`

**Interfaces:**
- Consumes: `_loop_announce_permission_batch` (Task 2), `_wait_for_permission_resolution(request_id, timeout) -> str`, `PermissionOutcome`.
- Produces: no new public symbol — `_loop_permission_check` keeps its signature and `PermissionOutcome` return.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_persistent_app_permission_batch.py`:

```python
from src.persistent_graph import PermissionOutcome


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_persistent_app_permission_batch.py::TestClaimsAnnouncedRow -v`
Expected: FAIL — `insert.assert_not_awaited()` raises, because the current pre-check only matches `status IN ('approved','denied')` and falls through to a fresh INSERT.

- [ ] **Step 3: Widen the pre-check to claim pending rows**

In `_loop_permission_check`, replace the wake-path SELECT block. The current query selects `status IN ('approved', 'denied')`; change it to include `pending` and branch on what it finds:

```python
    claimed_request_id: Optional[str] = None
    if _session.postgres_conn is not None and _thread_id is not None:
        try:
            async with _session.postgres_conn.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT id, status FROM thread_permission_requests "
                    "WHERE thread_id = $1 AND tool_call_id = $2 "
                    "  AND status IN ('approved', 'denied', 'pending') "
                    "ORDER BY decided_at DESC NULLS LAST, requested_at DESC "
                    "LIMIT 1",
                    _thread_id,
                    tool_call_id,
                )
            if existing is not None and existing["status"] != "pending":
                decision = existing["status"]
                _session.tool_decisions[tool_call_id] = decision
                logger.info(
                    "Phase 5 wake: reusing prior %s decision for tool_call %s "
                    "(tool=%s)",
                    decision,
                    tool_call_id,
                    tool_name,
                )
                return (
                    PermissionOutcome.APPROVED
                    if decision == "approved"
                    else PermissionOutcome.DECLINED
                )
            if existing is not None:
                # A batch announce already inserted this row. Claim it —
                # inserting again would orphan a card nobody waits on
                # (no unique constraint on thread_id + tool_call_id).
                claimed_request_id = str(existing["id"])
        except Exception as e:
            # Soft-fail: fall through to the regular INSERT-and-wait path.
            logger.warning(
                "Wake-path SELECT for tool_call %s failed (%s); falling back",
                tool_call_id,
                e,
            )
```

- [ ] **Step 4: Use the claimed id and suppress the duplicate broadcast**

Replace the insert-and-broadcast block that follows (currently `request_id = await _insert_permission_request(...)` through the `_broadcast("permission.request", ...)` call):

```python
    # Supervised mode (or shell under auto_accept): ask user via the
    # durable permission table, then wait on LISTEN/NOTIFY.
    if claimed_request_id is not None:
        request_id = claimed_request_id
    else:
        request_id = await _insert_permission_request(
            tool_call_id, tool_name, tool_args
        )
        if request_id is None:
            # DB unavailable — conservative deny rather than risk silent
            # auto-approval. Logged at WARNING by the insert helper. This is a
            # real DECLINE, not a park: with no durable row there is nothing a
            # later approval could resolve.
            if _session is not None:
                _session.tool_decisions[tool_call_id] = "denied"
            return PermissionOutcome.DECLINED

        # Broadcast carries both ids so clients can refer back via either.
        # Skipped when the row was claimed: the batch frame already announced
        # it, and a second frame would duplicate the card.
        _broadcast(
            "permission.request",
            {
                "id": tool_call_id,
                "approval_id": request_id,
                "tool": tool_name,
                "args": _safe_serialize(tool_args),
            },
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_persistent_app_permission_batch.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Verify the shipped gate behaviour is untouched**

Run: `python -m pytest tests/test_persistent_app_permission_outcomes.py tests/test_thread_permissions_phase3.py tests/test_attention_sleep_phase5.py -q`
Expected: all pass — these pin the three-state outcomes, tethered no-expiry, and wake path.

- [ ] **Step 7: Commit**

```bash
python -m ruff check src/
git add src/api/persistent_app.py tests/test_persistent_app_permission_batch.py
git commit -m "fix(permissions): claim an announced gate row instead of double-inserting"
```

---

## Task 4: Wire the callback into the loop

**Files:**
- Modify: `src/api/persistent_app.py` (the `PersistentLoopCallbacks(...)` construction, ~line 390)
- Test: `tests/test_persistent_app_permission_batch.py`

**Interfaces:**
- Consumes: `_loop_announce_permission_batch` (Task 2), `PersistentLoopCallbacks.announce_permission_batch` (Task 1).
- Produces: nothing new — the wiring makes the feature live.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_persistent_app_permission_batch.py`:

```python
class TestCallbackWiring:
    @pytest.mark.asyncio
    async def test_loop_callbacks_carry_the_announce_hook(self):
        """Without this wiring the batch card never appears in production.

        Captures the real PersistentLoopCallbacks the session builds, rather
        than asserting on source text.
        """
        captured = {}

        def _fake_run(**kwargs):
            captured["callbacks"] = kwargs.get("callbacks")

            async def _noop():
                return None

            return _noop()

        with (
            patch.object(pa, "_session", _mock_session()),
            patch.object(pa, "_thread_id", "tid"),
            patch.object(pa, "_loop_task", None),
            patch.object(pa, "_session_ready", lambda: True),
            patch.object(pa, "run_persistent_loop", _fake_run),
        ):
            pa._ensure_persistent_loop_started("test")

        cb = captured["callbacks"]
        assert cb is not None, "loop was never started"
        assert cb.announce_permission_batch is pa._loop_announce_permission_batch
```

Note: `_ensure_persistent_loop_started` creates an asyncio task, so the test must be async (an event loop must be running). If additional module globals turn out to be required for it to reach the callbacks construction, install them the same way the existing `tests/test_persistent_app.py` helpers do — do **not** fall back to asserting on source text.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_persistent_app_permission_batch.py::TestCallbackWiring -v`
Expected: FAIL — `AssertionError` / `AttributeError`, because `announce_permission_batch` is not passed when the callbacks are constructed.

- [ ] **Step 3: Wire it**

In the `PersistentLoopCallbacks(` construction, next to `permission_check=_loop_permission_check,`:

```python
            announce_permission_batch=_loop_announce_permission_batch,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_persistent_app_permission_batch.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
python -m ruff check src/
git add src/api/persistent_app.py tests/test_persistent_app_permission_batch.py
git commit -m "feat(permissions): wire batch announce into the session loop"
```

---

## Task 5: Cockpit — list signal and batch frame

**Files:**
- Modify: `cockpit/src/app/core/services/persistent-chat.service.ts` (signal ~line 584; `session.state` handler ~line 2717; `permission.request` ~line 2821; `permission.resolved` ~line 2842; `approve`/`deny`/`_resolvePermission` ~line 2482-2541)
- Test: `cockpit/src/app/core/services/persistent-chat.service.spec.ts`

**Interfaces:**
- Consumes: existing `PermissionRequest` interface `{id: string; approvalId?: string; tool: string; args: Record<string, unknown>}`.
- Produces: `readonly pendingPermissions = signal<PermissionRequest[]>([])`; `approveAll(): void`; `denyAll(): void`. The existing scalar `pendingPermission` is **removed** — Task 6 updates its only template consumer.

- [ ] **Step 1: Write the failing test**

Append inside the `PersistentChatService — SSE event dispatch` describe block in `persistent-chat.service.spec.ts`:

```typescript
    it('shows every call in a batch at once', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {
            method: 'permission.request_batch',
            params: {
                requests: [
                    {id: 'tc-0', approval_id: 'a-0', tool: 'web_search', args: {q: 'fr'}},
                    {id: 'tc-1', approval_id: 'a-1', tool: 'web_search', args: {q: 'jp'}},
                ],
            },
        }, '1:1');
        expect(service.pendingPermissions().map((p) => p.id)).toEqual(['tc-0', 'tc-1']);
        expect(service.pendingPermissions()[1].approvalId).toBe('a-1');
    });

    it('approveAll sends an explicit approval_id per entry', async () => {
        // _resolve_pending_permission falls back to "most-recent-pending"
        // when no id is given — with N gates open that resolves the WRONG one.
        const {service, es, mockHttp} = await setup();
        fireSseMessage(es, {
            method: 'permission.request_batch',
            params: {
                requests: [
                    {id: 'tc-0', approval_id: 'a-0', tool: 'web_search', args: {}},
                    {id: 'tc-1', approval_id: 'a-1', tool: 'web_search', args: {}},
                ],
            },
        }, '1:1');

        service.approveAll();

        const urls = mockHttp.post.mock.calls.map((c: unknown[]) => c[0] as string);
        expect(urls.some((u) => u.endsWith('/approve/a-0'))).toBe(true);
        expect(urls.some((u) => u.endsWith('/approve/a-1'))).toBe(true);
        expect(service.pendingPermissions()).toEqual([]);
    });

    it('permission.resolved removes only its own entry', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {
            method: 'permission.request_batch',
            params: {
                requests: [
                    {id: 'tc-0', approval_id: 'a-0', tool: 'web_search', args: {}},
                    {id: 'tc-1', approval_id: 'a-1', tool: 'web_search', args: {}},
                ],
            },
        }, '1:1');
        fireSseMessage(es, {
            method: 'permission.resolved',
            params: {id: 'tc-0', approval_id: 'a-0', decision: 'approved'},
        }, '1:2');
        expect(service.pendingPermissions().map((p) => p.id)).toEqual(['tc-1']);
    });

    it('restores a multi-entry batch from the session.state welcome frame', async () => {
        const {service, es} = await setup();
        fireSseMessage(es, {
            method: 'session.state',
            params: {
                pending_permissions: [
                    {id: 'tc-0', approval_id: 'a-0', tool: 'web_search', args: {}},
                    {id: 'tc-1', approval_id: 'a-1', tool: 'web_search', args: {}},
                ],
            },
        }, '1:1');
        expect(service.pendingPermissions().map((p) => p.id)).toEqual(['tc-0', 'tc-1']);
    });
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `cockpit/`): `npx vitest run src/app/core/services/persistent-chat.service.spec.ts`
Expected: FAIL — `service.pendingPermissions is not a function`

- [ ] **Step 3: Replace the scalar signal with a list**

At `persistent-chat.service.ts:584`, replace `readonly pendingPermission = signal<PermissionRequest | null>(null);` with:

```typescript
    /** Every gate currently awaiting a decision. A parallel tool batch puts
     *  all of its calls here at once so one card can list them —
     *  docs/superpowers/specs/2026-08-01-batch-tool-approval-design.md. */
    readonly pendingPermissions = signal<PermissionRequest[]>([]);
```

Add a shared mapper near the other private helpers (wire format is snake_case; the client type is camelCase — an unmapped `approvalId` silently degrades every decision to "most-recent-pending"):

```typescript
    private _toPermissionRequests(raw: unknown): PermissionRequest[] {
        const list = (raw as Record<string, unknown>[]) || [];
        return list
            .filter((r) => typeof r?.['id'] === 'string' && r['id'])
            .map((r) => {
                const approvalId = r['approval_id'] as string | undefined;
                return {
                    id: r['id'] as string,
                    ...(approvalId ? {approvalId} : {}),
                    tool: (r['tool'] as string) || '',
                    args: (r['args'] as Record<string, unknown>) || {},
                };
            });
    }
```

- [ ] **Step 4: Update the event handlers**

`session.state` — replace the `pending_permissions` block added in the previous fix:

```typescript
                if ('pending_permissions' in params) {
                    const list = this._toPermissionRequests(params['pending_permissions']);
                    if (list.length > 0) {
                        this.pendingPermissions.set(list);
                        for (const req of list) {
                            this.dispatch({
                                type: 'permission_request',
                                toolUseId: req.id,
                                tool: req.tool,
                                args: req.args ?? {},
                                timestamp: now,
                            });
                        }
                    }
                }
```

Add a new case next to `permission.request`:

```typescript
            case 'permission.request_batch': {
                const list = this._toPermissionRequests(params['requests']);
                if (list.length > 0) {
                    this.pendingPermissions.set(list);
                    for (const req of list) {
                        this.dispatch({
                            type: 'permission_request',
                            toolUseId: req.id,
                            tool: req.tool,
                            args: req.args ?? {},
                            timestamp: now,
                        });
                    }
                }
                break;
            }
```

`permission.request` — append instead of replacing:

```typescript
            case 'permission.request': {
                const id = (params['id'] as string) || '';
                const tool = (params['tool'] as string) || '';
                const args = (params['args'] as Record<string, unknown>) || {};
                const approvalId = (params['approval_id'] as string) || undefined;
                if (id) {
                    const entry: PermissionRequest = {
                        id,
                        ...(approvalId ? {approvalId} : {}),
                        tool,
                        args,
                    };
                    this.pendingPermissions.update((list) =>
                        list.some((p) => p.id === id) ? list : [...list, entry],
                    );
                }
                this.dispatch({
                    type: 'permission_request',
                    toolUseId: id,
                    tool,
                    args,
                    timestamp: now,
                });
                break;
            }
```

`permission.resolved` — remove just that entry:

```typescript
                this.pendingPermissions.update((list) =>
                    list.filter((p) => p.id !== resolvedId),
                );
```

- [ ] **Step 5: Replace approve/deny with batch verbs**

Replace the existing `approve()` and `deny()` bodies:

```typescript
    /** Approve every pending gate. Each decision carries its own approval_id:
     *  the no-id REST fallback resolves "most-recent-pending", which is the
     *  wrong gate when a batch is open. */
    approveAll(): void {
        const pending = this.pendingPermissions();
        this.pendingPermissions.set([]);
        for (const req of pending) {
            this.dispatch({
                type: 'permission_decision',
                toolUseId: req.id,
                decision: 'approved',
                timestamp: Date.now(),
            });
            this._resolvePermission(req, 'approve');
        }
    }

    /** Deny every pending gate. */
    denyAll(): void {
        const pending = this.pendingPermissions();
        this.pendingPermissions.set([]);
        for (const req of pending) {
            this.dispatch({
                type: 'permission_decision',
                toolUseId: req.id,
                decision: 'denied',
                timestamp: Date.now(),
            });
            this._resolvePermission(req, 'deny');
        }
    }
```

Update `stop()` to use the batch verb:

```typescript
    stop(): void {
        this.denyAll();
        void this.interrupt();
    }
```

Update the reset at line ~2040 (`this.pendingPermission.set(null);`) to `this.pendingPermissions.set([]);`.

- [ ] **Step 6: Run tests to verify they pass**

Run: `npx vitest run src/app/core/services/persistent-chat.service.spec.ts`
Expected: PASS. Pre-existing specs that referenced the old scalar (around lines 1255, 1955-2011) must be updated to `pendingPermissions()` — their intent is unchanged; assert on the list's first entry.

- [ ] **Step 7: Commit**

```bash
npx tsc --noEmit -p tsconfig.json
git add cockpit/src/app/core/services/persistent-chat.service.ts \
        cockpit/src/app/core/services/persistent-chat.service.spec.ts
git commit -m "feat(cockpit): track all pending tool gates as a list"
```

---

## Task 6: Cockpit — one card for the whole batch

**Files:**
- Modify: `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` (approval card ~line 1281-1296; `permissionTitle` ~line 3375; `approveAndAutoAccept` ~line 2957)
- Modify: `cockpit/src/assets/i18n/en.json` (and sibling locale files that carry `chat.permission.*`)

**Interfaces:**
- Consumes: `chat.pendingPermissions()`, `chat.approveAll()`, `chat.denyAll()`, `chat.stop()` (Task 5).
- Produces: user-visible card. No new exported symbol.

- [ ] **Step 1: Replace the card markup**

Replace the `@if (chat.pendingPermission(); as perm) { ... }` block with:

```html
        @if (chat.pendingPermissions().length > 0) {
          <div class="mile-marker mile-permission">
            <div class="mile-label">{{ 'chat.permission.title' | transloco }}</div>
            <div class="mile-title">
              {{ 'chat.permission.batchTitle' | transloco: {count: chat.pendingPermissions().length} }}
            </div>
            <ul class="permission-list">
              @for (perm of chat.pendingPermissions(); track perm.id) {
                <li class="permission-row">
                  <app-icon size="sm">{{ toolIcon(perm.tool) }}</app-icon>
                  <code class="permission-tool">{{ perm.tool }}</code>
                  <code class="permission-args">{{ permissionArgs(perm) }}</code>
                </li>
              }
            </ul>
            <div class="mile-actions">
              <app-button variant="success" size="sm" (clicked)="chat.approveAll()">{{ 'chat.permission.approveAll' | transloco }}</app-button>
              <app-button variant="info" size="sm" (clicked)="approveAndAutoAccept()">{{ 'chat.permission.autoAccept' | transloco }}</app-button>
              <app-button variant="danger" size="sm" (clicked)="chat.stop()">{{ 'chat.permission.stop' | transloco }}</app-button>
            </div>
          </div>
        }
```

- [ ] **Step 2: Add the arg-rendering helper**

Replace `permissionTitle(perm: PermissionRequest): string` with:

```typescript
    /** Compact one-line args preview so the user can see WHAT each call does
     *  before approving the batch — including a destructive shell command. */
    permissionArgs(perm: PermissionRequest): string {
        const args = perm.args ?? {};
        const parts = Object.entries(args).map(([k, v]) => {
            const s = typeof v === 'string' ? v : JSON.stringify(v);
            return `${k}: ${s.length > 120 ? s.slice(0, 120) + '…' : s}`;
        });
        return parts.join(', ');
    }
```

- [ ] **Step 3: Update approveAndAutoAccept**

```typescript
    approveAndAutoAccept(): void {
        this.chat.setMode('auto_accept');
        this.chat.approveAll();
    }
```

- [ ] **Step 4: Add the i18n strings**

In `cockpit/src/assets/i18n/en.json`, under `chat.permission`:

```json
        "batchTitle": "The agent wants to run {{count}} tool(s)",
        "approveAll": "Approve all"
```

Add the same two keys to every other locale file that already defines `chat.permission.approve` (mirror the English text if no translation is available — a missing key renders the raw key to the user).

- [ ] **Step 5: Fix the streaming-placeholder guard**

At component line ~1182 the empty-turn placeholder checks `!chat.pendingPermission()`. Update to:

```html
                    @if (streaming && turn.events.length === 0 && chat.pendingPermissions().length === 0) {
```

- [ ] **Step 6: Verify build and tests**

Run: `npx tsc --noEmit -p tsconfig.json` — expect exit 0 (this catches every remaining `pendingPermission` reference).
Run: `npx vitest run` — expect all pass.

- [ ] **Step 7: Commit**

```bash
git add cockpit/src/app/views/persistent-chat/persistent-chat.component.ts \
        cockpit/src/assets/i18n/
git commit -m "feat(cockpit): one approval card for a whole tool batch"
```

---

## Task 7: Full verification

**Files:** none modified — verification only.

- [ ] **Step 1: Full backend suite**

Run: `python -m pytest tests/ -q --ignore=tests/cloud --ignore=tests/cloud_sync -p no:randomly`
Expected: all pass except the two known-unrelated failures (`test_database_phase1` ×2 need local Postgres; `test_endpoint_inventory` is stale from separate in-flight work). Any other failure must be fixed before proceeding.

- [ ] **Step 2: Full cockpit suite + typecheck**

Run (from `cockpit/`): `npx vitest run && npx tsc --noEmit -p tsconfig.json`
Expected: all pass, exit 0.

- [ ] **Step 3: Lint**

Run: `python -m ruff check src/`
Expected: `All checks passed!`

- [ ] **Step 4: Report and hand off for the live gate**

Summarise: tests added, suites green, and that the **live gate on dev is still owed** — deploy, then run four parallel `web_search` calls in a Supervised session and confirm (a) all four appear in **one** card, (b) one `Approve all` runs all four sequentially, (c) `get_persistent_thread_messages` shows no fabricated denials, (d) a mid-batch reload still shows the remaining batch. Do not claim the feature works until that passes.

---

## Self-Review

**Spec coverage:** announce-before-gate → Task 1; insert rows + single frame + skip auto-approved → Task 2; claim row / no duplicate / no re-broadcast → Task 3; wiring → Task 4; list signal + explicit-id approve-all + resolved-removal + reload restore → Task 5; one card listing every call with full args + Approve all/Auto-accept/Stop → Task 6; sequential execution → unchanged by construction (no task touches the execute path). Spec tests 1-9 all map to a task. Live gate → Task 7 Step 4.

**Placeholders:** none — every step carries real code or a real command.

**Type consistency:** `announce_permission_batch` (Tasks 1, 4) and `_loop_announce_permission_batch` (Tasks 2, 4) used consistently; `pendingPermissions` / `approveAll` / `denyAll` / `_toPermissionRequests` / `permissionArgs` consistent across Tasks 5-6; `permission.request_batch` + `{"requests": [...]}` identical in Tasks 2 and 5.
