---
tags:
  - issue
  - orchestrator
  - grants
  - jobs
related:
  - "[[resume_job_grant_recheck_fails_open]]"
  - "[[expert_write_gate_holes_live_gate_2026-08-04]]"
---

# The Resume PEP exists **twice** — `_resume_job_on_agent` still swallows an unusable stored config and re-queues forever

**Status:** OPEN, filed 2026-08-04 by the whole-branch review of the fix for
[[resume_job_grant_recheck_fails_open]]. **Not a security regression** — see
"Why this is not Critical".
**Severity:** low-medium. Nothing escalates and nothing dispatches; a job becomes
permanently, silently unresumable with a log line that does not name the cause.
**Component:** `orchestrator/main.py`, `_resume_job_on_agent` (the dispatcher-side
block, ~`:3267-3298`), the twin of the `resume_job` endpoint block ~`:11558`.

## The defect

`_resume_job_on_agent` is a near-verbatim copy of the endpoint's Resume PEP: same
`_user_experts_enabled()` wrapper, same `resolve_config` then
`_enforce_dispatch_grants`. It catches **only `GrantDenied`**.

The 2026-08-04 fix widened the **endpoint** block to fail closed on an unusable
stored config (409). The dispatcher block was not touched, and the plan and both
issue docs speak throughout as if there is one site. That framing is wrong and is
corrected in [[resume_job_grant_recheck_fails_open]].

Concrete sequence: a paused job whose expert row holds a legacy `tools.shell:
true` (storable before `_validate_expert_fragment` existed). The dispatcher tick
calls `_resume_job_on_agent` directly — no endpoint, so the new 409 never runs.
`resolve_config` raises `ToolPolicyError`, which escapes to the outer
`except Exception` and logs `Dispatch: failed to resume job … on agent …` without
mentioning the config, then returns `False`. The job stays dispatchable and the
same ERROR repeats **every tick, forever**.

## Why this is not Critical

It **fails closed by accident**: the exception is raised before the POST to the
agent, so nothing is dispatched without a grant check. The security property the
sibling issue was about is intact on this path. What is broken is
**diagnosability** — an operator sees an unattributed dispatch error on a loop and
no indication that one stored row is the cause.

It is also pre-existing; the fix did not create it. What the fix created is an
**asymmetry**: the endpoint block now carries ~30 lines of comment explaining
"stored config unusable" versus "could not reach storage", while its twin ~8,000
lines earlier has neither the split nor the reasoning. That is one refactor away
from someone "consolidating" the two and silently picking the wrong behaviour.

## The fix, when someone takes it

Two options, and the second is the better one:

1. Mirror the endpoint's split into `_resume_job_on_agent` — catch the
   unusable-config class, log it naming the cause, and mark the job in a way the
   dispatcher stops retrying (otherwise the forever-loop persists, just with a
   better message).
2. **De-duplicate.** Extract the PEP into one helper both call sites use, so the
   classification exists once. The duplication is the actual defect; the divergence
   is its symptom. Note the two sites need different *outcomes* on refusal — an
   HTTP 409 versus a dispatcher-loop decision — so the helper should return a
   verdict rather than raise `HTTPException`.

Either way the forever-requeue needs its own answer: a job whose stored config can
never resolve should stop being re-queued, and there is currently no route that
can repair `jobs.config_name` / `jobs.config_override`
(see [[resume_job_grant_recheck_fails_open]], "no recovery path").
