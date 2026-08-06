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
**Status:** The *silence* mechanism this doc names (`_parse_shell_run_output`
hardcoding `stderr=""`) is FIXED at HEAD (sweep-verified 2026-08-06):
`22b2511e` (08-02) landed `stderr="" if exit_code == 0 else stdout` in
git_manager.py — shipped under the sibling investigation
`deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job.md`, whose
commit message independently reproduces this doc's exact symptom; never
cross-linked here until now. STILL OPEN: this doc's own motivating incident —
job `40efbb39`'s underlying push failure — was never re-diagnosed (its
timestamps likely PREdate the CWD-banner regression that `22b2511e` targets),
so Fix direction #3 ("re-run and capture the real push error") stands.
**Severity:** **high** — total, silent loss of a job's deliverables, with the
job reporting success. Everything downstream that reads the repo (critic,
cockpit, deliverable gate, cloud export, any re-clone) sees an empty
repository.
**Component:** `src/managers/git_manager.py:1219` (`_parse_shell_run_output`),
`src/managers/git_manager.py:813` (`push`).

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

## Related

- `docs/issues/verification_fail_closed_followups.md` — the live gate that
  surfaced this.
- `docs/issues/resumed_job_inherits_subjob_git_branch.md` — the other way a
  job's committed work becomes invisible to every reader. Same blast radius,
  different mechanism: there the push succeeded to an unread ref, here the push
  never happened at all.
- `docs/issues/session_git_versioning_push_throttle.md` — related push-path
  behaviour.
