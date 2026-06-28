---
tags:
  - issue
  - bug
  - orchestrator
  - llm
  - config
aliases:
  - blob path shadows context_window
  - admin context window override ignored
  - per-model context window shadowing
related:
  - "[[reasoning_aware_max_output_tokens]]"
  - "[[user_defined_experts]]"
  - "[[agent_llm_factory_collapse]]"
  - "[[mcp_created_jobs_ownerless_capability_grant_denied]]"
---

# Admin per-model `context_window` override is shadowed by the family default in the blob dispatch path

**Status:** **FIXED + k3d-verified 2026-06-28** (uncommitted on `develop`).
Found while researching `[[reasoning_aware_max_output_tokens]]`; split out because
it is broader than that feature — it silently **broke every** admin per-model
context-window override on the blob path. Fixed at resolve time via a new helper
`_seed_registry_model_overrides` (`orchestrator/main.py`) seeding the registry
`context_window` into `request_override.llm.model_max_context_tokens` *before*
`resolve_config` bakes the settings matrix, so both the window and the derived
`context_threshold_tokens` (`0.80 ×`) compute off the admin value. See
**## Resolution** at the bottom.
**Component:** orchestrator dispatch (`orchestrator/main.py`,
`orchestrator/services/config_resolver.py`) · agent config loader
(`src/core/loader.py`).
**Severity:** Medium-high — silent. No error; the override just doesn't take
effect. Costs tokens (the opposite of the admin's intent) and blocks the
per-model `max_output_tokens` override planned in
`[[reasoning_aware_max_output_tokens]]` (same mechanism).

## Symptom

An admin sets a per-model context window in **Admin → Models** (persisted to
`models.context_window`) — e.g. to cap `openrouter/minimax/minimax-m3` at 256k
"to save tokens." The setting is **silently ignored**: the agent runs the model
at the **family default** window instead, so context compaction fires far later
than intended (at `0.80 ×` the family window, not `0.80 ×` the admin value), more
input tokens are sent per turn, and the cost-saving never happens.

## Evidence (live)

Job `19707fa1-1788-4eda-a296-8b108429b108` (blob path) ran
`openrouter/minimax/minimax-m3`:

| Source | `model_max_context_tokens` |
|---|---|
| Registry `models.context_window` (admin value) | **262144** (256k) |
| Family `minimax-m3` default (`config/model_config_matrix.yaml:118`) | 1000000 (1M) |
| **Persisted `resolved_config` blob (what actually ran)** | **1000000** ← family wins |
| Persisted `context_threshold_tokens` (derived `0.80 × base`) | 800000 |

```sql
-- reproduce:
SELECT regexp_matches(resolved_config::text,'"model_max_context_tokens":\s*([0-9]+)','g'),
       regexp_matches(resolved_config::text,'"context_threshold_tokens":\s*([0-9]+)','g')
FROM jobs WHERE id='19707fa1-1788-4eda-a296-8b108429b108';
-- → 1000000 / 800000   (registry context_window for the model = 262144)
```

`EXPERTS_DB_ENABLED=true` confirmed on both dev orchestrator replicas.

## Root cause

There are two dispatch delivery paths, and the per-model override only survives on
one of them:

- **Legacy path** (config_name / `config_override`; prod today, experts-DB off):
  `_inject_dispatch_credentials` enriches the **bare** `config_override`
  (`main.py:2138`), so
  `setdefault("model_max_context_tokens", meta.context_window)`
  (`main.py:1615-1616`) **adds** the per-model value, and the agent re-runs
  `_apply_settings_matrix` with the override key protected (`agent.py:1579`) →
  **per-model wins.** ✓
- **Blob path** (`EXPERTS_DB_ENABLED=true`, `main.py:2056` / `:1034` — **the dev
  default**): `config_resolver.resolve_config` runs `_apply_settings_matrix` and
  **bakes the family `model_max_context_tokens` into the blob**
  (`config_resolver.py:136` → serialized at `loader.py:4416`) **before**
  `inject_blob_credentials` runs (`main.py:2106-2109`). `inject_blob_credentials`
  seeds `co["llm"]` from the **already-baked blob** (`config_resolver.py:220`), so
  the `setdefault` at `main.py:1616` finds the key already present and is a
  **no-op → the admin per-model `context_window` is shadowed by the family
  default.** ✗

The two `setdefault` sites — `_inject_dispatch_credentials` (`main.py:1615-1616`,
worker jobs) and `_inject_model_credentials` (`main.py:3583-3584`, sessions +
phase pins) — are the only `model_max_context_tokens` writers in the orchestrator.
`config_resolver` only knows the **family settings matrix**, not the per-model
registry row, so at bake time it has no way to apply the override.

The loader-side precedence (`loader.py:782-787`: dispatch-injected
`model_max_context_tokens` beats the family `settings:` value) is correct — the
bug is purely that the injected value never reaches the blob.

## Impact

- **Every** admin per-model `context_window` override is ignored on the blob path
  (dev now; prod once `EXPERTS_DB_ENABLED` flips on). Both **sessions**
  (`main.py:3584`) and **worker jobs** (`main.py:1616`).
- Caps set **down** for cost are silently defeated (token-saving fails — the model
  runs at the larger family window). Caps set to match a provider's real window
  are also lost.
- **Blocks `[[reasoning_aware_max_output_tokens]]`** — its planned per-model
  `max_output_tokens` override (registry `params_json`) would be injected the same
  way and shadowed identically, and its compaction-aligned output backstop
  (`≈ 0.20 × effective_ctx`) computes off the wrong (family) window until this is
  fixed.

## Fix

Make the per-model registry values reach `config_resolver.resolve_config` as an
**explicit override layer** that is applied **before** the matrix bakes — so they
sit in `explicit_llm_keys` and the family value does not re-bake over them
(`agent.py:1579` already protects such keys on the legacy path). Equivalently:
the resolver must consult the per-model registry row, not only the family matrix.

> ⚠️ **Do not "fix" this by post-hoc overriding `co["llm"]["model_max_context_tokens"]`
> after the blob is built.** `context_threshold_tokens` is **derived** as
> `0.80 × base` at resolve time (`loader.py:788`, `CONTEXT_THRESHOLD_FRACTION` at
> `:44`). A post-hoc patch of just the context window would leave the threshold at
> `0.80 × family` (e.g. 800k) — inconsistent, and the compaction trigger would
> still be wrong. Fix at **resolve time** so both the window and the derived
> threshold are computed from the correct per-model base.

Apply the same fix shape to **both** injection sites (`main.py:1615-1616` and
`:3583-3584`) and make it carry the future per-model `max_output_tokens` too, so
`[[reasoning_aware_max_output_tokens]]` rides the same corrected path.

## Verification

- **Repro (today):** `19707fa1` blob shows 1000000 / 800000 while the registry
  has 262144 (above).
- **After fix:** dispatch `openrouter/minimax/minimax-m3` on dev (blob path) →
  `resolved_config` (or agent loader log `max_context_tokens=`) shows **262144**,
  and `context_threshold_tokens` ≈ **209715** (`0.80 × 262144`).
- **Regression:** a model with **no** per-model `context_window` still resolves to
  the family default.
- **Legacy path** (experts-DB off) continues to honor the override (it already
  did — don't break it).

## Notes / gotchas encountered

- There are **no `config_resolver` precedence tests** today — add one for
  "per-model registry override beats family default in the blob path."
- Verifying via a fresh MCP-created worker job hit two unrelated frictions worth
  knowing: MCP `create_job` leaves `user_id=NULL` →
  `[[mcp_created_jobs_ownerless_capability_grant_denied]]` (job stuck `waiting`),
  and the `default` expert auto-spawns a scholar subjob. The cleanest way to
  observe the blob is the persisted `resolved_config` of an already-dispatched job
  (as done here), not a new dispatch.

## Resolution (2026-06-28)

Fixed exactly as the **## Fix** section prescribes — at resolve time, not
post-hoc — because the agent never re-derives `limits`: `load_config_from_resolved`
→ `load_agent_config_from_dict` (`src/core/loader.py`) parses the frozen blob
without re-running `_apply_settings_matrix`, so an injector-only patch of
`llm.model_max_context_tokens` would leave the baked `context_threshold_tokens`
stale. The value must reach `data["llm"]` *before* the matrix runs.

**Change set (uncommitted on `develop`):**

- `orchestrator/main.py` — new async helper `_seed_registry_model_overrides(request_override, *, user_id)`:
  resolves the model's registry `meta` via `_resolve_model` and `setdefault`s
  `meta.context_window` into a copy of `request_override.llm.model_max_context_tokens`.
  `config_resolver` stays pure/sync — the DB lookup lives in the caller. The value
  rides the existing `request_override` layer: its llm keys enter `explicit_llm_keys`
  (matrix won't clobber) and deep-merge into `data["llm"]` (becomes the derivation
  base at `loader.py:782`). `setdefault` semantics: an explicit caller pin still
  wins; the family default loses.
- `orchestrator/main.py` — wired into **both** blob `resolve_config` callsites:
  worker dispatch (`_dispatch_job_to_agent`) and session attach
  (`_resolve_session_config`). The legacy path (`EXPERTS_DB_ENABLED` off) does not
  call `resolve_config`, so it is untouched — its `_inject_dispatch_credentials` /
  `_inject_model_credentials` `setdefault` stays the mechanism there and becomes a
  harmless no-op on the blob path once the value is baked.
- `tests/test_config_resolver.py` — two precedence tests (the documented gap):
  a matrix-independent keystone (override `262144` → `262144` / `209715`) and a
  fallback guard (no override → larger family default, `0.80 ×` relationship intact).

**Verification:**

- Unit: 2 new tests + 95 resolver/settings-matrix/hydrate green; 51
  dispatch/override-loader regression tests green; `ruff check` + `ruff format`
  clean.
- **k3d live probe** (real helper vs the live registry row
  `openrouter/minimax/minimax-m3`, admin `context_window=32000`, family default
  1M): the real `_seed_registry_model_overrides` read `meta.context_window=32000`,
  and `resolve_config` baked **`model_max_context_tokens=32000` /
  `context_threshold_tokens=25600`** (`int(32000 × 0.80)`). Resolving the bare
  override (no seed) reproduced the pre-fix **`1000000 / 800000`** — matching
  `19707fa1`'s evidence. Both callsites confirmed present in the Tilt-synced pod.

**Forward link:** `[[reasoning_aware_max_output_tokens]]`'s per-model
`max_output_tokens` override now rides this same helper — add a field to
`ModelMeta` (`src/core/model_registry.py`) + the row→meta builders, then one more
`setdefault` in the helper.

**Deferred:** commit + push; phase-pin (strategic/tactical) sections still resolve
their windows via the injector `setdefault` only — fine today (top-level chat model
was the live bug), revisit if a phase pin needs a per-model cap.
