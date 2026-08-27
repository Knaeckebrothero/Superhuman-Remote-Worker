---
name: test-driven-development
description: Use when implementing a feature or bugfix whose success is checkable by a test — write the test first, run it and watch it fail for the right reason, write the minimal code to pass, then refactor, in small steps. The discipline is small verified increments with an executable check and a regression net, not 100% coverage. For driving new code from a check you write first — not the end-of-task completion check (that's verify-before-done) or diagnosing why existing code is broken (that's systematic-debugging).
display_name: Test-Driven Development
icon: science
color: "#a6e3a1"
tags:
  - testing
  - development
  - tdd
---

# Test-Driven Development

You're building something whose success is checkable by a test. Write the check
first, watch it fail, write just enough code to pass, then clean up — in small
steps, repeating.

Be honest about *why*, because the usual sales pitch is half wrong. Controlled
studies find the test-*first* ordering isn't the magic, and chasing 100% coverage
buys little. What actually pays is **small, uniform, verified increments** — an
executable definition of "done" written before the code, a regression net that
runs every cycle, and steady even steps instead of one big push. Writing the test
first is how you *get* those, and it's how you prove the test tests something: a
check you never watched fail might pass for the wrong reason — or pass because you,
an agent with no one watching, quietly shaped it to. The orchestrator decides
status, and a false "tests pass" ends the job, so the check has to be real.

## The loop

**1. Pick the next small behavior.** Decompose to the smallest slice you can check
on its own. Small even steps are the active ingredient — not a big implementation
followed by a wall of tests.

**2. Write one failing test for it.** One behavior, a clear name, asserting on the
*real* behavior — not on a mock you control. The test is the executable form of
the spec (for the developer, of `spec_lock.md`).

**3. Run it — watch it fail, for the right reason** (`run_command`). This is the
load-bearing step; don't skip it. Read the exit code, not the vibe: confirm it
fails because the behavior is *missing*, not because of a typo or a bad import. If
it passes already, you're testing behavior that exists — fix the test.

**4. Write the minimal code to pass.** Just enough to go green. Don't gold-plate
beyond what the test asks for.

**5. Run it — watch it pass, suite still green** (`run_command`). If it fails, fix
the **code, not the test.** Never weaken an assertion, never edit the test to match
buggy code, never make the test unable to fail — that's gaming the check, not
passing it, and the "done" it produces is a lie.

**6. Refactor on green, then repeat.** Clean up duplication and names with the test
holding the line; don't add behavior. Record at green and move to the next slice.

*The loop won't read as "stuck":* progress resets on writes and `todo_complete`,
and you write code between test runs — a healthy red→green cycle is progress, not
repetition.

*One level up:* the same discipline — fix the acceptance check before you produce
the work, then meet it without weakening it — applies to any work with a checkable
success criterion (it's why scientists pre-register hypotheses). But the run-it
loop above is for code.

## The cycle

```
RED      → write ONE failing test for the next small behavior
VERIFY   → run it; watch it FAIL for the right reason (behavior missing, not a typo)
GREEN    → write the MINIMAL code to pass
VERIFY   → run it; watch it PASS; whole suite still green
REFACTOR → clean up on green (no new behavior); record; next slice
```

**A good test** fails first · exercises *real* behavior (not a mock) · has *one*
reason to fail.

## When to skip it

TDD is overhead on exploratory or unknown-shape work, throwaway spikes, and pure
UI / glue / hard-to-assert integration — there's no stable check to write first.
Don't force it there, and never let testability or 100% coverage drive the design.
When you genuinely don't know the shape yet, spike first, then come back and build
the real thing test-first.

## Don't

- **Rubber-stamp test-after** — write the test first and watch it fail; a test you never saw red may prove nothing.
- **Move the goalpost** — fix the code, not the test; never weaken an assertion or make a test unable to fail.
- **Over-mock** — assert on real behavior, not on a mock you control (a test that only checks the mock checks nothing).
- **Big-bang it** — small even slices, each ending green; not a wall of code then a wall of tests.
- **Skip the refactor — or gold-plate** — clean up on green, but don't chase coverage targets or test trivia.
- **Claim green without running** — read the actual exit code; the orchestrator's "done" is only as true as the check you ran (→ `verify-before-done`).
