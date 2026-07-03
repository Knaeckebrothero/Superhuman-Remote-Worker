---
tags:
  - issue
  - delegation
  - subagents
  - spawn_subagent
  - project-loop
  - forensics
  - cost
  - evaluation
aliases:
  - subagent fan-out forensics
  - spawn_subagent post-adoption forensics
  - loop iter-13/14/15 subagent deep-dive
related:
  - "[[subagents_never_used]]"
  - "[[delegation_light_mode_missing]]"
  - "[[scholar_delegation_not_exercised]]"
  - "[[loop_run6_deep_dive_forensics]]"
  - "[[loop_optimization]]"
  - "[[loop_review]]"
  - "[[project_self_improvement_loop]]"
---

# `spawn_subagent` fan-out — first post-adoption forensics (loop iter 13/14/15) + field best-practice reconciliation

**Date:** 2026-07-03
**Status:** **Analysis / findings.** The `spawn_subagent` light fan-out is **LIVE and working** on the
dev cluster; this doc is the first forensic evidence that the adoption fix from
[[subagents_never_used]] actually landed, benchmarked against external deep-research /
multi-agent best practice, with a prioritized improvement backlog. No code changed here —
it produces the Phase-5b measurement + a recommendation list.
**Component:** `src/tools/delegation/spawn_subagent.py` (+ `light_runner.py`, `reader_env.py`,
`src/core/backends/subdir.py`), `config/experts/scholar/*`, `config/experts/critic/*`,
`orchestrator/services/project_loops.py` (`_ROLE_BLOCKS`), the audit DB (`llm_requests`,
`call_type='subagent'`).

---

## 0. Headline (and a stale-docs correction)

The newly-enabled `spawn_subagent` fan-out **works as designed** in the two roles that have it,
and the pattern it implements matches what Anthropic / Cognition / the OSS deep-research systems
independently converged on. **The topology is validated.** The remaining work is cost-routing,
closing the one role that still never delegates, and a handful of efficiency / verification
hardening items — most of them prompt/config-level.

**Correction to what the sibling docs currently say.** [[subagents_never_used]] and
[[delegation_light_mode_missing]] still describe the light feature as *"uncommitted on develop,
not k3d-e2e-verified (Phase 6), not deployed."* That is **stale**. Git shows it shipped and
deployed:

- `e65d5e32` — *"Enable subagent delegation with `spawn_subagent` and refactor expert prompts and configs"*
- `a4596376` — *"deploy: update image tags to sha-e65d5e3"*

The jobs analyzed below **are** the production evidence that [[subagents_never_used]] §"How to
measure adoption" asked for. **Adoption is demonstrated.** The *quantitative cost measurement*
(Phase 5b) that was owed here is now **done — see §7** (measured 2026-07-03). Headline: adoption
proven & metered, topology validated; the raw-token target (35.8M→10–15M) did **not** land on
n=3, but real-world signal (cache ~35%, jobs bounded to ~300 steps with no runaways, parallel-read
speed) says the early win is speed + boundedness — token-cost tuning is rightly deferred to scale.
The status lines in both sibling docs are updated to match.

---

## 1. Forensics — what the three roles actually did

All three jobs are one continuous loop (loop 4, iterations 13→15) building an ERP for Hotel
Rheinland (Bad Orb), model **MiniMax-M3**, rotating SCHOLAR → CRITIC → DEVELOPER. The loop
**closes end-to-end this cycle**: scholar proposed four Tier-1 axes, the critic selected L13-004,
and the developer is now implementing L13-004. That coherence is itself a good sign.

### 1.1 SCHOLAR iter-13 (`df0f519a-d530-425f-a244-2a1f687d7b2d`) — 5 subagents, GATHER fan-out — **strong**

| Batch | Spawns (one LLM turn) | Split (axes) | Verdict |
|---|---|---|---|
| Phase 13.1 | 3 × `scholar` (audit 264/265/266, doc 14333) | regulatory / own-hotel-ops / competitor | cleanly orthogonal ✓ |
| Phase 13.2 | 2 × `scholar` (audit 399/400, doc 14398) | fiscal (Kassen/DATEV) / re-verify subagent A | D distinct ✓; **E low-value** |

