---
tags:
  - architecture
  - git-integration
  - projects
  - decision
aliases:
  - repo resolution
  - repository model
  - job isolation
related:
  - "[[projects]]"
  - "[[project_knowledge_base]]"
  - "[[git]]"
  - "[[deliverables]]"
---

# Repository Resolution — One Repo Per Job, Subjobs as Branches

This document captures the architectural decision on how jobs relate to git repositories. It supersedes the shared "jobs repo" model described in [[projects]] and simplifies the repository assumptions in [[project_knowledge_base]].

## The Problem with Shared Repos

The original project model ([[projects]]) gave every project a single "jobs repo" where each job worked on a branch (`job/<short-id>/<slug>`) off `main`. Jobs would merge their results back to `main`, accumulating workspace files, deliverables, and knowledge across jobs.

In practice this creates several problems:

1. **Most job artifacts are noise on `main`.** `workspace.md`, `plan.md`, `todos.yaml`, `archive/` are all job-scoped working memory. They need to be gitignored on `main`, which means the merge flow is mostly about filtering things *out*.

2. **Branch coordination is complex.** Parallel jobs need conflict resolution. Subjobs (critic, curator) need sub-branches off the parent branch. The orchestrator has to manage branch lifecycles, merge ordering, and conflict handling — all git gymnastics for something that could be simpler.

3. **What actually merges to `main`?** Just `knowledge/`, `output/`, and `experts/`. Two of those three are better served by dedicated repos or database storage, and `output/` is often job-specific.

4. **External repos already handle code.** Agents push code to source repos. The jobs repo is really just a knowledge/workspace container with overhead.

5. **Working documents don't need git archiving.** MongoDB already logs every LLM call, tool invocation, and decision. Git history of `workspace.md` revisions is a redundant, worse version of what MongoDB already captures. The retrospectives and plans are intermediate artifacts — useful during the job, not after.

## The Decision

**One repository per root job. Subjobs are branches on the root job's repository. Squash merge on completion.**

### Terminology

- **`<short-id>`**: First 8 characters of the job's UUID (e.g., `ec38de5d`). Already used in the codebase as `job_id_str[:8]`.
- **`<type>`**: The subjob's `config_name` (e.g., `critic`, `curator`, `researcher`).
- **Root job**: A job with no `parent_job_id`. Always works on `main`.
- **Subjob**: A job with `parent_job_id` set. Works on a branch.

### How It Works

```
Root job created
  → Gitea repo created: job-<short-id>
  → Agent works on main branch
  → Commits freely (phase tags, todo completions, etc.)

Subjob spawned (critic, research, coding, testing, curator — any type)
  → Branch created off main: subjob/<short-id>/<type>
  → Subjob works independently on its branch
  → Commits freely (its own phases, todos, archives)

Subjob completes
  → Job-scoped files (workspace.md, plan.md, todos.yaml, archive/) cleaned from branch
  → Squash merge into main
  → Single commit: "Research on X completed" / "Review findings added"
  → Root agent sees the result as if it did the work itself
  → Subjob feedback sent to root agent via orchestrator

Root job completes
  → Repo contains clean history: root agent's work + squash-merged subjob results
  → Deliverables in output/
  → MongoDB has full audit trail
  → PostgreSQL has job inputs, config, metadata
```

### Why Squash Merge

The subjob's internal process (phases, retrospectives, todo churn) is noise from the parent's perspective. The parent cares about the *result*, not the journey. Squash merge gives exactly that — one commit with all the final files, none of the intermediate commits. From the root job's git history, it looks like the work just appeared, which is the right abstraction.

### Pre-Merge Cleanup

Subjobs create their own job-scoped working files (`workspace.md`, `plan.md`, `todos.yaml`, `archive/`) on their branch. These **must not** be squash-merged into `main` — they would overwrite the root job's files and corrupt its context (since `workspace.md` is read from disk on every LLM call).

Before creating the squash merge PR, the orchestrator deletes job-scoped files from the subjob branch via the Gitea API. This ensures only output files (`analysis/`, `knowledge/`, code, test results, etc.) reach `main`.

Files to clean before merge (same list as the current `PROJECT_JOB_IGNORE_PATTERNS`):
- `workspace.md`, `plan.md`, `todos.yaml`
- `archive/`
- `tools/`, `documents/`, `reference/`
- `instructions.md`, `task_brief.md`
- `output/job_frozen.json`, `output/job_completion.json`

This is a one-time cleanup commit on the branch, not a recurring gitignore concern. The orchestrator handles it automatically — the subjob agent doesn't need to know about it.

### Subjob Patterns

This model works uniformly for all subjob types:

| Subjob Type | What It Produces | After Squash Merge |
|-------------|-----------------|-------------------|
| **Critic** | Review findings in `analysis/`, test results | Root agent gets feedback + review artifacts |
| **Curator** | Knowledge notes in `knowledge/` | Root agent gets structured knowledge |
| **Research** | Research findings, source analysis | Root agent gets research output |
| **Coding** | Code changes, tests | Root agent gets implementation |
| **Testing** | Test results, coverage reports | Root agent gets test artifacts |

Whether sequential or parallel, the pattern is the same: branch, work, squash merge, send feedback.

### Nesting Constraint

Currently, subjobs cannot spawn their own subjobs — there is a guard in `src/api/app.py` that prevents recursive verification (`parent_job_id` must be null to trigger `create_verification_job`). This means the branch tree is always one level deep:

```
main (root)
  ├── subjob/abc/critic
  └── subjob/def/research
```

The branch model would support deeper nesting (a subjob's subjob branches off the subjob's branch), but this is not implemented and not planned for the initial rollout. If enabled later, the `from_branch` parameter in the orchestrator would need to resolve the parent's branch rather than always using `main`.

### Parallel Subjobs

Multiple subjobs can run concurrently on separate branches. Since they typically write to non-overlapping paths (critic writes `analysis/`, curator writes `knowledge/`, coding writes `src/`), merge conflicts are unlikely. If they do occur, the orchestrator handles resolution (or flags for the root agent).

```
main (root agent)
  ├── subjob/abc/research   (parallel)
  ├── subjob/def/coding     (parallel)
  └── subjob/ghi/testing    (parallel)
        ↓ squash merge (in any order, or wait for all)
main (root agent continues with merged results)
```

## What This Means for Projects

A project is a **database entity** that groups jobs. It does not own a shared repository.

### Cross-Job Sharing

Everything jobs need to share is handled outside the per-job repo:

| What's Shared | Where It Lives | How Jobs Access It |
|---------------|---------------|-------------------|
| **Job inputs** (instructions, kickoff message, parameters) | PostgreSQL (`jobs` table) | Stored on creation, available for replay/audit |
| **Resolved config** | PostgreSQL (`resolved_config` JSONB) | Already implemented — config snapshot at job start |
| **Audit trail** (full conversation, tool calls, decisions) | MongoDB | Already implemented — complete LLM request logging |
| **Knowledge** (decisions, learnings, patterns) | Knowledge repo or database (TBD, see [[project_knowledge_base]]) | Curator extracts, next job queries |
| **Expert configs** | Project-level config directory or database | Shared by project_id |
| **Datasources** | PostgreSQL (`datasources` table) | Attached at project level, inherited by jobs |
| **External code** | External repos (GitHub, Gitea) | Linked via job instructions or datasource config |

### What Changes from [[projects]]

| Before (shared jobs repo) | After (per-job repo) |
|--------------------------|---------------------|
| One jobs repo per project, branches per job | One repo per root job, branches per subjob |
| Jobs inherit `main` state (workspace.md, archives) | Jobs start fresh, get context from database |
| Merge flow filters job artifacts from `main` | No filtering needed — repo is self-contained |
| Branch coordination between parallel jobs | No coordination — jobs are isolated |
| Project-level gitignore gymnastics | Not needed |

### What Stays the Same

- **Git versioning within a job** — phase tags, todo completion commits, `git_log`/`git_diff`/`git_show` tools all work exactly as before. The agent still has full git history of its own work.
- **Subjob infrastructure** — `create_verification_job()`, `resume_job(feedback)`, `waiting` status — all unchanged. Only the repo/branch model changes.
- **Critic flow** — Still runs post-completion, still reviews deliverables, still sends feedback. Just operates on a branch instead of a separate repo.
- **Project datasources** — Still attached at project level, inherited by jobs.
- **External repo access** — Agents can still clone, branch, and PR to external repos.

## Archiving and Auditability

The concern with per-job repos is losing the ability to reconstruct or audit past jobs. This is solved by storing inputs in the database:

**Already stored:**
- `resolved_config` JSONB — full config snapshot (implemented)
- MongoDB audit trail — every LLM request, tool call, response (implemented)
- Job metadata — status, timestamps, agent assignment (implemented)
- Job description — `jobs.description` column (implemented)
- Job instructions — stored in `jobs.context` JSONB as `instructions` key (implemented)
- Kickoff message — stored in `jobs.context` JSONB as `kickoff_message` key (implemented)
- Document path — `jobs.document_path` column (implemented)
- Freeze data — `jobs.freeze_data` JSONB with summary, deliverables, confidence (implemented)

**Remaining gap:**
- Output references — links to final deliverables beyond what `freeze_data.deliverables` captures (e.g., external repo PRs, uploaded artifacts). Low priority since `freeze_data` covers the common case.

