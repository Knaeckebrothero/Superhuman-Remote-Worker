# Workspace Memory

This file is your persistent memory. It survives context compaction and is always in your system prompt.

**COMPACT, don't append.** Rewrite sections to remove redundancy. Target: under 50 lines.

**Don't duplicate plan.md.** Phase status and completion tracking belong in plan.md, not here.

## Status

Current position (update each strategic phase):

- **Phase**: (name from plan.md)
- **Blocked**: (active blockers, or "none")

## Key Decisions

Decisions AND their reasoning. Without the WHY, you may revisit unnecessarily.

(Keep only decisions that affect future work. Remove resolved ones.)

## Entities

Reference table for IDs, names, relationships you need to look up repeatedly.

(Example: "Customer: BO-042 | Invoice: BO-015")

## Critical Context

Information that MUST survive context compaction. Use sparingly.

(Example: "API rate limit: 100/min - batch accordingly")

## Deliverables

Track expected outputs (from plan.md) and their status:

| Deliverable | Path | Status |
|---|---|---|
| (from plan.md) | output/... | pending |

## Pinned Instructions

Rules from instructions.md and task_brief.md that must persist across context compaction.
Extract and place here during the first strategic phase.
