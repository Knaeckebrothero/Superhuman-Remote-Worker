# Codex session 401 — stale LiteLLM-gateway `base_url` pinned on a codex model

**Status:** **FIX IMPLEMENTED** (develop, uncommitted) · root cause confirmed + empirically verified on dev · fix + RED-verified regression tests green locally (161 passed across the affected suites; ruff clean) · NOT yet deployed (live session `a1153f56` stays broken until a dev cut) · worker top-level twin noted as follow-up. Was: fatal to any session whose `config_override` pins a gateway `base_url`/`provider` on a codex model (narrow config condition, not a global outage).
**Found:** 2026-06-25, investigating session `a1153f56` on the main cluster ("did the M0 push cause this 401?")
**Component:** `orchestrator/main.py::_inject_model_credentials` (transport-field injection) · `_inject_thread_dispatch_credentials` (session sibling) · LiteLLM gateway codex-bypass · session config_override persistence/redaction
**Related:** [[litellm_gateway_drops_gpt_codex_reasoning_capture]] (RESOLVED — introduced the codex-bypass this bug slips through) · [[session_empty_response_gpt5_codex_stop]] · [[phase_pin_endpoint_credentials_not_injected]] · [[litellm_reranker_model_unregistered]] (same `agent → srw-litellm` family)

## Symptom

Every turn of session `a1153f56-eb55-49ef-a21e-3151b1d46ff3` (dev, ns `superhuman-remote-worker`) fails immediately:

```
Error code: 401 - {'error': {'message': "LiteLLM Virtual Key expected.
Received=f66d****cce2, expected to start with 'sk-'.", 'type': 'auth_error', 'code': '401'}}
```

Agent log (the real call), `srw-agent-j-fbca2952`:

```
httpx  POST http://srw-litellm:4000/v1/chat/completions "HTTP/1.1 401 Unauthorized"
src.persistent_graph  ERROR  Error in turn 1  …  openai.AuthenticationError: 401 …
```

Thread `config_override`:

```json
{"llm":       {"model": "gpt-5.5",      "base_url": "http://srw-litellm:4000/v1", "provider": "openai"},
 "auxiliary": {"model": "gemma-4-moe",  "base_url": "http://srw-litellm:4000/v1", "provider": "openai"}}
```

The **auxiliary** slot (`gemma-4-moe`, a Local-Router/gateway model) is correctly gateway-routed. Only the **llm** slot is broken: `gpt-5.5` is a **codex-proxy** model (`list_models` → `System: codex-proxy: gpt-5.5`), which by design must bypass the gateway.

## TL;DR

- **NOT the M0 push.** M0 (2026-06-24) is docs + Helm only (orchestrator PDB + graceful-drain/probes); `git log <M0 range> -- orchestrator/ src/ agent/` is empty. It touched no LLM routing, credentials, gateway, or codex code.
- `f66d****cce2` is the **`CODEX_MANAGEMENT_KEY`** (64-hex, no `sk-`) from the `srw` secret — the codex-proxy endpoint's key.
- The session sends the **codex key to the LiteLLM gateway**, which only accepts `sk-` virtual keys → 401. The transport fields disagree: `base_url` = gateway, `api_key` = codex endpoint key, `provider` = `openai` (stale → wrong factory, Chat Completions).
- Cause: `_inject_model_credentials` fills `base_url`/`api_key`/`provider` with **per-field `setdefault`**. The codex-bypass branch injects the codex `api_key` but **cannot overwrite a pre-pinned gateway `base_url`**. The fix in [[litellm_gateway_drops_gpt_codex_reasoning_capture]] handles a *fresh* dispatch (base_url absent → set to the codex proxy) but not a session that already carries a stale gateway `base_url`.

## Root cause

