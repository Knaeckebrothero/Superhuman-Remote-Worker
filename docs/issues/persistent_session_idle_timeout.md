---
tags:
  - persistent-sessions
  - bug
  - orchestrator
  - agent
related:
  - "[[workspace_suspension]]"
  - "[[persistent_graph]]"
---

# Persistent Session Not Suspended After Inactivity

**Reported**: 2026-04-13
**Status**: Root cause verified

## Observed Behavior

A persistent session was started on the cluster. After the user disconnected (~10 hours prior), the session was still shown as running when they logged back in. The expectation was that sessions would be suspended after 15 minutes of inactivity.

**Affected thread**: `e68c15e9-dd8c-4643-b9df-ab297a7b26ba` ("Stuck After Building Perfect Enterprise Harness")
- Created: 2026-04-12T21:18:51Z
- Last user message: 2026-04-12T22:24:36Z (turn 11)
- Last AI response: 2026-04-12T22:25:38Z
- Status when checked (~07:25 on Apr 13): still `active`
- Bound agent: `18668d7b` (`srw-agent-s-5d11ea8b`), still heartbeating as `session`
- Config override: `{"llm": {"model": "codex/gpt-5.4"}}` (no idle_timeout_minutes override)
- Workspace container: still `ready`, pod IP `10.42.1.112`

## Expected Behavior

Persistent sessions should be detected as idle within a reasonable timeout and archived/suspended automatically, regardless of how the user disconnects (clean close, browser tab close, network drop, laptop sleep).

## Root Cause (Verified)

The bug is a combination of three factors that together create an unrecoverable zombie state.

### 1. K8s session pods use `persistent_app.py`, which has no disconnect cleanup

The agent provisioner (`orchestrator/services/agent_provisioner.py:718-726`) builds session pods with:
```python
command = (
    f"python agent.py"
    f" --mode persistent"      # <-- persistent_app.py, NOT dual_app.py
    f" --thread-id {thread_id}"
    f" --config {config_name}"
    f" --port 8001"
    f" --host 0.0.0.0"
)
```

No `--loop` flag is set. The agent runs `persistent_app.py` in one-shot mode.

**This is the primary bug.** In `persistent_app.py:1117-1142`, when the WebSocket disconnects (user closes browser), the `finally` block does NOT update the thread status:

```python
# persistent_app.py — the broken path
finally:
    idle_timed_out = False
    if loop_task.done() and not loop_task.cancelled():
        try:
            loop_task.result()
        except IdleTimeoutError:
            idle_timed_out = True    # only set on timeout, not disconnect
        except Exception:
            pass

    if not loop_task.done():
        loop_task.cancel()           # loop is cancelled, not timed-out

    if idle_timed_out:               # False on normal disconnect
        await _handle_idle_archive() # SKIPPED — thread stays 'active'

    logger.info(f"WebSocket session ended: thread={_thread_id}")
    # No _detach_session(), no status update, no process exit
```

By contrast, `dual_app.py:1317-1325` handles this correctly:
```python
# dual_app.py — the working path
if idle_timed_out:
    await pa._handle_idle_archive()

if _should_loop():
    await _reset_to_idle(...)        # calls _detach_session() → status='idle'
else:
    _schedule_exit(delay=2.0)        # exits process → K8s restarts → stale detector
```

### 2. Agent pod stays alive as a zombie

After the WebSocket handler returns in `persistent_app.py`:
- The uvicorn server keeps running (no `_schedule_exit()` equivalent)
- The heartbeat task keeps firing every 60 seconds
- The agent reports status `session` because `_session` is never cleared
- The liveness probe (`/health`) passes — K8s doesn't restart the pod

### 3. No server-side safety net catches the zombie

Two server-side mechanisms exist but both miss this case:

**`mark_orphaned_threads_idle()`** (`postgres.py:2142-2168`) — requires the agent to be offline:
```sql
WHERE status IN ('created', 'active')
  AND (agent_id IS NULL
       OR agent_id IN (SELECT id FROM agents WHERE status = 'offline'))
```
The agent is alive and heartbeating, so it's never `offline`. Thread stays `active`.

**`check_idle_threads()`** (`workspace_suspension.py:700`) — requires thread status `idle`:
```sql
WHERE status = 'idle'
```
Thread is `active`, so the sweeper ignores it entirely.

### 4. The idle timeout (120 min) never fires either

The client-side idle timeout (`asyncio.wait_for` on `user_queue.get()`) only fires while the WebSocket is still connected and the persistent loop is in the `get_user_input()` call. When the browser disconnects:

1. The orchestrator WS proxy detects the browser disconnect
2. Proxy closes the upstream WebSocket to the agent
3. Agent's `ws.receive_text()` raises `WebSocketDisconnect`
4. The main loop exits, cancelling the loop_task (which was waiting on `user_queue.get()`)
5. The cancellation preempts the idle timeout — `idle_timed_out` stays False

The disconnect races the timeout and wins. The 120-minute timeout is irrelevant.

### 5. The default timeout is 120 minutes anyway

Even if the idle timeout did fire, `config/persistent_defaults.yaml:175` sets `idle_timeout_minutes: 120`. There is no 15-minute default anywhere. The user's expectation of 15 minutes has no corresponding configuration.

## Full Zombie State Lifecycle

