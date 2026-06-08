# API key resolution

How provider API keys flow from configuration sources into agent runtime, and where the seams sit.

## Wire format vs config source

A single env var name like `OPENAI_API_KEY` used to play two roles:

- **Wire format** — the env var name the orchestrator writes into the agent's per-job environment for runtime delivery.
- **Config source** — the env var name local `.env` / Vault provided to the orchestrator process at boot, so it had keys to inject.

Those two roles are now decoupled:

- The **config source** is the `system_api_keys` table. It's seeded from a one-time `SEED_*` env var read (local dev: `python init.py`; cluster: `helm/templates/orchestrator/llm-seed-job.yaml`).
- The **wire format** stays unchanged: the orchestrator's `_inject_model_credentials` and `_inject_env_key_credentials` write `OPENAI_API_KEY` (and siblings) into the agent pod's per-job env. Agent-side helpers (`src/services/vision_helper.py`, `audio_helper.py`, LLM clients in `src/core/loader.py`, `src/tools/research/web.py`) read those bare names — they're the runtime-delivery target and stay as-is.

After this split, the orchestrator process **never reads bare `OPENAI_API_KEY`** etc. directly. All key lookups flow through `system_api_keys` (decryption is inline in `db.get_system_api_key(provider)`).

## Resolution order

