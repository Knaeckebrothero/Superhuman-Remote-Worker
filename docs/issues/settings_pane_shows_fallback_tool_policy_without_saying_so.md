---
tags:
  - issue
  - cockpit
  - sessions
  - ux
related:
  - "[[session_tool_group_checkbox_disagrees_with_the_agent]]"
---

# When the tool-group resolve fails, the settings pane silently renders base defaults as if they were the session's real policy

**Filed:** 2026-07-28, split out of
`docs/done/session_tool_group_checkbox_disagrees_with_the_agent.md`.
**Status:** OPEN.
**Severity:** low — the fallback is correct for a stock session, so the common
case is right. It is wrong exactly when the session is *non*-stock (an expert or
project layer changed a group), and it is wrong silently.
**Component:** `orchestrator/main.py:22662` (the `source: "error"` branch),
`cockpit/src/app/core/services/api.service.ts:1576` (`getSessionToolGroups`),
`cockpit/src/app/views/chat/settings-pane.component.ts:216`
(`serverToolGroups`).

## Summary

`GET /api/persistent/threads/{id}/tool-groups` returns three states. Two carry
an answer (`resolved`, `legacy`); the third does not:

```json
{"thread_id": "…", "source": "error", "tool_groups": null}
```

That is deliberate — a resolve error **refuses** the session attach
(`main.py:3159-3167`, `:19040-19044`), so there is genuinely no agent answer to
report, and guessing from `config_override` would reintroduce the original bug.

The client collapses that state, plus any transport failure and a 404 from an
orchestrator predating the endpoint, into `null`, and falls back to
`SESSION_TOOL_GROUP_BASE_ENABLED` — the `session_base.yaml` mirror. The
checkboxes then render base defaults with no indication that the session's own
expert / project layers were never consulted.

`source` is currently discarded entirely: `getSessionToolGroups` maps the
response to `tool_groups` and throws the rest away, so the pane cannot tell
`error` from `legacy` from a network failure even if it wanted to.

## Why this is not just cosmetic

The fallback's blind spot is precisely the sessions where the answer matters. A
stock session's real policy *is* the base default, so nobody notices. A session
whose expert disables Canvas, or whose project link enables Automations, gets
rendered as though those layers do not exist — and because the pane is
"pin-only", the user's next toggle diffs against that wrong baseline.

## Fix

Surface the state rather than flattening it:

1. Widen `getSessionToolGroups` to return `{source, tool_groups}` instead of
   just the map (the `SessionToolGroupsResponse` interface already models it).
2. Keep the mirror fallback, but when `source` is `error` — or the request
   failed — render a small warning affordance on the Tools group: *"couldn't
   verify this session's tool policy; showing defaults."*
3. Consider offering a retry, since the failure is often transient (the resolve
   touches Postgres).

Distinct from — but worth fixing alongside —
[[settings_pane_never_refetches_a_threads_config]], which is the other way the
pane's tool-group display can be confidently wrong.
