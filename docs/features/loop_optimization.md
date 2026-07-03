---
tags:
  - feature
  - plan
  - orchestration
  - projects
  - self-improvement
  - reliability
  - cost
aliases:
  - loop optimization
  - loop reliability plan
  - loop cost plan
  - loop hardening
related:
  - "[[project_self_improvement_loop]]"
  - "[[loop_repo_compounding]]"
  - "[[loop_parallel_execution]]"
  - "[[loop_review]]"
  - "[[kb_convergence_ttl_reverification]]"
  - "[[stuck_agent_recovery]]"
---

# Loop Optimization — Reliability First, Then Cost

> The optimization plan for the [project self-improvement loop](project_self_improvement_loop.md), derived from a full audit of every dev-cluster loop run to date (runs 2–5, 2026-06-26 → 2026-07-01, audited via the loop/jobs tables, the audit DB, the project KB, and Gitea). The one-line diagnosis: **the loop's design claims are all proven now — rotation, KB-blackboard coordination, in-place compounding — but no loop has ever survived to its budget.** All five runs were stopped by the user. What kills runs is operational fragility at the seams (model endpoints, the VM workspace backend, wedged jobs) and cost per iteration — not the architecture. So the plan is ordered: **Tier 1 makes one unattended overnight run survivable; Tier 2 makes it affordable; Tier 3 makes each iteration count.** Parallelism stays deferred.

## Status

**Proposal (2026-07-01). Nothing below is implemented.** Registers findings **F29–F34** (indexed in [`loop_review.md`](../loop_review.md), the living findings registry) and consolidates the still-open cost findings (F1/F2/F14/F16/F27) and quality findings (F22/F23/F25) into one ordered plan. Grounded in live evidence, not projection — every item cites a measured number or an observed failure from the run audit below.

**The plan's own Definition of Done:** one overnight run on the pod backend that either reaches its budget or stops for a *visible, attributed* reason — with every execution iteration provably advancing `main` (or loudly flagged), and per-cycle cost readable from the Loop tab the next morning.

**Run 6 update (2026-07-02).** The validation experiment effectively ran itself before any fix landed: loop `7ca259e2` (MiniMax-M3, **vm**, same project) completed **10/10 jobs — 3 full cycles, ~18.5 h unattended, zero failures, zero wedges**, paused cleanly. Operationally this is the first survivable night. Artifact-wise it failed exactly as predicted: **F29 ×3** — all three developers rebuilt from scratch in disconnected VM-local gits and lost everything (`main` still at `c0f272a8`). Consequences folded into the plan below: **F29/VM is promoted to the #1 item**; F22's supersede becomes a *mechanical* Tier-1 item (#5, prompt-only was 2-for-3); F33 promoted to P2 (KB doubled 144 → 374 notes in one run, feeding a new failure shape — the **scholar context-bloat runaway**: 5.9 h / 35.8 M tok / 8 phases / avg 212k prompt-tokens per call at 4 % cache). Scored against the experiment's metrics [below](#the-experiment-that-validates-the-plan).

