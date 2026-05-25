---
tags:
  - orchestration
  - git
  - subjob
  - delegation
  - critic
  - scholar
  - design
status: open
priority: high
created: 2026-05-24
related:
  - "[[subjob_merge_clobbers_parent_deliverables]]"
  - "[[repo_resolution]]"
  - "[[subagent_delegation]]"
  - "[[subjob_worktree_sharing]]"
---

# Subjob & delegation branch/merge model — current state, vision, and open questions

This is a **capture** document: it records the issue, the target vision, the current
implementation state, and the open questions — so we can design the proper solution
without losing any of the findings. **It deliberately does not pick a solution yet.**

## Background — how we got here

1. **Symptom reported:** "the critic subjob overwrites the main job's entry." Confirmed real
   on job `227329ed` ("RAG Chatbot v2"): the repo's `main` showed only the critic's review
   output; the main job's 34-PDF source corpus under `documents/` was gone. The critic's
   squash-merge (PR #1, `c1a9fdda75`, "104 files, +369 / −1434") had deleted it.
2. **Root cause:** the pre-merge "cleanup" in `_squash_merge_subjob` *deletes* a fixed set
   of dirs (incl. `documents/`) from the subjob branch; because a subjob is forked from and
   merged back into the parent branch, those deletions propagate onto the parent. Full
   write-up: [[subjob_merge_clobbers_parent_deliverables]].
3. **Immediate fix landed (2026-05-24, TDD):** critics are no longer squash-merged into the
   deliverable branch (detected via `context.verification_target`), and `documents`/`reference`
   were removed from `SUBJOB_CLEANUP_DIRS`. Regression tests:
   `tests/test_per_job_repo.py::TestSquashMergeDoesNotClobberParent`.
4. **The broader question (this doc):** the incident exposed that the whole subjob/delegation
   branch-and-merge model needs a coherent design. Four exploration agents surveyed the code
   and docs; their findings are captured below.

## The vision (target model)

Stated by the user; this is the north star.

- The **main job works on the `main` branch**.
- A **subjob branches off the parent's branch the moment it is created**, runs its own job,
  and **merges back into the parent's branch when complete**.
- The **subjob branch remains after merge**, so the user can still view the subjob's
  phase/commit history on its own branch.
- On merge, the subjob **squashes all its commits into a single commit** on the parent
  branch — so the parent isn't bloated by the subjob's phases (e.g. when `delegate_work`
  spawns 5 subagents, the parent sees 5 commits, not 5×N).
- The **same pattern repeats recursively to a user-defined depth**: a subjob (e.g. the
  critic) can spawn subjobs of its own, which branch off the subjob's branch and merge back
  into it exactly as the subjob does to the parent.

The user's four explicit open questions:
1. How do we handle **merge conflicts**?
2. If subjobs fork the parent's branch, what happens to the **parent's plan, tools, and
   other agent-control files**? (A parent must be able to delegate different experts with
   different configs — e.g. a `developer` using a `scholar` subjob to look something up.)
3. What do we **merge back** into the parent branch? Just the `output/` folder, leaving the
   rest of the agent's work on its own branch?
4. We **don't support async subjobs** today, so there shouldn't be concurrent merge
   conflicts yet — we could just error if one occurs.

## Current state

**Good news: the vision is essentially the documented canon and is ~80% built.**
`[[repo_resolution]]` is titled *"One Repo Per Job, Subjobs as Branches. Squash merge on
completion,"* and `[[subagent_delegation]]` is marked *Implemented*. The gaps are (a) it's
built **twice in two divergent ways**, (b) **recursion is mostly unwired**, and (c) the
**merge mechanic is unsafe** (the clobber bug).

### Two parallel subjob "worlds"

| | **World A — lifecycle subjobs** (scholar, critic) | **World B — delegation** (`delegate_work`, 1–5 subagents) |
|---|---|---|
| Branch | `subjob/<short_id>/<config>`, created server-side | tool/manifest assume `subagent/N` in local `.worktrees/`, but orchestrator actually creates `subjob/<short_id>/<config>` (mismatch) |
| Who merges | the **orchestrator** (`_squash_merge_subjob`, Gitea PR) | the **parent agent itself**, via `git_merge_squash` tool in a worktree |
| Trigger | auto, on subjob completion (`main.py:7548-7553`, gated `creation_order is None`) | parent reviews on resume; auto-merge is **skipped** for delegation children |
| Branch after merge | **retained** (`delete_branch_after_merge=False`) | **deleted** in `git_worktree_cleanup` |
| Status today | works; matches the vision (post clobber-fix) | half-broken (see "Delegation wiring inconsistencies") |

Reconciling these two worlds into one coherent model is the central architectural task.

### Branch model per job kind

- **Root job, standalone:** own repo `job-<short_id>`, `branch_name=None` → works on `main`
  (`main.py:3988-4005`). Matches "main job works on main."
- **Root job, project-attached:** branches `job/<short_id>` off `main` in the project's jobs
  repo (`main.py:3939-3969`) — *not* literally `main`.
- **Subjob (level 2):** `subjob/<short_id>/<config>` forked from
  `from_branch = parent.branch_name or "main"` (`main.py:3911`; critic `~7231`; scholar `~6556`).
- **Nested subjob (level 3+):** the branch/merge *plumbing is genuinely recursive* —
  `from_branch` and `base_branch = parent.branch_name or "main"` resolve correctly for a
  grandchild, and `resolve_job_repo` walks the `parent_job_id` chain. **Arbitrary depth works
  at the git layer.** What's missing is the *spawning* (see Recursion).

### Merge mechanics

- `_squash_merge_subjob` (`main.py:~360-470`): resolve base, run pre-merge cleanup
  (`SUBJOB_CLEANUP_FILES`/`SUBJOB_CLEANUP_DIRS`, now `["archive","tools"]`), create PR, squash-merge,
  `delete_branch_after_merge=False`.
- Squash gives the parent a single commit — matches the vision.
- **The clobber bug** (now fixed for critics + `documents`/`reference`) proves the deeper
  principle: *a squash applies the branch's net diff vs the merge base, so a delete on the
  branch is a delete on the parent.* "Don't overwrite the parent's copy" must be implemented
  as **"keep the branch copy byte-identical to base," never "delete the branch copy."** The
  long-term **additive / restore-from-base merge** (`git restore --source=<base>` or
  `git read-tree --prefix` subtree graft, in a worktree — the Gitea API has no path-scoping)
  is still **open** and is the binding constraint for everything else. See
  [[subjob_merge_clobbers_parent_deliverables]] §"Proposed solutions".

### Deliverable boundary — `output/` is NOT universal

What counts as a subjob's "deliverable" varies by expert:

| Expert | Deliverable location | `output/` enough? |
|---|---|---|
| scholar | `output/ideas/`, `output/experiments/` | ✅ |
| bughunter | `output/findings/`, `output/repros/` | ✅ |
| critic | verdict consumed from DB; not merged at all | n/a (don't merge) |
| **developer** | **code in `repo/`** (`output/` ≈ just `completion.json`) | ❌ drops the code |
| **designer** | **`mockups/`, `design_spec/`** | ❌ drops the design |
| designer-interactive | `mockups/`, `design_spec/`; **no `output/`** | ❌ merges nothing |
| curator | writes to the knowledge base, not files (now inline, not a subjob) | n/a |

Also: `output/` itself contains process-control JSONs that are **not** deliverables
(`completion.json`, `job_frozen.json`, `job_completion.json`, `critic_verdict.json`,
`verification_report.json`). So neither "merge only `output/`" nor "merge the whole branch"
is right as-is. Likely answer: a **per-expert declared deliverable subtree** merged additively.

### Recursion / depth — designed, not wired

- `delegation.max_depth` defaults to **1** (`config/defaults.yaml`).
- The depth check calls `GET /api/jobs/{id}/delegation-depth` (`delegate_work.py:~193`) — **that
  endpoint does not exist** → the check **fails open** (assumes depth 0).
- `postgres.get_delegation_depth()` (recursive CTE, the correct "Option C" logic where
  lifecycle links count as depth-0) exists but is **dead code — no callers**.
- Delegation children are created with `config_override = {"autonomy":"full","delegation":{"enabled":false}}`
  (`delegate_work.py:~322-326`), so a child **cannot sub-delegate at all** regardless of depth.
- Net: today recursion is blocked outright, not depth-limited. "User-defined depth" is the
  main net-new design space. (`[[subagent_delegation]]` decision #7: "No nesting in v1" — intentional.)

### Conflict handling — a dead-end

- **World A:** `merge_pr` returns `True` only on 2xx; **any other status → `False`** (so a
  real conflict is indistinguishable from a transient/permission error). On `False`,
  `merge_status="conflict"` is written **but never read** by anything; no retry, no parent
  notification, PR left open.
- **World B:** `git merge --squash` greps for `CONFLICT`, returns failure to the agent, and
  does **not** `git merge --abort` (leaves a dirty tree). Intended resolution = the **parent
  LLM resolves during review** (or resumes the child to rebase); no programmatic fallback.
- The user's "just error if a conflict happens" is viable *today* (no async), but note
  conflicts can still arise from **sequential** delegate-children merges and **multi-round**
  critics, so "error" needs a defined surface (who is told, what state the job lands in).

### Concurrency — parallel delegation, no async (deliberate)

- `delegate_work` fans out **1–5 children in parallel** (`MAX_SUBAGENTS=5`); the dispatcher
  assigns one job per free agent, so siblings run concurrently on separate branches.
- The parent **suspends to `waiting`** (freeze) until all children are terminal, then resumes.
- Merge order is forced **sequential by `creation_order`** at review time, even though
  execution is parallel.
- **No async/fire-and-forget mode** — a deliberate, industry-aligned decision
  (`[[subagent_delegation]]` decision #1), not a gap. Confirms the user's point #4.
- Scholar/critic are spawned one-at-a-time per lifecycle event (serial per parent).

### Worktree sharing & VM

Subjobs inherit the parent's VM/container and use `git worktree add` instead of a fresh
clone (`[[subjob_worktree_sharing]]`, implemented): parent at `/home/agent-host/workspace`
(`main`) + subjob at `/home/agent-host/worktrees/<short>-<config>`. Worktrees are ephemeral
(die with the VM); the Gitea branch persists.

### Multi-round / persistent subjob branch sync

For subjobs that pause and resume (multi-round critics, persistent threads), the documented
model is **bidirectional sync**: merge the subjob branch → parent `main` when entering
`waiting`, and **pull parent `main` → subjob branch when resumed**
(`[[project_knowledge_base]]` Q17/Q26). This is the documented answer to "what happens to the
fork while the parent moves on."

### Delegation wiring inconsistencies (World B is half-broken)

- **Branch-name mismatch:** tool/manifest/instructions assume `subagent/N`
  (`delegate_work.py`, `agent.py:~108`); orchestrator creates `subjob/<short_id>/<config>`
  (`main.py:~3911-3913`); the client never sends `branch_name`/`worktree_path` for delegation
  children (`orchestrator_client.py:~896-907`). The parent is told to diff/merge branches that
  don't exist under that name.
- **No merge tool is granted:** `git_merge_squash`/`git_worktree_cleanup` exist
  (`git_tools.py:243-326`) but **no shipped expert config lists them** in `tools.git` — so
  "the parent reviews and merges" has no merge tool wired in.
- **`developer` delegation is implicit:** it works only because the config omits
  `tools.delegation` and inherits it; explicit delegation-enabled experts are `scholar` and
  `critic`. There is no `dev` alias (the expert is `developer`).

## Vision ↔ current state scorecard

| Vision element | Status |
|---|---|
| Main job works on `main` | ✅ standalone; ⚠️ project jobs use `job/<id>` |
| Subjob forks parent's branch at creation | ✅ |
| Subjob squash-merges back as one commit | ✅ (World A auto; World B parent-driven) |
| Subjob branch retained for history | ⚠️ retained for scholar/critic; **deleted** for delegation; canon self-contradicts |
| Recursive to user-defined depth | ❌ capped at 1 and not wired (children can't sub-delegate) |
| Critic/other subjobs can spawn subjobs | ⚠️ designed (lifecycle = depth-0) but blocked in practice |
| Merge is safe / non-destructive | ❌ until the additive/restore-from-base rework lands (clobber-fix is partial) |

## Open questions (to resolve during design)

From the user:
1. **Merge conflicts** — error-and-surface vs parent-agent-resolves vs additive-merge-avoids-most. If "error," define the surface (job state, who's notified).
2. **Parent control-files on fork** — confirmed mostly a non-issue: a forked subjob inherits
   `plan.md`/`tools/`/`instructions.md` but overwrites them at startup (different expert), and
   they shouldn't merge back. Open: should a subjob see the parent's *live* plan vs the
   branch-point snapshot?
3. **What to merge back** — per-expert deliverable subtree (recommended) vs whole branch diff
   vs just `output/`. Needs a declared boundary (e.g. `workspace.deliverables` per expert).
4. **Async** — resolved as "intentionally none"; only revisit if we want it.

Surfaced by investigation:
5. **Unify the two worlds?** Bring delegation into the orchestrator/Gitea server-side model
   (consistent branches, retention, merge) — or keep them separate?
6. **Recursion depth** — wire the depth model (add the missing endpoint, stop force-disabling
   child delegation, decide lifecycle-vs-delegation depth policy)? How much for v1?
7. **Branch retention** — reconcile the contradiction (`repo_resolution` Phase 4 shows
   `delete_branch_after_merge=True`; `subjob_worktree_sharing` shows `False`; World B deletes).
   The vision wants **retained**.
8. **Conflict detection granularity** — distinguish true merge conflicts from transient/HTTP
   errors (currently both collapse to `conflict`).
9. **Two delegation paradigms** — branch-based parallel subagents vs the file-handoff
   *sequential pipeline* (`[[feature_development_pipeline]]`, `[[dev_workflow]]`). Which does
   "branch + merge + delegation" refer to, and how do they relate?

## Goal / success criteria

A single, coherent subjob/delegation branch-and-merge model where:
- Every subjob kind (scholar, critic, delegation child, future nested subjobs) follows **one**
  branch + merge mechanism.
- Merges are **non-destructive** — a subjob can never delete or overwrite parent content it
  didn't legitimately produce.
- Each expert's **real deliverables** (not just `output/`) merge back; agent-control/scratch
  files never do; critic output stays off the deliverable branch.
- Subjob branches are **retained** with viewable per-phase history; the parent branch gets a
  single squashed commit per subjob.
- **Recursion** works to a configurable depth (lifecycle subjobs depth-transparent), with a
  cycle guard.
- **Conflicts** have a defined, observable behavior (even if v1 is "fail cleanly and surface").

## Approach & sequencing

Decomposed into three sequential specs, each building on the previous. **Detailed
implementation tasks live in each phase's implementation plan (design → plan), not in this
issue doc** — writing tasks before the design would be guesswork.

**Phase 1 — Safe, expert-aware merge model** *(next; the "proper solution" to the original issue).*
Make every subjob merge non-destructive (additive / restore-from-base), define a per-expert
deliverable boundary (not just `output/`), keep critic output off the deliverable branch, and
define conflict behavior. Fully closes the data-loss bug. Open decisions: deliverable boundary,
merge mechanic, where the merge runs, conflict handling. → own design spec.

**Phase 2 — Unify the two subjob worlds.**
Bring delegation (World B) onto the same orchestrator/Gitea branch+merge mechanism as
scholar/critic (consistent `subjob/...` branches, retained, server-side merge), fixing the
branch-name mismatch and the missing/un-granted merge tooling. Depends on Phase 1's merge
primitive. → own design spec.

**Phase 3 — Recursive subjobs to user-defined depth.**
Wire the depth model: add the missing `delegation-depth` endpoint, stop force-disabling child
delegation, set the depth policy (lifecycle links depth-transparent), add a cycle guard.
Depends on Phases 1–2. → own design spec.

## Stale / superseded — don't be misled

- **Curator is no longer a subjob** — knowledge curation runs inline via `AuxiliaryLLM`
  (`[[auxiliary]]`). `[[project_knowledge_base]]` / `[[dev_workflow]]` still describe it as a
  subjob; those sections are stale. There is **no knowledge-merge step**.
- **`research/` directory** — scholar writes to `output/ideas/` + `output/experiments/`, not
  `research/`. References to `research/` (incl. `config/defaults.yaml:~261` and earlier notes
  in [[subjob_merge_clobbers_parent_deliverables]]) are stale.
- **`delete_branch_after_merge` contradiction** — see open question #7.

## Related

### Design docs
- [[repo_resolution]] — the canonical "one repo per job, subjobs as branches, squash merge" decision; §Nesting Constraint is the hook for recursion.
- [[subagent_delegation]] — *Implemented*; answers all four user questions for the agent-initiated case; §Nesting "Option C" depth model.
- [[subjob_merge_clobbers_parent_deliverables]] — the clobber bug, landed fix, and the long-term additive/restore-from-base merge mechanic (**binding constraint**).
- [[subjob_worktree_sharing]] — subjobs share the parent VM via worktrees.
- [[verification_phase]] — critic-as-subjob lifecycle; recursive-verification guard.
- [[auxiliary]] — curator is now inline, not a subjob.
- [[git]] — per-todo commits, phase tags, GitManager.
- [[project_knowledge_base]] — Q17/Q26 persistent-subjob bidirectional branch sync.
- [[repository]], [[scholar_unblock_dispatch]] — operational failure modes (orphan branches / unrelated histories; `waiting` parent invisibility; `httpx.delete(json=)` bug).
- [[feature_development_pipeline]], [[dev_workflow]] — the alternate chain/pipeline delegation paradigm.

### Key code locations
- `orchestrator/main.py` — `_squash_merge_subjob` (~360-470), `SUBJOB_CLEANUP_*` (319-337), subjob branch creation (3886-3970), auto-merge gate (7548-7553), delegation completion (6677-6781), critic spawn (~7052-7275), scholar spawn (~6450-6600), dispatcher (2040-2378), cascade cancel/pause (4235-4308).
- `orchestrator/database/postgres.py` — `get_delegation_children`/`all_delegation_children_terminal` (1171-1235), `get_delegation_depth` (1237-1281, **dead code**), `get_dispatchable_jobs` cascade guard.
- `orchestrator/services/gitea.py` — `create_branch`, `create_pr`, `merge_pr` (~1176, squash-only).
- `src/tools/delegation/delegate_work.py` — the delegate tool, child config override (delegation disabled), broken depth check.
- `src/tools/git/git_tools.py` (243-326) + `src/managers/git_manager.py` (`merge_squash` ~1090) — the agent-side merge tools (granted to no expert).
- `src/core/workspace.py`, `src/agent.py` (control-file seeding) — workspace structure + plan/tools/instructions.
- `config/defaults.yaml`, `config/experts/*/config.yaml` — expert configs, `workspace.structure`, `delegation` block.