- **Genuine single-turn parallelism** both times (all spawns emitted in one assistant turn, not
  gated on each other). Self-contained briefs (sources + expected return format). Strict
  **1 subagent → 1 KB evidence-note** persistence. Disciplined synthesis: a rolling per-axis
  verification-status note, a DoD-coverage map, and a self-verification checklist before
  `job_complete`. Final output = 4 differently-verified Tier-1 proposals (L13-001 BFSG-WCAG-AA
  accessibility [FULLY VERIFIED]; L13-002 NIS2 + DSGVO Art.28 supply-chain [PARTIAL]; L13-003
  Kassen-Revolution/DATEV fiscal [WEAK→upgraded]; L13-004 channel-manager MinStay 2-way sync
  [CONFIRMED]). This is textbook lead-synthesizes-isolated-readers.
- **Weakest spawn:** Subagent E re-verified subagent A's own *unverified* subset one phase later
  and returned "heavily budget-constrained"/thin output (audit ~415) — lowest marginal value of
  the five, and a soft cross-phase overlap.
- **The real inefficiency was NOT the delegation.** The fan-out phases ran clean (zero failed
  spawns). The churn lived in the serial tail: repeated `edit_file` "old_string not found" →
  hex-dump debugging of a **1665-line plan.md** (audit ~724/739/746/750/754), plus
  `next_phase_todos` "too few todos" and `todo_complete`/`kb_read` not-found retries. Bookkeeping
  friction inflated the ~3h45m / 955-entry run.

### 1.2 CRITIC iter-14 (`58ebf879-ecca-418c-b7e5-8c95e82717fc`) — 4 subagents, VERIFY fan-out — **strong**

- 4 × `critic-scholar-mode` in **one turn** (audit 115–118, doc 14581), one Tier-1 proposal each
  (L13-001..004), **disjoint targets**, genuinely concurrent (~90 s wall, 03:58:32→04:00:02) vs
  an estimated ~4× sequential. Each reader ran in an isolated ~9k-token context and scored its
  proposal against a shared 6-criterion rubric → per-criterion scorecard + severity counts +
  graded verdict. Parent normalized via an explicit **HARD-PASS-count** rule and selected L13-004,
  then wrote 4 per-axis learning notes + verdict + superseded losers + retrospective + review
  report. **Isolated contexts reduce cross-proposal anchoring/halo** — a real advantage of
  verify-fan-out over scoring all four in one window.
- **Gaps (all fixable):**
  1. **Single verifier per proposal** — "independent verification" here means independence
     *across* proposals, not a second opinion on any *one*. A wrong scorecard is only caught by
     the parent's cross-axis normalization, not a peer. No N-of-M consensus on borderline items.
  2. **Reconcile step overclaims.** Its todo says "direct re-read of cited KB plan-note sections,"
     but the reconcile turn (doc 14594) shows **no fresh `kb_read`** — the parent trusted the
     scorecards. Weakens the stated anti-confirmation-bias posture (a MAST task-verification
     failure shape). Because targets were disjoint, the "reconcile contradictions" machinery had
     nothing to reconcile and degraded to a no-op.
  3. **Redundant shared reads** — all 4 readers independently `kb_read` the same DoD /
     resavio-pain-points / shared evidence anchors (~4× duplicated I/O).
  4. **Prompt bloat** — the fixed 6-criterion rubric is re-embedded in every one of the 4 briefs
     (~5.4k spawn-turn completion tokens) instead of living in the `critic-scholar-mode` config
     system prompt.
  5. **Orchestrator context bloat scales with width** — the reconcile turn absorbed all 4
     *verbatim* returns → a **184,673-token** parent prompt. Light mode returns reader text inline
     as ToolMessages, so wide fan-out inflates the parent window. This is exactly the failure the
     external practice warns about (compress-before-return; persist a plan/index).

### 1.3 DEVELOPER iter-15 (`6dc25a0d-2356-451c-bc07-d0f6264b862f`) — 0 subagents — **the gap**

- **Not stuck** (initial appearance was a false alarm): the `0.0%` progress gauge is simply not
  wired for loop jobs. The job is productive — on phase 4, committed real code to `main`
  (`779fac1a`, a 371-line `test_channel_manager_minstay.py`), i.e. **implementing L13-004, the
  critic's winner**.
- But it **never fans out** (0 `spawn_subagent` calls). Only the `scholar` and `critic` expert
  configs override to `delegation.mode: light` + grant `spawn_subagent`. The developer sits on the
  global heavy default, where `spawn_subagent` is a stub and the only real path is heavy
  `delegate_work` — which has **0 invocations fleet-wide, all-time** (per [[subagents_never_used]]).
  So the loop's *implement* phase is entirely zero-parallelism, even for read-only exploration
  (understand N code areas / gather N call-sites) that has no merge conflict risk.

