---
tags:
  - issue
  - fix-spec
  - jobs
  - agent
  - workspace-lifecycle
  - git-versioning
  - delegation
---

# A resumed job inherits a subjob's git branch, so everything it writes afterwards is invisible on `main`

**Filed:** 2026-08-01, from job `6df02f64-b4d7-477e-877d-ba570610d54d` on dev.
**Status:** **ROOT CAUSE FOUND + FIXED** (`ensure_job_branch`, `src/agent.py`).
Regression test: `tests/test_resume_job_branch_restore.py`.
**Severity:** **high** — silent, total loss of every commit a job makes after
its first critic round, from the perspective of every reader in the system.
Not append-specific, not `edit_file`-specific, and not specific to the
feedback-resume path.
**Component:** `src/agent.py` (`_setup_job_workspace` reattach + resume-existing
branches), `orchestrator/services/job_provisioning.py` (subjob branch naming).

> This file replaces the originally-reported framing. The bug was reported as
> *"`edit_file` append silently fails to persist across the
> `[JOB_COMPLETED] → [FEEDBACK_RESUME]` boundary"*. **That framing is wrong in
> every particular** — see "Both candidate shapes were wrong" below. The
> recommended workaround (use read-then-write instead of `edit_file(position=end)`)
> would not have helped.

## Symptom

A job appends text to a tracked file with `edit_file`, completes, is returned
with critic feedback, and the appended text is gone. `edit_file` reports
success. The agent believes the edit landed. The reviewer reads the file and
correctly reports the content still missing.

It reads as a disagreement about a file rather than an I/O failure **because
both parties are right about different refs.** The agent is reading its
workspace (and its own branch, where the content genuinely is). The reviewer is
reading `main`, where it genuinely is not.

## Root cause

Critic/scholar/delegation subjobs run on their own branch,
`subjob/<short_id>/<config>` (`orchestrator/services/job_provisioning.py:164`).
When a parent job is resumed onto a workspace that a subjob last occupied, the
tree is still checked out on the **subjob's** branch. The two code paths that
attach to a *pre-existing* tree only re-pointed the branch when the job row
carried an explicit `branch_name`:

```python
# src/agent.py, reattach path (was ~2290) and resume-existing path (was ~2404)
if metadata.get("branch_name"):
    if git_mgr.current_branch() != metadata["branch_name"]:
        git_mgr.checkout_branch(metadata["branch_name"])
```

A standalone (non-project) job has `branch_name = NULL` and lives on `main`.
The guard reads NULL as *"don't care"*, so nothing re-points the tree and the
parent silently continues on the subjob's branch. Every subsequent per-todo
commit, phase-boundary commit, freeze commit **and push** goes to
`subjob/<id>/critic`. `main` never advances past the last pre-critic round.

Every reader resolves the job to `main` — the orchestrator uses
`job.get("branch_name") or "main"` in nine places
(`diff_source.py:118`, `deliverable_gate.py:192`, `ide_session.py:613/1065/1221`,
`job_cloud_baseline.py:615`, `main.py:12076/13182`,
`job_provisioning.py:162`). The agent's two attach-to-existing-tree paths were
the only places that treated NULL as "leave it wherever it is". The fix aligns
them with that convention.

This also violates an assumption the code states explicitly elsewhere — that
"a critic runs on its own `subjob/<id>/critic` branch and its workspace state
is a different thing from the target's"
(`src/tools/evaluation/evaluation_tools.py:115`, `orchestrator/main.py:19071`).

## Evidence (job `6df02f64`, dev)

The job is a verification fixture: write `output/glossary.md` with three
definitions, deliberately omit `## Sources` on round 1, add it on round 2.

**The append was committed *and pushed* — to the wrong branch.**

| Ref | `output/glossary.md` | `## Sources`? |
|---|---|---|
| `main` (HEAD) | 1756 bytes | **no** |
| `c04be0e4` (a later orphaned branch tip) | 1756 bytes | **no** |
| **`subjob/50dee4ae/critic`** | **2292 bytes** | **yes — all three references** |

The subjob branch carries the entire round-2 history:

```
8ec9f513 Job completed (autonomy=full)                     2026-07-30T00:23:46Z
813c632f [Phase 4 Strategic] todo_4: PLAN OR COMPLETE
8358eb93 [Phase 3 Tactical] Complete - archived 5 todos
d1ec26e9 [Phase 3 Tactical] todo_4: Verify AC4 preserved…
de2a6260 [Phase 3 Tactical] todo_3: Verify glossary.md post-append…
7c56a9bc [Phase 3 Tactical] todo_2: Append a `## Sources` section …   ← the append
8053dbcc [Phase 3 Tactical] todo_1: Re-read glossary.md baseline    2026-07-30T00:12:29Z
```

`main`'s last commit is `e18b3b71 "Job completed (autonomy=full)"` at
2026-07-29T23:55:58Z — round 1. Nothing after round 1 ever reached it.

**The agent's own git tools reported the branch, and nobody read it.** From the
round-2 audit trail, minutes after the append:

```
[382] git_log:    commit 8358eb93… (HEAD -> subjob/50dee4ae/critic, origin/subjob/50dee4ae/critic)
[383] git_status: Branch: subjob/50dee4ae/critic
                  Status: clean (no uncommitted changes)
