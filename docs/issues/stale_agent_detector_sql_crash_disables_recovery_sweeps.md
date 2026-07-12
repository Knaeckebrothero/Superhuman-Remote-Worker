# Stale-agent-detector SQL crash disables every recovery sweep — loop jobs wedge permanently in `processing`

**Status**: FIX BUILT + locally verified 2026-07-12, uncommitted — awaiting
push/deploy. The now-batch (F1, F2, F3, F8, F9) is implemented; verification:
targeted unit tests green (classifier cases, detector step-isolation,
non-loop draining heartbeat), the F8 real-Postgres sweep test passes AND
provably fails when F1 is reverted, and a k3d synthetic-orphan smoke
(offline agent + `processing` job seeded via psql) recovered to
`paused`/unassigned within one detector tick on the live cluster. Full
`pytest tests/` green except pre-existing environmental
`test_database_phase1::test_connect_disconnect` (needs a localhost:5432
Postgres; fails identically on clean HEAD). Prod (`main` cluster) still runs
the broken build until this deploys — job `a35b1fc2` auto-recovers ~1 min
after rollout.

Diagnosed 2026-07-11/12 on the main (prod) cluster, verified live at commit
`2a71df34` (orchestrator image `sha-2a71df3`, agent image `sha-20130c3`).
Two Better-Resavio loop scholar jobs lost to it: `6a186c76` (2026-07-11,
cancelled) and `a35b1fc2` (2026-07-12, recoverable). Introduced by
`03675d28` ("graph progress tracking", 2026-07-10) — the L3 layer of the
search_files wedge fix
(`docs/done/search_files_full_repo_grep_wedges_ssh_tool_node.md`).

## TL;DR

A one-character-class bug — an `int` bound to a SQL parameter that Postgres
infers as `text` — crashes the orchestrator's `stale_agent_detector` loop every
60 seconds. Because the detector's five reconciliation steps share a single
`try` block, the crash at step 2 kills **all downstream recovery**: orphaned-job
recovery, stuck-session release, orphaned-thread propagation, session-agent
reaping, and offline-agent GC. Since the 2026-07-10 deploy, **any agent death of
any cause permanently wedges its job in `processing`**. Two unrelated agent
deaths (a misclassified MiniMax 400, then an OOM kill) both landed on this
missing safety net within 24 hours.

```
{"ts": "2026-07-11T16:07:57.261156Z", "level": "ERROR", "logger": "main",
 "message": "Error in stale agent detector: invalid input for query argument $1: 10 (expected str, got int)",
 "file": "main.py:701"}
```

(fires every 60 s in `srw-orchestrator` logs since the deploy)

## Finding 1 (headline) — int bound to a text-typed SQL parameter

`mark_stalled_working_agents_by_graph_progress`
(`orchestrator/database/postgres.py:3805`):

```sql
AND (metadata->>'graph_progress_seen_at')::timestamptz
      < NOW() - ($1 || ' minutes')::INTERVAL
```

The `||` operator against a text literal makes asyncpg/Postgres type `$1` as
`text`; the caller passes `stall_minutes=10` as `int`
(`orchestrator/main.py:627`) → `invalid input for query argument $1: 10
(expected str, got int)` on every tick.

**Fix**: `NOW() - make_interval(mins => $1)` — the same idiom the neighboring
sweep already uses correctly (`make_interval(hours => $1::int)`,
`postgres.py:2994`).

## Finding 2 — one try block = shared failure domain

`stale_agent_detector` (`orchestrator/main.py:609-701`) runs all steps inside a
single `try`:

1. `mark_stale_agents_offline` — **still runs** (before the crash)
2. `mark_stuck_working_agents_ready` — **still runs**
3. `mark_stalled_working_agents_by_graph_progress` — **crashes here**
4. `mark_stuck_session_agents_ready` — dead
5. `reap_orphaned_session_agents` — dead
6. `mark_orphaned_threads_ended` / `mark_orphaned_threads_suspended` — dead
7. `recover_orphaned_jobs` — dead ← the safety net everything else leans on
8. `gc_offline_agents` — dead

The new L3 layer didn't just fail itself; it took the **pre-existing** recovery
down with it. Defense-in-depth became a single point of failure through shared
error handling.

**Fix**: wrap each step in its own `try/except` so one broken sweep degrades
one reconciliation dimension, not all of them.

## Finding 3 — MiniMax `bad_request_error` (400) misclassified as transient

`_classify_llm_error` (`src/graph.py:418-432`, mirrored at `:464-477`) only
maps a 400 to `permanent` when the error body's `type` is
`invalid_request_error`. MiniMax returns:

