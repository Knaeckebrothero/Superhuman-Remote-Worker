# Persistent session — message-granular persistence & faithful resume

**Date:** 2026-06-08
**Status:** Open. Design. This doc started as "mid-turn crash loses the turn's
output" and grew: that durability gap and the resume-OOM/fidelity gap turn out
to be the **same root cause** and want **one fix**. Confirmed by code read. **Chosen shape:**
direct DB writes (the agent already has the pool) + a message-granular
`boundary_seq` resume cursor, built in four phased slices (see Implementation
order); summarization itself is reused unchanged (the existing rolling-summary
pattern).
**Component:** Persistent-session persistence + resume — `src/api/persistent_app.py`
(turn callbacks, `_restore_session_messages`, `_record_compaction`,
`_save_turn_ai_messages`), `src/persistent_graph.py` (`_execute_turn` inner loop
+ compaction), `src/database/postgres_db.py` (`get_thread_messages_history`,
`get_latest_compaction_checkpoint`), and the `thread_messages` schema. The worker
path (`src/graph.py` + `AsyncSqliteSaver`) is the counter-example, not in scope.
**Severity:** High. Symptom 1 loses up to ~an hour of agent work on a crash;
Symptom 2 wedges large sessions on resume (exit-137 / `/ready` timeout).

## Two symptoms, one root cause

**Symptom 1 — a mid-turn crash loses the turn's output.** Messages persist only
at turn boundaries, so a crash mid-turn discards everything the agent produced
that turn (reasoning, tool calls, tool results).

**Symptom 2 — resume can't reconstruct "summary + the exact live tail."** The
compaction boundary is recorded at *turn* granularity (`boundary_turn`), but the
agent's live context is *message* granular (compaction fires mid-turn,
repeatedly, keeping a message-level tail). So resume either reloads whole
post-boundary turns, or — with no checkpoint — the **entire append-only log**,
which is the exit-137 OOM / `/ready`-timeout wedge on big threads.

**Root cause (shared):** persistence is turn-batched and the summary→messages
link is turn-granular, while the agent's real working set is a message-level
`summary + tail`. The persisted log and the live context drift apart, and
**nothing records the exact message at which the latest summary ends.**

## How it works today (verified in code)

**Persistence is per-turn, and mid-turn events are display-only:**

| When | What persists | Where |
|---|---|---|
| Turn start | the **user** message | `_loop_on_turn_start`, `persistent_app.py:2659-2674` (bounded 5 s) |
| During turn | nothing — `on_token`/`on_thinking`/`on_tool_start`/`on_tool_result` only `_broadcast` to the WS | `persistent_app.py:2270/2274/2278/2295` |
| Turn complete | **all** AI + tool messages, one batch | `_loop_on_turn_complete` → `_save_turn_ai_messages`, `:2677-2697` / def `:3163` |

**Other load-bearing facts:**

- `thread_messages` is an **append-only full log** — compaction never deletes
  rows; it prunes only in-memory `_session.messages` and writes one
  `role='summary'` row.
- Compaction runs **inside the inner agentic loop**: `_execute_turn`
  (`persistent_graph.py:453`) loops `while True:` (`:565`) and calls
  `ensure_within_limits` on every iteration (`:616`), breaking only when the LLM
  stops emitting tool calls (`:1028`). → compaction fires **mid-turn, possibly
  several times per turn.**
- Each compaction writes a summary via `_record_compaction` with
  **`boundary_turn = turn_count - 1`** (`persistent_app.py:2772`) — turn-granular.
  Summaries are rolling-merged: the newest `role='summary'` row is cumulative
  (`get_latest_compaction_checkpoint`).
- Mid-turn-summarized messages are pruned from `_session.messages` **before**
  `_save_turn_ai_messages` runs at turn-complete (it saves from the
  already-compacted `_session.messages`, `:3181`), so those messages are **never
  individually persisted** — they survive only inside the summary.
- **Writes go through the orchestrator, not the DB — a pure pass-through.** The
  agent persists via `_orchestrator_client.save_thread_message` →
  `POST /api/agents/threads/{id}/messages` (`main.py:11468`) →
  `postgres_db.save_thread_message` (a plain INSERT). The endpoint adds nothing
  but an `X-Internal-Key` check, and the persistent agent is its only caller —
  yet the agent *already* holds a direct asyncpg pool (`PostgresDB`,
  `src/database/postgres_db.py`) that it uses for resume reads and config writes
  (`store_resolved_config`). So message writes take a needless REST hop and the
  agent never sees the row's generated `id`/`seq`.
