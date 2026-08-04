---
tags:
  - architecture
  - git-integration
  - projects
  - self-improvement-loop
  - decision
aliases:
  - loop repo compounding
  - loop artifact compounding
  - project repo as core
  - in-place compounding
related:
  - "[[project_self_improvement_loop]]"
  - "[[loop_parallel_execution]]"
  - "[[repo_resolution]]"
  - "[[projects]]"
  - "[[project_knowledge_base]]"
  - "[[loop_review]]"
---

# Loop Artifact Compounding — Project Repo as Core, Agent Pushes to `main`

How the **project self-improvement loop** ([[project_self_improvement_loop]]) accumulates its *artifact* across iterations. It is a deliberate, **loop-scoped exception** to the per-job-repo decision in [[repo_resolution]]: for loop jobs, the project's shared repository `main` becomes the living deliverable, and each **execution** iteration works directly on `main` and pushes — so its code compounds *in place*.

> **Status:** SUPERSEDED by [[loop_repo_compounding_v2]] (accepted 2026-07-03) — per-job
> branches for ALL roles + orchestrator squash-merge onto `main`; the two-layer reframe
> and the `.gitignore` floor survive intact. Historical status: IMPLEMENTED (v1),
> 2026-06-26. Unit-verified (`tests/test_job_provisioning.py`, 15 passing) **and
> k3d-verified** (real developer loop run — see [k3d E2E results](#k3d-e2e-results-2026-06-26)).
> Fixed [[loop_review]] **F11** and the git half of **F12**; live-proven in run 7
> (2026-07-03, post-F29-fix) — the same run that demonstrated the costs (main bloat,
> discarded analysis output, heuristic integrity) that motivated v2.
>
> **Successor proposed (2026-07-03):** [[loop_repo_compounding_v2]] — per-job branches for *all* roles + orchestrator squash-merge + jobs-repo-as-coordination-repo. Addresses Open Q5 (`main` bloat, confirmed live in run 7), the analysis-output loss, and replaces the SHA-compare no-op guard with a literal merge outcome. This v1 model stays live in code until v2 ships.

> **Final successor (2026-08-04):** v2 is now also superseded. Every loop
> member uses an isolated `job-<short-id>` repository; project files are
> seeded from and applied back to the project cloud folder. See
> [[project_jobs_repo_retirement]]. Everything below is historical rationale.

## Problem

The loop's first real dev run ([[loop_review]], run 1, loop `27cabc53`) proved the *rotation* works — scholar proposes, critic selects, developer ships test-green code — but the loop **does not compound**:

- Every loop job branched `job/<short-id>` from the project repo's `main`, and **nothing ever merged back** (`job_provisioning.py` hardcoded `from_branch="main"`; `merge_status` was only ever `skipped`/`grafted`/`graft-failed`, never `merged`). So `main` stayed an empty README while 98 real commits sat stranded on `job/280719ed`. Iteration N+1 always started blank (**F11** — the dominant defect).
- With no merge and no graft *between sibling loop jobs*, the only way the developer saw predecessors' work was `git show origin/job/<id>:notes/…` across branches — git as an accidental, fragile handoff channel (**F12**).

The result was **N independent from-scratch attempts, not a chain.**

## The reframe — it is not "repo vs KB", it is two layers

The old "should the source of truth be the repo or the knowledge base?" debate ([[projects]] vs [[project_knowledge_base]]) was a false binary. A loop has **two things that compound, on two layers**:

| Layer | Source of truth | What accumulates | Mechanism |
|---|---|---|---|
| **Artifact** | git `main` (this doc) | the evolving codebase / deliverable — iteration N+1 edits the *same* files N produced | the execution agent commits + pushes `main` |
| **Reasoning** | the project KB (neo4j + pgvector) | proposals, the selected decision, tried/rejected approaches, the Definition of Done, convergence | `kb_write` / `kb_update`, read-at-start / write-at-end |

They are complementary, not competing. **Code lives in git; decisions live in the KB.** This is the split [[repo_resolution]] gestured at ("what actually merges to `main`? Just `knowledge/`, `output/`, `experts/` — two of those three are better in the database"). This doc owns the *artifact* layer; the *reasoning* layer is the companion work tracked in [[loop_review]] as F13/F22/F23/F24 (see [Boundary](#boundary--this-is-one-of-two-keystones)).

## The Decision

**For loop jobs, the project's shared repo `main` is the living artifact. The execution role works _directly on_ `main`; its existing `autonomy=full` commit + push (which the agent already does at every phase boundary and on completion) compounds the codebase in place. The next iteration clones the updated `main`. Analysis roles (scholar/critic) work on a throwaway `job/<id>` branch and coordinate only through the KB — they never touch `main`. A committed `.gitignore` keeps job-scoped scratch off `main`.**

This deliberately reuses what already exists rather than adding a merge subsystem:

- The agent **already** commits (`git add -A`, so it honours `.gitignore`) and pushes the **current branch** at `autonomy=full` (`src/managers/git_manager.py` `commit`/`push`; `src/core/phase.py`). Put the execution job *on* `main` and that push *is* the compounding — **no orchestrator merge, no PR, no squash, no new git code.**
- A clone leaves you on `main`, and `create_repo` auto-inits `main`. So "work on `main`" is just *not* creating a per-job branch.

### Why not the orchestrator-merge or subjob-graft designs

This doc previously specified an orchestrator-side curated squash-merge (`create_pr`/`merge_pr` driven off `freeze_data.deliverables`). It was dropped for the agent-push model after two realisations:

- **Agent-push reuses more and adds less.** The agent's commit+push already exists and is reliable at `autonomy=full`; pointing it at `main` needs no merge wiring and no deliverables-as-include-list (which would wrongly *drop* in-place edits to inherited files, and deleting inherited files to enforce an include-list would record them as deletions on `main`).
- **The subjob graft is the wrong shape.** Subjobs *graft* — purely-additive file-copy of `output/` into `outputs/<N>-<config>-<id>/` (`orchestrator/main.py:422`). That is correct for parallel subjobs (never clobber), but for a loop it yields **N side-by-side snapshots, not an evolving codebase**, and only captures `output/`. A loop needs in-place evolution; git history already provides the per-iteration archive the research wanted (DGM-style lineage) without subdir snapshots.

### Relationship to [[repo_resolution]]

[[repo_resolution]] decided "one repo per job, project is a pure DB entity, no shared repo." That stands **for one-off jobs**; its anti-shared-repo arguments were all about *arbitrary, parallel* project jobs (branch coordination, merge ordering, conflicts, gitignore gymnastics). **A loop has none of those properties** — it is sequential (the advance hook spawns N+1 only when N is terminal), single-goal, and system-orchestrated. So this is a *principled carve-out*, not a reversal. Non-loop jobs are unaffected.

> [[repo_resolution]] is itself only partially implemented — project jobs still use the shared repo today. This leans into that existing path for the loop case.

## How It Works

```
Loop job N spawned
  → _spawn_loop_job → provision_job_repo(work_on_main = is_loop_execution_role(role))
       EXECUTION role (developer / default / writer …):
         · ensure scratch .gitignore floor on main (idempotent)
         · branch_name = "main"  (NO per-job branch)
       ANALYSIS role (scholar / critic):
         · branch_name = "job/<short-id>"  (throwaway, never merged)
  → agent clones the repo (lands on main), checks out branch_name, works

Loop job N runs (autonomy=full)
  → agent commits (git add -A → honours .gitignore) + pushes current branch
    at each phase boundary and on completion (existing behaviour)
       EXECUTION → push lands on MAIN → the project artifact advances in place
       ANALYSIS  → push lands on the throwaway branch → main untouched;
                   its real output is KB notes

Loop advances (_advance_project_loop) → spawn job N+1
  → next EXECUTION job clones the UPDATED main → builds on prior code

Loop ends → main = the accumulated, in-place project artifact
          → KB   = the accumulated decisions / lineage
          → git history/tags = the per-iteration archive
```

### Guardrail 1 — in-place via the agent's own push

The execution job is *on* `main`; the agent's existing `git add -A` + push compounds it. "The agent decides what to push" is honoured at the natural granularity — what it writes to the working tree (minus the floor) — with no include-list to maintain and no orchestrator step.

### Guardrail 2 — the safety floor (`.gitignore`, resume-safe)

A committed `.gitignore` keeps job-scoped scratch off `main` (`git add -A` honours it). Seeded idempotently on `main` before the first execution job runs (`_ensure_loop_main_gitignore`, sentinel = presence of `todos.yaml`). Floor set:

```
workspace.md, plan.md, todos.yaml, archive/, tools/, documents/, reference/,
skills/, notes/, instructions.md, task_brief.md,
output/job_frozen.json, output/job_completion.json, repos/, .env, *.key, secrets*
```

`skills/` (capability bundles materialized into the workspace when `SKILLS_DB_ENABLED`) and `notes/` are framework/job-scoped, not deliverables — `skills/` was **added after the k3d E2E caught it leaking onto `main`**. The agent's actual code dir (the E2E developer used `repo/`) is deliberately **not** floored — that *is* the artifact and must reach `main`.

**Why gitignoring scratch is safe on resume** (verified): todos restore from the LangGraph **checkpoint**, not disk (`restore_todo_state`; `load_todos_from_yaml` is dead code); `plan.md`/`workspace.md` are read only for non-blocking curation with `FileNotFoundError` caught (`graph.py`); `archive/` is recovered from **phase snapshots** stored on the agent pod (independent of git), which still copy these files; pod-handoff clones from Gitea but restores state from the checkpoint. A fresh clone missing scratch therefore does not break resume — and this aligns with [[repo_resolution]]'s "working documents don't need git archiving."

### Guardrail 3 — role-scoping (via provisioning)

The role decides the branch, so analysis work physically cannot reach `main`:

| Role class | Roles | Branch | Reaches `main`? | Channel |
|---|---|---|---|---|
| Execution | `developer`, `default`, future `writer` | `main` | **yes** (its push) | git `main` (artifact) |
| Analysis | `scholar`, `critic` | `job/<id>` (throwaway) | **no** | KB (`kb_write`/`kb_update`) |

`is_loop_execution_role(role)` = `role not in {scholar, critic}` (the execution slot is swappable, so analysis is the closed set; unknown/empty → non-execution, the safe default). The role prompts (`_ROLE_BLOCKS`) state this: the developer is told it works on `main` and its commits auto-push and become the project; scholar/critic are told their branch is scratch and to coordinate via the KB (this also addresses **F4** role-bleed and the git half of **F12**).

## What This Means for Projects

- **Loop projects** get a living `main` that is the cumulative deliverable; git history is the per-iteration archive.
- **Non-loop project jobs** are unchanged: `provision_job_repo` defaults `work_on_main=False`, so they keep the existing `job/<id>` branch behaviour.
- **The KB remains the reasoning source of truth.** Decisions/proposals/rejections/DoD go to the KB, not `main`. `main` is code/deliverables only.

## Boundary — this is *one* of two keystones

This makes the **artifact** compound (kills F11; kills the git half of F12). It does **not** touch the **reasoning** layer. After this, the loop will still: re-propose rejected approaches (**F22** — no tried/rejected ledger); inject context by similarity-luck (**F23** — no pinning of decisions/DoD); be unable to measure convergence (**F24** — no project acceptance vector); let stale notes compete forever (**F13** — KB has no supersede path). Those are the **reasoning keystone**, to be tackled as a separate feature (own design doc). A loop that compounds code but still argues with itself about decisions is only half-fixed.

## Constraints

- **Real git required.** The agent commits/pushes a real workspace git, so loops are pinned to `remote`/`sandbox`/`vm` tiers — not `virtual`/`none` (no git; see [[no_workspace_agent_mode]]). For code loops this is fine; a prose/research loop would lean on the KB layer instead (see Open Questions).
- **Crash semantics.** Because the execution agent pushes at phase boundaries, a crashed mid-iteration developer can leave partial commits on `main`. For a continuous loop this is acceptable (progress persists; the critic reviews next; git history is recoverable) and is the price of the in-place model.

## Open Questions / Deferred fast-follows

1. **No-op guard / backstop (deferred).** v1 trusts the agent's reliable `autonomy=full` push. A fast-follow should, in `_advance_project_loop`, verify `main` advanced after an execution iteration (capture `main` HEAD at spawn, compare at advance), record `merge_status`, and warn loudly if it did not — so a silently non-compounding iteration is visible on overnight runs.
2. **Observability.** Surface per-iteration `main` diffs + `merge_status` in the cockpit Loop tab (`list_project_loop_jobs` does not yet select `merge_status`; ties into F16/F27).
3. **Finer curation.** If "everything minus floor" proves too coarse, add an opt-in exclude mechanism (a `promote`/`.loopignore` the agent controls). The include-list (`promote_to_main`) was rejected — it drops in-place edits.
4. **Non-code loops.** For prose `writer`/`default` execution, is `main` the right home (a growing document) or should prose compound through the KB only? v1 treats the deliverable uniformly as files-on-`main`.
5. **`main` bloat / lifecycle.** Retention over hundreds of iterations — defer.

## Implementation (as built, v1)

| File | Change |
|---|---|
| `orchestrator/services/project_loops.py` | `LOOP_ANALYSIS_ROLES = {scholar, critic}` + `is_loop_execution_role(role)`. `_ROLE_BLOCKS`: developer told it works on `main` (auto-push compounds the project); scholar/critic told their branch is scratch / coordinate via KB. |
| `orchestrator/services/job_provisioning.py` | `provision_job_repo(..., work_on_main=False)`. When `work_on_main` + a project jobs repo exists: seed the `.gitignore` floor (`_ensure_loop_main_gitignore`, idempotent) and set `branch_name="main"` (no per-job branch). `_LOOP_MAIN_GITIGNORE` constant = the floor set. Analysis + non-loop paths unchanged. |
| `orchestrator/main.py` (`_spawn_loop_job`) | Pass `work_on_main=is_loop_execution_role(role)` to `provision_job_repo`. |
| `tests/test_job_provisioning.py` | execution job → `branch_name="main"`, no branch, floor seeded; floor idempotent; analysis job → `job/<id>`, floor untouched; `is_loop_execution_role` cases. 15 passing. |

**No new git code**: `GiteaClient`/`GitManager` already have everything; the agent's existing commit+push does the compounding.

### Verification

- **Unit (done):** `pytest tests/test_job_provisioning.py` (15 pass) + `ruff check`/`format` clean.
- **k3d E2E (done):** see below.

### k3d E2E results (2026-06-26)

Ran a real `role_sequence=["developer"]` loop (`gpt-5.5`, admin owner, tiny `calc.py` goal) on the k3d cluster and inspected the orchestrator DB, Gitea, and the live agent workspace.

**Confirmed — the keystone works on a real run:**
- The execution job was provisioned **on `main`** (`HEAD=main`, no `job/<id>` branch) and the dispatcher ran it (agent + workspace pods spawned).
- `_ensure_loop_main_gitignore` seeded the floor `.gitignore` on `main` against real Gitea.
- The developer worked **directly on `main`** and committed real code — `repo/tests/test_calc.py` tracked, local `main` ahead of `origin/main` by 3 commits (pending the phase-boundary push); earlier spec artifacts (`spec.yaml`, `spec_lock.md`) had already pushed to `origin/main`. **Execution-role output compounds onto `main` in place.**
- The floor held: `plan.md`, `todos.yaml`, `archive/` existed in the workspace but were gitignored → **absent from `main`**.

**Caught + fixed:** `skills/` (capability bundles) leaked onto `main` — the floor was missing it. Added `skills/` + `notes/` to `_LOOP_MAIN_GITIGNORE`, with a regression assertion in the unit test.

**Not waited out (deliberately):** the developer hit the known **F1/F14** over-engineering pathology — ~28 min, phases 0→3, elaborate TDD ceremony on a one-line task — so full natural completion of both iterations was impractical. "Iteration 2 builds on the accumulated `main`" is **structurally guaranteed** (same verified main-provisioning path + a standard clone of `main`) and was not waited out. The throwaway test project/repo/loop were cleaned up afterward.

**Environment note (pre-existing, unrelated):** memory/KB retrieval errored on this cluster (`tsquery stack too small`) and the reranker returned `403` (gateway dark on dev, [[loop_review]] F19) — both contained/non-fatal and orthogonal to this change.

## What Doesn't Change

- **Per-job-repo model for non-loop jobs** ([[repo_resolution]]) — untouched (`work_on_main` defaults False).
- **Subjob graft** (`main.py:422`) — untouched; subjobs keep grafting `output/` to `outputs/`.
- **KB injection, memory recall, phase machinery, checkpoint/resume** — untouched.
- **`GiteaClient` / `GitManager`** — no changes.
- **The loop's advance/stop logic, role rotation, budget** — untouched (only the per-job branch decision changed).

## Related Documents

- [[project_self_improvement_loop]] — the loop feature this compounds.
- [[repo_resolution]] — the per-job-repo decision this carves a loop-scoped exception from.
- [[projects]] — original shared-repo model; superseded generally, revived loop-scoped here.
- [[project_knowledge_base]] — the reasoning layer / KB-as-blackboard.
- [[loop_review]] — test-run findings; fixes F11 + git-half of F12; F13/F22/F23/F24 are the companion reasoning-layer work.
