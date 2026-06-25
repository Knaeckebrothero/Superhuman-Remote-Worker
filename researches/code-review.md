# Skill Research Prompt — `code-review`

An instance of the [skill research template](./template.md) — the prompt to run
to produce the evidence base for the `code-review` (giving) skill.

- **Status:** ✅ **run + built 2026-06-25.** Research ran inline (three scoped
  passes, not the background harness — the long-running workflow kept getting
  orphaned by process restarts) → synthesized to [`code-review.report.md`](./code-review.report.md)
  → authored `config/skills/code-review/SKILL.md`, shipped **model-invoked**.
  Findings on the two foregrounded questions: (a) **transfer** — critique is
  universal *as a frame* (peer-review / Toulmin / steelman), so one body serves
  all experts, but the load-bearing verification steps stay code-shaped; (b)
  **scope** — model-invoked (catalog) as the default, with an optional `critic`
  `phase:tactical` binding as a deferred follow-up (deep-merge re-list cost).
  The first **Tier-2** build, and the first skill with a ready-made expert home:
  the `critic` exists to review but had **no codified review methodology**.
- **Scope:** GIVING a review (reviewing someone else's work). The mirror skill,
  **receiving-code-review**, is a separate Tier-3 entry — keep them distinct.
- **Roster:** Tier-2 opt-in, [`../docs/features/default_skill_roster.md`](../docs/features/default_skill_roster.md)
  (this is open decision #1 in that doc — TDD/code-review Tier-2-vs-3).
- **Likely enforcement:** model-invoked (rides the catalog) **plus** a candidate
  `phase:tactical` binding on the `critic` expert (reviewing is its whole job).
  Confirm via the research.

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
Name:        code-review  (GIVING a review — reviewing someone else's work, NOT
             receiving feedback on your own; that is a separate skill)
Intent:      When reviewing a change, artifact, or proposal — a code diff, a
             research finding, a draft, an analysis — systematically evaluate it
             against explicit criteria (correctness, security, performance,
             maintainability, and whether it actually does what was asked), then
             produce structured, evidence-backed, actionable findings with
             severity, separating blocking issues from nits, and render a clear
             verdict. Closes the gap where the agent rubber-stamps ("LGTM")
             without exercising the work, OR drowns the real issue in style nits,
             OR asserts problems with no evidence, severity, or suggested fix.
Must work across domains IF it is to be universal — and whether it genuinely does
is a key open question (see objective 3). The prior here is STRONGER than for
debugging: SRW's critic expert already "evaluates Scholar proposals," so review
plausibly generalizes — but test it, don't assume. Candidate instances:
  • Software:  review a PR / diff — read the change AND its surrounding context,
               check correctness / security / performance / tests, run it if you
               can, file findings with severity + file:line + a suggested fix,
               separate blocking from nits, give a verdict.
  • Research:  review a proposal or a set of findings — are the claims actually
               supported by the cited evidence? is the method sound? any
               overreach / cherry-picking / unsupported leap? (This is the
               critic-evaluates-Scholar case.)
  • Writing:   review a draft — does the thesis hold, does the structure carry the
               argument, is the evidence there — not just prose polish; return
               actionable revision notes, not a rewrite.
  • Analysis:  review an analysis / report — do the numbers reconcile, are
               assumptions stated, do the conclusions follow from the data, any
               methodology error.
Failure modes it must defeat:
  • Rubber-stamping ("LGTM") — approving without actually reading / exercising the
    work. The single most common review failure.
  • Sycophancy / over-approval — an LLM reviewer's bias toward saying it's fine;
    acute for an AUTONOMOUS critic whose verdict gates a job.
  • Nit-flooding / bikeshedding — burying the substantive issue under style nits,
    with no severity triage.
  • Hallucinated / false-positive findings — flagging problems that aren't real;
    erodes trust and wastes the author's revision loop.
  • No evidence — "this is buggy" without the failing case, the line, or the
    reasoning that shows it.
  • No severity / not actionable — a wall of observations with no blocking-vs-nice
    and no suggested fix.
  • Scope creep — rewriting the author's design instead of reviewing what was
    submitted; reviewing the whole codebase instead of the diff.
  • Missing the high-severity issue — thorough on trivia while the security hole /
    logic bug slips through.
Empirical grounding to dig into:
  • The code-review effectiveness canon — SmartBear/Cisco (review-size and rate
    limits: ~200–400 LOC per review, <500 LOC/hour, defect-detection falls off
    past that); Bacchelli & Bird "Expectations, Outcomes, and Challenges of Modern
    Code Review" (ICSE 2013 — finding: understanding the change is the hard part;
    defect-finding is weaker than expected; top value is knowledge transfer +
    alternative solutions); Sadowski et al. "Modern Code Review" at Google
    (ICSE-SEIP 2018). What MEASURABLY improves defect detection.
  • Reading techniques from the inspection literature — Fagan inspections,
    checklist-based reading (CBR) vs. perspective-based reading (PBR) vs. ad-hoc;
    checklists measurably improve detection. The severity scale + structured-
    findings format.
  • Cross-domain critique discipline — academic peer review, editorial review,
    "steelman before you critique" — evidence for a GENERAL review method that
    transfers beyond code (this is the universality bridge; treat it like the
    debugging transfer question — find evidence either way).
  • LLM-as-reviewer / LLM-as-judge evidence — how LLM reviewers fail (sycophancy,
    position/verbosity/self bias, hallucinated issues, style-over-substance) and
    what helps (explicit rubric, evidence-required, severity, run-don't-just-read,
    independent passes). MAST hook (arXiv 2503.13657): the Verification cluster —
    No/Incomplete Verification (FM-3.2, 8.2%) + Incorrect Verification (FM-3.3,
    9.1%) — plus the generator–critic dynamic. Find the numbers.
Platform anchors this skill should reference (keep guidance concrete):
  • This is the `critic` expert's CORE job — it reviews code diffs, evaluates
    Scholar proposals, audits for tech debt, runs tests. It already ships the
    review toolset: git_diff / git_show / git_log / git_status to see the change,
    run_command to exercise it, read_file/search_files for context, and an
    output/reviews/ + output/audits/ workspace to write the review into. Use these
    by name.
  • Review by EXECUTION, not just reading: run_command to run the tests / repro
    the claimed behaviour / build it — "I ran it and X" beats "it looks right."
    This is the handoff to / shared spine with verify-before-done.
  • Phase split: in SRW the critic's VERDICT tools are strategic-only, while the
    evidence-gathering (read the diff, run the tests, file findings) is tactical
    work. So the procedure spans phases — gather evidence tactically, render the
    verdict strategically. Reference this; an earlier bug (verdict tools forbidden
    in the phase the prompt told the critic to use) caused an infinite loop.
  • Completion stakes: a critic's verdict can gate a real job — a false "approve"
    ships a defect, a false "request-changes" wastes a cycle. Both are expensive,
    so the skill must force evidence behind the verdict.
  • This is the GIVING side; it pairs with the Tier-3 receiving-code-review (the
    author incorporating feedback) — name that counterpart but stay scoped to
    giving.
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
1. How leading agent systems implement a code-review / critique skill. Pull the
   ACTUAL procedure text where public — Claude Code "superpowers"
   requesting-code-review / receiving-code-review, Anthropic's code-review plugin,
   Cline code-review.md, Cursor, and the published rubrics of AI-reviewer products
   (CodeRabbit, Greptile, Graphite Diamond, Codacy). Quote the steps. Be explicit
   about which are GIVING-a-review vs. receiving.
2. The code-review effectiveness canon: SmartBear/Cisco (size + rate limits),
   Bacchelli & Bird (ICSE 2013 — what review actually achieves; understanding >
   defect-finding), Sadowski (Google, 2018), and the inspection literature
   (Fagan; checklist-based vs. perspective-based reading). Which practices
   MEASURABLY improve defect detection, and what review is genuinely good at
   vs. oversold for.
3. THE TRANSFER QUESTION (treat as a top objective): is there a genuinely
   universal critique discipline — academic peer review, editorial review,
   "steelman / strongest-form-first," structured argument evaluation — that
   applies to RESEARCH, WRITING, and ANALYSIS, or is the well-evidenced procedure
   specifically CODE review? The prior is more favourable than for debugging (the
   critic already evaluates non-code proposals), but find evidence either way.
   The answer decides whether this ships universal or is critic/developer-bound.
4. LLM-as-reviewer / LLM-as-judge failure + improvement: how LLM reviewers fail
   (sycophancy / over-approval, position & verbosity & self bias, hallucinated
   issues, style-over-substance) and what MEASURABLY helps (explicit rubric,
   evidence-required, severity scale, run-it-don't-just-read-it, independent
   passes, asking for the failing case). Quantify where possible. Address the
   autonomous-critic-gates-a-job stakes head-on.
5. Structured findings & severity: the severity scale (e.g. blocking / major /
   minor / nit), the finding format (location + issue + evidence + suggested
   fix), how to separate a blocking defect from a preference, and the
   review-by-execution principle (run the tests / reproduce the behaviour, don't
   just eyeball). Give the non-code analogue of each.
6. Termination & scope: when is a review DONE (covered the change, exercised it,
   filed evidenced findings, rendered a verdict), avoiding BOTH the rubber-stamp
   and the infinite-nitpick failure; and how to keep the reviewer reviewing the
   SUBMITTED change rather than redesigning it or auditing the whole repo.

DELIVERABLE — return ALL of the following, authoring-ready:
A. Executive synthesis: the strongest, best-supported approach to giving a review
   as an autonomous agent (3–6 tight paragraphs, cited).
B. The recommended SKILL.md BODY PROCEDURE — the concrete, ordered steps the skill
   should tell the agent to do (scope the review: what changed + what was asked →
   read for understanding, in context → check against an explicit rubric
   (correctness / security / performance / tests / maintainability) → exercise it:
   run the tests / reproduce the claim → file findings with severity + location +
   evidence + suggested fix → render a verdict with evidence), written as
   domain-generally as the evidence supports, with short code / research / writing
   examples per step. This is the heart of the output.
C. A reusable review scaffold the body can embed — a findings table
   (Severity · Location · Issue · Evidence · Suggested fix) PLUS a compact review
   rubric/checklist the agent works through. Keep the embedded version tight; push
   any long per-domain rubric to an L3 reference file and say so.
D. The quality bar: what makes a GOOD review (scoped, evidence-backed findings,
   severity-triaged, change actually exercised, clear verdict) vs. a rubber-stamp
   or a nit-flood — with a short example of each.
E. Anti-patterns section: the failure modes (from the SKILL block / objectives)
   the body should warn against, each with a one-line "instead, do X."
F. Enforcement AND SCOPE recommendation: (i) model-invoked vs. phase-injected vs.
   gated; (ii) — driven by objective 3 — should this ship UNIVERSAL (model-invoked
   for all experts) or be bound to the critic/developer role, optionally with a
   lighter "critique" framing for knowledge work? Note the candidate
   `phase:tactical` binding on the critic and the strategic-verdict / tactical-
   evidence phase split. Recommend and justify.
G. Trigger-description draft: a candidate third-person `description` line
   (what-it-does + when-to-use) optimized for accurate triggering, plus 2–3
   alternates. Make sure it triggers on "review this" without misfiring on the
   author's own self-check (that is verify-before-done's job).
