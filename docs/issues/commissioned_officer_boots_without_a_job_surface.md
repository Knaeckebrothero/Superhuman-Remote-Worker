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

# A commissioned officer booted without any job surface — or the right to use it

**Status:** FIXED 2026-08-15 (`5c2c5030`, `c62b8eae`). Two independent
defects in the same request; recorded because the way they hid is more
instructive than either one-line fix.

**The second one is the worse of the two.** With no `interactive` block on the
post, commission fell to the create endpoint's `supervised` default. An
officer is **headless** — there is no session in which a human could answer a
permission prompt — so every wake went:

```
Permission gate unanswered for tool get_current_project
  (call_S3LoKz…) — parking turn; 8 call(s) left ungated
repair_tool_pairing: dropped 0 orphaned tool result(s),
  stripped 14 orphaned tool call(s)
```

He issued 6–8 calls per turn, the first parked the turn, the rest were
stripped as orphans on the next pass, and he executed **nothing** — no reads,
no dispatches, no sleep filed, empty assistant text, zero `tool` rows ever
persisted.

That single default is the true origin of three separate symptoms chased for
an hour as independent bugs: the "permanent wedge" (orphaned calls piling up
into a provider 400), the thread that never wrote `tool` rows, and an officer
who looked healthy on every surface while accomplishing nothing. Every
symptom pointed at the model or the provider. None pointed at a config
default. `permission_mode` is now defaulted to `autonomous` at commission
(`OFFICER_PERMISSION_MODE`), with an explicit pin still winning.

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
UPDATE threads
   SET config_name = 'centurion',
       permission_mode = 'autonomous',
       metadata = jsonb_set(metadata, '{config_override,interactive}',
                            '{"permission_mode":"autonomous"}'::jsonb, true)
 WHERE id = '<officer-thread>';

-- so a recommission inherits it too
UPDATE project_officers
   SET config_override = config_override
       || '{"interactive":{"permission_mode":"autonomous"}}'::jsonb
 WHERE project_id = '<project>';
```
then delete his pod; the watchdog respawns him onto the correct toolset
within ~30 s.

## Diagnostic that would have found both in one minute

Compare a broken officer against one known to work:

```sql
SELECT LEFT(id::text,8), config_name, permission_mode
  FROM threads WHERE id IN ('<broken>', '<known-good>');
```

`centurion`/`autonomous` versus `session_base`/`supervised` is the whole
story. Reach for this before reading any agent logs — both defects are
invisible in the pod's own output, which reports a healthy session doing
nothing.
