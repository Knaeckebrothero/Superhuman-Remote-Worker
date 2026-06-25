# Brainstorming Skill — Cross-Domain Validation Plan

> **Status:** deferred — needs real agent jobs to run on. Captures the one open
> validation for the `brainstorming` skill so we can get back to it. Not yet
> automated. Owner: TBD.

## What we're validating

Does the `brainstorming` skill's diversity/quality benefit hold for **non-coding**
open-ended decisions — research framing, writing angles, analysis-method choice —
or only for the narrow domains the literature actually measured?

This is **open-question #1** from `researches/brainstorming.report.md`. The skill
ships as a **Tier-1 *universal* core** skill (`docs/features/default_skill_roster.md`),
but that universality is currently a *reasoned design bet*: every strong empirical
source (CreativeDC = intro-Python task generation; SCAMPER = engineering design;
the CHI'25 RCT = an alternate-uses creativity test) is narrow-domain. For
research/writing/analysis the value is plausible-but-indirect, **not measured**.

(Same flavour as the `systematic-debugging` transfer question — see
`researches/systematic-debugging.md` objective 3. A shared eval harness could cover
both.)

## Hypothesis

On a genuinely open-ended non-coding decision (≥2 viable directions), having the
brainstorming skill in context produces **(a) more genuinely-distinct options** and
**(b) an equal-or-better final decision** than not having it — at an acceptable
token cost.

## Method — skill-on vs skill-off A/B

- **Conditions:**
  - **A (control):** skill absent; agent decides as it normally would.
  - **B (treatment):** brainstorming skill content in context. Because the skill
    is *model-invoked*, **force it in condition B** (temporary `phase:strategic`
    binding for the eval cohort, or an explicit `use_skill("brainstorming")`) so we
    measure the skill's **content**, not its trigger reliability. Measure trigger
    reliability separately (below).
- **Tasks:** 6–10 open-ended, genuinely-non-trivial "which approach?" prompts,
  spread across the target domains:
  - *Research framing* — e.g. "What's the most useful angle to investigate <topic>?"
  - *Writing* — e.g. "What structure/thesis should this report take?"
  - *Analysis* — e.g. "Which method/metric should we use to assess <thing>?"
  - Each must have ≥2 genuinely viable directions (screen out one-sentence-diff tasks).
- **Runs:** 2–3 per (task × condition) to average over LLM stochasticity. This is a
  **directional signal, not a paper** — keep N small.
- **How to run on SRW:** create worker jobs (or isolate the strategic-phase decision)
  per task/condition via the orchestrator job API; capture the generated option set,
  the chosen direction, and the reasoning from the workspace/audit trail. Model the
  harness on `tests/test_memory_eval_harness.py` / the `eval/` dir.

## Metrics

| Metric | How | Notes |
|---|---|---|
| **Option distinctness** | Cluster the generated options → count genuinely-distinct clusters; or an embedding-diversity score (Vendi-style, as in CreativeDC); or an LLM-judge "how many different *axes* are represented (1–5)". | The primary diversity signal. |
| **Decision quality** | Blind LLM-judge (and a light human spot-check) rates the chosen direction + reasoning (1–5) on soundness, fit, and whether value/feasibility were actually weighed. | Diversity is worthless if the final pick is worse. |
| **Token cost** | Extra tokens spent in condition B. | The tradeoff — is the gain worth it? |
| **Trigger reliability** (separate sub-test) | With the skill *model-invoked* (not forced), does the agent load it on genuinely-open decisions and **skip** it on trivial ones? | Validates the `description` trigger, the crux for a model-invoked skill. |

## Scoring

LLM-as-judge, **blind to condition** where possible, plus a light human review of a
few cases. Flag the known caveat: LLM judges carry their own biases, and (per the
report's finding 7) LLM-suggested "strategies" can themselves homogenize — so an
LLM judge may under-credit genuine divergence. Prefer clustering/embedding diversity
over pure LLM-rated diversity for the primary metric.

## Decision rule

- **Keep as universal Tier-1** if condition B yields meaningfully more distinct
  options **and** ≥ decision quality at acceptable cost.
- **Re-scope / re-tune** if there's no diversity gain on non-code work — e.g. bind it
  to the `scholar` / `writer` experts instead of leaving it universal, or strengthen
  the lens push in the body.
- **Reconsider the trigger** if the trigger sub-test shows over-application (it fires
  on trivial work and wastes cycles) or chronic under-use.

## Caveats

- Brainstorming quality is **not mechanically verifiable** — there's no test to run
  that proves the agent "diverged well"; this eval is inherently judgement-based.
- Needs genuinely open-ended tasks; a poorly-screened task set will wash out the effect.
- The exact option-count N (we chose 3–5 → 2–4 contenders) and lens list are
  heuristics, not tuned — this eval could also sweep those.

## Related

- `config/skills/brainstorming/SKILL.md` — the skill under test.
- `researches/brainstorming.report.md` — evidence base (open-question #1, findings 1/7).
- `docs/features/default_skill_roster.md` — the universal-core claim being tested.
- `researches/systematic-debugging.md` — same transfer question; candidate shared harness.
- `eval/`, `tests/test_memory_eval_harness.py` — existing eval infra to model on.
