# LiteLLM gateway enablement dropped GPT/codex reasoning capture (job-debug chat + sessions chat)

**Status:** **RESOLVED 2026-06-24** (develop, uncommitted) · root cause isolated + empirically confirmed on dev · fix implemented + **k3d-verified end-to-end** (live gpt-5.5 session renders + persists reasoning again). See "Fix" below.
**Found:** 2026-06-24, investigating "we don't see the reasoning of gpt models in the job debug chat or in the sessions chat anymore"
**Component:** `src/llm/reasoning_chat.py` capture · `src/core/loader.py` factory selection · `src/core/model_registry.py` endpoint→meta · `orchestrator/main.py` dispatch gateway reroute · LiteLLM gateway · Codex proxy (CLIProxyAPI v7.2.27)
**Related:** [[session_empty_response_gpt5_codex_stop]] (same `agent → LiteLLM → codex-proxy` family, different symptom) · [[litellm_streaming_usage_not_surfaced]] (same route, usage-bar variant) · [[langchain_responses_api_streaming]] (Responses-API fragility) · [[reasoning_effort_injected_without_capability_guard]]

## Symptom

Reasoning ("thinking") content from **GPT / codex models** (`gpt-5.x`) stopped
appearing in **both** Cockpit surfaces that render it:

- **Job debug chat** (worker-graph audit trail, served from `chat_history.reasoning`)
- **Persistent sessions chat** (served from `thread_messages.thinking`)

Reasoning from non-GPT reasoning models (e.g. `gemma-4-moe`) still shows. The
two infra changes the user associated with the regression were the
MongoDB→Postgres audit cutover and the introduction of the LiteLLM proxy.

## TL;DR

- **NOT the Mongo→Postgres audit migration.** Storage, serialization and the
  cockpit bindings all preserve reasoning faithfully (proven three ways below).
- **It is the LiteLLM gateway**, enabled on dev by `ac211a52`
  (2026-06-22, `deployment/values-experimental.yaml:303` `litellm.enabled: true`).
- The break is **GPT/codex-specific** (the Responses-API-via-proxy path), **not**
  a blanket "LiteLLM strips reasoning" — `gemma-4-moe` is also LiteLLM-routed and
  keeps its reasoning.
- Both Cockpit surfaces share **one** upstream source
  (`additional_kwargs["reasoning_content"]`), so they went dark together.

## Evidence — longitudinal capture rate (the smoking gun)

Queried the live dev cluster (`main` ctx, ns `superhuman-remote-worker`),
read-only, from inside the orchestrator pod (its DB env, so no creds in transit).
Counts are rows with non-null reasoning ÷ total, per UTC day.

### Job-debug chat — `srw_audit.chat_history` (`call_type` rows incl. `main`)

| Day | GPT (`gpt-5.x`) total | w/ reasoning | rate | `gemma-4-moe` rate |
|-----|------:|------:|----:|----:|
| 06-19 (Postgres cutover) | 92 | 34 | **36%** | 50% |
| 06-20 | 71 | 24 | **33%** | 45% |
| 06-21 | 95 | 39 | **41%** | 20% |
| **06-22 (LiteLLM on)** | 303 | 19 | **6%** | 44% |
| **06-23** | 82 | 0 | **0%** | 41% |
| **06-24** | 407 | 0 | **0%** | 64% |

14-day per-model totals: `gpt-5.5` 682/2024 (33%, but all-zero after 06-22),
`gpt-5.4-mini` 156/540 (28%, all-zero after 06-22), `gemma-4-moe` 3425/9273
(36%, **stable through 06-24**). (`gpt-5.3-codex-spark` is 0/306 across the whole
window — a separate never-captured case, see Open questions. `gemini-3.5-flash`
0/261 is expected — non-reasoning model.)

### Sessions chat — `srw.thread_messages` (`role IN ('ai','assistant')`, col `thinking`)

