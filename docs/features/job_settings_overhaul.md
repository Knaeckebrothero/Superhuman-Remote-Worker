# Agent Settings Overhaul

The agent configuration UI has two separate problems that share the same root cause: settings don't reflect reality, and there's no consistent settings surface across agent types.

**Job creation** bundles all settings into a flat "Advanced Options" dump with a separate "Config Override Editor" underneath. Temperature sliders show 0, models show empty, and the form doesn't represent what the agent will actually get.

**Session creation** is a minimal dialog with hardcoded model options and no way to configure temperature, reasoning, tools, instructions, or autonomy. The only way to influence these is through the global "Persistent Agent" section on the user settings page, which applies to all sessions.

This overhaul introduces a shared settings surface used by both job creation and session creation, with resolved defaults, progressive disclosure, and user-saveable presets.

## Problems with Current Implementation

### 1. Settings don't reflect what the job will actually get

Form fields start as null/0/empty until an expert is selected. Temperature sliders show 0 and label "(default)" instead of showing the actual resolved value the agent will use. A user who hits "Create Job" without selecting an expert sees empty settings but the job gets full `defaults.yaml` values — the UI lies by omission.

**Root cause:** Settings are populated by `prefillConfigFromExpert()` only after expert selection. Before that, `getExpertPhaseDefault()` returns null (no `expertDetail`), so every fallback chain terminates at hardcoded values (`?? 0`, `?? false`, `?? 'review'`).

### 2. Everything is in one "Advanced" bag

Priority, autonomy, project memory, scholar/critic subjobs, model presets, two full LLM phase cards (model + reasoning + temperature + multimodal each), tool category toggles, a 12-row instructions editor, and a datasource picker — all in a single expandable. No grouping, no hierarchy.

### 3. Two disconnected config surfaces

The "Advanced Options" expandable and the "Config Override Editor" expandable are two separate UI surfaces that control the same underlying `config_override` object. The config editor note says "Values here take precedence over the Advanced Options above" — meaning users can unknowingly create conflicts, and there's no unified view of what the job will actually get.

### 4. Config override diffing is fragile

`buildConfigOverride()` computes a diff against expert defaults. If no expert is selected, almost nothing gets included in the override because there's nothing to diff against. The diff logic also has edge cases: temperature compares against `(stDefault ?? 0)`, so if the expert default *is* 0 and the user explicitly sets 0, no override is stored — which is correct but confusing when the UI shows "(default)" for an explicitly-set value.

### 5. Session creation has no settings at all

The "New Session" dialog (`sessions-page.component.ts`) is a minimal form with only: title, config (hardcoded 4-option dropdown), model (hardcoded list — not from `env.js`), projects, and permission mode. There's no way to configure:

- Temperature or reasoning level
- Tool categories
- Autonomy level
- Instructions or kickoff message
- Datasources
- Scholar/critic subjobs
- Multimodal

The only way to influence these is the global "Persistent Agent" section on the user settings page, which stores defaults in `users.settings.persistent_agent`. But that only covers model, permission mode, config, greeting, idle timeout, and command allowlist — still missing temperature, reasoning, tools, etc.

**Hardcoded models problem:** The session dialog has a hardcoded `<select>` with specific models (GPT-OSS 120B, GPT-5.4, Claude Opus 4.6, MiniMax M2.7) instead of reading from `env.js` like the job creation form does. Adding a new model requires a code change.

### 6. No shared settings component

Job creation and session creation are completely separate implementations with no shared code for model selection, reasoning levels, tool toggles, etc. Any fix to one (e.g., loading models from `env.js`) has to be manually replicated in the other.

## Design Goals

1. **WYSIWYG settings** — Every field shows the resolved value the job will actually use. No null states, no "(default)" mystery values. The form is pre-populated from the full resolution chain: expert config -> project override -> framework defaults.

2. **Progressive disclosure** — Common settings are visible on the main page. Rarely-changed settings live in a separate tab/section that doesn't clutter the primary flow.

3. **Single source of truth** — Eliminate the dual config editor / advanced options surfaces. All settings are managed through the settings UI. A "View as JSON" toggle can show the resulting override for debugging, but it's read-only (or at most a power-user escape hatch).

4. **Presets replace the config editor** — Users can save a particular combination of settings as a named preset and recall it later, replacing the need to manually edit JSON overrides. Presets are stored per-user in the backend.

## Architecture

### Resolved Defaults Pipeline

On component init (before any expert is selected), the form loads the full framework defaults:

```
GET /api/experts/defaults  →  frameworkDefaults
```

When an expert is selected, it loads the merged expert config:

```
GET /api/experts/{id}  →  expertDetail.config  (already merged with defaults server-side)
```

When a project is selected, the project's `default_config_override` is layered on top.

The resolution chain for any setting:

```
user override  >  project override  >  expert config  >  framework defaults
```

Every form field reads from this chain via a single resolver:

```typescript
resolveDefault(path: string): unknown {
  // 1. Expert config (already merged with framework defaults by the API)
  const expertVal = getByPath(this.expertDetail()?.config, path);
  if (expertVal !== undefined) return expertVal;

  // 2. Framework defaults (loaded on init, always available)
  return getByPath(this.frameworkDefaults(), path);
}
```

This means the form is **always populated** — even with no expert selected, temperature shows the real `defaults.yaml` value (e.g., 0.7), not 0.

### Shared Settings Component

The core of this overhaul is a reusable `AgentSettingsComponent` that both job creation and session creation embed. It encapsulates:

- Model selection (reads from `env.js`, not hardcoded)
- Reasoning level (adapts to selected model)
- Temperature slider (shows resolved value)
- Autonomy level
- Tool category toggles
- Multimodal toggle
- Scholar/critic subjobs
- Datasource picker
- Instructions editor

The host component (job-create or sessions-page) provides context-specific inputs:

```typescript
@Component({ selector: 'app-agent-settings' })
export class AgentSettingsComponent {
  /** The resolved expert config (merged with defaults). */
  expertConfig = input<Record<string, unknown> | null>(null);

  /** Framework defaults — always available, loaded on app init. */
  frameworkDefaults = input<Record<string, unknown> | null>(null);

  /** Which tabs to show. Sessions may hide "Instructions" or "Subjobs". */
  mode = input<'job' | 'session'>('job');

  /** Emits the config override whenever settings change. */
  configOverride = output<Record<string, unknown>>();
}
```

The host components remain responsible for their own context (description, title, files, project selector, etc.) and embed the shared settings component for everything config-related.

### Settings Page Layout — Job Creation

```
┌─────────────────────────────────────────────────────────────┐
│  Create New Job                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Project: [dropdown]           Priority: [dropdown]         │
│                                                             │
│  Description *                                              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                                                     │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  Opening Message (optional)                                 │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                                                     │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  Expert                                                     │
│  ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐        │
│  │ Dev   │ │Scholar│ │Critic │ │Custom │ │ ...   │        │
│  └───────┘ └───────┘ └───────┘ └───────┘ └───────┘        │
│                                                             │
│  Files                                                      │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  Drop files here or click to browse                 │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌──────────────────┐ ┌──────────────────────────────┐     │
│  │ ○ Settings       │ │                              │     │
│  │ ○ Instructions   │ │  (selected tab content)      │     │
│  │ ○ Advanced       │ │                              │     │
│  └──────────────────┘ └──────────────────────────────────┘  │
│                                                             │
│                              [Reset]  [Create Job]          │
└─────────────────────────────────────────────────────────────┘
```

### Settings Page Layout — Session Creation

The session creation dialog expands from its current minimal form into a proper settings page. Instead of a popup dialog, it becomes a full page (or a routable panel like job creation).

```
┌─────────────────────────────────────────────────────────────┐
│  New Session                                                │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Title                                                      │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                                                     │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  Projects                                                   │
│  ┌───────┐ ┌───────┐ ┌───────┐                             │
│  │Proj A │ │Proj B │ │Proj C │                             │
│  └───────┘ └───────┘ └───────┘                             │
│                                                             │
│  Expert                                                     │
│  ┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐                  │
│  │Interac│ │ Dev   │ │Scholar│ │Custom │                  │
│  └───────┘ └───────┘ └───────┘ └───────┘                  │
│                                                             │
│  ┌──────────────────┐ ┌──────────────────────────────┐     │
│  │ ○ Settings       │ │                              │     │
│  │ ○ Advanced       │ │  (selected tab content)      │     │
│  └──────────────────┘ └──────────────────────────────────┘  │
│                                                             │
│                            [Cancel]  [Create Session]       │
└─────────────────────────────────────────────────────────────┘
```

