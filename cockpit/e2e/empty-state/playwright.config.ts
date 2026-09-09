import {defineConfig, devices} from '@playwright/test';

const baseURL = process.env['EMPTY_STATE_E2E_BASE_URL'] || 'http://127.0.0.1:4174';

export default defineConfig({
  testDir: '.',
  testMatch: ['empty-state-layout.spec.ts'],
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env['CI'],
  reporter: [['list']],
  outputDir: '../../test-results/empty-state',
  use: {baseURL, trace: 'on-first-retry'},
  webServer: {
    command: 'node e2e/empty-state/fixture-server.mjs',
    cwd: process.cwd(),
    url: `${baseURL}/__e2e/health`,
    reuseExistingServer: false,
    timeout: 20_000,
  },
  projects: [{name: 'chromium', use: {...devices['Desktop Chrome']}}],
});