`orchestrator/main.py::_inject_model_credentials` (called for the session's `llm`/`auxiliary` slots from `_inject_thread_dispatch_credentials`, ~3714/3733):

1. **No early-return.** Line ~3523 returns only if **both** `base_url` and `api_key` are present. Persisted config has `base_url` (gateway) but no `api_key` (stripped by `redact_config_override`) → continues.
2. **Provider stays stale.** Line ~3547: `section.setdefault("provider", meta.provider)`. `meta.provider` for `gpt-5.5` is `codex`, but `provider="openai"` is already set → kept. The agent therefore builds the **OpenAI/Chat-Completions** factory (matches the `POST /v1/chat/completions` in the log), not the codex/Responses factory.
3. **Codex bypass, then stale base_url.** Lines ~3560-3577: because `meta.provider == "codex"`, `_gw = None` (codex must not use the gateway — it's Responses-API-only). It falls to the endpoint-row branch:
   ```python
   section.setdefault("base_url", endpoint_row["base_url"])  # ← base_url ALREADY "http://srw-litellm:4000/v1" → NOT overwritten
   section.setdefault("api_key",  endpoint_row["api_key"])   # ← injects CODEX_MANAGEMENT_KEY (f66d…cce2)
   ```
   The codex proxy's real base_url (`http://srw-codex-proxy:8317/v1`) never replaces the stale gateway URL.

Net transport sent to the wire: **OpenAI factory → `http://srw-litellm:4000/v1/chat/completions` → `Authorization: Bearer f66d…cce2`**. The gateway rejects the non-`sk-` key → 401. (A correctly-routed codex turn would be: codex factory → `srw-codex-proxy:8317/v1/responses` → codex key, which the proxy accepts.)

**Why the fields were stale:** `base_url`/`provider` survive the persist/redact cycle (only `api_key` is stripped) and `_inject_*` is `setdefault`-based, so a session that previously selected a gateway-routed model (or was authored with an explicit gateway `base_url`) keeps that `base_url`/`provider` after a model swap to a codex model. The per-field defaulting lets the resolved `(provider, base_url, api_key)` triple come from **three different sources**.

## What is NOT the cause (M0)

- **M0 active-passive hardening push (2026-06-24).** Both code commits are Helm-only: `2fc1e65f` (orchestrator PDB) and `3458a350` (graceful drain + startupProbe + probe tuning); the rest of the series is docs. `git log 0cf31299^..bb86e4ee -- orchestrator/ src/ agent/` returns nothing. M0 changed no application code, so it cannot have altered key handling. Deployed orchestrator is `sha-8c0366e` (post-M0), confirming M0 is present yet not implicated.
- **The temporal correlation the user noticed** is the **LiteLLM gateway rollout** (feature `5ce37659`, 2026-06-21; enabled on dev ~2026-06-24, `srw-litellm` pod ~20h old), not M0. Even that only *exposes* the latent injector flaw for codex+stale-base_url configs.
- **Not a fleet/scoped-key defect.** `compute_fleet_key` returns `sk-srw-fleet-…` and scoped/master keys are `sk-`; the gateway branch always yields an `sk-` key. The 401 proves the gateway branch was **not** taken (codex bypass), so key minting is irrelevant here.

## Reproduction / how to verify

- `gpt-5.5` is codex-backed: `list_models` → `System: codex-proxy`.
- The rejected key identity:
  ```bash
  kubectl --context main -n superhuman-remote-worker get secret srw -o json \
    | python3 -c "import json,sys,base64; d=json.load(sys.stdin)['data']; \
      [print(k) for k,v in d.items() if base64.b64decode(v).decode()[:4]=='f66d']"
  # → CODEX_MANAGEMENT_KEY  (len 64, not sk-)
  ```
- Confirm the wrong route on a live turn: agent log shows `POST http://srw-litellm:4000/v1/chat/completions 401`. A healthy codex turn logs `Created Codex LLM … base_url=http://srw-codex-proxy:8317/v1` and hits `/v1/responses`.
- A **fresh** `gpt-5.5` session (no pre-pinned `base_url`) works — `_inject_model_credentials` sets `base_url` to the codex proxy because it is absent. This isolates the bug to the *stale pre-pinned* `base_url`.

## Fix — implemented (2026-06-25)

**Immediate unblock (this session, until the fix deploys):** strip the stale `llm.base_url`/`llm.provider` from the thread's `config_override` (injector then resolves the codex-proxy endpoint coherently), or switch the chat model to a gateway model (`gemma-4-*`/`gemini`/`minimax`), or start a fresh `gpt-5.5` session.

**Code fix (shipped on develop):** in `_inject_model_credentials`'s endpoint-direct (gateway-bypass) branch, before the additive `setdefault`s, **drop a `base_url` that equals the gateway URL** (and re-set `provider` from the resolved meta) so the codex endpoint's `base_url`/`provider`/`api_key` repopulate coherently from one source:

```python
endpoint_row = await postgres_db.get_user_llm_endpoint(meta.endpoint_id)
if endpoint_row:
    if _gw_target is not None and section.get("base_url") == _gw_target[0]:
        section.pop("base_url", None)
        if meta.provider:
            section["provider"] = meta.provider
    if endpoint_row.get("base_url"):
        section.setdefault("base_url", endpoint_row["base_url"])
    if endpoint_row.get("api_key"):
        section.setdefault("api_key", endpoint_row["api_key"])
```

Scoped to the gateway URL only, so a deliberately caller-pinned **non-gateway** `base_url` (BYO codex endpoint) still wins (additive contract preserved). This covers the session `llm` + `auxiliary` slots and worker **phase** blocks (all routed through `_inject_model_credentials`).

**Tests (TDD, both watched RED before the fix):**
- `tests/test_dispatch_phase_credentials.py::TestCodexBypassesGateway::test_codex_model_replaces_stale_gateway_base_url` — the mechanism (unit, on `_inject_model_credentials`).
- `…::test_codex_model_keeps_caller_pinned_nongateway_base_url` — boundary guard (don't over-strip a real BYO base_url).
- `tests/test_thread_config_persistence.py::TestCodexSessionGatewayBaseUrl::test_stale_gateway_base_url_replaced_with_codex_endpoint` — the full session path (`_inject_thread_dispatch_credentials`, gateway enabled), i.e. the actual `a1153f56` scenario.

**Follow-up (NOT done):** the worker **top-level** `llm` path in `_inject_dispatch_credentials` (~main.py:1535-1541) has the identical `setdefault`-keeps-stale-gateway-`base_url` pattern in its codex `else` branch, and is currently untested for `meta.provider == "codex"`. Lower risk (worker top-level `base_url` is usually absent — the resolve_config blob's `None` is stripped), but it should get the same guard + a test. Also still open: the `500 invalid UUID` on a short thread-id prefix (input validation in `require_thread_owner`/`get_thread`).

## Open questions / adjacent findings

- **Blast radius:** how many existing sessions carry a gateway `base_url` pinned on a codex model? Worth a one-off scan of `threads.metadata.config_override` (codex model + `base_url LIKE '%srw-litellm%'`).
- **Adjacent minor bug:** `GET /api/persistent/threads/<8-char-prefix>` returns a raw **500** (`asyncpg: invalid UUID … got 8`) instead of 400/404 — `require_thread_owner`/`get_thread` (`database/postgres.py:get_thread`) don't validate UUID shape before the query. Cosmetic, unrelated to the 401.

## Appendix — facts established this investigation

- Session `a1153f56-eb55-49ef-a21e-3151b1d46ff3`, ns `superhuman-remote-worker`, agent `srw-agent-j-fbca2952` (10.42.3.24), workspace `ws-thread-a1153f56-eb5` @ 10.42.3.74. Errors 2026-06-25 11:43 & 11:45 (turns 1 & 2).
- Orchestrator image `ghcr.io/knaeckebrothero/superhuman-remote-worker-orchestrator:sha-8c0366e`, started 2026-06-25T09:41Z, 0 restarts.
- Dev gateway live: `srw-litellm` + `srw-litellmdb` pods present (~20h); prod stack `srw-prod-private` has **no** litellm pod.
- `LITELLM_BASE_URL` ← configMap `srw-config`; `LITELLM_MASTER_KEY` ← secret `srw` (gateway enabled).
- `compute_fleet_key` (`orchestrator/services/litellm_gateway.py:444`) → `sk-srw-fleet-<hmac40>`.
