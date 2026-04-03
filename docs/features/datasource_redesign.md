# Datasource Redesign: Generic, Repository & Managed Connector Architecture

## Problem

The current datasource system registers 20+ custom tools per type (sql_query, sql_execute, mongo_query, mongo_insert, execute_cypher_query, etc.), bloating the agent's tool list. Research shows tool invocation accuracy degrades significantly beyond ~30 tools in context. Modern LLMs are proficient with CLI tools and `--help` discovery, making most of these wrappers unnecessary.

Additionally, the `read_only` flag on datasources is advisory only — it's stored on the datasource record but never enforced. When credentials are injected as environment variables, the agent has full access regardless of the flag.

### Industry Context

This redesign aligns with patterns emerging across the AI agent ecosystem:
- **MCP servers** (Claude Code, Cursor, Windsurf) use structured tools as the security boundary — the agent never sees raw credentials. Read-only enforcement happens at the server layer.
- **Devin** runs in cloud VMs with env var injection and MCP for structured access. Repos are pre-cloned into workspace snapshots.
- **SWE-agent / OpenHands** run in Docker containers with repo cloning at startup and token injection via config files.
- **LangChain SQL toolkit** wraps SQLAlchemy with structured tools — the consensus pattern for read-only database access.

The common thread: **CLI for read-write, structured tools for read-only.** Our design follows this same principle.

## Design

Three datasource categories, unified by a single principle: **the agent should never need to handle credentials manually.**

### 1. Generic Datasource

A flexible type for any resource the agent can access via CLI. The user defines exactly how credentials are injected.

**How it works:**
- User defines environment variable mappings (key-value pairs)
- Credentials are injected as env vars into the agent's workspace at job start
- The agent discovers datasource info via knowledge base entries (Neo4j + pgvector)
- The KB entry lists env var *names* (not values) and CLI usage hints
- The agent uses standard CLI tools: `psql`, `cypher-shell`, `mongosh`, `git`, `curl`, etc.

**No read-only enforcement.** The agent has raw CLI access. Users who need read-only access should create a database account with restricted permissions and enter those credentials. A note in the UI makes this clear.

**Form fields:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| Name | text | yes | Human label ("Production DB", "Main Repo") |
| Description | textarea | yes | Free text for the AI — what this is, how to use it, what data it contains |
| Connection URL | text | no | Primary endpoint (displayed in KB entry if provided) |
| Environment Variables | key-value list | no | Dynamic list of env var mappings injected at job start |
| CLI Hint | text | no | Suggested CLI command (e.g. "psql $DATABASE_URL", "git clone $REPO_URL") |

**Environment variable editor:**
- Key-value pairs with a `+` button to add more rows
- Key: env var name (e.g. `DATABASE_URL`, `GIT_SSH_KEY`, `API_TOKEN`)
- Value: the secret value (masked input, stored in `credentials` JSONB)
- At least one env var or a connection URL should be provided
- A `x` button per row to remove

**Credentials storage:**
```json
{
  "env_vars": {
    "DATABASE_URL": "postgresql://user:pass@host:5432/analytics",
    "DB_SCHEMA": "public"
  }
}
```

Non-secret values (schema names, hostnames) can also go here — the distinction is that env var values never appear in KB entries.

**Generated KB entry example:**
```markdown
## Datasource: Production Analytics DB
PostgreSQL database containing user analytics and event data.
Query the `events` table for user activity tracking.

### Connection
- **URL:** postgresql://host:5432/analytics (credentials via env vars)
- **CLI:** `psql $DATABASE_URL`

### Environment Variables
- `DATABASE_URL` — PostgreSQL connection string (pre-configured)
- `DB_SCHEMA` — Target schema name
```

### 2. Repository Datasource

A dedicated type for git repositories. The agent uses standard `git` CLI — no custom tools. The value-add over generic is **automated workspace setup**: the orchestrator clones the repo and pre-configures git credentials so the agent never sees or handles them.

**How it works:**
- User provides a repo URL and credentials (HTTPS token or SSH key)
- At job start, the orchestrator:
  1. Configures git credentials transparently (credential helper for HTTPS, SSH key + config for SSH)
  2. Clones the repo into the workspace (e.g. `workspace/repos/{name}/`)
  3. The agent gets a KB entry saying "repo cloned at `./repos/{name}/`, git is pre-authenticated"
- The agent uses `git pull`, `git commit`, `git push`, etc. — credentials are transparent
- The agent never sees tokens or SSH keys