- Resume (`_restore_session_messages`, `:1199`): **Path A** =
  `[summary] + history(since_turn=boundary_turn)`; **Path B** (no checkpoint) =
  full load `limit=None`.

## Why turn-granularity breaks resume

- A turn can be hundreds of messages (N LLM↔tool cycles). `boundary_turn = turn-1`
  makes resume reload the **whole** post-boundary turn — not "the messages after
  the summary."
- **Path B loads the entire append-only log** → memory spike + long synchronous
  startup → exit-137 (OOM-kill or missed `/ready` probe), then a wedge on the
  stale `threads.agent_id`.
- A fixed `LIMIT N` can't fix this: it grabs the last N rows with **no
  relationship to where the summary ended.** Summary at message 780 of an
  800-message turn → `LIMIT 20` only works by luck; `LIMIT 1000` reloads
  everything.

## Why this is worse than "lost text"

1. **Severity scales with turn length.** A persistent turn is *agentic* — one
   user message drives many LLM calls + tool calls, for a coding/research
   session potentially **>1 hour** in a single turn. All of it evaporates on a
   mid-turn crash.
2. **Workspace side-effects survive, but the *record* doesn't.** `write_file`/
   shell act on the durable remote workspace, so a crash can leave half-applied
   changes the conversation has no memory of → the resumed agent re-runs blind to
   its own partial work (redo/duplication). A correctness wrinkle, not just
   durability.
3. **No checkpointer.** `persistent_graph.py` is compiled without a LangGraph
   checkpointer; Postgres `thread_messages` is the only persistence, written only
   at turn boundaries.

## Contrast with the worker

| | Worker (`src/graph.py`) | Persistent session (`src/persistent_graph.py`) |
|---|---|---|
| Checkpointer | `AsyncSqliteSaver` (`agent.py:532`) | **None** |
| Persist granularity | after **every graph node** | only at **turn boundaries** |
| Worst-case crash loss | ≤ 1 node | **the whole in-flight turn** |
| Recovery trigger | auto re-dispatch (`recover_orphaned_jobs`) | manual `/resume` (thread → `ended`) |

Mid-unit, the *interactive* session is the **more lossy** of the two.

## The fix — message-granular persistence + boundary

One coherent change; the pieces are interdependent.

### 1. A stable per-message order (`seq`)

```sql
ALTER TABLE thread_messages ADD COLUMN seq BIGSERIAL;
CREATE INDEX ON thread_messages (thread_id, seq);
```

`seq` is monotonic insertion order — exact and tie-free, unlike
`(turn_number, created_at)`. It becomes the cursor everything else keys off.

### 2. Direct DB writes + incremental message persistence (closes Symptom 1)

Two coupled changes:

**(a) Write messages directly, not through the orchestrator.** Add
`save_thread_message(...)` to the agent's `src/database/postgres_db.py` (mirroring
the existing two-copy split with `get_thread_messages_history`): a direct
`INSERT … RETURNING seq, id` that accepts a **caller-supplied `id`**. Switch the
three persistent call sites (`persistent_app.py:2790/3109/3230`) off
`_orchestrator_client.save_thread_message`. This removes the per-message HTTP hop
(making per-step persistence cheap), lets the agent own the `id`, and returns
`seq` in the same call. The orchestrator REST endpoint stays for back-compat; it
just goes unused by the persistent path.

**(b) Persist each message as it is produced** — instead of the turn-complete
batch. The turn-complete save becomes a reconciliation backstop. *Built via a
dedicated `persist_message` callback on `PersistentLoopCallbacks`, called by
`_execute_turn` right after every `messages.append(...)` (the AI step + each tool
result + the multimodal follow-up). Not piggybacked on `_loop_on_tool_result` as
first sketched — that callback only carries the result string, not the message
object, and never sees the `AIMessage`. The transport impl `_loop_persist_message`
serializes the one message through the shared `_persist_one_message` and writes it
with `_session.turn_count` as the turn number (the loop callback carries no turn
id, same convention as `_record_compaction`).*

Requirements:

- **Stable id on every message.** Today only AI responses get an id minted
  (`persistent_graph.py:59-60`); `ToolMessage`/`HumanMessage` (`:350`, `:1038-1100`)
  and the live summary do not. `_ensure_msg_id` stamps the rest at creation so
  every row has a deterministic key. (Couples with
  `[[persistent_session_restored_messages_no_ids]]`, which already mints ids on
  restore.)
