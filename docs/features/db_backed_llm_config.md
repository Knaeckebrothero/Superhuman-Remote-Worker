# DB-backed LLM Configuration (Helm as Seed, Not Source)

Move LLM providers, models, and API keys from helm values / env vars into the
database as the single runtime source of truth. Helm keeps a `seed` section
that is applied **only when the relevant tables are empty** — operators bring
up a fresh stack with zero, one, or many LLMs configured, and from then on the
live app (Cockpit + API) owns the lifecycle: add, rotate, delete without a
redeploy.

## Problem

The current split is inconsistent and brittle. Some pieces live in the DB, some
in YAML, some in env vars, and the three drift.

- **Provider API keys**: `user_api_keys` table already exists
  (`orchestrator/database/schema.sql:149`), with resolution chain *user → project
  → env*. But env fallbacks are populated from helm secrets, so "the stack is
  configured" still means "helm values are correct."
- **Custom endpoints**: `user_llm_endpoints` + `user_llm_endpoint_models`
  (schema.sql:175-205) are per-user and UI-managed — the shape we want
  everywhere else to adopt. There is currently no system-scoped equivalent, so
  a freshly seeded stack has no shared endpoint for the local vLLM until each
  user recreates one by hand.
- **Model catalog**: `config/models.yaml` is baked into the image and read at
  runtime by the loader. Adding a model means a code change and release.
- **Cockpit picker**: `cockpit.builderModels`, `cockpit.models`, and
  `cockpit.modelPresets` in `helm/values.yaml` render into `srw-cockpit-env`
  ConfigMap as `window.env.builderModels` etc. The comment calls them a
  "fallback — primary source is GET /api/models," but the fallback is the only
  path exercised, and it drifts whenever the real catalog changes.
- **Orchestrator env**: `BUILDER_MODEL`, `BROWSER_LLM_MODEL`, `CITATION_LLM_MODEL`,
  `LLM_BASE_URL` etc. are all set from helm values and written into `srw-config`.

We just hit the concrete failure mode this causes: commit 91a359b standardized
code/config on `RedHatAI/gemma-4-31B-it-FP8-Dynamic`, but `helm/values.yaml`
kept pointing at `gpt-oss-120b` and `gpt-5.2-pro`. The Cockpit's builder
dropdown renders from the stale list; picking the new Gemma model routes to
`api.openai.com` (because `LLM_BASE_URL=""` and no endpoint knows where Gemma
lives) and OpenAI returns `invalid model ID`. Documented in
`docs/done/helm_deployment.md`.

## Scope