**Form fields:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| Name | text | yes | Human label ("Frontend Repo", "Config Repo") |
| Description | textarea | no | What this repo contains, relevant branches, etc. |
| Repository URL | text | yes | HTTPS or SSH URL (e.g. `https://github.com/org/repo.git`) |
| Default Branch | text | no | Branch to clone (defaults to repo default) |
| Auth Method | select | yes | HTTPS Token / SSH Key |
| Token | password | if HTTPS | Personal access token or deploy token |
| SSH Private Key | textarea | if SSH | Private key (PEM format) |

**Credentials storage:**
```json
// HTTPS
{
  "auth_method": "token",
  "token": "ghp_xxxxxxxxxxxx"
}

// SSH
{
  "auth_method": "ssh",
  "ssh_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n..."
}
```

**Job start setup (orchestrator):**

HTTPS uses a git credential helper — credentials stay out of `.git/config`:
```bash
# HTTPS — credential helper that reads from a transient store
git config --global credential.helper 'store --file=/tmp/.git-credentials'
echo "https://oauth2:${TOKEN}@github.com" > /tmp/.git-credentials
chmod 600 /tmp/.git-credentials
```

SSH uses a per-repo key file:
```bash
# SSH — write key file and configure
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo "${SSH_KEY}" > ~/.ssh/repo_${name}
chmod 600 ~/.ssh/repo_${name}
cat >> ~/.ssh/config <<EOF
Host ${host}
  IdentityFile ~/.ssh/repo_${name}
  StrictHostKeyChecking accept-new
EOF

# Clone into workspace
git clone ${repo_url} --branch ${default_branch} repos/${name}/
```

**Read-only:** Not enforced at the tool level (agent has full git CLI). Users who need read-only should use a deploy token with read-only scope (GitHub, GitLab, Gitea all support this). The KB entry notes that git is pre-authenticated without specifying the access level.

**Generated KB entry:**
```markdown
## Repository: Frontend App
React frontend application. Main development branch is `develop`.

### Location
Cloned to `./repos/frontend-app/` — git is pre-authenticated.

### Usage
Use standard git commands:
- `cd repos/frontend-app && git status`
- `git pull`, `git commit`, `git push`
- No login or credential setup required.
```

### 3. Managed Connectors (postgresql, neo4j, mongodb, webdav)

Managed connectors have **structured forms** with type-specific credential fields and connection testing. They behave differently depending on the read-only setting — this is the key security differentiator.

**Read-write mode** (default):
- The orchestrator pre-installs the CLI tool in the workspace image (`psql`, `cypher-shell`, `mongosh`)
- Credentials are injected as env vars (`PGHOST`/`PGUSER`/`PGPASSWORD`, `NEO4J_URI`, `MONGOSH_URI`, etc.)
- The agent uses the CLI tool directly — same as generic, but with structured setup
- The KB entry tells the agent which CLI to use and which env vars are available
- No custom tools registered — reduces tool count

**Read-only mode** (project-level toggle):
- **No credentials injected** — no env vars, no CLI tool access
- Only read-only custom tools are registered (e.g. `sql_query` + `sql_schema`, but not `sql_execute`)
- The tools hold the credentials internally and mediate all access (same pattern as MCP database servers)
- The agent cannot access the database outside of the provided tools
- This is **real enforcement** — the agent has no credentials and no CLI access

**Exception: WebDAV** — no good CLI tool exists. WebDAV always uses custom tools regardless of read-only setting. Read-only just gates which tools are registered (read vs read+write).

**Why both modes?**
Users choose based on their needs:
- Need the agent to run migrations, create tables, insert data? → Read-write (CLI access)
- Need the agent to query data for analysis but never modify it? → Read-only (tool-gated)
- Don't care about typed forms? → Use the generic datasource instead

Users can also add the same database twice: once as a managed connector (read-only for safe querying in one project) and once as generic (full CLI access for a different project).

**Read-only scope:** Project-level setting on the junction table (`project_datasources.read_only`). The same datasource can be read-write in one project and read-only in another. This controls the mode (CLI vs tools) at job start.

## Schema Changes

### `datasources` table

Remove `read_only` from the datasource itself (move to project-level only):
```sql
ALTER TABLE datasources DROP COLUMN IF EXISTS read_only;
```

Add `'generic'` and `'repository'` to valid types. The `type` column is unconstrained TEXT, so no CHECK to update.

Add `cli_hint` and `default_branch` columns:
```sql
ALTER TABLE datasources ADD COLUMN IF NOT EXISTS cli_hint TEXT;
ALTER TABLE datasources ADD COLUMN IF NOT EXISTS default_branch TEXT;
```

