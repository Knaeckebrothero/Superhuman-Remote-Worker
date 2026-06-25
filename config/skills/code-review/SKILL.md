---
name: code-review
description: Use when reviewing someone else's work — a code diff or PR, a research finding, a draft, an analysis — to evaluate it against explicit criteria (correctness, security, performance, tests, maintainability) and return structured, evidence-backed findings with severity and a clear verdict. For reviewing submitted work, not for checking your own before completion (that's verify-before-done).
display_name: Code Review
icon: rate_review
color: "#f9e2af"
tags:
  - review
  - quality
  - critique
---

# Code Review

You've been handed someone else's work to judge — a diff, a proposal, a draft. A
review fails in one of two ways: you rubber-stamp it ("looks good") without
checking, or you bury the one real problem under a pile of style nits. Neither
helps the author. A good review is scoped, read with understanding, *exercised*,
and ends in a verdict you can defend with evidence.

The stakes are asymmetric when your verdict gates the work: a false "approve"
ships the defect; a false "request-changes" wastes a cycle. So every finding —
and the verdict — carries its evidence.

## The review

**1. Scope it.** Get exactly what changed and what was asked. For code,
`git_diff` / `git_show` against the base; otherwise the submitted draft, findings,
or proposal. Read the task or plan it's meant to satisfy. Review the *submitted
change* — not the whole repo, not a redesign you'd have preferred. A big diff
reviews worse (defect-finding falls off past a few hundred lines), so read it in
coherent slices rather than skimming the whole thing.

**2. Understand before you judge.** Read the change in context — the callers, the
surrounding code, the intent. Understanding the change is the genuinely hard part
of reviewing, and a finding on code you didn't understand is usually wrong.
Steelman first: state what the author was trying to do before you critique how
they did it.

**3. Read against the rubric — by perspective, not a checklist.** Work these
axes, reading *as* a different stakeholder each time (adopting a perspective
finds more than ticking boxes):
- **Correctness** — does it do what was asked? edge cases, error paths, off-by-ones. *(read as the caller)*
- **Security** — input validation, authz, secrets, injection. *(read as an attacker)*
- **Performance** — hot paths, N+1 queries, needless work. *(read as load)*
- **Tests** — do they cover the change and actually exercise it, not just pass? *(read as whoever changes this next)*
- **Maintainability** — clarity, naming, duplication, dead code.

For a non-code artifact the same axes map over: claims-vs-cited-evidence
(correctness), unstated assumptions (security-of-the-reasoning), does the
structure carry the argument (maintainability), are the conclusions actually
tested (tests).

**4. Exercise it — run, don't just read.** Check out the change and run the tests;
try to *reproduce* a suspected bug before you flag it (`run_command`). Running it
is the single best defense against a hallucinated finding and a false approve.
Mark each finding **verified** (you ran it and saw X) or **suspected**
(read-only) — and prefer to verify anything that would gate the work. This is the
shared spine with `verify-before-done`.

**5. File findings: severity · location · evidence · fix.** One row per finding,
triaged by severity. Lead with the blocking ones; don't drown them under nits.
Each finding names *where*, *what's wrong*, the *evidence*, and a concrete *fix* —
"this is buggy" with no failing case and no line number isn't a finding yet.

**6. Render a verdict — with evidence.** `approve` / `approve-with-nits` /
`request-changes`, backed by the findings and what you ran. In SRW, gather this
evidence during the **tactical** phase and render the verdict in the
**strategic** phase (the verdict tools are strategic-only). Don't soften a real
blocker to be agreeable, and don't manufacture one to look thorough — both
mis-gate the job. If you're genuinely unsure, say so, set the verdict to the safe
side, and give the reason.

## Findings table

Write this to a review file as you go (`output/reviews/` for the critic, else a
`notes/` file) — it's a real artifact and it resets the loop-detection counter:

| Severity | Location | Issue | Evidence | Suggested fix |
|---|---|---|---|---|
| Blocking | `file:line` | … | ran X → got Y | … |
| Major | … | … | … | … |
| Minor / Nit | … | … | (read-only) | … |

**Severity:** **Blocking** (must fix — bug, security, data loss, breaks the goal)
· **Major** (should fix — wrong on an edge case, missing tests) · **Minor**
(consider — quality, clarity) · **Nit** (optional, non-blocking — style, naming).

**Verdict:** `approve` / `approve-with-nits` / `request-changes` — **Reasoning:**
one or two lines tied to the findings and what you ran.

## Don't

- **Rubber-stamp** — "looks good" without naming what you checked and ran isn't a review.
- **Cave when pushed** — hold a finding while its evidence holds; drop it when the evidence changes, not to be agreeable.
- **Nit-flood** — a wall of style nits with the real bug buried is a failed review; triage by severity.
- **Assert without evidence** — reproduce it or mark it *suspected*; don't claim a bug you can't show.
- **Redesign it** — review what was submitted; raise bigger ideas separately and non-blocking.
- **Polish the trivia while the security hole ships** — spend the budget on correctness and security first.
