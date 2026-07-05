# A non-atomic loop advance wedges the whole project loop (running + current_job_id=NULL)

**Status:** investigated 2026-07-05 — root cause confirmed end-to-end from the live wedge, incident recovered by hand; **fix 2 (sweeper self-heal) IMPLEMENTED & k3d-verified 2026-07-05**; regression found same day (the heal raced healthy in-flight advances and double-spawned iteration 14 — see "Regression" section) and **CLOSED by the age gate, implemented 2026-07-05**: the heal now only fires when the NULL-pointer state is older than `PROJECT_LOOP_HEAL_GRACE_SECONDS` (default 600 s) — Python pre-check on the row's `updated_at` plus an authoritative DB-clock guard inside `heal_project_loop_pointer` (`AND updated_at < now() - make_interval(secs => $6)`). Tests: `tests/test_project_loop_sweeper.py` `TestHealAgeGate` (+5: fresh-NULL deferred silently before any DB reads, stale heals, naive-timestamp handling, unknown-age defers to the SQL guard, sweep-tick no-advance on fresh wedge). Fixes 1 (advance atomicity, incl. guarding tx C) and 3 (UI stalled badge) still open.
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
2. **Make the sweeper actually recover this state.** ✅ **IMPLEMENTED 2026-07-05.** For a `running` loop with `current_job_id IS NULL`, the sweeper now re-points at the newest spawned job (`list_project_loop_jobs` → `get_job`) and reconciles the counters the lost write-back dropped, deriving them from the job's spawn-time `context.loop_iteration`/`loop_role` stamps: `total_jobs_run = N`, `seq_index = (N-1) % len(role_sequence)` (start endpoint spawns iteration 1 at index 0; the sequence is immutable after start — the router has no update endpoint), `remaining_iterations = max_iterations - (N-1)` (seeded equal at create, decremented once per completed advance; None for deadline-only loops). The re-point is a guarded atomic UPDATE (`heal_project_loop_pointer`, `WHERE current_job_id IS NULL AND status='running'`) so concurrent sweeper replicas heal exactly once; the healed pointer then flows into the existing terminal-advance path (same tick if the job finished, via the completion hook if still running). Unguessable states (no spawned jobs — a create→first-spawn crash — or an unreadable iteration stamp) keep the loud "needs attention" warning. Code: `orchestrator/services/project_loop_sweeper.py` (`_heal_wedged_loop`, `_derive_loop_counters`), `orchestrator/database/postgres.py` (`heal_project_loop_pointer`). Tests: `tests/test_project_loop_sweeper.py` (21 cases: incident shape, claim→spawn tear identity, first-iteration tear, deadline-only, clamp, role-stamp precedence, refusal paths, guard race, running-orphan heal-without-advance, advance-exception containment). k3d-verified end-to-end by staging the exact incident state (running+NULL+completed orphan iter 2, max 2): logs show `healed torn advance — re-pointed at critic job … (iteration 2, seq_index 1, remaining 1)` then same-tick advance → budget stop (`completed`/`stop_reason=budget`/`remaining=0`); the second replica lost the guard silently as designed. With this in place, the production incident would have self-healed in ≤60 s instead of needing a 12 h-late manual UPDATE.
3. **Surface it in the UI.** Badge `running` + null-current-job (or `total_jobs_run < jobs.length`) as stalled, so the header/list divergence reads as "stuck," not "progressing."

## Regression: the heal races in-flight advances (discovered 2026-07-05, same day it deployed)

Fix 2 shipped to the main cluster (commits `cd37b41e`/`89b66f9e`, image `sha-c6a6b3d`) and the orchestrator now runs **two replicas**. Within hours, **iteration 14 spawned twice** (loop `105a6f98`, critics `ae7691c0` 13:29:05 UTC and `e785923c` 13:29:17 UTC — 12 s apart).

**Root cause of the regression:** the heal's trigger condition — `status='running' AND current_job_id IS NULL` — is not unique to a *torn* advance. It is also the **normal transient state of every healthy advance**, between the claim (tx A, nulls the pointer) and the re-point (tx C). The claim→spawn segment alone measured ~23 s on iteration 13's advance (gitea squash-merge at 13:28:42, retro commit at 13:28:50, spawn at 13:29:05). With a 60 s sweeper tick in each of two replicas, a tick lands inside that window roughly every other advance.

Two variants, both observed:

- **Harmful (13:29, old pods, reconstructed from DB + gitea):** replica A's completion hook for iter-13 claimed (pointer→NULL) and was doing the merge/retro. Replica B's sweeper ticked, saw NULL, "healed" by re-pointing at the newest job — still the *completed* iter-13 scholar — then, same tick, saw a terminal current job and ran a **second full advance**: second squash-merge (no-op, branch already merged), second retro write (deduped, identical content), **second iter-14 critic spawned**. Advance #2's unguarded tx C overwrote advance #1's pointer, so critic `ae7691c0` was orphaned: its failure advance early-returned (no retro, not counted — hence `total_jobs_run=15` with 16 jobs in the list), while `e785923c`'s failure advanced the loop to iter 15. The repo escaped damage only because a duplicate *critic* has idempotent side effects — a duplicate **developer** would race real writes.
- **Benign (13:38, caught live in current pod logs):** `srw-orchestrator-…-t42fr` ran the iter-15 advance while `…-b76fl` logged `healed torn advance — re-pointed at developer job 6b7a8668 (iteration 15, seq_index 2, remaining 19)`. The sweeper tick landed *after* the spawn, so the "heal" re-pointed at the already-spawned in-flight job with exactly the counters tx C was about to write — harmless, but proof the heal fires on routine advances.

Why k3d verification missed it: single replica + a statically staged wedge — there was never a *live* advance mid-flight for the sweeper to misread.

**Fix (age gate) — IMPLEMENTED 2026-07-05:** a torn advance is distinguishable from an in-flight one by **age**. The claim stamps `updated_at=now()`; a healthy advance re-points within seconds, while the real incident sat at NULL for 12 h. The heal now only fires when the NULL state is stale:

- Python pre-check in `_heal_wedged_loop` (`_wedge_age_seconds`): defers silently (debug log, no "needs attention" warning, no DB reads) when `loop.updated_at` is younger than `PROJECT_LOOP_HEAL_GRACE_SECONDS` (default 600, env-tunable) — an advance is in flight; a real tear will still be there, older, next tick.
- Authoritative SQL guard in `heal_project_loop_pointer`: `… AND updated_at < now() - make_interval(secs => $6)` — evaluated on the DB clock, immune to a stale Python-side read racing a fresh re-claim, and to clock skew.

A 10-minute grace is orders of magnitude above any healthy advance duration and irrelevant against the backstop's purpose (a real wedge previously sat for 12 h; worst case a genuine tear now waits ~10 min to self-heal instead of ~60 s). Separately, tx C (`update_project_loop` in the advance) should still become conditional (`WHERE current_job_id IS NULL`) as part of fix 1 so a late tx C can never clobber a healed pointer.

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
