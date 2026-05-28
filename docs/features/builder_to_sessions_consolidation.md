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
  - unified chat surface
  - lightweight sessions
related:
  - "[[builder]]"
  - "[[orchestrator_main_py_monolith]]"
  - "[[cloud_collaboration_model]]"
  - "[[job_cloud_export]]"
  - "[[two_graphs]]"
  - "[[headless_persistent_sessions]]"
  - "[[dynamic_canvas]]"
  - "[[ephemeral_workspaces]]"
  - "[[agent_open_source_split]]"
---

# Builder → Sessions Consolidation

> One chat surface, not two. The orchestrator orchestrates; the agent answers chats. Anything the builder can do, a session can do better — once we add the one verb it's missing.

**Status:** Concept. No work started.
**Filed:** 2026-05-28
**Supersedes (eventually):** [`builder.md`](builder.md)

## Motivation

Two concerns push in the same direction.

**1. The builder is the orchestrator's second AI loop.** `orchestrator/services/builder_*.py` (prompt, tools, config, search, dispatch) + the `builder_sessions` / `builder_messages` tables + the SSE endpoints in `main.py` re-implement what `src/api/persistent_app.py` + `src/persistent_graph.py` already do: streaming chat, model resolution, tool dispatch, message persistence, summarization. The orchestrator is supposed to *orchestrate* — assigning work, gating approvals, tracking state — not host a chat agent. The chat agent lives in `src/`.

**2. The builder's tool set is a strict subset of what a session can do.** Anything that grows on sessions — narration (`session_narration.md`), smart turn merging (`session_turn_rendering.md`), the BFF / cookie auth (`auth_bff_and_api_tokens.md`), the dynamic canvas (`dynamic_canvas.md`), headless / eager mode — does not automatically reach the builder, and the gap widens every release. Conversely, if sessions match the builder's UX, the user has no reason to choose the builder; it becomes a parallel code path with no remaining users.

Consolidation removes ~2k LOC from `main.py` (helps [[orchestrator_main_py_monolith]]), kills a 1262-line cockpit component, and unblocks the next direction the user wants: **lightweight session tiers** (no SSH workspace, just cloud-file IO + a Python sandbox).

## What the builder is today

Quick inventory so the deletion list at the end of the plan is concrete.

| Surface | Files / endpoints |
|---|---|
| Backend services | `orchestrator/services/builder_prompt.py`, `builder_tools.py`, `builder_config.py`, `builder_search.py`, `builder_dispatch.py` |
| Persistence | `builder_sessions` + `builder_messages` tables; ~6 `postgres_db.*builder_session*` methods |
| API | `/api/builder/sessions` (CRUD), `/api/builder/sessions/{id}/message` (SSE), `BuilderSessionCreate`, `BuilderMessageRequest` |
| System prompt | `orchestrator/config/builder_prompt_matrix.yaml` + per-model variants under `orchestrator/config/prompts/builder_*` |
| Model resolution | `BUILDER_MODEL`, `BUILDER_LLM_PROVIDER`, `BUILDER_API_KEY`, `BUILDER_BASE_URL` env vars; `get_builder_model()`, `get_builder_base_url()`, `default_builder_model`; the `builder_models` slice of `/api/models` |
| Cockpit | `cockpit/src/app/views/instruction-builder/instruction-builder.component.ts` (1262 LOC), `core/services/builder-stream.service.ts` (265 LOC), `core/services/job-artifact.service.ts` (artifact bidirectional sync) |
| SSE events | `token`, `tool_call`, `tool_executing`, `tool_result`, `workspace_proposal`, `step`, `done`, `error` |
| Artifact | `instructions.md` + `config_override` JSON + `description` — injected fresh into the system prompt each turn |
| Default landing | `https://localhost/` lands on `/builder` after login (per the README smoke-test path) |

What the builder *does* that sessions don't, today:

- **Mutates a structured artifact** (`instructions` / `config_override` / `description`) via tool calls that the cockpit sync-replays into form signals.
- **Promotes the artifact into a `JobStartRequest`** (the "launch" button at the end of the chat).
- **Active-job / active-project context** is injected into the system prompt and inspection tools default to it.

Everything else (chat, streaming, model selection, tools, thinking capture, summarization) overlaps with sessions.

## Plan: four reversible steps, deprecation last

Order matters. We don't delete `builder_*.py` until we've proven a session-based replacement covers the workflow.