| Day | GPT total | w/ thinking | rate |
|-----|------:|------:|----:|
| 06-11 | 2 | 2 | 100% |
| 06-13 | 47 | 15 | 31% |
| 06-14 | 43 | 8 | 18% |
| 06-16 | 33 | 10 | 30% |
| **06-22** | 10 | 0 | **0%** |
| **06-23** | 11 | 0 | **0%** |
| **06-24** | 4 | 0 | **0%** |

A non-GPT reasoning model in the same table still captured thinking 2/2 on 06-23.
(The separate `reasoning` JSONB component column is 0 everywhere on this write
path; `thinking` is the live column.)

**Reading:** GPT reasoning capture cratered from ~40% to 0% **exactly at the
06-22 gateway enablement**, on both surfaces, while a co-resident LiteLLM-routed
model is unaffected. This both **dates** the regression and **localizes** it to
the codex/Responses path.

## Why both surfaces broke together (single source)

Both surfaces ultimately read reasoning from the **same** field on the
LangChain `AIMessage`: `additional_kwargs["reasoning_content"]`, populated by
`src/llm/reasoning_chat.py`. The two persistence paths just copy it onward:

- **Worker graph → job-debug chat:** `src/core/archiver.py` extracts
  `response.additional_kwargs.get("reasoning_content")` into the
  `chat_history.reasoning` column (and the whole message dict, incl.
  `additional_kwargs`, into `llm_requests.response`).
- **Persistent graph → sessions chat:** `src/api/persistent_app.py::_extract_thinking`
  reads the same `additional_kwargs["reasoning_content"]` (among other shapes)
  and saves it to `thread_messages.thinking` via `save_thread_message`.

When the source field is empty, **both** paths faithfully persist "no reasoning",
and both cockpit views render nothing. The defect is at **capture**, upstream of
all persistence/serialization/rendering.

## Root cause — three co-timed changes (~2026-06-22)

1. **Dispatch reroute to the gateway.** With `litellm.enabled: true`, the chart
   sets `LITELLM_BASE_URL` on the orchestrator
   (`helm/templates/configmap.yaml:44`, `orchestrator/deployment.yaml:178-186`).
   At dispatch, any endpoint-backed model (`meta.endpoint_id` set) has its
   `base_url`/`api_key` overwritten to point at LiteLLM
   (`orchestrator/main.py:1506-1522`, mirrored for session/phase/aux models).

2. **Wrong LLM factory for codex-proxy-backed GPT.** `_endpoint_row_to_meta`
   hardcodes `provider="openai"` for **all** endpoint rows
   (`src/core/model_registry.py:229`). So `gpt-5.x` registered against the
   codex-proxy endpoint resolves to `_create_openai_llm`
   (`use_responses_api: False`, Chat Completions — `src/core/loader.py:2643,2726`)
   instead of `_create_codex_llm` (`src/core/loader.py:2608-2609`), which is the
   only factory that requests a reasoning summary
   (`reasoning={"effort":…,"summary":"auto"}` gated by
   `_should_use_reasoning_summary`, `loader.py:3299`). On the Chat-Completions
   path the model is never asked for a reasoning summary, and
   `src/llm/reasoning_chat.py` harvests reasoning from non-standard delta fields
   (`choices[].delta.reasoning_content` / `.reasoning` / `.reasoning_details`)
   that this route does not emit. (Corroborated by [[session_empty_response_gpt5_codex_stop]],
   where the same session shows the agent hitting LiteLLM `/v1/chat/completions`
   and LiteLLM doing a `responses → chat/completions` translation.)

3. **The crutch was removed.** The only thing that had been surfacing GPT
   reasoning on the Chat-Completions path was the codex proxy leaking its harmony
   `analysis` channel into `content`. Codex proxy bump v7.1.39 → **v7.2.27**
   (`51581b71`, "fix harmony tool-call leak", ~06-22) correctly closed that leak.

