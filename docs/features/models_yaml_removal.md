# Removing `models.yaml` and the Role-Completeness Gate

Completes the migration started in `db_backed_llm_config.md` and `db_backed_model_catalog.md`. The v1 catalog work introduced a `models` table and an Admin → Models UI but kept `config/models.yaml` as the seed source and a WARN-logged fallback in `resolve_model()`. This doc removes that fallback, deletes the YAML, kills `LLM_BASE_URL`-driven routing, renames the `role` field to `capability` for vocabulary consistency with v1's helm seed, and adds a hard capability-completeness gate at boot so the silent "fall through to api.openai.com with `not-needed`" path becomes structurally impossible.

## Problem

Three concrete failure modes still exist after v1:

- **`config/models.yaml` is two sources of truth.** v1 reads it at first boot to populate the `models` table, then keeps `_load_builtin_catalog()` available as a fallback when `resolve_model()` misses. The fallback has no `base_url` or `api_key` for self-hosted models, so a missing catalog row silently falls through to `api.openai.com` with the literal string `"not-needed"` (`src/core/loader.py:1751`).
- **`LLM_BASE_URL`-driven `Local`-group inheritance** (`src/core/model_registry.py:131`) is still wired in. Removing the env var — which is what `.env.example` now tells operators to do — leaves `Local`-group models with `base_url=None`, reproducing the same 401-against-OpenAI failure. This is the bug captured in `docs/llm_routing_issues.md`; the env-var path is the actual cause and removing it is the actual fix.
- **Two vocabularies for the same concept**: v1's `models.role` column and the helm seed's `systemEndpoints[].models[].capability` field describe the same thing — which slot a model fills (chat, embedding, …). `capability` was added first (commit `57327e8`, 2026-04-24) and is the more honest name on a model row; `role` came in a day later when the catalog table landed. The seeder currently maps one to the other, which is the kind of low-grade tax that compounds.

A softer fourth issue: the cockpit's "no provider configured" onboarding gate covers the API-key half of the story but not the model-binding half. A user can configure an Anthropic key, skip Admin → Models entirely, create a session, and hit a 503/401 on first turn because no chat-capability catalog row exists.

## Scope

**In**

- Delete `config/models.yaml`. Remove the YAML fallback path from `src/core/model_registry.py`. Remove `_load_builtin_catalog()` and `_builtin_registry` entirely.
- Delete `LLM_BASE_URL` env-var inheritance in the model registry (`model_registry.py:118-172`). Hard-fail orchestrator startup if `LLM_BASE_URL` is set, with a migration message pointing at the helm seed format.
- Move catalog seed source from `config/models.yaml` to `helm.llm.seed.systemModels[]` in `helm/values.yaml`, alongside the `helm.llm.seed.systemApiKeys[]` block already established by `db_backed_llm_config.md`. First-boot init reads the helm seed and inserts catalog rows; if the helm block is empty, the table starts empty and the capability-completeness gate (below) blocks the cockpit until the admin onboards via UI.
- Rename the `models.role` column → `models.capability`. Update the CHECK enum, every API request/response payload that names the field, every Python identifier (`ModelMeta.role`, `resolve_default_for_role`, `default_llm_models[role]`, the `_YAML_GROUP_ROLE` constant in `init.py:1005`, etc.) to use `capability`. The helm seed's existing `systemEndpoints[].models[].capability` becomes the authoritative spelling; the seeder's `capability` → `role` mapping is removed.
- Add a capability-completeness gate: `GET /api/system/readiness` returns `{ ready, missing_providers, missing_capabilities }`. The cockpit's existing "no provider configured" onboarding screen expands to also block on missing capabilities. Required: `chat`, `embedding`, `auxiliary`. Optional: `vision`, `whisper`, `tts` (vision falls back to chat via a system-settings flag; audio features disable when missing).
- Auto-discovery on system-key save: when an admin creates or rotates a `system_api_keys` row for a known provider, an async probe fetches a candidate list (provider `/models` endpoint where available, family registry where not), pre-populates a confirmation dialog with checkboxes + auto-detected family + suggested capability, and inserts only the rows the admin confirms.
- Family taxonomy: dropdown sourced from `prompt_matrix.yaml` / `settings_matrix.yaml` keys (already in v1). New: a regex/prefix matcher pre-fills the default selection. `GET /api/admin/families/detect?model_id=…` returns the matcher's choice; the admin can override.
- Add `whisper` and `tts` to the `models.capability` CHECK enum so future audio routes have a home, even though no call site queries them in this work.

**Out**

