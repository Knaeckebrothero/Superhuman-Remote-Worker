# Context Token Accounting — truth-anchored estimation, per-family image tokens, image-safe recovery

**Status:** S1+S2+S3+S4 implemented (2026-06-13/14) · **live-verified 2026-06-14** against real post-fix session `0ed8c0e0` (gpt-5.5, 10 multi-page PDFs → no compaction, turn completed; see verification runbook §6) · originally 2026-06-13
**Verification:** §3–§8 + the formula appendix were refined 2026-06-13 against a 19-agent codebase+web research pass (Workflow `w7c2inj8v`): every provider image-token formula was re-derived from its official primary source, and the load-bearing codebase claims were checked against `src/`. Material corrections to the first draft are flagged inline with **⚠**.
**Related:**
- `docs/tests/context_token_accounting_verification.md` — **the verification runbook**: how to test all four slices (unit suites, the per-family estimator oracle, k3d probe-pod recipes, the live-session gold standard).
- `docs/issues/multimodal_image_context_explosion.md` — the root-cause defect inventory (defects A–D, layers). This feature doc is the forward-looking *architecture*; that issue doc remains the detailed bug catalogue. Slices here subsume its Layer-A/C/0 work.
- `docs/features/multimodal_image_cost_optimization.md` — the deferred cost/estimator knobs (per-family render DPI, ingestion downscale, OpenAI multiplier, o4-mini mapping, Gemini API-vs-Vertex, auto-calibration). All low-priority tuning, not correctness; §8's open knobs live there now.
- `docs/features/context_summarization_rework.md` — the aux-budgeted rolling-fold summarizer (`src/core/summarizer.py`, shipped S1+S2). This doc fixes the *input* that feeds it and the *trigger* that invokes it.
- `docs/features/db_backed_model_catalog.md` / `config/model_config_matrix.yaml` — the per-family settings matrix this doc extends.

---

## 1. Problem

We have two token meters that disagree by ~290×, and a context-compaction path that wedges sessions outright. Both trace to a single missing capability: **the stack has no image-token accounting**. Every token counter stringifies multimodal message content (`str(msg.content)`) and tokenizes the embedded base64 image data as if it were text.

### 1.1 Reproduction — dev session `5dbb5770` (2026-06-13)

`gpt-5.5` via codex-proxy, `persistent_defaults`, the session was given only the `read_file` tool. The user asked it to check 12 regulatory/SDS PDFs in the compliance cloud folder. Each `read_file` on a PDF attaches full-page rendered images and injects an `"Image content from tool call …"` `HumanMessage`.

**Symptom 1 — the token numbers are nonsense.** Two meters, ~290× apart:

| Display | Value | Source | Correct? |
|---|---|---|---|
| `COMPACTING … 9188.6k / 1050.0k · 875%` | 9,188,623 "tokens" | internal counter `str(content)` over base64 (`context.py:448/502/706`) | ❌ base64-as-text |
| bottom bar `INPUT 31,744 · CTX 3%` | real `input_tokens` | provider `usage_metadata` (`persistent_graph.py:1284`) | ✅ |

Proof it's a measurement artifact, not real context: the persisted `thread_messages` for this thread total **93,105 chars with zero base64** (`has_b64 = 0`). The base64 exists only in live memory — 12 `HumanMessage`s (seq 4638…4667), each carrying a full-page render, flattened to a 61-char marker at persist. The real conversation is ~31k tokens against a 1.05M window = **3%**. The model request would have succeeded; nothing actually overflowed.

**Symptom 2 — summarization fails 3/3 and wedges the session.** The phantom 9.18M trips compaction. Then:

1. `_format_messages_for_summary` (`context.py:1259`) builds summary input with `f"User: {msg.content[:500]}"`. For a multimodal `HumanMessage`, `content` is a **list**, so `[:500]` slices the list (a no-op) and the f-string stringifies the entire base64 blob into the summarizer input.
2. The (correct) rolling-fold planner sizes chunks to the aux model's window: **5,499,648 tokens → 60 passes × ~96k** (chunk budget 96,077, gemma's 131,072 window). Every pass therefore ships ~96k tokens of base64 to `gemma-4-moe`.
3. Each fold call hits the **240s timeout** — the timestamps are exact: `11:44:15 → 11:48:15 → 11:52:20 → 11:56:35` (240s + the 5s/15s backoffs). The error logs as `failed ()` because `asyncio.TimeoutError` stringifies to empty (`summarizer.py:485`).
4. 3 timeouts → `SummarizationFailed("aux_unavailable")` → compaction aborts, keeps 64 messages → next turn re-counts 9.18M and re-triggers → **turn 1 never completes** (`total_tokens = 0`, wedged 20+ min).

The aux endpoint is healthy — measured this session: `/v1/models` → 200 in 77ms, `/v1/chat/completions` → 401 in 11ms. So the timeout is **input-driven** (base64 chokes a text model), not an outage.

### 1.2 How it came to be (so the fix targets the right layer)

