# `web_search` inlines full page bodies and bloats session context

**Status:** OPEN — hardened into an implementation brief 2026-07-11 (originally
diagnosed 2026-07-10 alongside the codex context-window wedge, session `4b82e6db`).
**Severity:** medium-high — it's the *trigger* that pushes sessions toward the
model/transport context ceiling, and it silently multiplies cost (>272K input
tokens bills 2× in / 1.5× out on OpenAI).
**Component:** `src/tools/research/web.py` — and NOT just `web_search`:
`extract_webpage` and `crawl_website` share the same inline-full-content
pattern.
**Related:** `docs/issues/codex_proxy_context_window_cap.md` (the transport
ceiling), `docs/done/compaction_keep_window_retains_oversized_tool_results.md`
(the now-shipped compaction backstop — this doc is the ingestion-side fix that
stops the bloat from entering the transcript at all).

## The problem

`web_search(include_raw_content=True)` appends up to
`MAX_RAW_CONTENT_WORDS = 5000` words of full page text **per result**
(`web.py:18`, `:384–388`), with `max_results` up to 20. Measured in session
`4b82e6db`: tool results of 100K–345K chars each (344,770 chars for 10
results = pegged at the cap); ~12 searches accumulated ~1.6M chars ≈ 416K
tokens and wedged the session against the codex ~400K ceiling.

The full text is **already persisted**: `web.py:349–362` saves every result
to the workspace (`save_web_content_to_disk`) and registers it as a citation
source (`get_or_register_web_source`). Inlining it on top is pure redundant
bloat.

## Verified behavior map (all anchors `src/tools/research/web.py`)

- **`_direct_web_search`** (:267): registers sources (:333–347), saves to disk
  (:349–362), then formats output (:364–405). With `include_raw_content=True`
  each result inlines `_truncate_content(raw)` at 5000 words (:384–386);
  without it, a 300-char snippet (:388). Inaccessible sources get a WARNING
  block (:390–398).
- **`_extract_webpage`** (:414): same save+register, then inlines
  `_truncate_content(raw)` per URL (:487–499). Up to 20 URLs/call (:440).
- **`_crawl_website`** (:521): same save+register, then inlines
  `_truncate_content(raw)` per crawled page (:612–624).
- **`save_web_content_to_disk`** (`src/tools/context.py:506`) RETURNS the
  workspace-relative path (`documents/external/<domain>_<hash8>.md`,
  deterministic per URL, first-save-wins) — **but every call site discards
  the return value**. Returns `None` when `has_workspace()` is false
  (lite/virtual sessions).
- Tavily results carry BOTH `content` (short snippet) and `raw_content`
  (full text) when raw is requested; the non-raw branch already renders
  `content[:300]`.
- **No existing tests** exercise `web.py` output shapes (only incidental
  registry/loader references). Tavily classes are imported *inside* the
  functions (`from langchain_tavily import TavilySearch` at :300), so tests
  inject a fake `langchain_tavily` module via `sys.modules` / monkeypatch.

## Decisions (implement these, not alternatives)

### 1. `web_search`: never inline raw bodies

Per result render: title, URL, Source ID line (unchanged), then the Tavily
`content` snippet capped at ~1,000 chars, then a pointer line:

```
   Full text saved: documents/external/example_com_a1b2c3d4.md — read it or
   extract_webpage(url) if you need the whole page.
```

Capture the path by finally using `save_web_content_to_disk`'s return value
(build a `url → path` map in the save loop at :349–362). No pointer line for
inaccessible/unsaved results.

**THE TRAP:** `include_raw_content=True` must STILL be forwarded to the
`TavilySearch` constructor (:302–305) — `raw_content` is what gets SAVED to
disk. Naively dropping the flag would silently stop archiving full text and
gut the citation flow. The parameter's meaning changes from "inline full
content" to "fetch + archive full text"; update the tool docstring
(`web.py:142`) and the registry `description`/`short_description`
(`RESEARCH_TOOLS_METADATA`, :22–31) so the LLM-facing contract says so.

### 2. `extract_webpage`: keep inlining (it's the on-demand escape hatch) but bound it

This tool is *how* the agent deliberately pulls a full page into context —
do not neuter it. Add an **aggregate per-call cap**
`MAX_TOTAL_INLINE_CHARS = 60_000` (~15K tokens), consumed first-result-first;
once exhausted, remaining results render snippet + saved-path pointer
instead. Keep the per-result 5000-word cap. A 1–2 URL extract (the steering
use case) is unaffected; a 20-URL extract gets bounded.

### 3. `crawl_website`: discovery tool — snippet + pointer per page

Replace the per-page `_truncate_content(raw)` inline with first ~500 chars +
saved-path pointer. Crawl's job is getting a site into the workspace/citation
store, not into the transcript.

### 4. No-workspace fallback (lite/virtual sessions)

When `save_web_content_to_disk` returns `None` there is no file to point at.
Fallback: inline a bounded excerpt instead — per-result 1,500 words AND the
same 60K-char aggregate cap — since dropping content entirely would lose it
(only re-search would recover). Gate on the returned path, not on
`has_workspace()`, so partial failures degrade the same way.

### 5. Constants, not config plumbing

New module constants next to `MAX_RAW_CONTENT_WORDS` (:18):
`MAX_SNIPPET_CHARS = 1000`, `MAX_TOTAL_INLINE_CHARS = 60_000`,
`NO_WORKSPACE_MAX_WORDS = 1500`. No YAML/loader threading — these are tool
implementation details, not per-deployment tuning knobs (revisit only if a
real need appears).

## Touch list

| File | Change |
|---|---|
| `src/tools/research/web.py` | Constants (:18); capture save paths (:349–362, :472–479, :597–606); rework render loops (:364–405, :481–512, :608–629); docstrings (:127, :142, :161+) |
| `src/tools/research/web.py` `RESEARCH_TOOLS_METADATA` (:22–56) | `web_search` + `extract_webpage` descriptions: content is archived; read the saved file / extract on demand |
| `tests/test_web_tools.py` (NEW) | See verification |

Explicitly NOT in scope: prompt/template mentions of `web_search`
(`config/prompts/instructions.md:54` etc.) are generic and stay; the
compaction keep-window cap (shipped) is untouched; `map_website` returns URLs
only — no change.

## Verification

Unit (`tests/test_web_tools.py`, NEW — fake `langchain_tavily` module in
`sys.modules`, stub `ToolContext` with tmp workspace à la `conftest.py`
`WORKSPACE_PATH`):

- 10 results × 200K-char `raw_content`, `include_raw_content=True` → output
  under ~15K chars; every saved result has snippet + its exact
  `documents/external/...` path; Tavily constructor still received
  `include_raw_content=True`; files exist on disk with full content.
- Same call with a workspace-less context → bounded excerpts (1,500
  words/result, 60K aggregate), no pointer lines, no crash.
- `extract_webpage` with 20 huge URLs → first results full (≤5000 words),
  aggregate cap kicks in, remainder snippet+pointer.
- `crawl_website` → ≤500 chars + pointer per page.
- Inaccessible source → WARNING block unchanged, no bogus pointer.

Live (k3d or homelab session): heavy research query with
`include_raw_content=True` → tool card a few KB instead of 100K+; context %
stays flat across several searches; `documents/external/` populated; agent
successfully reads a saved file / `extract_webpage`s when pressed for detail;
`cite_web` flow still works.

## Sizing expectation

A 10-result raw search goes from ~345K chars (~86K tokens) to ~6–10K chars
(~2K tokens) — per search. The compaction keep-window cap remains the
backstop for every other oversized producer.