> ⚠️ Provenance note: the workflow subagent tasked with the developer forensics failed
> (StructuredOutput retry cap). §1.3 is reconstructed from direct orchestrator scouting
> (`get_job_progress`, `get_job_summary`, `search_audit spawn_subagent`) + the design side, not a
> full audit-trail reconstruction like §1.1/§1.2. A full re-audit is cheap if wanted.

---

## 2. Field best practice — what deep-research / multi-agent systems do

### 2.1 Fact-checked (8/8 Anthropic claims → `supported` against the primary source)

Source: Anthropic, *How we built our multi-agent research system*
(`anthropic.com/engineering/built-multi-agent-research-system`). Each verified against the primary
text by an adversarial fact-check pass:

- **Orchestrator-worker + isolated per-worker context windows** (Opus-4 lead + Sonnet-4 subagents)
  beat single-agent Opus 4 by **90.2%** on Anthropic's internal research eval; parallelization
  cut research time **up to 90%** on complex queries.
- **Token spend alone explains ~80% of eval-performance variance** (95% with tool-call count +
  model choice). The multi-agent win comes *mainly from spending tokens across many fresh
  windows*.
- **Cost:** agents ≈ **4×** chat tokens; multi-agent ≈ **15×**. Only pays off for high-value,
  genuinely parallel, over-one-context-window tasks.
