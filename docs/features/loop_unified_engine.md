---
tags:
  - feature
  - plan
  - orchestration
  - projects
  - self-improvement
  - concurrency
aliases:
  - unified loop engine
  - loop pipeline mode
  - handover briefs
  - loop cycles budget
related:
  - "[[project_self_improvement_loop]]"
  - "[[loop_parallel_stages]]"
  - "[[loop_parallel_execution]]"
  - "[[loop_campaign_scheduling]]"
  - "[[loop_repo_compounding_v2]]"
  - "[[loop_review]]"
---

# Loop Unified Engine — one advance path, handover briefs, optional pipelining

> Today the project loop has **three scheduling shapes on two advance paths**: legacy single-job rotation (`current_job_id` rotate), the parallel-stages barrier (`current_stage_jobs`), and the campaign/planner machinery on top. This feature collapses them into **one engine with two modes**. Every spawn goes through a generalized stage barrier (a sequential step is a width-1 stage; a barrier over one job is trivially "last out drains"), the legacy rotate path is **deleted**, and roles hand off through explicit, user-configurable **handover briefs** instead of KB similarity luck. Budgets are counted in **cycles**, not jobs, everywhere. An `overlap` toggle turns the stage list into a lockstep software pipeline (fill ramp: S → S+C → S+C+D) — [[loop_parallel_execution]]'s Option A, buildable now because the handover brief removes the generation-leakage problem that made Option A the worst trade when handoff flowed through the KB.

## Status

