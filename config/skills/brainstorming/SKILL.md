---
name: brainstorming
description: Use when facing an open-ended decision with more than one viable approach — which design, framing, structure, or method — before committing. Generates several genuinely different options, weighs them, and commits to one with a stated reason instead of running with the first idea. Skip it when the choice is obvious or you could describe it in one sentence.
display_name: Brainstorming
icon: lightbulb
color: "#cba6f7"
tags:
  - planning
  - ideation
  - decisions
---

# Brainstorming

When you hit an open-ended decision with more than one viable approach — which
design, which research angle, which structure, which method — your first instinct
is usually the most obvious one, not the best one. LLMs default to homogeneous,
"safe-average" answers (a documented mode-collapse effect), so the first idea is
the one most models would also give. Widen before you commit: generate genuinely
different options, weigh them, choose one with a reason.

Use this only when the choice is genuinely open (≥2 viable directions). If you
could describe the decision in one sentence, or you've made this exact call many
times, skip it and proceed — brainstorming trivial work just burns budget.

## 1. Diverge — generate genuinely different options

Produce **3–5 options that differ on a real axis**, not flavors of one idea.
Defer judgment here; don't filter while generating. To force real spread, run the
decision through a few lenses and keep what comes out distinct:

- **Invert** — remove the obvious constraint, or reverse the goal.
- **Shift audience/scale** — solve it for 10× the size, or a different user.
- **Combine** — merge two partial ideas.
- **Simplify** — the smallest thing that could possibly work.
- **Borrow** — how does another domain solve this shape of problem?

A *real* axis of difference looks like:
- Software: a library vs. a service vs. inline — not three takes on the service.
- Research: causal vs. descriptive vs. comparative framing.
- Writing: lead with the problem vs. the result vs. a narrative.
- Analysis: a different metric, unit of analysis, or baseline.

## 2. Cluster — collapse the near-duplicates

Group options that are really the same idea; keep one per genuinely distinct
cluster. If everything collapses into one, you haven't diverged — push a harder
lens. Aim to leave 2–4 real contenders.

## 3. Converge — choose one, with a reason

Judge the contenders *qualitatively* — no heavy scoring spreadsheet. For each ask:
**value** (how well does it serve the goal?) and **feasibility** (can you actually
do it here, with these tools and constraints?); use **differentiation / fit** as
the tiebreaker between close options. Pick the high-value, feasible one.

You are autonomous: **decide.** Don't wait for approval — if your autonomy level
needs a checkpoint, it happens at the phase boundary, not inside this skill.

## 4. Record the decision

Write a short decision record so the choice survives context compaction and the
planning step can build on it — `kb_write` if available, else `notes/` or
`output/ideas/`:
- **Chosen** — the option + one-line why.
- **Rejected** — the other contenders + one-line why-not each.
- **Assumptions / open questions** the choice rests on.

Then hand the chosen direction to planning (`next_phase_todos`). This skill widens
the option space; it does **not** produce the detailed plan itself.

## Watch for

- **First-idea anchoring** — one option means you didn't brainstorm.
- **False diversity** — 3 near-identical options; push a lens that moves a real axis.
- **Judging while generating** — keep diverge (1) and converge (3) separate.
- **Never deciding** — step 3 always ends in one choice; more analysis ≠ progress.
- **Brainstorming the obvious** — if the answer's clear, you shouldn't be here.
