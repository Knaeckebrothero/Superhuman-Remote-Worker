# Codex / gpt-5.x models report 0 cached tokens in the By-Model usage view

**Status:** root-caused + FIX BUILT 2026-07-10 (uncommitted). Extraction fix across 4 files, unit-tested; k3d live-verification steps below.
**Severity:** low — cosmetic/reporting only. No tokens are lost and no billing is wrong at the provider; the dashboard just under-reports cache hits (and the derived "cost" for codex rows is computed off the full prompt count, so it can be modestly overstated).
**Component:** `orchestrator/services/audit_usage.py` (extraction SQL), `src/core/archiver.py` (worker capture), `src/persistent_graph.py` + `src/api/persistent_app.py` (session capture).
**Observed on:** dev/homelab By-Model view — every `gpt-*` row shows `CACHED TOK. 0 / CACHE HIT 0.0%`, while `MiniMax-M3` (14.7%) and `gemma-4-moe` (21.3%) show real cache numbers.
**Related:** `project_codex_proxy`, `project_rate_limiting_v2`, `reference_usage_view_gateway_metering_routing`.

---

## TL;DR

The metering extractor read cached prompt tokens from exactly **one** JSON path —
`metrics.token_usage.prompt_tokens_details.cached_tokens` — which is the **Chat
Completions** shape. MiniMax and gemma use Chat Completions, so their cache
numbers come through. The `gpt-*` / codex models route through the CLIProxyAPI
codex proxy, which speaks **only** the OpenAI **Responses API** (`/v1/responses`).
On that path:

- LangChain builds `response_metadata` **without** any `token_usage` key at all
  (`_construct_lc_result_from_responses_api` in `langchain_openai/chat_models/base.py`
  only copies `created_at/id/status/model/service_tier/...`). Token counts live
  **only** in the message's normalized `usage_metadata`, where cached tokens are
  under `usage_metadata.input_token_details.cache_read` (see
  `_create_usage_metadata_responses`, which maps the raw
  `input_tokens_details.cached_tokens` → `cache_read`).
- Worker codex jobs therefore had **no** `token_usage` to read, and sessions (the
  dominant codex traffic) meter from a hand-built `metadata` dict that never
  included a cached field.

So `m_cached` was always NULL for codex → no `cached-prompt-token` usage_event →
0 cached / 0.0% cache hit.

## The fix (extraction now reads three homes)

Cached prompt tokens are captured at the source into a provider-agnostic home and
read with a Python `_first_int` fallback chain in `audit_usage.py`:

| # | column | JSON path | who populates it |
|---|--------|-----------|------------------|
| 1 | `m_cached` | `metrics.token_usage.prompt_tokens_details.cached_tokens` | worker, Chat Completions (minimax, gemma, …) — unchanged |
| 2 | `m_cached_norm` | `metrics.usage_metadata.input_token_details.cache_read` | worker, LangChain-normalized — the only home for the Responses API / codex |
| 3 | `md_cached` | `metadata.cached_tokens` | persistent session turns |

Changes:

- **`src/core/archiver.py`** — worker rows now also store
  `metrics.usage_metadata = response.usage_metadata` (normalized; the only place
  codex token counts exist).
- **`src/persistent_graph.py`** — `turn_metrics` gains `cached_tokens` from
  `usage_md.input_token_details.cache_read` (fallback to
  `token_usage.prompt_tokens_details.cached_tokens`).
- **`src/api/persistent_app.py`** — `_loop_archive_llm_call` threads
  `cached_tokens` into the session `llm_requests.metadata`.
- **`orchestrator/services/audit_usage.py`** — `_SELECT_SQL` adds `m_cached_norm`
  and `md_cached`; the cached calc is now
  `min(_first_int(m_cached, m_cached_norm, md_cached), prompt)`.

Forward-only: the materializer advances a timestamp cursor, so only codex traffic
produced **after** the fix deploys will show cached tokens. Historical rows are
not backfilled.

## The one thing k3d verification must confirm

