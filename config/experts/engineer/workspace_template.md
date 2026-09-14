# Workspace Memory

This file is your persistent memory. It survives context compaction and is always in your system prompt.

**COMPACT, don't append.** Rewrite sections to remove redundancy. Target: under 3000 tokens (~60 lines).

**Don't duplicate plan.md.** Phase status and completion tracking belong in plan.md.

## Pinned Instructions

Rules from instructions.md and task_brief.md that must persist across context compaction. Extract them in the first strategic phase.

(PROTECTED — preserve verbatim during workspace rewrites unless provably wrong.)

## Repository

- **Repository**: (clone name from README.md → repos/<name>/, base branch, writable?)
- **Stack**: (language, framework, version)
- **Test command**: (exact command, run with working_dir="repos/<name>")
- **Lint / build commands**: (exact commands, or "none")
- **Source dir / test dir**: (paths)
- **Conventions**: (naming, import order, test style observed)
- **Branch**: (working branch; the PR base from README.md)

(PROTECTED — update as you learn; keep compact.)

## Deliverables

| Deliverable | Path | Status | Verified by |
|---|---|---|---|
| (from task_brief.md) | output/... or repos/<name>/... | pending | (command + decisive output line) |

## Key Decisions

Decisions AND their reasoning. Keep only decisions that affect future work.

## Status

- **Phase**: (name from plan.md)
- **Blocked**: (active blockers, or "none")

## Failed Approaches

What was tried and did NOT work, with the reason, so it is not retried after compaction.

(PROTECTED — only remove entries when the underlying issue is confirmed resolved.)
