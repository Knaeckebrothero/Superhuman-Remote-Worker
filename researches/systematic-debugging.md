# Skill Research Prompt — `systematic-debugging`

An instance of the [skill research template](./template.md) — the prompt to run
to produce the evidence base for the `systematic-debugging` skill.

- **Status:** not yet run. The third Tier-1 build, and the most coding-flavored
  of the universal core. This prompt foregrounds the **knowledge-work transfer
  question** (objective 3 + the scope recommendation in F): the report should
  settle whether `systematic-debugging` ships as a universal model-invoked skill
  or gets bound to the `developer` expert. Run it, save the report alongside,
  then author the skill (and decide its scope).
- **Roster:** Tier-1 universal core, [`../docs/features/default_skill_roster.md`](../docs/features/default_skill_roster.md)
  (this is open decision #2 in that doc).
- **Likely enforcement:** model-invoked (the agent loads it when it hits a
  failure / unexpected behaviour). **Scope:** TBD by the research.

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
Name:        systematic-debugging
Intent:      When something is broken or behaving unexpectedly, diagnose the root
             cause methodically — reproduce, hypothesize, gather evidence, isolate
             the cause — BEFORE changing anything, and fix the root cause rather
             than the symptom. Closes the "thrash / guess-and-check / patch the
             symptom" gap where the agent makes blind changes hoping one works.
Must work across domains IF it is to be universal — and whether it genuinely does
is THE open question for this skill (see objective 3). Candidate instances:
  • Software:  a failing test / crash / wrong output — reproduce it, hypothesize
               causes, add logging or bisect, find the actual cause, fix the root
               (not the symptom), one change at a time.
  • Research:  a claim that doesn't hold up or findings that contradict —
               hypothesize why (bad source, misread, overgeneralisation), check
               against evidence, isolate the flaw.
  • Writing:   a draft that isn't working (confusing, unconvincing) — hypothesize
               the specific cause (buried thesis, weak transition, wrong
               structure), test by changing one thing.
  • Analysis:  numbers that don't reconcile or an anomalous result — hypothesize
               (data error, wrong join, bad assumption), instrument / check,
               isolate.
Failure modes it must defeat:
  • Thrashing / guess-and-check — blind changes with no hypothesis.
  • Symptom-patching — silencing the error instead of fixing the cause.
  • No reproduction — trying to fix what you can't reliably reproduce (so you
    can't know it's fixed).
  • Confirmation bias — fixating on the first hypothesis, ignoring contradicting
    evidence.
  • Many changes at once — so you can't tell what fixed or broke it.
  • Fixing without understanding — the "works now, no idea why" trap.
Empirical grounding to dig into:
  • The scientific-method / hypothesis-driven debugging model (reproduce →
    hypothesize → test → isolate → fix → verify); the canon — Agans' "Debugging:
    The 9 Indispensable Rules," Zeller's "Why Programs Fail" / delta-debugging,
    bisection.
  • CROSS-DOMAIN root-cause analysis: 5 Whys, Ishikawa / fishbone, fault-tree
    analysis, blameless postmortem / incident-response practice — this is the
    bridge that would make the skill universal rather than code-only.
  • LLM-specific evidence on how agents debug badly (thrash, hallucinate fixes,
    symptom-patch, confirmation bias) and what measurably helps. MAST hook
    (arXiv 2503.13657): relates to Incorrect Verification (FM-3.3) and Step
    Repetition (FM-1.3, thrashing) — find the numbers.
Platform anchors this skill should reference (keep guidance concrete):
  • Debugging is usually a TACTICAL-phase activity; it is model-invoked — the
    agent loads it when it hits a failure or unexpected behaviour, not on a
    healthy path.
  • Reproduce + instrument in the workspace: run_command (run the failing test,
    add logging, bisect), read_file (read the code / data / draft); for research
    or analysis failures, cite_web / cite_document to re-check a questionable
    source and kb_search to recall prior findings.
  • It hands off to verify-before-done: a fix is not done until the check is
    re-run and confirmed. Name that handoff.
  • SRW already REWINDS thrashing agents via fingerprint-based loop detection —
    this skill is the proactive complement to that reactive backstop; reference
    it.
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
1. How leading agent systems implement a "systematic debugging" / root-cause /
   debug-mode skill. Pull the ACTUAL procedure text where public — Claude Code
   "superpowers" systematic-debugging, Cursor Debug Mode, Cline, and debugging
   SKILL.md / rule files in the wild. Quote the steps.
