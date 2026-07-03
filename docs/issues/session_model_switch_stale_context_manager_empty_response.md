# Session model switch keeps the old model's ContextManager → no compaction + repeated empty responses

**Status:** investigated 2026-07-03 (homelab thread `c43d7f8b`) · **Severity:** high
(a downswitch to a smaller-window model bricks the session until manual model change)
**Related:** `docs/issues/minimax_m3_think_tag_reasoning_leak_post_gateway.md` (same
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

## Fix proposal

- **P1 (the fix):** in `_handle_config_update`, after
  `_session.config = new_config`, call `_session._setup_context_manager()`;
  and refresh the loop's `context_manager`/`config` per turn the same way
  tools are refreshed (extend the `get_current_tools` getter or read from the
  session object each iteration). First turn after a downswitch then compacts
  125.7k → under 102.4k and the session recovers on its own.
- **P2 (diagnosability):** detect the reasoning-only/incomplete Responses case
  and surface a specific message ("model ran out of context/output while
  reasoning — compacting…") instead of the generic empty-response text;
  ideally treat it as a compaction trigger (`force=True`) rather than a
  user-facing dead end.
- **P3 (belt & braces):** on model switch, immediately run
  `ensure_within_limits(force=...)` when the new window is smaller than the
  current history estimate.

## Verification sketch

Unit: build session config at gpt-5.5, PATCH llm.model → codex-spark, assert
`context_manager.config.summarization_threshold_tokens == 102_400`.
k3d e2e: seed a session past 105k tokens on gpt-5.5, switch to codex-spark,
send a trivial message → expect a summarization marker + real answer, no
empty-response placeholder.
