---
tags:
  - orchestration
  - git
  - subjob
  - critic
  - data-loss
status: open
priority: high
created: 2026-05-23
---

# Subjob squash-merge clobbers parent deliverables via destructive pre-merge cleanup

## Summary

When a subjob (critic or scholar) completes, its branch is squash-merged back into the
parent job's branch. Before the merge, a "pre-merge cleanup" step **deletes** a fixed set
of files and directories from the subjob branch. Because the subjob branch was forked
*from* the parent branch and is merged *back into* it, those deletions propagate to the
parent branch. When the deleted paths happen to hold the parent job's actual deliverables,
the merge **destroys the parent's work**.

This was reported as "the critic subjob overwrites the main job's entry." It is real.

**Status (2026-05-23):** Immediate fix landed on `develop` (TDD) — critics are no longer
squash-merged into the deliverable branch, and `documents`/`reference` were removed from
`SUBJOB_CLEANUP_DIRS`. Regression tests:
`tests/test_per_job_repo.py::TestSquashMergeDoesNotClobberParent`. Still open: the long-term
additive/restore-from-base merge rework + out-of-band critic storage (§ Recommended fix 2),
and recovery/audit of already-clobbered jobs (§ Recoverability).

## Evidence — job `227329ed` ("RAG Chatbot v2")

- The repo `job-227329ed` `main` branch (what the workspace button shows) contains only
  the critic's output: `output/reviews/`, `output/critic_verdict.json`, plus leftover
  scaffolding (`notes/`, 42-byte `README.md`, `todo_guide.md`).
- The main agent's RAG source corpus — **34 PDFs in `documents/`** (datasheets:
  `Pyrogel-*.pdf`, `Spaceloft_*.pdf`, `Cryogel-*.pdf`, `Slentex-A2.pdf` (3.9 MB), …) —
  is **gone** from `main`.
