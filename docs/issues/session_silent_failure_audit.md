---
tags:
  - persistent-sessions
  - cockpit
  - orchestrator
  - agent
  - bug
related:
  - "[[persistent_session_swallowed_sends_and_truncated_history]]"
  - "[[persistent_chat_silent_disconnect]]"
  - "[[persistent_session_midturn_message_loss]]"
  - "[[surface_silent_aux_failures]]"
  - "[[persistent_session_runaway_generation_context_explosion]]"
---

# Session silent-failure audit — "messages swallowed, UI says Connected"

**Reported**: 2026-06-12 (student on dev cluster: "WebSocket stops sending updates but the UI still shows connected; messages get swallowed; Chrome; no console errors")
**Investigated**: 2026-06-12, threads `1f39a5a6` (gpt-5.5 @ 1.05M ctx) and `b60166ee` (gpt-5.3-codex-spark @ 128k ctx) — same user, same compliance-PDF workload
**Status**: 2026-06-12 — #1, #2, #3, #8, #9, #10, #11, #12, #13, #14, #16 **implemented** (same day, unit-verified: pytest + vitest green, not yet cluster-verified — **live verification runbook: `docs/tests/session_silent_failure_audit_verification.md`**). #4 got a non-destructive **stopgap** (failed summarization keeps history instead of replacing it with a placeholder). #4-full, #5, #6, #7 are deferred to the **summarization rework track** — design doc: **`docs/features/context_summarization_rework.md`** (2026-06-12; aux-budgeted rolling-fold engine, tool-result token budgets, keep-window elision, compaction progress UI, live token counters; includes the root-cause archaeology — summarization limits were derived from the *main* model's window since `4c8d149d`). #15 still needs a repro.

Nothing was browser-specific. One user report decomposed into **16 distinct issues** across agent, orchestrator, cockpit, and cluster ops. This doc is the tackle-list; each issue is independently fixable.

## How they stacked (causal chain)

```
giant PDF tool results (5)──→ context > aux limit (7)──→ summarizer overflow
                          └──→ context > main limit ──→ compaction can't evict (6)
aux router 503 flap ─────────→ summarization fails ──→ history destroyed (4)
                                                    └─→ turn wedged ~8 min silent
silent turn ──→ user types mid-turn ──→ input queued in RAM only (1) ──→ reload → message "gone"
           └──→ user ends "stuck" session (11) ──→ pod deleted ──→ queued inputs lost forever (1)
overflow raised in httpx layer (3) ──→ SDK retry storm ──→ turn dies ──→ invisible to user (2)
all the while: SSE to orchestrator pings → UI "Connected" (8); control WS dead, no reconnect (9);
stale approval card → 409 (10)
```

## Tackle order

| # | Issue | Component | Sev | Status |
|---|-------|-----------|-----|--------|
| 1 | Mid-turn user inputs never persisted — lost on reload/pod death | agent | P0 | ✅ done |
| 2 | Turn-level errors invisible to the user | agent + cockpit | P0 | ✅ done |
| 3 | `ContextOverflowError` retried as `APIConnectionError` | agent (llm) | P0 | ✅ done |
| 4 | Failed summarization replaces history with a placeholder string | agent (context) | P0 | ✅ stopgap; full fix → rework track |
| 5 | Tool-result size unbounded vs main-model context | agent (tools) | P1 | → summarization rework track |
| 6 | Per-turn compaction can't shrink below recent tool-paired messages | agent (context) | P1 | → summarization rework track |
| 7 | Aux calls not clamped to the aux model's context | agent (context) | P1 | → summarization rework track |
| 8 | "Connected" = orchestrator-SSE liveness, says nothing about the agent | cockpit | P1 | ✅ done (cockpit-side "agent quiet" chip; orch heartbeat variant deferred) |
| 9 | Control-WS death is silent and unrecoverable | agent + cockpit | P1 | ✅ done (ws.ping + watchdog + fresh-token reconnect) |
| 10 | Stale permission cards after reload → 409 on approve | agent + cockpit | P1 | ✅ done (permission.resolved journaled + 409 handled) |
| 11 | `end_thread` has no mid-turn guard | orch + cockpit | P1 | ✅ done (409 + `?force=true` + confirm dialogs) |
| 12 | Warm-pool create/delete thrash (1 pod/min churn) | orchestrator | P2 | ✅ done (scale-down respects AGENT_BUFFER) |
| 13 | Suspend path double-fires teardown + contradictory WARNING | orchestrator | P2 | ✅ done (idempotent suspend + in-flight guard) |
| 14 | Persistent sessions write no `llm_requests` audit | agent | P2 | ✅ done (archive_llm_call callback) |
| 15 | Post-snapshot-restore: PDF tool 404s on files `find` sees | agent/workspace | P2 | open — needs repro |
| 16 | Resumed sessions land on `purpose=job` pods with inconsistent labels | orchestrator | P2 | ✅ done (session_router label patch) |

