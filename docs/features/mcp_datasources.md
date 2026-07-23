---
tags:
  - feature
  - mcp
  - datasources
  - tooling
aliases:
  - MCPs & Data Sources
  - user-added MCP servers
  - agent MCP client
related:
  - "[[datasource_redesign]]"
  - "[[mcp]]"
  - "[[mcp_oauth_bridge]]"
  - "[[application_tool_surface_baseline]]"
---

# MCP Servers as Datasources (User-Added MCPs)

Let every user add external MCP servers to their agents the same way they add
custom datasources. The datasources section becomes **"MCPs & Data Sources"**;
an MCP server is a datasource of type `mcp` — user-owned, project-linked,
credential-encrypted — whose tools are discovered at runtime and bound to the
agent alongside native tools.

## Problem

Users can self-serve datasources (generic, repository, managed connectors) but
have no way to give their agents MCP tools. Some integrations only exist as MCP
servers, and many are simply easier to consume as MCPs than to wrap manually.
Meanwhile the ecosystem's hosted-MCP catalog keeps growing (GitHub, Linear,
Notion, Sentry, and most SaaS vendors now ship official servers).

**Direction check:** SRW already has MCP docs — [[mcp]], [[mcp_overhaul]],
[[mcp_oauth_bridge]] — but all of them cover SRW *as an MCP server* (the
orchestrator's FastMCP surface consumed by Claude Code / Claude.ai). This
feature is the mirror image: the **worker agent as an MCP client**, consuming
external servers. Nothing in the codebase does this today — the LangGraph agent
(`src/agent.py`) binds only native Python tools from `src/tools/registry.py`,
and no MCP client library is in `requirements.txt`.

## Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Architecture | **MCP as a datasource type** (`type='mcp'`), not a parallel subsystem | Reuses ownership, project linking, credential encryption, access control, index surfacing, and the verified dispatch path (`process_datasources` → `ToolContext` → `registry.py`). Matches the product framing "everyone can add a custom datasource, so everyone can add an MCP." |
| Transports | **Both from day one**: streamable-HTTP/SSE (remote) and stdio (local subprocess) | Maximum ecosystem coverage. stdio unlocks the npm/pip server catalog; remote covers hosted servers with zero image changes. |
| Tool exposure | **Expose all tools** a server offers, namespaced; per-server allow-list is a fast-follow | Simplest form, fastest ship. The pressure valve (enabled-tools allow-list) is additive and only built if bloat bites. |
| Read-only toggle | **None for MCP** | The server is the access boundary (same argument [[datasource_redesign]] makes for its read-only tools). Behaves like `generic`: access level = whatever the credentials grant. |
| stdio execution | **In-process subprocess on the agent pod** | The MCP client lives in the agent process; stdio servers must be its children. Security consequences below. |
| Failure posture | **Graceful degradation** | A server that fails to connect is logged and marked unavailable in the index; the job continues. One bad MCP never kills a job. |

## Data Model

No new table and no new junction. Reuse `datasources` + `project_datasources`.

- `type` = `'mcp'` (column is free text; add `mcp` to the accepted-type list in
  the orchestrator's datasource create/update validation).
- `connection_url` = server URL for remote transport; `NULL` for stdio.
  ([[datasource_redesign]] already requires `connection_url` nullable for
  `generic` — verify the ALTER landed; if not, it's the only schema change.)
- `credentials` JSONB (encrypted at rest via the existing datasource
  credential-encryption path):

```json
// Remote (streamable-HTTP; SSE fallback)
{
  "transport": "http",
  "auth": { "type": "bearer", "token": "..." }
  // or: "auth": { "type": "headers", "headers": { "X-Api-Key": "..." } }
}

// stdio (local subprocess)
{
  "transport": "stdio",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-github"],
  "env": { "GITHUB_TOKEN": "..." }
}
```

- `description` (existing column): user-authored context describing what this
  server is for and when to reach for it. It remains stored with the datasource;
  searchable KB ingestion is a fast-follow because no generic
  datasource-to-KB sync path exists today.

Ownership (`user_id`), project linking, and access checks
(`orchestrator/security/access.py`) are inherited unchanged.

## Agent Runtime Integration

New dependency: `langchain-mcp-adapters` (pin it — see the `browser-use`/uvx
drift lesson) in `requirements.txt`. It provides `MultiServerMCPClient`, which
wraps each discovered MCP tool as a LangChain `BaseTool`.

### Connection lifecycle

Slots into the existing hooks in `src/core/datasource_setup.py`:

1. `create_datasource_connection()` gains an `mcp` branch that builds one
   **`MCPManager`** for *all* attached MCP datasources. This matters because
   `ToolContext` datasource connections are **type-keyed (last-one-wins)** —
   a single object under key `mcp` must hold N servers, unlike the one-per-type
   managed connectors.
2. The manager connects to each server (per-server timeout, ~10s), issues
   `tools/list`, and keeps sessions open for the job's duration — required for
   stdio (subprocess must survive across tool calls) and cheaper for HTTP.
3. `close_datasource_connections()` tears down sessions and reaps stdio
   subprocesses.
4. A server that fails to connect is recorded as `unavailable` on the manager
   (with the error), logged, and skipped — mirroring `registry.py`'s existing
   "warn if context missing" posture.

### Tool registration

- On successful discovery, each tool's metadata is registered into
  `TOOL_REGISTRY` under category `mcp`, phase-gated like other datasource tool
  categories (tactical/execution). Registration is idempotent (namespaced keys
  overwrite), so session rebinds are safe. Per-process mutation is fine — the
  agent process serves one job/thread.
- `load_tools()` in `src/tools/registry.py` gains an `mcp` category branch:
  requires `context.has_datasource("mcp")`, pulls the live LangChain tool
  objects from the manager. `bind_tools()` in `src/agent.py` needs **zero
  changes**.
- The existing `datasources` capability grant covers MCP tools automatically
  (they are datasource-provided tools), so grant-restricted jobs exclude them
  with no new grant type.

### Naming

Tools are namespaced `mcp__<server-slug>__<tool>` using the existing
`_ds_slug_hyphen`-style slugification (underscore variant). Provider function-name
limits (64 chars for OpenAI-compatible endpoints) can overflow with long
server + tool names: truncate the server slug to 16 chars and, on residual
collision or overflow, truncate the tool name and append a 4-char hash. The
mapping back to the real MCP tool name lives in the registry metadata, so the
wire call always uses the server's true tool name.

## Surfacing to the LLM

V1 uses the existing **`datasources.md` index**
(`inject_datasource_index` in `src/core/datasource_setup.py`): one line per
server with its name, transport, tool count, and namespaced tool names. Lists
are capped at 40 names with a `+N more` tail. Failed servers appear as
unavailable so the agent does not hallucinate tools.

There is no generic datasource-to-KB synchronization path in the current
codebase; only OKF knowledge-base reindexing exists. A searchable per-server KB
entry is therefore a fast-follow rather than part of v1. Credential values
never appear in the index.

## Orchestrator / API

- Datasource CRUD endpoints already cover create/update/delete/link — add
  `mcp` to type validation plus shape validation (remote requires
  `connection_url`; stdio requires `command`).
- **Connection testing:** the existing test-datasource path gains an `mcp`
  branch — connect, `tools/list`, return tool names + count. Full support for
  remote; stdio test runs on the orchestrator only if the runtime is present,
  otherwise returns "untestable here, will resolve at job start" rather than
  a hard failure.
- **Feature gate:** `MCP_DATASOURCES_ENABLED` env + helm value (pattern:
  `SKILLS_DB_ENABLED`, `PROMPT_DB_OVERRIDES_ENABLED`), ON in dev, OFF in prod
  until the security review below is settled. A second flag
  `MCP_STDIO_ENABLED` lets hosted deployments offer remote-only.

## Agent Image

stdio servers need runtimes in **`docker/Dockerfile.agent`** (the MCP client
runs in the agent process — not the workspace pod, where the shell runs):

- `node` + `npx` (npm ecosystem) and `uv` + `uvx` (python ecosystem), pinned.
- Image size cost is real (~80–150 MB); acceptable given "both transports" is
  locked.

## Security

stdio is arbitrary third-party code execution **by design**, in the agent pod:

- **Trust stance:** an MCP server a user adds runs with the agent pod's
  privileges. This is the same trust class as `generic` datasource env-var
  injection (user-supplied creds reachable by the agent) but strictly more
  powerful. The `MCP_STDIO_ENABLED` gate exists so hosted/multi-tenant
  deployments can restrict to remote-only until per-tenant isolation exists.
- **Network egress:** remote MCP calls originate from the agent pod and are
  subject to existing NetworkPolicies (e.g. prod-private denies cluster-internal
  egress — an internal-host MCP URL fails closed there, which is correct).
- **Credentials:** stdio `env` vars are passed only to the subprocess, not the
  agent's own environment. Remote auth headers live only in the HTTP client.
  Neither reaches `datasources.md` or logs.
- **Prompt-injection surface:** MCP tool descriptions and results are untrusted
  input entering the agent's context. v1 accepts this (same class as web
  research content); noted for the guardrails matrix rather than solved here.

## Failure Modes

| Failure | Behavior |
|---|---|
| Server unreachable at job start | Log, mark `unavailable` in index, continue job |
| Connect exceeds per-server timeout | Same as unreachable (never blocks job start > ~10s per server) |
| Server dies mid-job (stdio crash, HTTP session drop) | One reconnect attempt on next tool call; on failure the tool call returns a tool-error string (agent can adapt), server marked unavailable |
| Tool call errors / times out | Standard tool-error result into the graph, per-call timeout (~60s) |
| Discovery returns 100+ tools | All registered (locked decision); index caps the listing; allow-list fast-follow is the mitigation |

## UI (Cockpit)

- Rename the section to **"MCPs & Data Sources"** (`en.json` / `de-DE.json`
  `datasources` keys; route/internal names unchanged).
- Type selector gains **MCP Server** — "Connect an MCP server; its tools become
  available to your agents."
- Form: Name, Description, Transport toggle:
  - **Remote:** Server URL; Auth (none / bearer token / custom headers
    key-value editor, masked values).
  - **Local (stdio):** Command, Args (list editor), Env vars (key-value,
    masked) — with a warning note that the command runs inside the agent
    environment.
- Test button wired to the connection-test endpoint, showing discovered tool
  count + names on success.
- Project linking UI unchanged; no read-only toggle for MCP (info text
  explains the server enforces access).

## Testing

- **Unit:** credentials-shape validation both transports; slug/namespacing +
  64-char truncation/hash rules; registry `mcp` branch with a stubbed manager;
  last-one-wins manager aggregation of multiple `mcp` datasources; graceful-
  degradation paths.
- **Integration:** in-process stdio echo server via the `mcp` python package
  (spawn, discover, call, teardown); streamable-HTTP against a local test
  server; end-to-end `process_datasources` → `load_tools` → `bind_tools` with
  a fake LLM asserting namespaced tools are bound.
- **k3d smoke:** add an MCP datasource via cockpit, link to a project, run a
  job that calls one MCP tool; verify index + audit trail.
- CI (Py3.12) is the gate, per usual.

## Rollout

1. Deps + agent image runtimes (behind flags, inert).
2. Backend: type validation, manager, registry branch, index rendering,
   connection test.
3. Cockpit form + rename.
4. Dev k3d smoke with one remote server (e.g. GitHub hosted MCP) and one stdio
   server.
5. Enable on dev; prod stays gated until the multi-tenant stdio stance is
   reviewed.

## Fast-Follows

- **Per-server tool allow-list** — `enabled_tools` on the datasource record
  (or per-link on `project_datasources`), with a discovery step in the form.
  The designed answer to tool bloat; build when it bites.
- **OAuth for remote servers** — hosted MCPs increasingly require OAuth 2.1;
  inverse of [[mcp_oauth_bridge]]. Token storage fits `credentials` JSONB.
- Health/status column in the UI (last successful connect, tool count drift).
- Searchable KB entries per MCP server with the full tool-description catalog,
  once a generic datasource-to-KB synchronization path exists.

## Non-Goals (v1)

- MCP **resources** and **prompts** (tools only; adapters support them later).
- MCP **sampling** (server-initiated LLM calls) — explicitly rejected.
- Sandboxing stdio beyond pod isolation (gVisor/nsjail etc.).
- Per-tool curation UI (fast-follow above).
- Session-scoped ad-hoc MCPs (add via chat) — datasource records only.