- Images are sent correctly. `make_image_content_block_from_b64` (`image_content.py:87`) emits an OpenAI-compatible `{"type":"image_url","image_url":{"url":"data:image/png;base64,…"}}` block; LangChain's per-provider translators reshape it. The provider's vision encoder bills it as a fixed, dimension-based count (~hundreds/page) — *not* by base64 length. That's why the real `input_tokens` is small. **The base64 is only a problem in the two places we stringify the structured content ourselves: the local counters, and the summarizer-input builder.**
- The summarizer rework (`summarizer.py`) is **not** the regression. It plans within budget, retries with backoff, and on exhaustion cleanly keeps the raw history. It simply *inherited* the upstream base64 leak via `_format_messages_for_summary`, and faithfully amplified it into 60 doomed passes.
- The recovery ladder already exists but is blind to images. `ensure_within_limits` (`context.py:932`) tiers elide-tool-results (`:1063`) → emergency-truncate (`:1116`) → summarize (`:1444`); the worker even catches `ContextOverflowError` → emergency-compact → retry (`graph.py:1747`). But the elide/truncate nets filter on `ToolMessage` only, so they **cannot shed the image `HumanMessage`s** — the bloat falls straight through to the summarizer, which then times out.

---

## 2. Goals

1. **One honest meter.** The internal context number converges with the provider's `input_tokens`; the "875%" phantom disappears. No compaction is ever triggered by image content counted as text.
2. **Recoverable overflows.** When a context *genuinely* approaches the window, compaction/summarization succeeds — the recovery path is image-aware (strips/sheds images) instead of feeding base64 to the aux model.
3. **Per-family accuracy with a safe default.** Image-token cost is configured per model family in the existing matrix, with a conservative `default` middle-ground. New families onboard via YAML, no code change.
4. **Stop wasting the budget at the source.** Don't rasterize text-extractable PDF pages — the agent already has their extracted text, which is both cheaper and more faithful.

### Non-goals

- **Perfect token precision.** We re-anchor on the provider's real count every turn, so the estimate only spans the gap between two calls. Biased-high and order-of-magnitude is sufficient (see §4.1).
- **Teaching the summarizer about images.** The summarizer should never *see* an image — it strips them. Summarizing image content is out of scope.
- **A universal cross-provider image tokenizer.** Image cost is a function of dimensions + the provider's tiling/patching; a small per-family formula keyed on width×height is enough.
- **Per-model image overrides.** Image cost is a property of the model's vision encoder = a family trait. Family granularity (matrix `settings`) is correct; the per-model DB `context_window` override stays text/window-only.

---

## 3. Decisions

1. **Truth is the provider's `input_tokens`.** We already capture it (`persistent_graph.py:1284`). It becomes the authoritative current-context size. **⚠ Corrected:** the *full-history `str(content)` recount* is retired as the trigger source — but the *bounded per-delta estimate* stays. The delta is what makes self-healing work for messages appended this turn (including the ephemeral memory/knowledge **injection pairs** that live on the per-call `prepared` copy, never on `messages`). Don't conflate the two: kill the full recount, keep the delta.
2. **The estimate covers only the delta** since the last call, and is **biased high**. Over-estimating compacts slightly early (safe); under-estimating risks a wasted overflow round-trip. Self-heals at the next call when truth re-anchors.
3. **The API error is a backstop, not the primary trigger.** A proactive biased-high estimate avoids shipping megabytes of base64 just to be rejected. The real `ContextOverflowError` → compact → retry loop catches the rare under-estimate.
4. **Image cost lives in `config/model_config_matrix.yaml`** `settings`, beside `multimodal` / `model_max_context_tokens`, with a `default` flat fallback. **⚠ Corrected:** family keys are the **hyphenated** `family_of()` strings (`gpt-5`, `codex`, `o-series`, `gemini`, `gemma`, `claude-opus`, `minimax-m3`) — not underscores, and there is no bare `claude` key. And it **must be routed through `LimitsConfig`, not `LLMConfig`**: `_parse_llm_config` (`loader.py:1534`) is a closed constructor that silently drops unknown keys (see §5).
5. **Recovery is image-aware.** The summarizer-input builder replaces image blocks with a marker; elision can shed image `HumanMessage`s, not just `ToolMessage`s. **⚠ New constraint:** elision *mutates the live list*, so it must run **only at actual compaction time** (or on a copy) — never as speculative per-turn hygiene, because adding/removing an image invalidates the prompt cache (§4.4, §8 risks).
6. **Prevention beats accounting.** Text-extractable PDF pages aren't rendered at all. **⚠ Corrected:** the gate uses **pdfplumber's per-page text** (PyMuPDF/`fitz` is not a dependency), and render DPI is **per-family** (downscaling helps patch/area models but *increases* tile counts for tile-mode models — §4.5).

---

## 4. Architecture

### 4.1 The estimation model — truth + biased-high delta

```
trigger_tokens ≈ last_provider_input_tokens
               + Σ estimate(msg) for msgs appended since the last provider call
               + injection_overhead          # memory/knowledge pairs added to `prepared`, not `messages`
```

