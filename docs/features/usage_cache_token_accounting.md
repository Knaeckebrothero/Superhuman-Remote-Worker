# Cache-token usage & cost accounting

**Status:** Implemented · 2026-07-08
**Related:** `docs/features/observability_and_quotas.md` (the ledger spine),
[[project_prompt_cache_tail_injection]] (the optimization that created the blind
spot), `docs/issues/remove_litellm_proxy_and_gateway_concept.md` (why cost is now
reconstructed from the audit, not a gateway spend log).

## Problem

We just shipped prompt-cache tail-injection (`eecd7faf`) and measured the LLM
prefix-cache hit ratio jump from ~40% to ~75% on the main-cluster loop. That is a
real ~50–60% cut in **input** spend per cached turn at the provider. **None of it
is visible in the product, and the Admin → Usage numbers actively overstate the
cost of every cache-hit turn.**

The provider tells us how many prompt tokens were served from cache. We persist
that number verbatim and then throw it away one hop later. Cost is computed as
`prompt_tokens × full_list_rate` regardless of how many of those tokens were
cached and discounted. So the single biggest per-turn cost lever we have — cache
hit ratio — is neither metered nor surfaced.

## Pre-implementation state (grounded in code before this change)

The metering pipeline is: **agent writes raw usage → materializer extracts a few
flat counts → ledger prices them → rollup aggregates → `/api/usage` serves →
Cockpit renders.** Cache tokens fall out at the *second* step.

1. **Capture — already complete.** `src/core/archiver.py:333` persists the
   *entire* provider `token_usage` dict verbatim into
   `llm_requests.metrics.token_usage` (JSONB, in `srw-auditdb`). For
   OpenAI-protocol responses that dict includes `prompt_tokens_details.cached_tokens`
   when the upstream supports prefix caching. **The raw cache breakdown is already
   on disk for every historical row** — we simply never read it.

2. **Materialize — where it's lost.** `orchestrator/services/audit_usage.py:76-89`
   (`_SELECT_SQL`) extracts exactly five fields: `prompt_tokens`, `input_tokens`,
   `completion_tokens`, `output_tokens`, `reasoning_tokens`. It never reaches into
   `prompt_tokens_details`. The emit loop (`audit_usage.py:231-236`) then produces
   two `UsageEvent`s per call: `prompt-token` (quantity = full prompt) and
   `completion-token`. Cached and uncached prompt tokens are collapsed into one
   full-price line.

3. **Price.** `usage_events` rows are priced from the effective-dated
   `usage_rates` table (`orchestrator/services/usage_ledger.py`, `UsageRates`).
   Rates are auto-seeded from OpenRouter's public catalog by
   `orchestrator/services/openrouter_pricing.py` — but `fetch_openrouter_prices`
   (line 105-113) reads only `pricing.prompt` and `pricing.completion`. There is
   **no `cached-prompt-token` unit and no discount tier.** Self-hosted models
   (`pricing_id = ""`) resolve to no rate → `cost_usd` NULL (metered as tokens
   only).

4. **Serve / render.** `usage_rollup.usage()` sums by `(category, unit)` and
   returns `by_category` + headline `total_cost_usd`; `/api/usage/breakdown`
   groups by `{user, model, project}`. Cockpit renders it in
   `cockpit/src/app/views/admin/usage/admin-usage.component.ts`.

**Net effect:** a turn that got a 75% cache hit is billed exactly as if 0% were
cached. Real provider spend dropped ~50–60%; the Admin total does not move.

### Why the impact is currently low-stakes (but the feature still matters)

The main loop runs on **MiniMax + self-hosted vLLM/gemma**, where "cost" in the
app is either NULL (unpriced) or a list-price *estimate*, and the real cache win
is **compute + MiniMax quota**, not dollars. Modeling the discount produces
dollar-accurate savings only for the auto-prefix commercial providers (OpenAI,
OpenRouter-brokered, and any vLLM build that reports `cached_tokens`). The reason
to build it anyway: **you cannot see the win you just measured (40→75%) anywhere
in the product**, and the moment any priced commercial model carries loop
traffic, the ledger silently overstates its cost.

## Goal / acceptance criteria

