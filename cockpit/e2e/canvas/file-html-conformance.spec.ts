import {APIRequestContext, Frame, Page, expect, test} from '@playwright/test';

const BASE_URL = 'http://127.0.0.1:4173';
const THREAD_ID = '11111111-1111-4111-8111-111111111111';
const WRAPPER_PATH = `/sessions/${THREAD_ID}/canvas`;
const STATIC_OUTER_FRAME = 'app-canvas-html-renderer > iframe';
const INTERACTIVE_OUTER_FRAME = 'app-canvas-interactive-html-renderer > iframe';

interface FixtureState {
  requests: Array<{path: string}>;
}

test.describe('File-backed HTML Canvas production-browser conformance', () => {
  test.beforeEach(async ({context}) => {
    await context.addCookies([
      {
        name: 'srw_session',
        value: 'file-canvas-parent-session',
        url: BASE_URL,
        httpOnly: true,
        sameSite: 'Lax',
      },
      {
        name: 'canvas_parent_canary',
        value: 'must-not-reach-opaque-frame',
        url: BASE_URL,
        sameSite: 'Lax',
      },
    ]);
  });

  test('renders sanitized static HTML and preserves rewritten native fragments', async ({
    page,
    request,
  }) => {
    await resetFixture(request, 'file-static');
    await openFileCanvas(page, STATIC_OUTER_FRAME, '#user-content-static-marker');

    const outer = page.locator(STATIC_OUTER_FRAME);
    const inner = page.frameLocator(STATIC_OUTER_FRAME).locator('iframe');
    await expect(outer).toHaveAttribute('sandbox', '');
    await expect(inner).toHaveAttribute('sandbox', '');
    await expect(inner).not.toHaveAttribute('src', /.+/);

    const frame = await nestedCanvasFrame(page, STATIC_OUTER_FRAME);
    expect(await frame.evaluate(() => document.baseURI)).toBe('about:srcdoc');
    expect(await frame.locator('head > base').getAttribute('href')).toBe('about:srcdoc');
    expect(await frame.locator('head > base').getAttribute('target')).toBe('_self');
    expect(
      await frame
        .locator('#user-content-static-marker')
        .evaluate(element => getComputedStyle(element).backgroundColor),
    ).toBe('rgb(18, 52, 86)');
    expect(await frame.locator('#static-script-control').count()).toBe(0);
    expect(await frame.locator('script').count()).toBe(0);
    expect(await frame.evaluate(() => document.body.dataset['scriptRan'] ?? null)).toBeNull();

    const jump = frame.locator('#user-content-static-jump');
    await expect(jump).toHaveAttribute('href', '#user-content-static-target');
    await jump.click();
    await expect
      .poll(() => frame.evaluate(() => location.hash))
      .toBe('#user-content-static-target');
    await expect(frame.locator('#user-content-static-target')).toHaveText('Static fragment target');
  });

  test('renders interactive HTML while retaining opaque isolation and no egress', async ({
    context,
    page,
    request,
  }) => {
    await resetFixture(request, 'file-interactive');
    await installCanvasParentBasePolicy(page);
    const escapedRequests: string[] = [];
    await context.route(/^https:\/\/canvas-egress\.invalid\//, async route => {
      escapedRequests.push(route.request().url());
      await route.abort('blockedbyclient');
    });
    await openFileCanvas(page, INTERACTIVE_OUTER_FRAME, '#interactive-marker');

    const outer = page.locator(INTERACTIVE_OUTER_FRAME);
    const inner = page.frameLocator(INTERACTIVE_OUTER_FRAME).locator('iframe');
    await expect(outer).toHaveAttribute('sandbox', 'allow-scripts');
    await expect(inner).toHaveAttribute('sandbox', 'allow-scripts');
    await expect(inner).not.toHaveAttribute('src', /.+/);
    await expect(inner).toHaveAttribute(
      'allow',
      "camera 'none'; microphone 'none'; geolocation 'none'; " +
        "clipboard-read 'none'; clipboard-write 'none'",
    );

    const outerFrame = await directFrame(page, INTERACTIVE_OUTER_FRAME);
    const wrapperPolicy = await outerFrame
      .locator('meta[http-equiv="Content-Security-Policy"]')
      .getAttribute('content');
    expect(wrapperPolicy).toContain("script-src 'unsafe-inline'");
    expect(wrapperPolicy).toContain('base-uri about:');
    expect(wrapperPolicy).toContain("frame-src 'none'");

    const frame = await nestedCanvasFrame(page, INTERACTIVE_OUTER_FRAME);
    expect(await frame.evaluate(() => document.baseURI)).toBe('about:srcdoc');
    expect(await frame.locator('head > base').first().getAttribute('href')).toBe('about:srcdoc');
    expect(await frame.locator('head > base').first().getAttribute('target')).toBe('_self');
    expect(await frame.evaluate(() => document.body.dataset['scriptReady'])).toBe('yes');
    expect(
      await frame.locator('body').evaluate(element => getComputedStyle(element).backgroundColor),
    ).toBe('rgb(18, 52, 86)');

    await frame.locator('#interactive-action').click();
    await expect(frame.locator('#interactive-result')).toHaveText('clicked');
    await frame.locator('#interactive-jump').click();
    await expect.poll(() => frame.evaluate(() => location.hash)).toBe('#interactive-target');
    await expect(frame.locator('#interactive-target')).toHaveText('Interactive fragment target');

    const isolation = await frame.evaluate(() => {
      let parentReadable = true;
      try {
        void window.parent.document.body;
      } catch {
        parentReadable = false;
      }
      let storageReadable = true;
      try {
        void localStorage.length;
      } catch {
        storageReadable = false;
      }
      let cookie: string | null = null;
      try {
        cookie = document.cookie;
      } catch {
        cookie = null;
      }
      return {parentReadable, storageReadable, cookie};
    });
    expect(isolation.parentReadable).toBe(false);
    expect(isolation.storageReadable).toBe(false);
    expect(isolation.cookie === null || isolation.cookie === '').toBe(true);

    const egressUrl = 'https://canvas-egress.invalid/probe';
    expect(
      await frame.evaluate(async url => {
        try {
          await fetch(url);
          return false;
        } catch {
          return true;
        }
      }, egressUrl),
    ).toBe(true);
    expect(
      await frame.evaluate(url => {
        try {
          return window.open(url) === null;
        } catch {
          return true;
        }
      }, egressUrl),
    ).toBe(true);
    expect(escapedRequests).toEqual([]);

    await frame.evaluate(url => {
      for (const base of document.querySelectorAll('base')) base.remove();
      location.href = url;
    }, `${BASE_URL}/trusted-target?via=self-navigation`);
    await page.waitForTimeout(500);
    expect(
      (await fixtureState(request)).requests.filter(entry => entry.path === '/trusted-target'),
    ).toEqual([]);
  });

  test('blocks meta refresh after hostile content removes the fragment base', async ({
    page,
    request,
  }) => {
    await resetFixture(request, 'file-interactive');
    await openFileCanvas(page, INTERACTIVE_OUTER_FRAME, '#interactive-marker');
    const frame = await nestedCanvasFrame(page, INTERACTIVE_OUTER_FRAME);

    await frame.evaluate(url => {
      for (const base of document.querySelectorAll('base')) base.remove();
      const refresh = document.createElement('meta');
      refresh.httpEquiv = 'refresh';
      refresh.content = `0;url=${url}`;
      document.head.append(refresh);
    }, `${BASE_URL}/trusted-target?via=meta-refresh`);
    await page.waitForTimeout(500);

    expect(
      (await fixtureState(request)).requests.filter(entry => entry.path === '/trusted-target'),
    ).toEqual([]);
  });
});

