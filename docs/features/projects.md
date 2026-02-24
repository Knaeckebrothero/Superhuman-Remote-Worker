---
tags:
  - feature
  - architecture
  - projects
  - multi-job
aliases:
  - projects
  - campaigns
  - multi-job workflows
related:
  - "[[datasources]]"
  - "[[memory_light]]"
  - "[[deployment]]"
  - "[[debug_cockpit]]"
---

# Projects — Multi-Job Resource Hub

Jobs today are isolated. Each gets its own workspace, its own Gitea repo, its own datasources. There's no way for job B to build on job A's output, run multiple jobs in parallel against a shared codebase, or chain a sequence of tasks into a larger effort.

**Projects** fix this. A project is a **resource hub** — a grouping entity that ties together jobs, repositories, datasources, and (eventually) wikis, backlogs, and other shared resources. Jobs within a project share these resources, building on each other's work.

**Comparable systems:** The resource-hub model aligns with how GitHub Projects V2 (cross-repo planning layer over issues/PRs), Linear (cross-team project containers), and Azure DevOps (one project, many teams/repos) separate planning from code. In the AI agent space, Devin uses Knowledge + Playbooks + machine snapshots as project-level context across sessions; Factory.ai uses AGENTS.md + integration context (Jira, Slack) per repo. No existing platform handles multi-repo AI agent orchestration with shared state — agents either work within a single repo (Devin, Cursor, Factory) or require external tooling for cross-repo coordination. Our architecture fills this gap by making the project the explicit coordination layer between repos, databases, and agent configs.

## Core Model

A **project** is:

1. A database entity grouping related jobs
2. A collection of **repositories** — one "jobs repo" for workspace continuity, plus any number of source/reference repos the agents work with
3. A set of shared **datasources** available to all jobs in the project
4. A set of **project-specific expert configs** — custom agent roles tailored to the project
5. A set of **members** — users who can access and contribute to the project
6. Optionally, a description and goal that frames the entire effort
7. Eventually: a wiki, a backlog/kanban board, cross-job memory

The project is not a repository — it's the hub that *connects* repositories, databases, and jobs. Think of it like a GitHub organization or a Jira project: the container that holds everything together.

### Users and Default Projects

Every user has a **default project**. It's created automatically when the user account is created and acts as a personal workspace — all jobs created without specifying a project land here. This means there are no "projectless" jobs; every job belongs to a project.

The default project is a full project entity: it has a jobs repo, can have datasources attached, can hold project-specific experts. The only thing special about it is that it's the fallback when no project is specified. Users can rename it, configure it, even share it — it's not a second-class citizen.

This has a nice side effect: a user's default project accumulates all their ad-hoc work in a single jobs repo. The workspace.md, archives, and expert configs from previous jobs are available to future jobs, even if those jobs were created independently. The default project becomes a personal knowledge base over time.

### Shared Projects

Projects are shareable between users. A project has **members**, each with a role:

| Role | Permissions |
|------|-------------|
| `owner` | Full control. Edit project settings, manage members, delete project. One owner minimum. |
| `editor` | Create jobs, manage repos/datasources/experts, merge branches. Cannot delete the project or manage members. |
| `viewer` | Browse the project, read repos and job outputs. Cannot create jobs or modify anything. |

The project creator is automatically the `owner`. Other users are invited by adding them as members with a role. A user can be a member of many projects and own multiple projects.

**Visibility model**: Projects are private by default — only members can see them. The project list in the cockpit shows projects the current user is a member of (any role). Job creation in a project context is restricted to `owner` and `editor` roles.

### Project Repositories

A project can have multiple repositories, each with a **role**:

| Role | Description | Created by | Agent access |
|------|-------------|-----------|--------------|
| `jobs` | Workspace continuity repo. Holds workspace.md, plan.md, todos, archives, outputs. Job branches live here. Every project has exactly one. | System (on project creation) | Read/write, branch-per-job |
| `source` | Code or content the agents work on. Frontend repos, backend repos, libraries, document collections. | User (attach existing or create new) | Read/write or read-only per config |
| `reference` | Context-only repos. Style guides, specs, existing codebases to learn from. Never modified by agents. | User (attach existing) | Read-only |

**Example — a full-stack SWE project:**
- Jobs repo (managed): `project-a1b2c3` — workspace files, phase archives, job branches
- Source repo: `my-app-backend` — the actual backend codebase agents develop against
- Source repo: `my-app-frontend` — the frontend codebase
- Reference repo: `design-system` — read-only design tokens and component specs

**Example — a book-writing project:**
- Jobs repo (managed): `project-d4e5f6` — outlines, drafts, research notes, chapter outputs
- Reference repo: `style-guide` — publisher's formatting requirements
- No source repos needed — the jobs repo *is* the content

**Example — this project (SRW itself):**
- Jobs repo (managed): agent workspace continuity
- Source repo: `Superhuman-Remote-Worker` — the main codebase
- Source repo: `CitationEngine` — the citation library (separate repo, pip-installed)

### Jobs Within a Project

A **job within a project** is:

- The same `jobs` entity as today, with an additional `project_id` FK
- Works on a **branch** of the jobs repo (not its own throwaway repo)
- Has access to all project repositories cloned into its workspace
- Pushes results back to the jobs repo on completion
- Inherits project-level datasources automatically

There are no "projectless" jobs. If a job is created without specifying a project, it goes into the user's default project. This means every job benefits from project infrastructure (shared jobs repo, project-level datasources, project experts) even if the user never explicitly creates a project.

### Project-Specific Experts

Global experts (`config/experts/developer/`, `config/experts/scholar/`, etc.) are generic roles that work for any job. But a project often needs **specialized agents** — a "backend developer" that knows the project's tech stack and conventions, a "domain expert" with project-specific instructions, a "reviewer" tuned to the project's quality standards.

Project-specific experts live in the **jobs repo** under `experts/`:

```
project jobs repo (main branch):
├── experts/
│   ├── backend-dev/
│   │   ├── config.yaml        ← $extends: developer (inherits from global expert)
│   │   ├── persona.txt        ← Project-specific persona overlay
│   │   └── instructions.md    ← "Always use FastAPI, follow our API conventions..."
│   ├── frontend-dev/
│   │   ├── config.yaml        ← $extends: developer
│   │   └── instructions.md    ← "Use Angular 19, follow component patterns in..."
│   └── domain-expert/
│       ├── config.yaml        ← $extends: scholar
│       ├── persona.txt
│       └── instructions.md    ← Project-domain knowledge
├── workspace.md
├── plan.md
└── ...
```

**How it works:**