- DB migration of `model_config_matrix.yaml` (the unified successor to `settings_matrix.yaml` / `prompt_matrix.yaml` / `instruction_matrix.yaml`). Stays file-based. Captured as future work in `db_backed_model_catalog.md`.
- Per-user model curation. Same as v1 — workspace-global.
- Custom-endpoint discovery upgrades. v1's `GET {base_url}/models` probe is unchanged.
- Real-time model-list refresh from providers (re-discovery on a schedule). Discovery fires on key save and on explicit "rediscover" click; cached for 24h.
- **DB-backed replacement for the `presets:` block in `models.yaml`** (the strategic+tactical quick-select bundles like `"Opus + Sonnet"`, `"Codex + Spark"`). Dropped without replacement when `models.yaml` is deleted in step 7 — these have seen ~no real use, and a job-create UX that relies on the user picking strategic and tactical models individually is acceptable. If demand surfaces later, the catalog already carries enough metadata to build presets; not worth pre-building.

## Architecture

### Removing `models.yaml` and `_load_builtin_catalog`

After v1, `config/models.yaml` is read in three places:

| Caller | Today | After this work |
|--------|-------|-----------------|
| `_load_builtin_catalog()` in `src/core/model_registry.py:103-174` | Parses YAML at module import, populates `_builtin_registry` for `resolve_builtin()` calls. | Function deleted. The registry is DB-backed only; orchestrator loads it on startup, refreshes on admin mutations via an in-process pub/sub. |
| `_seed_models_from_yaml` in `orchestrator/init.py:940-1031` | Reads YAML, inserts catalog rows. Called from `init.py:291`. | Renamed to `_seed_models_from_helm` and rewritten to consume `llm.seed.systemModels[]` plus the existing `llm.seed.systemEndpoints[].models[]`. |
| `_apply_openrouter_defaults` in `orchestrator/init.py:1104+` | Inserts catalog rows directly when an OR key is present. | Unchanged — already YAML-free. |

The agent process never reads the catalog directly. Today the dispatcher injects `base_url + api_key` into `config_override.llm`; this is extended so `family`, `context_window`, and `params_json` from the catalog row are also injected at dispatch time (relevant call site: `_inject_model_credentials` in `orchestrator/main.py:1627`). The agent stops calling `resolve_builtin()`. `src/core/loader.py:1765-1769`'s `if config.base_url … else: meta = resolve_builtin(...)` block becomes `if config.base_url: …` with a hard-fail on the else path (a missing `base_url` is now a dispatcher bug, not a fallback case). The mirror block in `_create_codex_llm` (`src/core/loader.py:2155-2165`) gets the same treatment.

### Helm seed schema

`helm/values.yaml:506-587` already defines `llm.seed.{enabled, systemApiKeys, systemEndpoints, resources}`. The existing `systemEndpoints[].models[]` block already carries a `capability` field — which becomes the *only* spelling for the concept after the rename below. Endpoint-served models therefore need no schema changes.

What's missing is **provider-direct models** (Anthropic, OpenAI, Google, Groq, OpenRouter — served by a `system_api_keys` row, not a custom endpoint). Today these come from `config/models.yaml`'s `groups:` block via `_seed_models_from_yaml` (`orchestrator/init.py:940`). Replacement: add a `systemModels[]` sibling block so operators specify them in the same place as keys and endpoints.

Delta to `helm/values.yaml`, inserted between `systemEndpoints` (line 577) and `resources` (line 580):

```yaml
    # -- Optional: provider-direct catalog rows (served by a system API key,
    # not a custom endpoint). Mirrors the YAML groups block that today lives
    # in config/models.yaml. Each entry is one (model, capability) catalog row;
    # repeat the model with a different `capability` if it serves multiple slots.
    # `provider` must match an entry in `systemApiKeys` above (or one already
    # in the DB) — entries whose provider has no key seeded are skipped.
    #
    # systemModels:
    #   - provider: "anthropic"
    #     id: "claude-opus-4-7"
    #     displayName: "Claude Opus 4.7"
    #     capability: "chat"      # chat | auxiliary | embedding | vision | whisper | tts
    #     family: "claude-opus"   # optional; family_matcher derives default from `id`
    #     contextWindow: 200000   # optional; settings_matrix default if null
    #     reasoningLevel: null    # optional
    #   - provider: "openai"
    #     id: "text-embedding-3-large"
    #     displayName: "OpenAI Embedding (Large)"
    #     capability: "embedding"
    #     family: "openai-embedding"
    systemModels: []
```

