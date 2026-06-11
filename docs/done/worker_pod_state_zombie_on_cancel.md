# Worker agents leak `_pod_state=WORKING` on cancel/pause → zombie agents → resume `409`→`502`

**Date:** 2026-06-10
**Status:** **Resolved** 2026-06-10 on branch `fix/worker-pod-state-zombie-on-cancel` (commit pending). Fix 1 (3 sites) + Fix 2 implemented, 5 unit tests + `ruff` green, **verified end-to-end on local k3d**. See **Resolution** below for what was/wasn't exercised live and the remaining homelab cleanup.
**Component:** `src/api/dual_app.py` (agent), `orchestrator/main.py` + `orchestrator/database/postgres.py` (orchestrator)

## Summary

Clicking **Resume** on a job in the cockpit can fail with a generic
`errors.jobs.resumeFailed` toast. The toast is a `502` from the orchestrator,
which is the orchestrator translating a `409 Conflict` returned by the agent
pod it tried to resume on.

Root cause is a **state drift** between the orchestrator's `agents.status`
column and the agent pod's in-memory `_pod_state`. The drift is *created* by a
real bug: the worker's stop paths (cancel/pause — **three** of them, including
the hard-kill timeout) clear `_current_job_id` but never call `_reset_to_idle()`,
so `_pod_state` is stranded at `WORKING`. That pod becomes a **zombie** —
heartbeating `working` with no job forever — and the orchestrator's
stuck-working reaper repeatedly flips it back to `ready`, making it a dispatch
target that then rejects work with `409`. The resume endpoint trusts the DB
`ready` with no liveness check and surfaces the rejection as a hard `502`.

This is the **worker-route `_pod_state` drift** that
[`agent_app_readiness_drift.md`](../done/agent_app_readiness_drift.md) (line 101)
explicitly predicted: *"Worker routes are reimplemented across `app.py` and
`dual_app.py`… `dual_app` adding a `_pod_state` pre-check. Same
parallel-implementation pattern; the same kind of drift bug could land there."*

## Resolution (2026-06-10)

**Fixed** on branch `fix/worker-pod-state-zombie-on-cancel` (commit pending).

- **Fix 1** (`src/api/dual_app.py`): new shared `_complete_stop()` helper
  (`await _reset_to_idle()` then `_stop_completed.set()`, in that order) routes
  the cooperative sites **A** (`:495-501`) and **B** (`:882-886`); site **C**
  (cancel hard-kill, `:778-793`) calls `_reset_to_idle("cancel hard-kill")`.
- **Fix 2** (`orchestrator/main.py`): `resume_job` maps an agent `409` to
  demote-and-re-queue via a new `_queue_for_dispatch()` helper, gated by the
  importable `_resume_reject_should_requeue()` predicate, instead of `502`.
- **Tests**: `tests/test_dual_app_stop_reset.py` +
  `tests/test_resume_stale_agent_requeue.py` (5 new, green; `ruff` clean).

**Verified end-to-end on local k3d** (`k3d-srw`): a job's scholar subjob ran with
agent `037bd49d` `working` on a freshly-provisioned pod (rebuilt image);
**cancel** flipped it `working → ready, job=none` instantly (no zombie);
**resume** returned `200 {"status":"resumed"}` back to that same now-ready agent
(no `502`); the reaper logged **zero** `stuck in 'working'` for the run.

**Caveats / still open:**
- The live test exercised **site A** (cancel-while-processing) — the same
  `_complete_stop` mechanism as site B. The racily-rebuilt dev image happened to
  lack site B (resume-then-cancel); it is unit-tested and ships in the
  committed/CI image. Site C was present in the dev image.
- **Homelab** dev-cluster zombies (`a4adc71e` / `srw-agent-j-efdba170` + the
  original agent of `e0360c9c`) still need a one-off recycle — see "Immediate
  cleanup". Fix 1 stops new ones; it can't un-strand existing pods.

## Symptom (observed 2026-06-02, `develop`, dev ns `superhuman-remote-worker`)