1. Expert configs in the jobs repo use the same format as global experts — `config.yaml` with `$extends`, prompt/instruction files resolved via the matrix system.
2. `$extends` can reference a global expert (`$extends: developer`) or `defaults`. This way project experts inherit tools, LLM config, and phase behavior from proven base configs and only override what's project-specific.
3. **Resolution order**: When creating a job in a project, the config selector shows project experts first, then global experts. Project experts are read from the jobs repo's `main` branch via Gitea API.
4. **Versioned with the project**: Since experts live in the jobs repo, they evolve with the project. Early jobs might use a generic developer; later jobs use a refined project-specific expert that accumulated knowledge in its instructions. Changes to expert configs are tracked in git history.
5. Project experts are **not** copied into the global `config/experts/` directory. They're resolved at job creation time from the repo and stored in the job's `resolved_config` JSONB (same as today — the resolved config snapshot prevents drift).

**Example — project expert that extends the global developer:**

```yaml
# experts/backend-dev/config.yaml (in the jobs repo)
# yaml-language-server: $schema=../../schema.json
$extends: developer

agent_id: backend-dev
display_name: Backend Developer
description: |
  Backend specialist for the MyApp project. Knows our FastAPI conventions,
  database schema, and deployment pipeline. Delegates to Claude Code for
  implementation, reviews against project standards.
icon: server
color: "#a6e3a1"
tags:
  - backend
  - fastapi
  - project-specific

# Override LLM if needed
# llm:
#   model: claude-sonnet-4-6

# Add project-specific tools (e.g., SQL tools for the project DB)
tools:
  sql:
    - sql_query
    - sql_schema
    - sql_execute
```

The `instructions.md` alongside it would contain project-specific guidance — API patterns, naming conventions, architectural decisions, things that a generic developer agent wouldn't know.

## Job Patterns Within a Project

The branch-per-job model doesn't need a `mode` setting — it inherently supports multiple usage patterns. There's no `mode` column on the `projects` table; the user simply creates jobs however they want.

### Sequential

Create one job at a time. Merge into `main` when done. Start the next job from updated `main`.

```
main: ─────●──────────●──────────●──────────●───
            \        /            \        /
job-1:       ──●──●─              |        |
                                   \      /
job-2:                              ──●──●─
```

Use case: Writing a book (outline → worldbuilding → chapter 1 → chapter 2 → editing). Each job builds on the accumulated result.

### Parallel

Create multiple jobs simultaneously. Each gets its own branch. Merge when ready — conflicts resolved via agent-assisted merge (see "Merge Conflict Resolution").

```
main: ─────●───────────────────●───
            \                 /|
job-A:       ──●──●──●───────  |
            \                  |
job-B:       ──●──●──●────────
```

Use case: Research project where one job gathers data while another builds the analysis framework. Or multiple agents implementing different features of the same codebase.

### Persistent Agent (Future — Phase 5)

A convenience layer on top of sequential where the system auto-creates the next job when the previous one completes. The user interacts via the builder chat — each message becomes a job. See Phase 5 for details.

```
main: ─────●────●────●────●────●────●───
            \  / \  / \  / \  / \  /
job-1:       ●    |    |    |    |
job-2:            ●    |    |    |
job-3:                 ●    |    |
job-4:                      ●    |
job-5:                           ●
```

This pattern sidesteps the context window problem — each job starts fresh with a clean context but has the full project history in the git repo.

## Database Schema

### Modified Table: `users`

```sql
-- Add default project reference to existing users table
ALTER TABLE users ADD COLUMN default_project_id UUID REFERENCES projects(id);
```

When a user is created, the system auto-creates a default project and sets `default_project_id`. This is a soft reference (not NOT NULL) to avoid circular dependency during initial creation — the user is created first, then the project, then the user is updated.

### New Table: `projects`

```sql
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    description TEXT,                         -- What this project is about
    goal TEXT,                                -- Success criteria / definition of done
    status VARCHAR(50) DEFAULT 'active',      -- active, paused, completed, archived
    is_default BOOLEAN NOT NULL DEFAULT FALSE,-- true = this is a user's default project
    default_config_name VARCHAR(100),         -- Default agent config for new jobs
    default_config_override JSONB,            -- Default config overrides for new jobs
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_projects_status ON projects(status);
```

Note: No `repo_url` on the project itself — repositories are linked via the `project_repositories` table. No `user_id` on the project — ownership is expressed through the `project_members` table. The `is_default` flag marks default projects so the UI can treat them appropriately (e.g. prevent deletion, show as "Personal Workspace").

### New Table: `project_members`

```sql
CREATE TABLE IF NOT EXISTS project_members (
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role VARCHAR(50) NOT NULL DEFAULT 'editor',  -- 'owner', 'editor', 'viewer'
    added_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project_id, user_id)
);

CREATE INDEX idx_project_members_user ON project_members(user_id);
```

- Composite primary key ensures a user appears at most once per project.
- `ON DELETE CASCADE` on both FKs: deleting a project removes all memberships, deleting a user removes them from all projects.
- The project creator is inserted as `owner` on project creation.

### New Table: `project_repositories`

```sql
CREATE TABLE IF NOT EXISTS project_repositories (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,                       -- Human-readable label (e.g. "Backend", "CitationEngine")
    description TEXT,                         -- What this repo contains (included in agent context)
    repo_url TEXT NOT NULL,                   -- Git clone URL (Gitea internal or external)
    credentials JSONB DEFAULT '{}',          -- Auth for external repos (token, ssh_key, username/password)
    role VARCHAR(50) NOT NULL DEFAULT 'source',  -- 'jobs', 'source', 'reference'
    read_only BOOLEAN NOT NULL DEFAULT FALSE, -- Whether agents can push to this repo
    is_managed BOOLEAN NOT NULL DEFAULT FALSE,-- true = created by us (Gitea), false = external
    branch TEXT DEFAULT 'main',              -- Default branch to clone from
    clone_path TEXT,                          -- Subdirectory name in workspace (default: repo name)
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,

    -- Reference repos must always be read-only
    CONSTRAINT chk_reference_read_only CHECK (role != 'reference' OR read_only = TRUE)
);

CREATE INDEX idx_project_repos_project ON project_repositories(project_id);

-- Exactly one jobs repo per project
CREATE UNIQUE INDEX uq_project_jobs_repo ON project_repositories(project_id) WHERE role = 'jobs';
```

