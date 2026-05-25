---
tags:
  - feature
  - sessions
  - agent
  - cockpit
  - architecture
  - message-state
  - streaming
aliases:
  - durable session state
  - source of truth refactor
  - event-sourced sessions
  - eliminate in-memory message list
related:
  - "[[headless_persistent_sessions]]"
  - "[[sessions]]"
  - "[[agent_lifecycle]]"
  - "[[direct_session_websockets]]"
  - "[[workspace_warm_pool_and_async_sessions]]"
  - "[[persistent_chat_visual_refresh]]"
  - "[[custom_llm_endpoints]]"
  - "[[llm_logging]]"
---

# Persistent Session — Durable Source of Truth

**Status**: Design — brainstormed + research-validated 2026-05-24. **Plan 1 (foundation) implemented & deployed to dev 2026-05-25**; Plans 2–4 pending. See *Implementation status* below.
**Scope**: Persistent interactive sessions only. Worker-agent unification is **out of scope** (separate follow-on spec).
**Supersedes the runtime model in**: [[headless_persistent_sessions]] (which built the Phase-2 event log but left the in-memory list authoritative).

## Implementation status (2026-05-25)

**Live on dev** (both `gemma-moe` and `gpt-5.5` sessions confirmed working):
- **I1/I2 stability guard** — `src/llm/response_guards.py` (commit `fdcb5f97`): coerces/drops empty streamed chunks at the append boundary. This is what stops the "malformed response" crash today.
- **Plan 1 — component-store foundation** (`docs/superpowers/plans/2026-05-24-persistent-session-component-store.md`, ✅ complete): migrations `0019`/`0020` (component columns + windowed-hydration index, applied & verified); the provider adapter `src/llm/session_components.py` (`normalize_response` + `components_to_provider_messages`); the lossless agent-side history reader; and write-path persistence of all component columns. **Additive and dormant** — present but not yet wired into the agent loop, so it changes no behavior yet.

**Pending:**
- **Plan 2 — authority inversion (D1):** rebuild working context from the transcript via the adapter; retire the in-memory list as source of truth; non-destructive compaction (Q4). *This is the change that makes the foundation live and renders the I1/I2 guard redundant.*
- **Plan 3 — streaming split (D3) + transport retirement (Q1).**
- **Plan 4 — frontend windowing (D4).**

**Key correction (Task 1, 2026-05-25):** the live `gpt-5.5` path is **Chat Completions, not the Responses API** — so D2's reasoning-pairing / `encrypted_content` constraints are *forward-compat only* (see D2).

## Motivation

