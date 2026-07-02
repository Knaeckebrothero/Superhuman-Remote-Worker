# Self-Improvement Loop — Test-Run Review

A living log of issues, rough edges, and optimizations found while running the
**project self-improvement loop** on real test runs. Companion to the design +
implementation plan: [`features/project_self_improvement_loop.md`](features/project_self_improvement_loop.md).

Related concept: [`features/loop_parallel_execution.md`](features/loop_parallel_execution.md) weighs an optional
parallel/pipelined execution mode; its "is speed even the bottleneck?" question is answered by the
cost/latency findings logged here (e.g. Run 1's prompt-token-dominated critic — parallelism cuts
wall-clock, not token cost).

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

### Runs 2–5 — 2026-06-26 → 2026-07-01 — post-fix era (audited 2026-07-01)

All on the Hotel Rheinland ERP goal; **all five loops to date ended `stop_reason=user`, none
reached budget**. Full audit + the consolidated plan:
[`features/loop_optimization.md`](features/loop_optimization.md).

- **Run 2** — loop `ed71a83d` · gpt-5.5 · pod. Scholar completed after **5.1 h**; stopped
  next morning mid-critic. (gpt-5.5 via codex-proxy records **no token usage** → F30.)
- **Run 3** — loop `53f59056` · `minimax/minimax-m3`. DOA on model keying; stopped in 25 min.
- **Run 4** — loop `3193732a` · `openrouter/minimax/minimax-m3` · pod. **Best run:** full
  scholar→critic→developer cycle, 28 phases pushed to `main` (F11 fix verified in anger) —
  then the developer **wedged 3 days** (last LLM call 06-27 17:25, hand-cancelled 06-30)
  → F25 confirmed, promoted P1. Cycle ≈ 85 M tokens (F31).
- **Run 5** — loop `09a19b50` · `MiniMax-M3` direct · **vm backend**. Critic failed fast on
  endpoint connection errors (advance counted it correctly → F32); developer "completed"
  7.4 h / 41.3 M tok **in a disconnected local git — all work lost** (F29). Counter-news:
  the scholar read run-4's KB state and proposed 4 genuinely new directions — **cross-loop
  KB coordination verified** (F20 behavioral confirm ✅; F22 still untested, critic died
  before selecting).

### Run 6 — 2026-07-01 → 07-02 — first operationally clean run (10 jobs, user-paused)

- **Loop** `7ca259e2` · project `68137e29` · **MiniMax-M3 · vm backend** ·
  `[scholar, critic, developer]` · max_iterations 33, paused cleanly at 10 (in-flight job
  finished, no next spawn — pause semantics verified live).
- **Verdict: operations solved, artifact lost.** 10/10 jobs completed — 3 full cycles +
  a 4th scholar, ~18.5 h unattended, zero failures, zero wedges. But vm again → **F29 ×3**:
  `main` untouched (tip `c0f272a8`, 06-27); all three developers greenfield-rebuilt
  `kurort_engine` in isolated VMs and every artifact was destroyed (~90 M of ~194 M total
  tokens). **Idea-space compounds — artifact-space is Groundhog Day**: each cycle picked a
  genuinely NEW pain point (offline-first sync → Kurverwaltung remittance CSV → MinStay
  enforcement), no re-proposals; dev 1's own plan note reads "Iteration 3 (Loop 4 **third
  restart**)" — the agents *know* the artifact keeps vanishing.
- **Numbers:** ~55–65 M tok/cycle @ **4–11 % cache**; critics leaner (6.5–10 M, 28–41 min);
  devs 93/152/180 min (growing); **scholar #10 RUNAWAY: 5.9 h / 35.8 M tok / 8 phases /
  avg 212k prompt-tok per call (max 298k, 127 of 168 calls >150k) @ 4 % cache, 31 s/call**
  — context bloat, not a wedge (F1 + F14-L3 + F33 compounding; worsens as the KB grows).
- **F22 partial:** 2 of 3 critics superseded their losers (project `superseded` 2 → 28);
  cycle-2's critic chose Proposal 004 and left 001–003 active — prompt-only enforcement is
  stochastic → make supersede **mechanical in the advance hook**.
- **F33 escalates:** KB doubled 144 → **374 notes in one run**. **F34 live:** three
  self-invented numbering schemes in one run ("Loop 3/4/7/10" vs iteration numbers).
  Critics self-organize well (pinned "operating rules" + "tie-breaker stack" notes).
- **Priority shift** folded into [`features/loop_optimization.md`](features/loop_optimization.md):
  **F29 is THE blocker** — nothing else is worth optimizing until execution output
  survives the night.

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
- [-] **F5 (P2 — ✂️ DESCOPED by design 2026-06-26) — Definition of Done is re-invented every
  iteration.** Because `acceptance_criteria` is empty, each job re-infers the DoD
  ("Definition of Done…" by the scholar, "Acceptance criteria for Critic iteration 2" by the
  critic) — a drift risk the research flagged (anchor "done" to *stable external* criteria).
  **Fix:** have the first job establish the DoD as one pinned note that later jobs must reuse,
  not re-create; or require acceptance criteria at loop start (see F7).
  **→ ✂️ DESCOPED as a *measurement* concern (2026-06-26).** A per-job re-invented DoD only
  mattered when aggregating local DoDs into a project convergence % (F24, descoped). Without
  that, each job's own DoD is fine. The *coordination* half — pin a stable goal so the loop
  doesn't drift — survives but lives in **F7** (ground the loop) and **F23** (pin the decision
  for the next agent), not as a standalone finding.
