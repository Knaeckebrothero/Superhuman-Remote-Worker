# Session model switch keeps the old model's ContextManager → no compaction + repeated empty responses

**Status:** ✅ DONE (root cause) 2026-07-03 (develop) — the P1 root-cause fix in
"Fix (implemented)" below resolves the reported bricking bug: a session model
downswitch now re-derives compaction thresholds in place and compacts on the
next turn instead of dead-ending in empty responses. Unit-tested replaying the
exact repro; not yet live-replayed (needs a real >100k-token session — verify
opportunistically on the next long gpt-5.5 session that switches to codex-spark).
Investigated 2026-07-03 (homelab thread `c43d7f8b`) · **Severity:** high
(a downswitch to a smaller-window model bricks the session until manual model change)

**Deferred follow-up (P2, separate from the root-cause fix):** even with correct
compaction, a Responses-API model that returns reasoning-only / `status:
incomplete` should surface a specific message ("model ran out of budget while
reasoning — compacting…") and force a compaction, instead of the generic
"empty response" text. There is still no explicit `status == "incomplete"`
handling in `src/llm/`. Lower severity now that the trigger no longer fires
spuriously; build when the empty-response UX becomes a real annoyance.
**Related:** `docs/done/minimax_m3_think_tag_reasoning_leak_post_gateway.md` (same
session's title bug), `docs/issues/langchain_responses_api_streaming.md`

## Repro (observed)

Thread `c43d7f8b-69c1-4bd6-afcf-c5ddbb8827e8`, 2026-07-03: session started on
`gpt-5.5`, ran ~4 turns to ~125.7k input tokens, user switched to
`gpt-5.3-codex-spark`. From then on **every** turn — including "Don't think
long, just answer with hello" — returned:

> ⚠ The model returned an empty response. Please try again or switch models.

First failure 16:14:36 (end of turn 3, after a read_file burst), then turns 4
and 5 failed instantly. Automatic summarization never ran.

## Root cause chain

1. **Model switch rebuilds the LLM but not the compaction manager.**
   `_handle_config_update` (`src/api/persistent_app.py:4284-4294`) does
   `create_llm(new_config.llm, new_config.limits)` (the HTTP-layer guard
   correctly gets codex-spark's 128k) and `_bind_tools()`, but never calls
   `_setup_context_manager()` — which is invoked exactly once, at session init
   (`src/api/persistent_session.py:280`).
2. **The turn loop holds stale references.** The loop captures
   `context_manager` + `config` at task creation
   (`src/api/persistent_app.py:352-353`); per-turn refresh exists only for
   llm/tools via `get_current_tools` (`src/persistent_graph.py:611-618`).
3. **Thresholds derive from the session-start model.**
   `src/core/loader.py:782-790`: `context_threshold_tokens = base × 0.80`,
   `message_count_min_tokens = base × 0.40`. gpt-5 family base = 1,050,000
   (`config/model_config_matrix.yaml:301`) → summarization threshold
   **840,000**. codex-spark family = 128,000 (`matrix:349`) → the correct
   threshold would be **102,400**.
4. **Compaction therefore never triggers.** `should_summarize`
   (`src/core/context.py:740-768`) counts honestly —
   `max(local tiktoken, last provider input_tokens)` = ~125.7k — but compares
   against the stale 840k → False → `ensure_within_limits`
   (`src/persistent_graph.py:1039-1048`) is a no-op every turn.
5. **codex-spark gets ~125.7k/128k every turn.** With ~2k headroom the
   Responses-API model spends it on reasoning and returns no message item.
   `is_degenerate_response` (`src/llm/response_guards.py:29-49`) ignores
   reasoning blocks → the reasoning-model branch retries once via `ainvoke`
   (`src/persistent_graph.py:1608-1684`), also empty → generic `_empty_msg`
   (`persistent_graph.py:1584-1587`). The `finish_reason == "length"` branch
   (`:1592-1607`) doesn't fire — the failure is a near-context-ceiling
   incomplete, not an output-cap hit.
6. **Layer-0 preflight passes.** `count_request_tokens`
   (`src/llm/reasoning_chat.py:566-601`) uses the cl100k fallback tokenizer
   (gpt-5 ids unknown to tiktoken) and scores the request just under 128k.
7. **No `status == "incomplete"` handling** exists anywhere in `src/llm/` for
   the Responses API — reasoning-only payloads silently become empty
   AIMessages.

## Fix (implemented 2026-07-03)

Implemented as an **in-place update** instead of a manager rebuild — the
running loop holds a reference to the manager, and the provider-usage anchor
(`_state.last_provider_input_tokens`) must survive the swap so the very next
turn sees the real context size:

- `ContextManager.update_limits(config, model)` (`src/core/context.py`) —
  swaps `config` + rebinds the token counter in place; all accumulated state
  (provider anchor, compaction bookkeeping, progress callback) survives.
- `PersistentSession._build_context_config()` (extracted) +
  `refresh_context_limits()` (`src/api/persistent_session.py`) — re-derives
  thresholds from the CURRENT `config.limits`.
- `_handle_config_update` calls `_session.refresh_context_limits()` after
  `_session.config = new_config` (both branches).
- The turn loop gained a `get_current_context` getter next to
  `get_current_tools` (`src/persistent_graph.py` + wiring in
  `persistent_app.py`) — re-reads `context_manager`, `config`, and
  `auxiliary_llm` from the session after each input wait. This also fixes two
  sibling stalenesses: the **CTX gauge** kept dividing by the old model's
  window (`config.limits` read per turn at `persistent_graph.py:~1540` — the
  suspiciously low "15%" in the repro screenshot), and the **aux hot-swap**
  (`fe60f945`) replaced `_session.auxiliary_llm` while the loop kept using the
  captured original for memory extraction/summarization.

Tests: `TestUpdateLimits` (`tests/test_context_methods.py`) and
`TestRefreshContextLimits` (`tests/test_persistent_session.py`) — the latter
replays the exact repro numbers (840k threshold, 125.7k anchor, switch →
102.4k, `should_summarize` flips False→True on the same manager object).

Note: an explicit "force compaction immediately on switch" step turned out
unnecessary — because the provider-usage anchor survives the in-place update,
the very next turn's `ensure_within_limits` already sees the oversized history
against the new threshold and compacts. The switch itself doesn't send a
request, so there's nothing to protect between the switch and the next turn.

## Verification

Unit (done): `TestRefreshContextLimits` builds the session at gpt-5.5, calls
`refresh_context_limits()` after swapping to codex-spark's limits, and asserts
the threshold moved 840k→102.4k on the *same* manager object and
`should_summarize` flips False→True with the 125.7k anchor intact.

Live (deferred — needs a real >100k-token session): seed a session past ~105k
tokens on gpt-5.5, switch to codex-spark, send a trivial message → expect a
compaction marker + real answer, no empty-response placeholder. Natural to
verify opportunistically on the next long session rather than by burning 100k
tokens synthetically.
