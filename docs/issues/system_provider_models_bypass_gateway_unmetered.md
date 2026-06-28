# System-provider models (minimax, gemini) bypass the LiteLLM gateway → never metered; gateway-routed models carry null cost

**Date:** 2026-06-28
**Status:** Investigation complete, root cause isolated + **empirically confirmed on the live main/dev cluster** (ns `superhuman-remote-worker`). Not fixed. Two distinct, independent gaps (**A** — bypass → zero usage rows; **B** — gateway rows present but `cost_usd` null). One **adjacent loose end** flagged for separate verification (§ "Loose end"). Surfaced while discussing whether to route reasoning models through LiteLLM for `docs/features/reasoning_aware_max_output_tokens.md`. **Update 2026-06-28:** we then *empirically tested* whether LiteLLM can run all models through the gateway with reasoning + metering intact (real proxy image **v1.83.7** + live OpenRouter) — **it can**; the Gap-A "meter-vs-reasoning tension" turns out **not to exist for Chat-Completions-native models**, which simplifies the fix. Full results in § "Empirical addendum". **Codex follow-up (2026-06-28):** the codex-proxy's Responses API was then tested through LiteLLM on k3d and **works end-to-end** — reasoning + cost preserved via `use_responses_api: true` (both `/responses` and the `/chat/completions` bridge, HTTP proxy verified), so even the Responses-API-only upstream can route through the gateway. The only remaining exception is Anthropic thinking *signatures* (future-only; SRW runs no Anthropic model).
> **RESOLUTION (2026-06-28) — Gap A + Gap B both FIXED.** Implemented via the roadmap `docs/features/route_all_models_through_litellm_gateway.md`. **Gap A (bypass → zero usage rows): RESOLVED** — P1 (`21c7022b`) registers system + codex models in the gateway; P2 (`ccb534a0`) routes them through it behind a default-off per-provider canary (`LITELLM_GATEWAY_ROUTED_PROVIDERS`). k3d-live-verified: a real minimax job routed through the gateway produced **20 `usage_events` rows** (the previously-bypassed paid lane is now metered). **Gap B (gateway rows carry null `cost_usd`): RESOLVED (P3, uncommitted on `develop`)** — rather than seed `usage_rates` per token, `materialize_llm_usage` now consumes LiteLLM's authoritative per-request `spend` and emits it as a dedicated `unit='request'` cost dimension (token rows stay cost-free; `SUM(cost_usd) == spend`, no double-count). k3d-live-verified: paid models priced (minimax $0.17 / codex $0.27 / gemini $0.0009), homelab gemma stays cost-free, `/api/usage` headline `total_cost_usd` **$0 → $0.454719**. `usage_rates` is untouched and now prices `category='compute'` only. P0 (`bdea2869`) separately added the gateway-down→direct fallback + `get_spend_logs` `start_date` bound.
**Severity:** **Medium.** No user-facing breakage and nothing is *wrong* with inference — this is a **governance / observability** gap, but a consequential one: the usage ledger, daily-quota freeze, and per-project/per-user rate limits **silently do not cover the paid external models** (minimax via OpenRouter, gemini via Google), which are exactly the models with real per-token dollar cost. The coverage is effectively **inverted** — the gateway meters the *free* self-hosted lane (gemma) and misses the *paid* lane. minimax ran **1,742** audited turns in 14 days with **0** ledger rows.
**Component:**
- Dispatch gateway-routing gate — `orchestrator/main.py:1551-1574` (the `and meta.endpoint_id` condition) + the direct-credentials fall-through `:1590-1589` (`elif resolved_keys`).
- Catalog → meta shapes — `src/core/model_registry.py:279-319` (`_catalog_row_to_meta`: `provider_kind='endpoint'` sets `endpoint_id`; `provider_kind='system'` leaves `base_url=None` / **no `endpoint_id`**). Endpoint factory resolution `_endpoint_factory_provider:295` (returns `"openai"` for every non-codex endpoint).
- Usage materialization — `orchestrator/services/litellm_gateway.py:295-307` (`get_spend_logs`; the `:302` "`spend` is dollar-only (0 for unpriced homelab)" comment), `:880-981` (`materialize_llm_usage`, the **only** writer of `category='llm'` `usage_events`).
- Quota / rate-limit coverage — `litellm_gateway.py:806-808` (quota inert unless `LITELLM_QUOTA` configured), `main.py` `quota_poll_loop` (worker-jobs only; see `reasoning_aware_max_output_tokens.md` §9).
- `usage_events` ledger (audit DB) — columns `id, ts, user_id, project_id, ref_kind, ref_id, category, resource, quantity, unit, rate_usd, cost_usd, source, source_id, details`. **No model column**; the model lives in `resource` + `details->>'model'`.
**Related:**
- `docs/done/litellm_gateway_drops_gpt_codex_reasoning_capture.md` — origin of the **meter-vs-reasoning tension**; codex was deliberately made to bypass the gateway to keep its reasoning. This issue is the same trade-off seen from the *metering* side.
- `docs/features/reasoning_aware_max_output_tokens.md` — §9 (quota is a weak/daily backstop; sessions uncovered); the evidence session `a0f826d7` that ran `openrouter/minimax/minimax-m3`.
- `docs/issues/codex_session_gateway_baseurl_401.md` — sibling gateway-routing issue.
- Memory: `project_usage_monitoring_rate_limiting`, `reference_debug_session_usage_llm_routing`.