1. **Cache tokens are a first-class metered dimension.** Every LLM call that
   reports `cached_tokens > 0` produces a `cached-prompt-token` `UsageEvent`, and
   the plain `prompt-token` line is *net of* cached tokens (see decision D1). The
   two lines always sum to total prompt tokens.
2. **Cost reflects the discount** for models whose cache-read rate is known;
   degrades gracefully (never *understates*) when it isn't (D3).
3. **Cache hit % is legible in the UI.** Admin → Usage shows a cache-hit ratio
   (`cached / (cached + prompt)`) at the headline and in the by-model breakdown.
4. **No regression** to existing `prompt-token` / `completion-token` accounting,
   idempotency, or attribution. Existing dashboards keep working.
5. Metering stays **non-load-bearing** — missing audit pool / absent cache field
   / unpriced model each degrade to "no cache line, cost NULL," never an error.

## Design

### D1 — Net split, not gross + adjustment (recommended)

Represent cached tokens by **splitting the prompt count**, not by bolting a
negative-cost correction line onto a gross total:

- `prompt-token` quantity → `prompt_tokens − cached_tokens` (uncached, full rate)
- `cached-prompt-token` quantity → `cached_tokens` (discounted rate)

The two lines sum to total prompt tokens; cost is a clean `Σ quantity × rate`;
cache-hit % is `cached / (cached + uncached)`. This reads naturally in the
`(category, unit)` breakdown `usage()` already produces, and needs no special
handling in the rollup or the UI beyond a new unit label.

Rejected alternative — keep `prompt-token` gross and add a signed
`cache-discount` line. Uglier (negative quantities/costs), and it breaks the
"quantity is a physical count" invariant the ledger otherwise holds.

### D2 — Dedupe safety

The ledger dedupe key is `ON CONFLICT (source, source_id, unit, ts)`
(`usage_ledger.py:156`). Because it includes `unit`, adding a new
`cached-prompt-token` unit is a **distinct key** — it will not collide with the
existing `prompt-token` row for the same request, and re-materialization stays
idempotent per unit.

⚠️ **Backfill trap (see D5):** this same property means naively resetting the
materializer cursor to re-scan history would *add* `cached-prompt-token` lines on
top of already-materialized **gross** `prompt-token` lines (which `ON CONFLICT DO
NOTHING` will not correct downward) → double-count. Backfill must delete-and-reemit
the window, not just re-run.

### D3 — Rate source & fallback

OpenRouter's catalog publishes a per-model `pricing.input_cache_read` (USD/token)
alongside `prompt`/`completion`. Extend `fetch_openrouter_prices`
(`openrouter_pricing.py:105-113`) to read it and `sync_llm_rates` to seed a
`cached-prompt-token` rate under `category='llm'`.

Fallback when a model publishes no `input_cache_read`:

- **Recommended (conservative):** price cached tokens at the **full prompt rate**
  (no discount modeled). Token accounting stays exact and cache-hit % is always
  visible; dollar cost may *overstate* slightly but never understates — the safe
  default for a billing-adjacent number.
- Alternative: leave the cached line unpriced (`cost_usd` NULL). Exact token
  counts, but dollar totals under-count. Rejected as default because "cheaper
  than reality" is the dangerous direction.

This is a genuine judgment call — flagged as **Open question Q1** for sign-off.

### D4 — Provider protocol coverage (v1 scope)

- **In scope:** OpenAI-protocol `prompt_tokens_details.cached_tokens` (read-only,
  discounted). This is what OpenAI, OpenRouter passthrough, and cache-reporting
  vLLM builds return, and it maps to a single new dimension.
- **Out of scope (follow-on):** Anthropic-protocol `cache_read_input_tokens` +
  `cache_creation_input_tokens`. Anthropic cache *creation* is billed at a
  **premium** (~1.25× base), so it needs a *third* dimension, not a discount — and
  SRW sets no `cache_control` breakpoints today, so there are no Anthropic cache
  tokens to capture yet. This is gated on the separate "cache_control breakpoints"
  work noted in [[project_prompt_cache_tail_injection]]; wire it up together.

### D5 — History / backfill