- **Complexity→width ladder** (baked into the lead's prompt): simple = 1 agent / 3–10 tool calls;
  comparison = 2–4 subagents / 10–15 calls each; complex = 10+; default 3–5 in parallel. Without
  it, early versions **spawned 50 subagents for trivial queries** (over-decomposition anti-pattern).
- **Self-contained briefs** (objective / output format / tool guidance / hard boundaries) were the
  single biggest fix for duplicated work and coverage gaps; a bare topic string caused subagents
  to duplicate searches.
- **Small (~20-query) golden set** surfaces large behavioral swings early; prompt/tool-description
  quality alone moved success and cut task time ~40%.
- **Synchronous fan-out** (lead waits per batch) is the pragmatic, reliable default — Anthropic
  deliberately deferred async coordination.

### 2.2 Directionally sound but NOT independently verified — cite with care

These came from the coordination/failure-modes research angle, which the verify pass did **not**
cover (the fact-check only ran over the 8 Anthropic claims above). Treat the *direction* as sound,
the *specific numbers* as unconfirmed:

- MAST "**~32%** of multi-agent failures = inter-agent misalignment" and "**up to 86.7%** of runs
  fail, mostly context-sharing" (MAST paper `2503.13657` is real; the 86.7% via a secondary blog).
- Financial-doc benchmark: "naive parallel = least token-efficient; reflexive-verify best F1
  **0.943** at 2.3× sequential cost; single-agent 15–17k vs multi 56–136k tokens/query"
  (arXiv `2603.22651` — unverified).
- Agent-Oriented Planning: "decompositions overlap on **>15%** of queries without a
  completeness/non-redundancy detector" (arXiv `2410.02189` — unverified).
- A few arXiv IDs in the `2603.*` / `2601.*` range look plausibly real but were not checked.
- Cognition, *Don't Build Multi-Agents* (**real**): keep writes single-threaded — "conflicting
  decisions carry bad results"; parallelism for read/gather breadth only.

Separately, the interface-design report the light-mode feature originally leaned on
(`Subagent Delegation Interface Design.md`) contains **known-synthetic** specifics (DeepSeek "128
parallel calls," Kimi "300 subagents") — its directional advice is fine, its numbers are not.

---

## 3. Where SRW already aligns (do NOT rebuild)

SRW is closer to best practice than the "delegation never fires" history implied:

- **Isolated per-reader context** (fresh 2-message list, never the parent's history).
- **Single-threaded synthesis** — readers return strings, the lead authors output. Matches
  Cognition's "parallel reads, single writer" and Anthropic's "lead synthesizes."
- **Self-contained briefs** already deliberately authored (sources + return format).
- **Decision-mandatory adoption** — the todo scaffold forces an explicit fan-out/sequential
  decision at planning time (`next_phase_todos` → plan.md), the exact structural fix for
  over-spawn. It mandates a *decision*, not fan-out (avoids garbage fan-out on non-separable tasks).
- **Synchronous fan-out** — the parent-wait pattern is intended design, not a bug.
- **Reader-spend metering** — reader LLM calls archive as `call_type='subagent'` under the parent
  job and fold into `usage_events`.
- **The cheap-reader tier (`llm.subagent`) is already built** — full `PhaseLLMOverride`, fallback
  `subagent → tactical → base`, excluded from `has_phase_overrides()` so it stays off the main
  graph.

---

## 4. Recommendations (prioritized)

### Tier 1 — do now (cheap + high impact)

| # | Change | Effort | Impact | Where |
|---|---|---|---|---|
| T1.1 | **Route readers to the cheaper `llm.subagent` tier** — set the config value for scholar + critic (and defaults). Tier + fallback already built; **config-only, no code**. The Opus-lead/Sonnet-reader cost split — the single biggest lever — and MiniMax is a documented false economy (F31). | low | high | `config/experts/{scholar,critic}/config.yaml`, `config/defaults.yaml` `llm.subagent` |
| ~~T1.2~~ ✅ | **Phase-5b cost measurement — DONE 2026-07-03 (§7).** Measured: scholar iter-13 45.2M / critic 12.7M / developer 52.7M+ (parent ~180k avg, subagents 2.5–5% of spend). Raw-token target (35.8M→10–15M) not hit on n=3 — but cache ~35%, steps bounded ~300 (no runaways), speed win. Token-cost tuning deferred to ~100-job scale. | medium | high | audit DB `llm_requests` |
| T1.3 | **Enable read-only light fan-out for the DEVELOPER** — `mode: light`, `spawn_subagent`, `allow_writes: false`. Parallel code/context *reads*, single-threaded writes (Cognition's thesis). Closes the zero-fan-out role without touching the deferred heavy/merge path. Mirror the scholar/critic `_ROLE_BLOCKS` sentence + todo-scaffold decision. | medium | high | `config/experts/developer/config.yaml`, `orchestrator/services/project_loops.py` `_ROLE_BLOCKS`, developer `strategic_todos_initial.yaml` |

### Tier 2 — efficiency (low effort, medium impact)

- **T2.1 Complexity→width ladder** in the strategic prompts + the plan.md fan-out-decision row,
  scaled *down* from Anthropic's 1/2-4/10+ to SRW's single-subscription budget (~6-7 agents; cap
  ≈ `max_parallel`=3). Add "emit all orthogonal sub-questions in ONE batch; follow-up spawns only
  for genuine dependencies" — kills the observed 3+2 split and the low-value re-verify (E).
- **T2.2 Pre-dispatch completeness/non-redundancy self-check** on the decomposition (a one-shot
  think step: "do any two overlap? is anything uncovered?") before spending parallel tokens.
- **T2.3 Pre-fetch shared context once + move fixed rubrics into config system prompts** — parent
  fetches DoD/pain/anchors once and inlines the slice into each brief (~4× → 1× reads); the
  critic 6-criterion rubric moves into `critic-scholar-mode` system prompt. Keep the flat scalar
  `task_description` schema (the weak-JSON-fleet decision stands — carry objective/boundaries/
  return-format as prose inside it).
- **T2.4 Compress-before-return contract** — enforce a compact `expected_return_format` so the
  parent absorbs a distilled index, not verbatim dumps. Caps the 184k reconcile-turn bloat that
  worsens with width. Reconciles with the design (still a string via ToolMessage, no merge).

### Tier 3 — verification & ops hardening (medium)

- **T3.1 Critic verification hardening** — make reconcile actually re-read the *winner's* cited
  sections (spot-check); add a second verifier **only for the 1–2 borderline** proposals
  (2-of-2 consensus), gated to respect the ~2.3× reflexive-verify premium — don't double-verify
  clear passes.
- **T3.2 Silent-subagent-failure detection** — a thin/empty/bounded-stop return (like Subagent E)
  must be flagged and re-spawned narrower or marked uncovered, never silently counted as "done"
  (MAST task-verification failure mode). One line in the synthesize todo.
- **T3.3 Auditability** — have the 1:1 synthesize step (or the light backend) persist each reader's
  raw findings + cited sources as its own KB note keyed by sub-question (the `[subagent done]`
  echoes hide all content); and **fix the `get_knowledge_note` / `list_knowledge_notes` "Unknown
  format code %" crash** blocking direct reads of the distilled notes. Citations already flow to
  the shared job-scoped CitationEngine under the parent job_id.
- **T3.4 Small (~20) golden-set eval** for scholar/critic prompt changes via the shipped
  `PROMPT_DB_OVERRIDES` mechanism, inspecting full traces (not just end-state) for coordination
  pathologies (overlap, over-spawn, thin returns).
- **T3.5 Split the monolithic 1665-line plan.md** into per-iteration files — kills the
  `edit_file`/hex-dump churn that dominated scholar's serial tail (efficiency, not fan-out).

---

## 5. Risks / caveats to weigh

- **Delegation compounds with, but does not replace, the loop's cache/cost bugs** — F35 tool-arg
  bloat (47–53% of peak prompt) and F37 prefix instability. **Update 2026-07-03: prefix cache is
  now ~35%** (MiniMax dashboard, recent days), not the 4–11% snapshot this bullet assumed — the F37
  lever is already substantially realized, so the gross token totals in §7 overstate billed-fresh
  cost. The measurement (§7) confirms fan-out alone doesn't cut raw tokens (subagents 2.5–5% of
  spend; parent still ~180k avg), so the 35.8M→10–15M target stays gated on T2.4 compress-return +
  F35 arg-trim + fewer parent turns — **deferred to ~100-job scale** so the hurdles show
  empirically. What fan-out *did* buy immediately: bounded ~300-step horizons (no runaways) +
  parallel-read speed ([[loop_optimization]] Tier 2).
- **Parallel-reads over a single locked SSH `RemoteBackend` is unmeasured under real load** — only
  LLM/web parallelize; fs/git/shell ops serialize behind the backend lock. If paramiko channels
  serialize badly at `max_parallel`, widening fan-out may disappoint until measured; the escalation
  (per-reader connection) is heavier and deferred.
- **Cheaper reader model trades cost for quality** — a too-weak `llm.subagent` on the weak-JSON
  fleet could mis-format tool calls or degrade evidence. Validate on the golden set (T3.4) before
  defaulting to the cheapest tier.
- **`_MeteredLLM` is the reader's ONLY metering path** — a wrapper regression = silent unmetered
  spend leak. Wider fan-out (developer enablement + cheaper readers encouraging more spawns) raises
  the blast radius; pin metering with a regression test before scaling width.
- **Real overnight cost validation is gated on infra** — the VM workspace backend is fenced for
  loops (F29 internal-Gitea-URL breaks artifact compounding) and the LLM gateway is dark on dev
  (no token-budget stops; codex-proxy jobs record NULL usage).
- **Budget ceiling** — second-opinion verifiers + wider developer fan-out add token cost against a
  hard single-subscription budget (~6-7 concurrent agents). Keep the ladder capped well below
  Anthropic's 10+.

---

## 6. Method & provenance

Produced by a background multi-agent workflow (`subagent-delegation-deep-dive`) — 16 agents, 15
succeeded, **1 failed** (developer forensics, StructuredOutput retry cap → §1.3 backfilled from
direct scouting). ~733k subagent tokens, 134 tool uses, ~510s wall.

- **Internal forensics** (3 agents): scholar iter-13, critic iter-14 via full audit-trail
  reconstruction (`search_audit` / `get_audit_trail` / KB notes); a design-vault reader over the
  sibling docs + `spawn_subagent` implementation.
- **External research** (3 agents): Anthropic multi-agent system; production + OSS deep-research
  architectures (OpenAI/Gemini/Perplexity Deep Research, open_deep_research, GPT-Researcher,
  STORM); coordination/context/failure-modes (Cognition, MAST, LLM-as-judge patterns).
- **Verification** (8 agents): adversarial fact-check of the 8 Anthropic quantitative claims — all
  `supported`. §2.2 numbers were **not** put through this pass; flagged accordingly.
- **Synthesis** (1 agent) + human reconciliation against git history (the stale-docs correction in
  §0) and live re-check of the developer job (the "not stuck" correction in §1.3).

**Jobs analyzed:** scholar `df0f519a-…`, critic `58ebf879-…`, developer `6dc25a0d-…` (dev cluster,
loop 4, iters 13/14/15, MiniMax-M3, 2026-07-03).

---

## 7. Phase-5b measurement — result (2026-07-03, DB-verified)

Ran T1.2 against the three jobs above via the audit DB (`llm_requests`, per-row token usage;
`iter=?` rows = `call_type='subagent'`/aux, `iter=N` = parent main-loop). Numbers are DB-verified
and arithmetic-consistent.

| job (loop 4, MiniMax-M3) | rows | total | parent | p-calls | p-avg | subagent | sub % | peak | parent prompt first10→last10 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| scholar iter-13 · 5 sub · ✅ | 290 | **45.2M** | 42.7M | 237 | 180k | 2.26M | 5.0% | 411k | 27k → 121k |
| critic iter-14 · 4 sub · ✅ | 80 | **12.7M** | 12.3M | 68 | 181k | 0.32M | 2.5% | 274k | 43k → 269k |
| developer iter-15 · 0 sub · ⏳ live | 307 | **52.7M+** | 52.6M | 307 | 171k | 0 | 0% | 262k | 36k → 165k |
| *baseline* run-6 scholar #10 · 0 sub | *169* | *35.8M* | *35.8M* | *169* | *212k* | — | — | — | *(212k climb)* |

**What the raw totals say:** the naive "35.8M → 10–15M via fan-out alone" did **not** materialize
on n=3. Subagents are only 2.5–5% of spend; the parent context is 95%+, and its per-call prompt is
~171–181k across all three roles **whether or not** they fan out, climbing 4.5–6× within each job.
On raw tokens, fan-out did not shrink the parent.

**What the operational signal says (rebalances the above — 2026-07-03, MiniMax dashboard + loop
step counts):**

- **Prefix cache is ~35% over recent days**, not the 4–11% snapshot §5 assumed. The F37 lever is
  already substantially realized, so effective *fresh*-token cost sits well below the raw prompt
  totals in the table — the 45.2M/52.7M figures are gross, not billed-fresh.
- **No runaways: jobs land at ~300 steps, not the pre-adoption 600–900.** Fan-out is buying
  **horizon-boundedness + wall-clock speed** (parallel isolated reads) even where it doesn't cut
  raw tokens — a structural win the token totals don't capture.
- **n=3, feature days-old, a handful of jobs.** Not enough to fix a cost ceiling.

**Verdict (revised).** Adoption is healthy and metered; the topology is validated (§0–3); cache and
step-boundedness are already good. Fan-out's value shows up as **speed + no-runaway + isolation**,
not (yet) as a raw-token cut — and that's fine this early. **Token-cost optimization (T2.4
compress-return, F35 arg-trim, fewer parent turns; T1.1 cheaper reader is a ~5% / quality lever,
not the cost fix) is deferred to a data-rich point — revisit once ~100 research jobs are stacked**
so the real hurdles show empirically rather than over-fitting three runs. The 35.8M→10–15M sizing
in [[subagents_never_used]] over-attributed the win to fan-out; corrected here and there.

*Method: kubectl to the dev cluster is Rancher-unauth (403), so measured via MCP `list_llm_requests`
pagination + deterministic re-parse; the developer job was still processing at capture (52.7M is a
floor). Raw per-row captures retained out-of-tree.*

---

## References

- **Adoption fix this validates:** [[subagents_never_used]] (0-invocation evidence + the 6-part
  adoption package; §"How to measure adoption" = the Phase-5b queries T1.2 runs).
- **Feature design + full build plan (Phases 0-6):** [[delegation_light_mode_missing]].
- **Prior single-job evidence:** [[scholar_delegation_not_exercised]]; **prior forensics method:**
  [[loop_run6_deep_dive_forensics]].
- **The loop that consumes delegation + why fan-out matters (cost):**
  [[project_self_improvement_loop]], [[loop_optimization]], [[loop_review]] (F42).
- **Code:** `src/tools/delegation/spawn_subagent.py`, `light_runner.py`, `reader_env.py`;
  `src/core/backends/subdir.py`; `src/core/loader.py` (`DelegationConfig`, `llm.subagent`);
  `config/experts/{scholar,critic}/config.yaml`; `orchestrator/services/project_loops.py`.
- **Ship commits:** `e65d5e32` (enable spawn_subagent), `a4596376` (deploy sha-e65d5e3).
- **External:** Anthropic, *How we built our multi-agent research system*
  (anthropic.com/engineering/built-multi-agent-research-system); Cognition, *Don't Build
  Multi-Agents* (cognition.com/blog/dont-build-multi-agents); Cemri et al., *Why Do Multi-Agent LLM
  Systems Fail?* (MAST, arXiv 2503.13657); open_deep_research (github.com/langchain-ai/open_deep_research).
