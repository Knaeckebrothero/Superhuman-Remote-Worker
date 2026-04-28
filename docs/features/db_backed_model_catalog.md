# DB-backed Model Catalog (Admin-curated, Provider-anchored)

A central `models` table, admin-managed via the Cockpit, becomes the **single curated list of LLMs available to the application**. Providers (system API keys + custom endpoints) supply the *transport*; the catalog supplies the *offering*. Builders, sessions, job-config dropdowns, and the auxiliary/embedding resolvers all read from this catalog — no more hardcoded `config/models.yaml` lists baked into the image.

This is the natural completion of the keys+endpoints work (`db_backed_llm_config.md`, `custom_llm_endpoints.md`). After those landed, an admin can add `anthropic` and an `openrouter` key, but the only Anthropic models the UI offers are the four hardcoded entries in `models.yaml`. Adding `claude-opus-4-7` requires a code change. This feature closes that gap.

## Problem

After the DB-backed key + endpoint work, three things still live in YAML and force a code change to evolve:

- **Built-in model lists** — `config/models.yaml` enumerates per-provider models with their families and display names. To offer a new vendor model (Opus 4.7, GPT-5.2, a new Gemma variant) the YAML has to be edited and the image rebuilt. Operators can configure keys but not models.
- **Per-endpoint model lists** — `user_llm_endpoint_models` (custom endpoints) is the right shape but lives only on user/system endpoint rows. Built-in providers have no equivalent storage, so the admin UI for endpoints can grow models freely while the admin UI for system providers cannot.
- **What the UI shows** — model pickers in builder, session create, job advanced-config, and auxiliary/embedding defaults all derive their lists from `models.yaml` groups (`builder_models`, `worker_models`, `auxiliary_models`). The admin has no way to hide a model that's wrong for their stack, no way to surface a model the org standardized on but isn't in YAML, and no way to express "this Anthropic key exists but only Sonnet is approved for use here."

The drift this causes is the same shape as the keys/endpoints drift before: helm/YAML says one thing, runtime says another, and the empty middle is "what the admin actually wants offered." A central `models` table — admin-curated, provider-anchored — collapses all three into one editable surface.

## Scope

**In scope**

- New `models` table: each row is a curated offering (provider reference + model name + role + family + admin metadata). Rows are admin-managed, not user-managed.
- Provider-anchored design: each row references either a `system_api_keys` provider (`provider_kind='system'`, `provider_ref='anthropic'`) or a `user_llm_endpoints` row (`provider_kind='endpoint'`, `provider_ref=<uuid>`). No standalone models — every offering must trace back to a transport.
- Admin REST surface under `/api/admin/providers/models` for CRUD + enable/disable.
- Cockpit **Admin → Models** page (sibling to Admin → Providers/Endpoints), form-driven like the existing endpoint editor.
- Model resolution refactor: `src/core/model_registry.py:resolve_model()` queries the `models` table first; falls back to the built-in YAML registry only for backward-compat during the transition window.
- Catalog surface (`GET /api/models/available`, builder picker, session/job dropdowns, auxiliary/embedding default selectors): only emit rows from the `models` table where `enabled = true`. Built-in YAML stops being read once the table is the authoritative source.
- Seed pipeline: first-boot seeder reads `config/models.yaml`'s built-in groups and inserts `provider_kind='system'` rows. Idempotent (insert-only on empty table) — admin edits never clobbered. Same shape as the `SEED_*` keys flow.
- Migration of `user_llm_endpoint_models`: each row becomes a `models` row with `provider_kind='endpoint'`. The endpoint row stops carrying a model list; the endpoint = transport, the catalog = offerings.

**Out of scope (v1)**

