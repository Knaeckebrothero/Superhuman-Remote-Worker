# Parked builder artifact-streaming machinery

These files are **reference snapshots, not live code.** They are excluded from
the Angular build (`tsconfig.app.json`) and the vitest run (`vitest.config.ts`)
via the `_parked/` glob, so they are never compiled, type-checked, or tested.

They are the cockpit half of the now-removed **instruction builder** — the
AI-driven artifact authoring that streamed `instructions` / `config` /
`description` mutations into the job-create form. The builder was removed in the
builder → sessions consolidation; this machinery is preserved verbatim as a
reference for future structured job/expert draft actions that may be presented
on the **Dynamic Canvas**. The pointer-based Canvas control plane itself does
not reuse this deleted Builder session loop.

## Contents

- `builder-stream.service.ts` — SSE client for the (deleted) `/api/builder/...`
  endpoints; parses `token` / `tool_call` / `workspace_proposal` events and
  forwards tool calls to the artifact service.
- `job-artifact.service.ts` — the FULL pre-split `JobArtifactService`, including
  the AI-sync half (`applyToolCall`, `WorkspaceProposal` plumbing, `streaming`,
  `builderModel`, builder session tracking). The live
  `core/services/job-artifact.service.ts` keeps only the form-state half
  (`instructions` / `config` / `description` / `reset`).
- `builder-stream.service.spec.ts` — its original unit test.

## Reuse

If a later structured `form` or `job-builder` canvas renderer revives these
mutation patterns, treat them as design reference and port only the applicable
state transitions into the shared application-action layer. Do not reconnect
the deleted `/api/builder/...` SSE flow or copy it into the core Canvas service.
Import paths here were shifted one directory level (`../`) when parked and the
snapshot is intentionally outside compilation, so any reused code must be
adapted and tested as new production code.

See:

- [`knowledge-base/knowledge/features/builder_to_sessions_consolidation.md`](../../../../../knowledge-base/knowledge/features/builder_to_sessions_consolidation.md) — why this was parked.
- [`knowledge-base/knowledge/features/dynamic_canvas.md`](../../../../../knowledge-base/knowledge/features/dynamic_canvas.md) — where it goes next.