---

## P0 — user-visible data loss / silent failure

### 1. Mid-turn user inputs are accepted but never persisted

**Symptom**: User sends a message while a turn is running → 200 OK → message renders locally → after a reload it's gone; if the agent pod dies it's gone forever. The student lost ≥4 messages this way (12:47:53, 12:49:02 died with the old pod; 12:54:36, 12:56:39 sat queued 20+ min behind a thrashing turn).

**Root cause**: `handle_api_input` (`src/api/persistent_app.py:1843`) puts the content on the in-memory `_loop_user_queue` and returns 200. Nothing is written to `thread_messages`, nothing is echoed over SSE. The queue drains only between turns and dies with the process.

**Fix**: Persist the user message to `thread_messages` (status `queued`/pending-delivery marker in `metrics`) *before* returning 200, and broadcast an SSE event so the optimistic render survives reloads. On loop pickup, reconcile. This is the user-input counterpart of the (completed) agent-side work in [[persistent_session_midturn_message_loss]].

**Note**: the comment at `persistent_app.py:1840` references `docs/issues/persistent_session_dual_mode_phase1_gap.md`, which **does not exist** — this doc is now the tracking doc; fix the dangling reference when touching the file.

### 2. Turn-level errors are invisible to the user

**Symptom**: Session `b60166ee` turn 1 worked 13 minutes (12:57:55→13:10:52), died on context overflow, and the UI showed *nothing* — no error, no message. Log: `Error in turn 1` → `Turn 1 complete: 0 tool calls, 14 total messages`.

**Root cause**: `run_persistent_loop`'s catch (`src/persistent_graph.py:423`) logs the exception and completes the turn as if it produced nothing. No `turn.error` event exists on the SSE stream. Related: the first input after a resume got a raw 503 (`Session not ready`, 12:50:04) — also nothing visible in the UI.

**Fix**: Emit a `turn.error` SSE event (+ persist a system/error row in `thread_messages` so it survives reload); cockpit renders an error bubble with retry affordance. Map agent 503s on `/input` to a visible toast.

### 3. `ContextOverflowError` is wrapped into a retryable `APIConnectionError`

**Symptom**: A deterministic "request body too big" failure produced a retry storm (~6 attempts across stream + ainvoke paths, 13:10:45–13:10:50) plus the misleading log `Streaming not supported (APIConnectionError), falling back to ainvoke`.

**Root cause**: the Layer-0 token guard raises `ContextOverflowError` **inside the httpx transport** (`src/llm/reasoning_chat.py:748`, in `send()`). The OpenAI SDK wraps any transport exception into `APIConnectionError` (`openai/_base_client.py:1683`), which its retry logic treats as transient. Downstream code loses the exception type (only reachable via `__cause__`), so `persistent_graph`'s streaming-fallback heuristic misfires too.