Make `connection_url` nullable (generic datasources may only use env vars):
```sql
ALTER TABLE datasources ALTER COLUMN connection_url DROP NOT NULL;
```

### `project_datasources` junction table

Keep `read_only` here (project-level control for managed connectors):
```sql
-- Already exists:
-- read_only BOOLEAN (nullable, NULL = use default behavior per type)
-- description TEXT (nullable, project-level override)
```

### Credentials JSONB structure

**Generic:**
```json
{
  "env_vars": {
    "KEY": "value"
  }
}
```

**Repository:**
```json
// HTTPS token
{ "auth_method": "token", "token": "ghp_xxxxxxxxxxxx" }

// SSH key
{ "auth_method": "ssh", "ssh_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n..." }
```

**Managed connectors (unchanged):**
```json
// PostgreSQL
{ "password": "..." }

// Neo4j
{ "username": "neo4j", "password": "..." }

// WebDAV
{ "username": "...", "password": "..." }

// MongoDB
{} // credentials in connection_url
```

## Credential Injection & Workspace Setup

### Generic datasources

The orchestrator reads `credentials.env_vars` and injects each key-value pair directly:
```python
if ds["type"] == "generic":
    env_vars = ds.get("credentials", {}).get("env_vars", {})
    for key, value in env_vars.items():
        job_env[key] = value
```

No guessing, no mapping — the user defines exactly what env vars the agent gets.

### Repository datasources

No env vars exposed. The orchestrator runs a setup script in the workspace before the agent starts:

```python
if ds["type"] == "repository":
    creds = ds.get("credentials", {})
    repo_url = ds["connection_url"]
    branch = ds.get("default_branch")
    name = slugify(ds["name"])

    if creds.get("auth_method") == "ssh":
        # Write SSH key, configure per-host identity
        setup_steps.append(write_ssh_key(creds["ssh_key"], name))
        setup_steps.append(configure_ssh_host(repo_url, name))
    elif creds.get("auth_method") == "token":
        # Use credential helper — keeps token out of .git/config
        setup_steps.append(configure_git_credential_helper(repo_url, creds["token"]))

    # Clone into workspace
    clone_cmd = f"git clone {repo_url} repos/{name}/"
    if branch:
        clone_cmd += f" --branch {branch}"
    setup_steps.append(clone_cmd)
```

The agent finds a ready-to-use repo at `repos/{name}/` with git pre-authenticated.

### Managed connectors (dual-mode)

**Read-write** — orchestrator injects env vars (CLI tools pre-installed in workspace image):
- PostgreSQL: `PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`, `PGPORT`
- Neo4j: `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`
- MongoDB: `MONGOSH_URI`
- WebDAV: `WEBDAV_URL`, `WEBDAV_USERNAME`, `WEBDAV_PASSWORD`

**Read-only** — no env vars. Only read-only custom tools registered:
- PostgreSQL: `sql_query`, `sql_schema`
- Neo4j: `execute_cypher_query` (read-only mode)
- MongoDB: `mongo_query`, `mongo_schema`, `mongo_aggregate`
- WebDAV: read-only file tools

```python
if ds["type"] in ("postgresql", "neo4j", "mongodb"):
    if read_only:
        # Tools hold credentials internally, agent has no direct access
        register_read_tools(ds["type"], ds)
    else:
        # Agent uses CLI — env vars injected, no custom tools
        inject_typed_env_vars(ds["type"], ds)
elif ds["type"] == "webdav":
    # Always tools-based (no good CLI)
    register_webdav_tools(ds, read_only=read_only)
    inject_typed_env_vars("webdav", ds)
```

## Read-Only Enforcement Matrix

| Datasource Type | Read-Only Toggle | Read-Write Mode | Read-Only Mode |
|----------------|-----------------|-----------------|----------------|
| Generic | Not available | CLI via env vars | N/A — use restricted accounts |
| Repository | Not available | `git` CLI (pre-authenticated) | N/A — use read-only deploy tokens |
| PostgreSQL | Project-level | `psql` CLI via env vars | `sql_query` + `sql_schema` tools only |
| Neo4j | Project-level | `cypher-shell` CLI via env vars | `execute_cypher_query` (read) tool only |
| MongoDB | Project-level | `mongosh` CLI via env vars | `mongo_query` + `mongo_schema` tools only |
| WebDAV | Project-level | Custom tools (read+write) | Custom tools (read only) |

**Key principle:** Read-only mode means **no credentials in the workspace** and **no CLI access**. The agent can only interact through gated read-only tools that hold credentials internally. This is stronger than most MCP database servers, which parse SQL to reject writes but still hold credentials in the agent's process.

