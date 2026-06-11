---
tags:
  - persistent-sessions
  - bug
  - agent
  - race-condition
related:
  - "[[persistent_session_idle_timeout]]"
  - "[[persistent_graph]]"
  - "[[stuck_thread_workspace_pods]]"
---

# `'NoneType' has no attribute 'permission_mode'` During Long Active Sessions

**Reported**: 2026-05-12
**Status**: **Fixes implemented 2026-05-12, awaiting cluster validation.**
All seven proposed fixes shipped. The race itself is closed by Fix 2
(`_detach_session()` now cancels the in-flight loop_task before nulling
`_session`); workspace destruction is closed by Fix 5 (agent-side
`ended` now suspends to S3 instead of deleting the pod+PVC); Fix 3
adds the agent-stderr capture we need to diagnose the *next* failure
if one slips through. See **Resolution** section below for code refs.

## Resolution (2026-05-12)

| Fix | Status | Where it landed |
|-----|--------|-----------------|
| **Fix 6** — ssh in orchestrator image | ✅ shipped | `docker/Dockerfile.orchestrator` runtime stage adds `openssh-client`. |
| **Fix 3** — capture agent stderr before reap | ✅ shipped | New `AgentProvisioner._capture_agent_logs_before_reap()` in `orchestrator/services/agent_provisioner.py`; invoked from `reap_pods` before each `delete_agent_pod`. Last 500 lines emitted at WARNING to orchestrator stderr with `logs_below_marker_BEGIN/END pod=<name>` markers (grep on the orchestrator pod logs to retrieve). |
| **Fix 2** — cancel `loop_task` before detach | ✅ shipped | New module-global `_loop_task` in `src/api/persistent_app.py`. `ws_chat` sets it on task creation, clears it in finally. `_detach_session()` now cancels and awaits `_loop_task` (with `is not asyncio.current_task()` self-deadlock guard) before any other teardown step. Auto-protects all 7 detach callers without per-caller refactoring. |
| **Fix 5** — preserve workspace on agent-side `ended` | ✅ shipped | New `_suspend_thread_resources()` helper in `orchestrator/main.py`. `agent_update_thread_status` routes agent-initiated `ended` through `workspace_suspension_service.suspend_thread_workspace()` (S3 snapshot + restore on resume) instead of the destructive `_release_thread_resources`. The user-facing DELETE handler keeps the aggressive teardown for explicit destruction intent. |
| **Fix 1** — defensive None guards | ✅ shipped | `permission_check` (denies + warns when `_session is None`), `mode.set` WS handler, `_handle_config_update` mid-function assignments — all in `src/api/persistent_app.py`. Belt-and-suspenders on top of Fix 2; only fires if a future code path forgets to cancel before detaching. |
| **Fix 7** — sanitize raw error strings in cockpit | ✅ shipped | New `sanitizeError()` in `cockpit/src/app/core/services/persistent-chat.service.ts`. Maps `NoneType`/`Got unknown type`/`Traceback` patterns to friendly messages; logs originals to console; truncates >240 chars. |
| **Fix 4** — don't drain busy persistent agents | ⏳ deferred | Not urgent: cluster runs `:v0.0.X` semver tags, so SHA-drift never fires. Revisit when image tagging switches to `:sha-...`. |

### Bonus findings during implementation

- **`llm_timeout=600` is NOT the trigger** (Open Question 2 closed). The timeout at `src/persistent_graph.py:179` only wraps the ainvoke RETRY path, not the primary streaming call. When it fires, the error is sent via `callbacks.on_error` and the turn returns cleanly — the loop continues, `_session` is *not* nulled, no `_detach_session()` is invoked. So whatever causes the ~11-min cadence is upstream of this code (LLM provider, WS proxy, or network), not internal.
- **There are 7 `_detach_session()` callers, not 2** (the original audit undercounted). Three are non-racy (boot-WS watchdog, lifespan shutdown, dead-code `POST /session/detach` that the orchestrator never invokes), two are racy as originally identified (`_handle_heartbeat_intents`, `_thread_status_watchdog`), one is safe-by-construction (WS receive `finally` cancels `loop_task` first). The Fix 2 implementation handles all of them at the `_detach_session()` boundary, so the per-caller count no longer matters.

