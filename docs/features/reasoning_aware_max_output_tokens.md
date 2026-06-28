---
tags:
  - feature
  - agent
  - llm
  - context-management
  - reasoning
aliases:
  - reasoning-aware max output
  - per-family max output tokens
  - max_output_tokens family setting
  - length truncation handling
related:
  - "[[session_empty_response_gpt5_codex_stop]]"
  - "[[context_summarization_rework]]"
  - "[[family_centered_reasoning]]"
  - "[[usage_monitoring_and_rate_limiting]]"
  - "[[model_assembly]]"
  - "[[builder_to_sessions_consolidation]]"
  - "[[per_model_context_window_override_shadowed_in_blob_dispatch]]"
---

# Reasoning-aware max output tokens — per-family/per-model output windows + length-truncation handling

**Status:** Proposed, **research-backed + cluster-verified 2026-06-28** (6
subagents: 3 codebase traces, 3 web; §7.4 confirmed live, see below). Motivated
by the minimax-m3 "empty response" investigation (session `a0f826d7`).
Implementation-ready pending sign-off on the per-family numbers.

**Routing note (2026-06-28):** all models now route through the **LiteLLM
gateway** (route-all `*` wildcard), with the direct route kept as fallback. This
is **transport only** — the output cap is set in the loader resolver *upstream*
of transport, so the core of this feature (§5, §5.4, §6, §7.4) is unaffected.
The factory is unchanged (`provider_ref=openrouter` → `_create_openrouter_llm`);
only the base_url moved to the gateway. The transport-specific observations
(§4/§7.3 OpenRouter reasoning limits, §7.1 doubled finish_reason) are now
*mediated by the gateway* and flagged for re-confirmation on the gateway path.

**Evidence base:** session `a0f826d7-eaa6-4ae7-960a-1c9bb776b712` on the main
cluster (`openrouter/minimax/minimax-m3`, 2026-06-28). Two turns rendered "⚠ The
model returned an empty response." Audit (`srw_audit.llm_requests`):

| iter | `finish_reason` | answer chars | reasoning chars | result |
|------|-----------------|--------------|-----------------|--------|
| 1 | `length` | 0 | 53,997 | ❌ empty |
| 2 | `length` | 0 | 58,427 | ❌ empty |
| 3 | `stop` | 841 | 1,110 | ✓ |
| 4 | `stop` | 2,552 | — | ✓ |

~16.5k tokens of reasoning consumed the entire 16,384-token output budget;
`finish_reason=length` before any answer. This is **not** the gpt-5.x codex
`stop`-with-empty bug (`[[session_empty_response_gpt5_codex_stop]]`, ~0.7%
non-deterministic, zero content). This is `finish_reason=length`, deterministic
on reasoning-heavy turns — a plain re-run re-hits the same cap.

> **Two bugs discovered while researching this doc — both impact users today,
> independent of this feature:**
> 1. **§7.1** — the OpenRouter-direct route doubled `finish_reason`/`model_name`
>    in captured metadata (`"lengthlength"`); a LangChain stream-merge defect.
>    **Likely resolved for free by the gateway migration** (LiteLLM re-streams
>    with a single finish_reason chunk) — the merge defect itself is unchanged,
>    so verify on the next minimax *session* turn through the gateway.
> 2. **§7.4 — FIXED + k3d-verified 2026-06-28** (was CONFIRMED LIVE). In the **blob dispatch path
>    (`EXPERTS_DB_ENABLED=true`, the dev default)**, an admin's per-model
>    `context_window` override is **shadowed by the family default.** Verified
>    against job `19707fa1` (ran `openrouter/minimax/minimax-m3`, registry
>    `context_window=262144`): its `resolved_config` blob baked
>    **`model_max_context_tokens: 1000000`** (family) with
>    `context_threshold_tokens: 800000`. **So a model capped to 256k in the UI is
>    running at the family 1M window — compaction fires at 800k not ~205k, and
>    the token-saving is silently defeated.** Fix in §7.4.

---

## 1. Problem

The agent caps **every** model's output at **16,384 tokens** regardless of the
model's real output window. For a reasoning model, reasoning tokens count against
that same budget, so on a hard turn the chain-of-thought can consume the whole
16k and leave nothing for the answer → a `length`-truncated, empty-content
response surfaced as "The model returned an empty response."

~90% of real turns finish under 16k, so the cap is invisible most of the time —
but it bites exactly the deep, high-reasoning turns we most want to succeed, on
models whose true output windows are far larger (minimax-m3 → 512k; gpt-5.x →
128k; deepseek/o-series → 32–64k+). It is a one-size-fits-all floor that starves
the models that need room most.

## 2. Root cause: the 16,384 global cap

One shared resolver, fed by an "auto" config default:

- **Config defaults to auto.** `config/defaults.yaml:16` +
  `config/persistent_defaults.yaml:23`: `max_output_tokens: null`.
