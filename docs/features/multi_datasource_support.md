# Multi-Datasource Support: Removing the One-Per-Type Constraint

## Problem

The system enforces a "one datasource per type" rule at three layers, preventing real-world configurations where agents need multiple datasources of the same type (e.g., two PostgreSQL databases, three git repositories).

### Where the constraint lives

| Layer | Mechanism | Location | Effect |
|-------|-----------|----------|--------|
| Database | `uq_datasource_type_job` unique index | `schema.sql:694` | 409 Conflict on duplicate type within same scope |
| Query resolution | `DISTINCT ON (type)` in `resolve_datasources_for_job` | `postgres.py:2542` | Silently drops all but one datasource per type |
| Orchestrator | `next(d for d in datasources if d["type"] == ds_type)` | `main.py:7142` | Tool override logic only considers first datasource per type |
| Agent env vars | `os.environ["PGHOST"]` etc. (one value per key) | `datasource_setup.py:120` | Only one set of credentials per type in process env |
| Agent tools | `ToolContext.datasources[type]` dict keyed by type | `context.py:149` | Only one connection object per type |

### Why it was added

Originally all managed datasources used tool-based access (`sql_query`, `cypher_query`, etc.). Tools were registered once per type and routed to a single connection object in `ToolContext.datasources[type]`. Two PostgreSQL datasources meant two connections behind one `sql_query` tool with no way to disambiguate.

### Why it needs to go

1. **Real projects need multiple repos.** A developer agent working on a frontend + backend needs both cloned. A scholar researching multiple codebases needs all of them.
2. **Real projects need multiple databases.** Analytics in one PostgreSQL instance, application data in another. Production MongoDB + staging MongoDB for comparison.
3. **The pivot to CLI-based access solves the disambiguation problem.** CLI tools accept connection targets as arguments — `PGSERVICE=x psql`, `mongosh $URI`, etc.
4. **The project datasource model (N:M) already assumes multiples.** The `project_datasources` junction table links many datasources to a project, but `DISTINCT ON (type)` collapses them at resolution time.

---

## Industry Context

### How others solve multi-source disambiguation

