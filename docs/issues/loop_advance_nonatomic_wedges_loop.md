# A non-atomic loop advance wedges the whole project loop (running + current_job_id=NULL)

**Status:** investigated 2026-07-05 — root cause confirmed end-to-end from the live wedge, incident recovered by hand, fix proposed, **not yet implemented**
**Severity:** high for the RSI loop — one interrupted advance silently stops the entire self-improvement loop indefinitely (the observed loop was dead ~12 h before anyone noticed)
**Component:** `orchestrator/main.py` `_advance_project_loop` / `_spawn_loop_job`, `orchestrator/database/postgres.py` loop methods, `orchestrator/services/project_loop_sweeper.py`, `cockpit/.../project-loop.component.ts`
**Observed on:** main cluster (`superhuman-remote-worker`), loop `105a6f98-134c-4077-b7e1-6d08916650d7` (project `68137e29` "Better Resavio")
**Related:** `docs/issues/reranker_transient_fault_hard_fails_job.md` (the trigger — a different bug), `docs/features/project_self_improvement_loop.md`

---

## TL;DR

`_advance_project_loop` rotates a project loop by running three **independent, separately-committed** transactions: (A) claim = null `current_job_id`, (B) create the next role's job, (C) re-point `current_job_id` at that new job + bump `seq_index`/`total_jobs_run`. If execution is interrupted between B and C — a swallowed exception, a cancelled request task, or a transient DB blip — the loop is left `status='running'` with `current_job_id=NULL` **and the next job already created but orphaned from the pointer**. The safety-net sweeper is hard-coded to *not* recover a NULL-current-job loop (it can't tell whether a next job was spawned), so it just logs "needs attention" forever. The loop stops advancing and no more iterations run.

A secondary, cosmetic-but-confusing effect: the cockpit Loop page renders its header from the (now-stale) control-row counters while its "Jobs this run" list reads the jobs table directly, so the two disagree — the header says "job 9" while the list shows 10 jobs. That divergence is actually the cleanest visual tell that a loop has wedged this way.

## Symptom (observed)

Loop `105a6f98` ran cleanly through iterations 1–9, then **stopped spawning jobs after iteration 10 completed**. State when investigated (~12 h later):

- `project_loops` row: `status=running`, `current_job_id=NULL`, `seq_index=2` (developer), `total_jobs_run=9`, `remaining_iterations=25`, `stop_reason=NULL`, `last_error=NULL`, `updated_at=2026-07-04 19:33:16`.
- Orchestrator log, every 60 s since `19:33:48` and continuing for ~12 h:
  `services.project_loop_sweeper: "project loop 105a6f98 is running with no current_job_id — needs attention"`.
- Jobs table: 10 loop jobs exist; the newest, `3fff55f7` (iter-10 scholar), **completed** at 20:54 but never advanced the loop.
- `retros/` on `main` contains `001-…` through `009-…` but **no `010-…`** file.

## Timeline (loop 105a6f98)

| time (UTC) | event |
|-----------|-------|
| 19:02:13 | iter 9 (developer, `f3c59632`) spawned by iter-8's advance; loop row: seq_index=2, total_jobs_run=9, current_job_id=iter9 |
| ~19:33:16 | iter 9 **fails** (reranker `ReadTimeout` — the separate bug). Its completion runs `_advance_project_loop`: **tx A claim commits** → `current_job_id=NULL`, `updated_at=19:33:16` |
| 19:33:20 | **tx B commits** → iter 10 (scholar, `3fff55f7`) row created |
| — | **tx C never commits** — no further write to the loop row, ever |
| 19:33:48 | sweeper starts logging "running with no current_job_id — needs attention" (repeats every 60 s) |
| 20:54 | iter 10 completes, but its advance early-returns (`job.id != current_job_id`, since NULL) → no merge, no `retros/010`, no iter 11 |
| +12 h | loop still `running`, still no current job, still not advancing |

## Root cause

`_advance_project_loop` (`orchestrator/main.py` ~10214) is **not atomic**. It performs three separate committed transactions with no overarching transaction and no crash-consistent recovery:

1. **tx A — claim.** `claim_project_loop_advance` (`postgres.py:9724`):
   `UPDATE project_loops SET current_job_id=NULL, updated_at=now() WHERE id=$loop AND current_job_id=$job AND status='running'`. Its own `acquire()` → commits immediately. (This atomic claim correctly prevents *double*-advance; it does nothing for *torn*-advance.)
2. **tx B — create next job.** `_spawn_loop_job` (`main.py:10170`) → `create_loop_job` → `db.create_job` inserts the next iteration's row and commits. (Repo provisioning + dispatch that follow are explicitly non-fatal.)
3. **tx C — re-point.** `update_project_loop` (`main.py:10420`, `postgres.py:9645`):
   `UPDATE project_loops SET current_job_id=<new>, seq_index=…, total_jobs_run=…, remaining_iterations=… WHERE id=$loop`.

The whole call is wrapped by the completion hook in a `try/except` that **swallows** any exception (`main.py:11109-11115`, "Error advancing project loop"). So if anything interrupts execution after tx B commits and before tx C commits — a transient asyncpg error on tx C, the request task being cancelled mid-await, an exception in the small window — the loop is permanently left in the tx-A state (`current_job_id=NULL`, counters unbumped) with an orphaned job already created.

