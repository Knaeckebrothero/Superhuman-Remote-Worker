# Multimodal image cost & estimator tuning — deferred from context token accounting

**Status:** Backlog. Nothing started. **None of this is a correctness issue.**
The multimodal context-explosion wedge is fixed and shipped (S1–S4,
`docs/done/context_token_accounting.md`, live-verified 2026-06-14). These are the
deliberately-deferred *cost* and *estimator-precision* knobs — they reduce real
provider tokens/$ on rendered pages and tighten the estimate, but the system is
already safe without them because the compaction trigger **re-anchors on the
provider's real `input_tokens` every turn** (so a biased-high estimate only ever
compacts slightly early, never wedges).

**Parents / context:**
- `docs/done/context_token_accounting.md` — the shipped fix (§8 "open knobs" is the source of this list).
- `docs/tests/context_token_accounting_verification.md` — verification runbook (§7 lists these as open approximations).
- `docs/issues/multimodal_image_context_explosion.md` — the original root-cause inventory.

**Explicitly NOT in this doc** (separate concerns, file as their own issues — they're
correctness/capability, not cost):
- **Browser-screenshot path** — screenshots return base64 in a dict that bypasses
  `extract_image_tags`, so a multimodal model never *sees* them and the base64 sits
  as text in a `ToolMessage`. Capability + context bug.
- **Phantom "user" image messages** — synthetic image `HumanMessage`s persist
  `role="human"` and render as user bubbles (`persistent-chat.service.ts:2171`). UX bug.

---

## Why these were deferred

After S1 (honest counting) + S2 (don't rasterize text pages) + S4 (per-family
estimator), the real-world result is already good: session `0ed8c0e0` (gpt-5.5, 10
multi-page PDFs) completed with no compaction. What's left only changes *how many
real tokens/dollars* a rendered page costs and *how tight* the pre-flight estimate
is — second-order, and in one case (DPI) genuinely nuanced (helps some model
families, hurts others). Do these when a concrete trigger fires, not speculatively.

---

## 1. Per-family render DPI (the main item)

**Current state:** `DEFAULT_DPI = 150` is a fixed module constant
(`src/services/document_renderer.py:28`). Every rasterized page is rendered at 150
DPI regardless of the target model. `render_page(file_path, page_num, dpi=None)`
(`document_renderer.py:283`) and `render_pdf_page(…, dpi=…)` (`:118`) already accept
a per-call DPI, but `_get_visual_content` (`src/tools/workspace/files.py:413`) calls
`renderer.render_page(full_path, page_num)` **without** one, so it falls back to the
singleton's 150.

**Why it matters (and why it's nuanced):** a 150-DPI A4 page is ~2835 Anthropic
visual tokens / well past the standard 1568 cap — the provider silently downscales
it anyway, so we pay to render detail the model discards. Lowering DPI reclaims that
for the model families we actually run as mains:
- **Patch-based** (`gpt-5` incl. gpt-5.5, `claude-*`): fewer pixels → fewer patches → **fewer tokens.** ✅
- **Tile-based** (`o-series`, `gpt-4o`): the pipeline upscales the short side to 768 regardless, so dropping a 150-DPI page to ~96 DPI *increases* tile count (4→6). ❌
- **Accuracy floor:** below ~150 DPI, OCR/vision fidelity degrades (Inoue arXiv:2503.23667). Don't go under 150 for text-bearing pages.

So this must be **per-family**, not a global knob, and it should never drop below
the accuracy floor for the patch families — its value is mostly "don't render *above*
what the model keeps."

**Approach (mirror the `image_tokens` plumbing):**
1. New matrix key `settings.pdf_render_dpi` per family in `config/model_config_matrix.yaml`
   (separate from `image_tokens` — render resolution ≠ token accounting). e.g. `gpt-5: 130`,
   `claude-*: 130`, `o-series`/`gpt-4o`: keep `150` (or omit → default), `default: 150`.
2. Route it through `LimitsConfig` exactly like `image_tokens`
   (`loader.py:594-600` arm → `LimitsConfig` field `:1231` → both inline constructors `:1897/:2098`).
   It **cannot** go via `LLMConfig` — `_parse_llm_config` is a closed constructor that
   drops unknown keys (same trap S4 hit).
3. Surface the resolved value into the tool layer: add `pdf_render_dpi` to the dict
   `ToolContext` reads via `get_config` (`src/tools/context.py:260`) when the tool
   context is built, then in `_get_visual_content` pass
   `renderer.render_page(full_path, page_num, dpi=context.get_config("pdf_render_dpi", 150))`.
   - *Alternative (no loader plumbing):* resolve `family_of(context._llm_config.model)`
     (`src/core/model_registry.py`) inside `_get_visual_content` and map to a DPI. Couples
     the tool to family logic but avoids threading config; pick whichever is cleaner at
     implementation time.
4. **Never** mutate the `get_document_renderer()` singleton's `.dpi` — it's shared
   across concurrent callers; always pass DPI per call.

**Priority:** Low. Do it when a tile-mode vision model becomes a main (to avoid the
upscale penalty), or when page-render token cost shows up as a measured spend
problem. Our current mains (gpt-5.5, claude-sonnet) are patch-based and already
correct at 150 — the win is marginal today.

---

## 2. Ingestion downscaling + OpenAI `detail:low` (Layer-0 prevention)

**Current state:** the full base64 of a 150-DPI render is sent as-is; no pre-send
resize. `image_content.py` sets no `detail`, so `gpt-5.5` defaults to the *most
expensive* `"original"` patch budget (10000).

**Approach:** cap the long edge to the target model's native resolution *before*
base64-encoding (≤1568px standard Claude / ≤2576px high-res / ~768px short side for
OpenAI tile models), in `document_renderer.py` or the encode path. For scanned/OCR
pages where fidelity is needed, leave high; for everything else, OpenAI's
`detail:"low"` (flat ~85 tokens) is a cheap per-request lever, threadable through
`_get_visual_content`.

**Relationship to #1:** downscaling and DPI are the same lever from two ends
(render fewer pixels vs. shrink after render); implement together. Largely subsumes
#1 if done as "resize to model-native cap."

**Priority:** Low–medium. Highest raw token savings of anything here, but overlaps
#1 and the S2 page-gate already removed the dominant waste (text pages aren't
rendered at all).