**Seam (verified):** there is no provider-truth slot today — `current_token_count` is written only internally, and `_loop_on_usage` (`persistent_app.py:2989`) only broadcasts SSE. Add:
- `last_provider_input_tokens: Optional[int] = None` + `record_provider_usage(n)` setter on `ContextManagementState` (`context.py:337-349`).
- Call `context_manager.record_provider_usage(turn_metrics["input_tokens"])` in `_execute_turn` (`persistent_graph.py:1303`, right after `turn_metrics` is assembled, before the `on_usage` callback). `context_manager` is already a named arg of `_execute_turn` — no callback threading. **Guard against `None`** so an empty-usage turn never overwrites the anchor with 0.

`estimate(msg)`:
- **text** → existing `get_token_counter(model)` (tiktoken-by-family, `context.py:513`). Slight over-count is fine.
- **image block** → the per-family image estimator (§4.3), biased high.

When `last_provider_input_tokens is None` (first turn of a session, or provider returned empty usage), **fall back to the existing `get_token_count` path** so behavior degrades gracefully.

**Self-healing:** because we re-anchor every turn, an estimate error only has to survive one turn. The estimator need not be accurate — only biased-high and roughly right. This is what makes "rough estimate + fallback" safe.

This is a **left-hand-side-only** change: the trigger compares `trigger_tokens` against the *unchanged* `config.limits.context_threshold_tokens` (= `window × 0.80`, see §8 resolved). The bottom-bar live gauge keeps showing raw provider `input_tokens`. After the change the two meters converge.

### 4.2 The matrix schema (new `settings.image_tokens`)

**⚠ The first draft's YAML was wrong** (key `gpt_5`, mode `openai_tiles` with GPT-4o's `85/170`, a bare `claude` key, stale `anthropic_area`/`gemini_tiles`). Corrected:

```yaml
# config/model_config_matrix.yaml  — keys are the hyphenated family_of() strings
default:
  settings:
    image_tokens: { mode: flat, flat: 1600 }       # unknown family / unreadable dims → never str(content)

gpt-5:               # family_of("gpt-5.5") → "gpt-5"  (NOT codex; 'codex' not a substring of 'gpt-5.5')
  settings:
    image_tokens: { mode: openai_patches, patch_px: 32, budget: 10000, budget_high: 2500, flat: 2500 }

codex:               # gpt-5.3-codex — secondary multimodal family, window 400k
  settings:
    image_tokens: { mode: openai_patches, patch_px: 32, budget: 10000, budget_high: 2500, flat: 2500 }

o-series:            # o1/o3/o4
  settings:
    image_tokens: { mode: openai_tiles, base: 75, per_tile: 150, tile_px: 512, flat: 975 }

claude-opus:         # Opus 4.7/4.8 — two-tier cap
  settings:
    image_tokens: { mode: anthropic_patches, patch_px: 28, max_edge: 2576, max_tokens: 4784, flat: 4784 }

claude-sonnet:       # NEW — homelab interactive main model; currently falls to default (multimodal:false!)
  settings:
    multimodal: true
    image_tokens: { mode: anthropic_patches, patch_px: 28, max_edge: 1568, max_tokens: 1568, flat: 1600 }

claude-haiku:        # NEW — same legacy cap as sonnet
  settings:
    multimodal: true
    image_tokens: { mode: anthropic_patches, patch_px: 28, max_edge: 1568, max_tokens: 1568, flat: 1600 }

gemini:              # ai.google.dev (NOT Vertex) bills flat ~258 at MEDIUM default
  settings:
    image_tokens: { mode: flat, flat: 2304 }        # biased-high (256 + Pan&Scan upside); 258 = exact-MEDIUM

gemma:               # self-hosted vLLM gemma-4-* — text-only summarizer post-S3; forward-looking only
  settings:
    image_tokens: { mode: flat, flat: 320 }         # Gemma 4 default 280 × ~1.14 bias (256 is Gemma 3)

minimax-m3:          # no public per-image formula found — inherit default until calibrated
  settings:
    image_tokens: { mode: flat, flat: 1600 }
# gpt-4o (if ever used for vision): { mode: openai_tiles, base: 85, per_tile: 170, tile_px: 512, flat: 1105 }
```

### 4.3 The estimator (`src/core/image_tokens.py`, new)

Pure function, no LLM, no image decode. Read dims from the bytes already in hand, then apply the family `mode`.

**Dimension reading (⚠):** PNG — `struct.unpack('>II', data[16:24])` (fixed IHDR offset). JPEG — **scan** for an SOF marker (`0xFFC0`/`0xFFC2`…), skip 3 bytes, read 2-byte BE height then width (not a fixed offset). **Do not use `imghdr`** — removed in Python 3.13, which this stack runs. Unreadable dims → family `flat`; unknown family / no block → `default.flat`.

