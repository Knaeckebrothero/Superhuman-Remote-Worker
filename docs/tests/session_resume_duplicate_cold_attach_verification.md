# Session-Resume Duplicate on Cold Attach — Verification Runbook

Verifies the fix for the 2026-06-22 report: opening an **already-active session on a
second device** rendered the last assistant turn **twice**, split by a spurious
`SESSION RESUMED` divider. A browser refresh on either client cleared both artifacts.
Only the assistant turn duplicated — never the user message.

Background: `docs/features/headless_persistent_sessions.md` (event-log + replay-from-cursor
architecture); memory `srw_session_resume_duplicate_cold_attach` (full root-cause write-up);
sibling path already fixed the same render for stale cursors (`_handleGoneBeyondHorizon`,
`cockpit/src/app/core/services/persistent-chat.service.ts`).

**Root cause (one line):** a fresh client has no cached SSE cursor, so it opened the stream
with no `last_event_id`; the orchestrator then replayed the **whole epoch from seq 0**
(`last_sent_seq = … else 0`), re-delivering every completed turn as a *live* frame. The
cockpit reducer can't reconcile those against REST history — history turns are keyed by
message id (`historyToTurns` → `id: m.id`, `historical: true`), replayed turns by `turn_id`
(`turn-reducer.ts` `turn_started`: `t.id === action.turnId`) — so the turn renders twice,
and `showSessionDividerAfter` draws the divider at the historical→live boundary.

- **Only the assistant duplicated** because `_accept_user_input` (`src/api/persistent_app.py`)
  persists the user message to `thread_messages` but never `_broadcast`s it — so it is in
  REST history but *not* in the `thread_events` log, and is never replayed.
- **Refresh cleared it** because the first (buggy) attach saved a tail cursor (`_saveCursor`),
  so the next open replayed only `seq > tail` = nothing.

**What was fixed** (`orchestrator/main.py`, commit `55a16c14` on `develop`, bundled with
unrelated Codex-proxy 401 work):

A no-cursor attach now anchors its replay floor **past the last completed turn** instead of
seq 0, via `_no_cursor_replay_start` (~`orchestrator/main.py:15691`):

```sql
SELECT COALESCE(MAX(seq), 0) FROM thread_events
WHERE thread_id = $1 AND epoch = $2
AND kind IN ('turn.completed', 'turn.error')
```

