# Compaction keep-window retains oversized tool results — post-compact context stays near the ceiling

**Status:** OPEN — design agreed 2026-07-10, ready for implementation.
**Motivating incident:** session `4b82e6db` lineage (see
`docs/issues/web_search_full_page_content_bloats_session_context.md` and
`docs/done/structured_output_method_mismatch_breaks_compaction_and_aux_tasks.md`).
After a *successful* manual `/compact`, input still sat at **~82% of the
context window** — the summarizer did its job, but the kept tail of 10
messages contained several ~86k-token web_search results that compaction is
not allowed to touch.

## TL;DR

`summarize_and_compact` keeps the last `keep_recent_messages: 10` messages
**verbatim and unbounded**, and **excludes them from the summary**. When those
ten messages contain large tool results (web_search full-page dumps, PDF text
extraction, big file reads, `get_page_text`, …), compaction achieves almost
nothing: post-compact context = tiny summary + a few hundred k tokens of
protected tail. Auto-compaction then re-triggers a turn or two later
(thrash: aux-LLM latency + prompt-cache invalidation each time), and a second
manual `/compact` is a **complete no-op** because of the
`len(conversation) <= keep_recent` early return.

Fix (two parts, both in `src/core/context.py`):

1. **Per-message size cap on the kept tail** — head-truncate any kept
   `ToolMessage` above a configurable char limit at compaction time,
   preserving `tool_call_id` pairing. Always-on, not gated on being
   over the model window.
2. **Include the tail in the summarizer input** — pass the full
   conversation (not just `conversation[:safe_start]`) to
   `summarize_conversation`, so anything important in a truncated result
   is still captured in the summary. The existing observation-masking
   gradient makes this cheap (~300 chars per tool result).

Result: post-compact context becomes *bounded and predictable* —
summary + ≤10 messages of ≤cap each (worst case ~40k tokens with the
default cap) — instead of unbounded because the keep window is
message-**count**-bounded, not token-bounded.

## Invocation map (verified — where compaction actually gets called)

`ensure_within_limits` (context.py:1044 — note the name; there is no
`ensure_context_within_limits`) is the wrapper that adds keep-window elision
and the progressive/force loop around `summarize_and_compact`. Callers:

- **Worker graph** `src/graph.py`: :1075 (per-turn), :1477, :2182,
  :3004 (phase-boundary `compact_on_archive`, force on strategic→tactical),
  :3635.
- **Persistent graph (auto compaction)** `src/persistent_graph.py:1053`
  (note: `src/persistent_graph.py`, NOT under `src/api/`).
- **Persistent resume bounding** `src/api/persistent_app.py:3892` and
  `:3961`.
- **Manual `/compact`** — `_handle_compact`
  (`src/api/persistent_app.py:4232`) calls **`summarize_and_compact`
  DIRECTLY** (:4252, `trigger="manual"`), **bypassing
  `ensure_within_limits` entirely**. Consequence today: keep-window elision
  and the progressive loop NEVER run on manual `/compact` — the exact path
  in the motivating incident. This is why the cap must live inside
  `summarize_and_compact` itself, not in the wrapper.

## Current behavior (code anchors, `src/core/context.py`)

- **Split point** (`summarize_and_compact`, :1749–1750):
  `messages_to_summarize = conversation[:safe_start]` /
  `recent_messages = conversation[safe_start:]`. The tail is **not** fed to
  the summarizer — the design assumed verbatim presence made that redundant.
- **Tail re-add** (:1845–1868): `fresh_recent` copies are appended after the
  summary. Note the existing wart: the `ToolMessage` re-add drops `name`
  (only `content` + `tool_call_id` are copied).
- **Keep-window elision exists but fires too late** (:1094–1098):
  `_elide_largest_tool_results` (:1206) replaces the largest kept tool
  results with `[tool result elided by compaction: ~N tokens …]` markers —
  but only when the post-compact total exceeds **100% of
  `model_max_context_tokens`**. At 82% it never fires.
- **Early return** (:1739–1742): if `len(conversation) <= effective_keep_recent`,
  `summarize_and_compact` returns the messages **unchanged**. This is why a
  second `/compact` on an already-compacted-but-tail-heavy session does
  nothing.
- **Summary-not-smaller guard** (:1790–1812): skips compaction if
  `summary_tokens > original_tokens`, where `original_tokens` counts only
  `old_summaries + messages_to_summarize` — it knows nothing about bytes
  saved by tail truncation.