The persistent-session stack grew organically: worker agent → added persistence → added headless mode → outsourced the WebSocket. Each layer was bolted on without a unifying state model, so today there are **three overlapping transports** (live WS, `thread_events`+SSE, NATS `session.events`) and **two message representations** (the agent's in-memory list and the `thread_messages` transcript) — and they can diverge.

The root disease, confirmed while debugging the "malformed response" incident (see `docs/issues/persistent_session_empty_chunk_history_corruption.md`): **the agent's runtime treats its in-memory message list as the source of truth and writes the durable tables as side-projections.** Durable state is *downstream* of RAM when it should be *upstream*. Every issue in that incident's cluster (I3–I6) is a symptom:

- **I3** in-memory ↔ Postgres divergence (non-atomic persist)
- **I4** in-memory never reloaded → corruption is sticky, retries re-fail
- **I5** transcript is lossy (no `additional_kwargs`/`response_metadata`/reasoning items)
- **I6** no durable per-step state; mid-turn crash loses in-flight work

I1/I2 (the shipped `response_guards` normalization) are a band-aid on the in-memory list. This refactor makes that band-aid **moot** by removing the in-memory list's authority.

## Goals / non-goals

**Goals**
- One **durable source of truth** for session state; the agent's in-memory list becomes a disposable per-turn projection rebuilt from it.
- A **provider-agnostic component model** for LLM exchanges, lossless enough to faithfully replay to the originating provider.
- **Decouple** the agent from any viewer: the agent appends to durable state and an event stream; clients tail. The agent runs identically whether or not anyone is watching.
- **Windowed** frontend hydration so a client never syncs a whole conversation up front.

**Non-goals (this spec)**
- Unifying the worker-agent graph (`graph.py`) onto this model.
- Replacing the LangGraph engine wholesale (durable-execution engines remain "considered, not adopted" — see [[headless_persistent_sessions]] ADR).
- Changing tool execution, permission semantics, or the workspace/pod lifecycle.

## What already exists (and stays)

Accurate as of 2026-05-24:

- **`thread_events`** (migration `0004_thread_events.sql`) — append-only wire-frame log `(thread_id, epoch, seq, kind, payload JSONB)`, unique on `(thread_id, epoch, seq)`. The agent writes every frame the cockpit sees; clients replay from their last `(epoch, seq)` via SSE `GET /api/threads/{id}/stream`. `threads.events_epoch` bumps on cold-checkpoint restart → `GONE_BEYOND_HORIZON` full resync.
- **`thread_messages`** — durable transcript `(role, content, tool_calls, turn_number, metrics)` + (migration `0011`) `tool_call_id, thinking`. One row per completed message.
- **NATS** `session.events.{tid}` bus → `orchestrator/services/nats_bridge.py`. Optional (no-op when unconfigured).
- **Last-Event-ID** resumable cursor (`<epoch>:<seq>`) already parsed by the orchestrator.
- **SSE is already the primary server→client path**, and user input + interrupt already use REST. The remaining live WebSocket is a *direct cockpit→agent-pod* control channel (not an orchestrator proxy), carrying ~8 client→server verbs and their non-persisted acks — that residue is all D5/Q1 retires.

The bones of the target are here. The refactor **inverts authority** over them and **consolidates** the overlap, rather than building greenfield.

## Design decisions (settled during brainstorming)

### D1 — Durable transcript is the source of truth; in-memory is a projection
The agent rebuilds its working context from the durable transcript at the start of each turn (or maintains an in-process cache that is provably derived from it, never the authority). Removing the in-memory list's primacy closes I3/I4 and makes the I1/I2 guard redundant. A reconstructed message always has a canonical type, so the `Got unknown type` class cannot recur.

### D2 — Normalized 4-component model **plus** provider-raw payload
Every LLM exchange is stored as four normalized components — **reasoning, text, tools (tool calls), tool results** — *and* the **provider-raw response payload** for the assistant turn. Rationale (chosen fork): a purely normalized store loses provider-specific replay material (OpenAI Responses-API reasoning items, Anthropic cache-control breakpoints). Storing both lets the **provider adapter** prefer raw for same-provider replay and use the normalized components for UI, logic, cross-provider portability, and audit. Storage is cheap; fidelity is preserved.

**Provider replay reality (Task-1 capture, 2026-05-24 — corrects an earlier assumption).** The *current* primary model (`gpt-5.5` via the codex proxy) speaks **OpenAI Chat Completions** — the proxy wraps a Responses backend in a `chat.completion` envelope, so langchain sees CC. For this path:
- Reasoning is a flat `reasoning_content` string (often `null`/hidden); it is **display/audit only** — CC does not replay reasoning between turns.
- Tool calls/results use the standard CC shape; a faithful request is reconstructable from the **normalized** components alone (assistant `content` + `tool_calls`; tool results as `role=tool` + `tool_call_id`). **No reasoning-pairing/ordering constraint.**
- So `provider_raw` here is **audit/forensics + forward-compat**, not replay-critical.

**Forward-compat (NOT the live path — store raw now so these are a drop-in later, not a schema migration):** the real **OpenAI Responses API** enforces reasoning↔`function_call` pairing/order (hard 400s; `rs_` ids, `encrypted_content`, `previous_response_id`); **Anthropic** requires byte-for-byte `thinking`+`signature` replay. If either is adopted, raw replay becomes load-bearing and the adapter gains a provider-specific path.

### D3 — Streaming split: ephemeral deltas, durable components
- **Token deltas** stream via **NATS only** (ephemeral, not persisted) — kills the per-token write amplification of today's `thread_events`.
- **`thread_events` persists only coarse frames**: turn/tool lifecycle + each **completed component** (+ status, permission requests).
- **Reconnect** replays coarse frames + completed components from durable state, then resubscribes to live deltas. A client joining mid-turn sees the turn-so-far as finished components (no token re-animation), then live tokens resume.

### D4 — Frontend windowing: three windows, two cursors
Three independent windows, designed separately:
1. **Render window** (~30 in DOM) — virtual scroll, frontend-only.
2. **Client cache** — **IndexedDB**, session-scoped + evictable (LRU by thread). *Not* a durable offline archive in v1 (revisit if offline history becomes a goal).
3. **Hydration payload** — backend sends the last ~30–50 completed messages on join; older history fetched on scroll-up.

Two cursors fall out of the architecture:
- **Live cursor** `(epoch, seq)` into the event log — mid-turn tailing + reconnect-resume.
- **History cursor** (message id / `created_at`) into the transcript — scrollback pagination.

On join: `GET last N messages` (history cursor) to paint the window + subscribe from the live cursor. **Critical invariant:** the frontend display window is fully independent of the agent's LLM-context window — both read the same transcript and window independently. "Last 30" never constrains what the agent remembers.

### D5 — Collapse transports toward event-log + NATS
Target: durable **completed** state in the transcript; **coarse durable frames** in `thread_events` (SSE replay); **ephemeral deltas** over NATS. The bespoke live WS proxy is retired in favor of the SSE-replay path for client delivery (concrete retirement sequence in *Resolved decisions → Q1*).

### D6 — Per-component error isolation
Each of the four components is validated and persisted independently. A dropped or garbled stream packet degrades a single component (e.g. lost reasoning) **without breaking the turn chain or poisoning the transcript** — the explicit "chain doesn't break if we lost a packet" requirement. Empty/degenerate components are never persisted, which generalizes the shipped I1/I2 `response_guards` rule from the in-memory append sites into the durable write path (so the guard exists in exactly one place: the write boundary). **Pairing rule:** the unit of drop/repair is the **exchange** — never orphan a `tool_call` from its `tool_result` (or vice-versa), and truncate/window only at **exchange boundaries**. For the live Chat-Completions path that is the *only* pairing constraint; the stricter reasoning↔`function_call` pairing (a hard 400) applies only to the forward-compat Responses-API/Anthropic adapters. Completed components are finalized to durable state even if the client disconnects mid-stream.

## Data model (sketch — to be finalized in the plan)

```
-- thread_messages: EXTEND in place via migration 0019 (don't replace — see Q3).
-- Append-only / immutable: one row per completed component; rows are never UPDATEd.
thread_messages  (durable transcript = source of truth)
  -- existing columns --
  id, thread_id, turn_number, role, content (TEXT), tool_calls (JSONB),
  tool_call_id, thinking (legacy-read; superseded by `reasoning`), metrics, created_at
  -- new in 0019 (all nullable → metadata-only migration; dev-cutover, no backfill) --
  reasoning         JSONB  -- normalized reasoning items (written going forward)
  tool_results      JSONB  -- normalized, linked by tool_call_id
  provider          TEXT   -- openai-responses | anthropic | ...
  provider_raw      JSONB  -- verbatim, ORDER-preserving completed-response items (D2)
  additional_kwargs JSONB  -- closes I5
  response_metadata JSONB  -- closes I5
  -- new index, CONCURRENTLY in a separate .notx migration, for windowed hydration (D4) --
  INDEX (thread_id, turn_number, created_at)

thread_events  (coarse durable frame log — exists; payload vocabulary trimmed)
  thread_id, epoch, seq, kind, payload, created_at
  -- kind ∈ {turn.started, turn.completed, tool.started, tool.completed,
  --         message.completed, status, permission.request, ...}
  -- NO token-delta rows (deltas are NATS-only)
```

The agent **appends** a `thread_messages` row when a component completes, and emits the matching coarse `thread_events` frame in the same unit of work (atomic — closes I3). Rows are never mutated — this immutability is what makes compaction non-destructive (Q4) and yields event-log benefits without a separate event store. **Prerequisite fix:** the agent-side reader `src/database/postgres_db.py::get_thread_messages_history` (~line 331) currently omits `tool_call_id`/`thinking`; it must select the full column set for the D1 rebuild to be lossless.

## Data flow

**Turn (agent side)**
1. Rebuild working context from `thread_messages` (provider adapter: components/raw → provider request).
2. Stream from the provider. Token deltas → NATS `session.events.{tid}` (ephemeral).
3. On each completed component: write `thread_messages` row + `thread_events` coarse frame (one unit of work).
4. Per-component error handling (see D6): a malformed/empty component is dropped or repaired; the turn chain does not break.

**Join / reconnect (client side)**
1. `GET` last N messages (history cursor) → paint render window; cache in IndexedDB.
2. Subscribe from live cursor `(epoch, seq)`; if epoch stale → `GONE_BEYOND_HORIZON` → refetch window.
3. Live token deltas arrive via the stream; completed components reconcile against the cached/rendered window.

## Error handling

- **Per-component isolation** (see **D6**) — a dropped/garbled stream packet degrades one component without poisoning the turn or the transcript; empty/degenerate components are never persisted.
- **Atomic append** (D1/D3): transcript row + event frame committed together; no divergence window.
- **Epoch resync**: unchanged from Phase 2 — stale client cursor forces a clean window refetch.
- **Crash mid-turn**: completed components are already durable; a fresh agent rebuilds from them and continues. No in-memory loss of finished work (closes I6 for completed-component granularity).

## Testing strategy

- **Provider adapter** round-trip unit tests per provider: response → components(+raw) → request, asserting faithful replay (esp. Responses-API reasoning items, Anthropic cache-control). Real fixtures, no mocks.
- **Projection rebuild**: transcript rows → working context yields canonical message types (regression for the `Got unknown type` class).
- **Streaming split**: deltas are NATS-only; `thread_events` contains no token rows; reconnect replays completed components + resumes live.
- **Windowing/pagination**: hydration returns last N; scroll-back paginates by history cursor; IndexedDB cache hit avoids refetch; display window independent of agent context.
- **Error isolation**: a malformed component is dropped without breaking the turn or corrupting the transcript.

## Alternatives considered

- **(A) Mutable "current state" + upsert.** Keep one row per message, upsert in place. Rejected: no clean audit/replay, and the in-memory↔DB sync problem persists.
- **(B) Pure append-only event log as the single source; transcript is a materialized view.** Full CQRS/event-sourcing. Attractive, but a bigger rewrite and it would durably persist garbage frames unless guarded; the transcript-as-truth + coarse-event-log split (chosen) gets most benefits with less blast radius and reuses today's tables.
- **(C) Adopt a durable-execution engine (Temporal / LangGraph PostgresSaver).** Still "considered, not adopted" per [[headless_persistent_sessions]]; revisit if/when the worker+persistent unification spec happens. A checkpointer would persist the empty-chunk poison durably unless combined with the D6 write-path guard.

**Chosen: D1–D6** — transcript (components + raw) as source of truth, coarse event log for replay, NATS for ephemeral deltas, windowed frontend, per-component error isolation. Best benefit-to-blast-radius given the existing infrastructure.

## Out of scope

- Worker-agent (`graph.py`) unification onto this model.
- Tool execution / permission / workspace lifecycle changes.
- Durable offline conversation archive in IndexedDB (session-scoped cache only in v1).

## Resolved decisions (research-validated 2026-05-24)

Each open question was checked against the codebase (audit) and external best practice (web).

**Q1 — Transport retirement → mostly already done; the residue is narrow.**
There is no orchestrator WS *proxy*; the cockpit dials the agent pod directly and SSE over `thread_events` is already primary (input/interrupt already on REST). The only WS-resident logic is ~8 client→server verbs (approve/deny, `/compact`, `/done`, `/undo`, `mode.set`, `narration.set`, `config.update`, vm-upgrade) **and their acks, which use `_ws_send` (not persisted, not on SSE)**. Sequence: (1) convert those acks to `_broadcast` (persist → replay over SSE); (2) add a REST `/control` endpoint for the verbs (route approve/deny through the existing `/approve/{id}`); (3) repoint the cockpit; (4) delete WS routes + per-session Ingress/token plumbing **last**. Steps 1–2 are additive (old clients keep working) — ship first, then flip, then delete in a later release. Serve SSE over **HTTP/2** (HTTP/1.1's ~6-connections-per-domain cap starves multi-tab); at the replay→live seam, **subscribe to NATS first → read log → dedup by `seq` → go live**, emitting an explicit "live" boundary frame.

**Q2 — Rebuild cost → reload is cheap; memoize tokenization.**
Reloading the transcript is one indexed `SELECT` (trivial). The real per-turn cost is tiktoken **re-tokenizing the whole history every inner iteration, unmemoized** — and that already happens today. Decision: full reload-per-turn is fine; do **not** build a message cache to avoid DB reads. The load-bearing optimization is **token-count memoization keyed by message id** (never re-encode unchanged history). (LangGraph's pickled-blob checkpointer ~4 s deserialize is the cautionary tale for full-replay/opaque-blob state.)

**Q3 — Schema shape → ADD COLUMNS to `thread_messages` (revised from "new table").**
A `session_components` table + view collides with the `CREATE TABLE IF NOT EXISTS thread_messages` baseline, needs `INSTEAD OF` triggers the migration dry-run/squawk gate can't validate, and forces a row-rewrite whose squawk findings are **unfixable-forward** (→ permanent lint exclusions, per `ci_migration_lint_bypassed_by_deploy.md`). Adding nullable columns is the proven `0011` shape: metadata-only, sub-second, squawk-clean. Decision: ship `0019_thread_messages_components.sql` (the columns in the data model above; keep `thinking` as legacy-read), **dev-cutover, no backfill**; add the `(thread_id, turn_number, created_at)` index `CONCURRENTLY` in a separate `.notx` migration. Rows are **append-only/immutable**. Prerequisite code fix: the agent-side reader `postgres_db.py:~331` omits `tool_call_id`/`thinking` today — fix it so D1's rebuild is lossless.

**Q4 — Compaction → derived view; transcript immutable (confirmed).**
Compaction is destructive today (`_session.messages[:] = …`; plus inert `RemoveMessage` leakage — there is no LangGraph reducer in this path). Decision: the transcript is never mutated; compaction produces a **derived** context view = `system(cached) + running_summary + verbatim last-N`, with the summary persisted as its own derived `kind=summary` row. This also deletes the `RemoveMessage`/no-IDs bug class (`persistent_session_restored_messages_no_ids.md`). (Mirrors Anthropic `pause_after_compaction` and LangMem's split `messages` vs `context`; compact proactively at ~60–70 % utilization; summarize hierarchically.)

**Cross-cutting — provider reality (Task-1 capture, corrected 2026-05-24).** The live `gpt-5.5` path is **Chat Completions, not the Responses API** (the proxy wraps a Responses backend in a `chat.completion` envelope). So normalized replay suffices for the live model and `reasoning_content` is display/audit only; `provider_raw` is **audit + forward-compat**. The verbatim/ordered-replay constraints apply only *if/when* the real Responses API or Anthropic is adopted (kept in D2 as forward-compat).

### Still to verify during implementation
- ~~Whether `srw-codex-proxy` forwards Responses reasoning items / `encrypted_content`~~ — **RESOLVED (Task 1, 2026-05-24):** the proxy speaks Chat Completions; reasoning is a flat `reasoning_content` (often null/hidden); no Responses items. Open follow-up: confirm whether `reasoning_content` is *ever* populated for `gpt-5.5` (a reasoning-heavy capture 503'd) — determines whether the reasoning component is ever non-empty for the live model.
- Exact `epoch`-bump semantics and dedup window at the replay→live seam under load.

## Related code

- `src/persistent_graph.py` — the loop holding the in-memory list (target of the inversion).
- `src/llm/response_guards.py` — I1/I2 guard, generalized into the write path here.
- `src/api/persistent_app.py` — session WS/SSE + NATS publish.
- `orchestrator/services/nats_bridge.py` — NATS session-event bridge.
- `orchestrator/database/migrations/app/0004_thread_events.sql`, `0011_thread_messages_tool_link_and_thinking.sql`.
- `src/database/postgres_db.py::get_thread_messages_history` — resume rehydration (currently omits `tool_call_id`/`thinking`; prerequisite fix — see Q3).
- `cockpit/src/app/core/services/persistent-chat.service.ts` — client stream/cache/window.

## Related issues

- `docs/issues/persistent_session_empty_chunk_history_corruption.md` (I1–I9; this spec resolves I3–I6 structurally).
- `docs/issues/persistent_session_restored_messages_no_ids.md`, `persistent_session_runaway_generation_context_explosion.md`, `langchain_responses_api_streaming.md`.
