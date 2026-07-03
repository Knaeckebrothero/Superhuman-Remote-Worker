# MiniMax-M3 reasoning arrives as mangled `<think>` content after gateway removal

**Status:** ✅ DONE — fix committed `33232103` (develop, 2026-07-03). Family
`settings.extra_body` passthrough (loader + matrix `reasoning_split: true` for
`minimax`/`minimax-m3`), verified by unit tests, a live wire test through
`create_llm` (clean content + captured reasoning), and a fresh k3d MiniMax
session (user-confirmed clean title). Contaminated titles repaired on k3d +
homelab. Investigated 2026-07-03 (homelab evidence) · confirmed provider-side
by raw wire probe + OpenRouter A/B (see "Wire probe verdict" below).

**Deferred follow-ups (separate, not blocking the fix):**
- Per-model `params_json.extra_body` escape hatch (layer 2) — the family-level
  fix here covers minimax; a per-model override in the Admin catalog would let
  a user attach provider params to any model whose family detection misses.
- `reasoning_details` history replay for interleaved-thinking quality in the
  loops — MiniMax's docs want the reasoning replayed within tool-call turns;
  we currently drop it (fine for aux/titles, possibly a quality cost for long
  loop tool chains — measure before building).
**Related:** `docs/issues/remove_litellm_proxy_and_gateway_concept.md`,
`docs/done/session_model_switch_stale_context_manager_empty_response.md`

## Symptoms (three surfaces, one cause)

1. **Worker-job chat view**: assistant messages render literal think tags with a
   doubled closer:

   ```
   <think>
   Let me start by reading the task brief and instructions carefully tounderstand what's expected.
   </think>

   </think>
   ```

   Seen on loop jobs `e1ede3c0` (iter 9) and `6dc25a0d` (iter 15).

2. **Live sessions (streaming)**: the same reasoning text appears TWICE — once as
   "Thought for a moment" bubbles, once as plain assistant text, interleaved
   fragment by fragment (thread `36fd62d9`, rare-earth prices session).

3. **Session titles**: threads auto-titled with raw truncated reasoning, e.g.
   `<think>\nThe user wants me to generate a short title (5-8 words) for this
   conversation. Let me analyz` (threads `c43d7f8b`, `36fd62d9`). MiniMax-M3 is
   the auxiliary model, and title generation uses the aux response verbatim.

## Wire evidence (audit store `llm_requests`)

- **doc 13334** (job e1ede3c0, iter 0, non-streaming worker `ainvoke`) and
  **doc 15330** (job 6dc25a0d, iter 557): the *persisted response content* is
  exactly the `<think>…</think>\n\n</think>` string above. The worker path does
  no client-side content assembly → the string arrives verbatim from MiniMax's
  API (`System: Minimax` direct endpoint).
- **Missing spaces at segment boundaries**: "carefully **tounderstand**",
  "Let me **doa** web search", "actual **pricedata**". In the session case these
  boundaries match *exactly* the live thought-bubble fragment boundaries
  ("…Let me do" | "a web search…"). MiniMax stitches its interleaved reasoning
  segments back into `content`, losing the joins and appending a stray
  `</think>` — and *also* streams the same segments as structured reasoning
  deltas (`delta.reasoning_content`-family), which is why our SSE tap
  (`src/llm/reasoning_chat.py:390` + sink at `src/persistent_graph.py:1156`)
  emits thinking frames while the content deltas re-deliver the same text →
  the interleaved duplication in the cockpit.
- **Intermittent**: doc 15200 (iter 427) has clean content, docs 14637/14638
  have no reasoning at all (19–53 completion tokens, pure tool calls). M3 only
  emits thinking when it "decides" to, so many turns look fine.

## Wire probe verdict (2026-07-03): it's MiniMax's API format, not our endpoint handling

Raw `httpx` POST from the orchestrator pod straight to `https://api.minimax.io/v1/chat/completions`
(key decrypted from `llm_endpoints`, no SRW LLM code in the path), title-style prompt:

- **Non-streaming**: `message` keys are `['audio_content', 'content', 'name', 'role']` —
  **no `reasoning_content`, no `reasoning`, nothing structured**. The reasoning arrives
  only as `<think>…` inside `content` (1815 chars), even though
  `usage.completion_tokens_details.reasoning_tokens: 165` is counted separately.
  This is the path title generation uses (`ainvoke`) → there is literally nothing to
  capture; the leak is unavoidable without client-side tag parsing.
- **Streaming**: delta keys are `['content', 'reasoning', 'role']` and **every reasoning
  fragment is delivered twice** — identical text in `delta.reasoning` AND in
  `delta.content` (content additionally carrying the `<think>` wrapper). Our SSE tap
  correctly captures `delta.reasoning` (→ thinking frames); the duplicated content
  deltas are what the token stream renders. This is the interleaved duplication seen
  in the cockpit, confirmed on the wire.
- **OpenRouter A/B (user test, k3d)**: same model via OpenRouter
  (`openrouter/minimax/minimax-m3`) → clean title "Rare Earth Metal Price Inquiry"
  (thread `e0ae3b75`); via MiniMax direct → leaky `<think>` title (thread `6a7b2a6c`).
  OpenRouter normalizes: strips tags from content, populates `reasoning`.

## THE FIX (web-verified + probe-verified 2026-07-03): send `reasoning_split: true`

