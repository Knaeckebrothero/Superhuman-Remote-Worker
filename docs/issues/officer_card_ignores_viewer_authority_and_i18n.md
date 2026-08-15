---
tags:
  - issue
  - officers
  - cockpit
  - authorization
  - i18n
status: open
priority: P1
created: 2026-08-15
aliases:
  - OC-10
  - officer card false controls
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_post]]"
  - "[[officer_backlog_pools]]"
---

# The officer card shows owner controls to viewers and bypasses i18n

**Status:** OPEN. Audit finding **OC-10**.

## Problem

The officer GET endpoint correctly permits project viewers, but
`project-detail.component.ts` passes no role/`canManage` fact into
`ProjectOfficerComponent`. The component renders commission, edit, hold, release,
decommission, policy, and conference controls to everyone. Server owner gates prevent the
mutation, so a viewer receives a UI that invites actions which always fail.

The component also contains extensive inline English and imports no Transloco support;
there is no matching officer namespace in `en.json`/`de-DE.json`.

## Required direction

- Derive a server-consistent management capability from the current project membership and
  pass it explicitly to the card (or include a safe capability in the summary).
- Keep read-only operational state visible to viewers while hiding/disabling mutations with
  an honest explanation.
- Move every user-visible label, message, error fallback, status, and accessibility string
  into Transloco keys in both locales.
- Do not infer authority solely from global admin or from a client-side role guess.

## Acceptance

- Viewer rendering contains no actionable owner-only controls; owner/admin behavior remains
  available and server-authorized.
- Editor behavior follows one documented project policy and is tested.
- A role change while the page is open refreshes the capability without requiring a new
  login.
- English and German catalogs have exact parity and `npm run i18n:check` passes.
- Component tests cover vacant, commissioned, held, conference, backlog-warning, and failed
  mutation copy in both authority modes.
- Keyboard and screen-reader labels are localized, not just visible text.

## Dependencies

The new auto-pull and roster controls must use this capability when
[[officer_post_cannot_enable_auto_pull]] and
[[officer_roster_patch_cannot_remove_or_drain_a_slot]] land.
