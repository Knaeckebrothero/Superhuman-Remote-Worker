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

**Proposed — awaiting alignment on acceptance criteria.** Depends on the new `product-qa` expert (`config/experts/product-qa/`, uncommitted) which is loader-validated but not yet loop-wired. Phase 0 (loop-wiring the expert, sequential) is independently shippable and delivers the functional goal on its own; Phases 1–3 add the concurrent stage.

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
| Convergence races — concurrent `AssembleKnowledgeTask` supersede/TTL mutations | Parallel-stage jobs run with the `memory_assembler` writer **disabled** via `config_override`; the single curation pass happens in the Critic's job immediately after (see [KB safety](#kb-safety)). |
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

1. **Per-job completion work first, unchanged and per-hook**: squash-merge the job's branch (expected `empty` for analysis roles), write the retro, record `merge_status`. Each stage job's own completion hook does its own merge/retro — so when the barrier releases, all sibling contributions are already landed.
2. **Atomic barrier claim** — one conditional UPDATE, the parallel analogue of `claim_project_loop_advance`:

```sql
UPDATE project_loops
SET current_stage_jobs = current_stage_jobs - $2::text, updated_at = now()
WHERE id = $1 AND current_stage_jobs ? $2::text AND status = 'running'
RETURNING jsonb_array_length(current_stage_jobs) AS remaining;
```

   The `?` membership guard makes each job's claim exactly-once (a re-delivered completion or a racing sweeper matches no row and backs off — same idempotency contract as today). `RETURNING remaining` tells the caller whether it was the **last finisher**.

3. `remaining > 0` → stage still in flight; record and return. No budget/rotation work.
4. `remaining == 0` → **last finisher rotates**: evaluate stop conditions, decrement budget, `seq_index = (seq_index + 1) % len(role_sequence)`, spawn the next stage (1 job → `current_job_id`; N jobs → `current_stage_jobs`), write back counters. Single writer — no concurrent-decrement races by construction.

The KB cycle-TTL decrement (fires on `next_index == 0`) is unchanged — it's already keyed to the sequence wrap, which is now a stage wrap.

### Counter semantics

**An iteration is a stage, not a job.** Both stage jobs are spawned together with the same `context.loop_iteration = N`; the cockpit shows "iter 29 · SCHOLAR ∥ iter 29 · PRODUCT-QA". Rationale: this preserves the sweeper's heal-derivation invariants (`seq_index = (N-1) % len(role_sequence)`, `remaining = max_iterations - (N-1)`) with `role_sequence` entries = stages, and keeps "one more full cycle = 3 iterations" arithmetic stable when a stage widens.

Consequences, stated honestly:

- `remaining_iterations` buys **stages**; a parallel stage burns 2 jobs (≈2× tokens) per iteration tick. The cockpit loop card should surface jobs-per-cycle so the budget isn't misread. (On the MiniMax token plan this is a non-issue; on metered keys it matters.)
- `total_jobs_run` keeps counting **jobs** (its name is its contract; retros and audit math depend on it): `total_jobs_run += len(stage)` at spawn. The iteration label therefore derives from a new `stage_count`-style counter — or simplest, from `seq_index` wraps — rather than from `total_jobs_run`. The sweeper's `_derive_loop_counters` is updated to the stage-aware derivation (it currently assumes iteration ≡ job — see [Sweeper](#sweeper--heal-stage-awareness)).

**Failure semantics**: a stage counts toward `consecutive_failures` only when **all** its jobs failed (no forward signal produced). Partial failure — Scholar dies, Product-QA lands findings — resets the counter to 0, logs loudly, and the retro records the dead sibling; the Critic can still triage the surviving stream. `last_error` carries the failed sibling's error either way.

**Stop conditions** are evaluated only at barrier release (stages are atomic: never stop with a sibling still running; never spawn a partial stage). If `remaining_iterations == 1` and the next stage is 2-wide, the full stage is spawned — budget is a stage budget, there is no mid-stage stop.