With this in place, any job can already be substantially reconstructed from database records alone. The per-job Gitea repo is the working surface, not the archive.

## Open Questions

1. **Repo lifecycle** — When does the per-job Gitea repo get cleaned up? Options: keep indefinitely, delete after N days, delete after deliverables are extracted, archive to cold storage. Probably configurable per project. Deferred — not blocking implementation.

2. **Knowledge repo** — Does the knowledge base live in a dedicated project-level repo (curator PRs to it) or purely in the database? See [[project_knowledge_base]] for the full design. Deferred — independent of the per-job repo model.

## Resolved Questions

3. **Parallel merge conflicts** — If a squash merge conflicts, the merge fails and the root agent sees the conflict on its next pull. System-spawned subjobs (e.g., critic) are sequential by design so conflicts shouldn't arise. User-initiated parallel subjobs may conflict if they touch overlapping paths, but this is an edge case to handle when it occurs rather than over-engineer upfront.

4. **Subjob output directory convention** — No hard enforcement. The root agent that spawns subjobs controls the kickoff message and can instruct them to write output to specific directories (e.g., "store your findings in `research/`"). This is a soft convention driven by the spawning agent's instructions, not a system-level constraint. If this proves insufficient in practice, directory enforcement can be added later.

## Implementation Roadmap

This section maps the decision above to concrete codebase changes. Each phase builds on the previous one — phases 1–4 are the critical path, the rest is incremental cleanup.

### Phase 1: Schema Migration

**Goal:** Prepare the database for per-job repos.

| File | Change |
|------|--------|
| `orchestrator/database/schema.sql` | Add `repo_name VARCHAR(200)` column to `jobs` table (stores `job-<short-id>`) |
| `orchestrator/database/schema.sql` | Deprecate `uq_project_jobs_repo` unique index — projects no longer require a `jobs`-role repo |
| `orchestrator/database/schema.sql` | Consider removing `'jobs'` from `valid_repo_role` constraint on `project_repositories` (keep `source`, `reference`) |
| `orchestrator/database/postgres.py` | Update `create_job()` to accept and store `repo_name` |

Branch naming convention changes from `job/<short-id>` to `subjob/<short-id>/<type>` for subjobs.

### Phase 2: Orchestrator Job Creation (Core Change)

**Goal:** Replace the 3-way repo/branch logic with per-job repos.

**File:** `orchestrator/main.py` — the Gitea repo/branch block inside `create_job()`

Current logic has three branches based on `project_id` and `parent_job_id`:

| Condition | Current | New |
|-----------|---------|-----|
| `project_id + parent_job_id` | Branch from parent's branch on **project** jobs repo | Branch `subjob/<short-id>/<type>` on **parent's per-job** repo |
| `project_id` (root job) | Branch on project's jobs repo | Create standalone repo `job-<short-id>` (same as non-project) |
| No project | Create repo `job-<uuid>` | Unchanged (shorten to `job-<short-id>`) |

New logic collapses to two cases:
- **Root job** (no `parent_job_id`): Always `gitea_client.create_repo(f"job-{short_id}")`. Store clone URL in context and `repo_name` in jobs table. Project membership is irrelevant for repo creation.
- **Subjob** (`parent_job_id` set): Look up parent's `repo_name` / `git_remote_url`. Call `gitea_client.create_branch(parent_repo, f"subjob/{short_id}/{config_name}", from_branch)` where `from_branch` is the parent's `branch_name` (or `main` if parent is the root job).

### Phase 3: Resolve Helper & Downstream Consumers

**Goal:** Fix repo resolution everywhere it's used.

**File:** `orchestrator/main.py` — `resolve_job_repo()` helper function

Current: looks up project's jobs repo from `project_repositories` table. New: derive from job's `repo_name` column. For subjobs, traverse `parent_job_id` chain to find root job's `repo_name`.

All downstream consumers automatically fixed:
- Freeze/approve flow (`approve_job()`, `get_freeze_data()`)
- Workspace browser proxy (`/api/jobs/{id}/workspace/`)
- Job status/resume endpoints

### Phase 4: Squash Merge on Subjob Completion

**Goal:** Automatically merge subjob results into the root job's main branch.

**This is new code.** Currently no merge-on-completion exists.

Trigger point: when a subjob's status transitions to `completed` (in the orchestrator's job update path, or after `_handle_critic_verdict()` in `src/api/app.py`).

