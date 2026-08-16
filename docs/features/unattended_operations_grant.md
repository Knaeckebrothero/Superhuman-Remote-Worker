---
tags:
  - feature
  - grants
  - projects
  - self-improvement
  - orchestration
status: approved
created: 2026-08-16
related:
  - "[[project_self_improvement_loop]]"
  - "[[loop_unified_engine]]"
  - "[[centurion]]"
  - "[[officer_post]]"
  - "[[merged_pr_completion_grant]]"
  - "[[global_expert_management]]"
---

# Put project loops and the officer behind a capability grant

**Status: implemented on develop, 2026-08-16. Not deployed.**

## 1. What and why

Two surfaces in the product spawn jobs with **no human clicking anything**:

- the **project self-improvement loop** — start it, and the completion hook keeps rotating
  scholar → critic → developer until a budget, a deadline, or a failure streak stops it;
- the **commissioned officer** (centurion) — a standing background thread that wakes on
  events and dispatches work from the project backlog on his own judgement.

Until now the only gate on either was a **project role**: any project owner could commission
an officer, any project *editor* could start a loop. Role answers "whose project is this",
which is the wrong question. The right one is "may this principal put unattended, unbounded
token spend in motion" — and the answer varies per person and per project in a way a role
cannot express.

The failure mode is not abuse. It is an ordinary user starting a loop to see what it does,
walking away, and discovering the bill in the morning.

## 2. Mechanism: one catalog key

`unattended_operations` — `bool`, **default `False`**, `restrict_only`, in the
`src/core/capability_grants.py` `CATALOG`. Being a catalog key it needs no migration (rows
are overrides; the default lives in code) and no admin-UI work (**Admin → Grants** renders
whatever the catalog exposes).

Deny-by-default and **not backfilled**, matching `catalog_authoring` and
`complete_unmerged_pr`: nobody held a key for this before, so there is nothing to
grandfather, and a backfill would hand the capability to every existing user silently.
Admins bypass, as everywhere in the grants plane.

### One key, not two

The officer and the loop are separable *today* — a standard loop needs no officer, and a
commissioned officer dispatches from the backlog with no loop running. They are gated
together anyway, because officer scheduling is already a **mode of the loop engine**
(`loop_unified_engine.md`, "Third mode added 2026-07-30") and the two surfaces are
converging. A split would have to be re-merged.

## 3. Where it is enforced

The two surfaces are *reached* differently, so the gate has two shapes. Both matter.

| Path | Enforcement |
|---|---|
| `POST /api/projects/{id}/loop` (start) | 403 in the router |
| `POST /api/projects/{id}/loop/resume` | 403 in the router |
| `POST /api/projects/{id}/loop/scheduling` (→ officer) | 403 in the router |
| `POST /api/projects/{id}/officer/commission` | 403 in `main.py`, before anything mutates |
| every loop spawn | re-read in `main._spawn_loop_stage` |
| `officer.enabled` in any session config | the `evaluate` PDP |

**The officer half rides the config PDP.** `evaluate` refuses `officer.enabled` without the
grant. That covers the commission endpoint *and* a hand-rolled thread create carrying the
flag in `config_override` — and because sessions re-resolve grants on **every attach**
(there is no freeze), revoking the grant stands a **running** officer down rather than only
blocking new ones. The commission endpoint additionally checks up front, because the PDP
would otherwise fire *after* `update_project_officer_post` has already written the kit — a
422 on a half-applied commission.

The rule gates on `officer.enabled` **only**. Slots, sleep bounds, spend ceilings and pools
are inert configuration on a post nobody holds, so a user without the grant may still read
and edit the durable kit; he just cannot raise anyone onto it. `strip_to_grants` mirrors
this, dropping only `.enabled` — the same shape as the `delegation` rule, and for the same
reason.

**The loop half has no config fragment**, so it is read directly via
`postgres_db.user_can_run_unattended_operations(user, project_id)` — the same shape as
`user_can_complete_unmerged_pr`, including the `project_id` passthrough (dropping it would
silently reduce the key to a user-only capability) and the **fail-closed** exception
handler: a capability whose read failed is not a capability the caller has.

### Why the spawn choke point too

Every loop spawn — the start endpoint's first stage, the rotation advance, the campaign
advance — funnels through `_spawn_loop_stage`. Gating only the endpoints would make the
grant a *starting* permission, leaving an already-running loop spending forever after its
owner's grant was revoked. The re-check lives at the choke point instead of the three call
sites, and raises; both advance callers already catch a spawn failure and stop the loop with
the reason in `last_error`.

The grant is resolved for the loop's **owner**, not for whoever triggered the advance — the
spawned jobs run as the owner, so the owner's entitlement is the one that must still hold.
An ownerless loop (a system child) is not gated, matching `_enforce_job_create_grants`.

### What is deliberately NOT gated

Reading a loop, reading the backlog, **pausing**, and **stopping** — plus officer
decommission, hold and release. Nobody may be locked out of *halting* work that is already
running. The fail-closed direction here is "no new work", never "no control": a grant
revoked mid-run must not strand a live loop with no way to stop it from the UI.

## 4. Cockpit

`CapabilitiesService.canRunUnattendedOperations` gates the project **Loop** and **Centurion**
tabs, which are filtered out of the tab list entirely rather than disabled — there is nothing
useful to show a user who cannot start either. It **fails closed while loading and on fetch
error**, the same posture as `canPublishDatasources`: the tabs appear only once a successful
capabilities fetch proves entitlement.

The tab *content* carries the same guard, so a grant revoked while the tab is open makes the
surface disappear on the next capabilities load rather than lingering.

Hiding is UX only. The orchestrator refuses start / resume / convert / commission with 403,
and the PDP refuses `officer.enabled`, regardless of what rendered.

## 5. Rollout

Nothing to migrate and nothing to backfill. On deploy:

- **admins** are unaffected (bypass);
- **every non-admin** loses the Loop and Centurion tabs until granted;
- **a loop already running** for a non-admin owner halts at its next advance, with
  `last_error` naming the grant, rather than dying silently.

To restore access for a user or a project, set `unattended_operations = true` at the
matching scope under **Admin → Grants**. Restrict-only still applies: a global `false` holds
against a per-user `true`, which is the deployment-wide off-switch if one is ever wanted.

## 6. Tests

- `tests/test_capability_grants.py` — catalog shape; the `evaluate` rule (including that the
  string `"false"` is not read as enabled, matching `main._officer_meta_enabled`); the
  `strip_to_grants` branch; the completeness harness that fails if a future catalog key
  arrives with no strip branch.
- `tests/test_unattended_operations_grant.py` — the router's gated verbs, the verbs that must
  stay ungated, and the capability read (admin bypass, scope resolution, restrict-only,
  fail-closed).
- `tests/test_loop_unified_advance.py` — the spawn choke point.
- `tests/test_officer_lifecycle.py` — the commission 403 fires before anything mutates.

Not covered by unit tests, and owed as a k3d exercise: the end-to-end UI path (tabs vanish
for a non-admin, reappear after the grant is set) and a live revocation halting a running
loop on the cluster.
