---
tags:
  - feature
  - agent
  - context-management
  - cockpit
  - ux
aliases:
  - summarization rework
  - compaction engine
  - compaction progress UI
related:
  - "[[session_silent_failure_audit]]"
  - "[[surface_silent_aux_failures]]"
  - "[[auxiliary]]"
  - "[[persistent_session_midturn_message_loss]]"
---

# Context Summarization Rework — aux-budgeted rolling compaction, progress UI, live token counters

**Status:** Implemented (2026-06-12, S1–S5 in one pass) + **§9 duplicate-banner
follow-up fixed same day** (found during live verification) + **§10 token-UI
refinement** (2026-06-23: composer-bar alignment shipped; reasoning-token
estimate + context-bar restyle implemented + test-verified; live render blocked
on the LiteLLM usage gap — `docs/issues/litellm_streaming_usage_not_surfaced.md`).
Absorbs the issues deferred from `docs/issues/session_silent_failure_audit.md` to the
"summarization rework track": **#4-full** (failure semantics), **#5**
(tool-result caps), **#6** (keep-window elision), **#7** (aux-context
clamping/chunking) — plus the compaction progress UI and live token counters.

**Evidence base:** threads `1f39a5a6` (gpt-5.5 @ 1.05M main ctx, 951k-token
conversation sent whole to a 131k summarizer) and `b60166ee` (gpt-5.3-codex-spark
@ 128k, four giant PDF tool results → unrecoverable 234k-token request),
dev cluster 2026-06-12.

---

## 1. Problem

Compaction is the agent's only defense against context exhaustion, and it
failed three ways at once:

1. **The summarizer was handed inputs sized for the wrong model.** The
   chunking thresholds (`summarization_safe_limit`, `summarization_chunk_size`)
   are derived from the **main** model's context window
   (`src/core/loader.py:616`), but summarization executes on the **auxiliary**
   model (gemma-4-moe @ 131k). With a 1.05M main model the "safe" limit is
   ~943k and chunks are ~630k — both ~5–7× the summarizer's window. The
   chunked path effectively never engages, and would overflow per-chunk if it
   did.
2. **Failure destroyed history or wedged the turn.** Structured pass (≤600s)
   → *different* unstructured fallback prompt (≤600s) → placeholder string
   replacing real history (pre-stopgap), all invisible to the user. During the
   aux-router 503 flap this produced both the ~8-minute silent wedges and the
   malformed "summary issues" seen across agents.
3. **Zero visibility.** The only event is `context.compacted`, emitted *after*
   success. During a multi-minute compaction the user sees a dead UI; on
   failure they see nothing at all.

### How it came to be (so the fix targets the right layer)

- `875d4e15` (2026-01-29): recursive summarization built when the summarizer
  **was** the main LLM — deriving limits from the main window was correct then.
- `71c90c47` (2026-03-08): AuxiliaryLLM introduced; execution moved to the aux
  model, but the budget stayed a leaf in the main-model `limits:` block.
  Benign by coincidence (90k static < 131k aux window).
- Hand-authored matrix entries then drifted upward with main windows
  (minimax: `summarization_safe_limit: 160000` — already > 131k).
- `4c8d149d` (2026-06-07): the settings-matrix refactor mechanized the pattern
  (all limit leaves = fractions of the main base, including per-model admin
  overrides) — arming it for 1M-context models.

**Lesson:** any formula living in the *main-model* settings taxonomy can
regress the same way. The summarizer's budget must be owned by the summarizer.

## 2. Goals

- **G1 — One path.** A single summarization algorithm for worker and
  persistent agents (they already share `ContextManager.ensure_within_limits`;
  keep that seam). No silent fallback to a second, different summarizer.
- **G2 — Budget from the executing model.** Every summarization call is sized
  against the aux model's own resolved context window, measured with a real
  tokenizer, at call time.
- **G3 — Scale-invariant.** Works identically for 1 pass or 10,000 passes:
  linear rolling fold (no recursion, no depth caps), per-pass bounded calls,
  no global timeout. If it takes long, the user *sees* it taking long.
