# Session Reliability & Transport Simplification

**Status**: PROPOSED (refined)
**Date**: 2026-07-06 (v2 — same day, post-review)
**Provenance**: v1 drafted from a two-agent code investigation; v2 refined via a
20-agent workflow — 4 codebase deep-dives + 4 web best-practice sweeps
(SSE architecture, streaming-LLM rendering, background-tab recovery,
chat outbox patterns), then one design-refiner per phase and one
adversarial critic per refined design. All six phases returned
**SHIP-WITH-FIXES**; the must-fix items are folded in below. Where v2
overturns v1, the phase section says so explicitly.
**Related**: `docs/issues/session_reliability_investigation_index.md` ·
`docs/issues/resumed_session_dead_stream_and_supervised_gate_timeout_as_denial.md`
(session-attach starvation — *out of scope here, tracked there*) ·
`docs/issues/persistent_thread_lifecycle.md` (thread auto-end — out of scope) ·
memory topics `project_session_epoch_duplicate_render`,
`project_session_message_swallow_investigation`, `project_ngsw_buffers_sse`

## Motivation

Three user-reported, code-confirmed reliability failures in persistent
sessions, plus the architectural fragility that keeps producing this bug
class:

1. **Send swallow during "Creating thread"** — a prompt typed while the
   thread is being created is silently destroyed by the client's own
   connection setup. (Review finding: the defect class is wider — *any*
   `connect()` while a message is queued swallows it, not just creation.)
2. **Flicker during generation** — the streaming turn's DOM subtree is
   re-processed on every token; code blocks get re-wrapped and re-buttoned
   per delta; KaTeX re-typesets per delta; auto-scroll fires per delta.
3. **Zombie stream after idle** — an SSE stream opened before an agent
   re-attach keeps polling a dead events-epoch forever while its keepalive
   pings convince the client it is healthy. Nothing renders until a manual
   refresh.

All three are fixable surgically (Phases 1–4). The deeper pattern — most
stream fragility lives in *idle long-lived connections* and in the
per-session WebSocket's provisioning machinery — motivates the follow-up
architecture change (Phases 5–6): hold a per-session stream **only while a
turn is in flight**, and retire the per-session control WebSocket in favor
of REST + the journaled SSE.

## Current State (verified mechanics, 2026-07-06 tree)

The working tree is clean; all references below are committed `develop`.

### Transport inventory

| Channel | Endpoint | Carries | Notes |
|---------|----------|---------|-------|
| Per-thread SSE | `GET /api/persistent/threads/{id}/stream` | all journaled agent frames (token, thinking, permission.request/resolved, turn.*, `ready`) | Backed by the `thread_events` journal (epoch+seq); the orchestrator generator **polls Postgres** 200ms–1s and pushes rows. Cursor = `epoch:seq`, persisted in IndexedDB. |
| Control WS | `/p/{thread_id}/ws` (per-session Traefik Ingress+Service, 60s JWT) | slash commands (`compact`, `archive`, `undo`, `upgrade-to-workspace`), `mode.set`, `narration.set`, `config.update`, approve/deny **fallback**, the `session.state` welcome frame | Requires `SessionRouterService.ensure_route()`, which only runs after `probe_ready` (`orchestrator/routers/sessions.py:400`) — the gate behind the 425/504 startup class. Reconnect caps at 8 attempts. |
| REST | `POST …/input` (`main.py:17835`), `POST …/interrupt` (`:17891`), `POST …/approve/{approval_id}` (`:17907`), agent-side `POST /api/approve` (`persistent_app.py:2091`, resolves most-recent-pending when `approval_id` omitted) | input, interrupt, permission resolution | The orchestrator approve endpoint is already **canonical**: direct `thread_permission_requests` UPDATE + DB trigger NOTIFY → agent LISTEN. No agent-forwarding hop. WS approve is back-compat only. |
| Global notifications SSE | `GET /api/notifications/events` | `session.lifecycle` (provisioning/booting/ready/failed, emitted from `_do_prepare`, `sessions.py:174/278/304`), notifications | Always-on, one per tab. `notification_feed` is **per-process** (replica-local) with a 100-entry drop queue — no journal, no replay. |

**Correction from review (v1 got this wrong):** `ready` *is* journaled and
arrives via SSE (`_broadcast("ready", …)` → client `markSessionReady`,
`persistent-chat.service.ts:1849-1853`). But **`session.state` is a WS-only,
never-journaled welcome frame** (`src/api/persistent_app.py:2334-2347`) and
the **sole carrier of `running_tool`**. Retiring the WS therefore requires a
welcome-frame substitute (Phase 6 §2c) — it is not a freebie.

### Bug A — send swallow (the class, not just creation)

The composer is intentionally enabled during all startup phases
(`canCompose`, `persistent-chat.component.ts:1676`). A send during
"Creating thread" (`isCreating`, set only client-side in
`createAndConnect()`, `persistent-chat.service.ts:619`) correctly draws the
optimistic bubble (`:1535`) and queues the text in the single-slot
`pendingMessage` signal because `sessionReady` is false (`:1544-1547`).

Then `POST /persistent/threads` resolves and `createAndConnect()` calls
`connect(threadId)` (`:626`), whose first act is `disconnect()` — which
**nulls `pendingMessage`** (`:1367`) — followed by a reducer
`{type:'reset'}` that **wipes the optimistic bubble**
(`turn-reducer.ts:89-90`), and a second `pendingMessage.set(null)`
(`:571`). The message never reaches `_postInput`. Silent loss.