### Original status (pre-fix)
Code-level race verified. Second incident on dev cluster
(thread `6623d88e`) reproduced the same ~10-11 min cadence and added a
**second-order failure**: the orchestrator's `_release_thread_resources`
DELETES the workspace pod when status flips to `ended`, and `/resume`
has no code path to recreate it — only to restore from `suspended`.
Net result: session dies → resume gets stuck on "Booting agent runtime"
forever.

## Observed Behavior

### Incident 1 — prod cluster, thread `8982a22d` (2026-05-12)

While running a coding-agent session on the deployed cluster, after roughly
15 minutes of *active* AI work (many sequential `read_file` / `search_files`
tool calls, no idle time waiting for user input), the cockpit suddenly:

1. Showed an error toast: **`'NoneType' object has no attribute 'permission_mode'`**
2. Flipped the chat header to **Disconnected**
3. Rendered the **resume card** ("This session is currently ended.")
4. Persisted the thread as `ended` in the database

The session was *not* idle — the AI was actively processing tool calls the
entire time. Logs show two ~11-minute sessions (the second was the resume).

### Incident 2 — dev cluster, thread `6623d88e` (2026-05-12, ~10:10 UTC)

Same root pattern, but with an additional failure mode visible:

1. Session ran from 10:00:49 → 10:10:50 (10m 01s, same ~10-11 min cadence)
2. PUT `/api/agents/threads/.../status` with status=`ended` from the agent
3. Orchestrator's endpoint immediately fires `_release_thread_resources`
   → `_archive_and_cleanup_workspace` → `container_provisioner.release_thread_workspace`
   → **deletes the workspace pod and its PVC**
   (Snapshot capture before delete failed with `FileNotFoundError [Errno 2]`
   on `ssh` — `ssh` binary is missing from the orchestrator image; data lost.)
4. User clicks Resume — `/resume` succeeds, agent is attached
5. Agent's `_poll_workspace_ready` polls `/workspace` for 120s
6. `metadata.workspace_container.status` is `"deleted"`, never goes ready
7. Agent times out, calls `release-agent`, exits
8. User sees **"Booting agent runtime"** stuck, eventually fails
9. Every subsequent resume repeats steps 4–8 indefinitely

Verified post-mortem against the DB:
```
SELECT id, status, metadata->'workspace_container'->>'status' as ws_status
FROM threads WHERE id = '6623d88e-...';
       id       | status  | ws_status
----------------+---------+----------
 6623d88e-...   | created | deleted
```

## Expected Behavior

A long-running AI turn (10–30+ min of tool calls) should not be terminated by
any inactivity / lifecycle sweep. The only legitimate teardown signals during
an active turn are:

- User clicks Stop / Interrupt
- Hard agent crash (OOM, panic, pod evicted)
- Explicit `/done` archive

A clean handoff would either complete the turn or surface an actionable
message — not a Python `AttributeError` toast.

Independently: an `ended` thread that the user intends to resume should
**not** delete the workspace pod and PVC. `ended` is the only "inactive
but resumable" state. The aggressive cleanup currently triggered by
`PUT status=ended` is correct for *explicit* user end (DELETE/`/done`),
not for agent-side ungraceful end.

## Root Cause (Verified at code level)

The error message string is a Python `AttributeError`. Searching for sites
that touch `.permission_mode` on a possibly-`None` object yields the WS
handler in `src/api/persistent_app.py`. The module-global `_session` is
nulled out by `_detach_session()` (line 926, confirmed: only assignment of
`_session = None` in the file) and is read **without a None guard** at
several sites that run concurrently with the persistent loop:

| File | Line | Code | Notes |
|------|------|------|-------|
| `src/api/persistent_app.py` | 1098 | `"permission_mode": _session.permission_mode,` | `session.state` send on WS accept |
| `src/api/persistent_app.py` | 1223 | `mode = _session.permission_mode` | **Permission gate per tool call** |
| `src/api/persistent_app.py` | 1255 | `_session.turn_count = turn_id` | `on_turn_start` callback |
| `src/api/persistent_app.py` | 1385 | `_session.permission_mode = new_mode` | `mode.set` handler |
| `src/api/persistent_app.py` | 1885 | `_session.permission_mode = pm` | inside `_handle_config_update` (has guard upstream) |
| `src/api/persistent_app.py` | 1908 | `"permission_mode": _session.permission_mode,` | `config.changed` ack |