---

## Background / how this surfaced

While weighing "could we run *all* models (incl. reasoning models) through LiteLLM to unify schemas and capture reasoning + usage," we noticed minimax usage never appears in the cost view. Two competing hypotheses:

- **H1** — minimax **bypasses** the gateway entirely (direct to OpenRouter) → no ledger rows at all.
- **H2** — minimax **routes through** the gateway but LiteLLM has no cost map → rows exist with `$0`/null cost.

The verification below settles it: **H1 is correct**, and we additionally discovered **H2's symptom is independently true for the models that *are* routed** (Gap B). They are two separate gaps.

## TL;DR — two independent metering gaps

| | Gap | Mechanism | Effect |
|---|---|---|---|
| **A** (bypass) | System-provider catalog models (`provider_kind='system'`: minimax/openrouter, gemini/google) have **no `endpoint_id`**, so the dispatch gateway-routing block (`main.py:1551`) is skipped. They get the provider's **direct** credentials and hit `openrouter.ai` / Google directly. | The gateway never sees the traffic → `materialize_llm_usage` (the only `category='llm'` writer) produces **zero rows**. No tokens, no cost, no rate-limit, no quota coverage. | **minimax: 0 ledger rows / 1,742 audited turns (14 d). gemini: 0.** |
| **B** (null cost) | Even endpoint-backed models that **do** route through the gateway log `cost_usd=NULL` — LiteLLM has no price map for the self-hosted/homelab models (`litellm_gateway.py:302`). | Ledger captures **token quantities but not dollars**. | gemma: 24,112 rows, every one `cost=None`. |

**The inversion:** Gap A removes the *paid* external models (minimax, gemini) from metering; Gap B blanks the dollar figure on the *free* self-hosted model (gemma) that's left. So the ledger currently meters tokens for the lane with **no marginal dollar cost** and is blind to the lane that **actually costs money**.

## Evidence (live, `main` ctx, ns `superhuman-remote-worker`, 2026-06-28)

`usage_events` over the gateway's entire lifetime — ledger `ts` range **2026-06-22 12:26 → 2026-06-28 11:02** (i.e. it went live exactly at gateway enablement, `ac211a52`):

```
usage_events by source/category:
  source='litellm'      category='llm'      -> 26,796
  source='orchestrator' category='compute'  ->    178

model presence in usage_events.details (the LLM ledger):
  details ILIKE %minimax%  -> 0      ← Gap A
  details ILIKE %gemini%   -> 0      ← Gap A (control: same root cause)
  details ILIKE %gemma%    -> 24,112
  details ILIKE %gpt-5%    -> 2,684  ← see "Loose end"
  details ILIKE %codex%    -> 0

sample llm row:
  resource=gemma-4-moe qty=934 completion-token cost=None
  details={"model":"gemma-4-moe","request_id":"chatcmpl-854d6699147ea62b"}   ← Gap B
```

Cross-check that minimax is genuinely *running* (so the 0 is a metering gap, not absence of traffic): `chat_history` (audit) over 14 days shows **1,742 minimax turns** vs 12,744 non-minimax. minimax is one of the heaviest-used models and contributes **nothing** to the ledger.

**The gemini control is decisive.** gemini is also a `provider_kind='system'` model (provider slug "google"), and it too reads **0**. Two unrelated system providers, both zero, while the lone endpoint-backed model (gemma, "Local Router") is fully present → the discriminator is *system-vs-endpoint registration*, not anything minimax-specific.