- `role = 'jobs'` — Enforced unique per project. Created automatically on project creation. Holds workspace files and job branches.
- `role = 'source'` — Code or content the agent works on. Can be read/write or read-only.
- `role = 'reference'` — Always read-only (enforced by CHECK constraint). Context for the agent, never modified.
- `credentials` — Auth for external (non-managed) repos. Follows the same pattern as `datasources.credentials`. Supports `{"token": "..."}` for HTTPS token auth, `{"username": "...", "password": "..."}` for basic auth, or `{"ssh_key": "..."}` for SSH. Managed repos use the Gitea admin credentials automatically. Stored in plain text for now (same caveat as datasources — encryption at rest added later with auth system).
- `is_managed` — `true` for Gitea repos we create, `false` for external repos the user links.
- `clone_path` — Where this repo appears in the workspace. Defaults to the repo name slug. The jobs repo is cloned as the workspace root; source/reference repos go into `repos/<clone_path>/`.
- `branch` — Which branch to clone from. For external repos this might be `main`, `develop`, a release tag, etc.

### Modified Table: `jobs`

```sql
-- Add to existing jobs table
ALTER TABLE jobs ADD COLUMN project_id UUID NOT NULL REFERENCES projects(id);
ALTER TABLE jobs ADD COLUMN branch_name VARCHAR(200);      -- Git branch for this job (on the jobs repo)
ALTER TABLE jobs ADD COLUMN merge_status VARCHAR(50);       -- pending, merged, conflict, skipped (jobs repo)
ALTER TABLE jobs ADD COLUMN repo_merge_statuses JSONB DEFAULT '{}';  -- Per-source-repo merge status

CREATE INDEX idx_jobs_project ON jobs(project_id);
```

- `project_id` — **NOT NULL**. Every job belongs to a project. If not specified at creation, the orchestrator assigns the user's default project.
- `branch_name` — The git branch this job works on in the jobs repo (e.g. `job/<short-id>/<slug>`)
- `merge_status` — Tracks whether the job's jobs-repo branch has been merged into `main`
- `repo_merge_statuses` — Per-source-repo merge status. Keys are `project_repositories.id`, values are merge status strings. Example: `{"uuid-1": "merged", "uuid-2": "conflict"}`

**Migration for existing jobs:** Add the column as nullable first, create default projects for all existing users, assign each existing job to its creator's default project, then set NOT NULL.

### Project-Level Datasources

Datasources already support `job_id = NULL` (global). For projects, we add a `project_id` column:

```sql
ALTER TABLE datasources ADD COLUMN project_id UUID REFERENCES projects(id);
```

Resolution order becomes: **job-specific → project-level → global**. A project can have its own Neo4j graph, PostgreSQL database, etc., shared across all jobs in the project without duplicating the datasource config per job.

The existing unique index on datasources (`uq_datasource_type_job`) needs updating to account for the three-level scope:

```sql
-- Drop the old index and create scope-aware uniqueness
-- One datasource of each type per scope (job, project, or global)
DROP INDEX IF EXISTS uq_datasource_type_job;
CREATE UNIQUE INDEX uq_datasource_type_scope ON datasources (
    type,
    COALESCE(job_id, '00000000-0000-0000-0000-000000000000'),
    COALESCE(project_id, '00000000-0000-0000-0000-000000000000')
);
```

## Workspace Layout

### Current Layout (Pre-Projects)

For reference, this is how jobs work today — each with an independent throwaway repo:

```
workspace/job_<uuid>/
├── .git/                  ← fresh git init, pushes to job-<uuid> Gitea repo
├── workspace.md
├── plan.md
├── todos.yaml
├── archive/
├── output/
├── documents/
└── tools/
```

### Project Layout (All Jobs)

```
workspace/job_<uuid>/
├── .git/                  ← cloned from jobs repo, on branch job/<short-id>/<slug>
├── workspace.md           ← from jobs repo main (accumulated project state)
├── plan.md
├── todos.yaml
├── archive/
├── output/
├── documents/
├── tools/
└── repos/                 ← project source + reference repos (gitignored by jobs repo)
    ├── my-app-backend/    ← source repo (read/write), own .git
    ├── my-app-frontend/   ← source repo (read/write), own .git
    └── design-system/     ← reference repo (read-only), own .git
```

Key points:
- The workspace root **is** the jobs repo (cloned, on a job branch). workspace.md, plan.md, todos — all tracked here, same as today.
- `repos/` is gitignored by the jobs repo. Each subdirectory is its own independent git clone.
- Source repos can be pushed back on job completion (if not read-only). The agent creates branches in source repos the same way it does in the jobs repo.
- Reference repos are cloned at the specified branch/tag and never modified.
- The agent's existing `read_file`, `write_file`, `list_directory` tools work across all repos transparently — they're just directories on the filesystem.
- Git tools (`git_log`, `git_diff`, `git_show`) operate on the jobs repo by default but can target a specific repo via a `repo` parameter.

### Initialization Flow

1. `WorkspaceManager.initialize()` creates `workspace/job_<uuid>/` as before
2. **Jobs repo**: Clone from Gitea, checkout job branch: `git clone <jobs_repo_url> . && git checkout -b job/<short-id>/<slug>`
3. **Source/reference repos**: For each `project_repositories` entry with `role != 'jobs'`, clone into `repos/<clone_path>/` at the configured branch
4. Source repos: create a job branch (same naming as jobs repo) for isolation
5. Agent works normally — doesn't know or care about the multi-repo setup
6. On job completion: push job branch on jobs repo + any modified source repos

**Performance note:** For large source/reference repos, use `git clone --depth 1` (shallow clone) to minimize clone time and disk usage. The agent rarely needs full history for source repos — it needs the current state of the code. If the agent needs history (e.g., `git log`, `git blame`), the git tools can `git fetch --unshallow` on demand. Jobs repos are always full clones since the phase archive history is valuable context.

### Branch Naming

Format: `job/<short-id>/<description-slug>`

Examples:
- `job/a1b2c3/outline-story-structure`
- `job/d4e5f6/chapter-1-the-beginning`
- `job/g7h8i9/code-review-and-refactor`

The short ID prevents collisions. The slug is derived from the job description. Same branch name is used across all repos (jobs repo + source repos) so it's easy to correlate changes.

## Merge Strategy

Gitea has no direct branch merge API — all merges go through the **Pull Request** workflow. This is actually better than raw git merge: we get conflict detection, review UI, CI hook points, and audit trail for free.

When a project job completes, the orchestrator merges across all repos that were modified:

1. **Jobs repo merge**: Always attempted. The orchestrator creates a PR from the job branch to `main`, then merges it via `POST /repos/{owner}/{repo}/pulls/{index}/merge` with strategy `"merge"` (equivalent to `--no-ff`). Gitea supports six merge strategies (`merge`, `rebase`, `rebase-merge`, `squash`, `fast-forward-only`, `manually-merged`) — the project can configure a default, but `merge` (no-ff) is the safe default for preserving branch history.
2. **Source repo merges**: For each source repo where the agent pushed changes, the same PR+merge flow is executed. Each repo's merge status is tracked independently.
3. **Reference repos**: Never merged (read-only, no changes to push).

