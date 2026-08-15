---
tags:
  - issue
  - officers
  - configuration
  - lifecycle
status: resolved
priority: P0
created: 2026-08-15
aliases:
  - LF-6
  - commission uses session_base
related:
  - "[[officer_backlog_pools_resavio_livefire]]"
  - "[[officer_post]]"
---

# A commissioned officer booted without any job surface

**Status:** FIXED 2026-08-15 (`5c2c5030`). Recorded because the way it hid is
more instructive than the one-line fix.

## Problem

`POST /api/projects/{id}/officer/commission` built its `ThreadCreateRequest`
without a `config_name`. That field defaults to `session_base`, so the
officer booted on the generic session expert instead of
`config/experts/centurion/`.

Consequences, all silent:

- **No `job_control` plane at all** — no `create_job`, `steer_job`,
  `approve_job`, `cancel_job`, `pause_job`, or message-routing actions. The
  officer could not dispatch, steer, or answer a worker. His entire charge
  was unreachable.
- **No `job_inspection`/evidence tools** — no `list_jobs`, `get_job`,
  `get_stuck_jobs`, `read_job_evidence`. He could not even see the century.
- **Research and citation tools instead**, which `centurion` deliberately
  empties (`research: []`, `citation: []`) so that every wake's tool choice
  is obvious.
- **A workspace he should not have** — `centurion` sets
  `workspace.backend: none`, which is how klug-und-faul is enforced
  structurally rather than by persona.

Observed live: the endpoint-commissioned officer loaded **34 tools, none of
which could create a job**, while the July officer — provisioned by hand with
`config_name=centurion` — loaded **49**.

## Why it survived a full O-series build and a live-fire

Every officer that had ever worked was raised through the **manual
provisioning recipe** (`POST /api/persistent/threads` with an explicit
config) or the cockpit provision form. The commission endpoint was exercised
by tests that asserted the kit, the model, the reasoning level, the
permission mode and the continuity brief — but never **which expert the
funnel was handed**. A thread was created, it attached, it took turns, it
even filed KB notes. It simply could not command.

The live-fire did not catch it either, because the live-fire officer was the
July thread. It surfaced only on the change of command, when a *new* officer
was commissioned through the endpoint for the first time in anger.

## Fix

`OFFICER_CONFIG_NAME = "centurion"`, passed explicitly at commission, and
`tests/test_officer_lifecycle.py::test_commission_goes_through_the_funnel_with_the_rows_kit`
now pins `req.config_name` so a default change surfaces as a failing test.

`officer.enabled` remains false in the expert and is flipped by the thread
override — the documented split, unchanged.

## Live repair for an already-commissioned officer

```sql
UPDATE threads SET config_name = 'centurion' WHERE id = '<officer-thread>';
```
then delete his pod; the watchdog respawns him onto the correct toolset
within ~30 s.