Net: `additional_kwargs["reasoning_content"]` is never populated for GPT/codex
once routed through the gateway → both surfaces store/show nothing.

Why `gemma-4-moe` is unaffected: it's a native Chat-Completions (vLLM) model that
emits `reasoning_content` in the delta, which LiteLLM forwards; the
`reasoning_chat.py` tap still catches it. The problem is specific to the
`codex-proxy → Responses` translation, not LiteLLM in general.

## What is NOT the cause (ruled out)

- **Mongo→Postgres audit cutover (2026-06-19, `da0123b9`/`ac7ad3ae`/`5d780a6b`/`5ffeb5fd`).**
  The new schema added a dedicated `chat_history.reasoning JSONB` column; the
  writer (`src/database/audit_writer.py`) and reader
  (`orchestrator/database/audit_store.py`) both preserve it; `llm_requests.response`
  keeps the full message dict incl. `additional_kwargs`. The cutover's
  `formatters.py` change was only `_id`→`id`. **Decisive proof:** `gemma-4-moe`
  reasoning survived 06-19 and is still captured today — if the audit store
  dropped reasoning, gemma would read 0% too.
- **Cockpit rendering / serialization.** Field names match end-to-end
  (`chat_history.reasoning`, `llm_requests.response.additional_kwargs.reasoning_content`
  → `request-viewer`; `thread_messages.thinking` → persistent-chat live + replay).
  Empty input renders empty; the code is correct.

## Reproduction / how to verify

```bash
# GPT reasoning is 0% after 06-22 while gemma is unaffected (audit DB):
kubectl --context=main -n superhuman-remote-worker exec -i deploy/srw-orchestrator \
  -c orchestrator -- python3 - <<'PY'
import os, asyncio, asyncpg
async def main():
    c = await asyncpg.connect(host=os.environ["AUDIT_POSTGRES_HOST"],
        user=os.environ["AUDIT_POSTGRES_USER"], password=os.environ["AUDIT_POSTGRES_PASSWORD"],
        database=os.environ["AUDIT_POSTGRES_DB"])
    rows = await c.fetch("""
      SELECT (lower(model) LIKE 'gpt%' OR lower(model) LIKE '%codex%') AS is_gpt,
             to_char(date_trunc('day',timestamp),'YYYY-MM-DD') d,
             count(*) total, count(reasoning) w
      FROM chat_history WHERE timestamp > now() - interval '10 days'
      GROUP BY 1,2 ORDER BY 2 DESC,1""")
    for r in rows: print(dict(r))
asyncio.run(main())
PY
```

To confirm the wire shape on a live turn: set `DEBUG_LLM_STREAM=1` on the agent
(prints a `[reasoning N chars]` tail per call, `reasoning_chat.py:1040-1048`) and
drive one `gpt-5.x` session turn. Absent `[reasoning …]` ⇒ reasoning never
reached `additional_kwargs`, confirming the source-side drop. (`DEBUG_CODEX_RAW_RESPONSE=1`
only fires on the **non-streaming** `/responses` path — see
[[session_empty_response_gpt5_codex_stop]] "Isolation complete".)

## Fix — implemented + verified (2026-06-24)

The designed way to get `gpt-5.x` reasoning is the **codex factory + Responses API
+ `reasoning.summary: "auto"`**, not a proxy content-leak. Two coordinated changes:

1. **Resolve codex-proxy-backed models to `provider="codex"`.** New
   `_endpoint_factory_provider(base_url, label)` in `src/core/model_registry.py`
   returns `"codex"` for the system Codex proxy (detected by the seeded
   `codex-proxy` label or a `codex-proxy` host in the base_url), `"openai"`
   otherwise. Wired into both `_endpoint_row_to_meta` and `_catalog_row_to_meta`
   (replacing the hardcoded `provider="openai"`). The agent then builds
   `_create_codex_llm` (Responses API + `reasoning.summary: "auto"`).

