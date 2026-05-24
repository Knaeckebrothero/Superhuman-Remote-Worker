# Persistent chat — assistant turn disappears from the UI when `connect()` runs mid-turn

**Date:** 2026-05-22
**Status:** **Resolved 2026-05-23** — both layers shipped on `develop` and verified on the dev cluster (mid-stream hard refresh produced a visible 4.6 KB recovered bubble starting mid-word "oni…", confirming Approach 2 absorbed the orphan replay events and Approach 1 prevented same-thread re-mount data loss).
**Component:** `cockpit/src/app/core/services/persistent-chat.service.ts`, `cockpit/src/app/core/services/turn-reducer.ts`
**Severity:** Medium-high — every mid-turn page refresh or `connect()` re-run silently wiped the in-flight assistant turn from the UI, even though the agent was still streaming.

## Resolution

Approach 1 (primary) and Approach 2 (defense-in-depth) both shipped.

- `connect()` now has a same-thread fast path: when `this.threadId() === threadId && this.historyLoaded()`, it skips the destructive `dispatch({type:'reset'}) + loadHistory()` and just refreshes transports. SSE replay from the cached cursor reconciles against the existing turns. (Approach 1.)
- `turn-reducer.ts` gained `ensurePlaceholderTurn(state, timestamp)`: when `appendDelta`, `tool_started`, `tool_completed`, `permission_request`, or `permission_decision` runs with `activeAssistantTurnId === null`, the reducer synthesises a placeholder `AssistantTurn` (`id: 'recovered:<ts>'`, `recovered: true`) so streaming events become visible instead of silently dropped. `turn_completed` / `turn_interrupted` then promote the placeholder to the real turn id and close it, so the bubble doesn't hang in `streaming` forever. (Approach 2.)
- `AssistantTurn` in `turn.model.ts` gained an optional `recovered?: boolean` flag.
- Tests: `turn-reducer.spec.ts` covers both the placeholder synthesis and the promotion-on-completion.

Approach 4 (drop cursor on connect()) was not necessary once Approach 1 prevented the destructive reset, and Approach 3 (server-side merge of `thread_events` into `/messages`) was deferred — the cockpit-only fix solved the visible bug without a wire-format change.

## Summary

Two layered bugs in the cockpit's reconnect path cause the in-flight assistant turn to vanish from the UI whenever `connect()` runs while the agent is still generating:

1. **`connect()` calls `loadHistory()` before opening SSE.** `loadHistory` GETs `/api/persistent/threads/{id}/messages`, which only returns *persisted* `thread_messages` rows. AI messages are only persisted in `_loop_on_turn_complete` (`src/api/persistent_app.py:2434-2454`), not during streaming — so mid-turn the endpoint returns just the user row. The reducer's `load_history` action at `turn-reducer.ts:89-94` replaces `turns` with that snapshot and resets `activeAssistantTurnId = null`. The in-flight assistant turn that was visible up to that moment is gone.

2. **The reducer silently drops streaming events when there's no active turn.** Once `activeAssistantTurnId` is null, the subsequent SSE replay delivers `token`, `thinking`, `tool.started`, and `tool.completed` events — all of which hit `appendDelta` (`:342-370`) or `updateActiveTurn` (`:372-384`). Both functions early-return with no state change when `activeAssistantTurnId` is null. `turn.completed` doesn't help either — it only updates existing assistant turns (`:166-185`), so a turn the reducer never opened is never closed. The agent finishes the turn server-side, but the UI shows the user message and nothing else.

Net effect: the agent IS doing work, the work IS being persisted to `thread_events` and (eventually) `thread_messages`, but the user sees nothing. They report it as "the session stopped responding" even though a fresh load after `turn.completed` would show the full transcript correctly.

## Symptom (observed 2026-05-22, thread `7a4856a8-8e18-4d1d-af22-f7bd580381d1`)

User started a new persistent session, sent a 36 KB prompt, got the agent into a streaming state with 11 tool calls and ~150 s of activity. Mid-turn, the orchestrator log shows three full reconnect cycles (each: `WS proxy ended` → `GET /messages` → `GET /thread` → `GET /stream` → new WS) between 08:02:56 and 08:03:49. Each cycle came back to `loadHistory` returning just the user message — turn hadn't completed yet (it completed at 08:04:48).