MiniMax's official OpenAI-compatible API docs
(https://platform.minimax.io/docs/api-reference/text-openai-api) document a
**`reasoning_split`** request parameter (applies to M3/M2.7/M2.5/M2.1/M2):

- **omitted/false (default)**: thinking arrives inside `content` wrapped in
  `<think>` tags — the leak is their *documented default*, deliberate so that
  clients replaying raw content preserve interleaved thinking across tool turns.
- **true**: thinking is separated into `reasoning_content` + `reasoning_details`;
  content stays clean. `reasoning_split` only controls formatting, not whether
  the model thinks (`thinking: {"type": "adaptive"|"disabled"}` controls that).

Probe from the orchestrator pod with `"reasoning_split": true` confirms:

- non-streaming: message keys gain `reasoning_content` + `reasoning_details`,
  `content` = pure answer;
- streaming: delta keys `['content', 'reasoning_content', 'reasoning_details',
  'role']` — reasoning only in structured fields, **no duplication**;
- tool-call turn: clean content + `tool_calls`, reasoning separate — and the
  **doubled `</think>` disappears** (without split it reproduces
  deterministically on tool-call turns: `'<think>\n…\n</think>\n\n</think>'`).

Both fields are already parsed by `_extract_reasoning_from_response` /
`_extract_reasoning_from_delta` in `src/llm/reasoning_chat.py`, so the fix is:
**inject `reasoning_split: true` (extra_body) for minimax-family models** —
e.g. via `config/model_config_matrix.yaml` family params or the model catalog
`params_json`.

Ecosystem context: the default format bites other agents too — qwen-code
(QwenLM/qwen-code#3387: "OpenAI-compatible MiniMax responses leak `<think>`
blocks into visible output"), opencode (anomalyco/opencode#3555), and
MiniMax-AI/MiniMax-M2#105 ("SSE streaming — thinking tags mixed in content",
open, no maintainer response). Genuinely MiniMax-side bugs regardless of mode:
the doubled `</think>` in no-split tool turns, the no-split streaming
double-delivery (`delta.reasoning` + tagged `delta.content`), and eaten spaces
at segment boundaries ("Ishould", "tounderstand") which persist even in split
reasoning text (cosmetic there).

**Implementation caveat — history replay:** MiniMax docs require the complete
assistant response (including thinking) to be replayed within a tool-call turn
for interleaved-thinking performance: with split on, that means passing
`reasoning_details`/`reasoning_content` back on assistant history messages (or
accepting the quality hit, as OpenRouter-routed usage does today unless clients
replay `reasoning_details`). Decide per surface: loops/worker (quality matters,
long tool chains) vs aux/title (irrelevant — one-shot).

Routing MiniMax through OpenRouter also works (normalizes to `reasoning`
field; verified by A/B), but the loops run on the MiniMax **Token Plan**
(coding-plan pricing, direct API only), so the parameter fix is the real one.

## Root cause (our side)

The LiteLLM gateway used to normalize MiniMax's think-tag format into
`reasoning_content`. After gateway removal, MiniMax is called direct — and the
codebase has **no `<think>`-tag handling anywhere**:

- `src/llm/reasoning_chat.py` captures only structured fields:
  `reasoning_content` (DeepSeek), `reasoning`/`reasoning_details` (OpenRouter),
  Responses-API reasoning blocks, Anthropic `thinking` blocks.
- `rg "think>" src/ orchestrator/ config/` → zero hits. Nothing strips tags
  from `content` on capture, persistence, replay, or render.
- The cockpit renders `content` verbatim and thinking frames separately →
  tags leak and text duplicates.
- `_generate_title` (`src/api/persistent_app.py:5045`) does
  `response.content.strip()[:100]` — verbatim aux output, blind mid-word
  truncation → reasoning becomes the title.

## Fix proposal (revised after `reasoning_split` discovery)

1. **Primary: `reasoning_split: true` for minimax-family requests** (see "THE
   FIX" above) — one extra_body param; existing capture layer handles the
   split fields. Wire it through the family params in
   `config/model_config_matrix.yaml` / catalog `params_json`, main + auxiliary
   paths.
2. **Defense-in-depth: `strip_think_tags(text) -> (clean_text, reasoning)`**
   helper in `src/llm/`, applied in `ReasoningChatOpenAI._post_process_result`
   — covers any other provider/self-hosted model that emits tags in content
   (vLLM without a reasoning parser, future models), and MiniMax regressions.
3. **Aux hygiene**: apply the helper in `_generate_title` (and other aux
   consumers) before using aux output; truncate titles on word boundary.
4. **History replay decision** for interleaved thinking (see caveat above).
5. **Repair pass**: `UPDATE threads SET title = 'Untitled Session' WHERE title
   LIKE '<think>%'` (or regenerate) for the contaminated rows.

## Notes

- MiniMax's docs recommend replaying think blocks for interleaved-thinking
  models; we already effectively don't (and the loop ran 573 iterations fine),
  so normalizing into `reasoning_content` (not replayed) keeps MiniMax
  consistent with every other reasoning model we run.
- Sessions do not appear to write `llm_requests` audit rows on the current
  homelab deploy (audit tip during investigation was all loop-job docs; ids
  past ~15349 404) — wire-level session forensics required inference from
  persisted `thread_messages`. Worth confirming/fixing separately.