- [x] **F6 (P3 — ✅ FIXED 2026-06-26, ×3) — invalid KB note types in the loop prompts.** The
  scholar role says "write each candidate as a `proposal` note," but the KB enum (`NOTE_TYPES`,
  `knowledge_graph.py:46`) has no such type → the scholar logged a "Blocker," did a fallback
  search, wasted a couple of calls, and used `plan` instead. **Bigger than filed:** the same
  bug hit **three** names — `proposal` (scholar), `verdict` (critic), and `definition_of_done`
  (kickoff fallback) — none valid.
  **→ ✅ FIXED 2026-06-26 (`project_loops.py`):** mapped each to a valid type + tag —
  `proposal`→`plan` tagged `proposal`, `verdict`→`decision` tagged `verdict`,
  `definition_of_done`→`goal` tagged `definition_of_done`. Bonus: aligns with the F13
  convergence TTLs — proposals (`plan`) age out (helps F22), the verdict (`decision`) is
  durable (helps F23). Alternative considered + deferred: first-class `proposal`/`verdict`
  enum types (needs a migration).

### Grounding / setup

- [ ] **F7 (P1 → P2, ♻️ RECAST 2026-06-26) — Loop self-grounds via research; don't gate on
  human grounding (was: "No grounding").** The project had **0 datasources** and empty
  acceptance criteria/steering — it is designing an ERP "better than Resavio" with
  no Resavio data, no hotel requirements, and no code; the agents invent
  requirements from web research. Generic output at high token cost. **Fix:** warn
  (or block) on loop start when a project has no datasources **and** no acceptance
  criteria; nudge the user to attach source material + define the DoD first.
  **→ ♻️ RECAST (2026-06-26) — the "require grounding" fix is the anti-pattern.** The loop's
  *purpose* is to self-ground: the scholar researches the target system (Resavio/Salesforce
  is public — features, docs, pricing are all on the web) **and** the domain ("what a good
  ERP needs"), the critic picks the best next action, the developer builds it, repeat —
  improving a poor first system until it's good. If the user had to supply datasources +
  acceptance criteria up front, they wouldn't need a loop; they'd dispatch one-shot jobs. So
  **do not gate the loop on human grounding.** Reclassification: the ~60-LOC toy was a
  **plumbing symptom**, not a grounding symptom — a non-compounding (F11), non-coordinating
  (F22/F23) loop *cannot* get past iteration-1's worth of work, so it necessarily looks like
  a toy. With F11 ✅ done and F22/F23/F13 landing, the research compounds and the system
  improves across cycles — the loop working as designed. **Residual (downgraded to P2):**
  ensure the *scholar role actually performs* the competitor + domain research as its first
  move **and writes it as durable grounding notes** (so it compounds, not re-researched each
  cycle) — a scholar-prompt + F22/F23/F13 concern, not a gate. Human-attached datasources
  become **optional enrichment** for *private/internal* context the web can't supply (e.g.
  "match our existing workflow"), never a precondition; a soft FYI at most, never a block.
  **→ ✅ residual addressed in prompt 2026-06-26 (pending next-run verification):** the scholar
  role block (`project_loops.py`) now opens with a hard "you MUST research the target/competitor
  system + domain… record concrete, named findings as durable KB notes… anchor proposals in the
  specifics you found, not generic boilerplate." Whether it actually self-grounds (vs. invents)
  is verified on the next loop run.

### Lifecycle / reliability

- [x] **F8 (P1) — Early-phase preemption replays the job opening 2–3×.** A higher
  priority job preempted the scholar during its first phase (before any checkpoint
  snapshot), so each cross-pod resume cold-restarted from `init_workspace`,
  re-running the kickoff and early KB writes. **Filed:**
  [`done/preemption_before_first_checkpoint_replays_job_opening.md`](done/preemption_before_first_checkpoint_replays_job_opening.md).

### Feature gaps

- [-] **F9 (P2 — ✂️ DESCOPED by design 2026-06-26) — No goal-met early stop.** The loop
  burns its full iteration budget even if the goal is met. Now **more feasible**: the critic
  already writes structured verdict notes, so parsing a verdict for a "DoD met" signal is a
  concrete path. Tracked as deferred in the implementation plan.
  **→ ✂️ DESCOPED — design rejection, not a deferral (2026-06-26).** The loop is
  unconditional: it stops only on the user's budget or a manual stop. "Goal met" is judged by
  the **human at check-in**, never auto-detected — an LLM "DoD met at 90%" is precisely the
  toy-declared-done failure we're trying to kill (F7). A sane budget is the user's
  responsibility; a forgotten 9999-round loop is user error, not a missing feature. Only the
  budget *unit* still needs honest naming → F15.
  **→ enacted in code 2026-06-26 (`project_loops.py`):** the critic prompt's "If the Definition
  of Done is genuinely and fully met… that is the goal-met stop signal" line was **removed** —
  the unconditional design now lives in the prompt, not just this doc. The critic always
  "selects the next most valuable improvement instead of declaring completion."

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

- [x] **F11 (P1) — Iterations do NOT accumulate; every job branches from an
  empty `main` and is never merged back.** ✅ FIXED + k3d-verified 2026-06-26 (uncommitted on `develop`). `orchestrator/services/job_provisioning.py:121-124`
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
  **→ Fixed (v1, 2026-06-26, unit + k3d-verified):** [`features/loop_repo_compounding.md`](features/loop_repo_compounding.md)
  — the execution role works directly on `main`; its existing `autonomy=full`
  commit+push compounds the codebase IN PLACE. Analysis roles stay on throwaway
  branches; a `.gitignore` floor keeps scratch off `main`. No orchestrator
  merge/PR — agent-push reuses existing machinery (rejected: orchestrator
  squash-merge, and subjob-graft snapshots). **k3d E2E confirmed** execution-on-main
  + in-place compounding + floor; caught + fixed a `skills/` floor leak. Artifact
  keystone; pairs with the reasoning keystone F13/F22/F23/F24.
