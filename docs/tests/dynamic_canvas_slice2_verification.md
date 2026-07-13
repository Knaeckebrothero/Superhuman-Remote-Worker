# Dynamic Canvas Slice 2 — implementation and local k3d verification

**Status:** Conditional file editing passed automated validation and a
two-replica local-k3d API smoke on 2026-07-13. The Cockpit editor passed its
component/service suites and production build. The final local deployment was
ready at two orchestrator replicas and one Cockpit replica, and the deployed
cross-origin response exposed both Canvas validators. A human browser pass
through the Monaco edit/conflict controls remains useful follow-up evidence.

**Feature:** `docs/features/dynamic_canvas.md`

## What this pass proves

- a supported workspace file can be republished as editable and reports
  `capabilities.can_edit=true` only on a writable backing;
- initial content, conditional content revalidation, conditional save, stale
  save, and source refresh use the documented state/content validators;
- a bodyless `304` no longer carries the representation `Content-Length` or
  triggers the prior Uvicorn short-response exception;
- two orchestrator replicas serialize identical save preconditions through the
  shared PostgreSQL advisory coordinator, so exactly one advances the Canvas;
- the saved workspace bytes, returned `source_version`, authorized content URL,
  and `X-Canvas-Content-ETag` agree;
- a missed browser control frame cannot leave an agent file-tool write
  authorized by only a stale path marker, because text reads and writes now
  compare the full-byte SHA-256 locally; and
- Cockpit preserves dirty bytes across the specified presentation conflicts and
  exposes the mutation headers in the supported cross-origin development setup.

This pass does not prove strict CAS against shell/process writes outside Canvas,
a CRDT, selection-to-message metadata, compiled LaTeX preview, live
applications, or shared browser behavior. It also does not replace a human
browser check of Monaco focus, keyboard, screen-reader, and narrow-screen
behavior.

## Delivered implementation surfaces

| Surface | Delivered location |
|---|---|
| Conditional state, file mutation coordination, and revision advancement | `orchestrator/services/canvas.py` |
| Authorized content `GET`/`HEAD`/`PUT`, source refresh, validators, and typed failures | `orchestrator/routers/canvases.py` |
| Bounded SFTP/shared-virtual materialization and writable adapters | `orchestrator/services/canvas_files.py` |
| Agent source-update and editing-awareness control handling | `src/api/persistent_app.py` |
| Recent-read byte-version guard for agent file writes | `src/tools/context.py`, `src/tools/workspace/files.py` |
| Cockpit mutation transport and reconnect-safe control delivery | `cockpit/src/app/core/services/canvas.service.ts`, `persistent-chat.service.ts` |
| Monaco editor, preview, dirty-buffer, and conflict state | `cockpit/src/app/views/canvas/` |
| Agent collaboration workflow | `config/skills/present-with-canvas/SKILL.md` |

## Local k3d execution record

Environment: context `k3d-srw`, namespace `srw`, thread `5432783a…`,
2026-07-13. Both orchestrator replicas and the Cockpit deployment were ready;
the unrelated MCP and OpenCloud pods remained crash-looping and are not part of
this acceptance claim.

The smoke reused the existing `test.md` source and wrote its exact existing
bytes, so the file content did not change. It was republished as editable and
the Canvas presentation revision advanced as part of the tested mutations.

| Check | Result |
|---|---|
| Editable internal set | **PASS** — `200`, editable state, and `can_edit=true`. |
| Initial authorized content | **PASS** — `200`; bytes retained SHA-256 prefix `f2548ec99c99`. |
| Conditional content GET | **PASS** — bodyless `304`, no representation `Content-Length`, and no matching ASGI/Uvicorn framing error in either replica log. |
| Conditional content save | **PASS** — `200`; new state/content validators agreed and the authorized readback matched byte-for-byte. |
| Stale old save | **PASS** — `409 canvas_presentation_changed`; the workspace bytes were not overwritten. |
| Conditional source refresh | **PASS** — `200`; full validation reran and the revision advanced. |
| Direct two-replica race | **PASS** — the same revision/hash preconditions sent concurrently to the two pod IPs produced exactly `[200, 409]`; the loser returned `canvas_presentation_changed`. |
| Cross-origin validator exposure | **PASS** — after the final rollout, an actual request with the supported `http://localhost:4200` origin returned that origin and exposed both `ETag` and `X-Canvas-Content-ETag` to browser JavaScript. |
| Final presentation | **PASS** — revision 5, editable, same source bytes. |

The smoke authenticated through the same delegated-owner path used by the
agent/Cockpit adapters. No credential value was printed or stored in this
record.

## Automated validation

- **Python:** 426 focused Canvas, tool-context, workspace-tool, delegated-reader,
  and persistent-runtime tests passed. The 13 warnings in the focused run are
  existing Python 3.14/deprecation and AsyncMock warnings.
- **Cockpit:** 1,030 tests passed. The final focused
  transport/Canvas/controller pass covered 166 tests.
- **Static/build:** focused Ruff check and format passed; Canvas Stylelint,
  English/German i18n parity and hardcoded-copy checks, skill validation, and
  `git diff --check` passed. The production Cockpit build completed with the
  existing initial-bundle and CommonJS warnings.

## Remaining acceptance work

1. In a real browser, edit Markdown and LaTeX/text, toggle preview, hide/reopen
   the pane, and confirm Monaco selection/scroll and unsaved bytes persist.
2. In two browser tabs, confirm editing awareness appears/expires and a save in
   one tab causes the other to reconcile after a control-WS reconnect.
3. Exercise `412`, republish, replacement, clear, and `423` UI notices while a
   dirty buffer is visible, then verify explicit “load current” is the only
   destructive action.
4. Rebind a current agent and observe the updated companion skill preserving a
   user edit through a fresh `read_file` and subsequent `set_canvas` republish.

These checks are acceptance follow-ups, not missing implementation. The
default-off Slice-3A callable/SSH foundation was implemented next and is
recorded in [[dynamic_canvas_slice3a_verification]]; the isolated viewer/proxy
and real-browser boundary remain the next user-facing Slice-3 work.
