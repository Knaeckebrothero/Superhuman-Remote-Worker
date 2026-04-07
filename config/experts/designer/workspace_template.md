# Workspace Memory

This file is your persistent memory. It survives context compaction and is always in your system prompt.

**COMPACT, don't append.** Rewrite sections to remove redundancy. Target: under 4000 tokens (~60 lines).

**Don't duplicate plan.md.** Phase status and completion tracking belong in plan.md, not here.

## Pinned Instructions

Rules from instructions.md and task_brief.md that must persist across context compaction.
Extract and place here during the first strategic phase.

(PROTECTED — preserve verbatim during workspace rewrites unless provably wrong.)

## Design System

Key design tokens and conventions discovered during pattern audit:
- **Theme**: (e.g., Catppuccin Mocha dark)
- **Primary accent**: (e.g., --accent-color: #cba6f7)
- **Base font**: (e.g., system sans-serif, 14px)
- **Card pattern**: (e.g., surface-0 bg, border-color border, 8px radius)
- **Layout**: (e.g., split-panel desktop, tab-bar mobile)

(Update as you discover more. Keep compact.)

## Component Inventory

Existing components relevant to the current feature:
- **[Component]**: path, purpose, when to reuse

(Only list components you'll actually reference in mockups.)

## Deliverables

Track mockups and their completion status:

| Mockup | File | Status | States Covered |
|---|---|---|---|
| (from plan.md) | mockups/... | pending | default, empty, ... |

## Key Decisions

Design decisions AND their reasoning. Without the WHY, you may revisit unnecessarily.

(Keep only decisions that affect future mockups. Remove resolved ones.)

## Status

Current position (update each strategic phase):

- **Phase**: (name from plan.md)
- **Blocked**: (active blockers, or "none")

(This section can be freely rewritten during strategic phases.)

## Failed Approaches

Visual approaches that were tried and did NOT work, with the reason.
This prevents retrying the same failed design direction after context compaction.

(Example: "Tried horizontal card layout for job list — too cramped at mobile widths, switched to vertical stack.")

(PROTECTED — only remove entries when the underlying issue is confirmed resolved.)