H. Model-variance note: does this skill need per-model-family wording, or is one
   body robust? Evidence-based.
I. 2–4 real example snippets from the wild (quoted, attributed) worth adapting —
   ideally including at least one actual review-skill SKILL.md / rule file and one
   AI-reviewer product rubric.
J. Open questions / weak spots in the evidence, explicitly flagged.
K. Full source list with one-line quality/relevance notes.

GUARDRAILS:
• Cite primary sources; mark FACT vs RECOMMENDATION.
• The transfer question (objective 3 / deliverable F) is a crux — do NOT
  hand-wave it. The prior favours universality (the critic reviews non-code
  proposals), but say plainly how much of any "universal critique" claim rests on
  peer-review / editorial literature vs. on actual agent practice.
• Stay scoped to GIVING a review. The author-incorporating-feedback side is the
  separate receiving-code-review skill — name it, don't write it.
• Confront the autonomous-critic stakes: with no human in the loop, the verdict
  can end or mis-gate a job. Sycophancy and hallucinated findings are the two
  load-bearing failure modes — weight them.
• Respect the budgets (body <500 lines / <5k tokens) — recommend what EARNS its
  place; push a full per-domain rubric / long checklist into an L3 reference file
  and say so.
• Where our platform's mechanics (the critic expert + its git-diff/run_command
  toolset, the strategic-verdict/tactical-evidence phase split, the verify-
  before-done handoff, the orchestrator-decides-status model) change the right
  answer, say how.
• Stay honest about domain coverage: most review evidence is code-specific — flag
  how much of any "universal" claim rests on the general critique / peer-review
  literature vs. on actual agent practice.
```
