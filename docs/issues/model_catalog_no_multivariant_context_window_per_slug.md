# Model catalog can't offer multiple context-window variants of the same upstream slug on one endpoint

**Date:** 2026-06-29
**Status:** Identified — design limitation, not a bug. Not fixed. A cosmetic workaround is **already deployed** (see § "Workaround (b)"). Surfaced while configuring a cost-capped MiniMax M3 entry (`minimax-m3-256`) and wanting both a cheap 256K variant and a 1M-burst variant selectable in the picker.
**Severity:** **Low.** No breakage. A workaround covers the immediate need; this is an ergonomics/modeling gap that becomes annoying only if multi-variant-per-slug turns into a recurring operator pattern.
**Component:**
- Catalog schema — `orchestrator/database/migrations/app/0001_initial.sql:328-349`, specifically `CONSTRAINT uq_model_provider_v2 UNIQUE (provider_kind, provider_ref, model_id)` (`:348`). `context_window INT` is a per-row column (`:339`).
- Dispatch slug derivation — `src/core/model_registry.py:177-184` (strips routing prefixes `openrouter/` / `groq/` / `codex/` before the slug is sent upstream) and `_catalog_row_to_meta` (`:279-319`). `model_id` doubles as *both* the catalog identity *and* (minus prefix) the upstream slug.
- Picker label gap (prerequisite UX) — `orchestrator/main.py:21423` (chat groups carry the bare id, not `display_label`), `cockpit/.../model-group.component.ts:52/77/122`, `orchestrator/services/formatters.py:1268`.
**Related:**
- `docs/issues/loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md` — same session lineage; the context-window cap that motivated this (256K to dodge Novita's pricing cliff).
- `docs/issues/system_provider_models_bypass_gateway_unmetered.md` — same catalog area (`provider_kind` system vs endpoint).
- The picker-label fix (chat groups → `{id,label}`) is **not yet filed separately**; it's the UX prerequisite for any of the options below and is described inline here.

---

## TL;DR

`model_id` is overloaded: it is simultaneously (1) the catalog row's identity, constrained `UNIQUE(provider_kind, provider_ref, model_id)`, and (2) the slug sent to the provider at dispatch (after routing-prefix stripping). Two catalog rows that want the **same upstream slug** (`minimax/minimax-m3`) on the **same endpoint** but **different `context_window`** (256K vs 1M) therefore cannot coexist — they collide on the unique constraint. There is no column that decouples "what we call this offering" from "what string we POST to the provider."

## Why an operator would want this

`context_window` is not cosmetic — it materially changes both behaviour and cost:
- SRW derives the compaction trigger as `0.80 × context_window` (`src/core/loader.py:44`), so the cap controls how large a request grows before it compacts and stops re-sending the whole context.
- For MiniMax M3 on Novita there is a hard **price cliff at 512K**: $0.30/$1.20 per Mtok under 524,288, **$1.20/$4.80 above** (4×). A 256K-capped variant is the cheap default; a 1M variant is an expensive burst option.

So "MiniMax M3 · 256K (cheap)" and "MiniMax M3 · 1M (burst)" are genuinely distinct, legitimately-pickable offerings of the *same* upstream model on the *same* endpoint. The schema forbids representing them as two rows.

## Workarounds available today

**(a) One capped entry — the clean path.** A single row with `context_window` set (e.g. 262144). Simplest, no schema fight. Cost: you lose the ability to pick a *different* cap per job — to change it you edit the catalog row, which re-resolves at the next dispatch (not for in-flight jobs).

**(b) Routing-prefix hack — currently deployed.** Give the two rows *different* `model_id` strings that both reduce to the same upstream slug: `minimax/minimax-m3` (bare) and `openrouter/minimax/minimax-m3` (prefixed). The `openrouter/` prefix is stripped at dispatch (`model_registry.py:183`), so both POST `minimax/minimax-m3` to the same endpoint and both dispatch correctly — but they are *distinct* `model_id` strings, so they sidestep the unique constraint. **This is fragile and confusing:**
- It only yields as many variants as there are valid routing prefixes that happen to strip to the same slug (`openrouter/`, `groq/`, `codex/`) — a coincidence, not a feature.
- Until the picker shows `display_label` (see below), the dropdown renders the ugly `openrouter/minimax/minimax-m3` string instead of the friendly label, so the variants look near-identical at the point of selection.
- A future change to the prefix-strip list would silently break it.

**(c) Separate endpoints.** Register the model under two endpoint rows (e.g. a Novita endpoint and an OpenRouter endpoint), each row with its own `context_window`. Works, and is the *right* shape if the variants genuinely differ by provider — but conflates "provider" with "variant" and needs duplicate endpoint rows / keys when the provider is actually the same.

## Prerequisite either way: surface `display_label` in the picker

Regardless of the variant question, the chat picker today renders the bare `model_id` (`model-group.component.ts:52` → `{{ model }}`), because `/api/models` puts only the id into chat groups (`main.py:21423`) — even though the helper buckets (auxiliary/vision/…) already carry `{id, label}` (`main.py:21380-21384`). So `display_label` (`NOT NULL` in the catalog) never reaches the dropdown. Fixing this is a small, self-contained change (chat groups → `{id,label}`; `formatters.py:1268`; `model.service.ts` type; the three `<option>` templates) and is the precondition for *any* multi-variant scheme to be legible. **It should land first**, independent of whether (d) is ever built.

## Proper fix (if multi-variant-per-slug becomes a real need): decouple identity from slug

Add a nullable column — `served_model_id` (a.k.a. `upstream_slug` / `model_name`):
- `model_id` becomes a free-form **catalog key** (still `UNIQUE(provider_kind, provider_ref, model_id)`), so `m3-256k` and `m3-1m` are legal distinct rows on one endpoint.
- `served_model_id` is the string actually sent upstream. `NULL` → fall back to today's behaviour (`model_id` minus routing prefix), so the change is **backward compatible** and no existing row needs migrating.
- Dispatch (`_catalog_row_to_meta` / the loader factories) uses `served_model_id` when present; the prefix-strip becomes a pure fallback.
- Admin → Models UI gains the field; the picker shows `display_label`, value stays the catalog `model_id`, dispatch uses `served_model_id`.

**Scope:** moderate — one migration (nullable column), `model_registry.py` resolution + the dispatch slug path, the admin Models form, and tests. No data migration. Removes the need for the prefix hack (b).

## Recommendation

**Low priority.** Ship the picker-label fix (prerequisite UX) regardless. For the variant need itself, the single-capped-entry workaround (a) covers the immediate case and the prefix hack (b) is already live for the 2-variant case — so the served-slug column (d) is **deferred** until multi-variant-per-slug recurs across more than this one model. When it does, (d) is the clean answer and lets the prefix hack be retired.
