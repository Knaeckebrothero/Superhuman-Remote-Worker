# Extended Model Roster: Custom Model Support

## Problem

When a user configures an API key for a provider like OpenRouter, they technically have access to hundreds of models — but the system only shows the handful defined in `config/models.yaml`. Conversely, showing all available models from a provider's `/models` endpoint leads to overwhelming dropdowns (Open WebUI explicitly warns against this for OpenRouter).

We need a way for users to use arbitrary model IDs beyond the curated catalog without flooding the UI.

## Industry Research

Seven comparable platforms were analyzed: TypingMind, Open WebUI, LibreChat, LobeChat, Jan.ai, AnythingLLM, and Dify.

### Common Patterns

| Pattern | Platforms |
|---------|-----------|
| Curated catalog + freeform model ID input | TypingMind, LobeChat, Jan.ai |
| Auto-discover via provider `/models` endpoint | Open WebUI, LibreChat, Jan.ai, LobeChat |
| Admin curates an allowlist | Open WebUI, Dify, LibreChat |
| "Custom" badge on user-added models | LobeChat, Dify |

### Key Takeaway

Every mature platform converges on the same hybrid: **a curated catalog for the common path, plus freeform model ID input as an escape hatch**. No platform that serves power users omits the ability to type an arbitrary model string.

### The OpenRouter / Large-Provider Problem

Every platform that supports OpenRouter faces the "thousands of models in a dropdown" problem. Solutions fall into three camps:

1. **Fetch all, let user search** (LibreChat, LobeChat) — overwhelming and slow
2. **Fetch all, admin curates allowlist** (Open WebUI) — requires admin involvement
3. **User types model IDs manually** (TypingMind) — simplest, requires user knowledge

Open WebUI explicitly warns against auto-discovery for OpenRouter due to 10-15s page load times.

## Proposed Design

### Overview

Keep `config/models.yaml` as the curated catalog (display names, family mappings, tested inference settings). Add a **user-managed custom model roster** stored per-user in the database. Custom models appear alongside curated ones in all dropdowns, visually distinguished.

### Two-Tier Model System

**Curated models** (from `models.yaml`):
- Have display names, family mappings, and known-good inference parameters
- Maintained by system admins via the config file
- Shown to all users with provider availability annotations

**Custom models** (user-added):
- Stored per-user in the `user_custom_models` DB table
- User specifies: provider, model ID, display name (optional)
- Get `default` family settings (or user picks from existing families)
- Shown only to the user who added them (or project-scoped if added at project level)
- Visually tagged as "Custom" in dropdowns

### Database Schema

```sql
CREATE TABLE IF NOT EXISTS user_custom_models (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,          -- e.g. "openrouter", "openai"
    model_id TEXT NOT NULL,          -- e.g. "openrouter/meta-llama/llama-4-maverick"
    display_name TEXT,               -- optional friendly name
    family TEXT DEFAULT 'default',   -- maps to settings_matrix.yaml
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, model_id)
);

-- Optional: project-scoped custom models (shared with team)
CREATE TABLE IF NOT EXISTS project_custom_models (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    display_name TEXT,
    family TEXT DEFAULT 'default',
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (project_id, model_id)
);
```

### API Changes

**`GET /api/models`** — extend the response to include a `"Custom"` group with user/project custom models:

```json
{
  "groups": [
    {"group": "Local", "provider": "local", "configured": true, "models": ["openai/gpt-oss-120b"]},
    {"group": "OpenAI", "provider": "openai", "configured": true, "models": ["gpt-5.4", "gpt-4o"]},
    {"group": "Custom", "provider": "mixed", "configured": true, "models": ["openrouter/meta-llama/llama-4-maverick"]}
  ],
  "configured_providers": ["openai", "openrouter"]
}
```

**`POST /api/settings/custom-models`** — add a custom model:

```json
{
  "provider": "openrouter",
  "model_id": "openrouter/meta-llama/llama-4-maverick",
  "display_name": "Llama 4 Maverick",
  "family": "default"
}
```

**`GET /api/settings/custom-models`** — list user's custom models.

**`DELETE /api/settings/custom-models/{model_id}`** — remove a custom model.

Equivalent project-scoped endpoints under `/api/projects/{id}/custom-models`.

### UI Changes

**Settings page** — new "Custom Models" section:
- Table listing the user's custom models (provider, model ID, display name, family)
- "Add Model" row with freeform text input for model ID, provider dropdown (from configured providers), optional display name, family picker
- Delete button per row

**Model dropdowns** — the "Custom" group appears as its own `<optgroup>` at the bottom, with models labeled distinctly (e.g., italicized or with a "custom" suffix).

### What We Deliberately Skip

- **No auto-discovery from provider `/models` endpoints** — avoids the dropdown explosion problem, avoids rate-limiting provider APIs, avoids stale caches. Users who want a specific model just type the ID.
- **No global admin custom models** — admins who want to add system-wide models should add them to `models.yaml` and redeploy. The custom model system is for user/project self-service.
- **No model validation at add time** — we don't call the provider to verify the model exists. If the user types a wrong ID, the agent will fail at runtime with a clear error. This matches how TypingMind and AnythingLLM handle it.

### Family Mapping for Custom Models

Custom models default to the `default` family in `settings_matrix.yaml`, which provides safe baseline inference parameters. Users can optionally pick a more specific family (e.g., `claude-opus`, `gemini`) if they know the model is compatible. This affects:

- Context window limits
- Temperature / top-p / top-k defaults
- Reasoning level options
- Prompt template variants (via `prompt_matrix.yaml`)

### Migration Path

1. Add DB tables (`user_custom_models`, `project_custom_models`)
2. Add CRUD API endpoints
3. Extend `GET /api/models` to merge custom models into the response
4. Add "Custom Models" section to the Settings page
5. No changes needed to the agent — it already receives the model ID as a string in config_override and doesn't care where it came from