- **Existing truncation helpers don't apply**: `truncate_long_tool_results`
  (:888, 5000-char cap) only touches results *older* than
  `keep_recent_tool_results` — which is **150** in both
  `config/defaults.yaml:236` and `config/persistent_defaults.yaml:141`,
  i.e. effectively never in a session.
- **Summarizer input masking** (`_format_messages_for_summary`, :1340–1465):
  User → 500 chars, AI → 800 chars, last-10 tool results → 300 chars,
  older tool results → `[Tool 'x' result omitted (N chars)]` placeholder,
  with atomic sibling grouping and a "RECENT CONTEXT" priority marker.

## Fix design

### Part 1 — kept-tail cap (head-truncation)

New `ContextConfig` field (dataclass at :402-ish, alongside
`max_tool_result_length`):

```python
keep_window_max_tool_result_chars: int = 16000   # ~4k tokens at ~3.9 chars/tok
```

At compaction time, when building `fresh_recent` (:1845), any `ToolMessage`
whose string content exceeds the cap is replaced by:

```
<first keep_window_max_tool_result_chars chars>

[…truncated by compaction: kept 16,000 of 344,770 chars (~86k tokens).
Full content was saved to the workspace / is re-fetchable — re-run the
tool or read the saved file if the rest is needed.]
```

Rules:

- **Head-truncation, not full elision.** The head of a web_search result is
  the query echo + result titles/snippets — that's the "where was I"
  continuity signal the tail exists to provide. Full elision
  (`_elide_largest_tool_results` style) stays reserved for the over-window
  emergency tier.
- **Preserve pairing metadata**: `tool_call_id` always; also carry over
  `name` (and fix the existing `fresh_recent` re-add to keep `name` while
  you're there — strict endpoints don't need it, but the triage/rendering
  paths use it).
- **Idempotent**: skip content already carrying the truncation marker or an
  `[tool result elided` marker (same pattern as :1245).
- **Always-on**: apply on every compaction (auto / manual / resume trigger),
  regardless of total token count. Also apply in the **early-return path**
  (:1739–1742): when `len(conversation) <= effective_keep_recent`, don't
  return `messages` unchanged — run the cap over the conversation's
  ToolMessages and, if anything was truncated, return proper
  `RemoveMessage` markers + capped fresh copies (mirror
  `_substitution_only_result`, :1731). This makes a repeated `/compact`
  actually heal a tail-heavy session.
