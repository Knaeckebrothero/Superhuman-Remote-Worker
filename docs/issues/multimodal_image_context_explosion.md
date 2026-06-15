---
tags:
  - issue
  - persistent-sessions
  - agent
  - context-management
  - multimodal
  - bug
related:
  - "[[session_silent_failure_audit]]"
  - "[[context_summarization_rework]]"
  - "[[persistent_session_runaway_generation_context_explosion]]"
  - "[[persistent_session_midturn_message_loss]]"
---

# Multimodal images explode + wedge context — counted as base64 text, invisible to every compaction net

**Filed:** 2026-06-13, from session `eb989b82` (a student testing the
compliance-PDF workload). Corroborated same-day by a second independent
investigation that reached the same keystone.

**Deep-dive refined:** 2026-06-13 — 6-agent codebase audit + provider-doc
research. This revision **corrects two claims in the first draft** (Layer-B
ordering and resume behavior — see [Corrections](#corrections-to-the-first-draft)),
adds a **second uncovered image path** (browser screenshots), replaces the
flat token constant with a **dimension-aware estimator**, and adds a
**prevention layer (downscale + selective rasterization)** that the research
flags as the single highest-leverage change.

**Status:** ✅ **RESOLVED — shipped (S1–S4) + live-verified 2026-06-14.** The fix
shipped as the four-slice design in **`docs/done/context_token_accounting.md`**
(S1 honest counting + provider-`input_tokens` anchor · S2 don't-rasterize-text-pages
gate · S3 image-safe summarizer/elision · S4 per-family estimator), verified
against real post-fix session `0ed8c0e0` (gpt-5.5, 10 multi-page PDFs → no
compaction, turn completed). Runbook: `docs/tests/context_token_accounting_verification.md`.

**This doc is the historical root-cause inventory; the as-built differs from the
A/B/C/0/Layer-0 framing below.** Notably the **Layer-B per-call
`keep_recent_images` eviction was NOT shipped** — it was replaced by
**compaction-time-only** image elision (S3) to avoid prompt-cache thrash. Deferred
cost/estimator knobs (per-family render DPI, downscaling, multiplier/Gemini-Vertex
calibration) now live in **`docs/features/multimodal_image_cost_optimization.md`**.
Still-separate issues: the **browser-screenshot** path and **phantom user-message**
role attribution.

Still the **multimodal sibling of [[session_silent_failure_audit]] #5/#6/#7** (the
text-PDF case): #6's keep-window elision shipped for `ToolMessage` text but its
filter **excluded** the image-bearing `HumanMessage`s, so images slipped through.

---

## TL;DR

A multimodal session that reads a multi-page PDF renders each page to a
150-DPI PNG, lifts it into a synthetic `HumanMessage` as a base64 `image_url`
block, and then **the token counter `str()`s the message and tokenizes the
entire base64 string** — counting ~11 page images as **2.79M tokens** (real
provider cost: a few thousand). That false overflow triggers endless
compaction, but every compaction net filters on `ToolMessage`, so the image
`HumanMessage`s (newest, pinned by `keep_recent_messages=10`) can't be shed →
**session wedged**. The same images also rendered as **phantom user bubbles**.

**Fix, in priority order:**
- **A (keystone, unwedges live):** count image blocks at real vision-token cost, not base64 length.
- **B:** "look-once" eviction — keep the last K image-bearing messages, replace older with a restorable text marker, on the per-call copy.
- **C:** emergency image elision inside `ensure_within_limits`, **before** the summarizer call.
- **0 (highest-leverage prevention, larger workstream):** downscale images to the model's native resolution cap at ingestion, and don't rasterize text-extractable PDF pages at all.
- Plus secondary fixes (summarizer formatter, an aux-formatter crash) and two **separate** issues to file (browser-screenshot delivery, phantom-message role).

---

## Symptom

Session `eb989b82` (gpt-5.5 via codex-proxy, `persistent_defaults`,
supervised, **multimodal**). Student uploaded `Cryogel_Z_SDS.pdf`, asked for
an EU/US compliance check. Observed:

1. "Nothing happened for a while" after sending.
2. After refresh: the user message was reformatted (attachment-name suffix
   appeared) and **three messages the student never sent** showed up as his
   own chat bubbles: `Image content from tool call call_TSML…:` / `…call_vust…:`
   / `…call_q6Kp…:` (each with the user-side `person` avatar).
3. The compaction display read **1396 % — 2791.7k / 200.0k tok**, stuck on
   `Komprimiere — Durchlauf 1 von 1 · 1:35`. The turn never completed
   (`threads.total_tokens=0`); the session is dead-in-place.
4. The turn rendered **twice** with a `SITZUNG FORTGESETZT` divider.

### Two different meters (important for diagnosis + verification)

The cockpit shows **two** context numbers from **two** sources — this is the
"CTX 7%" vs "1396%" confusion:

| Display | Source | Affected by the bug? |
|---|---|---|
| Live **"CTX %"** gauge (bottom-right) | the **provider's** real `input_tokens` (`reasoning_chat.py:1221-1232` → `usage.updated` → cockpit `usageCtxPct`, clamped ≤100%) | **No** — correct. Stays stale in the wedge because no successful LLM call ever returns usage. |
| **"2791.7k / 200.0k → 1396%"** compaction display | the **buggy** `context.py` counter (`get_token_count` at `context.py:1602` → `compaction.started` event, `ctx_used_pct` **not** clamped) | **Yes** — this is the bug's output. |

**Verification corollary:** after the Layer-A fix, the compaction display
should converge toward the live gauge (both derive from `config.limits.model_max_context_tokens` for the denominator).

### Evidence (live DB, `main` ctx → ns `superhuman-remote-worker`)

`thread_messages` for `eb989b82-…`, all turn 1, ends at seq 4261, never
produced a final AI answer:

| seq | role | clen | preview |
|---|---|---|---|
| 4256 | tool | 10036 | `[Pages 1-5 of 6] [PAGE 1] SAFETY DATA SHEET …` |
| 4257 | **human** | 64 | `Image content from tool call call_TSML…:` |
| 4258 | tool | 7215 | `[Pages 1-4 of 4] APPENDIX D TO §1910.1200 …` |
| 4259 | **human** | 63 | `Image content from tool call call_vust…:` |
| 4260 | tool | 3184 | `[Pages 1-2 of 2] Hazard Communication SDS …` |
| 4261 | **human** | 61 | `Image content from tool call call_q6Kp…:` |

Three PDFs → pages 1-5/6 + 1-4/4 + 1-2/2 ≈ **11 full-page 150-DPI PNGs**.
`threads.events_epoch=1`. Persisted `content` is only 61-64 chars — **the DB
is clean**; the base64 lives only in the in-memory working set (and never
survives a resume — see [Corrections](#corrections-to-the-first-draft)).

> Effective limit footnote: `gpt-5.5`'s catalog `context_window` is NULL, so
> the model-family default applies. The local `develop` gpt-5 family default
> is **1,050,000** (`config/model_config_matrix.yaml:194`), but the live meter
> showed **200k**, so the deployed image's value differs. Immaterial to the
> bug — 2.79M overflows either limit; correct counting fits under both.

## How it stacks (causal chain)

```
multimodal session + multi-page PDF read
  → each page rendered to a 150-DPI PNG, base64'd into a <page_image> tag      [Layer-0 waste]
  → post-processor lifts it into a synthetic HumanMessage (image_url block)    (image_content.py)
      ├─ persisted role="human" → cockpit renders it as a USER bubble          [separate issue: role]
      └─ in-memory content is a list with the full data: URL
           → token counter does str(content) → counts the whole base64        [A: 2.79M, the keystone]
              → should_summarize false-fires; summary compacts OLD text, frees ~0
              → images are the NEWEST msgs, held verbatim by keep_recent=10
              → every compaction net filters on ToolMessage → skips images     [C: structural gap]
                 → context stays pinned > limit → every retry re-overflows → WEDGED
```

## Root cause

**The stack has no image-token accounting.** Image content blocks are treated
as raw base64 text everywhere — counting, truncation, summarization,
persistence. The first time a multimodal session reads a multi-page PDF,
context management breaks.

### A — Token-count explosion (the keystone)

Both counters stringify list content and tokenize the entire
`data:image/png;base64,…` URL:

- `count_tokens_tiktoken` — `content = … else str(msg.content)` then `enc.encode(content)` (`src/core/context.py:448`)
- `count_tokens_approximate` — same pattern (`src/core/context.py:502`)

Every counting decision in `ContextManager` flows through these (`should_compact` `:656`, **`should_summarize` `:672`** — the trigger that false-fires, the elision gates, and the compaction-display value `:1602`). A 150-DPI full-page PNG ≈ hundreds of KB of base64 ≈ hundreds of thousands of "tokens" by our count; 11 pages → 2791.7k, matching the meter exactly. **We over-count images by ~1000×.**

Contrast: the HTTP pre-flight counter `count_request_tokens` (`src/llm/reasoning_chat.py:153`) already does the right shape — it iterates content parts and counts only `part["text"]`, contributing **0** for image blocks (so it never false-fires). It under-counts (0, not real cost), but it proves the content-block-aware pattern is already in the codebase.

### B — Every compaction net skips images (structural gap)

The text-case nets from [[session_silent_failure_audit]] #6 all filter on
`ToolMessage`, so image `HumanMessage`s — the **newest** messages, held
verbatim by the keep-window — are untouchable. Summarizing older text frees ~0.

| Mechanism | Filter | Image HumanMessage? |
|---|---|---|
| keep-window (`context.py:381`, `keep_recent_messages=10`) | keeps last 10 verbatim | ✅ kept (images are newest) |
| `_elide_largest_tool_results` (`context.py:1082`) | `isinstance(msg, ToolMessage) and isinstance(msg.content, str)` | ❌ skipped |
| `_emergency_truncate_tool_results` (`context.py:1139`) | `isinstance(msg, ToolMessage)`; `len(content)` on a list ≈ 2, never > limit | ❌ skipped |
| oversized backstop in `summarize_and_compact` (`context.py:1518-1528`) | `AIMessage` w/o tool_calls | ❌ skipped |

### C — Source-side waste: redundant page images for text PDFs

`_get_visual_content` (`src/tools/workspace/files.py:420-427`) renders a
full-page image for **every** page in multimodal mode (`DEFAULT_DPI=150`,
`src/services/document_renderer.py:28`), even when text extraction already
captured the page (the SDS is text — the tool returned `[PAGE 1] SAFETY DATA
SHEET …` *and* a page image). 11 redundant huge images for one text document.
Research strongly validates fixing this at the source (see
[Best-practice findings](#best-practice-findings-that-shape-the-design)).

### D — A SECOND uncovered image path: browser screenshots (new finding)

Browser tools (`src/tools/research/browser_direct.py:150-285`) return a Python
**dict** `{dom, url, screenshot, …}` where `screenshot` is a raw base64 string
**not wrapped** in `<image_data>`/`<page_image>` tags. So `extract_image_tags`
(which only regexes those tags out of a `ToolMessage` string) **never processes
it**. Consequences, different from the PDF path:

- The base64 stays embedded in the **`ToolMessage` string** (no synthetic `HumanMessage` is made).
- **A multimodal model never receives the screenshot as a real image block** — it only sees base64-as-text. (The `image_content.py` docstring lines 20-25 explicitly flags this as a known gap; confirmed still true on both worker + persistent paths.)
- For counting: because it's a `ToolMessage` *string*, the existing `_elide_largest_tool_results` net **can** shed it once over-limit — so the browser path is *less* prone to wedging than the PDF path, but is **broken in a worse way** (the agent is "blind" to screenshots it thinks it took).

**Implication:** a fix scoped only to `HumanMessage` image blocks (Layers A/B as written) **will not touch browser screenshots**. Two options: leave them to the text-result nets (and file the "screenshots not delivered as images" capability bug separately — recommended), or normalize them through `extract_image_tags` (fixes the capability bug but then they inherit the same over-counting and must be covered by A/B too). See [Related issues](#related-issues-to-file-separately).

### Secondary list-content hazards (found in the audit, fix alongside A)

- **`src/core/context.py:1259`** — `f"User: {msg.content[:500]}"` in `_format_messages_for_summary` is a **no-op on a list** (slicing a 2-elem list returns the whole list). If an image message ages into the summarize window, the **full base64 leaks into the summary prompt** and can OOM the aux model. (Issue §C secondary.)
- **`src/services/auxiliary.py:1116`** — `content.strip()` runs for any message including a list-content `HumanMessage` → **`AttributeError`** (caught as a non-fatal aux failure, but a latent crash on the aux path).
- **`src/persistent_graph.py:683-686`/`:718-722`, `src/services/memory/query.py:34`, `…/plugins/legacy.py:64`** — `str(msg.content)` of an image message builds the **embedding/retrieval query from the full base64** → wasted call + polluted retrieval (quality, not accounting). Minor.

### Pre-existing — epoch double-render (out of scope)

`events_epoch=1` is the message-sent-during-provisioning epoch self-bump:
early SSE pins to the dead epoch ("nothing happened"), refresh → reload +
live-replay → turn rendered twice + `SITZUNG FORTGESETZT`. Tracked in the
session-epoch notes; listed only because it shares this trace.

## Image token cost — the real numbers

From provider docs (2026). The `(w×h)/750` heuristic is **legacy/removed**;
use these. A 1240×1754 (150-DPI A4) page costs:

| Provider / mode | Formula | Tokens (1240×1754) |
|---|---|---|
| **Anthropic** standard (Sonnet/Haiku/3.x) | `⌈w/28⌉×⌈h/28⌉`, capped 1568, long edge ≤1568px | **~1568** (capped) |
| **Anthropic** high-res (Opus 4.7+/Fable 5) | same, cap 4784, edge ≤2576px | **2835** |
| **OpenAI** GPT-4o/4.1 **high** | `85 + 170×tiles` (fit 2048²→short side 768→512px tiles) | **1105** |
| **OpenAI** GPT-4o/4.1 **low** | flat **85** regardless of size | **85** |
| **OpenAI** gpt-5/4.1-mini patch | `min(⌈w/32⌉×⌈h/32⌉,1536)×1.62` | **~2488** |
| **Gemini** (default) | 258/tile, 768px tiles, `crop=⌊min/1.5⌋` | **~1032** |
| **Fallback** (dims unreadable) | constant | **~1600** |

Cheap dimension read (no full decode): **PNG** — width/height are big-endian
uint32 at byte offsets 16-19 / 20-23 (IHDR). **JPEG** — scan `FF` markers from
the `FF D8` SOI, skip segments by their 2-byte length, and at a SOF marker
(`0xC0-C3,C5-C7,C9-CB,CD-CF`) read height @+5, width @+7 (BE). Decode only the
leading bytes of the base64 (PNG ~64B; JPEG a few KB).

## Best-practice findings that shape the design

(Provider docs + Manus + JetBrains "Complexity Trap" + LangChain maintainers +
Anthropic Cookbook. Citations in [Cross-references](#cross-references).)

1. **Downscale at ingestion is the highest-leverage lever — and providers explicitly recommend it.** A 150-DPI A4 page is ~2835 visual tokens and gets **silently downscaled by the provider anyway** (you pay to render detail the model discards). Resize the long edge to the model's native cap before encoding (≤1568px standard Claude / ≤2576px high-res / ~768px short side for OpenAI tile models). This alone would have prevented most of the 2.8M case. OpenAI also exposes `detail:"low"` = flat 85 tokens as a per-request knob.

2. **For text-extractable PDF pages, send text — not images.** Anthropic's own PDF block costs ~7× by sending image+text per page; for born-digital docs the image is often wasteful *and less faithful* (VLM hallucination vs. exact text extraction). Best practice = classify per page, rasterize only pages with tables/charts/figures/scans (UniDoc-Bench: fusion wins on accuracy, but you route selectively for cost).

3. **"Keep last K observations" is the convergent industry pattern** (Anthropic context-editing `keep=3`; JetBrains masking M=10; LangGraph keep-window). Our Layer B is on the documented path. **Upgrade hard-delete → restorable marker** (Manus "restorable compression": keep the path/page/URL so the agent can re-fetch instead of hallucinate). Anthropic's reference string is `[cleared to save context]` and it **keeps the `tool_use`/message in place, replacing only the body**.

4. **Evict images BEFORE summarizing.** Sending base64 into the summarizer is how the summarizer call itself OOMs (OpenHarness ships exactly this ordering; Redis "reversible-before-lossy"). This dictates Layer C's order and the `_format_messages_for_summary` fix.

5. **Prompt-cache interaction — don't churn the head every turn.** Both providers cache a **prefix**; mutating earlier content invalidates everything after. Anthropic: *presence/absence of any image invalidates the message cache*. So **conditional, batched eviction** (only when over a threshold) beats rolling one-image-per-turn drops; keep tools/system/persona a stable prefix. Our Layer A makes this easy — with correct counting, eviction rarely fires at all.

6. **Caveat for reasoning models:** aggressive masking cost ~10% solve-rate under extended thinking (JetBrains). Keep K **tunable**, don't hard-code it aggressively, and watch task success, not just tokens.

## The solution

Five workstreams. **Layer A is the must-have** (unwedges existing sessions on
deploy). B+C harden it. Layer 0 is the highest-leverage prevention but a
larger, separate track. Each is independently shippable.

### Layer A — Count images at real cost (keystone)

Route both counters through a content-block-aware estimate. Ship the
dimension-aware estimator (we have the byte offsets + formulas); fall back to a
flat constant when dimensions can't be read.

```python
IMAGE_TOKEN_FALLBACK = 1600   # Hermes default; used when dims unreadable

def _content_token_estimate(content, encode, *, family="generic") -> int:
    if isinstance(content, str):
        return len(encode(content))
    if not isinstance(content, list):
        return len(encode(str(content)))
    total = 0
    for part in content:
        if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
            total += _estimate_image_tokens(part, family)   # dims→provider formula, else FALLBACK
        elif isinstance(part, dict) and part.get("type") == "text":
            total += len(encode(part.get("text", "")))
        else:
            total += len(encode(str(part)))
    return total
```

- `_estimate_image_tokens` reads w/h from the base64 header (PNG IHDR / JPEG SOF) and applies the provider formula selected from the model family (`src/core/model_registry.py:family_of`), defaulting to `IMAGE_TOKEN_FALLBACK`. For our deployment (gpt-5 family) use the OpenAI tile/patch path; keep Anthropic/Gemini branches for the other families.
- Wire into `count_tokens_tiktoken` (`context.py:448`) and `count_tokens_approximate` (`context.py:502`). **Self-heals the live session on deploy** (in-memory recount, no migration, DB untouched). Also fixes the "2791.7k / 1396%" display (same counter at `:1602`).
- **Also in this PR (cheap):** fix `_format_messages_for_summary` (`context.py:1259`) to skip image blocks instead of the `[:500]` no-op; guard `auxiliary.py:1116` against list content. (The embedding-query pollution at `persistent_graph.py:683`/`memory/query.py:34` can be a follow-up.)

### Layer B — "Look-once" image eviction (keep last K image-bearing messages)

```python
def cap_recent_images(self, messages, keep_recent_images=None):
    """Replace image blocks outside the most-recent-K image-bearing HumanMessages
    with a RESTORABLE text marker. Ephemeral — caller passes the per-call copy.
    Pairing-safe (image HumanMessages carry no tool_call_id)."""
    keep = keep_recent_images if keep_recent_images is not None else self.config.keep_recent_images
    img_idx = [i for i, m in enumerate(messages)
               if isinstance(m, HumanMessage) and isinstance(m.content, list)
               and any(isinstance(p, dict) and p.get("type") in ("image_url", "image") for p in m.content)]
    to_strip = set(img_idx[:-keep] if keep > 0 else img_idx)
    if not to_strip:
        return messages
    out = []
    for i, m in enumerate(messages):
        if i in to_strip:
            text = " ".join(p["text"] for p in m.content
                            if isinstance(p, dict) and p.get("type") == "text")
            n = sum(1 for p in m.content
                    if isinstance(p, dict) and p.get("type") in ("image_url", "image"))
            stripped = HumanMessage(
                content=f"{text}\n[{n} image(s) cleared to save context — re-read the source to view again]".strip())
            stripped.id = getattr(m, "id", None)   # preserve id for downstream bookkeeping
            out.append(stripped)
        else:
            out.append(m)
    return out
```

- **Insertion point (CORRECTED):** `src/persistent_graph.py:839`, right after `prepared = list(bounded)`, before the transient injections and `repair_tool_pairing` (`:889`) and `astream(prepared)` (`:936`). `prepared` is a shallow copy, and `cap_recent_images` builds **new** `HumanMessage` objects, so the durable `_session.messages` is untouched.
- **Its job is to bound the model request** for image-heavy sessions. It does **not** pre-empt the summarizer (that runs earlier, at `:775`) — the summarizer is protected separately by the `_format_messages_for_summary` fix in Layer A and by Layer C's ordering. *(This corrects the first draft's rationale; see below.)*
- **Worker path:** `src/graph.py` after the conversation tail is appended (`~:1138`), before the LAYER-1 safety check (`:1143`); re-apply after the safety rebuild at `:1172-1193` (or factor into a shared helper).
- **Cache-aware:** because correct counting (A) means most sessions never exceed K's real cost, this rarely fires — keeping the churn (and cache-busting) confined to genuinely image-heavy sessions.

### Layer C — Emergency image elision in `ensure_within_limits`, before summarize

Extend the over-limit block at **`context.py:982-986`** (runs on the auto path,
not just force): after `_elide_largest_tool_results`, if still over
`model_max_context_tokens`, elide image blocks oldest-first using the same
restorable marker. **Order matters:** image elision must precede the summarizer
call so base64 never reaches it.

### Layer 0 — Prevention: downscale + selective rasterization (separate, highest-leverage)

Larger workstream (touches the tool layer), but per the research it prevents
the blowup at the source:

- **0a — Selective rasterization:** in `_get_visual_content`/`read_file` (`files.py:420`), render a page to an image only when it carries visual structure (tables/charts/figures/scans or a sparse text layer); otherwise rely on the already-extracted text. Default text-heavy pages to text.
- **0b — Ingestion downscale:** in `document_renderer.py` (and the browser screenshot path), cap the long edge to the target model's native resolution before base64-encoding (≤1568/2576px Claude, ~768px short side OpenAI), and/or lower `DEFAULT_DPI` from 150. Wire DPI/max-edge as config (currently hard-coded module constants). OpenAI-specific cheap lever: emit `detail:"low"`.

### Config + constants

Add `keep_recent_images: int = 4`, mirroring `keep_recent_messages` at every
site (verified):

| # | File:line | Edit |
|---|---|---|
| 1 | `src/core/context.py:381` | add `keep_recent_images: int = 4` to `ContextConfig` (+ docstring ~`:362`) |
| 2 | `src/core/loader.py:1237` | add field to `ContextManagementConfig` dataclass |
| 3 | `src/core/loader.py:1836` | `keep_recent_images=context_data.get("keep_recent_images", 4),` (in `load_agent_config`) |
| 4 | `src/core/loader.py:2036` | same, in `load_agent_config_from_dict` (distinct entry point) |
| 5 | `src/api/persistent_session.py:676` | `keep_recent_images=ctx.keep_recent_images,` |
| 6 | `src/graph.py:3704` | `keep_recent_images=config.context_management.keep_recent_images,` |
| 7-8 | `config/defaults.yaml:190`, `config/persistent_defaults.yaml:133` | optional `keep_recent_images: 4` (loader default already covers absence) |

`IMAGE_TOKEN_FALLBACK` is a module constant near the counters
(`context.py:~415`). `context.py:537` is a docstring example, not a build site
— no edit. **Note:** `config/settings_matrix.yaml` and `config/models.yaml` no
longer exist — settings consolidated into **`config/model_config_matrix.yaml`**
(gpt-5 family: `multimodal: true` `:189`, `model_max_context_tokens: 1050000`
`:194`); models are DB-backed via `model_registry.py`. (CLAUDE.md still
references the old filenames — stale.)

## Corrections to the first draft

The deep dive overturned two claims in the original version of this doc:

1. **Layer-B insertion rationale was wrong.** The draft said inserting at
   `persistent_graph.py:839` happens "before `ensure_within_limits`, so
   summarization never sees aged-out base64." **False** — `ensure_within_limits`
   runs at `:775`, *before* `prepared = list(bounded)` at `:839` (verified:
   `bounded` is adopted via `messages[:] = bounded` at `:812` on real
   compaction). So Layer B at 839 bounds the *model request* only; the
   summarizer is protected separately (the `_format_messages_for_summary` fix +
   Layer C ordering). The insertion *point* is still correct for B's real job.

2. **"Resume still sees the images" was wrong.** The base64 is **never
   persisted** — `_persist_one_message` flattens list content to text
   (`persistent_app.py:3747-3751`), and resume rebuilds **string-content**
   `HumanMessage`s (`persistent_app.py:3353-3364`). So images survive only
   within a single live process; **"look-once" eviction already happens
   involuntarily at every resume boundary.** This *strengthens* Layer B (it
   merely makes in-turn behavior match what resume already does) — and means
   no migration or persistence change is needed.

## Related issues to file separately

- **Browser screenshots not delivered as images + uncovered by compaction
  normalization** (§D above). Both a capability bug (multimodal model never
  sees the screenshot) and a context bug (base64-as-text in a `ToolMessage`).
  Decide: route through `extract_image_tags` (then covered by A/B) vs. leave to
  text-result nets. Honors [[feedback_browser_priority]].
- **Phantom "user" messages.** Synthetic image `HumanMessage`s persist as
  `role="human"` and the cockpit renders them as user bubbles
  (`persistent-chat.service.ts:2171`). Tag them (distinct role or
  `additional_kwargs` flag) + cockpit render rule. Cosmetic but user-facing.
- **CLAUDE.md stale config references** (`settings_matrix.yaml`/`models.yaml`).

## Scope, phasing & verification

- **Phase 1 (unwedge):** Layer A + the two secondary fixes. ~one file
  (`context.py`) + a small estimator module. No migration. Ship first.
- **Phase 2 (harden):** Layers B + C + config leaf. Localized to `context.py` +
  two call-site insertions + plumbing.
- **Phase 3 (prevent):** Layer 0 (selective rasterization + downscale). Larger;
  touches `files.py` / `document_renderer.py` / browser path. Highest leverage
  on cost, lowest urgency on the wedge.

**Verification:**
- **Unit (`tests/test_context_safety.py`):** a multimodal `HumanMessage` counts
  ~1.6k (or the dim-aware value) not ~400k; `_estimate_image_tokens` on known
  PNG/JPEG headers returns expected w/h; `cap_recent_images` keeps exactly K,
  preserves ids, leaves tool pairing intact.
- **Live (k3d):** read a multi-page PDF in a multimodal session; assert the
  compaction % stays sane and the restorable markers appear; confirm the
  existing wedged session `eb989b82` recovers once Layer A deploys (in-memory
  recount).

## Cross-references

- [[session_silent_failure_audit]] #5 (tool-result caps), #6 (keep-window
  elision — the text analog, shipped for `ToolMessage`), #7 (aux context clamp).
- [[context_summarization_rework]] §4.7 (prevention siblings) — images are a
  gap there; fold Layer A/B in or land this first.
- Memory: `project_session_multimodal_pdf_context_explosion.md`.
- **Research sources:** Anthropic vision (`platform.claude.com/docs/en/build-with-claude/vision`),
  prompt-caching + context-editing docs, PDF-support;
  OpenAI images-vision (`developers.openai.com/api/docs/guides/images-vision`);
  Gemini token docs (`ai.google.dev/gemini-api/docs/tokens`);
  Manus context engineering (`manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus`);
  JetBrains "Complexity Trap" (`arxiv.org/abs/2508.21433`);
  UniDoc-Bench (`arxiv.org/abs/2510.03663`);
  Anthropic Cookbook context-engineering (`[cleared to save context]`);
  tool-pairing 400s (`github.com/anthropics/claude-code/issues/8004`).
