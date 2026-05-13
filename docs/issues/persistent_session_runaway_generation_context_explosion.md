# Persistent session — runaway generation poisons context, session unrecoverable

## Symptom (observed 2026-05-11)

Test session `7d845b7e-5e43-4fb1-8347-3a9c0b96947a` running `gemma-4-moe`
(131,072-token context). User flow:

| Turn | User input | Agent action |
|------|-----------|--------------|
| 1 | "Hey, can you see this image?" + 1 JPG (small white animal) | `read_file` on the JPG → vision-style description ✓ |
| 2 | (no text) + 1 PDF `Vertraulichkeitsvereinbarung.pdf` (~1k words) | `get_document_info` → "file not found"; then `list_files`; then `read_file` returned the PDF text fine |
| 3 | "Can you hear that?" + 1 voice message | session ended before any response |

After turn 2's small PDF read, the agent's *generation* exploded:

```
2026-05-11 10:06:09 - src.core.context - INFO - Context compaction triggered: 12 messages, 1535900 tokens
2026-05-11 10:06:09 - src.core.context - INFO - Starting single-pass summarization (22 tokens)
…
2026-05-11 10:08:09 - src.core.context - ERROR - Structured summarization failed: TimeoutError
2026-05-11 10:08:10 - src.core.context - INFO - Falling back to unstructured summarization
2026-05-11 10:08:10 - src.core.context - INFO - Unstructured fallback succeeded (264 chars)
2026-05-11 10:08:10 - src.core.context - ERROR - Summary (58 tokens) larger than original (27 tokens) — skipping compaction
2026-05-11 10:08:10 - src.llm.reasoning_chat - WARNING - Request approaching context limit: 1,550,833/131,072 tokens (1183.2%)
2026-05-11 10:08:10 - src.llm.reasoning_chat - ERROR - Context overflow at HTTP layer: 1,550,833 tokens exceeds limit of 131,072
… (8 retry attempts, all fail with the same overflow) …
2026-05-11 10:08:14 - src.persistent_graph - INFO - Streaming not supported (APIConnectionError), falling back to ainvoke
… 6× more retries, all fail …
2026-05-11 10:08:17 - src.persistent_graph - ERROR - Error in turn 2
src.llm.exceptions.ContextOverflowError: Request body has 1,550,833 tokens, exceeds model limit of 131,072
```

The agent then cleared its turn but never recovered — turn 3's voice
message arrived into the same poisoned state and got the same overflow.
User had to end the session manually.

## Root cause

The PDF was small (~1k words). The 1.5M tokens are not data — they
are the model itself, repeating tokens in a runaway generation loop,
all accumulated in a single `AIMessage` that subsequently became part
of the next turn's history.

Two co-conspirators:

1. **No output token cap on the wire.** `config/persistent_defaults.yaml`
   sets `max_output_tokens: null`. For Anthropic models, `loader.py`
   has model-aware fallbacks (Opus → 32K, Sonnet → 16K, otherwise
   `min(8192, ctx // 4)`), so a null value still produces a real cap.
   For the OpenAI provider (which is what gemma uses via vLLM), the
   loader was:

   ```python
   if config.max_output_tokens is not None:
       llm_kwargs["max_tokens"] = config.max_output_tokens
   ```

   No fallback. Null → no `max_tokens` passed to the SDK → vLLM's own
   default applies, which for the configured deployment is effectively
   unbounded for a 131K-context model. `_create_groq_llm` and
   `_create_google_llm` ignored `max_output_tokens` entirely (silent
   bug for their callers too); `_create_openrouter_llm` and
   `_create_codex_llm` had the same null-passthrough as OpenAI.

2. **A known model bug (vllm#40080)** — Gemma 4 + xgrammar JSON schema
   can enter infinite repetition loops. `config/model_config_matrix.yaml`
   already documents this and uses `repetition_penalty: 1.05` as a
   "partial mitigation." Without an output cap as the hard backstop,
   the partial mitigation isn't enough — once a loop starts, nothing
   stops it server-side.

Compaction couldn't dig the session out because the recent-message
slicer's input was tiny (only the older small turns were summarized
— "summarized 22 tokens" in the log). The 1.5M-token AIMessage lives
in the recent-keep window, never enters the summarization slice, and
the skip-compaction guard at `src/core/context.py:1482` correctly
notices that adding a summary would *grow* the conversation rather
than shrink it. So nothing changes between retries.