**Modes:**
- **`openai_patches`** (`gpt-5`, `codex`): `p = ceil(w/32)·ceil(h/32)`; if `p ≤ budget` then `patches = p`, else `shrink = sqrt(32²·budget/(w·h))`, `patches = ceil(floor(w·shrink)/32)·ceil(floor(h·shrink)/32)`. **`budget` is detail-dependent and the default is the *expensive* one:** `gpt-5.5` with no `detail` set defaults to `"original"` → `budget = 10000`; only `detail:high` drops it to `2500`. Biased-high ⇒ use `10000` unless `detail:high` is forced. No published per-patch multiplier for `gpt-5.5` ⇒ tokens = patches (×1.0).
- **`openai_tiles`** (`o-series`, `gpt-4o`): resize to fit `2048×2048`, then scale shortest side to `768`; `tiles = ceil(w/512)·ceil(h/512)`; tokens = `base + per_tile·tiles` (or `base` flat for `detail:low`).
- **`anthropic_patches`** (`claude-*`): `tokens = ceil(w_r/28)·ceil(h_r/28)` where `(w_r,h_r) = resized_size(w,h,max_edge,max_tokens)` is Anthropic's published binary-search resize (satisfies `ceil(side/28)·28 ≤ max_edge` per side **and** `ceil(w/28)·ceil(h/28) ≤ max_tokens`). **Port Anthropic's reference `resized_size()` verbatim.** Per-generation caps: legacy (Sonnet 4.6 / Haiku / Opus ≤4.6) `max_edge=1568, max_tokens=1568`; Opus 4.7/4.8/Fable 5 `max_edge=2576, max_tokens=4784`.
- **`flat`** (`gemini`, `gemma`, `minimax-m3`, `default`): the configured constant.

Worked example, a ~1700×2200 page: `gpt-5` **2508** (high) / **3726** (original) · Claude legacy **1530** / Opus 4.7+ **4758** · gpt-4o **1105** · o-series **675**. (Twelve such pages ≈ 30–45k for gpt-5.5 — consistent with the real **31,744** in `5dbb5770`.)

### 4.4 Image-safe recovery (S3 — **IMPLEMENTED 2026-06-14**)

