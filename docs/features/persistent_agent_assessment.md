---
tags:
  - architecture
  - agent
  - assessment
aliases:
  - persistent agent assessment
  - competitive analysis
related:
  - "[[agent_lifecycle]]"
---

# Persistent Agent — Competitive Assessment

Honest gap analysis comparing our persistent agent implementation (as of 2026-03-30) against state-of-the-art interactive coding agents: Claude Code, Codex CLI, Gemini CLI, Cursor, Devin, and Windsurf/Cascade.

## What We Built

A persistent interactive agent mode added alongside the existing worker agent. Single container image, `--mode persistent` flag. WebSocket transport, `while(tool_call)` loop, permission modes, tool execution, context compaction, job delegation to workers, Angular cockpit UI.

## Capability Matrix

| Capability | Ours | Claude Code | Codex CLI | Gemini CLI | Cursor | Devin |
|---|---|---|---|---|---|---|
| **Interactive loop** | `while(tool_call)` | `while(tool_call)` | Item/Turn/Thread | ReAct | ReAct + trajectories | Plan-and-execute |
| **Transport** | WebSocket | SSE | JSONL/stdio | Event stream | IDE-native | Web + Slack + API |
| **Token streaming** | Partial (chunks, not per-token for Anthropic) | Per-token SSE | Per-token JSONL | Per-token | Speculative decoding | Inline worklog |
| **Context window** | Model-dependent (no custom management) | 1M + 3-layer compaction | Auto-compaction | 1M + XML snapshots + semantic elision | Signal-retaining compaction | VM (not window-limited) |
| **Persistent memory** | workspace.md (transient injection) | CLAUDE.md + auto-memory | Thread persistence | GEMINI.md + 5-level hierarchy | Chat history | Checkpoints + secrets |
| **Permission modes** | 3 (supervised/auto/autonomous) | 4 + allowlists + managed policies | 3 + sandbox profiles + domain allowlists | 5 sandbox methods | Seatbelt/Landlock + network control | Cloud VM isolation |
| **Sandbox/isolation** | None (tools run in process) | File checkpoints + protected paths | OS-native (Seatbelt/Bubblewrap/seccomp) | gVisor/Docker/Seatbelt/LXC | Seatbelt/Landlock + overlay FS | Full VM |
| **Tool execution** | Sequential, direct invoke | Sequential, deferred schemas | Sequential + cloud 2-phase | Sequential via scheduler | Parallel reads / sequential writes | Shell + editor + browser |
| **Session resume** | None (in-memory only) | --continue/--resume/--fork | resume picker + fork | 30-day history + checkpoints | Chat history search | Checkpoint restore |
| **Background/delegation** | Worker job delegation via REST | Subagents + agent teams | Worktrees + A2A | Cloud VMs (8 parallel) + async subagents | "Devin Manages Devins" | N/A (is the background agent) |
| **Hooks/extensibility** | Config-driven instruction files | 24 lifecycle events, 4 handler types | Skills + hooks + A2A | Plugins + MCP + skills | Playbooks + MCP marketplace | .windsurfrules |
| **MCP integration** | REST tools (not MCP protocol) | First-class (deferred, OAuth) | First-class | First-class | First-class | MCP servers |
| **UI** | Angular WebSocket chat | Terminal + IDE + Web + Slack + CI/CD | Terminal + IDE + Web + Desktop | Terminal + IDE | IDE (VS Code fork) + Web | IDE (VS Code fork) |

## Gap Analysis

### Critical Gaps (must-have for production)

**1. No sandbox or isolation**
Our tools run in-process with no filesystem or network restrictions. Every other agent has OS-level sandboxing. Claude Code has file checkpoints (rewind edits). Cursor uses Landlock + seccomp overlays that make ignored files literally inaccessible. Gemini offers 5 different sandbox methods. Without this, a misguided tool call can damage the host.

*Mitigation path:* The VM backend already exists for worker agents. Persistent agents with VMs get full isolation. For local dev without VMs, implement file checkpoints (snapshot before write, rewind on error) and a command blocklist (already exists in ShellManager).

**2. No session persistence**
Messages are in-memory. If the agent restarts, the conversation is lost. Every competitor has session resume. Claude Code stores JSONL transcripts. Codex has Thread-level persistence with fork/resume. Gemini keeps 30 days of history.

*Mitigation path:* Checkpoint messages to the `threads.metadata` JSONB column or a dedicated checkpoint table. Load on reconnect. The LangGraph checkpointer pattern (SQLite) could be reused here.

