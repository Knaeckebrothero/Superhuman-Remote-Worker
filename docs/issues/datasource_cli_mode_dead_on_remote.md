---
tags:
  - issue
  - agent
  - datasources
related:
  - "[[../features/live_session_settings]]"
---

# CLI-mode datasource access is dead on remote workspace backends

**Status:** Diagnosed 2026-07-16 (live_session_settings.md P0.5 verification).
Not fixed — this doc scopes the fix out of the live-session-settings work and
records the evidence so it doesn't have to be re-derived.

## Summary

Read-write managed-connector datasources (postgresql, neo4j, mongodb) are
supposed to be reachable via CLI tools (`psql service=…`, `cypher-shell`,
`mongosh "$MONGO_X_URI"`) from the agent's shell. On remote workspace
backends — which is every production deployment; the agent process never uses
its own filesystem as the workspace — none of that plumbing reaches the shell.
Compounding it, read-write managed connectors get **no bound tools either**,
so a read-write postgresql/neo4j/mongodb datasource currently gives the agent
**no access path at all** (sessions and worker jobs alike). Read-only
connectors (real connections + read tools) and webdav (always tools) work.

## Evidence chain (verified on develop, 2026-07-16)

1. **Injection is agent-process-local.** `process_datasources` routes
   read-write managed connectors to env-var/service-file injection:
   `inject_postgresql_services` writes `~/.pg_service.conf` **on the agent
   pod** and sets `os.environ["PGSERVICEFILE"]`
   (`src/core/datasource_setup.py`); `inject_mongodb_env_vars` /
   `inject_neo4j_env_vars` set `os.environ` only. The worker path does the
   same (`src/agent.py:3243-3248`).
2. **The shell runs on a different machine.** Remote shells are tmux
   sessions created over SSH on the workspace host
   (`RemoteBackend._init_shell`, `src/core/backends/remote.py:999`). The only
   environment applied is `NONINTERACTIVE_ENV_EXPORT` (pager/prompt
   suppression, `src/tools/shell/shell_manager.py:142`) plus `cd`. Paramiko
   exec/tmux inherit nothing from the agent's Python process environment.
3. **Provisioning seeds nothing.** Neither `container_provisioner.py`,
   `vm_provisioner.py`, nor the workspace image inject datasource
   credentials; no caller writes `pg_service.conf` via
   `RemoteBackend.write_home_file`.
4. **No tools as fallback.** The (now shared) datasource→tool-category map
   (`datasource_tool_categories`, `src/core/datasource_setup.py`) binds no
   tools for read-write managed connectors — CLI mode was the design. Even
   under the agent's old divergent map (write tools), the tools never loaded:
   `process_datasources` creates no connection object for read-write managed
   connectors, and the factories raise without one
   (`create_postgresql_tools`, `src/tools/sql/postgresql.py:68`; the registry
   loader degrades to a warning and skips the category,
   `src/tools/registry.py:445-458`).
5. **The prompt still advertises it.** `_cli_datasources` renders a prompt
   block telling the model the CLI profiles exist (`loader.py` reads
   `config.extra["_cli_datasources"]` at render time) — a false affordance
   the model acts on and fails.

## Consequences

- Read-write postgresql/neo4j/mongodb datasources are unusable in sessions
  and jobs on remote backends (always, in practice).
- The CLI prompt block teaches the model a dead path; failed `psql`/`mongosh`
  attempts waste turns and can loop.
- The live-session-settings feature scopes CLI-mode out of live mutation
  entirely (its Slice B mutates connection-backed tools only).

## Fix directions (not decided here)

1. **Connection-backed write tools** (simplest, no new plumbing): route
   read-write managed connectors through `create_datasource_connection` like
   read-only ones and change the shared map's read-write branch to the write
   tool set. Kills CLI mode deliberately; `_cli_datasources` and the prompt
   block go away. Applies uniformly to jobs and sessions.
2. **Real CLI forwarding**: materialize `pg_service.conf` / env exports onto
   the workspace host at attach (via `write_home_file` + tmux setup preamble,
   or provisioner-level secret mounts). More faithful to the original design,
   but puts plaintext credentials on the workspace filesystem the model can
   read — needs a threat-model pass (`feedback_internal_creds_not_in_workspace`).
3. Hybrid: (1) now, (2) never unless a concrete CLI-only workflow demands it.

Direction (1) is the obvious candidate: it restores access with code that
already exists, and the credential never leaves the agent pod.