- **Summarizer input** — `_format_messages_for_summary` fix all three arms: `HumanMessage` (`context.py:1259`), `AIMessage` (`:1262`), `ToolMessage` (`:1281`). When `content` is a list, walk parts and replace each `image_url` block with `[image: <mime>, ~<est> tok]`; **keep** the existing `[:500]`/`[:800]`/`[:300]` *character* truncation for string content (don't weaken it). This builds a separate string list, so it is **cache-safe by construction**.
- **Elision** — the `isinstance(msg.content, str)` guard at `context.py:1082` is the specific blocker. Generalize `_elide_largest_tool_results` (and `_emergency_truncate_tool_results` `:1139`) to also accept image `HumanMessage`s as candidates, sized by the §4.3 estimator (or the flat default — precision is irrelevant for sort order). Treat image `HumanMessage`s as **always-elision-candidates** (ignore the ToolMessage recency window — they're lossless to shed, the model already saw them) and **elide images before tool results** (preserves more tool content). Keep the change **additive** so the ToolMessage `tool_call_id` pairing-safety contract is untouched.
- **⚠ Cache-thrash constraint:** elision mutates the *live* `messages[]` that flows into the next provider call. Adding/removing any image invalidates the Anthropic messages-layer cache and tanks OpenAI's hit-rate to 0% on first base64 introduction. So **only elide at actual compaction time**, never as a per-turn step — otherwise a `claude-sonnet` (homelab main) session re-pays cache writes every turn and costs more than it saves.
- **Cosmetic** — name `asyncio.TimeoutError` explicitly in `summarizer.py:485` so a timeout never again logs as `failed ()`.

### 4.5 Prevention (S2 — root removal)

Gate in `src/tools/workspace/files.py` `_read_visual_document`, **per page** (not a file-level decision):

- **Signal:** pdfplumber's per-page `extract_text()` — already computed in `read_pages()` (`pdf.py:184`) but currently **discarded**. Either extend `PDFReader.read_pages()` to return `page_char_counts`, or reopen the pdfplumber handle inside `_read_visual_document`. **Do not add `fitz`/PyMuPDF** — only `pdfplumber` + `pdf2image` are in `requirements.txt`.
- **Rule:** skip rasterizing a page when `len(text) ≥ MIN_TEXT_CHARS` **and** the page is not image-dominant (use `page.images` non-empty as the without-fitz proxy: render only when `chars < MIN_TEXT_CHARS` **and** `page.images`). Start `MIN_TEXT_CHARS = 100` (~25 words) — **but validate against the actual 12 SDS PDFs from `5dbb5770`**: regulatory pages carry mandatory GHS pictograms/signal-word boxes, so a page with 200 chars of body text *plus* a critical hazard diagram must still render.
- **DPI is per-call, per-family (⚠).** `render_pdf_page(path, page, dpi=…)` already takes a per-call `dpi` (`document_renderer.py:122`); set it per call, **not** on the module-level `get_document_renderer()` singleton (shared across concurrent callers). Expose `settings.pdf_render_dpi` as a **separate** key from `image_tokens` (render resolution ≠ token accounting). **Downscaling is not uniformly good:** for OpenAI *tile-mode* models the shortest-side→768 upscale means dropping a 150-DPI A4 to 96 DPI *increases* tiles (gpt-4o 4→6 tiles, 765→1105). Lower DPI helps Claude/patch models; it's neutral-to-harmful for tile-mode — hence per-family.
- **Scope limit:** PPTX/DOCX render via a LibreOffice-produced PDF with no pre-rasterization text pass, so the gate can't apply there — document it.

---

## 5. What changes where

| Area | File:line | Change |
|---|---|---|
| Provider-truth slot | `context.py:337-349` | `last_provider_input_tokens` field + `record_provider_usage()` setter |
| Truth call site | `persistent_graph.py:1303` | `record_provider_usage(turn_metrics["input_tokens"])`, None-guarded |
| Trigger (primary) | `context.py:932` `ensure_within_limits` | compare anchor+delta, not `str(content)` recount |
| Trigger (msg-count branch) | `context.py:680-684` | same anchor-based estimate |
| Trigger (banner log) | `context.py:960-963` | log the anchor estimate, not the phantom recount |
| Trigger (worker) | `graph.py:1145` layer-1 safety | **deferred** — worker has no usage feedback; relevant only if worker gets multimodal (see §6 out-of-scope) |
| Estimator | new `src/core/image_tokens.py` | dimension reader + per-mode formulas |
| Matrix schema | `config/model_config_matrix.yaml` | `settings.image_tokens` per family + `default`; **add `claude-sonnet`/`claude-haiku` rows** (currently fall to `multimodal:false`) |
| Matrix → config (⚠) | `loader.py` | route via `LimitsConfig`: (1) `elif key=='image_tokens': data.setdefault('limits',{})['image_tokens']=value` in `_apply_settings_matrix` (`:591-596`); (2) `image_tokens` field on `LimitsConfig` dataclass (`:1209`); (3) `image_tokens=limits_data.get('image_tokens')` in **both** inline constructors (`:1819`, `:2019`). **Not** via `LLMConfig` (drops unknown keys). |
| Summarizer input | `context.py:1259/1262/1281` | replace image blocks with markers; keep char truncation |
| Elision | `context.py:1082/1139` | allow shedding image `HumanMessage`s, before tool results, at compaction only |
| Timeout log | `summarizer.py:485` | name `TimeoutError` |
| Prevention | `files.py _read_visual_document`, `pdf.py:184`, `document_renderer.py:122` | pdfplumber per-page gate; per-family `pdf_render_dpi` |

---

## 6. Slices (independently shippable, in order — ordering confirmed by research)

1. **S1 — Truth-anchored trigger.** `record_provider_usage` seam + anchor+delta at the **three** persistent trigger sites (`context.py:932`, `:680-684`, `:960-963`). **No blocking prerequisite** — the codex `stream_usage=True` concern is **refuted** (codex uses Responses-API streaming, which carries usage via the `response.completed` event); just None-guard the setter. *Kills the 875% phantom and all false triggers; would have prevented `5dbb5770` alone (3% never triggers).*

   **Status — IMPLEMENTED 2026-06-13.** Built as (a) an **image-aware counter**: new `src/core/image_tokens.py` (`split_text_and_images` + flat `DEFAULT_IMAGE_TOKENS=1600`) wired into `count_tokens_tiktoken`/`count_tokens_approximate` so image blocks count flat instead of stringifying base64; plus (b) the **provider anchor**: `last_provider_input_tokens` on `ContextManagementState` + `record_provider_usage()` + `_trigger_token_count()` = `max(local, anchor)`, used by `should_summarize`/`should_compact`; called from `persistent_graph._execute_turn` (guarded). **Refinement vs the doc's literal "anchor + delta":** implemented as an *image-aware total + anchor floor* (no message-boundary tracking) — the provider count measures `prepared` (post-compaction + injections + system + schemas) while the trigger checks durable `messages`, so a precise delta would need fragile reconciliation; `max(local, anchor)` is simpler, biased-high, and self-heals each turn. **Bonus:** because the worker's layer-1 safety check (`graph.py:1145`) shares `get_token_count`, the counter fix also covers the worker path — the earlier "worker out-of-scope" note applies only to the *anchor* (worker has no usage-feedback path), not the counting fix. Tests: `tests/test_image_token_accounting.py` (13). The banner log (`:960`) is honest now via the counter fix (left calling `get_token_count`). Secondary base64 leaks in the embedding-query path (`persistent_graph.py:685`, `memory/query.py:34`) are tracked separately (issue doc), not part of the trigger.
2. **S2 — Prevention.** pdfplumber per-page gate + per-family DPI. *Removes the root for document workloads.*

   **Status — page-gate IMPLEMENTED 2026-06-13; per-family DPI deferred to S4.** `src/utils/pdf.py` gains `should_render_page(chars, has_images, min_text_chars=100)` (render iff text-sparse **or** image-bearing → never drops a pictogram page), `page_render_decisions()` (fail-open pdfplumber inspection over the read range), and `compress_ranges()`. `files.py` `_read_visual_document` (PDF branch only) gates the rasterization loop on these and appends a transparency note listing skipped text-only pages. For the 5dbb5770 workload, page 1 (pictograms) still renders, pages 2–N of pure regulatory text are skipped. Tests: `tests/test_pdf_render_gate.py` (11, pure-logic). **Calibrated 2026-06-13** against the real 5dbb5770 PDFs (Cryogel_Z_SDS, OSHA HazCom + Appendix D, TRGS-900 — pulled from workspace `ws-thread-eb989b82`): `page.images` correctly flagged the SDS pictogram page (Cryogel p1) and the TRGS figure page (p9); across **81 pages** every image-free text page had **≥584 chars** (well above the 100 floor), so `MIN_TEXT_CHARS=100` never mis-skips a text page nor mis-renders — **kept at 100**. Net: **81 pages → 2 rendered (~97.5% fewer rasterizations)**. **Residual caveat:** detection is `page.images` (raster); a vector-only pictogram on a text-heavy page would skip — did not occur in the real set, documented in `page_render_decisions`. **Plumbing caveat:** `page_render_decisions` re-opens the PDF (option-b) — fine for chunked reads; threading per-page char counts out of `read_pages` (option-a) is deferred. PPTX/DOCX intentionally ungated (LibreOffice PDF has no pre-rasterization text pass). **Per-family render DPI** (the downscaling-raises-tiles caveat) is bundled into S4 because it needs the matrix→`LimitsConfig` plumbing.
3. **S3 — Image-safe recovery.** Strip image blocks from the three summarizer-input arms; teach elision to shed image messages (at compaction only); fix the timeout log. *Makes genuine overflows recoverable.*

   **Status — IMPLEMENTED 2026-06-14.** Three changes, unit-verified (`tests/test_image_safe_recovery.py`, 24 tests; existing context/persistent suites green at 249):
   - **(a) Summarizer input** — new `content_to_summary_text()` in `src/core/image_tokens.py` flattens multimodal content to text + `[image: <mime>, ~N tok]` markers (base64 never stringified), wired into **all three** `_format_messages_for_summary` arms (Human/AI/Tool) while keeping the existing `[:500]`/`[:800]`/`[:300]` char truncation. This is the direct fix for the `5dbb5770` leak — `msg.content[:500]` was a no-op on list content, so the whole base64 blob flowed into the summarizer prompt.
   - **(b) Elision** — new `has_image_content()` + `ContextManager._shed_image_messages()` replace image `HumanMessage`s with a text marker. `_elide_largest_tool_results` now sheds images **first** (lossless — the model already processed them, and they carry no tool-pairing contract), then the largest tool results; `_emergency_truncate_tool_results` sheds images at the top of the last-resort path (char-truncation can't touch list content). Both callers run only after `summarize_and_compact`, so the **compaction-time-only** constraint (no per-turn prompt-cache thrash, §4.4) holds. Plain user turns and tool `tool_call_id` pairing are untouched.
   - **(c) Cosmetic** — `_describe_exc()` in `summarizer.py` names arg-less exceptions in both the per-attempt warning and the final error, so an aux timeout logs `TimeoutError` instead of the silent `failed ()` from the incident. Ruff clean.
4. **S4 — Per-family image estimator + matrix.** **⚠ Re-estimate UP (~3–4× the first draft):** loader routing arm + `LimitsConfig` field + two constructor edits; **two new modes** (`openai_patches`, `anthropic_patches`) plus `openai_tiles`/`flat`; corrected gemini-flat / gemma-320; **new `claude-sonnet`/`claude-haiku` rows**; per-generation Claude cap. Optionally split **S4a** (gpt-5/codex patch entries + loader plumbing — covers the failing-session family) / **S4b** (claude/gemini/gemma/o-series rows + dimension reader).

   **Status — IMPLEMENTED 2026-06-14.** A second primary-source research pass (Workflow background agent, 2026-06-14) confirmed/sharpened the appendix before building. Pieces:
   - **Estimator** (`src/core/image_tokens.py`): a header-only **dimension reader** (PNG IHDR fixed offset; JPEG SOF segment scan; no `imghdr` — gone in 3.13; decodes only a bounded ≤16k-char base64 prefix) + four modes — `openai_patches` (32px, budget-shrink with OpenAI's published `adj` refinement + `min(patches,budget)` clamp + per-model `multiplier`), `openai_tiles` (2048→768 fit, 512px tiles), `anthropic_patches` (a verbatim port of Anthropic's reference `resized_size` two-constraint binary search), `flat`. Every path biased-high, falling back to the family `flat` then the global `1600` — base64 is never tokenized. `split_text_and_image_blocks` returns the blocks so the counter can read dims; `split_text_and_images` is now a thin wrapper (S1/S3 tests unchanged).
   - **Matrix** (`config/model_config_matrix.yaml`): `settings.image_tokens` on `default` + every multimodal family (gpt-5/codex patches budget 10000; o-series tiles; claude-opus 2576/4784; gemini flat 2304; gemma flat 320; minimax-m3 flat 1600) **plus new `claude-sonnet`/`claude-haiku` rows** (they previously fell through to `default` ⇒ `multimodal:false`, so the homelab interactive main model never counted images — risk #4, now fixed with `multimodal:true` + legacy 1568 cap + real context windows).
   - **Plumbing**: routed via **`LimitsConfig.image_tokens`** (an `_apply_settings_matrix` arm sends `image_tokens` to `limits`, not `llm`; field on the dataclass; both inline constructors) → threaded into `ContextConfig.image_tokens` at both `ContextManager` build sites (worker `graph.py`, persistent `persistent_session.py`) → bound into `get_token_counter(model, image_config)` → the two counting functions. `_parse_llm_config` would have dropped it (closed constructor) — confirmed.
   - **Verified** against the research's primary-source worked examples for a 1700×2200 page: gpt-5 **3726**, claude legacy **1496** (⚠ the v1 doc's 1530 was the looser scale-then-shrink; the exact reference gives 1496), Opus-4.7+ **4758**, gpt-4o **765**, o-series **675**, plus Anthropic's own 1000×1000→**1296**. Tests: `tests/test_image_token_estimator.py` (dimension reader for PNG/JPEG/Anthropic/Responses shapes, every mode, dispatch fallbacks, loader routing, end-to-end `ContextManager`); `test_settings_matrix.py` updated (image_tokens is a passthrough `limits` leaf, dropped before the derived-leaf equality). Ruff clean. Full suite: **6495 pass**; the only S4 regression was `test_curation` passing a bare `AgentConfig` to `ContextManager` (now guarded with `getattr(self.config, "image_tokens", None)` — absent ⇒ flat, never a constructor crash). Two remaining failures are **pre-existing and unrelated**: `test_endpoint_inventory` (stale route manifest from the `feat(datasources)` commit — `/api/datasources/eligible`) and `test_database_phase1::test_connect_disconnect` (env-dependent, needs a live Postgres). **Verified end-to-end on k3d** in the deployed agent image: real matrix → `limits.image_tokens` (openai_patches/10000) → `ContextManager` count = 3732 for a page; claude-sonnet now `multimodal:true` + anthropic_patches; per-mode 3726/1496/4758/675 all exact. **Open:** OpenAI full-model patch→token multiplier is unpublished (using 1.0, biased-high budget covers it); o4-mini is actually patch-based but inherits o-series tiles (unused, biased-low — noted in YAML); Gemini API-vs-Vertex divergence (flat 2304 assumes the API); per-family render DPI (the S2 deferral) still open.

**Out-of-scope follow-ups (file separately, not slices here):**
- Worker usage-feedback path (`graph.py` uses `ainvoke` with no `on_usage`); same seam applies if worker sessions ever get multimodal content.
- MongoDB archiver reads only `response_metadata.token_usage`, so streaming providers log empty `token_usage` in `llm_requests` — independent of S1's trigger fix.

---

## 7. Acceptance / verification (dev cluster)

- **Reproduce `5dbb5770`:** new session, `read_file` only, the 12 compliance PDFs. After S1+S2: internal meter ≈ provider `input_tokens` (within ~10%), CTX stays single-digit %, **no compaction triggered**, turn completes; the COMPACTING banner does not appear.
- **Forced genuine overflow:** lower the threshold (or load a real >1M text context). After S3: summarization completes (no 240s aux timeout), turn completes, summary present + recent tail intact.
- **Estimator calibration (S4):** unit-test each mode against known dims; on dev, log `estimate vs actual input_tokens` per family across real multimodal turns and assert `estimate ≥ actual` (biased-high) within a bound. For Anthropic, the free `/v1/messages/count_tokens` endpoint is an exact, unbilled oracle for image/PDF blocks.
- **Matrix fallthrough:** a multimodal family with no `image_tokens` resolves to `default.flat`, never `str(content)`. A `claude-sonnet` session gets `multimodal:true` + the legacy-cap estimator.

---

## 8. Open questions

> **The deferred cost/estimator knobs (per-family render DPI, ingestion downscale /
> `detail:low`, OpenAI patch multiplier, o4-mini mapping, Gemini API-vs-Vertex,
> auto-calibration) are now tracked as a backlog in
> `docs/features/multimodal_image_cost_optimization.md`.** All are cost/precision
> tuning, not correctness — the trigger re-anchors on the real `input_tokens` each
> turn, so a biased-high estimate only ever compacts slightly early. The notes below
> remain as the design rationale.

### Resolved by the 2026-06-13 research

- **codex-proxy usage fidelity → YES, both paths.** The `ainvoke` fallback reads `response.usage` via `_create_usage_metadata_responses` (this is where the real **31,744** came from); the streaming path also carries usage because `reasoning={…}` makes LangChain use the Responses API, whose streaming emits usage in the `response.completed` chunk — so **`stream_usage=True` is not required**. Caveat: the proxy's Chat-Completions envelope drops `input_tokens_details`, so S4 can't read a provider image-token breakdown — the estimator stays the sole image-cost source.
- **`detail` hint → worse than framed.** `image_content.py:87` sets no `detail`, so `gpt-5.5` defaults to `"original"` (10000-patch budget — the *most expensive* mode), not `high`. The estimator's biased-high default for `gpt-5` must assume 10000. `detail:low` is a valid lever for *scanned* pages (OCR-style recovery), threadable through `_get_visual_content` keyed on the S2 trigger — but S2 (skip text pages) eliminates the dominant cost, making `detail` a secondary knob.
- **gemma calibration → mostly closable without a probe.** Default vLLM (pan-and-scan off) is a fixed 280/image (Gemma 4) or 256 (Gemma 3). Moot for the failure (gemma is the text-only summarizer; post-S3 it sees no images). To fully close: confirm Gemma 3 vs 4 on the homelab and the pan-and-scan flag.
- **Threshold semantics → no re-derivation.** `CONTEXT_THRESHOLD_FRACTION = 0.80` already expresses the threshold as a fraction of the real window (`loader.py:607-614`). S1 is an LHS-only change; keep comparing against the same `context_threshold_tokens` field (which the worker's injection-overhead path mutates in a try/finally) so that path keeps working.

### Still open

- **Auto-calibration loop?** A cheap job comparing per-family `estimate` vs actual `input_tokens` over time, flagging drift when provider tiling/patching rules change. Mitigates the Gemini API-vs-Vertex risk below.
- **Per-model `context_window` drift.** `5dbb5770`'s deployed meter showed 200k while `gpt-5.5`'s family default is 1,050,000 — a catalog-row override mismatch that changes *when* S1 fires (`threshold = window × 0.80`). Verify the catalog row matches the intended window; orthogonal to accounting but interacts with it.

### New risks surfaced by the research

1. **Prompt-cache thrash** — see §4.4; routine image elision would re-pay cache writes for a Claude main model. Constrain elision to compaction-time only.
2. **`gpt-5.5` original-detail default** — every multimodal call pays the 10000-patch tier because no `detail` is set; estimator must assume it, and a `detail:high` hint is the mitigation.
3. **Downscaling increases tiles for tile-mode** — per-family DPI, not a global knob.
4. **`claude-sonnet`/`claude-haiku` absent from the matrix** — Anthropic vision models fall to `multimodal:false`; S4 must add rows + pick the per-generation cap (1568 vs 4784, a ~3× swing).
5. **Gemini API-vs-Vertex billing divergence** (`googleapis/python-genai#1907`, open) — the API bills flat ~258 (MEDIUM) while Vertex bills crop tiles (≤7×). If Google aligns them, real `input_tokens` jumps 4–7× and the flat estimate flips to undercount. Log the endpoint.
6. **vLLM `prompt_tokens`-includes-image-placeholders is plausible-but-unverified** — S1's anchor for a *gemma vision main-model* rests on it; probe one image before hard-depending (low risk today — gemma is text-only).
7. **`MIN_TEXT_CHARS` vs GHS pictograms** — calibrate against the real SDS PDFs; gate on `chars < threshold AND page.images`, not chars alone.

---

## Appendix — Verified provider image-token formulas (2026-06-13, primary-source)

| Family (`family_of`) | Mode | Formula / constants | 1700×2200 | Primary source |
|---|---|---|---|---|
| **gpt-5** (gpt-5.5 ← the failing model) | patches | `p=⌈w/32⌉·⌈h/32⌉`; budget 10000 (no-detail/"original") or 2500 (high); shrink if `p>budget` | 2508 / **3726** | developers.openai.com images-vision |
| codex (gpt-5.3-codex) | patches | same; window 400k | 2508 / 3726 | openai images-vision |
| gpt-4o | tiles | `85 + 170·⌈w/512⌉·⌈h/512⌉` after fit-2048→short-768 | 1105 | openai images-vision |
| o-series (o1/o3/o4) | tiles | `75 + 150·tiles` (o4-mini is patches, budget 1536 ×1.72) | 675 | openai images-vision |
| claude-opus (4.7/4.8) | patches | `⌈w_r/28⌉·⌈h_r/28⌉`, `resized_size(…, 2576, 4784)` | **4758** | platform.claude.com vision |
| claude-sonnet/haiku (legacy) | patches | same, `resized_size(…, 1568, 1568)` | 1530 | platform.claude.com vision |
| gemini (ai.google.dev) | flat | **258** exact-MEDIUM / **2304** biased-high; PDF page 258 — *not* tiles (that's Vertex) | 258–2304 | ai.google.dev media-resolution |
| gemma (vLLM) | flat | **320** (Gemma 4 = 280×1.14; Gemma 3 = 256); pan-scan off by default | 320 | HF gemma4 / arXiv 2503.19786 |
| minimax-m3 | flat | 1600 (no public formula — calibrate via usage) | 1600 | — |
| default | flat | 1600 — unknown family / unreadable dims; never `str(content)` | 1600 | — |
