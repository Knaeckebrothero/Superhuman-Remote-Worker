# Visual walk

Captures the cockpit at fixed routes × both themes × desktop/mobile into
`playwright-report/visual-walk/<label>/`. It is a *review aid*: the PNGs are
looked at by a person; the spec only asserts structure (see the end of
`walk.spec.ts`). Never turn this into a pixel-diff — live data makes that flaky.

    VISUAL_WALK_LABEL=slice2 npm run test:e2e:visual-walk
    VISUAL_WALK_LOCALE=de-DE VISUAL_WALK_LABEL=slice2-de npm run test:e2e:visual-walk

Env: `VISUAL_WALK_BASE_URL` (default https://localhost), `VISUAL_WALK_USER` /
`VISUAL_WALK_PASSWORD` (default test/test, the local k3d stack's user),
`VISUAL_WALK_LOCALE` (`en-US` default, or `de-DE`; applied as the browser
context locale — the app resolves its language from `navigator.languages`).

Design context: `knowledge-base/knowledge/features/cockpit_modern_visual_refresh.md`
§7 (gates) and the plan next to it.

## Overflow probe

`overflow-probe.cjs` is the companion sweep for polish work: the same login and
routes, both viewports, plus the rail menus — and on every page it runs an
in-page probe that lists elements past the viewport edge, content spilling out
of its box, and text clipped without an ellipsis (`probe.json`), alongside the
PNGs and any console errors. Read `probe.json` first, then only the screenshots
it points at.

    node e2e/visual-walk/overflow-probe.cjs
    PROBE_ROUTES=/jobs,/projects PROBE_MENUS=0 node e2e/visual-walk/overflow-probe.cjs
    PROBE_THREAD=<thread id> PROBE_PROJECT=<project id> node e2e/visual-walk/overflow-probe.cjs

Env: `PROBE_BASE`, `PROBE_OUT` (default `playwright-report/overflow-probe/`),
`PROBE_ONLY=desktop|mobile`, `PROBE_USER` / `PROBE_PASSWORD`, `PROBE_LOCALE`.
Known noise is listed at the top of the script. The dev server keeps serving the
old bundle when a rebuild fails, so check the `ng serve` log for `✘ [ERROR]`
before trusting a capture.
