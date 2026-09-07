import {defineConfig, devices} from '@playwright/test';

const baseURL = process.env['CANVAS_E2E_BASE_URL'] || 'http://127.0.0.1:4173';

export default defineConfig({
  testDir: '.',
  testMatch: [
    'canvas-conformance.spec.ts',
    'file-html-conformance.spec.ts',
    'shared-browser-conformance.spec.ts',
  ],
  timeout: 30_000,
  expect: {timeout: 8_000},
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env['CI'],
  retries: process.env['CI'] ? 1 : 0,
  reporter: process.env['CI']
    ? [['line'], ['html', {open: 'never'}]]
    : [['list'], ['html', {open: 'never'}]],
  outputDir: '../../test-results/canvas',
  use: {
    baseURL,
    serviceWorkers: 'block',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    viewport: {width: 1440, height: 960},
  },
  webServer: {
    command: 'node e2e/canvas/fixture-server.mjs',
    cwd: process.cwd(),
    url: `${baseURL}/__e2e/health`,
    reuseExistingServer: false,
    timeout: 20_000,
  },
  projects: [
    {name: 'chromium', use: {...devices['Desktop Chrome']}},
    {name: 'firefox', use: {...devices['Desktop Firefox']}},
    {name: 'webkit', use: {...devices['Desktop Safari']}},
  ],
});
