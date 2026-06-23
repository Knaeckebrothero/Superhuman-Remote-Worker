# Skill Research Prompt — Template

A reusable prompt for researching how to build one **Agent Skill** *before*
writing its `SKILL.md`. Run it **one skill at a time** in a deep-research agent
(SRW's own research agent, a generic deep-research tool, or a Claude chat with
web access). The returned report is the evidence base for authoring the skill.

These skills come from the recommended roster in
[`../docs/features/default_skill_roster.md`](../docs/features/default_skill_roster.md)
(Tier-1 universal core first).

## How to use

1. Copy the prompt block below into a research agent.
2. Replace **only** the `SKILL UNDER RESEARCH` block with the target skill's
   details — everything else is skill-agnostic and stays fixed. (You may lightly
   tailor the *Research objectives* to the skill; [`verify-before-done.md`](./verify-before-done.md)
   is a worked example that does.)
3. Run it, then save the returned report and add a per-skill file in this folder
   that links to it (mirror `verify-before-done.md`).

---

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
SKILL UNDER RESEARCH  ←─ replace ONLY this block, per skill
═══════════════════════════════════════════════════════════
Name:        <skill-name>                 # e.g. systematic-debugging
Intent:      <1–2 sentences: what the skill makes the agent do, and the gap it
             closes>
Must work across domains (these skills are UNIVERSAL, not vertical) — give a
concrete instance of the skill in each:
  • Software:  <...>
  • Research:  <...>
  • Writing:   <...>
  • Analysis:  <...>
Failure modes it must defeat:
  • <the specific bad behaviours this skill exists to prevent>
Empirical grounding to dig into:
  • <papers / taxonomies / benchmarks that quantify why this matters — name the
    source and the numbers, e.g. a MAST failure-mode ID and its %>
Platform anchors it should reference (keep guidance concrete, not generic):
  • <the SRW tools / graph nodes / signals this skill plugs into — e.g.
    run_command, read_file, cite_web, kb_write, next_phase_todos, todo_complete,
    goal_achieved, check_goal, delegate_work, the strategic/tactical phases>
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
1. How leading agent systems implement THIS skill (or its closest equivalent).
   Pull the ACTUAL procedure text where public — Claude Code "superpowers",
   Cursor / Cline / Codex modes & rules, Devin and other autonomous agents, and
   any public SKILL.md / rule files encoding it. Quote the steps.
2. The academic & practitioner techniques behind doing this well — name the
   methods, what measurably helps, and the limits / failure cases.
3. How to make the skill's success criteria EXPLICIT and CHECKABLE, and how that
   transfers beyond software to research / writing / analysis.
4. How to keep the agent honest — how to detect and prevent the skill's
   characteristic failure modes (from the SKILL block).
5. Termination / scope: how the agent should know when the skill's job is done,
   avoiding both under-application and over-application.
6. Cross-domain phrasing: how to write one procedure that holds for code AND
   knowledge work without becoming vertical-specific — and whether the evidence
   supports one universal body or per-domain branches.

DELIVERABLE — return ALL of the following, authoring-ready:
A. Executive synthesis: the strongest, best-supported approach to this skill for
   an autonomous agent (3–6 tight paragraphs, cited).
B. The recommended SKILL.md BODY PROCEDURE — the concrete, ordered steps the
   skill should tell the agent to do, written domain-generally (with short
   code / research / writing examples per step). This is the heart of the output.
C. Any reusable checklist / scaffold / template the body should embed.
D. The quality bar: a crisp rule for what counts as doing this skill WELL vs. a
   shortcut or a fake, with examples of each.
E. Anti-patterns section: the specific failure modes the body should warn
   against, each with a one-line "instead, do X."
F. Enforcement recommendation: model-invoked vs. phase-injected vs. hard enforced
   gate for THIS skill — reasoning tied to the platform context above.
G. Trigger-description draft: a candidate third-person `description` line
   (what-it-does + when-to-use) optimized for accurate triggering, plus 2–3
   alternates.
H. Model-variance note: does this skill need per-model-family wording, or is one
   body robust? Evidence-based.
I. 2–4 real example snippets from the wild (quoted, attributed) worth adapting.
J. Open questions / weak spots in the evidence, explicitly flagged.
K. Full source list with one-line quality / relevance notes.

GUARDRAILS:
• Cite primary sources; mark FACT vs RECOMMENDATION.
• Stay domain-GENERAL — this is a universal skill; resist drifting into a
  single-vertical (e.g. software-only) guide.
• Respect the budgets (body <500 lines / <5k tokens) — recommend what EARNS its
  place; push depth into L3 reference files where appropriate and say so.
• Where the platform's mechanics (the phase model, goal_achieved/check_goal,
  todo_complete, workspace commands, the orchestrator-decides-status model)
  change the right answer, say how.
• Flag explicitly: is this skill validated for NON-coding knowledge work, or is
  most evidence from coding agents? This is a known open question.
```
