# Skill Research Prompt — `test-driven-development`

An instance of the [skill research template](./template.md) — the prompt to run
to produce the evidence base for the `test-driven-development` (TDD) skill.

- **Status:** ✅ **run + built 2026-06-25.** The fourth roster build, third
  Tier-2. Research ran inline (three scoped foreground passes) → synthesized to
  [`test-driven-development.report.md`](./test-driven-development.report.md) →
  authored `config/skills/test-driven-development/SKILL.md`, shipped **model-invoked,
  CODE-SCOPED** (the `systematic-debugging` pattern — a code-shaped description
  self-scopes). **All three cruxes settled:** (1) **transfer** = universal only as
  a *principle* (pre-registration, 96%→44% positive-result drop), code-specific as
  a *procedure*, agent practice near-totally code → ships code-scoped, NOT
  universal, with one principle-naming sentence; (2) **active ingredient** = Fucci
  IEEE TSE 2017 — test-*first* ordering "had no important influence", the win is
  *small uniform verified increments* + executable up-front check + regression net,
  so the body is built on that, not the ritual / 100% coverage (meta-analysis
  quality effect small/NS, negative vs incremental test-last); (3) **boundaries** —
  vs verify-before-done ("check written first to drive the build" vs "check run last
  to gate done"), vs systematic-debugging ("specify+build new behavior" vs
  "diagnose why existing is wrong", meeting at the reproduction test) — both named
  in the description. Agent teeth = watch-it-fail-first + don't-edit-the-test
  (defense vs documented test-gaming: `__eq__`→True, `sys.exit(0)`, editing the
  test file; ImpossibleBench), closing MAST FM-3.2+3.3 (~17%). Local k3d verified
  (parses in-pod via the real `skill_format` path; unbound → model-invoked). Open:
  optional `developer` `phase:tactical` binding, deferred with code-review→critic.