Review findings that widen the defect:
- `connect()` *always* runs `disconnect()` first (`:561`) — any reconnect
  with a queued message swallows it. The fix must change queue ownership,
  not special-case creation.
- `pendingMessage` is a single overwrite-slot; Enter bypasses `canSend`
  (`onKeydown` → `send()`, `component:2413-2415`), so a second keyboard
  send **silently overwrites** the first queued message today.
- Server side is sound: `POST /input` fails fast with 503 when no agent is
  bound (`main.py:17565-17575`); the agent persists accepted input to
  `thread_messages` at accept time (best-effort — persist failure still
  returns 200; durability is the in-process loop queue,
  `persistent_app.py:2129-2167`). There is **no SSE user-echo frame**; the
  persisted row renders only via history reload.

### Bug B — generation flicker

Per token frame: `dispatch → reduce` builds a new state (fine in itself —
`@for` tracks `turn.id`/`group.id`; completed events keep object identity,
so the "frozen completed blocks" memoization property already holds
structurally). The streaming text block renders through `<markdown
appCitationRef appKatex [data]="group.event.content">`
(`persistent-chat.component.ts:877`), and ngx-markdown re-parses and
resets innerHTML on every `[data]` change. The compounding costs:

- `ngAfterViewChecked` (`component:1949`) unconditionally runs
  `collapseCodeBlocks()` + `addCopyButtons()` (`:2575-2629`); the innerHTML
  reset destroys their `data-*` marker attributes, so streaming-turn code
  blocks are re-wrapped in `<details>` and re-buttoned **on every token**.
- `KatexDirective` re-typesets on every `markdown.ready`
  (`katex.directive.ts:38-56`).
- The auto-scroll effect keys on `currentStreamingTurn().events.length`
  (`component:1759-1768`) and sets `scrollTop = scrollHeight` per delta.
- No token coalescing anywhere; per-delta template re-eval calls
  un-memoized `groupedEvents()` etc. for every visible turn (window = 50).

Review corrections (v1 mis-attributed two things):
- innerHTML assignment is **atomic** (never paints empty); the visible
  per-delta jump is the raw-`$$…$$`→KaTeX re-typeset double-layout plus
  the `<details>`-wrapper destruction — not an "innerHTML height collapse".
- Near-bottom scroll detection **already exists** (`onMessagesScroll`,
  80px threshold, disengages `autoScroll` on wheel-up, `:2317-2334`). The
  real gaps: the scheduled `setTimeout` doesn't re-check `autoScroll` at
  fire time, browser scroll anchoring fights the pin, and per-delta
  frequency.

### Bug C — zombie-epoch SSE (stale stream until refresh)

`thread_event_stream` reads `events_epoch` **once at open**
(`orchestrator/main.py:17649`) and polls `WHERE epoch=$2` with that
constant forever (`:17765-17771`). When the agent re-attaches (idle
suspend/resume, pod churn), it bumps the epoch (sole bump site:
attach, `persistent_app.py:1556-1561`) and journals all new frames under
it — the open generator polls a dead epoch and delivers nothing. The same
generator keeps emitting typed `ping` frames every ~20s (`:17796-17805`),
which refresh the client watchdog — so the 45s watchdog **never fires**.
The stream is a zombie that actively proves its own liveness.

Refresh fixes it because a fresh open reads the new epoch, or gets
`gone_beyond_horizon` — whose client handler is already correct
(re-anchors cursor to the frame's `epoch`/`server_seq`, `service:998-1027`).

Secondary findings (client):
- The 5s watchdog `setInterval` freezes while the tab is backgrounded;
  the `visibilitychange`/`online`/`focus` handlers (`service:314-327`)
  exist but `_revalidateConnection` (`:945-962`) trusts
  `readyState===OPEN` for a socket that died silently <45s ago. There is
  no `pageshow`/`resume` handling (bfcache and frozen-page exits).
- Nothing on the send path verifies stream liveness: `_postInput`
  (`:1564-1583`) is fire-and-forget REST.
- **`_openSse` has no single-flight guard** — there is an interleaving
  window at `await this.cache.getThreadCursor()` (`:812`); watchdog,
  `reconnectNow`, and the horizon handler can race it. And an `_openSse`
  awaiting the cursor when `disconnect()` runs will assign `this.sse`
  *after* teardown, resurrecting a stream on an intentionally closed
  session (pre-existing bug).

## Non-goals

- **Session-attach starvation / `/connection` 425 storms** — infra-level,
  tracked in `resumed_session_dead_stream_and_supervised_gate_timeout_as_denial.md`.
- **Gate-timeout-as-denial** (Defect B in the same doc) — worth doing, not
  transport.
- **Thread auto-end / "stuck active"** — `persistent_thread_lifecycle.md`.
- The IDE proxy WebSocket (`/api/ide/{id}/proxy/*`) stays as is.

---

## Phase 1 — Kill the zombie epoch (server)

**Change**: `orchestrator/main.py`, `thread_event_stream` only (~25-line
diff). Inside the poll loop, re-read `events_epoch` after ~2s of
*accumulated idle time* (`epoch_idle += wait` in the empty-poll branch;
env-tunable `THREAD_EVENTS_EPOCH_RECHECK_S`, default `2.0`), sharing the
poll's existing pool acquire (zero new acquires). On change:

- anchor = **`_no_cursor_replay_start(conn, thread_id, new_epoch)`** —
  NOT the new epoch's tail. The bump happens at agent re-attach and the
  next frames are an in-flight turn; client history reload only carries
  *completed* turns, so a tail anchor would lose every already-journaled
  frame of the in-flight turn (a smaller version of the bug being fixed).
- emit `gone_beyond_horizon` with
  `{epoch, server_seq: anchor, reason: "epoch_bumped_mid_stream"}` and an
  **`id: {new_epoch}:{anchor}` line** (so a browser-native reconnect that
  bypasses the app handler converges to the same replay floor instead of
  replaying from `:0` — the known duplicate-render bug), then `return`.
- `events_epoch` fetchval returning `None` (thread deleted mid-stream) →
  return silently; reconnect hits `require_thread_owner` → 404.

The client handler needs **zero changes**. Do not touch the at-open
horizon paths' `id:` lines in this phase (they close before frames flow;
follow-up).