Merge outcomes per repo:

| Outcome | `merge_status` | What happens |
|---------|----------------|-------------|
| Clean merge | `merged` | PR merged into `main`, next job starts from updated state |
| Conflict | `conflict` | PR stays open. User resolves via Gitea's merge UI, cockpit, or follow-up job |
| No changes | `skipped` | Branch existed but had no commits beyond the base |
| User choice | `skipped` | User explicitly skips merge (exploratory/research jobs) |

The `merge_status` on the `jobs` table reflects the **jobs repo** merge. Source repo merge statuses are stored as JSONB on the job:

```sql
ALTER TABLE jobs ADD COLUMN repo_merge_statuses JSONB DEFAULT '{}';
-- Example value: {"repo-id-1": "merged", "repo-id-2": "conflict"}
```

The `delete_branch_after_merge` option on Gitea's merge API can be used to clean up job branches after successful merge, keeping the branch list tidy.

## Promote to Dedicated Project

Any completed job in the default project can be promoted to its own dedicated project:

1. Create a new `projects` entry
2. Move the job from the default project to the new project (`project_id` update)
3. The job's branch content from the default project's jobs repo becomes the seed for the new project's jobs repo — create the new Gitea repo, push the job's content as the initial `main`
4. Create a `project_repositories` entry with `role = 'jobs'` for the new repo
5. User can then attach additional source repos, datasources, and invite members

This is the "make this job into a project" workflow — the user ran a one-off task in their default project, realizes it needs follow-up work, and promotes it to a dedicated project without losing the existing output.

## Orchestrator API

### Project CRUD

```
POST   /api/projects                         — Create project (creates jobs repo, adds creator as owner)
GET    /api/projects                         — List projects for current user (all memberships)
GET    /api/projects/{id}                    — Get project details (includes repos, datasources, members, job count)
PATCH  /api/projects/{id}                    — Update project (name, description, status, defaults)
DELETE /api/projects/{id}                    — Delete project (owner only, cannot delete default projects)
```

### Project Members

```
GET    /api/projects/{id}/members            — List project members with roles
POST   /api/projects/{id}/members            — Add member (owner only, body: {user_id, role})
PATCH  /api/projects/{id}/members/{uid}      — Change member role (owner only)
DELETE /api/projects/{id}/members/{uid}       — Remove member (owner only, cannot remove last owner)
```

### Project Repositories

```
POST   /api/projects/{id}/repositories       — Attach repo to project (create managed or link external)
GET    /api/projects/{id}/repositories       — List repos in project
PATCH  /api/projects/{id}/repositories/{rid} — Update repo (name, description, read_only, branch)
DELETE /api/projects/{id}/repositories/{rid} — Detach repo (delete managed, unlink external)
```

### Project Experts

```
GET    /api/projects/{id}/experts            — List project experts (reads experts/ from jobs repo)
GET    /api/projects/{id}/experts/{name}     — Get expert config details
```

Project experts are stored in the jobs repo (not in a database table), so these are read-only endpoints that browse the repo. Creating/editing project experts is done via the cockpit's file editor (writing to the jobs repo) or by a job that modifies the `experts/` directory.

### Jobs Within Projects

```
POST   /api/projects/{id}/jobs               — Create job in project context
GET    /api/projects/{id}/jobs               — List jobs in project
POST   /api/projects/{id}/jobs/{job_id}/merge — Merge job branch into main (across all repos)
POST   /api/jobs/{id}/promote                — Promote default-project job to dedicated project
```

### Project Repo Browsing

Reuse the existing git content endpoints pattern but scoped to project repos:

```
GET    /api/projects/{id}/git/tree           — Browse jobs repo (main branch)
GET    /api/projects/{id}/git/file           — Read file from jobs repo
GET    /api/projects/{id}/git/log            — Commit history across all jobs
GET    /api/projects/{id}/git/branches       — List branches (one per job)
GET    /api/projects/{id}/repos/{rid}/tree   — Browse a specific project repo
GET    /api/projects/{id}/repos/{rid}/file   — Read file from a specific repo
```

## Cockpit UI

### Project List View

A new top-level page showing all projects the current user is a member of. Each card shows:
- Project name and description
- Role badge for the current user (owner / editor / viewer)
- Status badge (active / paused / completed)
- "Default" badge if it's the user's default project
- Resource counts (repos, datasources, jobs)
- Latest activity timestamp

The user's default project appears first (pinned). The existing "Jobs" list view becomes a flat view across all projects (or scoped to the currently selected project).

### Project Detail View

The project hub page. Tabs or sections for:

**Overview**: Name, description, goal, status, creation date. Quick stats (total jobs, active jobs, repos, datasources, members).

**Jobs**: Timeline or list of jobs. Each shows status, branch, merge status, creator. "Create New Job" button (editor/owner only). Filter by status.

**Repositories**: List of attached repos with role badges (jobs/source/reference), read-only indicators, branch info. "Add Repository" button (editor/owner only). Browse files in any repo.

**Experts**: List of project-specific experts (read from `experts/` in the jobs repo). Each shows name, description, which global expert it extends. "Create Expert" opens a guided form or file editor (editor/owner only). Experts are versioned with the project — the UI reads from the jobs repo's `main` branch.

**Datasources**: Project-level datasources. Same management UI as the global datasource panel but scoped to this project. Editor/owner only for modifications.

**Members**: List of project members with role badges. "Add Member" button (owner only). Role change dropdown (owner only). Remove button (owner only, cannot remove last owner).

**Settings**: Name, description, goal, default config, default config overrides. Danger zone — archive/delete project (owner only, cannot delete default projects).

### Job Creation Flow

The job creation form now always has a **project context**:

- **Project selector** at the top: defaults to the user's default project, can switch to any project the user has editor/owner access on
- Expert selector shows **project experts first**, then global experts (with a divider)
- Pre-fills config from project defaults
- Automatically inherits project datasources and repos
- Shows which repos the agent will have access to
- Shows which branch the job will start from on the jobs repo

The existing `POST /api/jobs` endpoint still works — if no `project_id` is specified, it uses the user's default project. This preserves backward compatibility for API consumers and CLI usage.

## What This Enables (Future Resource Types)

The resource hub model is extensible. Once the project entity and repo linking exist, additional resource types can be added without schema redesign:

### Project Wiki
A managed Gitea repo with `role = 'wiki'` (or a Gitea wiki on the jobs repo). Agents can read and contribute to a shared knowledge base across jobs. Human team members can edit it too.