**In scope**
- Extend the existing `user_llm_endpoints` / `user_llm_endpoint_models` tables
  with a nullable `user_id` so rows can be *system-scoped* (visible to all
  users) or *user-scoped* (today's behavior).
- New `system_api_keys` table mirroring `user_api_keys` for provider-level
  keys that aren't tied to a specific user.
- A bundled model-metadata catalog (`config/models.yaml`) becomes a *code-shaped*
  reference (families, display names, context windows, reasoning levels) —
  no provider URLs, no enablement flags. The DB is the provider list.
- New `helm.llm.seed` value block. On fresh install, a one-shot seed job
  inserts rows into `system_api_keys` and `user_llm_endpoints[_models]`.
  Existing rows are never overwritten or deleted by the seed.
- Admin REST surface under `/api/admin/providers/*` for CRUD on system-scoped
  endpoints, models, and keys. Gated by a Keycloak role (`srw-admin`).
- Encryption at rest for API keys: the app encrypts on write with a key
  derived from an existing ESO/Vault secret, matching how other at-rest secrets
  are handled. Applies equally to `user_api_keys`, `system_api_keys`, and
  endpoint `api_key` columns.
- Retire `BUILDER_MODEL`, `BROWSER_LLM_MODEL`, `CITATION_LLM_MODEL`,
  `LLM_BASE_URL`, `BROWSER_LLM_BASE_URL` as runtime config. They become seed
  inputs only, and the orchestrator reads the resolved values from the DB.
- Cockpit catalog sourcing: `GET /api/models` becomes authoritative; the
  `window.env.models/builderModels/modelPresets` fallback is removed from the
  ConfigMap template.

**Out of scope (v1)**
- Reconciliation (helm-as-source-of-truth). Seeding is strictly one-shot per
  table. Operators who need GitOps control of keys do it at the secret layer
  (ESO → seed) and accept that post-seed edits require a DB wipe of the
  seeded row. A future `reconcile: true` flag can be added without schema
  changes.
- Per-project provider overrides. Scope stays user and system for now.
- UI for rotating encryption keys. Key rotation is an operator procedure
  against the DB directly.
- Backwards-compatible handling of pre-feature data. App is pre-production;
  existing dev rows get wiped on upgrade.

## Architecture

### Storage

Two schema changes, both backward compatible.

```sql
-- 1. Make user_llm_endpoints.user_id nullable; NULL means system-scoped.
ALTER TABLE user_llm_endpoints ALTER COLUMN user_id DROP NOT NULL;

-- Existing unique constraint was (user_id, label). Replace with a partial
-- index so user rows stay unique-per-user and system rows unique globally.
ALTER TABLE user_llm_endpoints DROP CONSTRAINT uq_user_llm_endpoint_label;
CREATE UNIQUE INDEX uq_user_llm_endpoint_label_user
    ON user_llm_endpoints(user_id, label) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX uq_user_llm_endpoint_label_system
    ON user_llm_endpoints(label) WHERE user_id IS NULL;

-- 2. System-scoped provider keys (mirror of user_api_keys).
CREATE TABLE system_api_keys (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provider VARCHAR(50) NOT NULL UNIQUE,
    api_key TEXT NOT NULL,          -- encrypted at rest
    key_prefix VARCHAR(12) NOT NULL,
    label TEXT,
    seeded_from TEXT,               -- e.g. "helm:llm.seed.apiKeys[openai]"
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT valid_system_api_key_provider CHECK (provider IN (
        'openai', 'anthropic', 'google', 'groq', 'openrouter', 'tavily', 'vision'
    ))
);
```

`seeded_from` is a breadcrumb: it marks rows the seed job created so we can
offer an `--allow-reseed` path later (wipe seeded rows, leave UI-created ones).
It is not used for runtime decisions.

Why extend `user_llm_endpoints` rather than a new `system_llm_endpoints` table:
the schema, API shape, dispatcher injection, model registry, and encryption
path are all identical. Forking the table doubles the code surface for a
nullable field.

### Resolution order

`resolve_model(model_id, user_id)` in `src/core/model_registry.py` already
exists (see `docs/features/custom_llm_endpoints.md`). It extends to consult
system endpoints too:

1. User's `user_llm_endpoints` rows (unchanged)
2. System endpoints (`user_id IS NULL`) — new
3. Built-in providers (OpenAI, Anthropic, …) keyed by `models.yaml` metadata

API key resolution mirrors it:

1. `user_api_keys[provider]`
2. Project keys (if job has project)
3. `system_api_keys[provider]` — replaces env-var fallback
4. Env var — kept as a last-resort escape hatch, logged with a deprecation
   warning. Removed entirely after one release cycle.

### Seed pipeline

Helm renders a `srw-llm-seed` ConfigMap + Secret and a short-lived
`Job` (post-install, post-upgrade, `helm.sh/hook-weight: "10"`, runs after the
orchestrator schema init job). The job runs
`python -m orchestrator.seed.llm_config` with the rendered payload mounted at
`/seed/llm.yaml`. Idempotent semantics:

- For each `systemApiKeys[provider]`: `INSERT … ON CONFLICT (provider) DO
  NOTHING`. Logs "seeded" vs "skipped (exists)."
- For each `systemEndpoints[label]`: insert endpoint only if no row with
  `user_id IS NULL AND label = ?` exists. If it exists, leave the endpoint row
  untouched but still attempt to insert any missing models from the seed
  (`ON CONFLICT (endpoint_id, model_id) DO NOTHING`).
- Exit 0 on all-idempotent success. Exit non-zero on DB errors only, so helm
  upgrade doesn't fail when the job is re-run against an already-seeded stack.

Encryption: the seed job reads the encryption key from the same ESO-managed
secret the orchestrator uses. Plaintext keys exist briefly in the seed
ConfigMap/Secret and in memory during the job — this is acceptable because
ESO already holds the plaintext one level up, and the seed ConfigMap can be
deleted after the job completes (a `post-delete` hook can clean it).

### Helm value shape

Seed API keys are **never plaintext in `values.yaml`** — every key is a
reference to an existing `Secret` in the namespace. In ESO mode the chart
expects Vault to populate that Secret; in non-ESO mode the operator creates it
by hand (`kubectl create secret generic srw-llm-seed …`). The seed Job mounts
the Secret as files or env and the Python reads at runtime.

```yaml
llm:
  # Keep for image-default fallback; empty is valid.
  defaultModel: ""       # e.g. "RedHatAI/gemma-4-31B-it-FP8-Dynamic"

  seed:
    # Pure opt-in. Missing/empty means a clean stack with zero LLMs.
    enabled: false

    # Provider keys — secret references only. Empty map means no system keys
    # are seeded and users bring their own via Settings → API Keys.
    systemApiKeys:
      # openai:    { secretName: "srw-llm-seed", key: "openai_api_key" }
      # anthropic: { secretName: "srw-llm-seed", key: "anthropic_api_key" }

    # Custom OpenAI-compatible endpoints (vLLM, Ollama, local gateways).
    systemEndpoints:
      # - label: "Local Gemma"
      #   baseUrl: "http://vllm.ai.svc.cluster.local:8000/v1"
      #   apiKeyRef:                       # optional; omit for keyless endpoints
      #     secretName: "srw-llm-seed"
      #     key: "gemma_api_key"
      #   models:
      #     - id: "RedHatAI/gemma-4-31B-it-FP8-Dynamic"
      #       displayName: "Gemma 4 31B"
      #       family: "gemma"
      #       contextWindow: 128000
```

`cockpit.models`, `cockpit.builderModels`, `cockpit.modelPresets` are **deleted**
from `helm/values.yaml`. The `env.js` ConfigMap template stops rendering them;
Cockpit reads `GET /api/models` on boot. When the response is empty, a
persistent top banner shows: *"No LLMs configured yet — configure a provider
in Settings → API Keys (or Admin → Providers) to use the app."* Model pickers
in builder / job-create / agent-settings render disabled with the same CTA
inline. No hard block; everything else in the app stays reachable.

`llm.browserModel`, `llm.builderModel`, `llm.baseUrl`, `llm.citation.*`
collapse to optional seed presets only. The orchestrator no longer reads them
at runtime; it queries the DB.

### Admin API

New endpoints, Keycloak-role-gated:

| Method | Path | Purpose |
|--------|------|---------|
| `GET`    | `/api/admin/providers/keys`                            | List system API keys (prefix only) |
| `PUT`    | `/api/admin/providers/keys/{provider}`                 | Set/rotate system key |
| `DELETE` | `/api/admin/providers/keys/{provider}`                 | Remove system key |
| `GET`    | `/api/admin/providers/endpoints`                       | List system endpoints + models |
| `POST`   | `/api/admin/providers/endpoints`                       | Create system endpoint |
| `PATCH`  | `/api/admin/providers/endpoints/{id}`                  | Update endpoint |
| `DELETE` | `/api/admin/providers/endpoints/{id}`                  | Delete endpoint + cascade models |
| `POST`   | `/api/admin/providers/endpoints/{id}/models`           | Add model to endpoint |
| `PATCH`  | `/api/admin/providers/endpoints/{id}/models/{mid}`     | Update model |
| `DELETE` | `/api/admin/providers/endpoints/{id}/models/{mid}`     | Remove model |
| `POST`   | `/api/admin/providers/endpoints/{id}/test`             | Probe endpoint |

Shapes reuse the `user_llm_endpoints` DTOs with `user_id` omitted in responses.

### Cockpit surface

- New **Admin → Providers** page under the existing settings shell, only
  visible to users with the `srw-admin` Keycloak role. Sections:
  *Provider Keys* (OpenAI, Anthropic, …), *Custom Endpoints* (system-wide
  vLLM/Ollama), each with add/edit/delete and a test button.
- Existing user **Settings → API Keys** and **Settings → Custom LLMs** pages
  remain unchanged — they continue to write to `user_api_keys` /
  `user_llm_endpoints` with a real `user_id`.
- Model pickers (builder, job create, agent settings advanced panel) drop
  their `environment.ts` fallbacks. Empty catalog renders the empty-state
  admin CTA.

### Encryption

Introduce `orchestrator/security/crypto.py` with `encrypt(plaintext: str) → str`
and `decrypt(ciphertext: str) → str`. AES-GCM with a 32-byte key pulled from
the Secret referenced by `secrets.existingSecret` (or the chart-managed
`srw-secrets` when `secrets.create: true`) under a field named
`app_encryption_key`. Ciphertext format: `v1:<nonce-b64>:<ct-b64>`, prefixed
so a future rotation can coexist with existing ciphertexts.

Applied uniformly on write/read to:
- `user_api_keys.api_key`
- `system_api_keys.api_key`
- `user_llm_endpoints.api_key`

`key_prefix` is computed from plaintext pre-encryption and stored unencrypted
(12 chars is not enough to reconstruct a key).

#### Bootstrap modes

Key provisioning follows the existing `externalSecrets.enabled` switch — the
chart never straddles the two modes, which keeps ESO and chart-managed secrets
from racing each other.

- **`externalSecrets.enabled: true` (ESO / Vault)** — the chart expects
  `app_encryption_key` to exist in the ESO-synced Secret. It never generates.
  A preflight check in the orchestrator init container fails fast with a clear
  message ("`app_encryption_key` missing from secret `<name>`; add it to Vault
  path `<path>`") if the field is absent. Operator owns key lifecycle and DR.
- **`externalSecrets.enabled: false` (chart-managed)** — the chart templates
  use `lookup "v1" "Secret" .Release.Namespace <name>` to read the existing
  Secret; if it exists, the current `app_encryption_key` value is preserved;
  if it doesn't, a fresh 32-byte key is generated with `randAlphaNum 32 | b64enc`
  and written into the chart-managed Secret. Upgrades always preserve the
  existing value.

After install, `NOTES.txt` reminds the operator that the encryption key exists
and must not be lost:

> **Encryption key**: API keys and LLM endpoint credentials are encrypted at rest
> using `app_encryption_key` in Secret `<name>`. **If this key is lost, all
> stored credentials become unrecoverable.** Back it up:
>
> `kubectl -n <ns> get secret <name> -o jsonpath='{.data.app_encryption_key}' | base64 -d`

NOTES prints the *command*, not the value — the value should not land in
install-time shell history, CI logs, or `helm history` output.

### Migration

The app is pre-production — no stored credentials to preserve. Rollout is
just:

1. Schema delta applied by the orchestrator's idempotent init (no migration
   dir — matches the existing pattern per CLAUDE.md). Any existing dev data
   in `user_api_keys` / `user_llm_endpoints` is wiped during the same upgrade
   since it was stored plaintext and the new code only accepts `v1:`-prefixed
   ciphertext on read.
2. `helm/values.yaml` cleanup: delete `cockpit.models`, `cockpit.builderModels`,
   `cockpit.modelPresets`, and the `llm.*Model` / `llm.baseUrl` /
   `llm.browserBaseUrl` fields. Update the ConfigMap template to stop
   rendering them. The env vars they produced (`BUILDER_MODEL`,
   `BROWSER_LLM_MODEL`, `CITATION_LLM_MODEL`, `LLM_BASE_URL`,
   `BROWSER_LLM_BASE_URL`) are removed from the orchestrator's runtime path
   in the same PR.
3. Operator re-configures providers post-upgrade via Admin → Providers, or
   seeds them via `helm.llm.seed` on the next chart apply.

## Decisions

- **Encryption key bootstrap**: dual-mode, gated by the existing
  `externalSecrets.enabled` switch. ESO mode: chart expects the key in Vault,
  fails fast if missing. Non-ESO mode: chart generates on first install via
  `lookup` + `randAlphaNum 32`, preserves on upgrade. NOTES.txt prints the
  read-it-yourself command, never the value.
- **Seed-only, no re-apply**: the seed Job inserts on empty and skips on
  conflict. No `--force-reseed` flag. All post-install changes happen through
  the Admin UI (system rows) or user Settings (user rows).
- **Empty-catalog UX**: soft, not hard. A persistent top banner and disabled
  model pickers with an inline CTA. The rest of the app stays reachable.
- **Seed API keys**: secret references only. No plaintext in `values.yaml`.
  Operators create the referenced Secret via ESO (prod) or by hand (dev).
- **Catalog metadata**: `config/models.yaml` stays as a bundled code-shaped
  reference for families, display names, context windows, reasoning windows.
  DB only holds provider rows (keys, endpoints, model IDs). Revisit later.
- **Empty-state banner is role-aware**: admins see *"Configure a provider in
  Admin → Providers"*; regular users see *"Add an API key in Settings → API
  Keys"*. Role is read from the Keycloak token already in the request.
