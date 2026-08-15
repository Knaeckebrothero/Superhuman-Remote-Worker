---
tags:
  - issue
  - officers
  - communication
  - rate-limits
  - autonomy
status: open
priority: P1
created: 2026-08-15
aliases:
  - OC-07
  - officer traffic consumes user quota
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_message_routing]]"
---

# Officer-only worker messages consume the human interruption quota

**Status:** OPEN. Audit finding **OC-07**.

## Problem

`send_agent_message` calls `check_message_rate_limit` before it resolves the project’s
effective routing policy. Officer-first traffic that never reaches the user is also logged
as outbound in the same ledger queried for per-job/per-user human quotas.

The internal chain of command can exhaust the user’s 5/hour, 15/day, or 30/day limits. A
later genuine page then fails because the officer handled earlier questions exactly as the
policy requested.

## Required direction

- Resolve the effective audience before selecting a rate-limit bucket.
- Count human quotas only when durable delivery intent includes the user.
- Give officer-only traffic a separate project/job flood-control bucket with bounds suited
  to internal triage.
- When an officer escalates later, charge the human bucket at escalation time exactly once.
- Store audience/routing generation on the message/route so quota queries do not infer it
  from ambiguous `direction='outbound'` rows.

## Acceptance

- Officer-first messages handled solely by the officer do not change human quota counters.
- User-direct and immediate-both each charge the correct human counters once.
- Later escalation of officer-first charges once, including retries without duplicates.
- Internal flood controls stop a runaway worker without consuming or bypassing human limits.
- Policy fallback from unavailable officer to user is charged to the user bucket.
- Existing explicit-recipient behavior remains documented and tested.

## Dependencies

Implement against the durable routing snapshot, not transient policy re-resolution. It can
land independently of auto-pull.