The most likely site for the user-visible toast is **line 1223** —
`permission_check`. Each tool call routes through that callback. If
`_session` is cleared while a turn is in flight, the very next permission
check raises `AttributeError`, which propagates up:

```
permission_check (persistent_app.py:1223)
  → callbacks.permission_check (persistent_graph.py:812)
  → _execute_turn raises
  → run_persistent_loop's except Exception (persistent_graph.py:252-254)
  → callbacks.on_error(str(e))
  → _ws_send(ws, "error", {"message": "'NoneType' object has no attribute 'permission_mode'"})
```

That matches the screenshot exactly: a generic error toast carrying the raw
`str(e)` from the `AttributeError`.

### What nulls `_session` mid-turn (audited)

I grepped every call site that ends up at `_detach_session()`:

| # | Caller | Cancels `loop_task` first? | Notes |
|---|--------|----------------------------|-------|
| 1 | WS receive `finally` (line 1454-1477) | **Yes** — line 1465-1470 cancels and awaits `loop_task` before detach. | Safe path. |
| 2 | `_thread_status_watchdog` (line 151-186) | **No.** Calls `_detach_session()` directly, then `_schedule_exit(1.0)`. | **Race source.** |
| 3 | `_handle_heartbeat_intents` (line 73-99, drain intent) | **No.** Same pattern as 2. | **Race source.** |
| 4 | `_boot_ws_watchdog` (line 122-148) | n/a — fires only before a WS ever connects. | Not relevant here. |
| 5 | `_handle_archive` / `/session/detach` REST / lifespan shutdown | Varies; not implicated by the screenshot. | Out of scope. |

Note that `loop_task` is a **local** variable inside `ws_chat()`. Neither
watchdog nor the heartbeat-intent handler can see it without explicit
plumbing, which is why those paths skip the cancel step today.

### What flips the thread out of `active` (the upstream trigger)

The watchdog only fires `_detach_session()` when an HTTP poll to
`/api/agents/threads/{tid}/lifecycle` returns a status that isn't
`created` or `active`. So *something* on the orchestrator side wrote
`status='ended'` (or similar) to the threads table while the user's turn
was running. Audit of viable paths:

1. ~~**Drain intent.**~~ **Ruled out for this incident** — see
   "Cluster-log inspection" below. The cluster uses semver image tags
   (`:v0.0.X`), so `expected_agent_shas()` returns an empty set,
   `is_drift()` returns False, and `signal_drain_pending` never fires.
   Confirmed by checking `agents.intents` directly (`{}` for both
   session-2 agents).

2. ~~**Heartbeat starvation.**~~ **Ruled out for this incident.**
   The affected agent heartbeated every 60s, ±1s, like clockwork,
   until the last beat at 08:11:55. Also code-level: LangChain's
   `StructuredTool` delegates `ainvoke` of sync `@tool` functions to
   `BaseTool._arun` which dispatches to `run_in_executor(...)`, so
   `read_file` / `search_files` don't block the event loop.

3. **Idle timeout misfire.** Ruled out by code reading:
   `get_user_input` only awaits `user_queue.get()` between turns. While
   `_execute_turn` is running the timer is not active. Also, the idle
   path goes through `_handle_idle_archive` and emits
   `session.idle_timeout` on the WS — the user would see "Session paused
   after N minutes" instead of the Python error.

4. **Normal WS upstream close** — the most likely remaining path for
   this incident. The orchestrator's proxy uses
   `websockets.connect(ping_interval=30)`; the agent's uvicorn server
   uses `ws_ping_interval=20, ws_ping_timeout=20` (defaults). A
   transient delay on either side (event-loop hiccup, network blip,
   browser tab backgrounded) can fail the pong, the WS gets closed,
   the agent enters its `finally` block and runs `_detach_session()`.
   This path *does* cancel `loop_task` first, but cancellation only
   takes effect at the next `await` point — if the loop is in sync
   code (`_session.permission_mode` is a sync attribute access on
   the FIRST line of `permission_check`), the access runs before the
   cancellation kicks in. **However**, `_detach_session()` is called
   AFTER `await loop_task` completes, so by then `_session` should
   still be set… unless an earlier exception from a different path
   nulled it. Needs a stack trace from the agent pod to nail down.

5. **External archive / detach from another tab / `/done`.** Cannot
   rule out from logs alone.

