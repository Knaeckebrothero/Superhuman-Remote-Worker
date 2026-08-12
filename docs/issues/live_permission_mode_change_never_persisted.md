# Live permission-mode changes apply but are never persisted (a pod restart silently reverts them)

**Filed:** 2026-08-08, from the k3d live gate for the workspace tier row
([[workspace_tier_upgrade]] §7b). Found incidentally: the permission-mode select was
being used as a control-transport probe while verifying the new tier row, and its
change never reached the database.

**Severity:** Medium, but the failure mode is worse than the impact suggests — it is
**silent and looks like success**. Nothing surfaces an error, and the value survives a
page reload, so a user has every reason to believe a security-relevant setting stuck
when it did not.

**Status:** OPEN. Not fixed as part of the tier-row work — that change does not touch
this path (see *Attribution*).

---

## Symptom

Changing **Permission Mode** from the live session settings pane applies to the running
agent, but is never persisted:

- `threads.permission_mode` stays at its creation-time value
- `threads.metadata.config_override.interactive.permission_mode` is never written either

Reproduced on k3d against thread `57d3b707-110c-4b15-af2d-5983f0a51c0a` (a live
persistent session) with `auto_accept` and again with `autonomous`. The column read
`supervised` throughout.

## Why it looks like it worked

The change *does* take effect on the running session, and it survives a full page
reload — which is what makes this hard to notice. The reload path is:

`chat.permissionMode()` ← the agent's report on connect → `liveConfig()` →
`resolvedPermissionMode()` → the select.

So the pane renders the agent's **in-memory** mode, not a stored one. An agent pod
restart drops the session back to its creation-time mode with no notification.

## What is NOT broken

Do not debug this as a transport or dispatch problem — that ground is already covered:

- The pane's debounce/diff **does** fire (`settings-pane.component.ts`, `onSettingsChange`
  → `desiredState` → dispatch).
- The `mode.set` frame **does** reach the agent — behaviour changes.
- The WS control path is healthy generally: during the same session a
  `upgrade-to-workspace` control frame round-tripped end to end (virtual → sandbox,
  pod provisioned, tier persisted).

Only durability is missing. `docs/done/2026-07-16-live-session-settings.md:93` describes
this path as doing a **"top-level column sync"** alongside the dedicated frame. That sync
is what is absent.

## Attribution — established, not assumed

The bug was first suspected to be a regression from the tier-row change, since it was
found while testing it. It is not:

1. The tier-row commits (`e28a6b86`, `b7bb716a`) do not touch `onSettingsChange`, the
   debounce, `desiredState`, `lastApplied`, `setMode`, or `persistent-chat.service.ts`
   at all — verified by diff.
2. Reproduced directly on the **pre-change build**: the cockpit runtime files were
   checked out at `e28a6b86~1`, tilt re-synced, `ng serve` rebuilt, and setting
   `auto_accept` on that build also left the column at `supervised`. Code restored
   afterwards.

An earlier attempt at this test was invalidated because the probe landed ~8 s before a
workspace upgrade completed and could have raced the agent's config re-derivation; the
result above is from a clean re-test with nothing in flight.

## Where to look

The persistence seam for the `mode.set` frame — agent side and/or the orchestrator
endpoint it calls — not the cockpit. Worth checking whether `narration_mode` has the
same gap, since it travels the same dedicated-verb path
(`setNarrationMode`), and whether the `config.update` route (which *does* PATCH
`/config` — observed writing `llm.model` and `workspace.backend` successfully in the
same session) persists `interactive.permission_mode` correctly when it is the carrier
instead.

## Related

- [[workspace_tier_upgrade]] §7b — the live pane surface this was found through
- [[tool_configuration_defects_and_fix_roadmap]] — same family of live-pane seams that
  cannot fully express or persist state
