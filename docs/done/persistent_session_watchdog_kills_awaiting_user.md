# Persistent session — thread-status watchdog kills agents in `awaiting_user`

**Status:** Resolved 2026-05-14. `_thread_status_watchdog` in `src/api/persistent_app.py` now treats `awaiting_user` as a non-terminal state. The allowed-status tuple is `("created", "active", "awaiting_user")`; everything else (`suspended`, `ended`) still triggers self-termination. Docstring updated to call out the Phase 5 / orchestrator-attention-sleep boundary. Regression test in `tests/test_persistent_app.py::TestThreadStatusWatchdog::test_does_not_exit_when_thread_awaiting_user` pins the new tuple. A second new test pins the `suspended → exit` path that the docstring already implies.

## Symptom (observed 2026-05-14 in cluster usage)

User opened `/sessions/ba3c8064-…`, sent three prompts ("Hey", "Can you build a calculator app for me?", "Yeah let's go"), closed the browser tab during the assistant's third turn, then re-opened the tab a few minutes later expecting to find the agent still working in the background (the Phase 1 + Phase 5 headless-sessions promise).

Instead the UI showed `Disconnected` + a `Session ended May 14, 2026 at 6:11 PM` flag + a `Resume this session?` prompt. The workspace pod had been deleted, the workspace snapshot uploaded to S3, and the thread row stamped with `ended_at`.

## Timeline (UTC, from `superhuman-remote-worker/srw-orchestrator-…` log)

| Time | Event |
|---|---|
| 16:10:33 | Persistent agent `ed936485` ready, attached to thread `ba3c8064-…`. `_start_watchdogs()` arms `_thread_status_watchdog` with `poll_s=60`. |
| 16:10:38 | WS proxy established cockpit → orchestrator → agent. |
| 16:10:42–16:11:10 | Three user turns. |
| **16:11:23** | **WS proxy ended** — user closed the tab. Loop kept running per the headless-keystone (`persistent_app.py:1602`). |
| 16:11:33 | Agent heartbeat (still alive). |
| 16:11:36 | Agent posts six messages (`create_directory`, `write_file` × 2 successful; third file `script.js` emitted as malformed plain text — separate Gemma chat-template issue, see `project_gemma_reasoning_38855.md`). |
| **16:11:37,656** | Agent's `_loop_get_user_input` runs the eager-mode Phase 5 flip: `PUT /api/agents/threads/{id}/status` → `awaiting_user`. |
| **16:11:37,657** | `_thread_status_watchdog`'s first 60s poll fires, `GET /api/agents/threads/{id}/lifecycle` returns `status=awaiting_user`. |
| **16:11:37,661** | Watchdog: `status not in ("created", "active")` → `_terminate_session("thread_ended_oob")` → `PUT status=ended`. |
| 16:11:37,662 | `end_thread()` re-PUT after `ended_at` stamp. |
| 16:11:42 | `_suspend_thread_resources` snapshots workspace to S3, deletes `ws-thread-ba3c8064-f1d` pod, marks workspace suspended. |

The two-event race at `16:11:37` is the whole story: agent-side awaiting-user flip and agent-side watchdog poll fired within 1 ms of each other, the watchdog interpreted its peer's just-written status as orphaning, and self-terminated.

## Root cause

```python
# src/api/persistent_app.py:224 (before fix)
if status not in ("created", "active"):
    await _terminate_session("thread_ended_oob")
    _schedule_exit(delay=1.0)
    return
```

Phase 5 (commit `0e5994e`, migration `0008_thread_awaiting_user.sql`, design doc `docs/features/headless_persistent_sessions.md`) added `awaiting_user` as the eager-mode **transient idle** state — the agent's loop sets it via `_safe_set_thread_status("awaiting_user")` in `_loop_get_user_input` (`persistent_app.py:1838`) on natural pause with no subscribers. The orchestrator's attention-sleep watchdog (separate component, separate code path) owns the eventual `awaiting_user → suspended` transition.

`_thread_status_watchdog` was authored in the PR-1 frame ("exit if orphaned by orchestrator") and never widened when Phase 5 added a fourth valid in-flight status. The design doc had even flagged this at `docs/features/headless_persistent_sessions.md:116`:

> **Watchdogs on the agent side** | `_boot_ws_watchdog`, `_thread_status_watchdog` in `src/api/persistent_app.py` cancel via `_detach_session`. | **Watchdogs need to detect "untethered" rather than "ws-dropped" — different signal, different action.**

…but the actual code change didn't ship with Phase 5.

