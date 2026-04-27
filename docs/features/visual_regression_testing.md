# Visual Regression Testing for Cockpit Primitives

## Problem

Vitest covers behavior and logic — "this button calls `onClick`", "this badge renders the right tone class" — but runs in jsdom with no real layout, no fonts, no CSS computation. The test suite cannot see:

- Padding shrinking after a token edit
- Text overflowing its container at narrow widths
- Light-mode regression where a token resolves to white-on-white
- A flexbox child wrapping unexpectedly after a sibling grew
- Focus rings vanishing because outline got reset
- Icons rendering as broken `.notdef` boxes when the font fails (the bug we just fixed in the Material Symbols loader)

The Phase 5 migration just locked the cockpit onto a closed set of 21 primitives across 5 large pages. Light mode shipped recently. There is no automated mechanism today to catch a CSS-token edit that visually breaks a component that the test suite will still report as green.

The next planned change — restructuring the `ui/` / `simple/` / `shared/` / `layout/` folder topology — moves the surface area without changing what users see. That is precisely the situation a visual regression suite is for: a refactor with no behavioural delta should produce zero pixel diffs.

## Goal

Lock the visual contract of the cockpit so that:

1. Every primitive in `cockpit/src/app/ui/` has a stable per-variant pixel baseline.
2. Both light and dark themes are covered automatically on every CI run.
3. A folder restructure that touches imports but not output produces a 0-diff result.
4. A token change (palette, spacing, radius) shows every component that visually moved.
5. The suite runs in a pinned environment so cross-machine font hinting and subpixel rounding don't cause flakes.

Non-goals for v1:

- Per-page visual coverage. Pages are too large and change too often; we cover them via primitive composition, not whole-screen snapshots.
- Cross-browser matrix. Chromium-only. Firefox/WebKit can be added later if we hit a vendor-specific regression.
- Designer-facing review UI. No Storybook, no Chromatic. Diffs are reviewed in the PR like any other artifact.

## Approach: Playwright Component Testing

Three options were considered. Summary of the tradeoff:

| Option | Per-primitive isolation | Cost | Maintenance | Designer review UX |
|--------|------------------------|------|-------------|---------------------|
| Storybook + Chromatic | Excellent | ~$149/mo + parallel build pipeline | Stories drift from real usage | Web UI for accept/reject |
| Playwright page snapshots | Poor (per-screen, not per-component) | Free | Pages are noisy — every page change rerenders multiple components | PR diff artifacts |
| **Playwright component testing** | Excellent | Free | Stories live next to components | PR diff artifacts |

The middle option is the sweet spot. `@playwright/experimental-ct-angular` (or the stable `@analogjs/vitest-angular` + `@vitest/browser` route, which Angular 21 supports natively) mounts individual components in a real browser headlessly. It gets Storybook-style per-variant granularity without Storybook's parallel build pipeline and without Chromatic's monthly fee. Snapshots are PNGs committed to the repo, reviewed in PRs as binary diffs with a CI-attached visual diff artifact.

## How It Works

The loop:

1. **Author a story** — a small spec file enumerates the variants of a primitive (e.g. button: `primary | secondary | ghost` × `sm | md | lg` × `default | disabled | loading`).
2. **Render** — Playwright mounts each variant in a fixed viewport (e.g. 800×400) inside a chromium instance.
3. **Snapshot** — `await expect(page).toHaveScreenshot('button-primary-md-default.png')` captures the pixel state.
4. **Compare** — On subsequent runs, the new render is diffed against the committed PNG with a configurable `maxDiffPixels` threshold (default: 0).
5. **Decision** — Pass if identical (or within threshold). Fail if not. The PR run uploads side-by-side PNGs (expected, actual, diff) as build artifacts.
6. **Accept** — When a diff is intentional, the author runs `npx playwright test --update-snapshots` locally, commits the new PNG, and the PR contains both the code change and the visual delta for review.

Each variant is also rendered twice — once with `body.theme-dark`, once with `body.theme-light` — under the same story, producing a paired light/dark baseline.