Idempotent insert (`ON CONFLICT (provider_kind, provider_ref, model_id, capability) DO NOTHING`). Admin edits survive subsequent boots. Rendered into the existing `srw-llm-seed-config` ConfigMap (`helm/templates/orchestrator/llm-seed-configmap.yaml`) and consumed by the existing `srw-llm-seed-job` (`helm/templates/orchestrator/llm-seed-job.yaml`).

`systemModels[]` and `systemEndpoints[].models[]` use the same `capability` field name — one vocabulary across the helm seed.

### `role` → `capability` rename

The v1 catalog schema (`db_backed_model_catalog.md`) named the column `role`. The helm seed it integrates with names the same field `capability`, which was added a day earlier (commit `57327e8`, 2026-04-24, vs. the catalog table in `68a1c8b`, 2026-04-25). The seeder bridges the two with a one-line mapping. This work removes that bridge by renaming everywhere to `capability`.

Concrete edits:

- **Schema** (`orchestrator/database/schema.sql`): `ALTER TABLE models RENAME COLUMN role TO capability`. Drop and recreate the CHECK constraint and the index `idx_models_role_enabled` → `idx_models_capability_enabled`. Pre-production app, dev DBs only — no online migration needed.
- **Python identifiers**: `ModelMeta.role` → `ModelMeta.capability` (`src/core/model_registry.py`). `resolve_default_for_role(kind)` → `resolve_default_for_capability(capability)` (`orchestrator/database/postgres.py:4179` and ~5 call sites in `orchestrator/main.py`). `default_llm_models` settings keys (`llm.default_chat_model`, `llm.default_auxiliary_model`, etc.) keep their literal strings — they're already capability-named, no `role` substring. The `_YAML_GROUP_ROLE` constant in `orchestrator/init.py:1005` becomes `_YAML_GROUP_CAPABILITY`.
- **API surfaces**: `/api/admin/providers/models` (request + response field), `/api/system/readiness` (`missing_capabilities`), the catalog list endpoints, `default_llm_models` accessors. JSON field rename happens in the same PR as the column rename.
- **Cockpit**: every `role` field read on model rows in `cockpit/src/app/views/admin/models/admin-models.component.ts` and the picker components becomes `capability`. The TypeScript model in `cockpit/src/app/core/models/api.model.ts` updates in lockstep.
- **Seeder** (`orchestrator/init.py:_seed_models_from_yaml` and successor `_seed_models_from_helm`): the `capability` → `role` mapping line goes away. `systemEndpoints[].models[].capability` is read verbatim into the `models.capability` column.
- **Helm seed values** (`helm/values.yaml`): the new `systemModels[]` block uses `capability` from the start (already shown above). Existing `systemEndpoints[].models[].capability` is unchanged.

This is **step 0** in the migration table — a prerequisite for everything else, since later steps reference the field name in code, API, and helm.

### Unifying the three model matrix files

`config/prompt_matrix.yaml`, `config/settings_matrix.yaml`, and `config/instruction_matrix.yaml` are three family-keyed fall-through tables that describe the same set of model families with different value shapes. The family lists drift apart: settings has `claude-opus`, `gemini`, `deepseek`, and `o-series` that prompts doesn't; prompts has `codex` and `codex-spark` that instructions doesn't. With family taxonomy sourced from one file, families that only appear in another silently disappear from the discovery dropdown despite carrying real custom config.

Replacement: `config/model_config_matrix.yaml`, family-keyed at the top level, with three concern subsections per family (`prompts`, `instructions`, `settings`). Missing concerns or missing keys within a concern fall through to `default`, same semantics as today.

```yaml
default:
  prompts:
    systemprompt: systemprompt.txt
    persona: persona.txt
    strategic: strategic.txt
    tactical: tactical.txt
    summarization: summarization_prompt.txt
    memory_extraction: memory_extraction_prompt.txt
    memory_assembler: memory_assembler_prompt.txt
    curation: curation_prompt.txt
  instructions:
    instructions: instructions.md
    strategic_todos_initial: strategic_todos_initial.yaml
    strategic_todos_transition: strategic_todos_transition.yaml
    strategic_todos_resume: strategic_todos_resume.yaml
    workspace_template: workspace_template.md
    todo_guide: todo_guide.md
  settings:
    temperature: 0.0
    multimodal: false
    parallel_tool_calls: false
    model_max_context_tokens: 128000
    limits:
      model_max_context_tokens: 100000
      context_threshold_tokens: 80000
      summarization_safe_limit: 90000
      summarization_chunk_size: 80000
      message_count_min_tokens: 50000

claude-opus:
  settings:
    temperature: 1.0
    multimodal: true
    parallel_tool_calls: true
    model_max_context_tokens: 1000000
    limits:
      model_max_context_tokens: 200000
      context_threshold_tokens: 150000
      summarization_safe_limit: 180000
      summarization_chunk_size: 120000
      message_count_min_tokens: 100000
  # No prompts: or instructions: — fall through to default

gpt-oss:
  prompts:
    systemprompt: systemprompt_gpt_oss.txt
    # … rest of gpt-oss prompts
  instructions:
    instructions: instructions_gpt_oss.md
    todo_guide: todo_guide_gpt_oss.md
    strategic_todos_transition: strategic_todos_transition_gpt_oss.yaml
  settings:
    temperature: 1.0
    top_p: 1.0
    # … rest of gpt-oss settings
```