**3. No context compaction**
We call `ContextManager.ensure_within_limits()` but this is the worker's compaction — it summarizes via AuxiliaryLLM. We don't have the persistent-agent-specific patterns: preserving workspace.md across compaction (we inject it but don't protect it from summarization), no manual `/compact` command, no semantic elision of repeated errors.

*Mitigation path:* The ContextManager and AuxiliaryLLM already work. Need to: (a) mark workspace.md injection messages so they survive compaction, (b) add a `/compact` command via the WS protocol, (c) tune the compaction to preserve recent tool results more aggressively.

**4. Incomplete streaming**
Anthropic's streaming returns content in the final chunk (not per-token for tool-use responses). OpenAI/Groq stream per-token. The UI shows the response all at once for Anthropic models rather than the typing effect users expect.

*Mitigation path:* This is a provider-level limitation with `langchain`'s `astream()`. For true per-token streaming with Anthropic, use the raw SDK's `messages.stream()` instead of LangChain's wrapper, or switch to `astream_events()` which may yield finer granularity.

### Significant Gaps (important for usability)

**5. No interrupt/steering mid-turn**
We have an interrupt flag checked before each LLM call, but not mid-generation. Claude Code lets you type during generation to redirect. Codex has `turn/steer` to inject input into an active turn. Our interrupt only works between tool calls, not during a long LLM response.

**6. No file checkpoints / undo**
Claude Code creates a checkpoint before every file edit — press Esc twice to rewind. We have git versioning in the workspace but no per-edit snapshots or quick undo in the UI.

**7. No hooks system**
Claude Code has 24 lifecycle events with 4 handler types. Gemini has skills. Cursor has plugins. We have config-driven instruction files (passive injection) but no programmable hooks that can block, modify, or react to tool calls.

**8. No plan mode**
Claude Code restricts to read-only tools in plan mode. Gemini has a dedicated plan mode with `ask_user` tool. Our persistent agent is always in execution mode — there's no "think but don't act" mode.

**9. Permission model is basic**
We have 3 modes but no per-command allowlists, no protected paths, no managed policies. Claude Code has `settings.json` allowlists, protected paths (`.git`, `.claude`), and organization policies. Our ShellManager has `blocked_commands` and `sandbox` but the permission check in the WebSocket handler doesn't integrate with them.

**10. No subagent spawning**
Claude Code spawns isolated subagents with their own context windows. Cursor runs up to 8 parallel cloud VMs. We delegate to workers via orchestrator REST tools, which is functionally similar but slower (worker must boot, register, pick up job) and lacks real-time progress streaming back to the conversation.

### Minor Gaps (nice-to-have)

**11. No MCP protocol client** — We use direct REST. MCP would auto-discover tools and get new ones for free.

**12. No web search in UI** — The `web_search` tool exists but results aren't rendered specially in the chat UI (no link cards, no source attribution).

**13. No diff display** — File edits show as tool results, not as rendered diffs. Cursor and Claude Code show inline diffs with accept/reject.

**14. No slash commands** — No `/compact`, `/auto`, `/plan`, `/done` in the chat. Mode switching is via dropdown only.

**15. Single session per agent** — Each agent pod serves one thread. Multi-session would reduce infrastructure overhead.

## What We Do Well

**1. Worker delegation is unique.** No CLI agent has this. The persistent agent can create autonomous jobs that run the full phase alternation system (strategic planning, tactical execution, retrospectives) — strictly more powerful than subagents that share the parent's simple loop. The user can monitor and steer workers mid-execution via `resume_worker_job` with feedback.

**2. The architecture is right.** The `while(tool_call)` loop matches what Claude Code, Codex, and Gemini all converged on. The shared infrastructure (ContextManager, WorkspaceManager, tools, LLM creation) means the persistent agent gets everything the worker has — same models, same tools, same workspace system — without duplication.

**3. WebSocket is the correct transport.** SSE (Claude Code) is simpler but unidirectional — approval requests require a separate HTTP endpoint. WebSocket gives us bidirectional streaming, approval flow, interrupts, and mode switching in a single connection. Codex's JSONL/stdio is similar in capability but tied to local processes.

**4. The UI is functional.** Streaming markdown, tool call cards with collapsible results, permission request banners, mode switching, connect/disconnect — the essentials are there. The Catppuccin theme is consistent with the existing cockpit.

**5. Config-driven tool sets.** Expert configs customize the persistent agent's persona, tools, and LLM per session. This is comparable to Gemini's skills and Cursor's plugins but integrated into the existing config inheritance system.

## Priority Roadmap

Based on the gaps, ordered by impact:

| Priority | Gap | Effort | Impact |
|---|---|---|---|
| P0 | Session persistence (checkpoint to DB) | Medium | Users lose everything on disconnect |
| P0 | Context compaction hardening | Low | Long sessions will overflow |
| P1 | File checkpoints / undo | Medium | Safety net for write operations |
| P1 | Per-token streaming (Anthropic) | Medium | UX feels broken without typing effect |
| P1 | Mid-turn interrupt | Low | Can't stop a bad generation |
| P2 | Command allowlists + protected paths | Low | Permission model is too coarse |
| P2 | Sandbox (basic: chroot or namespace) | High | Security for untrusted tool execution |
| P2 | Plan mode (read-only tools) | Low | Safe exploration before committing |
| P3 | Hooks system | High | Extensibility for custom workflows |
| P3 | MCP client (replace REST tools) | Medium | Auto-discover orchestrator tools |
| P3 | Diff rendering in UI | Medium | Better file change visibility |
| P3 | Slash commands | Low | Power user convenience |

## Bottom Line

We built the foundation correctly — the loop, transport, tool system, and delegation model are architecturally sound and match industry patterns. But we're at **MVP level** compared to production agents that have had 12-18 months of hardening. The critical gaps (session persistence, compaction, sandbox) are what separate a demo from a product. The good news: most of these are incremental additions to infrastructure we already have, not architectural rewrites.
