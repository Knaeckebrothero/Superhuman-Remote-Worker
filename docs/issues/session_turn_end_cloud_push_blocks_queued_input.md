# Turn-end cloud push blocks the next turn; queued input is invisible in the cockpit

**Status**: FIX BUILT 2026-08-06 — see "The fix" below. Live repro on dev
2026-08-05, thread `5833c729-c0cd-496f-9a40-e9b811ae0ced` (agent pod
`srw-agent-j-3f9fbcc4`, build `03442f8`).

## What happens

The user sees the assistant's answer, the composer goes idle (send button
reverts to mic), and then their next message appears to be swallowed: no
"Agent is working…" placeholder, no typing dots, no stop button — for **three
minutes**. Then the reply suddenly streams. Messages sent during the window
stack up and drain back-to-back afterwards.

Timeline from the repro (all UTC):

| Time | Event |
|---|---|
| 21:53:24 | Turn 8's final LLM call returns; `turn.completed` broadcast; UI idle |
| 21:54:28 | User sends the next message → agent logs `Persistent loop already running: source=rest_input` → queued |
| 21:55:43 | "Hello?" → queued |
| 21:56:00 | "Test" → queued |
| 21:56:24 | `Synced 29 file(s) to cloud`; `Turn 8 complete`; queue drains |
| 21:57:58 / 21:58:21 / 21:58:44 | Turns 9/10/11 complete back-to-back |

## Root causes (three independent defects)

### 1. The turn-end cloud push is awaited inline, after `turn.completed`

`_loop_on_turn_complete` (`src/api/persistent_app.py`) broadcasts
`turn.completed` (UI goes idle), *then* awaits
`_resilient_cloud_sync("push", workspace_sync.push_all, …)` before returning.
The loop only re-parks — and only dequeues the next input — after the push
finishes. So the turn is visually over minutes before it is actually over, and
every queued input waits on WebDAV round-trips.

### 2. The push re-uploads the whole workspace on a fresh pod, serially

`WorkspaceSyncBase._push_via_backend` (`src/services/cloud_sync/base.py`)
dedups by size against `self._pushed_sizes` — a plain in-memory dict. A fresh
agent pod (the repro pod was 13 minutes old; sessions get a fresh pod after
every idle recycle) starts empty and re-uploads **every file**: 29 files ×
(SSH read + WebDAV MKCOL/PUT through the edge, one at a time) = 3 minutes.
Steady-state turns still pay a full walk + per-file SSH `read_file` (data
transfer included) just to learn nothing changed (~5–10 s/turn).

### 3. An accepted-but-queued message has no client-visible state

The agent's `POST /api/input` 200s (input persisted + enqueued — correct), the
cockpit removes it from the outbox (`isPendingSend` → false), and nothing else
happens until the agent broadcasts `turn.started` at the top of the *next*
turn. `isStreaming` is `activeAssistantTurnId !== null`
(`persistent-chat.service.ts`), set only by that frame. Between accept and
turn-start there is no signal at all: placeholder falls back to default, the
button reverts to mic, dots don't render. Any queueing — even a fast one —
reads as a swallowed message.

## Found while fixing (also addressed)

