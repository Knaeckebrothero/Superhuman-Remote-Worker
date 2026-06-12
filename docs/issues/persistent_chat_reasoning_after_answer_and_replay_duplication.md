---
tags:
  - issue
  - cockpit
  - persistent-sessions
  - reasoning
  - sse-replay
related:
  - "[[persistent_chat_lost_assistant_turn_on_mid_turn_reload]]"
  - "[[gemma_session_findings]]"
  - "[[context_summarization_rework]]"
---

# Reasoning renders after the answer (gemma-style models) and duplicates on replay

**Filed:** 2026-06-12, from user observation on k3d thread `e9699503`
(gemma-4-moe-strix). Diagnosed same day with full DB receipts; sibling of
the duplicate-compaction-banner bug fixed earlier that day (same
journal-vs-history double-accounting seam, different mechanism — **no
marker leak, no duplicate persistence**). Cosmetic only, no data loss,
but it erodes trust in the transcript.

## Two distinct symptoms

1. **Ordering (live view, long-standing):** for models that deliver
   reasoning via OpenAI-style `reasoning_content` (gemma via vLLM,
   DeepSeek, OpenRouter), the thinking bubble for each LLM call renders
   *after* that call's answer text. After a reload it jumps to *before*
   the text. This is the "why does gemma think after answering" oddity.
2. **Duplication (reload/reconnect):** sometimes the *same* reasoning
   renders twice — once before the answer and once as a trailing
   thought-only bubble after it. Several turns in thread `e9699503`
   showed this.

## Data layer is clean (receipts)

Checked thread `e9699503` on k3d (2026-06-12):

- All 24 `thinking` journal frames have unique content hashes and map
  one-to-one onto `thread_messages.thinking` values. No duplicate rows,
  no duplicate frames.
- Turn 10 (the screenshot turn) = ai rows seq 481/483/485, journaled
  thinking at event-seqs 1774/1778/2731.
- Journal order for the answer call: **949 `token` frames (seq
  1779–2729), then the single `thinking` frame at 2731, then
  `turn.completed` at 2732.** The reasoning frame trails the entire
  token run — this gap is what makes the duplication possible.

## Mechanism 1 — post-stream reasoning emission (agent side)

- The streaming loop in `src/persistent_graph.py:922-948` only surfaces
  Anthropic/Responses-style thinking **blocks** live. Gemma's reasoning
  arrives as `delta.reasoning_content`, which the loop never looks at;
  it's captured by the HTTP tap (`src/llm/reasoning_chat.py`) and lands
  in `additional_kwargs.reasoning_content` on the merged response.
- `src/persistent_graph.py:1227-1233` then broadcasts it **once, after
  the stream completes**. The comment says "if not already streamed"
  but the code is unconditional — for gemma-style models this is the
  only emission, so the live event order is: answer tokens first,
  reasoning last.
- On reload, `historyToTurns` renders the row canonically — thinking
  before text (`cockpit/.../persistent-chat.service.ts:2264`) — which
  is why the bubble "moves."

## Mechanism 2 — history + replay double-render (cockpit side)

- On a cold connect, history paints the completed turn (thinking before
  text). Then SSE reopens with the replay cursor from IndexedDB, which
  is saved asynchronously per-event and can lag a few events behind
  what was already rendered.
- Because the thinking frame is journaled *after* the whole token run,
  a cursor that lands anywhere in that gap replays **just the thinking
  frame** — no tokens, which is why the answer text never duplicates.
- The turn is no longer active at that point, so the reducer doesn't
  drop the frame: `ensurePlaceholderTurn`
  (`cockpit/.../turn-reducer.ts:420`, the mid-turn-reload recovery
  feature, Approach 2 of
  [[persistent_chat_lost_assistant_turn_on_mid_turn_reload]])
  materializes it as a `recovered:` thought-only bubble at the bottom.
  Result: same reasoning rendered twice, once from the row, once from
  the replayed frame.
- Root gap: **`thinking`/`token` deltas are the only reducer events
  with no idempotency key.** Tool events and turn lifecycle are keyed
  by stable ids; the compaction banner got its stable
  `compaction-<turn>` id on 2026-06-12 for exactly this seam.
- Stale comment: `persistent-chat.service.ts` `connect()` (~line 502)
  still claims the reducer "no-ops `token`/`thinking`/`tool.*` events
  when `activeAssistantTurnId === null`" — outdated since
  `ensurePlaceholderTurn` landed; orphan frames are absorbed, not
  dropped.

## Proposed fix

1. **Stable id + dedupe for thinking frames (the core fix).** Extend
   `on_thinking` to carry the AI message id (it's known at the
   broadcast site — same id the row gets via `_ensure_msg_id`, so
   journal frames and history rows share a key). Broadcast
   `{content, message_id}`; have `historyToTurns` stamp thought events
   with `think-<msgid>` and the reducer skip a `thinking` action whose
   id already exists in any turn. Replayed frames then converge instead
   of duplicating, including across the history/replay seam.
2. **Stream reasoning live (fixes ordering).** Surface
   `reasoning_content` deltas during the stream — either check
   `chunk.additional_kwargs` in the streaming loop or add a streaming
   callback to the HTTP tap — so the bubble lands *before* the answer
   in true chronology. Demote the post-stream send at
   `persistent_graph.py:1227-1233` to a guarded fallback (track whether
   reasoning was already streamed, making the existing comment true).
   Bonus: users see gemma "thinking" in real time instead of a
   reasoning dump after the answer.
3. **Fix the stale `connect()` comment** while in there.

(1) alone kills the duplication; (2) alone fixes ordering but not the
replay dup. Ship (1) first if splitting.

## Verification

- Vitest: reducer spec — replaying a `thinking` action with an id
  already present in a historical turn is a no-op; `historyToTurns` +
  replayed frame for the same message id yields one thought event.
- Live (gemma session, multi-call turn): reload immediately after a
  turn completes (cursor likely inside the token→thinking gap) →
  exactly one bubble per LLM call, positioned before its text; no
  trailing `recovered:` bubble. With (2): thinking bubble appears
  before the streamed answer live.