```

`origin/subjob/50dee4ae/critic` — pushed. `clean` — fully committed.

**The parent was in the critic's workspace.** Round 2's `list_files output/`
(audit `[359]`, `[390]`) shows `critic_verdict.json`, `verification_report.json`,
`audits/`, `reviews/` — critic artifacts that appear at no point in `main`'s
history. The round-2 agent even had to reason about them, writing a todo that
calls them "reviewer-injected … not worker-base output" (commit `d1ec26e9`).

**Corroborating timeline.** The append is at 2026-07-30T00:12:39Z (audit
`[342]`→`[343]`, `Appended to: output/glossary.md`). `main` has no commit
between 2026-07-29T23:55:58Z and 2026-07-30T00:57:41Z.

## Both candidate shapes were wrong

Recorded so nobody re-derives them:

- **(A) "The write never reached the freeze commit."** Wrong. It reached a
  per-todo commit (`7c56a9bc`), a phase-boundary commit (`8358eb93`), a freeze
  commit (`8ec9f513`), *and* a push to `origin`.
- **(B) "The resume restored an older tree."** Wrong. The tree was correct and
  its history continuous — `2d64f816 [Phase 0 Seed]` sits directly on round 1's
  `e18b3b71`. The agent read the correct round-1 baseline before appending.
  (`resume_fresh_workspace_no_clone_fallback.md` was the strongest lead and is
  a real bug, but it is **not** this one. It also could not be: phase snapshots
  carry only `checkpoint.db`, `workspace.md`, `plan.md`, `todos.yaml` and
  `archive/` — no `output/`, so a snapshot-seeded workspace could not have
  produced the `output/glossary.md` the agent actually read.)
- **The reported cause — `edit_file(position="end")` — is not implicated at
  all.** `src/tools/workspace/files.py:1082` is
  `content + new_string` → `workspace.write_file(path, new_content)`: a plain
  full-file write, the identical write path `write_file` uses. There is no
  append-specific I/O to fail. The discriminating experiment (same sequence
  with `write_file`) is answered by the code: `write_file` would have been lost
  identically.

**Do not adopt the workaround the affected agent invented** (read-then-write
instead of `edit_file(position=end)`). It changes nothing — the write was never
the problem.

## Blast radius — wider than reported

The loss is **not** append-specific, **not** `edit_file`-specific, and **not**
limited to the feedback-resume path. Any job resumed onto a workspace a subjob
last occupied continues on that subjob's branch, so **every** write it makes for
the remainder of the job — any tool, any file, any phase — is invisible to
`main` and to every consumer that reads it: the critic's next round, the
cockpit, MCP `get_workspace_file`/`get_job_file`, `list_job_files`, the
deliverable gate, IDE sessions, cloud export, and any later re-clone of the
workspace.

This makes the verification loop unable to converge by construction: the worker
fixes the finding on its branch, the critic re-reads `main`, sees it unfixed,
and returns it again. Job `6df02f64` burned all three rounds this way and ended
`pending_review` with "Round limit reached (3) with 2 finding(s) still open".

## Fix

`ensure_job_branch(git_mgr, metadata, job_id)` in `src/agent.py`, called from
both attach-to-existing-tree paths. A missing `branch_name` now resolves to
`DEFAULT_JOB_BRANCH = "main"`, matching the convention every reader already
uses. A branch it cannot switch to is logged at WARNING naming both branches —
this whole bug class is defined by its silence.

The fresh-clone path is deliberately untouched: a clone lands on the remote's
default branch, so it was already correct.

Regression tests: `tests/test_resume_job_branch_restore.py`. They drive a real
git repository through a real `GitManager`; mocking `current_branch`/
`checkout_branch` would assert only that the helper calls the methods it
obviously calls, and would have passed against the buggy code.

## Open — worth its own investigation

**Why was the parent in the critic's workspace at all?** The evidence that it
was is conclusive (the critic's artifacts and its branch are both in the
parent's round-2 tree), but the provisioning mechanism that put it there is not
established here. The code asserts the opposite invariant in two places
(`evaluation_tools.py:115`, `main.py:19071`), so either subjobs share the
parent's workspace PVC contrary to that intent, or the parent's resume
re-attached a workspace the critic had used. `ensure_job_branch` makes the
parent resilient either way — it now asserts its own branch instead of
inheriting one — but if the sharing is itself unintended, that is a second
defect upstream of this one.

**Secondary, unrelated to the loss above:** in both `finalize_job` branches the
final `todo_manager.archive("final")` runs *after* `git_mgr.commit()` +
`git_mgr.push()` (`src/core/phase.py:918-935` and `990-1007`), so the final
archive file it writes can never be committed. Real but minor, and not a cause
of this incident.

## Related

- `docs/issues/resume_fresh_workspace_no_clone_fallback.md` — the strongest
  lead, and a real bug, but ruled out as the cause here.
- `docs/issues/critic_feedback_resume_parent_freeze_data_wedge.md`
- `docs/issues/verification_round_reset_spawns_blind_critic.md`
- `docs/issues/failed_job_pvc_reclaimed_without_grace_period.md`
