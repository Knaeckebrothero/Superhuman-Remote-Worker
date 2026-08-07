---
tags:
  - issue
  - jobs
  - agent
  - git-versioning
  - workspace-lifecycle
  - observability
---

# Every `git push` failed for a whole job, logged an empty reason, and the job still completed at confidence 1.0

**Filed:** 2026-08-01, from the verification live-gate re-run
(dev job `40efbb39-0890-40fa-a464-6e3d6bd92832`).
**Status:** Cause **FIXED**; consequence-handling **FIXED 2026-08-07**.

- **The silence** (`_parse_shell_run_output` hardcoding `stderr=""`) — FIXED by
  `22b2511e` (08-02), which landed `stderr="" if exit_code == 0 else stdout`.
  Shipped under the sibling investigation
  `deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job.md`, whose
  commit message independently reproduces this doc's exact symptom.
- **The push failure itself** — FIXED by the same commit, and it *is* this
  doc's incident. `f41970ae` added a `CWD: <path>` line to `shell_run`'s
  output; the parser assumed the payload marker was the first line after
  `Exit code:` and otherwise returned the whole banner as `stdout`. `push()`
  does `branch = result.stdout.strip()`, so it pushed
  `"CWD: /home/agent-host/workspace\n--- stdout ---\nmain"` as a refspec —
  `fatal: invalid refspec`, every time.
- **A failed push was still not treated as a failure** — FIXED 2026-08-07; see
  "Fix" below.

> **Correction (2026-08-07) to the 2026-08-06 sweep entry.** The sweep recorded
> that job `40efbb39`'s failure *"likely PREdates the CWD-banner regression"*,
> leaving fix direction #3 ("re-run and capture the real push error") standing.
> That is a timezone slip, and it is worth naming because it made a solved
> incident look open. `f41970ae` was committed `2026-08-01 12:41:31 **+0200**`
> = **10:41:31 UTC**. The job's log timestamps are UTC (`…Z`): it started 11:26
> and its first push failed 11:27:13 — **45 minutes after** the regression
> landed, on the image built from that very commit (`sha-f41970a`, verified as
> the deployed `AGENT_IMAGE` at the time). Same cause. Fix direction #3 is
> discharged; nothing needs re-diagnosing.

**Severity:** **high** — total, silent loss of a job's deliverables, with the
job reporting success. Everything downstream that reads the repo (critic,
cockpit, deliverable gate, cloud export, any re-clone) sees an empty
repository.
**Component:** `src/managers/git_manager.py` (`_parse_shell_run_output`,
`push`), `src/core/phase.py` (completion-time push handling).

## What happened

Job `40efbb39` ran 72 minutes, wrote its deliverable, and called
`job_complete` with `confidence: 1.0` and
`deliverables: ['output/glossary.md']`. The tool succeeded and the job moved to
`reviewing`.

Its Gitea repository contains exactly one commit: `710fdd8 Initial commit`.
Nothing the job produced ever left the agent pod. The pod was then reclaimed
and the work is gone.

The archived log has the whole story, 26 times:

```
WARNING src.managers.git_manager  git push failed:      git_manager.py:813
```

Once at 11:27:13, then on every subsequent push, through 12:39:56 — the
job's entire lifetime. **Every push failed.** Note what follows the colon:
nothing.

The spawned verification critic then cloned that repository, correctly found no
`output/glossary.md`, and tried to return the job. So a whole verification round
was spent reviewing a phantom.

## Root cause of the silence

`push()` logs the reason from `result.stderr`:

```python
# src/managers/git_manager.py:813
if result.returncode != 0:
    logger.warning(f"git push failed: {result.stderr}")
    return False
```

When a workspace backend with shell support is configured, `_run_git` does not
use `subprocess`; it sends the command over the backend (SSH to the workspace
pod) and parses the formatted reply. That parser hardcodes stderr to empty:

```python
# src/managers/git_manager.py:1204-1220, the "Exit code:" branch
return subprocess.CompletedProcess(
    args=cmd,
    returncode=exit_code,
    stdout=stdout,
    stderr="",          # <-- always, including on failure
)
```