### Step 1 — `instruction-author` expert config

A new entry under `config/experts/instruction-author.yaml` that:

- Inherits from `persistent_defaults.yaml` (`$extends`).
- Constrains the tool set to a curated subset: artifact-write tools (`write_instructions`, `update_config`, `set_description`), inspection tools (`get_job`, `list_jobs`, `query_table` read-only, `search_knowledge`), and a search tool (replacing `builder_search.tavily_search`).
- Loads the builder's existing system prompt verbatim as the base persona (translated from the `builder_prompt_matrix.yaml` content into `config/prompts/persona_instruction_author.md`).
- Defaults to the same model the builder uses today (`BUILDER_MODEL` becomes a session-default override for this expert config).

**Exit criterion:** spin up a session with `expert: instruction-author`; the chat experience is comparable to today's builder for the canonical "help me write instructions for a research job" workflow.

This step is reversible — it adds files, deletes nothing.

### Step 2 — Promote workspace → `JobStartRequest`

The one capability sessions lack. A session running `instruction-author` writes:

```
workspace/
  instructions.md
  config.yaml          (or config_override.yaml)
  description.md
```

A new endpoint — `POST /api/sessions/{thread_id}/promote-to-job` — reads those files, validates them, and dispatches a `JobStartRequest` against the configured project. Returns the new `job_id`. The cockpit gets a "Launch Job" affordance in the session UI when these files exist.

This is structurally identical to the `job_cloud_export.md` accept path that shipped 2026-05-21: read files from a workspace, validate, materialize a downstream artifact. Same etag / external-mod gate model fits.

**Exit criterion:** a session can be authored end-to-end (chat → instructions written → job dispatched) without touching `/builder/*`.

### Step 3 — Deprecate the builder

Done in one PR, behind a feature flag if we want a soft rollout:

- Route `/builder` in the cockpit to a session with `expert: instruction-author` (auto-create on first visit, preserve last-used thread).
- Freeze `builder_sessions` / `builder_messages` read-only; expose them via a "legacy builder sessions" archive view, or migrate them into `persistent_threads` with a `provenance: 'builder'` marker. (Decide based on volume — likely an archive view is enough.)
- Delete `orchestrator/services/builder_*.py`, the `/api/builder/sessions*` endpoints, `BuilderSessionCreate`/`BuilderMessageRequest`, the `BUILDER_*` env vars, and the `builder_models` slice of `/api/models`.
- Delete `cockpit/src/app/views/instruction-builder/`, `core/services/builder-stream.service.ts`. Keep `JobArtifactService` only if step 1's session UI reuses it for the form sync.

Expected diff: ~2k LOC out of `orchestrator/main.py`, ~1500 LOC out of the cockpit, ~500 LOC of services and migrations marked dead. The README smoke-test path's "lands on `/builder`" becomes "lands on `/sessions/new?expert=instruction-author`".

### Step 4 — Lightweight session tier + code-exec sandbox

Only after step 3 lands. Two pieces:

- **Lightweight sessions.** A session mode that doesn't provision an SSH workspace pod — file IO goes directly to OpenCloud, no `paramiko`, no per-session container. Cheaper, faster to spin up, suitable for "just chat with my files" workflows (including instruction authoring after step 1).
- **Python execution sandbox.** A bounded code-exec environment the session can call into for one-off computations. Options range from a per-session sidecar container to a shared, ephemeral exec service. Out of scope for v1 of this consolidation; sketched here because it's the user-articulated long-term direction.

The open design question for this step is significant enough that it gets its own section.

## Open design question — what does "no workspace" mean?

Two implementations look similar from outside but have very different maintenance profiles.

### Option A — Cloud-backed workspace backend

Add a `backend: cloud` workspace mode alongside `remote` (SSH/SFTP). `WorkspaceManager` proxies all file IO to OpenCloud directly. The LangGraph state machine is unchanged: phases, todos, archive, todo-yaml — all still operate, just on cloud-backed files instead of SSH-mounted ones.

**Pros:** One graph to maintain. Reuses everything (phase alternation, archive, checkpoint resume, stuck detection). Per [[two_graphs]], the dual-graph cost is already real — no new graph to add.

**Cons:** You still pay for the full state machine on tasks that don't need phases. A 3-turn "fix this typo in my instructions" session runs `init_workspace` → `execute` → `tools` → `check_todos` → `archive_phase` → `handle_transition` → `check_goal` just to write one file.