async function resetFixture(request: APIRequestContext, scenario: string): Promise<void> {
  const response = await request.post('/__e2e/reset', {data: {scenario}});
  expect(response.ok()).toBe(true);
}

async function fixtureState(request: APIRequestContext): Promise<FixtureState> {
  const response = await request.get('/__e2e/state');
  expect(response.ok()).toBe(true);
  return (await response.json()) as FixtureState;
}

async function openFileCanvas(
  page: Page,
  outerSelector: string,
  markerSelector: string,
): Promise<void> {
  await page.goto(WRAPPER_PATH, {waitUntil: 'load'});
  await expect(page.locator(outerSelector)).toBeVisible();
  await expect(
    page.frameLocator(outerSelector).frameLocator('iframe').locator(markerSelector),
  ).toBeVisible();
}

async function installCanvasParentBasePolicy(page: Page): Promise<void> {
  await page.route(
    `${BASE_URL}${WRAPPER_PATH}`,
    async route => {
      const response = await route.fetch();
      const headers = response.headers();
      headers['content-security-policy'] =
        `${headers['content-security-policy']}; base-uri 'self' about:`;
      await route.fulfill({response, headers});
    },
    {times: 1},
  );
}

async function directFrame(page: Page, selector: string): Promise<Frame> {
  const handle = await page.locator(selector).elementHandle();
  const frame = await handle?.contentFrame();
  expect(frame).not.toBeNull();
  return frame!;
}

async function nestedCanvasFrame(page: Page, outerSelector: string): Promise<Frame> {
  const outer = await directFrame(page, outerSelector);
  const innerHandle = await outer.locator('iframe').elementHandle();
  const inner = await innerHandle?.contentFrame();
  expect(inner).not.toBeNull();
  return inner!;
}
