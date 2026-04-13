# Settings Default Resolution

## Problem

Settings forms across the application show placeholder labels like **"Not set"**, **"Not set (use expert default)"**, and **"Server default"** instead of showing the actual effective values. Users cannot tell what defaults are applied without digging into YAML config files.

## Two Patterns Exist Today

The application has **two different patterns** for handling defaults, which creates the inconsistency:

### Pattern A: Settings Page (broken -- shows placeholders)

`cockpit/src/app/pages/settings/settings.component.ts`

The Preferences section uses simple `[(ngModel)]` binding with empty-string fallbacks. When the user hasn't set a preference, the dropdown shows "Not set" or "Server default" -- hiding the actual effective value.

```typescript
// constructor effect -- syncs from API response
this.prefModel = prefs.default_model || '';      // '' maps to "Not set" option
this.prefAutonomy = prefs.default_autonomy || ''; // '' maps to "Not set" option
```

```html
<select [(ngModel)]="prefModel">
  <option value="">Not set (use expert default)</option>
  <!-- ... model options ... -->
</select>
```

**Affected fields (8 dropdowns + 3 persistent agent fields):**
- Default Model: `"Not set (use expert default)"`
- Auxiliary Model: `"Not set (use default)"`
- Default Autonomy: `"Not set"`
- Default Reasoning Level: `"Not set"`
- Vision Model: `"Server default"`
- Whisper Model: `"Server default"`
- Embedding Model: `"Server default"`
- Embedding Provider: `"Server default"`
- Persistent Agent > Permission Mode: `"Not set"`
- Persistent Agent > Model: empty text input (no placeholder showing default)
- Persistent Agent > Idle Timeout: empty (no hint of default 30min)

### Pattern B: Agent Settings Components (working -- resolves defaults)

`cockpit/src/app/shared/components/agent-settings/` (used in job creation and session creation)

These components receive the **merged expert config** from `GET /api/experts/{id}` (or `GET /api/experts/defaults` for framework defaults) and use signals with `null` = unmodified semantics:

```typescript
// Override signal: null means "use resolved default"
readonly strategicModel = signal<string | null>(null);

// Resolved default: reads from the expert/framework config
readonly resolvedStrategicModel = computed(() =>
  (readConfigPath(this.config(), 'llm.strategic.model') as string)
  ?? (readConfigPath(this.config(), 'llm.model') as string)
  ?? null
);

// Template: shows resolved default until user overrides
[ngModel]="strategicModel() ?? resolvedStrategicModel()"
```

Visual feedback:
- `[class.modified]="strategicModel() !== null"` -- highlights user-overridden fields
- Reset button appears when a field has been overridden, clicking sets signal back to `null`

**This pattern correctly shows the actual default value** (e.g. "openai/gpt-oss-120b" for model, "review" for autonomy, "high" for reasoning) and lets the user see what they're getting before touching anything.

## Data Flow

### How defaults are currently loaded

```
config/defaults.yaml                    ─┐
config/experts/{name}/config.yaml       ─┤  $extends deep-merge
config/persistent_defaults.yaml         ─┘
         │
         ▼
GET /api/experts/defaults  ──────────► job-create.component.ts
GET /api/experts/{id}      ──────────► (passes merged config to agent-settings)
         │
         ▼
agent-settings receives `config` input ──► sub-components use readConfigPath()
```

### How the Settings page currently loads

```
GET /api/settings/preferences ──► settings.service.ts ──► preferences signal
         │                                                    │
         ▼                                                    ▼
Returns raw user JSONB:                              prefs.default_model || ''
  { "default_model": null,                           ──► dropdown shows "Not set"
    "default_autonomy": null, ... }
```

**The gap:** The Settings page never calls `/api/experts/defaults` and has no access to framework/expert config. It only sees raw user overrides.

### Resolution at dispatch time (invisible)

```
orchestrator/main.py ~lines 850-933:
  user_settings = get_user_settings(user_id)
  if user_settings.get("default_model"):
      config_override["llm"]["model"] = default_model
  # ... same for autonomy, reasoning, vision, whisper, embedding, etc.
```

This is the only place where user preferences get merged with framework defaults, and it's invisible to the UI.

## All Settings Surfaces Audited

### 1. Settings Page -- `/settings`
**Status: BROKEN -- shows placeholders for all unset fields**

| Section | Fields | Placeholder |
|---------|--------|-------------|
| Preferences | Default Model | "Not set (use expert default)" |
| Preferences | Auxiliary Model | "Not set (use default)" |
| Preferences | Default Autonomy | "Not set" |
| Preferences | Default Reasoning Level | "Not set" |
| Preferences (Helper Models) | Vision Model | "Server default" |
| Preferences (Helper Models) | Whisper Model | "Server default" |
| Preferences (Helper Models) | Embedding Model | "Server default" |
| Preferences (Helper Models) | Embedding Provider | "Server default" |
| Persistent Agent | Model | empty text input |
| Persistent Agent | Permission Mode | "Not set" |
| Persistent Agent | Idle Timeout | empty (default is 30) |
| Persistent Agent | Command Allowlist | empty |
| Communication | Reply Delivery | "Next strategic phase (default)" -- label includes hint but ok |