- It is intact at commit `c1a9fdda75^` (the commit immediately before the critic's merge),
  so **the work is recoverable**; nothing was force-pushed away.
- The merge commit (`c1a9fdda75`, PR #1, "Subjob 4736fc7e/critic: Verify deliverables of
  job 227329ed…") shows **104 changed files, +369 / −1434**. The deletions are
  `documents/` (34 files, binary), `tools/` (59 files), `archive/`, and working files;
  the additions are the critic's `reviews/` + `critic_verdict.json`. This matches the
  cleanup lists exactly.

### Why other jobs were not affected

Jobs whose deliverables live under `output/` survive, because `output/` is not in the
cleanup set — the merge is purely additive for them:

- `12e6da83` keeps `output/use_case_report.md` alongside the critic's `reviews/`.
- `74bf5d46` keeps `output/calculator_app.html` alongside the critic's output.

The destruction is specific to deliverables placed under a cleanup-targeted directory
(`documents/`, `tools/`, `archive/`, `reference/`). Document-centric / RAG jobs are the
natural victims.

## Root cause

The intended model (see [[subjob_worktree_sharing]]) is correct and matches expectations:
the parent job owns `main`; subjobs fork from it onto `subjob/<id>/<role>`, work in a
worktree, and squash-merge back. The flaw is purely in the merge's cleanup step.

In `orchestrator/main.py` (line numbers verified 2026-05-23):

- `SUBJOB_CLEANUP_FILES` (`:319-327`) / `SUBJOB_CLEANUP_DIRS` (`:329-334`) list paths to
  strip before merge. `SUBJOB_CLEANUP_DIRS = ["archive", "tools", "documents", "reference"]`.
- `_squash_merge_subjob()` (`:350-448`): resolves `base_branch = parent.branch_name or "main"`
  (`:378`), then **deletes** each cleanup path from the subjob branch via
  `gitea_client.delete_file(..., branch=subjob_branch)` (`:381-402`), creates a PR (`:407`)
  and squash-merges it (`:424-429`). The dir loop (`:395-402`) is **non-recursive** — it
  deletes only the immediate files of each cleanup dir, so a flat `documents/*.pdf` corpus
  is wiped entirely while a nested layout would be only partially hit.
- Subjob branches fork from the parent (`from_branch = parent.branch_name or "main"`):
  critic at `:7209-7213`, scholar at `:6534-6539`, generic create-job at `:3889-3894`
  (project jobs get `job/<short_id>` at `:3924`; standalone root jobs get `branch_name=None`).
- The merge fires from **three** callers, none gated on the verdict: the completion handler
  for any non-delegation subjob (`:7528-7531`), the operator `approve_job` path (`:6204-6205`),
  and the agent-invoked `subjob-merge` endpoint (`:4200`). `merge_pr` (`gitea.py:1176`) is
  squash-only — no force / reset / `-X ours|theirs`; the deletion comes solely from the
  pre-merge `delete_file` loop. (The agent push is `push -u`, no `--force`.)

The cleanup's own docstring (`:353`) states its intent:

> "deletes job-scoped files from the subjob branch before creating the PR, **so the
> parent's workspace.md / plan.md are not overwritten**."

The implementation **inverts that intent.** Deleting a file on a branch that is then
merged *into* `base` deletes it from `base` — it does not preserve the parent's copy.
For regenerable scratch (`workspace.md`, `plan.md`) this is survivable; for `documents/`
(source corpus) it is catastrophic.

Two compounding factors:

1. **Destructive cleanup** — `delete_file` propagates deletions through the squash merge.
2. **Branch collapse** — job `227329ed` has `branch_name = None`, so `base_branch`
   resolved to `main`; the clobber landed on the repo's default/visible branch. (Jobs in
   project repos get an explicit `job/<id>` branch at `:3924`; the same destruction would
   still hit that deliverable branch — `None` only changed *which* branch was hit.)

### Additional findings (from investigation)

- **The critic merge has essentially no value to the deliverable.** The verdict is consumed
  from the DB (`freeze_data`, in `_handle_critic_verdict_on_complete` `:6914-7027`), not from
  any merged file. The only files a critic adds are `output/critic_verdict.json`
  (`src/core/phase.py:721`) and `output/verification_report.json`
  (`src/tools/evaluation/evaluation_tools.py:122`), and nothing reads them off the parent
  branch. (`output/reviews/` is scaffolded empty and stays empty — so "merge only
  `output/reviews/`" is not a meaningful additive target.)
- **Scholar vs critic asymmetry.** The scholar runs *before* the main job, so the parent
  branch is nearly empty at scholar-merge time → minimal blast radius; its output lands in
  `research/` (not a cleanup dir) and survives. The critic runs *after*, when the parent
  holds everything → catastrophic. Both run the identical destructive cleanup.
- **Cleanup of structure dirs is mostly moot for its stated purpose.** Workspace scaffolding
  `mkdir`s `documents/`, `tools/`, etc. without `.gitkeep`, so empty structure dirs are never
  committed and never appear in a diff. The cleanup of those dirs only ever bites when the
  *parent* committed real content there.

## Why this happens — the general principle

It is a **modeling error, not a git limitation.** A squash merge applies B's *net diff
against the merge base* onto A as a single commit; a deletion on B is therefore an
instruction to delete on A. `-X ours` / `-X theirs` don't help (they act only on
*conflicts*; a clean one-sided deletion sails through). The intent — "leave the parent's
copy alone" — must be implemented as "keep B's copy byte-identical to base," never as
"delete B's copy."

Industry practice converges on a sharper rule: **a reviewer's output is *metadata about*
the deliverable, not part of it.** Devin (review = PR comments + status checks), OpenHands
(critic scores the trajectory out-of-band), Aider (architect reasoning is consumed then
discarded — only edits land), and Reflexion / LangGraph (critique = separate memory; the
*actor* revises) all keep critic output off the deliverable. Pre-job research the actor
legitimately consumes is treated as *input/context*, not a peer commit merged back over
the actor's work.

## Proposed solutions

### Recommended (layered)

**1 — Immediate, stops the data loss:**

- **Don't merge critic branches into the deliverable at all.** Gate the three merge callers
  (`:7528-7531`, `:6204-6205`, `:4200`) to skip critic-type subjobs. Safe because the verdict
  is already consumed via DB `freeze_data`, and the critic's two JSON files aren't read off
  the parent branch. (Eliminates the catastrophic case at its source — no git plumbing.)
- **Drop `documents` and `reference` from `SUBJOB_CLEANUP_DIRS`** (`:329-334`) to protect the
  scholar/delegation merge paths. Safe because structure dirs are never committed empty, so
  the original "strip scratch" intent (covered by `archive/`) is unaffected.

**2 — Correct long-term (make every subjob merge non-destructive):**

- Replace delete-then-squash with an **additive / restore-from-base** merge, done by shelling
  out to `git` in a worktree (the Gitea HTTP merge API has **no path-scoping**; the repo
  already has a shell squash path at `src/managers/git_manager.py:1090`). Two viable mechanics:
  - *Pin protected paths to base before merging:* `git restore --source=<base> -- <paths>` on
    the subjob branch (handles un-delete + revert in one step), then squash as today.
  - *Add-only subtree graft:* build the result tree as "base + the subjob's new subtree" via
    `git read-tree --prefix=<dir>/ <subjob>:<dir>` / `git merge-tree --write-tree`, so paths
    outside the subjob's output dir structurally cannot be deleted.
- **Store critic output out-of-band** keyed to the deliverable commit — `git notes`
  (`refs/notes/review`), an orphan `reviews/<job-id>` branch, or a gitignored `.review/` the
  orchestrator harvests — and feed the verdict back to the **main job** as context for
  revision. **Scholar output** stays additive: consume as injected context, or merge only a
  disjoint subtree (e.g. `research/`, already outside the cleanup set).

### Guardrails to encode

Never whole-branch-squash a subjob (squash silently mishandles deletions); never hardcode
`-X ours|theirs` (semantics flip under rebase); use `--force-with-lease` if any shared ref
is involved; prefer path-scoped additive merges over whole-branch merges.

### Rejected / deprioritized

- *Restore-from-base via the Gitea API (per-file PUT):* most error-prone (the "exists on
  branch but not on base" edge silently fails) and heavy API traffic. Do it in a worktree.
- *Shrinking the cleanup set alone:* necessary but insufficient — leaves the
  delete-propagates-through-merge inversion in place for the remaining paths.

## Test plan

- Suite: `pytest` + `pytest-asyncio` (strict mode → every async test needs
  `@pytest.mark.asyncio`). CI runs `pytest tests/ -x -q --tb=short`; deps come from both
  `requirements.txt` and `orchestrator/requirements.txt`. Relevant file:
  `tests/test_per_job_repo.py` (`TestSquashMergeSubjob`, `TestSubjobCleanupConstants`).
- The current tests miss the bug because `gitea_client` is a **stateless** `AsyncMock` that
  only counts calls. Add a failing test driving `_squash_merge_subjob` with a **stateful fake
  Gitea** that models per-branch trees (`delete_file` mutates the branch tree; `merge_pr`
  squash overlays head→base), then assert a parent-only file under `documents/` **survives**
  the critic merge.
- Fixing the root cause will require updating tests that assert the current delete behavior:
  `test_directory_cleanup_deletes_files_in_dirs`, `test_successful_squash_merge` (delete-count
  assertion), and `TestSubjobCleanupConstants::test_cleanup_dirs_contains_archive` (asserts
  `documents` ∈ `SUBJOB_CLEANUP_DIRS` — directly contradicts the fix).

## Recoverability / cleanup

- Restore `227329ed`'s corpus from `c1a9fdda75^` in repo `job-227329ed`.
- Audit other completed jobs that received a subjob merge and stored deliverables under
  `documents/`/`tools/`/`archive/`/`reference/` for the same loss.

## References

Git mechanics:

- [git-restore](https://git-scm.com/docs/git-restore) — `--source`, no-overlay un-delete, `A...B` merge-base shorthand
- [git-read-tree](https://git-scm.com/docs/git-read-tree) / [git-merge-tree](https://git-scm.com/docs/git-merge-tree) — additive subtree graft / off-to-the-side tree merge
- [git merge-strategies](https://git-scm.com/docs/merge-strategies) — why `-X ours|theirs` and `-s ours` are the wrong tools here
- [Azure DevOps — squash merge](https://learn.microsoft.com/en-us/azure/devops/repos/git/merging-with-squash) — squash = net diff vs merge base as one commit
- [Gitea merge API](https://docs.gitea.com/api/1.20/) — no path-scoping on the merge endpoint

Prior art (review output kept off the deliverable):

- [Devin — automatic PR reviews](https://cognition.ai/blog/devin-101-automatic-pr-reviews-with-the-devin-api) (comments + status checks)
- [OpenHands — learning to verify](https://openhands.dev/blog/20260305-learning-to-verify-ai-generated-code) (critic scores the trajectory, out-of-band)
- [Aider — architect/editor](https://aider.chat/2024/09/26/architect.html) (reasoning consumed, only edits land)
- [Reflexion (arXiv 2303.11366)](https://arxiv.org/abs/2303.11366) / [LangChain reflection agents](https://www.langchain.com/blog/reflection-agents) (critique = separate memory; actor revises)
- [GitHub Checks API](https://docs.github.com/en/rest/checks/runs), [git notes / Gerrit `refs/notes/review`](https://gerrit.googlesource.com/plugins/reviewnotes/+/master/src/main/resources/Documentation/refs-notes-review.md) — side channels for verdicts/annotations

## Related

- [[subjob_worktree_sharing]] — the (sound) design this merge step belongs to
- [[repo_resolution]] — `resolve_job_repo` / branch model
- [[git]] — repository conventions