`shell_run` returns one combined stream (`Exit code: N\n--- stdout ---\n…`), so
git's error text arrives inside `stdout`. The parser preserves the exit code,
routes the diagnostic into `stdout`, and sets `stderr` to `""` unconditionally.

Every caller in this file that reports a git failure reads `result.stderr`. On
the backend path they all log an empty reason. The failure detail exists in
`result.stdout` and is thrown away at the moment it is needed.

The backend path was definitely in use for this job: the agent's shell tab list
includes the `git` tab that `_run_git` creates via `tab_name="git"`, and the
shells report `CWD: /home/agent-host/workspace`.

## Why this is worse than a logging bug

The empty message is what made it *unfalsifiable in production*. But two
independent failures stack:

1. **The reason is destroyed** (above), so nobody can diagnose the push.
2. **A failed push is only a WARNING.** `push()` returns `False` and every
   caller continues. The phase boundary proceeds, `job_complete` proceeds, the
   freeze proceeds, the job reports success at full confidence, and the pod is
   reclaimed with the only copy of the work on it.

A job whose every push failed is indistinguishable, from the outside, from a
job that pushed cleanly — until a reader opens the repo and finds it empty.

## Fix directions

1. **Stop discarding the reason.** In the `Exit code:` branch, when
   `exit_code != 0`, populate `stderr` with the output (or have failing callers
   log `stdout` as well). One line, and it is a prerequisite for diagnosing the
   actual push failure — which remains unknown precisely because of this.
2. **Make a failed deliverable push loud.** A push failure at a phase boundary
   is recoverable and worth a retry; a push failure at `job_complete` means the
   deliverable does not exist anywhere durable, and the job should not report
   success at confidence 1.0. At minimum it belongs in the freeze record and in
   `error_message`, so the critic and the cockpit can see that the repository is
   empty *by failure* rather than by the agent having produced nothing.
3. **Then** re-run and capture the real push error. Candidates worth checking
   first: credential/URL validity for the embedded-token remote, Gitea
   availability during that window, and whether the repo's default branch
   existed at first push.

Fix 1 before 3 — without it the next occurrence is equally mute.

## Fix (shipped)

**1 — shipped `22b2511e` (08-02).** `_parse_shell_run_output` now locates the
payload marker instead of assuming its position, and mirrors output into
`stderr` on a non-zero exit. Same commit fixed the cause it was hiding (the
`CWD:` banner read as a branch name) and made branch detection fall back to
`main` on an *empty* result, not only a non-zero exit.

**3 — discharged, not owed.** The real push error is known:
`fatal: invalid refspec`, from pushing the banner as a branch. See the
timezone correction in the status block — this doc's incident is the same one
`22b2511e` reproduced, not an earlier separate failure.

**2 — shipped 2026-08-07.** `_push_job_ending_state` in `src/core/phase.py`
replaces the bare `git_mgr.push()` at all four job-ending sites
(`freeze_for_review`, `_finalize_with_verdict`, and both `finalize_job`
branches). On a failed push it logs at **ERROR** and sets `delivery_failed` /
`delivery_error` on the freeze record the orchestrator persists, so a reader
can tell an empty repository *by failure* from one the agent never filled.

Three deliberate choices:

- **It does not retry.** `push()` already logged its reason and the pod is
  being reclaimed either way; a retry would add latency to a job that is
  ending, not recover the work.
- **It does not touch `confidence`.** Confidence is the agent's assessment of
  the *work*, which a delivery failure says nothing about. Downgrading it would
  corrupt an honest signal to carry an unrelated one.
- **It screens out the two non-failures first.** `push()` returns False for
  "git inactive", "no remote configured", and "the push failed" alike. Marking
  a remote-less job — a legitimate configuration — as undelivered would be a
  false alarm on every such run, which is worse than the silence it replaces.
  `test_no_remote_is_not_a_delivery_failure` pins this.