Loader changes in `src/core/loader.py` (the three resolvers that today read the three matrix files): repoint each at the same `model_config_matrix.yaml`, read its respective subsection (`families[name].prompts`, `.instructions`, `.settings`), preserve the existing deep-merge fall-through to `default`. The functions stay separate — they're called from different lifecycle points and consume different value shapes — but they share a parsed-once cache of the unified file.

Per-expert overrides keep working. Today an expert can drop a `settings_matrix.yaml` in its config directory; after this, it drops a `model_config_matrix.yaml` with the subset it overrides. Loader's expert→base merge is a one-line change.

This is **chunk 1** in the migration table — slotted before the helm-seed work because the family taxonomy used by chunk 3 (auto-discovery) and chunk 4 (capability gate's family-aware error messages) reads from the unified file.

### `LLM_BASE_URL` removal

The env-var inheritance block at `src/core/model_registry.py:131-145` (the `is_local_group and meta.base_url is None and env_base_url` branch) is deleted along with the `_load_builtin_catalog()` containing it. Operators currently relying on it must:

1. Add an endpoint to the helm seed (`llm.seed.systemEndpoints[]`).
2. Add catalog rows under that endpoint (either `systemEndpoints[].models[]` inline or `systemModels[]` referencing it).

Boot-time check (added to `orchestrator/main.py` startup): if `os.getenv("LLM_BASE_URL")` is set, the orchestrator logs an ERROR and exits with a message naming the helm seed path and the admin-UI alternative. Hard-fail rather than silent ignore — the env var pointing at a real local server but ignored is exactly the kind of half-working state that produces the 401 in `docs/llm_routing_issues.md`.

The `Local` group in the YAML group catalog disappears with the rest of the YAML. Self-hosted models are catalog rows referencing endpoint transports — same shape as any other custom endpoint, no special-case code path.

### Deprecated string-shaped LLM fields in `helm/values.yaml`

`helm/values.yaml:512-517` still carries env-var-shaped settings that pre-date the DB-backed work:

```yaml
llm:
  keyCooldownSeconds: "3600"
  visionModel: "gpt-4o-mini"
  embeddingModel: "qwen3-embedding-8b"
  embeddingBaseUrl: ""
  codexBaseUrl: ""
  builderProvider: "openai"
```

`keyCooldownSeconds` stays — it's a runtime knob, not a routing decision. The other five (`visionModel`, `embeddingModel`, `embeddingBaseUrl`, `codexBaseUrl`, `builderProvider`) are routing/selection decisions that belong in catalog rows + admin defaults pins (`default_llm_models`). They get removed in step 7, alongside `config/models.yaml`. The render targets in `helm/templates/configmap.yaml` and `helm/templates/orchestrator/deployment.yaml` are dropped at the same time. Migration message in the ERROR-and-exit boot check covers anyone with overrides set on these.

### Role-completeness gate

New endpoint `GET /api/system/readiness`:

```json
{
  "ready": false,
  "missing_providers": [],
  "missing_capabilities": ["embedding"],
  "missing_defaults": ["chat"],
  "optional_capability_fallbacks": {
    "vision": "use_chat",
    "whisper": null,
    "tts": null
  }
}
```

`ready = false` iff any of the following is true:
- No provider/endpoint configured at all (existing onboarding gate's logic, retained).
- Any **required** capability (`chat`, `embedding`, `auxiliary`) has zero `enabled = true` catalog rows → reported in `missing_capabilities`.
- Any **required** capability has rows but no admin-pinned default in `default_llm_models` → reported in `missing_defaults`.

The cockpit's onboarding screen expands: a single guarded route (today shows "Configure a provider") becomes a three-step checklist:

1. At least one provider key or endpoint configured (existing).
2. At least one chat, one embedding, and one auxiliary model in the catalog (new).
3. A default pinned for each of chat, embedding, and auxiliary in Admin → Models → Defaults (new).

Each step deep-links to its admin page. The dispatcher hard-fails (HTTP 503) on `POST /api/jobs` and `POST /api/persistent/threads` when the gate is failing, with the specific `missing_capabilities` and `missing_defaults` in the error body. This replaces today's "create the session, dispatch silently fails to OpenAI" flow.

Requiring an explicit default pin (vs. picking alphabetically or by some heuristic) is what closes the embedding-fallback ambiguity: when an admin has both native OpenAI and OpenRouter passthrough rows for `text-embedding-3-large`, the resolver doesn't guess — the admin has already chosen.

The optional-capability fallback flag (`llm.fallback_optional_capabilities_to_chat`, default `true`) is a `system_settings` row. When `true`, missing `vision` resolves to the configured chat model; `whisper`/`tts` simply disable the audio features. When `false`, the three optional capabilities also gate the cockpit. Pragmatic default — operators who want strict capability separation flip the flag.

`auxiliary` is required (not optional + chat-fallback) because the auxiliary LLM runs the memory observer and knowledge curator on a separate task budget; defaulting it to chat means every observer pass competes with the live agent for chat-model tokens, defeats per-capability rate-limiting, and tends to surface as "the agent feels slower" without an obvious cause. Cheaper to require an admin pin.

### Auto-discovery on key save

`POST /api/admin/providers/keys` (existing endpoint) gains a side-effect path. After successful insert/update for a known provider, it kicks off `discover_models(provider, key)` as an async task. The result is staged on the new key row (`discovery_cache_json JSONB`), and the cockpit's key-save success toast offers a "Review N discovered models" CTA that opens a confirmation dialog.

The discovery pipeline is **family-driven, not provider-driven**. The family taxonomy is the top-level keys of `config/model_config_matrix.yaml` minus `default` (e.g. `minimax`, `gpt-oss`, `gemma`, `claude-opus`, …) — a family appears there iff we have custom prompts, instructions, or settings for it. No separate registry file: the unified matrix is the single source of truth for "which families exist."

Discovery enumerates whatever each provider exposes, runs the family matcher (next section) on each model ID, and groups the results into two tiers in the confirmation dialog:

- **Supported** (default checked): models whose detected family is a non-`default` key in `model_config_matrix.yaml`. These get the family-specific prompts and settings.
- **Generic** (default unchecked, behind a "Show models without custom prompts" toggle): models whose family resolves to `default`. They work — the agent uses default prompts — but quality is on the model. Surfaced so an admin who knows what they're doing can opt in.

Provider sources (in `orchestrator/services/discovery.py`):

| Provider | Source |
|----------|--------|
| OpenAI | `GET https://api.openai.com/v1/models`. |
| Google | `GET https://generativelanguage.googleapis.com/v1beta/models?key=…`. |
| Groq | `GET https://api.groq.com/openai/v1/models`. |
| OpenRouter | `GET https://openrouter.ai/api/v1/models`. |
| Anthropic | Skipped — Anthropic doesn't expose an authed `/models` listing with the metadata we need. Admin adds Anthropic models manually via Admin → Models → Add (uses the same family dropdown + regex pre-fill). |

Adding a new family is a single edit to `model_config_matrix.yaml` (plus the prompt/instruction files referenced from it). Discovery picks it up automatically — no `discovery.py` change required, since the family matcher (next section) is the only piece that needs updating, and that's the same edit pattern.

Each discovery candidate carries `{ model_id, detected_family, supported (bool), suggested_capability, suggested_display_label }`. The admin confirmation dialog groups by `supported`, defaults the supported tier to checked, and lets the admin override family/capability per row before "Add selected" inserts catalog rows in a single transaction. After insert, if no default is pinned for the relevant capability yet, the dialog nudges the admin to pin one (the readiness gate will block them otherwise).

Discovery results cached in `system_api_keys.discovery_cache_json` for 24h. Explicit "Rediscover" button invalidates and re-fetches.

### Family auto-detection

`orchestrator/services/family_matcher.py`:

```python
FAMILY_RULES: list[tuple[re.Pattern, str | Callable]] = [
    # OpenRouter prefix: strip and recurse on the trailing segment
    (re.compile(r"^openrouter/(.+)$"), lambda m: detect_family(m.group(1))),

    (re.compile(r"^claude-opus"), "claude-opus"),
    (re.compile(r"^claude-sonnet"), "claude-sonnet"),
    (re.compile(r"^claude-haiku"), "claude-haiku"),

    (re.compile(r"^gpt-5"), "gpt-5"),
    (re.compile(r"^o[1-9](-|$)"), "gpt-5"),
    (re.compile(r"^gpt-4o"), "default"),
    (re.compile(r"^gpt-4"), "default"),

    (re.compile(r"^gemini-"), "gemini"),
    (re.compile(r"^gemma-"), "gemma"),
    (re.compile(r"gpt-oss"), "gpt-oss"),
    (re.compile(r"minimax"), "minimax"),
    (re.compile(r"kimi"), "default"),

    (re.compile(r"text-embedding"), "openai-embedding"),
]

def detect_family(model_id: str) -> str:
    for pattern, family in FAMILY_RULES:
        if m := pattern.search(model_id):
            return family(m) if callable(family) else family
    return "default"
```

`GET /api/admin/families/detect?model_id=…` returns `{ family, source: "matched"|"fallback" }`. The Admin → Models add-model dialog and the discovery confirmation dialog both call this to pre-fill the family dropdown. Admin can override before save. The matcher is intentionally a small ordered list — adding a family is a code change, but it has to be a code change anyway because `model_config_matrix.yaml` needs an entry for it.

The OpenRouter prefix recursion handles `openrouter/anthropic/claude-opus-4-7` → `claude-opus`, `openrouter/openai/text-embedding-3-large` → `openai-embedding`, etc. without duplicating rules.

### `config/experts/` (no changes)

The expert configs are kept. Loader path at `src/core/loader.py:2663` and the directory contents (`scholar/`, `critic/`, `developer/`, `curator/`, `designer/`, `designer-interactive/`) stay as-is. They're useful starting templates for operators who want a worker variant without writing one from scratch. `designer-interactive/` is the live persistent design persona (`$extends: persistent_defaults`).

Note: the worker-mode presets carry stale `llm.model` defaults (most point at `RedHatAI/gemma-4-31B-it-FP8-Dynamic` with no transport). After this work, dispatching against a worker preset still requires a chat-capability catalog row — the readiness gate enforces that. The presets become "shape templates" that need a model added at job-create time via `config_override`. Acceptable cost; the alternative is migrating their model defaults to point at a system default, which is a noisy edit for unused configs.

## Migration

The work lands as a single PR — pre-production app, no operators on prod, no soak window or cliff to manage. The numbered list below is a work-breakdown within that PR, ordered so a reviewer can read the diff top-to-bottom (rename → schema → boot path → routes → frontend → cleanup), not a multi-PR sequence.

| # | Chunk | Files touched |
|---|-------|---------------|
| 0 | **Rename `role` → `capability`** | `orchestrator/database/schema.sql` (`ALTER TABLE models RENAME COLUMN role TO capability` + recreate CHECK + rename index `idx_models_role_enabled` → `idx_models_capability_enabled`). `orchestrator/database/postgres.py` (`resolve_default_for_role` → `resolve_default_for_capability`, accessor parameter names, query SQL). `orchestrator/main.py` (~5 call sites: search for `for_role(`). `src/core/model_registry.py` (`ModelMeta.role` → `capability`). `orchestrator/init.py:1005` (`_YAML_GROUP_ROLE` → `_YAML_GROUP_CAPABILITY`). `cockpit/src/app/core/models/api.model.ts` and the admin-models / picker components. JSON field rename in all `/api/admin/providers/models` payloads + `/api/system/readiness` (`missing_roles` → `missing_capabilities`). |
| 1 | **Unify the three matrix files** | `config/model_config_matrix.yaml` (new — merged content from `prompt_matrix.yaml` + `settings_matrix.yaml` + `instruction_matrix.yaml`, structured as `families[name].{prompts,instructions,settings}`). Delete the three originals. Per-expert overrides exist today in `config/experts/{critic,developer,scholar,designer}/{prompt_matrix.yaml,instruction_matrix.yaml}` — merge each expert's two files into a single `config/experts/<name>/model_config_matrix.yaml` and delete the originals. No per-expert `settings_matrix.yaml` exists today, but the unified shape supports `settings:` per-expert if needed later. `src/core/loader.py` (three resolvers repointed at the unified file, deep-merge fall-through preserved across global → expert → family → default, parsed-once cache shared across the three callers). |
| 2 | **Helm seed source for models** | `helm/values.yaml` (add `systemModels[]` block ~line 578 with `capability` field), `helm/templates/orchestrator/llm-seed-configmap.yaml` (render new block), `orchestrator/init.py` (rename `_seed_models_from_yaml` → `_seed_models_from_helm`, read from ConfigMap). The seeder's `capability` → `role` mapping line goes away (already done in chunk 0). No YAML fallback — empty helm block means empty catalog, the readiness gate (chunk 5) handles the empty-catalog UX. |
| 3 | **Family matcher + detect endpoint** | `orchestrator/services/family_matcher.py` (new — reads family list from `model_config_matrix.yaml`), `orchestrator/main.py` (new route `GET /api/admin/families/detect`), `cockpit/src/app/views/admin/models/admin-models.component.ts` (wire into add-model dialog). |
| 4 | **Auto-discovery on key save** | `orchestrator/services/discovery.py` (new — per-provider `/models` clients for OpenAI/Google/Groq/OpenRouter; Anthropic skipped, admin adds manually), `orchestrator/database/schema.sql` (add `discovery_cache_json` column to `system_api_keys`), `orchestrator/main.py` (`POST /api/admin/providers/keys` side-effect, plus `POST /api/admin/providers/keys/{id}/rediscover`), `cockpit/src/app/views/admin/providers/admin-providers.component.ts` (confirmation dialog with supported/generic tiers). Behind `admin.discovery_enabled` system-settings flag. |
| 5 | **Capability-completeness gate** | `orchestrator/main.py` (new `GET /api/system/readiness`; hard-fail at 503 in `POST /api/jobs` and `POST /api/persistent/threads` when `missing_capabilities` or `missing_defaults` non-empty), `orchestrator/database/postgres.py` (helpers `count_enabled_models_by_capability` and `list_default_pin_capabilities`), `cockpit/src/app/core/services/readiness.service.ts` (new), `cockpit/src/app/layout/onboarding-gate.component.ts` (extend to three-step checklist: provider → model rows → defaults pinned). Defaults pin UI lives in Admin → Models → Defaults (already partially built; extend so each required capability has a required dropdown). System-settings flag `llm.fallback_optional_capabilities_to_chat` (default `true`). |
| 6 | **Drop YAML fallback and `LLM_BASE_URL` inheritance** | `src/core/model_registry.py` (delete `_load_builtin_catalog()`, `_builtin_registry`, the `is_local_group` env-var inheritance block at lines 131-145, and `resolve_builtin()` callers). `src/core/loader.py:1765-1769` and `2155-2165` (replace `else: meta = resolve_builtin(...)` with hard-fail). `orchestrator/main.py` startup hook: ERROR + `sys.exit(1)` if `os.getenv("LLM_BASE_URL")` is set. Catalog miss in the dispatcher returns `UnknownModelError` → HTTP 400 with a message naming the model. |
| 7 | **Delete `config/models.yaml` and deprecated `helm.llm.*` fields** | Delete `config/models.yaml`. Remove `llm.visionModel`, `llm.embeddingModel`, `llm.embeddingBaseUrl`, `llm.codexBaseUrl`, `llm.builderProvider` from `helm/values.yaml:512-517`. Remove their render targets from `helm/templates/configmap.yaml` and `helm/templates/orchestrator/deployment.yaml`. Remove the strategic+tactical preset reader and the cockpit dropdown that consumes it (`presets:` block has no DB-backed successor — see Out of scope). |

## Decisions

- **`role` → `capability` rename**: `capability` was added first in the helm seed; renaming the catalog column + Python identifiers + API field is a one-PR atomic edit while the app is pre-production. After this, the codebase has one word for the concept.
- **Required capabilities: chat + embedding + auxiliary**. Optional: vision/whisper/tts (vision falls back to chat via flag, audio features disable). Auxiliary is required because routing it to chat defeats per-capability rate limits and surfaces as latency without an obvious cause.
- **Default model pin is required per required capability**: the readiness gate refuses to release until the admin has pinned a default for `chat`, `embedding`, and `auxiliary` in Admin → Models → Defaults. Removes any "which row wins when multiple match" ambiguity (e.g. native OpenAI vs OpenRouter passthrough for `text-embedding-3-large`) — the admin chooses, the resolver doesn't guess.
- **No YAML fallback at any point in the migration**: pre-production app, single PR, so there's no soak window to gate on. Carrying two sources of truth — even briefly — is the bug we're fixing; the readiness gate (chunk 4) is what catches an empty catalog post-rename, not a YAML fallback.
- **`LLM_BASE_URL` deleted, not deprecated-with-warning**: the env-var path silently falls back to api.openai.com when the URL becomes empty. Hard-fail at boot if it's set after this lands; the migration message is one paragraph.
- **Helm seed for models, not `models.yaml`**: helm already owns `systemApiKeys` seed; models follow. Operators edit one place.
- **Expert configs kept**: cheap to keep as templates, loader path is small, no runtime impact.
- **Strategic+tactical presets dropped without DB replacement**: the `presets:` block in `models.yaml` (`"Opus + Sonnet"` etc.) has seen ~no real use. Job-create UX falls back to picking strategic and tactical models individually. Catalog has enough metadata to rebuild them later if demand surfaces.
- **Auto-discovery is admin-confirmed, not auto-insert**: provider responses can include preview/experimental models or models the admin doesn't want exposed. Checkbox prevents catalog bloat.
- **Discovery is family-driven, not provider-driven**: the set of "supported" models is whatever has a non-`default` entry in `model_config_matrix.yaml`. Models with no matching family are surfaced as "Generic" (default unchecked) — they work with default prompts; admin opts in. Adding a new family is a `model_config_matrix.yaml` edit + the prompt/instruction files it references, no `discovery.py` change.
- **Family matcher is a regex registry, not ML or fuzzy matching**: families are a closed set defined by `model_config_matrix.yaml`. Regex covers >95% deterministically. The dropdown handles the rest.
- **Three matrix files unified into `model_config_matrix.yaml`**: `prompt_matrix.yaml` + `settings_matrix.yaml` + `instruction_matrix.yaml` are family-keyed fall-through tables with drift between their family lists today (`claude-opus`, `gemini`, `deepseek`, `o-series` are in settings but not prompts; `codex`/`codex-spark` are in prompts but not instructions). Merging eliminates the drift, makes the family taxonomy unambiguous, and reduces "add a new family" from three file edits to one. Fall-through semantics are preserved by structuring as `families[name].{prompts,instructions,settings}`.
- **`whisper` and `tts` added to the capability enum now**: avoids a future schema migration when audio routes land. No call site queries them yet.
- **Boot-time `LLM_BASE_URL` check is ERROR + exit, not WARN + ignore**: the var being set indicates an active misconfiguration that won't self-heal. Loud failure beats slow 401s.
- **Discovery cache TTL: 24h**: provider catalogs change on the order of weeks; admin can force-rediscover. 24h cap prevents stale lists from blocking new model adoption while keeping API call volume low.
- **Family taxonomy = `model_config_matrix.yaml` keys, no separate registry**: the top-level keys (minus `default`) are the closed set of families. No `family_registry.yaml`, no parallel list of canonical model IDs per family. Providers without a `/models` endpoint (Anthropic) are simply skipped by auto-discovery; admin adds those models manually via Admin → Models. Adding a new family is one file edit plus the prompt/instruction files it references.

## Testing

- **Unit (`family_matcher`)**: 30+ fixture model IDs covering each rule + the `default` fallback + OpenRouter prefix recursion + edge cases (`gpt-4`, `gpt-4o`, `gpt-5.2`, `o3`, `o3-mini`, `claude-opus-4-7`, `text-embedding-3-large`, `openrouter/anthropic/claude-opus-4-7`).
- **Unit (`discover_models`)**: recorded fixtures for each provider's `/models` response shape (or hardcoded shortlist for Anthropic). Asserts candidate construction, family auto-detection, and 24h cache behavior.
- **Unit (`resolve_model`)**: catalog miss returns `UnknownModelError`; no YAML fallback path remains. Add a regression test that fails if `_load_builtin_catalog` is reintroduced.
- **Unit (boot check)**: orchestrator startup raises and exits when `LLM_BASE_URL` is set, with the expected migration message in the log.
- **Unit (helm seed)**: re-running the seed against a populated table is a no-op; admin edits survive; entries whose `providerRef` resolves to an absent transport are skipped with a log line.
- **Integration**: cold-boot orchestrator with empty helm seed and zero rows. Assert `/api/system/readiness` returns `ready=false`, `missing_capabilities=["chat","embedding","auxiliary"]`. Cockpit blocks behind the gate. Admin adds an Anthropic key, sees discovery results (registry-driven, since Anthropic has no `/models`), confirms a chat row + an auxiliary row, gate transitions: `missing_capabilities=["embedding"]`. Adds an OpenRouter key for embedding, gate releases.
- **Integration (dispatcher hard-fail)**: with no chat model, `POST /api/jobs` returns 503 with `missing_capabilities` in the body. Same for `POST /api/persistent/threads`.
- **Manual**: delete `config/models.yaml` from a built image, restart, confirm orchestrator boots cleanly with the helm-seeded catalog.

## Open questions

None — both prior open questions resolved (see Decisions: "Default model pin is required" and "Family taxonomy = `prompt_matrix.yaml` keys").
