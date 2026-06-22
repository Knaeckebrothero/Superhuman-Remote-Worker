---
name: verify-before-done
description: Use before marking a todo complete or signaling the goal is achieved. How to prove a task is actually done by running a workspace check and reconciling its real output against an explicit definition of done — instead of claiming success from assumption or inspection.
---

# Verify Before Done

You are fluent by design, so finished *looks* finished — which is exactly why
agents declare victory on work that doesn't hold up. Before you mark a todo
complete or signal `goal_achieved`, close the gap between "looks done" and
"proven done" with external evidence. (Missing, incomplete, or incorrect
verification is ~1 in 4 of all multi-agent failures — MAST, arXiv 2503.13657.)

The rule: a completion claim rests on the **fresh output of a check you actually
ran**, never on your own reading of your own work.

## The gate — four steps

**1. Define "done", pick the check.** Write the concrete, observable criteria
that prove this task is finished, then choose the workspace check that produces
that evidence:
- Code → `run_command` with the test/build/lint command — done = exit 0, 0 failures.
- Research → `cite_web` / `cite_document` — every claim resolves to a real source
  that contains the claimed fact.
- Writing → `run_command`: `wc -w`, `grep` for required headers — structure and
  length match the spec.
- Data/analysis → `run_command` with a check script or query — numbers reconcile,
  row counts match.

**2. Run it fresh.** Execute the check now, in the current state. A check you ran
before your last change proves nothing — capture the new output.

**3. Reconcile — read the actual output.** Quote the real result; don't recall it
from memory. Did it say `0 failed`, or did the build error after the linter
passed? Did the source return the claimed text, or a 404? Is it 2,100 words or 900?

**4. Decide on the evidence.**
- Falls short → do not complete. State the specific gap in the output, plan the
  fix, keep going.
- Meets "done" → complete, and include the exact verifying output in your message.

## What counts as evidence

Acceptable: an unmodified quote of a tool's output run in the current state.
Not acceptable: a success claim from your reasoning rather than a tool result —
- "I reviewed it, it handles the edge cases" → run the tests.
- "It meets the 5,000-word requirement" → run `wc -w`.
- "The build passes" → only if you ran it in this loop; stale output doesn't count.
- "The citation links to a real paper" → only if you resolved it this run.

## When there is no deterministic check (qualitative work)

Some criteria are genuinely subjective (tone, argument quality). Don't fake a
check — and don't skip verification either:
1. Verify every *checkable* aspect deterministically — structure, length, each
   required element present, each factual claim sourced.
2. For the subjective remainder, review it criterion-by-criterion against the
   instructions and **label it as judgement, not proof** ("structural checks
   passed via script; tone assessed by review against the 4 stated criteria").
   Honest scope beats a fabricated metric.

## Don't

- Assume tool output from how the work looks — run it. (Incorrect verification is
  the strongest single predictor of a fatal run.)
- Treat a broken check as "close enough." A check that won't run means the task
  isn't done — fix the check.
- Re-verify forever. Once the stated criteria pass, complete. (Loop-protection
  will rewind you, but stopping yourself is cheaper.)
- Promise a check in words and then shortcut it. One logged tool call per claim.