- **ToolMessages only.** Multimodal image `HumanMessage`s are list-content
  (char truncation can't touch them) and are handled by the existing
  `_shed_image_messages` tier at over-window; PDF/image bloat is a separate
  issue. Oversized plain-text AIMessages are handled by the runaway
  backstop at :1687.
- Evidence-preservation patterns (`preserve_tool_names` /
  `preserve_content_patterns`) are **deliberately not consulted**: this is
  head-truncation, not clearing — short evidence (errors, confirmations)
  is under the cap anyway, and anything over 16k chars keeps its head.

### Part 2 — tail included in the summary

Change the `summarize_conversation` call (:1763) to pass the **full**
`conversation` instead of `messages_to_summarize`:

- The masking gradient in `_format_messages_for_summary` handles cost
  naturally: with the tail appended, the tail's tool results occupy the
  "last 10 tool results → 300 chars" window and older results degrade to
  placeholders. Incremental fold input: a few k tokens.
- **Do not move the boundary.** `safe_start` still defines what is removed
  vs. kept, and `_last_compaction_boundary_id` (:1889–1893) must keep
  pointing at `original_conversation[safe_start - 1]` — only the summarizer
  *input* widens, not the removal set. Resume semantics
  (`seq > boundary_seq`) are unchanged.
- The summary will now mention in-flight material that is also present
  (truncated) in the tail. That redundancy is intentional — it is what
  makes tail truncation safe.

### Part 3 — guard math

Update the summary-not-smaller guard (:1790): `original_tokens` should be
`old_summaries + messages_to_summarize` **plus the tokens saved by tail
truncation** (tail-before minus tail-after). Otherwise a compaction that
saves 300k in the tail but produces a summary slightly larger than the tiny
summarized slice gets "skipped" — exactly wrong.

### Config plumbing (full chain, verified)

The key must be threaded through FOUR layers, mirroring
`keep_recent_messages` at each:

1. **YAML**: `config/defaults.yaml:237` + `config/persistent_defaults.yaml:142`
   — add `keep_window_max_tool_result_chars: 16000` in the
   `context_management:` block.
2. **Loader dataclass**: `src/core/loader.py:1519–1520` (field + default)
   and both `context_data.get(...)` build sites `:2238` and `:2446`.
3. **ContextConfig construction — two sites**:
   `src/api/persistent_session.py:962` (`_derive_context_config`, also used
   by hot-swap refresh) and `src/graph.py:4383`. Both currently map
   `config.context_management.keep_recent_messages` →
   `ContextConfig.keep_recent_messages`; add the new field the same way.
   (The `ContextConfig(...)` at `context.py:592` is a docstring example,
   not a construction site.)
4. **`ContextConfig` dataclass field**: `src/core/context.py` ~:404,
   next to `max_tool_result_length`.

## Gotchas for the implementer

1. **Resume rebuilds the tail at full size.** Truncation lives in graph
   state only; the message log rows keep original content, and a resume
   reloads `seq > boundary_seq` from those rows. Accept this: the next
   over-threshold compaction re-caps. Do **not** mutate persisted rows.
   Verify the transport doesn't re-persist `fresh_recent` copies as new
   rows (it shouldn't — compaction is recorded via `boundary_seq`).
2. **The cap must live inside `summarize_and_compact`**, not in
   `ensure_within_limits`: the progressive loop (:1123–1160) re-invokes
   `summarize_and_compact` from the ORIGINAL messages on every retry, and —
   decisively — manual `/compact` bypasses `ensure_within_limits`
   altogether (see invocation map). Putting it in the wrapper would leave
   the motivating incident unfixed.
3. **Prompt-cache**: mutating history invalidates the provider prompt cache
   (see comment at :1223–1225). Compaction time is the only right time for
   this pass — never truncate per-turn.
4. **Stats/events**: `_last_summarization_stats["after_tokens"]` (:1897) is
   computed from `fresh_recent` — build it from the *capped* copies so the
   cockpit's compaction event reports honest numbers. Optionally add a
   `tail_truncated` count to the `context.compacted` payload.
5. **Marker text matters**: the model acts on it. Keep the "re-run the tool
   / read the saved file" instruction — web_search content is already saved
   to the workspace and registered as citation sources
   (`src/tools/research/web.py:349–362`).
6. **`compaction_runs` counter seam**: `_handle_compact` decides whether
   anything happened by comparing `ctx_mgr.compaction_runs` before/after
   (`persistent_app.py:4251`), which today only increments on the full
   summarize path (context.py:1882). If the early-return path performs
   cap-only truncation, bump the counter (or emit a distinct
   `compaction.completed` reason) so the cockpit doesn't report "nothing to
   compact" after a truncation that actually freed 200k tokens.

## Relationship to the web_search fix

This cap is the **producer-agnostic backstop**; the deferred web_search
snippet+pointer change
(`docs/issues/web_search_full_page_content_bloats_session_context.md`) is
the **ingestion fix** for the worst single producer. Both are wanted: the
web fix stops sessions burning 300k tokens *before* compaction ever runs;
this cap guarantees compaction actually works no matter which tool produced
the bloat (drive/doc reads, PDF extraction, future tools).

## Verification

Unit (extend `tests/test_context_methods.py` / `test_context_overflow.py` /
`test_context_safety.py`, following their class-based patterns):

- Tail ToolMessage > cap → head-truncated, `tool_call_id` + `name`
  preserved, marker appended; ≤ cap → untouched; already-marked → skipped.
- Early-return path: conversation of ≤10 messages containing an oversized
  result → `/compact` (force) produces removal markers + capped copies, not
  a no-op.
- Summarizer input: `_format_messages_for_summary` receives the tail;
  boundary id still `original_conversation[safe_start - 1]`.
- Guard: summary larger than summarized-slice but tail savings dominate →
  compaction proceeds.
- Progressive loop: capped on every retry.

Live (k3d or homelab):

1. Session with a few `web_search(include_raw_content=True)` calls →
   context into the hundreds of k tokens.
2. Manual `/compact` → expect post-compact input around
   summary + ≤(10 × ~4k) tokens (i.e. ~10–20% of a 400k window, not 82%),
   `Compacted N messages to M` log line, and no immediate auto-recompact
   on the next turn.
3. Second `/compact` on an already-tail-heavy session → no longer a no-op.
4. Cockpit: compaction event shows honest after_tokens; conversation still
   coherent (model references truncated results via marker, re-reads from
   workspace when needed).
