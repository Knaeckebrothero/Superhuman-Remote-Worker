---
tags:
  - agent-architecture
  - prompting
  - context-management
  - cost-optimization
  - evaluation
---

# Caveman Mode — Output Compression (+ Deferred Reasoning Compression)

> **Status**: Design, 2026-06-28. **No code yet.** Two-part feature.
> **Part 1 — Caveman output toggle** *(ready to plan/build)*: an opt-in, per-job and per-session switch that appends a vendored ["caveman" directive](https://github.com/juliusbrussee/caveman) to the agent's system prompt so spoken output is terse — cutting *output* tokens while byte-preserving code, commands, and structured output. Default **OFF**; ships safely because it is opt-in.
> **Part 2 — Reasoning compression (Chain-of-Draft)** *(DEFERRED, measure-first)*: extending compression to the *reasoning/thinking* tokens. Real research area, but accuracy-affecting and the wrong default — gated behind the A/B evaluation below before any build.
> Builds on the orchestrator-resolved frozen-config substrate, the experts/persona-fencing layer ([[global_expert_management]]), the skills layer ([[agent_skills]]), and the usage ledger ([[usage_dashboard]]).

## TL;DR

- **Caveman** is an MIT-licensed Claude-Code-style skill (77k⭐) that instructs the agent to reply in compressed "caveman speak" — drop filler/articles/pleasantries, fragments OK, keep technical substance. ~65% *output*-token reduction on chatty prompts. Its own docs are explicit: it shrinks the **mouth, not the brain** — thinking/reasoning and input tokens are untouched.
- **Part 1** wires it as a clean `output_modifiers.caveman = {enabled, level}` flag injected at the single shared system-prompt assembly point (`src/core/loader.py`), which covers jobs *and* sessions. No DB migration. Levels `lite`/`full`/`ultra` (skip `wenyan`). Thin Cockpit toggle. Default off.
- **Honest expectation:** savings on SRW's code/agent-heavy jobs will be **well below the 65% headline** — output-only, and much agent "output" is byte-preserved code/tool-calls; the big input cost (system prompts, file contents, tool results) is untouched.
- **Quality is the open question**, which is the whole point: SRW has no automated per-job quality score today, so validating "does caveman make output worse?" requires a small **LLM-judge A/B harness** (the shared validation section below).
- **Part 2 (CoD)** is deferred because compressing *reasoning* has a **real accuracy tradeoff** (worse on hard problems; adaptive-by-difficulty beats fixed-terse; prompt-based budgets are unreliable; code tasks are the hard case). On hosted reasoning models you can't even prompt-shape hidden thinking — the only reliable lever is the `reasoning_effort`/thinking-budget knob. So CoD becomes **eval arms first**, a separate `concise_reasoning` modifier only if the data justifies it.

## Motivation

SRW runs a lot of LLM tokens across jobs and sessions, and output verbosity is pure overhead when the user wants the *answer*, not the prose around it. Caveman is a battle-tested, drop-in prompt technique for trimming that overhead. Two reasons it's attractive here:

1. **Cost.** Opt-in output compression is a low-risk lever — it changes wording, not work.
2. **It's measurable.** Because caveman only touches *output* tokens, and the SRW ledger records `completion-token` separately from `prompt-token`, we can quantify its effect honestly rather than guessing.

The standing worry — voiced as the reason this was never adopted personally — is that terseness *degrades output quality*. That worry is legitimate and is exactly what the A/B evaluation is built to answer. Shipping the toggle **off-by-default** lets us adopt the lever without betting on it, then let data decide where to use it.

## Background: what caveman is (and the mouth-vs-brain split)

[`juliusbrussee/caveman`](https://github.com/juliusbrussee/caveman) (MIT) ships a one-page `SKILL.md` directive. Core rules: drop articles / filler / pleasantries / hedging; fragments OK; short synonyms; **code blocks unchanged, error strings quoted exact, technical terms verbatim**; preserve the user's dominant language (compress *style*, not *language*); no self-reference. It has two built-in safety sections we get for free by vendoring it verbatim:

- **Auto-Clarity** — drop terseness for security warnings, irreversible-action confirmations, ambiguous multi-step sequences, and clarification requests.
- **Boundaries** — commits/PRs/code written normally; revert on "normal mode".

Levels: `lite` (no filler, keep grammar), `full` (default caveman), `ultra` (abbreviate prose words only — never code symbols), and `wenyan-*` (classical Chinese; **out of scope** — meme/special-case that changes language).

> **Crucial caveat, from caveman's own docs:** *"Caveman only affects output tokens — thinking/reasoning tokens untouched. Caveman no make brain smaller. Caveman make mouth smaller."* This is the line that separates Part 1 (safe) from Part 2 (risky), and the reason caveman deliberately never touches reasoning.

---

## Part 1 — Caveman output toggle  *(ready to build)*

An opt-in, per-job and per-session output-compression switch. Default OFF.

### Config schema (no DB migration)

Rides the existing flexible `config_override`:

```jsonc
"output_modifiers": {
  "caveman": { "enabled": true, "level": "full" }   // level ∈ {lite, full, ultra}
}
```

- **Default**: absent ⇒ disabled. `level` defaults to `full`. Validate the enum; ignore unknown levels (forward-compat for `wenyan`/future).
- **Jobs**: arrives via `JobStartRequest.config_override` (`src/api/models.py:278-281`) → deep-merged in the agent (`src/agent.py:1556-1590`).
- **Sessions**: lives in persistent-thread `config_override` built at create/prepare (`orchestrator/main.py:14968-15043`), stored in metadata, re-injected on attach/resume.
- Both paths converge in the orchestrator config resolver (`orchestrator/services/config_resolver.py:110-128,160`) into the frozen resolved-config blob.

### Directive injection (the load-bearing change)

- **Vendor the upstream `SKILL.md` text verbatim, per level**, into SRW (e.g. `src/core/prompts/output_modifiers/caveman_{lite,full,ultra}.md`), each with a header carrying **MIT attribution + source URL + pinned upstream commit**.
- Append the selected level's block at the **single shared assembly point** — `src/core/loader.py::get_phase_system_prompt()` (assembly spans ~3539-3709; append near the final return at ~3709). This one function feeds **both** interactive (session) and worker (job) prompts (skills menu renders here too: ~3617-3621 / ~3689-3693).
- **No fencing.** Unlike untrusted DB personas (`src/core/expert_resolution.py:131-160`), this is trusted vendored text, so it is appended as a plain directive — not wrapped in `fence_persona()`.
- Keep upstream's *Auto-Clarity* + *Boundaries* sections intact: they are our built-in correctness guardrails.

### UI (thin v1 — iterate from the rendered page)

- A "Caveman mode" toggle + `lite/full/ultra` dropdown in the **job settings** form and the **session settings** panel.
- Default off; "Experimental" tag; tooltip: *"Compress the agent's spoken output to save tokens. Code, commands, and structured output are preserved. Savings are output-only."*
- **Out of v1**: user-level / global defaults (fast-follow once the eval validates the feature).

### Observability (free now; enables the eval)

- The flag is persisted in the job/session `config_override` ⇒ we already know which runs were caveman, queryable after the fact.
- `usage_events` records `completion-token` and `prompt-token` as **separate rows** (`orchestrator/database/migrations/audit/0002_usage_events.sql:46-154`, `unit` column) ⇒ per-`ref_id` output-token Δ is measurable via `GET /api/usage?ref_id=<id>` (`orchestrator/services/usage_ledger.py::query_usage` 243-307).
- *Phase-2 nicety (deferred):* stamp `details.caveman_level` on usage rows for cleaner grouping.

### Testing (v1)

- **Unit**: config parse/merge; correct block appended per level; clean no-op when disabled/absent; enum validation.
- **Snapshot**: system prompt contains the directive when enabled and omits it when off — across **both** job and session paths.
- **Guard test (correctness)**: a job with caveman ON still emits valid tool calls and citations — the directive is prose-only and must not alter structured/tool output.

### Risks specific to Part 1

- **Modest real savings** — output-only; reasoning + input untouched; agent output is partly byte-preserved code/tool-calls. Set expectations in tooltip/docs; do not market the 65% headline.
- **Quality risk on downstream consumers** — terser intermediate narration could degrade summaries, knowledge notes, or critic handoffs that read the agent's prose. v1 is opt-in so the operator controls *where*; the A/B eval must measure *task* quality, not just single-answer quality.

---

## Part 2 — Reasoning compression (Chain-of-Draft)  *(DEFERRED — investigate/test first)*

"Can we extend caveman to the *reasoning* tokens too?" Yes — it's an active research area ("efficient reasoning" / "concise CoT") — but it is **not** the near-free win that output compression is, so it is deferred behind the A/B evaluation. **No build until the data justifies it.**

### Prior art (it's been tried)

- **Chain of Draft (CoD)** — the direct analog: keep each reasoning step to **≤5 words**; reports up to **92.4% fewer words / ~7.6% of CoT tokens with comparable accuracy** on math. ([arXiv 2502.18600](https://arxiv.org/abs/2502.18600))
- **Token-Budget-Aware Reasoning (TALE)** — budget in the prompt; **~67% fewer output tokens, ~59% cheaper**, competitive accuracy. ([arXiv 2412.18547](https://arxiv.org/abs/2412.18547))
- **BudgetThinker** — training-based budget control via control tokens. ([arXiv 2508.17196](https://arxiv.org/pdf/2508.17196))
- Native hybrid long/short thinking modes now ship in Qwen3, Kimi k1.5, GPT-5; plus `reasoning_effort` / thinking-budget knobs (o-series, Claude extended thinking).

### Why it's deferred (the caveats)

1. **Real accuracy tradeoff on hard problems.** "When More is Less" finds an **optimal *intermediate* CoT length** — both too-short and too-long underperform; in most settings shorter ⇒ worse. ([arXiv 2502.07266](https://arxiv.org/abs/2502.07266))
2. **Adaptive beats fixed-terse.** Winning pattern is *"fast on the easy, deep on the hard"* — brevity penalty scaled by difficulty. A global "always think terse" flag is the naive thing the field moved past. ([arXiv 2506.10446](https://arxiv.org/html/2506.10446v1))
3. **Prompt-based budgets are unreliable** — "directly inserting budget constraints into prompts often fails to reliably control length"; even SFT/RL struggle to enforce it.
4. **Code is the hard case — and that's us.** "Chain of Draft for Software Engineering" shows CoD's gains **don't transfer cleanly to code**. SRW is code/agent-heavy ⇒ assume the cautious end. ([arXiv 2506.10987](https://arxiv.org/html/2506.10987))

### The hidden-vs-visible-reasoning fork (decides what's even possible)

- **Hosted reasoning models** (Claude extended thinking, o-series, GPT-5): the reasoning tokens are **hidden** — you cannot prompt-shape them, and instructions aimed at the hidden scratchpad are unreliable/ignored. The only reliable lever is the **`reasoning_effort` / thinking-budget knob** (already carried on SRW's model-catalog row — see [[db_backed_model_catalog]]). "Caveman for reasoning" here = *turn the knob down*, not inject text.
- **Visible-CoT models** (DeepSeek-R1-style in-band thinking, or non-reasoning models doing CoT in the answer): here a Chain-of-Draft directive genuinely shrinks reasoning and is implementable as a prompt.

### Future shape (only if validated)

- A **separate** `output_modifiers.concise_reasoning` block — *not* folded into the caveman toggle (opposite risk profiles: mouth vs brain). Likely adaptive (by task difficulty / model family), and on hosted models it maps to the effort knob rather than a directive.
- Until then, CoD lives **only as eval arms** (next section).

---

## Validation — A/B evaluation harness  *(shared; validates Part 1, gates all of Part 2)*

The Part 1 toggle ships without waiting on this (it is opt-in, off by default). The eval then **validates** caveman — informing whether to widen or default it — and is the **hard gate** before any Part 2 build. Answers the actual question: *does compression make SRW worse, and by how much, for how much saving?* Reuses patterns from the existing offline harness (`eval/memory/`: `run.py`, `judge.py`, `report.py`, `arms/*.yaml`), which today is memory-specific and offline — so this needs a **job-dispatch variant** that runs real orchestrator jobs.

- **Arms** (honest framing — compare against a real concise baseline, not a strawman verbose default):
  1. `baseline` (no modifier)
  2. `concise` (plain "be concise" instruction)
  3. `caveman-full` (Part 1)
  4. `reasoning_effort=low` (Part 2, hosted-model lever)
  5. `caveman + CoD-directive` (Part 2, **visible-CoT models only**)
- **Run**: dispatch a representative task set (≥10–20/arm) ON each arm; serial per arm to avoid cache confounds; collect job UUIDs; poll to completion.
- **Cost metrics**: per `ref_id`, sum `completion-token` Δ (the isolated caveman effect) and total `cost_usd` Δ via `GET /api/usage?ref_id`; record wall-clock from job timestamps.
- **Quality metric**: an **LLM-judge** comparing deliverables across arms (build on `eval/memory/judge.py`), plus job success status (`jobs.status`) and, where present, critic verdicts (`src/tools/evaluation/evaluation_tools.py`).
- **Known gap**: reasoning tokens are **not** materialized to `usage_events` today (only in `turn_metrics`; recording path `orchestrator/services/litellm_gateway.py::materialize_llm_usage`). To measure Part 2's reasoning-token savings, either add a `reasoning-token` unit row there or read `turn_metrics` directly.

## Non-goals

- Input-side compression (`caveman-compress` of SRW's own large system prompts/memory) — a bigger but more invasive lever; separate future doc.
- MCP tool-description shrinking (`caveman-shrink`), caveman subagents (`cavecrew`), `wenyan` levels, user/global defaults — all out.
- Building Part 2 / the full eval harness in the Part 1 cut.

## Open questions

- **Part 1 UI scope**: ship the thin Cockpit toggle in the first cut, or start backend/config-only (settable via API/`config_override`) so caveman jobs can run immediately and add UI right after? *(Either is fine; affects plan size.)*
- Should caveman be **suppressed automatically for self-improving-loop internal roles** (where prose feeds the next stage), or left fully to operator opt-in? *(Lean: operator opt-in for v1; revisit after eval.)*

## References

**Upstream**: [juliusbrussee/caveman](https://github.com/juliusbrussee/caveman) (MIT) · `skills/caveman/SKILL.md`.
**Part 2 literature**: [Chain of Draft](https://arxiv.org/abs/2502.18600) · [Token-Budget-Aware Reasoning](https://arxiv.org/abs/2412.18547) · [When More is Less](https://arxiv.org/abs/2502.07266) · [Fast on the Easy, Deep on the Hard](https://arxiv.org/html/2506.10446v1) · [CoD for Software Engineering](https://arxiv.org/html/2506.10987) · [BudgetThinker](https://arxiv.org/pdf/2508.17196).
**Code pointers**: prompt assembly `src/core/loader.py:3539-3709`; job config `src/api/models.py:278-281` + `src/agent.py:1556-1590`; session config `orchestrator/main.py:14968-15043`; config resolver `orchestrator/services/config_resolver.py:110-160`; persona fencing (contrast) `src/core/expert_resolution.py:131-160`; usage schema `orchestrator/database/migrations/audit/0002_usage_events.sql:46-154`; usage query `orchestrator/services/usage_ledger.py:243-363`; usage recording `orchestrator/services/litellm_gateway.py::materialize_llm_usage`; eval harness `eval/memory/`.
**Related docs**: [[agent_skills]] · [[default_expert_roster]] · [[global_expert_management]] · [[usage_dashboard]] · [[db_backed_model_catalog]].
