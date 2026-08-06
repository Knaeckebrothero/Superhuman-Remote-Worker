---
tags:
  - issue
  - fix-spec
  - agent
  - verification
  - critic
  - lifecycle
---

# An orchestrator drain silently destroys a rendered critic verdict — a rejection becomes an approval

**Filed:** 2026-07-27, found while auditing
`verification_round_reset_spawns_blind_critic.md`.
**Status:** CONFIRMED in code and the overwrite is still there at HEAD
(sweep-verified 2026-08-06) — but the CONSEQUENCE is now structurally
prevented for both decision classes: critic verdicts are durably journaled on
the target's ledger inside the verdict tool (`_submit_verdict` →
`record_verification_round`; `_resolve_critic_outcome` reads only the
ledger), and as of batch #3 (2026-08-06) job_complete decisions are likewise
journaled on the job row (`context.completion_decision`, see
`job_finalization_decisions_held_only_in_process_memory.md`) — a drain freeze
can clobber the freeze BLOB, not the decision. The guard/reorder/WARNING
fixes remain worth doing as defense-in-depth for any other decision-bearing
freeze. Severity downgraded high → low.
**Original severity claim (pre-journal):** **high** — converts an explicit "returned, severity high" into
an approval, with **no log line anywhere** recording that a verdict existed.
Triggered by ordinary deploys.
**Component:** `src/graph.py:3604-3636`, `src/core/phase.py:796-798`.

## The defect

At a phase boundary, `handle_transition`'s result is propagated into graph
state (`src/graph.py:3607-3608`):

```python
updates = result.state_updates
if result.freeze_data:
    updates["freeze_data"] = result.freeze_data      # ← the verdict freeze
```

Immediately afterwards, the Continue-as-New drain check runs
(`src/graph.py:3619-3636`):

```python
if _is_drain_requested():
    upgrade_freeze = {"freeze_type": "version_upgrade", ...}
    ...
    updates["freeze_data"] = upgrade_freeze          # ← unconditional overwrite
    updates["should_stop"] = True
```

The overwrite is unconditional. If the transition that just completed was a
critic rendering its verdict, the verdict freeze is discarded.

**And it cannot be recovered**, because `finalize_job` already cleared the
in-process verdict store before returning that freeze
(`src/core/phase.py:796-798`):

```python
verdict = get_verdict_data(job_id)
if verdict:
    clear_verdict_data(job_id)          # ← gone
    clear_final_phase_data(job_id)
    return _finalize_with_verdict(...)
```

So on the post-drain re-dispatch the critic starts with an empty
`_verdict_data`. If it then closes itself — or completes without re-rendering
— it lands on one of the implicit-approval paths
(`phase.py:803-812` or `main.py:12228-12236`) and the target is **approved**.

A `returned` verdict at severity high inverts to an approval, and the only
trace is a `version_upgrade` freeze that looks entirely routine.

## Why the comment does not save it

The drain block's own comment says:

> The check fires regardless of transition success — even a rejected
> transition is a fine point to hand off.

That reasoning is sound for a *rejected* transition, which produced no
durable decision. It does not hold for a *successful* transition that
produced a verdict — the freeze there is not a checkpoint marker, it is the
job's output.

## Blast radius

Any deploy that drains agents. The lifecycle reconciler marks workers on a
stale image with `intents.should_drain`, so this fires during normal rolling
updates — precisely when many jobs are at phase boundaries at once.

The same overwrite discards **any** freeze carrying a decision, not only
critic verdicts. A worker's `job_complete` freeze at the same boundary is
equally exposed; see
`job_finalization_decisions_held_only_in_process_memory.md`.

## Fix proposal

1. **Don't overwrite a decision-bearing freeze.** Guard the assignment at
   `src/graph.py:3635`: if `updates.get("freeze_data")` already carries a
   `verdict` or a terminal `status`, keep it and skip the drain freeze — the
   job is finishing anyway, so there is nothing to continue-as-new. Log at
   INFO that the drain deferred to a completed decision.
2. **Or re-order:** evaluate `_is_drain_requested()` *before*
   `handle_phase_transition`, so a drain at a boundary never races a
   transition that is about to produce output.
3. **Log the loss unconditionally.** Whatever the resolution, an overwrite of
   a non-empty `freeze_data` must emit a WARNING naming both freeze types.
   The absence of any such log is why this has been invisible.
4. **Make it moot** by persisting the verdict durably inside the tool call
   (Slice 2 of `verification_round_reset_spawns_blind_critic.md`). With a
   durable record, a lost freeze costs a re-render, not a false approval.

Items 1-3 are cheap and independent; item 4 is the structural fix.

## Test

A regression test must drive a critic through `handle_transition` with
`_is_drain_requested()` patched true and assert that the resulting
`freeze_data` still carries `verdict`. Note that `_verdict_data` is cleared
by `finalize_job`, so the test has to assert on the freeze, not on the store.

## Related

- `docs/issues/verification_round_reset_spawns_blind_critic.md` — row 1 of
  its fail-open inventory; full context.
- `docs/issues/job_finalization_decisions_held_only_in_process_memory.md` —
  why the verdict was unrecoverable once the freeze was dropped.
- `docs/done/version_upgrade_drain_masked_by_coincident_error.md` — the
  neighbouring `updates["error"] = None` line, added for a different
  drain-interaction bug.
