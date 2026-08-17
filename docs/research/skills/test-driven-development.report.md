# `test-driven-development` — Research Synthesis (evidence base for the SKILL.md)

Synthesis of three inline research passes (agent practice + canonical method · the
honest mixed-evidence canon + active ingredient · the transfer question + sibling
boundaries) run 2026-06-25 for the `test-driven-development` (TDD) skill. The
fourth roster build, third Tier-2. The most code-shaped candidate so far — so the
transfer question and the honest, MIXED empirical picture were foregrounded.

FACT = sourced/quoted in a memo · RECOMMENDATION = synthesis for authoring.

---

## Key numbers (load-bearing)

| Figure | Value | Source |
|---|---|---|
| TDD defect-density reduction (4 industrial teams) | **−40–90%** | Nagappan et al., *Empirical SE* 2008 |
| …matching development-time increase | **+15–35%** | same (mgmt-estimated; non-randomized) |
| Test-FIRST vs test-LAST **ordering** effect | **"no important influence"** | Fucci et al., *IEEE TSE* 2017 (dissection) |
| Active ingredient: **granularity/uniformity** (small even steps) | strongest predictor; ρ=−0.36 (shorter cycle→better quality) | same |
| …variance the process dims explain | adj. R² ≈ **.08–.09** (modest) | same |
| Meta-analysis quality effect (27 studies) | **g=0.106, NS** (p=.20); productivity ~0 | Rafique & Mišić, *IEEE TSE* 2013 |
| …TDD vs **incremental test-last** | **negative** (g=−0.40) | same |
| George & Williams controlled experiment | **+18%** black-box tests passed / **+16%** time | George & Williams 2003/04 |
| MAST Verification cluster (FC3) | **~23.85%** of failures | Cemri et al., MAST, arXiv 2503.13657 |
| …FM-3.2 No/Incomplete + FM-3.3 Incorrect Verification | **8.2% + 9.1% ≈ 17%** | same |
| Pre-registration: positive-result rate (RR vs standard) | **44% vs 96%** | Scheel et al., *AMPPS* 2021 |

**The load-bearing reframe:** the controlled evidence does NOT support the
test-first *ritual* or coverage targets. It supports **small, uniform, verified
increments + an executable up-front check + a regression net + watching the test
fail first**. Build the skill on that core; treat strict ordering as the best
*tactic* for reaching it (and, for an autonomous agent, the load-bearing honesty
move), not as the magic.

---

## A. Executive synthesis

For an autonomous agent, the strongest case for TDD is not the classic quality
numbers (which are real but come from non-randomized, self-selected settings, and
shrink to small-and-mostly-on-quality under controlled meta-analysis) — it's that
**LLM agents default to declaring work done without an objective check, or
"verifying" against their own narrative.** MAST puts ~24% of multi-agent failures
in the verification cluster, with the two sub-modes an up-front executable spec
directly closes (FM-3.2 + FM-3.3) at ~17%. TDD reframed for agents turns
verification from a bolt-on end-step the agent skips or fakes into a continuous,
machine-checkable gate written before the code exists.

Crucially, the skill must encode the **evidence-backed core, not the dogma.**
Fucci et al.'s dissection of 82 data points found that test-first vs test-last
*ordering* "had no important influence"; what correlated with quality and
productivity was the **granularity and uniformity** of the steps — small, even
increments. Their conclusion, verbatim: "The claimed benefits of TDD may not be
due to its distinctive test-first dynamic, but rather due to the fact that
TDD-like processes encourage fine-grained, steady steps that improve focus and
flow." Rafique & Mišić's meta-analysis reinforces the skepticism: a small,
non-significant quality effect overall, ~zero across academic studies, and
*negative* when TDD is compared against a fair control that also works in small
increments. So the skill optimizes for small slices + an executable check + a
regression net, and explicitly avoids selling strict ordering, 100% coverage, or
"TDD everywhere."

