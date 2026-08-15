---
tags:
  - issue
  - officers
  - cockpit
  - configuration
  - backlog
status: open
priority: P1
created: 2026-08-15
aliases:
  - BP-11
  - officer slot deletion no-op
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_post]]"
  - "[[officer_backlog_pools]]"
---

# Officer roster PATCH cannot remove one slot or set a zero-count drain

**Status:** OPEN. Audit finding **BP-11**.

## Problem

Post and thread configuration writers recursively deep-merge `officer.slots`. Cockpit’s
`removeSlot()` sends the desired remaining map; omitted keys survive the server merge, so
removing one slot among several is a no-op. Only clearing the entire roster with
`slots:null` works.

The server accepts slot counts from 0 through 20, but `buildSlotsSpec()` and `toCount()`
clamp to a minimum of 1. The documented zero-count drain/disable cannot be expressed in
the card.

## Required direction

- Give `slots` replace-map semantics at the Officer Post PATCH boundary, or define explicit
  per-key tombstones. Recursive merge remains appropriate for unrelated nested config, not
  for a desired-complete roster.
- Mirror the resulting whole roster to the current thread under the lifecycle lock.
- Permit the full server-supported 0–20 range and explain that zero prevents new admission
  but does not cancel current jobs.
- Validate duplicate/blank/renamed slot keys and preserve category/model/backend/ceiling
  fields without ghosts.

## Acceptance

- Remove one of two slots through the UI/API; GET, durable row, live thread, admission, and
  recommission all show only the remaining slot.
- Rename a slot without retaining the old key.
- Set count 0 with jobs in flight: no new job enters, existing utilization remains visible,
  and raising the count reopens capacity.
- `slots:null` still reverts to the documented flat-cap behavior.
- Concurrent edits are serialized or rejected with revision/CAS rather than silently
  merging two complete maps.

## Dependencies

Use owner capability from [[officer_card_ignores_viewer_authority_and_i18n]] and stable
post locking from [[officer_admission_does_not_lock_the_durable_post]].