- **The loader derives it.** `src/core/loader.py:_resolve_max_output_tokens`
  (`:2748`). Honors `config.max_output_tokens` first (`:2767`), else
  `return min(16384, ctx // 4)` (`:2773` — the cap), else `8192` (`:2774`). For
  any model with ctx ≥ ~64k, the hard-coded `16384` always wins.
- **Bound onto the client.** All six non-Anthropic factories call the resolver
  and bind the result; **minimax routes through `_create_openrouter_llm`, not
  `_create_openai_llm`** (doc was previously wrong):

  | Factory | def | resolve | bind |
  |---|---|---|---|
  | `_create_openai_llm` | `:2777` | `:2903` | `:2904` `max_tokens` |
  | **`_create_openrouter_llm`** (minimax) | `:3152` | `:3259` | `:3260` `max_tokens` |
  | `_create_google_llm` | `:3026` | `:3059` | `:3060` `max_output_tokens` |
  | `_create_groq_llm` | `:3092` | `:3138` | `:3139` |
  | `_create_mistral_llm` | `:3298` | `:3351` | `:3352` |
  | `_create_codex_llm` (gpt-5.x) | `:3377` | `:3472` | `:3473` |

  Dispatch is `create_llm` (`:2701`, routing `:2732-2745`). One resolver edit
  covers all six.
- **The Anthropic path is separate** — `_create_anthropic_llm` (`:2937`) does
  **not** call the resolver; it honors `config.max_output_tokens` first
  (`:2970-2971`) but has its own hard-coded fallback ladder (`32000` `:2977` /
  `16384` `:2979` / `min(8192, ctx//4)` `:2981`,`:2983` / `4096` `:2985`). The
  family value already flows in; only the ladder needs the §5.5 bump.