### Backlog / Kanban Board
A `project_backlog_items` table linked to the project. Items have status (backlog → ready → in progress → review → done), priority, labels, and optionally a linked job. The cockpit shows a kanban view. Agents can pull tasks from the backlog, and the critic agent can add new items based on review findings.

### Cross-Job Memory (Integration with Memory Light)
Project-scoped memories that persist across jobs. When `memory.enabled: true` and a job belongs to a project, memories are scoped to `project_id` instead of `job_id`. Every job in the project benefits from insights gathered by previous jobs. The `memories` table already has `job_id` — adding `project_id` and changing the retrieval query to `WHERE project_id = $1` (instead of `WHERE job_id = $1`) is the only schema change needed. This directly implements Memory Light's Phase 5 (cross-job memory) from [[memory_light]].

### Multi-Agent Collaboration
Multiple agents work on the same project concurrently (parallel branches across repos). A critic agent reviews completed branches before merge. A scholar agent proposes new backlog items. The shared repos and datasources are the collaboration surface.

### Messaging / Feedback Loop
The "send a message to continue" UX — when a job completes or freezes, the user can send feedback that creates the next job in the chain. This is the persistent agent experience: a continuous conversation where each turn is a job.

### Automated Pipelines
Define a sequence of jobs that execute automatically: "first extract requirements, then generate code, then run tests, then review." Each step is a job config template. The pipeline creates and runs jobs sequentially, with conditional branching based on job outcomes.

## Implementation Phases

### Phase 1: Database + API Foundation — COMPLETED

All items below have been implemented.

- [x] Add `projects` table to orchestrator schema (with `is_default` flag, status CHECK, `updated_at` trigger)
- [x] Add `project_members` table to orchestrator schema (composite PK, role CHECK, CASCADE FKs)
- [x] Add `project_repositories` table to orchestrator schema (role CHECK, `chk_reference_read_only`, unique partial index for jobs repo)
- [x] Add `default_project_id` to `users` table
- [x] Add `project_id`, `branch_name`, `merge_status`, `repo_merge_statuses` columns to `jobs` table (nullable at schema level, API enforces for new jobs)
- [x] Add `project_id` to `datasources` table, replace unique index with three-level scope (`uq_datasource_type_scope`)
- [x] Update `job_summary` view with project columns
- [x] **Default project auto-creation**: `POST /api/users` auto-creates default project, Gitea jobs repo, owner membership, sets `users.default_project_id`
- [x] **Init migration**: `_seed_default_projects()` creates default projects for existing users without one; `_migrate_orphan_jobs()` assigns orphan jobs to default projects
- [x] 17 PostgresDB methods: project CRUD (with aggregate counts), member management (with user JOIN), repository management (with role filter), default project lifecycle
- [x] Updated existing DB methods: `create_job` (project_id, branch_name), `get_job`/`get_jobs` (project columns), `get_user`/`list_users` (default_project_id), `resolve_datasources_for_job` (three-level resolution)
- [x] 17 API endpoints: project CRUD, members, repositories, project jobs, merge/promote stubs (501)
- [x] `POST /api/jobs` auto-resolves user's default project when `project_id` not specified
- [x] GiteaClient: `create_branch()`, `list_branches()`, `delete_branch()`, `create_pr()`, `merge_pr()`, `rename_repo()`
- [x] Angular TypeScript models: `Project`, `ProjectMember`, `ProjectRepository` interfaces with create/update types; updated `User`, `Job`, `JobCreateRequest`
- [x] Project deletion cleans up managed Gitea repos, blocks deletion of default projects

### Phase 2: Agent Workspace Integration — COMPLETED

All items below have been implemented unless noted otherwise.

- [x] Extend `JobStartRequest` (both orchestrator and agent sides) with `repositories`, `branch_name`, `project_id` fields
- [x] Orchestrator dispatch (`assign_job_to_agent`): resolve project repositories, derive `git_remote_url` from jobs repo, pass `project_id` to three-level datasource resolution
- [x] Agent API passthrough: `_process_orchestrator_job` and `start_job_from_orchestrator` forward all three new fields via metadata dict
- [x] Extend `WorkspaceManagerConfig` with `branch_name` and `repositories`; add `_source_repos` dict and `source_repos` property to `WorkspaceManager`
- [x] `WorkspaceManager.initialize_project_workspace()`: clone jobs repo as workspace root, checkout job branch, create subdirectories, clone auxiliary repos, update `.gitignore`
- [x] `WorkspaceManager._clone_auxiliary_repos()`: clone source/reference repos into `repos/` subdirectory with branch checkout, commit `.gitignore` update
- [x] `GitManager.checkout_branch(branch_name, create=False)`: handles local, remote-tracking, and new branch creation
- [x] `GitManager.current_branch()`: returns current branch name
- [x] Agent startup routing: `_setup_job_workspace` routes to `initialize_project_workspace()` when repositories are present, falls back to `initialize()` for non-project jobs
- [x] Pod handoff: clones workspace, checks out correct branch, clones auxiliary repos for project workspaces
- [x] Resume: verifies and corrects branch alignment on workspace resume
- [x] Phase push: existing `push()` works unchanged (pushes current branch); added branch logging in `_complete_phase_with_git()`
- [x] Graceful degradation: all new git/repo operations wrapped in try/except; non-project jobs completely unaffected
- [x] Datasource resolution passes `project_id` for three-level scope (job > project > global)

### Phase 3: Merge + Promote — COMPLETED

All items below have been implemented unless noted otherwise.

- [x] `PostgresDB.update_job_merge_status()` — dedicated method for updating `merge_status` and `repo_merge_statuses` JSONB columns
- [x] **Merge endpoint** (`POST /api/projects/{id}/jobs/{jid}/merge`): PR-based merge of job branch into main via Gitea. Supports `merge`, `rebase`, `squash` strategies. Optional branch deletion after merge. Returns PR number and URL.
  - [x] Conflict detection: sets `merge_status = 'conflict'`, returns 409 with PR info for manual resolution
  - [x] No-diff handling: sets `merge_status = 'skipped'` if PR creation fails (no changes)
  - [x] Validates job belongs to project, is completed, not already merged
- [x] **Skip-merge endpoint** (`POST /api/projects/{id}/jobs/{jid}/skip-merge`): marks `merge_status = 'skipped'` for exploratory/research jobs
- [x] **Promote endpoint** (`POST /api/jobs/{jid}/promote`): converts a default-project job into a dedicated project
  - [x] Creates new project with Gitea jobs repo
  - [x] Seeds new repo from old job branch via temp clone (preserves git history)
  - [x] Adds user as owner, moves job to new project
  - [x] Validates job is in a default project and is completed
