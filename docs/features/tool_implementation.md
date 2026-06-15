---
tags:
  - tool-development
  - agent-architecture
  - context-management
  - llm-configuration
aliases:
  - ["Tool Implementation", "Tool Description System", "Tool Deferral"]
related:
  - "[[builder_to_sessions_consolidation]]"
  - "[[two_graphs]]"
  - "[[prompts]]"
---

# Tool Deferral & Description Management

> **Status:** Mechanism partially implemented (v0 = description-shortening + workspace docs). The "proper" deferral that actually reclaims context is **unfinished** — see [Gaps](#the-gaps-to-proper-deferral) and [Design](#design--finishing-it).
> **Rewritten:** 2026-06-13 to current architecture. This doc previously described the retired Code_Repository "Creator/Validator" requirements pipeline (JSON configs, `write_requirement_to_cache`, `validate_schema_compliance`, `src/agents/tools/…`); all of that is gone. The old token tables (27/40 tools, ~6.6k/9.5k) measured that defunct system and are **not** representative — do not cite them.

## Overview

The agent registry (`src/tools/registry.py`) holds **97 tools across 17 categories**. No agent binds all of them — each expert config (`config/experts/*`, `config/persistent_defaults.yaml`) curates a per-category subset, and worker mode further splits them by phase (strategic vs tactical). Even so, a persistent session binds ~71 and the (being-deprecated) builder binds ~91.

Two mechanisms provide tool knowledge to the agent:

1. **LangChain binding** — tool name + description + parameter schema are serialized into every LLM request via `bind_tools()`.
2. **Workspace documentation** — full per-tool markdown generated into `workspace/tools/` for on-demand reading.

## Current mechanism (v0 — implemented)

Lives in `src/tools/description_manager.py`.

- **Per-tool registry metadata** `defer_to_workspace: True` + a one-line `short_description`, set in the tool modules. Deferred today (~12 tools): `sql_*` (3), mongodb (5), graph/neo4j (3), `get_document_info` (1) — the datasource-heavy, complex-usage ones.
- **`apply_description_overrides(tools)`** — at load time, for any tool flagged `defer_to_workspace`, swaps the full docstring for `short_description` via `model_copy(update={"description": ...})`. Called at `agent.py:1865` (worker) and `persistent_session.py:464` (sessions).
- **`generate_workspace_tool_docs(...)`** — writes `tools/README.md` (index grouped by category) + `tools/<tool>.md` (full doc per tool) into the workspace. Called just before the overrides at both sites.
- Helpers: `get_deferred_tools()`, `get_core_tools()`.

So the "short blurb in context, full doc on disk to read on demand" loop **is built** — for a hardcoded set of tools.

## Token cost (honest accounting)

Tool definitions ride on **every** LLM call, so they are pure fixed overhead competing with work content for the window. The current mechanism trims this only partially:

> ⚠️ **Deferral today shortens the *description text* only. The tool stays in `bind_tools()`, so its full parameter schema is still serialized every call.** Description-shortening ≠ removing the tool from context.

This is the load-bearing fact. To actually reclaim context you must **unbind** the tool, not just shorten its blurb.

**Magnitudes are currently unmeasured on this architecture.** Before leaning on numbers, measure for real: introspect the serialized tool schemas (the OpenAI function-call format LangChain emits), tokenize, and compare full-vs-`short_description` per category on a live config (the session-71 set, or `developer`). External anchor only: Anthropic reports tool defs consuming tens-to-hundreds of K tokens in large multi-server setups — directionally why this matters, not our numbers.

## The gaps to "proper" deferral

1. **No user/config control.** `defer_to_workspace` is hardcoded in the tool's Python metadata. Nothing in `config/experts/*`, `config/schema.json`, or the `AgentConfig` tools model controls it — the `tools:` block is still plain name-lists per category. User-driven provisioning (the goal: "let me give an agent everything, cheaply") is unbuilt.
2. **No real context savings.** Per above, the param schema stays bound. Deferring all domain tools still pays for ~all their schemas.

## Design — finishing it

The intent (per the owner): the **user** chooses how to provision an agent — possibly *all* tools — and deferral makes that affordable. The agent is told "you have toolset X (N tools) — read `tools/README.md`/`tools/<name>.md` for usage," then uses them. To deliver that, two decisions:

### Invocation fork — once a deferred tool is *unbound*, how does the agent call it?

- **(a) Generic dispatcher** — bind one `invoke_tool(name, args)` (or per-group `use_sql_tool(op, args)`). Agent reads the doc, calls the dispatcher. One schema for N tools; matches the "read doc then call" UX with no round-trip. **Cost:** loses per-tool schema validation at bind time — the model hand-builds args from prose, which weaker models (gemma-4, gpt-oss; cf. the tool-call parser loop) fumble more often.
- **(b) On-demand loader + rebind** — bind a `load_toolset(group)`; when called, rebind the LLM with those *real* tools via the existing `get_current_tools()` hook (`persistent_graph.py:343` re-fetches tools every turn — already wired; worker mode pre-binds two phase-LLMs in `graph.py` → would need wiring). Keeps full schemas + validation once loaded. **Cost:** a round-trip per group + the worker-mode plumbing.
- **(c) Hybrid (recommended lean)** — deferred groups advertised as a one-line manifest in the system prompt ("You have the SQL toolset — 3 tools, see `tools/README.md`"); a `load_toolset` promotes a whole group to fully-bound for the rest of the session. Group-grained (b). Genuine savings (unbound until needed), real validation once loaded, reuses the hook, and the unit-of-deferral = the category group, which is already how the README and configs organize tools. Chosen because our fleet skews to models that struggle with tool-call formatting, so keeping real schemas beats a generic dispatcher's brittleness.

Note: a one-line "these tools are deferred, look them up" manifest handles *discovery* but not *callability* — with the tools still bound it's just today's state; with them unbound you still need (a) or (b). Schema-cost and callability are linked.

### Provisioning UX

- **Per-group defer toggle** (light) vs **per-tool checkbox** (the literal "tick a box on the add-tools button" idea — heavy as a 90-row list). Compose: group toggles by default, per-tool override for power users.
- The tool-picker UI today lives in `cockpit/.../instruction-builder.component.ts` — the **builder being deprecated**. So this UI should land on the session/expert-config surface, not the dying component. See [[builder_to_sessions_consolidation]].

### Motivating case

Giving a session **builder-parity** (~90 inspection/operator tools) is the concrete driver: unaffordable if bound, fine if deferred. This is why deferral and the builder→sessions consolidation are coupled.

## Approaches considered (research)

External patterns evaluated. Our constraint that decides between them: **multi-model fleet** (gemma, gpt-oss, minimax, deepseek, codex/gpt-5.x, groq, openrouter, Claude) bound via LangChain → anything Claude-only can't be the foundation.

| Approach | LLM-agnostic | Complexity | Reclaims context? | Notes |
|---|---|---|---|---|
| **1. Anthropic Tool Search Tool** (`defer_loading` + `tool_search_tool_*`, beta `advanced-tool-use-2025-11-20`) | No — Claude Sonnet/Opus 4.5+ | Low | Yes (server-side) | Strands most of our fleet. Best applied to the **MCP server** (Claude clients consume it directly), not the agent's internal binding. LangChain passthrough unverified. |
| **2. LangGraph dynamic selection (vector store)** | Yes | Medium | Yes (~top-k bound) | Retrieve relevant tools per turn; bind only those. We already have pgvector + qwen3-embedding + hybrid search to reuse. Risk: recall miss (a tool the model needed wasn't retrieved). |
| **3. Workspace-based deferral** | Yes | Low | **Partial — v0, this is what we built** | Short desc bound + full doc in `tools/`. Today only shortens the description (schema stays bound). The "finishing it" design above turns this into real savings. |
| **4. Prompt caching** (`cache_control`) | No — Claude | Low | No (cost/latency only) | We cache **nothing** today (`cache_control` 0× in the codebase). Cheapest cost win on Claude paths; doesn't reduce window pressure. |
| **5. Token-efficient tools beta** (`token-efficient-tools-2025-02-19`) | No — Claude | Low | Output only | Compact tool-call output; orthogonal. |

Composability: (3) + (2) is natural — workspace docs for the long tail, a `search_tools`/retrieval default set for relevance — and (1)/(4) layer onto the Claude-specific surfaces.

## Open questions

1. Dispatcher (a) vs loader (b/c) — the brittleness-vs-round-trip trade for weak models. **Leaning (c).**
2. Defer granularity in config/UI: per-group vs per-tool vs both.
3. Worker-mode rebinding: phase-LLM pre-binding (`graph.py`) needs a path equivalent to the session `get_current_tools()` hook if loader-style deferral is used.
4. Where the provisioning UI lands post-builder-deprecation.
5. Real token measurement on current configs (deferred until it actually blocks a decision — owner's call: not urgent).

## Related

- [[builder_to_sessions_consolidation]] — the motivating case (session builder-parity)
- [[two_graphs]] — worker vs persistent binding paths
- [[prompts]] — model-class-aware description tiers (a complementary lever)
- `src/tools/description_manager.py` — the implementation
- `src/tools/registry.py` — `TOOL_REGISTRY`, `get_all_tool_names`