The `16384` exists as a **runaway-generation guard** for the gemma4 + xgrammar
repetition loop (vllm#40080) — a blunt band-aid, not a per-model policy. That bug
is **still open** as of 2026-06 and structured-output-triggered; see §8 for the
modern replacement.

**Settings flow (confirmed, good news):** there is **no allowlist to edit**.
`max_output_tokens` is already an `LLMConfig` field (`:1290`), parsed by
`_parse_llm_config` (`:1853`); the settings-merge flat-key loop (`:750-771`)
copies every family `settings:` key into `data["llm"]` (skip-set: `limits`,
`image_tokens`, `pdf_render_dpi`; expert-override guard `:769`). So a family
`settings.max_output_tokens` flows into `config.max_output_tokens` with **zero
plumbing changes**; only the resolver logic needs editing. (Same is true of
`timeout` — see §7.2.)

## 3. How frontier labs avoid "all thinking, no answer" (reference)

Reasoning tokens count against the output budget *everywhere*; the labs avoid the
starvation failure with mechanisms SRW uses neither of:

- **OpenAI o-series / GPT-5.x:** reasoning tokens count against
  `max_completion_tokens` / `max_output_tokens` and are billed as output.
  Official guidance: **reserve ≥ 25,000 tokens** for reasoning + output, then
  tune down using observed `reasoning_tokens`. Truncation → `finish_reason:
  length` (Chat) / `status: incomplete, reason: max_output_tokens` (Responses).
  Empty reasoning-only completions are explicitly documented. Output cap 128k.
- **Anthropic:** older API **enforces `budget_tokens < max_tokens`** (a 400
  otherwise) → answer headroom is structurally guaranteed. 2026 models reject
  `budget_tokens` and use **adaptive thinking** (model trained to converge) +
  **Task Budgets** (`output_config.task_budget`, min 20k) — a countdown the model
  *sees* and self-moderates against. Rec: **`max_tokens ≥ 64k` at high effort**.
  Truncation → `stop_reason: max_tokens`.
- **Google Gemini:** the sharpest failure of all — thinking counts against
  `maxOutputTokens` with **no automatic answer reservation**; when thinking eats
  the budget the API returns candidates with **no content field**. Caller must
  size `maxOutputTokens > thinkingBudget + answer`.
- **OpenRouter unified `reasoning`:** `effort` maps to ~% of `max_tokens`
  (max/xhigh ≈ 95%, high ≈ 80%, medium ≈ 50%, low ≈ 20%, minimal ≈ 10%);
  `max_tokens` = explicit reasoning budget (min 1024, "must be strictly higher
  than the reasoning budget"); `enabled`/`exclude`. Reasoning billed as output.
  **Caveat:** some models silently drop these (minimax does — see §4).

Takeaway formula (from the cross-provider survey):
`max_output = reasoning_allowance + answer_reserve`, where
`answer_reserve = max(8k, p95_answer_tokens)` (16k+ for code/long docs), clamped
to the model cap, with a ≥25k floor for OpenAI reasoning families.

## 4. MiniMax-specific findings (community, June 2026)

Per-model reality (native vs OpenRouter; **OpenRouter caps vary by provider**):

| | M2.5 | M2.7 | M3 |
|---|---|---|---|
| native context | 204,800 | 204,800 | 1,048,576 (≥512k guaranteed) |
| native max output | 131,072 | 131,072 | **512,000** |
| OpenRouter max output (provider-dependent) | 32,768 (DeepInfra/Venice) … 131,072 (minimax) | 131,072 | 131,072 (Novita) … 1,048,576 (Parasail); minimax-native 512,000 |

**Three** empty-response failure modes (SRW has A; must be robust to B and C):
- **(A) reasoning exhausts `max_tokens`** → HTTP 200, empty content,
  `finish_reason=length` (SRW's case). Bigger `max_tokens` "only masks it" but is
  the practical fix — it lets reasoning finish *then* answer.
- **(B) reasoning emitted as `<think>…</think>` inside `content`** on the
  OpenAI-compat endpoint (`reasoning_split=false`); clients that don't strip it
  show empty/garbled output.
- **(C) [OpenRouter-specific]** when the response carries **both** `content` and
  `reasoning_details`, some clients drop `content` → `input:0, output:0`
  "incomplete turn." Confirmed for minimax-m2.7. SRW must read `content`
  regardless of `reasoning_details` and feed `reasoning_details` back across
  turns (interleaved thinking).

**⚠ Correction to earlier advice — OpenRouter cannot disable or budget-cap
MiniMax reasoning:**
- `reasoning: {enabled: false}` → **HTTP 400 "Reasoning is mandatory for this
  endpoint and cannot be disabled."**
- `effort` and reasoning `max_tokens` are **ignored** for minimax (OpenRouter
  only maps budgets to Anthropic/Gemini/Qwen).
- `exclude: true` only *hides* reasoning — still computed and billed.
- **On OpenRouter the only real lever is overall `max_tokens`.** Disabling or
  budgeting reasoning requires the **native MiniMax endpoint** (Anthropic-compat
  `thinking:{type:enabled, budget_tokens:N}` — MiniMax's officially recommended
  agentic path; or OpenAI-compat `thinking:{type:disabled}`, **M3 only**).

**Provider-routing nondeterminism:** without pinning, OpenRouter can silently
route M3 to a 131k-output provider (or M2.5 to a 32k one). **Pin provider**
(`provider: {order:[...], allow_fallbacks:false}`) so the effective cap and
reasoning behavior are deterministic.

**The ~17,100 report was a client bug**, not an OpenRouter limit — LibreChat
mis-derived it from "System" default metadata (fixed in their PR #12673). Real
M2.7 = 204,800 ctx / 131,072 output. Lesson: **set explicit per-model caps;
don't trust auto-resolution.**

## 5. Design

### 5.1 `max_output_tokens` as a family setting

Add `max_output_tokens` to each family's `settings:` block in
`config/model_config_matrix.yaml`. Confirmed to flow with zero plumbing (§2). The
Anthropic path picks it up too (`:2970`).

### 5.2 Per-model override (registry) — **IMPLEMENTED + k3d-verified 2026-06-28**

Output windows are ultimately per-model. An optional per-model value rides
`models.params_json` (`{"max_output_tokens": N}`, no migration), applied at
dispatch via the **same path** as `context_window` (the §7.4 fix made that path
correct):

- `ModelMeta.max_output_tokens` field (`model_registry.py`).
- `_params_max_output_tokens(row)` extracts it from the row's parsed `params_json`
  (dict-only — catalog rows arrive parsed via `postgres._row_to_model`; returns a
  positive int or None). Wired into all three row→meta builders
  (`_endpoint_row_to_meta`, both `_catalog_row_to_meta` branches).
- Seeded into dispatch at the three sites that mirror `context_window`:
  `_seed_registry_model_overrides` (blob path), `_inject_dispatch_credentials`
  (legacy/resume), and the per-capability section inject (chat slot).

**Mechanism (corrected — *not* "override semantics"):** the seed does
`llm.setdefault("max_output_tokens", meta.max_output_tokens)` into
`request_override.llm` *before* `resolve_config`. Because `request_override`'s llm
keys enter `explicit_llm_keys`, `_apply_settings_matrix` **skips** the family value
for that key, so the per-model value survives into the baked blob and wins; then
`_resolve_max_output_tokens` clamps it to the §5.4 backstop. A caller-pinned
per-job value still wins (setdefault). Same shadow-avoiding path §7.4 established
for `context_window` and §7.5 unblocked for the family value.

**k3d-verified:** baked = per-model (not family); unclamped when ≤ backstop;
clamped to the backstop when above it; family floor when no per-model value.

> **UI gap (follow-up):** the Admin → Models form has no `max_output_tokens`
> field yet — it writes `context_window` and only a `voice` key into `params_json`.
> So per-model output caps are **DB/API-settable today, not UI-settable**. Adding a
> number input → `params_json.max_output_tokens` is a small cockpit follow-up
> (`admin-models.component.ts`).

### 5.3 Resolution order

```
max_output =
    per_model_output_override    # registry params_json (§5.2, §7.4)
      ?? family_max_output       # matrix settings.max_output_tokens (the 90% lever)
      ?? bumped_default          # global fallback, 16384 → 32768 (§5.5)
max_output = min(max_output, relative_backstop, ABSOLUTE_CEILING)   # §5.4 / §5.5
```

### 5.4 Compaction-aligned backstop (adapts to admin-overridden context)

The backstop bounds output by the *effective* context so a token-saving context
cap proportionally shrinks output. **The formula is sound and both inputs are
already available** inside `_resolve_max_output_tokens(config, limits)`:
- `effective_ctx = config.model_max_context_tokens` (`:2769`)
- `compaction_threshold = limits.context_threshold_tokens` =
  `base × CONTEXT_THRESHOLD_FRACTION` where the fraction is **0.80**
  (`loader.py:44`, applied `:788`). Both derive from the same `base`, so they're
  consistent.

```
backstop      = effective_ctx − compaction_threshold − SAFETY_MARGIN
              ≈ 0.20 · effective_ctx − margin          # since threshold = 0.80·ctx
max_output    = max(MIN_FLOOR, min(resolved_value, backstop, ABSOLUTE_CEILING))
```

This guarantees post-compaction input + output ≤ context (no overflow 400s).
`SAFETY_MARGIN` is **load-bearing** (the threshold is a *trigger* input can
briefly overshoot before the next compaction) — do not set it to 0.

Worked example (minimax, admin ctx 262,144), **k3d-verified on the deployed
artifact**: `backstop = 262144 − 209715 − 4096 = 48333`. So the 64k family value
clamps to **48,333** — still ~3× today's 16k, and the failed turn (12k input) had
ample room. For the 1M family default: `1M − 800k − 4k = 195,904` ⇒ the 65536
value is **unclamped** (→ 65536). **Prerequisites (both now FIXED + k3d-verified
2026-06-28):** §7.4 — the admin `context_window` actually reaches
`model_max_context_tokens` (was shadowed in the blob path); §7.5 — the family
`max_output_tokens` actually reaches `config.max_output_tokens` (the base
`max_output_tokens: null` shadowed it). **Floor caveat:** below ~33k effective
context the backstop hits the `MIN_FLOOR` (4096) — too small a window re-starves
reasoning (k3d's minimax is set to ctx 32000 → 4096; see §7.5).

### 5.5 Bumped default + absolute ceiling

- Global fallback (`loader.py:2773`, and the Anthropic ladder `:2977-2985`):
  raise `16384` → **32768**.
- **Absolute hard ceiling 131072 (128k)** — the largest legitimate single-turn
  output across current reasoning models (GPT-5.x 128k, Gemini 65k, DeepSeek
  32–64k). Nothing legitimate exceeds it; it's the outermost fail-safe above the
  relative backstop.

### 5.6 Proposed per-family values (tune for cost/latency)

| Family | `max_output_tokens` | Notes |
|--------|--------------------:|-------|
| `minimax-m3` | 65536 | < Novita's 131k cap → safe on *any* OpenRouter provider; pin provider (§4) to go higher |
| `minimax` (M2.7) | 49152 | native 131k; safe everywhere |
| `minimax` (M2.5, if used) | 32768 | some providers cap output at 32,768 — pin to exceed |
| `deepseek` | 65536 | 1M-ctx hybrid reasoning |
| `o-series` | 65536 | reasoning |
| gpt-5.x / codex | 65536 | ≥25k OpenAI floor satisfied |
| gemma / non-reasoning chat | 16384–32768 | little reasoning |
| **global default** | 32768 | replaces 16384 |

Numbers are starting points — the §5.4 backstop clamps each to the effective
context, and cost is bounded (loosely — see §9) by the quota system, so erring
generous is safe.

## 6. Length-aware retry / fallback

Once the cap is generous, hitting the ceiling is rare; for the rare case handle
it **fluently** and **length-aware** (a plain re-run re-hits the cap — the
existing `stop`+empty reasoning retry at `persistent_graph.py:1552` cannot fix
`length`). Branch on whether there is visible content:

- **Empty / reasoning-only** (the core bug): retry once with **raised
  `max_tokens`** (×1.5–2, up to the §5.4 backstop) **and/or lower reasoning**
  (where controllable — *not* minimax-on-OpenRouter, §7.3). Do **not** "continue"
  — there's nothing to continue from.
- **Truncated mid-answer** (has content): **continue** (append the partial, raise
  the cap, re-request).
- Still truncating → surface a **"truncated at output limit"** message (distinct
  from "empty response"). **Never** treat a truncation as a successful turn.

**Hook points (exact):**
- **`src/persistent_graph.py`** (streaming): `finish_reason` lands in `meta`
  (`:1448`) but is **doubled** on the minimax route (§7.1) — read the clean
  per-chunk value from the `:1179` collection loop instead, or depend on the §7.1
  fix. Insert a new branch in the empty block (`:1531-1628`) between the refusal
  check (`:1548`) and the existing reasoning-retry (`:1552`). Mirror at the
  ainvoke-fallback empty branch (`:1400-1421`, un-doubled there).
- **`src/graph.py`** (worker): uses `ainvoke` (`:1458-1461`) → no doubling; read
  `response.response_metadata.get("finish_reason")`. Today a `length`-empty turn
  accrues toward `_check_empty_response_streak` (`:582`, evaluated `:1544-1549`)
  and after >3 **hard-fails the job** (`:1574-1594`, `recoverable:False`). Hook
  the length check after `content_len` is computed (`:1462-1479`) and **before**
  the streak check, so the worker retries-bigger instead of failing.

## 7. Prerequisites & interactions

### 7.1 Doubled `finish_reason` / model-name (bug; blocker for §6)

Root cause is a **LangChain stream-merge defect**, not SRW's audit writers (they
pass metadata through verbatim: `archiver.py:152-154`, `session_components.py:78`,
`response_guards.py:66`). Chain: `persistent_graph.py:1233` does
`response = response + chunk`; `AIMessageChunk.__add__` → langchain_core
`merge_dicts` **concatenates same-key string values** (`_merge.py:64`), and
`finish_reason`/`model_name` are **not** in the exempt set (`:59-63`).
`langchain_openai` writes both fields **per finish-bearing chunk**
(`base.py:1199-1202`), and **OpenRouter emits finish_reason on two chunks** (final
content + trailing `stream_usage` chunk, enabled `loader.py:3275`) → 2× concat →
`"lengthlength"`. Native OpenAI/vLLM emit it on one chunk (no concat); the worker
path uses `ainvoke` → `_create_chat_result` (no merge) — exactly matching the
observed route-specificity.

Fix (pick one; 2/3 also clean the audit): (1) tolerant `"length" in finish_reason`
in the §6 branch; (2) normalize `response.response_metadata` right after the merge
at `:1233`; (3) **cleanest** — capture `finish_reason` from the last per-chunk
`chunk.response_metadata` in the `:1179` loop and never trust the merged value.

**Gateway migration likely resolves this for free.** The doubling required the
*upstream* to emit `finish_reason` on two chunks (OpenRouter-direct: final content
+ trailing usage). Routing now goes agent → **LiteLLM gateway** → OpenRouter →
minimax, and LiteLLM re-streams with the standard single-finish_reason pattern
(usage chunk has empty `choices`) → no concat. So §7.1 may already be moot for
minimax on the gateway path — **verify on the next minimax *session* turn**
(streaming; the audit `finish_reason` reads `"length"` not `"lengthlength"`).
Regardless, build §6 on fix (3) (per-chunk read) so it's robust either way, and
keep this fix if any provider still double-emits. *Cluster check 2026-06-28: the
only minimax data predates the gateway migration (`a0f826d7` doubled via
streaming; `19707fa1` clean via `ainvoke`), so the gateway-path doubling is
unconfirmed — first organic gateway session turn settles it.*

### 7.2 Timeout is a parallel ceiling — **IMPLEMENTED + k3d-verified 2026-06-28**

`LLMConfig.timeout` (`:1286`, default 600s from `config/defaults.yaml:14` +
`persistent_defaults.yaml:21`) is applied per-provider (`:2889-2890` openai,
`:2966-2967` anthropic, `:3055-3056` google, …). At ~50 tok/s, 600s only covers
~30k tokens — so bumping `max_tokens` to 200k without scaling the timeout trades
"length truncation" for "timed out after 600s." **Scale timeout with `max_tokens`
at construction** in each factory (right after the resolver call, e.g.
`loader.py:2903`); the LLM is built per-dispatch with `max_tokens` known, so the
static case needs **no per-call plumbing**. (Per-call override is awkward — the
httpx deadline is baked at `reasoning_chat.py:520`/`:742` — and only the deferred
dynamic-max_output variant needs it.) `timeout` is also a flowed-through settings
key, so per-family timeouts are config-only if wanted.

**Implemented:** `_resolve_timeout(config, limits)` (`loader.py`, beside the output
resolver) = `max(base, round(max_tokens / 30 + 60))` — a conservative 30 tok/s
decode rate + 60s overhead, floored at the configured base, recomputed from the
*resolved* cap so it adapts to an admin `context_window` via the same `limits`.
Wired **value-only** into all 7 factory timeout sites (`config.timeout` →
`_resolve_timeout(config, limits)`); the per-factory log line now prints the
effective (scaled) timeout. k3d-verified through the real config path: minimax-m3
65536 → **2245s**, @256k 48333 → **1671s**; small/old caps keep ~600s; the 131072
ceiling bounds it at **~4429s** (~74 min). Per-family `timeout` overrides remain
config-only (flowed settings key).

### 7.3 Reasoning control for minimax — corrected

The "user can dial reasoning down in the UI" escape hatch is **not achievable for
minimax-on-OpenRouter** (§4: `enabled:false` 400s, effort/budget ignored). The
`binary_toggle`/`extra_body` machinery exists and works (`loader.py:2880-2886`;
gemma uses it, `model_config_matrix.yaml:386-396`), and is still the right
mechanism for **other** families and for self-hosted/native minimax — but for the
OpenRouter route:
- **Do NOT emit `reasoning:{enabled:false}` for any `minimax-*`** (it 400s) —
  omit the param.
- Practical minimax-on-OpenRouter levers: **raise `max_tokens`** (the fix), plus
  `extra_body` hardening — `reasoning_split:true` + `include_reasoning:true` (keep
  reasoning out of `content`, defeats modes B/C) and a **pinned provider** (§4).
- To genuinely disable/budget minimax reasoning, route to the **native MiniMax
  endpoint** (Anthropic-compat `budget_tokens`, or OpenAI-compat
  `thinking:{type:disabled}` for M3) — a bigger lift; §12.
- Adding `reasoning:{effort:…}` via `extra_body` for providers that *do* honor it
  is a ~5-line addition to the effort_enum branch (`:2872-2879`).

### 7.4 Blob-path shadowing of per-model overrides (bug; **FIXED + k3d-verified 2026-06-28**; prerequisite for §5.4)

> **Tracked as its own issue (RESOLVED):**
> `docs/done/per_model_context_window_override_shadowed_in_blob_dispatch.md`
> (`[[per_model_context_window_override_shadowed_in_blob_dispatch]]`). **Fixed**
> via the new `_seed_registry_model_overrides` helper (`orchestrator/main.py`)
> seeding the registry `context_window` into `request_override` *before*
> `resolve_config` bakes the settings matrix, wired into both blob callsites
> (worker dispatch + session attach). This feature's per-model `max_output_tokens`
> override now rides the same corrected path (one added `ModelMeta` field +
> `setdefault`). Summary retained here for context.

**Verification:** job `19707fa1` (blob path, `EXPERTS_DB_ENABLED=true`) ran
`openrouter/minimax/minimax-m3` whose registry `context_window` is **262144**,
yet its persisted `resolved_config` baked **`model_max_context_tokens: 1000000`**
(the family default) + `context_threshold_tokens: 800000`. The admin override was
shadowed — exactly the static trace below.

There are two dispatch delivery paths:
- **Legacy** (config_name/config_override; prod, experts-DB off):
  `_inject_dispatch_credentials` enriches the *bare* override (`main.py:2138`), so
  `setdefault("model_max_context_tokens", meta.context_window)` (`:1616`) **adds**
  the per-model value and the agent re-runs the matrix with it protected
  (`agent.py:1579`) → per-model **wins**. ✓ (This is the path the doc's
  `loader.py:782` precedence describes.)
- **Blob** (experts-DB on, `main.py:2056`/`:1034` — **the dev default, and the
  path `a0f826d7` ran**): `config_resolver.resolve_config` bakes the **family**
  `model_max_context_tokens` into the blob (`config_resolver.py:136` → serialized
  `loader.py:4416`) **before** `inject_blob_credentials` runs (`main.py:2106`),
  which seeds `co["llm"]` from the already-baked blob (`config_resolver.py:220`).
  So the `setdefault` at `:1616` is a **no-op → the admin per-model
  `context_window` is shadowed by the family default.**

**Implications:** (a) the admin's 256k minimax cap is very likely **not in
effect** in the blob path right now (running at family 1M → compaction far too
late; the token-saving intent is silently defeated); (b) a per-model
`max_output_tokens` injected the same way (§5.2) would be **shadowed by the family
`max_output_tokens`** too. **Fix:** apply per-model overrides with **override (not
`setdefault`) semantics**, or feed them into `resolve_config` as an explicit
layer (e.g. `request_override` so they sit in `explicit_llm_keys` and the matrix
doesn't re-bake the family value). The two `setdefault` sites (`main.py:1616`,
`:3584`) are the only `model_max_context_tokens` writers and there are no
config_resolver precedence tests. **Confirmed live** via `19707fa1`'s blob (see
top of this section) — the fix lands per-model output overrides *and* repairs
every admin `context_window` override in the blob path.

### 7.5 Base-config `max_output_tokens: null` shadowed the family matrix value (bug; **FIXED + k3d-verified 2026-06-28**; prerequisite for §5.1)

Sibling of §7.4, but at the *base-config* layer instead of the registry layer, and
it silently made **§5.1 (T1) a no-op in both dispatch paths** until fixed.

**Symptom (caught only by k3d integration, not parse-checks):** after adding
`settings.max_output_tokens` to 12 families (§5.6), the live blob path still
resolved `minimax-m3` to **32768** (the §5.5 default), not the family **65536**.
The baked blob carried `agent.llm.max_output_tokens = None`.

**Root cause:** both base configs declared `llm.max_output_tokens: null`
(`config/defaults.yaml:16`, `config/persistent_defaults.yaml:23`). That key lands
in `explicit_llm_keys` (`config_resolver.resolve_config` via `_raw_leaf_llm_keys`,
and the agent's equivalent in `load_agent_config`), and `_apply_settings_matrix`'s
flat-key loop **skips any key already in `expert_llm_keys`** (`loader.py:769`).
So the family `max_output_tokens` was never written into `data["llm"]`, leaving
`config.llm.max_output_tokens = None` → resolver falls back to the default. Unlike
`model_max_context_tokens` — which survives because the limits-derivation has an
explicit family-value fallback (`loader.py:783-784`) — `max_output_tokens` has
**no fallback**, so the shadow was total.

**Fix:** removed the `max_output_tokens: null` declaration from both base configs
(replaced with a preventive comment, since re-adding it silently re-breaks the
feature). The matrix family value now flows; a per-job/expert override still wins
because it re-enters `explicit_llm_keys` with a *real* value (correct §5.3 order).

**k3d verification (deployed orchestrator artifact, both bases):**
`minimax-m3` → blob bakes **65536**, resolver yields **65536** (1M ctx) / **48333**
(admin 256k) / `minimax` M2.7 → **36864** (49152 family clamped to its 204,800
ctx backstop) / `gpt-5.5` → **65536** / `gemma` (no family value) → **22119**
(default 32768 clamped to its 128k ctx). All correct.

**Edge case surfaced (see §5.4):** k3d's `minimax-m3` registry `context_window`
is set to **32000** — the backstop collapses output to the **4096 MIN floor**
(`32000 − 25600 − 4096 = 2304 → floored`). The resolver is mathematically correct
(a 32k window can't hold large input *and* large output), but a too-small admin
window **can re-starve a reasoning model**. Below ~33k context, a reasoning model
needs a larger window, not a larger output cap — there is no room to give.

## 8. Runaway guard

Replace the blunt 16k cap with generous-cap + real detection:
- **vLLM#40080 is still open** (structured-output-triggered repetition loop);
  `max_tokens` is explicitly a band-aid and penalties only *partially* help — so a
  backstop stays warranted, but it should be a **detector**, not a length floor.
- **vLLM now ships native `RepetitionDetectionParams`** (`max_pattern_size`,
  `min_pattern_size`, `min_count ≥ 2`) that **terminates** generation on a
  repeating n-gram — the right model. Enable it on any **self-hosted gemma lane**
  via `extra_body`.
- **Primary (provider-agnostic, build in the harness):** a sliding-window n-gram
  repetition detector on streamed deltas that **terminates the turn** when a
  pattern repeats beyond a threshold (`min_count` ~4–6; exempt code/table blocks
  to avoid false-positiving dense reasoning). Emit a **distinct finish reason
  `repetition_detected`** (vs `length`) so the orchestrator can react via the
  existing `freeze_data`/stuck-detection machinery instead of silently
  truncating. SRW's existing stuck-detection is tool-level (`tool_name,
  args_hash`) — this is the missing within-a-generation layer.
- **Sampling penalties** (`repetition_penalty`/`frequency_penalty`;
  `presence_penalty` for Qwen) help **only on the open/non-reasoning lane** —
  OpenAI reasoning models **reject** them and DeepSeek thinking **ignores** them,
  so they can't be the primary guard. For open reasoning models the lever is
  sampling (temp 0.6 / top_p 0.95, **never greedy**), not penalties.

Ship the generous cap + distinct-finish-reason plumbing now; streaming n-gram
detection as the fast-follow (it's the fiddly part).

## 9. Cost guard — corrected

`max_tokens` was never the right spend lever, but the quota system is a **weaker
backstop than assumed**:
- It is **across-turn / daily**, not intra-turn — a background poll every ~120s
  (`main.py:quota_poll_loop:1421-1456`) over a **UTC-day aggregate** read from the
  LiteLLM gateway (`litellm_gateway.py:833-876`, `get_team_daily_usage:265-293`),
  with ~tens-of-seconds aggregation lag. A single runaway 200k-token turn runs to
  completion and is only caught at the next poll *if* the daily total crosses the
  limit.
- It freezes **worker jobs only** (`main.py:1336-1338`) — **persistent
  sessions/threads are not covered**.
- It is **per-project** and **inert unless `LITELLM_QUOTA` is configured**
  (`litellm_gateway.py:806-808`).

So within a single turn the only bounds are `max_output_tokens` (this feature) +
the timeout (§7.2). That's consistent with "quota catches daily abuse," but the
design must not lean on it as a per-turn guard — and **sessions have no quota
coverage at all**, which is worth a separate follow-up.

## 10. Implementation plan (change set)

Substantive fix = 1–5 together; 6 is fast-follow.

1. **`max_output_tokens` family setting + resolution** — values in
   `config/model_config_matrix.yaml` (§5.6); rewrite
   `loader.py:_resolve_max_output_tokens` (order §5.3, backstop §5.4, default +
   absolute ceiling §5.5); bump the Anthropic ladder (`:2977-2985`). No
   settings-merge change needed (§2).
2. **Per-model override** — registry field + builders + dispatch injection
   (§5.2), with **override semantics** to dodge §7.4.
3. **Fix blob-path override shadowing (§7.4)** — prerequisite so #1/#2 honor
   admin caps. Also independently fixes admin `context_window` overrides.
4. **Doubled `finish_reason`/model_name (§7.1)** — verify on the gateway path
   first (likely already resolved by the route-all migration); build #5 on the
   per-chunk read regardless, keep the normalize-fix only if a provider still
   double-emits.
5. **Length-aware retry/fallback** — `persistent_graph.py` + `graph.py` (§6),
   empty-vs-content branching; **scale timeout with `max_tokens`** (§7.2).
6. **(Fast-follow)** streaming n-gram repetition detection + `repetition_detected`
   finish reason (§8); minimax `extra_body` hardening (`reasoning_split`,
   provider pin) (§7.3); session quota coverage (§9).

## 11. Acceptance criteria / verification

- Re-create the `a0f826d7` naming task ("focus on old languages" synthesis) on
  k3d/dev; the previously-truncating turn completes with a non-empty answer.
- Unit: `_resolve_max_output_tokens` returns the family value clamped by the
  effective ctx — family 64k + admin ctx 64k → ≈ backstop(64k); + admin ctx 1M →
  64k. Lowering a model's registry `context_window` lowers resolved max output
  (proves §5.4 + §7.4).
- §7.4: confirmed broken today (job `19707fa1` blob = 1000000 vs registry
  262144); **after the fix** a minimax dispatch resolves `max_context_tokens=262144`.
- §7.1: audited `finish_reason` for a minimax `length` turn reads `"length"`, not
  `"lengthlength"` (likely already true on the gateway path — verify).
- A forced `length` truncation surfaces "truncated at output limit" and the
  length-aware retry recovers it (sessions **and** worker jobs).
- `ruff check src/ orchestrator/ tests/` + relevant pytest green.

## 12. Deferred

- Per-turn **dynamic** max_output (`effective_ctx − live_input − margin`) — more
  precise than the static backstop; needs per-call `max_tokens` override.
- Route minimax to the **native MiniMax endpoint** for real reasoning control
  (Anthropic-compat `budget_tokens` / OpenAI-compat `thinking:disabled`) — the
  only way to disable/budget minimax reasoning (§4/§7.3).
- OpenRouter **provider pinning** + `reasoning_split` hardening surfaced in the
  Admin → Models UI (§4/§7.3); per-model max-output field in the editor.
- Session-scoped **quota coverage** (§9 — sessions are currently uncovered).
- `reasoning:{effort}` via `extra_body` for providers that honor it (§7.3).

## 13. Sources

**Codebase trace** (file:line throughout) — 3 read-only subagents, 2026-06-28.

**MiniMax / reasoning truncation:**
- opencode #20176 (M2.5/M2.7 empty + `finish_reason=length`):
  https://github.com/anomalyco/opencode/issues/20176
- openclaw #64922 / claude-code-router #1238 (`enabled:false` → 400 mandatory
  reasoning): https://github.com/openclaw/openclaw/issues/64922 ·
  https://github.com/musistudio/claude-code-router/issues/1238
- openclaw #67410 / #65533 (mode C — content dropped when reasoning_details
  present): https://github.com/openclaw/openclaw/issues/67410
- nanobot #3068 (`reasoning_effort` ignored; use Anthropic-compat):
  https://github.com/HKUDS/nanobot/issues/3068
- NVIDIA forum (M2.5 reasoning leak; bigger max_tokens "only masks it"):
  https://forums.developer.nvidia.com/t/minimaxai-minimax-m2-5-leaks-reasoning-into-choices-0-message-content-on-v1-chat-completions-larger-max-tokens-only-masks-it/364812
- OpenRouter endpoints API (per-provider caps): M3
  https://openrouter.ai/api/v1/models/minimax/minimax-m3/endpoints · M2.7 / M2.5
  analogous · MiniMax M3 blog https://www.minimax.io/blog/minimax-m3 ·
  tool-use/interleaved thinking
  https://platform.minimax.io/docs/guides/text-m3-function-call
- LibreChat #12671 (the ~17,100 was a client bug):
  https://github.com/danny-avila/LibreChat/discussions/12671

**Cross-provider reasoning budgets:**
- OpenAI reasoning (≥25k reserve; empty completions):
  https://developers.openai.com/api/docs/guides/reasoning ·
  https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning
- Anthropic extended thinking + Task Budgets:
  https://platform.claude.com/docs/en/build-with-claude/extended-thinking ·
  https://platform.claude.com/docs/en/build-with-claude/task-budgets
- Gemini thinking (counts against maxOutputTokens; no reservation):
  https://firebase.google.com/docs/ai-logic/thinking ·
  https://github.com/googleapis/python-genai/issues/782
- OpenRouter reasoning tokens:
  https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
- Harnesses: Aider https://aider.chat/docs/config/reasoning.html · Roo #5784
  (hardcoded 8192) https://github.com/RooCodeInc/Roo-Code/issues/5784

**Runaway guard:**
- vLLM #40080 (gemma/xgrammar loop, open):
  https://github.com/vllm-project/vllm/issues/40080
- vLLM `RepetitionDetectionParams`:
  https://docs.vllm.ai/en/latest/api/vllm/sampling_params/
- Production repetition study: https://arxiv.org/pdf/2512.04419
- OpenRouter params (penalties): https://openrouter.ai/docs/api/reference/parameters
  · DeepSeek thinking (penalties no-op):
  https://api-docs.deepseek.com/guides/thinking_mode