The ~11-minute cadence (not 15 — see log timing) is *not* a hardcoded
session lifetime anywhere in the code I audited. Both sessions for
this thread happened to end at almost identical durations (10m 46s
and 10m 14s), which is suggestive of *some* upstream-driven cause
rather than user action — but exactly what remains undetermined.

## Code References

| Component | File | Lines | Role |
|-----------|------|-------|------|
| **`permission_check` callback (crash site)** | `src/api/persistent_app.py` | 1220-1252 | `mode = _session.permission_mode` with no None guard |
| `_session` clear | `src/api/persistent_app.py` | 926 | `_detach_session()` nulls the global |
| Thread-status watchdog (race source) | `src/api/persistent_app.py` | 151-186 | Calls detach + schedule exit; **doesn't cancel `loop_task`** |
| Heartbeat-intent drain handler (race source) | `src/api/persistent_app.py` | 73-99 | Same anti-pattern as the watchdog |
| WS receive `finally` (safe path) | `src/api/persistent_app.py` | 1454-1484 | Cancels `loop_task` before detach |
| Loop error propagation | `src/persistent_graph.py` | 252-254 | `except Exception: await callbacks.on_error(str(e))` |
| Permission gate in turn loop | `src/persistent_graph.py` | 811-814 | `await callbacks.permission_check(...)` per tool call |
| `on_error` callback | `src/api/persistent_app.py` | 1303-1304 | Forwards `str(e)` to client as WS `error` |
| Cockpit error binding | `cockpit/src/app/core/services/persistent-chat.service.ts` | 926-928 | `this.error.set(message)` on WS `error` |
| Cockpit error banner | `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` | 734-741 | Renders `chat.error()` raw |
| Lifecycle reconciler (sets drain intent) | `orchestrator/services/lifecycle/reconciler.py` | 162-180 | `signal_drain_pending` fires on SHA drift |
| Agent SHA drift signal | `orchestrator/services/lifecycle/agent_manager.py` | 146-175 | `UPDATE agents SET intents = intents \|\| '{"should_drain":true,...}'` |
| Stale agent detector | `orchestrator/main.py` | 394-465 | Marks agents offline after 3 min |
| `mark_orphaned_threads_ended` | `orchestrator/database/postgres.py` | 2220 | Flips orphaned thread → `ended` |

## Reproduction

### Deterministic reproduction (drain-during-turn)

This nails the race without depending on heartbeat timing:

1. Start a persistent session and submit a prompt that keeps the agent
   in `_execute_turn` for at least one tool round — e.g. "Read every
   file in src/api/ and summarize each."
2. While the turn is mid-flight, set the drain intent on the bound
   agent row from a psql shell:
   ```sql
   UPDATE agents
     SET intents = intents || '{"should_drain":true,"drain_reason":"repro"}'::jsonb
     WHERE thread_id = '<thread-uuid>';
   ```
3. Within ~60s the agent's heartbeat picks this up,
   `_handle_heartbeat_intents` calls `_detach_session()` (no loop_task
   cancel) and schedules exit. The next `permission_check` call
   inside the still-running turn will raise the `'NoneType'` error and
   send it to the cockpit as a WS `error`.

### Alternative reproduction (status flip)

Same as above but flip the thread row directly instead of the agent
intent:

```sql
UPDATE threads SET status = 'ended', ended_at = NOW() WHERE id = '<thread-uuid>';
```

`_thread_status_watchdog` will notice on its next 60s tick.

### Cluster-log inspection (done 2026-05-12)

Ran:
```bash
kubectl logs -n srw-prod-private srw-prod-orchestrator-8db865d-btl7h --since=3h \
  --timestamps | grep "8982a22d"
```

What the logs show for thread `8982a22d-b3a4-462a-9a1c-7e477421676d`:

**Two distinct sessions** on the same thread, each ~11 minutes long
(the user's "15 minutes" is a perception of the second one after
resuming the first):

| Session | Start (UTC) | End (UTC) | Duration | Agent pods |
|---------|-------------|-----------|----------|------------|
| 1 | 07:50:17 | 08:01:03 | ~10m 46s | `bb3336ed` + `95711ebd` |
| 2 | 08:01:48 (resume) → 08:02:21 (WS proxy) | 08:12:35 | ~10m 14s | `a406fd7c` + `298c67a2` |

Smoking-gun timeline for **session 2's end**:

```
08:11:55.009  POST .../heartbeat 200  (last successful heartbeat, agent a406fd7c)
08:11:56.509  POST .../heartbeat 200  (last successful heartbeat, agent 298c67a2)
              [56 seconds of silence — no heartbeats, no lifecycle polls]
08:12:11.480  PUT  .../status 200     (no preceding GET — direct _detach_session)
08:12:20.092  GET  .../lifecycle 200
08:12:20.096  PUT  .../status 200     (4 ms later — watchdog flow on the other agent)
08:12:34.198  Agent pod deleted: srw-agent-s-298c67a2
              Reaped agent pod(s): {'completed': 1}   (Succeeded phase)
08:12:35.787  WS proxy ended
```

Key observations:

1. **Heartbeats were perfectly regular** (every 60s, ±1s) for the entire
   session — see `kubectl logs ... | grep 5507953f.*heartbeat`. The
   heartbeats only stopped at 08:11:55; the agent did not gradually
   fall behind. So **the "heartbeat starvation" hypothesis is wrong
   for this incident.**
2. **The `agents.intents` JSONB column was `{}`** for both session-2
   agents (verified directly: `SELECT hostname, status, intents FROM
   agents WHERE thread_id = '...'`). No `should_drain` was ever set,
   so the drain-intent path **also did not fire** for this incident.
3. **The cluster runs `:v0.0.X` semver-tagged agent images, not
   `:sha-...` tags.** `expected_agent_shas()` parses tags as
   `tag.rsplit(":sha-", 1)[-1]` — with semver tags it returns an
   empty set. `is_drift()` short-circuits to False when expected is
   empty. So **SHA-drift detection never runs in this environment**;
   the lifecycle reconciler logs at 08:01:15 and 08:13:16 confirm this
   (only `unhealthy` counts, never `drift`).
4. **The PUT at 08:12:11 has no preceding GET** in the orchestrator
   log. `_thread_status_watchdog` always polls lifecycle (GET) before
   PUTing — so this PUT was emitted by a non-watchdog `_detach_session()`
   caller (WS receive `finally`, /session/detach, or lifespan shutdown).
5. **The PUT at 08:12:20 was preceded by a GET 4ms earlier** — that's
   the canonical `_thread_status_watchdog` signature, firing on the
   partner agent (the warm-pool pod) whose watchdog observed the
   thread now reads `ended`.

What this rules out for **this specific incident**:

- Drain intent from lifecycle reconciler (no SHA tags, intents=`{}`)
- Heartbeat starvation (heartbeats were regular up to the cutoff)
- `mark_orphaned_threads_ended` (no `Marked ... orphaned` log line)

What this **doesn't** rule out:

- A normal upstream WS close (browser disconnect, idle ping/pong
  timeout on the proxy↔agent WS, transient network glitch). The WS
  receive `finally` block PROPERLY cancels `loop_task` first, but
  there is still a small window between cancellation and the next
  await where sync code (including `_session.permission_mode` access)
  can complete — if cancellation is scheduled but the loop is in sync
  code, the next `permission_check` runs to completion. Whether this
  is actually reachable depends on the exact interleaving.
- Agent pod-level termination (uvicorn graceful shutdown raising
  WebSocketDisconnect → finally block fires).

**Cannot be determined from logs because the agent pods were reaped
(Succeeded phase) before their logs could be captured.** A future
repro should capture agent logs immediately on session end.

## Proposed Fixes

### Fix 1 — Defensive None checks on `_session` in WS callbacks (Required)

The callbacks should treat "session disappeared mid-turn" as a benign
shutdown signal, not raise. Concrete edits in `src/api/persistent_app.py`:

```python
async def permission_check(...) -> bool:
    if _session is None:
        # Session was detached mid-turn; deny silently so the loop
        # finishes its tool sweep without a user-visible AttributeError.
        return False
    mode = _session.permission_mode
    ...
```

Same pattern for the other dereferences enumerated above (lines 1098, 1255,
1385, 1908).

### Fix 2 — Cancel `loop_task` before `_detach_session()` nulls `_session` (Required)

**Implementation note (chosen during shipping):** rather than reorder
each racy caller individually, the cancel-then-detach logic lives
*inside* `_detach_session()` itself. That auto-protects every caller —
including ones we haven't audited and any added in the future —
without per-call-site changes.

Concrete shape:

1. New module-global `_loop_task: Optional[asyncio.Task] = None` in
   `src/api/persistent_app.py`.
2. `ws_chat` assigns `_loop_task = loop_task` immediately after
   `asyncio.create_task(run_persistent_loop(...))`, and clears it in
   the WS finally block right before calling `_detach_session()`.
3. `_detach_session()` reads `_loop_task` at the top, and if it's set
   and not `done()` and not the current task (self-deadlock guard),
   cancels it and `await`s it (suppressing `CancelledError`) before
   any other teardown step.

The "set then clear in finally" plumbing in `ws_chat` is required
because `_loop_task` must reflect the *currently running* loop, not a
stale reference from a prior session in pool-mode agents.

Original recommendation (per-caller refactor, kept for context):

> `_thread_status_watchdog` (`persistent_app.py:151-186`) and
> `_handle_heartbeat_intents` (`persistent_app.py:73-99`) both call
> `_detach_session()` while the persistent loop is still running.
> `loop_task` is currently a local in `ws_chat`; promote it (or its
> cancel-handle) to module state so out-of-band shutdown paths can stop
> it cleanly. Reorder both call sites to:
>
> 1. Get the active `loop_task` reference.
> 2. `loop_task.cancel()` and `await loop_task` (suppress `CancelledError`).
> 3. *Then* `_detach_session()`.
> 4. *Then* `_schedule_exit(delay=1.0)`.
>
> A lighter alternative: close the active WebSocket from the watchdog —
> the existing `ws_chat` `finally` block already does the right cancel +
> detach ordering. That requires plumbing the live `ws` (or a cancel
> event) to the module level.

### Fix 3 — Capture agent logs on session end (Required for diagnosis)

Right now agent pods that exit with `_schedule_exit(0)` reach phase
`Succeeded`, then `reap_pods` deletes them — and `kubectl logs` can
no longer reach the previous container. We can't diagnose the
upstream trigger without a stack trace from the agent itself.

Options:

- **Ship logs to a sink before exit.** Pipe agent stderr to a sidecar
  (Fluent Bit / Vector) or a persistent volume, or `await
  flush_logs()` immediately before `os._exit(0)` in `_schedule_exit`.
- **Delay reaping.** Bump the `reap_pods` grace period for
  `completed` pods from "delete immediately" to something like
  60-180s so a human can read `kubectl logs --previous` after the
  fact. Trade-off: keeps slot count higher transiently.
- **Wire up dozzle / dozzle-equivalent inside `srw-prod-private`.**
  The current dozzle pod lives in `superhuman-remote-worker`, not
  the prod namespace.

### Fix 4 — Don't drain busy persistent agents on SHA drift (Defensive)

Out of scope for this incident (SHA-drift never fires with semver
image tags), but the drain race is still real on any deploy that
switches to `:sha-...` tags. `AgentInstanceManager.signal_drain_pending`
(`orchestrator/services/lifecycle/agent_manager.py:146`) fires the
`should_drain` intent on every drifted agent regardless of whether
it's mid-session. If/when SHA-pinned images are adopted, gate
`signal_drain_pending` on `inst.metadata['status'] != 'session'`, or
have the agent's `_handle_heartbeat_intents` wait for the current
turn to complete before detaching.

### Fix 5 — Don't tear down workspace + PVC on agent-side `ended` (Required)

`agent_update_thread_status` (`orchestrator/main.py:9460-9489`) routes
every `status="ended"` PUT through `end_thread` + `_release_thread_resources`,
which calls `container_provisioner.release_thread_workspace` — that's
"snapshot to S3 then DELETE the pod AND the PVC"
(`container_provisioner.py:316-368`). With the orchestrator image
missing `ssh` (visible as `FileNotFoundError` in `snapshot_service.py`
during capture), the snapshot fails first, the pod gets deleted anyway,
and the PVC follows. **The user's data is gone, and `/resume` has no
way to bring it back** — line 10547 only handles `status == "suspended"`,
not `"deleted"`.

Options:

- **Distinguish agent-side `ended` from user-side `ended`.** When the
  agent flips status to `ended` (recoverable, possibly bug-induced),
  pause-and-suspend the workspace instead of destroying it. The user-
  initiated DELETE path keeps its current aggressive teardown.
