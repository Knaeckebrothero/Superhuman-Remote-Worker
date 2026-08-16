---
tags:
  - issue
  - workspace
  - git
  - datasources
  - tools
status: open
priority: P1
created: 2026-08-16
aliases:
  - shallow repo datasource checkout
  - repo_pull cannot fetch another branch
related:
  - "[[verification_ticket_cannot_reach_another_jobs_candidate_commit]]"
  - "[[deliverable_contract_satisfied_by_a_note_about_failure]]"
  - "[[session_restore_drops_repo_checkouts]]"
---

# A cloned repository datasource can only see its default branch, and no bound tool can repair it

**Status:** OPEN. Observed live on job `c4849fa1` (2026-08-16 08:24, project
Better Resavio). This is the blocker that survived after the connector-attachment
problem resolved itself — the repo *was* present and correctly attached, and the
work still could not proceed.

Distinct from [[verification_ticket_cannot_reach_another_jobs_candidate_commit]]
(LF-4): that is about per-job Gitea repo isolation. This is about the working-tree
checkout of an **external repository datasource** inside a correctly provisioned
workspace.

## Observed

The worker's own account, from `context.completion_decision`:

> FIRST ACT repository gate FAILED: nested `repos/KurortEngine/` could not resolve
> `design/hotel-rheinland-theme` at commit `5e08d4fa06da12a9ec00bbffd78225c6faefbe55`
> (`git_show` → `fatal: bad object`; `packed-refs` contains only `origin/main`; both
> `repo_pull` attempts to repair failed because the tool fast-forwards only the
> checked-out branch and `repo_clone`/`shell_execute` are not bound).

Three separate walls in one sentence:

1. **The clone carries only `origin/main`.** `packed-refs` has one entry. No other
   branch is fetchable.
2. **The named commit is not in the object store** — `fatal: bad object`. Note that
   `5e08d4fa` *is* reachable on `main` in the upstream repo (it is in the history
   behind merge `aafad4ac`), so this is clone depth/refspec, **not** a deleted
   object. The worker could see the commit named in its own ticket and could not
   open it.
3. **No repair path is bound.** `repo_pull` fast-forwards only the checked-out
   branch; `repo_clone` and `shell_execute` are not in the worker's toolset.

## Compounding: the ticket named a branch that no longer existed

`design/hotel-rheinland-theme` was deleted when PR #1 merged (2026-08-15 16:09).
The ticket, written before the merge, still targeted it. So even a complete clone
would have failed on the branch name — the correct target had become `main`.

Nothing in the chain noticed that a merged PR invalidates the refs of tickets
written against its branch.

## Why it matters

A repository datasource whose checkout cannot leave its default branch cannot
support the ordinary shape of repository work: branch from a base, stack on
someone else's branch, verify a specific commit, or resume against a ref recorded
in an earlier turn. Every one of those is a normal loop operation.

The failure is also silent at provisioning and expensive at discovery — it costs a
full worker run to learn that the workspace was never capable of the task.

## Direction

Cheapest first:

- **Fetch what the job names.** If `context` names a branch or commit for a
  repository datasource, fetch that ref at provisioning, or fail the dispatch with
  the reason. A worker should never be the thing that discovers its own workspace
  was unusable.
- **Give `repo_pull` a ref argument**, or bind a `repo_fetch`, so a worker can
  reach a ref it can name. Today the only tools that could repair it are exactly
  the ones not granted.
- **Re-anchor refs on merge.** When a PR that a ticket depends on merges, the
  ticket's base branch is gone; the ticket needs re-pointing at the merge commit or
  at `main` rather than failing on a dead branch name.

## Acceptance

- A job naming a non-default branch or an arbitrary commit of an attached
  repository datasource can check it out.
- A ref that genuinely cannot be resolved fails at dispatch with a precise message,
  not after a full worker run.
