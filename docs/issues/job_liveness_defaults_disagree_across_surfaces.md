---
tags:
  - issue
  - officers
  - jobs
  - liveness
  - tooling
status: open
priority: P1
created: 2026-08-15
aliases:
  - OC-08
  - split stuck-job threshold
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_supervision_surface]]"
  - "[[officer_backlog_pools]]"
---

# Job liveness defaults disagree across REST, tools, and officer state

**Status:** OPEN. Audit finding **OC-08**.

## Problem

The shared liveness helper/environment and the MCP inspection descriptor default to a
30-minute stall threshold. The REST stuck-jobs endpoint and officer/session-facing default
use 60 minutes. Callers that omit the option therefore receive contradictory classifications
for the same job.

Backlog stale-claim reporting compounds the mismatch: SITREP, Cockpit, and inspection tools
can disagree about whether one capacity slot is healthy, stale, or page-worthy.

## Required direction

- Define one server-owned liveness policy object/default and import/serialize it into every
  surface.
- Keep an explicit request override where useful, but label the effective threshold and
  source in every response.
- Make stale-claim thresholds derive from or explicitly differ from the same policy; no
  hidden magic numbers.
- Add drift tests across descriptor schema, REST query default, helper, SITREP, and Cockpit.

## Acceptance

- With no override, every surface classifies a fixed set of timestamps identically.
- Changing the deployment default changes all surfaces without editing several literals.
- Explicit 30/60-minute overrides remain deterministic and are reported in output.
- Terminal, waiting, paused, processing, and unavailable-heartbeat cases retain the shared
  six-state semantics.
- Stale-claim age/page decisions name their own effective threshold.

## Dependencies

No dependency on auto-pull enablement. Close before trusting liveness in the live-fire gate.