Job `e0360c9c-9212-4139-b749-d311c12ea03d` ("Research 01 (GPT-5.5, run 2)") —
created 21:22, cancelled by the user 21:33, Resume clicked 21:41. Orchestrator
log:

```
21:33:03  Agent confirmed graceful cancel for job e0360c9c…
21:33:07  Workspace container deleted: workspace-e0360c9c-921
21:33:07  Snapshot uploaded: job=e0360c9c… size=98527558
21:33:07  PUT  /api/jobs/e0360c9c…/cancel  200            → status = cancelled
   …
21:41:55  OPTIONS /api/jobs/e0360c9c…/resume 200          ← user clicks Resume
21:41:55  Auto-selected agent a4adc71e-… for job resume
21:41:55  POST http://10.42.0.108:8001/job/resume → 409 Conflict   ← agent rejects (busy)
21:41:55  POST /api/jobs/e0360c9c…/resume 502 (31ms)               ← orchestrator → UI toast
```

`a4adc71e` (pod `srw-agent-j-efdba170`, 13 days old at the time) and the job's
own original agent `9ff7155d` were both showing `working` in `GET /api/agents`
with **no `current_job_id`** — i.e. zombies. The reaper logs `Released N
agent(s) stuck in 'working' with no job` on every tick, repeatedly, for the
same pods.

## Root cause — the full chain

### 1. The leak (the source): cancel/pause strands `_pod_state`

`_process_orchestrator_job` cooperative-stop branch, `dual_app.py:495-501`:

```python
if _stop_requested.is_set():
    reason = _stop_reason
    _clear_stop()
    logger.info(f"Job {job_id} stopped gracefully (reason={reason})")
    _current_job_id = None
    _stop_completed.set()
    return  # Don't exit — pause means orchestrator may want to reassign
```

It nulls `_current_job_id` but `return`s without resetting `_pod_state`. Every
`_pod_state = PodState.IDLE` write-site is accounted for and **none is on this
path**:

| `dual_app.py` line | sets `_pod_state` to | reached on |
|---|---|---|
| 368 (inside `_reset_to_idle`) | `IDLE` | normal completion (`:522`) / error (`:538`) |
| 726 | `WORKING` | `/job/start` |
| 833 | `WORKING` | `/job/resume` |
| 959 | `SESSION` | `/session/attach` |
| 993 | `IDLE` | session-setup failure |

So after a cancel/pause the pod sits at `_pod_state=WORKING`, `_current_job_id=None`.
`_reset_to_idle()` — the *only* code that flips `_pod_state→IDLE` (`:368`) — is
called on exactly five paths (completion `:522`, error `:538`, resume-completion
`:905`, resume-error `:920`, session-detach `:1057`), and **none is a stop path**.
There are **three** leak sites:

| # | site | trigger | skips |
|---|---|---|---|
| **A** | `dual_app.py:495-501` | cooperative cancel/pause (job loop sees `_stop_requested`) | `_pod_state` reset |
| **B** | `dual_app.py:882-886` | cooperative cancel/pause during a *resume* | `_pod_state` reset |
| **C** | `dual_app.py:778-793` | cancel handler's 120s **hard-kill** timeout (`_stop_completed` never fired — e.g. blocked in an LLM call) | `_pod_state` reset |

