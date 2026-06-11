# Persistent session — Phase-1 untethered loop survival is missing in dual-mode pods

**Status:** Resolved 2026-05-14. Three follow-on commits closed the gap:

1. `sha-6f540a1` extracted the persistent-app WS handler to module-level `handle_persistent_websocket` and rewrote `dual_app.ws_chat` to delegate after a `_pod_state == PodState.SESSION` pre-check, deleting the parallel `_run_persistent_websocket` (340 lines). Phase-1 keystone now lives in one place.
2. `sha-a790c79` tightened the readiness gate in both apps: WS handler and `dual_app:/session/status` now require `_loop_user_queue is not None`, not just `_session.llm_with_tools`. Closed an attach-time race where dual mode's deferred `_setup_session()` could expose `_session` to a WS connect before the module-level queue was initialized (line ~935 of `_attach_session`).
3. `sha-6fe485c` mirrored the same extract-and-delegate pattern to `/api/input`, `/api/interrupt`, `/api/approve` after orchestrator-log review showed `POST /api/input → 404`. Handler bodies lifted to module-level `handle_api_{input,interrupt,approve}` in `persistent_app.py`; `dual_app` added three delegating routes with the same `_pod_state == PodState.SESSION` pre-check. Same `create_persistent_app` vs `create_dual_app` registration drift this issue called out.

### Final cluster verification (2026-05-14 21:00–21:03 UTC, sha-6fe485c)

Session `e0f06fb5-5284-4a35-96dc-989e8dcde335`, agent pod `srw-agent-j-869c04c0` @ `10.42.0.181`. The line that returned `404 Not Found` in the prior probe now returns `200 OK`:

```
21:00:51,557  httpx: POST http://10.42.0.181:8001/api/input "HTTP/1.1 200 OK"
21:00:51,558  POST /api/persistent/threads/.../input                → 200
21:00:51,600  POST /api/agents/threads/.../messages                 → 200  (user msg persisted)
21:00:54,655  POST /api/agents/threads/.../messages                 → 200  (agent reply persisted)
21:01:43,013  WS proxy ended for thread e0f06fb5...                       (tab closed)
21:02:17,941  GET  /api/agents/threads/.../lifecycle                → 200  (pod alive 35s post-WS-drop — KEYSTONE)
21:03:17,948  GET  /api/agents/threads/.../lifecycle                → 200  (pod alive 95s post-WS-drop — KEYSTONE)
21:02:38,362  Proxying WS for thread e0f06fb5... to ws://10.42.0.181:8001/ws/chat   (same pod on reattach)
```

UX side: re-navigating to `/sessions/e0f06fb5-…` returned `connected: true, sessionEnded: false, resumePrompt: false, historyIntact: true`. End-to-end turn worked (user said *"Say 'Hi from task 136 smoke test' and nothing else"*, agent replied *"Hi from task 136 smoke test"*).

The original analysis below remains accurate for the failure mode and is kept for archaeology.

---

**Original status:** Open (discovered 2026-05-14 during cluster verification of [[persistent_session_watchdog_kills_awaiting_user]] fix). Cluster agent pods run `agent.py` in dual mode without `--loop` / `AGENT_LOOP=1`, so any WebSocket disconnect that does not receive a reconnect within ~2 s triggers `_schedule_exit(delay=2.0)` in `src/api/dual_app.py` and the pod calls `os._exit(0)`. The Phase-1 keystone from `docs/features/headless_persistent_sessions.md` ("loop survives WS drop") is implemented in `src/api/persistent_app.py`'s WS handler but is **not** the code path the cluster reaches — dual-mode pods use `src/api/dual_app.py:_run_persistent_websocket` instead, which has its own finally-block that cancels the loop and exits the process. Even when `AGENT_LOOP=1` is set, the reset-to-idle path calls `_detach_session()`, which still tears down the persistent session — so the gap is structural, not just a missing env var.

## Symptom (observed 2026-05-14 during cluster verification)

Running the verification probe for the watchdog fix:

1. Opened `/sessions/b3bccfdc-…`, page-reloaded mid-session.
2. Manually flipped `threads.status = 'awaiting_user'` in PostgreSQL to simulate the Phase-5 eager-mode idle flip.
3. Confirmed `_thread_status_watchdog` accepted the new tuple (fix verified — pod stayed alive across the lifecycle poll at 17:28:31).
4. Navigated away from the session page in the cockpit, which closed the WebSocket without a reconnect.
5. **~2 seconds later, the agent process exited with `Pod task complete — exiting process`** and the pod was recycled by the deployment.

The thread row was untouched (`status = 'awaiting_user'`, `ended_at IS NULL`, no `thread_ended_oob` in the orchestrator log). The pod exit was driven by `dual_app.py`, not the watchdog.

