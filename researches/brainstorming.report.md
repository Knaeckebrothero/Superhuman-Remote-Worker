# Brainstorming Skill — Research Report

Evidence base for the `brainstorming` skill (Tier-1 universal core). Produced by a
`deep-research` run (104 agents · 22 sources fetched · 107 claims → 25 verified →
**23 confirmed / 2 killed**), 2026-06-24. Prompt: [brainstorming.md](./brainstorming.md).
Skill: `config/skills/brainstorming/SKILL.md`.

## Executive synthesis

The strongest evidence-backed design is a single, domain-general **divergent→convergent**
two-phase procedure: diverge (generate N genuinely-different options via concrete ideation
lenses), cluster/combine, then converge to a chosen option with a stated reason. It is
peer-reviewed-validated to counter LLM "Artificial Hivemind" homogeneity/mode-collapse, and
three public production `SKILL.md` files converge on the same skeleton. Convergence should use
a **lightweight qualitative** method (value × feasibility + differentiation tiebreaker), not
heavy numeric scoring, and must always END in a decision.

**The key adaptation:** every public brainstorming skill examined is **human-in-the-loop**
(approval gates, "ask one question per message," "wait for the user") — none holds up at the
platform's "never pause" extreme. The mechanics (generate-N, diverge/converge) transfer; the
approval choreography does not. Our skill converges autonomously and records a decision instead
of asking permission. Enforcement: **model-invoked** (loaded on an open-ended "which approach?"
decision), not forced on every task.

## Findings

1. **Diverge→converge is the canonical, peer-reviewed structure** — HIGH (3-0). CreativeDC
   ([2512.23601](https://arxiv.org/abs/2512.23601)): two stages (explore creative space →
   satisfy requirements), ~1.6× more distinct high-utility outputs (+72% effective diversity),
   explicitly prevents premature convergence. Implemented by idea-refine, doc-coauthoring.
   *Caveat: CreativeDC's domain is narrow (intro-Python task generation).*
2. **Force variation via named ideation lenses + quantity scaled to complexity** — HIGH (3-0;
   2-1 on the specific 5–20 number). idea-refine lenses (inversion, constraint removal, audience
   shift, combination, simplification, 10× scaling, expert perspective); doc-coauthoring "5–20
   numbered options"; gstack "≥2, 3 preferred." Lenses *encourage*, don't *guarantee* diversity.
3. **Converge with a lightweight QUALITATIVE method, ending in a decision** — HIGH (2-1 on the
   specific 2×2; 3-0 on converge-as-decision). idea-refine's value×feasibility 2×2 + differentiation
   tiebreaker; no numeric weighted scoring found. End in a one-pager: chosen + why, rejected + why-not,
   assumptions, open questions.
4. **Brainstorming is a distinct phase BEFORE planning that FEEDS it** — HIGH (3-0). superpowers
   separates `brainstorming` (owns divergence) from `writing-plans` (sequences an existing spec):
   "the ONLY skill you invoke after brainstorming is writing-plans." → strategic phase, before
   `next_phase_todos`; must not itself produce a plan.
5. **Every public brainstorming skill is human-in-the-loop / approval-gated** — HIGH (3-0).
   superpowers, idea-refine, doc-coauthoring, gstack all hard-gate on user approval. None survives
   "never pause" as-is → strip the approval gates; converge + record autonomously.
6. **Model-invoked, NOT phase-injected/gated** — HIGH (3-0). Cursor + Anthropic: "if you can
   describe the diff in one sentence, skip the plan." Match effort to complexity; forcing it wastes
   cycles. Mitigate under-use with an assertive trigger description, not a mandate. Hard gate not
   recommended.
7. **LLM "Artificial Hivemind" homogeneity is real and persists** — HIGH (3-0).
   [2510.22954](https://arxiv.org/abs/2510.22954) (NeurIPS'25 best paper, Infinity-Chat 26K queries);
   CHI'25 RCT n=1,100 ([2410.03703](https://arxiv.org/pdf/2410.03703)): LLM-exposed participants stayed
   homogeneous even unassisted; no-LLM control improved. *Warning: LLM-suggested "strategies" can
   themselves homogenize — push for genuinely different axes, not a fixed template applied identically.*
8. **One universal body; no per-domain branches; no per-family procedure variants** — MEDIUM
   (synthesis). Mechanism is domain-independent. But brainstorm *quality* varies by model more than a
   deterministic skill, so the quality bar matters more than wording tweaks.
9. **Vendor plan/explore modes (Cline, Cursor) institutionalize deliberation-before-execution** —
   HIGH (3-0) — but produce a SINGLE plan, not divergent options. So brainstorming must do the
   divergence that plan-modes skip.
10. **Structured ideation > unstructured** — MEDIUM / analogical (human design studies; SCAMPER
    doubled novel ideas vs control). Human-subject, indirect for LLMs; counterweighted by finding 7.

## Refuted (do NOT use)

- superpowers' "2-3 approaches" line as *the canonical* structure — **1-2**.
- Cline Plan Mode "structurally forbidden from editing files" — **0-3** (documented intent, but leaks
  in practice).

## Caveats

- **Domain-transfer gap (most important):** empirical diversity validation is narrow-domain (Python
  tasks, engineering design, alternate-uses tests). For research framing / writing angles / analysis-
  method choice the value is **plausible-but-indirect**, not measured. Treat cross-domain universality
  as a reasoned design bet.
- **Quality is not mechanically verifiable** — unlike a deterministic skill, you can't run a test to
  confirm the agent diverged well.
- The exact lens list and 2×2 thresholds come from single production skills, not controlled comparisons.

## Open questions

1. Does the diversity win hold for NON-code decisions (research/writing/analysis)? A small internal
   A/B (skill-on vs skill-off, scored on option distinctness + decision quality) would settle it.
2. Right minimum option count N + number of forced axes for an autonomous agent? Public skills disagree
   (gstack 2-3 / idea-refine 5-8 / doc-coauthoring 5-20). Currently a judgment call (we chose 3-5 → 2-4).
3. Does the persisted decision artifact actually get re-read by the planning step after compaction, or
   does the agent re-anchor on live context?
4. How much does brainstorm quality vary across model families, and does that warrant per-family nudging?

## Sources (primary)

idea-refine `SKILL.md` (addyosmani/agent-skills) · doc-coauthoring `SKILL.md` (anthropics/skills) ·
superpowers brainstorming + writing-plans `SKILL.md` (obra) · gstack plan-ceo-review `SKILL.md` ·
Cline Plan/Act docs · Cursor Plan Mode + agent-best-practices · CreativeDC (arXiv 2512.23601) ·
Artificial Hivemind (arXiv 2510.22954, NeurIPS'25) · CHI'25 diversity-persistence RCT (2410.03703) ·
SCAMPER/Design-by-Analogy ideation studies (MIT/SUTD) · MAST (arXiv 2503.13657).