**PROPOSED (2026-07-19). Nothing implemented.** Design agreed with the owner; refined same day by a five-agent research round — three codebase audits (advance-path unification blast radius, budget/counter consumers, completion-gate hooks) and two external surveys (agent-framework handoff patterns; merge-queue/CI/workflow-engine + handoff-literature practice). Audit findings are folded in below as **[A1]**(advance), **[A2]**(budget), **[A3]**(hooks); external evidence as **[R-fw]**(frameworks) / **[R-eng]**(engineering) with sources in [Prior art & evidence](#prior-art--evidence).

Decisions locked:

- **Unify to two modes** — `standard` (subsumes rotation *and* parallel stages) and `campaign` (planner). The legacy single-job advance path is deleted, not kept alongside. One engine, one wedge signature, one heal.
- **Overlap is a per-loop toggle, default off.** Migrated loops keep sequential semantics and cost; the pipeline ramp is opt-in.
- **Parallel execution members allowed** (e.g. 3 developers in one stage), serialized squash-merges at the barrier, conflicts loud (`merge-failed`), branch kept.
- **Cycle budgets are backend-wide** — `max_cycles`/`remaining_cycles` replace `max_iterations`/`remaining_iterations` in both modes, with a per-mode conversion migration.

Implements, in user-configurable form, the control-plane assessment's top P0 (typed selection→mission→outcome handoff + refuse-to-advance on missing output — `docs/issues/loop_control_plane_assessment.md`) and closes findings **F15** (budget unit), **F23** (handoff by similarity luck), and **F32** (outcome-blind advance) for the brief-carried path; see [[loop_review]].

**Phase 1 implemented on develop (2026-07-19)** — unified engine live in code: every turn barrier-tracked (width 1 included; `current_job_id` = display mirror), campaign advance threaded through the barrier, legacy rotate path + `claim_project_loop_advance` + pointer heal deleted, modes renamed `standard`/`campaign` (migration 0063). Phases 2–7 not started; their schema lands in later migrations (0064+), not 0063 as originally sketched.

**Phase 1 deployed to dev and live-validated (2026-07-21 → 2026-07-25).** Migration 0063 applied cleanly on dev (CHECK swap, defaults, in-place rename of pre-migration rows). The planned k3d smoke was superseded by live validation on real dev loops, all against the deployed images:

- **Standard rotation through failures** — a 4-turn width-1 loop advanced scholar→critic→scholar→critic through 3 member failures and 1 success (`consecutive_failures` reset on success), then budget-stopped with both pointer columns cleared.
- **Consecutive-failure stop** — a separate standard loop stopped itself with `stop_reason=failures` after 3 consecutive VM-provisioning member failures; the guardrail fired exactly at `max_consecutive_failures`.
- **Torn-advance heal (tear drill, run twice)** — manually nulling both pointer columns and backdating `updated_at` 20 min on an in-flight loop was healed by the sweeper within one 60 s tick: membership + display mirror restored to the in-flight member, counters re-derived unchanged, **no advance and no duplicate spawn** (the member was still running), with the expected `healed torn advance` warning on the leader replica.
- **Campaign chain through the barrier** — a live campaign loop ran 14 barrier turns: checkpoint critic filed a plan, members (developer → product-qa) chained through the barrier path, `campaign.cursor` advanced 0→2, a failed member was tolerated per design (consecutive `member_failures` reset by the next success, stage counted done), and the review critic spawned immediately after the chain. This confirms the **[A1]** campaign-threading risk closed in production.

**Open observation from live validation (pre-existing, agent-side — not the engine):** the validated campaign sat in `status="review"` with an empty `campaign_history` across three subsequent critic checkpoints — no critic ever filed a disposition, while standard rotation continued around the parked campaign. Root cause was structural, not (only) prompting: the disposition could only ride a full new plan (non-empty stages + initiative), so a "ship/kill, open nothing new" verdict had no legal filing — the critic wrote it to a KB note the engine cannot read. **Fixed 2026-07-25:** dispose-only filing (`{"disposition": {outcome ship|kill, notes}}` with no stages) accepted end-to-end (validator, advance path, intake, `loop_plan` agent tool), the silent no-plan fallback now emits a `loop_campaign_review_skipped` event + warning while a campaign awaits review, and the checkpoint prompt teaches the dispose-only shape ("a KB note is NOT a disposition"). See `docs/features/loop_campaign_scheduling.md`. An auto-kill cap after N skipped reviews remains an open policy decision.

## Motivation

Three independent pressures point at the same redesign:

1. **Duplicate control-plane logic.** `_advance_project_loop` (orchestrator/main.py:12679) branches into the scarred single-job rotate (source of the torn-advance and double-spawn incidents, `docs/issues/loop_advance_nonatomic_wedges_loop.md`) and the newer barrier path from [[loop_parallel_stages]] (tear-drilled, real-Postgres-validated, live-verified). Campaign scheduling adds bespoke member-spawn/succession on top. Every new capability currently needs three implementations or silently supports one mode.
2. **Handoff is the loop's weakest link.** The Developer is told "implement the Critic's chosen action" and must rediscover what that was from a noisy, similarity-ranked KB (DoD reached 0 of 3 developers across 788 turns — F23; a 9.16M-token developer iteration ran on a stale mission — control-plane assessment). The KB should be memory, not the control channel. External evidence is unanimous that explicit briefs beat re-derivation: Anthropic's four-field subagent task contract, CrewAI's verbatim context injection, LangChain's *measured* "telephone game" loss through supervisor re-narration **[R-fw]**.
3. **Users can't shape the loop.** Stage composition is fixed role vocab, no duplicates, no per-stage contracts; budget is in jobs (F15) which nobody thinks in; parallelism is limited to a hardcoded analysis fan-out.

## Design

### Two modes, one engine

`project_loops.scheduling` renames: `rotation → standard`, `planner → campaign` (migration + API/cockpit rename; note `scheduling` is currently start-time-only and absent from the update allowlist, postgres.py:11426 **[A1]**).

The **engine** is the only advance path and is mode-blind:

1. **Barrier drain** — every in-flight turn is a set of jobs in `current_stage_jobs` (width 1 included). Completion hooks run the existing `claim_project_loop_stage_barrier` (orchestrator/database/postgres.py:11589); exactly one caller drains, exactly one rotates. `current_job_id` is demoted to display-only (backfilled with the job id when a turn has width 1, so cockpit links and MCP formatters keep working).
2. **Per-job completion work** — squash-merge `job/<id>` (serialized for execution members, in instance order — see [Merge policy](#parallel-execution-members--merge-policy)), write retro, record `merge_status` (unchanged from [[loop_repo_compounding_v2]]).
3. **Harvest** — assemble the turn's handover briefs (persisted earlier by the `loop_handover` tool) into injection payloads for the successors.
4. **Charge & stop** — `charge_cycle()` at the single billing point (see [Cycles](#cycles-backend-wide)), then stop conditions (budget, `max_consecutive_failures`, `run_until`, user stop). Stop-writes must clear **both** pointer columns — today the single-role stop path clears only `current_job_id` and leaves a stale `current_stage_jobs` (main.py:12759 vs 12852 **[A1]**).
5. **Schedule & spawn** — ask the mode's scheduler "which stages are due next turn"; spawn with brief injection; stamp counters in one `_writeback_loop_stage` row-write.

The **scheduler** is the only mode-specific code:

- `standard`, `overlap: false` — the next single stage in the list (wraps to stage 0 = new cycle). Today's rotation semantics, including width-N fan-out stages.
- `standard`, `overlap: true` — all stages with `stage_index ≤ turn` (the fill ramp); post-fill, every stage every turn, each consuming the previous turn's briefs.
- `campaign` — the accepted plan's stage list (see [Campaign on the engine](#campaign-on-the-engine)).

**Unification blast radius (from the audit — each is an explicit Phase-1 work item) [A1]:**

- `_writeback_loop_stage` (main.py:12065) currently writes width-1 into `current_job_id` with an empty stage set — must write width-1 into `current_stage_jobs` or the barrier never tracks the job and the loop wedges on the first unified turn.
- The sweeper heal (`project_loop_sweeper.py:352`) heals torn width-1 stages via `heal_project_loop_pointer` — under unification that "recovers" the loop into a state the barrier can never fire from. `heal_project_loop_pointer` folds into `heal_project_loop_stage`; both DB guards that assume "width-1 uses the pointer" (postgres.py:11577, 11723) change with it.
- **The campaign step is wired only into the legacy path**: `_advance_loop_parallel_member` deliberately passes no `completed_job`/`completed_ctx` into `_rotate_loop_to_next_stage`, so campaign advance is a structural no-op on the barrier path (main.py:12865 vs 12785). The engine must thread the completed member's job + context stamps (`loop_campaign_id/_index`, `loop_role`, `loop_seq_index`, `loop_plan`) through the barrier — required for campaign compatibility in Phase 1 and reused by the brief harvest in Phase 3.
- `file_loop_plan`'s in-flight gate (`current_job_id != job.id` → 409, main.py:13033) re-expresses as membership in `current_stage_jobs`, else no planner loop can ever file a plan.
- `_resume_project_loop`'s `elif cur:` branch (main.py:12900) goes dead; the stage-set branch already handles members.
- Campaign invariants to preserve verbatim: campaign mutation + stage pointer written in **one** `_writeback_loop_stage` call; the `plan_job_id` idempotency guard and persist-campaign-before-spawn ordering; `member_failures` stays separate from the loop's `consecutive_failures`; `seq_index = execution_slot` stamping for members.
- Display-only degradations to fix in the same pass: MCP `_format_loop`/`_explain_loop` (src/tools/orchestrator/workflows.py:320/427) and the cockpit's `@else if current_job_id` branch (project-loop.component.ts:246) read the pointer.

Deleted: the single-job rotate entry path, `claim_project_loop_advance`, the sweeper's pointer-vs-stage heal split, and (Phase 5) campaign's bespoke member-spawn. The tear-recovery age gate (`PROJECT_LOOP_HEAL_GRACE_SECONDS`, DB-clock guard) applies unchanged — the engine must not reopen the double-spawn incident.

### Stage grammar

For `standard` loops, `role_sequence` entries become **stage objects** (legacy strings and arrays normalize at read time via an extended `normalize_stage`):

```jsonc
[
  {"experts": ["scholar"],
   "handover": "finish with a list of the research reports/suggestions you created"},
  {"experts": ["critic"],
   "handover": "compare candidates against the backlog; state exactly what each developer should implement"},
  {"experts": ["developer", "developer", "developer"],
   "handover": "report how implementation went, errors hit, and what should happen next"}
]
```

- **Any DB expert** is valid in any stage (resolution via the existing role→expert path in `create_loop_job`). `LOOP_ANALYSIS_ROLES` stays only for merge expectations (analysis merges empty) — it no longer gates *placement*.
- **Duplicates allowed**, including execution roles. Instance identity = member index within the stage, stamped into job context (`loop_member_index`) and retro filenames (`retros/NNN-developer-2-<jobid8>.md`). Duplicated instances are the agent-world equivalent of a CI **matrix axis** on one stage **[R-eng]**.
- **Validation** (start endpoint): every stage non-empty; total turn width ≤ `PROJECT_LOOP_MAX_TURN_WIDTH` (default 8, env-tunable) — concurrent token spend and workspace capacity are real limits.
- `handover` is optional; per-role defaults ship for scholar/critic/developer/product-qa; unknown experts get a generic default.

### Handover briefs — the handoff channel

**Format: fixed skeleton, free-text sections** — not free prose, not rigid JSON. The platform owns the envelope and validates it on write; the user's per-stage `handover` field customizes the NEXT section's contract; the agent fills the text. This is the A2A envelope-vs-content split, CrewAI's `expected_output`, and Anthropic's finding that contract quality is causal ("without detailed task descriptions, agents duplicate work, leave gaps") **[R-fw]**; the section set follows the I-PASS handoff structure, the best-evidenced handoff format in the safety literature (23% error reduction in a 9-hospital trial) **[R-eng]**:

```markdown
STATUS: green | watch | blocked          # one enum, first line (I-PASS severity)
BASELINE: <main HEAD sha at write time>  # stamped by the tool, not the agent
## Summary        — what happened this cycle and WHY (not just what)
## Decisions      — decisions made, with rationale
## Tried & failed — dead ends, so successors don't repeat them
## Artifacts      — repo references as path @ sha, never re-narrated content
## Next           — ordered missions for the consuming stage; one per member
                    when the consumer is duplicated ("developer 2: …")
```

- **Pointers over payloads** **[R-fw]**: briefs reference `docs/plan.md @ abc123`, `tests/test_x.py::test_y` — the consumer re-reads ground truth from the repo. This is Anthropic's filesystem-artifacts pattern ("minimize the game of telephone") and Manus's restorable-compression rule.
- **Size cap ~2k tokens (8,000 chars), enforced at intake** — evidence puts effective brief size at 1,000–2,000 tokens, and SWE-agent's ablation shows verbatim-everything *underperforms* concise-recent **[R-fw]**. Oversize → actionable rejection, agent shortens and re-files. (The earlier 16KB draft cap is dead.)
- **No accumulation**: a consumer receives the predecessor stage's briefs from the previous turn — never the full chain of prior cycles. Accumulating carryover is AutoGen's documented bloat anti-pattern; the ring's cross-cycle memory is the *latest* developer report only (Reflexion bounds its lesson buffer to 1–3 entries for the same reason) **[R-fw]**.
- **Verbatim injection is an invariant** **[R-fw]**: the orchestrator never paraphrases a brief (LangChain measured the "telephone game" cost and shipped `forward_message` to stop it; AG2's `reflection_with_llm` is the canonical anti-pattern). The only entity that compresses stage k's knowledge is stage k itself, at write time, with full context in view.
- **Receiver synthesis** **[R-eng]**: every loop kickoff instructs the agent to *restate its received mission in one short paragraph as its first act* (logged in the transcript). I-PASS's closed-loop check, nearly free, and makes misunderstood handoffs visible in the audit trail.
- **Ring topology**: stage k consumes stage k−1's briefs; stage 0 consumes the **last** stage's briefs (the developer report feeding the next scholar). Same-cycle when sequential, previous-turn when overlapped — identical code path.
- **Failure marker**: a failed member contributes an explicit `FAILED: <role-instance> produced no handover (<error>)` line — a visible pipeline *bubble*, never silent staleness (F32 for the brief path). Tolerated failure stays distinct from success in what the consumer sees — GH Actions' continue-on-error-reports-success wart is explicitly not copied **[R-eng]**.
- **Audit trail**: briefs are appended to the job's `retros/NNN-*.md` on `main` — one trail, no new repo-pollution surface.
- The KB remains available to all roles as memory/search; it stops being the control channel.

**Mechanics (from the hooks audit) [A3]:**

- **Tool**: `loop_handover` in the existing `loop` category (`src/tools/loop/`), cloning the `loop_plan` shape — POST to a new `POST /api/jobs/{job_id}/loop-handover` endpoint in orchestrator/main.py (loop endpoints live inline on `@app`, not in routers), `X-Internal-Key` auth, domain validator raising `ValueError → 400` whose detail round-trips verbatim to the agent as "Handover REJECTED … fix and call loop_handover again — nothing was stored."
- **Injection broadening**: `create_loop_job` currently injects `tools.loop` only for planner checkpoint critics (project_loops.py:769) — becomes unconditional for loop jobs (`loop_handover` for everyone; `loop_plan` additionally for checkpoint critics).
- **The gate is orchestrator-side — an agent-side check alone is unenforceable**: a loop job that stops *without* calling `job_complete` is deliberately mapped to `completed` so the loop advances (services/completion.py:910-922). Authoritative check in `complete_job` (main.py) after `determine_job_status`, before the status write and the §5d advance: `new_status == "completed"` ∧ loop job ∧ no stored brief → **reject-once** using the existing memory-retry pause/re-dispatch pattern (increment `context.handover_retry_count`, pause + re-dispatch with an explicit instruction), second miss → `failed` with `error_message="no handover brief produced"`. An agent-side check in `job_complete` (mirroring the existing deliverable-gate at src/tools/core/job.py:204) stays as first-line UX.
- **Freeze/pause immunity confirmed**: auto-redispatch freezes map to `paused` (non-terminal) and never traverse the gate; `waiting_for_reply` jobs are rejected by `complete_job`'s status guard before the gate could see them. No carve-outs needed.
- **Injection plumbing exists end-to-end**: the brief text rides the existing `extra_context` parameter (`create_loop_job` ← `_spawn_loop_job` ← `_spawn_loop_stage`/`_spawn_campaign_member`) into `build_loop_kickoff` as a `PREDECESSOR HANDOVER (read this first)` part placed after the role blocks; the kickoff lands verbatim and untruncated in `task_brief.md` (the trimmer only touches ToolMessages).
- **Storage**: dedicated `loop_handovers` table (not `context` JSONB like `loop_plan`) — the ring lookup ("stage k−1's briefs, turn N−1"), the cockpit briefs viewer, and cross-cycle queries all want indexed rows, not per-job context spelunking.

### Parallel execution members & merge policy

At barrier release, execution members' branches squash-merge onto `main` **serially in instance order** — a merge train of window 1 **[R-eng]**. Policy, aligned with how merge queues handle mid-train failure (eject-and-continue; nobody aborts the train):

- A member whose merge conflicts is **ejected loudly**: `merge_status=merge-failed`, branch kept, work preserved. Merging continues with the remaining members against the updated `main` — one bad branch never discards its siblings' work.
- The ejection is recorded in the stage's outgoing brief; the next cycle's corresponding stage receives it as an explicit **rebase/repair task** with a bounded requeue count (`context.merge_requeue_count`, cap 2) — after the cap, the branch stays as a permanently recorded artifact, not silent loss **[R-eng]**.
- Branch names stay deterministic (`job/<id>`) and merges idempotent (already-merged check before acting) so heal-time re-merges are safe no-ops **[R-eng]**.
- The upstream stage's default handover instructions tell a critic feeding N developers to assign **disjoint missions/scopes** — parallel writers with overlapping scope is the single best-documented multi-agent failure mode (Cognition's "writes stay single-threaded" revision) **[R-fw]**. Enforced file-scope validation was considered and deferred.
- Deferred, explicitly: speculative rebase-and-revalidate before each merge (running the repo's verification per member — merge-queue style) — worth revisiting once loop repos carry a cheap test gate **[R-eng]**.

Failure semantics (engine-wide): `consecutive_failures` increments only when **all** jobs of a turn fail; partial failure resets it to 0, logs, and records the dead siblings in retro + briefs. Infra-type failures keep riding the existing freeze/auto-redispatch machinery (the Argo-style errored-vs-failed split already exists in SRW as freeze-types vs job failure **[R-eng]**). Stop conditions are evaluated only at barrier release — stages are atomic.

### Cycles, backend-wide

- Columns rename: `max_iterations → max_cycles`, `remaining_iterations → remaining_cycles` (migration **`0070`** — 0063 went to Phase 1, 0064–0069 to unrelated work; `project_loop_has_budget` CHECK re-created; `schema_current.sql` regenerated). Full consumer blast-radius is mapped in the budget audit — DB layer allowlist (`_PROJECT_LOOP_UPDATABLE_FIELDS`), heal writers, spawn/writeback kwargs, `validate_loop_plan`, `_format_budget` UI strings, sweeper, MCP formatters, cockpit models/labels, API request model **[A2]**. Scope guard: agent-side `max_iterations` (aux tool loops, subagent caps, loader config) is an unrelated concept — never sweep it.
- **One billing point.** *(Partly delivered by Phase 1: the decrement is now single — `_advance_loop_member`, main.py:13279 — and the stop check already sees the post-charge value.)* What remains is the *timing*: the charge fires per turn, upstream of the wrap index (`next_index`, main.py:13037, computed inside `_rotate_loop_to_next_stage`). The engine's `charge_cycle()` step must know the wrap before charging, so the wrap computation moves ahead of the charge/stop-evaluation pair **[A2]**.
- **Per-mode cycle semantics** (each mode charges 1 at its natural boundary):
  - `standard`, sequential: at sequence wrap (stage 0 due next) — exactly where the KB cycle-TTL decrement already fires; the two decrements unify into **one wrap hook** so loop cycles and KB TTLs tick in lockstep (they finally denote the same unit).
  - `standard`, overlap: once per turn (fill turns count; each steady-state turn retires ≈ one generation).
  - `campaign`: **once per campaign member advance** — numerically identical to today's per-member billing, so `validate_loop_plan`'s affordability arithmetic (`len(stages) ≤ remaining − reserve`) survives unchanged. This dodges the audit's nastiest trap: campaign members bypass the sequence wrap entirely, so naive decrement-at-wrap would make campaigns cost **zero** and run planner loops unbounded **[A2]**.
- **Per-mode conversion migration** **[A2]**: rotation loops divide by `jsonb_array_length(role_sequence)` (stage *entries*, not job count — a fan-out entry already billed once per barrier): `remaining_cycles = CEIL(remaining_iterations::numeric / GREATEST(len, 1))`, **same divisor for `max_cycles`** (converting only one desyncs the seed relationship `remaining = max`). Planner loops convert **identity** (unit relabel; members bill 1 each before and after). Guards: `WHERE remaining_iterations IS NOT NULL` (run_until-only loops stay NULL — the advance already no-ops on NULL), `GREATEST(len,1)` for degenerate rows, `remaining = 0` stays 0. Documented softening: a mid-cycle remainder rounds **up** to a full extra cycle.
- **Sweeper fallback**: `_derive_loop_counters`' legacy modulo formula (`max_iter − (iteration − 1)`, sweeper:246) silently mis-heals by a factor of the sequence length after conversion — becomes `max_cycles − ((iteration − 1) // len)` for stamp-less legacy jobs; the stamped path reads the new cycle stamps **[A2]**.
- **Naming collision — handle with care**: `knowledge_index.remaining_cycles` (pgvector) already exists; the KB TTL SQL inline at main.py:12610 and `knowledge_store.py` must **not** be touched by rename scripts. Post-change both columns tick at the same wrap hook, which is the intended convergence, not a bug **[A2]**.
- `total_jobs_run` and the `loop_iteration` context stamp stay **job-counted** — `get_newest_loop_stage` groups fan-out stages by shared `loop_iteration` and retro filenames key on it; re-basing them to cycles breaks stage grouping **[A2]**. Retro front-matter additionally records `cycle:` so agents aren't confused by the iteration/cycle vocabulary split.

### KB safety

All members of a multi-job turn run with `memory_assembler` disabled via the existing scalar override (`auxiliary.tasks.assemble_memories.enabled = false`, the [[loop_parallel_stages]] lever), **except exactly one designated member per turn** (deterministic: first member of the last due stage), which carries the cycle's single TTL-curation pass. Width-1 sequential turns are unaffected. Preserves single-writer-per-turn without needing a sequential slot.

### Campaign on the engine

Campaign **advance compatibility lands in Phase 1** (threading `completed_job` through the barrier — without it, unification silently kills campaign scheduling **[A1]**). The Phase-5 migration is then a collapse of the remaining bespoke machinery:

- Planner plans emit **stage objects** — campaign stages gain widths and `handover` fields for free; `validate_loop_plan` extends to the stage-object grammar.
- `_spawn_campaign_member` routes through the engine's schedule/spawn step; checkpoint critics become ordinary stages whose kickoff carries the PLANNER/DISPOSITION duty blocks.
- Plan intake, dispositions, caps, history, notifications are untouched.
- The checkpoint critic receives the final campaign stage's briefs — a better disposition input than today's KB re-derivation.

### Cockpit

- **Builder** (project-loop.component.ts): ordered stage list; per stage an expert picker (repeat to duplicate) + optional handover textarea; cycles input; overlap toggle with a **ramp preview** (which stages run in which turn, jobs per turn — so a cycle budget can't be misread as a job budget).
- **Running panel**: turn/cycle number, per-stage job chips (`role-N`), merge/brief status per member, and a **briefs viewer** backed by new `GET /api/projects/{id}/loop/handovers`.
- Rotation-era loops render through the same components after normalization; "iterations" language becomes "cycles" everywhere (component labels, `api.model.ts`, start-form fields **[A2]**).

## Data model & code surface

- **Migrations** (originally sketched as one `0063`; split per phase as the work landed). House style per `0035`/`0050`:
  - `0063` — **shipped (Phase 1)**: `scheduling` value rename + CHECK swap, width-1 barrier membership backfill, column comments.
  - `0070` — **Phase 2**: `max_cycles`/`remaining_cycles` rename + per-mode conversion; `project_loop_has_budget` CHECK re-created.
  - Phase 3 — `loop_handovers` table (id, loop_id FK, job_id, cycle, stage_index, member_index, role, status TEXT CHECK (green/watch/blocked), baseline_sha, content TEXT, created_at; index on (loop_id, cycle, stage_index)).
  - Phase 4 — `overlap BOOLEAN NOT NULL DEFAULT FALSE` on `project_loops`.
- `orchestrator/services/project_loops.py`: stage-object `normalize_stage`/`validate_role_sequence`; `validate_loop_handover` (envelope headings, size cap, path@sha spot-check); brief part in `build_loop_kickoff`; instance stamping + unconditional `tools.loop` injection in `create_loop_job`.
- `orchestrator/main.py`: `_advance_project_loop` becomes engine steps 1–5 with the [A1] work items; `POST /api/jobs/{job_id}/loop-handover`; completion gate in `complete_job`; scheduler functions per mode.
- `orchestrator/database/postgres.py`: barrier claim reused as-is; heal unification; `loop_handovers` CRUD; updatable-fields allowlist rename.
- `orchestrator/services/project_loop_sweeper.py`: single stage-shaped sweep/heal; converted fallback formula; cycle stamps.
- `src/tools/loop/handover.py` + metadata; agent-side first-line check in `job_complete`.
- Tests: the loop suite (70+), `tests/test_loop_merge.py`, `tests/test_loop_campaign_scheduling.py` (68), `tests/test_project_loop_sweeper.py` — the sweeper/campaign tests encode legacy-path invariants and need rewriting, not renaming **[A1][A2]**; new brief-gate, ring-injection, serialized-merge/eject, conversion-migration, and width-1 tear-drill tests.

## Implementation phases & acceptance criteria

Order note: the engine comes **before** cycles — decrement-at-wrap needs the wrap computed ahead of stop-evaluation, which is exactly the restructuring the engine does; building it into the two legacy paths first would implement it twice on code about to be deleted **[A2]**.

**Phase 1 — unified engine (behavior-preserving). ✅ DONE (migration 0063; deployed dev 2026-07-21, validated through 2026-07-25).** All spawns through the barrier path; the [A1] work-item list (writeback, heal, campaign threading, plan-filing gate, stop-writes, resume, display); legacy rotate deleted; mode renames. Billing stays per-advance. AC met, with one substitution: the k3d smokes were superseded by validation on live dev loops (standard rotation through failures, consecutive-failure stop, tear drill ×2, a 14-turn campaign chaining through the barrier) — see [Status](#status). Unplanned addendum shipped in the same arc: dispose-only campaign filing + `loop_campaign_review_skipped`, closing an accountability hole the validation surfaced.

**Phase 2 — cycles backend-wide. ← NEXT.** `charge_cycle()` at the engine; unified wrap hook (loop budget + KB TTL); per-mode conversion migration; sweeper fallback; renames through DB/API/MCP/cockpit. AC: conversion correct on live-shaped fixtures (rotation ÷ entries, planner identity, NULL/0/empty guards); budget stop fires on the post-charge value; campaign affordability arithmetic unchanged.

*Post-Phase-1 corrections to the Phase-2 brief (the audit predates unification):*

- **Migration number is `0070`**, not 0063 — Phase 1 consumed 0063, and 0064–0069 went to unrelated expert/automation work.
- **The decrement is no longer duplicated.** Phase 1 collapsed the two per-path decrements into exactly one (`_advance_loop_member`, main.py:13279), and the stop check already reads the post-charge value. The remaining Phase-2 work is therefore *charge-at-wrap*, not de-duplication: the charge still fires per **turn** and still precedes the wrap computation (`next_index`, main.py:13037, lives inside `_rotate_loop_to_next_stage`, downstream of the charge and of `_loop_stop_reason`).
- **The KB TTL hook is the wrap the loop budget must join** (main.py:13046, gated on `next_index == 0`). Note it sits *after* the campaign branch's early return, so campaign advances never tick it today — preserve that, or decide it deliberately.
- **`max_iterations` is an overloaded name.** Agent-side `max_iterations` (auxiliary tool loops `src/services/auxiliary.py`, light-runner/subagent caps `src/tools/delegation/`, `src/core/loader.py:1797`, `src/agent.py:560`) is an unrelated concept. A repo-wide rename sweep WILL break the agent — the rename is scoped to `project_loops` columns and their consumers only.

**Phase 3 — handover briefs.** Tool + endpoint + table + orchestrator gate + ring injection + retro mirror + receiver-synthesis kickoff line + per-stage instruction field. AC: gate drills (reject→re-dispatch→fail-loud; stop-without-job_complete path caught; waiting_for_reply untouched); envelope validation (headings, 8k cap, oversize round-trip); k3d smoke where a critic's brief verbatim reaches the developer kickoff and the developer report reaches the next scholar.

**Phase 4 — overlap scheduler.** Fill ramp + per-turn charge + cross-turn brief flow + failure bubbles. AC: scheduler unit tests (fill/steady/partial-failure); k3d smoke of `[S],[C],[D]` with `overlap:true` showing S / S+C / S+C+D turns and briefs crossing turns with correct BASELINE SHAs.

**Phase 5 — campaign on the engine.** Plan grammar → stage objects; member spawn through the engine; checkpoint-as-stage; briefs feed dispositions. AC: campaign suite green on the engine; k3d planner smoke reproducing the `loop_campaign_scheduling` green path end-to-end.

**Phase 6 — cockpit.** Stage builder, ramp preview, briefs viewer, cycles vocabulary. AC: component tests; k3d Playwright pass building a 3-stage loop with a duplicated developer and reading its briefs.

**Phase 7 — live validation.** Better Resavio in standard mode with briefs (sequential), then flip `overlap:true`. AC: one full overnight run where every handoff is brief-carried, every stage outcome visible in retros + briefs, and cost per cycle readable.

*Phases 3–7 are not started.* Each gets its own plan under `docs/superpowers/plans/` at the time it is picked up (Phase 1's is `2026-07-19-loop-unified-engine-phase1.md`). Phase 7 note: the Better Resavio dev loop is currently `failed` (`stop_reason=failures`, three consecutive VM-provisioning failures on 2026-07-24) — it needs the workspace/VM provisioning issues resolved before it can serve as the validation vehicle.

## Open questions

1. **Drain at budget end** (overlap mode): no mirror-image drain phase in v1 — the final scholar/critic tail is discarded (rides in KB/briefs for the next run). Revisit if tail waste bothers real runs.
2. **Cross-cycle no-progress detector**: successive near-identical briefs/diffs from the same stage signal a stuck ring (the OpenHands stuck-detector idea lifted to cycle granularity **[R-fw]**). Cheap heuristic worth a v2 slice; watch their documented false positive (legitimate long work looks stuck).
3. **Producer-identity blinding**: briefs are labeled by role+instance; for competing candidates feeding a critic, the author label risks judge bias (`docs/issues/loop_critic_producer_identity_bias.md`; Cognition recommends clean-context verifiers seeing diffs + criteria, not reasoning **[R-fw]**). Unresolved there, unchanged here.
4. **Verification-command merge gate**: merge-queue practice validates against predicted post-merge state; loop repos have no cheap test gate today. If they grow one, run it per member before merging (upgrades eject-and-continue to a real merge train) **[R-eng]**.
5. **Designated-curator choice**: first member of the last due stage is deterministic but arbitrary; if curation quality suffers, consider a barrier-time orchestrator-side pass.

## Risks

- **Phase 1 replaces the loop's most incident-prone code.** Mitigations: the surviving path is the tear-drilled, live-verified barrier; behavior-preserving phase gated by the full suite before any new semantics; heal age-gate retained verbatim; the [A1] blast-radius list is the review checklist.
- **Campaign silently breaking under unification** — the audit's top risk; addressed by pulling campaign threading into Phase 1 with its own smoke AC, not deferring it to Phase 5.
- **Cycles mis-billing campaigns** — addressed by per-mode charge semantics + identity conversion; the affordability tests are the guard.
- **Merge-conflict churn with duplicated developers** if briefs don't partition work — bounded by eject-and-continue, requeue caps, and kept branches; watch the first real 3-developer run.
- **Overlap multiplies concurrent spend** (tokens, workspaces, provider RPM) by turn width — default-off toggle, width cap, ramp preview.
- **Brief quality drift** (vague or bloated briefs re-creating the KB problem in a new place) — envelope validation, the 8k cap, receiver synthesis, and pointers-over-payloads are the defenses; Phase 7's run reviews real briefs against them.

## Prior art & evidence

**[R-fw] Agent-framework handoff patterns:** Anthropic multi-agent research system (four-field task contract; filesystem artifacts to "minimize the game of telephone"; sync fan-in) — anthropic.com/engineering/built-multi-agent-research-system; Cognition "Don't Build Multi-Agents" + 2026 follow-up ("writes stay single-threaded"; clean-context verifiers) — cognition.com/blog/dont-build-multi-agents, /multi-agents-working; LangChain multi-agent benchmark (measured supervisor-translation loss; `forward_message`) — langchain.com/blog/benchmarking-multi-agent-architectures; CrewAI verbatim task-context injection — docs.crewai.com/concepts/tasks; AutoGen carryover bloat (anti-pattern) + composable termination with recorded `stop_reason` — microsoft.github.io/autogen; A2A artifacts-not-messages, per-task terminal states, immutable terminal tasks — a2a-protocol.org; SWE-agent (collapsed-history ablation 18.0% vs 15.0%; "succeed quickly, fail slowly") — arXiv:2405.15793; OpenHands condenser + stuck detector — docs.openhands.dev; Reflexion bounded lesson buffer — arXiv:2303.11366; Manus restorable compression — manus.im/blog.

**[R-eng] Orchestration engineering:** GitHub merge queue / GitLab merge trains / Bors / Zuul (eject-and-continue, speculative prefix stacking, adaptive window) — docs.github.com, docs.gitlab.com, zuul-ci.org; GH Actions matrix + fail-fast wart, Airflow trigger rules, Argo failed-vs-errored retry split — respective docs; Temporal deterministic replay over an event history (the record-before-act principle the writeback/claim design approximates) — docs.temporal.io; I-PASS handoff bundle (NEJM 2014, 23%/30% reductions; AHRQ MHS-IV: strongest-certainty handoff format) — nejm.org/doi/full/10.1056/NEJMsa1405556, ahrq.gov.

## Related

- [[loop_parallel_execution]] — the concept paper; this builds its Option A with the barrier it assumed and the handover channel it lacked.
- [[loop_parallel_stages]] — the barrier, heal shapes, and assembler lever the engine generalizes; its analysis-only restriction is lifted.
- [[loop_campaign_scheduling]] — the campaign machinery Phases 1+5 carry onto the engine.
- [[loop_repo_compounding_v2]] — merge/retro spine, reused per member.
- `docs/issues/loop_control_plane_assessment.md` — the P0 handoff/outcome gaps this implements in user-configurable form.
- `docs/issues/loop_advance_nonatomic_wedges_loop.md` — why the legacy rotate path dies instead of gaining features.
- [[loop_review]] — findings registry; F15/F23/F32 addressed here, F34 (generation identity) partially via cycle/stage/member stamps.