- **The in-memory id is NOT a UUID, but the PK column is.** Message ids are
  provider-issued (`chatcmpl-…`, `resp_…`) or locally minted (`msg_…`); none parse
  as UUIDs, and `thread_messages.id` is `UUID`. asyncpg rejects a non-UUID string
  for a UUID param *client-side*, so passing `id=msg.id` raw would raise and the
  non-fatal `except` would **silently drop every save**. The agent's
  `save_thread_message` therefore runs the id through `_coerce_row_id`: valid
  UUIDs pass through; everything else maps via `uuid5` (deterministic → re-saves
  hit the same row); `None` mints a fresh UUID. The DB row id has always been
  independent of the in-memory id (restore assigns a fresh `uuid4` regardless),
  so deriving it breaks no correlation — it only makes the key stable.
- **Idempotent upsert** keyed by that (coerced) `id` (`ON CONFLICT (id) DO
  UPDATE`), so the turn-complete reconciliation never double-inserts a row already
  written incrementally; the reconciliation pass updates the content columns but
  never touches `seq` (the cursor stays stable across upserts).
- **Bounded/non-blocking** writes (reuse the `asyncio.wait_for(timeout=5)`
  pattern; persist per step/tool-result, not per token).
- `_repair_tool_pairing` (already on the resume path) cleans a tool pair split at
  a crash boundary.

### 3. Incremental summary persistence + a message-level boundary (closes Symptom 2)

**This is the piece that makes "resume where the agent left off" actually work —
and the one that's easy to get wrong.** The agent's live context at any instant
is `latest_summary + messages after its boundary`. To resume that *exact* state,
the **latest summary must already be in the DB, paired with a precise message
cursor** — you cannot resume a summarized state you never saved.

- Every compaction (which already fires mid-turn and already calls
  `_record_compaction`) records **`boundary_seq`** — the `seq` of the last
  message the summary covers — **alongside `boundary_turn`**.