### 2. Job Creation -- `/create`
**Status: WORKING -- resolves defaults from expert/framework config**

All fields in the agent-settings component show resolved values:
- Autonomy: shows "Review" (resolved from config)
- Strategic/Tactical Model: shows "openai/gpt-oss-120b" (from defaults.yaml)
- Reasoning: shows "High"
- Temperature: shows resolved matrix value (1.0 for gpt-oss)
- Scholar/Critic/Memory toggles: show resolved boolean defaults
- All Advanced accordion sections: show resolved values

**Minor issue:** Model dropdown "Default" option doesn't show what model that resolves to -- just says "Default".

### 3. Session Creation -- `/sessions/new`
**Status: WORKING -- resolves defaults**

- Permission Mode: shows "Supervised" (resolved)
- Model: shows "openai/gpt-oss-120b" (resolved)
- Tools: all resolved from config

### 4. Project Detail -- `/projects/{id}` Settings tab
**Status: MINOR -- "Default Config" text input is empty, no hint of what default means**

- Default Config: empty text input, placeholder "e.g. developer, scholar"
- Memory toggle: shows resolved value (checked)

### 5. Project Expert Settings -- `/projects/{id}` Experts tab
**Status: WORKING -- uses config-editor component with expertConfig resolution**

The `config-editor.component.ts` follows the same Pattern B with `expertConfig` input.

## Proposed Solution

### Approach: Extend the existing Pattern B to the Settings page

Rather than building a new system, the Settings page should reuse the same default resolution infrastructure that job/session creation already uses. The key pieces already exist.

### Step 1: Backend -- Add resolved defaults to preferences endpoint

Extend `GET /api/settings/preferences` to optionally return resolved defaults alongside user overrides.

```
GET /api/settings/preferences?resolved=true
```

```json
{
  "user": {
    "default_model": null,
    "default_autonomy": null
  },
  "resolved": {
    "default_model": "openai/gpt-oss-120b",
    "default_autonomy": "review",
    "default_reasoning_level": "high",
    "default_vision_model": "gpt-4o",
    "default_whisper_model": "openai/whisper-1",
    "default_embedding_model": "qwen3-embedding-8b",
    "embedding_provider": "local",
    "persistent_agent": {
      "model": "openai/gpt-oss-120b",
      "permission_mode": "supervised",
      "idle_timeout_minutes": 30
    }
  }
}
```

The orchestrator already loads `defaults.yaml` and expert configs at startup. The resolution logic from dispatch (~lines 850-933) can be extracted into a reusable function.

### Step 2: Frontend -- Adopt the override/resolved signal pattern

Refactor `settings.component.ts` to follow Pattern B:

```typescript
// Signal: null = use resolved default, string = user override
readonly prefModel = signal<string | null>(null);

// Loaded from GET /api/settings/preferences?resolved=true
readonly resolvedModel = signal<string>('');

// Template binding
[ngModel]="prefModel() ?? resolvedModel()"
[class.is-default]="prefModel() === null"
```

When saving, only send non-null overrides. When clearing, send `null` to remove the override.

### Step 3: Visual indication for defaults vs overrides

Use the same `[class.modified]` / reset button pattern from agent-settings:
- **Default value visible** -- user sees "openai/gpt-oss-120b" in the dropdown, not "Not set"
- **Subtle visual hint** -- dimmed text or "(default)" label when showing a resolved default
- **Reset button** -- appears when user has overridden, clears back to resolved default

### Step 4: Unify into a shared utility

Both the Settings page and agent-settings components deal with the same problem: showing resolved values with override semantics. Consider extracting:

- **`ResolvedField<T>`** -- a signal wrapper that holds `{ override: T | null, resolved: T, source: string }` and exposes `effective()` computed
- **`DefaultIndicatorComponent`** -- a small component that shows "(default)" or a reset button based on override state

This would replace the ad-hoc signals in both places and ensure consistent UX.

## Files to Change

### Backend
- `orchestrator/main.py` -- Extract dispatch-time default resolution into a reusable function; add `?resolved=true` support to `GET /api/settings/preferences`

### Frontend (primary)
- `cockpit/src/app/pages/settings/settings.component.ts` -- Refactor all preference fields to use override/resolved signal pattern
- `cockpit/src/app/core/services/settings.service.ts` -- Add `loadResolvedPreferences()` method

### Frontend (shared utilities, optional)
- `cockpit/src/app/shared/components/agent-settings/agent-settings.types.ts` -- Potential home for `ResolvedField<T>` utility
- New: `cockpit/src/app/shared/components/default-indicator/` -- Reusable default/override indicator component

### Frontend (minor fixes)
- `cockpit/src/app/shared/components/agent-settings/model-group.component.ts` -- "Default" option in model dropdown should show the resolved model name (e.g. "Default (openai/gpt-oss-120b)")