### Option B — A second (lighter) agent loop

A chat-only agent loop with no phase alternation, no todos, no archive. Just LLM-with-tools in a streaming loop. Lives next to `graph.py` / `persistent_graph.py` as a third entry point.

**Pros:** Cheap at runtime. Latency budget matches today's builder. Less to summarize / persist per turn.

**Cons:** A third graph in a codebase where [[two_graphs]] already complains about two. Every new session capability has to be ported in three places now.

**Recommendation:** **Option A.** The runtime overhead of phase machinery on short tasks is real but small (the LLM call dominates). The maintenance overhead of a third graph is large and recurring. If short-task latency becomes a measured problem, the right fix is a "skip phases if `max_phases <= 1`" config flag inside the existing graph, not a parallel graph. Decision deferrable until step 4 is actually queued.

## Risks & migration concerns

- **Latency UX regression.** Today's builder feels snappy because it's chat-in-the-orchestrator. A session has to provision a pod (or warm-pool wait). Step 4 / Option A largely fixes this, but step 3 lands before step 4. Possible mitigation: warm-pool the `instruction-author` expert specifically, or default it to `backend: cloud` once available.
- **Existing `builder_sessions` data.** Decide between archive-view vs. migration into `persistent_threads`. Probably archive-view (low volume, low value of historical lookups).
- **Curated vs. full tool set.** The session UI shows whatever tools the expert config exposes. If `instruction-author` is too constrained, users will switch to a regular session and lose the curated artifact UX. If it's too open, it's no longer "an instruction builder." Step 1's exit criterion is the gate — the curated set needs to *feel* like the builder, not be a stripped-down general agent.
- **Deep links to `/builder/{id}`.** Cockpit deep links + saved tabs need a redirect (`/builder/{id}` → `/sessions/{id}` with the legacy session ID resolved via `provenance`).
- **Workspace-edit approval flow.** The builder has its own `workspace_proposal` SSE event for path-validated writes. Sessions need an equivalent approval gate or the `instruction-author` expert needs to skip it (cheap-to-revert artifact edits, not real workspace mutations).
- **`config_override.yaml` shape.** Today the cockpit `JobArtifactService` parses `config_override` as a typed JSON object with form-bound fields. The session-workspace version writes a YAML file the LLM authored freely. The promote endpoint needs to validate it against the same schema or sessions will produce job-start payloads the orchestrator rejects.

## Why this lands well in the current codebase

- **`main.py` is already being broken up.** This deletes ~2k LOC of the monolith without touching the dispatcher or jobs APIs — a clean cut.
- **The cloud-mirror workspace pattern is shipped.** Step 2's "promote workspace files to downstream artifact" is a copy of `job_cloud_export.md`'s accept path. Same shape, smaller surface.
- **The OSS split design needs this.** [[agent_open_source_split]] argues `src/` + `agent.py` + `config/` could be open-sourced if the orchestrator coupling is HTTP-only. The builder violates that — it's an AI loop *inside* the orchestrator. Consolidating onto sessions makes the OSS split cleaner.
- **No new infra.** No new tables (or one tiny one for `provenance` if we migrate). No new services. No new containers. Just files moving between directories and ~3.5k LOC deleted.

## Out of scope

- Multi-tenant model selection / per-org instruction-author defaults (M1 → M2 territory in [[multi_tenancy]]).
- A second "operator" or "admin" expert config — out of scope for this consolidation; same architectural shape if we want one later.
- Code-exec sandbox design — flagged as step 4 direction but deserves its own doc when queued.

## Acceptance criteria (when this work is "done")

1. `config/experts/instruction-author.yaml` exists and produces a chat experience comparable to the current `/builder` for the canonical research-job authoring workflow.
2. `POST /api/sessions/{thread_id}/promote-to-job` dispatches a valid job from a session workspace's `instructions.md` + `config.yaml` + `description.md`.
3. `/builder` route in the cockpit redirects to or auto-creates a session with `expert: instruction-author`.
4. `orchestrator/services/builder_*.py`, `/api/builder/sessions*`, `BUILDER_*` env vars, and the `instruction-builder` cockpit component are deleted.
5. The README's smoke-test step 1 is updated to reflect the new landing page.
6. No regression in the canonical workflow as measured against the pre-deprecation UX.
