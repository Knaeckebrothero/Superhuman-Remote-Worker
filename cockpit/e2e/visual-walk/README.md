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