---

## 3. OpenAI patch→token multiplier calibration

**Current state:** the `openai_patches` estimator uses a `1.0` patch→token
multiplier (the per-model multiplier for gpt-5.x is unpublished). Biased-high because
the budget cap (10000) over-reserves.

**Approach:** when a real `input_tokens` breakdown becomes available for a
single-image turn, back out the true multiplier and set it per family. Until then
`1.0` + the budget cap is safe (over-estimates → compacts slightly early at worst).
Note: the codex-proxy Chat-Completions envelope drops `input_tokens_details`, so the
breakdown isn't directly readable today — see #6.

**Priority:** Low. Self-correcting via the per-turn re-anchor.

---

## 4. `o4-mini` patch-vs-tiles mapping

**Current state:** `o4-mini` is actually patch-based but inherits the `o-series`
`openai_tiles` config (biased-*low* — noted in the YAML). Unused today (o4-mini isn't
a configured main).

**Approach:** add an `o4-mini` row (or an o-series patch variant) with
`openai_patches`, budget 1536, ×1.72. Trivial YAML once o4-mini is actually used.

**Priority:** Low (dormant until o4-mini is configured).

---

## 5. Gemini API-vs-Vertex billing divergence

**Current state:** `gemini` is `flat: 2304` (biased-high), assuming the
**ai.google.dev** API which bills a flat ~258/image at MEDIUM. **Vertex** bills crop
tiles (up to ~7× higher). If the deployment moves to Vertex, the real `input_tokens`
jumps 4–7× and the flat estimate flips to *undercount* (the one direction that's
unsafe). Tracked upstream: `googleapis/python-genai#1907`.

**Approach:** log the resolved Gemini endpoint; if Vertex, switch the gemini row to a
crop-tile mode (mirror `gemini_tiles`). The auto-calibration loop (#6) would catch
this drift automatically.

**Priority:** Low (no Gemini main today), but **revisit before** routing any vision
traffic to Vertex — this is the one knob whose miss-direction is unsafe.

---

## 6. Auto-calibration loop (ties #3/#5 together)

**Idea:** a cheap periodic job that, per family, compares the estimator's output
against the provider's actual `input_tokens` over real multimodal turns, and flags
drift (e.g. estimate < actual, or estimate ≫ actual) when a provider changes its
tiling/patching rules. For Anthropic, `POST /v1/messages/count_tokens` is a free,
unbilled exact oracle for image blocks.

**Approach:** log `(family, estimate, actual_input_tokens, image_count, dims)` per
turn (where the provider returns a usable breakdown); a small analysis job asserts
`estimate ≥ actual` within a band and alerts on violation. This is the durable
mitigation for #3 and #5.

**Priority:** Low; worth it only once multiple vision families are in active use.

---

## 7. (Note) Gemma vision calibration — moot post-S3

`gemma` is `flat: 320`. Gemma is the **text-only summarizer** post-S3, so it never
sees images in the live path; this is forward-looking only (if a gemma-vision main is
ever introduced). To close: confirm Gemma 3 (256) vs 4 (280) on the homelab and the
pan-and-scan flag. **Priority:** None until gemma-vision is a main.

---

## Summary

| # | Item | Type | Trigger to do it |
|---|------|------|------------------|
| 1 | Per-family render DPI | render cost | a tile-mode vision main appears, or page-render $ measured as a problem |
| 2 | Ingestion downscale / `detail:low` | render cost | same as #1 (implement together) |
| 3 | OpenAI patch multiplier | estimator precision | a single-image `input_tokens` breakdown becomes available |
| 4 | o4-mini patch mapping | estimator precision | o4-mini configured as a model |
| 5 | Gemini API-vs-Vertex | estimator safety | **before** any Vertex vision routing |
| 6 | Auto-calibration loop | estimator safety | ≥2 vision families in active use |
| 7 | Gemma vision flat | estimator precision | a gemma-vision main appears |

All Low priority. The shipped S1–S4 already make the system correct and safe; this
doc exists so the deferred tuning isn't lost.
