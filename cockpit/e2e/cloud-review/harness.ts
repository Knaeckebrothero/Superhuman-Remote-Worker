import { expect, type Page } from '@playwright/test';
import { mkdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * Shared fixture wiring for the protected-cloud review browser gate.
 *
 * Nothing here can reach a real orchestrator, a real cloud, or the preserved
 * epoch-5 evidence thread 34743d6c-9224-4866-94a9-18c3828b8b29 — the fixture
 * server is the only backend, and apply/reject are mocked.
 */

/** Mirrors THREAD_ID in fixture-server.mjs. Duplicated rather than imported:
 *  Playwright's TS transform loads specs as CJS and cannot require an ESM
 *  fixture module. */
export const THREAD_ID = '33333333-3333-4333-8333-333333333333';

const SHOTS =
  process.env['CLOUD_REVIEW_SHOT_DIR'] ??
  resolve(process.cwd(), '..', 'protected-cloud-review-screens');
const LABEL = process.env['CLOUD_REVIEW_SHOT_LABEL'] ?? 'after';
const CAPTURE = process.env['CLOUD_REVIEW_CAPTURE'] === '1';

mkdirSync(SHOTS, { recursive: true });

/**
 * PNG dimensions, straight out of the IHDR chunk (bytes 16..24).
 *
 * The suite used to *declare* 1440x900 and silently render at 1280x720
 * because of a config bug, so "the screenshot is the size we think it is" is
 * now something the suite proves rather than something it asserts in prose.
 */
export function pngSize(path: string): { width: number; height: number } {
  const head = readFileSync(path).subarray(0, 24);
  return { width: head.readUInt32BE(16), height: head.readUInt32BE(20) };
}

export type Scenario =
  | 'pending'
  | 'empty'
  | 'conflict'
  | 'partial'
  | 'forbidden'
  | 'offline'
  | 'probeFail'
  | 'holdApply'
  | 'rejectStale'
  | 'rejectRefused'
  | 'fileFlaky'
  | 'fileGone'
  | 'fileUnreadable';

export async function resetFixture(page: Page, scenario: Scenario = 'pending'): Promise<void> {
  await page.request.post('/__e2e/reset', { data: { scenario } });
}

export function fixtureState(page: Page): Promise<Record<string, number | string | null>> {
  return page.request.get('/__e2e/state').then((r) => r.json());
}

/** Let a held apply finish. See the `holdApply` scenario. */
export async function releaseApply(page: Page): Promise<void> {
  await page.request.post('/__e2e/release');
}

/** Load the session with an explicit theme, before Angular's first paint. */
export async function openSession(
  page: Page,
  theme: 'travertine' | 'senate',
  scenario: Scenario = 'pending',
): Promise<void> {
  await resetFixture(page, scenario);
  await page.addInitScript((value) => {
    try {
      localStorage.setItem('cockpit:theme', value);
      // A receipt left by an earlier test would change which banner renders.
      for (const key of Object.keys(localStorage)) {
        if (key.startsWith('srw:cloud-review-receipt:')) localStorage.removeItem(key);
      }
    } catch {
      /* private mode */
    }
    // Record window.open instead of performing it: the cloud host does not
    // resolve, and what PC-19 is about is *which URL* the app chooses.
    const opened: string[] = [];
    (window as unknown as { __opened: string[] }).__opened = opened;
    window.open = ((url?: string | URL) => {
      opened.push(String(url));
      return null;
    }) as typeof window.open;
  }, theme);
  // domcontentloaded, not load: the shell holds two SSE streams open, so the
  // load event never fires.
  await page.goto(`/sessions/${THREAD_ID}`, { waitUntil: 'domcontentloaded' });
  await expect(page.locator(`body.theme-${theme}`)).toHaveCount(1);
}

export const banner = (page: Page) => page.locator('app-cloud-review-banner .crb');
export const dialog = (page: Page) => page.locator('[role="dialog"]');
export const surface = (page: Page) => page.locator('app-job-diff-review .review');

/**
 * Expand the file list if it is currently a collapsed phone disclosure.
 *
 * Idempotent on purpose: clicking the toggle unconditionally *collapses* an
 * already-open list, which is a much better way to write a test that hangs
 * than to write one that passes.
 */
export async function ensureFilesVisible(page: Page): Promise<void> {
  const list = surface(page).locator('[role="listbox"]');
  if (await list.count()) return;
  const toggle = surface(page).locator('.review__files-toggle');
  if (await toggle.count()) await toggle.click();
  await expect(list).toBeVisible();
}

/**
 * Collapse the phone chooser if it is open.
 *
 * This is the state a review is actually conducted in: the chooser is a
 * transient picker, and layout assertions belong against the settled state,
 * not against a dropdown someone is mid-way through using.
 */
export async function ensureFilesHidden(page: Page): Promise<void> {
  const toggle = surface(page).locator('.review__files-toggle');
  if (!(await toggle.count())) return; // desktop: the list is permanent
  if (await surface(page).locator('[role="listbox"]').count()) await toggle.click();
  await expect(surface(page).locator('[role="listbox"]')).toHaveCount(0);
}

/** Open the review from the banner and wait for the file list to be usable.
 *  On a phone the list is a collapsed disclosure, so this expands it. */
export async function openReview(page: Page, expectFiles = 4): Promise<void> {
  await expect(banner(page)).toBeVisible();
  await banner(page).getByRole('button', { name: /Review changes|Änderungen prüfen/ }).click();
  await expect(dialog(page)).toBeVisible();
  await ensureFilesVisible(page);
  await expect(surface(page).locator('[role="option"]')).toHaveCount(expectFiles);
}

export async function shot(page: Page, theme: string, name: string): Promise<void> {
  if (!CAPTURE) return;
  const path = resolve(SHOTS, `${LABEL}-${theme}-${name}.png`);
  await page.screenshot({ path });
  const declared = page.viewportSize();
  const actual = pngSize(path);
  // The capture must match the viewport it claims, or the evidence is a
  // record of a different layout than the one under test.
  expect(actual).toEqual({ width: declared!.width, height: declared!.height });
}
