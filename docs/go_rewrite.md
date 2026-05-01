# Go Rewrite — Design Notes

Forward-looking architectural notes for an eventual rewrite. Not a commitment, not a plan, not scoped. A scratchpad for ideas that come up while shipping the Python version.

## Core Thesis

The current system is bottlenecked by Python and LangGraph's loop-driver assumptions, not by the agent model itself. The agent model is sound: phase-alternating state machine, checkpointed at node boundaries, workspaces externalized via SSH. What's wrong is that **the graph drives itself in one process** — meaning the pod owns the loop, owns in-memory state, and is therefore non-fungible.

The rewrite flips this: **the database drives the graph, pods are stateless turn executors**.

## Architecture Sketch

### State lives in Postgres, period

- No SQLite checkpointer, no in-process state.
- Graph state (messages, todos, phase flags, freeze_data, staged_todos) is a single JSONB row in Postgres keyed by `thread_id`.
- Every node execution is a transaction: `SELECT FOR UPDATE` the row, run one turn, `UPDATE` the row, `NOTIFY` the next worker.

### Pods are stateless turn executors

- Not "Agent X processes Job Y forever." Instead: "any pod pulls from a work queue, executes one turn, releases."
- One turn = one LLM generation + the resulting tool/action call + checkpoint write.
- Mid-stream stays on one pod (don't try to migrate a partial token stream — re-run from last checkpoint if pod dies).
- `kubectl scale deployment/agent --replicas=N` actually means something. HPA on queue depth.

### Work queue via Postgres

- `Postgres LISTEN/NOTIFY` for low-latency dispatch (no Redis, no NATS for this path).
- Lease semantics: claim a thread_id with a short TTL (e.g., 60s), heartbeat extends the lease, expired leases get re-claimed.
- Two pods cannot run the same thread concurrently — enforced by row lock on the lease column.
- Orphaned-job recovery becomes trivial: lease expires, queue picks it up.

### Tool calls become idempotent

- Every tool call gets a UUID at dispatch time.
- Workspace-side dedup: "have I seen this tool_call_id before? Return cached result."
- Makes "at-least-once with safe replay" the default. Pod can die mid-tool-call without corrupting workspace state.

## Why Go Specifically

| Property | Python (current) | Go (target) |
|---|---|---|
| Cold start | ~3-5s (imports + LangGraph init) | <100ms |
| RSS per worker | ~300-500MB | ~10-30MB |
| Concurrency model | asyncio + one process per pod | goroutines + N workers per pod |
| LLM streaming | `astream` + RemoveMessage gymnastics | channels, native backpressure |
| Compile-time guarantees | None (config bugs at runtime) | Strong typing on State, ToolCall, Phase |
| Deploy artifact | ~1.2GB image (Python + deps + Playwright) | ~50MB static binary |

The big unlock isn't raw speed — Python is fine for I/O-bound LLM work. It's:

1. **Memory footprint** → can run 50 workers per node instead of 4.
2. **Cold start** → HPA actually responsive, can scale to zero between bursts.
3. **Goroutines + channels** → the per-turn-per-worker model maps natively. No need to bolt async onto a sync graph framework.

## What Stays The Same

- Postgres schema (jobs, threads, datasources, sudo_approval_requests). Already DB-driven.
- pgvector for memory/embeddings. Already external.
- Workspace backends (SSH/SFTP). Already externalized.
- Cockpit (Angular). Frontend is decoupled, talks REST/WS.
- Orchestrator API contract. Just the implementation rewrites.
- YAML config system (experts, prompts, instruction matrices). Parse with `yaml.v3`, deep-merge in Go.

## What Changes

### Graph

Replace LangGraph with a hand-rolled state machine. The 10 nodes are well-defined enough that a 500-line `graph.go` with a switch statement on `phase_state` would be cleaner than the current dynamic-dispatch setup. Bonus: phase-restricted tools become a compile-time check on a sum type, not a runtime gate.

### LLM Client

`anthropic-sdk-go`, `openai-go`, plus a thin adapter for Groq/local. Streaming via `iter.Seq2[Chunk, error]` (Go 1.23+ range-over-func). Cache control on system blocks for prompt caching.

### Tool Registry

Tools as a typed interface:

```go
type Tool interface {
    Name() string
    Phase() PhaseRestriction
    Schema() jsonschema.Schema
    Execute(ctx context.Context, args json.RawMessage, ws Workspace) (ToolResult, error)
}
```

Registry is a `map[string]Tool`, populated at init. Phase restrictions checked at the type level where possible.

### Orchestrator

This is the trickier one — the current orchestrator is ~11500 lines of FastAPI in one file. Rewrite path is probably **incremental**: keep Python orchestrator, swap agent first. The HTTP contract between them is already well-defined (`/api/jobs/{id}/complete`, heartbeat, dispatch). Once agents are Go, then port orchestrator endpoints one router at a time.

### Frontend — Server-Rendered, Agent-Composable, Voice-First

This is the part that justifies "rewrite" instead of "refactor." The Angular SPA is replaced with a **server-rendered, component-based UI that the agent can dynamically compose**. Voice becomes the primary I/O channel between user and agent; the UI becomes a visual side-channel the agent paints on to show what's happening.

**The shape:**

- Server-rendered HTML, partial swaps via HTMX (or templ + server-driven reactivity à la Phoenix LiveView).
- A finite, well-typed **component catalog**: `<JobCard>`, `<FleetGrid>`, `<DiffViewer>`, `<LogTail>`, `<DashboardPanel>`, etc. Each component has a documented prop schema. The debug cockpit's component-based architecture is the seed of this catalog.
- The agent gets a **UI control tool** with a constrained action space: `open_panel(component, props)`, `close_panel(id)`, `update_panel(id, props)`, `highlight(selector)`, `swap_view(layout)`. Not freeform HTML/CSS — that path leads to inconsistency and token waste.
- **Auxiliary UI model** runs in parallel to the main agent (like the memory curator). Smaller/faster model dedicated to UI orchestration: deciding when to open a new pane, which dashboard layout fits the current conversation, what to surface vs. hide. Non-blocking — main agent reasons, UI model paints.
- **Voice loop** uses native voice-input model + GPU TTS co-located on the same cluster. See "Voice pipeline — dual-track speak-while-thinking" below for the actual approach.

**Why server-rendered specifically:**

- Agent needs to *see* what it rendered. Server-rendered HTML can be screenshotted, DOM-snapshotted, or fed back to the agent as structured tree without going through a browser at all.
- Same template renders for browser AND for agent verification — single source of truth.
- No JS bundle round-trips for every UI mutation.
- Server holds session UI state — agent and server agree on "current layout" without a sync layer.
- Cold render is fast; partial swaps are cheap.

**Verification feedback loop — two-pass rendering:**

- **Fast pass (50dpi)** — low-res screenshot rendered immediately, fed back to agent for "is the layout roughly right?" Cheap enough to run on every mutation.
- **Slow pass (600dpi)** — high-res render runs async in the background, agent only consults it when fine details matter ("is the chart legend readable?", "are these two columns aligned?"). Doesn't block the conversation.
- **Structured assertions** as the cheapest path: DOM tree + "is `<JobCard id=X>` visible? Does it show status=running?" — preferred when the question is functional rather than aesthetic.

This three-tier verification (structured → 50dpi → 600dpi) keeps the common case cheap and reserves vision-model cost for the questions that actually need pixels.

**Voice pipeline — dual-track speak-while-thinking:**

Naive STT→LLM→TTS pipelines hit 1-3s round-trip and feel broken. The actual approach:

- **Native voice-input model** (not STT→text→LLM). Audio goes directly into a model that takes voice tokens.
- **GPU TTS co-located on the same cluster** so the response audio doesn't round-trip across networks. Groq-tier inference if local isn't fast enough.
- **Two async generations triggered by every user turn:**
  1. **Fast track** — small/fast model formulates an immediate spoken response. Optimized for latency, not depth. Output: "Yeah, so... let me think about that for a second..." or a quick partial answer that buys reasoning time.
  2. **Slow track** — main reasoning model does proper thinking, RAG, tool calls, etc. Output: the substantive answer.
- **Merge layer** stitches them into a single coherent spoken stream. Fast track talks first (filler, acknowledgment, partial answer), slow track interrupts with the real content when ready. Like how humans actually talk: *"Well, ahh... <pause> okay so I thought of this..."*

This isn't a hack — it's modeling the same dual-track behavior humans use to bridge the gap between "I heard you" and "I have a thought-through answer." The merge layer is the interesting engineering problem; the fast/slow tracks are commodity inference.

**Component schema as a model concern, not a prompt concern:**

The constrained UI action space (`open_panel`, `update_panel`, etc.) is the v1 shape, but the v2 shape is **specialized models that natively understand the layout vocabulary**:

- **Option A** — Finetune the main agent model on the component catalog so it picks layouts naturally without needing tool-call scaffolding.
- **Option B** — Train a small specialized translator model (~1B params) that takes "I want to show the user the running jobs and the latest PR diff" as text input and emits `layout + HTML` as output. Dedicated hardware, fast inference. Decoupled from main agent reasoning.

This mirrors the broader trend of LLMs moving away from rigid tool-call schemas toward natural code generation (e.g., agents writing `python -c` instead of calling structured tools). For UI, the same pattern: emit layout descriptions natively, let a specialized model handle the structured output.

**UI rearrangement — start conservative, lock-button as escape hatch:**

- **Default behavior is sticky.** Agent shows X, user inspects, AI doesn't touch the UI again until instructed. Compute cost naturally rate-limits aggressive rearrangement, so this falls out of the architecture rather than needing explicit policy.
- **Lock button** on individual panels for the cases where the user wants to be sure the agent won't reflow during a long inspection.
- Soft-rearrange (animation, "closing this — say 'wait' to keep") as a fallback for cases where the agent *does* need to reflow but should warn first.
- Real testing is the arbiter — paper design only goes so far on this.

**Open UX questions (still open):**

- **Voice + keyboard duality.** Power users will still want keyboard shortcuts for common actions. Voice is primary but not exclusive. Hotkeys + voice + agent-control all need to coexist without stepping on each other.
- **Multi-pane state addressability.** URL deeplinks should restore a layout. Probably: layout state serialized in the URL or session, agent actions produce a layout diff that's applied + recorded.

**Why this is the strongest justification for the rewrite:**

The orchestrator UI is fundamentally read-heavy — fleet status, job timelines, PR diffs, dashboards, logs. It's not Figma or Excel. That's exactly the workload AI-composed UI is good at: *display surfaces*, not high-interaction tools. Voice + visual side-channel is a genuinely novel UX pattern — not "voice mode" (no visual context) and not "artifact panel" (single surface). It's a composable workspace the agent paints on while you talk. For an agent-fleet orchestrator, that's the right primitive.

Server-rendered + Go also means the frontend stack collapses into the same binary as the agent/orchestrator — no separate Node toolchain, no `npm ci`, no Angular CLI, no separate container. One static binary serves API + HTML + WebSocket + voice channel.

## Open Questions / Things To Figure Out Later

- **LangGraph's interrupt model** (HITL approval gates) — how to replicate cleanly without LangGraph's built-in support? Probably: a `pending_approval` row, agent loop checks it at each turn, sleeps until resolved.
- **Auxiliary LLM tasks** (memory extraction, knowledge curation) — currently `asyncio.create_task()`. In Go, just goroutines on a separate worker pool, with a bounded channel.
- **Context compaction** — the RemoveMessage / token-threshold logic ports straightforwardly, but worth designing the message-store as append-only with a "live cursor" rather than mutating an array.
- **Migration story** — can a Go agent and a Python agent coexist on the same orchestrator during cutover? Probably yes if both honor the same HTTP contract and checkpoint format. Worth keeping the checkpoint format JSON (not pickle, not protobuf) for this reason.
- **Embedded SQLite vs Postgres for tests** — Go has good Postgres-in-test stories (`pgtest`, dockertest), but startup cost matters. Maybe `pgx` against a shared test instance with schema-per-test.

## Non-Goals

- ~~Rewriting the Cockpit. Angular is fine.~~ — Reversed. The frontend rewrite (server-rendered, agent-composable) is now the *centerpiece* of the rewrite, not a non-goal. The debug cockpit's component architecture migrates forward as the seed component catalog.
- Replacing pgvector. It's the right tool.
- Distributed tracing infra (OTEL is good, keep it).
- Custom LLM inference. Always call out to providers.
- Custom voice models. Use Deepgram/Whisper-streaming for STT, ElevenLabs/Cartesia for TTS. Voice infra is a deep specialty; don't build it.

## When To Actually Do This

Not now. Not next quarter. The Python version is shipping, the architecture is still settling, and rewrites done before the design has stabilized are wasted work. The signals to revisit:

1. Memory cost of agent pods becomes a real bill line item.
2. Cold-start latency on autoscale starts hurting UX.
3. The agent codebase stops changing weekly (i.e., the design has converged).
4. There's a multi-week window where the team can stop shipping features.

Until then: this doc is a graveyard for architectural daydreams.
