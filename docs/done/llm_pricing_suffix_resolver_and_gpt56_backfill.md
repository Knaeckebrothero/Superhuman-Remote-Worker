# By-Model usage shows `—` (no cost) for models whose bare id ≠ the OpenRouter id

**Status:** ✅ RESOLVED 2026-07-15 — shipped in `5b413467` (deployed to
`superhuman-remote-worker` via `bcf43638`, image `sha-5b41346`) and the historical
rows backfilled the same day. Two parts: a forward-looking suffix resolver
(auto-prices new usage, no admin step) + a one-off backfill script for the rows
already written unpriced.
**Severity:** low-medium — cost-observability only; no functional impact. But it
silently under-reports spend on the dashboard (`gpt-5.6-terra`/`-sol` read `—`
despite ~$67 of real usage) and imposed manual per-model toil that scaled with
every new model added.
**Component:** `orchestrator/services/openrouter_pricing.py` (the resolver);
`scripts/backfill_llm_pricing.py` (the historical fix); `tests/test_openrouter_pricing.py`.
**Related:** `reference_llm_cost_pricing_pipeline` (memory — the end-to-end pricing
mechanism); sibling metering gaps `docs/issues/codex_cached_tokens_not_metered.md`,
`docs/issues/system_provider_models_bypass_gateway_unmetered.md`;
`docs/features/usage_dashboard.md`.

## The problem

By-Model cost is a **stored snapshot**: `UsageLedger.record_events` prices each
token row from `usage_rates` once at INSERT (`usage_ledger.py:208-213`); no read
path re-consults rates. `usage_rates` is seeded by `sync_llm_rates`
(`openrouter_pricing.py`) from OpenRouter's public catalog. Matching was an
**exact-key lookup** — `prices.get(pid)` where the fallback candidate for an unset
`params_json.pricing_id` was the lowercased `model_id` (`_pricing_id_for`).

Our recorded model string is the bare `gpt-5.6-terra`; OpenRouter keys it
`openai/gpt-5.6-terra`. Exact lookup missed → left unpriced (`cost_usd` NULL → `—`).
Every regularly-named model therefore needed a hand-set `pricing_id` (Admin →
Models) just to carry the `openai/` prefix — pure toil. `gpt-5.6-terra`/`-sol` were
added without one and read `—` despite real usage; OpenRouter *does* publish their
price (`openai/gpt-5.6-terra` $2.5/$15 per M, `openai/gpt-5.6-sol` $5/$30 + a
$0.50/M cache-read rate).

## The fix

### Part 1 — suffix resolver (forward, `openrouter_pricing.py`)

`_build_price_resolver(prices)` builds two indexes over the OpenRouter catalog and
returns `resolve(candidate)`:
- **`by_full`** — lowercased full id (`openai/gpt-5.5`) → price. Exact match, so an
  admin `pricing_id` carrying the prefix (and any `model_id` that is itself a full
  OpenRouter id) still resolves.
- **`by_suffix`** — the provider-prefix-stripped suffix (`gpt-5.5`) → price, **only
  for suffixes unique across the catalog** (a `Counter` drops any seen >1×).

Resolution: `by_full` first, then unique `by_suffix`. So a bare `gpt-5.6-terra`
auto-matches `openai/gpt-5.6-terra` with no admin mapping. **Ambiguous suffix (two
providers) → left unpriced (fail closed), never mis-priced** — a wrong number on a
cost dashboard is worse than an honest `—`. Measured live: 0 suffix collisions
across 344 catalog models. `_pricing_id_for` is unchanged, so `pricing_id=""`
force-unprice (self-hosted / free) and explicit overrides are preserved exactly.
The one lookup line in `sync_llm_rates` swaps `prices.get(pid)` → `resolve(pid)`.

**What still needs an explicit `pricing_id`:** irregular names the suffix can't
reach — e.g. `gpt-5.3-codex-spark`, whose real price is under `openai/gpt-5.3-codex`
(the resolver correctly refuses to guess `-spark` means `-codex`) — and self-hosted
models (`""` to force-unprice). Everything regularly named now self-prices.

### Part 2 — historical backfill (`scripts/backfill_llm_pricing.py`)

The forward fix only prices *new* rows. Re-pricing the existing `—` rows is more
invasive than a rate insert: `usage_events` is an **append-only ledger** ("NEVER
UPDATE rows", `migrations/audit/0002`), and closed days are served from the
`usage_daily` rollup (which only re-closes a 7-day trailing window). The script is a
deliberate, manually-run one-off (dry-run by default; `--apply`):

1. Resolve the price exactly as the sync does (`list_models` × `_build_price_resolver`).
2. Seed a historical `usage_rates` row (app DB), effective from the resource's
   earliest event.
3. UPDATE the NULL-cost `usage_events` rows (auditdb) — the tightly-scoped,
   idempotent invariant exception.
4. `UsageRollup.run_pass(trailing_days=N)` (full-replace upsert) re-closes the
   affected `usage_daily` days.

DBs: `usage_rates` + `usage_daily` + `rollup_state` = app DB (migrations/app/0033,
0047); `usage_events` = auditdb (migrations/audit/0002).

## Verification

- **Unit** (`tests/test_openrouter_pricing.py`, +9 tests → 29 pass; + `test_audit_usage`
  = 46): unique-suffix auto-match, exact full-id still wins, bare `pricing_id` via
  suffix, ambiguity fails closed (0 inserts), `pricing_id=""` force-unprices despite
  a suffix match, resolver units (exact beats suffix, collision dropped, case-insensitive).
- **Live resolver vs real catalog:** `gpt-5.6-terra`/`-sol` resolve; `gemma-4-moe`
  (self-hosted), `gpt-5.3-codex-spark` (irregular), malformed `MiniMax-M3MiniMax-M3`
  stay unpriced; `gpt-5.5`/`gpt-5.4-mini`/`MiniMax-M3` now auto-resolve (their manual
  `pricing_id`s became redundant).
- **Backfill on `superhuman-remote-worker`:** dry-run matched the ledger, then
  `--apply` repriced **448 rows** (terra 222 = $34.85; sol 226 = $32.52, incl. 52
  cached at the true $0.50/M cache-read rate) and re-closed days 2026-07-10..07-14
  (144 daily rows). Post-checks: **0 remaining NULL-cost** gpt-5.6 rows; a second
  dry-run reports "no NULL-cost rows" (idempotent); the exact serving path
  `UsageRollup.breakdown(group_by="model")` now returns sol **$32.52** / terra
  **$34.85** (the open-tail remainder correctly merged from the raw ledger).

## Notes / follow-ups

- New models auto-price on the next 6h `llm_pricing_sync_loop` tick — no manual step.
- Audit self-hosted models (gemma, RedHat quants) still carry `pricing_id=""` so a
  coincidental suffix match can't start pricing a free model.
- **`srw-prod-private` is a separate deployment/DB** — the backfill only touched the
  experimental env. Re-run there if it has unpriced gpt-5.6 rows.
- Ops note: run the backfill via stdin (`kubectl exec -i … python -`); `kubectl cp`
  fails because the orchestrator image has no `tar`.