## Variant Matrix

The full primitive set (21 components) collapses to roughly 60–80 stories, since not every primitive has 9 variants:

| Primitive | Stories (approx.) | Notes |
|-----------|-------------------|-------|
| button | 18 | 3 variants × 3 sizes × 2 states (default, disabled). Loading state separately. |
| input | 12 | 4 types × 3 states (default, focused, error). Focused state requires keyboard input. |
| select | 6 | open + closed × 3 states |
| checkbox / switch | 6 | checked, unchecked, disabled — each in sm + md |
| textarea | 4 | empty, filled, error, disabled |
| form-field | 4 | with/without label, with/without hint, error |
| badge | 12 | 4 tones × 3 sizes (xs, sm, md) — solid + soft appearance |
| icon / icon-button | 4 | smoke test that font-loaded state renders glyphs |
| spinner | 2 | sm + md |
| dialog / panel | 4 | open, with-title, with-actions, scrollable body |
| tooltip | 2 | top, bottom positioning |
| **Total** | ~60 | Each rendered light + dark = ~120 PNGs |

A baseline of ~120 PNGs at typical sizes is roughly 2–4 MB committed to the repo. Acceptable.

## Repository Layout

```
cockpit/
  tests/
    visual/
      __snapshots__/                  # committed PNG baselines (one folder per spec)
      button.visual.spec.ts
      input.visual.spec.ts
      ...
      _harness/
        mount.ts                      # shared mount helper, sets viewport + theme
        themes.ts                     # iterator over theme-dark / theme-light
  playwright.config.ts                # pinned browser revision, deterministic settings
```

Snapshots are platform-specific by design — Playwright stores them under `__snapshots__/<spec>-<browser>-<platform>/`. We standardize on `linux` baselines (the CI runner) and skip local-OS overrides to avoid two sets of truth.

## Determinism

The hardest part of visual regression is suppressing legitimate noise. The configuration must:

- **Pin the browser**: Playwright has its own bundled chromium, version-locked via `package.json`. No system Chrome.
- **Pin the runner OS**: CI uses the same Ubuntu image (the existing GitHub Actions runner). Local devs accept-snapshot only after running in the Docker harness, not against their host renderer.
- **Disable animations**: Global CSS injection sets `*, *::before, *::after { animation-duration: 0s !important; transition-duration: 0s !important; }` during snapshot runs. Phase-1 tests don't try to capture animated states.
- **Wait for fonts**: The `body.fonts-loaded` class added in `index.html` for Material Symbols also gates the snapshot — the harness waits on `document.fonts.ready` before each capture.
- **Disable cursor / caret**: `caret-color: transparent` and `cursor: none` to remove the blinking insertion point in input fields.
- **Stub the date**: Any primitive that renders a relative timestamp (badges with "5 minutes ago") gets a fixed `Date.now()` via `page.clock`.
- **Threshold**: Default `maxDiffPixels: 0`. Per-spec opt-in to small thresholds (`maxDiffPixels: 50`) only for components with anti-aliased curves where 1–2 pixels can flap (e.g. spinner mid-rotation if it ever ends up captured).

## CI Integration

The existing `cockpit` job in `.github/workflows/production.yml` runs `npm run build` and `npm run test` (vitest). We add a new step:

```yaml
- name: Visual regression
  run: npx playwright install chromium && npm run test:visual
- name: Upload visual diffs
  if: failure()
  uses: actions/upload-artifact@v4
  with:
    name: visual-diffs
    path: cockpit/test-results/
    retention-days: 7
```

`test:visual` is a new npm script: `playwright test --config=playwright.config.ts`. Failures publish the `test-results/` folder, which contains the expected/actual/diff PNG triplet per failing spec. Reviewers click the artifact link in the PR check, scroll through the diffs, and either ask for a fix or approve.

The job adds ~30s to CI on cold cache (chromium download), ~10s warm. Acceptable.