## Timeline (UTC, from agent pod `srw-agent-j-d657868d` and orchestrator log)

| Time | Event |
|---|---|
| 17:22:31 | Persistent agent attached, `Session attached events_epoch=0` (dual_app + persistent_app). |
| 17:23:31 – 17:27:31 | Five lifecycle watchdog polls fired; all 200; pod alive. |
| 17:26:49 | **First WS drop** (cockpit page reload). `Persistent loop cancelled while waiting for input` logged. |
| 17:26:50 | **WS reconnected within window.** `Cancelled pending exit — new WebSocket connecting`. New `run_persistent_loop` started. |
| 17:27:58 | `threads.status` flipped to `awaiting_user` (manual). |
| 17:28:31 | Lifecycle poll on `awaiting_user`. 200. Watchdog fix held — no termination. |
| **17:29:07** | **Second WS drop** (cockpit navigated away from session page). `Persistent loop cancelled while waiting for input`. |
| **17:29:09** | `Pod task complete — exiting process`. `os._exit(0)`. Pod recycled by deployment. |

The two-second gap between 17:29:07 and 17:29:09 is the `_schedule_exit(delay=2.0)` window. The pod waits two seconds for a new WS to arrive (which would call `_pending_exit_task.cancel()` and resume); when none arrives, it exits.

## Root cause

Three contributing facts in `src/api/dual_app.py`:

1. **The WS-disconnect handler always cancels the loop.** In `_run_persistent_websocket`'s `finally` block:

   ```python
   # src/api/dual_app.py:1419-1444
   except Exception:
       logger.info(f"WebSocket disconnected: thread={_thread_id}")
   finally:
       # ...
       if not loop_task.done():
           loop_task.cancel()
           try:
               await loop_task
           except asyncio.CancelledError:
               pass
       # ...
       if _should_loop():
           await _reset_to_idle(
               "idle timeout" if idle_timed_out else "WebSocket disconnect"
           )
       else:
           _schedule_exit(delay=2.0)
   ```

   The `loop_task.cancel()` fires on every disconnect, regardless of whether the disconnect is final or transient. That alone violates the Phase-1 contract ("loop survives WS drop").

2. **`_should_loop()` reads an env var that nothing in the deployment sets.** The cluster's agent pod runs `sh -c "python agent.py --config defaults --port 8001 --host 0.0.0.0"` — no `--loop` flag, no `AGENT_LOOP=1` env. So `_should_loop()` returns `False` and the disconnect handler falls through to `_schedule_exit`. Inspect with:

   ```
   kubectl get pods -n superhuman-remote-worker -l app=srw-agent \
     -o jsonpath='{.items[0].spec.containers[?(@.name=="agent")].command}'
   ```

3. **Even if `AGENT_LOOP=1` were set, the session would still be torn down.** The "loop back to IDLE" branch calls `_reset_to_idle("WebSocket disconnect")`, which contains:

   ```python
   # src/api/dual_app.py:346-352
   if _pod_state == PodState.SESSION and not skip_session_cleanup:
       try:
           from .persistent_app import _detach_session
           await _detach_session()
   ```

   So a looping dual-mode pod would survive past the WS drop, but it would lose the *session* — the loop, the workspace binding, the conversation state. The user reconnecting would land on a different agent attaching to the same thread via checkpoint resume, not the still-running loop they expected.

The Phase-1 fix that **does** exist — in `src/api/persistent_app.py`'s own `ws_chat` handler, which keeps the loop running on disconnect and only detaches when the orchestrator declares the thread done — is unreachable from the cluster, because dual-mode pods route to `dual_app.py:_run_persistent_websocket` instead.

## Impact

For *any* persistent session in the cluster, the moment the cockpit's WebSocket drops without a reconnect within ~2 s, the agent pod exits. This includes:

- User closes the tab and re-opens it 30 s later (browser does not immediately re-establish the WS for a backgrounded tab).
- User navigates to a different cockpit page and comes back.
- Cockpit's `PersistentChatService` loses the WS over a network blip and the reconnect handler takes longer than 2 s.
- Mobile users backgrounding the app for longer than the OS keep-alive window.

In all those cases:

- `threads.status` stays in whatever state it was (commonly `awaiting_user` if the agent had just finished a turn, or `active` if mid-turn).
- `ended_at` is **not** set — the row is still resumable.
- But the pod is gone, the loop is gone, and any in-flight tool calls / generation are lost.
- The next `/sessions/{id}` open spawns a new agent and resumes from the last checkpoint — the user experience is "the session waited for me" only when there's a checkpoint to resume from; pending work between checkpoints is dropped.

The watchdog fix that landed in `fb3f571` closed the most violent failure mode (pod self-destructs + workspace snapshotted + thread marked `ended`), but tab-close-then-resume is still not fully reliable until this `dual_app.py` gap is closed.

## Why this didn't show up earlier

