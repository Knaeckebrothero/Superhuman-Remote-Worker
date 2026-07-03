# MiniMax-M3 reasoning arrives as mangled `<think>` content after gateway removal

**Status:** investigated 2026-07-03 (homelab evidence) · **Severity:** high (every MiniMax
turn renders broken; titles corrupted; live sessions show duplicated text)
**Related:** `docs/issues/remove_litellm_proxy_and_gateway_concept.md`,
`docs/issues/session_model_switch_stale_context_manager_empty_response.md`

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

## Fix proposal (layered)

1. **Wire-layer normalization** (primary): add a `strip_think_tags(text) ->
   (clean_text, reasoning)` helper in `src/llm/` and apply it in
   `ReasoningChatOpenAI._post_process_result` for the non-streaming path:
   move think-block text into `additional_kwargs["reasoning_content"]`
   (append to any structured capture, dedupe if identical), drop orphan
   `</think>` remnants. This also cleans what gets replayed in history and
   what the archiver persists.
2. **Streaming path**: content deltas can split tags across chunks. Options:
   (a) small state machine filtering the content token stream when the model
   family is flagged think-tag-emitting; (b) minimum viable: post-merge
   sanitation of the final message before persistence + suppress content
   deltas while the accumulated content is inside an unclosed `<think>`.
   Since MiniMax double-delivers (structured deltas + tagged content), simply
   *dropping* think-tagged content spans loses nothing — the sink already
   streams the reasoning live.
3. **Aux hygiene**: use the same helper in `_generate_title` (and any other
   aux consumers — memory extraction, observer prompts) before using aux
   output; for titles, prefer text after the last `</think>`, and truncate on
   word boundary.
4. **Config gate**: a `content_think_tags: true` flag on the `minimax` family
   in `config/model_config_matrix.yaml` to scope the parser (harmless if
   applied globally — the tags have no legitimate use in content).
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
