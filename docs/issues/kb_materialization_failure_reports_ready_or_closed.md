---
tags:
  - issue
  - officers
  - backlog
  - knowledge
  - data-integrity
  - git
status: open
priority: P1
created: 2026-08-15
aliases:
  - BP-08
  - false successful KB disposition
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_knowledge_plane]]"
  - "[[officer_backlog_pools]]"
---

# KB materialization failure can report a ticket as ready or closed

**Status:** OPEN — truthful-write blocker/K4. Audit finding **BP-08**.

## Problem

`knowledge_tools._materialize_note()` deliberately returns a failed result rather than
raising. `kb_write`/`kb_update` callers ignore it, continue updating projections, and
return success text. The officer can be told that a ticket is READY or closed while its
canonical OKF file was not changed.

The file and index then disagree. A later git reindex can restore stale tags/status and
resurrect work the officer believed dispositioned.

## Required direction

- Make materialization outcome part of every knowledge mutation result.
- Define which store is authoritative during degradation. The current feature contract says
  canonical project knowledge must not silently diverge from projections.
- For dispatch authorization and closure, fail closed or enter an explicit pending-sync
  state that the tick treats as ineligible/unresolved.
- Persist retry intent and expose it in the officer’s knowledge/SITREP availability section.
- Do not return “Updated,” “READY,” or “closed” unless the required durable boundary
  succeeded.

## Acceptance

- Inject missing writable binding, git unavailable, push conflict, malformed frontmatter,
  and materializer exception. Every tool result names the failure/degraded state.
- A failed ready write does not dispatch; a failed close does not release executor
  disposition or disappear from the backlog.
- Retry convergence updates file and projection once without bumping `ready_at` twice.
- Reindex during/after failure cannot resurrect or invent state silently.
- Cockpit/officer tools distinguish canonical, pending-sync, failed, and projection-only
  observations.
- Existing add-only/tag-removal semantics remain intact.

## Dependencies

Authorization from [[backlog_machine_tags_trust_any_persistent_session]] must be enforced
before the mutation begins; this issue governs truth after an authorized mutation.
