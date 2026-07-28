---
tags:
  - issue
  - cockpit
  - sessions
related:
  - "[[session_tool_group_checkbox_disagrees_with_the_agent]]"
  - "[[2026-07-16-live-session-settings]]"
---

# The settings pane fetches a thread's config once per session and never again, so a policy change made outside the pane shows stale until you switch sessions

**Filed:** 2026-07-28, split out of
`docs/done/session_tool_group_checkbox_disagrees_with_the_agent.md`.
**Status:** OPEN. Pre-existing for `config_override`; the tool-groups endpoint
inherited it.
**Severity:** low — the stale window needs an out-of-band config change to open
at all, and switching sessions clears it.
**Component:** `cockpit/src/app/views/chat/settings-pane.component.ts:235`
(`prefilledThread`), `:291-292` (the load effect), `loadThread`.

## Summary

The pane's load effect is guarded by thread identity:

```ts
if (this.prefilledThread === threadId) return;
this.prefilledThread = threadId;
this.loadThread(threadId);
```

`loadThread` is therefore the **only** writer of `threadOverride` and
`serverToolGroups`, and it runs at most once per thread per pane lifetime —
closing and reopening the pane does not re-fetch (confirmed on dev 2026-07-28:
one `/tool-groups` request across an open → close → reopen cycle).

That is correct for changes the pane itself makes, which it applies optimistically
and the server echoes back over `config.changed`. It is wrong for changes made
anywhere else:

- an admin edits the expert the session is bound to,
- a project-expert link's `config_override` changes,
- the session's `config_override` is edited through
  `PATCH /api/persistent/threads/{id}/config` from another client,
- account-level session defaults change.

In all of those the pane keeps rendering the answer it fetched on open. Because
the pane is pin-only and diffs against `lastApplied`, the user's next toggle also
diffs against the stale baseline.

## Why it wasn't fixed with the tool-groups work

The same staleness already applied to `threadOverride` (model, temperature,
workspace tier) before the tool-group endpoint existed; the endpoint did not
introduce it, it joined it. Fixing it means choosing an invalidation signal, and
that is a design decision rather than a bug fix.

## Fix

Needs an invalidation trigger. Options, roughly in order of cost:

1. **Re-fetch on pane open** — drop the `prefilledThread` short-circuit for the
   fetch (keep it for the prefill-once semantics). Cheap, bounded, and covers
   the realistic "admin changed something while I had the tab open" case.
   **Careful:** re-fetching must not re-anchor `lastApplied` mid-edit or
   re-prefill over a pending pin — that is the trap recorded in
   `docs/done/2026-07-16-live-session-settings.md` and widened in the
   tool-groups change. Any re-fetch has to reconcile, not clobber.
2. **Server push** — extend the existing `config.changed` broadcast to carry the
   resolved tool groups, so out-of-band edits reach open panes the same way the
   pane's own edits do. Correct, but needs the orchestrator to know which
   threads an expert/project change affects.
3. **TTL / focus-based revalidation** — refetch when the tab regains focus.
   Conventional, but adds a request pattern the codebase does not otherwise use.

Option 1 plus the reconcile rule is probably the right first slice.