- **Remote listing was never recursive.** `_list_remote_files` is documented
  as recursive ("List the session folder recursively") but both
  implementations call webdav3 `client.list("/")` — a Depth-1 PROPFIND. Pull
  therefore only ever saw **root-level** files: cloud-side edits under
  `output/`, `uploads/`, etc. never reached the agent (observed: "Pulled 1
  file(s)" on a workspace with 29 synced files). This also matters for the
  push fix: dedup seeding needs the full remote tree.
- **Memory extraction crashed every turn on list-shaped content.**
  `_format_messages_for_extraction` (`src/services/auxiliary.py`) called
  `content.strip()`; AIMessage content from the responses-API path is a list
  of blocks → `AttributeError: 'list' object has no attribute 'strip'`,
  killing extraction (contained, but every time).
- **Short thread id → 500.** `GET /api/persistent/threads/5833c729` (8-char
  prefix) raised an unhandled `asyncpg.DataError` out of the UUID bind in
  `PostgresDatabase.get_thread` → 500. Should be a 404.
- *Not addressed here*: `recall_two_tier` retriever timing out every turn on
  dev (`Memory retriever failed (contained): TimeoutError`) — separate
  transport issue, see `project_reranker_transport_decoupling`.

## The fix

### A. Push moves off the turn-close critical path (`src/api/persistent_app.py`)

`_loop_on_turn_complete` now *spawns* the push as a background task
(`_run_turn_end_cloud_push`, same `_resilient_cloud_sync` retry + the same
`workspace_sync.pushing/pushed/error` broadcasts). The loop parks immediately;
queued input starts its turn (and broadcasts `turn.started` → UI shows
working) without waiting.

Ordering is preserved by awaiting the pending task at the two places that
must not overlap with it:

- `_loop_on_turn_start` awaits it **before** the turn-start pull — strict
  push(N) → pull(N+1) per mount, no concurrent walk of the same dedup state;
- both teardown paths (`_terminate_session`, `_handle_archive`) await it
  before their final `push_all` + `aclose`, so teardown can't race or close
  the transport under an in-flight push.

Net effect: the push cost overlaps the user's read-the-answer idle time. Only
a user who replies faster than the push finishes waits — visibly, inside a
started turn.

### B. Dedup seeds from the remote tree; unchanged files cost a stat, not a read (`src/services/cloud_sync/base.py`)

- New `_list_remote_tree()` in the base class walks the mount by repeated
  Depth-1 `_list_remote_files(rel_dir)` calls (primitive gains the `rel_dir`
  parameter; ignored subtrees pruned; defensive against servers echoing the
  listed collection). Listings now carry `size` (webdav3 `get_info` exposes
  it).
- First push (or first pull — shared `_remote_seeded` flag) seeds
  `_pushed_sizes` from the remote listing, so a fresh pod skips every file
  whose size matches what the cloud already has. The 29-file/3-minute
  re-upload becomes a no-op.
- `_push_via_backend` checks `backend.stat()` (one SFTP round-trip, no data)
  against the dedup entry and only falls back to `read_file` when the size
  moved. `_push_local` gets the same seeded-size skip for its first pass
  (mtime dedup takes over after).
- `pull()` now uses the recursive tree (fixing the subdir blind spot) with a
  reconcile guard: a file with no tracked etag whose remote size equals the
  local size is recorded as in-sync without downloading — attach stays fast
  and unpushed local work isn't clobbered by a same-content re-download.

Size-equality remains the dedup heuristic it always was (same-size,
different-content changes are missed until the size moves); the seeding and
reconcile extend that existing, documented tradeoff to pod boundaries rather
than introducing a new one.

### C. Queued input gets a visible state (cockpit)

`persistent-chat.service.ts` tracks `pendingTurnCount`: +1 when a send is
accepted by the server (2xx from `/input`, not the 409-duplicate path), −1 on
`turn.started`; reset on `session.ended` / `turn.error` / `interrupt.ack` /
disconnect / thread switch. `isAwaitingTurn = pendingTurnCount > 0 &&
!isStreaming`.

While awaiting: composer placeholder shows "Agent is working…", the action
button shows the spinner (not mic, not stop — there is no turn to stop yet),
and a standalone typing-dots bubble renders after the last turn.

### D. Small fixes riding along

- `PostgresDatabase.get_thread` validates the UUID and returns `None`
  (→ caller's 404) for malformed ids.
- `_format_messages_for_extraction` flattens list content via
  `content_to_summary_text` (which also keeps base64 image payloads out of
  the extraction prompt).

## Acceptance criteria

1. On a fresh agent pod whose workspace matches the cloud, the first turn-end
   push uploads **0 files** (verified by unit test seeding + k3d smoke:
   `Synced 0`/no `Synced` line, turn parks within ~2 s of `turn.completed`).
2. A message sent within seconds of a turn completing starts its turn without
   waiting for the previous turn's push; `turn.started` arrives before the
   push finishes (unit test: ordering hooks; k3d: send immediately after a
   29-file turn).
3. While an accepted input waits for its turn, the cockpit shows the working
   placeholder + spinner + dots (vitest: accept-without-turn-started sets
   `isAwaitingTurn`; cleared by `turn.started`).
4. A cloud-side edit under `output/` reaches the agent on the next turn-start
   pull (unit test with nested remote listing; previously impossible).
5. `GET /api/persistent/threads/<8-char-prefix>` returns 404, not 500.
6. Teardown after a turn never runs `push_all` concurrently with the
   turn-end task: both teardown paths call `_await_pending_cloud_push()`
   before their final sync (unit tests cover the helper's semantics and the
   turn-start ordering; the terminate paths reuse the same helper).

## Verification so far

**Unit/spec (2026-08-06)**: 28 cloud_sync base tests, 6 app-ordering tests,
42 thread-db tests, 194 chat-service + 97 chat-component specs — all green.
Full pytest sweep: 13858 passed; the 11 remaining failures reproduce with
this change stashed (in-flight MCP-migration work + known py3.14 env noise).

**Live transport smoke (2026-08-06, k3d Nextcloud, real webdav3)** —
`scratchpad/webdav_live_smoke.py`, all checks passed:

- fresh instance pull: **0 downloads** (reconcile-by-size), push: **0
  uploads** (seeded) on a 3-file mount incl. subdir + binary — criteria 1
  and part of 2 at the transport layer;
- a cloud-side edit under `output/` pulled — criterion 4, previously
  impossible;
- a local edit still pushed (dedup does not over-skip).

This validates the risky webdav3 assumptions the unit fakes can't: subdir
listing path shapes, string `size` props, collection self-echo.

## Verification owed after deploy

- Session-level k3d/dev smoke of criterion 2's wall-clock behavior: send a
  message seconds after a heavy turn completes on a fresh pod → turn starts
  promptly, composer shows working/spinner while queued.
- Dev-cluster check on thread `5833c729`: reply latency after idle recycle,
  and `Pulled` lines showing subdir files when edited in Nextcloud.