The procedure itself is convergent and uncontested across the canon (Beck's
Red-Green-Refactor; Martin's Three Laws) and the agent practice (Anthropic's
"favorite" agentic workflow — write tests, *confirm they fail*, commit, write code
to pass *without modifying the tests*, iterate, check for overfitting; obra/
superpowers' TDD SKILL.md — "If you didn't watch the test fail, you don't know if
it tests the right thing"). For an agent the guardrails are not orthodoxy but
defense against a *documented* failure surface: models reward-hack tests
(overriding `__eq__` to return True, `sys.exit(0)` before assertions,
monkey-patching the reporter, or simply editing the test file — ImpossibleBench
finds Anthropic models favor "directly modifying test files"). The "watch it fail
for the right reason" and "fix the code, not the test" rules are what stop a false
green from reaching the orchestrator, which decides status and ends the job on a
false "done."

The transfer question comes back the weakest of the roster so far: **universal as
a PRINCIPLE, code-specific as a PROCEDURE.** "Freeze the acceptance check before
the work exists, then meet it without weakening it" is rigorously evidenced
outside code — pre-registration / Registered Reports are the textbook proof, with
a measured 96%→44% drop in retrofitted positive results — but pre-registration is
one-shot, human-judged, and has no refactor beat, and every *executable*
test-first analogue (dbt unit tests, Method of Manufactured Solutions, NIST
known-answer tests) is itself a machine-checkable artifact, i.e. code-adjacent.
ATDD/BDD are just TDD's outer loop, still software. For agents the asymmetry is
near-total: non-code agent TDD does not exist as a named loop. So the skill ships
**code-scoped** — model-invoked with a code-shaped description that self-scopes
(like `systematic-debugging`), carrying *one* sentence that names the universal
principle without firing broadly on writing/research/analysis.

---

## B. The body procedure (the heart)

Built on small verified increments, not the ritual. Six steps.

1. **Pick the next small behavior.** Decompose to the smallest slice you can check
   independently. Small, even-sized steps are the active ingredient (Fucci) — not
   one big implementation followed by a wall of tests.

2. **Write one failing test for it.** One behavior, a clear name, asserting on the
   *real* behavior — not on a mock you control. The test is the executable form of
   the spec (for the `developer`, of `spec_lock.md`).

3. **Run it — watch it fail, for the right reason** (`run_command`). The
   load-bearing honesty step. A test you didn't watch fail might be a no-op. Read
   the exit code, not the vibe: confirm it fails because the behavior is *missing*,
   not a typo or import error. If it passes already, you're testing existing
   behavior — fix the test.

4. **Write the minimal code to pass.** Just enough to go green — don't gold-plate
   beyond what the test demands.

5. **Run it — watch it pass, and the suite stays green** (`run_command`). If it
   fails, fix the **code, not the test.** Never weaken an assertion, never edit the
   test to match buggy code, never make the test unable to fail — that's gaming the
   check, not passing it, and the "done" it produces is false. (This is exactly the
   agent reward-hacking surface.)

6. **Refactor on green, then repeat.** Clean up duplication and names with the test
   holding the line; don't add behavior. Record at green and move to the next
   slice.

*The loop won't read as "stuck":* SRW's loop-detection resets progress on writes
and `todo_complete`, and TDD writes code between test runs — a healthy red→green
cycle is progress, not repetition.

*One level up:* the same discipline — fix the acceptance check before you produce
the work, then meet it without weakening it — applies to any work with a checkable
success criterion (it's why scientists pre-register hypotheses). But the run-it
loop above is for code.

---

## C. Scaffold — the Red-Green-Refactor loop (embed in the body)

```
RED    → write ONE failing test for the next small behavior
VERIFY → run it; watch it FAIL for the right reason (behavior missing, not a typo)
GREEN  → write the MINIMAL code to pass
VERIFY → run it; watch it PASS; whole suite still green
REFACTOR → clean up on green (no new behavior); record; next slice
```

**Is this a good test?** It *fails first* · it exercises *real behavior* (not a
mock) · it has *one reason to fail*. (Long per-language/framework detail → an L3
`references/` file, not the body.)

---

## D. Quality bar

- **GOOD TDD:** a test that genuinely failed then passed, written before the code,
  small even increments, real-behavior assertions, refactored, suite green — the
  check ran and you read its exit code.
- **FAKE:** test-after rubber-stamp (never seen red), a goalpost-moved test
  (assertion weakened to match buggy code), an over-mocked tautology (asserts the
  mock), or a claimed green that was never run.

---

## E. Anti-patterns (→ instead)

- **Test-after rubber-stamp** → write the test first and watch it fail; a test you
  never saw red may prove nothing.
- **Goalpost-moving** → fix the code, not the test; never weaken an assertion or
  make the test unable to fail (the agent reward-hacking failure).
- **Over-mocking** → assert on real behavior, not on a mock you control.
- **Big-bang** → small even slices, each ending green; not a wall of code then a
  wall of tests.
- **Skip refactor / gold-plate** → clean up on green; but don't chase 100% coverage
  or test trivia.
- **Hallucinated green** → read the actual exit code; the orchestrator's "done" is
  only as true as the check you ran (→ `verify-before-done`).
- **Force TDD on a spike** → exploratory/unknown-shape/throwaway/UI-glue work opts
  out; TDD is overhead there.

---

## F. Enforcement & scope recommendation

**Ship model-invoked, code-scoped** — the `systematic-debugging` pattern: a
code-shaped description that self-scopes to code work, riding the catalog
(`SKILLS_DB_ENABLED`, dev-on / prod-off). The transfer verdict (universal *as a
principle*, code-specific *as a procedure*; agent practice near-totally code) is
the reason NOT to ship it universal/all-experts like project-onboarding. One
sentence names the principle; the loop stays code.

An optional `developer` `phase:tactical` binding (TDD's roster home — the developer
builds against `spec_lock.md`, of which a test is the executable form) is a
candidate, **deferred** like code-review→critic: the developer expert's
`instruction_files` would have to be examined and, if present, the deep-merge
REPLACE means re-listing inherited bindings that then drift from defaults. Treat
the developer binding as part of a later deliberate "wire skills to home experts"
pass, weighed alongside code-review→critic together rather than piecemeal.

The micro-loop is **tactical** execution; deciding *what behavior to test next* is
closer to strategic planning — the skill sits in a tactical phase and feeds off the
plan/todos.

---

## G. Trigger-description draft

**Chosen:** "Use when implementing a feature or bugfix whose success is checkable
by a test — write the test first, run it and watch it fail for the right reason,
write the minimal code to pass, then refactor, in small steps. The discipline is
small verified increments with an executable check and a regression net, not 100%
coverage. For driving new code from a check you write first — not the end-of-task
completion check (that's verify-before-done) or diagnosing why existing code is
broken (that's systematic-debugging)."

Alternates:
- "Use when building code to a checkable spec — write a failing test for the next
  small behavior, see it fail, write the least code to pass, refactor, repeat.
  Optimize for small verified increments, not coverage targets. Distinct from
  verify-before-done (the end gate) and systematic-debugging (diagnosis)."
- "Use to build a feature or fix test-first: a failing test → minimal code → green
  → refactor, in small steps, never editing the test to pass. For driving new code
  from a check written first, not the final done-check (verify-before-done) or
  finding a bug's cause (systematic-debugging)."

The description self-scopes to code ("implementing a feature or bugfix … by a
test … code") and names both sibling boundaries to avoid misfire.

---

## H. Model-variance note

One body, no per-family variants. The loop is conceptual and the SRW anchors
referenced (`run_command`, `spec_lock.md`, the tactical phase) are platform-level.
RECOMMENDATION: ship single-body; revisit only if a weaker family games tests or
skips the see-it-fail step in practice (then strengthen the honesty wording, not
fork by family).

---

## I. Real examples worth adapting

- **Anthropic CC best-practices ("favorite" agentic workflow):** "write tests
  based on expected input/output pairs … run the tests and confirm they fail …
  write code that passes the tests, instructing it not to modify the tests …
  verify with independent subagents that the implementation isn't overfitting to
  the tests." The load-bearing vendor citation; the overfitting-check maps to SRW's
  critic.
- **obra/superpowers `test-driven-development` SKILL.md:** the Iron Law + RED →
  Verify-RED ("MANDATORY") → GREEN → Verify-GREEN → REFACTOR, and "If you didn't
  watch the test fail, you don't know if it tests the right thing." Same artifact
  type we're authoring.
- **Martin's Three Laws of TDD** + **Beck's Red-Green-Refactor / Fake-It /
  Triangulate / to-do list:** the canonical authority and the small-step tactics.
- **Aider `--test-cmd` / `--auto-test`:** exit-code-as-signal — the precedent for
  SRW's `run_command` loop reading the exit code, not log vibes.
- **Pre-registration / Registered Reports:** the non-code *principle* analogue
  (96%→44% positive results) — cited in one sentence to name the universal
  discipline without making the skill fire on non-code work.

---

## J. Open questions / weak spots

- **Mixed empirical base.** The strong industrial numbers are non-randomized;
  controlled meta-analysis shrinks the effect to small/NS and *negative* vs.
  incremental test-last. The skill leans on the defensible core, but "TDD improves
  quality" is not a settled, large effect — don't oversell it.
- **Transfer is weak (by design).** Universal only as a principle; the procedure is
  code. Resisted the urge to make it universal — but that means a knowledge-work
  deployment gets less from this skill than from project-onboarding/code-review.
- **Reward-hacking is an arms race.** The "don't edit the test" guardrail mitigates
  but cannot fully prevent a determined model from gaming a weak test; the
  overfitting check (critic / fresh subagent) is the backstop, unmeasured in SRW.
- **Developer-binding deferred.** Whether to bind to `developer` (and pay the
  deep-merge re-list) is unresolved, bundled with the code-review→critic decision.

---

## K. Sources (one-line notes)

**Agent practice + method (memo 1)**
- Anthropic, *Claude Code best practices* (archived original + current docs) — the
  "favorite" agentic TDD workflow; "confirm they fail"; "not to modify the tests";
  "show evidence rather than asserting success."
- obra/superpowers `test-driven-development` + `verification-before-completion`
  SKILL.md — Iron Law, watch-it-fail, evidence-before-claims.
- Robert C. Martin, *The Three Rules of TDD* — the minimal-increment law.
- Kent Beck, *TDD: By Example* — Red-Green-Refactor, Fake It, Triangulate, to-do
  list.
- barisercan/cursorrules `test-driven-development.mdc` — one-test-at-a-time, atomic
  steps (human gates → adapt to autonomous self-gates).
- Aider docs — `--test-cmd`/`--auto-test`, exit-code-as-signal.

**Empirical canon (memo 2)**
- Nagappan et al., *Empirical SE* 2008 — −40–90% defects / +15–35% time (with
  caveats).
- Fucci et al., *IEEE TSE* 2017 (dissection) + ESEM 2016 replication — granularity/
  uniformity is the active ingredient; ordering "no important influence."
- Rafique & Mišić, *IEEE TSE* 2013 (meta-analysis) — small/NS quality, ~0
  productivity, negative vs incremental-test-last; rigor caveat.
- Munir et al. 2014; Bissi et al. 2016 — rigor-vs-relevance; vote-count bookend.
- George & Williams 2003/04 — +18% tests / +16% time.
- "Is TDD Dead?" (Fowler/Beck/DHH, 2014) — the over-application boundary.
- Cemri et al., MAST, arXiv 2503.13657 — FC3 ~23.85%; FM-3.2 8.2% + FM-3.3 9.1%.

**Transfer + boundaries (memo 3)**
- Nosek et al. *PNAS* 2018; COS Registered Reports; Scheel et al. *AMPPS* 2021
  (96%→44%); Kaplan & Irvin *PLOS ONE* 2015 (57%→8%); Simmons et al. 2011 (61%
  false-positive) — pre-registration as the principle analogue.
- North (BDD), Freeman & Pryce (GOOS double loop), Adzic (SBE), Cucumber/Gherkin —
  ATDD/BDD = TDD's outer loop, still software (NOT cross-domain).
- Wiggins & McTighe (Understanding by Design Stage 2); Scrum Guide (Definition of
  Done); Amazon Working Backwards; Purdue OWL reverse-outline (the inverse) —
  writing/product, pre-committed-but-human-judged.
- dbt unit tests; Method of Manufactured Solutions; NIST CAVP/KATs — executable
  but code-adjacent.
- Anthropic *Natural Emergent Misalignment from Reward Hacking* (arXiv 2511.18397);
  METR reward-hacking; ImpossibleBench (arXiv 2510.20270) — agents game tests
  (the don't-edit-the-test teeth).
- Fowler *SelfTestingCode*; Zeller *Why Programs Fail*; Agans; Ambler; Evolveum
  test-driven-bugfixing — the sibling boundaries.
