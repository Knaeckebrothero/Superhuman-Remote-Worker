# Custom LLM Endpoints

Per-user, UI-configurable OpenAI-compatible LLM endpoints and models. Replaces the current env-var-only mechanism (`LLM_BASE_URL`, hardcoded entries in `config/models.yaml`) with a settings page where a user pastes a base URL, API key, and one or more model IDs, and those models immediately appear in every model picker across the app.

## Problem

The loader accepts `config.llm.base_url` and routes requests correctly, but nothing above the loader populates it from user input. The only paths today are:

- Editing `config/defaults.yaml` / `config/persistent_defaults.yaml` and rebuilding the image.
- Setting `LLM_BASE_URL` on the agent pod env — only honored for models matching `_needs_custom_base_url()` (the `openai/` prefix), silently ignored for anything else.

Consequences:

- A deployer of SRW cannot point `openai/gpt-oss-120b` at their own vLLM without patching manifests.
- Model pickers in the Cockpit are driven by hardcoded lists in `cockpit/src/app/core/environment.ts:40-47` and `config/models.yaml:75-77`. Adding a model means a code change.

Compounding all of this, provider selection today is derived from the **model string itself**: `_detect_provider()` at `src/core/loader.py:1615` (and its duplicate `_detect_provider_from_model()` at `orchestrator/main.py:1450`) does prefix matching — `openrouter/` → openrouter, `claude` → anthropic, default → openai. The same style drives `detect_model_family()` for prompt-matrix lookups and `_needs_custom_base_url()` for env-var routing. The approach is a recurring source of bugs: it breaks for model IDs that don't match a known prefix, can't express that a model ID belongs to a user's custom endpoint, and any addition means editing the detection functions in two places. Custom endpoints make this untenable — a user-configured model ID could be anything, with no lexical tell about which provider or base URL it belongs to.

## Scope

**In scope**
- New DB tables for per-user LLM endpoints and the models served by each
- Cockpit settings section: CRUD for endpoints and their models, plus a "test connection" button
- Merge user-defined models into the existing model-picker API (`/api/models/available` and friends) so sessions, jobs, builder, and the advanced config panel all see them
- Dispatch-time injection of `base_url` + `api_key` into `config_override["llm"]` when the selected model matches a user endpoint
- **Replace string-based provider detection with a registry-based resolver** shared by the loader and the dispatcher; retire `_detect_provider`, `_detect_provider_from_model`, `detect_model_family`, and `_needs_custom_base_url`
- Keep existing hardcoded providers (OpenAI, Anthropic, Google, Groq, OpenRouter, Codex) unchanged at the factory level — they continue to resolve via `user_api_keys` and built-in base URLs, just looked up through the registry instead of inferred from the ID

