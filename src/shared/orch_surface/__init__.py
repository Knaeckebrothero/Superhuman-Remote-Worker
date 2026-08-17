"""The orchestrator surface — one client + one presentation layer, N runtimes.

Home of the shared job-management toolset
(knowledge-base/knowledge/features/unified_orchestrator_tool_surface.md): the REST client
(``client``) and the output formatters (``formatters``) that every LLM-facing
surface — the MCP server, persistent sessions, officers — builds its
orchestrator tools from. Import the modules by their canonical path
(``src.shared.orch_surface.client`` / ``.formatters``); this resolves both
in-repo (repo root on ``sys.path``) and inside the images (``/app/src/...``),
which is what retired the old per-image fallback-import chains.
"""
