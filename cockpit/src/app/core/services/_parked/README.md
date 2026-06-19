# Parked builder artifact-streaming machinery

These files are **reference snapshots, not live code.** They are excluded from
the Angular build (`tsconfig.app.json`) and the vitest run (`vitest.config.ts`)
via the `_parked/` glob, so they are never compiled, type-checked, or tested.

They are the cockpit half of the now-removed **instruction builder** — the
AI-driven artifact authoring that streamed `instructions` / `config` /
`description` mutations into the job-create form. The builder was removed in the
builder → sessions consolidation; this machinery is preserved verbatim because
the **Dynamic Canvas** will reuse it.

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

The dynamic canvas plan copies `JobArtifactService.applyToolCall()` +
`BuilderStreamService` into `CanvasService`. Import paths here were shifted one
directory level (`../`) when parked — fix them on reuse.

See:

- [`docs/features/builder_to_sessions_consolidation.md`](../../../../../docs/features/builder_to_sessions_consolidation.md) — why this was parked.
- [`docs/features/dynamic_canvas.md`](../../../../../docs/features/dynamic_canvas.md) — where it goes next.