## Sequencing With the Folder Restructure

The user's intent is to **build the test net before the restructure**, then use a 0-diff visual test to prove the restructure is purely a topology change.

Phase A — **Build the safety net** (this feature)

A.1 Set up Playwright component testing infrastructure (config, harness, npm scripts, CI step).
A.2 Author the ~60 stories listed in the variant matrix.
A.3 Capture initial baselines on the CI runner (not locally — snapshots must be committed from a deterministic environment).
A.4 Verify that intentionally regressing a token (e.g. set `--accent-color` to red) produces visible diffs as expected.
A.5 Land the suite, snapshots, and CI step in a single PR.

Phase B — **Restructure** (separate feature, separate PR)

B.1 Design the new `cockpit/src/app/` topology (TBD — likely consolidates `ui/` + `shared/` + `layout/` + `simple/` into a coherent structure with documented responsibilities per folder).
B.2 Move files. Update imports. Delete empty leaves.
B.3 Run `npm run test:visual`. **Expectation: 0 diffs**. If anything diffs, the restructure unintentionally changed rendering — investigate before merging.
B.4 If 0 diffs achieved, the restructure is mechanically safe. Land the PR.

Phase C — **Ongoing** (continuous)

- Every PR runs the suite. Token edits and primitive changes show their visual blast radius automatically.
- New primitives ship with their stories in the same PR (block via CI check or by convention — TBD).
- Quarterly: prune unused stories, accept legitimate visual drift, regenerate baselines under a `chore: refresh visual baselines` PR.

## Implementation Plan

The estimated work for Phase A is one focused day plus on-call CI tuning:

| Step | Effort |
|------|--------|
| Install `@playwright/test`, configure `playwright.config.ts` | 1h |
| Write mount harness (`tests/visual/_harness/mount.ts`) supporting theme + viewport overrides | 2h |
| Author ~60 stories — script-generate from primitive metadata where possible | 4h |
| Capture and commit initial baselines on CI | 1h (mostly waiting) |
| Wire the GitHub Actions step + artifact upload | 1h |
| Verify the suite catches a deliberately introduced regression | 30m |
| Documentation in `cockpit/tests/visual/README.md` (how to add a story, how to accept a snapshot) | 1h |

Total: ~10 hours of focused work, spread across 1–2 days.

## Open Questions

1. **Component testing flavor**: `@playwright/experimental-ct-angular` is officially experimental as of Playwright 1.49 — it works but ships with caveats. Alternative: render via a tiny dev route in cockpit itself (`/__visual__/button?variant=primary`) and snapshot that route. Pros: no experimental dependency, real bootstrap path. Cons: routes live in production bundle (gateable behind dev mode). **Leaning toward the dev-route approach.**
2. **Threshold defaults**: Start strict (0 pixels) or lenient (50 pixels)? Strict catches more but flakes more. Recommend start strict, raise per-spec only when a real flake is observed.
3. **Snapshot storage**: PNGs in git work for ~120 baselines. If we ever 10x the variant count (tablet × phone × desktop matrix), Git LFS becomes worth it. Defer.
4. **Mobile viewport coverage**: Light/dark is required. Mobile-vs-desktop viewport snapshots double the PNG count and rarely reveal regressions in primitives (which adapt via flex, not media queries). Defer to per-screen Playwright snapshots in a later phase if we want it.
5. **Primitive vs page balance**: This proposal is primitive-only. After the restructure, consider adding ~5 page-level snapshots (login, project list, settings index) as smoke tests. Keep that scope explicit and small — pages drift faster than primitives.

## Success Criteria

- Phase A merged: CI runs visual checks on every cockpit-touching PR. Failed runs publish PNG diffs.
- A token edit (e.g. spacing scale tweak) produces a visible diff in CI for every affected primitive without anyone running anything locally.
- Phase B restructure PR: 0-diff visual report confirms no rendering regression.
- Light-mode contrast bug or icon-loading bug equivalent is caught on the PR that introduces it, not in production.