**Out of scope (v1)**
- Admin-defined shared endpoints (only user-scoped endpoints for v1; a later "workspace endpoint" concept can be added)
- Per-project endpoint overrides (a user's endpoints are global to their account)
- Auto-discovery via `/v1/models` listing from the endpoint — the user types model IDs explicitly
- Encryption-at-rest for API keys beyond what `user_api_keys` already does (same tradeoff, not this feature's problem to solve)
- Migration tooling for existing `LLM_BASE_URL` env configs — operators recreate those as user endpoints by hand

## Architecture

### Storage

Two tables in `orchestrator/database/schema.sql`, modeled on `user_api_keys`:

```sql
CREATE TABLE user_llm_endpoints (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL,             -- "FINIUS vLLM", "Home Ollama"
    base_url TEXT NOT NULL,          -- https://llm.example.com/v1
    api_key TEXT,                    -- nullable; some local servers skip auth
    key_prefix VARCHAR(12),          -- for UI display; NULL when api_key is NULL
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_user_llm_endpoint_label UNIQUE (user_id, label)
);

CREATE TABLE user_llm_endpoint_models (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    endpoint_id UUID NOT NULL REFERENCES user_llm_endpoints(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,          -- sent verbatim to the server
    display_name TEXT NOT NULL,      -- UI label
    family TEXT,                     -- optional override; settings_matrix auto-detects if NULL
    context_window INT,              -- optional override; settings_matrix default if NULL
    reasoning_level TEXT,            -- optional default (none/minimal/low/medium/high/xhigh)
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_endpoint_model UNIQUE (endpoint_id, model_id)
);
```

Why two tables: a single endpoint typically serves several models (a vLLM gateway, Ollama with five pulled models, a compatibility shim in front of a private API). One row per (endpoint, model) would duplicate the URL and key across rows; moving them keeps key rotation to one edit.

The same constraint pattern as `user_api_keys` — cascade deletes, unique labels scoped per user — keeps API shape predictable.

### Model resolution (registry)

A single function, `resolve_model(model_id, user_id) → ModelMeta`, replaces every string-sniffing code path. It lives in a new module `src/core/model_registry.py` so the orchestrator (`orchestrator/main.py`) and the loader (`src/core/loader.py`) import it from the same place — no more duplicated detection logic.

`ModelMeta` is a plain dataclass:

```python
@dataclass(frozen=True)
class ModelMeta:
    model_id: str              # passed to the API verbatim after provider-specific stripping
    provider: str              # "openai" | "anthropic" | "google" | "groq" | "openrouter" | "codex"
    family: str                # prompt-matrix key: "claude-opus", "gpt-oss", "deepseek", ...
    display_name: str
    base_url: Optional[str]    # built-in default if None; always set for custom endpoints
    api_key_ref: Optional[str] # which user_api_keys.provider row to pull from; None for custom (inline key)
    context_window: Optional[int]
    reasoning_level: Optional[str]
    origin: str                # "builtin" | "custom"
    endpoint_id: Optional[str] # populated when origin == "custom"
```

Resolution order (strict — no string-sniffing fallback):

1. **Custom endpoints** — query `user_llm_endpoint_models JOIN user_llm_endpoints` for `(user_id, model_id)`. If a row matches, return a `ModelMeta` with `origin="custom"`, `provider="openai"` (the factory used), `base_url` and `api_key_ref=None` (key travels inline), and the per-row overrides for `family`, `context_window`, `reasoning_level`.
2. **Built-in catalog** — `config/models.yaml` is the canonical source. It already carries `provider` and `family` on each model entry; we add `base_url` (null for provider-default) and make the loader read these fields instead of inferring. Loaded once at startup into an in-memory `dict[str, ModelMeta]`.
3. **Miss** — raise `UnknownModelError(model_id)`. No silent fallback. The loader catches this and surfaces a clear error to the dispatcher; the orchestrator returns a 400 with a message naming the model and pointing the user at settings.

Custom entries win over built-in entries with the same ID by virtue of step 1 running first — a user who configures `openai/gpt-oss-120b` on their own vLLM gets their endpoint, not `api.openai.com`.

`config/models.yaml` gains the fields needed to make it self-sufficient. Today's entry:

```yaml
- id: "openrouter/minimax/minimax-m2.7"
  display_name: "MiniMax M2.7"
  family: minimax
```

Becomes:

```yaml
- id: "openrouter/minimax/minimax-m2.7"
  display_name: "MiniMax M2.7"
  provider: openrouter              # was implied by the "openrouter/" prefix
  family: minimax
  base_url: null                    # null = use provider default
  context_window: 204800
  reasoning_level: null
```

This is a one-time YAML migration. The provider prefix stays in the ID string because it's still sent to OpenRouter (after the provider-specific `openrouter/` strip in `_create_openrouter_llm`) — the registry treats it as an opaque label, not a routing signal.

Factory dispatch in `create_llm()` becomes a dict lookup on `meta.provider` instead of a chain of `startswith()` calls. Custom endpoints route through `_create_openai_llm` because `provider` is set to `"openai"` in their `ModelMeta` (the factory already honors `config.base_url`; no loader changes beyond this).

#### What gets retired

| Function | Replaced by |
|----------|-------------|
| `_detect_provider()` (loader.py:1615) | `resolve_model().provider` |
| `_detect_provider_from_model()` (main.py:1450) | `resolve_model().provider` |
| `detect_model_family()` (loader.py:1649) | `resolve_model().family` |
| `_needs_custom_base_url()` (loader.py:1736) | `resolve_model().base_url is not None` |

Callers to audit during the refactor: `src/core/loader.py` (multiple factory functions), `orchestrator/main.py` (dispatch block, auxiliary model resolution, `/api/models/available` endpoint, vision/whisper provider detection), and `tests/test_loader_routing.py`, `tests/test_models_api.py`, `tests/test_settings_matrix.py`, `tests/test_prompt_matrix.py` — those all construct model strings and call the detection functions directly, so they'll need updates or deletion. Roughly a dozen sites total.

#### Explicit provider escape hatch

`LLMConfig.provider` (already exists, currently optional) stays supported — when set, it overrides `ModelMeta.provider`. This is the only escape hatch from the registry: useful for ad-hoc debugging and for legacy configs that set provider directly. The `explicit_provider` parameter on the old detect function becomes this field, and that's it.

### Orchestrator API

New REST endpoints under `/api/settings/llm-endpoints`, all requiring an authenticated user:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/settings/llm-endpoints` | List user's endpoints with nested models |
| `POST` | `/api/settings/llm-endpoints` | Create endpoint |
| `PATCH` | `/api/settings/llm-endpoints/{id}` | Update endpoint (label, base_url, api_key) |
| `DELETE` | `/api/settings/llm-endpoints/{id}` | Delete endpoint and its models |
| `POST` | `/api/settings/llm-endpoints/{id}/models` | Add a model to endpoint |
| `PATCH` | `/api/settings/llm-endpoints/{id}/models/{model_id}` | Update model row |
| `DELETE` | `/api/settings/llm-endpoints/{id}/models/{model_id}` | Remove model |
| `POST` | `/api/settings/llm-endpoints/{id}/test` | Probe `GET {base_url}/models` (or `POST /chat/completions` with a minimal payload) and return status |

API keys are returned as `key_prefix` only, never the full value, mirroring `user_api_keys` responses.

### Dispatch injection

The dispatcher block at `orchestrator/main.py:818-837` becomes a single registry call instead of provider-sniffing. Conceptually:

```python
meta = await resolve_model(model_id=resolved_model, user_id=user_id)
llm_over = config_override.setdefault("llm", {})

if meta.origin == "custom":
    # Inline key + base_url from the user's endpoint row.
    llm_over.setdefault("base_url", meta.base_url)
    endpoint = await postgres_db.get_user_llm_endpoint(meta.endpoint_id)
    if endpoint.get("api_key"):
        llm_over.setdefault("api_key", endpoint["api_key"])
else:
    # Built-in: resolve api_key from user_api_keys by meta.api_key_ref (== meta.provider).
    if meta.api_key_ref and meta.api_key_ref in resolved_keys:
        llm_over.setdefault("api_key", resolved_keys[meta.api_key_ref])
    if meta.base_url:
        llm_over.setdefault("base_url", meta.base_url)

if meta.context_window:
    llm_over.setdefault("model_max_context_tokens", meta.context_window)
if meta.reasoning_level:
    llm_over.setdefault("reasoning_level", meta.reasoning_level)
```

The `setdefault` calls preserve higher-priority overrides (per-job, per-project). Injection is additive.

The same `resolve_model()` call replaces the auxiliary-model detection at `orchestrator/main.py:843-855`. Vision/whisper/embedding models still go through env vars for v1 — reconsider when we unify the auxiliary model path.

### Model catalog surfacing

The endpoints that today return available models — `GET /api/models/available`, the builder models list, the advanced config model dropdown — merge the user's custom models into the response:

```json
{
  "groups": [
    { "name": "OpenAI", "models": [...] },
    ...
    {
      "name": "Custom: FINIUS vLLM",
      "provider": "custom",
      "endpoint_id": "uuid",
      "models": [
        { "id": "openai/gpt-oss-120b",
          "display_name": "GPT-OSS 120B (FINIUS)",
          "family": "gpt-oss" }
      ]
    }
  ]
}
```

The frontend doesn't need to know the model is custom — it passes `model_id` back on job/session create as usual, and the dispatcher figures out routing.

### Cockpit UI

New section in the settings page, sibling to the existing "API Keys" panel:

- **Endpoints list** — each row shows label, base URL (host only), masked key prefix, test-connection button, model count, edit/delete
- **Endpoint edit dialog** — label, base URL (required, must parse as URL, HTTPS by default with an "I know what I'm doing" checkbox for HTTP), API key (optional, masked input)
- **Models subtable** (expandable per endpoint) — add / edit / delete rows, each with model ID, display name, optional family override, optional context window, optional reasoning level, enable toggle
- **Test connection** — runs the test endpoint, reports HTTP status + first error message if any; does not persist any state

When a user deletes an endpoint that is in active use (referenced by a session's `config_override` or a running job), the delete succeeds but in-flight agents keep their already-injected base_url until restart. Acceptable — matches the existing behavior for API key rotation.

## Migration

`LLM_BASE_URL` and the hardcoded `openai/gpt-oss-120b`-as-local convention become deprecated but not removed:

- Loader continues to honor `LLM_BASE_URL` when no `config.base_url` is set — existing deployments keep working during the transition.
- The agent emits a `DeprecationWarning` at startup if `LLM_BASE_URL` is set, pointing at the settings UI.
- `config/models.yaml` keeps its "Local" provider group for now; can be simplified or removed in a follow-up once no code depends on the hardcoded entry.

The registry refactor ships as part of this feature, not after:

1. Add `provider`, `base_url`, `context_window`, `reasoning_level` fields to every entry in `config/models.yaml`. Schema migration only — no behavior change until step 3.
2. Introduce `src/core/model_registry.py` with `resolve_model()` and `ModelMeta`, reading the augmented YAML at startup. Cover built-ins first; the custom-endpoints lookup lands once the DB tables exist.
3. Replace call sites of `_detect_provider`, `_detect_provider_from_model`, `detect_model_family`, `_needs_custom_base_url` with `resolve_model()`. Delete the old functions. Update the tests named above in the same PR (they construct model strings just to exercise detection — most should call `resolve_model()` directly, some can be deleted).
4. Flip the dispatcher to use the new injection shape. Ship the UI.

Step 3 is the only one that can break things; keep it in a single PR so a revert is clean.

## Security

- Plaintext storage of `api_key` matches existing `user_api_keys` policy (not this feature's scope to change).
- `base_url` validation on save: must be a parseable URL with an `http`/`https` scheme, rejects `file://`, `javascript:`, etc. HTTPS is required unless the request body sets `allow_insecure=true` — this guards against copy-paste accidents, not against a user who wants local HTTP.
- Never log the full `api_key`. Log `key_prefix` only.
- Test-connection endpoint runs server-side from the orchestrator pod (not from the user's browser), so the key is never exposed to the client.
- Rate-limit the test-connection endpoint (10 requests per minute per user) to prevent abuse as a port-scanner proxy.

## Testing

- **Unit (registry)**: `resolve_model()` returns correct `ModelMeta` for every built-in entry in `models.yaml`; custom endpoint lookup beats built-in on matching IDs; unknown IDs raise `UnknownModelError`; `LLMConfig.provider` override is honored.
- **Unit (orchestrator)**: dispatcher injects `base_url` and `api_key` into `config_override` using the resolver output; injection respects existing higher-priority overrides; auxiliary model path uses the same resolver.
- **Unit (loader)**: already covered by `tests/test_loader_routing.py::TestOpenAILLMRouting` — explicit `config.base_url` wins. Add a regression test that a model with a custom `base_url` never falls back to `LLM_BASE_URL`.
- **Integration**: end-to-end job dispatch against a mock OpenAI-compat server running on localhost, confirming the request lands on the mock and not on `api.openai.com`.
- **Cockpit**: vitest specs for the endpoint list component, form validation (URL format, HTTPS guard), masked key display.
- **Manual QA checklist**:
  - Create endpoint + model, create a session with that model, confirm first request hits the endpoint (check orchestrator logs for the injected base_url).
  - Hot-swap a session's model to a custom one via the session config dropdown, confirm swap lands correctly (exercises the recent `persistent_graph.py` refresh fix).
  - Delete the endpoint while a session is running — session keeps working; new sessions cannot pick it.
  - Send a request with a completely unknown `model_id` and confirm it 400s with `UnknownModelError` instead of silently routing to `api.openai.com`.

## Open questions

- **Should endpoints support a `headers` map** (e.g. `X-Custom-Auth`, `OpenAI-Organization`)? Some proxy gateways need extra headers. Defer to v1.1 unless a concrete user hits it.
- **Sharing**: when an admin wants to publish a shared endpoint to all users, where does it live? Likely a sibling `workspace_llm_endpoints` table with the same shape, merged into `available_models` for all users in the workspace. Design when needed.
- **Registry reload**: `config/models.yaml` is loaded at startup into the registry. Should edits require a full agent restart, or do we expose a signal/endpoint that reloads in place? Lean restart-only for v1 — YAML edits are an operator action, not a user flow.
- **Family default for custom models**: registry lookup returns `family` from the DB row, so if a user doesn't supply one, the column is NULL. We either default to `"default"` (generic prompt matrix entry) or require `family` on model creation. Leaning default-and-let-user-override; the UI form pre-fills a best guess from the model ID string and the user can correct it before saving.
