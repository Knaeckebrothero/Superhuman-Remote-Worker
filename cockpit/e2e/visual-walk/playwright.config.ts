import {defineConfig, devices} from '@playwright/test';

const baseURL = process.env['VISUAL_WALK_BASE_URL'] || 'https://localhost';

export default defineConfig({
  testDir: '.',
  testMatch: ['walk.spec.ts'],
  timeout: 5 * 60_000,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  outputDir: '../../test-results/visual-walk',
  use: {
    baseURL,
    ignoreHTTPSErrors: true,
    serviceWorkers: 'block',
    actionTimeout: 20_000,
    navigationTimeout: 60_000,
  },
  projects: [{name: 'chromium', use: {...devices['Desktop Chrome']}}],
});