- [x] **F12 (P1) — The developer role has no KB tools, so the real handoff
  channel is git, not the blackboard.** The developer's tool set is 18 tools with
  **no `kb_*`**; it made **0 KB calls** and instead read predecessors via
  `git show origin/job/<id>:notes/…` (it flagged this itself in
  `notes/repository_findings.md`). This contradicts the design's "coordinate ONLY
  through the project KB," makes scholar/critic KB writes effectively write-only,
  and **breaks entirely on `virtual`/`none` workspace tiers** (no git). The
  verdict only reached the developer because it was mirrored to both git and KB.
  Fix: grant `kb_*` to the developer capability set (or auto-inject KB tools for
  loop jobs), and choose one canonical handoff channel.
  **→ Git-channel half addressed in** [`features/loop_repo_compounding.md`](features/loop_repo_compounding.md)
  (v1: role-scoped channels via provisioning — execution→`main`, analysis→throwaway
  branch + prompted to use the KB). ✅ **Capability half also FIXED** — a misdiagnosis: every role already inherits
  all 10 `kb_*` from `config/defaults.yaml`, so there was nothing to grant. The tools
  were dropped at runtime (`registry.py:518`) when a missing embedding key killed
  `knowledge_store` init — the embedding-key bug, fixed+committed `00c47f4d`, deployed
  dev `sha-3c1fa7a`
  ([`done/embedding_key_missing_silently_disables_memory_and_kb.md`](done/embedding_key_missing_silently_disables_memory_and_kb.md)).
  Behavioral confirm: the 2026-06-26 k3d loop run hit KB *retrieval* errors
  (`tsquery stack too small`), not *init* errors → the store came up + tools present.
- [x] **F13 (P1) — The KB blackboard has no supersede/dedup/contradiction; that
  machinery runs on the *other* store.** `knowledge_index` notes are **100%
  `active`, 0 superseded/archived**; the bi-temporal verdict/MERGE/supersede
  machinery (memory-overhaul Phase 4) fires only on `memories` (RecallStore —
  72% retired). So the deliberate coordination notes the loop depends on never
  converge: every stale "Acceptance criteria for iteration N" stays active and
  competes in future searches forever. Fix: route KB writes through the same
  verdict/supersede path, or at least auto-archive superseded meta notes +
  contradiction-check `verdict` notes.
  **→ ✅ SOLVED 2026-06-26 (v1, uncommitted develop; unit + k3d E2E-verified).** The fix is
  *not* the store-machinery route above — that framing was refined during design. The real
  cause: the "curator" is an inline aux pass (`CurateKnowledgeTask`) that only *populates*;
  the KB never got the *assembler* half its sibling store (`memories`) already has. v1 adds
  **`AssembleKnowledgeTask`** — a per-cycle, stale-queue-gated convergence pass that
  supersedes/merges/refreshes/archives notes whose `note_type`-aware **TTL** (`remaining_cycles`,
  migration `0007`) ran out (decremented once per loop cycle-wrap). **k3d E2E**: converge
  superseded 4 + refreshed 2 of 6 seeded stale notes → project `superseded` **0 → 4**;
  TTL-on-write confirmed (`state`→2 / `goal`→3 / `decision`→durable). **Subsumes F22** (the
  pass owns supersede; no critic action needed) and **de-risks F23** (injection ranks over a
  converged set). Full design + E2E + deferred follow-ups:
  [`features/kb_convergence_ttl_reverification.md`](features/kb_convergence_ttl_reverification.md).

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
  **→ still dead, new shape (run 6, 2026-07-02):** now `404` from
  `https://api.minimax.io/v1/rerank` on **every** memory-inject (168/168 on the job-10
  scholar) — the reranker follows the loop model's endpoint to a provider with no rerank
  API. Memory scoring silently degraded on all MiniMax runs.

### F8 is broader than originally filed
The preemption cold-restart hit **job 2 (critic) as well** — 4 cold starts,
preempted 3× within 2 minutes during its phase-0 restart — on top of job 1's 3
starts, and it **failed an unrelated bystander scholar job** (`a3edd26e`). The
preemptor fleet is **~10–11 stale priority-10 paused `critic` zombies** (06-17 →
06-23); only the oldest (`e92fcfcc`) fired in this window. Both loop jobs still
completed legitimately (confidence 0.88 / 0.95) — F8 wastes compute + latency,
not correctness. Captured in
[`done/preemption_before_first_checkpoint_replays_job_opening.md`](done/preemption_before_first_checkpoint_replays_job_opening.md).

### Verified healthy (no action)
Advance machinery is exactly-once and race-safe (`claim_project_loop_advance`
CAS; 3 serialized advances; no double-spawn; the safety-net sweeper was never
needed). Pod/agent GC is clean (no orphans/leaks). Both completed jobs froze with
real `job_complete` payloads. The developer is doing disciplined TDD
(spec→tests→code) in the right order and on-target with the critic's verdict —
just slowly (43 calls / 1.35M tok for the spec phase so far).

---

## Findings — cycle-boundary review (added 2026-06-24, round 2: 5 subagents)

Run after cycle 1 completed (scholar1→critic2→developer3) and cycle 2 began
(scholar4 `85238a88`). **Decisive result: the loop does NOT compound — cycle 2
replays cycle 1 — and we now have the root-cause *mechanisms* (F20, F22–F24), not
just the symptom (F11).** Counter-news: the *agents* do good work — the developer
shipped real, test-verified code; no hallucination; no false victory. The failure
is entirely in the coordination/accumulation plumbing.