- **Make `/resume` recover from `deleted` workspaces.** Re-provision
  a fresh pod and either restore from snapshot or skip restoration and
  warn the user. Currently the resume endpoint just attaches an agent
  and silently expects the workspace to materialise; it never does.

### Fix 6 — Restore `ssh` to the orchestrator image (Required prerequisite for Fix 5's snapshot path)

Recurring log line:
```
ERROR services.snapshot_service: Snapshot capture failed for thread ...:
  [Errno 2] No such file or directory
```
The traceback bottoms out at `asyncio.create_subprocess_exec` on an
`ssh` command — meaning the binary isn't on the orchestrator pod's
PATH. Even when a workspace tear-down is intentional, this means S3
snapshots aren't being captured. Add `openssh-client` to the
orchestrator Dockerfile (and verify it's also in any path that calls
`paramiko.SSHClient` — those import paramiko directly and don't
shell out to `ssh`).

### Fix 7 — Cockpit should not surface raw Python error strings

Even after Fix 1+2, the cockpit's WS `error` handler
(`persistent-chat.service.ts:926`) blindly renders `params['message']`.
For unknown messages, prefer a localized "Session ended unexpectedly —
the agent was disconnected" toast and log the raw string only to console.

## Priority (historical, pre-implementation)

Shipping order chosen during the work was: Phase 1 instrumentation
(Fix 6 + Fix 3) → Phase 2 fixes (Fix 2 + Fix 5 + Fix 1 + Fix 7). Fix 4
deferred. See **Resolution** at the top of this doc for what landed.

The original priority ranking is preserved here for context:

1. **Fix 5** (preserve workspace on agent-side `ended`) — **highest impact**:
   currently the user's data is destroyed on every occurrence, and resume
   never recovers. Until this lands, every session that hits Bug 1 is a
   total loss.
2. **Fix 6** (ship `ssh` in orchestrator image) — required for any
   snapshot to actually succeed; without it Fix 5's "snapshot before
   suspend" is no-op.
3. **Fix 1** (defensive guards) — stops the user-visible toast immediately
   and prevents the agent from emitting the wrong end signal.
4. **Fix 2** (cancel `loop_task` before detach in watchdog/drain paths) —
   eliminates the race for the paths we know about.
5. **Fix 3** (capture agent logs on exit) — required to actually
   diagnose the upstream trigger; until we have this, every future
   occurrence is a guessing game.
6. **Fix 4** (don't drain busy persistent agents) — defensive; not
   urgent until image tags switch to `:sha-...`.
7. **Fix 7** (UX polish) — prevents Python tracebacks from leaking to users.

## Open Questions

- **What is actually nulling `_session` mid-turn?** Still undetermined
  at the implementation milestone. Ruled out so far: drain intent
  (intents=`{}`, semver tags), heartbeat starvation (60s cadence held
  until cutoff), `llm_timeout=600` (only wraps the ainvoke retry path
  and exits cleanly without detaching). The PUT at 08:12:11 without a
  preceding GET still points at a non-watchdog `_detach_session()`
  caller, but agent stderr is gone for both incidents. Fix 3 now
  captures it for next time. With Fix 2 in place, even if the trigger
  re-fires, it should no longer crash the session — but identifying it
  matters because the same trigger may bite paths we haven't audited.
- ~~Why did both sessions for the same thread end at almost identical
  ~11-minute marks?~~ — `llm_timeout=600` ruled out (only wraps the
  retry, not the primary streaming call, and clean exit on fire).
  Remaining candidates: upstream LLM-provider timeout (Anthropic /
  OpenAI request timeouts at the HTTP layer), traefik proxy timeout
  (no custom value configured but checking default), or a WS
  ping/pong deadline on the cockpit↔orchestrator↔agent chain. None
  yet investigated.
- A new, **separate bug** surfaced during validation testing
  (2026-05-12): when the browser screen-locks during a streaming
  response, the WS pauses/reconnects and the agent emits a
  LangChain-internal `Got unknown type content='' additional_kwargs={}
  response_metadata={} id='lc_run--...'` error on the next turn. This
  is *not* the race; it's a streaming-resume corruption. Fix 7 now
  sanitises the user-visible string, but the underlying message
  corruption deserves its own issue if it recurs.
- Even after Fix 1+2 ship, a busy agent can still be drained on the
  legitimate "agent pod is being deleted" path (eviction, node drain).
  Fix 1 + Fix 2 are still required to keep those clean.