A and B null `_current_job_id`, `_stop_completed.set()`, and `return`; C cancels
the task and clears stop state. All three leave the pod heartbeating `working`
with no job. (The original write-up listed only A and B — C is the same bug on
the hard-kill branch, where the cancelled task re-raises `CancelledError` at
`:526-528` without resetting and the handler that killed it doesn't reset either.)

### 2. The agent reports the "impossible" state

The heartbeat reads the two variables independently:
`_get_heartbeat_status()` (`dual_app.py:111-123`) returns `"working"` from
`_pod_state`; `_get_current_job_id()` (`:126-127`) returns `_current_job_id`.
A leaked pod therefore reports `status="working", job_id=None`.

### 3. The orchestrator writes it verbatim

Heartbeat handler `main.py:11413` → `postgres.heartbeat()` `postgres.py:2075`.
The UPDATE (`:2153-2186`) is `SET status = CASE WHEN 'draining' … WHEN 'offline'
… ELSE $1` — outside draining/offline, the agent's self-reported status is
written as-is. DB now holds `working` + `current_job_id=NULL`.

### 4. The reaper manufactures a false `ready` (flip-flop)

`stale_agent_detector` (`main.py:496-567`) calls
`mark_stuck_working_agents_ready()` (`postgres.py:2849-2876`):

```sql
UPDATE agents SET status = 'ready' WHERE status = 'working' AND current_job_id IS NULL
```

Its docstring (`:2852`) asserts this state *"no normal lifecycle transition can
produce — the heartbeat handler updates status and current_job_id atomically."*
**That invariant is false** — step 1 produces exactly this state. The reaper
flips the DB row to `ready` (touching only the DB, never the pod); the pod's
next heartbeat re-asserts `working` (step 3); repeat. The row oscillates
`ready ↔ working` indefinitely — which is why a 13-day-old pod still triggers
the reaper. During each `ready` window the pod is a dispatch target.

### 5. Resume trusts the DB and hard-errors on rejection

`resume_job` (`main.py:5966`) auto-selects the first `status="ready"` agent
(`:6017-6058`, `list_agents(status="ready")`) with **no liveness check**, then
POSTs `/job/resume` (`:6157-6163`). The agent's resume handler rejects because
`_pod_state != PodState.IDLE` (`dual_app.py:827-832` — its only `409` path).
The orchestrator maps any non-2xx to `502` (`:6165-6169`,
`"Agent rejected resume request: …"`) → the cockpit toast.

### Why this is intermittent — and why the `502` is the *loud minority* case

Heartbeat cadence is **5s** (`postgres.py:2137`: "overwrite drain intent on the
next 5s tick"); the reaper runs every **60s** (`main.py:498,563`). So a zombie's
DB row is `ready` only in the ~5s window right after each reaper tick and
`working` the other ~55s. What a Resume click does depends on timing *and* pool
composition:

- **A healthy `ready` agent exists** → resume picks it and succeeds. No symptom.
- **A zombie is mid-`ready`-window and gets picked** (~5/60 of clicks, likeliest
  on an all-zombie pool) → agent `409` → **`502` toast.** The loud case.
- **The zombie is in its `working` window, or nothing is `ready`** → the
  synchronous endpoint takes the queue path (`main.py:6122-6158`, returns
  `{"status":"queued"}`, *no error*) and the async dispatcher retries — but
  `_resume_job_on_agent` **swallows the `409`** (`main.py:1521-1525`,
  `return False`). On an all-zombie pool the job then just **sits paused and
  never resumes**, with no toast at all.

So the real symptom surface is broader than the `502`: the *common* failure is a
silent "Resume did nothing / job stuck paused." Verifying the fix means checking
**both** — no `502` *and* the job actually resumes.

### Why `app.py` is immune (and what the structural root is)

`app.py` (worker-only / non-dual) models busy with a **single** variable:
`is_busy = _current_job_id is not None` (`:1123`), and its stop branch clears
`_current_job_id` (`:512`) — so clearing the job *is* clearing busy; it
self-heals. The bug is specific to **dual_app's two-variable model**
(`_pod_state` *and* `_current_job_id`), where the heartbeat derives status from
one and the job from the other and the cancel path only clears one. That
two-source-of-truth design is the real structural root.

## The fix

Priority order. Fix 1 is the bug; the rest is making the system tolerant.

### Fix 1 — Agent: funnel *every* stop path through `_reset_to_idle()` *(the bug — do this)*

`_reset_to_idle()` already nulls `_current_job_id` + `_current_job_task`
(`dual_app.py:343-344`) and calls `_clear_stop()` (`:345`), then flips
`_pod_state→IDLE` and pushes a `ready` heartbeat. Route all three leak sites
(A/B/C above) through it.

**Sites A + B** — the cooperative branch (`:495-501` job; `:882-886` resume,
which is the same shape minus the `reason`/log line):

```python
if _stop_requested.is_set():
    reason = _stop_reason
    logger.info(f"Job {job_id} stopped gracefully (reason={reason})")
    await _reset_to_idle(f"job {reason}")   # _pod_state→IDLE + ready heartbeat + log-handler cleanup
    _stop_completed.set()                    # MUST be after reset (see below)
    return
```

**Ordering constraint (load-bearing).** `_clear_stop()` (`:86-90`) clears
`_stop_completed`, which the waiting `cancel_job`/`pause_job` handler is blocked
on. `_stop_completed.set()` must fire *after* `await _reset_to_idle(...)`, or the
handler hangs to its 120s timeout.

**Site C** — the cancel handler's hard-kill timeout (`:778-793`). Replace the
manual `null job id + _clear_stop` (which skips `_pod_state`) with a reset after
the task is killed:

```python
except asyncio.TimeoutError:
    if _current_job_task and not _current_job_task.done():
        _current_job_task.cancel()
        try:
            await _current_job_task
        except asyncio.CancelledError:
            pass
    await _reset_to_idle("cancel hard-kill")   # replaces: _current_job_id=None; _clear_stop()
    return { ... "graceful": False }
```

No ordering issue here — the handler already timed out waiting on `_stop_completed`.
The task-side `except asyncio.CancelledError` (`:526-528`, `:909-910`) needs no
change: the handler now owns the reset, and the only other `CancelledError`
source is process shutdown, where a stranded `_pod_state` dies with the pod.

**Design decision — reset-and-stay, not reset-and-exit.** Normal completion does
`_reset_to_idle` *then* `_schedule_exit` (`:522-524`: pod dies, provisioner
respawns). The stop paths deliberately *don't* exit (`:501` comment: "pause means
orchestrator may want to reassign"), so Fix 1 resets to IDLE and leaves the pod
alive as a generic ready worker. In pod-per-task K8s mode this leaves a
workspace-less idle pod that the pool reconciler can scale down — harmless (it's
genuinely ready; the next dispatch re-provisions its workspace) and consistent
with loop mode. One code path for both cancel and pause.

**Rejected shortcut.** "Just report `ready` from the heartbeat when
`_current_job_id is None`" fixes the DB but the resume handler still gates on
`_pod_state != IDLE` (`:828`), so the pod keeps `409`-ing — converting a visible
flip-flop into a *permanent invisible* reject. The reset must touch `_pod_state`;
this is the correct layer.

### Fix 2 — Orchestrator: treat agent-`409` as "stale ready," not `502` *(recommend with Fix 1)*

In `resume_job`, special-case the agent `409` at the POST (`main.py:6252-6256`):
demote the picked agent (its DB `ready` was stale) and fall through to the
existing queue-for-auto-dispatch path (`:6122-6158`, which returns
`{"status":"queued"}` and triggers the dispatcher) instead of raising `502`. The
async dispatcher `_resume_job_on_agent` already does exactly this — swallows a
`409`, returns `False` (`main.py:1521-1525`) — so this only makes the synchronous
endpoint match. Net effect: the user never sees the error; the job re-queues and
a genuinely-ready agent picks it up. Principle: the pod is the source of truth —
react to its rejection rather than trusting the cached `ready`.

### Fix 3 — Orchestrator: make the reaper reconcile against the pod *(hardening — defer)*

`mark_stuck_working_agents_ready()` flips `working+no-job → ready` on the DB row
alone. Hardened: probe the pod's `/job/current` first; if genuinely non-idle,
mark it `offline`/recycle rather than re-advertising `ready`. Lower priority —
Fix 1 makes this state rare and Fix 2 makes it non-fatal. This overlaps the
"consistency repair" phase of the reconciler redesign in
[`agent_lifecycle_management.md`](../issues/agent_lifecycle_management.md) and is best
folded into that work rather than done standalone.

## Implementation roadmap

One PR — Fix 1 + Fix 2 are small and interlocking. Defer Fix 3.

| # | Where | Change | Done-when |
|---|---|---|---|
| 1 | `src/api/dual_app.py` | **Fix 1** — funnel sites **A** (`:495-501`), **B** (`:882-886`), **C** (`:778-793`) through `_reset_to_idle()`; keep `_stop_completed.set()` *after* the reset on A/B | ~10 lines, three branches |
| 2 | `tests/` (beside `test_drain_intent.py`) | Fix 1 unit tests — cooperative-stop **and** hard-kill (see Verification) | both assert `_pod_state==IDLE` + `ready`/`job_id=None` heartbeat |
| 3 | `orchestrator/main.py:6252-6256` | **Fix 2** — map agent-`409` → demote agent + queue path (`:6122-6158`), not `502` | resume never raises `502` on a stale-ready pick |
| 4 | `tests/` | Fix 2 unit test (see Verification) | mocked `409` → queued response |
| 5 | local | `pytest tests/test_dual_app.py tests/test_drain_intent.py -x` + `ruff check src/ orchestrator/ tests/` | green |
| 6 | k3d | start → cancel → resume e2e (see Verification) — **the gate** | no `502` *and* job resumes; reaper stops flip-flopping that agent |
| 7 | dev ops | recycle stranded pods (see "Immediate cleanup") | after Fix 1 lands; confirm with user first |

Deferred — **Fix 3** (reaper reconciles against the pod): folds into
`agent_lifecycle_management.md`'s consistency-repair phase. Fix 1 makes the state
rare; Fix 2 makes it non-fatal.

## Verification

- **Unit (agent):** (a) a cooperative stop mid-job leaves `_pod_state == IDLE`
  and emits a `ready`/`job_id=None` heartbeat; (b) a cancel whose `_stop_completed`
  never fires hits the 120s hard-kill branch and *also* lands at `_pod_state == IDLE`.
  Both fit alongside `tests/test_drain_intent.py`, which already drives these
  module globals.
- **Unit (orchestrator):** resume endpoint with a mocked agent returning `409`
  demotes the agent and returns the queued response, not `502`.
- **End-to-end on k3d:** start a job → cancel → resume → confirm it resumes with
  no `502`; confirm the reaper stops flip-flopping that agent (no recurring
  `Released … stuck in 'working'` for it). Watch
  `kubectl --context=k3d-srw -n srw logs -l srw/managed-by=agent-provisioner -f`.

## Immediate cleanup (separate from the fix)

Existing zombies do **not** self-heal from a code change — their `_pod_state` is
already stranded. On dev, `srw-agent-j-efdba170` (`a4adc71e`) and the original
agent of `e0360c9c` need their pods recycled (`kubectl delete pod` → provisioner
respawns fresh). Confirm with the user before deleting, and skip any agent with a
real `current_job_id`. Fix 1 prevents new ones; it can't un-strand existing ones
(same bootstrap caveat as `agent_lifecycle_management.md` §"Immediate cleanup").

## Open / to confirm

- **Heartbeat cadence — resolved.** 5s heartbeat (`postgres.py:2137`) vs 60s
  reaper (`main.py:498,563`); the ~5/60 `ready`-window math is in "Why
  intermittent" above. No longer blocking.
- **`_pod_state` relocation.** The proper fix in
  [`agent_app_readiness_drift.md`](../done/agent_app_readiness_drift.md) moves `_pod_state`
  into a shared `session_runtime` library. Fix 1 is small and urgent — land the
  three-site funnel first; the refactor inherits it.

## Related

- [`agent_app_readiness_drift.md`](../done/agent_app_readiness_drift.md) — sibling
  `_pod_state`/readiness drift in the *session* path; predicted this worker-route
  variant; its app-unification refactor would relocate `_pod_state`.
- [`agent_lifecycle_management.md`](../issues/agent_lifecycle_management.md) — the systemic
  reconciler redesign; owns the reaper. Fix 3 belongs there. This doc is the
  concrete, immediately-shippable bug instance + minimal fix (Fix 1+2) that
  doesn't require the redesign.
- `docs/features/dual_mode_agent.md` — the `_pod_state` (`IDLE`/`WORKING`/`SESSION`)
  state machine.