**Cross-cycle verdict (headline):** scholar4 re-proposed cycle-1's space — of its 3
proposals, #1 = the **already-selected** "PMS-first modular monolith" (it told the
critic to "evaluate build-vs-buy cleanly," redoing a decided question) and #3 = the
critic's **#3-ranked** "integration hub"; only 1 is novel (~⅔ repeated). It branched
from the empty `main` (cycle-1 code is HTTP 404 to it) and made **zero KB calls**.

### Root causes of non-compounding (P1)

- [x] **F20 (P1) — KB tools are inconsistently injected across loop jobs.** Same
  `config_name`, different toolset: the cycle-1 scholar had **45 tools incl. all
  `kb_*`** ("knowledge base" hierarchy); the cycle-2 scholar `85238a88` (35 tools)
  and developer `280719ed` (18 tools) had **no `kb_*`** ("memory" hierarchy) and
  made 0 KB calls. The role that most needs to read the blackboard at cycle start
  can't. (Same intermittency seen in job1's cold-restart runs: 35 then 45.) Fix:
  assert `kb_*` in every loop job's resolved config at dispatch; root-cause why
  iter-4 resolved a different toolset than iter-1.
  **→ ✅ FIXED (root cause).** The intermittency *was* the embedding-key bug — a blob
  job that didn't receive `EMBEDDING_API_KEY` failed `knowledge_store` init and dropped
  every `kb_*` at `registry.py:518`. Fixed+committed `00c47f4d`, deployed dev
  `sha-3c1fa7a` ([`done/embedding_key_missing_silently_disables_memory_and_kb.md`](done/embedding_key_missing_silently_disables_memory_and_kb.md)).
  The suggested dispatch-time `kb_*` assertion was NOT added (optional backstop); a
  clean behavioral re-verify on the next loop run is the remaining confirmation.
  **→ ✅ behavioral confirm done (run 5, 2026-07-01):** the scholar `d199f0c5` read run-4's
  KB state, wrote "Loop 2 anti-patterns from Loop 1", and proposed 4 new directions; the
  developer found the prior iteration's notes. KB tools present + actually used. CLOSED.
