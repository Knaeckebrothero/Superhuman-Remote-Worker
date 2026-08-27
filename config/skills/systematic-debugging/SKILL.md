---
name: systematic-debugging
description: Use when something is broken or behaving unexpectedly — a failing test, a crash, a wrong output, a build error. Diagnose the root cause methodically (reproduce, hypothesize, gather evidence, isolate) before changing anything, and fix the cause rather than the symptom — never guess-and-check or patch the symptom.
display_name: Systematic Debugging
icon: bug_report
color: "#f38ba8"
tags:
  - debugging
  - development
  - diagnosis
---

# Systematic Debugging

When something breaks — a failing test, a crash, a wrong output — the fast move is
to guess a fix and try it. That's how you get ten changes deep with no idea what
helped. Diagnose first: find the cause with evidence, then make one change. A fix
you don't understand isn't a fix.

This is the proactive complement to loop-detection — it stops the thrash before a
rewind has to.

## The loop

**1. Reproduce.** Get a small, repeatable case that triggers the failure
(`run_command`) — you'll run it many times, and you can't confirm a fix without it.
If you can't reproduce it, gather more data; don't guess.

**2. Hypothesize.** Before changing anything, write down candidate causes — "X is
the cause because Y." List a few; if you only have one, you're guessing. You'll test
them one at a time.

**3. Gather evidence — before you change anything.** Instrument to test the
hypotheses: log what enters and exits each component boundary, read the relevant
code/data, check state at each layer. Tag each log line with the hypothesis it
tests. Run once, then read the output to see *where* it breaks.

**4. Isolate the root.** Trace the bad value back to where it originates — keep going
up until you find the source. The root cause is the one that, once fixed, explains
all the evidence. (A failure can have more than one contributing cause.)

**5. Fix the root, one change at a time.** Make the smallest change that fixes the
*cause*, not the symptom — don't catch-and-ignore the error or patch the output.
Change one thing. If it doesn't work, form a *new* hypothesis; don't stack another
fix on top.

**6. Verify.** Re-run the check (and the broader suite) to confirm the bug is gone
*and* nothing new broke, then remove the instrumentation. Hand off to
`verify-before-done` — a fix isn't done until a fresh check says so.

## Keep a diagnosis log

Write this to a `notes/` file as you go (it doubles as real progress, which resets
the loop-detection counter):

| Hypothesis | Evidence gathered | Verdict |
|---|---|---|
| H1: … because … | [log / output] | confirmed / ruled out |
| H2: … | … | … |

**Root cause (with evidence):** … · **Fix:** … · **Verification:** … (filled only
after the check re-runs clean)

## When to stop

After **three** failed fixes, stop patching. Three strikes isn't a failed
hypothesis — it's a signal the approach or architecture is wrong. Record the doubt
(knowledge base or `notes/`) and set `stop` with `goal_achieved=false` so the
orchestrator can decide. Don't attempt fix #4 blindly — a false "fixed" is expensive.

## Don't

- **Guess-and-check** — no fix without a hypothesis and evidence first.
- **Patch the symptom** — silencing the error leaves the bug; fix where the bad value originates.
- **Change many things at once** — you won't know what worked or what broke.
- **Fix without understanding** — "works now, no idea why" comes back.