**Fix**: Pre-flight the token count *before* handing the request to the SDK (in `ReasoningChatOpenAI._agenerate`/`_astream`), raising typed `ContextOverflowError` directly. Keep the httpx-layer check as a backstop but have callers unwrap `__cause__` and treat it as non-retryable. `persistent_graph` should branch on it explicitly (force-compact or fail the turn with issue #2's error surface).

### 4. Failed summarization replaces history with a placeholder string

**Symptom**: During the aux-router 503 flap, agents' compaction summaries came out as garbage/placeholder ("summary issues in some agents").

**Root cause**: when the structured pass and the unstructured fallback both fail, `_generate_summary` returns the literal string `f"[Summarization failed: {e}]"` (`src/core/context.py:1348`) and `summarize_and_compact` proceeds — the summarized messages are dropped and replaced by a failure string. The agent permanently loses everything outside `keep_recent`.

**Fix**: On total summarization failure, do **not** compact-with-placeholder. Prefer: keep raw messages and fall back to non-destructive trimming (tool-result clearing), retry summarization next turn, and emit an aux-failure event ([[surface_silent_aux_failures]] is the umbrella for making these visible).

---

## P1 — structural causes that wedge sessions

### 5. Tool-result size is unbounded relative to the main model's context

**Evidence**: turn 1 of `b60166ee` ingested four PDF tool results of 162k/165k/189k/246k chars (191-page + 220-page CFR, 851-page + 1355-page EU regulations) on a **128k-token** model → 234,863-token request that can never succeed. The PDF tool already paginates (`[Pages 1-72 of 1355]`) but its page budget is fixed, not derived from the model window.

**Fix**: cap per-tool-result tokens to a fraction of the main model's `max_context_tokens` (e.g. ≤15–20%), instruct the model to read ranges/sections beyond that. Applies to `read_pdf` first; audit other bulk readers (file read, SQL results, web fetch).

### 6. Per-turn compaction can't shrink below recent tool-paired messages

**Evidence**: every retry of the `b60166ee` turn sent exactly 234,863 tokens (183.5%) — compaction wrote a summary (13:10:38) but `keep_recent` + tool-call/ToolMessage pairing kept the four giant results in the prepared context.

**Root cause**: `ensure_within_limits` (`src/core/context.py:901`) summarizes *old* messages; there is no oversized-single-message elision in the live per-turn path (one exists in the resume path only, per [[persistent_session_midturn_message_loss]] S4).

**Fix**: add a final stage to the progressive loop: if still over limit, elide the *content* of the largest tool results in the keep-window (replace with `[tool result elided: N tokens, stored at seq X]`), preserving pairing. With #5 in place this becomes a rare fallback rather than the norm.

### 7. Aux/summarizer calls are not clamped to the aux model's own context

**Evidence**: thread `1f39a5a6` (main: gpt-5.5 @ 1.05M) accumulated ~951k tokens; summarization sent the whole conversation to the 131k summarizer → `ContextOverflowError: 951,682 > 131,072` (13:03:58). `_recursive_summarize` (chunking, `context.py:1350`) exists but did not engage on this path.

**Fix**: route every aux invocation through a clamp that chunks/truncates input to the aux model's window (use `_recursive_summarize` unconditionally when input exceeds it). Memory-extraction and title calls need the same guard.

### 8. "Connected" indicates orchestrator-SSE liveness, not agent liveness

**Evidence**: while the agent was wedged 8+ minutes (and earlier: while its pod was being deleted), the UI showed a green Connected dot the whole time, because the SSE terminates at the orchestrator, which emits `ping` every ~20s regardless of agent state.

**Root cause**: `connectionState` in `cockpit/src/app/core/services/persistent-chat.service.ts` is driven solely by the SSE; there is no agent-health signal on the stream.

**Fix**: orchestrator includes agent heartbeat/state in the SSE (it already tracks heartbeats + lifecycle); cockpit renders a distinct agent state chip: *working / waiting / stalled (no agent activity for N s) / restarted*. "Stalled" plus a running turn is exactly the state the student needed to see.

### 9. Control-WS death is silent and unrecoverable

**Evidence**: the agent-direct control WS (`/p/{thread_id}/ws`) died at 12:57:17; zero reconnect attempts for 4.5 min (no `GET /api/sessions/{id}/connection` re-fetch); UI unchanged. Approve/deny and config verbs ride this channel.

**Root cause**: no liveness watchdog on the control WS (the SSE got one; the WS didn't — exactly the gap left open in [[persistent_chat_silent_disconnect]] / Bug 3 of [[persistent_session_swallowed_sends_and_truncated_history]]). Reconnect caps at 8 attempts / ~15 s total (`CONTROL_WS_RECONNECT_*`, persistent-chat.service.ts:53), and the connect token is a 60-second JWT, so any later reconnect *must* re-fetch `/connection` first. Half-open sockets (edge/tunnel idle kill during quiet turns) never fire `onclose`, so even the capped retries don't start.

**Fix**: app-level heartbeat on the WS with client watchdog (option A from the silent-disconnect doc), unbounded-with-backoff reconnect that always re-fetches a fresh connection token, and reconnect-on-`visibilitychange`/`online`. Longer term: fold control verbs into REST/SSE as the service header already proposes.

### 10. Stale permission cards after reload → 409

**Evidence**: at 13:01:53 the student reloaded; the UI rendered an approval card for request `e0afb160`, which had been approved at 12:52:37 → his click returned 409.

**Root cause**: on history load the cockpit reconstructs permission cards from message/turn data without reconciling against current `thread_permission_requests` state (and missed the resolution event while its channels were down — #8/#9).

**Fix**: on load (and on SSE reconnect), fetch open permission requests and render only those as actionable; show resolved ones as resolved. Treat 409 responses as "already resolved → refresh card state" instead of error.

### 11. `end_thread` has no mid-turn guard

**Evidence**: 12:49:21 — `DELETE /api/persistent/threads/1f39a5a6` (soft end) arrived while turn 1 was in flight, directly after a 6-thread cleanup sweep by the same actor (12:47:27–40). The live workspace + agent pod were torn down mid-turn; in-memory queued inputs were destroyed; the user's open tab auto-resumed 8 s later onto a pool pod, masking the whole event.

**Fix**: if the thread has a turn in flight (agent status busy/heartbeat fresh), return 409 with a clear "session is mid-turn" body unless `?force=true`; cockpit shows a confirm dialog. Log the acting user on end/delete (today the actor is not attributable — see also #14).

---

## P2 — ops & observability

### 12. Warm-pool create/delete thrash

**Evidence**: since the orchestrator restart at 09:50, every odd minute the warm pool creates one agent pod (`active=4, idle=0, min=2, buffer=1`) and the next minute scale-down deletes one (`active=3, min=2`) — 3+ hours of a pod/minute churn (`srw-agent-j-b21989e8`, `-5915fd78`, `-b0119d24`, … `-e2affe4f` died mid-startup in `Error` state). The two loops disagree about the target count, and the pool is effectively never warm.

**Fix**: make the warm-pool target and the scale-down floor read the same formula (min + buffer vs min), or have scale-down ignore pods younger than the reconcile interval. `services/agent_provisioner.py`.

### 13. Suspend path double-fires teardown

**Evidence**: thread `655d0bf3` at 12:44:53 — snapshot uploaded, workspace deleted, agent pod deleted, "Workspace suspended to S3"… then one second later `WARNING: Workspace suspend unavailable or failed … keeping workspace alive (reconciler will reap) but deleting the agent pod` and a **second** `Agent pod deleted: srw-agent-s-096e7880`. Our thread later got the same double snapshot+delete (12:49:26 and 12:49:34) from the suspend flow racing the end-thread teardown.

**Fix**: make `_release_thread_resources` / `workspace_suspension` idempotent and single-owner (re-entrancy guard like the `_terminating` one from the drift-drain fix); the success path should not be followed by a failure-path WARNING.

### 14. Persistent sessions write no `llm_requests` audit

**Evidence**: `srw_logs.llm_requests` contains job-agent calls only (latest 09:19, scholar). The original `1f39a5a6` agent's fatal turn-1 hang (12:44→12:49) is unattributable because the pod is gone and there is no request audit for sessions.

**Fix**: wire the same audit write (model, latency, status/error, thread_id) into the persistent path. Cheap and it would have answered the core question of this investigation in one query.

### 15. Post-snapshot-restore: PDF tool 404s on files `find` sees

**Evidence**: after the 12:49 restore, at 13:08:35 the PDF tool reported `File not found: /home/agent-host/workspace/uploads/…` and `…/cloud/…` for files a shell `find` had listed at 13:01:41.

**Status**: unresolved — needs repro. Suspects: stale SFTP/SSH connection caching the old workspace pod, cwd/absolute-path mismatch after restore, or the rclone `cloud/` mount not surviving restore for path-based (non-shell) tools.

### 16. Resumed sessions land on `purpose=job` pods with inconsistent labels

**Evidence**: `srw-agent-j-d32661ab` (a warm *job* pod) served session `1f39a5a6` after resume: label `srw/purpose=job`, has `srw.io/thread-id` but lacks the `srw/thread-id` label that session-provisioned pods (`srw-agent-s-*`) carry. Any dashboard/selector filtering on `purpose=session` or `srw/thread-id` misses resumed sessions.

**Fix**: on attach, patch the pod labels (purpose + both thread-id label keys) so provenance and selectors stay truthful. `services/agent_provisioner.py` / resume path in `orchestrator/main.py`.

---

## Context: environmental factors (not code bugs, but live today)

- **`ai.h4ll.app` router flapping** — `503 All backends unavailable: ReadTimeout` intermittently 12:57–13:01+. Every agent's aux model (gemma-4-moe) and embeddings (qwen3-embedding-8b) sit behind it. Same class as the 06-03→06-06 outage; [[surface_silent_aux_failures]] is the systemic answer.
- **codex-proxy models** (gpt-5.5, gpt-5.3-codex-spark) — long latencies on huge contexts stretch turns into multi-minute silences, which is what exposes #1/#8/#9 to users.
