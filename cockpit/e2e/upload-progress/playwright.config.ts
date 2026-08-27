import {defineConfig, devices} from '@playwright/test';

/**
 * Live-backend gate for real attachment upload progress.
 *
 * Unlike `e2e/canvas/playwright.config.ts` — which starts a synthetic
 * in-process fixture and blocks service workers — this config serves the
 * **production** Cockpit bundle and lets the Angular service worker register
 * and claim the page, because the SW × XHR-upload-progress interaction is
 * half of what is under test. It talks to a **real** SRW backend
 * (`https://localhost` = local k3d by default). It must never be pointed at a
 * shared or remote cluster: it creates a session and uploads a large file.
 *
 * Run from `cockpit/`:
 *
 *   npm run build                       # production bundle with ngsw
 *   npx playwright test --config=e2e/upload-progress/playwright.config.ts
 */
const PORT = process.env['UPLOAD_E2E_PORT'] || '4174';
const baseURL = `http://localhost:${PORT}`;

export default defineConfig({
  testDir: '..',
  testMatch: ['attachment-upload-progress.spec.ts'],
  // A cold agent pod plus a deliberately throttled multi-MB upload; this is a
  // deployment gate, not a unit test.
  timeout: 8 * 60_000,
  expect: {timeout: 15_000},
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env['CI'],
  retries: 0,
  // Relative to this config file, so both land in cockpit/test-results/,
  // which .gitignore already covers.
  reporter: [
    ['list'],
    ['html', {open: 'never', outputFolder: '../../test-results/upload-progress-report'}],
  ],
  outputDir: '../../test-results/upload-progress',
  use: {
    baseURL,
    // The whole point: the production SW must be allowed to install and claim.
    serviceWorkers: 'allow',
    // Only the sign-in leg touches the upstream's mkcert TLS; the app under
    // test is served over plain-HTTP loopback (a trustworthy origin, so the
    // SW still registers). TLS is not what this gate measures.
    ignoreHTTPSErrors: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    viewport: {width: 1440, height: 1000},
  },
  webServer: {
    command: 'node e2e/upload-progress/prod-serve.mjs',
    cwd: process.cwd(),
    url: `${baseURL}/__e2e/health`,
    reuseExistingServer: false,
    timeout: 30_000,
    stdout: 'pipe',
  },
  projects: [{name: 'chromium', use: {...devices['Desktop Chrome']}}],
});
