# Persistent-session history sync, render windowing & compaction display

**Status:** Design — not started
**Date:** 2026-05-27
**Owner:** TBD
**Related:** `docs/issues/persistent_session_swallowed_sends_and_truncated_history.md` (Bug #1),
`project_persistent_session_display_bugs.md`, `project_persistent_session_context.md`,
`docs/features/headless_persistent_sessions.md`

---

## 1. Context & problem

Persistent-session chat threads grow large (793 messages on the reference thread
`05220a87`). Three coupled problems:

1. **History truncation (Bug #1).** The cockpit display path loads only the
   *oldest* 200 rows. `orchestrator/database/postgres.py:2998`
   (`get_thread_messages_history`) does `ORDER BY created_at ASC LIMIT 200`;
   the endpoint `GET /api/persistent/threads/{id}/messages`
   (`orchestrator/main.py:12387`) defaults `limit=200` / caps `min(limit, 500)`;
   the cockpit `loadHistory()` (`persistent-chat.service.ts:450`) sends *no*
   params, so it always gets the first 200 rows. On a long thread the UI renders
   the conversation's *opening* and silently drops everything recent — "only the
   first message shows." (The agent's own copy `src/database/postgres_db.py:323`
   was fixed 2026-05-25 to support `limit=None`; the orchestrator/display copy
   was never updated.)

2. **No render windowing.** Even once we load everything, the turn list is
   **not virtualized** — `persistent-chat.component.ts:507` is a plain
   `@for (turn of chat.turns())`. Rendering ~800 turns (some with 100+ tool
   events) is thousands of DOM nodes the user never scrolls to.

3. **Compaction is invisible & ephemeral.** Both compaction paths discard the
   summary and persist nothing:
   - Auto (per-turn) — `persistent_graph.py:500-529`: `ensure_within_limits`
     produces `RemoveMessage` markers + a summary, folds them into the in-memory
     `prepared` list for one LLM call, then `strip_removal_markers` drops them.
     Only durable trace: a git "Auto-compaction checkpoint" commit.
   - Manual `/compact` — `persistent_app.py:2978`: rewrites in-memory
     `_session.messages`, emits a `context.compacted {before, after, focus}` SSE
     event (**counts only, no summary text**), git checkpoint.

   So `thread_messages` always holds the full raw log, but the user has no idea
   *what the agent currently sees* or *that* a summarization happened.

## 2. Goals / non-goals

**Goals**

- G1. Cache the **entire** conversation for a thread on the frontend in
  IndexedDB (Dexie), kept in sync with the live SSE stream and across reconnects.
- G2. Render only a bounded **window** (latest N turns) with **backfill on
  scroll-up** and correct scroll-anchoring, so the DOM never holds the whole
  thread.
- G3. Show a **compaction banner** (visual twin of the existing "SESSION ENDED"
  divider) at every summarization boundary — both manual `/compact` and
  automatic `ensure_within_limits` summarization — so the user always knows the
  agent's state. Banner is collapsed by default and **expandable to view the
  summary text**.
- G4. Persist compaction events durably so the banner survives reload, **without
  polluting the agent's own resumed context**.

**Non-goals**

- Worker-job (batch) chat history / compaction — that path uses `src/graph.py`
  + MongoDB `chat_history` and is out of scope.
- Server-side render virtualization or message editing/deletion.
- Porting Fessi's periodic multi-conversation `SyncEngineService` — our live SSE
  already delivers freshness; we only need initial sync + scroll backfill +
  reconnect catch-up.
- Reworking the agent's *internal* compaction strategy. We only surface and
  persist its boundaries.

## 3. Current state & integration seams

### Backend (orchestrator)
- `GET /api/persistent/threads/{id}/messages` — `main.py:12387`; returns
  `{messages, total, thread_id}` (`total` already plumbed, currently unused by
  the client). Calls `postgres.py:2998` (`ASC LIMIT 200`, SELECT
  `id, role, content, tool_calls, turn_number, metrics, tool_call_id, thinking, created_at`).
- SSE generator — `main.py:~12530` (`thread_event_stream`): polls `thread_events`,
  yields `id: <epoch>:<seq>`, emits `gone_beyond_horizon` (epoch mismatch or
  cursor < retention floor) and `ping` ~20s.
- `thread_messages.role` is bare `VARCHAR`, **no CHECK constraint**
  (`0001_initial.sql:886`) → a new `summary`/`compaction` role needs **no
  migration**.

### Agent
- ContextManager — `src/core/context.py`: `ensure_within_limits` (entry point,
  :818) → `summarize_and_compact` (:836) → `_single_pass_summarize` /
  `_recursive_summarize` produce a `CompactionSummary.summary` **string** that is
  formatted (`**Summary:**\n…`) and inserted as a message, then currently
  discarded by callers.
- Manual compaction — `persistent_app.py:2978` (`_handle_compact`).
- Auto compaction — `persistent_graph.py:500-529`; detects "happened" via
  `len(prepared) < pre_compact_len`.
- Agent resume full-loads `thread_messages` via `src/database/postgres_db.py:323`
  (`limit=None`) and repairs tool-call pairing.

### Cockpit
- `persistent-chat.service.ts`: signals at `:246-259`
  (`conversation`, `turns = computed`, `currentStreamingTurn`, `historyLoaded`);
  `loadHistory()` `:450`; `historyToTurns()` `:1660`;
  `_handleGoneBeyondHorizon` `:671` (→ truncating `loadHistory`);
  SSE `_openSse()` `:502`; `context.compacted` case `:1479` (→ `_systemMessage`);
  `dispatch()` `:1561`.
- `turn-reducer.ts`: `ReducerAction` union + pure `reduce()`; `load_history`
  replaces state wholesale.
- `turn.model.ts`: `UserTurn | AssistantTurn | SystemTurn`; `ConversationState
  {threadId, turns, activeAssistantTurnId}`.
- `indexed-db.service.ts`: Dexie `CockpitDatabase`, `CACHE_VERSION = 3`,
  `threadCursors: 'threadId'` (SSE replay cursors only — **no message content**).
- `persistent-chat.component.ts`: `@for` `:507`; `session-divider` banner
  `:667-670`; `onMessagesScroll()` `:1895`; `scrollToBottom()` `:2061`.

## 4. Design overview

Three layers, each independently shippable:

```
 Orchestrator                    Cockpit
 ─────────────                   ────────────────────────────────────────
 /messages (cursor-capable)  →   Dexie threadMessages (full cache, G1)
   before= / after= / total        ↑ initial sync + scroll backfill + SSE append
                                  visibleTurns window (last N, G2)  →  @for render
 SSE context.compacted (+text)  ↘  compaction banner (G3)  ↗  historyToTurns maps
 thread_messages role='summary' →  persisted summary rows (G4, survive reload)
```

The full IndexedDB cache (G1) is what makes windowing (G2) cheap: scroll-up
reads older turns from IDB instantly; the network is only touched on first sync
and reconnect catch-up.

## 5. Phased plan

### Phase 1 — Backend: stop truncating, add cursor paging *(unblocks everything; fixes Bug #1 on its own)*

`postgres.py` `get_thread_messages_history`:
- Switch ordering to `turn_number ASC, created_at ASC` (match the agent copy so
  parallel tool calls in a turn don't scramble).
- Support `limit: Optional[int] = None` (full load) **and** a keyset cursor:
  - `before` (ISO ts or `turn_number`) → `… < cursor ORDER BY … DESC LIMIT n`,
    reversed to ASC before return (backfill / initial tail).
  - `after` (ISO ts) → `… > cursor ORDER BY … ASC` (incremental catch-up).
- Keep the existing display column projection.

Endpoint `main.py:12387`:
- Accept `before` / `after` / `limit` (default tail window, e.g. 50; `limit=0`
  or omitted-with-no-cursor ⇒ honor full-load for debug). Drop the silent
  `min(limit, 500)` clamp in favor of an explicit, larger cap.
- Return `{messages, total, has_more}` (add `has_more`; `total` already present).
- Mirrors the proven Fessi contract (`backend/api/conversations.py:362`:
  `before_timestamp DESC LIMIT` / `after_timestamp ASC`, `206 + X-Has-More`).

Shipping Phase 1 alone already fixes "only first message shows" (client can ask
for the tail).

### Phase 2 — Frontend: full IDB cache + windowing + backfill

**Dexie** (`indexed-db.service.ts`): bump `CACHE_VERSION` → 4; add
`threadMessages: 'id, threadId, [threadId+createdAt], [threadId+turnNumber]'`.
New methods: `getThreadMessages(threadId, {beforeTurn?, limit?})`,
`upsertThreadMessages(rows)`, `getNewestCached(threadId)`,
`clearThreadMessages(threadId)`. Upsert is **append/replace by `id`** (a
streaming turn finalizes → same `id`, updated content).

**Sync** (`persistent-chat.service.ts`):
- `loadHistory()` becomes cache-first: (1) read tail window from IDB →
  immediate `load_history` dispatch (zero-latency paint); (2) fetch server tail
  (`before=<none>`/latest, `limit=N`) → upsert IDB → re-dispatch; (3) in the
  background, page **older** (`before=<oldest cached>`) until `has_more=false`,
  upserting into IDB but **not** into the render window (G1 fill without
  blocking — addresses gap #5).
- SSE: on `turn.completed`/`tool.completed`, upsert the finalized turn's rows to
  IDB (incremental sync). On reconnect, pull `after=<newest cached created_at>`
  to catch up missed rows.
- **Redefine `_handleGoneBeyondHorizon`** (`:671`): instead of the truncating
  `loadHistory`, run an `after`-cursor catch-up sync from newest-cached, then
  re-window to the tail. No more silent gap.

**Windowing** (service + component + reducer):
- Service: `windowSize` signal (default ~50 turns) + `visibleTurns = computed`
  (tail slice of the full turn list, which is hydrated from IDB). New
  `loadOlderTurns()` widens the window by reading the next page from IDB.
- Reducer (`turn-reducer.ts`): add `prepend_history` action (merge older turns at
  the front without clobbering `activeAssistantTurnId` or the streaming turn).
- Component: render `chat.visibleTurns()`; in `onMessagesScroll()` add a
  `scrollTop < 100` → `loadOlderTurns()` trigger; add an "end of conversation"
  marker when fully backfilled; add a **"jump to latest"** pill when new turns
  arrive while scrolled up (gap #6).

**Scroll preservation (salvage from Fessi, the hard part).** Port the
scrollHeight-delta technique verbatim in spirit from
`Advanced-LLM-Chat/src/app/chat-ui/chat-ui.component.ts:263-290`:
snapshot `scrollHeight`+`scrollTop` before prepend, then in `requestAnimationFrame`
set `scrollTop = before + (scrollHeightAfter - scrollHeightBefore)`, guarded by
an `isRestoringScroll` signal that mutes the scroll handler during restore.
Re-implement on signals + `afterNextRender` (Angular 21) — no CDK virtual scroll
(Fessi rejected it as overkill; turn heights vary too wildly).

### Phase 3 — Compaction persistence + banner *(greenfield — Fessi has nothing here)*

**Surface the summary out of ContextManager** (`src/core/context.py`):
`ensure_within_limits` / `summarize_and_compact` return (or set on a passed
context) `{summarized: bool, summary_text: str|None, before, after}` so callers
know **whether an actual summarization happened** (not just tool-clearing/trim —
gap #3) and can read the text.

**Persist a display-only boundary row** in `thread_messages` with
`role='summary'` (no migration — role is unconstrained), carrying the summary
text in `content`, `{before, after, trigger: 'manual'|'auto'}` in `metrics`, and
the boundary `turn_number`/`created_at`. Written from:
- `_handle_compact` (`persistent_app.py:2978`) for `/compact`.
- The auto path (`persistent_graph.py:518`, the `len(prepared) < pre_compact_len`
  branch) for automatic summarization — **only when `summarized=True`**, and
  **coalesced** (one row per summarization event; consecutive auto-summarizations
  with no intervening user turn collapse — gap, see §7).

**⚠️ Resume must skip the marker (gap #2, the correctness trap).** The agent's
`src/database/postgres_db.py:323` full-load + the resume/context rebuild must
**exclude `role='summary'` rows** from the LLM context (they are display-only).
Add a `WHERE role <> 'summary'` (or post-filter) on the agent-side load and in
the tool-pairing repair. The orchestrator *display* copy **includes** them.

**Emit live** (`context.compacted`): add `summary` (text) + `trigger` to the SSE
event payload so the live UI renders the banner immediately, and have the auto
path emit it too (today only `/compact` emits, with counts only).

**Render the banner** (cockpit):
- `turn.model.ts`: add `CompactionTurn { kind: 'compaction'; id; summary: string;
  before: number; after: number; trigger; timestamp }` (or a `SystemTurn`
  `variant: 'compaction'`).
- `historyToTurns()` (`:1660`): map a `role='summary'` row → a `CompactionTurn`
  at its chronological position.
- `context.compacted` handler (`:1479`): dispatch a `CompactionTurn` instead of
  the plain `_systemMessage`.
- Component: render it with the **`session-divider` pattern** (`:667-670`) — flag
  icon + centered label `CONTEXT SUMMARIZED · {{before}} → {{after}} MESSAGES`
  between two `divider-line`s — and make the label a `<details>`/expander
  revealing `summary` text (collapsed default, per G3).

## 6. Data model & schema

| Layer | Change | Migration? |
|---|---|---|
| `thread_messages` | new `role='summary'` value, summary in `content`, counts/trigger in `metrics` JSONB | **No** (role unconstrained; `metrics` already JSONB) |
| Dexie `CockpitDatabase` | v3→v4, add `threadMessages` store | Dexie version bump only |
| ContextManager API | return/emit `{summarized, summary_text, before, after}` | code only |
| Endpoint response | add `has_more` | code only |

No SQL migration file is required for Phase 1/3 (ordering + role are not
schema-constrained). If we later want a typed flag instead of a magic role
value, that would be a `migrations/app/NNNN_*.sql` per `docs/db_migration.md`.

## 7. Open questions / decisions (recommended defaults)

1. **Window size N** — default **50 turns**; widen by 50 per `loadOlderTurns`.
   (Tunable; turns ≠ messages — one turn can be many rows.)
2. **Auto-summary banner coalescing** — *recommended:* one banner per
   summarization event; collapse consecutive auto-summarizations that have no
   intervening user turn into a single "summarized again" marker (avoid divider
   spam). Decide threshold during impl.
3. **Banner content** — collapsed: `CONTEXT SUMMARIZED · before→after`;
   expanded: full `summary_text`. Manual vs auto distinguished by label
   (`You compacted` vs `Context auto-summarized`).
4. **IDB eviction** — *recommended:* no eviction in v1 (threads are bounded);
   add LRU-by-thread or a `expiresAt` TTL (Fessi uses 30 days) only if storage
   pressure shows up.
5. **Full-load vs progressive paging for G1** — *recommended:* progressive
   (`before`-cursor pages in the background) so first paint isn't blocked; the
   end state (full cache) is identical.
6. **Known limitation** — browser Ctrl-F only searches rendered (windowed)
   turns. Out of scope for v1; note in UI if needed.

## 8. Testing

- **Backend:** cursor query (`before`/`after`/`limit`, `has_more`, ordering by
  `turn_number`), summary-row persistence, **agent resume excludes `role='summary'`**.
- **Cockpit (vitest):** `prepend_history` reducer (merge front, preserve active
  streaming turn); `historyToTurns` maps `role='summary'` → `CompactionTurn`;
  windowing `visibleTurns` slice; cache-first `loadHistory` double-dispatch;
  `gone_beyond_horizon` → after-cursor catch-up (not truncating reload). Scroll
  preservation is hard to unit-test — cover the `scrollTop` math in isolation.
- **i18n:** EN + DE keys for banner label/expander, "load older", "jump to
  latest", "end of conversation".
- **Manual (live cluster):** thread `05220a87` (793 msgs) — open shows the
  *tail*; scroll up backfills without the viewport jumping; `/compact` shows a
  banner live and after reload; force an auto-summarization and confirm the
  banner appears and the agent doesn't re-ingest its own summary on resume.

## 9. Reuse map — Advanced-LLM-Chat ("Fessi", Angular 19, same monorepo)

| Salvage | From | Into |
|---|---|---|
| scrollHeight-delta preservation + `isRestoringScroll` | `chat-ui.component.ts:263-290` | Phase 2 component scroll |
| `scrollTop < 100` load-older trigger | `chat-ui.component.ts:236` | `onMessagesScroll()` |
| dual-mode `before`/`after` query, `206 + X-Has-More` | `backend/api/conversations.py:362`, `complex.sql:38/60` | Phase 1 endpoint |
| `slice(-N)` window + `loadOlderMessages` backfill + `hasReachedEnd` | `conversation.repository.ts:729/744`, `chat-state.service.ts:858` | Phase 2 windowing |
| 3-way merge (server-authoritative + keep-pending + keep-older-local) | `conversation.repository.ts:512` | Dexie upsert/merge |

**Do NOT carry over:** Fessi's `idb`-based `DBService` (we use Dexie already),
its periodic `SyncEngineService` (we have live SSE), its auth/CSRF, file-upload,
and `Message` class serialization. **Not available to salvage:** compaction
display — Fessi's `compact_chat.md` is backend research only; nothing built.
**Gotcha:** Fessi's `replaceConversationMessages()` nukes-then-reinserts; on a
long thread that loses history. Use **append/upsert-by-id only**, never
full-replace a thread longer than the window.

## 10. References

- `docs/issues/persistent_session_swallowed_sends_and_truncated_history.md`
- Survey of `Advanced-LLM-Chat/` (2026-05-27): sync/IndexedDB, scroll/windowing,
  compaction-absence findings.
- `Advanced-LLM-Chat/scroll_solution_implementation.md` (scroll design rationale).