```
User connects → proxy → agent (persistent_app.py)
  Thread status: active
  Agent status: session (heartbeating)

User closes browser
  Proxy detects disconnect → closes upstream WS → agent gets WebSocketDisconnect
  Agent: finally block runs, idle_timed_out=False → NO status update
  Agent: uvicorn keeps running, heartbeat continues
  Thread status: STILL active
  Agent status: STILL session

Hours pass...
  mark_orphaned_threads_idle(): agent is alive → SKIP
  check_idle_threads(): thread is 'active' → SKIP
  Workspace sweeper: thread is 'active' → SKIP

Result: zombie state with no recovery path
  Thread: active forever
  Agent pod: alive forever
  Workspace container: running forever
```

## Evidence from Cluster

- Thread `e68c15e9`: status `active`, last_activity 9+ hours stale
- Agent `18668d7b`: still heartbeating, status `session`, host `srw-agent-s-5d11ea8b`
- 5 persistent agents alive (`srw-agent-s-*`), all reporting status `session`
- Thread last user message was at 22:24, agent responded at 22:25 with "If you want, I can also write a **proposed re..." — clearly waiting for user input
- No subsequent messages — user disconnected without responding
- The `get_agent_system_info` endpoint returns 404 for this agent (the endpoint may not exist in `persistent_app.py`)

## Code References

| Component | File | Lines | Role |
|-----------|------|-------|------|
| **Pod command (bug origin)** | `orchestrator/services/agent_provisioner.py` | 718-726 | `--mode persistent` without `--loop` |
| **Missing cleanup (primary bug)** | `src/api/persistent_app.py` | 1117-1142 | No status update on WS disconnect |
| Working cleanup (dual_app) | `src/api/dual_app.py` | 1317-1325 | `_reset_to_idle` / `_schedule_exit` |
| Detach session | `src/api/persistent_app.py` | 575-584 | Sets thread to `idle` (never called on disconnect) |
| Idle archive handler | `src/api/persistent_app.py` | 1537-1603 | Sets thread to `idle` (only on idle timeout) |
| Default timeout config | `config/persistent_defaults.yaml` | 175 | `idle_timeout_minutes: 120` (not 15) |
| Client-side timeout | `src/api/persistent_app.py` | 798-832 | `asyncio.wait_for()` on user input |
| Orphaned thread detector | `orchestrator/database/postgres.py` | 2142-2168 | Only catches offline agents |
| Thread sweeper | `orchestrator/services/workspace_suspension.py` | 677-731 | Only checks `status='idle'` |
| Stale agent detector | `orchestrator/main.py` | 349-386 | Marks agents offline after 3 min no heartbeat |
| WS proxy (orchestrator) | `orchestrator/main.py` | 9520-9750 | No thread status update in finally block |
| Heartbeat endpoint | `orchestrator/main.py` | `/api/agents/{id}/heartbeat` | Updates agents table only |

## Recommended Fixes

### Fix 1: `persistent_app.py` — always clean up on WS disconnect (primary)

Add `_detach_session()` call in the finally block regardless of idle timeout:

```python
# persistent_app.py, in the finally block after line 1140
finally:
    ...
    if idle_timed_out:
        await _handle_idle_archive()
    else:
        # WebSocket disconnect without idle timeout — still need to set idle
        await _detach_session()

    logger.info(f"WebSocket session ended: thread={_thread_id}")
```

### Fix 2: Add server-side stale-thread sweeper (safety net)

Add a background task in `orchestrator/main.py` that catches `active` threads whose `last_activity` exceeds a threshold, regardless of agent status. This handles edge cases where the agent-side cleanup fails:

```python
async def stale_thread_detector(shutdown_event):
    """Mark active threads as idle if last_activity is stale."""
    while not shutdown_event.is_set():
        # Threads active for >30 min with no activity update
        count = await postgres_db.mark_stale_active_threads_idle(timeout_minutes=30)
        if count:
            logger.info(f"Marked {count} stale active thread(s) as idle")
        await asyncio.wait_for(shutdown_event.wait(), timeout=300)
```

This requires a new DB method that checks:
```sql
UPDATE threads
SET status = 'idle', last_activity = CURRENT_TIMESTAMP
WHERE status = 'active'
  AND last_activity < NOW() - INTERVAL '30 minutes'
```

### Fix 3: Update `last_activity` on user messages

Thread `last_activity` should be updated when the user sends a message, not just on status transitions. This gives the server-side sweeper accurate data. Options:
- Agent heartbeat includes a `last_user_interaction` timestamp
- The WS proxy updates `last_activity` when it relays a browser→agent message
- The agent calls a lightweight API to bump `last_activity` on each user turn

### Fix 4: Change default idle timeout — DONE

Unified all defaults to 30 minutes:
- `config/defaults.yaml` (worker): 15 → 30
- `config/persistent_defaults.yaml` (persistent): 120 → 30
- `config/experts/designer-interactive/config.yaml`: 120 → 30
- `config/schema.json`: 60 → 30
- `src/core/loader.py` (InteractiveConfig class + loading fallbacks): 60 → 30

### Fix 5: WS proxy should update thread status on disconnect

Add thread status update in the orchestrator WS proxy's finally block (`main.py:9745-9750`) as a belt-and-suspenders defense:

```python
finally:
    try:
        await ws.close()
    except Exception:
        pass
    # Safety: if the agent didn't clean up, mark thread idle from proxy side
    try:
        await postgres_db.update_thread_status(thread_id, "idle")
    except Exception:
        pass
    logger.info(f"WS proxy ended for thread {thread_id}")
```

### Priority

1. **Fix 1** (agent-side cleanup) — eliminates the primary bug
2. **Fix 2** (server-side sweeper) — safety net for edge cases and crashes
3. **Fix 3** (activity tracking) — enables accurate idle detection
4. **Fix 4** (default timeout) — config change, user expectation
5. **Fix 5** (proxy cleanup) — defense-in-depth
