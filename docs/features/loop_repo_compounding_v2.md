---
tags:
  - architecture
  - git-integration
  - projects
  - self-improvement-loop
  - decision
status: draft
created: 2026-07-03
aliases:
  - loop repo compounding v2
  - per-job branches + squash merge
  - jobs repo as coordination repo
related:
  - "[[loop_repo_compounding]]"
  - "[[project_self_improvement_loop]]"
  - "[[okf_knowledge_base]]"
  - "[[loop_parallel_execution]]"
  - "[[loop_review]]"
  - "[[loop_optimization]]"
---

# Loop Repo Compounding v2 — Per-Job Branches, Orchestrator Squash-Merge, Jobs Repo as Coordination Repo

**Status:** DRAFT — proposed successor to [[loop_repo_compounding]] (v1). v1 stays live in
code until this ships; **do not churn the model while the current run era is proving the
F29 fix bundle.** Origin: design discussion 2026-07-03, prompted by live run-7 evidence.

## TL;DR

- **Every loop job — scholar, critic, developer alike — works on its own `job/<id>`
  branch** carrying its full phase-commit history as the audit log.
- **On completion, the orchestrator squash-merges the job's net contribution onto `main`**
  (one clean commit per job) and keeps the branch. `main` stays readable; nothing is lost.
- **The jobs repo becomes the project's coordination repo** — knowledge base notes
  ([[okf_knowledge_base]]), backlog/task tracking, and a standardized per-job **retro
  collection**. In real projects, code goes to *attached artifact repos*; the test-project
  case (code on the jobs repo) still works, it just squash-merges like everything else.
- `merge_status` becomes **literal**: `merged` / `empty` / `merge-failed`. An empty squash
  from a job that claimed work *is* the no-op detection — mechanical, replacing the
  SHA-compare guard heuristic.
- `main` gets **branch protection**: agents physically cannot push it; only the
  orchestrator merges. Defense in depth for role-bleed (F4).

## Why v1 needs superseding — live evidence

v1 ([[loop_repo_compounding]]) deliberately chose "execution works directly on `main`,
the agent's phase-boundary push is the merge" to ship with zero new git machinery. It
worked — run 7 (2026-07-03, post-F29-fix) is the first run where `main` actually
compounds. The same run demonstrates the costs v1 knowingly deferred:

1. **`main` bloat is real** (v1 Open Q5): a single developer iteration pushed ~8 phase
   commits (`[Phase 5 Tactical] todo_3: …`) straight onto `main`. Per-iteration history
   belongs on a branch; `main` should read as one commit per job.
2. **The audit-trail dilemma**: under v1 you either bloat `main` (status quo) or squash
   the agent's push and lose the developer's phase trail. Analysis roles have branches;
   the developer — the role whose history matters most — is the only one without.
3. **Analysis file output is discarded**: scholar/critic branches are "throwaway, never
   merged"; only their DB KB notes survive. Acceptable while the KB is a database;
   untenable once the KB is markdown-in-the-repo ([[okf_knowledge_base]]) — their
   contribution *is* files.
4. **Artifact truth is a heuristic**: v1's integrity signal is the retrofitted no-op guard
   (capture `main` HEAD at spawn, compare at advance) — which shipped broken once already
   (Gitea `/git/commits` 404-on-branch-names, silently swallowed; fixed 2026-07-03).
   A guard that *infers* whether work landed is strictly worse than a merge step that
   *knows*.
5. **Crash semantics**: a crashed mid-iteration developer leaves partial commits on
   `main` (v1 "Constraints" accepted this as the price of in-place).
6. **No branch protection possible**: v1 *requires* agents to push `main`, so nothing
   stops a confused analysis role from doing the same.
7. **F40 gaslighting**: a destroyed developer's "SHIPPED" retro stays active in the KB
   and no later role can check it against `main`. Fixed structurally below (retro
   collection + literal merge status).

## The model

```
Loop job N spawned
  → provision: branch job/<short-id> cut from current main   (ALL roles — no asymmetry)
  → agent clones, works on its branch
  → phase-boundary commits+pushes land on the BRANCH (durable in Gitea, main untouched)

Loop job N completes (status=completed)
  → _advance_project_loop:
      squash-merge job/<id> → main            (one commit: "iter NN · role: <summary>")
      merge_status = merged | empty | merge-failed
      branch KEPT (audit log; lifecycle/retention is a later concern)
      post-merge hook → KB reindex ([[okf_knowledge_base]] §5)
  → spawn job N+1: branch cut from the NEW main

Loop job N fails / crashes
  → NO merge. main untouched. Branch preserved with whatever partial work exists.
```

**Conflict-free by construction (sequential loop):** the advance hook merges job N
*before* spawning N+1, so every branch is cut from current `main` and the squash is
always fast-forward-clean. Only [[loop_parallel_execution]] introduces real conflicts
(needs a rebase-or-merge policy + [[okf_knowledge_base]] id-claim; out of scope here).

### What lands on `main` — repo roles

