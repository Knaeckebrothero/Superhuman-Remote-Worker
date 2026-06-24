# Self-Improvement Loop — Test-Run Review

A living log of issues, rough edges, and optimizations found while running the
**project self-improvement loop** on real test runs. Companion to the design +
implementation plan: [`features/project_self_improvement_loop.md`](features/project_self_improvement_loop.md).

Anything big enough to need its own write-up gets a `docs/issues/` doc and is
linked from here; everything else — the small stuff and the tuning ideas —
lives here so it doesn't get lost between runs.

## How to use this doc

- Add a dated entry under **Run log** for each meaningful test run (loop id,
  project, model, what you watched, the one-line verdict).
- Record observations under **Findings** with: severity, what we saw (with
  evidence), why it matters, the proposed fix, and status. Tick the box when done.
- Promote anything large or cross-cutting to `docs/issues/` and link it back.

Severity: **P1** = cost/correctness-impacting · **P2** = output quality ·
**P3** = minor/cosmetic.

---

## Run log

### Run 1 — 2026-06-24 — first real dev-cluster run
- **Loop** `27cabc53` · **project** `54426051` ("Build an ERP for Hotel Rheinland
  in Bad Orb, better than Resavio") · **model** `gpt-5.5` · **roles**
  `[scholar, critic, developer]` · **max_iterations** 30.
- Watched jobs 1 (scholar) and 2 (critic).
- **Verdict:** the core mechanism works — the scholar wrote **5 genuinely
  distinct proposals**, the critic **read them and selected one** ("PMS-first
  modular monolith") with rationale + handoff. Propose→select→hand-off through
  the KB blackboard is validated on a capable model. The problems are **cost,
  KB noise, and lack of grounding** — not correctness.
- **Evidence snapshot:**
  | Step | LLM calls | Tokens | For |
  |---|---|---|---|
  | scholar (job 1) | 53 | ~1.57M | 5 proposals + handoff |
  | critic (job 2) | 115 | ~3.5M (98% prompt) | *selecting one proposal* |

  KB held 23 notes, of which ~8–10 were per-iteration **meta** notes; project had
  **0 datasources**, empty `acceptance_criteria`, empty `user_prompt`.

---

## What's working (don't regress)

- Scholar produces **genuinely distinct** candidate approaches (not variations).
- Critic **reads the proposals and selects one** with an explicit verdict note.
- Roles **hand off through the KB** (handoff notes written and read by the next role).
- KB blackboard usage is real on a capable model (`gpt-5.5`) — the gemma smoke
  run's zero notes was weak tool-use, not a design flaw.

---

## Findings

### Cost / efficiency

- [ ] **F1 (P1) — Per-job overhead for analysis roles is huge.** The critic ran
  **115 LLM calls / ~3.5M tokens across 5+ phases** (incl. a "Final Deliverable
  Verification" phase) just to *select a proposal*; the scholar ran 53 calls /
  ~1.57M tokens. Analysis roles run as full multi-phase worker agents with no
  brevity ceiling, so they pad. One cycle ≈ 5M+ tokens → a 30-job budget ≈ tens
  of millions of tokens. **Fix:** phase / tool-call cap for non-execution roles,
  and a brevity directive in the kickoff ("this is a single analysis step —
  finish in one phase, do not pad with extra verification"). *Biggest cost lever.*
- [ ] **F2 (P1) — One model for all roles.** The loop has a single `model` field
  (gpt-5.5 for everything). Scholar/critic are analysis and can run on a cheaper,
  high-volume model. **Fix:** per-role model override; for unattended long runs
  prefer the planned **MiniMax M3** over gpt-5.5 (cost) and reserve the strong
  model for the developer.

### Output quality / KB hygiene

- [ ] **F3 (P2) — KB fills with per-iteration *meta* notes.** Every job writes
  ~3–4 process notes — "Required deliverables / Hard rules / Style-and-language
  constraints / Acceptance criteria for *[role] iteration N*" — restating its own
  kickoff. Over 30 iterations these drown the ~5 real proposals, and each agent
  re-reads the noise (the critic did `kb_read`×8 then ×7). **Fix (cheap kickoff
  edit):** state that the KB is a **shared domain blackboard** — write only
  durable findings (proposals, decisions, verdicts, blockers, handoffs), never
  notes restating the task, role, deliverables, or style.
  *(`services/project_loops.py:build_loop_kickoff`)*
- [ ] **F4 (P2) — Role bleed: the critic edited the repo.** The critic ran
  `write_file` + `git_diff`/`git_status` — the developer's job. **Fix:** scope it
  in the kickoff ("As Critic, do not modify the repo; only read, evaluate, and
  write a verdict").
- [ ] **F5 (P2) — Definition of Done is re-invented every iteration.** Because
  `acceptance_criteria` is empty, each job re-infers the DoD ("Definition of Done…"
  by the scholar, "Acceptance criteria for Critic iteration 2" by the critic) — a
  drift risk the research flagged (anchor "done" to *stable external* criteria).
  **Fix:** have the first job establish the DoD as one pinned note that later jobs
  must reuse, not re-create; or require acceptance criteria at loop start (see F7).
- [ ] **F6 (P3) — `proposal` note type doesn't exist.** The scholar role says
  "write each candidate as a `proposal` note," but the KB enum has no such type →
  the scholar logged a "Blocker," did a fallback search, wasted a couple of calls,
  and used `plan` instead. **Fix:** point the role text at a real type + a
  `proposal` tag, or add `proposal` to the KB note-type enum.

### Grounding / setup

- [ ] **F7 (P1) — No grounding.** The project had **0 datasources** and empty
  acceptance criteria/steering — it is designing an ERP "better than Resavio" with
  no Resavio data, no hotel requirements, and no code; the agents invent
  requirements from web research. Generic output at high token cost. **Fix:** warn
  (or block) on loop start when a project has no datasources **and** no acceptance
  criteria; nudge the user to attach source material + define the DoD first.

### Lifecycle / reliability

- [x] **F8 (P1) — Early-phase preemption replays the job opening 2–3×.** A higher
  priority job preempted the scholar during its first phase (before any checkpoint
  snapshot), so each cross-pod resume cold-restarted from `init_workspace`,
  re-running the kickoff and early KB writes. **Filed:**
  [`issues/preemption_before_first_checkpoint_replays_job_opening.md`](issues/preemption_before_first_checkpoint_replays_job_opening.md).

### Feature gaps

- [ ] **F9 (P2) — No goal-met early stop.** The loop burns its full iteration
  budget even if the goal is met. Now **more feasible**: the critic already writes
  structured verdict notes, so parsing a verdict for a "DoD met" signal is a
  concrete path. Tracked as deferred in the implementation plan.

### Tooling (incidental, not loop-specific)

- [ ] **F10 (P3) — MCP `list_knowledge_notes` throws `Unknown format code '%'`.**
  A formatter bug surfaced while inspecting the KB during this review; unrelated to
  the loop but worth fixing (`get_knowledge_summary` / direct DB queries are the
  workaround).

---

## Findings — full-cycle review (added 2026-06-24, 5-subagent deep dive)

Job 3 (developer `280719ed`) dissected alongside re-analysis of jobs 1–2.
**Headline: two of the loop's three foundational claims are currently false in
practice** — work does **not** accumulate across iterations, and agents do
**not** actually coordinate through the KB. The propose→select→execute *rotation*
is sound; the *compounding* is not.

### Structural — the loop can't compound (P1)

- [ ] **F11 (P1) — Iterations do NOT accumulate; every job branches from an
  empty `main` and is never merged back.** `orchestrator/services/job_provisioning.py:121-124`
  hardcodes `create_branch(..., from_branch="main")` for project/loop jobs, and
  nothing ever merges `job/<id>` → `main`. Evidence: the 3 job branches
  (`job/1b099a61`, `job/7a777bb0`, `job/280719ed`) all share merge-base = the
  empty "Initial commit"; `main`'s tree is a 60-byte README; orchestrator logs
  show 3 branch-creates and **0 merges/PRs**. So iteration N+1 always starts
  blank — the loop is N independent from-scratch attempts, not a chain. **This is
  the dominant defect.** Fix: branch each iteration from the previous iteration's
  tip, or merge each completed loop job into `main` in the advance hook. (Subjobs
  already chain correctly via `from_branch = parent.branch_name`; only top-level
  loop jobs are broken.)
- [ ] **F12 (P1) — The developer role has no KB tools, so the real handoff
  channel is git, not the blackboard.** The developer's tool set is 18 tools with
  **no `kb_*`**; it made **0 KB calls** and instead read predecessors via
  `git show origin/job/<id>:notes/…` (it flagged this itself in
  `notes/repository_findings.md`). This contradicts the design's "coordinate ONLY
  through the project KB," makes scholar/critic KB writes effectively write-only,
  and **breaks entirely on `virtual`/`none` workspace tiers** (no git). The
  verdict only reached the developer because it was mirrored to both git and KB.
  Fix: grant `kb_*` to the developer capability set (or auto-inject KB tools for
  loop jobs), and choose one canonical handoff channel.
- [ ] **F13 (P1) — The KB blackboard has no supersede/dedup/contradiction; that
  machinery runs on the *other* store.** `knowledge_index` notes are **100%
  `active`, 0 superseded/archived**; the bi-temporal verdict/MERGE/supersede
  machinery (memory-overhaul Phase 4) fires only on `memories` (RecallStore —
  72% retired). So the deliberate coordination notes the loop depends on never
  converge: every stale "Acceptance criteria for iteration N" stays active and
  competes in future searches forever. Fix: route KB writes through the same
  verdict/supersede path, or at least auto-archive superseded meta notes +
  contradiction-check `verdict` notes.

### Cost — where the money actually goes (P1, quantifies F1/F2)

- [ ] **F14 (P1) — 98% of spend is uncompacted prompt re-send at ~18% cache;
  compaction never fires.** Measured cycle (3 jobs) = **7.14M tokens / 228 calls**
  (completion only 1.8%); the critic alone burned **4.23M tok / 132 calls / 7
  phases / 60 min** to select a proposal. 30-job budget ≈ **71–78M tokens
  (~$70–90, rough)**. No `compact`/`summarize` event in any job; prompt grows
  monotonically within a phase; cache only 17–24% despite a stable ~14k kickoff
  prefix. Ranked levers (each tied to a measured number): **L1** cap analysis-role
  phases/calls (critic→scholar-size ≈ −2.6M/cycle); **L2** enable compaction /
  trim tool-result bodies (≈ −1.75M); **L3** fix prompt-cache utilization (biggest
  $ lever — move ~2.9M prompt tok to cached); **L4** mini-tier model for critic
  strategic (≈ −80–90% on 2.94M tok); **L5** trim the 14k kickoff/system prelude;
  **L6** reduce memory/aux churn (currently invisible spend: critic logged 132
  `memory_inject`).

### Other findings

- [ ] **F15 (P2) — Budget unit is per-JOB, not per-cycle.** `_advance_project_loop`
  decrements `remaining_iterations` on every job, so `max_iterations=30` = 10 full
  cycles, not 30. Matches your "3 jobs = 1 cycle" framing, but the field name, the
  cockpit field, and the kickoff's "iteration N" all imply cycle-granularity.
  Decide the unit, name it clearly (`remaining_job_runs`?), and consider a
  cycle-aware option.
- [ ] **F16 (P2) — No per-job token accounting.** `jobs.total_tokens_used` /
  `total_requests` are 0 for all loop jobs despite hundreds of LLM steps; spend
  lives only in the audit DB. Blocks cost visibility and the planned token-budget
  stop (Phase 4 / F9). Fix: write totals back on completion, or have cost views
  read `srw_audit.llm_requests`.
- [ ] **F17 (P2) — Tactical phase silently runs on `gpt-5.4-mini` while the loop
  declares `gpt-5.5`.** The family matrix splits strategic (gpt-5.5) vs tactical
  (gpt-5.4-mini); good for cost, but invisible to the operator and it means the
  *implementation* (tactical) work runs on the weaker model. Surface the effective
  per-phase model; allow pinning.
- [ ] **F18 (P2) — KB read-churn + near-duplicate notes.** The critic issued **64
  KB-read calls** re-reading the same 5 proposals 3× and its verdict 4× (no
  cross-phase caching); there are already 3 competing "definition of done /
  criteria" notes and 3 overlapping "handoff" notes. O(iterations) read cost with
  no convergence (compounds with F13).
- [ ] **F19 (P3) — Memory reranker returns `403 Forbidden` on dev** (`/v1/rerank`
  via the litellm gateway, which is disabled on dev) → the Phase-3 reranker is
  silently inert on these runs. Expected consequence of the gateway being dark on
  dev; noted so memory-quality results from dev runs aren't over-trusted.

### F8 is broader than originally filed
The preemption cold-restart hit **job 2 (critic) as well** — 4 cold starts,
preempted 3× within 2 minutes during its phase-0 restart — on top of job 1's 3
starts, and it **failed an unrelated bystander scholar job** (`a3edd26e`). The
preemptor fleet is **~10–11 stale priority-10 paused `critic` zombies** (06-17 →
06-23); only the oldest (`e92fcfcc`) fired in this window. Both loop jobs still
completed legitimately (confidence 0.88 / 0.95) — F8 wastes compute + latency,
not correctness. Captured in
[`issues/preemption_before_first_checkpoint_replays_job_opening.md`](issues/preemption_before_first_checkpoint_replays_job_opening.md).

### Verified healthy (no action)
Advance machinery is exactly-once and race-safe (`claim_project_loop_advance`
CAS; 3 serialized advances; no double-spawn; the safety-net sweeper was never
needed). Pod/agent GC is clean (no orphans/leaks). Both completed jobs froze with
real `job_complete` payloads. The developer is doing disciplined TDD
(spec→tests→code) in the right order and on-target with the critic's verdict —
just slowly (43 calls / 1.35M tok for the spec phase so far).

---

## Notes

- Most of the quality fixes (F3–F6) are edits to the single kickoff builder
  (`orchestrator/services/project_loops.py:build_loop_kickoff`) and the role
  blocks above it — cheap, no architecture change.
- These changes affect **future** jobs only, and on the dev cluster need a
  commit + push + CI cycle to go live (unlike k3d/Tilt's live sync) — so they
  won't alter an in-flight run.
- The full-cycle findings (F11–F19) are **not** all cheap kickoff edits: F11 is in
  `job_provisioning.py` (branch-from / merge-back), F12 is a capability grant, F13
  is KB-store routing — these are the structural changes that decide whether the
  loop can actually compound, and they outrank the prompt-level fixes.