**Rejected**: LISTEN/NOTIFY on epoch bump (right long-term shape, but it's
Phase 5's machinery; a 2s-latency win on a rare event doesn't justify the
listener plumbing); stateful heartbeat + client watchdog (the server can
terminate itself deterministically, which is strictly stronger). Optional
forward-compat freebie: include `{epoch, seq}` in the existing ping's
`data:` (client ignores ping data today).

**Known residuals (documented, not fixed here)**:
- *Failed-init zombie*: the agent's attach-time epoch init is non-fatal on
  failure (`persistent_app.py:1571-1576`) — frames then journal under
  in-memory epoch 0 while the DB row holds N. Invisible to a DB re-read.
  Separate agent-side hardening item.
- *Live old-epoch writer*: if a second agent still journals under the old
  epoch (double-attach pathology), rows keep flowing and the idle re-check
  is deferred until that writer pauses. Pre-P1 behavior was strictly worse
  (zombie forever).
- *Anchor race*: a fast turn completing between anchor computation and the
  client's history reload can double-render once — same sub-second window
  as the shipped `epoch_mismatch` path. Accepted.
- Side benefit: the next attach's bump now terminates previously-immortal
  orphaned generators (partial down-payment on Phase 5's idle-cost win).

**Acceptance criteria**

Unit (`tests/test_thread_events_phase2.py`; the generator poll loop has
**zero existing tests** — the scripted-`acquire()` harness is greenfield,
budget it; monkeypatch `THREAD_EVENTS_EPOCH_RECHECK_S = 0`):
1. Mid-stream bump: epoch fetchval 3, 3, then 4; anchor query yields 17 →
   iterator emits `: open`, then exactly one `gone_beyond_horizon` with
   `event:` line, `id: 4:17`, `params == {epoch: 4, server_seq: 17,
   reason: "epoch_bumped_mid_stream"}`, then terminates.
2. Bump with empty new epoch → `id: 4:0`, `server_seq: 0`.
3. Thread deleted (fetchval None) → terminate, no horizon frame.
4. Steady-state: epoch stays 3, rows flow → correct `id: 3:{seq}` lines,
   no horizon frame, no epoch query before the accumulator trips (assert
   on call counts/absence, not exact ordering — the loop's first-ever
   happy-path test).

k3d live:
5. **Synthetic bump** (`UPDATE threads SET events_epoch=events_epoch+1`
   mid-turn via psql): orchestrator logs `epoch bump N→N+1` within ~2.5s;
   cockpit reloads history, reopens, no duplicate turn, no "SESSION
   RESUMED" divider. Do **not** expect new-epoch frames to render in this
   synthetic test — the live agent's in-memory epoch is unchanged by psql,
   so it keeps journaling under N (this is the failed-init residual by
   construction). Streaming continuity is criterion 6's job.
6. **Real path**: idle-suspend a session, resume it, send from the stale
   tab → reply streams without refresh.
7. Steady-state smoke: no spurious horizon events over a multi-turn
   session.

**Effort**: ~½ day for the change; budget **up to 1 day** total — the test
harness is most of the work.

## Phase 2 — Send-liveness kickstart + wake-recovery fix (client)

All in `persistent-chat.service.ts`. Three parts (C3 is a review
discovery v1 wrongly assumed already existed):

**C1 — send kickstart.** Mirror the existing `_armInterruptFallback`
pattern (`:1741-1753` — one-shot `setTimeout` → `reconnectNow()`). Arm
**inside `_postInput`** on both success paths (200 *and* 409
`turn_in_flight`) — this covers the direct send and the queued-flush path
for free. `SEND_KICKSTART_TIMEOUT_MS = 5000`. At fire time, compare
timestamps (deadline-check, which survives hidden-tab timer clamping)
against a **new dedicated `sseDataLastAt`, bumped only in
`_handleSseFrame`** (post-parse). Guard on the armed-time `threadId`;
clear the timer in `disconnect()`.

Two signal-purity traps the review caught — the obvious signals are
contaminated and would defuse the kickstart in exactly the zombie case:
- `agentLastEventAt` is reset in `_startSseWatchdog` (`:896`), which runs
  from `sse.onopen` — so **every reopen bumps it with zero data** (a
  browser-native mid-window retry would defuse the timer while
  re-attaching to a dead stream).
- The control-WS routes non-`_seq` frames into `_handleEvent`
  (`:1214-1216`) — `session.state`, `mode.changed`, `interrupt.ack` from
  the *other transport* would defuse it too.
- Do **not** repurpose `agentLastEventAt` (the `agentSilenceSeconds`
  badge depends on its reset semantics); add the fresh timestamp.

One-shot, no re-arm, **never re-POST** (input is accepted server-side;
no idempotency key exists — replay-from-cursor either renders the turn or
the error surfaces).

**C2 — wake recovery.** Track `hiddenAt` on `visibilitychange`; on return,
call `_revalidateConnection(force)` with `force = hiddenFor >
SSE_WATCHDOG_TIMEOUT_MS` — a socket that died silently while hidden lies
with `readyState===OPEN`. Add `pageshow` (`persisted===true`) and
`resume` (Chromium-only, progressive enhancement) listeners → forced
revalidate (bfcache entry can close sockets; frozen pages miss arbitrary
events). New listeners join the existing DestroyRef teardown.
`online`/`focus` stay un-forced.

**C3 — generation guard on `_openSse` (the missing single-flight).**
`private sseGeneration = 0`; increment at entry and in `disconnect()`;
after the cursor await, bail if superseded; every handler starts with a
stale-instance check (`if (this.sse !== es) return`). This also fixes the
pre-existing disconnect-resurrection bug (see Current State, Bug C).

**Rejected**: `GET /head` probe-before-reconnect (Phase 5 adds it for its
own reasons; blind reopen + cursor replay is cheap enough here); strict
echo-matching on the POST's `turn_id` (any data frame proves the
pipeline); backoff scheduler (every trigger is one-shot per user action).

