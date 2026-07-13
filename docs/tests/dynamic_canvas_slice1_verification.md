# Dynamic Canvas Slice 1 — local k3d verification

**Status:** Primary file presentation passed on local k3d on 2026-07-13. The
bodyless-`304` framing defect found in that pass was fixed and live-reverified
during Slice 2 later the same day.

**Feature:** `docs/features/dynamic_canvas.md`

## What this pass proves

The live pass covered a durable virtual workspace, not a test filesystem:

- an existing thread can be rebound after a Canvas deployment and receive a
  durable virtual workspace generation;
- the current agent image loads the three Canvas tools and deploys the bundled
  `present-with-canvas` skill;
- the internal delegated-user adapter validates and presents an existing
  workspace file;
- public owner-authorized state and content routes expose the same file and
  source hash to Cockpit; and
- Cockpit renders the Markdown stage beside the conversation.

This was not a live test of Slice 2 editing, Slice 3 applications, a browser
source, or autonomous model selection of the skill. The final mutation was
driven through the authenticated internal adapter used by `set_canvas`; runtime
logs prove the model tool schemas and skill were loaded, but the rebound agent
has not yet produced a model-originated `set_canvas` call.

## Original failure and repair

The first attempt used thread
`5432783a-8fcb-4b11-8968-f2d4f236e397`. Its running agent predated Slice 1 and
had neither Canvas tools nor the companion skill, so the model could only read
`test.md` and explain that no Canvas command was available. The Cockpit was not
the blocker: no `set_canvas` tool call or Canvas state existed for it to render.

At the same time, the orchestrator rollout was mixed and stalled. A new replica
correctly refused startup because the repository copy of applied migration
`0058_canvases.sql` no longer matched the checksum in `schema_migrations`.
Serving traffic continued to hit older replicas, and the old agent could not
hot-load a tool registry from a newer image.

The forward-only repair was:

1. Restore `0058_canvases.sql` byte-for-byte to its applied SHA-256
   `e04eb6a4e27a120ec86682226b3cfa9c6abeeeb64d53b9781a45ae83ef11cff5`.
2. Keep the newer `threads.events_epoch` catalog comment in
   `0060_canvas_events_epoch_comment.sql`, after
   `0059_docker_workspace_leases.sql`.
3. Pin the immutable `0058` checksum in the Canvas migration test and exclude
   that applied file from new Squawk annotations instead of editing it again.
4. Let Tilt rebuild and roll the chart; do not patch the database migration
   ledger or Kubernetes Deployment by hand.
5. Release and re-prepare the stale session agent so the existing thread uses
   the current image. Reattach also backfills the deterministic durable-virtual
   workspace binding.

## Execution record

Environment: context `k3d-srw`, namespace `srw`, 2026-07-13. Tilt image tags
below are evidence from this run, not stable release identifiers.

| Check | Result |
|---|---|
| Orchestrator rollout | **PASS** — 2/2 desired, updated, ready, and available; both replicas ran `srw-orchestrator:tilt-32805a327a77cfe2`; both service endpoints were ready and non-terminating. |
| Migration ledger | **PASS** — `0058`, `0059`, and `0060` were recorded successful with repository-matching checksums. No checksum row was rewritten. |
| Existing-thread upgrade | **PASS** — thread `5432783a…` became active with a durable `virtual` workspace binding on reattach. |
| Current agent | **PASS** — `srw-agent-s-85dcd09d` ran `srw-agent:tilt-4638930c5f03bd1e` and reported `supports_canvas=True`. |
| Tool registry | **PASS** — logs showed `get_canvas`, `set_canvas`, and `clear_canvas` loaded; 75 total tools were loaded. |
| Companion skill | **PASS** — the agent deployed `skills/present-with-canvas/SKILL.md` and `agents/openai.yaml`. |
| File presentation | **PASS** — internal set returned `200` for `test.md`, title `Test Document`, renderer `markdown`, revision 1. |
| Owner state/content | **PASS** — public state and initial content returned `200`; the content SHA-256 matched source version `f2548ec99c9994bb5d18b6880437576b46e30c0e164430c35bb68ec73d633458`. |
| Cockpit | **PASS** — the user confirmed the Canvas pane rendered the document. |
| Conditional content revalidation | **PASS after Slice-2 follow-up** — the same authorized request returned a bodyless `304` without the representation `Content-Length`; neither orchestrator replica logged the prior ASGI/Uvicorn framing exception. |

The unrelated `srw-opencloud` pod was already crash-looping during this pass;
do not describe the entire cluster as healthy. It did not participate in Canvas
state, virtual file delivery, or Cockpit rendering.

## Automated validation

- `199 passed, 4 skipped` across focused Canvas state/file/tool, Docker
  inventory, session provisioning, and configuration tests.
- Fresh replay of all application migrations through `0060`, followed by a
  clean `scripts/schema-snapshot.sh app --check`.
- Ruff check/format, `git diff --check`, and pinned Squawk validation passed.
- The broader Slice-1 pass also completed the 1,007 Cockpit tests, production
  build, i18n checks, changed-Canvas style checks, Helm overlays, Compose
  rendering, workspace shell syntax, and skill validation. Full repository
  Stylelint retains unrelated baseline failures.

## Re-verification after the 304 fix

The 2026-07-13 Slice-2 follow-up completed the original framing checklist on the
real ASGI server:

1. Initial state and content fetch return `200`, and the bytes hash to the
   stored `source_version`.
2. Repeating the content fetch with the returned `If-None-Match` produces a
   bodyless `304` without a representation `Content-Length` and without an
   orchestrator/Uvicorn exception.
3. Same-source `set_canvas` refresh advances the presentation revision and
   Cockpit refreshes without losing chat state or focus.
4. A fresh/rebound agent loading the tools and companion skill was already
   established by the original pass. A further model-originated editing pass is
   tracked in [[dynamic_canvas_slice2_verification]].

Editing was implemented and separately verified in
[[dynamic_canvas_slice2_verification]]. Live applications, multi-port routing,
and shared browser acceptance remain in Slices 3–5 and are not implied by this
Slice-1 record.
