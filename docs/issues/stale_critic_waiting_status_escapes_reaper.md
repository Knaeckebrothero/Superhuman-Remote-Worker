---
tags:
  - issue
  - jobs
  - critic
  - lifecycle-reaper
---

# A critic in `waiting` whose parent terminally fails is never reaped — `cancel_stale_verification_subjobs` only matches `created`/`paused`

**Status:** CONFIRMED in code + live DB on dev 2026-07-18. UNFIXED.
**Severity:** low-medium — leaks a zombie `waiting` job row per incident
(confusing in the cockpit, counts against project job lists), no compute
burned.
**Component:** `orchestrator/database/postgres.py`
(`cancel_stale_verification_subjobs`, ~4271)

## Symptom

Job "netzteil" `b988e3f0` was returned with feedback by its critic
`20c2fcb4-a59a-420e-95f8-3f454f484b57` on 2026-07-16 16:52Z; the critic went
to `waiting` for the next verification round. The parent then terminally
**failed** on 2026-07-17 09:07Z (cockpit-resume credential bug, see
`job_resume_direct_path_skips_credential_injection.md`). The critic is still
`waiting` on 2026-07-18 — nothing will ever re-trigger it (only the parent's
next `/complete` does that) and nothing cancels it.

## Root cause

Between verification rounds a critic subjob parks in **`waiting`** (the
verdict handler sets it so; its freeze blob even records
`"status": "waiting"`). The stale-verification reaper
`cancel_stale_verification_subjobs` cancels subjobs whose parent is terminal
with:

```sql
WHERE j.context->>'verification_target' IS NOT NULL
  AND j.status IN ('created', 'paused')   -- ← 'waiting' missing
  AND j.assigned_agent_id IS NULL
  AND (parent.status IN ('completed','failed','cancelled') OR …stale…)
```

A critic in `waiting` is invisible to it, so it outlives its dead parent
indefinitely.

## Fix direction

Add `'waiting'` to the status list (the parent-terminal arm is exactly the
right condition for it; the age-based arm should keep excluding `waiting`
critics whose parent is still alive, since inter-round waits are legitimate
and unbounded). Alternatively cancel the critic inline wherever a
verification target transitions to `failed`/`cancelled`.

Cleanup for the live victim: cancel `20c2fcb4` manually (or let the widened
sweep do it).

## Related

- `critic_feedback_resume_parent_freeze_data_wedge.md` — found in the same
  2026-07-18 sweep; that bug is why the parent needed a manual resume at all.
- `critic_failure_leaves_parent_job_stuck_reviewing.md` — the mirror-image
  leak (dead critic, live parent).