- [~] **F23 (P1, ⏸ DEFERRED — verification pending) — KB injection is similarity-ranked
  to the current todo (top-5), with no pinning of decision/proposal/DoD notes.**
  `graph.py:1132` runs `hybrid_search(query=pending_todos[0].content, match_count=5)`.
  So compounding works only when the current task is semantically near the note you
  need: the **developer succeeded by luck** (todo "implement PMS-first slice" matched
  the verdict note) while **scholar4 failed** (generic "propose approaches" todo doesn't
  match the prior *proposal* notes). Coordination is luck correlated with task
  phrasing, not a guarantee.
  **⏸ Deferred until the next loop run collects evidence.** The defect is real for an
  *autonomous* loop — injection is the only coordination channel (no human to say "check
  the decision"), and similarity delivers *relevance* when the loop needs *state*: a
  "propose approaches" todo is in the opposite vocabulary from a "decision: X" note, so
  the load-bearing note gets buried rather than ranked. **But** the priority hinges on an
  untested assumption — that the agent won't self-serve via manual `kb_search` — and the
  observed run couldn't test it, because F20 (the embedding-key bug) had stripped the
  `kb_*` tools entirely. Now that F20 is fixed, re-run and collect: (1) does the agent
  `kb_search` on its own? (2) does the decision/goal note land in the injected context?
  If yes to both → drop to P3 (injection is just a prime; the agent self-serves). If it
  still re-proposes decided work → confirmed P1. Lighter fix than "inject all proposals":
  a small always-on loop-state header (DoD + latest `decision` + "N proposals / M
  rejected — `kb_search` to see them"), then similarity fills the rest. Notes already
  carry `note_type` (`decision`/`goal`/`question`/`state`) + `status`, so pinning is a
  cheap `WHERE` clause — no schema work.
  **→ ✅ SETTLED — CONFIRMED, injection is similarity-luck (run 6 deep dive, 2026-07-02).**
  Hard numbers across 1,414 injected turns: the **DoD/goal note reached 0 of 3 developers
  in 788 turns**; the same-cycle verdict reached the developer in only 6.6 % / 25 % / 34 %
  of turns (improving by vocabulary accident, not design); retrospectives crowd 48–83 %
  of the top-5 slots (truncated to 500 chars = useless at that size); one mid-job sample
  had **all 5 slots** filled with June-27 notes from a prior loop. Scholars/critics repair
  injection's misses with an opening KB-archaeology ritual (20–51 reads); **developers
  barely self-serve (10–25 reads)** — the worst-served role compensates least. The pinned
  header is confirmed necessary; exact contents: current verdict + DoD +
  current-iteration status (loop_optimization Tier 3 #10).
- [~] **F22 (P2, ⏸ DEFERRED — verification pending) — No tried/rejected ledger is ever
  written; ranking ≠ rejection.**
  The critic *ranked* all 5 proposals but recorded the 4 non-selected as "useful but
  narrower," never `rejected`/`superseded` (the KB `superseded` status exists, unused).
  So even a KB-reading agent finds no dead-end list, and the kickoff's "don't
  re-propose tried/rejected" guard has nothing to consult (→ the live repetition
  above). Fix: the critic's selection must mark non-selected proposals `superseded`
  (or write one tried/rejected note).
  **⏸ Deferred until the next loop run collects evidence.** The F13 convergence pass
  (`AssembleKnowledgeTask`, shipped 2026-06-26) *claims* to subsume this, but only at the
  *mechanism* level — it now owns `supersede`, triggered by **TTL expiry / newer-same-type
  duplicate**, not by the **critic's decision**. The F13 doc's own acceptance criterion #4
  reads "✅ MET (mechanism) … the specific critic-rejected-proposal flow isn't separately
  exercised yet." TTL aging does not prevent the *observed* bug: a just-rejected proposal
  keeps fresh TTL through the immediately-following cycle — exactly when scholar4
  re-proposed the decided "PMS-first monolith." Re-run and collect: (1) do the non-selected
  `proposal` notes get superseded/aged before the next scholar proposes? (2) does the next
  scholar still re-propose already-decided/rejected work? If it still re-proposes →
  confirmed P2, add a **decision-driven** supersede (critic flips losers at selection, or
  the convergence pass supersedes any `proposal` older than the `decision` that selected a
  different one). If not → close as subsumed by F13.
  **→ decision-driven fix ADDED to the critic prompt 2026-06-26 (`project_loops.py`), ahead of
  the F13 wait-and-see:** the critic now must "mark every non-selected proposal `superseded`
  (ranking is NOT rejection — flip their status)" right after writing its verdict. So the next
  run *tests the fix* rather than re-confirming the bug — check (2) becomes "with the critic
  explicitly superseding losers, does the next scholar still re-propose them?" Kept `[~]`
  pending that evidence.
  **→ still untested as of 2026-07-01:** run 5's critic failed on endpoint errors before
  selecting (all proposals remain `active`, 142/144 notes active). The next *completed*
  critic run is the test — metric #4 of the validation run in
  [`features/loop_optimization.md`](features/loop_optimization.md).
  **→ PARTIAL PASS (run 6, 2026-07-02):** 2 of 3 completed critics superseded their losing
  proposals (project `superseded` 2 → 28); cycle-2's critic skipped it entirely. Prompt-only
  enforcement is stochastic — fix is to make the supersede **mechanical in the advance
  hook** when a verdict lands (loop_optimization Tier 1 #5).
  **→ WORSE than partial (07-02 deep dive):** only cycle-3 actually flipped statuses
  (`kb_update {status: superseded}` ×4). Cycle-1 and cycle-2 wrote parallel "Superseded —"
  *learning tombstones* without flipping anything — and **cycle-2's verdict falsely claims
  "each marked SUPERSEDED"** (its 13 kb_updates were all `add_links`). The other ~43 flips
  this run came from the aux convergence/curator passes, cycles later. Ironic detail: the
  cycle-1 critic *authored* the pinned rule "Three losers always superseded" it then
  skipped. Mechanical identifiers verified reliable: `note_type='plan'` + slug
  `loop-{N}-proposal-{NNN}` + same-cycle scholar `job_id` + winner from the verdict slug;
  `tags`/`phase` are NOT reliable (mostly empty).
- [-] **F24 (P1 — ✂️ DESCOPED by design 2026-06-26) — No project-level acceptance vector →
  the loop can't measure convergence.** Each job grades itself against a *self-authored
  local* DoD: the developer's freeze (93%) checks only its own 4 spec ACs ("pytest collected
  4, passed 4"), never the project's ~10 capability areas. So every job can honestly report
  90%+ confidence forever while the project sits at ~1%. Fix: pin a project capability
  checklist as a KB note, require each job to map its slice onto capabilities and flip
  statuses, and surface "% capabilities green."
  **→ ✂️ DESCOPED (design decision 2026-06-26).** The loop is **unconditional by design** —
  it runs until the user's iteration/token budget is spent or the user stops it; there is no
  goal-met termination (F9). A convergence % presupposes a *fixed denominator*, but software
  is never done (Excel didn't stop in 1985) → the target is **undefined**, not merely hard to
  measure. The real acceptance function is the **human at check-in reading the artifact**
  (repo + Loop tab): more reliable than any checklist %, and free because the user already
  checks in periodically. Overshoot is bounded (~one night) on idle compute, so the cost of
  *not* measuring is trivial. Crucially, once the loop ignores the agent's self-confidence,
  the "90% self-grade" becomes **harmless** — nothing stops on it. The residual risk this
  would have guarded — a night of **thrashing** (re-doing decided work / drifting off-goal) —
  is owned by **F22/F23/F13** (make each cycle productive), which a progress meter wouldn't
  fix anyway (it would just show "flat"). Anti-vacuity (real system vs. toy) is owned by
  **F7**.

### Reliability / tooling (P2–P3)

- [ ] **F21 (P2) — The prompt orders tools the agent doesn't have.** scholar4's
  system prompt still commands `kb_search`/`kb_write`/`kb_update` (10 tools) it was
  never given → guaranteed instruction-following failure + wasted reasoning. Gate the
  KB prompt section on tool availability (or always provide the tools, per F20).
- [ ] **F25 (P2 → P1, ⚠ CONFIRMED live 2026-07-01) — No stall/liveness detection.** A job
  wedged in non-terminal `processing` trips **neither** the safety-net sweeper (it only
  recovers terminal-but-not-advanced loops) **nor** the consecutive-failure cap, and the
  cockpit shows a static "processing" badge with no age. An overnight operator can't
  tell "working" from "hung." Fix: a processing-age stall detector + surface job
  age/current phase.
  **→ ⚠ CONFIRMED in the worst form (run 4):** developer `19707fa1` made its last LLM call
  06-27 17:25 (after 60.6 M tok / 28 phases pushed to `main`), then sat in `processing`
  until hand-cancelled 06-30 18:37 — **three nights lost to one wedge**. `jobs.updated_at`
  does NOT tick during a wedge (only changed at cancellation), so the detector needs a real
  activity signal (audit-DB last llm/tool timestamp). Fix designed as Tier 1 #1 in
  [`features/loop_optimization.md`](features/loop_optimization.md).
- [ ] **F26 (P3) — Code-job workspace lacks pytest; pip is externally-managed.** The
  developer burned ~10–15 min building a `/tmp/pytest-venv`, and its handoff repro
  commands point at that **ephemeral** venv (won't survive a workspace recreate → not
  reproducible). Bake pytest / a venv policy into the code-job workspace image.
- [ ] **F27 (P3) — Cost is unsurfaceable end-to-end for loop jobs.** Beyond F16
  (`jobs.total_tokens_used=0`): `list_project_loop_jobs` (`postgres.py:9270`) doesn't
  select token columns, and the cockpit `Job` model has no token field — so per-job/
  loop cost can't reach the UI without API+model changes. Zero spend visibility for an
  overnight run.
- [ ] **F28 (P3) — MCP `list_job_commits` returns "No commits found"** for jobs that
  have commits on their own `job/<id>` branch (it defaults to the `main` ref) —
  blinding the very tooling meant to audit loop branches.

### Verified healthy / good news (don't regress)
- **Developer execution quality is high.** developer3 shipped **real, runnable,
  test-verified code** — a correct `RoomInventory`/`Reservation` module (half-open
  overlap rejection that preserves the existing booking), 4 pytest tests **executed
  green** (`Exit 0, collected 4 items`), faithful spec→red→green TDD, on-scope with
  the critic's verdict, two self-caught scope violations, honest 93% freeze. The
  feared "execution errors are the #1 failure mode" did **not** occur. Tiny (~60 LOC)
  and stranded (F11), but genuine working software.
- **No hallucination.** Under zero grounding the agents *abstained* rather than invent
  Resavio/hotel facts — the failure mode is **vacuity, not fabrication** (a ~60-LOC
  toy where every differentiating feature is in `not_included`).
- **No false victory.** Per-job freeze confidence is well-calibrated and scoped
  (scholar 88%, critic 95%, developer 93%); the danger is the misleading *aggregate*
  (3 "successful" jobs ≈ 1% of goal), not individual over-claiming.
- **Loop API is correct after rollover.** `/loop` and `/loop/jobs` exactly match the
  DB post-wrap (status, role=scholar cycle 2, total_jobs_run=4, remaining=27, all 4
  jobs) — the Phase-3 cockpit backend is sound. F8 did **not** recur on the developer
  (its 2.5h was per-call latency × 335 calls + the pytest-env fight, not preemption/
  thrash).

### Updates to earlier findings
- **F7 (no grounding) — deepened:** the consequence is a ~40–60 LOC toy, not
  fabrication; the 5 proposals are distinct-from-each-other but generic boilerplate
  ("distinct ≠ tailored"); the critic's "grounded" criterion means *grounded in the
  KB's own vocabulary* (self-referential, can't detect the data void); citations
  [16]-[19] are real but generic PMS blogs ("citation as decoration"). Highest-leverage
  input = **the hotel's Resavio pain-points / replacement requirements** ("better than
  Resavio" is unfalsifiable without it).
- **F11 — sharpened:** developer3 has **98 commits** stranded on `job/280719ed` (never
  merged; `main` = empty "Initial commit"); job4 branched from empty `main` and 404s on
  cycle-1 files.
- **F15 — confirmed live:** the badge reads "scholar · job 4 of 30" — no cycle counter
  anywhere, so an operator can't tell they're in cycle 2.

---

## Findings — post-fix run audit (runs 2–5, added 2026-07-01)

Registered here for the index; **defined with full evidence + fixes in
[`features/loop_optimization.md`](features/loop_optimization.md)** (the consolidated
reliability-then-cost plan). Headline: the design claims all hold now — what kills runs
is seams (endpoints, VM backend, wedges) and cost.

- [ ] **F29 (P1) — VM workspace backend silently breaks artifact compounding.** Run 5's
  developer (`workspace_backend=vm`, `1ba2298b`) worked in a **local git init** — never saw
  `main`'s 200+ commits, re-built files that already existed there, pushed nothing that
  landed; 41.3 M tokens lost on teardown. Fix: fence `vm` for loops + the no-op
  compounding guard (Tier 1 #2/#4).
  **→ ⚠ RECURRED ×3 (run 6, 2026-07-02):** all three developers of loop `7ca259e2`
  greenfield-rebuilt and lost their work the same way; cumulative damage now **four
  developer iterations / ~130 M tokens / zero surviving artifacts**. Promoted to **the
  plan's #1 item** in [`features/loop_optimization.md`](features/loop_optimization.md).
  **→ ✅ ROOT-CAUSED (2026-07-02 deep dive, live-verified):** the harness clones via SSH
  *on the workspace backend* using the cluster-internal URL `http://srw-gitea:3000`
  (persisted in `project_repositories.repo_url`). VMs are tailnet nodes outside the
  cluster → `Could not resolve host: srw-gitea`, exit 128 → **silent fallback to
  `git init`** (`src/core/workspace.py:415-418, 362-372`) with the same unreachable URL
  as origin → every phase-boundary push fails as a swallowed warning
  (`git_manager.py:654`) while the job completes "successfully". Orchestrator side is
  fine (payload identical to pod jobs). **Fix:** dispatch-time URL rewrite in the VM
  block at `orchestrator/main.py:2159-2176` — swap the `GITEA_INTERNAL_URL` host for the
  already-working external `GITEA_URL` (`git.superhuman-remote-worker.com`, Cloudflare
  ingress, HTTP 200) on `git_remote_url` + all `repositories_payload[*].repo_url`; fixes
  clone AND push. **Hardening regardless:** a failed jobs-repo clone on a `work_on_main`
  role must fail the job loudly, never silently git-init.
- [ ] **F30 (P2) — codex-proxy jobs record no token usage in the audit DB.**
  gpt-5.5 loop jobs from 06-26 on have `metrics.token_usage` NULL on every
  `llm_requests` row (run 1, pre-proxy, recorded fine) — cost-blind exactly for the
  strong model. Extends F16/F27 (Tier 2 #9).
- [ ] **F31 (P1) — MiniMax-M3 is a false economy in the loop.** ~66–85 M tokens/cycle at
  7 % cache (direct) / 13–23 % (openrouter) vs gpt-5.5's ~7.1 M — ~10× volume in a chatty
  small-completion call pattern, plus keying DOA (run 3) and endpoint flake (run 5).
  Quantifies/inverts F2's cheap-model assumption (Tier 2 #6).
- [ ] **F32 (P2) — Rotation is outcome-blind.** Run 5's critic died on a transient error
  and was simply skipped — the developer built on the *previous loop's* stale verdict.
  Fix: retry a failed analysis role once before rotating (Tier 3 #12).
- [ ] **F33 (P3 → P2, run 6) — KB meta-noise is now self-inflicted via `curator: enabled`.**
  96 of 144 notes are auto-curated `learning`/`retrospective`, swamping the ~24 deliberate
  blackboard notes; F3 fixed the agent-written half, the curator now writes it for them
  (Tier 3 #13).
  **→ promoted P2 (run 6):** the KB doubled 144 → **374 notes in a single 10-job run**
  (~+23/job), and the growth directly feeds the scholar context-bloat runaway (212k-token
  prompts) — no longer cosmetic.
- [ ] **F34 (P2) — No cycle/generation identity.** Two developers wrote colliding
  "Iteration 3" note sets (both `active`); the run-5 scholar self-styled "Loop 2". Stamp
  cycle+generation into kickoffs and required note titles — also the declared
  prerequisite for any parallel mode (Tier 3 #11).

---

## Findings — run-6 deep-dive forensics (added 2026-07-02, 5-subagent investigation)

Mechanism-level findings from dissecting run 6's jobs (audit DB request payloads, KB
rows, code traces). F29's root cause and the F22/F23 resolutions are annotated on their
own entries above; these are the NEW defects. **Full evidence archive** (prompt-anatomy
tables, per-critic audits, per-developer reconstructions, KB attribution, fix option
details): [`issues/loop_run6_deep_dive_forensics.md`](issues/loop_run6_deep_dive_forensics.md).

- [ ] **F35 (P1) — Tool-call *arguments* are never trimmed from context; the agent
  re-reads its own writings forever.** At the job-10 scholar's peak call (298k prompt
  tokens, 466 messages), **~47–53 % of the prompt is serialized tool-call args** —
  kb_write 267k chars, write_file 238k — content already durable in the KB/workspace.
  The existing trimmer stubs old tool *results* (140 stubbed) but never args; it also
  retains 37 near-identical `todo_complete` echoes (~55k chars). History ≈ 94 % of the
  prompt; system/kickoff/KB-injection ≈ 1 % each. Fix: stub write-side args older than
  ~10 turns → ~40 % token cut on long jobs.
- [ ] **F36 (P2) — Compaction works but triggers inconsistently.** The run-6 developer
  compacted once (200,853 → 24,294 tokens with a 9.8k-char handoff summary — proof the
  machinery works); the scholar sailed to **298k** and the critic to 250k without it ever
  firing. Trace why the threshold didn't trip for analysis roles.
- [ ] **F37 (P1) — The 4–11 % cache rate is self-inflicted prefix instability, not a
  provider limit.** MiniMax provider caching demonstrably works (cached_tokens
  14–16k on most calls) but caps at the stable head: message positions 2–6 mutate
  **every call** — the `<active_tasks>` block rewrites on todo changes, and the synthetic
  memory/KB injection messages regenerate **random tool-call ids per call**
  (`memory_inject_<hex>`), so the huge append-only history behind them is structurally
  uncacheable. Fix: move injections + active_tasks to the message-list tail +
  deterministic ids → estimated **58–78 % cache** and the main latency lever
  (31 s/call is prefill-dominated).
- [ ] **F38 (P2) — The curator *reconstructs agent deliverables* and floods the KB;
  learning/retrospective notes are TTL-exempt (immortal).** Run-6 growth: 23 notes/job;
  curator ≈ 30 % by count (more by bytes); **54 retrospectives = 946 kB = 34 % of all
  bytes**. The proposal double-writes are the curator racing the agent (it re-derived
  "Loop 3/10 Proposal" notes from plan.md — one injected note even self-identifies
  "Author: Knowledge Curator"). 245 of 322 active notes have no TTL and are invisible to
  convergence; projection for a 33-job run ≈ 900 rows / 9–10 MB. Fix: curator stops
  writing per-phase retros + deliverable reconstructions to the KB; give
  learning/retrospective a TTL; collapse per-job role-boilerplate notes into loop-level
  singletons.
- [ ] **F39 (P2) — `refresh_ttl` has never landed a row: stale survivors decrement past
  zero forever.** TTL *decrement* works exactly to spec, but `last_verified_cycle` is
  NULL on all 374 notes and 38 active notes sit at `remaining_cycles ≤ 0` (some at −1) —
  the convergence pass's "keep" verdict never stamps, so the same notes are re-adjudicated
  every pass (`refresh_ttl`, `src/services/auxiliary.py:1467`). Code bug, needs a trace.
- [ ] **F40 (P1) — KB-truth vs artifact-truth: the blackboard gaslights the loop after
  artifact loss, and no role can detect it.** Destroyed developers' retros ("SHIPPED
  GREEN, 9/9 tests, 85.23 % coverage") stay `active` and later critics cite them as
  verified ground truth — the evidence chain is fully self-referential (KB notes
  validating KB notes). Critics run in fresh workspaces with no reachable `main`, so
  even the cycle-3 critic that actively hunted (`find . -name "kurort*"`, found nothing)
  rationalized the absence as per-job workspace isolation. The developers likewise never
  conceived of a remote (pushes are the harness's job) and treated empty workspaces as
  normal ("this is the third restart"). Consequences: (a) the no-op compounding guard
  (Tier 1 #2) is the ONLY viable artifact-integrity check — no agent role can own it;
  (b) after any artifact loss, `state`/`retrospective` notes describing shipped code are
  actively harmful and need invalidation (tie note validity to a `main` HEAD stamp?).
- [ ] **F41 (P3) — Two incidental agent-harness bugs surfaced by the forensics.**
  (a) The cycle-2 developer's **shell output capture died at 04:50** — its last ~20
  commands (even `echo HELLO`) returned "Exit code: 0 / (no output)"; it committed and
  froze blind and did not report it. (b) The cycle-1 developer suffered an in-job
  `todo_rewind` rollback that wiped its phase-2/3 files ("Operating Rule 8 violation"),
  making it pay the bootstrap tax twice in one job. Both predate/outlast the loop but
  cost loop tokens.
- [ ] **F42 (P2) — Subagent delegation is never used: 0 invocations fleet-wide,
  all-time, despite rendered instructions.** (Added 2026-07-02, follow-up audit.)
  `delegate_work` was in the tool menu on 100% of run-6 loop calls, and the delegation
  playbooks *rendered into the live prompts* (~50% of scholar calls, ~55% of critic
  calls — the strategic-phase ones); zero invocations in 788 turns. All-time on dev:
  `jobs.creation_order IS NOT NULL` → **0 rows** since the feature shipped 2026-03-23,
  across all models (gpt-5.5 and MiniMax-M3 both confirmed declining). Prompt prose
  doesn't drive tool adoption in a todo-driven harness — the decision happens at
  `next_phase_todos`, and the seeded todo scaffold has no delegation step. Matters
  because fan-out is the *structural* fix for the loop's context-accumulation cost
  (scholar #10's 35.8M → est. 10–15M), compounding with F37 instead of competing.
  Fix session anchor: [`issues/subagents_never_used.md`](issues/subagents_never_used.md)
  (consolidates this + `issues/scholar_delegation_not_exercised.md` + the light-tool
  gap in `issues/delegation_light_mode_missing.md`; a light ReAct subagent tool is in
  progress in a parallel session).
- [x] **F43 (P1 security) — The project repositories REST endpoint leaked the shared
  Gitea admin credentials, and displayed an unusable internal clone URL.** (Added +
  fixed 2026-07-02; surfaced from the project Repos tab while scoping the F29 fix.)
  Three distinct problems on one row (screenshot: `project-…-jobs` →
  `http://srw:<admin-pw>@srw-gitea:3000/…`):
  **(A, the leak)** `GET /api/projects/{id}/repositories` (`main.py:list_project_repositories`)
  and the create endpoint returned `project_repositories.repo_url` **raw** — including
  the embedded `user:password@` of the *shared Gitea admin* account (`gitea.py:45-46`).
  The endpoint is `require_project_member`, not owner-gated, so every member of every
  project could read admin write-access to the whole Gitea instance over REST (and it
  rendered in plaintext in Cockpit). Datasources already followed the F3 "creds never
  leave over REST" policy (`redact_datasources`); repos skipped it.
  **(C, same root as F29)** the displayed host is the cluster-internal `srw-gitea:3000`
  — unreachable from a browser. Same internal-vs-external URL fact as F29, but a
  *different surface*: F29's dispatch-time rewrite fixes the agent clone/push path and
  does **not** touch the API-read path.
  **(B)** the "Remove" button rendered for the managed `jobs` repo, which the backend
  correctly rejects (`main.py:remove_project_repository` → 400 "Cannot remove the jobs
  repository") — so the button could only ever fail. Cosmetic (no data-loss), but
  misleading. *Fix:* new `redact_repository`/`redact_repositories` + shared
  `externalize_gitea_url` in `security/access.py` (strip creds + rewrite host on read,
  applied to both list and create endpoints); F29's dispatch path reuses
  `externalize_gitea_url` (creds preserved so the agent can still push); Cockpit hides
  Remove for `role==='jobs'`. 14 unit tests in `tests/test_repository_redaction.py`.
  Structural note carried forward: repos embed creds *in the stored URL* (datasources
  keep them in a separate redact-on-read field + inject at dispatch); storing repos the
  same way would kill the leak at the source — deferred as a larger change.

---

## Notes

- Most of the quality fixes (F3–F6) are edits to the single kickoff builder
  (`orchestrator/services/project_loops.py:build_loop_kickoff`) and the role
  blocks above it — cheap, no architecture change.
- These changes affect **future** jobs only, and on the dev cluster need a
  commit + push + CI cycle to go live (unlike k3d/Tilt's live sync) — so they
  won't alter an in-flight run.
- The full-cycle findings (F11–F19) are **not** all cheap kickoff edits: F11 ✅ done (execution-on-`main` in-place compounding, not branch/merge), F12 ✅
  done (the capability-grant framing was a misdiagnosis — it was the embedding-key
  bug `00c47f4d`), F13 ✅ done (curator-assembler / TTL re-verification, *not* the
  KB-store-routing framing — see [`features/kb_convergence_ttl_reverification.md`](features/kb_convergence_ttl_reverification.md))
  — these are the structural changes that decide whether the
  loop can actually compound, and they outrank the prompt-level fixes.
