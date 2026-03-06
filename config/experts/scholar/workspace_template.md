# Workspace Memory

This file is your persistent memory. It survives context compaction and is always in your system prompt.

**COMPACT, don't append.** Rewrite sections to remove redundancy. Target: under 4000 tokens (~60 lines).

**Don't duplicate plan.md.** Phase status and completion tracking belong in plan.md, not here.

## Pinned Instructions

Rules from instructions.md and task_brief.md that must persist across context compaction.
Extract and place here during the first strategic phase.

(PROTECTED — preserve verbatim during workspace rewrites unless provably wrong.)

## Exploration Focus

Current direction from the task description. What are you exploring and why?

- **Task**: (one-line summary of what you're investigating)
- **Modes**: (which exploration modes are relevant: web, codebase, logs, experiments)
- **Scope boundaries**: (what's in scope, what's explicitly out of scope)

## Ideas Index

Ideas written so far (one line each, for deduplication and coverage tracking):

| # | Title | Mode | Status |
|---|---|---|---|
| 001 | (slug) | web/codebase/logs/experiment | written / dead end |

## Experiments Index

Experiments run (one line each):

| # | Title | Result |
|---|---|---|
| 001 | (slug) | confirmed / rejected / inconclusive |

## Key Sources

Important sources discovered during exploration (for cross-referencing in future phases):

(Example: "LangGraph docs: https://... — state management patterns")

## Dead Ends

Topics explored that weren't worth pursuing, with the reason. Prevents revisiting.

(Example: "Investigated pgvector HNSW vs IVFFlat — no measurable difference at our data scale")

(PROTECTED — only remove entries when the underlying situation has changed.)

## Status

Current position (update each strategic phase):

- **Phase**: (name from plan.md)
- **Coverage**: (which aspects of the task have been explored vs untouched)
- **Blocked**: (active blockers, or "none")

(This section can be freely rewritten during strategic phases.)
