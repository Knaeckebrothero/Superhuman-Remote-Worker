# Workspace Memory

This file is your persistent memory. It survives context compaction and is always in your system prompt.

**COMPACT, don't append.** Rewrite sections to remove redundancy. Target: under 4000 tokens (~60 lines).

**Don't duplicate plan.md.** Phase status and completion tracking belong in plan.md, not here.

## Pinned Instructions

Rules from instructions.md and task_brief.md that must persist across context compaction.
Extract and place here during the first strategic phase.

(PROTECTED — preserve verbatim during workspace rewrites unless provably wrong.)

## Repository

Framework, conventions, and key paths discovered during codebase exploration:
- **Stack**: (language, framework, version)
- **Test command**: (e.g., pytest tests/ -v, npm test)
- **Lint command**: (e.g., ruff check, eslint)
- **Entry points**: (key files relevant to the task)
- **Conventions**: (naming, patterns, style observed in the codebase)

(Update as you discover more about the codebase. Keep compact.)

## Deliverables

Track expected outputs and their verification status:

| Deliverable | Status | Verified By |
|---|---|---|
| (from plan.md) | pending | (test command or git evidence) |

## Session IDs

Active claude_code sessions for potential follow-ups:

(Format: "todo_N: session_abc123 — brief description")

(Clear completed sessions during strategic phases. Keep only active/resumable ones.)

## Key Decisions

Architectural decisions AND their reasoning. Without the WHY, you may revisit unnecessarily.

(Keep only decisions that affect future work. Remove resolved ones.)

## Status

Current position (update each strategic phase):

- **Phase**: (name from plan.md)
- **Branch**: (current working branch)
- **Blocked**: (active blockers, or "none")

(This section can be freely rewritten during strategic phases.)

## Failed Approaches

Approaches and delegations that were tried and did NOT work, with the reason.
This prevents retrying the same failed strategy after context compaction.

(Example: "Tried async database calls — Claude Code produced code with race conditions. Switched to sync with connection pooling.")

(PROTECTED — only remove entries when the underlying issue is confirmed resolved.)
