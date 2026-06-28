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

**Status:** **Confirmed live 2026-06-28** (dev, `EXPERTS_DB_ENABLED=true`). Not
yet fixed. Found while researching `[[reasoning_aware_max_output_tokens]]`; split
out because it is broader than that feature — it silently breaks **every** admin
per-model context-window override on the blob path.
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
