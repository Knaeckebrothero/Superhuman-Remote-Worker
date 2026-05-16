---
tags:
  - feature
  - sessions
  - agent
  - cockpit
  - architecture
  - notification
aliases:
  - decoupled sessions
  - headless sessions
  - browser-independent sessions
  - eager sessions
  - session continuation
related:
  - "[[sessions]]"
  - "[[agent_lifecycle]]"
  - "[[unified_instance_lifecycle]]"
  - "[[notify_user_tool]]"
  - "[[ephemeral_workspaces]]"
  - "[[sudo_permissions]]"
  - "[[workspace_warm_pool_and_async_sessions]]"
  - "[[persistent_chat_visual_refresh]]"
---

# Headless Persistent Sessions

> The session is the source of truth, not the WebSocket. Close the browser and the agent keeps working. Reopen and the cockpit catches up like it never left. When attention is needed and nobody is watching, the system reaches out.

**Status:** Phases 1-6 shipped 2026-05-12/13. Cockpit WS→SSE migration also shipped 2026-05-13. All filed hygiene issues resolved. Feature is complete for v1.
**Filed:** 2026-05-12
**Last updated:** 2026-05-13

## Motivation

The persistent agent is supposed to be a long-running collaborator — start a research run before lunch, come back after, see what it found. In practice, the browser tab *is* the session today: closing it cancels the in-flight loop, drops streaming output on the floor, and leaves any pending permission request dangling on an in-memory future that nobody will ever resolve.

This is a UX cliff. The user's mental model — "the agent is doing the work, the cockpit is a window onto the work" — does not match the implementation, where the cockpit is the loop's lifeline.

Three things change with this feature:

1. **The loop stops being WS-bound.** Close the browser; the turn completes, the next one starts, work continues to a natural pause.
2. **Reconnect is replay, not restart.** The cockpit reattaches at the cursor it left off — same view it would have had if it had stayed open.
3. **Attention bridges leave the cockpit.** When the agent needs something from the user and no UI is attached, the system delivers via email (existing channel) and queues for richer channels (Web Push, mobile) later.

The user gets a real "agent that runs while I'm asleep" instead of an interactive chat that happens to remember its messages.

## Decision: eager, not polite

Two stances were considered for "what does the agent do when nobody is watching":

- **Polite.** Finish the current turn, park, don't start a new one until the user reattaches or acks a notification. Cheap, conservative, matches "the user drives."
- **Eager.** Keep generating. Plan, execute tool calls, finish todos, reach a real stopping point even with no UI attached. More expensive in tokens and pod time, but recovers the "agent runs while I'm asleep" promise.