`list_models` grouping corroborates the registration shapes:
```
System: Local Router (ready):  gemma-*          ← endpoint  → routed → metered
System: codex-proxy (ready):   gpt-5.*          ← endpoint  → (see Loose end)
Google (ready):                gemini-3.5-flash ← system    → bypassed → 0
Openrouter (ready):            minimax-m3       ← system    → bypassed → 0
```
Endpoint-backed models appear as `System: <endpoint-label>`; system-provider models appear under their bare provider slug.

## Root cause

The gateway base-URL injection is gated on `endpoint_id` being truthy:

```python
# orchestrator/main.py:1551-1574 (abridged)
if (meta is not None
        and meta.origin in ("custom", "system", "catalog")
        and meta.endpoint_id):                    # ← system models have endpoint_id=None
    ...
    _gw = _gw_scoped if meta.provider != "codex" else None
    if _gw is not None:
        llm_over.setdefault("base_url", _gw[0])    # gateway URL — never reached for minimax/gemini
        llm_over.setdefault("api_key",  _gw[1])
# else: falls through to `elif resolved_keys:` → provider's DIRECT base_url/key
```

`_catalog_row_to_meta` (`model_registry.py:308-318`) builds the `provider_kind='system'` shape with **no `endpoint_id`** (and `base_url=None`, so the agent factory uses its hardcoded provider default — `openrouter.ai`, Google, etc.). Therefore minimax and gemini never satisfy the `and meta.endpoint_id` clause and are dispatched with direct provider credentials, entirely outside the gateway. Gap B is a separate, well-understood property: stock LiteLLM only knows public OpenAI/Anthropic/etc. prices, so self-hosted model spend logs as null.

## Fix options

> **⚡ REVISED by the "Empirical addendum" below (2026-06-28).** The "reasoning risk" of routing system-provider models through the gateway was tested and **disproven for Chat-Completions-native models** (minimax, gemini, deepseek, gpt-5-via-OpenRouter). For SRW's current roster, **option 1 (route through the gateway) is now the preferred Gap-A fix** — it preserves reasoning *and* meters *and* populates cost in one place. Option 2 (audit-derived) drops to a fallback for upstreams that genuinely can't take the normalized path (the codex-proxy; future Anthropic-with-signatures).

### Gap A — meter the bypassed system-provider models

Three approaches, originally framed by a **meter-vs-reasoning tension** (the one that made codex bypass the gateway) — now **partially disproven**, see addendum:

1. **Register them as gateway-fronted endpoints** (`provider_kind='endpoint'`). *Simplest plumbing, real risk.* `_endpoint_factory_provider:295` returns `"openai"` for every non-codex endpoint → the agent would build `_create_openai_llm`, **not** `_create_openrouter_llm`, losing minimax's OpenRouter-specific reasoning handling (`reasoning_details` extraction, `reasoning_split`, the mandatory-reasoning quirk) — and Google isn't OpenAI-wire-compatible at all. **Originally feared to recreate a codex-style reasoning drop — TESTED + DISPROVEN for CC-native upstreams (see addendum):** LiteLLM returns reasoning as `reasoning_content` (the exact field SRW's `reasoning_chat.py` tap reads), so `_create_openai_llm` → gateway still captures reasoning; `reasoning_details`/`reasoning_split` are nice-to-haves, not required for capture. Google is the only sub-case needing care (not OpenAI-wire-native — reach it via OpenRouter or LiteLLM's google provider). **Now the preferred Gap-A fix for minimax/gemini.**
   - **(1a)** First teach `_endpoint_factory_provider` to resolve `openrouter`/`google`/etc. factories, *and* make the gateway a non-normalizing **passthrough** so the native wire shape (and reasoning) survives. This is the deferred "meter without translating" work from the codex doc — the clean but larger lift.
2. **Audit-derived metering (was recommended; now a *fallback* for non-CC upstreams — see addendum).** The audit trail (`chat_history` / `llm_requests`) **already captures token usage for every call, gateway or not** — that's how we proved minimax is running. Add a *second* materialization source: `usage_events` rows synthesized from the audit token counts for non-gateway models, keyed by `resource=model`, `source='audit'`. This closes Gap A **without touching routing and without any reasoning risk**, and it naturally also covers persistent sessions (which the gateway quota never covered — `reasoning_aware_max_output_tokens.md` §9). Trade-off: it meters but doesn't *rate-limit at the chokepoint* (no pre-flight key enforcement) — acceptable, since rate-limiting the paid external models is a smaller concern than *seeing their cost*.
3. **Status quo + documentation.** Accept that system-provider models are unmetered and label them as such in the cost UI so the gap is explicit rather than silent.