The extractor can only surface cached tokens the proxy actually returns. It is not
yet pinned by a fixture whether **CLIProxyAPI forwards** `input_tokens_details.
cached_tokens` in its `/v1/responses` `usage` block, or strips it. Two outcomes:

- proxy forwards it → cached tokens now appear (fix complete).
- proxy strips it → cached still 0; the fix is correct but the gap moves upstream
  to the proxy (out of scope here). The raw-dump step below distinguishes these.

## k3d verification

### 1. Deploy the change

Tilt rebuilds the agent image (`src/*`) and reloads the orchestrator
(`orchestrator/*`). Wait for both resources green in the Tilt UI.

### 2. Generate codex traffic with prompt caching

Cache hits need a repeated prefix, so run **two** turns/jobs on a `gpt-5.x` (codex)
model in the same context — the second call reuses the first's prompt prefix:

- **Session path (dominant codex traffic):** open `https://localhost/`, start a
  session on a codex model, send one message, then a follow-up in the same thread.
- **Worker path:** create two jobs on a codex expert against the same project.

### 3. Confirm cached-token usage_events now materialize

The materializer runs on a timer with a 60 s aging window — allow ~2 min, then:

```bash
# cached-prompt-token rows for codex models (should be non-empty post-fix)
kubectl --context=k3d-srw -n srw exec deploy/srw-orchestrator -c orchestrator -- \
  psql "$DATABASE_URL" -c \
  "SELECT resource AS model, unit, SUM(quantity) qty, COUNT(*) events
     FROM usage_events
    WHERE unit = 'cached-prompt-token' AND resource LIKE 'gpt-%'
    GROUP BY resource, unit ORDER BY qty DESC;"
```

Then reload the Admin → Usage → By-Model view: the codex rows should show a
non-zero CACHED TOK. / CACHE HIT %.

### 4. If cached is still 0 — capture the raw proxy usage block

The codex raw-response dump is now values-driven (`llm.debugCodexRawResponse`,
default `"0"`). It writes each non-streaming `/v1/responses` request+response pair
to `llm.codexRawDumpDir` (default `/app/logs/codex-raw`) on the pod running the LLM
call — the on-demand **agent** pod for sessions/jobs, which inherits the value via
`envFrom` the shared `srw-config` ConfigMap.

Enable it, then start a **fresh** session (so a newly-provisioned agent pod picks
up the flag) and inspect the `usage` object the proxy actually returned:

```bash
# 1. flip the flag in the gitignored local overlay
#    deployment/values-local.yaml:
#      llm:
#        debugCodexRawResponse: "1"

# 2. apply (non-Tilt: helm upgrade; Tilt: it re-applies values — do NOT
#    `helm upgrade` under Tilt, it reverts the dev image, see
#    reference_tilt_helm_upgrade_reverts_image)
helm upgrade srw ./helm -n srw --kube-context=k3d-srw -f deployment/values-local.yaml

# 3. start a FRESH codex session in the UI and send a turn, then read the
#    newest dump's usage block from that agent pod:
POD=$(kubectl --context=k3d-srw -n srw get pod -l srw/managed-by=agent-provisioner \
        -o name | head -1)
kubectl --context=k3d-srw -n srw exec "$POD" -- \
  sh -c 'f=$(ls -t /app/logs/codex-raw/codex-raw-*.json | head -1); \
         python -c "import json; d=json.load(open(\"$f\")); \
                    print(json.dumps(d[\"response_body\"].get(\"usage\"), indent=2))"'
```

- `usage.input_tokens_details.cached_tokens` present & > 0 → proxy forwards it; the
  fix is sufficient (if step 3 was still 0, check the cursor/aging window).
- key absent or `usage` lacks details → the proxy strips cache stats; extraction is
  correct but blind, and the follow-up is a proxy-side fix (file separately).

Set `debugCodexRawResponse` back to `"0"` when done (the dump is verbose and
writes a file per call). It fires only on the non-streaming Responses path
(`src/llm/reasoning_chat.py:_dump_codex_raw_response`); codex sessions use
`ainvoke` (non-streaming), so it triggers for them.
