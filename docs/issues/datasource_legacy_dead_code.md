# Dead legacy datasource/proxy code on the agent (sweep candidate)

**Status**: Open — cleanup, no functional impact. Filed 2026-06-11
(surfaced by the §9.4 datasource-clone audit and the local-browser
removal; see `docs/features/no_workspace_agent_mode.md` §9).

Verified-dead code that survived earlier refactors. None of it is
reachable in production; listed for one focused sweep instead of folding
into unrelated PRs. Line references were accurate on 2026-06-11 —
re-grep before deleting.

1. **`Agent._inject_datasource_index` + `Agent._format_rw_cli_block`**
   (`src/agent.py` ~:2165-2242) — superseded by the module-level
   `datasource_setup.inject_datasource_index()` /
   `_format_rw_cli_entry()`, which the worker path actually uses. The
   method has zero callers; `_format_rw_cli_block` is called only by it.

2. **`Agent._inject_typed_env_vars`** (`src/agent.py`, directly below) —
   zero callers, but still has a test class
   (`tests/test_datasource_redesign.py` ~:567+) exercising the dead
   method; delete both together. Live equivalents:
   `inject_postgresql_services` / `inject_mongodb_env_vars` /
   `inject_neo4j_env_vars` in `datasource_setup.py`.

3. **`ProxyConfig.to_playwright_proxy`**
   (`src/tools/research/utils/network.py`) — zero consumers since the
   local-browser fallback removal (the workspace `browser-exec` daemon is
   standalone and does not import agent code). Its tests in
   `tests/tools/research/test_browser_tools.py`
   (`TestProxyConfigToPlaywright`) go with it.

4. **Shell config plumbing wart** (related, from the §9.3 hard-off):
   `ShellManager` still accepts `max_tabs`/`scrollback_limit`/
   `default_timeout`/`no_change_timeout`/`sandbox_cwd` purely as inert
   plumbing — enforcement lives in `RemoteBackend`, which both call sites
   configure directly. Additionally the session call site
   (`persistent_session.py`) passes RemoteBackend fewer shell config keys
   than the worker call site (`agent.py`): no `scrollback_limit`, no
   `blocked_commands`. Align the two call sites and slim the ShellManager
   signature in the same sweep. Note `RemoteBackend.blocked_commands`
   matters independently of ShellManager's gate: direct backend callers
   (e.g. GitManager) bypass ShellManager.