Phase 1 of the headless-persistent-sessions design (`docs/features/headless_persistent_sessions.md`) was scoped against `src/api/persistent_app.py` — the "pure persistent mode" code path entered via `python agent.py --mode persistent`. The smoke runbook in `docs/done/headless_sessions_smoke_leaks_cluster_pods.md` was also written against that mode.

But the cluster's `Deployment` for `srw-agent` launches `python agent.py --config defaults --port 8001 --host 0.0.0.0`, which is **dual mode by default** (job dispatch + persistent sessions on the same pod). Dual mode routes WS traffic through `src/api/dual_app.py:_run_persistent_websocket`, a parallel implementation that predates Phase 1 and never had the keystone applied.

The unit tests for `_thread_status_watchdog` in `tests/test_persistent_app.py` exercised `persistent_app.py`'s WS handler in isolation, so they confirmed the Phase-1 behavior in the file the test imports — not the code path the cluster runs. The same gap likely exists for other Phase 1 / Phase 5 work that landed in `persistent_app.py` and was never mirrored into `dual_app.py`.

## Proposed fix (not implemented)

Two options, in increasing order of work:

**Option A — minimum-viable: set `AGENT_LOOP=1` in the deployment and accept that sessions detach on WS drop.**
A one-line env var add in `deployment/srw-agent.yaml`. The pod survives, but every WS drop ends the in-memory session and the user resumes from the last checkpoint on reconnect. This is what `_reset_to_idle("WebSocket disconnect")` does today and is at least *not destructive* to the workspace. Buys time but does not deliver the Phase-1 contract.

**Option B — proper: make `dual_app.py:_run_persistent_websocket` honor the Phase-1 keystone.**
Mirror the structure of `persistent_app.py`'s `ws_chat` handler:

- Do **not** cancel `loop_task` on every disconnect. Only cancel when (a) the orchestrator declared the thread `ended`/`suspended` (i.e. the `_thread_status_watchdog` told us to detach), or (b) the agent has reached an explicit teardown signal.
- Keep the session attached across WS drops. The loop continues, the agent keeps generating, the next WS reconnect picks up the event stream from `events_epoch`.
- Only call `_reset_to_idle` / `_schedule_exit` when the orchestrator confirms the session is done.

This is a structural change to dual_app's WS finally-block but the logic already exists, fully tested, in `persistent_app.py`. The refactor is largely about importing and routing to that handler rather than maintaining a parallel one.

Either option needs a cluster-smoke test added (per the follow-up in [[persistent_session_watchdog_kills_awaiting_user]]) that asserts the pod is still listed 90 s after a natural tab-close, with no reconnect. The unit-test gap that let this slip is the same one that let the watchdog regression slip — file-level unit tests can't see cross-file interactions.

## Follow-ups

- Decide between A and B above. **Recommended: B**, because A leaves the user experience strictly worse than Phase 1 promised (checkpoint-resume rather than continuation).
- Audit other Phase 1 / Phase 5 code in `persistent_app.py` for similar mirroring gaps in `dual_app.py`:
  - `_loop_get_user_input` eager-mode flip — is the same flip wired in the dual_app loop path?
  - `_thread_status_watchdog` itself is started by `persistent_app._start_watchdogs()`; verify dual_app actually invokes this (the log confirms it did for our session, but worth a code audit).
  - Heartbeat-intent handling.
- Either remove `dual_app.py:_run_persistent_websocket` entirely in favor of delegating to `persistent_app.ws_chat`, or factor out a shared `_handle_persistent_ws(...)` that both call so the keystone lives in one place.

## References

- `src/api/dual_app.py:1108` — `_run_persistent_websocket` (the parallel WS handler).
- `src/api/dual_app.py:1419-1446` — the finally-block that unconditionally cancels the loop and schedules exit.
- `src/api/dual_app.py:303-317` — `_schedule_exit` and the 2 s grace window.
- `src/api/dual_app.py:320-322` — `_should_loop` (reads `AGENT_LOOP`).
- `src/api/dual_app.py:325-369` — `_reset_to_idle` (detaches the session even in the loop branch).
- `src/api/persistent_app.py` — the file where Phase 1's keystone *was* implemented. Compare its WS handler's finally block against `dual_app.py`'s.
- `docs/features/headless_persistent_sessions.md` — Phase 1 design and the keystone contract.
- `docs/done/persistent_session_watchdog_kills_awaiting_user.md` — sibling issue; this was the first symptom of the dual-mode/persistent-app split.
- Cluster command verification: `kubectl get pods -n superhuman-remote-worker -l app=srw-agent -o jsonpath='{.items[0].spec.containers[?(@.name=="agent")].command}'` returns `["sh","-c","python agent.py --config defaults --port 8001 --host 0.0.0.0"]` — confirming no `--loop` flag.
