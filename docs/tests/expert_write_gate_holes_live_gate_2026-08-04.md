---
tags:
  - test
  - live-gate
  - experts
  - grants
  - security
related:
  - "[[duplicate_expert_bypasses_user_experts_kill_switch]]"
  - "[[resume_job_grant_recheck_fails_open]]"
  - "[[dispatcher_resume_pep_twin_still_fails_open]]"
  - "[[tool_configuration_deferred_findings]]"
---

# Live gate — the expert-write kill switch and duplicate's grant handling

Run 2026-08-04 against **k3d** (`k3d-srw` / namespace `srw`) as the real non-admin
`catalog-gate@k3d.local` (`230ce19a`) over the MCP internal-header auth path,
because the grants half returns early for admins and gating as an admin would
exercise the permissive branch.

**Run twice**, deliberately: once against the refuse-outright behaviour
(`55080ed0`) and again after it was replaced by strip-and-report (`b0333217`). The
table below records the **final** behaviour; the superseded results are noted where
they differ, because this table doubles as the re-run recipe and a stale expected
value makes a passing check look like a failure.

The `user_experts` kill switch is **deployment-wide**, so the harness flips it and
restores the original row (or its absence) in a `finally` block.

Harness note: inside the orchestrator pod `/app` **is** the orchestrator package —
`main.py` is at `/app/main.py`, not `/app/orchestrator/main.py`, and the import
root is `services.…`.

## Results

| # | Check | Result |
|---|---|---|
| 0 | Baseline, switch enabled: non-admin duplicates a global expert | **200**, row owned by the caller |
| 1 | Switch OFF, all **five** expert-write routes | **403 × 5**, each *"User-defined experts are disabled by the administrator"* — `create`, `update`, `import`, `expert-defaults/{type}/fork`, `duplicate` |
| 1b | Rows owned by the test user across check 1 | **unchanged** (1 → 1) — no write slipped past a 403 |
| 2 | Switch ON. Non-admin duplicates a **visible** expert declaring `tools.shell`, holding no `shell_tools` grant | **200**, `dropped: ["shell_tools"]`, a row IS created, and the **stored** config has `tools.shell` removed (`tools` keys: `[]`). *Was 422 + no row under `55080ed0`.* |
| 3 | **Control:** the same duplicate as the **admin** | **200**, `dropped: []`, and the stored config **keeps** `tools.shell` (`tools` keys: `['shell']`) |

Check 1 is the reported defect. Check 2's **stored-config** assertion is the one
that matters most: a route reporting `dropped` while persisting the original would
be strictly worse than the refusal it replaced — it would mint exactly the
ungranted row this work exists to prevent, and claim it hadn't. Check 3 is what
makes check 2 mean anything: without it, check 2 would also pass if stripping fired
for everyone, admins included.

## Two harness bugs worth writing down

**A 404 masquerading as the wrong verdict.** The first run of check 2 returned
**404 Expert not found**. Cause was the harness: the staged source was
**admin-owned and private**, and `get_expert_visible_by_id`
(`orchestrator/database/postgres.py:9033`) admits a row only when `is_admin`,
`owner_id = caller`, `is_global = TRUE`, or a `project_experts` link exists — so
the call 404'd before reaching any gate. This is the same fact the issue doc leads
with (*visibility, not ownership, is the test on this route*) arriving from the
other direction. Staging now sets `is_global = TRUE`.

Worth keeping for **how** it would have failed silently: had the gate asserted only
*"no row was created"*, a 404 would have passed. A check that cannot tell a refusal
from an unreachable fixture is not a check.

**A control that stopped controlling.** Check 3 originally read *"the same
duplicate as the admin must succeed — proves the 422 is the grants PDP, not a
shape rejection"*. Once check 2 became a 200, that control proved nothing: both
users succeed, so a bare status comparison passes even if stripping ran for
everyone. Rewritten to assert the admin's stored config **keeps** the ungranted
key. A control has to be re-derived when the behaviour it contrasts against
changes, not just kept because it is green.

## Not covered

- **`resume_job`'s 409 has no live gate.** Staging needs a job in a resumable state
  whose stored config is permanently unusable. Covered by unit tests asserting
  **non-dispatch** rather than merely the status, in both directions — the fix
  narrows rather than reverses, so a transient error still proceeds.
- **The dispatcher-side twin.** `_resume_job_on_agent` was never in scope and still
  fails open on the same class; see
  [[dispatcher_resume_pep_twin_still_fails_open]].
- **The cockpit.** Entirely server-side. Nothing here says the Duplicate button
  reports the 403, or surfaces `dropped` — the latter was a real gap at the time of
  this gate and is tracked with the fix.