The thread's `thread_events` log is intact end-to-end (`seq=1..565`, includes `turn.started`, 6× `thinking`, 11× `tool.started/completed`, 526 tokens, `turn.completed`, `title.updated`). The `thread_messages` table has all 24 rows including the final 1964-char AI response. The data path worked perfectly. The cockpit just couldn't render it because its `activeAssistantTurnId` was null by the time replay arrived.

User's report:

> "The agent's message parts/toolcalls are gone and I just see the message I've send with no response. The input component shows no generation active."

That's exactly what the reducer state produces: one `UserTurn` from history, no `AssistantTurn` (because the AI rows weren't yet persisted at the time of the last `loadHistory`), and `activeAssistantTurnId = null` so `isStreaming` (`persistent-chat.service.ts:173`) is false.

## Root cause

### Layer 1 — `connect()` rebuilds state from a snapshot that doesn't include in-flight work

`cockpit/src/app/core/services/persistent-chat.service.ts:258-291`:

```typescript
async connect(threadId: string): Promise<void> {
    this.disconnect();
    this.dispatch({type: 'reset', threadId});             // wipe everything
    // ... reset all signals ...
    this.threadId.set(threadId);
    await this.loadHistory(threadId);                     // GET /messages → only persisted turns
    await this.loadThreadMeta(threadId);
    if (this.threadStatus() === 'ended') {
        this.connectionState.set('disconnected');
        return;
    }
    this.intentionalClose = false;
    await this._openSse(threadId);                        // SSE replays from cursor
    this._openControlWs(threadId);
}
```

`loadHistory` calls `GET /api/persistent/threads/{id}/messages` which is backed by `postgres_db.get_thread_messages_history` (`orchestrator/database/postgres.py:2927`) — a straight `SELECT ... FROM thread_messages ORDER BY created_at ASC`. There's no "include in-flight events from `thread_events`" — that table is queried separately by the SSE endpoint at `orchestrator/main.py:12378`.

So the snapshot returned by `loadHistory` reflects only the data that's been flushed through `_save_turn_ai_messages`, which the agent calls inside `_loop_on_turn_complete`. Mid-turn it's just the user message.

### Layer 2 — the reducer silently drops events with no active turn

`cockpit/src/app/core/services/turn-reducer.ts:342-370`:

```typescript
function appendDelta(state, eventKind, content, timestamp): ConversationState {
    if (!state.activeAssistantTurnId) return state;       // ← silent drop
    return updateActiveTurn(state, (turn) => { /* ... */ });
}

function updateActiveTurn(state, updater): ConversationState {
    if (!state.activeAssistantTurnId) return state;       // ← silent drop
    return { ...state, turns: state.turns.map(/* ... */) };
}
```

When SSE replay arrives after `load_history` has nulled `activeAssistantTurnId`, every `token`, `thinking`, `tool_started`, `tool_completed`, `permission_request`, `permission_decision` event is no-op'd. The `tool_completed` case has a small recovery path (synthesizes an orphan entry at `:230-242`) but that requires the event to fire `updateActiveTurn` — which it can't, because of the early-return.

`turn_completed` at `:166-185` also no-ops in this state — it iterates `turns.map` looking for an assistant turn with matching `id`, finds none, and returns state unchanged. So even when the turn finishes, nothing in the UI changes.

### What triggers the mid-turn `connect()`

In the observed session, `loadHistory` was called three times in 53 seconds (orchestrator log timestamps 08:02:56.708, 08:03:01.033, 08:03:48.606). `loadHistory` has only two callers in the service:

1. `connect()` at `:278`.
2. `_handleGoneBeyondHorizon()` at `:531`.

`gone_beyond_horizon` can't fire on this thread (`events_epoch=0` throughout, `min_seq=1` so the retention floor check at `orchestrator/main.py:12452` can never trigger). So each of the three `loadHistory` calls came from `connect()`.

`connect()` callers in the cockpit:

- `chat-page.component.ts:50` — runs on `ngOnInit` if `!isConnected()`. Re-mounts when the user navigates back to the chat route or the connection drops to `'connecting'` / `'error'`.
- `persistent-chat.service.ts:316` — inside `createAndConnect`, only on a new thread.
- `persistent-chat.service.ts:732` — inside `resumeSession`, after `POST /resume`.

The observed pattern (browser refreshes, route navigation) all funnel through `chat-page.component.ts:50`. When the WS proxy drops (in this session, plausibly because of the split-brain race in [[persistent_thread_double_provisioning_race]] but the trigger is incidental to this bug), `connectionState` flips to `'error'` or `'connecting'`, `isConnected()` becomes false, and a re-mount fires `connect()` again. Each re-mount wipes the conversation and replaces it with `loadHistory`'s persisted-only snapshot.

A user hard-refresh during streaming reproduces the same state mechanically: `chat-page` ngOnInit runs, `isConnected()` is false (page just loaded), `connect()` runs, the in-flight turn is gone.

## Why the SSE cursor design doesn't catch this

The SSE replay path was designed to handle reconnects via cursor: after a drop, the cockpit opens `/stream?last_event_id=<epoch>:<seq>` and the orchestrator replays all events with `seq > cursor_seq`. The cursor is updated in `_saveCursor` (`:536-545`) on every received event.

This works fine when only the *SSE* drops and reopens — the connection-state stays `'connected'`, no `connect()` is triggered, the reducer's existing turns stay in memory, and replay just continues. The problem is specifically when `connect()` runs, because:

1. `dispatch({type: 'reset'})` wipes the existing turns including the open assistant turn.
2. `loadHistory` replaces with a persisted-only snapshot.
3. SSE then replays *from the cursor*, which is already past `turn.started`. Events arrive but there's no active turn for them to attach to.

If the cursor were dropped (set to 0) on `connect()`, the full event log would replay and the reducer would correctly reconstruct the in-flight turn. But the cursor is cached in IndexedDB and reused on reconnect — that's the whole point of the design, to avoid replaying thousands of events on every reconnect.

So the cursor design is correct for SSE-only reconnects, but `connect()` is a heavier path that needs different recovery semantics.

## Possible fixes

Listed without prejudgement; not mutually exclusive.

### 1. Don't reset the conversation in `connect()` when `threadId` is unchanged