- [x] **Cleanup**: removed redundant `job-{id}` Gitea repo creation in `create_project_job` — project jobs use the project's jobs repo directly via `git_remote_url`
- [x] `MergeRequest` and `PromoteRequest` Pydantic models

### Phase 4: Cockpit UI — COMPLETED

All items below have been implemented unless noted otherwise.

- [x] `MergeRequest`, `PromoteRequest` TypeScript interfaces added to `api.model.ts`; `project_id` added to `JobSummary` audit model
- [x] `'project-list'` added to `ComponentType` union in `layout.model.ts`
- [x] 19 project API methods added to `api.service.ts`: project CRUD, members, repositories, project jobs, merge/skip-merge, promote
- [x] **Project list page** (`pages/project-list/`): CSS grid of project cards with name, truncated description, status badge, "Personal" badge for default projects, count chips (jobs/repos/members). Inline create form (name required, description, goal). Loading spinner + empty state.
- [x] **Project detail page** (`pages/project-detail/`): Tabbed interface with 4 tabs:
  - [x] **Overview tab**: Description, goal (inline editable), stats cards (jobs/repos/members count), default config, timestamps
  - [x] **Jobs tab**: Table with status, description, config, branch, merge status columns. Merge controls for completed jobs (strategy dropdown: merge/rebase/squash, delete-branch checkbox, merge + skip buttons). Per-job loading state. 30-second auto-refresh.
  - [x] **Repos tab**: Table with role badges (jobs/source/reference), name, URL, read-only, managed columns. Inline add form (name, URL, role dropdown, read-only + managed checkboxes). Remove button per repo.
  - [x] **Members tab**: Table with user avatar + name, role dropdown (inline change), joined date. Add member form (user dropdown from `getUsers()`, role dropdown). Remove button (disabled for last owner).
- [x] Routes: `/projects` and `/projects/:id` with `authGuard`
- [x] Sidebar navigation: "Projects" link with `folder_shared` icon between Simple and Debug
- [x] Mobile shell: "Projects" tab in bottom bar after Jobs, renders project-list component
- [x] Component registry: `project-list` registered in `App.registerComponents()`
- [x] **"Promote to Project" action** on job-list: teal-colored "Promote" button on completed jobs without a project. Inline form row (name, description, goal) that calls `promoteJob()`.

- [x] **Project expert endpoints** (`GET /api/projects/{id}/experts`, `GET /api/projects/{id}/experts/{name}`): Scans `experts/` directory in project's Gitea jobs repo. Returns `ExpertInfo` list / full merged config + instructions. Graceful degradation when Gitea unavailable.
- [x] **Frontend API methods**: `getProjectExperts()`, `getProjectExpertDetail()` added to `api.service.ts`
- [x] **Experts tab** on project detail: Expert card grid (icon, name, description, tags). Loading spinner + empty state with hint about `experts/` directory.
- [x] **Settings tab** on project detail: General section (editable name, default config name, save button). Danger Zone (archive for active projects, delete with confirm dialogs). Default projects show disabled message.
- [x] **Project selector in job-create**: Dropdown above description field. Loads user's projects, defaults to `is_default` project. Honors `?project=` query param. Sends `project_id` with job create request. Hidden when user has only 1 project.
- [x] **"New Job" button** on project detail Jobs tab: Navigates to job-create with `?project={id}` query param for pre-selection.

### Phase 5: Persistent Agent Mode — NOT STARTED

The idea is to elevate the cockpit's **builder chat** into a project's persistent agent. Today the builder is a stateless assistant that helps draft job instructions. In this phase, it becomes a project-scoped conversational agent: the user sends messages in the builder chat, each message becomes a job, and the chat history reflects the chain of jobs and their outcomes.

This is far from implementation. Open questions:
- How does the builder chat maintain project context across jobs? (workspace.md is the likely carrier, but the builder currently has no project scope)
- What's the UX for reviewing job output inline in the chat vs. navigating to the job review panel?
- How does the auto-chain interact with autonomy levels? (A persistent agent with `review` autonomy would freeze after every job, requiring approval before the next one starts — that might be the right default)
- Should the persistent agent have its own expert config, or does it use the project's default?

Rough sketch:
- Auto-chain mechanism: on job completion + merge, if the project has persistent mode enabled, the system waits for the user's next message in the builder chat
- Message → job: user sends text in the builder chat → orchestrator creates a new job in the project with that text as the description, using the project's default expert config
- Session continuity: the new job starts from merged `main`, so it has the full accumulated project state in workspace.md and deliverables in `output/`
- Chat history: the builder chat shows the sequence of messages and job summaries, giving a conversational view over what is internally a chain of independent jobs

### Open Work

Consolidated list of deferred items across all phases. Roughly priority-ordered.

**Critical (blocks reliable project job execution):**
- [ ] `.gitignore` job-scoped files in project workspaces — prevents stale signal files from leaking between jobs (see "Job State vs Project State")
- [ ] Move freeze/completion signaling to DB-only — `freeze_data JSONB` column on `jobs` table, approve endpoint reads from DB instead of Gitea
- [ ] Fix approve endpoint repo name for project jobs — currently hardcoded to `job-{job_id}`, needs to resolve project's actual jobs repo

**Important (completes the project workflow):**
- [ ] Source repo job branches — agents should create job-specific branches in source repos for isolation (currently cloned at configured branch without branching)
- [ ] Push-on-completion for modified source repos — only jobs repo is pushed today
- [ ] Source repo merge via PR — same flow as jobs repo merge, per-repo merge status tracking
- [ ] Agent-assisted merge conflict resolution — extend MCP/builder tools to inspect and resolve conflicts
- [ ] Authorization enforcement — role-based endpoint gating (owner/editor/viewer); `get_user_role_in_project()` exists but endpoints don't check it
- [ ] Integration test: two sequential project jobs verifying second sees first's output

**Nice to have (UI/UX polish):**
- [ ] Expert selector in job-create showing project experts first, then global experts (with divider)
- [ ] Project expert creation via guided form (currently read-only display)
- [ ] Project datasource management tab (project-scoped datasource panel)
- [ ] Repo file browser per project repository
- [ ] Authorization enforcement in UI (role-based button visibility)
- [ ] Auto-merge on job completion (configurable per project, deferred until more testing)

**Long-term (Phase 5+):**
- [ ] Persistent agent mode via builder chat elevation
- [ ] Content-only repositories (move all job metadata to DB, repositories hold only deliverables)
- [ ] Automated merge phase (agent resolves conflicts automatically, user approves)