- **Where the boundary actually comes from (corrected during build).** The doc
  first assumed `_session.messages` is pruned to `[summary] + kept_tail`, so the
  boundary would be `_session.messages[1]`. It isn't: the persistent loop
  compacts a *transient* `prepared = list(messages)` copy each LLM call and never
  prunes `_session.messages` (only `_handle_compact`'s manual path does). And the
  kept tail is re-added as **fresh id-less copies**, while the removal markers
  cover the *entire* conversation — so neither `_session.messages` nor the
  post-compaction list reveals the split. The only place that knows it is
  `summarize_and_compact`, where `messages_to_summarize = conversation[:safe_start]`.
  So it now stashes `self._last_compaction_boundary_id =
  original_conversation[safe_start-1].id` (the newest *covered* message, original
  id so it matches the persisted row). The transport reads that, resolves it to a
  seq with `get_seq_for_message_id` (one indexed lookup, coercing the id the same
  way the write did), and stores `boundary_seq`. Resume then loads
  `seq > boundary_seq`. The persistent path calls `ensure_within_limits` without
  `force=True`, so `summarize_and_compact` runs exactly once per compaction and
  the side-channel can't go stale.
- **Ordering invariant:** a summary may claim `boundary_seq = S` only once every
  message with `seq ≤ S` is persisted. Step 2 makes this hold automatically, but
  the write order must be enforced — **persist the messages, then persist the
  summary that covers them.** A summary that references unpersisted messages
  would make resume inconsistent.
- **Save *every* intermediate summary, not one per turn.** Because compaction can
  fire several times within a turn, and each summary is what the agent operates
  under until the next one, each must be written immediately with its own
  `boundary_seq`. Deferring summary writes to turn-complete would lose the
  agent's actual current summarized state if the pod dies mid-turn.
- **Resume reads the latest summary — and the existing summarizer already
  produces it cumulatively.** `summarize_and_compact` detects prior summaries by
  the `[Summary of prior work]` prefix and folds them into the new one — the code
  itself calls this the *"rolling summary pattern"* (`context.py:1631-1633`,
  old-summary detection at `:1547-1551`). So the newest `role='summary'` row is
  always cumulative and resume reads just that one. **We reuse the summarizer
  unchanged** — this work touches only *where the boundary is stored* (seq, not
  turn) and *that the referenced messages are persisted*, never how summaries are
  generated. Older summary rows remain as a harmless audit trail.

### 4. Resume on the cursor

```python
ckpt = await get_latest_compaction_checkpoint(thread_id)     # newest role='summary' row
restored = [SystemMessage(ckpt['summary']),
            *rows_where(thread_id, seq_gt=ckpt['boundary_seq'])]   # ORDER BY seq ASC
restored = _repair_tool_pairing(restored)
```

800-message turn, summary at message 780 → loads `summary + messages 781…800`,
**exactly the agent's live view.** Turn numbers never enter in.

### 5. Migration / back-compat

- `seq BIGSERIAL` backfills monotonically on existing rows (assigned in physical
  ≈ insertion order; good enough for old threads).
- Old summary rows have `boundary_turn` but no `boundary_seq`: resume falls back
  to the existing `since_turn=boundary_turn` path when `boundary_seq` is absent.
  The first new compaction on an old thread upgrades it.
- `thread_messages` stays the source of truth — no data-loss risk either way.
- The orchestrator REST write endpoint (`/api/agents/threads/{id}/messages`)
  stays for back-compat but is off the persistent hot path after step 2; it can
  be retired separately once nothing else uses it.

### 6. `LIMIT` backstop (defense-in-depth only)

✅ **DONE (2026-06-08).** `get_thread_messages_history(newest_first=True)` selects
`seq DESC LIMIT N` (the **newest** N, reversed back to chronological) and every
resume read (Path A both cursors + Path B) passes
`limit=_resume_message_limit, newest_first=True` (`RESUME_MESSAGE_LIMIT` env,
default 1000). It is **not** the mechanism — step 3 is — just a floor so one
pathological *count* tail (thousands of tool calls, no usable boundary) can't OOM
the restore; a healthy boundary_seq tail is ~`keep_recent`, far under it. It's
orthogonal to the oversized-*single*-message elision (the 1.5M-token runaway in
`[[persistent_session_runaway_generation_context_explosion]]`, handled in
`summarize_and_compact`). Taking the **newest** N (not oldest) preserves recent
context, so it doesn't reintroduce the b4478b88 oldest-truncation orphan;
`_repair_tool_pairing` (already on the resume path) cleans any tool batch sliced
at the floor. Logged when it trims.

### Implementation order

Four slices, each independently verifiable on the k3d cluster:

1. **Schema + direct writes** — ✅ **DONE (2026-06-08).** `0023` migration
   (`seq BIGSERIAL` + `idx_thread_messages_thread_seq (thread_id, seq)`); direct
   `save_thread_message` on the agent's `PostgresDB` (`_coerce_row_id` →
   `ON CONFLICT (id) DO UPDATE` → `RETURNING id, seq` + the `threads` activity
   bump); `_ensure_msg_id` stamps Human/Tool/multimodal at creation; the three
   call sites (`_loop_on_turn_start`, `_loop_on_turn_complete`,
   `_record_compaction`) write through `_session.postgres_conn`, guarded on it
   (safe — resume already requires that handle). *Verified: migration applied on
   the dev DB in 107 ms, `seq` backfilled monotonically (92 rows, `1..92`, no
   nulls); 6 new `test_postgres_db_save_message.py` unit tests + the existing
   persistent/compaction suites green (412 tests). Full live-session direct-write
   exercise deferred to slice 2, which has the observable acceptance (crash →
   tail survives).*