- **Scope:** writing the executable acceptance check (a failing test) BEFORE the
  implementation, then minimal code to pass, then refactor — in tight, small
  increments. NOT end-of-task verification (that's `verify-before-done`), NOT
  diagnosing a failure (that's `systematic-debugging`).
- **Roster:** Tier-2 opt-in, [`../docs/features/default_skill_roster.md`](../docs/features/default_skill_roster.md)
  (this is open decision #1 — TDD/code-review Tier-2-vs-3; and TDD's roster home is
  the `developer` expert / dev bundle).
- **THREE cruxes the research must settle:**
  1. **TRANSFER** — does "commit to the acceptance check before you build, don't
     move the goalposts" generalize beyond code (pre-registration / registered
     reports in science; acceptance-criteria-first / spec-by-example in writing &
     product)? Or is the well-evidenced procedure specifically *code* TDD? Expect a
     WEAKER verdict than project-onboarding's — test it, find evidence either way.
  2. **THE ACTIVE INGREDIENT** — the evidence on TDD is famously mixed. Fucci et
     al.'s dissection studies suggest the benefit comes from *small, uniform
     increments / granularity*, NOT the test-first *ordering* per se. The skill
     must encode the evidence-backed core, not the dogma. Pin this down with
     numbers.
  3. **THE `verify-before-done` BOUNDARY** — TDD writes the check FIRST and lets it
     drive the build loop; verify-before-done runs a check at the END as a
     completion gate; systematic-debugging writes a failing test to REPRODUCE a
     bug. The description must disambiguate all three.
- **Likely enforcement:** model-invoked (rides the catalog), consistent with the
  other Tier-2 builds; an optional `developer` binding (the roster's home) is a
  candidate, deferred like code-review→critic pending the deep-merge re-list cost.
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
Name:        test-driven-development  (TDD — write the test BEFORE the code, then
             code to pass, then refactor; the test-FIRST design loop, NOT
             end-of-task verification)
Intent:      When building something whose success is checkable, write an
             executable acceptance check (a failing test) BEFORE the
             implementation, watch it FAIL (so you know it tests something), write
             the MINIMAL code to make it pass, watch it pass, then refactor with
             the test as a safety net — iterating in tight, small increments so the
             spec drives the design and a regression net always exists. Closes the
             gap where the agent writes code first and back-fills rubber-stamp
             tests that can't fail, moves the goalpost by editing the test to match
             the code, writes a wall of code then a wall of tests in one big bang
             (losing the design feedback), or claims "tests pass" without ever
             running them.
Must work across domains IF it is to be universal — and whether it genuinely does
is a KEY open question, and the prior here is WEAKER than for the prior skills
(this is the most code-shaped candidate in the roster). The literal
Red-Green-Refactor cycle is welded to executable tests; the question is whether the
UNDERLYING discipline — "commit to the acceptance check before you build, then
build to it without moving the goalposts" — generalizes. Candidate instances (test
them, don't assume):
  • Software:  Red-Green-Refactor — write a failing unit/integration test for the
               next small behaviour, run it and SEE it fail, write the least code to
               pass, run it green, refactor, repeat. Don't edit the test to pass;
               don't write code the test doesn't demand.
  • Research:  PRE-REGISTRATION — state the hypothesis and the exact evidence /
               analysis that would confirm or refute it BEFORE gathering data, so
               you can't post-hoc rationalize. (Registered reports are the
               science analogue of "write the test first / don't move the
               goalpost.")
  • Writing:   state the acceptance criteria up front — the specific claims a
               section must land, the question it must answer — BEFORE drafting,
               then write to them and check each is met (acceptance-criteria-first /
               spec-by-example).
  • Analysis:  state the expected result and the validation check (a reconciliation,
               a sanity bound, a known-answer case) BEFORE running the analysis, so
               a wrong pipeline is caught rather than rationalized.
Failure modes it must defeat:
  • Test-after rubber-stamping — writing code first, then a test shaped to pass,
    that never actually fails and proves nothing. (For an autonomous agent this is
    the dominant failure; the cure is the SEE-IT-FAIL-FIRST step.)
  • Goalpost-moving — editing the TEST to match buggy code instead of fixing the
    code. The cardinal TDD sin.
  • Tautological / over-mocked tests — tests that pass without exercising the real
    behaviour (mock everything → assert the mock). Shared with code-review's "tests
    that actually exercise it."
  • Big-bang — writing the whole implementation, then a wall of tests at the end.
    Loses the small-increment design feedback that the evidence says is the active
    ingredient.
  • Skipping refactor — stopping at green, never cleaning up → debt; or the
    opposite, gold-plating / 100%-coverage dogma / testing trivia.
  • Hallucinated green — claiming the tests pass without RUNNING them. Handoff to
    verify-before-done.
  • Over-application — forcing TDD on throwaway spikes, exploratory/unknown-shape
    work, or untestable glue, where it's pure overhead (the "TDD is dead" critique).
Empirical grounding to dig into (BE HONEST — the evidence is MIXED):
  • Nagappan, Maximilien, Bhat & Williams, "Realizing quality improvement through
    test driven development" (Empirical SE, 2008) — 4 teams (IBM + 3 Microsoft):
    defect density DOWN 40–90%, but development time UP 15–35%. Get the exact
    numbers and the caveats.
  • Fucci et al., the "dissection" studies — "A Dissection of the Test-Driven
    Development Process: Does It Really Matter to Test-First or Test-Last?" (TSE
    2017) and the 2016 ESEM "An External Replication…": the finding that
    TEST-FIRST vs TEST-LAST ordering matters LESS than the GRANULARITY / UNIFORMITY
    of the steps (small uniform increments). THIS IS THE CRUX of objective 2 — the
    active ingredient. Quote it precisely.
  • The meta-analyses / systematic reviews — Rafique & Mišić (TSE 2013), Munir et
    al., Bissi et al. — and the recurring finding that more RIGOROUS studies report
    SMALLER effects, and that effects are heterogeneous. Quantify.
  • George & Williams (2003) — TDD passed ~18% more functional black-box tests
    (but took longer). Beck's "TDD By Example" (the canonical method) and Uncle
    Bob's "Three Laws of TDD." The 2014 "Is TDD Dead?" (DHH / Beck / Fowler)
    debate for the over-application boundary.
  • MAST (arXiv 2503.13657): the Verification cluster — No/Incomplete Verification
    (FM-3.2, ~8.2%) and Incorrect Verification (FM-3.3, ~9.1%). TDD as
    BUILT-IN, continuous verification (an executable spec written up front) vs a
    bolt-on check. Also the spec-disobedience modes (a test IS an executable
    specification). Find the numbers.
Platform anchors this skill should reference (keep guidance concrete):
  • run_command is the ENGINE of the loop — run the test to see it FAIL, run it to
    see it pass, run it after refactor. "I ran it and saw red, then green" beats
    "it should pass." Reference it by name.
  • TDD's home is the `developer` expert; the developer works against a locked spec
    recorded in `spec_lock.md`. A test is the executable form of that spec — tie
    TDD to spec-lock.
  • The phase model: the TDD micro-loop (write test → run → code → run → refactor →
    run) is TACTICAL execution; deciding WHAT behaviour to test next is closer to
    strategic planning. Reference how the loop sits in a tactical phase.
  • Stuck / loop detection: SRW fingerprints repeated (tool, args) calls; the TDD
    loop repeatedly calls run_command on the test. Progress resets on WRITES and
    todo_complete — and TDD writes code between runs — so a healthy loop shouldn't
    trip detection. Note this so the procedure doesn't read as "stuck."
  • Completion stakes: never signal goal_achieved on unrun or hallucinated green —
    the orchestrator decides status and a false "done" ends the job. This is the
    handoff to verify-before-done (TDD writes the check first; v-b-d runs the check
    at the end before claiming done).
  • Boundary with systematic-debugging: a failing test that REPRODUCES a bug, then
    fix-to-green, is the TDD-for-bugfixing pattern — name the shared "failing test"
    point but keep TDD scoped to DESIGN-driving, debugging to diagnosis.
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
1. How leading agent systems implement a TDD skill. Pull the ACTUAL procedure text
   where public — Claude Code "superpowers" test-driven-development SKILL.md,
   Anthropic's own CC best-practices (which calls TDD one of the "strongest"
   agentic patterns — quote it), Cursor / Cline TDD rules, Aider's test-driven
   flow, and any public TDD SKILL.md / rule file. Quote the steps AND the
   agent-specific guardrails (see-it-fail-first, don't-edit-the-test,
   one-test-at-a-time, don't-write-unrequested-code).
2. THE ACTIVE INGREDIENT (treat as a top objective): the canonical method (Beck
   Red-Green-Refactor; Uncle Bob's Three Laws) AND the honest empirical picture —
   Nagappan 2008 (defects −40–90%, time +15–35%), George & Williams 2003,
   Fucci 2016/2017 (test-first vs test-last matters LESS than granularity /
   uniform small steps), the meta-analyses (Rafique & Mišić 2013; rigorous studies
   → smaller effects). Land a clear, numbers-backed answer: what about TDD
   MEASURABLY helps (small verifiable increments, executable spec, regression net)
   vs. what is DOGMA (strict test-first ordering, 100% coverage). The body should
   encode the former.
3. THE TRANSFER QUESTION (treat as a top objective): is "commit to the acceptance
   check before you build, then build to it without moving the goalposts" a
   genuinely universal discipline — pre-registration / registered reports in
   science, acceptance-test-driven development (ATDD) / behaviour-driven
   development (BDD) / specification-by-example / executable specifications in
   product, acceptance-criteria-first in writing — or is the well-evidenced,
   step-by-step procedure specifically CODE TDD? Be decisive and honest about how
   much of any "universal" claim rests on the pre-registration / spec-by-example
   literature vs. on actual agent practice (almost all of which is code). This
   decides whether the skill ships universal or is developer-bound.
4. Keeping the agent honest: how to detect/prevent test-after rubber-stamping,
   goalpost-moving (editing the test), tautological/over-mocked tests, and
   hallucinated green. What measurably helps — RUN the test and observe red BEFORE
   writing code (the load-bearing honesty step), assert on real behaviour not
   mocks, one behaviour at a time, never modify a test to make it pass.
5. Termination & scope: when is the TDD loop DONE for a unit of work (the targeted
   behaviour is covered by a test that genuinely failed-then-passed, code is
   refactored, suite is green), and WHEN NOT TO USE TDD (throwaway spikes,
   exploratory/unknown-shape work, untestable glue) — the over-application
   boundary from the "Is TDD Dead?" debate. Avoid both under-testing and
   gold-plating.
6. The boundaries with the sibling skills: TDD (write the check FIRST, drives the
   build loop) vs verify-before-done (run a check at the END as a completion gate)
   vs systematic-debugging (a failing test to REPRODUCE a bug). Give the crisp
   one-line distinction for each, so the trigger-description doesn't misfire.

DELIVERABLE — return ALL of the following, authoring-ready:
A. Executive synthesis: the strongest, best-supported approach to TDD for an
   autonomous agent — built on the EVIDENCE-BACKED core (small verifiable
   increments + executable spec + see-it-fail-first), not the dogma (3–6 tight
   paragraphs, cited).
B. The recommended SKILL.md BODY PROCEDURE — the concrete, ordered steps (pick the
   next small behaviour → write a test for it → RUN it, see it fail for the right
   reason → write the minimal code → RUN it, see it pass → refactor → RUN it,
   still green → repeat; commit/record at green), written as domain-generally as
   the evidence supports, with short code / research / writing examples per step.
   This is the heart of the output. Make the small-increment discipline, not the
   ritual, the spine.
C. A reusable scaffold the body can embed — a compact Red-Green-Refactor loop
   checklist + the "is this a good test?" bar (fails first, exercises real
   behaviour, one reason to fail). Keep it tight; push any long
   per-language/per-framework detail to an L3 reference file and say so.
D. The quality bar: what makes GOOD TDD (a test that genuinely failed then passed,
   small increments, real-behaviour assertions, refactored, suite green) vs. a
   fake (test-after rubber-stamp, goalpost-moved test, over-mocked tautology) —
   with a short example of each.
E. Anti-patterns section: the failure modes (from the SKILL block / objectives)
   each with a one-line "instead, do X."
F. Enforcement AND SCOPE recommendation: (i) model-invoked vs. phase-injected vs.
   gated; (ii) — driven by objective 3 — UNIVERSAL (model-invoked, all experts)
   or developer-bound? Weigh a candidate `developer` binding (TDD's roster home)
   and the strategic-plan / tactical-loop phase split. Recommend and justify
   against the platform context. Note honestly if the transfer is too weak for
   universal and it should be developer-scoped like systematic-debugging is
   code-scoped.
G. Trigger-description draft: a candidate third-person `description` line
   (what-it-does + when-to-use) optimized for accurate triggering, plus 2–3
   alternates. It MUST trigger on "build this test-first / write tests first / do
   TDD" WITHOUT misfiring on verify-before-done (end-of-task self-check) or
   systematic-debugging (diagnosing a failure). Name those boundaries in the line.
H. Model-variance note: does this skill need per-model-family wording, or is one
   body robust? Evidence-based.
I. 2–4 real example snippets from the wild (quoted, attributed) worth adapting —
   ideally including at least one actual TDD SKILL.md / rule file and the Anthropic
   "strongest pattern" guidance.
J. Open questions / weak spots in the evidence, explicitly flagged — especially the
   mixed empirical picture and the transfer weakness.
K. Full source list with one-line quality/relevance notes.

GUARDRAILS:
• Cite primary sources; mark FACT vs RECOMMENDATION.
• BE HONEST ABOUT THE MIXED EVIDENCE (objective 2). Do not sell TDD as a
  silver bullet. The defensible core is small verifiable increments + an
  executable spec + a regression net + see-it-fail-first; strict test-first
  ordering and coverage targets are weakly supported dogma. Say so with numbers.
• The transfer question (objective 3 / deliverable F) is a crux — the prior is
  WEAKER than for the earlier skills (TDD is the most code-shaped). Say plainly how
  much of any "universal" claim rests on pre-registration / spec-by-example
  literature vs. actual agent practice (almost all code). A "developer-scoped"
  verdict is an acceptable and maybe correct outcome — don't force universality.
• Disambiguate from the siblings (objective 6): verify-before-done (end gate) and
  systematic-debugging (bug reproduction) share machinery with TDD; the
  description must not misfire onto them.
• Respect the budgets (body <500 lines / <5k tokens) — recommend what EARNS its
  place; push long per-language/framework detail into an L3 reference file and say
  so.
• Where our platform's mechanics (run_command as the loop engine, the developer
  expert + spec_lock.md, the tactical-loop phase, stuck/loop-detection resetting on
  writes, the verify-before-done handoff, the orchestrator-decides-status model)
  change the right answer, say how.
```
