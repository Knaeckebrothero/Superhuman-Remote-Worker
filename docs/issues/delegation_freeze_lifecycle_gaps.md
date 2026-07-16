---
tags:
  - issue
  - jobs
  - delegation
  - dispatcher
---

# Delegation freeze lifecycle gaps: resumed parent may be dispatcher-invisible; re-suspend drops timeout; manual cancel skips unblock

**Filed:** 2026-07-16, found during the multi-agent code audit for
[`llm_outage_subjob_resilience`](../features/llm_outage_subjob_resilience.md).
Line numbers are develop @ 2026-07-16.

> **2026-07-16 update: Gaps 1 + 2 CONFIRMED and FIXED on develop** (TDD, same
> day). Gap 1 was proven live on real Postgres (testcontainers): after
> `claim_delegation_resume`, the row kept the full delegation blob and
> `get_dispatchable_jobs` returned nothing. Fix: the claim now clears
> `freeze_data`, and `_handle_delegation_child_completion` re-queues via that
> same CAS (also closing its status-check TOCTOU) instead of
> `update_job_status` (which cannot clear a freeze). Gap 2 fix: the resume
> tool takes an explicit `timeout` (default 7200, capped by
> `delegation.max_timeout`) and carries it in the re-suspend freeze.
> Regression tests: `tests/test_delegation_resume_claim.py` (real-PG
> dispatcher-contract test), `tests/test_per_job_repo.py`
> (TestDelegationUnblockDispatcherContract),
> `tests/test_delegation.py` (TestResumeChildFreezeTimeout).
> **Gap 3 remains OPEN.**

These three gaps all live in the `delegate_work` parent-freeze lifecycle. They are
independent of the outage feature but **gate its delegation slice** — in particular,
if Gap 1 is real, a delegation parent cannot reliably resume at all, so pausing its
children is moot until this is fixed.

## Gap 1 — parent `freeze_data` never cleared on `waiting → paused` requeue (CONFIRMED → FIXED, see update above)

Both writers that re-queue a delegation parent flip status but leave the
`freeze_data` delegation blob on the row:

- `_handle_delegation_child_completion` (`main.py:11048-11050`) —
  `update_job_status(target_id, status="paused", assigned_agent_id="")`;
  `update_job_status` only touches `freeze_data` when explicitly passed
  (`postgres.py:1144-1147`).
- `claim_delegation_resume` (`postgres.py:4327-4354`) —
  `UPDATE jobs SET status='paused', assigned_agent_id=NULL WHERE id=$1 AND
  status='waiting'`; no `freeze_data` touch.

But the dispatcher hard-requires `freeze_data IS NULL`
(`get_dispatchable_jobs`, `postgres.py:4476`; partial-index contract from
migration 0046). The stash-and-clear path on pause covers only
`AUTO_REDISPATCH_FREEZE_TYPES` = `{version_upgrade, memory_unavailable,
kb_unavailable, workspace_upgrade_required}` (`completion.py:270-277`,
`main.py:13397-13425`, recover-sweep `postgres.py:4114-4128`) — **`delegation`
is not in the set**.

On its face: children complete → parent re-queued `paused` with delegation
`freeze_data` still set → **invisible to the dispatcher forever**.

**Why nobody has hit it:** heavy delegation is barely exercised in prod
([`subagents_never_used.md`](subagents_never_used.md)), and no test covers
`freeze_data` content through the resume claim
(`tests/test_delegation_resume_claim.py` fixtures carry none).

**Verify first:** reproduce on k3d (delegation parent with real freeze_data →
children complete → watch dispatch). If confirmed, fix = clear/stash
`freeze_data` in both writers (mirroring the AUTO_REDISPATCH stash so
`delegation_results` context survives), plus a regression test that asserts the
resumed parent is picked up by `get_dispatchable_jobs`.

## Gap 2 — re-suspend freeze drops the `timeout` key (FIXED, see update above)

The initial delegation freeze carries `timeout` (tool input, default 7200s,
cap `max_timeout` 14400 — `delegate_work.py:236-244,152-155`). The
**re-suspend** freeze written by `resume_delegation_child`
(`delegate_work.py:459-468`) omits it, so `_check_delegation_timeouts` falls
back to `freeze.get("timeout", 7200)` (`main.py:11098`) — a delegation started
with a 4h timeout silently shrinks to 2h after any re-suspend.

Fix: carry `timeout` (and any other original freeze fields worth preserving)
into the re-suspend freeze.

## Gap 3 — manual cancel of a delegation child never unblocks the parent

The cancel endpoint (`main.py:8267-8272`) invokes only
`_handle_scholar_completion`, not `_handle_delegation_child_completion`.
Hand-cancelling the **last** non-terminal delegation child leaves the parent in
`waiting` until the delegation timeout sweeper eventually fires (up to 2-4h
later).

Fix: also invoke the delegation child-completion handler from the cancel path.

## Relationship

- Gates the delegation slice of
  [`llm_outage_subjob_resilience`](../features/llm_outage_subjob_resilience.md)
  (its P0.1/P0.2 entries point here).
- Related context: [`subagents_never_used.md`](subagents_never_used.md),
  [`delegation_light_mode_missing.md`](delegation_light_mode_missing.md).