| System | Pattern | Mechanism |
|--------|---------|-----------|
| **MCP ecosystem** | Per-connection tool suffixes | [FreePeak/db-mcp-server](https://github.com/FreePeak/db-mcp-server) generates `query_{conn_id}`, `schema_{conn_id}` per connection. [FastMCP](https://deepwiki.com/jlowin/fastmcp/6.3-multi-server-configuration-with-mcpconfig) supports `prefix` per server. Tool name collision is a [known gap](https://github.com/orgs/modelcontextprotocol/discussions/291) and [security risk](https://vulnerablemcp.info/vuln/tool-name-collisions.html) (malicious server can shadow tools). |
| **MCP (alt)** | Connection-as-parameter | [pg-mcp-server](https://github.com/stuzero/pg-mcp-server): single tool, `conn_id` parameter returned by a `Connect` tool. No tool proliferation — connection is a runtime param, not a separate server. |
| **LangChain** | Separate toolkits per DB | No native multi-DB agent ([issue #7581](https://github.com/langchain-ai/langchain/issues/7581)). Workaround: separate `SQLDatabaseToolkit` per database, renamed tools, wrapped as sub-agents. Also: [federated query](https://medium.com/@official.indrajit.kar/building-a-scalable-text-to-sql-agentic-system-with-langchain-vector-db-and-multi-db-federated-5656e7115451) with vector DB as semantic router. |
| **Devin** | Pre-cloned workspace + knowledge | Repos at `$HOME/repos/` in VM. Auto-indexes repos into "DeepWiki" knowledge. Databases via CLI + env vars. Manual Knowledge entries for context. |
| **SWE-agent / OpenHands** | Sandbox per repo | Single repo per container typical. Multi-repo for batch ops spawns separate agent instances, not multi-repo-per-agent. |
| **Terraform** | Named provider aliases | `provider "aws" { alias = "us_east" }` + explicit `provider = aws.us_east` binding. Mature, well-tested. Cannot be dynamic. |

### Anthropic's guidance on tool naming

From [Anthropic's engineering blog on writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents):

- **Namespace with prefixes**: Group related tools under common prefixes (`asana_search`, `jira_search`). Prefix vs suffix positioning produces measurable differences in eval performance — test both.
- **Consolidate over proliferate**: Rather than `query_postgres_prod` and `query_postgres_staging` as separate tools, consider a single `query_database` tool with a `datasource_name` parameter — reduces the LLM's decision surface.
- **Descriptions are critical**: Even minor refinements to tool descriptions yielded "dramatic performance improvements."

This creates a design tension: per-datasource tools (more tools, unambiguous names) vs. parameterized tools (fewer tools, LLM must pick the right parameter). Both are valid — our CLI approach sidesteps this entirely since `run_command` is already a single tool with the datasource name embedded in the command.

**Key insight**: The emerging standard is **per-instance naming** — whether via tool name suffixes, env var prefixes, config file sections, or provider aliases. Our CLI-based approach aligns naturally with this by using named config profiles (`PGSERVICE`, `MONGO_{SLUG}_URI`). The connection-as-parameter pattern is worth considering for Phase 3's read-only tools.

---

## Current Behavior (per type)

### Repositories

Agent-side setup (`datasource_setup.py:203-263`) **already handles multiples correctly**:
- Each repo gets its own SSH key at `~/.ssh/repo_{slug}`
- SSH config is appended (not overwritten) so multiple host entries coexist
- Token auth appends to `~/.git-credentials` (multiple entries work)
- Repos clone to `repos/{slug}/` (unique per datasource name)

**Only the DB index and DISTINCT ON query block this from working.**

**Known limitation**: SSH config maps by hostname. Two repos on the same host with different deploy keys conflict (see SSH strategy section for the fix).

### PostgreSQL (CLI mode)

`inject_typed_env_vars` sets process-level `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`. A second PostgreSQL datasource would overwrite the first.

### Neo4j / MongoDB (CLI mode)

Same pattern: `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` or `MONGOSH_URI` are single-value globals.

### Read-only tool mode

`ToolContext.datasources` is `Dict[str, Any]` keyed by type. Only one connection object per type. Additionally, `_build_datasource_tool_override` (`main.py:7142`) uses `next()` to grab only the first datasource of a given type, ignoring any others.

### Generic

Each generic datasource has its own `env_vars` dict. Overlapping key names would collide, but in practice users name them differently (`DB1_URL`, `DB2_URL`). No structural issue.

### Datasource discovery by the LLM

Datasource info reaches the LLM through three channels:
1. **System prompt**: A Jinja2 conditional block `{% if cli_datasources %}` injects CLI examples for each datasource type. Currently type-based, not instance-based.
2. **workspace.md**: `inject_datasource_index()` appends a compact datasource list. For **job agents**, the agent must explicitly call `read_file("workspace.md")` to see it. For **persistent sessions**, workspace.md is auto-injected as a transient system message before each LLM call.
3. **Tool list**: Datasource-specific tools (sql_query, cypher_query) appear in the agent's tool schema when configured. Tool descriptions don't reference specific connection info.

No automatic knowledge base entries are created for datasources.

---

## Access Model

Datasources are **private by default**. Visibility and usage follow these rules:

| Action | Who can do it |
|--------|--------------|
| View in datasource panel | Owner, or anyone if `is_global = true` |
| Use on a job | Owner, or anyone if global, or project member if datasource is linked to the job's project |
| Link/unlink to a project | Owner only |
| Edit / delete | Owner only |

### Schema changes

- Add `is_global BOOLEAN NOT NULL DEFAULT FALSE` to `datasources` table
- `created_by` already exists — used for ownership checks

### Resolution for job dispatch

When resolving datasources for a job, return datasources where:
```sql
WHERE d.created_by = $current_user
   OR d.is_global = true
   OR (pd.project_id = ANY($project_ids) AND EXISTS (
       SELECT 1 FROM project_members pm
       WHERE pm.project_id = pd.project_id AND pm.user_id = $current_user
   ))
```

### API endpoint filtering

- **Datasource panel** (`GET /api/datasources`): `WHERE created_by = $user OR is_global = true`
- **Job datasource picker**: Same as panel + datasources linked to the job's project (even if not owned or global)
- **Link to project** (`POST /api/projects/{id}/datasources`): `WHERE created_by = $user` — only owner can link

### Project sharing flow

1. User A creates a datasource "Production DB" (private, `is_global = false`)
2. User A links it to project "Backend" — only A can do this
3. User B is a member of project "Backend"
4. User B creates a job in project "Backend" — "Production DB" appears in the datasource picker
5. User B cannot see "Production DB" in their datasource panel, cannot edit it, cannot link it to other projects

---

## Proposed Solutions

### Layer 1: Database Constraint

**Drop `uq_datasource_type_job`.**

Datasources are always global entities — there are no job-scoped datasources. The index prevents creating two datasources of the same type, which is the exact constraint we're removing. Replace with a softer uniqueness on `(name, type)` to catch accidental exact duplicates.

```sql
DROP INDEX IF EXISTS uq_datasource_type_job;

-- Prevent accidental exact duplicates (same name + type per owner)
CREATE UNIQUE INDEX IF NOT EXISTS uq_datasource_name_type_owner ON datasources (name, type, created_by);
```

### Layer 2: Resolution Query

**Replace `DISTINCT ON (type)` with a query that returns all matching datasources.**

Since datasources are always global, resolution simplifies to: get all datasources linked to the job's project(s), plus any unlinked global datasources.

Current query (`postgres.py:2542`):
```sql
SELECT DISTINCT ON (d.type) ...
ORDER BY d.type,
         CASE WHEN d.job_id IS NOT NULL THEN 0
              WHEN pd.project_id IS NOT NULL THEN 1
              ELSE 2
         END
```

New query — returns all matching datasources without collapsing:
```sql
SELECT DISTINCT d.id, d.name, d.type, d.connection_url, d.credentials, ...
FROM datasources d
LEFT JOIN project_datasources pd ON pd.datasource_id = d.id
WHERE pd.project_id = ANY($1)  -- project-linked
   OR NOT EXISTS (SELECT 1 FROM project_datasources pd2 WHERE pd2.datasource_id = d.id)  -- unlinked global
ORDER BY d.type, d.name
```

**Simplification**: No priority ordering needed. A datasource is either linked to a project or it's a global unlinked one. `$1` is an array of project IDs associated with the job.

### Layer 3: Orchestrator Tool Override Logic

**Update `_build_datasource_tool_override` (`main.py:7139-7156`) to iterate all datasources, not just the first per type.**

Current code:
```python
ds = next(d for d in datasources if d["type"] == ds_type)  # BUG: only first
```

New logic must decide tool categories based on all datasources of that type. If any is read-write (CLI mode), strip tools for that category. If all are read-only, register read-only tools. Mixed scenarios (some read-only, some read-write of same type) need a policy — simplest is: if any is CLI-mode, all are CLI-mode for that type.

### Layer 4: Agent-Side Multi-Source Setup

This is where the real work is. Each datasource type needs a strategy for coexisting with siblings.

---

#### Repositories: `core.sshCommand` per-repo (improved approach)

The current SSH config approach (`~/.ssh/config` with `Host` entries) breaks when two repos share a hostname (e.g., two GitHub repos with different deploy keys). **Git's `core.sshCommand` config solves this cleanly.**

After cloning, set a per-repo git config:
```bash
# During setup
GIT_SSH_COMMAND="ssh -i ~/.ssh/repo_frontend -o IdentitiesOnly=yes" \
  git clone git@github.com:org/frontend.git repos/frontend/

cd repos/frontend/
git config core.sshCommand "ssh -i ~/.ssh/repo_frontend -o IdentitiesOnly=yes"
```

The `core.sshCommand` setting is stored in `repos/frontend/.git/config` and **persists for all subsequent git operations** (pull, push, fetch) without any env var or SSH config needed. Each repo uses its own key automatically.

**Critical**: Always include `-o IdentitiesOnly=yes` to prevent the SSH agent from offering other keys first.

**Why this is better than SSH host aliases**:
- No URL rewriting needed — the agent sees the real repo URL
- No global `~/.ssh/config` manipulation — avoids conflicts between repos
- Works for any number of repos on the same host
- Standard git feature, supported since Git 2.10+

**Token auth**: No change needed. `~/.git-credentials` already supports multiple entries and the credential helper matches by hostname. Multiple repos on the same host sharing a token just work.

**Implementation change in `setup_repository_datasource`**:
```python
# Replace SSH config append with per-repo core.sshCommand
if auth_method == "ssh":
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    key_file = os.path.join(ssh_dir, f"repo_{name}")
    with open(key_file, "w") as f:
        f.write(creds.get("ssh_key", ""))
    os.chmod(key_file, 0o600)

    # Clone with explicit SSH command
    clone_env = {**os.environ, "GIT_SSH_COMMAND": f"ssh -i {key_file} -o IdentitiesOnly=yes"}
    subprocess.run(["git", "clone", repo_url, clone_path], env=clone_env, ...)

    # Set persistent per-repo SSH command (survives without env var)
    subprocess.run(
        ["git", "config", "core.sshCommand", f"ssh -i {key_file} -o IdentitiesOnly=yes"],
        cwd=clone_path,
    )
```

#### PostgreSQL: `pg_service.conf`

PostgreSQL's built-in named connection profiles. **Supported by all libpq-based tools** (`psql`, `pg_dump`, `pg_restore`, `pgbench`, `createdb`, `dropdb`, and any application using libpq) because the service file is resolved by libpq itself.

```ini
# ~/.pg_service.conf
[analytics_db]
host=analytics.example.com
port=5432
dbname=analytics
user=reader
password=secret123

[app_db]
host=prod.example.com
port=5432
dbname=myapp
user=admin
password=hunter2
```

Agent usage:
```bash
PGSERVICE=analytics_db psql -c "SELECT count(*) FROM events"
PGSERVICE=app_db psql -c "SELECT * FROM users LIMIT 10"
PGSERVICE=app_db pg_dump --schema-only  # works for pg_dump too
```

**Implementation**:
```python
def inject_postgresql_service(datasources: list[dict], slug_fn) -> None:
    """Generate ~/.pg_service.conf entries for all PostgreSQL datasources."""
    service_file = os.path.expanduser("~/.pg_service.conf")
    os.environ["PGSERVICEFILE"] = service_file

    entries = []
    for ds in datasources:
        slug = slug_fn(ds["name"])
        parsed = urlparse(ds["connection_url"])
        creds = ds.get("credentials") or {}
        entries.append(f"[{slug}]")
        if parsed.hostname: entries.append(f"host={parsed.hostname}")
        if parsed.port: entries.append(f"port={parsed.port}")
        if parsed.username: entries.append(f"user={parsed.username}")
        password = parsed.password or creds.get("password", "")
        if password: entries.append(f"password={password}")
        db_name = parsed.path.lstrip("/").split("?")[0]
        if db_name: entries.append(f"dbname={db_name}")
        entries.append("")

    with open(service_file, "a") as f:
        f.write("\n".join(entries))

    # Backward compat: also set legacy env vars when only one PG datasource
    if len(datasources) == 1:
        inject_typed_env_vars("postgresql", datasources[0])  # existing function
```

**Complementary: `.pgpass` for password separation**

`pg_service.conf` can be shared across teams (no secrets). Passwords go in `~/.pgpass` (must be `chmod 0600`):
```
# hostname:port:database:username:password
analytics-db.internal:5432:analytics:reader:s3cret
app-db.internal:5432:myapp:admin:hunter2
```

For our agent workspaces, keeping passwords in `pg_service.conf` directly is simpler (the whole workspace is ephemeral and single-tenant). `.pgpass` separation is more relevant for shared environments.

**Container gotcha**: Always set `PGSERVICEFILE` explicitly — in containers `~/.pg_service.conf` may not resolve correctly if `HOME` is unset or points to a non-standard location.

**References**:
- [PostgreSQL docs: Connection Service File](https://www.postgresql.org/docs/current/libpq-pgservice.html)
- [PostgreSQL docs: The Password File](https://www.postgresql.org/docs/current/libpq-pgpass.html)
- [Cybertec: pg_service.conf — The Forgotten Config File](https://www.cybertec-postgresql.com/en/pg_service-conf-the-forgotten-config-file/)

#### MongoDB: Per-Datasource URI Environment Variables

**mongosh has no built-in connection profiles** — no equivalent to `pg_service.conf`. Confirmed by MongoDB docs: the config file (`~/.mongosh/mongosh_global_config.yaml`) only controls shell settings (editor, historyLength), not connection profiles.

Best approach: per-datasource env vars with a naming convention.

```bash
# Set by agent setup
MONGO_ANALYTICS_URI="mongodb://user:pass@analytics-host:27017/analytics"
MONGO_APP_URI="mongodb://user:pass@app-host:27017/myapp"

# Agent usage
mongosh "$MONGO_ANALYTICS_URI" --eval "db.events.countDocuments()"
mongosh "$MONGO_APP_URI" --eval "db.users.find().limit(5)"
```

**Implementation**:
- Set `MONGO_{SLUG}_URI` per datasource
- If only one MongoDB datasource: also set `MONGOSH_URI` for backward compat

#### Neo4j: Per-Datasource Environment Variables

`cypher-shell` accepts `--address`, `--username`, `--password` as arguments but has no profile file.

```bash
# Set by agent setup
NEO4J_KNOWLEDGE_URI="bolt://knowledge-host:7687"
NEO4J_KNOWLEDGE_USERNAME="neo4j"
NEO4J_KNOWLEDGE_PASSWORD="secret"

# Agent usage
cypher-shell --address "$NEO4J_KNOWLEDGE_URI" \
  --username "$NEO4J_KNOWLEDGE_USERNAME" --password "$NEO4J_KNOWLEDGE_PASSWORD" \
  --format plain "MATCH (n) RETURN labels(n), count(*)"
```

**Implementation**:
- Set `NEO4J_{SLUG}_URI`, `NEO4J_{SLUG}_USERNAME`, `NEO4J_{SLUG}_PASSWORD` per datasource
- If only one Neo4j: also set `NEO4J_URI`/`NEO4J_USERNAME`/`NEO4J_PASSWORD` for backward compat

#### Generic Datasources

Already work with multiples. Each datasource has its own `credentials.env_vars` dict with user-chosen key names. No structural change needed.

---

## Read-Only Tool Mode (Phase 3)

`ToolContext.datasources` keyed by type currently supports one connection per type. For multiple read-only datasources of the same type, we need a disambiguation strategy.

### Options considered

| Option | Description | Pros | Cons |
|--------|-------------|------|------|
| **A. Key by name** | `ToolContext.datasources["analytics_pg"]` | Simple | All tool factories need updating |
| **B. Key by type with list** | `ToolContext.datasources["postgresql"] = [conn1, conn2]` | Minimal API change | Tools need a selector param |
| **C. Per-datasource tools** | `sql_query_analytics(...)`, `sql_query_app(...)` | Unambiguous for LLM | Increases tool count |
| **D. Connection-as-parameter** | Single `sql_query(datasource, query)` | Fewer tools, consolidates | LLM must remember datasource names |
| **E. Defer** | Keep one-per-type for read-only | No work | Limits read-only multi-source |

### Recommendation: Option C or D depending on datasource count

**Option C (per-datasource tools)** aligns with the MCP ecosystem's standard and is best when datasource count is low (2-3):

This aligns with the MCP ecosystem's emerging standard:
- [FreePeak/db-mcp-server](https://github.com/FreePeak/db-mcp-server) generates `query_{connection_id}`, `schema_{connection_id}` per database
- [FastMCP](https://deepwiki.com/jlowin/fastmcp/6.3-multi-server-configuration-with-mcpconfig) auto-prefixes tools per server
- LangChain users rename tools per database to avoid collisions

**Implementation sketch**:
```python
# In tool factory (e.g., src/tools/sql/postgresql.py)
def create_postgresql_tools(context: ToolContext, ds_name: str, conn) -> list:
    suffix = f"_{slugify(ds_name)}" if ds_name else ""

    @tool(name=f"sql_query{suffix}")
    def sql_query(query: str) -> str:
        """Execute a read-only SQL query against {ds_name}."""
        ...
```

**Option D (connection-as-parameter)** follows [Anthropic's "consolidate over proliferate" guidance](https://www.anthropic.com/engineering/writing-tools-for-agents) and is better when many datasources would create tool bloat:
```python
@tool
def sql_query(datasource_name: str, query: str) -> str:
    """Execute a read-only SQL query. Available datasources: {available_list}."""
    conn = context.get_datasource_by_name(datasource_name)
    ...
```

This mirrors [pg-mcp-server](https://github.com/stuzero/pg-mcp-server)'s approach of connection-as-runtime-parameter. Fewer tools but the LLM must pick the right datasource name. Including the available list in the tool description mitigates this.

**Decision rule**: Use Option C when <= 3 datasources of a type (tool count stays manageable). Use Option D when > 3 (avoids tool explosion). The orchestrator can decide at dispatch time based on the actual datasource count.

**When to implement**: Defer to Phase 3. CLI mode (Phases 1-2) covers the common cases. Read-only multi-source is a less common scenario — most users who need multiple databases want write access (CLI mode).

---

## Workspace Index and LLM Discovery

### Current flow

1. `inject_datasource_index()` (`datasource_setup.py:266`) appends a "## Available Datasources" section to `workspace.md`
2. System prompt includes a `{% if cli_datasources %}` Jinja2 block with CLI examples (in all prompt variants: `systemprompt.txt`, `systemprompt_interactive.txt`, `systemprompt_gpt_5.txt`, `systemprompt_gpt_oss.txt`, `systemprompt_codex_spark.txt`, `systemprompt_minimax.txt`)
3. **Job agents**: workspace.md is NOT transient-injected into messages (despite a misleading comment at `graph.py:529`). The agent must explicitly call `read_file("workspace.md")` or rely on the system prompt's `<datasource_access>` block.
4. **Persistent sessions**: workspace.md IS injected as transient `SystemMessage` before each LLM call (`persistent_graph.py:396-407`), refreshed on every turn.

**Note**: This means for job agents, the system prompt's `{% if cli_datasources %}` block is the primary datasource discovery channel. It currently uses `has_cli_datasource("postgresql")` etc. (type-based, not instance-based). This must be updated for multi-source.

### Required changes

**`inject_datasource_index`** must list every datasource with its specific named access method:

```markdown
## Available Datasources

### Repositories
- **Frontend App** — cloned at `./repos/frontend-app/`, git pre-authenticated
- **Backend API** — cloned at `./repos/backend-api/`, git pre-authenticated
- **Shared Libs** — cloned at `./repos/shared-libs/`, read-only (no push credentials)

### Databases
- **Analytics DB** (postgresql, read-write):
  Use `PGSERVICE=analytics_db psql` — credentials pre-configured.
- **App DB** (postgresql, read-write):
  Use `PGSERVICE=app_db psql` — credentials pre-configured.
- **Event Store** (mongodb, read-write):
  Use `mongosh "$MONGO_EVENTS_URI"` — credentials pre-configured.

### Other
- **Cloud Storage** (webdav, read-only) — `webdav_list`, `webdav_read` tools
- **External API** (generic) — `curl` via `$API_BASE_URL`, `$API_TOKEN`
```

**System prompt `{% if cli_datasources %}` block** needs updating to reference named connections instead of assuming one-per-type. It should dynamically list the available service names / env vars.

**Workspace index size**: With many datasources, keep entries concise (name + one-liner). Expanded CLI examples only for the first datasource of each type; subsequent ones reference the same pattern. Cap at ~20 lines to avoid bloating workspace.md.

---

## SSH Key Strategy for Multiple Repositories

### Recommended: `core.sshCommand` with per-repo deploy keys

Each repo datasource has its own SSH key. After cloning, `git config core.sshCommand` is set per-repo. No SSH config file manipulation needed.

**Advantages over SSH host aliases**:
- No URL rewriting — agent sees real repo URLs
- No `~/.ssh/config` conflicts between repos
- Works for any number of repos on the same host
- Per-repo `.git/config` is self-contained

### Alternative: HTTPS tokens (simplest for same-host repos)

A single personal access token covers all repos on one host. Stored via `git-credential-store`. All repos authenticate automatically.

**Best for**: Users with many repos on one host (GitHub org, Gitea instance). One credential, zero per-repo setup.

**Trade-off**: Token scope may be broader than needed. But GitHub fine-grained PATs and GitLab project/group deploy tokens can limit scope.

**Per-repo token matching**: By default, `git-credential-store` matches by hostname only. For different tokens per repo on the same host, enable path-based matching:
```bash
git config --global credential.github.com.useHttpPath true
```
Then `~/.git-credentials` can have separate entries:
```
https://token-a:x-oauth-basic@github.com/org/repo-a.git
https://token-b:x-oauth-basic@github.com/org/repo-b.git
```

### Alternative: `GIT_CONFIG_COUNT` (container-native, Git 2.31+)

Inject git config via environment variables without touching any config file:
```bash
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=core.sshCommand
export GIT_CONFIG_VALUE_0="ssh -i /workspace/.ssh/deploy_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
```

**Pros**: No config files to write. Set once in container env, applies to all git commands. Doesn't persist to disk (good for security).
**Cons**: Process-wide — doesn't work if a single process needs different keys for different repos concurrently. For that, use per-repo `core.sshCommand` (our primary approach).

**Best for**: Single-repo-per-container scenarios or as the clone-time transport before setting per-repo `core.sshCommand`.

### Alternative: Shared agent key

One SSH key registered as a machine user or collaborator across repos. Simplifies credential management but has broader blast radius.

**Implementation**: Could be a global agent setting rather than a per-datasource credential. Not a blocker for Phase 1.

### Alternative: GitHub Apps (recommended for GitHub-heavy workflows)

Short-lived installation tokens (1-hour expiry), fine-grained per-repo permissions, no user seat consumed. Clone via:
```bash
git clone https://x-access-token:${INSTALLATION_TOKEN}@github.com/org/repo.git
```

**Pros**: Tokens auto-expire (limits blast radius), scoped to specific repos, cross-repo access without SSH.
**Cons**: Higher setup complexity. Token refresh needed for long-running jobs. Not applicable to Gitea/GitLab.

---

## Migration Plan

### Phase 1: Unblock multiples (database + query + orchestrator)

| Change | File | What |
|--------|------|------|
| Drop unique index | `schema.sql` | Remove `uq_datasource_type_job`, add `uq_datasource_name_type_owner` |
| Fix resolution query | `postgres.py` | Replace `DISTINCT ON (type)` with `DISTINCT ON (id)` |
| Fix tool override | `main.py:7139-7156` | Iterate all datasources per type, not just `next()` |
| Fix payload builder | `main.py:7179` | Ensure `_build_datasources_payload` passes all datasources through |

**Result**: Multiple datasources of same type can be created, stored, resolved, and sent to agent.

### Phase 2: Agent multi-source setup

| Change | File | What |
|--------|------|------|
| PostgreSQL service file | `datasource_setup.py` | Generate `~/.pg_service.conf` entries instead of global env vars |
| MongoDB named URIs | `datasource_setup.py` | Set `MONGO_{SLUG}_URI` per datasource |
| Neo4j named connections | `datasource_setup.py` | Set `NEO4J_{SLUG}_URI` etc. per datasource |
| Repo SSH fix | `datasource_setup.py` | Replace SSH config append with `core.sshCommand` per-repo |
| Workspace index | `datasource_setup.py` | Update `inject_datasource_index` for multi-source format |
| System prompt | `config/prompts/` | Update `{% if cli_datasources %}` for named connections |
| Backward compat | `datasource_setup.py` | Set legacy env vars when only one datasource of a type exists |

**Result**: Agent can access multiple datasources of the same type via named connections.

### Phase 3: Read-only multi-source tools (deferred)

| Change | File | What |
|--------|------|------|
| Per-datasource tools | `src/tools/sql/`, `src/tools/graph/`, `src/tools/mongodb/` | Generate `sql_query_{slug}` etc. per datasource |
| ToolContext refactor | `src/tools/context.py` | Key by datasource name or support lists |
| Tool override update | `main.py` | Generate per-datasource tool lists in config_override |

**Result**: Full multi-source support in both CLI and tool modes.

---

## Files to Modify (Phases 1-2)

| File | Lines | Changes |
|------|-------|---------|
| `orchestrator/database/schema.sql` | 690-697 | Drop `uq_datasource_type_job`, add `uq_datasource_name_type_owner`, add `is_global` column |
| `orchestrator/database/postgres.py` | 2511-2578 | Remove `DISTINCT ON (type)`, return all project-linked + unlinked global |
| `orchestrator/main.py` | 7137-7158 | Fix `_build_datasource_tool_override` to handle multiples |
| `orchestrator/main.py` | 7179-7230 | Verify `_build_datasources_payload` passes all through |
| `src/core/datasource_setup.py` | 18-90 | Refactor `process_datasources` for multi-source per type |
| `src/core/datasource_setup.py` | 115-142 | Replace `inject_typed_env_vars` with named connection generators |
| `src/core/datasource_setup.py` | 203-263 | Update `setup_repository_datasource` to use `core.sshCommand` |
| `src/core/datasource_setup.py` | 266-340 | Update `inject_datasource_index` for multi-source format |
| `config/prompts/systemprompt.txt` | cli_datasources block | Update for named connections |

---

## Resolved Questions

1. **Uniqueness constraint**: Replace type-based uniqueness with name+type+scope uniqueness. Two "postgresql" datasources are fine; two datasources named "Analytics DB" of type "postgresql" in the same scope are likely a mistake.

2. **SSH same-host conflict**: Solved by `core.sshCommand` per-repo config. No URL rewriting needed. Each repo's `.git/config` points to its own key file.

3. **Workspace index size**: Keep entries concise (one-liner per datasource). Expanded CLI examples only for first datasource of each type.

4. **Legacy env var cutover**: Keep both — legacy env vars set when only one datasource of a type exists (backward compat). Named connections always available regardless. No breaking change.

## Anti-Patterns to Avoid

Based on industry research:

1. **Flat tool lists without namespacing** — LLMs struggle to pick the right tool when names are ambiguous. Always namespace by datasource name.
2. **Relying on tool descriptions alone** to disambiguate identical tool names — models often ignore descriptions when names match. Use unique names.
3. **Dynamic/implicit source selection** — Terraform learned this: explicit binding (`provider = aws.east`) is always safer than inference. Our approach is explicit: `PGSERVICE=analytics_db psql`.
4. **SSH agent forwarding into agent containers** — gives the container access to ALL host keys, violates least privilege, and creates dependency on host state. Use per-repo deploy keys instead.
5. **Global env vars for multi-source** — `PGHOST` can only hold one value. Use config files (`pg_service.conf`) or namespaced env vars (`MONGO_{SLUG}_URI`) instead.
6. **Overwriting connection objects in a dict** — Current code at `datasource_setup.py:79` does `datasources_dict[ds_type] = conn` which silently drops the first connection when two of the same type exist.

## Resolved Decisions

4. **Generic datasource env var collisions**: The user specifies a prefix in the datasource UI. If two generic datasources define overlapping env var keys, the prefix is prepended to disambiguate (e.g., `DB1_HOST`, `DB2_HOST`). Collision detection happens at the orchestrator level.

5. **Tool count budget**: Deferred to Phase 3. CLI mode covers the common cases.

6. **Datasource scoping**: Datasources are **always global** — registered in the datasource panel. There are no job-scoped or project-scoped datasources. Jobs and projects **link** to global datasources via `project_datasources`. This simplifies the resolution query: no job-level vs project-level vs global priority ordering.