The most surgical change. If `this.threadId() === threadId` already (i.e. we're reconnecting to the same thread we were on), skip the `dispatch({type: 'reset'})` and skip `loadHistory`. Just close stale transports, reopen them, and let SSE replay continue against the existing state.

```typescript
async connect(threadId: string): Promise<void> {
    const sameThread = this.threadId() === threadId && this.historyLoaded();
    if (!sameThread) {
        this.dispatch({type: 'reset', threadId});
        // ... all the signal resets ...
        this.threadId.set(threadId);
        await this.loadHistory(threadId);
    } else {
        // Soft reconnect: keep turns, reuse cursor.
    }
    await this.loadThreadMeta(threadId);
    // ...
}
```

Pros: minimal change, preserves the in-flight turn through reconnects. Cons: `loadThreadMeta` and the SSE replay must be enough to reconcile any state we missed during the drop (model changes, narration changes, etc.). They already are, per the existing reconnect path.

### 2. Make the reducer reconstruct a placeholder turn on orphan events

Defense-in-depth at the reducer layer. When `appendDelta` or `updateActiveTurn` runs with `activeAssistantTurnId === null`, instead of silently no-op'ing, open a placeholder assistant turn with a synthetic id and mark it as `recovered` so the UI can show "partial transcript — refresh to see full" if desired.

```typescript
function appendDelta(state, eventKind, content, timestamp): ConversationState {
    if (!state.activeAssistantTurnId) {
        state = openPlaceholderTurn(state, timestamp);    // synthetic
    }
    return updateActiveTurn(state, ...);
}
```

Pros: protects against any future reconnect bug, not just the one in `connect()`. Cons: the placeholder turn won't have the correct turn id, so when a later `turn.completed` arrives with the real id it won't match — needs a second mechanism to reconcile when the real `turn.completed` finally fires (e.g. accept any active assistant turn as the target when its id is synthetic).

### 3. `loadHistory` includes in-flight events from `thread_events`

Server-side: have `GET /messages` (or a new endpoint) merge the persisted `thread_messages` rows with any uncommitted events from `thread_events` past the last `turn.completed`. The cockpit gets a complete snapshot regardless of whether the active turn is mid-flight.

Pros: solves the problem entirely on the backend; cockpit logic doesn't change. Cons: complex — needs to reconstruct an AI message from streaming events (token deltas, tool call records) on the server. The agent already does this work in `_save_turn_ai_messages`; we'd be duplicating it in the orchestrator. Significant code surface and a new abstraction to keep in sync.

### 4. Drop the cursor and replay everything when `connect()` runs

When `connect()` is invoked, clear the cached cursor (`cache.deleteThreadCursor`) before `_openSse`. The full event log replays, the reducer reconstructs the in-flight turn from `turn.started` onward.

Pros: simple, no reducer changes. Cons: every `connect()` replays the full event log — for long sessions this is meaningful traffic (the observed session had 565 events). Also doesn't help with `gone_beyond_horizon` if retention has trimmed the log.

### Recommendation

Approach **1 (don't reset on same-thread reconnect)** is the right primary fix. It addresses the immediate cause (`connect()` is too destructive) without changing wire formats or backend schemas. The "same thread" check is well-defined: if `threadId` hasn't changed and `historyLoaded()` is already true, we have everything we need locally — just refresh the transports.

Approach **2 (placeholder turn in reducer)** is worth doing as defense-in-depth. It costs near-zero and turns "silently lost UI state" into "visible partial state with a recovery hint", regardless of what triggered the reconnect.

Approaches **3** and **4** solve the problem but at higher cost; defer unless 1 + 2 prove insufficient.

## What's NOT the fix

- **Hiding the streaming indicator while reconnecting.** Doesn't address the lost transcript; just papers over one symptom.
- **Forcing a hard refresh on every WS drop.** Aggravates the issue — every drop becomes a full state wipe.
- **Disabling browser refresh during streaming.** Out of our control; users will refresh anyway, including via OS-level events.

## Open questions

1. When `historyLoaded()` is true but `threadId` changed, do we ever expect `connect()` to be called against a thread the user is *already* on? (i.e. is the same-thread fast-path safe to add?)
2. Are there other call paths into the reducer's `appendDelta`/`updateActiveTurn` that would benefit from the placeholder fallback (e.g. SSE replay race during initial load)?
3. Does the existing `historical: true` flag on `AssistantTurn` (`turn-reducer.ts:1400`) need a sibling `recovered: true` for the placeholder, or is reusing `historical` correct?

## References

- `cockpit/src/app/core/services/persistent-chat.service.ts:258-291` — `connect()` (the destructive path).
- `cockpit/src/app/core/services/persistent-chat.service.ts:336-352` — `loadHistory()` (the persisted-only snapshot).
- `cockpit/src/app/core/services/turn-reducer.ts:89-94` — `load_history` reducer action (sets `activeAssistantTurnId = null`).
- `cockpit/src/app/core/services/turn-reducer.ts:342-370` — `appendDelta` (the silent drop site).
- `cockpit/src/app/core/services/turn-reducer.ts:372-384` — `updateActiveTurn` (the other silent drop site).
- `cockpit/src/app/core/services/turn-reducer.ts:166-185` — `turn_completed` action (no-op when turn doesn't exist).
- `cockpit/src/app/views/chat/chat-page.component.ts:46-50` — `chat.connect(threadId)` on re-mount.
- `src/api/persistent_app.py:2434-2454` — `_loop_on_turn_complete` (where AI messages get persisted).
- `orchestrator/main.py:12235-12259` — `GET /api/persistent/threads/{id}/messages` (the snapshot endpoint).
- `orchestrator/main.py:12378` — `GET /api/persistent/threads/{id}/stream` (the SSE replay endpoint).
- [[persistent_thread_double_provisioning_race]] — likely upstream trigger that creates the WS-drop conditions where this bug becomes visible.
- `docs/issues/persistent_chat_silent_disconnect.md` — earlier work on SSE liveness watchdog (commit `31062bd0`), tangentially related.