1. **Custom / system endpoint** (`llm_endpoints` row, possibly `user_id IS NULL` for system-scoped). The endpoint row carries inline `base_url` + `api_key`. This is the path for vLLM, OpenRouter-style gateways, private deployments, and the **codex proxy** (a system endpoint seeded automatically when `CODEX_PROXY_URL` is set — see "Codex proxy as a model source" below). Wins over everything below.
2. **Built-in catalog model** → infer provider from model name (`claude-*` → anthropic, `openrouter/*` → openrouter, else openai) → `db.get_system_api_key(provider)`. No DB row → `RuntimeError` with a seed-hint message ("Configure via Admin → Providers or seed via SEED_<PROVIDER>_API_KEY").
3. **Legacy codex raw-string fallback** (`codex/*` model IDs that aren't in the catalog) — read `CODEX_API_KEY` env var (default `not-needed`). Once codex models are added to the catalog via the codex-proxy endpoint, this path stops being used. Codex auth is user-bound via the CLIProxyAPI proxy and is intentionally absent from `VALID_SYSTEM_API_KEY_PROVIDERS`.

## Seeding

Both helm and local dev funnel through the same idempotent seeder (`orchestrator/seed/llm_config.py`). The seeder accepts a payload of `apiKey:` (literal) or `apiKeyEnv: "<NAME>"` (resolved at run time) and inserts only into providers without an existing row. After first boot the table is authoritative — rotating keys via Admin → Providers is never clobbered.

Local dev: `python init.py` calls `_seed_llm_keys_from_env(db)` which iterates `VALID_SYSTEM_API_KEY_PROVIDERS` and constructs a payload from any present `SEED_<PROVIDER>_API_KEY` env vars. The helm seeder Job follows the same pattern with values supplied by the Vault-synced secret.

Env vars that participate:

- `SEED_OPENAI_API_KEY`
- `SEED_ANTHROPIC_API_KEY`
- `SEED_GOOGLE_API_KEY`
- `SEED_GROQ_API_KEY`
- `SEED_OPENROUTER_API_KEY`

The legacy `vision` slot in `VALID_SYSTEM_API_KEY_PROVIDERS` is intentionally excluded — vision keys ride along on the per-endpoint inline `api_key` for custom endpoint rows.

**Tavily is not a seedable provider.** Tavily is a web search engine, not an LLM, and is managed exclusively as a `TAVILY_API_KEY` env var sourced from the shared application Secret (Vault-synced in production; chart-managed `secrets.values` in local dev). It has **two** runtime consumers, both reading from that Secret:

- **Agent** pods — the `web_search`/`extract_webpage`/`crawl_website`/`map_website` tools (`src/tools/research/web.py`). Agents inherit the *entire* Secret via `envFrom` in `orchestrator/services/agent_provisioner.py` (there is no static agent Deployment — pods are provisioned on demand), so any key in the Secret is automatically present.
- **Orchestrator** process — the instruction builder's in-process `web_search` (`orchestrator/services/builder_search.py:tavily_search`). The orchestrator Deployment lists env vars explicitly, so it pulls `TAVILY_API_KEY` via an explicit `secretKeyRef` (`optional: true`) in `helm/templates/orchestrator/deployment.yaml`. **Adding the key to the Secret alone does not reach the orchestrator** — the builder needs this entry.

It is not stored in `system_api_keys`/`user_api_keys`/`project_api_keys` and is not surfaced under Admin → Providers. Rotation: update the Secret source (Vault bundle in prod, `secrets.values` in `values-local.yaml` for dev) → restart the orchestrator; newly provisioned agent pods pick up the new value automatically.

## What changed in this migration

Three orchestrator code paths used to read bare env names directly. They now go through the DB resolver (or, for codex, the explicit env path):

| File | Before | After |
|---|---|---|
| `orchestrator/services/builder_search.py:tavily_search` | `os.getenv("TAVILY_API_KEY")` | accepts an optional `api_key: str \| None` kwarg; falls back to `os.getenv("TAVILY_API_KEY")` when unset. Tavily is not a DB-managed provider. |
| `orchestrator/main.py:_create_builder_llm` | fell through `BUILDER_*_API_KEY` → bare-name env | infer provider, look up in `system_api_keys`, raise `RuntimeError` with seed-hint when absent. Codex unchanged. |
| `orchestrator/services/message_triage.py:_resolve_from_env` | env-var legacy fallback under `DeprecationWarning` | **deleted** — DB-only path now |

Helm cleanup: `helm/templates/orchestrator/deployment.yaml` no longer mounts `BUILDER_API_KEY` (the orchestrator pod doesn't read it after this migration). The seeder Job (`helm/templates/orchestrator/llm-seed-job.yaml`) continues to receive `SEED_*` env vars from the Vault-synced secret unchanged.

`.env.example` carries a `SEED_*` block at the top of the LLM section. The bare-name `OPENAI_API_KEY` block stays as runtime-delivery documentation for the agent container — it's read by the agent process, never by the orchestrator.

## OpenRouter defaulting

When `SEED_OPENROUTER_API_KEY` is set (and no other defaults are configured), `init.py`'s `_apply_openrouter_defaults(db)` step pins OpenRouter-routed models for the otherwise-empty `auxiliary` and `embedding` slots:

| Slot | Default model |
|---|---|
| auxiliary | `openrouter/google/gemini-2.5-flash` |
| embedding | `openrouter/openai/text-embedding-3-large` |

This is a "fill-empty-slots" nudge, not a clobber — admins who already set defaults via Admin → Defaults are never overridden. Override either pin via `PUT /api/admin/providers/defaults/{auxiliary,embedding}`.

## Custom OpenAI-compatible endpoints

`LLM_BASE_URL` was previously used to point built-in `openai/`-prefixed models at a private vLLM/Ollama server. That path is removed from `.env.example` — custom endpoints are now configured via Admin → Providers → Endpoints. Each row in `llm_endpoints` carries its own `base_url + api_key + model list` and is resolved by the model registry at dispatch time. See `docs/features/custom_llm_endpoints.md`.

## Codex proxy as a model source

The codex proxy (CLIProxyAPI, OpenAI-compatible at `:8317`) is a third "source" admins can attach catalog models to, alongside system API keys and generic custom endpoints. The transport is implemented as a regular system `llm_endpoints` row (no schema change) — what differs is how the row gets created and how it's surfaced in the UI:

- **Seeding**: `orchestrator/init.py:_seed_codex_proxy_endpoint` runs on every boot. When `CODEX_PROXY_URL` is set, it inserts one `llm_endpoints` row with the well-known label `codex-proxy`, `base_url=$CODEX_PROXY_URL/v1`, and `api_key=$CODEX_MANAGEMENT_KEY` (resolved from env via `apiKeyEnv`). Idempotent — re-running boot is a no-op once the row exists, and admin deletes are not retroactively recreated. Skip when `CODEX_PROXY_URL` is unset (proxy not deployed).
- **Authoring**: Admin → Models. The codex-proxy endpoint appears in the provider dropdown like any other system endpoint, but with a "(codex subscription)" label hint. Click "Discover available models" to query the proxy's `/v1/models` endpoint and quick-fill catalog rows with `provider_kind='endpoint'`, `provider_ref=<codex-proxy-uuid>`. From there it's identical to vLLM/Ollama: each catalog row routes through the seeded transport.
- **Availability**: `GET /api/admin/providers/codex/availability` reports `{available, account_count, models, endpoint_id}`. The frontend uses it to render an inline status banner under the provider picker — green when ≥1 active subscription, amber otherwise (with a "Manage subscriptions" deep link to Settings → Codex). Catalog authoring is not gated on availability; admins can pre-seed catalog rows before logging in.
- **Runtime**: dispatch goes through the standard endpoint-backed path (`_inject_model_credentials`) — `base_url` + `api_key` get inlined just like any other custom endpoint. The proxy enforces Bearer auth uniformly across `/v0/management/*` and `/v1/*`, so the seeded `CODEX_MANAGEMENT_KEY` works for both discovery and chat-completion calls.
- **What's NOT covered**: per-account routing inside the proxy (the proxy itself decides which auth file mediates each request); user-scoped codex-proxy endpoints (the proxy is a shared system service); migrating the legacy `codex/*` raw-string env path (kept as a fallback, but new installs land on the catalog flow).

Admin → Providers continues to manage the seeded row like any other endpoint (test, delete, edit). The codex-proxy row gets a "codex subscription" chip and a "Manage subscriptions" link that deep-links to the existing Settings → Codex OAuth flow.

## Adjacencies (out of scope)

- `WHISPER_BASE_URL` / `EMBEDDING_BASE_URL` are still mounted from helm config as deployment-level defaults. They could become a starter custom-endpoint seed (one `llm_endpoints` row), but that's a separate change.
- The legacy `"vision"` slot in `VALID_SYSTEM_API_KEY_PROVIDERS` could be retired once all consumers route through endpoint rows.
- Codex auth migration is not on the table — codex is per-user and routed through the proxy.