| Repo | Role | Contents on `main` |
|---|---|---|
| **Jobs repo** | Coordination | KB notes (if the project KB lives here — see [[okf_knowledge_base]] open question), `backlog/` task tracking, `retros/` (below), and — for projects without an artifact repo — the code itself. |
| **Attached artifact repo(s)** | Deliverable | The actual product code. Same branch + squash-merge pattern per job; recorded in `jobs.repo_merge_statuses` (the column already exists, plural, unused — this is its purpose). |

The `.gitignore` floor from v1 is unchanged and still necessary — it keeps job-scoped
scratch (`plan.md`, `todos.yaml`, `archive/`, `skills/`, …) out of the branch commits so
the squash contains only contribution.

### The retro collection (F40 killer)

Every job's final act (or the orchestrator's, from `freeze_data`) is a standardized
retro note — `retros/NNN-<role>-<jobid8>.md` — with OKF frontmatter: iteration, role,
job id, what was attempted, what landed, links to the KB decision/proposal notes it
executed. Because it merges *with* the contribution:

- A retro on `main` is **backed by a real merge** — critics read `retros/` + `git log
  main` instead of trusting KB self-reports from destroyed workspaces.
- `merge_status=empty` + a retro claiming shipped work = the contradiction is mechanical.

### `merge_status` semantics (per role)

| Outcome | Meaning |
|---|---|
| `merged` | Squash produced a non-empty commit on `main`. Expected for every role once the KB is file-based (retro at minimum). |
| `empty` | Job completed but its branch has no net diff vs `main`. For a developer: the F29-family red flag, surfaced in the advance hook exactly where the v1 no-op guard fires today. For analysis roles under a DB KB: expected-ok (until [[okf_knowledge_base]] lands, then also a flag). |
| `merge-failed` | Squash errored (conflict — sequential loop shouldn't see one; treat as loud failure, don't advance silently). |

This retires the SHA-compare no-op guard: keep its `_advance_project_loop` placement and
logging shape, replace its heuristic with the merge call's actual outcome.

## What changes vs. v1 (mechanics)

| Area | v1 | v2 |
|---|---|---|
| Branching | Execution on `main`; analysis on throwaway `job/<id>` | **All roles on `job/<id>`** — `work_on_main` provisioning asymmetry deleted |
| Merge | The agent's own push *is* the merge (execution only) | Orchestrator squash-merge on completion, all roles |
| `is_loop_execution_role` | Decides branch + guard scope | Prompt-content concern only (+ `empty` severity) |
| Role prompts | Developer: "you work on `main`, pushes compound" | Developer: "your branch merges to `main` on completion; partial work is safe on your branch" |
| No-op guard | SHA compare at spawn/advance | Literal merge outcome |
| Branch protection | Impossible | Enable on `main` (Gitea) |
| Agent git machinery | Untouched | **Still untouched** — agents keep committing/pushing their current branch; only the *target* changes back to a branch |
| Orchestrator git machinery | None | One squash-merge step (Gitea PR-merge API with squash, or merge endpoint — the `create_pr`/`merge_pr` shape sketched in the design v1 dropped) |

v1's core objection to orchestrator-merge — "an include-list drops in-place edits" — does
not apply: a whole-branch squash *has* no include-list; the floor `.gitignore` already
defines contribution vs. scratch at commit time.

## Sequencing

1. **Not before the current run era concludes.** The F29 bundle + fixed no-op guard need
   the overnight-run soak they were built for; v2 replaces the guard, so let the guard
   first tell us what runs 7+ actually do.
2. **v2 is independent of [[okf_knowledge_base]]** (works with today's DB KB — analysis
   merges are just usually `empty` + a retro) but is *designed for* it; the retro
   collection is the first file-based-KB convention and can ship with v2 alone.
3. Implementation slices: (a) provisioning: all roles branch; (b) advance hook:
   squash-merge + literal `merge_status` (+ retire the SHA guard); (c) retros;
   (d) branch protection on `main`; (e) artifact-repo merge via `repo_merge_statuses`.

## Open questions

- **Branch retention**: keep every `job/<id>` forever (cheap, it's Gitea) vs. prune after
  N iterations / on loop end. Leaning keep-until-loop-end + archive tag.
- **Squash author/message**: commit as the orchestrator bot with `iter NN · role:
  <freeze_data summary>`? Trailer with job UUID for traceability?
- **Merge gate**: run `kb_lint` before merging KB-touching branches
  ([[okf_knowledge_base]] open question — leaning merge-then-flag)?
- **Non-loop project jobs**: unchanged here (their `job/<id>` + graft model stands), but
  v2's squash-merge is the obvious future for them too — flag for [[repo_resolution]].

## Related

- [[loop_repo_compounding]] — v1, superseded by this design when it ships (v1 doc carries
  a pointer). Its reframe (artifact layer vs. reasoning layer) and `.gitignore` floor
  survive intact.
- [[okf_knowledge_base]] — the KB-as-OKF-repo design; v2's merge flow is its reindex
  trigger and its notes are v2's payload.
- [[project_self_improvement_loop]] / [[loop_review]] / [[loop_optimization]] — the loop
  and its findings (F4, F29, F40 addressed structurally here).
- [[loop_parallel_execution]] — the concurrency mode that inherits real merge conflicts.
