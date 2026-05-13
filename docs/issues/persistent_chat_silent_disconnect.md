# Persistent chat — silent network drops don't trigger reconnect

## Symptom (observed 2026-05-10 on dev cluster)

User loaded `/sessions/<id>` on the deployed cockpit, opened DevTools → Network → "Offline", and disabled the laptop's Wi-Fi entirely. The PWA shell flipped to "Off the road — working from cache" within a second (HTTP layer noticed offline), but:

- The session header still read **Connected** (green dot).
- The WebSocket entry in the network panel sat at **Pending** indefinitely.
- The F4 reconnect banner never appeared.
- `PersistentChatService.connectionState()` stayed `'connected'`.

The same was true after pushing to the cluster — not a local-only artifact.

## Root cause

A WebSocket only fires `onclose` when one side **explicitly** closes it (TCP FIN/RST, application-level close frame) or when the OS-level TCP keepalive eventually trips. On a silent network drop, none of those happen. Default Linux TCP keepalive (`tcp_keepalive_time`) is 7200s — browsers don't override it for WebSocket sockets — so the WS sits in a zombie "open" state for up to 2 hours after the link is gone.

The cockpit's F4 reconnect engine is correctly wired (close code → `_scheduleReconnect()` → backoff loop → banner), but it has nothing to react to: `onclose` is the only entry point, and `onclose` never fires.

The PWA's "Off the road" toast is independent — it's driven by `navigator.onLine` / fetch failures via the service worker, which doesn't proxy WS traffic.

## Impact

- F4 only handles **server-initiated** drops in practice: orchestrator pod restart, agent crash, idle-timeout 4408, terminal 4503 — those send a real close frame and the banner appears as designed.
- **Client-initiated** disconnects (laptop sleep, Wi-Fi blip, captive-portal kick, switching networks) are silent until the TCP keepalive trips, which can be minutes to hours. Users see a working session that quietly stops responding.
- Server doesn't know either — the orchestrator's `_detach_session()` only fires on its own idle timer (~30 min) or on the WS proxy's view of the connection going stale.

## Possible solutions

### A. Application-level heartbeat (client-side watchdog)

Server already sends periodic messages during normal use (turn lifecycle, tool events). When the session is idle, **add a server→client ping every N seconds** (orchestrator's WS proxy: `{"method": "ping"}` every 20s). Client tracks `lastMessageAt` and starts a watchdog: if no message for 2× ping interval, force-close the WS with a synthetic abnormal code and let F4 take over.

- **Server work:** ~30 lines in `orchestrator/main.py`'s WS handler — add an `asyncio.create_task` that sends a heartbeat on the existing send queue. Probably already needs idle-mark cleanup, so this lands in the same code path.
- **Client work:** ~25 lines in `persistent-chat.service.ts` — bump `lastMessageAt` in `onmessage`, `setInterval` watchdog, on timeout call `this.ws.close(4001, 'heartbeat timeout')`. F4's `shouldReconnect` already treats 4001 as reconnectable.
- **Tunable:** ping every 20s, watchdog timeout at 45s. Detects drops within ~45s instead of ~2 hours.

This is the recommended path — small, contained, gives the existing F4 banner a useful trigger.

### B. Browser `online`/`offline` events

Listen on `window.addEventListener('offline', ...)` and force-close the WS when the browser flips to offline. Nice as a complement to A, but not sufficient on its own — these events fire on *the browser's view* of connectivity, which is HTTP-driven and can be wrong (captive portal that returns 200s for everything, transparent proxies, partial outages where DNS works but our endpoint doesn't).

### C. WebSocket protocol-level ping/pong

The WebSocket spec defines control-frame pings (opcode 0x9). Browsers send pong responses automatically but **do not expose any API to send pings or observe missed pongs from JavaScript**. So this isn't actionable from the cockpit; it would have to be entirely server-driven (orchestrator pings the client expecting a pong within N seconds, closes if not). Less useful than A because the close still has to propagate over the dead network — same problem in reverse.

## Recommendation

Ship **option A** as F4.5. It reuses F4's reconnect engine completely (banner, backoff, attempt cap, retry button) — the only addition is a watchdog that converts silent drops into a synthetic close event. Add `online`/`offline` listeners as a belt-and-suspenders trigger if it's free.

## Related code

- `cockpit/src/app/core/services/persistent-chat.service.ts` — `_connectWs()` (where the `onmessage`/`onclose` handlers live), `_scheduleReconnect()`, `shouldReconnect()`, `RECONNECT_TERMINAL_CODES` (4001 must stay out of this set).
- `orchestrator/main.py` — the persistent-thread WS handler (search `/ws/persistent/`); idle-detach logic is the obvious place to also drive a heartbeat.
- `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` — `isShowingReconnectBanner` computed; banner rendering. **No changes expected** for F4.5 — the trigger fix is server + service only.
- `docs/features/persistent_chat_visual_refresh.md` §F4 — references this issue.

## Decision pending

User parked F4.5 to revisit later (2026-05-10). Ship F5/F6/F7/F8 first.