```
Error code: 400 — {'type': 'error', 'error': {'type': 'bad_request_error',
 'message': 'invalid params, invalid function arguments json string,
 tool_call_id: call_E7U6VHuNDwmxi6Hl8jkjkrG8 (2013)', 'http_code': '400'}}
```

→ falls through to `transient` → `pause_backoff_redispatch` loop instead of
fail-fast. Audit rows 246326/246331 (job `6a186c76`): two cycles, 6 attempts
each, `classification: transient`.

The **same `tool_call_id` in both cycles** proves the malformed tool call is in
the checkpointed message history: every resume replays the poison and gets the
identical 400. A job in this state can never succeed by retrying — it burns
`llm_outage` attempts to the give-up ceiling.

**Fix (classifier)**: treat `bad_request_error` like `invalid_request_error` in
the 400 disambiguation (keeping the existing `rate`/`tool_use_failed`
carve-outs).

**Fix (root cause, separate)**: sanitize/repair malformed `tool_call.arguments`
before send — ideally at ingestion when the model emits unparseable JSON, with
a send-time sanitize as backstop. Same family as the gpt-5.5 tool-pairing
sanitize (`docs/issues`/memory: `persistent_compaction_tool_pairing_400`);
worth doing as one "outbound message hygiene" pass.

## Finding 4 — dispatcher claims before contact, with no rollback

`_try_dispatch_pending_jobs` (`orchestrator/main.py:4770-4785`) atomically sets
`status='processing'` + `assigned_agent_id` (**CAS**, `claim_job_for_agent`,
`postgres.py:3073`) *before* POSTing to the pod. On a failed resume it does not
roll back — by design; the comment says: *"a failed dispatch/resume below
self-heals via recover_orphaned_jobs"*. That design is only sound while
recovery is bulletproof; Finding 1 broke its sole healing path.

**Fix (considered follow-up, not reflex)**: on failed notify, CAS the job back
to `paused`/unassigned inline, keeping the sweep as backstop. Needs care: a pod
that actually accepted the job but answered slowly must not be double-queued.

## Finding 5 — one-shot workers exit after *any* completion, including "pause me"

`dual_app.py:951-953` (and the parallel sites at `:562`, `:578`, `:967`): after
reporting completion — **including an `llm_unavailable` pause** — a worker
without `AGENT_LOOP=1` heartbeats `ready` and schedules `os._exit(0)` in 2 s.
The agent row then looks `ready` with a fresh heartbeat for up to ~3 min (the
offline threshold) while the pod is already gone. In that window the dispatcher
happily claims a backoff-expired job for the dead agent (Finding 4), which is
exactly how `6a186c76` got its final false-`processing` state.

Not a bug in isolation (the row flips offline and recovery re-queues the job
~1 min later — when recovery works), but it defines the race window Finding 4
falls into.

## Finding 6 — debug view shows dead jobs as "running": audit spans never closed on failure

Two rendering artifacts, one cause — spans get `completed_at` only on success:

- **LLM spans** (incident 1): one `llm_request` audit span wraps the whole
  6-attempt retry group; on exhaustion a separate `warning` row is written
  (`event_phase='warning'`, not an error step) and the span is never closed.
- **Tool spans** (incident 2): the `file_exists` span (`agent_audit` 247555)
  opened ~200 ms before the process was SIGKILLed; nothing can close it.

The cockpit renders `completed_at === null` as "pending…/Executing…"
(`cockpit/src/app/debug/components/agent-activity/agent-activity.component.ts:959-965`)
and does not count warnings as failures → "two requests pending, 0 errors" on a
job that had been dead for hours.

**Fix**: close LLM spans with an error status on retry exhaustion; count
exhausted-retry warnings as errors; render tool/LLM spans older than some
threshold with an "unknown/interrupted" state instead of "Executing…".

## Finding 7 — agent OOM (exit 137) during the aux memory pipeline (separate lead)

Incident 2's trigger. Pod `srw-agent-j-fde10fe0` SIGKILLed
(`exit_code=137, reason=Error, phase=Running`) at 19:38:50Z mid-iteration 24.
Evidence of memory pressure, not a Python crash:

- Captured dying logs (reaper, `agent_provisioner.py:731`) end mid-stride, no
  traceback, no shutdown.
- Heartbeats stopped at 19:37:50Z — **60 s before death** — while the event
  loop still completed LLM/embedding calls (starvation profile).
- Final minute: dozens of `/v1/embeddings` + `/v1/rerank` + MiniMax calls;
  `memory_retrieve` pulled 145 candidates / ~23 k tokens; plus
  `Memory assembly failed (non-fatal): Structured-output validation failed for
  AssembleMemoriesTask`.
