---
tags:
  - feature
  - architecture
  - sessions
  - builder
  - orchestrator
  - deprecation
aliases:
  - replace builder with sessions
  - remove builder
  - builder deprecation
related:
  - "[[builder]]"
  - "[[dynamic_canvas]]"
  - "[[orchestrator_main_py_monolith]]"
  - "[[tool_implementation]]"
  - "[[two_graphs]]"
  - "[[agent_open_source_split]]"
---

# Builder Removal (→ Canvas later)

> Remove the builder now (it's unused and ships as the default chat surface right before the OSS release + pilots). **Park** its artifact-authoring machinery rather than delete it — the dynamic canvas will reuse it. Do **not** build an `instruction-author` session bridge; the capability comes back inside the [[dynamic_canvas]], not as a constrained session expert.

**Status:** Plan of record. Decided 2026-06-13.
**Supersedes:** the earlier plan in this doc (build an `instruction-author` expert + `promote-to-job` verb *before* deleting the builder). Dropped — nobody uses the builder, so there is no live workflow to preserve through a bridge; the real replacement is canvas-hosted collaborative job/expert creation.

## Why now, why this shape

- **Urgent / visible.** The builder is the shell's default view (`cockpit/.../shell.component.ts:93` renders `<app-instruction-builder/>`; root routes to the shell) — the "first chat tab." It's undocumented, the internal test students were already told it's deprecated, and it's barely used. It should not be the first thing OSS users and pilots see in the coming weeks.
- **Its only real delta over sessions is the artifact tools** (mutate `instructions`/`config_override`/`description` and stream them into a form). Those tools are under-tested and some don't work — we don't want to wire them into sessions as-is.
- **Canvas is the right home.** The builder's UX was always weak: it mutated cockpit form state in the background, with no clear signal of *which* field/window it changed. A Claude-Artifacts-style canvas tile makes job/expert creation a **visible, collaborative surface** — agent and user fill it together. [[dynamic_canvas]]'s v1 plan already lists "copy `JobArtifactService.applyToolCall()` + `BuilderStreamService` **verbatim** for `CanvasService`" — so parking that machinery feeds the canvas directly.
- **Decoupled timelines.** Removal ships on its own; canvas + job-builder-in-canvas is the ~2-week later track. Removal is **not** blocked on canvas.

## Current surface (verified 2026-06-13)

The consolidation doc's old inventory was stale (no `/builder` route anymore; BUILDER_* env already gone; no prompt config assets). Accurate map:

**The shared artifact-streaming layer (the thing that shapes the whole removal):**
```
instruction-builder.component (chat UI)
  → builder-stream.service.ts   (SSE → /api/builder/sessions/{id}/message)
    → JobArtifactService         (instructions/config/description/streaming signals)
      → job-create.component.ts            (form bound to artifacts.*)
      → agent-settings/instructions-tab.component.ts (streaming input)
```
`JobArtifactService` is **dual-purpose**: builder-AI sync target *and* the job-create form's own state container (`job-artifact.service.ts:42`). It cannot be deleted wholesale — job-create needs the form-state half.

**Cockpit:**
| Piece | Location | Disposition |
|---|---|---|
| Builder chat component (~1262 LOC) | `views/instruction-builder/` | **Delete** |
| Shell default render + sessions sidebar | `views/shell/shell.component.ts:93,406,463` | **Swap** default → sessions |
| ComponentRegistry registration | `app.ts:28,344`; `debug/layout.model.ts:32` | **Unregister** |
| Sidebar nav item | `shell/sidebar/sidebar.component.ts:46` (`nav.builder`) | **Remove** |
| `builder-stream.service.ts` | `core/services/` | **Park** (canvas seed) |
| `JobArtifactService.applyToolCall()` + `WorkspaceProposal` + `streaming`/`builderModel` | `core/services/job-artifact.service.ts` | **Park** the AI-sync half; **keep** form-state half |
| `job-create` + `agent-settings/instructions-tab` | `views/` | **Keep** as manual forms (drop AI-fill bindings) |
| `builderModels` signal / `builder_models` field | `core/services/model.service.ts:30,50,75` | **Rename → `chatModels`/`chat_models`** (NOT delete) |

**Orchestrator (`main.py` unless noted):**
| Piece | Ref | Disposition |
|---|---|---|
| 5 endpoints `/api/builder/sessions*` | `20635,20659,20679,20750,20759` | **Delete** |
| `BuilderSessionCreate`, `BuilderMessageRequest` | `3311,3785` | **Delete** |
| Second AI loop | `_create_builder_llm:21057`, `_summarize_builder_session:21220`, `_generate_builder_title:21256` | **Delete** |
| Service imports | `160,169,170,171,203` | **Delete** (after parking artifact tools) |
| `services/builder_{tools,prompt,config,search,dispatch}.py` | — | **Delete**, except **park** `builder_tools.py`'s 5 artifact-tool schemas + their `builder_dispatch` handlers |
| `get_builder_model` / `default_builder_model` | `167,3681,20795,20835,21235,21267` | **Delete** (builder-specific) |
| `builder_models` slice of `/api/models` | `17783–17883` | **Rename → `chat_models`** (it's the chat-capable list) |
| Postgres methods + table allowlist | `postgres.py:195–196, 7386+` | **Delete** |
| `builder_sessions` / `builder_messages` tables | `migrations/app/0001_initial.sql` (+ frozen `schema.sql`) | **Drop** via new migration |

**Config/env:** BUILDER_* already stripped from `helm/` (only `deployment/legacy/` reference YAMLs retain them — leave or scrub, non-functional). No `builder_prompt_matrix.yaml` / `config/prompts/builder_*` exist — the prompt is built in code by `builder_prompt.build_system_prompt`.

## Plan — ordered PRs

Risk-ordered so the urgent visible win lands first and reversibly, and the careful rename is isolated.

### PR 1 — Hide the builder (visible cut; ship first; trivially reversible)
- Make root land on **sessions** instead of the builder: stop rendering `<app-instruction-builder/>` in the shell (route `''` → sessions, or render the sessions view as shell default). The `chat → sessions` redirect already signals sessions as the primary surface.
- Remove the `nav.builder` sidebar item and the `instruction-builder` ComponentRegistry/`layout.model.ts` registration.
- **Leave all backend + the component file in place, just unwired** — pure "make it invisible." Delivers the pre-pilot outcome immediately; revert = one commit.
- Update README smoke-test step 1 ("lands on `/builder`" → "lands on sessions").

### PR 2 — Remove the builder frontend + split `JobArtifactService`
- Delete `views/instruction-builder/`.
- **Split `JobArtifactService`**: keep the form-state container (instructions/description/config signals job-create writes directly); **park** the AI-sync half (`applyToolCall`, `WorkspaceProposal`, `streaming`, `builderModel`) into `core/services/_parked/` (canvas seed).
- Park `builder-stream.service.ts` alongside it.
- Update `job-create` + `agent-settings/instructions-tab` to drop the streaming/AI-fill bindings — they remain working **manual** forms.

### PR 3 — Remove the builder backend
- Delete the 5 endpoints + 2 request models + the AI-loop helpers.
- Park `builder_tools.py`'s **5 artifact-tool schemas** (`update_/edit_/insert_instructions`, `update_config`, `update_description`) + their `builder_dispatch` handlers into `orchestrator/services/_parked/builder_artifact_tools.py`. Delete the rest of `builder_tools.py` (the ~85 operator/inspection schemas are redundant — they're the orchestrator's own REST API, still exposed via MCP) and `builder_{prompt,config,dispatch}.py`.
- **Verify `builder_search.tavily_search` has no other consumer**; if shared, keep it, else delete.
- Delete `get_builder_model` / `default_builder_model`.

### PR 4 — Untangle `builder_models` → `chat_models` (careful rename, isolated)
- `/api/models` field `builder_models` → `chat_models`; cockpit `model.service.ts` `builderModels` → `chatModels` + consumers.
- Resolve the `'builder'` model-role/slot in the capability classification (`admin-defaults.component.ts` "chat-slot kinds: chat/builder/browser/citation") — drop the `builder` slot or alias it to `chat`. Confirm no session/admin model-picker regresses.

### PR 5 — DB cleanup
- New migration `migrations/app/NNNN_drop_builder_tables.sql` dropping `builder_sessions` + `builder_messages` (low volume, unused → drop, not freeze). **Do not edit `schema.sql`** (frozen). Remove the postgres methods + allowlist entries.

## Park strategy & canvas hand-off

Parked, unwired, with a `README.md` pointing here + to [[dynamic_canvas]]:
- `cockpit/src/app/core/services/_parked/` — `builder-stream.service.ts` + the extracted `applyToolCall`/`WorkspaceProposal` sync logic. The canvas plan copies this shape into `CanvasService`.
- `orchestrator/services/_parked/builder_artifact_tools.py` — the 5 artifact-tool schemas + dispatch handlers, the seed for the canvas's job/expert authoring operations.

These are reference/reuse, not live code. Alternative (rely on git history) was rejected — the owner asked for an explicit parked folder so the canvas work has a visible starting point.

## Deferred to canvas (out of scope here)

How the builder's capability returns inside [[dynamic_canvas]] is a **separate design**, not part of this removal. The open fork (record only):
- **(a) a structured `form` canvas kind** — schema-driven form the agent populates + user edits + a submit action dispatches the job/expert; generalizes beyond job creation.
- **(b) a bespoke `job-builder` tile** in the canvas grid — reuses grid/lock/awareness plumbing, keeps a hand-built form component (closest to today's UI, minus the invisible-mutation problem since it's a visible tile).

Decide when canvas v1 (steps 1–4 in `dynamic_canvas.md`) is stable.

## Acceptance criteria

1. Fresh login lands on sessions; no builder tab, nav item, or route.
2. `/api/builder/*`, `BuilderSessionCreate`/`BuilderMessageRequest`, the second AI loop, and `builder_{tools,prompt,config,dispatch}.py` are gone; artifact tools + dispatch handlers preserved under `_parked/`.
3. `job-create` and `agent-settings/instructions-tab` still create/edit manually (no AI-fill, no console errors from a missing builder backend).
4. `builder_models` renamed to `chat_models` end-to-end; session/admin model selection unaffected.
5. `builder_sessions`/`builder_messages` dropped via migration; no orphaned postgres methods.
6. README smoke-test + this doc reflect the new landing; the canvas seed is parked and pointed at from [[dynamic_canvas]].

## Risks / open checks

- **`JobArtifactService` split** is the main scope risk — job-create binds tightly to its signals. Land PR 2 behind a quick manual job-create smoke test (create a job with instructions + config override, no AI assist).
- **`tavily_search`** sharing — verify before deleting `builder_search.py`.
- **`'builder'` model-slot** removal must not drop chat-capable models from session pickers (PR 4 smoke test).
- **Default-landing swap** — confirm whether the shell renders sessions inline or root should `redirectTo: 'sessions'`; pick the one that keeps the sessions sidebar behavior intact.
- Table **drop vs freeze** — drop assumes negligible historical value; if any pilot data exists, switch to a `provenance`-tagged archive instead.
