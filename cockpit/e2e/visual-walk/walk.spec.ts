import {expect, test, type Page} from '@playwright/test';
import {mkdirSync} from 'node:fs';
import {join} from 'node:path';

// A review aid, not a pixel test: PNGs go to playwright-report/visual-walk/<label>/
// for a person to look at. Only the structural checks at the bottom assert.

const LABEL =
  process.env['VISUAL_WALK_LABEL'] ||
  new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
const USER = process.env['VISUAL_WALK_USER'] || 'test';
const PASSWORD = process.env['VISUAL_WALK_PASSWORD'] || 'test';
// The app resolves its language from navigator.languages (i18n.service.ts
// fromBrowser), so the locale is a browser-context property, not storage.
const LOCALE = process.env['VISUAL_WALK_LOCALE'] || 'en-US';
// Accent axis (tyrian default, porphyry, graphite) — a second body class
// beside theme-*; see theme.service.ts.
const ACCENT = process.env['VISUAL_WALK_ACCENT'] || 'tyrian';
const OUT = join(process.cwd(), 'playwright-report', 'visual-walk', LABEL);

const ROUTES = ['/', '/jobs', '/projects', '/settings', '/datasources', '/admin/users', '/experts'];
const THEMES = ['travertine', 'senate'] as const;
const VIEWPORTS = [
  {name: 'desktop', width: 1440, height: 900},
  {name: 'mobile', width: 390, height: 844},
] as const;

async function login(page: Page): Promise<void> {
  await page.goto('/', {waitUntil: 'domcontentloaded'});
  // A live session cookie skips the form; a fresh context lands on Keycloak.
  const username = page.locator('#username');
  const appShell = page.locator('app-root .rail-item, app-root [role="tablist"]').first();
  await Promise.race([
    username.waitFor({state: 'visible', timeout: 30_000}),
    appShell.waitFor({state: 'visible', timeout: 30_000}),
  ]);
  if (await username.isVisible().catch(() => false)) {
    await username.fill(USER);
    await page.locator('#password').fill(PASSWORD);
    await page.locator('#kc-login').click();
  }
  await appShell.waitFor({state: 'visible', timeout: 60_000});
}

async function setTheme(page: Page, theme: (typeof THEMES)[number]): Promise<void> {
  await page.evaluate(
    ({t, a}) => {
      document.body.classList.remove('theme-travertine', 'theme-senate');
      document.body.classList.add(`theme-${t}`);
      [...document.body.classList].filter((c) => c.startsWith('accent-')).forEach((c) => document.body.classList.remove(c));
      document.body.classList.add(`accent-${a}`);
    },
    {t: theme, a: ACCENT},
  );
}

function slug(route: string): string {
  return route === '/' ? 'home' : route.replace(/^\//, '').replace(/[/:]/g, '_');
}

async function captureBothThemes(page: Page, name: string, viewport: string): Promise<void> {
  for (const theme of THEMES) {
    await setTheme(page, theme);
    await page.waitForTimeout(150);
    await page.screenshot({path: join(OUT, `${name}--${theme}--${viewport}.png`)});
  }
}

test('capture walk', async ({browser}) => {
  mkdirSync(OUT, {recursive: true});
  for (const vp of VIEWPORTS) {
    const context = await browser.newContext({
      viewport: {width: vp.width, height: vp.height},
      ignoreHTTPSErrors: true,
      locale: LOCALE,
    });
    const page = await context.newPage();
    await login(page);

    for (const route of ROUTES) {
      // 'load' + a settle: the app holds sockets and polls, so 'networkidle'
      // never fires on /jobs and friends.
      await page.goto(route, {waitUntil: 'load'});
      await page.waitForTimeout(1_200);
      await captureBothThemes(page, slug(route), vp.name);
    }

    // First session in the rail, if any: the only route that needs an id.
    await page.goto('/', {waitUntil: 'load'});
    await page.waitForTimeout(800);
    // Navigate by href rather than clicking: on the mobile viewport the rail
    // entry exists but sits behind the collapsed drawer.
    const firstSession = page.locator('a.rail-item').first();
    const href = (await firstSession.count()) ? await firstSession.getAttribute('href') : null;
    if (href) {
      await page.goto(href, {waitUntil: 'load'});
      await page.waitForTimeout(1_500);
      await captureBothThemes(page, 'session', vp.name);
    }

    // Structural assertions live here so a slice cannot regress them silently.
    // Jobs rows: at most two text buttons (View + one contextual) and exactly
    // one kebab per row on desktop.
    await page.setViewportSize({width: 1440, height: 900});
    await page.goto('/jobs', {waitUntil: 'load'});
    await page.waitForTimeout(1_200);
    const rows = page.locator('table.app-table tbody tr:not(.promote-row)');
    const n = await rows.count();
    for (let i = 0; i < n; i++) {
      const cell = rows.nth(i).locator('td.actions-cell');
      expect(await cell.locator('app-button').count(), `row ${i} text buttons`).toBeLessThanOrEqual(2);
      expect(await cell.locator('app-icon-button').count(), `row ${i} kebab`).toBe(1);
    }

    await context.close();
  }
});
