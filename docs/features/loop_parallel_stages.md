---
tags:
  - feature
  - orchestration
  - projects
  - self-improvement
  - concurrency
  - experts
aliases:
  - parallel stage
  - scholar + qa stage
  - stage fan-out
  - product-qa loop integration
related:
  - "[[project_self_improvement_loop]]"
  - "[[loop_parallel_execution]]"
  - "[[loop_repo_compounding_v2]]"
  - "[[kb_convergence_ttl_reverification]]"
  - "[[project_knowledge_base]]"
---

# Loop Parallel Stages — Scholar ∥ Product-QA feeding the Critic

> Today a project loop's `role_sequence` is a flat list of roles run **one at a time**. This feature makes a sequence entry optionally a **set of roles that run concurrently as one stage**: `[["scholar", "product-qa"], "critic", "developer"]`. Scholar looks *outward* (new opportunities) while Product-QA looks *inward* (issues, gaps, missing product surfaces); both write candidate work items to the KB; the Critic then triages **build-a-feature vs. fix-the-product** from a quiescent KB. Parallel stages are restricted to **analysis roles** — the execution role stays a singleton, so the repo-compounding invariant is untouched.

## Status

**Phase 0 + Phase 1 COMPLETE (2026-07-07), uncommitted.** Phase 0 loop-wires the `product-qa` expert (sequential) — done, unit-tested, and live-verified on a k3d gemma smoke loop (`670130c6`: scholar→product-qa→critic advanced correctly, no F29). Phase 1 adds the concurrent stage — schema, grammar, barrier, advance, and torn-advance recovery — done, with **70 loop unit tests green**, the barrier SQL **validated against real Postgres** (last-out drill), and the **refactored single-role advance path live-verified on k3d** (the gemma loop's scholar→product-qa rotation ran through the new `_spawn_loop_stage`/`_rotate_loop_to_next_stage` and stamped `loop_seq_index` correctly). The last Phase 1 sub-item — disabling `memory_assembler` for fan-out members — is **done** (scalar `auxiliary.tasks.assemble_memories.enabled` override, 4 unit tests), so Phase 1 is complete.

The k3d smoke also surfaced — and we fixed — an **unrelated interaction bug**: Mode-A cloud-diff capture held the product-qa job at `pending_review`, wedging the loop (the advance hook fires only on a terminal status). Loop jobs are now exempt from that gate. Details in [Phase-0 smoke findings](#phase-0-smoke-findings) and `docs/done/job_cloud_export.md`.

**Phase 2** (cockpit stage chips) and **Phase 3** (representative `scholar ∥ product-qa` e2e on homelab/minimax + flip the Better Resavio loop) remain.

## Phase-0 smoke findings

The k3d gemma smoke (`670130c6`, sequence `[scholar, product-qa, critic, developer]`) validated the loop-wiring plumbing and, in doing so, surfaced one **unrelated pre-existing bug** worth recording because it would break *any* self-improvement loop on a cloud-attached project.

**Mode-A cloud-diff capture wedges the loop at `pending_review`.** The job-cloud-export "Mode A" completion hook (`orchestrator/main.py`, §1a) overrides a job's status from `completed → pending_review` whenever it has a `cloud_diff_baseline_commit` *and* wrote changes under `projects/<slug>/`, so a human can review the diff before it's pushed to the cloud folder. Loop jobs attach the project's datasources (so they receive a baseline) and routinely write project files — the `developer` role always, `product-qa` when it drops a repro script or audit transcript. The loop advance hook (`main.py`, §5d) fires **only** on a terminal status (`completed`/`failed`/`cancelled`), and the torn-advance sweeper only heals a NULL `current_job_id`, so a job parked at `pending_review` stalls the loop indefinitely — every iteration for an execution role. Observed live: product-qa `7515953a` sat at `pending_review`, loop pinned at `seq_index 1`.

**Fix (implemented, uncommitted):** loop jobs are exempt from the Mode-A gate. A new pure helper `services/project_loops.py:job_loop_id(job)` (reads `context.loop_id`, JSON-string tolerant, 7 unit tests) gates the block: `... and not job_loop_id(job)`. A loop routes its changes through its own branch squash-merge + `retros/` trail, so the cloud accept/reject diff path is inapplicable and a human review gate is the wrong gate for an unattended `autonomy: full` loop. Full write-up in `docs/done/job_cloud_export.md` (Update 2026-07-07).

**Verification:** 7 unit tests for the helper (+63 → **70 loop tests green**), ruff clean, guard confirmed live on the k3d pod. The sweeper self-heal was live-verified: flipping the wedged job to `completed` made the sweeper re-run the advance and rotate the loop cleanly to `critic` — confirming `pending_review` was the *only* blocker (it is deliberately outside the sweeper's `_TERMINAL` set). **The definitive live check passed 2026-07-07:** the smoke loop rotated all the way through `scholar → product-qa → critic → developer`, and the `developer` job (`3ce2f836`) — an execution role that *did* carry a `cloud_diff_baseline_commit` — landed **`completed`, not `pending_review`**, with `diff_status` never set (the Mode-A capture block was skipped entirely, exactly as the `and not job_loop_id(job)` guard intends). The loop then advanced developer → done cleanly (`rem 0`). Execution roles no longer wedge under Mode-A.

## Problem

The Better Resavio run (28+ iterations) exposed a structural blind spot in the Scholar → Critic → Developer rotation: the Scholar keeps finding *new* domain features, the Critic selects among them, the Developer ships them — and nobody ever asks whether the accumulated product is **usable**. The loop produced a well-tested backend domain engine with no UI, no demo path, and packaging defects, because every input to the Critic's triage was a *new-feature proposal*. The Critic can only choose among what it's given.

The fix is a counterweight input stream: a **Product QA** role that audits the current application as a product and files evidence-backed issue candidates (missing UI, broken setup, integration gaps, regressions) that **compete with Scholar proposals in the same triage**. For that competition to be fair and causally clean, both streams should be produced against the same repo state and both be complete before the Critic reads them.

Two ways to get there:

1. **Sequential insert** (works with zero orchestrator changes): `["scholar", "product-qa", "critic", "developer"]`. Both analysis roles see the same repo state anyway (analysis roles merge empty — the repo doesn't change between them), so this is *functionally* equivalent.
2. **Parallel stage** (this doc): `[["scholar", "product-qa"], "critic", "developer"]`. Saves one full job duration per cycle and keeps the cycle count semantics ("one analysis step, one triage, one build") instead of quietly growing the cycle from 3 to 4 sequential slots.

We do 1 as Phase 0 (it's also the fallback if parallel stalls) and build 2 on top.

## Relationship to the pipelining concept paper

[`loop_parallel_execution.md`](loop_parallel_execution.md) explored making the *whole loop* concurrent and concluded pipelining the chain (its Option A) is the worst trade — cross-generation KB leakage, convergence races, developer-capped ceiling. **This feature is not that.** It is a narrow, barriered instance of that doc's **Option C (stage fan-out)** — parallel *producers*, single consumer — which the paper itself rates lowest-risk because producers only **add** KB notes. Concretely, the hazards the paper identified and how this design dodges them:

| Hazard (from the concept paper) | Why it doesn't apply here |
|---|---|
| Generation leakage — Critic(T) reads Scholar(T)'s live writes | The Critic runs **strictly after the stage barrier**; it reads a quiescent KB. The only concurrency is *within* the stage, between two additive writers. |
| Convergence races — concurrent `AssembleKnowledgeTask` supersede/TTL mutations | Fan-out members run with the `memory_assembler` writer **disabled** via a scalar `config_override.auxiliary.tasks.assemble_memories.enabled = false`; the single curation pass happens in the Critic's job immediately after (see [KB safety](#kb-safety)). |
| Execution singleton — two developers writing `main` | Parallel stages are **restricted to analysis roles** (validation at loop start). The execution role can never be in a parallel stage. |
| Wall-clock ceiling capped by serial developer | Not the goal. The goal is a *second input channel for the Critic*; the one-job-duration saving per cycle is a side benefit. |

The concept paper's Option A (true pipelining) remains unbuilt and undecided; nothing here forecloses it.

## Design

### role_sequence grammar

A `role_sequence` entry is either a **string** (single-role stage, exactly today's behavior) or a **non-empty array of distinct strings** (parallel stage):

```jsonc
["scholar", "critic", "developer"]                      // today — unchanged
[["scholar", "product-qa"], "critic", "developer"]      // parallel analysis stage
```

The existing DB CHECK (`jsonb_typeof = 'array' AND length >= 1`) already admits nested arrays; entry-shape validation lives in the start endpoint (`orchestrator/routers/project_loops.py`):

- every entry: string, or non-empty array of distinct strings;
- every role in a **parallel** stage must be an analysis role (`LOOP_ANALYSIS_ROLES`) — reject otherwise with a clear 422;
- `seq_index` semantics unchanged: it indexes **stages** (sequence entries), not jobs.

### Stage tracking: `current_stage_jobs`

`project_loops.current_job_id` is a single FK and stays the pointer for single-role stages — **the existing advance path is untouched** (deliberate: that code path carries the torn-advance scar tissue of [loop_advance_nonatomic_wedges_loop.md](../issues/loop_advance_nonatomic_wedges_loop.md) and we don't rewrite it).

New column (migration `orchestrator/database/migrations/app/00NN_loop_parallel_stages.sql`):

```sql
ALTER TABLE project_loops
  ADD COLUMN current_stage_jobs JSONB NOT NULL DEFAULT '[]'::jsonb;
```

Invariant: **exactly one of the two pointers is populated** while a stage is in flight. Single-role stage → `current_job_id` set, `current_stage_jobs = []`. Parallel stage → `current_job_id IS NULL`, `current_stage_jobs = ["<job-uuid>", ...]`.

### Advance algorithm (barrier)

`_advance_project_loop` (`orchestrator/main.py:10315`) grows a second entry path. When the completed job is *not* `current_job_id`, check membership in `current_stage_jobs`:

1. **Per-job completion work first, unchanged and per-hook**: squash-merge the job's branch (expected `empty` for analysis roles), write the retro, record `merge_status`. Each stage job's own completion hook does its own merge/retro (`_merge_and_retro_loop_job`, shared with the single-role path) — so when the barrier releases, all sibling contributions are already landed.
2. **Atomic barrier claim** — `claim_project_loop_stage_barrier(loop_id, member_id)`, the parallel analogue of `claim_project_loop_advance`.

**As built — deviation from the sketch above.** Rather than *incrementally shrinking* the array (`current_stage_jobs - $2 RETURNING remaining`), membership is **immutable for the stage's life** and drained in one shot by the member that finishes last:

```sql
UPDATE project_loops
SET current_stage_jobs = '[]'::jsonb, updated_at = now()
WHERE id = $1 AND status = 'running'
  AND jsonb_array_length(current_stage_jobs) > 0
  AND current_stage_jobs @> to_jsonb($2::text)              -- this member still in the set
  AND NOT EXISTS (                                          -- and NO member still runs
      SELECT 1 FROM jobs j
      WHERE j.id::text IN (SELECT jsonb_array_elements_text(project_loops.current_stage_jobs))
        AND j.status NOT IN ('completed','failed','cancelled'));
```

   Each member runs this after it goes terminal (its own status is already written before the hook fires). Everyone but the last matches no row: an *earlier* finisher fails the `NOT EXISTS` (a later member still runs); the *co-last* loser fails `jsonb_array_length > 0` (the winner already drained it); a *stray/re-delivered* hook after rotation fails `@>` (no longer in the set). Exactly one caller drains → exactly one rotate.

   **Why the change:** incremental shrink leaves the array in a *partially-drained* intermediate state, so a torn advance could strand it at any width — many signatures to reason about in recovery. One-shot drain means the only post-barrier state is `current_stage_jobs = '[]'`, i.e. **the same single wedge signature the single-role tear already has** (`current_job_id IS NULL ∧ current_stage_jobs = '[]'`). Recovery reasons about one shape, not N.

3. **Not last out** (claim returned no row) → record own merge and return. No budget/rotation work.
4. **Last out** (claim drained the set) → `_advance_loop_parallel_member` aggregates the stage outcome from all members' final statuses, evaluates stop conditions, decrements budget, and calls the shared `_rotate_loop_to_next_stage`: `seq_index = (seq_index + 1) % len(role_sequence)`, spawn the next stage (1 job → `current_job_id`; N jobs → `current_stage_jobs`), write back counters. Single writer — no concurrent-decrement races by construction.

The KB cycle-TTL decrement (fires on `next_index == 0`) is unchanged — it's already keyed to the sequence wrap, which is now a stage wrap. Both advance paths reach it through `_rotate_loop_to_next_stage`.

### Counter semantics

**As built — deviation from "iteration = stage".** The original plan kept `loop_iteration = stage ordinal` so the sweeper's `(N-1) % len(role_sequence)` modulo still held. But that modulo *breaks* the moment stages have different widths (a 2-wide stage 0 then a 1-wide stage 1 means job-count no longer maps to stage index). Rather than add a `stage_count` column to carry the ordinal, the implementation **stamps the counters directly**:

- `loop_iteration` = the **post-stage cumulative job count** (`base_total + width`), shared by every member of a stage. For all-single-role loops this is identical to today (`iteration == total_jobs_run`), so nothing changes for the common case; a fan-out stage's members simply share the cumulative count (the first `[scholar, product-qa]` stage shows iter 2 — cosmetically lumpy, honest, monotonic).
- `loop_seq_index` and `loop_remaining` are stamped into each job's `context` at spawn (`create_loop_job`). These are **spawn-time truth the heal reads back directly** — no arithmetic, correct for any stage-width mix.

`_derive_loop_counters` *prefers* these stamps and falls back to the legacy modulo only for pre-parallel (stamp-less) single-role jobs, so the migration is seamless.

Consequences, stated honestly:

- `remaining_iterations` buys **stages**; a parallel stage burns 2 jobs (≈2× tokens) per iteration tick. The cockpit loop card should surface jobs-per-cycle so the budget isn't misread. (On the MiniMax token plan this is a non-issue; on metered keys it matters.)
- `total_jobs_run` keeps counting **jobs** (its name is its contract; retros and audit math depend on it): `total_jobs_run += len(stage)` at spawn.

**Failure semantics**: a stage counts toward `consecutive_failures` only when **all** its jobs failed (no forward signal produced). Partial failure — Scholar dies, Product-QA lands findings — resets the counter to 0, logs loudly, and the retro records the dead sibling; the Critic can still triage the surviving stream. `last_error` carries the failed sibling's error either way.

**Stop conditions** are evaluated only at barrier release (stages are atomic: never stop with a sibling still running; never spawn a partial stage). If `remaining_iterations == 1` and the next stage is 2-wide, the full stage is spawned — budget is a stage budget, there is no mid-stage stop.

### Sweeper + heal stage-awareness

`project_loop_sweeper.py` gets two extensions, both mirroring existing logic:

1. **Missed-completion backstop** (`_sweep_parallel_stage`): for a running loop with non-empty `current_stage_jobs`, act only once **every** member is terminal (a missed barrier hook) and re-run `advance_fn(rep, {}, [])`. Idempotent via the barrier claim, exactly like the `current_job_id` path — while any member still runs, the members' own hooks fire the barrier, so the sweep leaves it alone (no age gate needed; the barrier is the guard).
2. **Wedge discrimination** (`_heal_wedged_loop`, via `get_newest_loop_stage`): today `current_job_id IS NULL ∧ running ∧ old` = torn advance. A healthy parallel stage in flight has non-empty `current_stage_jobs` and is handled by (1); a *torn* parallel advance drained the set in one shot, so it lands on the **same** `current_job_id IS NULL ∧ current_stage_jobs = '[]'` signature as a single-role tear. `heal_project_loop_pointer` gains the guard `AND current_stage_jobs = '[]'::jsonb`; the heal then fetches the newest stage (all jobs at the max `loop_iteration`) and branches on width + status:
   - **width 1** → re-point `current_job_id` (unchanged single-role behavior).
   - **width N, some member still running** (torn during the next stage's spawn) → `heal_project_loop_stage` restores `current_stage_jobs` to the **full** membership (the barrier checks live statuses, so already-terminal members count correctly); the members' hooks / next tick fire the rotate.
   - **width N, all terminal** (torn after the last-out drain, before the rotate) → re-point `current_job_id` at a representative so the normal single-job advance rotates.

   A representative's re-merge on recovery is a harmless `empty` (analysis-only stages); a member whose hook was fully lost has its retro skipped (documented gap — analysis merges carry nothing on `main` anyway). Misclassifying a just-finished Tear B as Tear A self-corrects next tick.

The age-gate discipline (`PROJECT_LOOP_HEAL_GRACE_SECONDS`, DB-clock guard) applies unchanged — this feature must not reopen the double-spawn incident.

### KB safety

Same-stage roles are **additive-only** KB writers — Scholar writes `plan` notes tagged `proposal`, Product-QA writes `plan` notes tagged `qa-finding`. Neither supersedes, flips status, nor curates. Cross-contamination via recency-boosted search (Scholar surfacing QA's in-flight notes or vice versa) is harmless-to-useful; the *dangerous* reader — the Critic, who supersedes losers — runs strictly post-barrier.

The one real race is the per-job **`memory_assembler`** aux writer (TTL curation / supersede re-verification over the *project-scoped shared* RecallStore — a read-modify-write that retires/re-TTLs existing memories), which was designed single-writer-per-turn ([kb_convergence_ttl_reverification.md](kb_convergence_ttl_reverification.md)). N of these running concurrently over one project would race that curation slot. Fan-out members disable it; the Critic's job — immediately after the barrier — runs the cycle's single curation pass.

> **✅ IMPLEMENTED (Phase 1).** `_spawn_loop_stage` sets `disable_memory_assembler=len(roles) > 1` on each member, threaded through `_spawn_loop_job` → `create_loop_job`, which injects `config_override.auxiliary.tasks.assemble_memories.enabled = false`.
>
> **As-built deviation — cleaner lever than the doc sketched.** The original plan was to list-*replace* `memory.pipeline.writers` with the full list minus `memory_assembler` (brittle: duplicates the default list, drifts when writers change, and would clobber any expert customization). Instead we flip the **existing** `auxiliary.tasks.assemble_memories.enabled` gate that the assembler writer already honours (`src/services/memory/plugins/legacy_writers.py:_aux_task_enabled`, `MemoryAssembler.on_event`) — the same lever `config/persistent_defaults.yaml` uses to disable it there. This is a **scalar deep-merge**: the writers list, the append-only extractors, and the KB curator (`curate_knowledge`) all keep running untouched; only the racy TTL-curation pass is silenced. Single-role stages are unaffected (`is_fan_out` is `False`), so sequential loops still compound the assembler every iteration.

### Expert loop-wiring (Phase 0 — independent of concurrency)

The `product-qa` expert config is valid (loader-verified) but the loop doesn't know the role. All in `orchestrator/services/project_loops.py`:

1. **`LOOP_ANALYSIS_ROLES`** `+= "product-qa"`. Without this it's treated as an execution role and every iteration trips the F29 empty-merge alarm.
2. **`_ROLE_TASKS["product-qa"]`**: `"audit the current product & file issue candidates"`.
3. **`_ROLE_BLOCKS["product-qa"]`** — the loop-kickoff protocol, mirroring the scholar/critic blocks' register:
   - Audit the application **as a product a user must operate**: setup from fresh checkout, runnable surfaces (UI/CLI/API/demo), integration between shipped modules, docs, regressions. Missing product surfaces (no UI, no demo path) are valid HIGH findings — the evidence is the audit trail, not a repro.
   - Check the KB for already-filed findings (don't re-file; update instead). Check `retros/` for what previous iterations actually landed.
   - File **3–7 issue candidates maximum** as KB `plan` notes tagged `qa-finding` (severity, evidence, user impact, smallest remediation, acceptance criteria, and an explicit *Critic selection argument*). Do **not** fix anything; do not propose new features — that's Scholar's lane.
   - If the product is stable and usable, say so in a single summary note and recommend the Critic pick a Scholar proposal — being fair to Scholar is part of the role.
   - Own branch, empty merge expected: findings live in the KB, working artifacts (repro scripts, audit transcripts) may stay in the workspace but are not the handoff.
4. **`_ROLE_BLOCKS["critic"]` update** — triage now reads **both** streams: open `proposal` notes *and* open `qa-finding` notes, judged with one rubric against the Definition of Done (user-visible value, product stability risk, leverage of already-shipped work, size, evidence quality). Selecting a fix over a feature is a first-class outcome. Non-selected candidates of *both* kinds get flipped `superseded` so the tried/rejected record stays real.
5. **`config/experts/product-qa/tactical.txt` tweak**: findings must land as KB notes when knowledge tools are available (job-local `output/findings/` demoted to working copy). The expert already inherits the full `knowledge` toolset from `defaults.yaml` — verified, no config change needed.

Phase 0 is verifiable today by starting a k3d test loop with `role_sequence: ["scholar", "product-qa", "critic", "developer"]`.

### Dispatch & capacity

A parallel stage means N concurrent agents + N workspaces. Kubernetes provisions agent pods and workspace PVCs on demand — no change. The 30 s post-job agent cooldown may stagger stage-job pickup by one dispatch tick; harmless (the barrier doesn't care about start skew). Compose's static `WORKSPACE_HOSTS` pool and VM-tier loops (`workspace_backend: vm`) need capacity ≥ stage width — a doc'd operational constraint, not code.

### Cockpit

Minimal: the loop status payload (`GET /api/projects/{id}/loop`) adds `current_stage_jobs`; the Loop tab renders one job chip per in-flight stage job instead of the single current-job link, plus a jobs-per-cycle hint next to the iterations budget. No new controls — `role_sequence` with a nested array is accepted through the existing start request (advanced/JSON path first; a stage-builder UI is out of scope).

## Implementation phases & acceptance criteria

**Phase 0 — product-qa loop-wiring (sequential; no schema change)** — IMPLEMENTED + unit-tested; k3d smoke **passed** (full `scholar → product-qa → critic → developer → done` rotation, Mode-A guard confirmed).
- [x] `LOOP_ANALYSIS_ROLES` includes `product-qa`; unit test asserts `is_loop_execution_role("product-qa") is False`.
- [x] Role block + task registered; kickoff snapshot test (mirrors existing role-block tests).
- [x] Critic block names both note streams and the build-vs-fix choice.
- [x] `tactical.txt` directs findings to KB notes tagged `qa-finding`.
- [x] k3d (gemma smoke `670130c6`): scholar→product-qa→critic all advanced correctly (analysis roles merged empty, **no F29**). Product-qa initially wedged at `pending_review` (Mode-A interaction, since fixed — see [Phase-0 smoke findings](#phase-0-smoke-findings)); after the fix the sweeper self-healed the advance. **NB:** gemma under-uses `kb_write`, so note-*quality* (`qa-finding` notes, Critic verdict references) is the homelab/minimax job's to prove (Phase 3) — k3d proves plumbing only.

**Phase 1 — schema + barrier advance** — COMPLETE 2026-07-07, unit-tested (70 loop tests) + barrier SQL validated against real Postgres + single-role advance path live-verified on k3d. Uncommitted.
- [x] Migration `0048_loop_parallel_stages.sql` adds `current_stage_jobs` (default `[]`, backfill-free) + `CHECK jsonb_typeof = 'array'`; `schema_current.sql` regenerated.
- [x] `claim_project_loop_stage_barrier()` — **immutable-membership / drain-when-all-terminal** contract (deviation from the incremental-shrink sketch; see [Advance algorithm](#advance-algorithm-barrier)). Real-Postgres drill: earlier finisher → no-op, last-out → single drain, re-fire after drain → no-op.
- [x] `_advance_loop_parallel_member`: per-job merge/retro always runs (shared `_merge_and_retro_loop_job`); only the last finisher rotates (shared `_rotate_loop_to_next_stage`); stop conditions at barrier only; failure only when **all** members failed.
- [x] `_spawn_loop_stage` spawns N jobs with shared `loop_iteration` + stamped `loop_seq_index`/`loop_remaining`; single-role path stays `current_job_id`, fan-out sets `current_stage_jobs`. Grammar (`normalize_stage` / `validate_role_sequence`, analysis-only fan-out) enforced at the start endpoint.
- [x] Sweeper: `_sweep_parallel_stage` backstop; `heal_project_loop_pointer`/`heal_project_loop_stage` guard on `current_stage_jobs = '[]'`; stamp-preferring `_derive_loop_counters`; `get_newest_loop_stage`. Tear-drill unit tests (Tear A repoint, Tear B restore) for both widths.
- [x] disable `memory_assembler` for fan-out members via `config_override` — done via the scalar `auxiliary.tasks.assemble_memories.enabled = false` gate (not the sketched writers list-replace; cleaner, see [KB safety](#kb-safety)), 4 unit tests. Single-role stages unaffected.

**Phase 2 — API + cockpit**
- [ ] Start endpoint accepts nested arrays; rejects execution roles in parallel stages (422) and duplicate roles within a stage.
- [ ] Loop status exposes `current_stage_jobs`; Loop tab renders stage chips; vitest for the component mapping.

**Phase 3 — e2e + rollout**
- [ ] k3d: loop with `[["scholar","product-qa"],"critic","developer"]` runs ≥2 full cycles; both stage jobs run concurrently (overlapping timestamps); Critic reads both streams post-barrier.
- [ ] Kill drill: delete the orchestrator pod mid-stage → sweeper recovers, no double-spawn (age-gate respected), no lost rotation.
- [ ] Flip the Better Resavio loop to the parallel sequence; first triage where the Critic explicitly weighs a `qa-finding` against a `proposal` is the acceptance demo.

## Open questions

1. **Budget display vs. semantics** — iteration=stage is decided above for invariant-preservation, but should `remaining_iterations` in the cockpit show "(≈N jobs)" to prevent misreading? (Cheap; recommend yes.)
2. **QA depth cadence** — every cycle a full product audit may be repetitive once findings stabilize. Later: alternate light/deep QA missions via the kickoff (no schema impact), or let the Critic's verdict steer next cycle's QA charter.
3. **Stage width > 2** — nothing in the design caps width (e.g. adding a `security-auditor` later), but rate limits (concurrent tokens/RPM against the provider) and workspace capacity do. Cap validation at, say, 4?
4. **Critic prompt-level filtering** — should the Critic's KB read explicitly query both tags, or is recency-boosted hybrid search enough to surface both streams? Phase 0's k3d run answers this empirically before Phase 1 lands.

## Risks

- **The advance path is the loop's most scarred code.** Mitigated by additivity: the single-job path is byte-identical, the parallel path reuses the same claim idempotency pattern, and the heal keeps its age gate. The tear-drill tests in Phase 1 are the gate.
- **Counter drift** between `total_jobs_run` (jobs) and iteration (stages) — the sweeper derivation must be updated *in the same commit* as the spawn change, or a heal after a parallel stage reconstructs garbage counters.
- **Two analysis jobs of overlapping scope** — Scholar may "discover" what QA files as a gap (e.g. both flag the missing UI). Acceptable: the Critic dedupes at triage, and the QA role block's "check already-filed findings" plus Scholar's tried/rejected check bound the redundancy.
- **Assembler gap** — with `memory_assembler` off for stage jobs, TTL curation happens once per cycle (Critic's job) instead of once per job. That's the *intended* single-writer-per-turn design, but if cycles get long the stale-queue can grow; watch `knowledge_index` TTL lag on the first real runs.

## Related

- [`project_self_improvement_loop.md`](project_self_improvement_loop.md) — parent design: advance hook, `project_loops` schema, bare-job model, role blocks.
- [`loop_parallel_execution.md`](loop_parallel_execution.md) — the concept paper this narrows; this feature is its Option C, barriered, with heterogeneous producers.
- [`loop_repo_compounding_v2.md`](loop_repo_compounding_v2.md) — merge-status truth + F29 empty-merge signal; why `LOOP_ANALYSIS_ROLES` membership matters.
- [`../issues/loop_advance_nonatomic_wedges_loop.md`](../issues/loop_advance_nonatomic_wedges_loop.md) — the torn-advance incident + age-gated heal this design must not regress.
- `config/experts/product-qa/` — the expert this wires in (uncommitted).
