---
tags:
  - llm-configuration
  - agent-architecture
  - coding-tools
  - tool-development
aliases:
  - Claude Code SDK
  - CLI Wrapper
  - Claude Code Integration
related:
  - "[[coding_agent]]"
  - "[[cloud_workspace]]"
  - "[[agent_improvements]]"
  - "[[auxiliary_tasks]]"
---

# Claude Code SDK as Alternative LLM Backend

## Context

Running the agent with Anthropic API using Opus 4.6 hits account rate limits after ~2 messages — not enough for even half a strategic phase. Claude Code has separate, higher rate limits. The Claude Agent SDK (`claude-agent-sdk`) allows programmatic use of Claude Code from Python. The goal is to integrate the SDK so agents can run using Claude Code's rate limits while preserving the phase/todo/workspace model.

---

## Open Issue: LangGraph and Tool Redundancy

The initial plan (Phase Delegation — wrap our tools as MCP, plug into LangGraph's execute node) has a fundamental problem: **Claude Code already does most of what our LangGraph graph + tool layer does**, making large parts of the codebase redundant rather than reusable.

### The SDK is an agent, not an LLM

The Claude Code SDK manages its own ReAct loop (tool calling + result processing). It cannot be used as a drop-in LLM replacement that returns `AIMessage` objects with `tool_calls` for LangGraph to execute. This means:

- **LangChain message types don't work** — The SDK returns its own message format, not LangChain `AIMessage`/`ToolMessage`. The LangGraph state machine that routes based on `tool_calls` in messages can't consume SDK output directly.
- **The execute → tools → check_todos loop is redundant** — Claude Code already runs this loop internally. Wrapping it inside LangGraph's loop creates two nested ReAct loops for no benefit.
- **State sync is artificial** — Converting SDK output back into LangGraph state updates is glue code that exists only to preserve an architecture that's being bypassed.

### Tool redundancy

Claude Code has built-in equivalents for roughly half our tool categories:

| Our Tool Category | Claude Code Built-in | Still Needed as MCP? |
|---|---|---|
| **workspace** (`read_file`, `write_file`, `list_files`, etc.) | `Read`, `Write`, `Edit`, `Glob`, `Grep` | **No** — redundant and inferior |
| **git** (`git_log`, `git_diff`, `git_status`) | `Bash` (can run any git command) | **No** — redundant |
| **coding** (`run_command`) | `Bash` | **No** — redundant |
| **research** (`web_search`) | `WebSearch`, `WebFetch` | **No** — redundant |
| **core** (`todo_complete`, `next_phase_todos`, `mark_complete`, `job_complete`) | Nothing equivalent | **Yes** — our orchestration logic |
| **citation** (`cite_document`, `cite_web`, `search_library`, etc.) | Nothing equivalent | **Yes** — CitationEngine integration |
| **database** (`execute_cypher_query`, `sql_query`, `mongo_query`, etc.) | Nothing equivalent | **Yes** — datasource connectors |
| **document** (`chunk_document`) | Nothing equivalent | **Yes** — document processing pipeline |

### What actually needs to be bridged via MCP

Only the domain-specific tools that have no Claude Code equivalent:
- **Core tools** — phase/todo management (the orchestration protocol)
- **Citation tools** — CitationEngine library management
- **Database tools** — Neo4j, PostgreSQL, MongoDB datasource connectors
- **Document tools** — PDF/document chunking

Everything else (file I/O, git, shell, web search) should use Claude Code's native tools — they're better maintained, have proper sandboxing, and Claude is already trained to use them.

### Does LangGraph still make sense?

If Claude Code handles the inner ReAct loop, file operations, context management, and auto-compact, then LangGraph's role shrinks to:

1. Phase transitions (strategic ↔ tactical)
2. Checkpointing (resume after crash)
3. Graph routing (init → execute → archive → transition → goal check)

But the phase transition logic is ~100 lines of Python, not a complex state machine. A simpler alternative:

```python
# What the architecture could look like without LangGraph
while not job_complete:
    result = await query(phase_prompt, options=ClaudeAgentOptions(
        mcp_servers={"agent": domain_mcp_server},  # only core + citation + db tools
        system_prompt=strategic_or_tactical_prompt,
        model="claude-opus-4-6",
    ))
    phase_complete, job_complete = check_completion(result)
    if phase_complete and not job_complete:
        prepare_next_phase()
```

This raises the question: **do we drop LangGraph entirely for Claude Code mode**, or do we keep it as dead weight to maintain a single codebase?

---

## Approaches

### Approach A: Full Replacement — Claude Code *is* the agent

Throw out LangGraph, LangChain, and the entire existing execution stack. Claude Code becomes the agent. The system reduces to a thin Python loop that calls the SDK per-phase, with only domain-specific tools (todos, citations, databases) bridged via MCP. Everything else — file I/O, git, shell, web search, context management — is Claude Code native.

```
┌─────────────────────────────────────────┐
│  Thin Python orchestrator (~200 lines)  │
│  - Phase loop (strategic ↔ tactical)    │
│  - Job init / completion                │
│  - Workspace setup                      │
└──────────────┬──────────────────────────┘
               │ query() per phase
               ▼
┌─────────────────────────────────────────┐
│  Claude Code SDK                        │
│  - Full ReAct loop                      │
│  - Native: Read/Write/Edit/Bash/Web     │
│  - Auto-compact, context management     │
│  - MCP: todo, citation, db, document    │
└─────────────────────────────────────────┘
```

**Pros:**
- Simplest possible architecture — delete most of the codebase
- Claude Code's native tools are battle-tested and better than our wrappers
- No LangChain/LangGraph dependency, no message translation, no state sync
- Auto-compact, retries, context management all handled for free
- ~200 lines of orchestration vs ~2000 lines of graph + nodes

**Cons:**
- Throw away a working, provider-agnostic system
- Locked to Anthropic/Claude Code — no more OpenAI, Gemini, Groq, local models
- No LangGraph checkpointing (need custom checkpoint logic or accept phase-level granularity)
- Phase snapshot recovery needs reimplementation
- Two completely separate execution paths if we keep the old system alongside

---

### Approach B: Claude Code as a Tool — agent delegates work to Claude Code

Keep the existing LangGraph agent exactly as-is. Add a single new tool: `claude_code`. The agent still runs on the Anthropic API (Opus 4.6), sees its todos, plans its approach — but instead of doing the heavy lifting itself (which burns through rate-limited API calls), it delegates work to Claude Code sessions.

The key insight: **the agent only needs a few API calls per phase** (read todos, decide what to delegate, review results). The expensive work (writing long documents, researching, file operations) happens inside Claude Code, which has separate rate limits.

```
┌──────────────────────────────────────────────┐
│  Existing LangGraph Agent (unchanged)        │
│  - Phase alternation, todos, checkpointing   │
│  - Uses Opus 4.6 via API (few calls)         │
│  - Sees todos, decides what to do            │
│                                              │
│  New tool: claude_code(prompt, workdir)      │
│  ┌────────────────────────────────────────┐  │
│  │  Spawns Claude Code session            │  │
│  │  - Executes the actual work            │  │
│  │  - Writes files, does research         │  │
│  │  - Returns results to agent            │  │
│  │  - Uses Claude Code rate limits        │  │
│  └────────────────────────────────────────┘  │
│                                              │
│  Agent reviews result, marks todo complete   │
└──────────────────────────────────────────────┘
```

**How it works:**
1. Agent enters tactical phase, sees todo: "Write chapter 5.1 on system architecture"
2. Agent calls `claude_code(prompt="Write chapter 5.1...", workdir="workspace/job_xxx/output/")`
3. Claude Code session runs — reads source docs, writes the chapter, edits files — all on Claude Code rate limits
4. Tool returns a summary of what was done (files created/modified, key decisions)
5. Agent reviews, marks todo complete, moves to next todo
6. Total API calls for agent: ~2-3 per todo (plan, delegate, review) instead of ~20-50

**Pros:**
- **Minimal changes** — add one tool to the registry, everything else stays
- **Provider-agnostic** — the agent itself can still run on any LLM; Claude Code is just a tool
- **Preserves all infrastructure** — checkpointing, phase snapshots, audit trail, cockpit UI all work
- **Rate limit arbitrage** — expensive work runs on Claude Code limits, cheap orchestration on API limits
- **Graceful degradation** — if Claude Code isn't available, agent works normally (just slower/rate-limited)
- **Hybrid by nature** — agent uses its own judgment about when to delegate vs do directly

**Cons:**
- Still uses some Anthropic API calls (but far fewer — ~2-3 per todo instead of ~20-50)
- Agent ↔ Claude Code handoff adds latency per delegation
- Agent can't "watch" Claude Code work in real-time (gets results after completion)
- Claude Code sessions are independent — no shared conversation context between delegations
- Need to design the prompt/result protocol (what does the agent tell Claude Code, what does it get back)

**Tool interface sketch:**
```python
@tool
async def claude_code(
    prompt: str,         # What to do (detailed instructions from agent)
    workdir: str = ".",  # Working directory for the session (default: workspace root)
    max_turns: int = 50, # Safety limit on SDK turns
) -> str:
    """Delegate a task to a Claude Code session.

    Spawns an independent Claude Code session that can read/write files,
    run commands, search the web, and perform complex multi-step work.
    Returns a summary of what was done and any files created/modified.

    Use this for heavy work: writing long documents, complex research,
    multi-file edits, code generation. The agent should provide clear
    instructions and review the results.
    """
    options = ClaudeAgentOptions(
        model="claude-opus-4-6",
        permission_mode="acceptEdits",
        system_prompt="You are working inside a job workspace...",
        max_turns=max_turns,
    )
    messages = []
    async for msg in query(prompt=prompt, options=options):
        messages.append(msg)
    return summarize_session(messages)
```

---

### Approach C: Phase Delegation (preserve LangGraph shell)

Keep LangGraph as the outer loop but delegate each phase's execute node to the SDK. Bridge ALL tools (including redundant ones) as MCP to maintain consistency.

**Pros:**
- Single graph topology for both modes
- Checkpointing and phase snapshots work as-is
- Less divergence between standard and Claude Code execution paths

**Cons:**
- Wrapping redundant tools (Claude Code has better built-in versions)
- Artificial state sync between SDK messages and LangGraph state
- LangGraph becomes a pass-through shell that adds complexity without value
- LangChain message incompatibility requires translation layer
- Most complex to implement of all three approaches

---

### Comparison

| | A: Full Replacement | B: Claude Code as Tool | C: Phase Delegation |
|---|---|---|---|
| **Effort** | High (rewrite) | Low (one new tool) | Medium (bridge layer) |
| **Risk** | High (throw away working system) | Low (additive change) | Medium (glue code) |
| **Provider lock-in** | Anthropic only | None — tool is optional | Anthropic for CC phases |
| **Existing infra** | Discarded | Fully preserved | Partially preserved |
| **API calls saved** | All (no API needed) | Most (~90% reduction) | All CC phases (no API) |
| **Checkpointing** | Needs reimplementation | Works as-is | Works as-is |
| **Complexity** | Simplest runtime | Simplest change | Most complex |

---

## Original Detailed Plan (for reference)

> The sections below describe the Phase Delegation approach (Approach B) in detail.
> They may need revision depending on which approach is chosen.

### Architecture (Phase Delegation)

```
Standard mode:                          Claude Code mode:
  execute → tools → check_todos           execute (delegates to SDK)
    ↑         ↓                              ↑         ↓
    └─────────┘                              │   SDK runs inner loop
                                             │   (tools via MCP bridge)
  LLM layer:                                 │         ↓
  - create_llm() per provider             check_todos → archive_phase
  - ContextManager (3-layer safety)
  - ReasoningChatOpenAI wrapper          LLM layer:
  - Summarization LLM for compaction     - None. Claude Code handles everything.
  - bind_tools() for each phase          - Auto-compact replaces summarization.
                                         - SDK manages its own context window.
```

### What gets bypassed in Claude Code mode

| Component | Standard Mode | Claude Code Mode |
|-----------|--------------|-----------------|
| `create_llm()` | Creates ChatOpenAI/ChatAnthropic/etc. | Not called |
| `bind_tools()` | Attaches tool schemas to LLM | Not called — tools are MCP servers |
| `ContextManager` | Three-layer overflow protection | Not used — SDK has auto-compact |
| `ReasoningChatOpenAI` | HTTP-layer token counting | Not used |
| Summarization LLM | Compacts context when full | Not needed — SDK auto-compacts |
| `workspace_injection.py` | Injects workspace.md as fake tool result | Not used — workspace.md in user prompt |
| `retry_manager` | Exponential backoff on LLM errors | Not used — SDK handles retries |

### New Files (Phase Delegation)

#### 1. `src/llm/claude_code_executor.py` (~350 lines)
Main integration class.

- `ClaudeCodeExecutor.__init__(config)` — stores config (model name, timeout, max_turns)
- `async execute_phase(state, tools, tool_context, config)` — runs one phase via SDK
- `_build_system_prompt(state, config)` — reuses `get_phase_system_prompt()` from `src/graph.py`
- `_build_user_prompt(state, config)` — workspace.md + plan.md + todos + phase instructions
- `_calculate_max_turns(todos)` — `len(todos) * 10` with configurable cap

#### 2. `src/llm/mcp_tool_bridge.py` (~250 lines)
Wraps LangChain tools as SDK MCP tools (in-process, shares ToolContext closures).

#### 3. `src/llm/claude_code_hooks.py` (~150 lines)
Audit hooks for MongoDB audit trail (PreToolUse, PostToolUse, Stop).

#### 4. `src/llm/claude_code_state_sync.py` (~120 lines)
Translates SDK session results → LangGraph state updates.

### Modified Files (Phase Delegation)

- `src/core/loader.py` — Add `"claude-code"` to provider detection
- `src/agent.py` — Skip LLM creation + `bind_tools()` for Claude Code mode
- `src/graph.py` — Add `claude_code_executor` parameter to execute node + graph builder
- `config/schema.json` — Add `"claude-code"` to provider enum
- `requirements.txt` — Add `claude-agent-sdk>=1.0.0` (optional)

### Configuration

```yaml
# Full Claude Code mode — no other API provider needed
llm:
  model: claude-opus-4-6
  provider: claude-code
```

### Error Handling

| Scenario | Strategy |
|----------|----------|
| `claude-agent-sdk` not installed | `ImportError` → fall back to standard `anthropic` provider with warning |
| SDK rate limited | SDK handles internally; surface error if persistent |
| Phase timeout | `asyncio.timeout()` wrapper around `query()`, configurable via `llm.timeout` |
| SDK process crash | Catch subprocess error, return partial state, let LangGraph retry from checkpoint |
| `max_turns` exceeded without completion | Return partial state with `phase_complete=False`, LangGraph re-enters execute |

### Limitations

- **No intermediate checkpoints** during SDK execution (phase is atomic)
- **No `DEBUG_LLM_STREAM`** — SDK has its own streaming
- **No `reasoning_content` capture** — SDK doesn't expose model reasoning tokens
- **Context management fully delegated** — Claude Code's auto-compact handles everything
- **One model per phase** — can't switch models mid-phase
- **Claude Code CLI required** — Node.js runtime + `claude auth` setup needed

## Related

- [[coding_agent]] — Coding agent that could delegate work via Claude Code
- [[cloud_workspace]] — Container architecture for production deployment
- [[agent_improvements]] — Architecture improvements for the same agent system
- [[auxiliary_tasks]] — Phase alternation model that Claude Code would replace
- [[calculator_code]] — Code execution tools that Claude Code handles natively
