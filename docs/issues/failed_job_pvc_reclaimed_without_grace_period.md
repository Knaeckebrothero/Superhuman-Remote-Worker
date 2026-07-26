---
tags:
  - issue
  - fix-spec
  - jobs
  - workspace-lifecycle
  - storage
---

# Issue — failed jobs lose their workspace PVC instantly (no salvage window)

**Status:** Designed 2026-07-25 from the job-`52949749` salvage. Not yet
implemented. Work on `develop`.

**One line:** PVC reclaim treats `failed` exactly like `completed`/`cancelled`,
so the moment a job fails its workspace volume — including every byte not yet
pushed to the job's Gitea repo — is garbage-collected, destroying precisely the
state an operator needs to diagnose or resurrect the job.

## Current behavior

Two reclaim paths, both keyed on the same terminal predicate
(`completed` / `failed` / `cancelled` job, ended thread):

1. **Inline** — `WorkspaceManager.delete()`
   (`orchestrator/services/lifecycle/workspace_manager.py`, "PVC GC (Branch a
   leak guard)"): when the reconciler tears down a terminal instance it calls
   `delete_workspace_pvc(owner)` plus the stable-DNS Service delete.
2. **Backstop** — `WorkspaceManager.reap_orphans()`: once-per-tick sweep that
   lists `pvc-workspace-*` PVCs directly and deletes any whose owning job is
   terminal or gone and has no live pod.

This is the intentional "PVC dies when the job dies" guard from
`docs/features/workspace_pvc_branch_a_implementation.md` — correct as a leak
guard, too eager for the `failed` arm.

## Why this bites (incident evidence)

Job `52949749` ("historische Kernwerke", 2026-07-23, see
`docs/issues/maxsessions_parallel_tools_false_workspace_death.md`):

- The job failed-loud on a misclassified workspace error. Its PVC held ~28
  minutes of remediation work that had not yet been committed/pushed to the
  job repo (per-todo commit cadence — mid-todo edits are volume-only).
- The reclaim destroyed that state immediately. When the fix was deployed two
  days later and the job was resurrected (`failed` → `paused`), the fresh PVC
  was blank; the resume seed path then git-inited an empty workspace and the
  agent began re-planning the whole job from scratch.
- Salvage required manual surgery: fetch the job repo in-pod, checkout
  `research/ output/ archive/` at the freeze commit, hand-restore a missing
  bound skill, and drop a `feedback.md` redirect. Total loss avoided only
  because the freeze state happened to be pushed and the remediation roadmap
  happened to live in the KB.

The failure mode generalizes: **`failed` is the one terminal state that is
unplanned**. Completed and cancelled are deliberate ends; failed jobs are the
ones most likely to be diagnosed, retried, or resurrected — and the current
policy deletes their working state at the exact moment it becomes valuable.

## Proposal — grace period for failed jobs

Keep immediate reclaim for `completed` and `cancelled`. For `failed` jobs,
retain the PVC for a bounded grace window:

- New env `FAILED_WORKSPACE_PVC_GRACE_HOURS` (chart:
  `workspace.failedPvcGraceHours`), default **72**.
- **Inline path:** `delete()` skips PVC reclaim when the owning job's status is
  `failed` (still deletes the pod; the Service can stay with the PVC since the
  resume path reattaches by deterministic name).
- **Backstop path:** `reap_orphans()` becomes the single authority for failed
  jobs: reclaim a failed job's PVC only when `now - job.updated_at > grace`.
  It already lists PVCs and resolves owning jobs, so this is one predicate
  change plus a timestamp comparison.
- **Resurrection flow becomes trivial:** flipping a failed job back to
  `paused` inside the window re-dispatches onto the *intact* volume — no
  Gitea surgery, no checkpoint-vs-files skew.

### Quota interplay (Phase 3a)

PVCs are 10Gi each under the per-class ResourceQuota (capacity guard,
Phase 3a). Grace-held failed PVCs consume quota and could starve new
workspaces on a bad day. Bound it:

- Cap the number of grace-held PVCs (`FAILED_WORKSPACE_PVC_GRACE_MAX`,
  default e.g. 10); beyond the cap, `reap_orphans()` reclaims oldest-first
  even inside the window.
- `reap_orphans()` logs each grace-held PVC (job id + age + deadline) so the
  holdings are visible, not silent.

### Alternatives considered

- **Snapshot-then-reclaim** (S3 snapshot before PVC delete): attractive on
  paper, but the snapshot pipeline streams over SSH from a live pod — at
  fail-loud the pod is typically dead or being torn down, so the snapshot is
  unavailable exactly when this matters. Rejected as the primary mechanism.
- **Keep PVC until job deletion:** unbounded growth on dev where failed jobs
  accumulate; rejected.

## Scope

Small: one predicate change in `delete()`, one grace/cap check in
`reap_orphans()`, chart value + configmap plumbing, and unit tests for the
sweep decision matrix (failed-in-grace kept, failed-past-grace reclaimed,
completed reclaimed immediately, cap-overflow reclaims oldest-first).

## Related

- `docs/issues/maxsessions_parallel_tools_false_workspace_death.md` — the
  incident that exposed this; its slice-D "no leak on fail-loud" deletes the
  *pod* at cap exhaustion, which is compatible with keeping the PVC.
- Open sibling gap (to be filed separately): the resume-onto-fresh-workspace
  seed path ("seed from last snapshot") never falls back to cloning the
  existing job repo, which is what turned this PVC loss into a from-scratch
  restart.