**Chosen: eager**, subject to the safety rails in [Eager-mode bounds](#eager-mode-bounds). The polite stance is recoverable as a per-thread setting for users who want it.

## Decision: SSE for stream, REST for input

The existing transport is a single WebSocket (`/ws/persistent/{thread_id}` in `orchestrator/main.py:10931`). It works, but the reconnect state machine in `persistent-chat.service.ts` is where every silent-disconnect bug has lived ([`docs/issues/persistent_chat_silent_disconnect.md`](../issues/persistent_chat_silent_disconnect.md)). The 2025 industry consensus has moved back the other way: SSE for server→client streaming + a small REST channel for client→server input. Vercel AI SDK 5 (July 2025) defaults to SSE, ElectricSQL launched [Durable Streams](https://electric-sql.com/blog/2025/12/09/announcing-durable-streams) specifically for resumable LLM token streaming over HTTP, and the "persist token before send + browser-native `Last-Event-ID` reconnect" pattern has become the default in ChatGPT, Claude.ai, and the Vercel AI SDK.

**Chosen: SSE + REST.** Server→client over `GET /api/threads/{id}/stream` with `Last-Event-ID` header for native browser reconnect. Client→server via `POST /api/threads/{id}/input` (turn input, slash commands, approvals, interrupts). The existing WebSocket handler stays available as a fallback for environments where corp proxies break SSE — gated by a feature flag, opt-in only.

This decision removes a whole class of bugs: there is no client-side reconnect state machine to maintain, since SSE's `EventSource` retries automatically and replays from `Last-Event-ID`. It also gives us symmetric semantics for browser tabs, MCP clients, and curl-based dev tooling — all consume the same stream.

The seq/epoch protocol in [Per-thread event log](#1-per-thread-event-log) is the same regardless of transport — SSE just provides the wire format. If we later need bidirectional duplex, WebSocket is the documented fallback.

## Implementation status (2026-05-13)

Backend for all six implementation phases landed between 2026-05-12 and 2026-05-13. The cockpit-side work is the largest remaining slice — the SSE consumer migration in `persistent-chat.service.ts` is the keystone that makes the new transport actually reach users; the Phase 6 settings panel rides along.

| Phase | Backend | Cockpit | Notes |
|---|---|---|---|
| 1 — Loop decoupling + SSE transport | ✅ shipped (`7c8d544`, `37a1692`) | ✅ shipped 2026-05-13 | EventSource + REST `/input`/`/interrupt`; WS retained as control plane only |
| 2 — Event log + epoch + horizon | ✅ shipped (`37a1692`, migration `0004_thread_events.sql`) | ✅ shipped 2026-05-13 | Cursor in IndexedDB (`threadCursors`, schema v3); `gone_beyond_horizon` reloads transcript + reopens |
| 3 — Permission gates via DB | ✅ shipped (`128f702`, migration `0005_thread_permission_requests.sql`) | n/a | LISTEN/NOTIFY path live; existing approval UI keeps working |
| 4 — Notification fan-out + magic links | ✅ shipped (`13f7461`, migration `0006_headless_notifications.sql`) | ✅ settings UI shipped via Phase 6 | Two hygiene gaps resolved 2026-05-13 — see [Resolved issues](#resolved-issues) |
| 5 — Attention sleep | ✅ shipped (`0e5994e`, migration `0008_thread_awaiting_user.sql`) | n/a | Cluster smoke green 2026-05-13; smoke-leak runbook callout shipped |
| 6 — Polite mode + per-thread TTL | ✅ shipped (`6203057`) | ✅ shipped 2026-05-13 | `HeadlessConfig` dataclass + sweeper COALESCE + 3 cockpit controls under "Persistent Agent" |

### Resolved issues

Surfaced-then-fixed bugs from the post-rollout cluster smoke. Kept inline as a record of the fix cycle.

**Shipped 2026-05-11** — two recovery bugs from the PDF-poisoning incident:

- ✅ [[persistent_session_runaway_generation_context_explosion]] (resolved 2026-05-11) — New `_resolve_max_output_tokens(config, limits)` helper in `src/core/loader.py` derives `min(16384, ctx // 4)` when `max_output_tokens: null`, applied to all five non-Anthropic providers (`_create_openai_llm`, `_create_google_llm`, `_create_groq_llm`, `_create_openrouter_llm`, `_create_codex_llm`). Secondary backstop in `summarize_and_compact()` substitutes any non-tool-calling `AIMessage` larger than `model_max_context_tokens / 2` with an "elided by compaction — likely runaway generation" stub, with `_substitution_only_result()` bypassing the skip-compaction guard so poisoned sessions self-heal. Test coverage in `tests/test_context_safety.py::test_oversized_aimessage_replaced_with_stub`. See [`docs/issues/persistent_session_runaway_generation_context_explosion.md`](../issues/persistent_session_runaway_generation_context_explosion.md).
- ✅ [[persistent_session_restored_messages_no_ids]] (resolved 2026-05-11) — `_restore_session_messages()` in `src/api/persistent_app.py:2379` now mints a fresh UUID for every rehydrated `HumanMessage` / `AIMessage` / `ToolMessage`, so the compactor's `RemoveMessage` markers actually target something. Couples directly with #1 — resumed sessions that were already poisoned before the loader fix landed now also self-heal on first compaction. Test coverage in `tests/test_persistent_app.py::TestRestoreSessionMessageIds`. See [`docs/issues/persistent_session_restored_messages_no_ids.md`](../issues/persistent_session_restored_messages_no_ids.md).

**Shipped 2026-05-15** — receive-path liveness for SSE-primary transport:

- ✅ [[persistent_chat_silent_disconnect]] (resolved 2026-05-15) — `thread_event_stream` in `orchestrator/main.py:11082` now emits typed `event: ping\ndata: {}\n\n` frames every ~20s of idle (was a bare `: keepalive\n\n` comment that kept the socket warm but never fired `onmessage`). Cockpit `persistent-chat.service.ts` adds an `_startSseWatchdog(threadId)` setInterval that ticks every 5s and force-closes/reopens the SSE after 45s of total silence; `sseLastEventAt` is bumped from `onopen`, `onmessage`, the typed `ping` listener, and the `gone_beyond_horizon` listener. Detects silent network drops within ~50s instead of waiting hours for OS-level TCP keepalive to trip. WS-side heartbeat deliberately deferred — the SSE+REST migration removed the user-visible failure modes that motivated F4.5 on the WS path. Test coverage in `cockpit/src/app/core/services/persistent-chat.service.spec.ts::PersistentChatService — SSE liveness watchdog`. See [`docs/issues/persistent_chat_silent_disconnect.md`](../issues/persistent_chat_silent_disconnect.md).

**Shipped 2026-05-13** — three tech-debt items from smoke testing, all bundled in one PR (sweeper SQL widening + SMTP TLD blocklist + smoke runbook callout):

- ✅ [[headless_notifications_skipped_status_dedup]] (resolved 2026-05-13) — Sweeper SQL widened to include `skipped_no_email` and `skipped_already_resolved` in the permanent-suppression `IN (...)` set, plus a `2 × interval_s` recency floor (`make_interval(secs => $2)`) for transient `skipped_rate_limit` / `skipped_smtp` cases. In-process `already_notified()` probe deleted in favor of the now-authoritative sweeper SQL. See [`docs/issues/headless_notifications_skipped_status_dedup.md`](../issues/headless_notifications_skipped_status_dedup.md).
- ✅ [[headless_notifications_smtp_recipient_validation]] (resolved 2026-05-13) — Module-level `_is_undeliverable_recipient` helper in `orchestrator/services/email.py` rejects RFC 6761 reserved TLDs (`invalid`/`test`/`example`/`localhost`) plus malformed addresses; `_send` filters before composing. Option B (`email-validator` lib) deliberately deferred. See [`docs/issues/headless_notifications_smtp_recipient_validation.md`](../issues/headless_notifications_smtp_recipient_validation.md).
- ✅ [[headless_sessions_smoke_leaks_cluster_pods]] (resolved 2026-05-13) — `docs/tests/headless_sessions_smoke.md` §P5.6 now opens with a dev-cluster heads-up and the cleanup snippet ends with a `kubectl get | grep ${THREAD_ID:0:8} | xargs delete` step that catches workspace pod + PVC + agent pod in one shot. Option B (env knob) deferred until broader dev-cluster smoke-mode story. See [`docs/issues/headless_sessions_smoke_leaks_cluster_pods.md`](../issues/headless_sessions_smoke_leaks_cluster_pods.md).

### Open issues (post-rollout polish backlog)

Bugs that surfaced during cluster smoke between 2026-05-09 and 2026-05-12 — none blocks the v1 headless feature itself, but each is worth working through. Ordered roughly by user impact (top = most severe).

1. ⏳ [[agent_lifecycle_management]] — Fragmented reconcilers leave orphaned agent pods accumulating across redeploys; the `draining` status is written but never read by any consumer, so drain eligibility is too narrow. Cluster-hygiene problem rather than per-session, but it pages people indirectly. See [`docs/issues/agent_lifecycle_management.md`](../issues/agent_lifecycle_management.md).
2. ⏳ [[persistent_graph_misleading_embedding_connection_error]] — Every user turn logs a six-line "Memory/Knowledge retrieval failed (Connection error)" block even when the embedding endpoint responds 200 OK and retrieval works. Misleads incident response and buries real errors in the noise. See [`docs/issues/persistent_graph_misleading_embedding_connection_error.md`](../issues/persistent_graph_misleading_embedding_connection_error.md).
3. ⏳ [[cloud_sync_phantom_readme_pull_spam]] — OpenCloud's listing surfaces a phantom `README.md` per session; the poller logs the same "pulling README.md" line every 15s for the entire session lifetime. Log spam only, no data corruption. See [`docs/issues/cloud_sync_phantom_readme_pull_spam.md`](../issues/cloud_sync_phantom_readme_pull_spam.md).
4. ⏳ [[persistent_chat_tool_only_messages_not_expandable]] — Tool-only assistant turns are inspectable while streaming, then collapse to a single non-expandable line once the turn finalizes. UX paper cut — the data is still in the event log, just no longer reachable from the UI. See [`docs/issues/persistent_chat_tool_only_messages_not_expandable.md`](../issues/persistent_chat_tool_only_messages_not_expandable.md).

### Remaining work

The migration is complete for v1. Future polish items that are deliberately out of scope:

- **Full client→server REST migration for slash commands.** Approve/deny, compact, archive, undo, mode/narration/config updates currently still cross the WebSocket. They only fire while the user is actively in the cockpit (browser-close-survival doesn't depend on them), so leaving them on WS preserves the headless promise without invasive agent-side refactoring of the existing WS dispatch (which would need `_ws_send` error paths migrated to proper HTTP responses + `_broadcast` for SSE-consumer visibility). Defer until there's a concrete user pull for it.
- **Custom `Last-Event-ID` request header.** EventSource doesn't accept request headers, so the cockpit passes the cached cursor as `?last_event_id=` on the initial connection (backend accepts both). On automatic browser-driven reconnects, the native `Last-Event-ID` header carries the cursor. A polyfilled EventSource would unify the two paths but adds a dependency for marginal benefit.

Items that are explicitly **not** queued (deferred / out of scope by decision):

- Headless budgets (per-run / cumulative token + wall-clock caps) — its own future feature doc.
- Tiered presence-aware attention-sleep defaults — listed in [open questions](#open-questions) #2.
- Per-project eager defaults — [open questions](#open-questions) #3.
- Mobile / desktop push channels — wait for a mobile/desktop cockpit to exist.

## What already exists

A lot of the substrate is already there — this feature is mostly *wiring* and *one new event log*, not a ground-up rebuild.

| Capability | Where | What's missing for this feature |
|---|---|---|
| **Threads as first-class DB objects** | `threads` table, status `active`/`idle`/`ended`. `thread_messages` for transcript. | Nothing — already independent of WS. |
| **Workspace suspension on idle** | `workspace_suspension_service` — idle → S3 snapshot → pod deletion → restore on reconnect. | Need a new trigger: "agent reached natural pause + notification unanswered for X min." |
| **WS reconnect re-hydrates suspended workspaces** | `orchestrator/main.py:10964` in `persistent_ws_proxy`. | Nothing — reattach already does the right thing. |
| **Permission gates in DB** | `sudo_approval_requests` table, [[sudo_permissions]]. | The agent's `wait_for_approval` waits on an in-memory event; needs to wait on the DB (poll or LISTEN/NOTIFY). |
| **Transcript persistence** | `thread_messages` written fire-and-forget after each turn ([[sessions]] Phase A). | Need an event log too — transcript captures completed turns; we also need streaming chunks and tool events to replay mid-turn state on reconnect. |
| **REST history load** | `GET /api/persistent/threads/{id}/messages`, used by cockpit before WS attach. | Extend with a cursor / sequence id so reconnect can ask "events since seq N" cleanly. |
| **Agent-initiated email** | [[notify_user_tool]] (agent-side), parent doc `docs/email_and_mobile.md`. | We need *system*-initiated notifications too — orchestrator pings the user when state needs attention. Same SMTP plumbing; different trigger. |
| **Autonomy levels** | `full | review | partial | guided | dependent` already present on jobs. | Reuse the same lever for "how eager when untethered." |
| **Watchdogs on the agent side** | `_boot_ws_watchdog`, `_thread_status_watchdog` in `src/api/persistent_app.py` cancel via `_detach_session`. | Watchdogs need to detect "untethered" rather than "ws-dropped" — different signal, different action. |
| **Stale-binding repair on reconnect** | `persistent_ws_proxy` clears `agent_id` when bound agent is missing/offline/lost-session. | Works today; will keep working. |

## What's new

The list of net-new pieces is short. Most of the work is plumbing.

### 1. Per-thread event log

A small append-only log of *everything the cockpit would have seen if it had been attached*. Distinct from `thread_messages`, which holds completed turns; the event log holds streaming chunks, tool-call markers, status updates, permission events, errors, narration mode changes — every WS frame the cockpit currently receives.

```sql
CREATE TABLE thread_events (
    id BIGSERIAL PRIMARY KEY,
    thread_id UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    epoch INTEGER NOT NULL,               -- bumped on any server-side rebuild
                                          -- (agent pod restart with cold checkpoint,
                                          --  admin reset, etc.)
    seq BIGINT NOT NULL,                  -- monotonic per (thread, epoch)
    kind TEXT NOT NULL,                   -- 'token', 'tool_start', 'tool_result',
                                          -- 'turn_complete', 'permission_open',
                                          -- 'permission_resolved', 'status', etc.
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_thread_events_thread_epoch_seq
    ON thread_events(thread_id, epoch, seq);
CREATE INDEX idx_thread_events_thread_created
    ON thread_events(thread_id, created_at);
```

**Why `epoch`.** Mirrors Discord's gateway protocol: a session-id bump tells the client "your cursor is from a previous server-side incarnation, do a full re-sync." Without it, clients with stale cursors silently get partial replays after a pod restart — the [Discord-net `#938`](https://github.com/discord-net/Discord.Net/issues/938) infinite-loop class of bug. Bumped by: agent pod restart with cold checkpoint, explicit admin reset, schema migrations that touch event meaning.

**Retention.** Tiered:

- Threads in `active` / `awaiting_user`: keep events 7 days. Long enough that a Friday-evening notification opened Monday morning still replays cleanly. (24h was the original number — too aggressive; it silently broke the email-reattach flow.)
- Threads in `ended`: 24h, then prune. The `thread_messages` transcript is the durable history for these.
- Threads in `suspended`: 7 days from suspension, then prune.

**Write path.** The agent's turn loop writes to the log directly via its `postgres_conn`. Same fire-and-forget pattern as `thread_messages` save. On error, the loop keeps running — log writes are best-effort. Token chunks are persisted *before* they hit the wire (lesson from [zknill on resumable SSE](https://zknill.io/posts/everyone-said-sse-token-streaming-was-easy/)) — write-then-stream, otherwise reconnect loses the last in-flight chunk.

**Read path.** Three responses depending on the client's cursor:

- *Live mode* (no cursor, or cursor matches the live tail). Server attaches the client to the agent pod's in-process pub/sub (asyncio broadcast) and streams new events as they're written. Session affinity via the existing `persistent_ws_proxy` binding ensures a single pod owns each thread.
- *Catch-up mode* (`Last-Event-ID: <epoch>:<seq>` and `(epoch, seq)` is within retention). Server replays `epoch=E AND seq > N`, then transitions to live mode without a gap.
- *Gone-beyond-horizon* (`epoch` mismatch OR `seq` older than retention). Server responds with a typed `GONE_BEYOND_HORIZON` event carrying the current `(epoch, server_seq, retention_horizon)`. Client must drop its cursor, pull the thread snapshot from REST (`thread_messages`), and start a fresh stream. Don't silently return-from-zero — that hides bugs.

**Why not `LISTEN/NOTIFY` for live fan-out.** [Recall.ai documented (March 2025)](https://www.recall.ai/blog/postgres-listen-notify-does-not-scale) that `NOTIFY` acquires a global commit-serializing lock; under simultaneous writers, throughput collapses as everything queues on `AccessExclusiveLock`. Our event-log writes plus sudo signalling on the same primitive is two hot paths on a single queue. Live event fan-out uses in-pod pub/sub instead; `LISTEN/NOTIFY` is reserved for the low-volume sudo-approval path (Phase 3), where the failure mode doesn't apply.

### 2. Loop lifecycle decoupled from any transport

Today, `_detach_session()` in `src/api/persistent_app.py:886` cancels `_loop_task` when the WebSocket closes. New semantics:

- Transport close (SSE disconnect, WS close, REST 499) → unsubscribe that consumer; *do not* cancel the loop.
- The loop runs until one of: turn boundary + `should_stop`, idle-no-input timeout (configurable, e.g. 5 min after the agent's last action with no human input pending), eager-mode budget exhausted, explicit user interrupt arriving over the input channel.
- "Untethered" is a *derived property* — `tethered = subscriber_count > 0`, evaluated per turn. Not a session FSM state (see [Session state machine](#session-state-machine) for the simplified state set).

Concretely: split `_detach_session` into two distinct paths:

- `_unsubscribe(client_id)` — called on consumer close (SSE, WS, or REST long-poll). Just removes the consumer from the in-pod broadcast list. Cheap.
- `_terminate_session(reason)` — called by watchdogs (untethered-and-budget-spent), `/done`, `thread.status='ended'`, or pod shutdown. Cancels the loop, persists final state, releases the workspace.

The "out-of-band thread.status='ended'" path (`persistent_app.py:880`) still works — it routes through `_terminate_session`.

### 3. Permission gates wait on the DB

Today the agent's permission/sudo paths wait on an `asyncio.Event` that's only signalled when the orchestrator processes the cockpit's "approve" message over the live transport. New behavior:

- Agent inserts an open permission request into `sudo_approval_requests` (or the relevant table) and `LISTEN`s on a thread-specific channel.
- Orchestrator updates the DB row when *anything* approves it: cockpit click (in-app), email-link click (out-of-band), `/api/approvals/{id}/approve` REST call (for MCP / scripted clients).
- DB trigger emits `NOTIFY` → agent receives → resumes.

`LISTEN/NOTIFY` is appropriate *here* because the volume is tiny (one approval per sudo prompt, well under a Recall.ai-class scaling concern). The live event fan-out path deliberately avoids it (see [§1 Event log](#1-per-thread-event-log) read path).

This means an approve link in an email can land an approval even when no client is attached. The notification fan-out (next section) is the mechanism that gets the link to the user in the first place.

### 4. Notification fan-out service

A new orchestrator subsystem that watches for "session needs attention, nobody is watching" conditions and delivers to the user's external channels.

**Triggers:**

| Condition | Detection | Action |
|---|---|---|
| Permission request open and no client attached | `sudo_approval_requests.status='pending'` AND no entry in `clients_attached(thread_id)` for >N seconds | Email with approve / reject magic links |
| Agent reached a natural pause and no client attached | Agent finishes a turn with no further tool calls queued AND no client attached AND timer has run for >N seconds | Email summary of recent activity, "reattach" link |
| Error / stuck state | Existing stuck-agent detection ([[stuck_agent_recovery]]) fires | Email with the stuck context |

**Channels (v1):**

- **Email** — reuse the SMTP plumbing from [[notify_user_tool]]. Cheapest hop; works everywhere.

**Channels (later):**

- **Web Push** — browser-native notifications when the cockpit is registered. Best for "active user with cockpit installed as PWA."
- **System notification center** — desktop app integration. Out of scope until we have a desktop app.
- **Mobile push** — out of scope until there is a mobile cockpit (Capacitor wrapper of the existing `simple/` shell is a candidate).
- **Slack / Discord / Telegram** — via [[automations]] generic-action surface; not a first-class channel.

**Rate limits + de-duplication.** Match the [[notify_user_tool]] guardrails: max 1 email per thread per 5 minutes, max 5 emails per thread per hour. **Coalesce by `(thread_id, request_id)`, not just `(thread_id)`** — the actual annoyance is the *same* approval being re-emailed every 5 minutes. Two distinct triggers in the same rate window get one summary email; two emails for the same request inside the window get suppressed (the first email is still valid).

**Magic links.** Industry-converged shape ([Gupta deep-dive](https://guptadeepak.com/mastering-magic-link-security-a-deep-dive-for-developers/), [MojoAuth](https://mojoauth.com/blog/are-magic-links-secure-technical-deep-dive)):

- **Opaque random tokens**, stored as SHA-256 hashes in the DB. Not JWT — single-use enforcement forces server state anyway, and JWTs add algorithm-confusion footguns without compensating benefit.
- **Bound to a specific `sudo_request_id`** in the DB, not just user/expiry. Replay against a different request must fail.
- **30-minute expiry** (longer than auth-magic-link norm of 15 min, because approvals are reviewed asynchronously; AWS Step Functions allows up to 30 days for the same reason).
- **GET shows confirmation page, POST executes.** Critical: Outlook Safe Links and Gmail link preview *auto-fetch URLs* server-side. A GET-executes link is consumed by the email scanner before the human ever clicks. The GET page shows the request context (which thread, which tool, the arguments) and a single "Approve" / "Reject" button that POSTs.
- **Single-use enforcement at DB level** via atomic CAS update. Double-click lands on a friendly "already approved" page, not a 404.
- **Land in the cockpit's session view after action**, with a toast showing the resolution. Single-page approval flow with a "back to session" link as a fallback for users without a session in the same browser.
- **No bare token in the email plaintext** — only inside the `<a href>` of the rendered HTML. Plaintext fallback links carry only the request-id and a "go to cockpit" URL, no consumable token.

**Extend-window affordance.** The 60-min attention-sleep clock can pinch on a nontrivial approval. The GET confirmation page exposes a "I'm reviewing this — extend" button that bumps the sleep clock by another 60 minutes per click, up to a per-thread ceiling. Pattern borrowed from AWS Step Functions' approval flow.

### 5. Sleep handoff

**Definition of "natural pause."** The agent has reached a natural pause when it has just produced one of:

- Text output with no further tool calls queued — the agent is "talking" without doing.
- A tool call that requires user permission (sudo gate).

These are the two states where the agent is genuinely waiting for the human. Every other tool-call boundary is *not* a pause — the agent is doing work.

Two distinct triggers for workspace suspension and pod scale-down:

- **Polite sleep (existing path).** Workspace idle for `WORKSPACE_IDLE_TIMEOUT` (default 30 min, see `workspace_suspension_service`). Triggers regardless of UI presence.
- **Attention sleep (new).** Agent reached a natural pause (as defined above) + notification fired + no human response within `ATTENTION_SLEEP_TIMEOUT` (default 60 min). The 60-min timer starts the moment the natural pause is reached, *not* when the notification is fired. Suspends the workspace and scales the agent pod down. On reattach (transport reconnect or magic-link approval) the existing restore path wakes it back up. 60 min, not 15: median response to a non-urgent email is hours, and the original 15 min default would suspend before most users finished their lunch. Tiered defaults are fair game (Slack-active = 15 min, email-only = 4h) — punt to the open questions.

A turn in flight *never* suspends. The agent's tool-call boundary is the only safe suspension point. The watchdog scans for "agent at natural pause AND timer expired AND no client attached."

### 6. Eager-mode behavior (no budgets in v1)

What stops an eager agent in v1 — in order of priority:

1. **Hard sudo gate.** Any tool requiring sudo blocks regardless of eager mode. Existing [[sudo_permissions]] behavior, preserved.
2. **Per-thread autonomy level.** `full | review | partial | guided | dependent`, existing. `review` still pauses at job-complete equivalents; `dependent` still pauses every phase. Untouched by this feature.
3. **Natural pause.** The agent itself decides it has nothing more to do without input — text output with no tool call, or a sudo-gated tool call. See [§5](#5-sleep-handoff) for the precise definition.
4. **Explicit user action.** `/done`, interrupt POST, or `thread.status='ended'`.
5. **Pod shutdown.** Drain, eviction, deploy.

**Polite mode** stays as an opt-in setting per thread. Definition: the agent parks at the end of each turn and does not start a new turn without explicit user input or reattach. Useful for review-heavy workflows where the user wants to see every step.

User-visible settings (cockpit "Persistent Agent" section, alongside [[sessions]] settings):

- `headless_mode`: `eager` (default) | `polite`
- `headless_attention_sleep_minutes`: int (default 60)
- `notification_channels`: array (default `["email"]`)

Stored under `users.settings.persistent_agent` JSONB, same merge order as the rest of [[sessions]].

**Budgets deferred to a future feature.** The original draft of this section included per-run + cumulative + org-wide token / wall-clock budgets motivated by the "[$47K agent loop](https://dev.to/waxell/the-47000-agent-loop-why-token-budget-alerts-arent-budget-enforcement-389i)" post-mortem. Budget enforcement is a separable concern from headless session decoupling — the two answer different questions ("can the user reattach cleanly" vs. "how much can a session cost"). Headless ships first; a "headless budgets" feature doc will pick up the token / wall-clock / cumulative work when prioritized. Until then, runaway protection in v1 leans on the autonomy levels above, the natural-pause definition, the attention-sleep watchdog, and the user's ability to `/done` from a magic-link email.

## Session state machine

```
   created ──→ active ──→ awaiting_user ──→ suspended
                  ▲             │              │
                  └─────────────┴──────────────┘
                  (reattach or magic-link restores to active)

                  active ──→ ended (explicit /done or autonomy-driven)
```

| State | What it means | What's happening physically |
|---|---|---|
| `active` | Loop running. May or may not have subscribers. | Pod up, loop running, events written to the log (and broadcast to subscribers if any) |
| `awaiting_user` | Natural pause reached, notification fired, awaiting response | Pod up (briefly), loop idle, sudo_approval_requests or eager_budget_exhausted event pending |
| `suspended` | `attention_sleep_minutes` elapsed without response | Workspace snapshotted, agent pod scaled down |
| `ended` | Terminal | Workspace can be archived per [[ephemeral_workspaces]] |

**Why no separate `untethered` state.** Devin, Cursor Cloud Agents, and Codex Cloud don't have one — every turn checks `tethered = subscriber_count > 0` on demand. Treating it as an FSM transition adds complexity that buys nothing: the *state* of the session is the same whether one or zero subscribers are attached; only the *behavior* differs (notification thresholds, idle detection windows). We keep "untethered" as a derived property at the turn level, not a state in the DB.

Transitions:

- **active → awaiting_user** when the agent reaches a natural pause (text output with no tool call, or a sudo-gated tool call) and no client is attached. See [§5](#5-sleep-handoff).
- **awaiting_user → active** when a client reattaches (transport reconnect or magic-link land), a permission resolves, or the user POSTs new input.
- **awaiting_user → suspended** after `attention_sleep_minutes` with no human response.
- **suspended → active** on transport reconnect or magic-link action (restore workspace, rebind agent, resume loop).
- **anywhere → ended** on `/done`, autonomy-driven completion, or explicit termination.

## Out of scope

- **Server-side LLM call cancellation on user interrupt.** Today's interrupt is in-memory; making interrupt survive WS disconnect is its own piece of plumbing and not strictly required to ship this feature. v1: a reconnecting user sees the in-flight turn complete (or hit its cap); v2 adds reconnect-then-interrupt.
- **Multi-cockpit fan-out beyond best-effort.** Two browser tabs on the same thread both subscribe to the event log; "last write wins" for things like narration-mode changes. We do not invent a CRDT here — see [[dynamic_canvas]] for related multi-user concerns; for chat this is sufficient.
- **Mobile / desktop push.** Mobile push needs a mobile cockpit; desktop push needs a desktop app. Email-first ships value without those.
- **Cross-device "approve from phone" rich UI.** v1 ships magic links over email. A signed-in mobile cockpit comes later, possibly via the same Capacitor wrap that [[automations]] discusses.
- **Server-driven WS heartbeat** (the F4.5 work parked in `docs/issues/persistent_chat_silent_disconnect.md`). The SSE+REST migration changes the urgency here: SSE's `EventSource` re-establishes automatically and the *server* tracks each `Last-Event-ID` request, so "UI is gone" becomes detectable by "no GET stream open and no recent POST input." The F4.5 issue is still relevant for the WS-fallback path, but the SSE-primary path solves it natively.
- **WebSocket transport for v1.** The SSE+REST path is the primary; the existing WebSocket handler stays as a fallback (opt-in feature flag) for environments where corp proxies break SSE. We are not rewriting the WS handler — just letting it continue to function for the small subset of users who need it.
- **Cost reporting per untethered run.** Useful follow-up; not required for v1.

## Open questions

1. **Reattach UX when a long untethered run happened.** If the user closes the browser for two hours, comes back to find the agent did 80k tokens of work, do they see (a) a single "summary of what happened" pseudo-message in the chat or (b) the full event stream replayed? Probably (a) by default with a "show details" affordance to expand into (b), but worth a UI sketch in [[persistent_chat_visual_refresh]] follow-up.
2. **Tiered attention-sleep defaults.** Slack-active users (presence available) want 15 min; email-only users want 4h. Currently we ship a single 60 min default. Worth wiring presence-aware tiering in a follow-up.
3. **Per-project eager defaults.** If a project is marked "research" vs "review-each-step," the eager defaults should probably differ. Maybe a per-project override in `projects.metadata`. Punt to follow-up.
4. **Eager mode + [[memory_light]] extraction.** Background memory extraction every N turns; if N turns happen while untethered, do we still extract? Probably yes — auxiliary LLM work is decoupled from main loop anyway. Worth a sentence in the implementation.
5. **In-pod pub/sub backend.** Asyncio broadcast per pod is the simplest fit and assumes session affinity (one pod owns each thread). If we ever want zero-affinity reads — e.g. a read-only "manager dashboard" that subscribes to multiple threads — we'd need a cross-pod fan-out (Redis pub/sub, NATS). Not v1.
6. **SSE response headers / proxy tuning.** Some K8s ingress controllers buffer SSE; need an explicit `X-Accel-Buffering: no` / `proxy_buffering off` audit during Phase 1 implementation.
7. **Session epoch versioning across migrations.** When the event schema changes (new `kind` values, payload shape), do we bump `epoch` per-thread on first read after the migration, or globally? Globally is simpler; per-thread is gentler on active sessions. Probably global at deploy time.

## Implementation phases

Each phase is independently shippable. They're ordered so each one improves the UX even if the next never happens.

### Phase 1 — Loop decoupling + SSE transport (P0)

The keystone change. Without this nothing else works.

- [x] Split `_detach_session` into `_unsubscribe(client_id)` and `_terminate_session(reason)` in `src/api/persistent_app.py`.
- [x] Add an in-pod subscriber list / asyncio broadcast so the loop's output fans out to zero/one/many subscribers.
- [x] Move loop cancellation out of transport `finally` blocks into `_terminate_session` only.
- [x] Add SSE endpoint `GET /api/threads/{id}/stream` honoring `Last-Event-ID` header.
- [x] Add REST input endpoints `POST /api/threads/{id}/input`, `POST /api/threads/{id}/interrupt`, `POST /api/threads/{id}/approve/{approval_id}`.
- [x] Implement interrupt semantics: if a tool call is in flight, wait for it to complete then stop (don't leak partial side effects); if the model is generating text without a pending tool call, cancel the LLM stream immediately.
- [x] Per-turn input lock: `POST /input` acquires a thread-scoped lock keyed by turn number; a second concurrent POST for the same turn returns 409 with the in-flight turn id. Handles multi-tab double-send cleanly.
- [x] Audit ingress proxy buffering — set `X-Accel-Buffering: no` on SSE responses.
- [x] Cockpit consumer (`cockpit/src/app/core/services/persistent-chat.service.ts`, 2026-05-13): `EventSource` replaces the WS receive path; `POST /input` + `POST /interrupt` replace WS sends for those two verbs; control-plane verbs (approve/deny/slash commands/mode/narration/config) retained on a simplified WS channel until there's a concrete pull for full REST conversion.
- [x] Tests: transport close mid-turn → turn completes; subsequent reconnect sees the completed turn. Interrupt mid-text → LLM cancels immediately, no partial AIMessage persisted. Interrupt mid-tool → tool completes, no new turn starts. Concurrent POSTs from two tabs → one wins, the other gets 409.

### Phase 2 — Event log + epoch + horizon

The "catch up like you never left" UX, with explicit "you missed too much" path.

- [x] `thread_events` migration (`orchestrator/database/migrations/app/0004_thread_events.sql`) with `epoch` + `(thread_id, epoch, seq)` index.
- [x] Loop writes events *before* broadcasting (write-then-stream, never the reverse).
- [x] SSE handler honors `Last-Event-ID: <epoch>:<seq>`; mismatched epoch or out-of-retention seq → emit `gone_beyond_horizon` event with current `(epoch, server_seq, retention_horizon)`.
- [x] Tiered TTL prune cron: 7 days for `active`/`awaiting_user`/`suspended`, 24h for `ended`.
- [x] Cockpit (2026-05-13): cursor stored in IndexedDB as a `threadCursors` Dexie table (schema v3) keyed by threadId; passed as `?last_event_id=<epoch>:<seq>` on initial open (EventSource doesn't accept custom request headers — backend accepts both query param + header). `gone_beyond_horizon` → drop cursor + REST-reload transcript snapshot + reopen stream without a cursor. Covered by `persistent-chat.service.spec.ts` (32 tests).
- [x] Tests: disconnect → 5 turns happen untethered → reconnect → cockpit shows the 5 turns. Bump `epoch` mid-stream → cockpit sees `gone_beyond_horizon` → re-syncs cleanly.

### Phase 3 — Permission gates outlive the transport

Closes the "user closes browser mid-permission-request" hole.

- [ ] Agent's permission wait reads from DB / `LISTEN` instead of in-memory event. (LISTEN/NOTIFY is fine here — low volume.)
- [ ] DB trigger emits `NOTIFY` on `sudo_approval_requests` updates.
- [ ] REST endpoint to approve/reject (already exists for MCP — confirm and reuse).
- [ ] Tests: open permission request → close transport → approve via REST → agent resumes.

### Phase 4 — Notification fan-out + magic links

Bridges the "nobody's watching" gap to the outside world.

- [ ] Notification service in `orchestrator/services/notifications.py`.
- [ ] Watchers for the four triggers in [Notification fan-out service](#4-notification-fan-out-service).
- [ ] Email templates: permission-request, eager-pause, eager-budget-exhausted, stuck.
- [ ] Magic-link routes: GET-shows-confirmation, POST-executes. Opaque random tokens, SHA-256 hashed in DB, bound to `sudo_request_id`, 30-min expiry, single-use atomic CAS.
- [ ] Rate limits + de-duplication coalesced on `(thread_id, request_id)`.
- [ ] "Extend window" affordance on the confirmation page.
- [ ] Settings UI under the cockpit's "Persistent Agent" section.

### Phase 5 — Attention sleep

Saves cluster cost when nobody actually wants to come back.

- [ ] New watchdog in `workspace_suspension_service`: detect `awaiting_user` for >`headless_attention_sleep_minutes` (default 60).
- [ ] Suspend integrates with existing snapshot path (already used by polite sleep).
- [ ] Reawake on transport reconnect (already works) or magic-link approval (new — restore-then-resolve).
- [ ] "Extend window" POST extends the sleep clock per click.
- [ ] Tests: untethered run → pause → no response → 60 min → suspended → email approve → wakes → resumes.

### Phase 6 — Polite mode + settings UI

- [x] `polite` mode wired in the loop: park at end of each completed turn unless a client is attached OR new input is in the queue. (`src/api/persistent_app.py:_loop_get_user_input`, 2026-05-13)
- [x] Cockpit "Persistent Agent" settings section: `headless_mode` select (default / eager / polite), `headless_attention_sleep_minutes` number input, `notification_channels` email-only checkbox. (`cockpit/src/app/views/settings/settings.component.ts`, 2026-05-13)
- [x] Per-thread overrides via `metadata.config_override.headless.*` with user-settings as the merge base. (`orchestrator/main.py:create_thread` for snapshot at create time; `attention_sleep_sweeper` uses 3-tier COALESCE for runtime resolution, 2026-05-13)

Backend landed 2026-05-13 alongside `HeadlessConfig` in `src/core/loader.py` and `UserSettingsUpdate.persistent_agent` passthrough. Cockpit UI followed the same day: three controls slotted into the existing Persistent Agent section, round-tripping through `/api/settings/preferences` with full transloco coverage (EN + DE) and a clean dev build.

> **Future:** A separate "headless budgets" feature will add per-run / cumulative / org-wide token + wall-clock caps with their own UI and notification triggers. Out of scope for this feature.

## ADR: durable execution engines considered, not adopted in v1

This design hand-rolls durability on top of LangGraph's `AsyncSqliteSaver` checkpointer + the new `thread_events` log + the existing Postgres-backed session metadata. Several mature alternatives were considered.

**[Temporal](https://temporal.io/blog/orchestrating-ambient-agents-with-temporal).** Battle-tested durable execution. Replit's agent runs on it; their published [case study](https://temporal.io/resources/case-studies/replit-uses-temporal-to-power-replit-agent-reliably-at-scale) treats per-agent Workflow IDs with single-active enforcement as the primary value. **Rejected for v1** because Temporal requires a separate cluster + worker SDK + splitting our agent into two services. Migration cost is multi-week with broad code impact.

**[DBOS](https://www.dbos.dev/blog/why-postgres-durable-execution).** Python library, Postgres-only, in-process — the lowest-friction fit for our stack. `@DBOS.workflow` + `@DBOS.step` decorators give durable execution without new infra; the migration shape is wrapping the top-level loop and tool calls. **Rejected for v1** because LangGraph's checkpointer + our event log already cover the failure modes DBOS targets (pod restart with checkpoint resume, partial-write recovery). Worth revisiting if pod-eviction frequency exceeds what those primitives tolerate.

**[Restate](https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk).** Durable handlers via a sidecar that intercepts HTTP. Active 2025 marketing toward agent backbones. **Rejected** because sidecar adds an extra hop in every tool call and we lose direct LangGraph integration.

**[Inngest](https://www.inngest.com/docs/features/inngest-functions/steps-workflows/wait-for-event)** / **[Trigger.dev v3](https://trigger.dev/blog/beta-to-latest-announcement)**. Hosted SaaS (with self-host options) targeting AI agent workflows. **Rejected** because they're not Postgres-native and we'd be pulled into their platforms' operational model.

**Decision.** Stay hand-rolled for v1. The migration path if we outgrow it is **DBOS** — it has the smallest distance from our current architecture (Python + Postgres, in-process, no new cluster). The cross-cutting opinion from [Temporal](https://temporal.io/blog/orchestrating-ambient-agents-with-temporal) and [Restate](https://www.restate.dev/blog/durable-ai-loops-fault-tolerance-across-frameworks-and-without-handcuffs) — "LangGraph protects against application errors; durable engines protect against infrastructure errors; production wants both" — is a real point. We are accepting that risk for v1 and shipping the infrastructure-error protection in a later release.

## Related code

- `src/api/persistent_app.py` — `_detach_session`, `_loop_task` global, watchdogs. Phase 1's keystone implementation lives here at module level — `handle_persistent_websocket` and `handle_api_{input,interrupt,approve}` are called from both `create_persistent_app()` (pure mode) and `dual_app.py` (cluster default). When extending Phase 1/5 behavior, edit the module-level functions; do **not** add a parallel implementation to `dual_app.py`. See [[persistent_session_dual_mode_phase1_gap]] for the drift incident that motivated this rule.
- `src/persistent_graph.py` — the loop body itself. Phase 2 instrumentation hooks here.
- `orchestrator/main.py:10931` — `persistent_ws_proxy` (kept as fallback). New SSE handler at `GET /api/threads/{id}/stream` + REST input endpoints land alongside it.
- `orchestrator/services/workspace_suspension_service.py` — Phase 5 extension point.
- `orchestrator/database/migrations/app/` — Phase 2 migration (`thread_events`), Phase 4 `notifications` + `magic_link_tokens` tables.
- `cockpit/src/app/core/services/persistent-chat.service.ts` — currently the WS reconnect state machine; Phase 1 replaces with an SSE consumer + REST input client.
- `docs/issues/persistent_chat_silent_disconnect.md` — F4.5, still relevant for the WS-fallback path.
- `docs/features/notify_user_tool.md` — SMTP plumbing reused in Phase 4.
- `docs/features/sudo_permissions.md` — Phase 3 builds on this.
- `docs/features/sessions.md` — foundational session model this design extends.

## Decision log

- **2026-05-12:** Eager mode chosen as default. Polite remains as a per-thread setting. (User decision.)
- **2026-05-12:** SSE for server→client + REST for client→server chosen as primary transport. Existing WebSocket handler retained as opt-in fallback. (User decision after research review.)
- **2026-05-12:** No durable execution engine for v1. DBOS named as the migration path if needed. (User decision after research review.)
- **2026-05-12:** `untethered` is a derived property (`subscriber_count > 0`), not an FSM state. State set is `created | active | awaiting_user | suspended | ended`. (Refinement after research review of Devin / Cursor / Codex patterns.)
- **2026-05-12:** Cumulative + org-wide eager-mode budgets added on top of per-run caps. Per-run reset on reattach was the [$47K agent loop](https://dev.to/waxell/the-47000-agent-loop-why-token-budget-alerts-arent-budget-enforcement-389i) failure mode; cumulative cap closes it. Orchestrator (not agent) owns cumulative enforcement.
- **2026-05-12:** Event log retention bumped from 24h to 7 days for non-ended threads. 24h was silently breaking the Friday-evening notification → Monday-morning reattach flow.
- **2026-05-12:** Attention-sleep default bumped from 15 min to 60 min. Tiered presence-aware defaults parked as a follow-up.
- **2026-05-12:** Live event fan-out uses in-pod asyncio pub/sub + session affinity, not `LISTEN/NOTIFY`. Per [Recall.ai's documented scaling issue](https://www.recall.ai/blog/postgres-listen-notify-does-not-scale) with `NOTIFY`'s commit-serializing lock. `LISTEN/NOTIFY` reserved for low-volume sudo signalling only.
- **2026-05-12:** `thread_events` carries `(epoch, seq)` rather than just `seq`. Mirrors Discord's gateway resume protocol; provides an explicit `GONE_BEYOND_HORIZON` re-sync path that prevents the Discord-net `#938` infinite-loop class of bug.
- **2026-05-12 (refinement pass 2):** "Natural pause" defined precisely as "text output with no tool call OR a sudo-gated tool call." Attention-sleep timer starts at the natural-pause moment, not at notification fire.
- **2026-05-12 (refinement pass 2):** Eager-mode token/wall-clock/cumulative/org-wide budgets dropped from v1. Budget enforcement is a separable concern; deferred to a future "headless budgets" feature. v1 runaway protection leans on autonomy levels, natural pause, attention-sleep watchdog, and magic-link-driven `/done`. (User decision: scope cut — budgets are their own feature.)
- **2026-05-12 (refinement pass 2):** No org-wide budget ceiling. The codebase has no first-class org concept and v1 is pre-production; the failure mode the org cap was meant to catch (multi-tenant runaway from a careless prompt-loop) doesn't apply at our scale.
- **2026-05-12 (refinement pass 2):** Migration strategy: no migration needed. Headless is shipping into a pre-production codebase with no existing active sessions to preserve.
- **2026-05-12 (refinement pass 2):** Interrupt semantics: in-flight tool call → wait for it to complete (no side-effect leak), then stop. Mid-text-generation with no tool call → cancel the LLM stream immediately.
- **2026-05-12 (refinement pass 2):** Per-turn input lock adopted for multi-tab safety. POST /input acquires a thread-scoped, turn-number-keyed lock; concurrent POSTs return 409.
- **2026-05-14:** Phase-1 keystone consolidated. WS handler and `/api/{input,interrupt,approve}` lifted to module-level in `persistent_app.py` (`handle_persistent_websocket`, `handle_api_*`); `dual_app.py` deletes its parallel `_run_persistent_websocket` (340 lines) and routes everything through the shared functions after a `_pod_state == PodState.SESSION` pre-check. Driven by the cluster verification finding in [[persistent_session_dual_mode_phase1_gap]]: cluster pods run dual mode by default, so the Phase-1 work that was only in `persistent_app.py` never ran in prod. The "extract → delegate" pattern is now the maintenance rule for cross-mode behavior; do not add session-side routes to `create_dual_app()` directly.