**Deep-dive addendum (2026-07-02, 5-subagent forensics on run 6).** Full evidence archive: [`issues/loop_run6_deep_dive_forensics.md`](../issues/loop_run6_deep_dive_forensics.md). Mechanism-level findings folded into the items below and registered as **F35–F41** in [[loop_review]]: F29 root-caused (VM can't resolve the cluster-internal Gitea URL → silent git-init fallback; fix = dispatch-time URL rewrite, item #4); the token bloat is untrimmed **tool-call args** (F35) + inconsistent compaction (F36); the cache floor is **self-inflicted prefix instability** (F37, fix sized at 58–78 % cache); the curator reconstructs deliverables and writes immortal retros (F38) + `refresh_ttl` never fires (F39); the KB **gaslights** the loop after artifact loss and no role can detect it (F40 → item #2 is the only integrity check); two incidental harness bugs (F41). One vindication worth stating: developer forensics verified the lost work was **real, tested, domain-correct, and honestly reported** — coverage numbers verbatim from pytest, self-audits against fake tests, zero gaming. The agents are fine; the plumbing loses their work.

## Run audit (2026-06-26 → 2026-07-01)

Five loops, all on the "Hotel Rheinland ERP" goal, all `stop_reason=user`, none reached budget:

| # | Loop | Model / backend | What happened |
|---|------|-----------------|---------------|
| 2 | `ed71a83d` 06-26 | gpt-5.5 / pod | Scholar completed after **5.1 h**; user stopped the loop next morning mid-critic. 2 jobs. |
| 3 | `53f59056` 06-27 | `minimax/minimax-m3` / pod | DOA on model keying; scholar never produced, user stopped in 25 min. |
| 4 | `3193732a` 06-27 | `openrouter/minimax/minimax-m3` / pod | Best run: full scholar→critic→developer cycle, 28 phases pushed to `main` — then the developer **wedged for 3 days** (last LLM call 06-27 17:25, cancelled 06-30 18:37). |
| 5 | `09a19b50` 06-30 | `MiniMax-M3` (direct) / **vm** | Critic failed fast (endpoint connection errors, correctly counted + advanced); developer "completed" after 7.4 h / 41 M tokens — **in a disconnected local git; all work lost** (F29). |
| 6 | `7ca259e2` 07-01→02 | `MiniMax-M3` (direct) / **vm** | **First operationally clean run**: 10/10 jobs, 3 full cycles, ~18.5 h unattended, 0 failures/wedges, paused cleanly. But F29 ×3 — all three developers' work lost; scholar #10 ran away (5.9 h / 35.8 M tok / 8 phases, 212k avg prompt @ 4 % cache); ~194 M tokens total (~90 M destroyed). |

Token economics (audit DB, `llm_requests.metrics->token_usage`):

| Job | Role | Calls | Tokens | Cache | Note |
|-----|------|------:|-------:|------:|------|
| `7cc5b31f` (loop 4) | scholar | 215 | 12.4 M | 18 % | 74 min |
| `40cf0d50` (loop 4) | critic | 162 | 11.9 M | 13 % | 34 min — *to select a proposal* (F1 again) |
| `19707fa1` (loop 4) | developer | 1 202 | 60.7 M | 23 % | 28 phases in ~3.75 h, then wedged (F25) |
| `d199f0c5` (loop 5) | scholar | 168 | 24.8 M | **7 %** | 200 min, VM |
| `b17d63bc` (loop 5) | critic | 8 | 0.3 M | — | failed fast on connection errors |
| `011fcfce` (loop 5) | developer | 266 | 41.3 M | **7 %** | 446 min, VM, work lost (F29) |
| `6e8bfcc5` (loop 2) | scholar | 512 | **NULL** | — | gpt-5.5 via codex-proxy records no usage (F30) |

≈ **66–85 M tokens per MiniMax cycle vs ~7.1 M for the gpt-5.5 baseline** (run 1, `loop_review.md`) — the "cheap" model runs ~10× the tokens at 7 % cache (F31).

### Proven working — do not regress

- **Cross-loop KB coordination is real** (the run-1 doubt is settled): the loop-5 scholar read loop-4's state, wrote *"Loop 2 anti-patterns from Loop 1 — what not to repeat"*, and proposed 4 genuinely new directions instead of re-proposing the decided PMS-first monolith. F20's behavioral confirm is done (kb tools present + used); F13 TTLs stamp correctly (`remaining_cycles` by note type).
- **Execution-on-`main` compounding works on the pod backend** ([[loop_repo_compounding]]): 28 phases of real per-todo commits, `.gitignore` floor holding.
- **Advance machinery is exactly-once through failures**: the failed critic incremented `consecutive_failures` and rotation continued; no double-spawns; the sweeper stayed idle (never needed).

### Still unverified

- **F22 (critic supersedes losing proposals)** could not be tested — loop 5's critic died before selecting. All proposals in the KB remain `active`. The prompt fix is in; the next completed critic run is the test.
- **F23 (similarity-luck injection)** got weak positive evidence — both loop-5 agents *self-served* prior state via KB reads — but the pinned-header question stays open (Tier 3).

## New findings (F29–F34)

- **F29 (P1) — The VM workspace backend silently breaks artifact compounding.** Loop 5 ran with the new per-loop `workspace_backend=vm` (`1ba2298b`; `config_override.workspace.backend` on every spawned job). Its developer `011fcfce` never received the project repo: the audit trail shows it in a **freshly-initialized local git** ("the initial commit" tracking `.venv/` and `tools/` — files the compounding floor would exclude; iteration baseline `9a7112d`), while `origin/main` sat at 200+ commits it never saw. It spent 7.4 h / 41.3 M tokens faithfully re-building "iteration 3" files that *already existed on `main`* (spec.yaml, rates.py, errors.py), pushed nothing that landed (main tip `c0f272a8` unchanged 06-27 → 07-01), and the work evaporated on teardown. [[loop_repo_compounding]] pinned loops to "remote/sandbox/vm" assuming VM had working git provisioning — it does not (root-cause open: repo seed on VM boot / golden-image interaction). Its deferred no-op guard exists precisely for this (→ Tier 1 #2); until VM repo delivery is verified end-to-end, `vm` must be fenced for loops (→ Tier 1 #4).
- **F30 (P2) — codex-proxy jobs record no token usage in the audit DB.** gpt-5.5 loop jobs from 06-26 onward (`6e8bfcc5`: 512 calls, `8bf2be7e`: 121 calls) have `metrics.token_usage` NULL on every row, while run 1 (06-24, pre-codex-proxy) recorded usage for the same model. Cost is blind exactly for the strong model — on top of F16's `jobs.total_tokens_used=0`. Extends F16/F27.
- **F31 (P1) — MiniMax-M3 is a false economy in this harness.** ~10× the tokens per cycle (66–85 M vs 7.1 M) in a chatty many-small-calls pattern (developer: 1 202 calls, 5.3 calls/min), at 7 % cache direct / 13–23 % via OpenRouter — plus one loop DOA on keying and one critic killed by endpoint connection errors. Whatever the per-token discount, volume × cache-miss × flakiness eats it. Model choice must account for call-pattern and cache support, not sticker price (→ Tier 2 #6).
- **F32 (P2) — Rotation is outcome-blind.** A failed critic is simply skipped: loop 5's developer proceeded on the *previous loop's* stale verdict because `_advance_project_loop` rotates by position regardless of whether the role produced its artifact. Acceptable for execution failures (the research says treat them as steady state) but wrong for a role whose *output is the next role's input* dying on a transient endpoint error (→ Tier 3 #12).
- **F33 (P3) — KB meta-noise is now self-inflicted.** The loop enables `curator` (the convergence pass needs it), and the auto-curated stream — `learning` (73) + `retrospective` (23) of 144 notes — swamps the ~24 deliberate blackboard notes. F3's kickoff fix addressed agent-written noise; the curator now writes it for them (→ Tier 3 #13).
- **F34 (P2) — No cycle/generation identity.** Jobs self-narrate inconsistent iteration numbers: two developers wrote colliding "Iteration 3" note sets (the cancelled `19707fa1`'s and `011fcfce`'s, both `active`), and the loop-5 scholar called itself "Loop 2". Nothing stamps cycle number or generation into kickoffs or required note titles. Also the explicit prerequisite for any future parallel mode ([[loop_parallel_execution]]: generation leakage) (→ Tier 3 #11).

## The plan

Ordering rationale: a wedge or a silent no-op costs an entire night regardless of cost-per-token, so reliability outranks cost; cost outranks per-iteration quality because the budget determines how many iterations a night buys at all.

### Tier 1 — survive the night (prerequisite for any further test runs)

- [ ] **1. Stall watchdog (closes F25).** Extend `_sweep_tick` (`orchestrator/services/project_loop_sweeper.py:67`) with a second check for *running loops whose current job is non-terminal*: fetch last-activity = `max(timestamp)` from `agent_audit`/`llm_requests` for `current_job_id` (the orchestrator already holds an audit-DB connection for the MCP audit tools); if `processing` with no activity for `PROJECT_LOOP_STALL_MINUTES` (default 30) — or still `created`/unassigned past a dispatch timeout — **fail the job** (`error_message="stalled: no LLM/tool activity for N min"`), which flows through the normal completion fan-out: the advance counts it toward `max_consecutive_failures` and rotates on. *Fail, don't cancel*: `_advance_project_loop` derives failure from `status == "failed"`, and cancellation is reserved as the user-stop verb. Distinct layer from [[stuck_agent_recovery]] (burning calls without progress, agent-internal) and from the existing sweeper case (terminal-but-not-advanced): this is the zero-activity wedge nothing owns today. Evidence: `19707fa1` — last LLM call 06-27 17:25, cancelled by hand 06-30 18:37; three nights lost to one wedge. Surface job age + last-activity in the Loop tab while at it (the F25 "static processing badge" half).
- [x] **2. No-op compounding guard — SHIPPED, then SUPERSEDED same-day by the [[loop_repo_compounding_v2]] merge step (2026-07-03).** The SHA-compare version shipped 07-02 but was dead on arrival (Gitea `/git/commits` 404s on branch names and `get_commits` swallowed the 404 silently — fixed 07-03, endpoint corrected to `/commits`). v2 then replaced the heuristic outright: every loop job works on `job/<id>` and the advance hook **squash-merges** it onto `main`, recording literal `jobs.merge_status` = `merged` / `empty` / `merge-failed` / `skipped`. An `empty` from an execution role is the F29-family loud flag; the orchestrator-written `retros/NNN-<role>-<jobid8>.md` on `main` (with merge SHA) closes F40's KB-validating-KB loop — critics are prompted to trust `retros/` over KB self-reports. Live-verified on k3d 07-03 (all four outcomes).
- [ ] **3. Preflight at loop start.** In the start endpoint (`orchestrator/routers/project_loops.py`), before spawning iteration 0: (a) resolve the model against the catalog and fire a 1-token ping through the configured endpoint — reject with a clear 400 on failure; (b) verify the project jobs-repo exists and is reachable. Two of five loops died at exactly these seams (loop 3 DOA on keying; loop 5's critic on endpoint errors). A per-spawn re-ping is optional (endpoints can degrade mid-run) but start-time is the 80 %.
- [ ] **4. Fence the VM backend for loops (until F29 is fixed) — promoted to the plan's #1 item (run 6).** Reject `workspace_backend="vm"` in the router's validation (`routers/project_loops.py:103-113`) with an explicit "VM repo provisioning unverified for loops" error — or gate behind an env flag for testing. Lift the fence only after an E2E proves: VM boots → project repo cloned at current `main` → agent push lands on `origin/main`. Root-cause candidates for the actual fix: the VM repo-seed path (cloud-init / SSH-seeded like code-server settings) and the golden-image boot acceleration skipping seed steps. *Run-6 escalation: F29 recurred ×3 in one night — cumulative damage is four developer iterations / ~130 M tokens / zero surviving artifacts across runs 5–6. Every other item in this plan is second-order until execution output survives.*
  **→ ROOT-CAUSED (07-02 deep dive, live-verified — see F29 in [[loop_review]]):** VMs (tailnet nodes) can't resolve the cluster-internal clone URL `http://srw-gitea:3000` → exit 128 → **silent `git init` fallback** (`src/core/workspace.py:415-418, 362-372`) → all pushes fail as swallowed warnings. **The real fix is small:** dispatch-time URL rewrite in the VM block at `orchestrator/main.py:2159-2176` (swap `GITEA_INTERNAL_URL` host → external `GITEA_URL`, which already works through the Cloudflare ingress) on `git_remote_url` + `repositories_payload[*].repo_url` — fixes clone and push together. Plus hardening: a failed jobs-repo clone on a `work_on_main` role fails the job loudly. Fence `vm` only until that lands + one E2E.
  **→ RESOLVED WITHOUT FENCE (2026-07-03):** the URL-rewrite fix + fail-loud clone shipped 07-02 and run 7 (dev, VM backend) proved it live the same night — the developer iterations cloned, pushed, and `main` advanced for the first time since 06-27. No fence needed.
- [ ] **5. Mechanical proposal supersede in the advance hook (F22, promoted from prompt to mechanism — run 6).** When an analysis job completes and a new `decision` tagged `verdict` exists, the orchestrator flips the non-selected `proposal` notes of that generation to `superseded` itself — the critic prompt stays as belt-and-suspenders. Evidence: with the prompt-only fix live, **2 of 3 critics complied**; cycle-2's critic chose Proposal 004 and left 001–003 active. A coordination guarantee can't be stochastic.
  **→ Sharpened (07-02 deep dive):** it's worse — cycle-2's verdict **falsely claims** the losers were "each marked SUPERSEDED" (its 13 kb_updates were all `add_links`); cycle-1 wrote tombstone learning-notes without flipping either. Reliable selection keys for the mechanical flip (verified across 5 generations): `note_type='plan'` + slug `loop-{N}-proposal-{NNN}` + the same-cycle scholar's `job_id`, winner extracted from the verdict slug / IMPLEMENTS link. Do NOT key on `tags`/`phase` (mostly empty) or critic prose (lies).

### Tier 2 — cost (the F14 levers, re-ranked by the new data)

- [ ] **6. Per-role models via DB experts (F2, unblocked).** The plumbing already exists: `create_loop_job` resolves the role name to a DB expert (`services/project_loops.py:267-284`, since `46349bb4`), and experts carry their own `llm.model`. **Blocked today by precedence**: the loop's `config_override.llm.model` is a *request-level* override — the highest layer in the resolver (`orchestrator/services/config_resolver.py:75-88`, "most-specific wins") — so it clobbers the expert's model. Fix locally in `create_loop_job`: when the resolved expert pins `llm.model`, skip the loop-model injection; the loop-level model becomes the *default for roles that don't pin their own*. Recommended split from the evidence: strong model where output quality gates the cycle (developer, probably critic — it's the decider), cheap-but-cache-friendly for the scholar. F31 rules out M3-for-everything.
- [ ] **7. Phase/call caps for analysis roles (F1 — still the biggest single lever).** Loop 4's critic burned 11.9 M tokens / 162 calls to select one proposal; run 1's burned 4.2 M — the pathology survives model changes. Scholar/critic are single-increment analysis steps and should be 1–2-phase jobs *by construction*: a `max_phases` / call-ceiling knob in `config_override` for analysis roles (hard stop → wrap up + freeze), not a polite kickoff request. Measured expectation from run 1: ≈ −2.6 M/cycle at gpt-5.5 scale, far more at M3 scale. *Run-6 escalation: scholar #10 ran **8 phases** for 5.9 h / 35.8 M tokens — averaging 212k prompt-tokens per call (max 298k, 127 of 168 calls >150k, 31 s/call) — a context-bloat runaway that grows with the KB. The cap bounds it structurally.*
  **→ Sized (07-02 deep dive):** capping that scholar at 2 phases keeps ~6.6 M and saves **~29 M (−81 %)**; per-phase context reset with a handoff summary (compaction already builds one) saves ~70 %. Companion context fixes now filed as **F35** (tool-call *args* are never trimmed — 47–53 % of the peak prompt is the agent's own kb_write/write_file arguments; the trimmer only stubs results) and **F36** (compaction works — the developer compacted 200k→24k — but never triggered for scholar/critic at 250–298k).
- [ ] **8. Prompt-cache utilization (F14-L3 — where the money actually goes).** 7–23 % cache on 40–60 M prompt tokens/job. Harness-wide, not loop-specific, but the loop is its biggest customer: stable prefix ordering (system + kickoff + pinned KB header first, volatile tool results last), and verify the codex-proxy and OpenRouter paths forward cache headers at all. Measure via the audit DB `cached_tokens` ratio per job — the table above is the baseline.
  **→ Mechanism found (07-02 deep dive, F37):** it's **self-inflicted prefix instability, not a provider limit** — MiniMax caching works (14–16k cached on most calls) but message positions 2–6 mutate every call: the `<active_tasks>` block rewrites on todo changes and the synthetic memory/KB injection messages regenerate **random tool-call ids per call**, so the huge append-only history behind them can never cache. Fix: relocate injections + active_tasks to the message-list *tail* + deterministic ids → estimated **58–78 % cache** on the run-6 scholar (21–28 M tokens moved to the cached tier) and the main latency lever (31 s/call is prefill-dominated).
- [ ] **9. Close the accounting loop (F16 + F27 + F30).** Fix codex-proxy usage recording (F30); write `total_tokens_used`/`total_requests` back to `jobs` on completion (F16); select token columns in `list_project_loop_jobs` (`postgres.py:9270`) and render per-iteration cost + per-iteration `main` diff in the Loop tab (F27 + [[loop_repo_compounding]] observability). Prerequisite for the deferred Phase-4 token-budget stop — and for knowing whether items 5–7 worked.

### Tier 3 — quality per iteration

- [ ] **10. Pinned loop-state header (F23's lighter fix).** Always inject DoD + latest `decision`/verdict + "N proposals, M superseded — `kb_search` for details" into loop-job context; similarity fills the rest. Notes already carry `note_type` + `status`, so it's a `WHERE` clause, no schema work. Loop 5 showed agents *can* self-serve — the header removes the luck.
  **→ CONFIRMED NEEDED (07-02 deep dive — F23 settled):** across run 6's 1,414 injected turns the DoD reached **0 of 3 developers**; the same-cycle verdict reached the developer in 6.6–34 % of turns; retrospectives crowded 48–83 % of the top-5 slots. Scholars/critics repair misses by self-serve KB-archaeology; developers (10–25 reads) don't. Header contents, per the data: current verdict + DoD + current-iteration status — those three, deterministically, every role.
- [ ] **11. Cycle/generation stamping (F34).** Stamp cycle number + generation into the kickoff (`build_loop_kickoff` already gets `iteration`; add the cycle wrap) and require it in KB note titles the roles write (`cycle 2 · developer · …`). Kills the colliding-"Iteration 3" ambiguity, makes the KB legible per generation — and is the declared prerequisite for any future parallel mode.
- [ ] **12. Outcome-aware advance, minimal version (F32).** In `_advance_project_loop`: when an *analysis* role fails, retry the same role once (fresh job) before rotating on; only a second failure rotates. Keeps the unconditional-loop philosophy (no goal-gating, cap still bounds) while ensuring the developer doesn't build on a missing/stale verdict because of one transient endpoint blip.
- [ ] **13. Tame curator noise in loop projects (F33 — promoted P2, run 6).** Shorter TTLs (or lower retrieval rank) for auto-curated `learning`/`retrospective` notes in projects with an active loop, so the deliberate blackboard notes stay findable. Knobs live in the [[kb_convergence_ttl_reverification]] machinery; no new storage. *Run-6 escalation: the KB doubled 144 → 374 notes in one 10-job run (~+23/job) and directly feeds the scholar context-bloat runaway (#7) — no longer cosmetic.*
  **→ Sharpened (07-02 deep dive, F38/F39):** the curator writes ~30 % of new notes (more by bytes — 54 retrospectives = 34 % of all KB bytes) and **reconstructs agent deliverables** (the proposal double-writes were the curator racing the agent, re-deriving "Loop N Proposal" notes from plan.md). `learning`/`retrospective` are **TTL-exempt → 245 of 322 active notes are immortal** and invisible to convergence; 33-job projection ≈ 900 rows. Also **F39**: `refresh_ttl` has never stamped `last_verified_cycle` (NULL on all 374 rows; 38 notes at TTL ≤ 0, some −1) — the stale queue never drains via "keep". Concrete fix set: curator stops per-phase retros + deliverable reconstructions; TTL for learning/retrospective; loop-level singleton notes for role boilerplate; fix `refresh_ttl` (`src/services/auxiliary.py:1467`).

### Explicitly deferred / rejected

- **Parallel execution** ([[loop_parallel_execution]]) — the run data answers its own "is speed the bottleneck?" question: cost is token-dominated and the developer is the long pole; phase caps + the stall watchdog shrink wall-clock more cheaply than concurrency, without touching the KB-coordination model. Revisit only after Tier 1+2 land and F34 (generation stamping) exists.
- **Goal-met early stop** — stays rejected (F9's descope stands; the loop is unconditional by design).
- **Grounding gates** — stay rejected (F7 recast stands; the loop self-grounds, datasources are optional enrichment).

## The experiment that validates the plan

After Tier 1 (and ideally #6, per-role models): **one overnight run, pod backend, gpt-5.5-class developer, budget 9–12 jobs (3–4 cycles)**. Collect per run — these are the plan's metrics, all readable from the audit DB / Loop tab once #9 lands:

1. Did it reach budget or stop for a visible, attributed reason? (Tier 1's DoD)
2. Tokens + cache % per role per cycle (baseline table above; #7/#8's success metric)
3. `main` advanced every execution iteration? (`merge_status`, #2's metric)
4. F22: did the completed critic supersede the losing proposals?
5. Wall-clock per role (the 5 h scholar of loop 2 is the anti-benchmark)

### Run 6 scored against these metrics (2026-07-02 — pre-fix, vm + M3, so a broken-config baseline)

| # | Metric | Result |
|---|--------|--------|
| 1 | Visible outcome | ✅ operationally — 10/10 completed, no invisible deaths, clean user-pause (budget not reached: paused at 10 of 33) |
| 2 | Tokens + cache | ❌ ~55–65 M/cycle @ 4–11 % cache (~194 M total; F31) |
| 3 | `main` advanced | ❌ **0 of 3** execution iterations (F29 ×3 — all developer work destroyed) |
| 4 | F22 supersede | 〜 partial — 2 of 3 critics complied → mechanical fix (Tier 1 #5) |
| 5 | Wall-clock | critics 28–41 min ✅ · devs 93→180 min · scholar #10 **5.9 h runaway** ❌ (#7's target) |

The pod-backend + per-role-model rerun after Tier 1 remains the plan's actual validation run; run 6's value is that it isolates the failures to exactly the planned fixes — nothing unexplained happened in 10 jobs.

## Related

- [[loop_review]] — the findings registry; F29–F34 indexed there, defined here.
- [[project_self_improvement_loop]] — the parent design; its Phase-4 deferrals (token-budget stop, scholar fan-out) sit behind Tier 2 #9 and the parallel deferral respectively.
- [[loop_repo_compounding]] — the artifact keystone; Tier 1 #2 implements its deferred no-op guard, F29 breaks its VM assumption.
- [[loop_parallel_execution]] — deferred; F34 is its prerequisite.
- [[stuck_agent_recovery]] — the agent-internal stuck layer; Tier 1 #1 is the orchestrator-side zero-activity layer above it.
- [[kb_convergence_ttl_reverification]] — the KB convergence machinery Tier 3 #13 tunes.
