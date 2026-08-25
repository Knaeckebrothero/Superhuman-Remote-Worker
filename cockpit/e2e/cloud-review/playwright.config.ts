import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env['CLOUD_REVIEW_BASE_URL'] || 'http://127.0.0.1:4174';

/**
 * Viewports the review surface is gated at.
 *
 * `devices['Desktop Chrome']` carries its own `viewport` (1280x720), so a
 * project that spreads it AFTER declaring one silently discards it — which is
 * how a suite that claimed to run at 1440x900 actually ran at 1280x720 for its
 * entire life, including the screenshots. Every project below therefore sets
 * `viewport` after the spread, and the layout spec asserts the size it
 * actually got rather than the size it asked for.
 */
const VIEWPORTS = [
  { name: 'phone-375', viewport: { width: 375, height: 667 } },
  // German runs roughly 1.6x English; the confirmation copy is where that
  // used to leave no viewer at all.
  { name: 'phone-375-de', viewport: { width: 375, height: 667 }, locale: 'de-DE' },
  { name: 'tablet-768', viewport: { width: 768, height: 720 } },
  { name: 'laptop-1280', viewport: { width: 1280, height: 720 } },
  { name: 'desktop-1440', viewport: { width: 1440, height: 900 } },
  { name: 'desktop-1920', viewport: { width: 1920, height: 1080 } },
] as const;

/**
 * Browser gate for the protected-cloud review surface. Chromium only — this
 * exists to gate the surface against a real production build across the
 * viewports it has to work at, and to capture the light/dark screenshots, not
 * to certify cross-browser behaviour (the canvas suite is that gate).
 */
export default defineConfig({
  testDir: '.',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  outputDir: '../../test-results/cloud-review',
  use: {
    baseURL,
    serviceWorkers: 'block',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'node e2e/cloud-review/fixture-server.mjs',
    cwd: process.cwd(),
    url: `${baseURL}/__e2e/health`,
    reuseExistingServer: false,
    timeout: 20_000,
  },
  projects: [
    {
      name: 'behaviour',
      testMatch: ['cloud-review.spec.ts'],
      // Viewport AFTER the spread. See the note above.
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
    ...VIEWPORTS.map((size) => ({
      name: `layout-${size.name}`,
      testMatch: ['cloud-review-layout.spec.ts'],
      use: {
        ...devices['Desktop Chrome'],
        viewport: size.viewport,
        locale: 'locale' in size ? (size.locale as string) : 'en-US',
      },
    })),
  ],
});