2. **Incremental persistence** (step 2b) — ✅ **DONE (2026-06-08).** New optional
   `persist_message` callback; `_execute_turn` calls it after every append (AI
   step, all four `ToolMessage` sites, multimodal follow-up). Transport
   `_loop_persist_message` (bounded `wait_for(5s)` + non-fatal) writes through the
   shared `_persist_one_message`, which `_save_turn_ai_messages` now also uses, so
   the turn-complete pass *reconciles* the same rows (fills in metrics + approval
   decisions via the idempotent upsert; `seq` preserved). *Verified by unit tests:
   `persist_message` fires `ai → tool → ai` in order with stable ids
   (`test_persists_ai_and_tool_messages_as_produced`); the transport impl persists
   via the session pool with `turn_count`, no-ops without a pool, and is non-fatal
   on DB error; no-callback path is a clean back-compat no-op. 419 related tests
   green. Side effects: mid-turn-pruned messages are now persisted before the
   prune (they weren't before), and `threads.last_activity` bumps per message so a
   long turn no longer looks idle. Full live mid-turn-`kill` exercise is the
   formal acceptance — additive + non-regressing vs slice 1, so deferred to run
   alongside slice 3 resume verification (which reads this persisted tail).*
3. **`boundary_seq` + seq-resume** (steps 3–4) — ✅ **DONE (2026-06-08).** No new
   migration (boundary_seq rides in the summary row's `metrics` JSONB).
   `summarize_and_compact` stashes the covered/kept boundary id
   (`ContextManager._last_compaction_boundary_id`); `_record_compaction` resolves
   it via the new `PostgresDB.get_seq_for_message_id` and writes `boundary_seq`;
   `get_thread_messages_history` gained a `seq_gt` filter (ordered by `seq ASC`);
   resume Path A loads `seq > boundary_seq` when present, else the `boundary_turn`
   cursor. *Verified by unit tests: the boundary lands on the exact last
   summarized message (`m4` in an 8-msg/keep-3 compaction) and resets to None on
   a no-op; the seq cursor filters + orders by seq; `_record_compaction` resolves
   id→seq onto the row; Path A picks seq-cursor vs turn-cursor correctly. 12 new
   tests; the worker graph (shares `context.py`) stays green (66). The live
   >500-message-turn resume-fidelity + no-OOM acceptance is deferred to a real
   session run (Phase 2's mid-turn-`kill` exercise feeds straight into it).*
4. **`LIMIT` floor + tests** (step 6) — ✅ **DONE (2026-06-08).** `newest_first`
   floor wired into all resume reads + trim logging; the three pre-existing
   "full-load" contract tests updated to the bounded newest-N contract (the
   b4478b88 orphan stays covered by `_repair_tool_pairing`). New unit tests:
   newest-N selects `seq DESC` and returns chronological, composes with the seq
   cursor, and the floor params + trim warning fire on resume. Full lint + format
   clean across `src/ orchestrator/ tests/`; ~317 persistent/context/postgres
   tests green.

### Status — implementation complete (all four slices)

All four slices shipped + unit-verified on 2026-06-08; migration `0023` verified
on the dev DB. **The one thing still outstanding is the live end-to-end
acceptance** (long real session → mid-turn compaction → kill the pod → resume →
assert `summary + exact tail`, no OOM), which needs a real LLM session and is the
shared capstone for slices 2–4. Run it on the k3d cluster once the agent image
carries these changes.

## Open questions

1. **Trailing partial turn on resume.** Preserve the partially-persisted tail
   (so the agent doesn't redo tool work) vs roll back to the user message and
   re-run cleanly. `_repair_tool_pairing` keeps either well-formed; (a) is better
   for avoiding duplicated workspace work, (b) is simpler.
2. ~~Rolling-merge vs summary chain~~ — **not a real fork.** The existing
   `summarize_and_compact` already rolling-merges (`context.py:1631-1633`); we
   reuse it as-is and resume reads the latest (cumulative) summary row. A
   non-merging "chain" was a speculative way to dodge the
   re-summary-expands-and-skips failure in
   `[[persistent_session_runaway_generation_context_explosion]]`, but that's a
   separate, already-mitigated concern (oversized-AIMessage elision,
   `context.py:1556-1595`) and out of scope here.
3. ~~`seq` source for the boundary~~ — **resolved** by the direct-write
   decision: `INSERT … RETURNING seq` hands the agent every message's `seq`, so
   the boundary is recorded as a literal `boundary_seq` with no read-time
   resolution.

## Acceptance criteria

- Kill the pod mid-turn after several tool calls → resume reflects work up to the
  **last persisted message/step**, not just the last completed turn (Symptom 1).
- A session compacted mid-turn resumes to **`latest_summary + exactly the
  messages after its boundary`**, independent of turn size — verified on a single
  >500-message turn (Symptom 2).
- Resume of a large, no-recent-summary thread does **not** OOM / `/ready`-timeout
  (the `LIMIT` floor holds even with no usable summary).
- Turn-complete reconciliation produces **no duplicate** rows (idempotent
  upsert); restored tail passes `_repair_tool_pairing` with no orphaned calls.
- Old threads carrying only `boundary_turn` still resume (back-compat path).

## Related

- `[[persistent_session_runaway_generation_context_explosion]]` — adjacent
  resume/context work; tracks the open exit-137 mid-resume pod kill, and its
  re-summary-expands-and-is-skipped failure is the argument for the non-merging
  summary chain in step 3.
- `[[headless_persistent_sessions]]` — eager mode makes long, unwatched turns the
  common case, which is what raises both symptoms from "minor" to "high."
- `[[persistent_session_restored_messages_no_ids]]` — message-id minting on
  restore; the same id discipline is the prerequisite for the idempotent
  incremental upserts in step 2 and the `boundary_after_id` cursor in step 3.
