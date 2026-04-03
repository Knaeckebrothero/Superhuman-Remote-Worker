# Issue: Hardcoded Model Lists Across the System

Created: 2026-04-02

## Problem

Model lists are duplicated and hardcoded in **12 locations** across frontend, backend, and deployment configs. Adding or removing a model requires manual edits in multiple files with no single source of truth. This leads to drift between environments and stale options in the UI.

## Affected Locations

### Frontend (5 locations)

| File | Lines | What |
|------|-------|------|
| `cockpit/src/assets/env.js` | 18-50 | `models`, `modelPresets`, `builderModels` — primary runtime config |
| `cockpit/src/app/core/environment.ts` | 41-49 | `builderModels` fallback defaults |
| `cockpit/src/app/simple/pages/sessions/sessions-page.component.ts` | 96-112 | Persistent session model `<select>` dropdown |
| `deployment/22-cockpit.yaml` | 24-50 | K8s ConfigMap copy of `env.js` values |
| `cockpit/src/app/pages/settings/settings.component.ts` | 16-24 | API key provider list |

### Backend (2 locations)

| File | Lines | What |
|------|-------|------|
| `src/core/loader.py` | 1585-1672 | `_detect_provider()` and `detect_model_family()` — pattern-matching |
| `orchestrator/services/builder_config.py` | 42-89 | Duplicated `detect_model_family()` |

### Configuration (5 locations)

| File | What |
|------|------|
| `config/settings_matrix.yaml` | Model family settings (8 families) |
| `config/prompt_matrix.yaml` | Model-specific prompt file mappings |
| `config/defaults.yaml` | Default model references |
| `config/experts/developer/config.yaml` | Expert-level model overrides |
| `orchestrator/config/builder_settings_matrix.yaml` | Builder model family settings |

## Current Models

```
Local:       openai/gpt-oss-120b
OpenAI:      gpt-5.2, gpt-5.2-pro, gpt-5.4, codex/gpt-5.4-pro
Anthropic:   claude-opus-4-6, claude-sonnet-4-5-20250929
Google:      gemini-2.5-pro, gemini-2.5-flash
Groq:        groq/moonshotai/kimi-k2-instruct-0905, groq/gpt-oss-120b
OpenRouter:  openrouter/minimax/minimax-m2.7
```

## Proposed Solution

### 1. Backend: `/api/models` endpoint

Add a REST endpoint on the orchestrator that returns the canonical model list:

```json
GET /api/models

{
  "models": [
    {"id": "openai/gpt-oss-120b", "label": "GPT-OSS 120B (Local)", "group": "Local", "provider": "openai"},
    {"id": "claude-opus-4-6", "label": "Claude Opus 4.6", "group": "Anthropic", "provider": "anthropic"},
    ...
  ],
  "presets": [
    {"label": "Opus + Sonnet", "strategic": "claude-opus-4-6", "tactical": "claude-sonnet-4-5-20250929"},
    ...
  ]
}
```

Source of truth: a single YAML file (e.g. `config/models.yaml`) loaded by the orchestrator.

### 2. Frontend: fetch from API

- `env.js` / deployment ConfigMap: remove hardcoded model arrays
- `environment.ts`: fetch from `/api/models` on init (with `env.js` as offline fallback)
- Sessions page `<select>`: populate from the same fetched list
- Builder model dropdown: same source

### 3. Dedup `detect_model_family()`

- Keep one copy in a shared location (or have the orchestrator call the agent's version)
- The YAML matrices (`settings_matrix.yaml`, `prompt_matrix.yaml`) remain as-is since they define per-family *behavior*, not the model list itself

## Impact

- **Low risk** — purely additive; existing env.js fallback ensures no breakage if API is unavailable
- **High value** — eliminates the most frequent manual edit when models change (currently ~5 files per model add/remove)

## Priority

Medium. Not blocking, but every model change currently requires touching multiple files and redeploying the cockpit ConfigMap.