**Acceptance criteria**
1. Live k3d: sever the SSE server-side unnoticed, then send → reconnect
   fires ≤ ~6s after the POST 200; turn renders live; **no duplicated
   streamed text**.
2. Unit: arm kickstart; deliver only `ping` frames **plus an `onopen`
   plus a WS `session.state` frame**; advance 5s → `reconnectNow` called.
   Deliver one SSE data frame → not called. (Without the onopen/WS frames
   in the fixture, the test validates the bug.)
3. Unit: queued-message flush path arms the kickstart.
4. Unit: hidden >45s then visible → reopen even with a mocked OPEN
   EventSource and fresh `sseLastEventAt`; short hide → no reopen.
5. Unit: `pageshow(persisted)` / `resume` → unconditional revalidate.
6. Unit: two concurrent `_openSse` with deferred cursor → exactly one
   EventSource retained; `disconnect()` during the await → none assigned.
7. Existing service specs green (fake-timer + `reconnectNow`-spy machinery
   at `spec:1105`, `:2083-2110` — extend, don't rewrite).
8. Live: background the tab >45s during an active turn, focus → frames
   resume ≤1s.

**Effort**: ~1 day (C3 + its concurrency tests are the added half).

## Phase 3 — Outbox: queue is user intent, not transport state (client)

The categorical fix (review reframing): the queued message must stop
being owned by the transport lifecycle. Replace `pendingMessage` with an
**outbox**:

```ts
interface OutboxItem { localId: string; content: string; attempts: number; }
readonly outbox = signal<OutboxItem[]>([]);
```

`localId` = the optimistic bubble's `makeLocalId('user')` — the
bubble↔queue linkage a bare `string[]` loses (needed for rollback and
queued-styling).

**Ownership rules**:
- `disconnect()` (`:1367`): **delete** the queue-clearing line. Transport
  teardown never destroys queued sends — this fixes the whole class, not
  just creation.
- `connect()` cold path (`:571`): the only wholesale clear (genuine thread
  switch) — except when called as `connect(threadId, {carryOutbox: true})`
  (what `createAndConnect` now uses). When carrying: after
  `await loadHistory()` but **before `_openSse()`**, re-dispatch a bubble
  per outbox item. Ordering is load-bearing twice: `load_history`
  wholesale-replaces turns (earlier bubbles die), and readiness can fire
  from the first SSE frame. Invariant to state in code: **re-dispatch
  before *any* readiness trigger** — `markSessionReady` has three callers
  (SSE `session.state`/`ready` handlers *and* `_openControlWs` on
  `/connection` `state==='ready'`, `:1083-1085`).
- All sends route through the outbox: `sendMessage` always enqueues +
  `void _flushOutbox()`. This serializes to **one POST in flight per tab**
  (the converged Matrix/Slack/WhatsApp shape) and closes a live hazard:
  two concurrent POSTs collide on the default `turn_id` and the server
  drops the second's content behind a 409 the client calls success.

**`_flushOutbox()`** (single-flight via a boolean): while items remain and
`sessionReady()`: POST head; on ok (incl. 409) remove **by `localId`** —
never positionally; on 404/410 drain the queue and roll back its bubbles;
on any other failure **stop flushing, set the error banner, keep items
queued** — retrigger on the next `markSessionReady` or `sendMessage`.
`_postInput` returns `{ok, status}` (keep the `error.set` in its catch).

Two review must-fixes shaped this:
- *Cross-thread head-swap*: the flush loop's `await` can outlast the
  queue's identity (up to 30s forward timeout). Mutate by `localId` and
  **drop the await's result if `threadId()` changed** since the POST was
  issued — otherwise thread A's resolution deletes thread B's queued item
  (the exact silent-swallow class this phase exists to kill).
- *No timed auto-retry* (v1 had 1s/2s backoff ×3): the agent persists +
  enqueues *before* returning 200, and the orchestrator's 30s timeout /
  pod-churn reset can convert an **accepted** input into a client-visible
  503 — auto-retry would double-send. Current code never retries; don't
  introduce the hazard. (Revisit with a `client_msg_id` idempotency key
  only if blind retries or multi-tab send coordination are ever added.)
- *Horizon re-dispatch*: `_handleGoneBeyondHorizon` re-dispatches bubbles
  for **unflushed** items after its `loadHistory` — but skip the head
  while its POST is in flight (accept-time persistence means the row can
  already be in the reloaded history; reconcile when the POST resolves).
- *Create-failure path*: if `POST /threads` fails, re-dispatch the queued
  bubbles on the error screen too (retained outbox with invisible
  messages is a trap).

**Component**: `isPendingSend` → `outbox().length > 0 ||
isUploadingAttachments()`; **remove** the second-send block from `canSend`
(queueing is now supported; Enter already bypassed it anyway); queued
bubbles get a muted style + clock glyph (id ∈ outbox set — no reducer
change needed since UserTurn id *is* the localId). Committed sends never
demote back to draft (drafts and queued messages are different objects).

**Rejected**: durable IndexedDB outbox (the loss window is the
seconds-long creation window with no threadId to key on; accepted sends
are already server-persisted); disabling the composer during startup.

**Acceptance criteria**
Vitest (new `persistent-chat.service.outbox.spec.ts`):
1. N sends while `!sessionReady` → N items, N bubbles, no overwrite.
2. `disconnect()` leaves the outbox untouched; `connect(other)` clears;
   `connect(id, {carryOutbox:true})` re-dispatches after `load_history`.
3. Flush is FIFO with exactly one POST in flight.
4. 409 → item removed, bubble kept. 503 → item + bubble kept, banner set,
   **no timer retry**; next `sendMessage` retriggers. 404 → drain + bubble
   rollback.
5. Thread switch mid-POST → resolution dropped, no cross-thread mutation.
6. Concurrent flush calls → single-flight. Horizon reload with one
   unflushed item → bubble re-dispatched exactly once; in-flight head
   skipped.

Playwright (k3d):
7. Type+send on the "Creating thread" card: bubble persists (clock style)
   through provisioning, POSTs once on ready, reply streams; after a hard
   reload the message renders exactly once.
8. Two Enter-sends during startup → both queued, flush in order.

**Effort**: ~1 day (the dropped retry taxonomy pays for the identity
guards).

## Phase 4 — De-flicker generation (client)

Five sub-changes (v1's four, corrected and extended):

**4a — Coalesce token/thinking dispatches**: plain **80ms `setTimeout`**
(not rAF: throttled in background tabs and not driven by vitest fake
timers; 50–100ms is the industry window — Vercel AI SDK
`experimental_throttle`, assistant-ui `minCommitMs`). Buffer sits before
the signal write; adjacent same-type deltas merge (thinking merges only
same-`messageId`; keep the first delta's timestamp); a flush folds the
whole queue via **one** `conversation.update`.

**Flush placement (review must-fix — v1's `dispatch()`-internal flush
ships a wedge)**: flush at the **top of `_handleEvent` for every
non-token/thinking method, plus in `disconnect()`**. Two handlers read
`conversation()` *before* dispatching and may skip dispatching entirely
(`_closeActiveTurnIfAny` `:2328-2331`; the `turn.completed` handler
`:1986-1989`) — with a dispatch-internal flush, buffered replay tokens
would materialize a `recovered:` placeholder *after* the close ran,
sticking `isStreaming()` true (composer wedged on Stop) until the next
turn. Flushing in `_handleEvent`/`disconnect` closes every path; `reset`
and thread switches can never leak old-thread deltas.

**4b — Block-level gating of DOM post-processing**: bind
`[class.streaming-block]="group.event.status === 'streaming'"` on the
`.event-text` div (`:876`) and the thought-card wrapper — **and on the
collapsed-turn `finalAnswer` path (`:834`)**, which the review caught: a
user can collapse a still-streaming turn (`isTurnCollapsed` honors the
manual override before the streaming check) and the growing text renders
there. `collapseCodeBlocks`/`addCopyButtons` skip
`pre.closest('.streaming-block')` **before** marking. No `turn.completed`
wiring: the block's `status` flip drops the class and the next
`ngAfterViewChecked` processes the now-final pres — finer than a
turn-level gate (an early-completed code block gets its copy button while
the turn still streams).

**4c — KaTeX defer**: `katexDefer` boolean input on `KatexDirective`
(default false); skip typeset while true; an `effect` typesets once on the
true→false transition (required — block close changes `status`, not
`[data]`, so `ready` won't re-fire). Bind at `:877`, `:834`, and the
thought card.

**4d — Scroll (three one-liners, not a rewrite — v1 overstated this)**:
re-check `autoScroll` *inside* the scheduled timeout at `:1766` **and in
the second (attachments) effect at `:1775-1780`**; add
`overflow-anchor: none` on `.messages` (browser scroll anchoring fights
programmatic pinning exactly when the last child mutates); keep the
existing 80px threshold.

**4e — Memoize `groupedEvents`** with a `WeakMap<Turn, EventGroup[]>`
(~6 lines; reducer immutability makes it exact — eliminates 49/50
rebuilds per pass). OnPush conversion of the 2977-line component is
explicitly **out of scope**.

**Rejected**: typewriter smoothing (delays content, adds drain
complexity); porting `use-stick-to-bottom`; auto-close repair of
incomplete markdown (marked already treats an unclosed fence as
code-to-end); incremental-parser hand-rolling. Accepted residuals:
selection loss inside the streaming block (frequency ~10× reduced) and
emphasis-marker flicker on incomplete constructs (cosmetic).

**Acceptance criteria**
Vitest (service; fake timers; note ~12 existing spec sites fire
token/thinking frames and need `advanceTimersByTimeAsync(80)` — budget
the churn):
1. 5 token frames → 0 updates before 80ms, exactly 1 at flush, wire-order
   concatenation.
2. token, token, `tool.started`, token → text precedes tool; post-tool
   token opens a new block.
3. Distinct thinking `message_id`s never cross-merge.
4. `thinking.reset` / `turn.completed` / `turn.error` / `ready`
   mid-buffer → buffered content lands *before* the control frame; **no
   `recovered:` placeholder, no stuck `isStreaming()`** (the S1 wedge
   test).
5. `disconnect()` mid-buffer → queue flushed/cancelled; timer no-ops;
   thread switch leaks nothing.
6. Reducer suite: zero changes required (coalescing is upstream).

Playwright (k3d), long-turn fixture (5 fenced code blocks + `$$math$$`):
7. Collapse/copy-button counts monotonically non-decreasing during the
   stream; never attach inside `.streaming-block`; each appears within
   one CD of its block completing — **including with the turn manually
   collapsed mid-stream** (the vacuous-pass trap).
8. KaTeX absent in the streaming block, present after completion.
9. Pinned-to-bottom stays pinned without jumps; wheel-up mid-stream never
   overridden.
10. DevTools trace over a token burst: layout/reflow visibly reduced;
    attach before/after traces to the PR (timebox the "≥10×" measurement —
    don't let the harness eat the verification budget).

**Effort**: 1–2 days, low end likely (implementation ~½ day; spec churn +
Playwright/perf the rest).

## Phase 5 — On-demand per-session SSE

**Depends on P1 (epoch handling at reopen) and P2 (wake funnel +
kickstart plumbing).**

### Server

- **`thread.activity` via DB trigger** (decision settled — v1 left it
  open, review closed it): migration
  `orchestrator/database/migrations/app/0048_thread_activity_notify.sql`
  — `AFTER INSERT ON thread_events FOR EACH ROW WHEN (NEW.kind =
  'turn.started')` → `pg_notify('thread_activity', {thread_id, turn_id})`.
  Trigger wins because (a) `notification_feed` is **replica-local**; an
  inline emit from the `/input` forward only reaches same-replica SSE
  subscribers (prod runs 2 replicas); (b) turns started via the agent WS
  path or resume-consumed queued input have **no orchestrator hop** at
  all; (c) the agent writes `thread_events` directly, so the trigger
  fires regardless of topology. NOTIFY volume ≈ 1/turn. **Regenerate
  `schema_current.sql` (`scripts/schema-snapshot.sh`) or CI fails.**
- **`orchestrator/services/thread_activity.py`** LISTEN service, started
  in the lifespan next to the prune sweeper. Modeled on
  `services/cloud/reload.py` but it is **new code, not a clone**: it
  needs a payload *queue* (reload.py coalesces into a single Event and
  drops payloads), and a deliberate connection decision — reload.py's
  "hold a pool connection forever" would **pin 1 of the max-10 pool
  slots per replica**; use a dedicated `asyncpg.connect()` outside the
  pool instead. Per notification: look up `user_id`/`status`, skip ended,
  `notification_feed.broadcast(user_id, "thread.activity", …)`.
- **`GET /api/persistent/threads/{id}/events/head`** → `{epoch, seq}`
  (`require_thread_owner` already returns the thread row with
  `events_epoch`; one `MAX(seq)` query on the covered index). This is the
  revalidate-first probe; v1's "cheap status GET" did not exist.

### Client (`persistent-chat.service.ts`)

Soft-suspend state machine — **not** `disconnect()`, which destroys
`sessionReady`/`pendingPermission`/outbox/cursor state that nothing
re-delivers on reopen:

- `streamMode: 'active' | 'idle-closed'`; new `'idle'` `connectionState`
  rendered as healthy (this union change fans out to every consumer —
  budget it).
- `_suspendSse()`: close SSE, `streamMode='idle-closed'` — but **keep the
  5s watchdog interval running** and branch its tick body (review
  must-fix: v1 stopped the watchdog, which silently killed the fallback
  poll that the design itself calls mandatory).
- Idle deadline (`SSE_IDLE_CLOSE_MS = 60_000`, timestamp-based): armed on
  `ready`, `turn.completed`, **and `turn.error`**; close only if
  `!isStreaming() && !pendingPermission() && sessionReady()`. The
  `pendingPermission` guard is load-bearing: a magic-link/second-tab
  approval resumes the turn **without a new `turn.started`** — no NOTIFY
  fires; an idle-closed tab would never learn of the resumption.
- **Clear the idle deadline on every SSE open** (review must-fix: a stale
  expired deadline would re-close a just-reopened stream in the
  open→first-frame window — a flap exactly on the hot send path).
- `_ensureSse()` (idempotent, generation-guarded via P2's C3): called
  from (1) `send()` before `_postInput` **and from the outbox flush
  path** (review: the queued-send flush bypasses `send()`); (2) a new
  `effect` on `NotificationService.threadActivity` matching the current
  thread (fresh object identity per event so back-to-back turns
  retrigger); (3) `_revalidateConnection` on wake — when idle-closed,
  `GET /events/head` and reopen only if `(epoch,seq)` is ahead of the
  cached cursor; (4) the ~15s visible-tab head poll riding the (still
  alive) watchdog tick — **mandatory, not belt-and-braces**: the
  notification feed has no journal/replay and drops at 100 queued.
- While the control WS still exists (pre-P6): a 3-line hook — idle-closed
  + incoming WS frame with `_seq` → `_ensureSse()` — a free same-tab wake
  path that dies naturally with P6.
- Guard `onerror`/watchdog-reopen/`reconnectNow` to no-op while
  `idle-closed` (a deliberately closed stream must not resurrect itself).

**Rejected**: SharedWorker/Web-Locks leader election (idle tabs now hold
nothing; N-tabs is already cheap); NOTIFY-wakeup for the stream
generator's own poll loop (idle-close removes the idle cost — don't
double-count); close-on-hidden (grace timer + wake revalidate is simpler;
frozen tabs deliver nothing anyway).

**Acceptance criteria**
Vitest: deadline arming/guards (streaming, pending gate); 60s expiry
closes with `sessionReady` preserved + cursor untouched +
`connectionState==='idle'`; reopen clears the deadline; `threadActivity`
for the current thread reopens (foreign thread doesn't; back-to-back
coalesce); send-path and flush-path call order (`_ensureSse` before
POST); wake head-check reopen-vs-skip; `onerror`/watchdog/`reconnectNow`
no-op while idle-closed; notification service parses `thread.activity`.
Pytest: `/events/head` (owner/non-owner/empty epoch); listener callback
(valid → broadcast; ended thread skipped; malformed JSON survives).
k3d runbook: idle tab drops its `/stream` ≤ ~65s after `turn.completed`
(orchestrator access log / `ss`), no reconnect attempts after; psql-insert
a `turn.started` row → idle tab reopens ≤2s; two tabs — send in B, idle A
live-renders ≤2s; kill A's notification EventSource → recovers ≤15s via
head poll; send from idle tab renders with no perceptible latency delta
and no duplicate turns; pending gate held 3+ min → stream stays open,
approval renders without reopen; schema snapshot diff committed.

**Effort**: ~3 days (server 1, client 1–1.5 — the vitest around fake
timers + EventSource mocks + signal effects is where it goes — k3d ½).

## Phase 6 — Retire the per-session control WebSocket

The payoff analysis stands: with no per-session WS there is no
per-session route to provision — `ensure_route`, per-session
Ingress+Services, 60s JWTs, and the zombie-WS/504 class are **deleted,
not fixed**. But the review found v1's premises missing three real work
items; the honest estimate is **5–7 days across three PRs**.

### 6a. Orchestrator `POST /api/persistent/threads/{id}/control`

Beside `thread_input`. Body `{method, params}`. Ownership check as
`/input`; **allowlist** `{mode.set, narration.set, config.update,
compact, archive, undo, upgrade-to-workspace}` → 400 otherwise (point
`message`/`interrupt` at their canonical endpoints). Forward via the
existing `_forward_to_agent`. **Approve/deny are NOT in `/control`** —
the orchestrator REST route is canonical, and the client's
no-`approval_id` WS fallback maps to the **existing agent
`POST /api/approve`** (`persistent_app.py:2091`), which already resolves
the most-recent-pending row. No duplicate implementation.

### 6b. Agent `POST /api/control` (+ dual_app wrapper)

Dispatch to the existing verb handlers. Two response classes:
- **Sync 200**: `mode.set`, `narration.set`, `undo` — with a **new**
  `_turn_in_flight()` → 409 guard (v1 claimed this guard exists; it does
  not, `persistent_app.py:2473-2497`). Extend the guard to `compact`,
  `archive`, `upgrade-to-workspace` (mutating the workspace under a
  running tool call is a corruption vector the WS path got lucky with).
  Note the behavior change: `/done` during a pending permission gate now
  409s — spec a readable client message for **all four** guarded verbs.
- **Async 202**: `config.update`, `compact`, `archive`,
  `upgrade-to-workspace` — re-home their progress/result frames from
  `_ws_send(ws, …)` onto `_broadcast` (journal → SSE). This *fixes a live
  bug* (a WS drop mid-upgrade kills the multi-minute progress UX today)
  and makes results survive reconnects. Two review carve-outs:
  - **Keep the no-op compact notice non-journaled** (subscriber-queues-only
    variant): journaling it was the 2026-06-12 duplicate-banner bug, and
    a journaled `context.compacted {summary:null}` would replay "nothing
    to compact" on every reconnect.
  - **Add typed, journaled failure frames** — `config.failed`,
    `compaction.failed`, `archive.failed` (+ client handlers). Today the
    failure paths emit generic `error` via `_ws_send`; under REST+202 a
    failed verb would otherwise produce *nothing* (spinner forever).
    This is real, uncounted design work — it's in the estimate now.
  Replayed progress frames must be idempotent state-sets client-side
  (keyed on the terminal frame); add a reducer spec.

### 6c. Welcome-frame substitute (the biggest v1 gap)

`session.state` is WS-only and the sole carrier of `running_tool`. Extend
agent `GET /status` (`persistent_app.py:1964`) with `narration_mode`,
`model`, `temperature`, `running_tool`; extend `GET
/{thread_id}/connection` to forward it after `probe_ready` and embed it
as a `session` object (needs a **GET forward helper** — `_forward_to_agent`
is POST-only; 2s timeout, `session: null` degrade). The client's
`session.state` handling moves to consuming this object — including the
`markSessionReady()` flip for reconnect-to-idle where the cursor sits
past the last `ready`. **Schema note**: `ConnectionResponse.ws_url/token/
expires_at` are required fields today (`sessions.py:344-348`) — they must
become optional, and old clients feeding `null` into `_installControlWs`
must be considered in the rollout order (below). Dual-mode (compose pool)
`/status` has none of these fields — acceptable degrade, compose is
deprecation-slated; state it in the PR.

### 6d. Boot-watchdog rewiring (unlisted hard dependency)

`_boot_ws_watchdog` (`persistent_app.py:554-580`) kills the pod 600s
after the last WS client. Rename `_ws_connected_event` →
`_client_contact_event`, set from: the WS handler (deprecation window),
`handle_api_input`, the new `/api/control`, **and `/connection` success**
(review must-fix: v1's stream-open-triggered `client-attach` ping fires
during cold start when no agent exists yet and is swallowed — the
watch-only user still dies at 600s; the `/connection` poll lands exactly
when the agent is ready).

### 6e. Flag + rollout (review inverted v1's default)

`system_settings` key `session_control_transport`, read at `/connection`,
surfaced as `control_transport: "ws" | "rest"`. **Fail to `"ws"`** — v1's
fail-to-rest would brick control verbs for every pre-cutover client (and
every ngsw-stale-cached bundle) the moment PR A deploys, silently (chat
still works, masking it). Flip to `"rest"` per-environment only after the
client PR is deployed and SW caches have churned.

Three PRs: **A** server (both `/control` sides, `/status` + `/connection`
extension + GET helper, frame re-homing + failure frames, watchdog
rewiring, flag; WS path fully intact) ~2.5d. **B** client cutover behind
the flag; flip dev to `rest` ~1.5d. **C** deletion — `SessionRouterService`
(+`teardown_route`, already dead code), `session_tokens.py`,
`_session_auth.py`, agent WS routes + `_ws_send`, provisioner env
(`SESSION_JWT_SECRET` etc.), Helm secret/env/RBAC (`services`/`ingresses`
verbs; keep configmaps), the ~500-line client WS block ~1d. **Gate C on
observed WS-connection counts, not calendar** — this repo has documented
history of service workers serving stale bundles.

**Acceptance criteria** (beyond unit tests listed per-PR in 6a–6e):
verb-by-verb k3d parity drive-through with flag=rest, including: progress
frames **survive an SSE reconnect mid-upgrade**; failed `config.update`
surfaces its typed failure frame; second tab renders `mode.changed`;
`undo`/`/done` mid-turn 409s render readable messages; fresh session with
flag=rest creates **no** `session-*` Service/Ingress and `/connection`
returns no ws_url; watch-only session (never opens WS, only SSE +
`/connection`) survives past 600s; rollback drill: flip flag to `ws` via
admin PUT → new sessions use the WS path, no restart.

---

## Ordering & sizing (v2)

| Phase | Size | Depends on | Ships alone? |
|-------|------|-----------|--------------|
| 1 — epoch re-read | ½–1 day (test harness is greenfield) | — | yes |
| 2 — kickstart + wake + `_openSse` guard | ~1 day | — | yes |
| 3 — outbox | ~1 day | — (touches same file as 2; land after 2) | yes |
| 4 — de-flicker | 1–2 days | — | yes |
| 5 — on-demand SSE | ~3 days | 1, 2 | yes |
| 6 — retire control WS | 5–7 days, 3 PRs | 6c interacts with 5's soft-suspend; pairs well after 5 | yes, behind flag (fail-to-ws) |

Phases 1–4 are the reliability fixes; land in that order (1+2 attack the
worst symptom; 2 and 3 touch `persistent-chat.service.ts` — sequence, do
not parallelize). For the structural phases: v1 said "do 6 first if only
one is funded"; v2 revises — P6 grew to 5–7 days once the welcome
substitute, failure-frame taxonomy, and watchdog rewiring were priced in,
while P5 stayed at 3. If only one is funded, **P5 now has the better
value-per-day**, and its `thread.activity`/head-endpoint plumbing is
useful groundwork regardless. P6 remains the bigger structural payoff.

## Verification (per CLAUDE.md: verify locally on k3d before commit)

Per-phase criteria above are the gate. Cross-phase regression sweep after
each landing: README smoke-test path + one long streaming turn + one
suspend/resume + one supervised-gate approval. P5/P6 each ship with a
dedicated runbook (connection-count assertions, verb-by-verb parity,
rollback drill).
