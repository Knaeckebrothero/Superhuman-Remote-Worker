---
tags:
  - issue
  - officers
  - backlog
  - cockpit
  - configuration
status: open
priority: P0
created: 2026-08-15
aliases:
  - BP-01
  - unreachable auto-pull control
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_post]]"
  - "[[officer_backlog_pools]]"
---

# The Officer Post API and Cockpit cannot enable auto-pull

**Status:** OPEN — functional release blocker. Audit finding **BP-01**.

## Problem

Cockpit’s `OfficerPostPatch` type declares `auto_pull` and
`worker_spend_ceiling_daily`, and the GET summary renders both. The server’s
`_OFFICER_POST_EFFECTS`/numeric allowlists omit them, so commission and PATCH reject those
fields as unknown. The officer card displays auto-pull state but offers no control for it,
the century spend ceiling, or a pool’s `spend_ceiling_daily`.

The mounted tick is therefore dormant through every supported Officer Post surface. It can
be armed only by mutating thread/row JSON out of band, bypassing the durable configuration
contract.

## Required direction

- Add validated, owner-only post fields for `auto_pull` and the century spend ceiling.
- Add validated per-slot spend ceilings without changing `slots` replacement semantics
  chosen in [[officer_roster_patch_cannot_remove_or_drain_a_slot]].
- Persist auto-pull authority on the durable post and mirror only the documented runtime
  projection to the commissioned thread.
- Give each field an honest effect label and continuity behavior across recommission.
- Add Cockpit controls with explicit confirmation that enabling starts unattended spend.
- Preserve the safe database and UI default: off.

## Release guard

Building this control must not make it usable before the other P0 invariants land. The
server may expose the field while refusing `false → true` behind a temporary release gate;
silent out-of-band enablement is worse.

## Acceptance

- Commission/PATCH/GET round-trip all three spend/enable layers with owner authority.
- Viewer/editor permissions follow the project’s chosen management policy and are tested.
- `auto_pull=false` dispatches nothing; a deliberate enable starts only the current
  commissioned post after transactionally revalidating it.
- Disable/hold/decommission wins races against the next dispatch.
- Settings survive recommission and the UI reports their actual source/effect boundary.
- First live enable uses disposable non-executor tickets after every P0 audit gate passes.

## Dependencies

Do not release the enable transition before
[[officer_admission_does_not_lock_the_durable_post]],
[[backlog_machine_tags_trust_any_persistent_session]], and
[[backlog_fixed_windows_starve_eligible_tickets]] are closed.