**The live evidence pins the interruption to the B→C gap:** tx A committed at 19:33:16, tx B (iter-10 row) committed at 19:33:20, and there is **no loop-row write after 19:33:16** — so tx C never ran. The row is still `status='running'` (not `failed`), which means the spawn-failure `except` (`main.py:10406`, which would set `status='failed'`) did **not** fire either — i.e. `_spawn_loop_job` itself succeeded. The only unguarded step left is tx C.

### Why the sweeper doesn't recover it

`project_loop_sweeper.py:75-83`: for a running loop whose `current_job_id` is NULL, the sweeper logs "needs attention" and `continue`s, deliberately, because it "can't guess a recovery" — it doesn't know whether a next job was already spawned. But the information exists: `list_project_loop_jobs(loop_id)` (already implemented, `postgres.py:9685`) returns the loop's jobs newest-first, and the newest here is a terminal iter-10 that was never advanced. The sweeper *could* recover by advancing from the newest terminal loop job.

## The UI misalignment (secondary finding)

The cockpit Loop page (`cockpit/src/app/views/project-detail/project-loop.component.ts`) drives its two displays from **different sources**, and neither reads `current_job_id`:

- **Header** (line 118): `{{ currentRole() }} · job {{ l.total_jobs_run }}{{ ' of ' + l.max_iterations }}`, where `currentRole()` (lines 433-436) = `l.role_sequence[l.seq_index % l.role_sequence.length]`. All from the **control row** → renders "developer · job 9 of 33" (frozen at the wedge).
- **"Jobs this run (N)"** (lines 166-167, populated at line 487 via `api.listProjectLoopJobs` → `list_project_loop_jobs`, `WHERE context->>'loop_id'`): a direct **jobs-table** query → shows 10 real jobs, newest an orphaned scholar.

So the header count being **less than** the "Jobs this run" count is the visible fingerprint of a lost tx C: N jobs were created but the loop's own `total_jobs_run` never caught up. It also makes a wedged loop *look* like it's still progressing (a fresh job sits at the top of the list), which is how this went unnoticed for ~12 h. Worth a small UI signal: when `status='running'` but `current_job_id` is null (and/or `total_jobs_run < jobs.length`), badge the loop as "stalled — needs attention" instead of the plain "Running" chip.

## Relation to the reranker bug

Two **distinct** bugs on the same chain. The reranker transient-timeout hard-fail (`reranker_transient_fault_hard_fails_job.md`) is what killed iter 9; iter 9's terminal transition is what *invoked* the advance that then tore. But this wedge is an independent non-atomicity defect — it can strike the advance of **any** job, success or failure (a success path is actually a *longer* B→C window because it also does the gitea squash-merge + retro before tx C). A transient version of the same NULL-current-job state even appeared right after the 18:26 deploy and self-healed only because a completion hook happened to re-run tx C. Fixing the reranker bug makes jobs die less often; it does **not** fix this.

## Fix directions

1. **Make the advance crash-consistent.** Options, roughly in order of preference:
   - Fold claim + create + re-point so the loop row is never left pointing at nothing: e.g. create the next job first, then a single atomic `UPDATE` that both validates the old current job is terminal and sets the new pointer/counters (compare-and-set), so an interrupt either leaves the *old* pointer (sweeper re-advances) or the *new* one (done) — never NULL.
   - Or make the advance idempotent and keyed on the newest loop job, so a re-run reconstructs the correct next state.
2. **Make the sweeper actually recover this state.** For a `running` loop with `current_job_id IS NULL`, look up `list_project_loop_jobs(loop_id)`; if the newest job is terminal, re-point to it and run the advance (idempotent via the claim). Turn the current "needs attention" dead-end into a real self-heal. Keep a bounded attempt counter so a genuinely broken loop still fails loud rather than looping.
3. **Surface it in the UI.** Badge `running` + null-current-job (or `total_jobs_run < jobs.length`) as stalled, so the header/list divergence reads as "stuck," not "progressing."

## Recovery performed (2026-07-05)

Manual un-wedge on the cluster (guarded so it no-ops if the state had changed):

```sql
UPDATE project_loops
SET current_job_id='3fff55f7-33f2-442a-94fe-b2bcfd393a12',  -- completed iter-10 scholar
    seq_index=0,            -- scholar = the role that actually ran as iter 10
    total_jobs_run=10,      -- reflect the 10 jobs really created
    remaining_iterations=24 -- decrement for iter 10 (the lost tx C never did); keeps run+left invariant
WHERE id='105a6f98-134c-4077-b7e1-6d08916650d7'
  AND status='running' AND current_job_id IS NULL;
```

With a terminal current-job on a running loop, the safety-net sweeper advances within one tick (≤60 s): merges iter 10, writes the missing `retros/010`, rotates `seq_index 0→1`, and spawns iter 11 (critic). The loop then continues on its own. This is a data patch for the one stranded loop — it does not address the root cause; the fixes above still need to land.

## Verification sketch (for the fix)

- Unit: simulate tx C raising after tx B commits → assert the loop is left recoverable (never `running`+null with an un-advanced newest job), and that a subsequent sweeper tick spawns exactly one next job.
- Unit: sweeper given a `running` loop with `current_job_id IS NULL` and a terminal newest loop job → advances exactly once (idempotent under two concurrent replicas via the claim).
- Cockpit: `running` + null current-job renders a "stalled" badge, and `total_jobs_run < jobs.length` is reconciled/flagged.
