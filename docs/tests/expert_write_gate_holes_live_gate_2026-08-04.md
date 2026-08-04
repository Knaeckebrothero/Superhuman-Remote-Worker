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
  - "[[tool_configuration_deferred_findings]]"
---

# Live gate — the expert-write kill switch and duplicate's grants gate

Run 2026-08-04 against **k3d** (`k3d-srw` / namespace `srw`) as the real non-admin
`catalog-gate@k3d.local` (`230ce19a`) over the MCP internal-header auth path,
because `_enforce_save_grants` returns early for admins and gating as an admin
would exercise the permissive branch.

The `user_experts` kill switch is **deployment-wide**, so the harness flips it and
restores the original row (or its absence) in a `finally` block. Inside the
orchestrator pod `/app` **is** the orchestrator package — `main.py` is at
`/app/main.py`, not `/app/orchestrator/main.py`, and the import root is
`services.…`.

## Results

| # | Check | Result |
|---|---|---|
| 0 | Baseline, switch enabled: non-admin duplicates a global expert | **200**, row owned by the caller — the happy path is unbroken |
| 1 | Switch OFF, all **five** expert-write routes | **403 × 5**, each with *"User-defined experts are disabled by the administrator"* — `create`, `update`, `import`, `expert-defaults/{type}/fork`, `duplicate` |
| 1b | Rows owned by the test user across check 1 | **unchanged** (1 → 1) — no write slipped past a 403 |
| 2 | Switch back ON. Non-admin duplicates a **visible** expert declaring `tools.shell`, holding no `shell_tools` grant | **422** *"config exceeds your capability grants: shell_tools: tools.shell requires the shell_tools grant"*, and **no row created** (1 → 1) |
| 3 | **Control:** the same duplicate as the **admin** | **200** — so check 2's 422 is the grants PDP, not a shape rejection or a visibility failure wearing a different number |

Check 1 is the fix. Check 3 is what makes check 2 mean anything.

## The harness bug worth writing down

The first run of check 2 returned **404 Expert not found**, not 422, and the gate
correctly failed rather than reporting a pass. Cause was the harness, not the
product: the staged source expert was **admin-owned and private**, and
`get_expert_visible_by_id` (`orchestrator/database/postgres.py:9033`) admits a row
only when `is_admin`, `owner_id = caller`, `is_global = TRUE`, or a
`project_experts` link exists. So the call 404'd before reaching any gate.

This is the same fact the issue doc leads with — **visibility, not ownership, is
the test on this route** — arriving from the other direction. Staging now sets
`is_global = TRUE` on the source.

Worth keeping because of how it would have failed silently: had the gate asserted
only *"no row was created"*, a 404 would have passed. A test that cannot tell a
refusal from an unreachable fixture is not a test. The gate asserts the **status
and** the row count, and the admin control catches the remaining case.

## Not covered

- **`resume_job`'s 409 has no live gate.** Staging it needs a job in a resumable
  state whose stored config is permanently unusable — a malformed `tools` value
  written directly to the DB past validation, a job created against it, then
  paused. Doable on k3d and not done here. It is covered by unit tests that assert
  **non-dispatch** and not merely the status code, in both directions (the fix
  narrows rather than reverses: a transient error still proceeds).
- **The kill switch against a *built* cockpit.** This gate is entirely
  server-side; nothing here says the cockpit's Duplicate button reports the 403
  well.