- `helm/values.yaml:104` comment: *"2Gi (was 1Gi): headroom for a rare,
  unattributed transient memory"* — i.e. **at least the second unattributed
  agent OOM**.

Prime suspect: aux memory pipeline (observer/assembler embedding batches).
Needs its own investigation — don't silently bump the limit again. Related:
`project-persistent-resume-exit137-wedge` (exit-137 family, persistent side).

Note the provisioner reaper (categories at `agent_provisioner.py:593-663`)
captures logs and deletes the crashed pod but deliberately does not touch the
job — recovery is 100 % delegated to `recover_orphaned_jobs`.

## Finding 8 — test gap: mocked `conn.execute` can't catch bind-type bugs

The graph-progress feature shipped with tests (`20130c33`,
`tests/test_stale_agent_detector.py`), but they mock the DB layer, so asyncpg's
parameter typing was never exercised — the bug was untestable by construction.
Options: a real-Postgres test tier for sweep SQL, or the cheap interim rule
"new raw SQL gets executed once against the k3d cluster before push".

## Incident timelines (UTC)

### Incident 1 — job `6a186c76-3582-416f-baaf-b07031262b2c` (scholar, loop iter 1)

| Time | Event |
|---|---|
| 06:58 | Job created; VM `agent-vm-6a186c76` SSH-ready 07:02 |
| 07:56–08:06 | Iterations 99–106 run normally on MiniMax-M3 |
| 08:06:59 | Next main-model call → 400 `bad_request_error` × 6 attempts |
| 08:07:37 | Pause #1 (`llm_unavailable`, attempt 1); one-shot pod exits |
| ~08:08 | Backoff expires → resume on agent `f9d7270f` → same 400 × 6 |
| 08:08:58 | Pause #2 (attempt 2); agent's final heartbeat; pod exits ~08:09 |
| ~08:11 | (reconstructed — logs rotated) backoff #2 due → outage sweeper requeues → dispatcher claims job for `f9d7270f` (row still `ready`, pod dead) → resume POST fails → no rollback |
| ~08:12 | Agent marked offline (heartbeat sweep still works); job stuck `processing` |
| 08:12 → ∞ | `recover_orphaned_jobs` never runs (Finding 1); VM heartbeats keep bumping `jobs.updated_at` every 60 s via `context.vm.last_heartbeat` — **row activity ≠ job alive** |

Outcome: user cancelled 2026-07-11 evening; job later surfaced as `failed`
("Embedding service unavailable at startup…").

### Incident 2 — job `a35b1fc2-649e-4050-8b02-0a4761afede0` (scholar, loop restart)

| Time | Event |
|---|---|
| 19:22 | Loop re-kicked; pod `srw-agent-j-fde10fe0` (image `sha-20130c3`) + VM provisioned |
| 19:26–19:37 | 140 healthy steps (iterations 0–23) |
| 19:37:50 | Last heartbeat — process still running (starvation) |
| 19:38:50.76 | LLM iter 23 completes, `file_exists` tool span opens — process SIGKILLed (exit 137) within ~200 ms |
| 19:40:20 | Reaper detects `category=crashed`, captures 526 log lines, deletes pod |
| ~19:41 | Agent marked offline; `current_job_id` still set; job stuck `processing` |
| → ∞ | Same missing safety net as incident 1 |

State as of 2026-07-12 06:10Z: job `processing`, agent `5052d99a` offline, VM
`100.64.24.154` alive and heartbeating. **No poisoned history — genuinely
resumable** from the iteration-23 checkpoint.

## Fix plan

| # | Fix | Where | Size | Priority |
|---|---|---|---|---|
| F1 | `make_interval(mins => $1)` | `postgres.py:3833` | 1 line | **P0 — prod is running without recovery right now** |
| F2 | Per-step try/except in detector | `main.py:609-701` | small | P0 (same PR as F1) |
| F3 | Classifier: `bad_request_error` → permanent | `src/graph.py:418-432, 464-477` | small | P1 |
| F4 | Close/mark audit spans on failure; warnings count as errors | archiver + `agent-activity.component.ts` | medium | P2 |
| F5 | Tool-call argument sanitize (ingestion + send) | `src/graph.py` / serialization seam | medium | P2 — bundle with gpt-5.5 tool-pairing sanitize |
| F6 | Dispatcher inline rollback on failed resume POST | `main.py:4776-4785` | small but subtle | P3 — considered follow-up (superseded by the lease pickup-TTL, see roadmap) |
| F7 | Agent OOM during aux memory pipeline | separate issue | investigation | separate doc when picked up |
| F8 | Real-Postgres smoke test executing every sweep function once | `tests/` | small | P1 — would have caught F1 pre-commit; same PR as F1/F2 ideally |
| F9 | Non-loop worker deregisters (or heartbeats `draining`) before its scheduled exit — no `ready`-then-die | `src/api/dual_app.py:937-953` | small | P1 — closes the Finding-5 race window at the source |

