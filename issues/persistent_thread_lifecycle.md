# Persistent thread lifecycle: auto-end suspended threads

## Problem

Clicking on a "sleeping" persistent session in the cockpit silently restores
the S3 snapshot and spins up a fresh pod + agent. There is no way to peek at
a thread's history without paying the cost of waking it up.

Underlying cause: "sleeping" isn't an end-state, it's a *suspended workspace*.
After `WORKSPACE_IDLE_TIMEOUT` (default 30 min) on an `idle` thread,
`WorkspaceSuspensionService.check_idle_threads()`
(`orchestrator/services/workspace_suspension.py:677`) snapshots the workspace
to S3 and deletes the pod. The thread row stays alive; only
`metadata.workspace_container.status` flips to `suspended`. The UI has only
one path to interact with such a thread — restore + reattach — so any click
restarts it.

There is **no max-idle sweep that escalates to `ended`**. Threads only become
`ended` via the explicit DELETE in `orchestrator/main.py:10129` (the "End"
button in the UI). `ide_session.py` has a `max_lifetime_minutes`, but that's
for the code-server IDE wrapper, not for chat threads. In practice users
rarely click End — they walk away — so suspended threads accumulate
indefinitely.

## Proposal

Add a second, longer timer that auto-soft-ends threads that have been idle
for >Nh. Soft-end already preserves Gitea repo + cloud session folder + the
S3 workspace snapshot, so resume still works identically. Only the status
badge and the agent/pod accounting change.

Suggested shape:

1. **Soft-end timer** (e.g. 2h, env-configurable like
   `WORKSPACE_IDLE_TIMEOUT`). Extend `check_idle_threads()` with a second
   pass that calls `postgres_db.end_thread(...)` when a thread has been
   `idle` + `suspended` for longer than the threshold.
2. **(Optional) Hard-expiry timer** (e.g. 30d) that triggers a permanent
   delete — the same path as `DELETE /api/persistent/threads/{id}?permanent=true`
   — to actually reclaim S3 / Gitea / cloud session storage. Soft-end
   alone never reclaims anything.

Two timers is the clean version. One timer (soft-end only) is fine if we'd
rather keep snapshots forever and just clean up status.

## Open questions

- What's the right soft-end threshold? 2h is the user's instinct; could
  also be 24h to match a "didn't come back today" mental model.
- Do we surface the auto-ended state differently in the UI from a manual
  end, or is it the same badge?
- For hard-expiry: is 30d enough, and do we want a one-time warning email
  before permanent deletion?

## References

- `orchestrator/services/workspace_suspension.py:677` —
  `check_idle_threads()` is where the new sweep lives
- `orchestrator/main.py:10129` — `end_thread` endpoint, the soft/permanent
  end logic to call into
- `orchestrator/main.py:10202` — `resume_thread`, already accepts both
  `ended` and `idle` so soft-ended threads resume without changes
