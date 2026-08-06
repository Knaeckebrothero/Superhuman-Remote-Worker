---
tags:
  - issue
  - fix-spec
  - orchestrator
  - jobs
  - verification
  - critic
---

# Approving a *critic* job from the UI wedges its target in `reviewing` forever

**Filed:** 2026-07-27, found while auditing the verification subsystem.
**Status:** CONFIRMED reachable in code, unchanged at HEAD (sweep-verified
2026-08-06: approve_job still has no verification-critic check). Fix items 1
and 3 remain open. The SEVERITY claim is corrected though: the wedge is no
longer permanent — `unstick_reviewing_parents` (commit `f2d054bd`, the
ledger-aware watchdog) flips a target back to `pending_review` after the
grace window when its critic left no ledger row, which a UI-approved critic
never does. Bounded ~30 min, a side effect of the fail-closed rewrite, not a
deliberate fix here. Severity medium → low.
**Original severity claim:** medium — a permanent, silent wedge. The target never reaches a
terminal state and no watchdog can rescue it, but it takes an unusual
operator action to trigger.
**Component:** `orchestrator/main.py:10928-11099` (`approve_job`),
`orchestrator/database/postgres.py:4462-4517` (`unstick_reviewing_parents`).

## The defect

`POST /api/jobs/{id}/approve` accepts any job in `pending_review` **or**
`reviewing` (`main.py:10955`) and sets `status='completed', freeze_data=NULL`
(`main.py:11067-11071`). It does not check `parent_job_id`, and it never
routes through `_handle_critic_verdict_on_complete`.

A critic can legitimately reach `pending_review` — the subjob fallback at
`orchestrator/services/completion.py:984` returns `pending_review` when a
subjob stops without an explicit status and `goal_achieved` is false. At that
point it is visible in the UI as an ordinary job awaiting review, with
nothing marking it as a critic.

Approving it:

1. sets the critic to `completed`;
2. runs **no** verdict handling, so the target is never advanced;
3. leaves the target sitting in `reviewing`.

The rescue path cannot fire. `unstick_reviewing_parents` flips a stuck
`reviewing` parent to `pending_review` only when **every** critic child is in
`('failed', 'cancelled')` (`postgres.py:4500-4512`). A `completed` critic
fails that test permanently, so the watchdog will never touch this target —
not after 30 minutes, not ever.

The exclusion is deliberate and correct in general: per
`docs/superpowers/specs/2026-07-05-reviewing-parent-unstick-watchdog-design.md:75`,
excluding `completed` critics is what stops the watchdog racing the verdict
handler. The bug is that a path exists to reach `completed` *without* the
verdict handler having run.

## Reachability vs. observation

Reachability is verified from the code above. I have **not** confirmed a live
occurrence. A cheap check:

```sql
SELECT p.id, p.status, p.updated_at, c.id AS critic_id, c.status
FROM jobs p
JOIN jobs c ON c.parent_job_id = p.id
WHERE p.status = 'reviewing'
  AND p.updated_at < now() - interval '1 day'
  AND c.status = 'completed'
  AND c.context->>'verification_target' IS NOT NULL;
```

Any row is an instance of this wedge.

## Related hazard on the same endpoint

The endpoint also accepts the **target** while it is `reviewing`. Approving a
target mid-review sets it to `completed`, but the critic keeps running; a
later `returned` verdict calls `_internal_resume_job` on the completed job,
and `queue_job_for_resume` has **no status CAS**
(`postgres.py:4879-4896`) — so it resurrects a terminal job to `paused`. A
later `approved` verdict re-writes it to `pending_review` if autonomy is not
`full` (`main.py:11440`). Both are terminal-status regressions.

## Fix proposal

1. **Refuse to approve a critic.** In `approve_job`, reject jobs whose
   `context.verification_target` is set — they are not human-reviewable
   artifacts; their verdict tools are the interface. Return a 409 explaining
   that the critic's verdict, not its job row, decides the outcome.
   `verification_target` is the canonical critic discriminator and is already
   used by both sweepers.
2. **Or route through the verdict handler** if operators genuinely need a
   manual override: treat the approval as an explicit human verdict on the
   *target*, recorded with the approving user's identity — the break-glass
   shape, attributed and audited, rather than a silent status flip.
3. **Add a CAS to the verdict-application writes.**
   `_set_target_to_autonomy_status` (`main.py:11432-11440`) and
   `queue_job_for_resume` should refuse to move a job out of a terminal
   status. The unstick watchdog is careful to CAS; the verdict path is not.
4. **Give the watchdog a positive deadness signal for this case.** A target
   in `reviewing` whose only critic children are `completed` *and* which has
   no verification round record is unambiguously wedged. Once the durable
   round record from
   `verification_round_reset_spawns_blind_critic.md` exists, this becomes a
   cheap, safe condition to add.

## Related

- `docs/issues/verification_round_reset_spawns_blind_critic.md`
- `docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md` — the
  watchdog this bug routes around.
- `docs/issues/stale_critic_waiting_status_escapes_reaper.md`