Key differences from job creation:
- No description/kickoff (sessions are conversational — the first message is the kickoff)
- No file upload (files can be shared through the chat)
- No instructions tab (the session's interactive nature replaces static instructions)
- No scholar/critic subjobs (not applicable to interactive sessions)
- Permission mode is a primary setting (supervised/auto_accept/autonomous)
- Session-specific settings: idle timeout, greeting, command allowlist

The Settings tab for sessions includes:

| Setting | Control | Notes |
|---------|---------|-------|
| Model | Dropdown | From `env.js`, not hardcoded |
| Permission Mode | Dropdown | supervised / auto_accept / autonomous |
| Autonomy | Dropdown | Resolved from config |
| Idle Timeout | Number input | Minutes before auto-suspend |
| Greeting | Text input | Custom welcome message |
| Datasources | Checkbox list | Only if datasources exist |

The Advanced tab mirrors the job creation Advanced tab (temperature, reasoning, tools, multimodal, etc.) via the shared `AgentSettingsComponent`.

### Relationship to Global User Settings

The global "Persistent Agent" section on the user settings page (`settings.component.ts`) currently stores user-level defaults for sessions. With this overhaul:

- **Global settings** set the user's preferred defaults (model, permission mode, idle timeout, greeting, command allowlist). These are read by the session creation form as the initial values — same role as framework defaults for jobs.
- **Per-session settings** override global defaults for a specific session. The resolution chain becomes:

```
per-session override  >  user persistent_agent settings  >  expert config  >  framework defaults
```

The global settings page stays as-is — it's the right place for "I always want supervised mode with a 2-hour timeout." The new session creation page is for "this particular session needs autonomous mode with a different model."

The bottom section uses a vertical tab bar (or an accordion/horizontal tabs — to be decided during implementation) with three panels:

#### Settings Tab (default, always visible)
The most commonly adjusted options:

| Setting | Control | Notes |
|---------|---------|-------|
| Model (Strategic) | Dropdown | Shows resolved default when unset |
| Model (Tactical) | Dropdown | Shows resolved default when unset |
| Model Presets | Chip row | Quick-select preset model combinations |
| Autonomy | Dropdown | Shows resolved default label |
| Scholar | Toggle | Pre-research phase |
| Critic | Toggle + rounds | Post-verification phase |
| Datasources | Checkbox list | Only if datasources exist |

Each field shows the resolved effective value. When user overrides a value, a subtle indicator (dot or "modified" label) appears so they know what they've changed vs. what's a default.

#### Instructions Tab
- Instructions textarea (monospace, 12+ rows)
- Clear / Reset to expert default buttons
- Builder AI integration (streaming edits)
- Warning when empty and expert is selected

#### Advanced Tab
Rarely-changed settings that most users never touch:

| Setting | Control | Notes |
|---------|---------|-------|
| Temperature (Strategic) | Slider | Shows actual resolved value, not 0 |
| Temperature (Tactical) | Slider | Same |
| Reasoning Level (Strategic) | Dropdown | Options adapt to selected model |
| Reasoning Level (Tactical) | Dropdown | Same |
| Multimodal (Strategic) | Toggle | Vision capability |
| Multimodal (Tactical) | Toggle | Same |
| Tool Categories | Toggle list | Research, Citation, Document, Coding |
| Project Memory | Toggle | Only if project supports it |
| View as JSON | Read-only code block | Shows resulting `config_override` |

### Presets System

Presets replace the config editor as the primary way to reuse settings.

#### Data Model

```typescript
interface SettingsPreset {
  id: string;
  user_id: string;
  name: string;
  description?: string | null;
  settings: PresetSettings;
  created_at: string;
  updated_at: string;
}

interface PresetSettings {
  config_name?: string;          // Expert ID
  config_override?: Record<string, unknown>;
  instructions?: string;
  autonomy?: string;
  priority?: number;
  datasource_ids?: string[];
  enable_scholar?: boolean;
  enable_critic?: boolean;
  critic_max_rounds?: number;
}
```

#### Backend Endpoints

```
GET    /api/presets              → list user's presets
POST   /api/presets              → save current settings as preset
GET    /api/presets/{id}         → get preset detail
PUT    /api/presets/{id}         → update preset
DELETE /api/presets/{id}         → delete preset
```

Storage: `job_presets` table in the app database.

#### UX Flow

- **Save**: After configuring settings, user clicks "Save as Preset" → names it → stored.
- **Load**: Preset selector (dropdown or chip row) above the settings tabs. Selecting a preset populates all fields. User can still tweak individual settings after loading.
- **Model presets vs. settings presets**: The existing `modelPresets` from `env.js` only set strategic/tactical model. Settings presets capture the full form state. Both can coexist — model presets as quick chips, settings presets as a dropdown.

### Override Change Indicators

When a user modifies a setting from its resolved default, the UI needs to communicate this clearly:

- **Unmodified**: Field shows resolved value, no indicator. The user sees "what they'll get."
- **Modified**: Small dot or accent-colored left border on the field. Tooltip: "Modified from default (X)."
- **Reset**: Per-field reset button (small "x" or "reset" link) to revert to resolved default.
- **Summary**: Footer or header shows "N settings modified" as an at-a-glance indicator.

This replaces the current approach where `buildConfigOverride()` silently diffs against expert defaults and the user has no visibility into what's being overridden.

## Implementation Plan

### Phase 1: Resolved Defaults (Foundation)
- Load `frameworkDefaults` on init (already partially done from the memory toggle fix)
- Replace all `?? 0`, `?? false`, `?? 'review'` fallbacks with `resolveDefault()` calls
- Temperature sliders, reasoning dropdowns, multimodal toggles, autonomy all show real values immediately
- No layout changes yet — just fix the data flow in the existing job-create component

### Phase 2: Shared Settings Component
- Extract model selection, reasoning, temperature, tools, autonomy, etc. into `AgentSettingsComponent`
- The component takes `expertConfig`, `frameworkDefaults`, and `mode` as inputs
- Emits `configOverride` whenever settings change
- Job-create embeds it, delegating all config-related UI
- Models loaded from `env.js` (single source, not hardcoded)

### Phase 3: Job Creation Layout
- Reorganize job-create into the tab layout (Settings / Instructions / Advanced)
- Move fields into their respective tabs
- Remove the "Advanced Options" expandable and the "Config Override Editor" expandable
- Add override change indicators (modified dot, per-field reset)

### Phase 4: Session Creation Settings
- Replace the minimal session dialog with a full settings page
- Embed `AgentSettingsComponent` with `mode='session'`
- Add expert selector (currently sessions have config dropdown but no expert cards)
- Wire up the resolution chain: per-session > user persistent_agent settings > expert config > framework defaults
- Session-specific fields: permission mode, idle timeout, greeting, command allowlist
- Backend: pass `config_override` through `threads.metadata.config_override` (already supported)

### Phase 5: Presets
- Backend: `agent_presets` table + CRUD endpoints (shared between jobs and sessions)
- Frontend: Preset selector dropdown, "Save as Preset" button
- Loading a preset populates all form fields
- Presets are per-user, stored server-side
- Preset type field distinguishes job vs. session presets (or presets are type-agnostic and store whatever settings were configured)

### Phase 6: Cleanup
- Remove `ConfigEditorComponent` (or keep as read-only JSON viewer in Advanced tab)
- Remove dual override merging logic (`buildConfigOverride()` + `configEditorOverrides`)
- Simplify `buildConfigOverride()` to diff against `resolveDefault()` uniformly
- Remove hardcoded model lists from sessions-page component

## Migration Notes

- The `ConfigEditorComponent` (`config-editor.component.ts`) is a schema-driven visual/JSON editor that parses `config/schema.json` to generate form fields. It's currently a separate expandable below "Advanced Options." It can be repurposed as the read-only "View as JSON" in the Advanced tab, but should not be the primary editing surface.
- Existing `env.js` model presets (`window.env.modelPresets`) continue to work as quick model chips. They're orthogonal to the new settings presets which capture the full form state.
- The `JobArtifactService` integration (builder AI editing instructions/description) is unaffected — it only touches the instructions and description fields, which remain in the same form.
- The global "Persistent Agent" section on the user settings page stays. It fills the same role as `defaults.yaml` does for jobs — user-level defaults that the per-session form shows as initial values.
- Session `config_override` is already supported in the backend via `threads.metadata.config_override`. The `POST /api/persistent/threads` endpoint already merges user settings into this field. The overhaul just gives the user a UI to set per-session overrides directly instead of relying on the global settings page.
- The hardcoded model list in `sessions-page.component.ts` (lines 96-111) is replaced by reading from `environment.models` — the same source job creation uses.

## Files Affected

### Cockpit (Frontend)
| File | Change |
|------|--------|
| `shared/components/agent-settings/` | **New** — shared settings component |
| `shared/components/job-create/job-create.component.ts` | Refactor to embed `AgentSettingsComponent`, remove inline settings |
| `simple/pages/sessions/sessions-page.component.ts` | Replace dialog with full settings page, embed `AgentSettingsComponent` |
| `shared/components/config-editor/config-editor.component.ts` | Demote to read-only JSON viewer or remove |
| `core/models/api.model.ts` | Add `SettingsPreset` model |
| `core/services/api.service.ts` | Add preset CRUD methods |
| `pages/settings/settings.component.ts` | No change (global persistent agent settings stay) |

### Orchestrator (Backend)
| File | Change |
|------|--------|
| `orchestrator/main.py` | Add preset CRUD endpoints |
| `orchestrator/database/schema.sql` | Add `agent_presets` table |
| `orchestrator/database/postgres.py` | Add preset CRUD queries |
