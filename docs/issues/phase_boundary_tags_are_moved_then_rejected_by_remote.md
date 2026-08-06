---
tags:
  - issue
  - git
  - phase-model
  - jobs
  - observability
related:
  - "[[phase_model_overhead_amnesia_loop]]"
  - "[[overnight_minimax_m3_scholar_batch_2026-08-03]]"
  - "[[deliverable_lost_to_nested_repo_commit_and_stranded_mode_a_job]]"
aliases:
  - stale phase boundary tag
  - git push tags already exists
---

# Phase-boundary tags are force-moved locally and rejected by the remote

**Filed:** 2026-08-04 from the five-job main-cluster overnight Scholar batch.

**Status:** **CORE FIXED 2026-08-06 (batch #2)** — fix directions 1-3 built
(src/managers/git_manager.py + src/core/phase.py):
- `tag()` is create-once idempotent: no `-f`; an existing tag at the current
  HEAD commit is a no-op success; an existing tag elsewhere logs the typed
  `TagInvariantViolation` ("refusing to move an audit boundary") and returns
  False — the original ref is never moved. New `resolve_tag_commit()`
  plumbing.
- `_complete_phase_with_git` reordered commit → tag → push, so the boundary
  tag dereferences to the phase-completion commit it claims to delimit.
- Tag delivery is per-ref: new `push_ref("refs/tags/<name>")` pushes exactly
  the new tag; `push()`'s `tags` default flipped to False, ending the
  `--tags` spray that retried every historical tag (and pushed unrelated
  tags from the shared project repo / external repos). The two job-completion
  tag sites push their exact ref the same way.
Tests: create-once/no-op/violation + push_ref + no-spray pins
(tests/test_managers_git.py), tag-dereferences-to-completion-commit +
double-completion idempotency (tests/test_phase_git.py).
Remaining OPEN (adjacent, not this defect): fix direction 4 — the graph can
still archive the same phase instance twice (the duplicate transition that
*triggered* the moves; now harmless to tags but still a graph exactly-once
gap) — and direction 5 (strategic review consuming orchestrator phase events
instead of assuming tags), which is softened now that a tag, when present,
is guaranteed current.

**Originally:** OPEN. P1 phase evidence / Git observability defect. Branch
commits and deliverables remained safe, but remote phase tags can describe an
earlier boundary than the branch history the worker actually completed.

## Live evidence

Two successful jobs emitted repeated tag-push failures:

- job `96bb50c2-...`: three `git push --tags failed` warnings;
- job `90c74b6a-...`: four warnings.

The remote rejected the job's tactical-complete tag because it already existed:

```text
! [rejected] <job>-phase-1-tactical-complete ->
             <job>-phase-1-tactical-complete (already exists)
```

The branch push succeeded and both reports reached their final job branches.
The tag failure is deliberately ignored by `GitManager.push()`, so neither job
failed.

The commit histories show why the tag was pushed again. Both jobs archived and
committed “Phase 1 Tactical Complete” twice:

- `96bb50c2`: first complete at `21:21:37Z`, another tactical todo/closeout,
  second complete at `21:22:06Z`;
- `90c74b6a`: first complete at `21:48:46Z`, another closeout todo, second
  complete at `21:53:30Z`.

The remote `96bb50c2-phase-1-tactical-complete` tag still describes the earlier
todo-7 verification commit, not the later todo-8/second-complete state. The
report itself is present at both points, but the tag omits part of the phase it
claims to delimit.

## Source-level cause

Two choices combine:

1. `GitManager.tag()` always runs `git tag -f`, so an existing local boundary
   tag is silently moved to the current `HEAD`.
2. `_complete_phase_with_git()` creates/moves the tag **before** creating the
   phase-complete commit, then calls `git push --tags`.

Git tags are immutable on the remote by default. The first phase-completion path
pushes the tag. A repeated completion moves it locally; the next normal
`git push --tags` cannot update the existing remote tag and returns a partial
failure. Later finalization keeps retrying every local tag, so the stale tag
produces more warnings even while a new job-completed tag is accepted.

The generic strategic transition template treats boundary tags as authoritative
evidence and instructs the model to diff between the two most recent tags. A
stale tag therefore makes the review omit changes that are present on the
actual branch and can amplify REVIEW-AND-ADAPT reconciliation work.

## Required invariants

- A concrete phase instance has one immutable completion event.
- Its tag points at the actual phase-complete commit, not the commit immediately
  before it.
- Re-entering phase completion is idempotent; it must not move an already-pushed
  boundary.
- Pushing one new job tag must not retry every historical tag in the shared
  project repository.
- A tag failure must be visible as a phase-evidence defect even when the branch
  push succeeds.

## Fix direction

1. Commit the phase-completion record first, then create the namespaced tag at
   that commit.
2. Replace force-move behavior with create-once/idempotent comparison:
   - missing tag: create and push;
   - existing tag at expected commit: success/no-op;
   - existing tag at another commit: emit a typed invariant violation and keep
     the original immutable ref.
3. Push the exact new ref (`refs/tags/<name>`), not every local tag via
   `--tags`.
4. Find why the graph can archive the same phase instance twice and make that
   transition exactly-once. Idempotent Git behavior is a backstop, not a reason
   to retain the duplicate phase transition.
5. Have strategic review use the orchestrator's phase event plus commit SHA (or
   exact branch commits) rather than assuming a tag exists and is current.

Do not solve this with forced remote tag updates. Rewriting an audit boundary
destroys the evidence an observer may already have used.

## Acceptance criteria

- A normal phase tag dereferences to its phase-complete commit.
- Calling the completion path twice produces one completion commit/tag or one
  explicit invariant error, never a moved remote tag.
- Concurrent jobs in a shared project push only their own new tags.
- `git push` has separate branch/tag outcomes in telemetry.
- Transition review sees every commit made by the completed tactical phase.