Both terminal kinds persist their turn to `thread_messages` (a completed turn via
`_loop_on_turn_complete`; an errored turn via `_loop_on_error`'s `role='error'` row), so the
client already has them from REST history — replaying only what comes *after* the last
terminal event delivers just the in-flight, not-yet-persisted turn. Returns `0` when no turn
has finished yet (first turn still streaming, absent from history) so that turn still replays.
The **with-cursor path is byte-for-byte unchanged** (~`orchestrator/main.py:15832`); only
fresh attaches behave differently. `turn.interrupted` is intentionally **not** an anchor kind
— the agent never broadcasts it (it is a frontend-reducer-only action).

**Coverage map** — what each layer proves:

| Layer | Proves | Needs |
|---|---|---|
| §0 Automated unit tests | the anchor logic (past terminal events; 0 when none) | local pytest |
| §1 Static check | the fix is present in the deployed code | repo / image |
| §2 DB-level proof | a no-cursor attach would now replay nothing past the last completed turn | DB access |
| §3 Live two-device | end-to-end: no duplicate, no `SESSION RESUMED` divider | dev cluster + deploy |
| §4 Stream inspection | the `/stream` response carries no completed-turn frames on cold attach | dev + browser |

Target time: **~12 min** (§0 automated ~1 min; §3–§4 live ~10 min after deploy).

---

## 0. Automated tests (quick gate — no cluster)

Exercise the anchor helper with a mocked Postgres connection (SQL-substring style, like the
file's existing `_persist_event` test). They run locally despite the env being noisy for the
full suite (see memory `local_test_env_vs_ci_and_ruff`).

```bash
python -m pytest tests/test_thread_events_phase2.py::TestNoCursorReplayStart -q
```

**Pass criteria:** `3 passed`.

- `test_anchors_past_last_terminal_event` — returns the terminal `MAX(seq)` (e.g. 7), **not**
  0, and the query filters on `turn.completed` + `turn.error`.
- `test_returns_zero_when_no_turn_has_finished` — first turn still in flight → 0 (replay from
  the start).
- `test_coerces_null_max_to_zero` — defensive `NULL → 0`.

Regression (the stream endpoint's other behavior must be intact):

```bash
python -m pytest tests/test_thread_events_phase2.py tests/test_thread_access.py -q
```

**Pass criteria:** all green (19 + 12 at the time of writing).

---

## 1. Static check: the fix is present

Run against the checked-out branch (or `kubectl exec` into the orchestrator pod and `rg`
inside `/app/orchestrator/main.py`) to confirm the deployed image carries the change.

```bash
rg -n "_no_cursor_replay_start" orchestrator/main.py
```

**Pass criteria:**
- The helper is **defined** once (`async def _no_cursor_replay_start`).
- It is **called** in `thread_event_stream`'s no-cursor branch — i.e. the replay floor is
  `if cursor_seq is not None: last_sent_seq = cursor_seq` else `await _no_cursor_replay_start(...)`,
  and the old `last_sent_seq = cursor_seq if cursor_seq is not None else 0` is gone.

---

## 2. DB-level proof (decisive, no second browser needed)

This proves the backend behavior directly: for an active thread sitting idle after a completed
turn, a no-cursor attach replays **nothing**. Use `psql` against the orchestrator's Postgres
(or the MCP `query_table`). Substitute the thread's full UUID — the Cockpit header shows a
short prefix (e.g. `252a21cc`).

```sql
-- 1. Thread + its current epoch.
SELECT id, events_epoch, status FROM threads WHERE id::text LIKE '252a21cc%';

-- 2. The anchor the endpoint now computes for a no-cursor attach.
SELECT COALESCE(MAX(seq), 0) AS anchor
FROM thread_events
WHERE thread_id = '<thread-uuid>' AND epoch = <events_epoch>
  AND kind IN ('turn.completed', 'turn.error');

-- 3. Events a no-cursor attach would replay (seq > anchor).
SELECT seq, kind FROM thread_events
WHERE thread_id = '<thread-uuid>' AND epoch = <events_epoch> AND seq > <anchor>
ORDER BY seq;
```

**Pass criteria:**
- When the session is **idle** (last event is a `turn.completed`/`turn.error`), step 3 returns
  **0 rows** → the cold attach replays nothing → no duplicate.
- When a turn is **in flight**, step 3 returns only that turn's events (a `turn.started` plus
  its `token`s) and **no** events from any earlier, already-completed turn.

**Fail signal (pre-fix behavior):** step 3 would return the entire epoch starting at `seq 1`
— including completed turns' `turn.started`/`token`/`turn.completed` rows. (To see the old
floor for contrast, re-run step 3 with `seq > 0`.)

---

## 3. Live end-to-end: open an active session on a second device (dev cluster)

> Prereq: the `develop` image carrying commit `55a16c14` is built and rolled out to dev (dev
> tracks `sha-XXX` tags from develop CI — see memory `deployment_topology`). Confirm via the
> deploy image-tag bump or §1 against the running pod.

1. On **device A** (or browser A), start a **new session** and send one prompt; wait for the
   assistant reply to finish.
2. On **device B** (a different browser/profile, or a fresh incognito window — it must have
   **no cached cursor** for this thread), open the **same session** from the Sessions list.

**Pass criteria:** device B shows the conversation **once** — the user message and the single
assistant reply — with **no** `SESSION RESUMED` divider and **no** duplicated bubble.

**Fail signal (regression):** the last assistant turn appears twice, split by a
`SESSION RESUMED` divider; a refresh makes it correct.

Bonus — **mid-turn attach:** on device A send a prompt that takes a while, and open device B
*while it is still streaming*. Expect device B to show the in-flight turn (no duplicate of the
earlier completed turns). The in-flight turn may render from the attach point forward; it is
fully correct after it completes / on refresh.

---

## 4. Inspect the SSE stream on cold attach (dev + browser DevTools)

No DB access needed. In device B's DevTools → Network, find the EventSource request to
`…/persistent/threads/{id}/stream` opened on attach (it has **no** `last_event_id` query
param on a fresh client).

**Pass criteria:** its event stream carries **no** `turn.started`/`token`/`turn.completed`
frames for the already-completed turn(s) — only a `: open` comment, periodic `ping`s, and
frames for any genuinely new activity.

**Fail signal (regression):** the stream re-emits the completed turn's `turn.started` + `token`
frames immediately on connect.

---

## Known gaps — NOT covered by this fix

- **`turn.completed`-before-persist race:** `_loop_on_turn_complete` broadcasts `turn.completed`
  *before* its `thread_messages` write finishes. A fresh client whose REST `loadHistory` lands
  in that ~ms window could briefly miss the just-completed turn; it self-heals on refresh. Far
  narrower than the bug it replaces.
- **Frontend reconciliation (Solution B) not implemented.** The reducer still keys history
  turns by message id and live turns by `turn_id`, so any *future* full-epoch replay (e.g. a
  forced cursor drop) could re-introduce the duplicate. Since `turn_id == turn_number`
  (1-indexed, no off-by-one), the reducer *could* reconcile by number and reset-in-place — a
  belt-and-suspenders follow-up, deliberately deferred.
- **Other `/stream` consumers.** Only the cockpit (which always loads REST history before
  opening SSE) is exercised. A hypothetical consumer that relied on no-cursor replay-from-0 to
  bootstrap full history would now receive only the in-flight turn.

---

## Rollback

The change is small and additive: one helper (`_no_cursor_replay_start`) plus a 6-line
branch at the replay-floor assignment in `thread_event_stream`. To revert, restore
`last_sent_seq = cursor_seq if cursor_seq is not None else 0` and delete the helper. No data
or schema is affected (it only changes which stored events a fresh attach replays).
