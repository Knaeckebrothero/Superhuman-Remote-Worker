---
tags:
  - persistent-sessions
  - cockpit
  - bug
  - orchestrator
related:
  - "[[persistent_chat_silent_disconnect]]"
  - "[[persistent_chat_tool_only_messages_not_expandable]]"
  - "[[persistent_session_idle_timeout]]"
---

# Persistent session — swallowed sends, lost latest history, and empty-turn i18n

**Reported**: 2026-05-26 (user, live cluster)
**Status**: Root cause verified (all three). Fixes not started.
**Affected session**: `05220a87-288c-4dcc-bc35-90aca82a37ee` ("Building a RAG Chatbot Demo") — `active`, 11 turns, **793 `thread_messages` rows**, model `gpt-5.5` via codex-proxy.

## Summary

A single user report ("the session goes stale, my message gets swallowed, and after reconnecting it only shows the first message") turned out to be **three independent bugs** that stacked. They are documented separately below, then linked by the causal chain in [§How they chain](#how-they-chain).

1. **History truncation** — the cockpit's history endpoint returns only the *oldest* 200 messages, so long threads render their first turn(s) and silently drop everything recent.
2. **Missing i18n keys** — a text-less turn renders the literal string `chat.turn.collapsedEmpty`.
3. **Stale-connection swallowed sends** — backgrounded-tab freezing plus an un-monitored control WebSocket let a "Connected" session silently drop messages. (Extends the parked [[persistent_chat_silent_disconnect]] / "F4.5".)

---

## Bug 1 — History truncation: "shows the first message, not the latest"

### Symptom
After a reconnect (or page reload, or the `gone_beyond_horizon` path), the chat shows only the earliest turn(s); the most recent AI responses are gone. On `05220a87` the user saw essentially just turn 1.

### Root cause
The cockpit loads transcript history with no pagination params:

```ts
// cockpit/src/app/core/services/persistent-chat.service.ts:428
private async loadHistory(threadId: string): Promise<void> {
    const resp = await firstValueFrom(
        this.http.get<{ messages: HistoryMessage[]; total: number }>(
            `${environment.apiUrl}/persistent/threads/${threadId}/messages`  // no ?limit / ?offset
        )
    );
    ...
}
```

The orchestrator endpoint defaults to `limit=200` and the DB query is **oldest-first**:

```python
# orchestrator/main.py:12365
async def get_thread_messages_history(thread_id, request, limit: int = 200, offset: int = 0):
    messages = await postgres_db.get_thread_messages_history(
        thread_id=thread_id, limit=min(limit, 500), offset=offset)
```
```sql
-- orchestrator/database/postgres.py:3006
SELECT ... FROM thread_messages
WHERE thread_id = $1
ORDER BY created_at ASC          -- oldest first
LIMIT $2 OFFSET $3               -- default 200, hard cap 500
```

`05220a87` has **793 rows**, and turn 1 alone (~103 tool calls → call + result + thinking rows) exceeds 200. So the cockpit receives roughly *just turn 1* and never fetches the latest ~593 messages.

`historyToTurns` (`persistent-chat.service.ts:1594`) renders the server's order verbatim and **drops any tool result whose originating call isn't in the returned set** (`:1624`, `if (!tc) continue`) — so a non-turn-aligned window would also corrupt tool pairing.

### This was already fixed — on the wrong copy
The identical bug was fixed for the **agent's LLM-context restore** on 2026-05-25, but only in the agent's separate copy of the function:

```python
# src/database/postgres_db.py:322  (AGENT copy — FIXED)
async def get_thread_messages_history(self, thread_id, limit: Optional[int] = 200, offset=0):
    """... Pass ``limit=None`` to load the entire conversation. ... A fixed
    message cap can slice a parallel tool-call batch and orphan a function
    call, which the Responses API rejects with a 400."""
```

The orchestrator's copy (`orchestrator/database/postgres.py:2998`), which feeds the **display**, was never given `limit=None` and still hard-defaults to 200 ASC. Two same-named functions; one fixed, one not. See [[persistent_session_context]].

### Not caused by `/compact`
`_handle_compact` only mutates the in-memory LLM context; it never deletes or rewrites `thread_messages`:

```python
# src/api/persistent_app.py:2985
_session.messages[:] = await _session.context_manager.summarize_and_compact(...)
```

So compaction is a red herring for the lost history — it merely triggers a reconnect that exposes the truncation.

### Impact
On any thread > ~200 messages, every full history (re)load loses the most recent content. Worsens with thread age; near-total for tool-heavy sessions like this one.

### Fix sketch
Mirror the agent-path fix: serve the full conversation (or a **turn-aligned** latest window with back-pagination). A naive newest-N window must not split a turn or it re-triggers the `:1624` tool-result drop. See [Solutions](#solutions-deferred).

---

## Bug 2 — `chat.turn.collapsedEmpty` rendered literally (missing i18n keys)

### Symptom
A turn's headline shows the raw key text `chat.turn.collapsedEmpty` instead of a translation. (Screenshot: the German turn, mid-generation.)

### Root cause
Four keys are used in the turn template but defined in **neither** locale file:

```
cockpit/src/app/views/persistent-chat/persistent-chat.component.ts
  :571  chat.turn.thoughtCount
  :574  chat.turn.textCount
  :577  chat.turn.toolCount
  :591  chat.turn.collapsedEmpty
```
`cockpit/src/assets/i18n/en.json` and `de-DE.json` have no `chat.turn` object at all — the `turn` at `en.json:846` is `chat.status.turn` ("Turn {{count}}"), a different path. Transloco falls back to printing the key.

The `collapsedEmpty` branch is taken when a turn is **collapsed, has >1 event, and has no text event** (only thoughts + tool calls):

```ts
// persistent-chat.component.ts
:563  @if (turn.events.length > 1) {        // chevron shown
:583  @if (isCollapsed) {
:588    @if (last) { <span class="turn-headline">{{ last.content }}</span> }
:590    @else { <span class="turn-headline-empty">{{ 'chat.turn.collapsedEmpty' | transloco }}</span> }
```

A still-streaming tool-only turn ("Agent is working…", thoughts + tools, no prose yet) hits this. Note the collapsed branch has no streaming indicator, so even with the key present it would read "empty" while actively generating.

This is the turn-rendering-era successor to [[persistent_chat_tool_only_messages_not_expandable]] (that doc predates the turn reducer and references the old `msg.role` path).

### Impact
Cosmetic but conspicuous — looks broken to the user, in both locales.

### Fix sketch
Add the four keys to `en.json` + `de-DE.json`. Optionally, in the collapsed-empty branch, show a working/streaming indicator when `turn.status === 'streaming'` instead of the "empty" copy.

---

## Bug 3 — Stale connection swallows sends ("type → swallowed, green light flips")

This extends the parked **"F4.5"** issue [[persistent_chat_silent_disconnect]] (2026-05-10). Since that was filed, the transport moved to **SSE-primary + control WS**, which changes the analysis.

### Symptom
Session sits idle ~5–10 min (not long enough for any server timeout). User types a message, hits enter; it is silently swallowed and the header flips off "Connected." `/compact` issued in this state also does nothing.

### Root cause — three contributing gaps

**(a) Only the SSE has liveness; the control WS has none.**
The SSE stream (which drives the green light) now has a client watchdog and a server ping:
```ts
// persistent-chat.service.ts:63   SSE_WATCHDOG_INTERVAL_MS = 5000
// persistent-chat.service.ts:64   SSE_WATCHDOG_TIMEOUT_MS  = 45000
// :560 _startSseWatchdog — force-reopen if no SSE event for 45s
```
```python
# orchestrator/main.py:12663 — "event: ping" every ~20s idle; :12556 ": open" kickstart
```
But the **control WS** (carries `/compact`, approvals, mode changes) has **no ping and no watchdog** — it only reconnects on an explicit `onclose`. `_sendControl` writes into whatever socket reports `OPEN`, including a silently-dead zombie:
```ts
// persistent-chat.service.ts:861
private _sendControl(data) {
    if (this.controlWs?.readyState === WebSocket.OPEN) {  // zombie also reads OPEN
        this.controlWs.send(JSON.stringify(data));         // → swallowed, no error
        return;
    }
    ...
}
```

**(b) Regular sends are fire-and-forget and never re-queued.**
Regular messages go over REST `POST /input`, are added to the UI *before* the POST, and are only queued for retry when the session isn't ready:
```ts
// persistent-chat.service.ts:1079  dispatch(user_message)  // shows in UI first
// :1088  if (!this.sessionReady()) { this.pendingMessage.set(...); return; }  // else not queued
// :1098  _postInput()  // on failure: error banner, but the message is gone
```
`sessionReady` is set by the WS welcome frame (`:1545`), **not** the SSE — so it stays `true` through a silent SSE drop, and the send takes the non-queued path.

**(c) No visibility / online awareness — the actual 5–10 min trigger.**
There is **no `visibilitychange`, `focus`, or `online`/`offline` handler anywhere in the persistent-chat layer** (the only `online`/`offline` listeners are in `pwa-banner.component.ts`, which drive the cache toast and never touch the WS/SSE). The sole liveness mechanism is the `setInterval` watchdog — exactly what browsers throttle/freeze for a backgrounded tab (~5 min, Page Lifecycle API). Leave the tab idle, return, and the watchdog hasn't run: the light is still green, the SSE is dead, and the first send is swallowed. A manual `reconnectNow()` exists (`:881`) but nothing invokes it on refocus or network restore.

The 60s connection-token TTL (`orchestrator/services/session_tokens.py:34`) is handshake-only and refreshes on a `4401` close — **not** the trigger. The agent's ~30 min idle timeout (`config/persistent_defaults.yaml`, see [[persistent_session_idle_timeout]]) is also not the trigger for a 5–10 min idle.

### Impact
On a "Connected"-looking session, the first message after returning to the tab is lost with no durable feedback; `/compact` and approvals can vanish entirely.

### Fix sketch
Give the control WS the same heartbeat/watchdog as the SSE; add `visibilitychange` + `online` triggers that call `reconnectNow()` / re-validate liveness on resume; re-queue a failed `_postInput` into `pendingMessage` instead of only flashing an error.

---

## How they chain

The three bugs intersect, which is why one report looked like one bug:

```
idle 5-10 min, tab backgrounded ──► watchdog setInterval frozen, SSE suspended
   │                                  (light still green — Bug 3c)
   ├─ type a regular message ───────► POST into stale path, not re-queued ─► swallowed (Bug 3b)
   ├─ run /compact ─────────────────► _sendControl into zombie control WS ─► swallowed (Bug 3a)
   │
   └─ agent re-attaches ("SESSION RESUMED") ──► SSE epoch changes
            │
            └─► gone_beyond_horizon (persistent-chat.service.ts:618)
                     │
                     └─► calls loadHistory (:627) ──► ASC LIMIT 200 on 793 msgs (Bug 1)
                              │
                              └─► "only the first message shows; latest gone"
```

The kicker: `_handleGoneBeyondHorizon`'s comment says it reloads "so visible history doesn't have a silent gap" — but on this thread the truncating reload *creates* a 593-message gap.

## Symptom → cause

| Observed | Cause |
|---|---|
| Type after idle → swallowed; "Connected" flips | Bug 3 (no visibility/online trigger; `setInterval` frozen) |
| `/compact` did nothing | Bug 3a (control WS has no liveness; `_sendControl` into zombie) |
| AI's last message vanished after reconnect | Bug 1 via `gone_beyond_horizon` → `loadHistory` |
| "Only the first message, not the latest" | Bug 1 (ASC LIMIT 200 on a 793-msg thread) |
| `chat.turn.collapsedEmpty` literal text | Bug 2 (missing i18n keys on a text-less streaming turn) |

## Related code

| Area | File | Lines |
|---|---|---|
| Cockpit history load (no params) | `cockpit/src/app/core/services/persistent-chat.service.ts` | 428–445 |
| Cockpit history → turns (tool-result drop) | same | 1594–1687 (1624) |
| Orchestrator history endpoint (limit=200) | `orchestrator/main.py` | 12364–12388 |
| Orchestrator history query (ASC LIMIT) | `orchestrator/database/postgres.py` | 2998–3038 |
| Agent history query (FIXED, limit=None) | `src/database/postgres_db.py` | 322–351 |
| `/compact` handler (in-memory only) | `src/api/persistent_app.py` | 2977–3019 |
| Empty-turn i18n template branch | `cockpit/.../persistent-chat.component.ts` | 563, 571, 574, 577, 583–591 |
| Missing i18n keys | `cockpit/src/assets/i18n/{en,de-DE}.json` | (absent) |
| SSE watchdog + ping | persistent-chat.service.ts / orchestrator/main.py | 63–64, 560–591 / 12556, 12663 |
| Control WS send / no watchdog | persistent-chat.service.ts | 846–875 |
| Regular send / re-queue gap | persistent-chat.service.ts | 1021–1115 (1079, 1088) |
| `gone_beyond_horizon` → loadHistory | persistent-chat.service.ts | 618–629 |
| Session token TTL (60s) | `orchestrator/services/session_tokens.py` | 34 |

## Solutions (deferred)

Per the user, solutions are discussed separately. Notes:
- **Bug 1** is the highest-impact, contained fix (one endpoint/query). Open choice: load full history vs. turn-aligned latest window + lazy back-pagination.
- **Bug 2** is trivial and safe (4 keys × 2 locales, optional streaming-state copy).
- **Bug 3** is the delicate one (real-time reconnect logic on the live cluster); it supersedes the open items in [[persistent_chat_silent_disconnect]].