Flow:
```
subjob completes
  → orchestrator detects status change + parent_job_id is set
  → pre-merge cleanup: delete job-scoped files from subjob branch via Gitea API
      (workspace.md, plan.md, todos.yaml, archive/, tools/, documents/,
       instructions.md, task_brief.md, output/job_frozen.json)
  → gitea_client.create_pr(parent_repo, title, head=subjob_branch, base=parent_branch)
  → gitea_client.merge_pr(parent_repo, pr_number, merge_strategy="squash", delete_branch_after_merge=True)
  → resume parent job with feedback (existing flow)
```

The pre-merge cleanup is critical — without it, the subjob's `workspace.md` would overwrite the root job's long-term memory file, corrupting its context on resume. See "Pre-Merge Cleanup" section above.

`GiteaClient` already has `create_pr()`, `merge_pr()`, and `delete_file()` — no client changes needed.

### Phase 5: Job Deletion & Cleanup

**Goal:** Clean up repos/branches when jobs are deleted.

**File:** `orchestrator/main.py` — `delete_job()` endpoint

| Job type | Current | New |
|----------|---------|-----|
| Root job | Deletes branch on project jobs repo | `gitea_client.delete_repo(repo_name)` — also deletes any remaining subjob branches |
| Subjob | Deletes branch on project jobs repo | `gitea_client.delete_branch(root_repo, subjob_branch)` (no-op if already merged and deleted) |

### Phase 6: Workspace Initialization Cleanup

**Goal:** Remove shared-repo workspace logic.

**File:** `src/core/workspace.py`

| Code | Change |
|------|--------|
| `PROJECT_JOB_IGNORE_PATTERNS` | **Remove** — existed to prevent job artifacts leaking to shared `main`. Pre-merge cleanup (Phase 4) handles this now. |
| `_setup_project_gitignore()` | **Remove** — same reason |
| `initialize_project_workspace()` | **Simplify** — root jobs fall through to `initialize()`. Only subjobs clone parent's repo + checkout branch. |
| `_clone_auxiliary_repos()` | **Keep** — source/reference repos still useful |
| `WorkspaceManagerConfig.repositories` | `jobs` role becomes irrelevant; `source`/`reference` stay |

**File:** `src/agent.py` — `_initialize_workspace()` method
- `branch_name` only set for subjobs now (root jobs work on `main`)
- Pod handoff logic (clone from Gitea on resume) unchanged

### Phase 7: Remove Legacy Merge Endpoints

**Goal:** Remove endpoints that served the shared-repo model.

**File:** `orchestrator/main.py`

| Endpoint | Action |
|----------|--------|
| `POST /api/projects/{project_id}/jobs/{job_id}/merge` | **Remove** — manual merge to project main is obsolete; squash merge is automatic (Phase 4) |
| `POST /api/projects/{project_id}/jobs/{job_id}/skip-merge` | **Remove** — no merge to skip |
| `jobs.merge_status` / `jobs.repo_merge_statuses` columns | Repurpose for squash merge tracking (subjob → root merge status) or remove |

### Phase 8: Cockpit Frontend

**Goal:** Update UI to reflect per-job repo model.

| Area | Change |
|------|--------|
| Project detail view | Remove "jobs repo" display/management |
| Project repository management | Keep source/reference repo UI, remove jobs role |
| Job detail workspace browser | Updated automatically via resolver fix (Phase 3) |
| Merge/skip-merge buttons | Remove from project job views |

### Phase 9: Documentation

**Goal:** Update related design docs to reference the new model.

| Document | Change |
|----------|--------|
| `docs/features/projects.md` | Update shared jobs repo references; mark superseded sections |
| `docs/features/project_knowledge_base.md` | Update assumption that `knowledge/` lives in a shared jobs repo |
| `docs/git.md` | Add subjob branch/merge documentation |

### What Doesn't Change

These components work as-is with the new model:

- **GitManager** (`src/managers/git_manager.py`) — already has all needed methods
- **GiteaClient** (`orchestrator/services/gitea.py`) — already has create_repo, create_branch, merge_pr, delete_repo
- **Git tools** (`src/tools/git/git_tools.py`) — agent-facing read tools unchanged
- **Phase transitions** (`src/core/phase.py`) — tags, commits, pushes unchanged
- **Todo/archive system** — unchanged
- **Critic flow** — `create_verification_job()`, `resume_job(feedback)`, `waiting` status all unchanged; only the repo/branch wiring changes

## Related Documents

- [[projects]] — Project infrastructure (database schema, API, cockpit UI). The shared jobs repo model described there is superseded by this document.
- [[project_knowledge_base]] — Knowledge base design. The curator and knowledge note concepts are compatible with this model; the assumption that `knowledge/` lives in a shared jobs repo needs updating.
- [[git]] — Git versioning for agent workspaces. Per-job git tools and phase tracking are unchanged.
- [[deliverables]] — Output/deliverable management. Per-job repos simplify deliverable handling (no merge-to-main step).