## Impact

- Any runaway generation poisons the session permanently — every
  subsequent turn fails the same way.
- Recovery requires the user to `/done` the session and start over,
  which loses all conversational context.
- The bug is most likely on local-vLLM gemma deployments today, but
  ANY OpenAI-compatible endpoint without server-side `max_tokens`
  enforcement is exposed.

## Fix (2026-05-11)

Layered defense across the providers:

### Primary — symmetric `max_tokens` derivation in `loader.py`

New helper `_resolve_max_output_tokens(config, limits)`:

```python
def _resolve_max_output_tokens(config, limits=None) -> int:
    if config.max_output_tokens is not None:
        return config.max_output_tokens
    ctx = config.model_max_context_tokens or (
        limits.model_max_context_tokens if limits else None
    )
    if ctx:
        return min(16384, ctx // 4)
    return 8192
```

Applied to **all five** non-Anthropic providers (`_create_openai_llm`,
`_create_google_llm`, `_create_groq_llm`, `_create_openrouter_llm`,
`_create_codex_llm`). Anthropic keeps its existing model-aware
ceilings (intentional — Opus deserves 32K, Sonnet 16K).

For gemma's 131K context this resolves to 16,384 — a single response
can never grow larger than that, and the runaway loop is killed
server-side after 16K tokens regardless of repetition penalty efficacy.

### Secondary — oversized-message backstop in compaction (shipped 2026-05-11)

`summarize_and_compact` in `src/core/context.py` now scans the
conversation for any `AIMessage` exceeding half the configured
`model_max_context_tokens` (the threshold matches "this single message
alone could plausibly break the next request"). Matching messages are
substituted with a stub:

> `[Previous response of ~N tokens elided by compaction — likely runaway generation. See workspace logs for details.]`

The substitution survives the existing skip-compaction guard (so a
poisoned session recovers even when the summarizer can't reduce
non-poisonous content further) via a `_substitution_only_result()`
return path. Originals' IDs feed `RemoveMessage` markers as before.

`AIMessage`s carrying `tool_calls` are deliberately exempted —
substituting one would orphan paired `ToolMessage`s and break the
turn. `ToolMessage` content is not in scope for this rule (it's
governed by `truncate_long_tool_results`, which fires earlier in the
pipeline and middle-truncates over-long tool results).

Combined with the **restored-messages-need-IDs** fix shipped under
`persistent_session_restored_messages_no_ids.md`, sessions resumed
from a state poisoned BEFORE this whole stack was in place now
self-heal on first compaction.

### Out of scope (deliberately, this round)

- **Streaming-side runaway detector** — defense-in-depth for endpoints
  that ignore `max_tokens`. vLLM honors it correctly so the primary
  fix is sufficient for our current deployment. Worth adding if a
  future endpoint is found to ignore the cap.
- **Insertion-time tool result cap** — was the original (incorrect)
  diagnosis. PDFReader already caps at 25K words and shell at 30K
  chars; the bug wasn't tool-result size.

## Verification

- The same test scenario (small PDF, gemma model) should generate
  ≤16,384 tokens per response, never trigger the compaction loop, and
  remain responsive across many turns.
- Logged at LLM creation time as `max_tokens=16384` in the loader's
  startup line.

## Related code

- `src/core/loader.py` — new `_resolve_max_output_tokens` helper +
  five wire sites (OpenAI, Google, Groq, OpenRouter, Codex)
- `src/core/loader.py:2034` — Anthropic's existing equivalent
  (untouched)
- `config/persistent_defaults.yaml:23` — `max_output_tokens: null`
  (still the default; the loader now derives a real value rather than
  passing it through)
- `config/model_config_matrix.yaml:241-243` — vllm#40080 documentation
  and `repetition_penalty` mitigation
- `src/core/context.py:1403-1521` — compaction slicer (the
  recent-message exemption that masks the symptom)

## Decision

**Fixed 2026-05-11.** Primary cause (missing `max_tokens` fallback for
non-Anthropic providers) and secondary backstop (oversized-message
compaction rule) both shipped. Streaming-side runaway detector
deferred — vLLM honors `max_tokens` correctly and the loader-side fix
is sufficient for our current deployment; revisit if an endpoint is
ever found that ignores the cap.