## Design Decisions

### Why a Resource Hub Instead of "Project = Repo"?

The initial design had the project as a single shared repo. But real projects have multiple repos (frontend/backend, libraries, documentation), multiple databases, and eventually other resources. Making the project a hub that *links to* resources rather than *being* a resource is more flexible.

This is a well-validated pattern. GitHub Projects V2 was redesigned specifically to be a cross-repo planning layer — a project references issues/PRs from any repo in the org but doesn't own code. Linear Projects span multiple teams. Azure DevOps recommends "one project, many repos" as the default structure. In the AI agent space, Devin's project context is assembled from Knowledge (org-wide tips), Playbooks (task templates), and machine snapshots — not from a single repo. Factory.ai's AGENTS.md files + Jira/Linear/Slack integrations form the project context implicitly.

The consensus is clear: **the planning layer and the code layer must be separate**. The project owns the "why" and "what"; repos own the "how."

The jobs repo is special (one per project, managed, holds workspace state) but it's still just one entry in `project_repositories`. This keeps the model uniform.

### Why Git as the Continuity Mechanism?

Alternatives considered:
- **Shared filesystem**: Simple but no history, no branching, no conflict resolution
- **Database records**: Would need a custom "project state" model; git already solves this
- **Artifact store (S3/R2)**: Good for outputs but not for iterative work

Git gives us branching, merging, history, diffs, and conflict detection for free. The agent already uses git versioning in its workspace. Making repos shared across jobs is a natural extension, not a new paradigm.

### Why Branch-per-Job Instead of All-on-Main?

Working directly on `main` would be simpler but:
- Parallel jobs would conflict constantly
- Failed jobs would pollute `main` with broken state
- No way to review/reject a job's output before it affects the project

Branch-per-job gives isolation during execution with controlled integration afterward. It's the same model as feature branches in software development.

### Why Repos as Filesystem Clones Instead of API Access?

The agent could access source repos via Gitea's API (read files over HTTP). But cloning into `repos/` means:
- All existing file tools (`read_file`, `write_file`, `list_directory`) work without changes
- The agent can run code, build projects, execute tests — not possible via API
- Git operations (diff, log, blame) work locally with no latency
- Large repos aren't re-fetched on every file read

The tradeoff is disk space and clone time, but for the repo sizes we're dealing with this is negligible.

### Why Store Expert Configs in the Jobs Repo Instead of a Database Table?

Alternatives considered:
- **JSONB column on `projects`**: Simple but configs are multi-file (config.yaml + persona.txt + instructions.md + prompt files). Cramming all of that into JSON loses the natural file structure.
- **Separate `project_experts` table**: Would need columns for every possible file (persona, instructions, strategic prompt, tactical prompt, etc.). Rigid and duplicates the file-based resolution system.
- **Separate managed Gitea repo per project for configs**: Overkill. Another repo to manage for what amounts to a few YAML and markdown files.

