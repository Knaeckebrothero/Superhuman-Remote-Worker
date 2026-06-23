# Skill Research Prompt — `verify-before-done`

The first instance of the [skill research template](./template.md). This is the
exact prompt that was run to produce the evidence base for the `verify-before-done`
skill.

- **Status:** report returned → [`../Verify Before Done Skill Research.md`](../Verify%20Before%20Done%20Skill%20Research.md).
  Skill authored + shipped at `config/skills/verify-before-done/SKILL.md` (bound
  `phase:tactical` on the worker experts). Enforcement follow-ups (read-gate,
  trace-gate) are tracked under "Enforcement model & follow-ups" in
  [`../docs/features/default_skill_roster.md`](../docs/features/default_skill_roster.md).
- **Roster:** Tier-1 universal core, `docs/features/default_skill_roster.md`.

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
SKILL UNDER RESEARCH  ←─ swap only this block for the next skill
═══════════════════════════════════════════════════════════
Name:        verify-before-done
Intent:      Before an agent claims a task/goal/sub-task is complete, it must
             actually verify the work and confirm the result against evidence —
             never assert success from assumption.
Must work across domains (this is a UNIVERSAL skill, not a coding skill):
  • Software:  run the tests/build/lint, hit the health endpoint, read the
               actual output — don't say "it works" because the code looks right.
  • Research:  every claim traces to a real, resolvable source; citations check out.
  • Writing:   the deliverable meets the stated requirements (sections, length,
               every instruction addressed) — verified, not assumed.
  • Analysis:  numbers reconcile / reproduce; sanity-checks pass.
Failure modes it must defeat:
  • Premature "done" — declaring success before doing the work.
  • Assertion-without-evidence — "Verified, looks good" with nothing run.
  • Fabricated or incorrect verification — claiming a check passed that didn't,
    or running the wrong check.
  • Termination-unawareness — not knowing what "done" even means for this task.
Empirical grounding to look deeper into: the MAST multi-agent failure taxonomy
  (arXiv 2503.13657) attributes ~17% of failures to No/Incomplete + Incorrect
  Verification, plus a separate "Unaware of Termination Conditions" mode. Find
  what the literature recommends to fix exactly these.
Our platform anchors this skill should reference (so its guidance is concrete,
not generic): the agent signals completion via a `goal_achieved` decision at a
`check_goal` step and marks sub-tasks via a `todo_complete` action; verification
should gate THOSE. The agent can run shell commands and read files in its
workspace, and can cite web/document sources.
═══════════════════════════════════════════════════════════

CONTEXT — THE PLATFORM THIS SKILL RUNS ON (so your recommendations actually fit):
• It's a multi-tier orchestration system running FULLY AUTONOMOUS agents (a
  LangGraph state machine) as well as interactive sessions. Autonomous agents
  often run for many steps with NO human in the loop — so self-verification is
  the only safety net before they declare a job done.
• Agents work in phases: STRATEGIC phases (planning, creating a todo list)
  alternate with TACTICAL phases (executing those todos). A skill can be bound
  to fire automatically in a given phase.
• Completion model: the agent never writes final job status itself — it sets a
  "stop + goal_achieved" signal and an orchestrator decides the real outcome.
  This means a FALSE "done" is expensive: it ends the job. Verification rigor
  directly protects this.
• The agent operates in an isolated remote workspace it reaches over SSH: it can
  run commands (tests, builds, curl, scripts), read/write files, search the web,
  and record citations and knowledge notes. Verification can and should produce
  real artifacts (command output, a written check-result), not just claims.
• It supports a WIDE RANGE of LLMs across providers, with per-model-family prompt
  variants. So: guidance must be model-agnostic and robust, and you should advise
  whether this skill needs per-family wording variants or works as one body.
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
1. How leading agent systems implement "verify before claiming done" / completion
   gates. Pull the ACTUAL procedure text where public — e.g. Claude Code
   "superpowers" verification-before-completion, Cursor/Cline testing & review
   workflows, Devin/other autonomous agents, and any public SKILL.md/rule files
   encoding this. Quote the steps.
2. The self-verification literature & techniques: self-refine, Reflexion,
   chain-of-verification (CoVe), self-consistency, LLM-as-judge applied to one's
   own output, "generate then verify" patterns — what measurably reduces false
   "done" / hallucinated success, and what its limits are.
3. "Definition of Done" / acceptance-criteria practice — how to make completion
   criteria EXPLICIT and CHECKABLE up front, and how that transfers beyond
   software to research/writing/analysis.
4. Evidence vs. assertion: how to force an agent to produce verification ARTIFACTS
   (command output, a filled checklist, a resolvable citation) instead of saying
   "looks good"; how to detect and prevent fabricated verification.
5. Termination-awareness: how agents are taught to recognize when a task is
   genuinely complete vs. when to keep going — and how to avoid the opposite
   failure (endless re-verification / not stopping).
6. Cross-domain phrasing: how to write one procedure that holds for code AND
   knowledge work without becoming code-specific or vague — and whether the
   evidence supports one universal body or per-domain branches.

DELIVERABLE — return ALL of the following, authoring-ready:
A. Executive synthesis: the strongest, best-supported approach to verification-
   before-completion for an autonomous agent (3–6 tight paragraphs, cited).
B. The recommended SKILL.md BODY PROCEDURE — the concrete, ordered steps the
   skill should tell the agent to do, written domain-generally (with short
   code/research/writing examples per step). This is the heart of the output.
C. A reusable "Definition of Done" checklist pattern the body can embed.
D. Evidence standard: a crisp rule for what counts as acceptable verification
   evidence vs. an unacceptable bare assertion, with examples of each.
E. Anti-patterns section: the specific failure modes (from objectives 1–5) the
   body should explicitly warn against, each with a one-line "instead, do X."
F. Enforcement recommendation: model-invoked vs. phase-injected vs. hard enforced
   gate for THIS skill — with reasoning tied to the autonomous/false-done-is-
   expensive context above.
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
• Stay domain-GENERAL — this is a universal skill; resist drifting into a
  software-only testing guide.
• Respect the budgets (body <500 lines / <5k tokens) — recommend what EARNS its
  place; push depth into L3 reference files where appropriate and say so.
• Where our platform's mechanics (goal_achieved/check_goal, todo_complete,
  workspace commands, the orchestrator-decides-status model) change the right
  answer, say how.
• Flag explicitly: is "verify before done" validated for NON-coding knowledge
  work, or is most evidence from coding agents? This is a known open question for
  us.
```
