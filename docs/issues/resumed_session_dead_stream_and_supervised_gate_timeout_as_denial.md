# Resumed session shows no agent output, and supervised tool-gates that time out are reported to the LLM as "User denied"

**Status:** Filed — investigation complete, root causes isolated on a live prod session; **no fix yet**. Two distinct defects, causally linked (Defect B is only *harmful* while Defect A is in effect).
**Found:** 2026-06-26, reproduced live on session `7692637b-9c60-4698-9875-b57ec34e66a6` ("Cloud storage file inspection and summary"), main cluster (`superhuman-remote-worker`), user `operator@redacted.invalid`.
**Severity:** High. The session looks completely dead to the user (no reply, no "generating", no approval prompt) while the agent is actually healthy and working. Worse, in Supervised mode the dead stream silently converts every tool-call into a fake user-denial after a 5-minute stall, so the agent concludes the user refused and abandons real work.
**Component:** cockpit `PersistentChatService` + Angular service worker · orchestrator SSE relay (`orchestrator/main.py` `thread_event_stream`) · agent permission gate (`src/api/persistent_app.py`, `src/persistent_graph.py`)
**Related:** `session_silent_failure_audit.md` · `persistent_session_idle_expiry_message_swallow.md` · `persistent_session_swallowed_sends_and_truncated_history.md` · `cockpit_session_startup_timers_transient_sse.md` · `persistent_session_empty_chunk_history_corruption.md` · memory topics `project_session_epoch_duplicate_render`, `project_session_message_swallow_investigation` · README "Service Worker hijack of WebSocket handshakes" troubleshooting note

---

## Symptom (user-reported, then confirmed)

User resumed an existing session on the main cluster and sent a message. The UI:

- showed the green **"Connected"** badge and a "SESSION RESUMED" divider,
- showed **"Nothing to compact — context is within limits."**,
- echoed the user's own messages ("Okay, ich brauche eine ExcelTabelle …", then "Hallo?"),
- and then **nothing**: no assistant reply, **no "generating" indicator**, no approval prompt. It looked frozen.

The user sent a second message ("Hallo?") — same result.

## What was actually happening (it was *not* stuck)

The agent received both messages, replied to both, **and attempted the task**. None of it reached the browser. Reconstructed from prod logs + DB:

| Time (UTC) | Event |
|------------|-------|
| 20:06:02 | `POST /input` 200 — Excel/Vereine request received |
| 20:06:34 | Agent reply persisted ("Ja, das geht …") — **never rendered client-side** |
| 20:10:10 | `POST /input` 200 — "Hallo?" received; agent log: `Persistent loop already running … source=rest_input` |
| 20:10:28 | `[LLM] iter=3 … 1 tools` → agent calls `task_add` → **permission gate** req `6a3cf8d3` inserted `pending` |
| 20:11:06 | Headless sweeper emails user "[SRW] Approval needed: task_add" (no in-UI client acted on it) |
| 20:15:28 | `task_add` gate **expires** (300 s TTL), `decided_by = system` → agent receives **"User denied this tool call."** |
| 20:15:34 | Agent calls `web_search` (`site:michelstadt.de Vereine …`) → **gate** req `2dff830c` `pending` |
| 20:20:34 | `web_search` gate **expires** → again **"User denied this tool call."** |
| 20:20:41 | Agent gives up gating, emits a final turn ("Sorry — ich hatte auf deine Bestätigung gewartet … schreib einfach „Ja, starte"."), `turn.completed`, `ready` → idle |

Turn 1 (two days earlier) worked precisely because the user *was* connected live and approved the `srw_cloud_status` gate in-UI (`decided_by = <user uuid>`), proving the gate UX is fine when the live channel is up.

### DB evidence

`thread_permission_requests` for this thread:

```
2dff830c  web_search        pending   req 20:15:34  exp 20:20:34  by None        (later expired)
6a3cf8d3  task_add          expired   req 20:10:28  exp 20:15:28  by system
8efe3473  srw_cloud_status  approved  req 10:22:28(06-24)         by <user uuid> (turn 1, rendered + approved)
```

`thread_events` for this thread: **epoch 2, seq 1‥898**, including live `token` / `thinking` / `permission.request` / `permission.resolved` / `turn.completed` / `ready` frames (the 20:20:40‑41 tail is a full `token`…`turn.completed`…`ready` burst). **The frames the user never saw are all journaled and replayable.**

### Orchestrator log evidence

- `POST /input` 200 at 20:06 and 20:10 (input path healthy).
- On resume at 20:00:18, two `GET …/stream` requests both **completed in <10 ms** (a healthy SSE stays open — these closed immediately), interleaved with `GET …/connection` returning **425** until the workspace was ready at 20:00:36.
- `Sent permission-pending email (thread=7692637b req=6a3cf8d3)` at 20:11:06 (`main.py` `thread_permission_notify_sweeper`).

### Agent log evidence

`Persistent loop already running`, `POST /api/input 200`, embedding + rerank (one benign `rerank 403` — separate issue), `codex-proxy /v1/responses 200`, `[LLM] … iter=3`, then **only** `/ready` + `/health` probes, heartbeats, and lifecycle GETs until the next gate. The agent is alive and progressing the entire time.

---

## Defect A — resumed session's live stream delivers no agent frames to the browser

**Effect:** After resume, the browser receives **none** of the agent's `token` / `thinking` / `permission.request` / assistant-message frames, even though all of them are written to `thread_events` (here: epoch 2, up to seq 898). The "Connected" badge is misleading — it reflects the orchestrator control/last-known status, not a live agent stream. This is the same family as the prior "swallowed session" / epoch-duplicate-render investigations.

**Where it lives:**
- SSE relay: `orchestrator/main.py` `thread_event_stream` (`@app.get("/api/persistent/threads/{thread_id}/stream")`, ~`main.py:16460`). On **epoch mismatch** (`cursor_epoch != server_epoch`, ~`16513`) or **cursor older than retention** (~`16545`) it emits a single `gone_beyond_horizon` frame and **returns immediately** — which matches the <10 ms `GET …/stream` closes observed at resume.
- Cursorless re-attach floor: `_no_cursor_replay_start` (~`main.py:16432`) anchors past the last completed turn to avoid double-rendering.
- Frame journaling (the part that *works*): agent `_broadcast()` → `_persist_event()` into `thread_events` (`src/api/persistent_app.py:2512`, `:2594`).

**What is NOT yet pinned (open):** whether the browser's EventSource (a) reconnected to a **stale epoch** and got `gone_beyond_horizon` but the cockpit handler failed to re-attach a live epoch-2 stream, (b) was **hijacked by the service worker** (`ngsw-worker.js`, the README "Unexpected response code: 200" footgun), or (c) connected but silently dropped and never reconnected while keeping the "Connected" badge. The access logs captured did not include the `?cursor=`/epoch query params, so the exact branch is unconfirmed.

**To confirm root cause:** capture the cockpit `GET …/stream` request with its cursor/epoch query param on resume and compare against `server_epoch` (=2 here); check DevTools → Application → Service Workers for a SW intercepting the stream; check whether the cockpit's `gone_beyond_horizon` handler re-attaches cursorless and renders the **in-flight pending** `permission.request`.

**Proposed direction:** make the cockpit prove live-channel liveness (drive the "Connected" badge off the typed `ping` event / last-frame timestamp, not off connect success), and ensure the `gone_beyond_horizon` → REST-history-reload path also re-attaches a live tail **and** re-surfaces any currently-pending `permission.request` (REST `/messages` history does not carry the pending gate — it only lives in `thread_events`).

---

## Defect B — a supervised gate that **times out** is reported to the LLM as an explicit user denial

**Effect:** When a permission gate is never answered, it expires after the **300 s TTL** (`expires_at DEFAULT now() + interval '300 seconds'`, migration `0005_thread_permission_requests.sql:46`) and the agent injects the ToolMessage **`"User denied this tool call."`** into the conversation — identical to a real deny. The LLM therefore believes the user *refused* the tool, when in reality the prompt never reached the user (Defect A). After two such "denials" here, the agent abandoned the actual task and fell back to asking the user to re-confirm.

**Where it lives:**
- `src/api/persistent_app.py:2916` `_wait_for_permission_resolution()` correctly returns a **3-state** status: `'approved' | 'denied' | 'expired'` (CAS-expires the row on timeout, ~`:2960`).
- `src/api/persistent_app.py:3122-3123` collapses it to a boolean: `approved = (final_status == "approved")` — **the `expired` vs `denied` distinction is discarded here.**
- `src/persistent_graph.py:1699-1713` then treats any `not approved` identically and writes the literal `"User denied this tool call."` (`:1706`).

**Proposed direction:**
1. Thread the 3-state status (or at least an `expired` flag) from `permission_check` through to the ToolMessage, and word the timeout case honestly, e.g. *"Approval request timed out with no response — the prompt may not have reached the user."* so the LLM does not infer a deliberate refusal.
2. Consider **not auto-failing on expiry at all** for interactive sessions: instead pause/await (flip `awaiting_user`, which already happens for the untethered case at `persistent_app.py:3116`) rather than synthesize a denial — the email magic-link approval path can still land after the agent's local wait window.
3. Distinguish `expired` from `denied` in `tool_decisions` / audit so this failure mode is observable rather than masquerading as user intent.

**Secondary:** `task_add` (adding a todo) is being gated in Supervised mode at all. Gating a no-risk, internal bookkeeping tool generates needless approval friction and was the first domino here. Consider a per-tool risk floor so trivial/internal tools are not gated even under Supervised.

---

## Recovery / workaround (operational)

The session is **not** server-side stuck — the agent is idle/`ready` waiting for input. For the user:

1. **Hard-reload** the cockpit tab (Ctrl/Cmd+Shift+R). If the live stream still doesn't come alive, **unregister the service worker + clear caches** (DevTools → Application → Service Workers → Unregister) and reload — see README "Service Worker hijack" snippet. History (including the unseen replies) reloads from REST; a healthy resume re-attaches a live epoch-2 stream.
2. Either reply and approve each prompt as it appears, **or** switch the session **Supervised → Auto-accept** to stop per-tool gating for a benign research task.

## Reproduce

1. Start a Supervised persistent session; run one turn that requires (and gets) an approval, so it works.
2. Leave/return such that the cockpit re-attaches the SSE stream on a stale epoch (or with the SW intercepting it).
3. Send a message that makes the agent call a gated tool.
4. Observe: no "generating", no reply, no approval card in the UI; agent log shows the LLM ran and a `thread_permission_requests` row goes `pending`; ~5 min later it flips `expired`/`system` and the agent records "User denied this tool call." and moves on.

## Open questions

- Exact Defect-A branch (epoch mismatch vs SW hijack vs silent drop) — needs the cockpit stream request's cursor/epoch + SW state at repro time.
- Should expiry pause-and-wait (with email approval still able to land) instead of failing the tool, for interactive sessions specifically?
- Should the "Connected" badge be redefined to require recent live frames, so users aren't misled during a dead stream?