- **Settings/prompt matrix absorption** — `config/settings_matrix.yaml` (context limits, inference params) and `config/prompt_matrix.yaml` / `instruction_matrix.yaml` (prompt template files) stay file-keyed by family. The model row carries `family` so future per-row overrides can land here without further schema changes, but this v1 only adds the catalog. See **Future direction** below.
- **Auto-discovery via provider `/models` API** — admin types model IDs explicitly. A "fetch from provider" button is a phase-2 nice-to-have (especially for OpenRouter's huge catalog).
- **Per-user / per-project model curation** — the catalog is workspace-global. Users see what the admin has enabled. Project-level overrides can layer on later.
- **User-added models** — feature is removed in this work. The legacy *Settings → Custom LLMs* "add model to endpoint" form goes away; users no longer add their own models. Catalog is admin-curated, full stop. (Self-managed endpoints have seen ~zero use; not worth the dual-path complexity.)
- **Model deprecation/sunset workflow** — `enabled = false` is the only off-switch in v1. Tagging with deprecation dates, migration hints, etc. lives in a follow-up.

## Architecture

### Storage

One new table, plus a soft retirement of `user_llm_endpoint_models` (kept for rollout safety, drained after migration).

```sql
CREATE TABLE models (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),

    -- Provider anchor: every model traces back to a transport.
    provider_kind TEXT NOT NULL CHECK (provider_kind IN ('system', 'endpoint')),
    provider_ref  TEXT NOT NULL,
        -- 'system' → matches system_api_keys.provider (e.g. 'anthropic')
        -- 'endpoint' → matches user_llm_endpoints.id (UUID as text)

    -- Identity sent to the provider verbatim (after provider-specific stripping).
    model_id TEXT NOT NULL,                  -- e.g. 'claude-opus-4-7', 'openrouter/anthropic/claude-3.5-sonnet'
    display_label TEXT NOT NULL,             -- e.g. 'Claude Opus 4.7'

    -- Catalog metadata.
    role TEXT NOT NULL CHECK (role IN ('chat', 'auxiliary', 'embedding', 'vision')),
    family TEXT NOT NULL,                    -- prompt-matrix key: 'claude-opus', 'gpt-5', 'gemma', ...
    context_window INT,                      -- optional override; settings_matrix default if NULL
    reasoning_level TEXT,                    -- optional default
    params_json JSONB,                       -- optional inference param overrides (temperature, etc.)

    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    seeded_from TEXT,                        -- breadcrumb: 'helm:llm.seed' or 'config/models.yaml'
    notes TEXT,                              -- admin freeform
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT uq_model_provider UNIQUE (provider_kind, provider_ref, model_id, role)
);

CREATE INDEX idx_models_role_enabled ON models(role) WHERE enabled = TRUE;
CREATE INDEX idx_models_provider ON models(provider_kind, provider_ref);
```

`provider_ref` is a discriminated string rather than two nullable FKs because PostgreSQL can't enforce a check across two heterogeneous reference types cleanly; the trade is "validate at write time in the API layer" instead. The unique constraint on `(provider_kind, provider_ref, model_id, role)` allows the same model to appear under multiple providers (e.g. `claude-opus-4-7` via `anthropic` direct *and* via an `openrouter` gateway as `openrouter/anthropic/claude-opus-4-7`) — they're different catalog entries with different routing.

`role` distinguishes catalogue placement. A single physical model serving multiple roles (e.g. Opus for both chat and vision) gets one row per role — duplicating identity is fine and keeps per-role enable/disable trivial. Builder/session pickers query `role = 'chat'`. Auxiliary defaults query `role = 'auxiliary'`. Embedding/vision similarly. The `role` enum is locked at the schema level (CHECK constraint) — adding a new role requires code changes in the consuming call sites anyway, so a typo-tolerant open string would only hide bugs.

**JSONB params null vs zero**: `params_json` and the optional override columns (`context_window`, `reasoning_level`) must distinguish "absent" (use default) from explicit zero/false. Validate at write time — LiteLLM's `model_info` migration silently dropped explicit `input_cost_per_token=0` because the migration treated 0 as null ([LiteLLM #14661](https://github.com/BerriAI/litellm/issues/14661)). The accessor returns `None` only when the column is `NULL`; `0` and `false` round-trip as themselves.

`family` is required (not nullable) — without it the settings/prompt matrix lookups fail open to defaults that may be wrong. The admin form pre-fills a best-guess family from the model ID and the admin confirms before save. A future enum-validated dropdown sourced from `settings_matrix.yaml` keys can tighten this.

`enabled = false` is the soft delete. Hard `DELETE` is allowed but the API warns when the row is referenced by a `default_llm_models` pointer or by an in-flight job's `config_override`.

### Resolution flow

`src/core/model_registry.py` already exists with a `resolve_model(model_id, user_id) → ModelMeta` function (per `custom_llm_endpoints.md`). It currently checks: user endpoints → system endpoints → built-in YAML registry. This feature inserts the DB catalog ahead of YAML:

1. **Custom user endpoint match** — unchanged. User's `user_llm_endpoints` + `user_llm_endpoint_models` join wins for personal endpoints.
2. **DB catalog match** — query `models` for `(model_id, enabled=true)` ordered by provider preference (system rows before endpoint rows when both exist for the same `model_id`). Resolve `provider_ref` to a transport:
   - `provider_kind='system'`: look up `system_api_keys[provider_ref]` → returns API key + provider's default base URL
   - `provider_kind='endpoint'`: look up `user_llm_endpoints[provider_ref]` → returns inline base_url + api_key
3. **Built-in YAML fallback** — kept during the transition window for any model ID that was previously hardcoded but hasn't been seeded into the catalog yet. Logged at **WARN** level so we can confirm the catalog covers all live traffic before deletion. Removed once the catalog is authoritative (one release cycle after v1 ships).
4. **Miss** — `UnknownModelError(model_id)`. Surfaces a clear error to the dispatcher; orchestrator returns 400 with a message naming the model and pointing the admin at *Admin → Models*.

`ModelMeta.origin` today is `'custom' | 'system' | 'builtin'` (`src/core/model_registry.py:50-64`). This feature adds `'catalog'` as a fourth value. The dispatcher already branches on `origin in ("custom", "system")` to do endpoint-inline credential injection vs provider-key resolution (`orchestrator/main.py:843-857, 1593-1600, 15259-15270`). `'catalog'` rows resolve their transport at lookup time:

- `provider_kind='endpoint'` → catalog row inherits the endpoint's `base_url` + `api_key`; dispatcher injects identically to `origin='system'`.
- `provider_kind='system'` → catalog row inherits the provider's default base URL + system API key; dispatcher injects identically to `origin='builtin'` (resolves the key from `system_api_keys[provider_ref]`).

Net: extend the dispatcher branch from `origin in ("custom", "system")` to `origin in ("custom", "system", "catalog")` for the endpoint-inline path, and let the existing `else` arm handle the `provider_kind='system'` catalog rows via the same provider-key lookup.

### Catalog surfacing

The endpoints that today return available models — `GET /api/models/available`, builder model list, session/job-create dropdowns, auxiliary/embedding default selectors — pivot to query the `models` table:

```json
{
  "groups": [
    {
      "name": "Anthropic",
      "provider_kind": "system",
      "provider_ref": "anthropic",
      "models": [
        { "id": "claude-opus-4-7", "display_label": "Claude Opus 4.7", "role": "chat", "family": "claude-opus" },
        { "id": "claude-sonnet-4-6", "display_label": "Claude Sonnet 4.6", "role": "chat", "family": "claude-sonnet" }
      ]
    },
    {
      "name": "OpenRouter",
      "provider_kind": "system",
      "provider_ref": "openrouter",
      "models": [
        { "id": "openrouter/google/gemini-2.5-flash", "display_label": "Gemini 2.5 Flash (OR)", "role": "auxiliary", "family": "gemini" }
      ]
    },
    {
      "name": "Local Gemma (vLLM)",
      "provider_kind": "endpoint",
      "provider_ref": "9f3c…",
      "models": [
        { "id": "RedHatAI/gemma-4-31B-it-FP8-Dynamic", "display_label": "Gemma 4 31B", "role": "chat", "family": "gemma" }
      ]
    }
  ]
}
```

Pickers filter client-side by role. The frontend doesn't need to know whether a model is system or endpoint — it sends `model_id` back on session/job create as usual; the dispatcher resolves the route via the catalog.

### Admin API

New endpoints, gated by the existing `srw-admin` Keycloak role (same gate as `/api/admin/providers/*`):

| Method | Path | Purpose |
|--------|------|---------|
| `GET`    | `/api/admin/providers/models`                        | List all catalog rows (filterable by role, provider, enabled) |
| `POST`   | `/api/admin/providers/models`                        | Create catalog row |
| `PATCH`  | `/api/admin/providers/models/{id}`                   | Update row (display_label, family, overrides, enabled) |
| `DELETE` | `/api/admin/providers/models/{id}`                   | Hard-delete row (warns if referenced) |
| `POST`   | `/api/admin/providers/models/{id}/test`              | Probe the route — sends a minimal completion request through the resolved transport, returns latency + error if any |
| `GET`    | `/api/admin/providers/endpoints/{id}/discover`       | Probe a custom endpoint's `GET {base_url}/models` for the add-model picker (custom endpoints only; not exposed for known providers) |
| `GET`    | `/api/admin/families`                                | List family keys (sourced from `settings_matrix.yaml`) for the form dropdown |

The create/update payload validates that `provider_kind + provider_ref` resolves to an existing transport before insert, so the catalog can't reference a stale `system_api_keys` row that was deleted.

### Cockpit UI

New **Admin → Models** page under the existing settings shell, sibling to *Admin → Providers* and *Admin → Endpoints*. Sections:

- **Models list** — table of catalog rows grouped by provider, with columns: display label, model ID, role, family, enabled toggle, edit/delete.
- **Add model** dialog:
  1. Provider dropdown (populated from `system_api_keys` rows + system-scoped `user_llm_endpoints` rows). Disabled providers are greyed out with a "configure key" inline link.
  2. Model ID text field. Free-text for known providers (Anthropic, OpenAI, OpenRouter, Google, Groq) — the admin pastes the model ID from the provider's docs/dashboard. For **custom endpoints only**, a "discover" button next to the field probes `GET {base_url}/models` and offers the listing as a picker. Known providers don't get discovery — model browsing is a documentation problem, not a UX one, and OR's catalog alone is too large to make discovery useful here.
  3. Display label (auto-suggested from the ID, editable).
  4. Role dropdown (`chat`, `auxiliary`, `embedding`, `vision`).
  5. Family dropdown (sourced from `GET /api/admin/families` — see below — which exposes `settings_matrix.yaml` keys; the admin picks one).
  6. Optional: context window override, reasoning level default, inference params JSON.
  7. Test button — runs `POST /api/admin/providers/models/{tmp}/test` against an unsaved row; reports latency + first error.
- **Empty state** — when zero rows exist, a CTA matches the empty-state pattern for endpoints/keys: *"Add a model from a configured provider to make it available in builder, sessions, and jobs."*

The legacy *Settings → Custom LLMs* user-facing surface for adding models to endpoints is **removed** in this work. Users no longer self-add models — the admin curates the entire offering. Users who configured personal endpoints retain their endpoint *transports* (still settable from the user settings page), but the only models that appear in pickers are admin-curated catalog rows.

### Default-model selection

`default_llm_models` is not a dedicated table — it's a set of `system_settings` rows keyed `llm.default_{kind}_model` with `value = {"model": "<model_id>"}`. Accessors live at `orchestrator/database/postgres.py:4338-4370` (`get_default_llm_model(kind)`, `set_default_llm_model(kind, model)`). The pointer behavior described below applies to those rows; the storage shape doesn't need to change.

- The admin pin remains a string `model_id`, not an FK. The resolver looks it up in the catalog at call time.
- If the pinned model has been deleted or `enabled=false`'d, the pointer is treated as absent — no error, no halt.
- When the pointer is absent (never set, deleted, or pointing at a missing/disabled row), the picker and the auto-resolver pick the **first enabled catalog row for that role, sorted by `display_label` alphabetically**. The admin UI shows this fallback explicitly ("currently using *X* — first available since no default is set").
- The Cockpit dropdown for selecting a model in builder/session/job-config always pre-selects the pinned default if present, otherwise the alphabetical first. The user clicking through without touching the dropdown therefore always lands on a working choice.

Keeping the FK soft (string, not `models.id`) preserves the current snapshot semantics: an in-flight job's `config_override` already pins the model name verbatim and survives admin deletes; the default pointer behaves the same way.

### Seed pipeline

First-boot seeder reads `config/models.yaml`'s built-in groups and inserts catalog rows. Mirrors the `SEED_*` keys flow:

- For each entry in `models.yaml` groups (`builder_models`, `worker_models`, `auxiliary_models`, `embedding_models`, `vision_models`), construct a row:
  - `provider_kind = 'system'`
  - `provider_ref = <inferred provider>` (anthropic for `claude-*`, openrouter for `openrouter/*`, openai for `gpt-*`, google for `gemini-*`, etc. — same prefix logic as today)
  - `role = <derived from group>` (`builder_models` and `worker_models` → `chat`, `auxiliary_models` → `auxiliary`, etc.)
  - `family = <yaml entry>.family`
  - `context_window`, `reasoning_level` from the YAML entry if present
  - `seeded_from = 'config/models.yaml'`
- Insert with `ON CONFLICT (provider_kind, provider_ref, model_id, role) DO NOTHING` — admin edits never clobbered on subsequent boots.
- Skip rows whose inferred provider has no `system_api_keys` row yet — they'd be unreachable. Logged at INFO level.

After this seed runs once, `config/models.yaml` becomes legacy. The fallback in `resolve_model()` (step 3 above) keeps it readable for backward compatibility during the transition window; a follow-up PR deletes the file once telemetry confirms the YAML fallback path is no longer hit.

#### OpenRouter convenience seed (replaces `_apply_openrouter_defaults`)

The recently-added `_apply_openrouter_defaults(db)` step in `init.py` hardcodes pins on `default_llm_models` for `auxiliary` (→ `openrouter/google/gemini-2.5-flash`) and `embedding` (→ `openrouter/openai/text-embedding-3-large`). Once the catalog exists, that path inverts: instead of pinning *defaults*, the seeder inserts **catalog rows** for those models when an OpenRouter key is present.

Concretely, the new behavior:

- When `system_api_keys[openrouter]` is present at init time, insert catalog rows for the OpenRouter convenience models with `seeded_from='helm:openrouter-defaults'`:
  - `(provider_kind='system', provider_ref='openrouter', model_id='openrouter/google/gemini-2.5-flash', role='auxiliary', family='gemini', display_label='Gemini 2.5 Flash (OpenRouter)')`
  - `(provider_kind='system', provider_ref='openrouter', model_id='openrouter/openai/text-embedding-3-large', role='embedding', family='openai-embedding', display_label='text-embedding-3-large (OpenRouter)')`
- Same idempotent insert (`ON CONFLICT … DO NOTHING`) — admin edits/disables survive subsequent boots.
- **No `default_llm_models` writes**. The default-selection flow described above (admin pin → fallback to first-enabled-alphabetical) handles the "what gets used when nothing else is configured" question on its own.

The existing `_apply_openrouter_defaults` and its tests (`tests/test_init_seed_llm_keys.py::TestApplyOpenrouterDefaults`) are reworked rather than retained — the function name stays for continuity but its body becomes catalog-row insertion, and the test class adapts to assert "two new catalog rows exist" instead of "two default-model pins were set." Same intent (an OpenRouter key alone is enough to have a working stack), different mechanism.

### Retirement of `user_llm_endpoint_models`

Users no longer add their own models. The migration is therefore a *removal*, not a copy:

1. **Drop the user-side write path** — the user **Settings → Custom LLMs** "add model to endpoint" form is removed from the Cockpit. Users can still create/edit/delete their own endpoint *transports* (`user_llm_endpoints`), but the model list under each endpoint is no longer user-editable.
2. **Promote system endpoint models to the catalog** — for every row in `user_llm_endpoint_models` whose endpoint has `user_id IS NULL` (system-scoped endpoint), insert a `models` row with `provider_kind='endpoint'`, `provider_ref=endpoint_id`, `role='chat'`. User-scoped endpoint model rows are dropped without migration — they were never used in practice and the dual-path complexity of mixing scopes in one table isn't worth carrying forward.
3. **Read path flip** — `resolve_model()` for endpoint-served models reads from `models` instead of `user_llm_endpoint_models`.
4. **Drop the table** in the same PR — once the read/write paths are flipped and the system-scoped rows have been promoted, `user_llm_endpoint_models` is dropped. The pre-production status of the app means we're not preserving stored user data.

## Future direction: matrix absorption

The medium-term arc, captured here so future work doesn't have to re-derive it: the `models` row is the right home for everything currently keyed by family in `settings_matrix.yaml`, `prompt_matrix.yaml`, and `instruction_matrix.yaml`. Today those files are the implicit catalog of *known model behaviors*; once the explicit catalog exists in the DB, per-model overrides graduate from "edit the YAML, rebuild the image" to "edit the row in Admin → Models."

Concretely:

- **`settings_matrix.yaml`** (context limits, inference defaults, tool-call budgets per family) → per-model `context_window`, `params_json`, plus a future `tool_budgets_json` column. Family lookup remains the *default*; the row override wins when present.
- **`prompt_matrix.yaml` / `instruction_matrix.yaml`** (prompt and instruction template filenames per family) → per-model `prompt_template_ref`, `instruction_template_ref` columns pointing at **DB-stored prompt rows** (not filesystem paths — pinning to a path would just defer the YAML problem to the prompts directory). Same default-vs-override semantics.

The v1 row already carries `family`, `context_window`, `reasoning_level`, `params_json` — these are the seed for that future. **Adding `claude-opus-4-7` from the Anthropic API is the right place to manage Opus-specific prompts and settings**, and the catalog row is where that management surface lives. The matrix files don't disappear, they become defaults the catalog overrides.

This is intentionally not in v1 scope — the catalog needs to exist and be exercised before per-row override semantics are layered on. The `params_json` column ships in v1 as the staging ground for the matrix migration; per-row override behavior in the resolver lands in the follow-up.

### Prompt absorption — DB-backed, file-seeded

When the prompt-template absorption lands, prompts move into a `prompts` table seeded from `config/prompts/` files on first boot — the same shape as this catalog seeds from `config/models.yaml`. The matrix files (`prompt_matrix.yaml`, `instruction_matrix.yaml`) become seeds for catalog-row default associations. Admin edits via a future *Admin → Prompts* page own the lifecycle from then on. Prompt editing UI itself is out of scope for *this* feature; it gets its own design doc.

## Decisions

- **Admin-only authoring** — users don't add catalog rows or endpoint models. The legacy *Settings → Custom LLMs* model-add form is removed. Users still own their endpoint *transports*, but offerings are admin-curated. Self-managed model lists were ~unused; not worth the dual-path complexity.
- **Row-per-(model, role)** — a model serving multiple roles gets one row per role. Duplication is fine; keeps per-role enable/disable trivial. No `roles[]` column.
- **Provider-anchored, not standalone** — every catalog row references a transport (`system_api_keys` provider OR `user_llm_endpoints` system-scoped row). No "draft" or "placeholder" rows. Validated at insert time.
- **Free-text model IDs (v1)** — admin types the model name. Provider `/models` discovery is deferred. OpenRouter's catalog is too large to enumerate without a search UI; first-class providers (Anthropic, OpenAI) get discovery in phase 2.
- **`enabled = false` over hard-delete** — soft toggle is the primary off-switch; hard delete works but warns when referenced. Avoids breaking in-flight jobs whose `config_override` pinned a model.
- **Family is required, admin-selected, locked enum** — `settings_matrix.yaml` keys exposed via `GET /api/admin/families` to populate the form dropdown. API-driven not hardcoded so adding a family doesn't require a frontend rebuild.
- **Discovery: custom endpoints only** — the "browse models from the provider" button only fires for custom endpoint rows (probes `GET {base_url}/models`). Known providers (Anthropic, OpenAI, OpenRouter, Google, Groq) get a free-text model ID field — admins paste from the provider's docs. Avoids building a paginated search UI for OR's catalog and the dual code paths that come with it.
- **Per-job pin survives `enabled=false`** — when an admin disables a model that an in-flight job has pinned in `config_override`, the job keeps using the pinned model until restart. The snapshot semantics already in place handle this with no extra code; halt-on-disable would be a behavior change for an edge case.
- **`role` is a locked CHECK enum** (`chat | auxiliary | embedding | vision`) — every consumer call site is hardcoded for these four; a new role needs code changes anyway.
- **`default_llm_models` stays string-keyed (not FK to `models.id`)** — soft pointer into the catalog; missing/disabled target falls through to "first-enabled-alphabetical." Matches the snapshot semantics already in place for in-flight jobs' `config_override`.
- **YAML fallback during transition (WARN-logged)** — `models.yaml` stays readable for one release cycle; WARN log on hit so we can confirm coverage before deletion.
- **Multiple catalog rows per physical model** — a model reachable via two providers (Anthropic direct + OpenRouter passthrough) gets two rows. The unique constraint `(provider_kind, provider_ref, model_id, role)` allows this; the admin can `enabled=false` one to prefer the other.
- **`role` is indexed** — driven by query patterns: every picker filters by role.
- **OpenRouter convenience: catalog seed, not default pin** — `_apply_openrouter_defaults` is reworked to insert catalog rows for the OpenRouter auxiliary/embedding convenience models when an OR key is present, instead of writing to `default_llm_models`. The default-selection fallback ("first-enabled-alphabetical") handles which one gets used.

## Migration

The app is pre-production — no stored model catalog to preserve. Rollout is:

1. Schema delta (idempotent `CREATE TABLE … IF NOT EXISTS`) applied by the orchestrator's init.
2. Seed step in `init.py` (mirroring `_seed_llm_keys_from_env`): reads `config/models.yaml`, inserts one row per entry with `seeded_from='config/models.yaml'`. Idempotent.
3. Rework `_apply_openrouter_defaults` to insert catalog rows for the OpenRouter convenience models (auxiliary + embedding) instead of pinning `default_llm_models`. Same idempotent shape.
4. Promote system-scoped `user_llm_endpoint_models` rows to `models` rows; drop user-scoped rows; drop the `user_llm_endpoint_models` table; remove the user-side "add model to endpoint" form from the Cockpit.
5. `resolve_model()` updated to query `models` first, YAML fallback second with WARN log. The dispatcher's resolution branch is unchanged (it consumes `ModelMeta` regardless of origin).
6. Default-model selection: the resolver and pickers fall back to "first-enabled-alphabetical for role" when the `default_llm_models` pointer is absent/dangling.
7. Catalog API endpoints (`GET /api/models/available` and friends) flip to query the `models` table.
8. Cockpit Admin → Models page ships in the same PR.

Steps 5 and 4 are the breakage risks — keep the YAML fallback active with WARN logging until live traffic confirms full catalog coverage, then drop the fallback (and `models.yaml`) in a follow-up.

## Testing

- **Unit (registry)**: `resolve_model()` returns catalog rows ahead of YAML; provider transport correctly resolved for both `system` and `endpoint` kinds; soft-disabled rows don't match; multiple rows for the same `model_id` (different providers) resolve in stable order.
- **Unit (admin API)**: create/update validates that `provider_ref` exists; reject rows whose provider has no `system_api_keys` entry; `enabled=false` toggle round-trips; hard-delete warns when row is referenced by `default_llm_models`.
- **Unit (seed)**: re-running the seed against an already-populated table is a no-op; admin edits to `display_label` survive a re-seed; entries whose provider has no key seeded are skipped with a log line.
- **Integration**: end-to-end — admin adds Opus 4.7 via the API, builder picker shows it, session created with that model dispatches to Anthropic with the expected `model` field. Then admin disables it, picker hides it, in-flight session keeps working.
- **Cockpit (vitest)**: form validation (provider+model+role+family required), empty-state CTA renders with zero rows, soft-disable toggle round-trips, "test" button surfaces latency + error.
- **Manual QA checklist**:
  - Add a model to a system provider, confirm it appears in builder and session create.
  - Add the same model ID to two providers (Anthropic + OpenRouter passthrough), disable one, confirm the other resolves.
  - Delete a `system_api_keys` row that has catalog rows pointing at it — admin API blocks the delete with a clear error citing the catalog rows.
  - Add a model with an explicit `context_window` override smaller than the family default, run a long prompt, confirm the override is honored.

## Open questions

- **Seeding from `models.yaml` vs `helm.llm.seed`** — once the YAML is legacy, do we keep it as the seed source or move the seed payload into the `helm.llm.seed` block (same shape as `systemApiKeys`)? The latter is more consistent but means operators edit helm values to add a model when they could be using the Admin UI. Lean: keep `models.yaml` as the seed source for the project's curated defaults; let admins layer their own additions via the UI.
