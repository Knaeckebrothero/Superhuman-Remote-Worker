---
tags:
  - issue
  - agent
  - datasources
related:
  - "[[../done/2026-07-16-live-session-settings]]"
---

# CLI-mode datasource access is dead on remote workspace backends


**Closed by the 2026-08-06 doc-truth sweep (batch #3):** cli_ds_types permanently empty; process_datasources()/datasource_tool_categories() docstrings self-cite this doc; tests/test_datasource_tool_categories.py 33/33 green. Direction 2 (real CLI forwarding) stays future-scope.

**Status:** Diagnosed 2026-07-16 (live_session_settings.md P0.5 verification).
**Direction 1 IMPLEMENTED 2026-07-16** (hybrid decision): read-write managed
connectors now get real connections + write tools; CLI mode is retired as a
routing target but its machinery is kept in place for a future genuine
CLI-forwarding feature (direction 2) — that feature is out of scope for the
live-session-settings session and picks up from this doc.

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

## Consequences (as diagnosed; resolved by direction 1 below)

- Read-write postgresql/neo4j/mongodb datasources were unusable in sessions
  and jobs on remote backends (always, in practice).
- The CLI prompt block taught the model a dead path; failed `psql`/`mongosh`
  attempts wasted turns and could loop.
- The live-session-settings feature scopes CLI-mode out of live mutation
  entirely (its Slice B mutates connection-backed tools only).

## Fix directions (hybrid decided 2026-07-16)

1. **Connection-backed write tools** — ✅ **IMPLEMENTED 2026-07-16**:
   - `process_datasources` routes ALL managed connectors (read-write
     included) through `create_datasource_connection`; the CLI-mode branch is
     gone. Read-only entries are processed first so on mixed same-type
     attachments the read-write connection wins the type-keyed registry slot
     (write tools must never bind to a read-only-linked connection).
   - `datasource_tool_categories` (shared map, both boundaries) maps any-RW
     to the write tool set instead of `[]`.
   - `cli_ds_types` is now always empty → the `_cli_datasources` prompt
     block never renders. The plumbing (return value, attach-path
     application, loader template conditional, `inject_*` env helpers) is
     deliberately KEPT as the seam for direction 2.
   - The `datasources.md` read-write entry describes "query + write tools";
     the PGSERVICE/cypher-shell/mongosh usage lines were removed
     (`_format_rw_cli_entry` deleted).
   - Tests: `tests/test_datasource_tool_categories.py`
     (`TestProcessDatasourcesConnectionRouting`, `TestDatasourceIndexNotes`,
     updated map tests).
2. **Real CLI forwarding** — the remaining future feature; picks up from
   here. Materialize `pg_service.conf` / env exports onto the workspace host
   at attach (via `write_home_file` + tmux setup preamble, or
   provisioner-level secret mounts). Faithful to the original design, but
   puts plaintext credentials on the workspace filesystem the model can
   read — needs a threat-model pass
   (`feedback_internal_creds_not_in_workspace`). When built, it re-enables
   the kept seam: populate `cli_ds_types` again, reintroduce proper CLI
   usage notes, and decide the tools-vs-CLI split per type. Cleanup
   candidates for the same change: the dead worker-side duplicates
   `Agent._inject_datasource_index` / `_format_rw_cli_block` /
   `_inject_typed_env_vars` (`src/agent.py:3140-3248`, no production
   callers, kept alive only by their tests).