2. **Codex models bypass the LiteLLM gateway.** `orchestrator/main.py`
   `_inject_dispatch_credentials` (worker main model) now injects `meta.provider`
   in the endpoint branch (it previously didn't, so endpoint models silently
   defaulted to the openai factory) **and** sets
   `_gw = _gw_scoped if meta.provider != "codex" else None`;
   `_inject_model_credentials` (strategic/tactical/aux + sessions) gets the same
   `provider == "codex"` guard. Codex models then hit the proxy's `/v1/responses`
   directly. Non-codex endpoint models (e.g. `gemma-4-moe`) still route through
   the gateway, so measurement/rate-limiting is unaffected for them.

Tradeoff accepted for v1: codex traffic is not measured/rate-limited at the
gateway. *Follow-up (deferred):* teach the gateway to forward `/responses` +
reasoning so codex models stay metered **and** keep reasoning.

Tests: `tests/test_model_registry.py::TestEndpointFactoryProvider` +
`::TestEndpointMetaProviderResolution` and
`tests/test_dispatch_phase_credentials.py::TestCodexBypassesGateway` (11 new;
63 across the two files green; ruff clean).

### Verification (k3d, image-baked fix)

- **Resolution** (deterministic probe vs the live catalog DB):
  `resolve_model("gpt-5.5")` → `provider=codex`, routing `via gateway = False`,
  factory `_create_codex_llm`. Baseline before the fix: `provider=openai`,
  `via gateway = True`, `_create_openai_llm`.
- **Agent** (live session turn): `Created Codex LLM: model=gpt-5.5,
  base_url=http://srw-codex-proxy:8317/v1, reasoning=responses_api(effort=high)`,
  while `gemma-4-moe` still logs `Created OpenAI LLM ...
  base_url=http://srw-litellm:4000/v1` (gateway). `reasoning_level=high` comes
  from `config/persistent_defaults.yaml` / `config/defaults.yaml` (the model row
  carries `reasoning_level=None`, which dispatch does not inject over the default).
- **Sessions surface** end-to-end: a live gpt-5.5 session rendered a reasoning
  bubble ("Thought for a moment" + summary) with a Reasoning-token count, and the
  text persisted to `thread_messages.thinking` (447 chars) → survives reload.
- **Job-debug surface**: jobs use the same codex factory via the worker dispatch
  path; the non-streaming `ainvoke` path populates
  `additional_kwargs["reasoning_content"]` → `chat_history.reasoning` (the same
  faithful archive path the storage analysis already cleared). (A session's
  incidental `chat_history` row stays empty — sessions stream reasoning into
  `thinking`, not `additional_kwargs`; pre-existing, not this regression.)

A lightweight daily check that per-model-class reasoning capture doesn't collapse
to 0% remains a nice-to-have beyond the unit guards.

## Open questions

- **Relative contribution of (1) reroute vs (3) proxy-bump.** Both co-timed on
  06-22; the empirical signature can't separate them, and the fix is the same
  regardless. One `DEBUG_LLM_STREAM=1` capture on a gpt-5.x turn routed *direct
  to the codex proxy* (gateway bypassed) vs *through the gateway* would
  disambiguate.
- **`gpt-5.3-codex-spark` = 0/306 across the entire window** (never captured,
  even pre-06-22) — likely a model that doesn't emit a reasoning summary, or a
  config gap; separate from this regression but worth a glance.

## Appendix — facts established this investigation

- `deployment/values-experimental.yaml:303` → `litellm.enabled: true` (commit
  `ac211a52`, 2026-06-22 14:11). Supersedes the stale "gateway never enabled on
  dev / LOCAL-k3d-only" note.
- Dev pods present: `srw-litellm`, `srw-codex-proxy` (v7.2.27), `srw-auditdb-0`,
  `srw-orchestrator`. Gateway live.
- Storage paths verified: `chat_history.reasoning` + `llm_requests.response`
  (audit), `thread_messages.thinking` (app DB) all carry reasoning when present.
