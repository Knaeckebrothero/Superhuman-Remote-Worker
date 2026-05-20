---
tags:
  - data-management
  - git-integration
  - cloud-infrastructure
  - tool-development
  - knowledge-management
---

# Repository Datasource

Design document for adding git repository support as a datasource type, enabling persistent agent memory (e.g. Obsidian vaults) and multi-repo workflows (e.g. coding agents contributing to external projects).

## Motivation

The agent needs access to external git repositories for two primary use cases:

1. **Persistent memory across jobs**: An Obsidian vault (or any note system) sitting in a git repo on a local Gitea/k3s cluster. The agent reads notes for context and writes back observations, learnings, and summaries. Knowledge persists across jobs because it lives in the repo, not in the workspace.

2. **Code contributions**: A coding agent that needs to clone, modify, and push to one or more external repositories as part of its work.

Both cases share the same mechanics: clone a repo into the workspace, give the agent tools to interact with it, and optionally sync changes back on completion or phase transitions.

## Datasources as a General Concept

From a system architecture perspective, databases, git repos, object stores, SharePoint instances, live sensors, etc. are all **external data sources**. They differ in protocol and interaction pattern, but the user-facing model is the same: configure a prompt, a set of tools, and external data sources.

The current datasource system supports three database types (`postgresql`, `neo4j`, `mongodb`). Adding git is the first non-database datasource. Future candidates include S3/R2 object stores, REST APIs, SharePoint, message queues, etc.

### Potential Sub-Categories

As the datasource type list grows, sub-categories may help organize the UI and documentation:

| Category | Types | Interaction Pattern |
|----------|-------|-------------------|
| Databases | postgresql, neo4j, mongodb, elasticsearch, redis | Query/write via tools |
| Repositories | git (+ future: svn, mercurial) | Clone to workspace, file I/O + sync |
| Object Stores | s3, r2, minio | Upload/download via tools |
| APIs | rest, graphql, sharepoint | Request/response via tools |
| Streams | kafka, mqtt, sensors | Subscribe/publish via tools |

Sub-categories are a UI/documentation concern, not a schema change. The `type` field remains a flat string (`git`, `s3`, etc.). If we add sub-categories later, it's a display-layer grouping, not a data model change.

## Design

### 1. Clone Location (Configurable Relative Path)

Each git datasource specifies a `path` — a relative path within the workspace where the repo should be cloned. This gives full flexibility:

```yaml
# Single repo at workspace root (simple case)
path: "."

# Named subdirectory
path: "notes"

# Organized under a datasources directory
path: "repositories/obsidian-vault"
path: "repositories/frontend-app"
```

The path is stored in the datasource's `credentials` JSONB field (or a new `config` JSONB field — see Storage Model below). The agent's workspace tools (`read_file`, `write_file`, `list_files`) already operate on relative paths within the workspace, so cloned repos are immediately accessible.

### 2. Tools

The agent already has the building blocks:

- **Workspace tools** (`read_file`, `write_file`, `list_files`, etc.) for file I/O on the cloned repo
- **Git tools** (`git_log`, `git_show`, `git_diff`, `git_status`, `git_tags`) for inspecting history

Currently, git tools are hardcoded to the workspace's own `git_manager`. For external repos, we need to either:

**Option A — Multi-repo aware git tools**: Extend the existing git tools with an optional `repo` parameter (defaults to workspace root). The tool resolves the repo name to a `GitManager` instance from the datasource registry.

```python
# Current: git_log() always uses workspace git
# Proposed: git_log(repo="notes") uses the cloned repo's GitManager
git_log(repo="notes", max_count=10)
git_diff(repo="notes")
git_commit(repo="notes", message="Update meeting notes")  # New write tool
git_push(repo="notes")                                      # New write tool
```

**Option B — Dedicated repo tools**: A separate tool category (`repo`) with its own tools that take a repo identifier. This avoids overloading the existing git tools which are designed for workspace versioning.

**Recommendation**: Option A for reads (extend existing tools), but new dedicated tools for writes (`repo_commit`, `repo_push`, `repo_pull`). The workspace git tools are intentionally read-only — write operations on external repos are a different concern with different safety implications.

### 3. Credentials

Git repos need authentication. Two mechanisms:

**HTTPS with token**:
```json
{
  "username": "agent",
  "token": "ghp_xxxx..."
}
```
The token is embedded in the clone URL at runtime: `https://agent:ghp_xxxx@gitea.local/user/vault.git`

**SSH key**:
```json
{
  "ssh_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n...",
  "ssh_key_passphrase": ""
}
```
The key is written to a temp file at clone time and configured via `GIT_SSH_COMMAND="ssh -i /tmp/key_xxxx -o StrictHostKeyChecking=no"`.

Both fit naturally into the existing `credentials` JSONB field on the datasources table.

#### Future: General Credentials Store — **superseded**

> Superseded by [[credential_file_datasources]] (shipped 2026-05-17). The follow-on direction landed as **three new datasource types** rather than a sibling credentials store: `kubeconfig`, `ssh_key`, and `generic_file`. Each carries a `credentials.files[]` payload that is materialized as files on the agent's filesystem at job start (`~/.kube/config`, `~/.ssh/<slug>`, user-chosen paths). The whole `datasources.credentials` JSONB column is now AES-256-GCM-encrypted at rest, so the "secrets in plain text" caveat from the original design no longer applies.
>
> If a true cross-datasource credentials registry (one secret referenced by N datasources) becomes desirable later, it can build on the same encryption + materialization machinery; the design notes below are kept for historical context.

<details>
<summary>Original proposal (historical)</summary>

Beyond datasources, an agent may need credentials for other purposes (API keys, service accounts, etc.). A general-purpose credentials section in the agent config is worth considering:

```yaml
credentials:
  gitea:
    type: ssh_key
    key: "..."
  github:
    type: token
    token: "ghp_..."
  jira:
    type: basic
    username: "agent"
    password: "..."
```

This is out of scope for the initial implementation but should be kept in mind. Datasource credentials stay in the datasources table for now. A unified credentials store can be added later and datasources can reference it by name instead of embedding secrets directly.

</details>

### 4. Unique Constraint (One Per Type Per Job)

The current `uq_datasource_type_job` index enforces one datasource per type per job. This works for databases (one PostgreSQL connection per job) but breaks for git (an agent may need multiple repos).

**For now**: We only need one repo, so the constraint holds. The first implementation supports a single git datasource per job.

**Later**: Options to support multiple repos of the same type:
- Remove the unique constraint and add a `name` uniqueness constraint instead (`UNIQUE(name, job_id)`)
- Add a `subtype` or `alias` field to the unique index
- Use composite keys (`type + name` instead of `type` alone)

This is explicitly deferred — the constraint will be revisited when multi-repo support is needed.

### 5. Sync Behavior (Agent-Managed vs Auto-Sync)

A `sync_mode` setting determines how the repo stays in sync:

| Mode | Clone | Pull | Commit | Push |
|------|-------|------|--------|------|
| `manual` | On job start | Agent decides | Agent decides | Agent decides |
| `auto` | On job start | On phase transition (strategic start) | On phase transition (tactical end) | On phase transition (tactical end) |
| `readonly` | On job start | On phase transition | Never | Never |

**`manual`**: The agent has full control via git tools. Appropriate for coding agents that need precise commit messages, branching, etc.

**`auto`**: The system pulls at strategic phase start (get latest) and commits+pushes at tactical phase end (persist changes). Appropriate for the Obsidian vault use case — the agent just reads/writes files and sync happens transparently.

**`readonly`**: Clone and pull only, no writes. Appropriate for reference repos the agent should read but never modify.

The `sync_mode` is stored alongside `read_only` in the datasource config. The `read_only` flag on datasources maps to `readonly` sync mode. For `manual` and `auto`, `read_only` is false.

Integration points for auto-sync:
- **Phase transition** (`src/core/phase.py`): After archiving the tactical phase, before starting strategic — pull. After strategic planning, before starting tactical — commit+push pending changes.
- **Job completion** (`src/core/phase.py`): Final commit+push to ensure nothing is left uncommitted.
- **Job start** (`src/agent.py` → `_setup_job_tools` or workspace init): Clone the repo.

### 6. Storage Model

The existing datasources table works with minimal changes:

```sql
-- Existing fields used:
--   type = 'git'
--   connection_url = 'https://gitea.local/user/vault.git' or 'git@gitea.local:user/vault.git'
--   credentials = {"username": "agent", "token": "..."} or {"ssh_key": "..."}
--   read_only = true/false
--   name, description = user-provided metadata

-- New: config JSONB for type-specific settings (alternative: embed in credentials)
-- For git:
--   config = {"path": "notes", "sync_mode": "auto", "branch": "main"}
```

Options for where to store `path`, `sync_mode`, `branch`:
- **In `credentials` JSONB**: Quick, no schema change, but semantically wrong (these aren't credentials)
- **New `config` JSONB column**: Clean separation, one migration. Recommended.

### 7. Agent-Side Implementation

#### Connection Creation (`_create_datasource_connection`)

For database datasources, this method creates a connection object (Neo4jDB, psycopg conn, etc.). For git, the equivalent is cloning the repo and returning a `GitManager` instance:

```python
elif ds_type == "git":
    config = ds.get("config") or {}
    rel_path = config.get("path", "repository")
    branch = config.get("branch", "main")
    clone_target = self._workspace_manager.get_path(rel_path)

    # Build authenticated URL
    url = self._build_git_url(ds["connection_url"], ds.get("credentials", {}))

    # Clone (or pull if already exists from a resumed job)
    if clone_target.exists() and (clone_target / ".git").exists():
        mgr = GitManager(clone_target)
        mgr.pull()
    else:
        mgr = GitManager.clone(url, clone_target)

    if mgr and branch != "main":
        mgr._run_git(["checkout", branch])

    return mgr
```

The returned `GitManager` is stored in `ToolContext.datasources["git"]`, accessible to tools.

#### Tool Integration

Git tools need access to the datasource `GitManager` in addition to the workspace one. The `create_git_tools` function can be extended to check for a git datasource and expose repo-aware operations.

### 8. Cockpit UI

The datasource creation form needs a "Git" type option with fields:

- Repository URL (text input)
- Branch (text input, default: "main")
- Clone path (text input, default: "repository")
- Sync mode (dropdown: manual / auto / readonly)
- Authentication method (radio: HTTPS token / SSH key)
  - HTTPS: username + token fields
  - SSH: key textarea + optional passphrase

Connection test: attempt clone to a temp directory, verify access, clean up.

## Implementation Phases

### Phase 1: Core Git Datasource

- Add `config` JSONB column to datasources table (migration)
- Add `git` to `DS_TOOL_MAP` in orchestrator (tool category mapping)
- Implement clone logic in `_create_datasource_connection`
- Extend `GitManager` with credential-aware clone (HTTPS token URL building, SSH key file)
- Add `repo_commit`, `repo_push`, `repo_pull` tools (write operations on external repos)
- Extend existing git read tools with optional `repo` parameter
- Add auto-sync hooks in phase transition logic
- Add `git` type to cockpit datasource form

### Phase 2: Multi-Repo Support (Deferred)

- Revisit unique constraint on datasources table
- Support multiple git datasources per job with distinct names/paths
- Tool disambiguation (which repo to operate on)

### Phase 3: General Credentials Store — **superseded**

The follow-on work landed as [[credential_file_datasources]] (shipped 2026-05-17): three new datasource types (`kubeconfig`, `ssh_key`, `generic_file`) whose `credentials.files[]` payload is materialized as files on the agent's filesystem. Encryption-at-rest for the `datasources.credentials` column shipped alongside.

## Open Questions

- **Conflict resolution**: What happens if auto-sync pull encounters merge conflicts? Options: fail loudly and let the agent resolve via tools, or always force-pull (reset to remote). For the Obsidian use case, the agent is the only writer, so conflicts shouldn't occur. For multi-agent scenarios, this needs thought.
- **Large repos**: Should we support shallow clones (`--depth 1`) for large repos where history isn't needed? Probably yes, as a config option.
- **Submodules**: Ignore for now, add `--recurse-submodules` as a config flag later if needed.
- **.gitignore on workspace**: When a repo is cloned into the workspace, the workspace's own `.gitignore` should exclude the cloned repo directory to avoid the workspace git tracking external repo files.

## Related

- [[datasources]]
- [[cloud_workspace]]
- [[obsidian]]
- [[security_checklist]]
- [[deployment]]
- [[tool_issues]]