The smoke test in `tests/test_persistent_app.py::TestThreadStatusWatchdog` covered `ended` (exit) and `active` (no-exit) but not the new `awaiting_user` case, so the regression slipped through CI green.

## Impact

For any persistent session where the user closed the WS subscriber before the orchestrator-side attention-sleep watchdog had a chance to run, the agent pod self-terminated within ~60 s of the eager-mode flip. The thread was marked `ended`, the workspace was snapshotted and the pod deleted. The user could still `/resume` from the snapshot — but the session was *not* still working in the background, contradicting the Phase 1 + Phase 5 design intent.

This explains user-reported "I closed the tab and came back to a `Session ended`" experiences as the eager-mode flow becoming the path of least resistance after the WS-decoupling commit (`3a1d265`) made tab-close benign. With the WS-decoupling fix in place, the loop survives long enough to *reach* the awaiting-user flip — at which point the watchdog kills it.

Side effect: every such killed session leaves a `Resumable` thread in the user's sessions list, even though the user never explicitly ended it. The `ended` status is correctly recoverable, so no data loss; just unnecessary UI friction and a confusing mental model.

## Fix

`src/api/persistent_app.py:224` — extend the allowed-status tuple:

```python
if status not in ("created", "active", "awaiting_user"):
    ...
```

Docstring updated to spell out the boundary: `awaiting_user` is owned by the orchestrator's attention-sleep watchdog and pre-empting it from the agent side defeats the Phase-1+Phase-5 untethered-survival behaviour. `suspended` remains a self-exit trigger — at that point the orchestrator has already deleted the workspace pod, so the agent is stranded and should not hold its slot.

Regression coverage in `tests/test_persistent_app.py::TestThreadStatusWatchdog`:
- `test_does_not_exit_when_thread_awaiting_user` — would have failed against the old code.
- `test_exits_when_thread_suspended` — pins the `suspended → exit` path that's now load-bearing (previously implicit under the `not in (...)` branch).

The existing `test_exits_when_thread_status_is_ended` and `test_does_not_exit_when_thread_active` tests remain unchanged and still pass.

## Why this didn't show up in Phase 5 smoke

The Phase 5 smoke runbook (`docs/done/headless_sessions_smoke_leaks_cluster_pods.md`) exercises the orchestrator-side attention-sleep watchdog by manipulating the `awaiting_user_since` column directly. It doesn't exercise the agent-side `_thread_status_watchdog` poll race with the agent's own `awaiting_user` flip — the two watchdogs were treated as independent components in test, so their interaction wasn't probed.

The integration test that would have caught this is "attach a persistent agent to a thread, let one turn complete, leave it idle, and assert the pod is still up 90 s later". That probe should be added — see "Follow-ups" below.

## Follow-ups (not done in this fix)

- Add a cluster smoke step asserting `kubectl get pods -n superhuman-remote-worker` still lists the per-thread agent pod 90 s after a natural-pause turn completion. This is the only test that would have caught the race; the unit test added here is a guard for future regressions of the same kind, not the canonical coverage.
- Audit `_boot_ws_watchdog` and `_handle_heartbeat_intents` for similar "exit on any non-`active`" assumptions. Spot-check: `_boot_ws_watchdog` only fires on missing-WS during the boot window so it's not affected. `_handle_heartbeat_intents` only fires on explicit `should_drain` so it's not affected.
- Cockpit-side polish: when a session ends because the agent self-terminated (vs. user clicked End), surface the reason in the `Session ended` banner so the user knows it was system-initiated. Out of scope for this fix; tracked separately.

## References

- `src/api/persistent_app.py:200` — `_thread_status_watchdog` (the fix site).
- `src/api/persistent_app.py:1836` — `_loop_get_user_input` (the eager-mode `awaiting_user` flip).
- `orchestrator/main.py:9599` — `GET /api/agents/threads/{thread_id}/lifecycle` (what the watchdog polls).
- `orchestrator/main.py:9649` — `PUT /api/agents/threads/{thread_id}/status` handler (the `ended → _suspend_thread_resources` chain).
- `docs/features/headless_persistent_sessions.md:116` — the design doc note that anticipated this exact thing.
- `docs/done/persistent_session_permission_check_race.md` — earlier watchdog work that audited `_detach_session()` callers and added the cancel-loop-first guard at commit `3a1d265`.
- `docs/done/headless_sessions_smoke_leaks_cluster_pods.md` — Phase 5 smoke runbook; the integration smoke gap noted above belongs here.
