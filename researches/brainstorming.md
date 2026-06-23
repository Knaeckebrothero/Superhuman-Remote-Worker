# Skill Research Prompt — `brainstorming`

An instance of the [skill research template](./template.md) — the prompt to run
to produce the evidence base for the `brainstorming` skill.

- **Status:** not yet run. `brainstorming` is the second Tier-1 build (after
  `verify-before-done`) — the cheap, model-invoked creative skill, and the one
  non-code member of the universal core. Run this prompt, save the report
  alongside, then author `config/skills/brainstorming/SKILL.md`.
- **Roster:** Tier-1 universal core, [`../docs/features/default_skill_roster.md`](../docs/features/default_skill_roster.md).
- **Likely enforcement:** model-invoked (the agent loads it when it hits an
  open-ended "which approach?" decision) — confirm via deliverable F.

## The prompt

```text
You are a research agent. Your job is to produce the complete evidence base
needed to AUTHOR one highly-optimized "Agent Skill" — a SKILL.md procedural guide
that an autonomous AI agent loads on demand. Your output will be handed directly
to whoever writes the skill, so it must be concrete and authoring-ready, not an
abstract survey.

Go out to the internet. Find best practices, concrete procedures, tips & tricks,
failure modes, and real example implementations for the skill named below. Prefer
primary sources (vendor docs, the actual SKILL.md/rule files in public repos,
peer-reviewed papers) over listicles. Cite every non-obvious claim with a URL.
Clearly separate VERIFIED FACT (sourced) from RECOMMENDATION (your synthesis).

═══════════════════════════════════════════════════════════
SKILL UNDER RESEARCH
═══════════════════════════════════════════════════════════
Name:        brainstorming
Intent:      Before committing to an approach, deliberately generate a diverse
             set of candidate options and explore the solution space — defer
             judgment, widen first, then converge on the strongest option with a
             stated reason. Closes the "latched onto the first idea" gap
             (anchoring / premature convergence) that produces mediocre plans.
Must work across domains (this is a UNIVERSAL, non-code-first skill):
  • Software:  enumerate 3–4 genuinely different design/architecture approaches
               with tradeoffs before writing any code — not just build the first.
  • Research:  generate multiple hypotheses, framings, and search angles for a
               question before diving into one.
  • Writing:   generate several angles / structures / thesis options (and titles)
               for a piece before drafting.
  • Analysis:  enumerate alternative methods, metrics, and explanations before
               settling on an interpretation (avoid tunnel vision).
Failure modes it must defeat:
  • Premature convergence / anchoring — taking the first plausible idea and never
    exploring alternatives.
  • False diversity — emitting 3 near-identical options that only look like a set.
  • Judging during generation — critiquing / filtering mid-divergence, which
    kills the breadth.
  • Never converging — endless ideation that never ends in a chosen option.
  • Brainstorming-as-planning — jumping to a detailed plan without first widening
    the option space (this skill FEEDS planning; it is not planning).
Empirical grounding to dig into:
  • The divergent→convergent two-phase model and Osborn's classic brainstorming
    rules (defer judgment, go for quantity, welcome wild ideas, combine/build).
  • LLM-specific evidence on output homogeneity / "mode collapse," and the
    techniques that MEASURABLY increase genuine idea diversity (role/persona
    variation, temperature, "generate N deliberately different options",
    tree-of-thoughts / graph-of-thoughts deliberate branching).
  • The MAST taxonomy hook (arXiv 2503.13657): committing to a wrong approach
    early is a "system-design" failure — MAST's single largest failure category
    (~44%) — so widen-before-committing targets the biggest class. Find the
    supporting numbers.
Platform anchors this skill should reference (keep guidance concrete):
  • Brainstorming happens in STRATEGIC phases, BEFORE planning — it feeds
    next_phase_todos / the planning skill (it widens the space; planning then
    sequences the chosen option).
  • Persist the option set so it survives context compaction: kb_write, notes/,
    or output/ideas/ (one file per idea with evidence + proposal).
  • Optionally fan divergent options out to sub-agents via delegate_work.
  • It is model-invoked: the agent loads it when it hits an open-ended "which
    approach?" decision — not on every task.
═══════════════════════════════════════════════════════════

CONTEXT — THE PLATFORM THIS SKILL RUNS ON (so your recommendations actually fit):
• It's a multi-tier orchestration system running FULLY AUTONOMOUS agents (a
  LangGraph state machine) as well as interactive sessions. Autonomous agents
  often run for many steps with NO human in the loop — so the skill must hold up
  with no one watching.
• Agents work in phases: STRATEGIC phases (planning, creating a todo list)
  alternate with TACTICAL phases (executing those todos). A skill can be bound to
  fire automatically in a given phase, gated before a specific tool, or left for
  the agent to invoke by judgment.
• Completion model: the agent never writes final job status itself — it sets a
  "stop + goal_achieved" signal and an orchestrator decides the real outcome.
  A FALSE result is expensive: it ends the job.
• The agent operates in an isolated remote workspace it reaches over SSH: it can
  run commands (tests, builds, curl, scripts), read/write files, search the web,
  and record citations and knowledge notes. Procedures can and should produce
  real artifacts, not just claims.
• It supports a WIDE RANGE of LLMs across providers, with per-model-family prompt
  variants. So: guidance must be model-agnostic and robust, and you should advise
  whether the skill needs per-family wording variants or works as one body.
• Autonomy levels range from "never pause" to "pause every phase." The skill must
  hold up at the "never pause" extreme.

WHAT AN "AGENT SKILL" IS (the format you're writing FOR):
• Open SKILL.md standard (agentskills.io) — a directory: a SKILL.md (YAML
  frontmatter `name` + `description`; markdown body) plus optional references/
  and scripts/. Portable to/from Claude Code and Codex.
• Progressive disclosure: L1 = name+description (~100 tokens, ALWAYS in the
  system prompt — this is also the trigger text the agent matches on); L2 = the
  SKILL.md body, loaded on demand (target <500 lines / <5k tokens); L3 = bundled
  reference files / scripts, pulled only when the body points to them.
• AUTO-INJECTION: a skill can be (a) model-invoked (agent decides to load it),
  (b) phase-injected (auto-loaded in strategic/tactical phases), or (c) an
  ENFORCED gate (the agent is refused a specific action until it has read the
  skill). One of your deliverables is a recommendation on which mode fits this
  skill.
• Authoring rubric to respect in every recommendation: one job per skill;
  instructions-first (scripts only for deterministic steps); the third-person
  `description` states what-it-does + when-to-use and IS the trigger; AVOID rigid
  ALL-CAPS ALWAYS/NEVER — explain WHY instead (the model is capable); keep it
  tight.

RESEARCH OBJECTIVES — find and synthesize:
1. How leading agent systems implement a "brainstorming" / ideation /
   explore-options-before-committing skill or mode. Pull the ACTUAL procedure
   text where public — Claude Code "superpowers" brainstorming, any Cursor/Cline
   plan/ask/explore modes, and brainstorming SKILL.md / rule files in the wild.
   Quote the steps.
2. The divergent–convergent thinking literature: Osborn's brainstorming rules,
   the diverge-then-converge two-phase model, and structured ideation methods
   (SCAMPER, "How Might We", Crazy 8s, six thinking hats) — which translate to a
   SINGLE-agent procedure and which are inherently group techniques.
3. LLM-specific idea diversity: the evidence on homogeneous / mode-collapsed
   generations, and the techniques that MEASURABLY increase genuine diversity
   (persona/role variation, temperature, "generate N deliberately different
   options", tree-of-thoughts / graph-of-thoughts branching, self-consistency).
   Separate what works from what's cargo-cult.
4. Convergence done well: structured selection among options (weighted criteria,
   tradeoff tables, decision matrices) — and how to avoid BOTH premature
   convergence and never-deciding. The skill must end in a chosen option with a
   stated reason.
5. Scope / when NOT to brainstorm: brainstorming a trivial or fully-specified
   task wastes cycles. How should the skill scope itself to genuinely open-ended
   decisions, and how is that trigger best described?
6. Cross-domain phrasing: one procedure that holds for software design, research
   framing, writing angles, AND analysis approaches — without collapsing into a
   software-design-only or creative-writing-only guide. One universal body vs.
   per-domain branches?

DELIVERABLE — return ALL of the following, authoring-ready:
A. Executive synthesis: the strongest, best-supported approach to a brainstorming
   skill for an autonomous agent (3–6 tight paragraphs, cited).
B. The recommended SKILL.md BODY PROCEDURE — the concrete, ordered steps the
   skill should tell the agent to do (diverge → cluster/combine → converge →
   record the choice), written domain-generally with short design/research/
   writing examples per step. This is the heart of the output.
C. A reusable ideation scaffold the body can embed — e.g. a "generate ≥N
   genuinely different options across these axes, then score them against these
   criteria" template the agent fills in.
D. The quality bar: what makes a GOOD brainstorm (genuine breadth, judgment
   deferred, ends in a reasoned choice) vs. a shallow or fake one (near-identical
   options, mid-generation filtering, no decision) — with examples of each.
E. Anti-patterns section: the failure modes (from the SKILL block / objectives)
   the body should warn against, each with a one-line "instead, do X."
F. Enforcement recommendation: model-invoked vs. phase-injected (strategic) vs.
   gated. State the tradeoff explicitly — forcing brainstorming on every task
   wastes cycles on trivial work, but the agent may under-use it. Recommend and
   justify.
G. Trigger-description draft: a candidate third-person `description` line
   (what-it-does + when-to-use) optimized for accurate triggering, plus 2–3
   alternates. Triggering is the crux here — the agent must reach for this on
   open-ended decisions without over-applying it to trivial ones.
H. Model-variance note: does this skill need per-model-family wording, or is one
   body robust? Evidence-based (note: idea-diversity behavior may differ more by
   model than a deterministic skill does).
I. 2–4 real example snippets from the wild (quoted, attributed) worth adapting.
J. Open questions / weak spots in the evidence, explicitly flagged.
K. Full source list with one-line quality/relevance notes.

GUARDRAILS:
• Cite primary sources; mark FACT vs RECOMMENDATION.
• Stay domain-GENERAL — this is a universal skill; resist drifting into a
  software-design-only or creative-writing-only guide.
• Respect the budgets (body <500 lines / <5k tokens) — recommend what EARNS its
  place; push depth into L3 reference files where appropriate and say so.
• Where our platform's mechanics (the strategic phase, next_phase_todos / the
  planning skill it feeds, kb_write / notes / output/ideas persistence,
  delegate_work fan-out) change the right answer, say how.
• Unlike a deterministic skill, brainstorming quality is hard to verify
  mechanically. Flag honestly how much of the recommended procedure is
  evidence-backed vs. plausible heuristic — and whether the value is validated
  beyond software design (e.g. for research framing and writing).
```