### Acceptance criteria

1. `Error in stale agent detector` no longer appears in orchestrator logs; the
   graph-progress sweep runs (add an INFO/debug line or verify via a seeded
   stalled agent on k3d).
2. Kill an agent pod mid-job on k3d (`kubectl delete pod --grace-period=0`):
   job flips to `paused` + unassigned within ~2 detector ticks and re-dispatches
   to a fresh pod, resuming from checkpoint.
3. A synthetic sweep exception (monkeypatched step) no longer prevents
   `recover_orphaned_jobs` from running in the same tick.
4. A MiniMax-shaped 400 `bad_request_error` classifies as `permanent`
   (unit test on `_classify_llm_error`); Groq `tool_use_failed` and
   rate-limit-shaped 400s keep their existing classifications.
5. Debug view: a job whose retry group exhausted shows an error state, not
   "pending…"; error counter > 0.

## Proper solution — roadmap (2026-07-12)

The eight findings reduce to three design flaws. The fix-plan table above is
the tactical batch for *this* incident; the structural work is designed
elsewhere so it survives this doc's move to `docs/done/`:

- **Now (this doc)**: F1–F3 + F8 + F9. Restores the safety net, fails fast on
  deterministic 400s, closes the ready-then-die window, guards against the
  next bind-type bug.
- **Next — job execution lease**:
  [`docs/features/job_execution_lease.md`](../features/job_execution_lease.md).
  Liveness by renewal instead of a four-link inference chain; makes the
  dispatcher's no-rollback design sound (pickup TTL supersedes F6); single
  self-contained expiry sweep with no cross-table ordering dependency.
- **Next — outbound message hygiene**: validate/repair tool-call arguments at
  AIMessage finalization (nothing malformed reaches the checkpoint), send-time
  sanitize as backstop, bundled with the gpt-5.5 tool-pairing sanitize (same
  seam). Plus a determinism fingerprint on LLM errors (same request + same
  error N times → permanent, regardless of classifier verdict) so the F3-class
  enum gap can never wedge a job for more than minutes again.
- **Eventually — agents as a reconciler kind**:
  [`docs/features/unified_instance_lifecycle.md`](../features/unified_instance_lifecycle.md)
  (already the plan of record, deferred twice). Fold the detector's sweeps
  into per-kind reconciliation with isolated error handling and an observable
  `last_successful_tick` per concern — the 36-hours-of-unread-ERROR-logs
  lesson. Addendum added to that doc referencing this incident.

## Recovery runbook (until F1 deploys)

Manually do what `recover_orphaned_jobs` would (safe, mirrors the sweep's own
UPDATE):

```sql
UPDATE jobs SET status = 'paused', assigned_agent_id = NULL,
       updated_at = CURRENT_TIMESTAMP
 WHERE id = '<job-id>' AND status = 'processing'
   AND assigned_agent_id IN (SELECT id FROM agents WHERE status = 'offline');
```

The dispatcher then resumes from checkpoint on a fresh pod. Applies to
`a35b1fc2` (clean history, VM alive). Do **not** bother for jobs with a
poisoned tool call in history (incident 1 pattern) — they re-fail identically
until F5; cancel or `resume_job_with_feedback` (compaction may drop the
malformed message) instead.

## Diagnosis gotchas (for the next investigator)

- `jobs.updated_at` moving does **not** mean the job is alive: the VM
  management daemon merges `context.vm.last_heartbeat` into the row every 60 s
  with no log line. Check `agents.last_heartbeat` + audit trail instead.
- The debug view's "pending" LLM/tool cards on a stuck job are usually
  tombstones (Finding 6), not live requests.
- Orchestrator log retention on prod is short (pods restart on deploy); the
  reaper's "Reap log capture" WARNING preserves a crashed agent pod's final
  lines — search for `Reap log capture: pod=<name>`.
- The search_files wedge fix (L0–L3) **is** deployed and did not fail here:
  L2's 15-min tool timeout guards hung-tool-while-alive; nothing in-process
  survives SIGKILL. The dead-process case is exactly what
  `recover_orphaned_jobs` is for.