### Sweeper + heal stage-awareness

`project_loop_sweeper.py` gets two extensions, both mirroring existing logic:

1. **Missed-completion backstop**: for a running loop with non-empty `current_stage_jobs`, check each member; any terminal member → re-run `advance_fn(job, {}, [])`. Idempotent via the `?` claim guard, exactly like the `current_job_id` path.
2. **Wedge discrimination**: today `current_job_id IS NULL ∧ running ∧ old` = torn advance. A parallel stage in flight is *also* `current_job_id IS NULL` — so `heal_project_loop_pointer` gains the guard `AND current_stage_jobs = '[]'::jsonb`, and the Python-side pre-check skips loops with a non-empty stage. A **parallel** torn advance (claim landed, stage spawn or write-back lost) presents as *both pointers empty + age*; heal re-points `current_stage_jobs` at all non-terminal jobs carrying the newest `loop_iteration` stamp (terminal ones flow through path 1 next tick). Partial-spawn tears (some stage jobs created, others not) heal to **what exists** with a loud log — no speculative re-spawning of missing siblings.

The age-gate discipline (`PROJECT_LOOP_HEAL_GRACE_SECONDS`, DB-clock guard) applies unchanged — this feature must not reopen the double-spawn incident.

### KB safety

Same-stage roles are **additive-only** KB writers — Scholar writes `plan` notes tagged `proposal`, Product-QA writes `plan` notes tagged `qa-finding`. Neither supersedes, flips status, nor curates. Cross-contamination via recency-boosted search (Scholar surfacing QA's in-flight notes or vice versa) is harmless-to-useful; the *dangerous* reader — the Critic, who supersedes losers — runs strictly post-barrier.

The one real race is the per-job **`memory_assembler`** aux writer (TTL curation / supersede re-verification, `config/defaults.yaml` `memory.pipeline.writers`), which was designed single-writer-per-turn ([kb_convergence_ttl_reverification.md](kb_convergence_ttl_reverification.md)). Parallel-stage jobs get it removed from their writer pipeline via the loop's existing `config_override` injection in `_spawn_loop_job`; the Critic's job — immediately after the barrier — runs the cycle's single curation pass. (Exact override key pinned at implementation; it's the `memory.pipeline.writers` list.)

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

**Phase 0 — product-qa loop-wiring (sequential; no schema change)**
- [ ] `LOOP_ANALYSIS_ROLES` includes `product-qa`; unit test asserts `is_loop_execution_role("product-qa") is False`.
- [ ] Role block + task registered; kickoff snapshot test (mirrors existing role-block tests).
- [ ] Critic block names both note streams and the build-vs-fix choice.
- [ ] `tactical.txt` directs findings to KB notes tagged `qa-finding`.
- [ ] k3d: loop with `["scholar","product-qa","critic","developer"]` completes one full cycle; product-qa iteration merges `empty` **without** an F29 alarm; `qa-finding` notes visible in the KB; Critic's verdict note references at least one of them.

**Phase 1 — schema + barrier advance**
- [ ] Migration adds `current_stage_jobs` (default `[]`, backfill-free).
- [ ] `claim_project_loop_stage_job()` with the conditional-UPDATE-RETURNING contract; unit tests: double-claim is a no-op, last-out detection correct under concurrent claims.
- [ ] `_advance_project_loop` parallel path: per-job merge/retro always runs; only the last finisher rotates; stop conditions evaluated at barrier only; partial-failure semantics as specced.
- [ ] `_spawn_loop_stage` spawns N jobs with shared `loop_iteration`, disables `memory_assembler` for stage jobs via `config_override`.
- [ ] Sweeper: terminal-stage-member backstop; heal guards on `current_stage_jobs = '[]'`; stage-aware `_derive_loop_counters`. Tear-drill unit tests (claim-then-crash, spawn-then-crash) for both stage widths.

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