The jobs repo is the right place because:
- Expert configs are already a file-based system (directory with config.yaml + prompt/instruction files). Keeping them as files preserves the format.
- They're versioned alongside the project's other state (workspace.md, plan.md, archives). When you look at the project's git history, you can see when and why expert configs changed.
- The `$extends` mechanism and matrix resolver already support directory-based config loading. We just add one more directory to the resolution chain.
- Jobs can modify expert configs (an agent could refine its own successor's instructions). This is powerful for the persistent agent mode — the project's agents improve over time.

This mirrors the emerging industry pattern: Factory.ai stores custom Droid definitions as `.md` files in `.factory/droids/` (repo-level) or `~/.factory/droids/` (global). Cursor uses `.cursor/rules/` with hierarchical loading. Claude Code uses `CLAUDE.md` with directory-based inheritance. The consensus is that agent configuration belongs in the repo, version-controlled alongside the code it applies to.

### Why Not Git Submodules?

Submodules would track source repos as part of the jobs repo. But:
- Submodules are notoriously brittle and confusing
- The agent would need to understand submodule semantics
- Detached HEAD states, version pinning, recursive updates — all complexity with no benefit
- Independent clones in `repos/` are simpler and the agent doesn't even need to know they're there

### Why Not a Full CI/CD Pipeline?

Projects intentionally don't define execution order or dependencies between jobs. Jobs are created and run manually (or via the persistent agent auto-chain). A full pipeline system (DAGs, triggers, conditional branching) would add significant complexity for limited initial value. The kanban/sprint layer (future) is the right place for workflow automation.

### Job State vs Project State — What Merges Into Main

The jobs repo is shared across all jobs in a project. When a job branch is merged into `main`, every committed file goes with it. But not all workspace files are "project state" — many are job-specific transient data that should never reach `main`. If they do, the next job that clones from `main` inherits stale state from a previous job.

**File classification:**

| Path | Scope | Should merge? | Reason |
|------|-------|---------------|--------|
| `workspace.md` | Project | Yes | Accumulated project knowledge, persists across jobs |
| `output/` (deliverables) | Project | Yes | Work products that future jobs build on |
| `experts/` | Project | Yes | Project-specific agent configs, evolve over time |
| `plan.md` | Job | No | Job-specific execution plan, not relevant to next job |
| `todos.yaml` | Job | No | Current job's task list |
| `archive/` | Job | No | Phase history (archived todos + retrospectives) |
| `tools/` | Job | No | Auto-generated tool documentation |
| `documents/` | Job | No | Input documents for this specific job |
| `instructions.md` | Job | No | Generated instructions for this job |
| `output/job_frozen.json` | Job | No | Freeze signal — causes `check_goal` to stop new jobs |
| `output/job_completion.json` | Job | No | Approval signal — same issue |
| `reference/` | Job | No | Job-specific reference materials |

**The core problem:** The agent commits everything (via `git add -A` in `GitManager.commit()`), and the PR-based merge moves all of it to `main`. There's no mechanism to distinguish deliverable outputs from job machinery.

**Current dependencies that complicate this:**

1. **The approve endpoint reads `job_frozen.json` from Gitea** (`GET /api/jobs/{id}/approve`, line 929 of `orchestrator/main.py`). It looks in repo `job-{job_id}`, which is the legacy per-job repo name — this is already broken for project jobs where the repo is the project's jobs repo, not `job-{job_id}`.

2. **The `check_goal` node in `src/graph.py` detects freeze/completion by checking file existence** (`output/job_frozen.json`, `output/job_completion.json`). This is the mechanism that breaks when stale files leak across jobs.

3. **Phase transitions (`freeze_for_review`, `finalize_job`) write signal files and immediately commit + push them.** The approve endpoint then reads and deletes them. This file-based signaling was designed for isolated per-job repos, not shared project repos.

**Proposed approach — `.gitignore` job-scoped files + DB-only signaling:**

1. **Add job-scoped paths to `.gitignore` in project workspaces.** During `initialize_project_workspace`, configure the gitignore to exclude:
   ```
   # Job-scoped files (not merged to main)
   plan.md
   todos.yaml
   archive/
   tools/
   documents/
   reference/
   instructions.md
   output/job_frozen.json
   output/job_completion.json
   ```
   This prevents job machinery from entering git. Deliverable files in `output/` (e.g. reports, generated code, analysis results) still get committed — only the signal files are excluded.

2. **Move freeze/completion signaling to DB-only.** The jobs table already has `status` (`pending_review`, `completed`) and can carry metadata. Instead of writing `job_frozen.json` and reading it back:
   - `freeze_for_review()` and `finalize_job()` store freeze metadata (freeze_type, summary, deliverables, confidence, etc.) in a `freeze_data JSONB` column on the `jobs` table
   - The approve endpoint reads from the DB column instead of Gitea
   - `check_goal` checks the DB status or a state flag instead of file existence
   - The file-based signaling becomes a local-only debug artifact (written but not committed)

3. **Fix the approve endpoint repo name for project jobs.** Currently hardcoded to `job-{job_id}`. For project jobs, resolve the actual project jobs repo name via the job's `project_id` → `project_repositories` (role=jobs).

4. **Selective `.gitignore` for project vs non-project jobs.** Non-project jobs (legacy isolated repos) can continue committing everything — there's no cross-job contamination risk. The extended `.gitignore` only applies to project workspace initialization. `GitManager.DEFAULT_IGNORE_PATTERNS` stays unchanged; the project-specific patterns are appended by `initialize_project_workspace`.

**What this means for the merge flow:**

After this change, a PR from a job branch to `main` only contains:
- `workspace.md` updates (project memory)
- `output/` deliverables (actual work products)
- `experts/` changes (if the agent refined project-specific configs)

Everything else stays on the job branch as historical record (browsable in Gitea, never merged). The branch can be kept or deleted after merge — either way, `main` stays clean.

**Alternative considered — pre-merge cleanup:**

Instead of preventing commits, the orchestrator could delete job-scoped files from the branch via Gitea API before creating the PR. This works but is fragile — it requires the orchestrator to know which files are job-scoped, creates extra commits, and if someone merges manually (bypassing the endpoint), stale files leak through. The `.gitignore` approach is self-enforcing.

**What happens without this fix (observed in production):** Job A runs with `review` autonomy, completes, pushes `output/job_frozen.json` to its branch. Job A's branch is merged into `main`. Job B starts, clones from `main`, checks out a new branch — inherits job A's stale `job_frozen.json`. Job B completes strategic phase 0, transitions to tactical, then `check_goal` finds the stale file and stops the agent immediately. Since `check_goal` is a sync function that doesn't update the database, job B's status stays `processing` — the cockpit shows no Review/Continue button and the job appears stuck. The `.gitignore` approach prevents this entirely by keeping signal files out of git.

### Long-Term Direction: Content-Only Repositories

The `.gitignore` approach is the pragmatic near-term fix, but the long-term direction is to make project repositories **content-only** — no job metadata, no signal files, no agent machinery. All job state (plan, todos, archives, phase snapshots, freeze signals, tool docs) lives in the database, not in git. The repository holds only deliverables: workspace.md, output files, expert configs, and user-uploaded content.

This simplifies everything: merges are always about content, not about filtering out infrastructure files. The agent's workspace is assembled from two sources — content from git, state from the database — and the two never mix.

New features should follow the **DB-first principle**: store configuration and metadata in database columns/JSONB, not as files in the repository. The `.gitignore` approach bridges the gap for existing file-based mechanisms (signal files, todos.yaml, archive/) that predate the project model.

### Jobs Repo Initial Content

A newly created project starts with an empty Gitea repository. The initial content comes from one of:

1. **First job populates it.** The agent's workspace initialization creates workspace.md, plan.md, todos.yaml, etc. on the job branch. After merge, `main` has the project's first workspace.md and any deliverables.
2. **User seeds it manually.** Clone the project's jobs repo, push files (a starter workspace.md, expert configs in `experts/`, reference documents), then create jobs that build on that content.
3. **Cockpit upload (future).** The project creation form could accept initial files — a project description that becomes workspace.md, uploaded documents, etc.

There is no Gitea template repo for jobs repos. Each project starts from scratch or from promoted job content (see "Promote to Dedicated Project").

### Merge Conflict Resolution

When a job branch can't merge cleanly into `main` (e.g., two parallel jobs modified the same file, or `main` advanced while the job was running), the merge endpoint returns `409 Conflict` with the PR info.

**Current approach:** The user resolves conflicts manually via Gitea's merge UI or by cloning the repo locally.

**Planned approach — agent-assisted conflict resolution:**

1. **MCP/Builder agent tooling.** Extend the cockpit's builder chat and the MCP server (for Claude Code) with tools to inspect merge conflicts, view diffs between branches, and propose resolved versions. The user asks the builder "resolve the merge conflict on job X" and the agent reads both versions, produces a merged result, and pushes it — subject to user approval.

2. **Automated merge phase (future).** When a job completes and a merge conflict is detected, the system could automatically spawn a short "merge resolution" job. This job gets the conflict context (base, ours, theirs) and produces a clean merge commit. The user reviews and approves the result before it's merged into `main`. This avoids the user needing to understand git internals while keeping them in control.

For parallel jobs that both modify `workspace.md`, conflicts are expected. Sequential mode avoids this naturally (each job starts from merged `main`). For parallel mode, the merge resolution agent is the intended solution — it understands project context and can intelligently combine workspace updates.

### Auto-Merge Policy

Merge is currently manual-only, triggered via `POST /api/projects/{id}/jobs/{jid}/merge`. Auto-merge on job completion is deferred until the project system has been tested more thoroughly in real workflows. When added, it should be configurable per project (some projects want review before merge, others want fast sequential chaining).

### Backward Compatibility

- `POST /api/jobs` without a `project_id` still works — the orchestrator assigns the user's default project. API consumers and CLI usage don't need to change.
- All current job API endpoints remain unchanged — they operate on jobs regardless of which project owns them.
- The `projects`, `project_repositories`, and `project_members` tables are additive.
- **Migration**: Existing jobs need a `project_id` (NOT NULL). The migration creates a default project for each existing user, assigns their jobs to it, then adds the constraint. This is a data migration, not just a schema change — it needs to run as part of `init.py --force-reset` or as a versioned migration script.
