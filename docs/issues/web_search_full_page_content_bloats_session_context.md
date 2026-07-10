# `web_search` inlines full page bodies and bloats session context

**Status:** **OPEN — deliberately deferred** (session 2026-07-10; "do this in another session"). Not fixed. Diagnosed alongside the codex context-window wedge (session `4b82e6db`).
**Severity:** medium-high — it's the *trigger* that pushes sessions over the model/transport context ceiling, and it silently multiplies cost.
**Component:** `src/tools/research/web.py` (`web_search`, `_direct_web_search`, `_truncate_content`, `MAX_RAW_CONTENT_WORDS`).
**Related:** `docs/issues/codex_proxy_context_window_cap.md` (the ceiling this blows past), `project_session_web_search_context_bloat` (memory), `project_session_multimodal_pdf_context_explosion` (same context-explosion class, images).

## The problem

`web_search(include_raw_content=True)` appends **up to `MAX_RAW_CONTENT_WORDS = 5000` words of full page text per result** (`web.py:18`, `:384-388`), and `max_results` can be up to 20. So a single search can inline 5–10 full pages: measured tool results of **100K–345K chars each** (session `4b82e6db`: ord 41 = 344,770 chars for 10 results ≈ 34.5K chars/result ≈ 5000 words, pegged at the cap). ~12 searches over the session accumulated **~1.6M chars ≈ 416K tokens**, which pushed the persistent turn past the codex proxy's ~400K ceiling and hard-wedged the session (every subsequent turn re-sent the whole history → `context_too_large` → empty response).

It's not duplication and it's not the model legitimately reasoning over a million tokens — it's the tool stuffing whole page bodies into the transcript, permanently, times many results, times many searches.

## Key point: the full content is ALREADY persisted elsewhere

`web.py:349-362` already calls `context.save_web_content_to_disk(url, content, ...)` for every result, and each result is registered as a **citation source** (`get_or_register_web_source`). So the full page text is already on the workspace filesystem and retrievable by source ID. **Inlining it into the conversation on top of that is pure redundant bloat** — the agent could look up the detail later instead of carrying it in every turn's context forever.

## Proposed direction (for the follow-up session)

The idea from the diagnosis session: **return a compact result — snippet + a note pointing at where the full content lives** (the on-disk path and/or citation source ID) — instead of the full raw body, so the agent can pull specifics on demand (`extract_webpage`, read the saved file, or cite by source ID) without paying the context/token cost every turn. Concretely, some combination of:

1. Stop inlining full `raw_content` for multi-result searches; emit snippet + `Source ID [N] (full text saved: <path>)`.
2. Lower `MAX_RAW_CONTENT_WORDS` (e.g. 5000 → ~1000–1500) for the cases that still inline.
3. Add an **aggregate per-call cap** (total chars across all results, not just per-result) so a 10-result search can't dump 345K chars regardless of per-result limits.
4. Consider making `include_raw_content=True` at high `max_results` a no-op or a warning — steer the model to `extract_webpage` the 1–2 pages it actually needs.

## Why it still matters even with the codex cap shipped

The codex context-window cap (400K) stops the *hard wedge*, but a single 88K-token search result is reckless inside any window, and above 272K input tokens OpenAI bills **2× input / 1.5× output** — so the bloat was also silently doubling cost on turns that did squeak through. Fixing the tool is the durable fix; the window cap is the backstop.
