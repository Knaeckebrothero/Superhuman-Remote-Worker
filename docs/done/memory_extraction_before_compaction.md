# Memory Extraction Before Compaction — chunked, full-coverage extraction at the summary boundary

> Status: DONE (2026-07-01) — Slices 0–3 + the OQ-C job-end drain shipped, unit-verified (436
> memory/context tests green, `ruff` clean), and the **persistent path confirmed live on k3d** (§7).
> Uncommitted on develop at time of writing. Only follow-ups left are non-implementation: commit/push
> and an optional live worker-job drive (shares the engine + writer with the verified persistent
> path). Parent context: `docs/openclaw_research.md` §10.2 (memory-flush-before-compaction was that
> audit's highest-value unimplemented pattern). Builds on the memory seam from
> `docs/features/agent_memory_overhaul.md` and reuses the chunking engine from
> `docs/features/context_summarization_rework.md`.

---

## What shipped (impl record)

| Slice | What | Where |
|-------|------|-------|
| 0 | Store-side dual-trigger race guard: pre-insert re-validation on every ADD terminal of the verdict path (`recheck_threshold=0.9`, reuses the embedding, excludes already-adjudicated neighbours → NOOP+bump on a *new* twin). Hardens **every** writer. | `src/services/recall_store.py` |
| 1 | `ChunkPlanner` factored out of `SummarizationEngine` — two-reserve budget (`output_reserve`+`carry_reserve`) + native whole-part `overlap_ratio`. Summarizer delegates byte-identically; `count_text_tokens`/`SummarizationFailed`/constants re-exported. | `src/core/chunk_planner.py` (new), `src/core/summarizer.py` |
| 2 | `MemoryExtractionEngine` — map-not-fold, low-loss formatter (no truncation), `TextExtractMemoriesTask` (bypasses the 40-cap), ~15% overlap, in-memory dedup, sequential store, per-chunk skip-on-fail. | `src/services/memory/extraction_engine.py` (new), `src/services/auxiliary.py` |
| 3 | `pre_compaction` CaptureKind; `PreCompactionExtractor` writer + factory; `capture_nowait` (ref-held) + `drain_background`; inline emits before `ensure_within_limits` in both graphs; both YAML pipelines. | `types.py`, `manager.py`, `plugins/legacy_writers.py`, `src/graph.py`, `src/persistent_graph.py`, `config/defaults.yaml`, `config/persistent_defaults.yaml` |
| OQ-C | Job-end drain wired: builder attaches the manager to the compiled graph (`_srw_memory_service`); the worker run loop's `finally` drains in-flight captures, aux-timeout-bounded, before connection teardown. | `src/graph.py`, `src/agent.py` |

Tests: `tests/test_recall_store.py`, `tests/test_context_safety.py`, `tests/test_memory_extraction_engine.py` (new), `tests/test_memory_capture_equivalence.py`, `tests/test_memory_cutover.py`.

---

## 0. Positioning (read this first)

This is **well-trodden prior art, not a novel mechanism.** MemGPT (2023) fires a "memory-pressure
warning" before recursive summarization so the model can persist facts first; Claude Code ships a
`PreCompact` hook; Anthropic's memory tool + context-editing docs auto-warn the model to "save tool
results or context to its memory files **before they're cleared**"; LlamaIndex feeds the evicted
flush window to a fact-extractor before truncating. So the design question is not *whether* to do
this but *how to do it well*.

The literature's clear guidance shapes our framing: a **synchronous boundary flush is a safety net
over a primary async extractor, not a replacement.** The dominant production posture (Zep, Mem0,
LangMem, Anthropic note-as-you-go) is continuous/async per-turn extraction, decoupled from the lossy
step; Mem0 even argues boundary-only extraction is "already too late." We already run that async
primary — `WorkerIntervalExtractor` (modulo turn-gate) and `PersistentIntervalExtractor` (elapsed
gate). **This feature adds the backstop that catches whatever the async scheduler hasn't captured by
the time a mid-phase token-threshold compaction fires.** The highest-leverage risk is therefore not
building the flush (easy) but making the async + boundary extractors *idempotent and reconciled*
(see §4.7) — the single most-reported failure mode for this dual-trigger topology.

## 1. Problem

When context is compacted, `ContextManager.ensure_within_limits()` (`src/core/context.py:1014`)
summarizes old messages into a lossy blob and drops the detail. Its own formatter
(`_format_messages_for_summary`, `context.py:1310`) *replaces old tool results with
`[… omitted (N chars)]` placeholders* (`context.py:1431`) — i.e. the durable facts we most want to
mine are the first thing thrown away. What's captured at that moment today:

- the **summary blob**, stored as one low-importance memory by `CompactionMemoryWriter`
  (`legacy_writers.py:258`), and
- whatever the **async interval extractor** happened to grab on its modulo turn-gate — not aligned
  with a mid-phase token-threshold compaction, so it may never have run over the evicted window.

**Why the naive fix fails — and why chunking is mandatory, not an optimization.**
`extract_and_store_memories` (`auxiliary.py:1115`) tail-caps input to `_MAX_OBSERVATION_WINDOW = 40`
messages (`auxiliary.py:1112`), keeping `messages[-40:]` — the **newest** messages, exactly the ones
compaction *keeps*. The at-risk content is the **oldest** (evicted) messages. Worse, if we simply
lifted the cap and passed a large history, `AuxiliaryLLM.chain()` **raises the non-retryable
`AuxInputTooLarge`** when input exceeds the aux model's `max_context_tokens` (`auxiliary.py:861-873`)
— it does not truncate or warn-and-proceed — and `extract_and_store_memories` swallows it non-fatally
and returns **zero** memories (`auxiliary.py:1184-1191`). So on any oversized history, single-shot
extraction silently extracts *nothing*. Chunking to stay under the aux window is the only thing
standing between "long history" and "silent zero extraction."

## 2. Goals

- Extract durable memories from **all** messages about to be evicted by a compaction, before the
  summary replaces them — regardless of message count or size.
- Reuse the summarizer's proven chunk-planning approach rather than inventing a second one.
- Keep it off the critical path (compaction latency must not regress, esp. interactive sessions).
- Additive only — do not disturb the equivalence-tested existing extractors
  (`tests/test_memory_capture_equivalence.py`).

**Non-goals (v1):** replacing the 40-cap for interval/phase/teardown extractors (they intentionally
look at a recent window); covering the `force=True` manual `/compact` and resume compaction paths
(see the call-site table in §4.6 — they don't route through the two auto sites, and phase-transition
compaction is already covered by `phase_boundary_extractor`); extract-time conditioning on existing
memories (§8); gleaning passes (§8).

## 3. Decisions

1. **Map, not fold.** Summarization folds a rolling summary across chunks
   (`SummarizationEngine.run`, `summarizer.py:425`). Extraction runs each chunk **independently** and
   unions the results. Confirmed correct by the literature: LangChain's "brute force" extraction
   pattern is per-chunk map + union; *no* surveyed memory system folds facts (fold is reserved for the
   summary blob). Refine/fold's sequential coherence is wasted on independent facts and only adds
   latency + drift.
2. **Reuse the planner via a two-reserve `ChunkPlanner`.** Factor `plan()` / `chunk_budget` /
   overhead-measurement out of `SummarizationEngine` into a pure, shared `ChunkPlanner`. Critical
   parity detail: today's `chunk_budget` subtracts the output budget **twice** (`summarizer.py:333-338`)
   — once for model output, once because the rolling summary rides along each fold chunk. Extraction
   has no rolling summary, so the planner must take **two explicit reserves** (`output_reserve` +
   `carry_reserve`); the summarizer passes both (byte-identical), extraction passes `carry_reserve=0`.
3. **Extraction needs its own low-loss formatter.** Neither existing formatter is usable: the summary
   formatter placeholders-out old tool results (`context.py:1431`), and `_format_messages_for_extraction`
   (`auxiliary.py:1571`) returns one joined string (not the `List[str]` the planner packs) and caps
   tool results at 1000 chars. The engine supplies a new `messages→List[str]` formatter that filters
   workspace-injection messages but does **not** truncate — the planner's oversized-part hard-split
   handles giant tool results.
4. **Add chunk overlap (extraction-only).** Our planner packs with hard, non-overlapping boundaries,
   which risks dropping/garbling facts that straddle a chunk edge — the one thing the literature flags
   against for extraction. Add ~15% overlap (chat reads as narrative; consensus 10–20%), snapped to
   whole-message boundaries, as a `ChunkPlanner` param defaulting to 0 (summarizer stays
   non-overlapping for parity). The usual overlap penalty (duplicate facts) is absorbed by our
   write-time dedup, so it's near-free insurance.
5. **Serialize stores + in-memory cross-chunk dedup.** Write-time dedup (`RecallStore.store`) compares
   against **already-committed** `valid_to IS NULL` rows, so concurrent `store()` calls race and both
   ADD. The map's `store()` phase therefore runs **sequentially**; additionally, dedup the unioned
   memories **in memory** before persisting so cross-chunk duplicates never depend on DB visibility.
6. **Window = evicted slice.** Pass `messages[:-keep_recent_messages]` (keep_recent=10), not the full
   list. Precedent is strong (LlamaIndex extracts only the flush window; MemGPT only the evicted
   slice), and full-history re-extraction is what drives the cost/accuracy tax and stale-fact churn.
   The slice is an approximation of the true eviction boundary but errs safe (§4.6).
7. **Keep both the summary blob and extracted facts.** Near-unanimous in the literature (LlamaIndex
   runs a fact block *and* a raw vector block; Anthropic keeps compaction-summary + memory). Distinct
   stores, distinct retrieval paths — the lossy summary must never overwrite a reconciled fact.
8. **Fire-and-forget over a snapshot.** The emit snapshots the evicted slice and schedules extraction
   as a detached task (mirroring `graph.py:1960`), so compaction proceeds immediately and the snapshot
   guarantees pre-compaction detail. Because the chunked run is longer-lived than the existing one-shot
   captures, use a ref-holding `capture_nowait` helper (the existing bare `create_task` holds no ref —
   real GC hazard, confirmed).

## 4. Architecture

### 4.1 `ChunkPlanner` (extracted from `SummarizationEngine`)

New module (e.g. `src/core/chunk_planner.py`); **re-export `count_text_tokens` + constants from
`summarizer.py`** — external importers exist (`persistent_graph.py:32`, `auxiliary.py:862`, and tests
import `DEFAULT_AUX_WINDOW`/`MAX_ATTEMPTS`/`_describe_exc`). Pure/deterministic, no LLM calls.

```python
class ChunkPlanner:
    def __init__(self, aux_window, *, overhead_tokens, output_reserve,
                 carry_reserve=0, overlap_ratio=0.0,
                 token_counter=None, counting_model=None): ...
    @property
    def chunk_budget(self) -> int:
        # floor(aux_window*SAFETY_MARGIN) - overhead_tokens - output_reserve - carry_reserve
    def plan(self, formatted_parts: list[str]) -> ChunkPlan: ...   # hard-splits oversized parts;
        # applies overlap_ratio (whole-part) between adjacent chunks
```

Moves with it: `ChunkPlan`/`Chunk` (renamed from `SummarizationPlan`/`ChunkSpec`, plain dataclasses →
compare by value for the parity test), `count_text_tokens`, `_ENCODING_CACHE`, and constants
`SAFETY_MARGIN=0.85`, `DEFAULT_AUX_WINDOW=100_000`, `SCHEMA_OVERHEAD_TOKENS=2_000`,
`_SPLIT_MARKER_RESERVE_TOKENS=64`, `_FALLBACK_CHARS_PER_TOKEN`. The planner raises the shared
`SummarizationFailed("aux_window_too_small")` that `context.py` catches by `reason` (keep the class
importable by both). `SummarizationEngine.plan()` stays as a **thin delegator** to an internal
`ChunkPlanner` (with `overlap_ratio=0`, `output_reserve=carry_reserve=output_budget`) so `context.py`
and existing tests stay byte-identical — the extraction path is then a pure add.

### 4.2 `MemoryExtractionEngine` (new, `src/services/memory/extraction_engine.py`)

Mirror of `SummarizationEngine`, but map-not-fold:

```
class MemoryExtractionEngine:
    def __init__(self, auxiliary, recall_store, extraction_prompt, *, token_counter=None):
        # aux_window = auxiliary.max_context_tokens (same budgeting authority)
        # overhead = _measure_overhead() against ExtractMemoriesTask prompt + ExtractedMemories schema
        # planner = ChunkPlanner(aux_window, overhead_tokens=overhead,
        #                        output_reserve=<fixed extraction-JSON reserve>, carry_reserve=0,
        #                        overlap_ratio=0.15, token_counter=..., counting_model=<aux model>)

    def _format(self, messages) -> list[str]:   # NEW low-loss formatter (§3.3), no truncation
    async def run(self, messages, *, phase=0) -> int:
        parts = self._format(messages)
        plan = self.planner.plan(parts)
        collected = []
        for chunk in plan.chunks:                      # MAP; per-chunk retry/backoff (§4 Q4 model)
            try:
                task = TextExtractMemoriesTask(chunk.text, self.extraction_prompt)
                result = await self.auxiliary.chain(task, timeout=CALL_TIMEOUT)
                collected.extend(result.memories)
            except asyncio.CancelledError:
                raise                                  # hard-interrupt passthrough
            except Exception as e:
                logger.warning("pre-compaction chunk %s failed (skipped): %s", chunk.index, e)
                continue                               # skip-on-exhaustion (PARTIAL, not abort)
        deduped = _dedup_in_memory(collected)          # §3.5 — before persisting
        stored = 0
        for mem in deduped:                            # SEQUENTIAL store (§3.5 race)
            mid = await self.recall_store.store(
                content=mem.content, summary=mem.summary, keywords=mem.keywords,
                importance=mem.importance, memory_type=mem.type, source="observer",
                source_phase=phase, retrieval_messages=mem.retrieval_messages or None)
            stored += bool(mid)
        return stored
```

Notes:
- **`TextExtractMemoriesTask`** — trivial ~10-line `AuxTask` mirroring `SummarizeTask(conversation_text=…)`
  (`auxiliary.py:377`): `build_context()` returns the chunk text straight through, so `chunk.text`
  feeds `chain()` without re-formatting and without the 40-cap.
- **Token counter** — `MemoryRuntime` carries no counter, so the engine builds its own via
  `count_text_tokens` seeded with the aux model name (best-effort tokenizer selection).
- **Store kwargs** reproduce `extract_and_store_memories`'s contract (`auxiliary.py:1156-1167`);
  `source_turn_start/end` omitted (no single turn window for a multi-chunk slice).
- **No progress callback** for v1 (fire-and-forget, no cockpit surface).
- **Retry model** mirrors the summarizer (`MAX_ATTEMPTS=3`, `BACKOFF_SECONDS`, overflow = non-retryable,
  `CancelledError` re-raised) — but **skip-on-exhaustion per chunk, not fatal**, since chunks are
  independent (unlike a fold, where a lost pass corrupts the rolling summary).

### 4.3 The `pre_compaction` event + writer

- `types.py` — add `"pre_compaction"` to `CaptureKind` **and** `CAPTURE_KINDS` (`__post_init__` raises
  on unknown kinds).
- `legacy_writers.py` — `PreCompactionExtractor` subscribes `{"pre_compaction"}`, gates on
  `_aux_task_enabled(runtime, "extract_memories")` (like `PhaseBoundaryExtractor`), but constructs a
  `MemoryExtractionEngine` from the runtime and calls `engine.run(event.messages, phase=event.phase)`
  — it does **not** call the 40-capped `extract_and_store_memories`. Register as
  `pre_compaction_extractor`. `MemoryManager.capture` already contains per-writer exceptions
  (`manager.py:254-276`), so a failed run can't break compaction.
- `MemoryRuntime` already carries `auxiliary_llm`, `recall_store`, `extraction_prompt`, `memory_config`,
  `auxiliary_config` — everything the engine needs except the token counter (built internally).

### 4.4 `capture_nowait` (new, `MemoryManager`)

Net-new — there is no background-task registry today, and the existing `create_task(capture(...))`
calls hold no reference (GC hazard). `capture_nowait(event)` = `create_task` + add to a `self._bg_tasks`
set + `done_callback` discard. Used by both emit sites so the longer-running chunked extraction isn't
collected mid-flight.

### 4.5 Emit wiring (both graphs)

```
snap = list(messages[:-keep_recent])          # evicted slice; keep_recent = context_mgr.config.keep_recent_messages
if memory_service is not None and context_mgr.should_summarize(messages):
    memory_service.capture_nowait(CaptureEvent(kind="pre_compaction", messages=snap, phase=phase_number))
messages = await context_mgr.ensure_within_limits(...)
```

- **Worker** (`create_execute_node`, `graph.py:813`): insert **after** the temporary threshold-lowering
  (`graph.py:~987`) and **before** the `try` at 994, so the `should_summarize` gate is evaluated under
  the same lowered thresholds `ensure_within_limits` uses (emitting earlier under-fires on
  injection-overhead-tipped cases). In scope: `messages`, `phase_number`, `memory_service` (closure),
  `context_mgr` (closure).
- **Persistent** (`_execute_turn`, `persistent_graph.py:799`): insert at `~1020`, before the
  `_await_or_hard_interrupt` wrap. The manager is named `context_manager`; **no phase concept** → use
  `phase=0` (matches the existing `turn_end` capture). `memory_service`, `context_manager`, `messages`
  all params.

### 4.6 Coverage boundary — all `ensure_within_limits` call sites

| Site | Trigger | v1 |
|------|---------|----|
| `graph.py:995` | auto | ✅ worker emit target |
| `persistent_graph.py:1022` | auto | ✅ persistent emit target |
| `graph.py:1372`, `2075`, `3398` | `force=True` | deferred (overflow/emergency/resume rebuild) |
| `graph.py:2773` | `force=force_summarize` | already covered by `phase_boundary_extractor` |
| `persistent_app.py:3773`, `3842` | `trigger="resume"` | deferred (resume restore) |
| `persistent_app.py:4113` (`summarize_and_compact` direct) | manual `/compact` | deferred — bypasses `ensure_within_limits` entirely |

**Evicted-slice fidelity:** the true boundary is `find_safe_slice_start(conversation, len(conversation)-keep_recent)`
(`context.py:1715-1716`), which walks backward to avoid orphaning a `ToolMessage` from its parent
`AIMessage`. `messages[:-keep_recent]` differs by (a) leading summary/injection messages the real path
strips (→ harmlessly included) and (b) a few extra recent messages held by tool-pairing (→ harmlessly
re-extracted). Drift is single-digit messages; planner coverage + dedup absorb it. It **errs toward
over-inclusion — never misses an evicted message.**

### 4.7 The dual-trigger dedup race (the load-bearing risk)

The async interval extractor and this boundary flush both fire as detached tasks and both call
`store()`. Two race manifestations:

1. **Cross-chunk (within one run):** handled by §3.5 — in-memory dedup of the union + sequential stores.
2. **Cross-task (interval vs boundary overlap):** two concurrent `store()` calls each run
   `find_similar_many` then insert; if they interleave before either commits, both ADD → near-duplicate
   *and potentially diverging* rows. This is the most-reported failure mode for dual-trigger memory
   (cf. OpenViking #687: "candidate N+1's dedup search can't find candidate N's not-yet-indexed
   vectors → 3–4 near-duplicate files").

Our situation is milder than the worst case, and the design shrinks it further:
- `RecallStore.store` embeds+inserts **synchronously** (no lagging async embedding queue), and the
  default verdict path (0.5 cosine floor → LLM ADD/NOOP/UPDATE/MERGE, bi-temporal supersede)
  reconciles rather than blind-inserts.
- The **evicted-slice window (§3.6) already minimizes the overlap**: the boundary flush mines the
  *old* messages while the interval extractor windows the *recent* turns — opposite ends of the
  conversation — so the two triggers rarely cover the same content by construction.

**v1 decision — double-check dedup (optimistic concurrency) in `RecallStore.store`.** Today the
dangerous window spans `find_similar_many → LLM verdict → insert`, and the verdict is a *slow* aux
call, so the window is long. Add a **second `find_similar_many` immediately before the ADD insert**,
reusing the already-computed embedding (a cheap extra SELECT, no re-embed). If a near-identical twin
(high threshold, ≈0.9+) has appeared *since* the first check, downgrade the ADD to NOOP+bump instead
of inserting. This collapses the race window from "LLM-inclusive" to a single DB round-trip, and it
fixes the race for **every** writer (interval/phase/boundary/teardown), not just this feature — it's
the "make all memory tasks dedup last" fix. It only triggers under genuine concurrency: a non-racing
write's second check sees the same state as the first, so existing behavior (and the equivalence
tests) is unchanged. The residual sub-window (second SELECT → INSERT, pure DB, sub-millisecond) needs
two writes to land at the same instant — negligible, and self-healed by the next verdict write + the
assembler. Heavier fixes (job-scoped write lock, content-addressed range-IDs, or a DB uniqueness
constraint on a content hash) stay available if k3d ever shows residual dups, but shouldn't be
needed. Ships as an independent store-side hardening slice (§6, Slice 0).

## 5. What changes where

| File | Change |
|------|--------|
| `src/core/summarizer.py` | Extract `ChunkPlanner`; `SummarizationEngine.plan()` delegates (no behavior change); re-export `count_text_tokens`+constants |
| `src/core/chunk_planner.py` | **New** pure planner (two-reserve budget + `overlap_ratio`) |
| `src/services/memory/extraction_engine.py` | **New** `MemoryExtractionEngine` (map + low-loss formatter + in-mem dedup + serial store) |
| `src/services/auxiliary.py` | Add `TextExtractMemoriesTask` (mirror `SummarizeTask`) |
| `src/services/memory/types.py` | Add `"pre_compaction"` kind |
| `src/services/memory/plugins/legacy_writers.py` | `PreCompactionExtractor` + factory |
| `src/services/memory/manager.py` | `capture_nowait()` helper (ref-held bg tasks) |
| `src/services/recall_store.py` | Pre-insert re-validation on the ADD path (Slice 0 race hardening) |
| `src/graph.py`, `src/persistent_graph.py` | Inline emit before `ensure_within_limits` |
| `config/defaults.yaml:256`, `config/persistent_defaults.yaml:172` | Add `- pre_compaction_extractor` |
| `tests/` | Planner parity; engine chunk/map/overlap/dedup/skip; writer + dispatch |

## 6. Slices (independently shippable, in order)

0. **Store-side race hardening (independent, ships first).** Add pre-insert re-validation to the ADD
   path of `RecallStore._store_with_verdict` (§4.7): reuse the candidate embedding for a second
   `find_similar_many`; on a *new* ≥0.9-cosine twin, NOOP+bump instead of inserting. Benefits the
   existing async extractors immediately. Test: two `store()` calls of the same fact where a twin is
   committed between the first and second check yield one row (bump, not a second insert); a
   non-racing write is unaffected (second check == first).
1. **Planner extraction** — `ChunkPlanner` with two-reserve budget; `SummarizationEngine` delegates.
   Parity test (`tests/test_context_safety.py`): `new.describe() == legacy.describe()` + budget tuple,
   fed the same `char_counter`. Ships alone, zero behavior change. (`overlap_ratio` lands here,
   default 0, with its own unit test.)
2. **Extraction engine** — `MemoryExtractionEngine` + `TextExtractMemoriesTask` + low-loss formatter.
   Unit-test (`tests/test_memory_extraction_engine.py`): a >40-message / oversized-message history
   produces N chunks, memories come from the **oldest** messages (proves the 40-cap is bypassed),
   overlap captures a boundary-straddling fact, in-memory dedup collapses cross-chunk dups, and one
   failing chunk (`chain` side_effect) is skipped without aborting.
3. **Event + writer + wiring** — `pre_compaction` kind, `PreCompactionExtractor`, `capture_nowait`,
   the two inline emits, both YAML lines. Writer test (`tests/test_memory_capture_equivalence.py`):
   spy on the **engine** (not `extract_and_store_memories`), assert gated by task flag /
   `aux_enabled` / `recall_store=None`, plus registration + `MemoryManager.capture` dispatch.

## 7. Acceptance / verification

- **Unit:** slice-by-slice as in §6. Parity test must fail if the double-subtraction is dropped.
- **k3d end-to-end (the real gate):** drive a session into a mid-phase token-threshold compaction;
  assert memories derived from the **evicted (old)** messages exist and were written *before* the
  summary memory, in **both** a worker job and a persistent session. Confirm compaction latency is
  unchanged (fire-and-forget). Watch for cross-task duplication (§4.7) — if the Slice-0 double-check
  leaves material dups, escalate to the reserve fixes (write lock / content-addressed IDs).

  **VERIFIED (persistent, 2026-07-01).** k3d session on `minimax-m3` (32k window), driven via the
  cockpit UI to a mid-turn compaction (`Context compaction triggered: 12 messages, 18773 tokens`,
  >keep_recent so the evicted slice was non-empty). Both writers fired at the boundary —
  `ExtractMemoriesTask` (interval) *and* `TextExtractMemoriesTask` (this feature's engine) — and the
  engine logged `pre-compaction extraction: 1 chunk(s), 1 unique, 1 stored (phase 0)`
  (`extraction_engine.py:227`). The stored memory carried facts from the **oldest, evicted** message
  (a planted "PROJECT-ORCHID-SEVEN / QUARTZ-BRAVO / Reykjavik" block). The ingestion verdict +
  Slice-0 double-check reconciled the interval/boundary overlap — only unique rows persisted, no
  duplicate spray (§4.7 holds in practice). Compaction proceeded immediately; extraction ran detached
  (and persisted even though the post-compaction LLM turn 413'd on the tiny 32k window — a test-rig
  artifact, and incidentally proof of the fire-and-forget resilience). Worker path not separately
  driven live, but it shares the writer + engine + event and is covered by the unit + cutover-wiring
  suites.

## 8. Resolved decisions (were open questions)

- **OQ-A — dedup race → double-check dedup in `RecallStore.store` (§4.7, Slice 0).** Re-validate
  against committed rows immediately before the ADD insert (reusing the embedding); NOOP+bump on a
  *new* ≥0.9-cosine twin. Collapses the LLM-inclusive race window to a single DB round-trip and
  hardens **every** writer. Residual sub-window is negligible + self-healed. Heavier fixes (write
  lock / content-addressed IDs / DB uniqueness constraint) stay in reserve if k3d shows residual dups.
- **OQ-B — extract-time existing-memory conditioning → deferred to v2.** Write-time verdict already
  reconciles for correctness; conditioning is a cost-optimization (fewer redundant candidates +
  verdict calls) that enlarges prompts and couples extraction to retrieval. Also a lever to revisit
  if OQ-A residual ever bites.
- **OQ-C — worker job-end drain → await-with-timeout, DONE.** `MemoryManager.capture_nowait` holds a
  strong ref in `self._bg_tasks`; `drain_background(timeout=)` awaits the pending set and leaves
  stragglers detached on timeout. Wired end-to-end: `build_phase_alternation_graph` attaches the
  manager to the compiled graph (`compiled._srw_memory_service`, since the builder's return type is
  pinned by the deprecated wrapper + multiple callers), and the worker run loop's `finally`
  (`agent.py`) drains it — bounded by the aux call timeout — *before* tearing down connections, so a
  compaction on the last LLM call before freeze still persists. Low stakes in practice:
  `pre_compaction` fires on a *mid-run* token-threshold compaction, so its task is almost always done
  long before the graph reaches END; the drain only matters for that last-call edge.
- **OQ-D — gleaning → single-pass v1.** One extraction pass per chunk; revisit adding a GraphRAG-style
  gleaning pass (a config knob) only if recall measures low.

## 9. Sources

Codebase refs are inline (`file:line`). Web/prior-art (load-bearing claims):

- **Map vs fold / brute-force extraction, overlap, dedup warning:** LangChain "How to handle long text"
  (extraction_long_text) · chunk overlap 10–20%: TechNet Experts "Optimizing RAG: Chunk Size and
  Overlap" · dedup thresholds θ_E≈0.8/θ_R≈0.7: itext2kg/ATOM; Microsoft GraphRAG→Neo4j dedup ·
  gleaning + cheap model + concurrency: GraphRAG (Neo4j); Zep `SEMAPHORE_LIMIT=10`.
- **Flush-before-compaction prior art:** MemGPT memory-pressure warning (arXiv 2310.08560) · Claude
  Code `PreCompact` hook (code.claude.com/docs) · Anthropic memory-tool + context-editing ("save …
  before they're cleared") + "Effective context engineering" (anthropic.com/engineering) · OpenAI
  personalization cookbook (session notes reinjected before trimming) · LlamaIndex flush→fact-extractor
  (developers.llamaindex.ai) — the closest direct precedent.
- **Reconciliation / bi-temporal:** Zep/Graphiti (arXiv 2501.13956) · Mem0 ADD/UPDATE/DELETE/NOOP
  (arXiv 2504.19413) — also the "extraction at compression time is already too late" argument.
- **Pitfalls:** dual-trigger dedup race — OpenViking #687 · cost/accuracy tax — Mem0 LOCOMO
  (full-context 72.90% vs 66.88%), ConvoMem · context rot — Chroma "context rot".
- **Preprint-grade (treat numbers cautiously; qualitative claims corroborated by vendor docs):** Engram
  (arXiv 2606.09900), SSGM (2603.11768). The widely-repeated "36.7× → 60% lost" figure is untraceable —
  do not cite.
</content>
