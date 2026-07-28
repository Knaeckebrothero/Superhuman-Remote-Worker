---
tags:
  - issue
  - orchestrator
  - config-resolution
  - tech-debt
related:
  - "[[session_tool_group_checkbox_disagrees_with_the_agent]]"
---

# The enabled/disabled rule for session tool groups is implemented twice — once as disable markers on the attach path, once as a boolean predicate for the read API

**Filed:** 2026-07-28, split out of
`docs/done/session_tool_group_checkbox_disagrees_with_the_agent.md`.
**Status:** OPEN by choice, not oversight. Drift is currently caught by a test
rather than prevented by construction.
**Severity:** low — the two implementations agree today and a test fails if they
stop agreeing. The cost is that a future change must remember there are two.
**Component:** `orchestrator/main.py:3506` (`_SESSION_TOOL_DISABLED_MARKERS`),
`orchestrator/main.py:3556` (`_session_tool_group_disabled_markers`),
`src/core/session_tool_overrides.py:103` (`session_tool_group_enablement`).

## Summary

Both functions encode the same rule — *a session tool group is disabled iff its
merged list is exactly `[]`* — in opposite polarity:

- `_session_tool_group_disabled_markers` returns `{marker_name: True}` for each
  empty group. It runs on the **live attach path**, where its output is folded
  into the resolved blob the agent hydrates and turns into
  `_fleet_management_disabled` and friends.
- `session_tool_group_enablement` returns `{group: bool}`. It backs
  `GET /api/persistent/threads/{id}/tool-groups`, the read surface the Cockpit
  checkboxes render from.

If those two ever disagree, the UI resumes lying about the agent's toolset —
which is the exact bug the endpoint was built to fix.

## Why it was left duplicated

Rewriting `_session_tool_group_disabled_markers` in terms of the shared
predicate is mechanical and covered by
`tests/test_session_config_plumbing.py::test_session_tool_group_disabled_markers`.
It was still the single change in that work with the power to alter a **running
session's toolset**, because it sits on the attach path — and the fix that
motivated it was a display bug. Trading a real (if small) runtime risk for a
structural guarantee was not worth it in that change.

Instead,
`tests/test_session_tool_groups_endpoint.py::test_lean_resolve_matches_full_resolve_markers`
runs both paths over a parametrized matrix of expert / project / request layers
and asserts the four booleans agree. That is the drift protection; this issue is
about replacing it with impossibility.

## Fix

Reimplement `_session_tool_group_disabled_markers` as the negation of
`session_tool_group_enablement`, keeping `_SESSION_TOOL_DISABLED_MARKERS` as the
group→marker-name map. Keep the agreement test afterwards — it costs nothing and
becomes a tautology-check rather than the only guard.

Worth pairing with an attach-path smoke on a live cluster, since the unit tests
cover the function but not the hydration that consumes its output.
