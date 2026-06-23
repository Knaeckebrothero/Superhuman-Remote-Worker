# Phase-pinned endpoint models ship without transport (codex 401)

## Status: Resolved — Option B + D shipped to dev and verified live (2026-06-23)

## Problem

A worker job that pins the **strategic/tactical phases** to endpoint-backed models
(e.g. the Codex-proxy models `gpt-5.5` / `gpt-5.4-mini`) fails on the **first
strategic LLM call** with:

```
Error code: 401 - {'error': {'message': 'Invalid, disabled, or expired API key', 'type': 'authentication_error'}}
```

Audit: `classification: permanent, recoverable: false, attempts: 1` — the job dies
on iteration 0. The **same model used as a flat chat model in a persistent session
works fine**.

Incident: job `eec20eeb-cf8f-496d-aa2b-8214041207c7` ("Research 01: verify-before-done"),
`config_name=scholar`, dev cluster (`superhuman-remote-worker`), 2026-06-22.

## Evidence

Job `config_override` (`jobs.config_override`):

```json
{ "llm": { "strategic": {"model": "gpt-5.5"}, "tactical": {"model": "gpt-5.4-mini"} },
  "scholar": {"enabled": false}, "autonomy": "review" }
```

Delivered/stored `resolved_config.agent.llm` (Postgres `jobs.resolved_config`;
`redact_config_override` strips **only** secret keys — `provider`/`base_url` are
preserved verbatim, `security/access.py:717`):

```json
{ "model": "gemma-4-moe", "base_url": "https://ai.h4ll.app/v1", "provider": null,
  "strategic": { "model": "gpt-5.5",      "temperature": 0, "reasoning_level": "high" },
  "tactical":  { "model": "gpt-5.4-mini", "temperature": 0, "reasoning_level": "high" } }
```

The base model carries `base_url`; the **phase pins carry no `base_url`, no
`api_key`, and no `provider`.**

Supporting facts:

- **The codex models are correctly catalogued.** `models` rows `gpt-5.5`,
  `gpt-5.4-mini`, `gpt-5.3-codex-spark` are all `provider_kind='endpoint'`,
  `provider_ref=aac23bfd…` → `llm_endpoints` row `codex-proxy`
  (`base_url=http://srw-codex-proxy:8317/v1`, api_key present). This is the
  **identical shape** to the working `gemma-4-moe` (Local Router endpoint).
- **The codex proxy is healthy and needs no inbound key.** An unauthenticated
  `GET /v1/models` returns the catalog, and a live `POST /v1/chat/completions
  {model: gpt-5.5}` returns a completion. A request that *reaches* the proxy
  cannot produce this 401.
- Therefore the failing `gpt-5.5` request **never reaches the proxy**. With no
  `base_url`/`provider`, the agent's `create_llm` resolves `gpt-5.*` → the OpenAI
  factory default (`api.openai.com`); the 401 wording is OpenAI's signature.

## Root cause

Endpoint transport (`base_url`/`api_key`/`provider`) is injected for the
**top-level** `llm.model` (base model `gemma-4-moe` → `ai.h4ll.app`) but **not**
for **phase pins** (`llm.strategic` / `llm.tactical`). The phase pins reach the
agent as bare `{model, …}` and misroute to the OpenAI default.

The dispatch logic that *should* cover this is present in the deployed build
(`sha-3371e3c`):

- blob path: `resolve_config(...)` → `inject_blob_credentials(_resolved, lambda co:
  _inject_dispatch_credentials(job, co))` → `store_resolved_config`
  (`orchestrator/main.py:2000-2006`, `services/config_resolver.py:144-187`);
- the strategic/tactical loop calls `_inject_model_credentials`
  (`orchestrator/main.py:~1555-1564`);
- `_inject_model_credentials` injects endpoint transport for `origin in
  {custom,system,catalog}` rows with an `endpoint_id` (`main.py:~3383`), and
  `resolve_model`/`_catalog_row_to_meta` give endpoint rows `provider='openai'`,
  `endpoint_id=<endpoint>` (`src/core/model_registry.py:248-287`, `290-341`).

### Pinpointed mechanism (confirmed)

`serialize_resolved_config` emits the phase blocks with **explicit `None`
leaves** for every field, e.g. `strategic = {"model": "gpt-5.5", "provider":
None, "base_url": None, ...}`. `inject_blob_credentials` then strips `None`-valued
keys **only at the top level** of `co["llm"]` (`config_resolver.py:172-176`), so:

- the **base model** keys (`llm.base_url=None`, …) ARE stripped → the top-level
  branch's `setdefault("base_url", …)` fills them → gemma gets transport; but
- the **nested phase blocks** keep `base_url=None` / `provider=None`, and
  `_inject_model_credentials` injects via `section.setdefault(...)` — **a no-op
  against a present-but-`None` key**. So the codex pins keep `base_url=None`,
  ship without transport, and misroute to `api.openai.com`.

The phase loop *does* run (it logs `"injected credentials for strategic
override"`), which is why the gap was invisible — but the "did we inject?" guard
`"base_url" not in _section` reads the `None` key as present and the log is
misleading. Confirmed by a reproduction test driving the real
`resolve_config` → `inject_blob_credentials` path (`KeyError`/`None` on the
delivered `strategic.base_url`).

## Why recent fixes don't cover it

- The agent-side codex-401 classification change (`src/graph.py`
  `_is_codex_proxy_error`, `sha-3371e3c`) only reclassifies a **codex-proxy** 401
  as retry/recoverable. This request hits **`api.openai.com`**, which it
  (correctly) leaves `permanent`. Orthogonal — keep it as defense-in-depth.
