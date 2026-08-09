# `canvas-rendering.spec.ts` node-ceiling test flakes under full-suite load

**Status:** Open, low severity. Reproduced once; passes reliably in isolation.
**Found:** 2026-08-08, during unrelated cockpit work.
**Component:** `cockpit/src/app/views/canvas/canvas-rendering.spec.ts`

## Symptom

Running the **full** cockpit suite (`npx vitest run`, 119 files) failed with:

```
× Dynamic Canvas rendering trust boundary >
  accepts the exact sanitized node ceiling and rejects one additional text node   6132ms
Test Files  1 failed | 118 passed (119)
```

Re-running that spec file alone passes **32/32** in ~4 s. The failure did not recur on a subsequent full run.

## Why it happens

The test builds `'<span>x</span>'.repeat(CANVAS_RENDER_MAX_NODES / 2)` and sanitizes it — thousands of DOM nodes in jsdom. It is CPU-bound and has no explicit timeout override, so under parallel full-suite load it can exceed vitest's default 5 s per-test timeout. The observed 6132 ms is consistent with that: the assertion logic is fine, the wall clock is not.

This is a **test-harness timing** problem, not a defect in the canvas trust boundary — the ceiling logic itself is what the test asserts, and it passes whenever the test is given room to run.

## Suggested fix

Give the two heavy ceiling tests an explicit generous timeout (e.g. `it('…', () => {…}, 30_000)`), or shrink the fixture to the smallest size that still crosses `CANVAS_RENDER_MAX_NODES`, so the assertion no longer depends on machine load.

## Why this is worth knowing

A one-off red in a 1800+ test suite invites the wrong conclusion — that whatever you just changed broke the canvas. It does not. Confirm by re-running the single spec file before investigating; if it passes alone and your diff touches no canvas files, this is the cause.
