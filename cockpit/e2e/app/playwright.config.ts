import { defineConfig, devices } from '@playwright/test';
import { ARTIFACT_DIRECTORY, AUTH_STATE_PATH, BASE_URL, REPORT_DIRECTORY } from './environment';

export default defineConfig({
  testDir: '.',
  testMatch: ['*.spec.ts', 'auth.setup.ts'],
  globalTimeout: 15 * 60_000,
  timeout: 7 * 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env['CI'],
  failOnFlakyTests: !!process.env['CI'],
  preserveOutput: 'always',
  outputDir: ARTIFACT_DIRECTORY,
  reporter: [
    [process.env['CI'] ? 'line' : 'list'],
    ['./non-empty-reporter.ts'],
    [
      'html',
      {
        open: 'never',
        outputFolder: REPORT_DIRECTORY,
      },
    ],
  ],
  use: {
    baseURL: BASE_URL,
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
    serviceWorkers: 'block',
    ignoreHTTPSErrors: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    viewport: { width: 1440, height: 960 },
  },
  projects: [
    {
      name: 'auth-setup',
      testMatch: /auth\.setup\.ts/,
      timeout: 90_000,
      use: { ...devices['Desktop Chrome'], storageState: undefined },
    },
    {
      name: 'app-chromium',
      testMatch: /.*\.spec\.ts/,
      dependencies: ['auth-setup'],
      timeout: 7 * 60_000,
      use: { ...devices['Desktop Chrome'], storageState: AUTH_STATE_PATH },
    },
  ],
});