### Gap B — price the metered models

Add a price map so `cost_usd` is populated. Either configure per-model prices in LiteLLM, or (composes with option 2) compute cost in the materializer from a local price table keyed by `model`. Prioritize the **paid external** models (minimax/OpenRouter, gemini/Google) — the self-hosted lane (gemma) has no marginal dollar cost and `cost=NULL` there is arguably correct (its cost is compute, already tracked as `category='compute'`).

**Recommended (revised 2026-06-28):** for the current CC-native roster, **option 1 — route minimax/gemini through the gateway** — fixes Gap A *and* Gap B together (LiteLLM surfaces OpenRouter's returned `cost`, so no price table is needed for them; see addendum). Keep **option 2 (audit-derived)** as the metering path for upstreams that can't take the normalized route (codex-proxy; future Anthropic-with-signatures), since it also covers persistent sessions. A small **Gap-B price table** is only needed if you later route a *paid* model whose upstream doesn't return cost.

## Empirical addendum (2026-06-28) — LiteLLM *can* run all models through the gateway with reasoning + metering intact (tested)

**Why this is here:** the Gap-A options above were framed by a *meter-vs-reasoning tension* (routing through the gateway "would likely recreate a codex-style reasoning drop"). We tested that assumption directly against the **real deployment image** (`ghcr.io/berriai/litellm:main-stable`, **v1.83.7**) **and** the LiteLLM SDK, driving live OpenRouter traffic with a throwaway key. **For Chat-Completions-native models the tension does not exist** — routing them through the gateway preserves reasoning *and* meters *and* populates cost. That is what revised the recommendation.

**Method:** `podman run` of the proxy image with a 3-model `config.yaml` (`openrouter/{minimax-m3, openai/gpt-5, anthropic/claude-haiku-4.5}`), plus `litellm.completion` SDK calls; every field compared against direct-OpenRouter ground truth. Test scripts live in the session scratchpad. (Repro note: the proxy's published port is unreachable from the sandboxed shell netns — host curl needs the sandbox disabled; the proxy itself is healthy.)

**Results — normalized `/v1/chat/completions` via OpenRouter, both the SDK and the real HTTP proxy:**

| Model (`openrouter/*`) | reasoning text | `reasoning_tokens` | cost | streaming reasoning | settings |
|---|---|---|---|---|---|
| minimax-m3 | ✓ `reasoning_content` | ✓ (27–111) | ✓ `usage.cost` + `x-litellm-response-cost` hdr | ✓ | provider-pin ✓ |
| openai/gpt-5 | ✓ (90/124 SSE chunks) | ✓ (103→1280) | ✓ | ✓ | `reasoning_effort` low→high scales toks 6.7× ✓ |
| deepseek-r1 | ✓ 1794c | ✓ 595 | ✓ | ✓ | — |
| anthropic/claude-haiku-4.5 | ✓ 242c | ✓ 70 | ✓ | ✓ | — |

Confirmations that bear on the fix:
- **HTTP-proxy parity (not just the SDK).** `reasoning_content` is serialized in the JSON body **and** in streaming SSE deltas; `reasoning_tokens` + `cost`/`cost_details` are in `usage`; every call carries an `x-litellm-response-cost` response header. Metering happens at the exact HTTP layer the agent hits.
- **The `_create_openai_llm` factory is fine** (the crux of the Gap-A correction). LiteLLM normalizes reasoning to `reasoning_content` — the **exact field SRW's `reasoning_chat.py` streaming tap reads** (`delta.reasoning_content`). So a system-provider model registered as an endpoint → openai factory → gateway **still captures reasoning** for CC-native upstreams.
- **Gap B fixes itself for the paid lane.** minimax is *not* in LiteLLM's built-in price map (the proxy even warns about it), yet `response_cost`/`usage.cost` are populated — LiteLLM surfaces **OpenRouter's returned `cost`**. So routing minimax/gemini through the gateway yields **non-null cost with no price table**; only genuinely-free gemma stays $0 (correct). **(Refinement 2026-06-28, per the migration research: this non-null cost is in *LiteLLM's* view — `x-litellm-response-cost` / `/spend/logs`. SRW's own `usage_events.cost_usd` stays NULL until `usage_rates` is seeded or `materialize_llm_usage` is changed to consume LiteLLM's `response_cost` — the materializer deliberately ignores LiteLLM's `spend` and prices token quantities from the empty `usage_rates` table. See `docs/features/route_all_models_through_litellm_gateway.md` §4.4.)**
- **Settings pass through faithfully.** Provider pinning (`provider.order`/`allow_fallbacks`) takes effect (response `provider` flips Parasail↔Novita); `reasoning_effort` low→high scales reasoning tokens 192→1280; temperature/max_tokens honored.
- **The old OpenRouter-streaming-reasoning bug (#8631) is fixed** — filed on v1.61.8 (Feb 2025); current is v1.83.7.

**The two genuine exceptions (narrow; neither blocks "route everything through it"):**
1. **Anthropic interleaved-thinking *signatures*.** Via OpenRouter's CC shape you get reasoning *text* + tokens + cost but **not** `thinking_blocks`+`signature` (needed only to *replay* thinking across turns). Preserve them via LiteLLM's **native Anthropic provider** (maps `thinking_blocks`+signature onto the CC response — per docs; **not tested here**, no Anthropic key) or its **`/v1/messages` passthrough**. **SRW runs no Anthropic model today** → future-only.
2. **A Responses-API-*only* upstream = the codex-proxy.** Normalizing Responses→Chat-Completions is what dropped codex reasoning (`litellm_gateway_drops_gpt_codex_reasoning_capture.md`) — one specific upstream, **not** "OpenAI models": **gpt-5 reasoning comes through fine via OpenRouter CC** (table above). Handle by keeping the codex-proxy on bypass (it's subscription-billed → per-token metering matters least), **or front it with LiteLLM configured `use_responses_api: true`** — **TESTED 2026-06-28 + WORKS end-to-end (see "Codex-proxy follow-up" below): reasoning *and* cost are preserved through the gateway, so codex need not bypass at all.**

The one documented limitation behind both exceptions: **passthrough routes (`/v1/messages`, `/anthropic`) are NOT spend-tracked — only `/v1/chat/completions` is** (LiteLLM #24204). Since CC preserves reasoning for everything *except* Anthropic-signatures, passthrough is rarely needed.

**Not tested directly** (no keys / no upstream at the time): native OpenAI `/v1/responses` against api.openai.com; LiteLLM's native Anthropic provider w/ signatures; DB-backed spend *persistence* / budget enforcement (tested the no-DB image — cost is computed + header-emitted; dev already runs the DB-backed image, working for gemma). The verdict holds for the SRW-relevant routes (everything via OpenRouter / the gateway), not those native endpoints.

**Bottom line:** **no replacement gateway and no self-built proxy are warranted** — LiteLLM does what SRW needs; we simply weren't routing the system-provider models through it. The Gap-A fix is the *simpler* one (route through the gateway) for the current CC-native roster.

### Codex-proxy follow-up (2026-06-28) — RESOLVED: a Responses-API-only upstream works through LiteLLM, metered

The codex-proxy (`cli-proxy-api:v7.2.27`, an authed Codex account) was stood up on k3d, letting us test the one untested exception directly. Ground truth: the codex-proxy's `/v1/responses` returns a `reasoning` output item (`encrypted_content` + `summary`) and `usage.output_tokens_details.reasoning_tokens`. Fronting it with LiteLLM (SDK **and** a real proxy pod, `ghcr.io/berriai/litellm-database:main-stable`, v1.89.3):

| Path: LiteLLM → codex-proxy | reasoning | reasoning_tokens | cost |
|---|---|---|---|
| `litellm.responses()` SDK | summary 425c + `encrypted_content` ✓ | 41 ✓ | `response_cost` 0.0088 ✓ |
| `litellm.completion(use_responses_api=True)` SDK | `reasoning_content` 357c ✓ | 42 ✓ | 0.0048 ✓ |
| `litellm.responses(stream=True)` SDK | reasoning events + `OUTPUT_TEXT_DELTA` (answer) stream ✓ | — | — |
| **HTTP proxy `POST /v1/responses`** | summary 434c + `encrypted_content` ✓ | 22 ✓ | `x-litellm-response-cost` 0.004005 ✓ |
| **HTTP proxy `POST /v1/chat/completions`** (bridge) | **`reasoning_content` 429c** ✓ | 38 ✓ | `x-litellm-response-cost` 0.004485 ✓ |

**The key config:** register the codex model in the gateway with **`use_responses_api: true`** + `api_base` = the codex-proxy. LiteLLM then calls the upstream's `/v1/responses` (preserving reasoning) instead of `/v1/chat/completions` (which the codex-proxy doesn't even serve, and whose normalization dropped reasoning in `litellm_gateway_drops_gpt_codex_reasoning_capture.md`). The `/chat/completions` **bridge** exposes the reasoning as `reasoning_content` — the exact field SRW's `reasoning_chat.py` tap reads — so **the agent needs no change**, and cost is emitted as `x-litellm-response-cost` on both endpoints.

**Consequence:** this **resolves the deferred "gateway `/responses` passthrough" follow-up** from the codex done-doc. Codex no longer has to bypass the gateway — it can route through it **metered and with reasoning intact**, closing the codex half of the metering gap. The only remaining genuine exception is Anthropic interleaved-thinking *signatures* (#1, future-only). **Net: every model in SRW's current roster (minimax, gemini, gemma, gpt-5.x/codex) can route through the LiteLLM gateway with reasoning + metering.** (Caveat: `usage.cost` is null inline on the `/responses` object; the computed cost rides the `x-litellm-response-cost` header / spend-log — which is what SRW's materializer reads.)

## Loose end (needs separate verification — adjacent, not this bug)

`%gpt-5%` shows **2,684** ledger rows spanning to 06-28, i.e. gpt-5/codex traffic **is still routing through the gateway** on dev. Per `litellm_gateway_drops_gpt_codex_reasoning_capture.md`, codex models are supposed to **bypass** the gateway so their reasoning survives; that fix was "uncommitted on develop" as of 06-24. If gpt-5 is still gateway-routed, **codex reasoning capture may still be broken on dev right now.** Quick check to confirm/deny:
- Date-distribution of the 2,684 `%gpt-5%` rows (`SELECT date_trunc('day',ts), count(*) … WHERE details ILIKE '%gpt-5%' GROUP BY 1`) — are they only 06-22..06-24 (pre-fix) or still arriving today?
- One live gpt-5 dispatch log line: `base_url=` should read the codex proxy (`srw-codex-proxy:8317`), **not** `srw-litellm:4000`.
- `chat_history` gpt-5 `reasoning` capture rate for the last 2 days (0% ⇒ still broken).

## Acceptance criteria / verification

- After the fix, `usage_events` contains `category='llm'` rows for minimax and gemini whose token quantities reconcile with the audit `chat_history` token counts for the same window (currently 1,742 minimax turns → expect non-zero rows).
- Paid external models carry a non-null `cost_usd`; self-hosted models are either priced at 0 or explicitly excluded.
- The cost view reflects minimax/gemini spend (or labels them "unpriced" — no silent zero).
- Reasoning capture for minimax is **unchanged** by the metering fix (regression guard). *Asserted in principle by the empirical addendum — LiteLLM returns `reasoning_content` over the gateway; this re-asserts it on SRW's own dispatch path.*
- **End-to-end k3d proof (option 1):** register minimax as an endpoint-backed model on the gateway → a real session/job captures reasoning (`reasoning_content` non-empty in `chat_history`/`thread_messages`) **and** produces a `usage_events` row with **non-null `cost_usd`**, reconciling with the audit token counts.
- `ruff check src/ orchestrator/ tests/` + relevant pytest green.

## Appendix — reproduction

```bash
# Ledger has zero rows for the bypassed system providers, despite live traffic.
kubectl --context=main -n superhuman-remote-worker exec -i deploy/srw-orchestrator \
  -c orchestrator -- python3 - <<'PY'
import os, asyncio, asyncpg
async def main():
    c = await asyncpg.connect(host=os.environ["AUDIT_POSTGRES_HOST"],
        user=os.environ["AUDIT_POSTGRES_USER"], password=os.environ["AUDIT_POSTGRES_PASSWORD"],
        database=os.environ["AUDIT_POSTGRES_DB"])
    for kw in ("minimax","gemini","gemma","gpt-5"):
        n = await c.fetchval("SELECT count(*) FROM usage_events WHERE details::text ILIKE $1", f"%{kw}%")
        print(f"usage_events ~{kw}: {n}")
    # proof minimax is actually running (audit), so 0 above is a metering gap:
    n = await c.fetchval("SELECT count(*) FROM chat_history "
                         "WHERE lower(model) LIKE '%minimax%' AND timestamp > now() - interval '14 days'")
    print(f"chat_history minimax (14d): {n}")
asyncio.run(main())
PY
```