Default: **going-forward only.** Historical rows keep their gross `prompt-token`
line (consistent with pre-feature behavior — they overcounted before too). No
cursor reset.

Optional backfill of a bounded recent window (e.g. since the tail-injection
deploy `sha-eecd7fa`, so the 40→75% win shows retroactively): a one-shot script
that, per `llm_request` in the window, **deletes** its existing
`(source_id=<llm_request id>)` usage rows and re-emits from the preserved raw
`token_usage` JSONB. Ships behind an explicit flag, not the steady-state loop.
Deferred unless we want the retroactive dashboard.

## Implemented changes (file-by-file)

1. **`orchestrator/services/audit_usage.py`**
   - `_SELECT_SQL`: add
     `metrics->'token_usage'->'prompt_tokens_details'->>'cached_tokens' AS m_cached`.
   - Emit loop: `cached = _first_int(r["m_cached"])`; clamp
     `cached = min(cached, prompt)`; emit `prompt-token` at `prompt − cached` and,
     when `cached > 0`, a `cached-prompt-token` event (same `common` attribution,
     `unit="cached-prompt-token"`). Skip the split when `cached == 0` (unchanged
     behavior for non-caching providers).

2. **`orchestrator/services/openrouter_pricing.py`**
   - `fetch_openrouter_prices`: also parse `pricing.input_cache_read`; widen the
     return to carry a third optional rate (tuple→small dataclass/dict).
   - `_LLM_TOKEN_UNITS` / `sync_llm_rates`: seed `cached-prompt-token` when
     present; apply the D3 fallback when absent.

3. **`orchestrator/services/usage_ledger.py`** — no schema change (unit is just a
   string; dedupe already unit-aware). Confirm `query_usage` groups the new unit
   through untouched.

4. **`orchestrator/services/usage_rollup.py`** — verify the daily rollup groups on
   `unit` (it sums by `(category, unit)`), so the new unit rides through to
   `usage_daily` with no change. Add a derived `cache_hit_ratio` to the `usage()`
   response payload (computed from the two prompt-unit sums) so the UI need not
   re-derive.

5. **Cockpit `admin-usage.component.ts` / `admin-usage.service.ts`** — render the
   `cached-prompt-token` line, a headline cache-hit % badge, and a per-model
   cache-hit column in the breakdown table.

6. **No DB migration required.** `usage_events` / `usage_rates` are already
   unit-keyed generic tables; the new dimension is data, not schema.

## Testing

- **`audit_usage`:** unit test that a row with `prompt_tokens=1000`,
  `cached_tokens=750` emits `prompt-token=250` + `cached-prompt-token=750`; a row
  with no `prompt_tokens_details` emits the single legacy `prompt-token=1000`
  (regression guard); `cached > prompt` clamps rather than emitting a negative
  quantity.
- **`openrouter_pricing`:** a catalog entry with `input_cache_read` seeds the
  third rate; one without it applies the D3 fallback.
- **`usage_ledger` / rollup:** the new unit dedupes independently and sums into
  `by_category` + `total_cost_usd`; `cache_hit_ratio` math on known inputs.
- **Cockpit:** vitest for the new badge/column bindings.
- Full `pytest tests/ -x -q` + `ruff` clean; `cd cockpit && npx vitest run`.

## Verification (local, per CLAUDE.md Plan→Develop→Verify)

With Tilt up: create a session on a cache-reporting model, drive a few turns,
then
`kubectl --context=k3d-srw -n srw exec deploy/srw-orchestrator -c orchestrator -- curl -sf http://localhost:8085/api/usage`
and confirm a `cached-prompt-token` line appears with a non-zero cache-hit ratio;
open Admin → Usage and confirm the badge renders.

## Out of scope / future

- Anthropic cache-read/creation dimensions (D4) — bundle with the `cache_control`
  breakpoint work.
- Retroactive backfill (D5) unless the retroactive dashboard is wanted.
- Per-turn cache-hit visualization in the session/job trace (this feature is the
  aggregate ledger only).

## Decisions

- **Q1:** Models with no published `input_cache_read` price cached tokens at the
  full prompt rate. This is conservative: it may overstate cost but will not
  understate it.
- **Q2:** Implementation is going-forward only. No historical backfill was added.
