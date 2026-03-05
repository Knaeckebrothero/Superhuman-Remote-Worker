# Workspace Memory

This file is your persistent memory. It survives context compaction and is always in your system prompt.

**COMPACT, don't append.** Rewrite sections to remove redundancy. Target: under 4000 tokens (~60 lines).

**Don't duplicate plan.md.** Phase status and completion tracking belong in plan.md, not here.

## Pinned Instructions

Rules from instructions.md and task_brief.md that must persist across context compaction.
Extract and place here during the first strategic phase.

(PROTECTED — preserve verbatim during workspace rewrites unless provably wrong.)

## Deliverables

Track expected outputs and their verification status:

| Deliverable | Path | Status | Verified By |
|---|---|---|---|
| (from plan.md) | output/... | pending | (tool call or evidence) |

## Facts

Durable knowledge discovered during work: IDs, paths, credentials, configurations, relationships.

(PROTECTED — preserve during workspace rewrites unless provably wrong.)

(Example: "API endpoint: /v1/models | Rate limit: 100/min | Customer ID: BO-042")

## Key Decisions

Decisions AND their reasoning. Without the WHY, you may revisit unnecessarily.

(Keep only decisions that affect future work. Remove resolved ones.)

## Status

Current position (update each strategic phase):

- **Phase**: (name from plan.md)
- **Blocked**: (active blockers, or "none")

(This section can be freely rewritten during strategic phases.)

## Failed Approaches

Approaches that were tried and did NOT work, with the reason.
This prevents retrying the same failed strategy after context compaction.

(Example: "Tried paramiko for file upload — failed with timeout. Used scp instead.")

(PROTECTED — only remove entries when the underlying issue is confirmed resolved.)