Like `content_tree`, the marker reaches only the record the orchestrator
stores, never the on-disk `job_frozen.json`: that file is written and committed
*before* the push whose outcome it would describe. Unavoidable, and harmless —
the orchestrator's copy is the one that outlives the pod.

`_complete_phase_with_git` (the per-phase boundary) is deliberately untouched:
a mid-job phase push that fails is recoverable, since the next boundary or the
job-ending push carries the same commits. Only the endings are terminal.

Tests: `tests/test_phase_delivery_failure.py`. Mutation-checked — disabling the
recording fails the three positive tests, so they are load-bearing rather than
satisfied by the surrounding code.

**Propagation — shipped 2026-08-07.** `_trigger_verification_on_complete`
(`orchestrator/main.py`) now escalates instead of spawning a critic when the
completion freeze carries `delivery_failed`. Nothing was delivered, so there is
nothing to review: a critic would clone the empty repository, correctly observe
the deliverable missing, and return the job for work that exists but was never
delivered — the exact misdiagnosis that cost job `40efbb39` a 105-minute critic
livelock (`rejected_verdict_livelocks_critic_and_wedges_parent.md`).

The check sits **before** `_verification_gate_decision`, deliberately: that gate
compares `content_tree`, which here describes a tree that was never pushed, so
its no-progress reasoning is meaningless on this input and its round-cap
escalation would report the wrong reason even when it fires.

Routing through `_escalate_target` also discharges the rest of fix direction 2
without new surface: it already writes the reason to the job's `error_message`
— which is what the cockpit displays — and already applies the loop-job status
rule (`completed`, never `pending_review`, or the loop's advance hook wedges).

Tests: `tests/test_verification_flow.py::TestUndeliveredCompletionSkipsTheCritic`,
including the contrast case that a *delivered* completion still spawns (so the
check reads the flag rather than always firing) and the loop-job status case.
Mutation-checked.

**Deliverable gate — shipped 2026-08-07, and it is what makes the above bite.**
`evaluate_deliverable_gate` now returns `{"skipped": True, …}` when the freeze
carries `delivery_failed`, carrying the agent's own reason through to the
context stamp.

This was first triaged as a lower-priority separate slice. That was wrong, and
the ordering is why: the gate runs at `orchestrator/main.py` **before**
verification, and a bounce **early-returns** — *"Skip the status write,
notifications, subjob graft, critic/curator spawns and loop advance"*. So for
any job carrying a `required_deliverables` manifest, an undelivered completion
was bounced by the gate and never reached the verification escalation at all.
Worse than a wrong report: the bounce calls `queue_resume`, telling the agent
to produce files it already produced, onto a workspace pod that may no longer
exist.

The fix is a fifth member of a category the gate already models rather than a
new branch. It already fails open on Gitea unavailable, repo unresolvable, tree
unreadable, and KB-unverifiable, under a house rule stated in its own code:
*"never block a seal on infrastructure the worker cannot fix."* A failed push is
squarely that class; it evaded the four existing cases only because it is the
one infrastructure failure that looks like a **clean read** — the tree is
perfectly readable, it is merely empty.

Tests: `test_undelivered_completion_skips`,
`test_undelivered_without_a_reason_still_skips`,
`test_delivered_completion_is_still_evaluated` (contrast — the check reads the
flag rather than skipping every completion), and
`TestRunGate::test_undelivered_completion_does_not_bounce`, which pins the
composition: asserting the skip at the evaluate level alone would leave the
ordering — the thing that was actually broken — unproven. Mutation-checked.

**Still not done:** nothing surfaces `delivery_failed` in the cockpit *beyond*
`error_message`, and no alerting fires on it. Both are optional; the incident
class is now reported rather than misattributed, which was the point.

## Related

- `docs/issues/verification_fail_closed_followups.md` — the live gate that
  surfaced this.
- `docs/done/resumed_job_inherits_subjob_git_branch.md` — the other way a
  job's committed work becomes invisible to every reader. Same blast radius,
  different mechanism: there the push succeeded to an unread ref, here the push
  never happened at all.
- `docs/issues/session_git_versioning_push_throttle.md` — related push-path
  behaviour.