- The 2026-05-12 phase-override fix added the strategic/tactical injection loop,
  but the **blob-delivered scholar + endpoint-phase-pin** combination still slips
  through.

## Session vs. job (the real difference)

- **Session (works):** flat `llm.model=gpt-5.5` → top-level injection →
  `base_url=http://srw-codex-proxy:8317/v1`, `provider=openai`.
- **Job (fails):** `strategic`/`tactical` phase pins → no injection →
  `api.openai.com` → 401.

It is **not** timing/transience and **not** a codex-proxy outage.

## Fix options

### Option A — Repair the phase-pin injection in dispatch (targeted)
Make the strategic/tactical loop reliably resolve + inject endpoint transport,
exactly like the top-level `llm.model` branch. Requires pinning the runtime
trigger (trace above) first; the fix may be as small as correcting how the blob
path threads phase sections into the injector.
- **Pros:** minimal, localized; matches the current design (orchestrator injects
  transport).
- **Cons:** depends on the still-unconfirmed exact trigger; the blob/override/
  `deep_merge` interaction is subtle; risk of a partial fix that misses other
  slots (e.g. `vision`/`auxiliary` phase pins).

### Option B — Resolve transport for *every* model slot in the blob resolver (structural; recommended)
Have `inject_blob_credentials` / `resolve_config` walk **all** model-bearing slots
(`llm.model`, `llm.strategic`, `llm.tactical`, `auxiliary`, `vision`, …) and attach
catalog transport, so the delivered blob is **transport-complete by construction**.
Honors the existing "a delivered blob is complete" contract (`main.py:2034-2038`)
and removes the top-level-vs-phase asymmetry.
- **Pros:** uniform, config-shape-independent; fixes all phase/capability pins at
  once; no reliance on the override-shaped injector seeing the right keys.
- **Cons:** larger change in the resolver; must enumerate all slots; more test
  surface.

### Option C — Agent-side guard (defense in depth)
When a phase model arrives with no `base_url`/`provider`, the agent must NOT
silently default to `api.openai.com` — either resolve the endpoint agent-side or
fail with a clear error.
- **Pros:** protects against any future orchestrator gap.
- **Cons:** duplicates resolution the architecture centralizes in the orchestrator;
  inheriting the parent base_url is what produced the original 404 incident and
  must be avoided.

### Option D — Fail-fast at dispatch (companion to A/B)
Upgrade the existing "pinned `<phase>` model … no endpoint or provider key
resolvable … almost certainly 404" **warning** (`main.py:~1574`) into a hard
dispatch failure with an actionable message, so missing phase transport surfaces
as a clear error rather than an opaque downstream 401.
- **Pros:** cheap; turns confusing 401s into actionable errors; catches
  regressions.
- **Cons:** doesn't fix routing — pair with A or B.

## Recommendation

**B + D.** Make the resolver emit a transport-complete blob (fixes the asymmetry at
the layer that's contractually responsible for a "complete" blob, and covers every
slot), and add a dispatch-time fail-fast so any future gap is loud, not a silent
OpenAI 401. Run the runtime trace first to confirm B fully covers the trigger (and
to size A if a minimal patch is preferred instead).

## Resolution (shipped to dev + verified live 2026-06-23)

Committed on `develop`, deployed to the dev orchestrator, and confirmed working
by re-running the codex phase-pinned job (no longer 401s at api.openai.com).

**Option B** — `inject_blob_credentials` now strips `None`-valued keys from the
**nested** phase blocks (`llm.strategic` / `llm.tactical` / `llm.summarization`),
not just the top-level section (`orchestrator/services/config_resolver.py`). With
the `None` leaves gone, the existing phase-pin injection's `setdefault` lands, so
the delivered blob is transport-complete for phase pins exactly as for the base
model. Regression test: `tests/test_dispatch_phase_credentials.py::
TestPhaseOverrideCredentialInjection::test_blob_delivery_injects_phase_pin_transport`
(drives the real `resolve_config` → `inject_blob_credentials` path).

**Option D** — `unrouted_model_slots(blob)` (`config_resolver.py`) flags any
model-bearing slot (`llm` / `llm.strategic` / `llm.tactical` / `auxiliary`) left
with no `base_url`/`api_key`/`provider` after injection; `_dispatch_job_to_agent`
calls it post-injection and **fails the job with an actionable error** instead of
letting the agent misroute. Conservative (only fires when a slot is truly
unrouted). Tests: `tests/test_config_resolver.py::test_unrouted_model_slots_*`.

The agent-side codex-401 classification change (`_is_codex_proxy_error`) is kept
as orthogonal defense-in-depth (it does not apply here — the misroute hits
`api.openai.com`, not the proxy).

## Verification / repro

1. Dispatch a worker job with
   `config_override={"llm":{"strategic":{"model":"gpt-5.5"},"tactical":{"model":"gpt-5.4-mini"}}}`
   (config_name `scholar`).
2. **Before fix:** strategic call 401s; `jobs.resolved_config.agent.llm.strategic`
   has no `base_url`/`provider`.
3. **Trace:** log `co["llm"]` entering `_inject_dispatch_credentials` and the
   `resolve_model("gpt-5.5")` meta in-process to pin the exact line.
4. **After fix:** `resolved_config.agent.llm.{strategic,tactical}` carry
   `base_url=http://srw-codex-proxy:8317/v1` + `provider=openai`; the job reaches
   the proxy (200) and completes.

## Related

- `src/graph.py` `_is_codex_proxy_error` / `_classify_llm_error` — codex-401
  classification (orthogonal; defense-in-depth).
- `docs/issues/langchain_responses_api_streaming.md` — codex/Responses-API path.
- 2026-05-12 phase-override credential incident (same class; the loop this issue
  shows is incomplete for blob-delivered phase pins).