## Datasource Discovery

### Datasource index (always injected)

To ensure the agent always knows what datasources are available — even before KB retrieval fires — a compact **datasource index** is injected into the workspace context (similar to `workspace.md`):

```markdown
## Available Datasources
- **Production DB** (postgresql, read-write) — `psql` via env vars
- **Analytics Repo** (repository) — cloned at `./repos/analytics/`
- **Staging Neo4j** (neo4j, read-only) — `execute_cypher_query` tool
- **External API** (generic) — `curl` via `$API_BASE_URL`
```

This is a lightweight list (names + one-line descriptions only). Full details — CLI examples, env var names, tool lists — are retrieved on demand from the knowledge base.

### KB entries (retrieved via hybrid search)

Full datasource details stored as KB entries in Neo4j + pgvector. Retrieved automatically when the agent's current task context matches (e.g. todo mentions "database" → datasource notes surface via hybrid search).

**Content varies by type and mode:**

**Generic — user-authored context:**
```markdown
## Datasource: {name}
{description}

### Connection
- **URL:** {connection_url} (if provided)
- **CLI:** {cli_hint} (if provided)

### Environment Variables
- `{KEY_1}` — available in workspace
- `{KEY_2}` — available in workspace
```

**Repository — auto-generated:**
```markdown
## Repository: {name}
{description}

### Location
Cloned to `./repos/{slug}/` — git is pre-authenticated.

### Usage
Use standard git commands:
- `cd repos/{slug} && git status`
- `git pull`, `git commit`, `git push`
- No login or credential setup required.
- Default branch: `{default_branch}` (if set)
```

**Managed connector (read-write) — auto-generated:**
```markdown
## Datasource: {name}
**Type:** {type} | **Access:** full (CLI)
{description}

### Connection
Use `{cli_tool}` to connect — credentials are pre-configured via environment variables.

### Environment Variables
- `PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` — PostgreSQL connection

### Examples
- `psql -c "SELECT * FROM users LIMIT 10"`
- `psql -c "CREATE TABLE ..."`
```

**Managed connector (read-only) — auto-generated:**
```markdown
## Datasource: {name}
**Type:** {type} | **Access:** read-only (tools)
{description}

### Available Tools
- `sql_query` — execute SELECT queries
- `sql_schema` — inspect tables, columns, types, constraints

No CLI access or write operations available.
```

**WebDAV — always tools:**
```markdown
## Datasource: {name}
**Type:** webdav | **Access:** {read_only ? "read-only" : "read-write"}
{description}

### Available Tools
- `webdav_list` — list files and directories
- `webdav_read` — read file contents
{if !read_only:}
- `webdav_write` — write/upload files
- `webdav_delete` — delete files
```

### Retrieval messages

Optimized for hybrid search discovery:
```python
# Generic
[f"{name} connection", f"How to access {name}", "available datasources"]

# Repository
[f"{name} repository", f"git repo {name}", f"How to access {name} code"]

# Managed connectors
[f"{name} database", f"{type} access", f"How to query {name}"]
```

## Security Considerations

### Credential leakage prevention

Credentials injected via env vars are accessible to the agent process. Mitigation layers:

1. **KB entries never contain credential values** — only env var names and CLI hints. The agent knows `$DATABASE_URL` exists but not its value.
2. **Repository credentials are transparent** — git credential helpers and SSH configs handle auth without the agent constructing auth headers. Credentials stay out of `.git/config` (credential helper reads from a transient file).
3. **Read-only mode withholds credentials entirely** — for managed connectors in read-only mode, the agent has no env vars, no CLI access, and no way to reach the database except through the gated tools.
4. **Output scrubbing** (recommended) — a post-tool-call interceptor should scan tool outputs for credential-like patterns (API keys, connection strings, tokens) before they enter the LLM context. This prevents accidental echo-back.
5. **Shell command restrictions** — consider blocking commands that dump environment variables (`env`, `printenv`, `cat /proc/self/environ`) to prevent credential extraction via shell tools.

### Token scoping recommendations

For repository datasources, recommend users create scoped credentials:
- **GitHub:** Fine-grained PATs with repository-specific access
- **GitLab:** Project deploy tokens (read-only or read-write)
- **Gitea:** Application tokens scoped to specific repos

For managed connectors in read-write mode, recommend database accounts with minimal necessary permissions rather than admin credentials.

### Defense-in-depth for read-only

Read-only enforcement for managed connectors uses three layers:
1. **No credentials injected** — agent cannot construct a connection
2. **No CLI tools available** — `psql`/`cypher-shell`/`mongosh` not accessible
3. **Gated tools only** — read-only tools mediate all access with credentials held internally