- **G4 — Failure is loud and non-destructive.** Retry transient errors with
  backoff; on exhaustion keep history, emit a failure event, let the turn
  error surface (audit #2 infra) explain what happened.
- **G5 — Observable.** Progress events drive a cockpit animation (mockup
  below) and land in the SSE journal so reloads mid-compaction reconstruct
  state. Live token counters ride the same event spine.

**Non-goals:** fixing the `ai.h4ll.app` router flap (environmental — tracked
in [[surface_silent_aux_failures]]); a jobs-page compaction UI for worker
agents (v1 = logs/audit only); changing *when* compaction triggers
(thresholds/cadence unchanged in v1 — revisit incremental pre-folding later);
parallel map-reduce execution (see Decisions).

## 3. Decisions

**D1 — Rolling fold, not parallel map-reduce.** Plan N chunks upfront, then
sequentially fold: `summary_i = summarize(summary_{i-1} + chunk_i)`. Each pass
sees the running state (coherent cross-chunk references — the existing
"old summaries incorporated" behavior, done properly), there is no separate
combine/unify failure point, progress maps 1:1 onto a pass counter, and we
don't hammer the one flaky aux endpoint with concurrent calls. Parallelism
buys nothing when the endpoint is the bottleneck.

**D2 — The unstructured fallback is deleted.** One structured prompt/schema.
Robustness comes from bounded retries with exponential backoff on the *same*
call, not from a second algorithm that produces differently-shaped output.

**D3 — Budget is computed at call time from the aux client's window**, which
is already resolved at every AuxiliaryLLM construction site
(`agent.py:347`, `persistent_app.py:~1195`) but currently only handed to the
HTTP guard. `summarization_safe_limit` and `summarization_chunk_size` are
**deleted** from the `limits:` taxonomy (loader derivation, matrix docs,
LimitsConfig) so there is no leaf left to mis-derive.

**D4 — Progress is a transport-agnostic callback** on the ContextManager.
Persistent sessions wire it to `_broadcast` (journaled → SSE replay); worker
agents wire it to the logger (+ optional audit row). The engine doesn't know
about WebSockets.

**D5 — v1 UI scope = persistent sessions** (the mockup). Worker jobs get
structured log lines; a jobs-page surface can subscribe to the same events
later.

## 4. Architecture

### 4.1 The engine (new: `src/core/summarizer.py`)

Extract the summarization machinery from `ContextManager` into a
`SummarizationEngine` with a pure, testable planning step:

```
ensure_within_limits / summarize_and_compact        (unchanged seam)
        │  messages_to_summarize (+ old summaries as seed)
        ▼
┌─ SummarizationEngine ────────────────────────────────────────────┐
│ 1. measure()   exact tokenizer for the aux model                 │
│ 2. plan()      → SummarizationPlan {n_passes, chunks:[{range,    │
│                  first_idx,last_idx,tokens}], budgets}           │
│ 3. fold loop   for i, chunk in plan:                             │
│                  summary = call(summary, chunk)   ── retries ──┐ │
│                  emit progress(pass=i, …)                      │ │
│                                                  backoff 5/15/45s│
│ 4. result      SummaryResult | SummarizationFailed (typed)       │
└──────────────────────────────────────────────────────────────────┘
        ▼
summarize_and_compact applies result (existing guards: summary-larger-
than-original, boundary id, RemoveMessage markers — all unchanged)
```

**Budget math** (all numbers measured, not guessed):

```
aux_window     = AuxiliaryLLM.max_context_tokens        # new attr, see 4.2
overhead       = count(system_prompt + schema + fold template)
output_budget  = tokens(max_summary_length)             # summary is bounded
safe_input     = floor(aux_window * 0.85) - overhead - output_budget
chunk_budget   = safe_input - output_budget             # room for running summary
n_passes       = ceil(input_tokens / chunk_budget)
```

For gemma @131k that's ~95–105k per chunk → the 951k conversation from
`1f39a5a6` becomes a **10-pass fold** — exactly the "divide into ~10 <128k
parts" behavior originally expected.

Properties:
- **Exact counting:** `get_token_counter(aux_model)` replaces every
  `len(text)//4` in the plan path (the estimate-vs-exact mismatch is how
  951k slipped past a 943k gate).
- **Oversized single messages:** if one formatted message exceeds
  `chunk_budget`, the planner hard-splits it with `[part i/j]` markers.
  Rare once S4 (tool caps) lands, but the plan must never produce an
  over-budget call.
- **Retries:** per fold call, 3 attempts, expo backoff (5s/15s/45s), retrying
  timeouts/429/5xx. A 413/`context_overflow` is a plan bug → no retry, abort,
  log loudly. Per-call timeout `summarization_call_timeout` (default 240s)
  replaces the single 600s blob.
- **Cancellation:** the fold loop checks for cancellation between passes;
  the existing hard-interrupt race in `persistent_graph.py:786` keeps working.
- **Failure:** any chunk exhausting retries → `SummarizationFailed` → caller
  keeps history uncompacted (today's #4 stopgap, now with an event). Delete
  `_recursive_summarize`, `_split_into_chunks`, and the unstructured fallback
  in `_single_pass_summarize`.

### 4.2 Budget wiring

`AuxiliaryLLM.__init__` gains `max_context_tokens: Optional[int]`. The three
construction sites already hold the resolved value
(`model_settings.get("model_max_context_tokens")`) — pass it through:

- `src/agent.py:374` (worker, dedicated aux model)
- `src/agent.py:341` + `graph.py:3737` (fallback: aux == summarization/main
  LLM → pass the **main** window; correct, since the summarizer *is* the main
  model there)
- `src/api/persistent_app.py:~1207` and `:~3898` (persistent + aux hot-swap)

Engine fallback when absent: conservative 100k floor + WARNING.

**Pre-flight guard for every aux task** (not just summarization): a typed
check in `AuxiliaryLLM.chain()` — input tokens > window ⇒ raise
`AuxInputTooLarge` (non-retryable, recorded via `AuxHealth`). Memory
extraction and title calls were sending the same 951k payloads; they should
fail fast and visibly, not at the transport. (Their callers already swallow
exceptions gracefully.)

### 4.3 Config deletions/additions

| Change | Where |
|---|---|
| DELETE `summarization_safe_limit`, `summarization_chunk_size` derivation | `src/core/loader.py:616-617`, `SUMMARIZATION_SAFE_FRACTION`/`_CHUNK_FRACTION` consts |
| DELETE the two fields | `LimitsConfig` (loader.py), `ContextConfig` (context.py), matrix header comment, `config/README.md:333`, `config/prompts/catalog.yaml:146` |
| ADD `auxiliary.summarization_call_timeout` (default 240s) | `AuxiliaryConfig`; replaces `summarization_timeout=600` plumbing |
| ADD `max_context_tokens` attr | `AuxiliaryLLM` |
| KEEP `max_summary_length` (output bound) and all trigger thresholds (`context_threshold_tokens` etc. — genuinely main-model) | unchanged |

### 4.4 Event contract

Emitted via the engine's progress callback; persistent wires to `_broadcast`
(stamped `(epoch, seq)` → `thread_events` journal → SSE replay covers
mid-compaction reloads), worker wires to structured log lines.

```jsonc
// compaction.started
{ "trigger": "auto" | "manual" | "resume",
  "total_tokens": 951682, "ctx_used_pct": 91,
  "ctx_limit_tokens": 1047576,          // main window (for the header %)
  "aux_limit_tokens": 131072,           // summarizer window
  "n_passes": 10,
  "plan": [ { "pass": 1, "first_msg": 1, "last_msg": 112, "tokens": 98000 }, … ] }

// compaction.progress  (1/pass + attempt bumps)
{ "pass": 4, "n_passes": 10, "first_msg": 113, "last_msg": 141,
  "in_tokens": 38000, "out_tokens": 2500,
  "stage": "summarizing" | "finalizing", "attempt": 1 }

// compaction.completed — extends the existing context.compacted params
{ …existing…, "n_passes": 10, "duration_ms": 184000,
  "before_tokens": 951682, "after_tokens": 41000 }

// compaction.failed
{ "reason": "aux_unavailable" | "aux_input_too_large" | "cancelled",
  "pass": 7, "n_passes": 10, "kept_messages": true }
```

### 4.5 Cockpit UI (the mockup)

Service (`persistent-chat.service.ts`): new `compaction = signal<CompactionState
| null>`; dispatcher cases for the four frames (`context.compacted` case at
`:1772` becomes the completion path). State must be reconstructible from a
replayed `compaction.progress` alone (reload mid-fold).

Timeline block (in-chat, replaces nothing — appears where the compaction
happens chronologically):

```
│ ◌ COMPACTING CONTEXT   auto · context 91%        176.2k / 184.3k tok
│ ████████░░ ████████░░ ██████████ ███░░░░░░░      (segmented: n_passes)
│ Pass 4/10  summarizing messages 113–141              38.0k → 2.5k
```

- Segmented bar: one segment per pass for `n_passes ≤ 20`; above that,
  a single continuous bar + `pass i/N` text (the "800000 bjillion steps"
  case must render fine — never assume small N).
- Footer status strip (composer-adjacent, like the mockup):
  `COMPACTING · structural summarization — pass 4 of 10 · elapsed 12:49`,
  elapsed timed client-side from the `started` frame.
- Retry visibility: `attempt > 1` renders `(retry 2/3)` after the pass label —
  during an aux flap users see *waiting-with-retries*, not a dead spinner.
- Failure: strip + block turn to warning tone, durable ⚠ system line via the
  #2 `turn.error` rendering; history visibly intact.
- Completion: collapse into the existing "Context summarized" banner.
- These frames count as agent activity → reset the `agentSilenceSeconds`
  quiet-badge timer (audit #8) so the two indicators don't contradict.
- i18n: EN + DE keys for all labels.

### 4.6 Live token counters (mockup bottom-right panel)

Data already exists per turn (`persistent_graph.py:1147`:
`turn_metrics {input_tokens, output_tokens, latency_ms}`) but is
persisted-only. Slice 5 adds:

- `usage.updated` frame after **each main-LLM call** inside a turn
  (the accumulator already sits in `_execute_turn`):
  `{turn, input_tokens, output_tokens, reasoning_tokens?, ctx_used_tokens,
  ctx_limit_tokens}` — reasoning tokens best-effort where the provider
  reports them (`completion_tokens_details`).
- `turn.completed` gains the final metrics object (today it sends bare
  `turn_id`, `persistent_app.py:2966`).
- Cockpit panel (composer corner per mockup): cumulative INPUT / REASONING /
  OUTPUT for the running turn + CTX fill gauge (`ctx_used/ctx_limit`) +
  turn elapsed. CTX gauge doubles as the at-a-glance "compaction will trigger
  soon" hint.
- Same numbers feed the audit rows from fix #14 — one accumulator, two sinks.

### 4.7 Prevention siblings (audit #5 + #6)

These shrink the pathological inputs so the engine rarely sees a 950k backfill:

- **#5 — token-derived tool-result caps.** Bulk readers cap output at
  `min(tool_cap, ~15% × main_window)` (cheap char-estimate is fine here);
  `read_pdf` derives its *page budget* from that instead of a fixed count;
  responses end with an explicit continuation hint (`[showing pages 1–40 of
  1355 — call again with page_start=41]`). Audit the other bulk readers
  (file read, SQL formatters, web fetch) for the same cap.
- **#6 — keep-window elision.** New final stage in the force-path of
  `ensure_within_limits`: when still over the main limit after summarization
  (because `keep_recent` + tool pairing protect giant recent results), replace
  the *content* of the largest keep-window tool results with
  `[tool result elided: N tokens — re-read the file if needed]`, preserving
  pairing. Runs before `_emergency_truncate_tool_results` (which stays as the
  last resort). Recent agent-scaffold research ("observation masking") finds
  tool-output elision matches full summarization quality at half the cost —
  this is the cheap tier, not a hack.

## 5. What changes where

| Area | Files | Change |
|---|---|---|
| Engine | `src/core/summarizer.py` (new), `src/core/context.py` | plan-then-fold engine; delete `_recursive_summarize`, `_split_into_chunks`, unstructured fallback; `summarize_conversation` delegates |
| Budget | `src/core/loader.py`, `src/services/auxiliary.py`, `src/agent.py`, `src/api/persistent_app.py`, `src/graph.py:3737` | delete derived leaves; `max_context_tokens` attr + pre-flight guard |
| Events | `src/core/context.py` (callback), `src/api/persistent_app.py` (`_broadcast` wiring, extend `_record_compaction`), `src/graph.py` (log adapter) | 4 frames |
| Cockpit | `persistent-chat.service.ts`, new `compaction-block` component, status strip, `persistent-chat.component.ts`, i18n EN/DE | animation UI |
| Counters | `src/persistent_graph.py`, `persistent_app.py`, cockpit panel component | `usage.updated` + panel |
| Prevention | `src/tools/workspace/files.py` + bulk-reader audit, `src/core/context.py` | #5 caps, #6 elision |
| Tests | `tests/test_context_safety.py`, new `tests/test_summarizer.py`, provisioned cockpit specs | planner is pure → heavy unit coverage; fold loop with scripted aux failures |

## 6. Slices (independently shippable, in order)

| Slice | Contents | Closes | Est. |
|---|---|---|---|
| **S1 — budget correctness** | aux window attr + call-time budget + exact tokenizer + config-leaf deletion + aux pre-flight guard | #7 | 0.5–1 d |
| **S2 — engine** | `summarizer.py` plan-then-fold, retries/backoff, typed failure, fallback deletion | #4-full | 1–1.5 d |
| **S3 — progress UI** | event frames + journaling + cockpit block/strip/i18n | mockup | 1–1.5 d |
| **S4 — prevention** | #5 tool caps + #6 keep-window elision | #5, #6 | 1 d |
| **S5 — token counters** | `usage.updated` + `turn.completed` metrics + cockpit panel | counters | 0.5–1 d |

S1 alone stops the structural overflow (chunking re-engages at the right
size). S2 depends on S1. S3 depends on S2. S4 and S5 are independent of each
other and of S3.

## 7. Acceptance / verification

Unit gates: planner produces within-budget chunks for adversarial inputs
(one 600k message; 10k tiny messages; exact-boundary sizes); fold loop
survives scripted 503-flap (succeeds on retry), aborts cleanly on permanent
failure with history intact; no remaining reference to the deleted config
leaves (`grep summarization_safe_limit` → only migration notes).

Live (extend `docs/tests/session_silent_failure_audit_verification.md` §4
or add a sibling runbook at implementation time):

1. **Recreate `1f39a5a6`:** big-context main model, grow the conversation
   large (giant PDFs), `/compact` → expect the N-pass animation, passes
   advancing, summary lands, `thread_events` contains the full frame
   sequence; reload mid-fold → block reconstructs from replay.
2. **Recreate `b60166ee`:** 128k main model + 4 giant PDFs → S4 caps keep the
   request under limit in the first place; with caps force-disabled, #6
   elision shrinks below limit after summarization.
3. **Aux outage:** dead aux endpoint (Admin → Models) mid-fold → `(retry i/3)`
   visible → `compaction.failed` ⚠ line → history intact → turn proceeds or
   errors visibly (#2), never silently.
4. **Counters:** panel matches `llm_requests` audit rows (#14) for the same
   turn within rounding.

## 8. Open questions

1. **Incremental pre-folding** (keep the running summary warm at turn/phase
   boundaries instead of backloading one giant fold) — natural v2 once the
   fold engine exists; changes trigger cadence, so explicitly out of v1.
2. **Parallel fold opt-in** for healthy/high-capacity aux endpoints — the
   planner already produces independent chunks; a `fold_concurrency > 1`
   config could map-reduce the first level. Deferred until an actual
   wall-clock need shows up.
3. **`/compact focus=…`** — `_handle_compact` accepts a focus arg that goes
   unused; the fold prompt could honor it. Cheap to add in S2, decide then.
4. **Coordination:** S1 touches `src/services/auxiliary.py`, shared ground
   with the in-flight memory-overhaul work (`agent_memory_overhaul.md`
   Phase 3) — sequence the merges, the diff surface is small.

---

## 9. Follow-up: the duplicate-banner bug (found + fixed 2026-06-12)

Live verification on k3d (thread `51c71e83`, softDsim review) surfaced four
chained defects around how compaction results are adopted and rendered. One
manual `/compact` at 13% ctx produced **four** identical `role='summary'` rows
(seq 359/362/365/368, identical text, stale `boundary_seq=320`), a doubled
"compacting" animation, and banners rendering below the reply they preceded.

**Root causes and fixes:**

1. **Marker leak (agent)** — `_handle_compact` assigned the raw reducer delta
   (`RemoveMessage` markers included) into `_session.messages`. Every later
   LLM call then "shrank" after `strip_removal_markers`, and the loop's
   length-delta heuristic re-detected a compaction and re-persisted the same
   summary row — once per LLM call, forever. *Fix:* strip markers before
   adopting, and replace the length heuristic everywhere with
   `ContextManager.compaction_runs` — a monotonic counter bumped only when a
   summarization actually produced a compacted result. Transports snapshot it
   around the call (loop, manual `/compact`, resume Path B).
2. **No-op `/compact` re-persisted the previous summary** —
   `extract_summary_text` found the *old* summary message in the live list.
   *Fix:* the counter gate; a no-op now answers the requesting client with a
   summary-less `context.compacted` (cockpit renders "Nothing to compact",
   no banner, no row).
3. **Manual completion never journaled** — `context.compacted` for `/compact`
   went `_ws_send`-only, so SSE replay could resurrect the progress block
   (journaled started/progress) with nothing to clear it, badge defaulting to
   "auto". *Fix:* real manual compactions broadcast (journaled) like auto;
   every engine progress frame now carries `trigger` so replay-synthesized
   state never guesses; the redundant local "Compacting context..." echo was
   removed from the cockpit.
4. **Banner trailing the turn (cockpit)** — `historyToTurns` anchors a turn's
   block at its first `ai` row, so mid-turn summary rows (correct seq
   position) rendered *after* the whole turn's content. *Fix:* a summary row
   whose turn is already open becomes an inline `CompactionEvent` in the
   turn's event stream at its true position; between-turn rows stay top-level
   dividers; consecutive identical summaries collapse (renders pre-fix
   threads sanely).

**Plus the deferred adopt-into-live-list change (open question of the
ephemeral-prepared design):** auto-compaction now runs on the durable session
list *before* the per-call copy + transient memory/knowledge injections, and
adopts its result (`messages[:] = stripped`) when the counter says a real
summarization ran. Ends the re-summarize-every-call thrash ("5–21 summary
rows/turn" incidents); injections can no longer be folded into a durable
summary. Substitution-only/elision results stay per-call (failure paths never
durably destroy content). Safe now that persistence is message-granular
(thread_messages keeps the full log; resume Path A loads summary + tail).

Tests: `TestCompactionRunCounter`, `test_progress_frames_carry_trigger`
(context safety); `test_compact_strips_removal_markers`,
`test_noop_compact_does_not_persist_marker` (persistent app);
`test_compaction_adopts_result_into_session_list`,
`test_passthrough_does_not_fire_compaction_side_effects` (persistent graph);
in-turn placement + dedupe + no-op + trigger-synthesis specs (cockpit).

**Live verification complete (2026-06-12, k3d thread `e9699503`):** no-op
manual `/compact` → transient "Nothing to compact" line, zero summary rows,
agent log `summarized=False`; real manual `/compact` → journaled
`compaction.started`/`progress`/`context.compacted` (trigger=manual,
47→13 messages), exactly **one** summary row (seq 491, fresh
`boundary_seq: 464`), and two subsequent turns produced no duplicate rows.
Pre-fix artifact note: this thread carries two journaled `compaction.started`
frames (seq 383/387) with no terminal frame — size-guard skips from before
`compaction.skipped` shipped; harmless (fresh connects start at the journal
tail) but explains a ghost "COMPACTING" block if a stale cursor ever replays
past them.

A sibling render-layer bug found in the same thread (gemma reasoning bubble
after the answer + duplicated on replay — same journal-vs-history seam, no
data duplication) was filed and fixed separately (commit `20916662`,
2026-06-13):
`docs/done/persistent_chat_reasoning_after_answer_and_replay_duplication.md`.

## 10. Follow-up: token-counter UI refinement + reasoning estimate (2026-06-18 → 06-23)

A second pass on the §4.6 live counters, prompted by the panel reading as
"unfinished and out of place" in real sessions. Three UI changes plus one
infrastructure blocker.

**10.1 Composer-bar alignment (shipped, commit `9da9ad17`).** The panel lived in
`.composer-wrap` (full-width, 36px padding) with no width cap, so
`justify-content: flex-end` right-aligned it to the *viewport* edge — floating in
the right margin, detached from the composer, which is capped at
`--chat-content-width` (700px) and centered. (That unconstrained full-width flex
also produced a stray `%` artifact floating on the left.) *Fix:* constrain
`.usage-panel` to the same `max-width: var(--chat-content-width); margin: 0 auto`
as the composer so it sits flush above the input. Verified on k3d — panel and
composer right edges align to the pixel.

**10.2 Reasoning-token estimate for providers that omit it (implemented,
unit-verified).** §4.6 promised reasoning tokens "best-effort where the provider
reports them" — but self-hosted gemma via vLLM streams reasoning *text* while
folding it into `output_tokens` and reporting
`completion_tokens_details.reasoning_tokens: 0`, so the reasoning chip stayed
permanently hidden for the most-used local model even though the thinking is
visibly captured. Capturing reasoning *text* (the `reasoning_chat.py` SSE tap →
`additional_kwargs.reasoning_content`) is a **separate path** from the provider's
token *count*; holding the words means we can count them ourselves.

- **Agent** (`src/persistent_graph.py`): `_on_reasoning_delta` now also
  accumulates streamed reasoning into `_reasoning_buf`. After `turn_metrics` is
  built, the new module helper `_maybe_estimate_reasoning_tokens(turn_metrics,
  reasoning_text)` fires when the provider gave no `reasoning_tokens`: it
  tokenizes the buffer (falling back to `additional_kwargs.reasoning_content`)
  via `summarizer.count_text_tokens` (tiktoken/cl100k), **clamps to
  `output_tokens`** (reasoning is a subset, never additive — a different
  tokenizer could otherwise overshoot the provider's output count), sets
  `reasoning_tokens`, and flags `reasoning_estimated=True`. Rides the existing
  `usage.updated` frame (new `reasoning_estimated` field).
- **Cockpit**: `UsageState.reasoningEstimated` (sticky across a turn, resets per
  turn); the chip renders `~70` with a tooltip
  (`chat.usage.reasoningEstimatedHint`, en + de) — "estimated from the captured
  reasoning text, counted within Output."
- **Tests**: `TestMaybeEstimateReasoningTokens` (estimate+flag, clamp, two
  no-ops) in `tests/test_persistent_graph.py`; reducer set/sticky/reset specs in
  `persistent-chat.service.spec.ts`. 105 backend + 130 cockpit green.

**10.3 Context-bar restyle (implemented, harness-rendered).** The flat,
equal-weight mono strip with an *inverted* ramp (accent-red at rest → gold "hot"
≥80%) became a hierarchy: per-turn token counts demoted to compact
`k`-formatted chips (`formatTokens`), the **context-window fill promoted to a
colour-ramped hero** (divider + larger %), driven by a new `usageCtxLevel()`
computed — a correct 3-tier ramp on real theme tokens: `--info` (ok) →
`--warning` (warn ≥75%) → `--danger` (danger ≥90%). Mirrors the "Variant B"
mockup's content model (ctx-% hero, demoted counts) but as a *horizontal* bar,
not the vertical side-rail — the side-rail fights the 700px centered reading
column. Thresholds are static for now; tying warn/danger to the actual
compaction trigger (so "danger" literally means "compaction imminent") is the
obvious refinement.

**10.4 Blocker: LiteLLM-routed models surface no usage at all.** Live
verification of 10.2/10.3 is blocked because both reachable local models are
unusable for it: `gemma-4-moe-strix` 401s (expired endpoint key — the known
persistent main-model key issue), and `gemma-4-moe` (via the LiteLLM gateway)
emits **no `usage.updated` frame at all**, so the whole bar (input/output/ctx
*and* the reasoning estimate) stays hidden for it. Curling the gateway proves it
returns usage only when `stream_options.include_usage` is set — which SRW's
`stream_usage=True` is supposed to send (verified in langchain source) — yet the
usage chunk never reaches `response.usage_metadata`. Characterized with evidence
+ close-out steps in `docs/issues/litellm_streaming_usage_not_surfaced.md`
(prime suspect: the `AsyncReasoningCapturingClient` httpx tap dropping the final
usage-only chunk). **This is the keystone**: closing it renders the live bar for
every LiteLLM-routed model and unblocks the 10.2/10.3 screenshot.

**Status:** 10.1 shipped (`9da9ad17`); 10.2 + 10.3 implemented + test-verified,
live render pending 10.4; 10.4 characterized + filed, unfixed. The 10.2/10.3
changes are on the working tree, uncommitted as of 2026-06-23.