2. The debugging methodology canon: scientific-method debugging
   (reproduce → hypothesize → test → isolate), Agans' 9 rules, Zeller's delta /
   systematic debugging, bisection. Which steps are essential vs. nice-to-have.
3. THE TRANSFER QUESTION (treat as the most important objective): is there a
   genuinely universal diagnosis discipline — 5 Whys, Ishikawa / fishbone,
   fault-tree analysis, blameless postmortem — that applies to RESEARCH, WRITING,
   and ANALYSIS failures, or is the well-evidenced procedure specifically CODE
   debugging? Find evidence either way. The answer decides whether this ships as
   a universal skill or is scoped to a software/developer role. Do NOT assume —
   the candidate non-code instances in the block above are our hypothesis, not a
   finding.
4. LLM-specific debugging failure + improvement: how agents debug badly (thrash,
   symptom-patch, hallucinate fixes, confirmation bias) and what MEASURABLY helps
   (hypothesis-first, evidence-before-fix, one-change-at-a-time, "explain the bug
   before fixing").
5. Reproduction & instrumentation: how to force "reproduce first" and "gather
   evidence before changing anything," including minimal-repro / bisection
   techniques — and what the non-code analogues are (if any).
6. Handoff & termination: how debugging ends (root cause identified WITH evidence
   + fix verified — the handoff to a verify step) and how to avoid both premature
   "fixed it" and endless thrashing.

DELIVERABLE — return ALL of the following, authoring-ready:
A. Executive synthesis: the strongest, best-supported approach to systematic
   debugging for an autonomous agent (3–6 tight paragraphs, cited).
B. The recommended SKILL.md BODY PROCEDURE — the concrete, ordered steps the
   skill should tell the agent to do (reproduce → hypothesize → instrument /
   gather evidence → isolate root cause → fix one thing → verify), written as
   domain-generally as the evidence supports, with short examples per step. This
   is the heart of the output.
C. A reusable diagnosis scaffold the body can embed — e.g. a hypothesis log /
   "expected vs. observed vs. hypotheses vs. evidence vs. conclusion" table the
   agent fills in.
D. The quality bar: what makes a GOOD diagnosis (reproduced, hypothesis-driven,
   root cause identified with evidence, one change at a time) vs. a guess-and-
   check thrash — with examples of each.
E. Anti-patterns section: the failure modes (from the SKILL block / objectives)
   the body should warn against, each with a one-line "instead, do X."
F. Enforcement AND SCOPE recommendation: (i) model-invoked vs. phase-injected vs.
   gated; (ii) — driven by objective 3 — should this ship as a UNIVERSAL skill
   (model-invoked for all experts) or be bound to the developer/software role,
   with optionally a lighter "root-cause analysis" variant for knowledge work?
   Recommend and justify both.
G. Trigger-description draft: a candidate third-person `description` line
   (what-it-does + when-to-use) optimized for accurate triggering, plus 2–3
   alternates.
H. Model-variance note: does this skill need per-model-family wording, or is one
   body robust? Evidence-based.
I. 2–4 real example snippets from the wild (quoted, attributed) worth adapting.
J. Open questions / weak spots in the evidence, explicitly flagged.
K. Full source list with one-line quality/relevance notes.

GUARDRAILS:
• Cite primary sources; mark FACT vs RECOMMENDATION.
• The transfer question (objective 3 / deliverable F) is the crux — do NOT
  hand-wave it. If the evidence for non-code debugging is thin, say so plainly;
  that is a valid and useful finding (it would scope the skill to the developer
  role).
• Respect the budgets (body <500 lines / <5k tokens) — recommend what EARNS its
  place; push depth into L3 reference files where appropriate and say so.
• Where our platform's mechanics (the tactical phase, run_command / read_file
  instrumentation, the verify-before-done handoff, fingerprint loop-detection)
  change the right answer, say how.
• Stay honest about domain coverage: most public debugging procedures are
  code-specific — flag how much of any "universal" claim rests on the general
  root-cause-analysis literature vs. on actual agent practice.
```