This is stronger than single-layer approaches (SQL parsing, advisory flags, or connection-level `SET TRANSACTION READ ONLY`).

## UI Changes

### Datasource Management Page

**Type selector** organized by category:

**CLI-based (agent uses standard CLI tools):**
- Generic — "Connect any CLI-accessible resource (databases, APIs, etc.)"
- Repository — "Git repository, cloned and authenticated automatically"

**Managed connectors (CLI in read-write, gated tools in read-only):**
- PostgreSQL — "CLI or read-only query tools via `psql`"
- Neo4j — "CLI or read-only query tools via `cypher-shell`"
- MongoDB — "CLI or read-only query tools via `mongosh`"
- WebDAV — "File storage access (tools only, no CLI)"

**Generic form:**
```
Name:           [________________________]
Description:    [________________________]
                [________________________]
                [________________________]

Connection URL: [________________________] (optional)
CLI Hint:       [________________________] (optional, e.g. "psql $DATABASE_URL")

Environment Variables:
  [DATABASE_URL    ] = [••••••••••••••••] [x]
  [DB_SCHEMA       ] = [public          ] [x]
  [+] Add variable

  Note: Credentials are injected as environment variables into the
  agent workspace. Read-only access cannot be enforced — use a
  database account with restricted permissions if needed.
```

**Repository form:**
```
Name:             [________________________]
Description:      [________________________] (optional)
Repository URL:   [________________________] (e.g. https://github.com/org/repo.git)
Default Branch:   [________________________] (optional, e.g. main)

Auth Method:      ( ) HTTPS Token   ( ) SSH Key

  [Token:         [••••••••••••••••]      ]  <- if HTTPS
  [SSH Key:       [                     ] ]  <- if SSH
  [               [                     ] ]
  [               [_____________________] ]

  The repository will be cloned into the workspace and git
  credentials configured automatically. The agent uses standard
  git commands — no credential setup required.
```

**Typed datasource forms** — unchanged, but `read_only` checkbox removed (moved to project-level linking).

### Project Detail — Data Sources Tab

When linking a datasource to a project:
- **Generic datasource:** No read-only toggle. Info: "Access level is determined by the credentials configured on the datasource."
- **Repository datasource:** No read-only toggle. Info: "Use a read-only deploy token for restricted access."
- **Managed connector (postgresql, neo4j, mongodb):** Read-only toggle available. Explains the mode switch:
  - Off: "Full access — agent uses `{cli_tool}` with credentials in env vars"
  - On: "Read-only — agent gets query tools only, no credentials or CLI access"
- **WebDAV:** Read-only toggle available. Label: "Restrict to read-only tools"

## Migration Path

1. **Schema changes** — add `cli_hint`, `default_branch` columns; make `connection_url` nullable; add `generic` and `repository` types
2. **Workspace image** — pre-install CLI tools (`psql`, `cypher-shell`, `mongosh`) in the agent container image
3. **Backend: generic datasource** — env_vars credential handling, env var injection at job start
4. **Backend: repository datasource** — clone + credential setup script at job start (workspace init)
5. **Backend: managed connector dual-mode** — read-write injects env vars (no tools), read-only registers tools (no env vars)
6. **Backend: KB sync** — different content templates per type and mode; datasource index generation
7. **Backend: read_only** — remove from `datasources` table, enforce only via `project_datasources` junction
8. **Cockpit: datasource forms** — generic form with env var editor, repository form with auth method selector
9. **Cockpit: project detail** — read-only toggle only for managed connectors, info text for generic/repository

Existing typed datasource tools remain unchanged — they continue to work for read-only mode. The generic and repository types are additive.

## Future Work

- **Credential rotation / vault integration** — short-lived tokens via HashiCorp Vault or similar, per-job scoped credentials
- **Output scrubbing** — regex-based credential detection in tool outputs before they enter LLM context
- **Per-job scoped git tokens** — create/revoke tokens for each job instead of shared credentials
- **Shell command restrictions** — block `env`/`printenv`/`/proc/self/environ` access in agent shell tools
- **Connection testing for generic/repository** — protocol-specific validation where possible
- **Repo caching** — cache cloned repos at the workspace image level for frequently used repositories (similar to Devin's VM snapshot model)

## Non-Goals

- Removing existing typed datasource tools (they provide read-only enforcement)
- Building new CLI wrapper tools (the point is to use standard CLIs)
- Custom git tools (git CLI is comprehensive, replicating it is unnecessary)
