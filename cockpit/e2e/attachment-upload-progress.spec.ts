/**
 * Real upload progress, in a production build, with the service worker active.
 *
 * WHY THIS CANNOT BE A UNIT TEST
 * ------------------------------
 * Every progress spec under `src/` runs on `provideHttpClientTesting()`, whose
 * mock backend emits whatever event the test hands it. Those specs stay green
 * even when production reports nothing at all — which is exactly what happened
 * before Task 8: Angular's `FetchBackend` emits **zero** `UploadProgress`
 * events, so `reportProgress: true` was a silent no-op app-wide. Only a real
 * browser pushing real bytes at a real socket can distinguish the two.
 *
 * There is a second reason this must be a *production* build. A service worker
 * that answers a request with `respondWith()` destroys XHR upload progress
 * outright (angular/angular#24683, #24716). Cockpit's protection is the
 * `ngsw-bypass: 1` header stamped by `auth.interceptor.ts` on every non-safe
 * request, and ngsw only registers when `!isDevMode()` (`app.config.ts`). The
 * k3d cockpit pod runs `ng serve`, so a run against it proves nothing about
 * that interaction. `e2e/upload-progress/prod-serve.mjs` therefore serves the
 * real `dist/` bundle and proxies to the live backend, and this spec refuses
 * to assert anything until it has proven the SW is *controlling the page*.
 *
 * WHY THE CEILING IS 90, NOT 100
 * ------------------------------
 * `sendProgressPercent()` scales the byte upload to `UPLOAD_SHARE_OF_SCALE`
 * (90%) of the track, reserving the rest for the POST that follows. So "0 then
 * 100" is NOT the shape a broken build produces here — a build with no
 * `UploadProgress` events produces *indeterminate* (no `aria-valuenow`, since
 * an uploading file with `total == null` makes the whole item indeterminate)
 * and then a single jump to **90** when the file flips to `done`. A test that
 * accepted "strictly between 0 and 100" would therefore pass on the exact
 * regression it exists to catch. It must be `0 < v < 90`.
 *
 * And the attachment must be a SINGLE file. With two files, per-file `done`
 * transitions alone would produce 45 — an intermediate reading that needs no
 * byte-level progress event at all. With one file, every value in (0, 90) is
 * reachable only from a real `HttpEventType.UploadProgress` carrying both
 * `loaded` and a computable `total`.
 */
import {expect, test, type Page} from '@playwright/test';
import {mkdirSync, rmSync, writeFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {UPLOAD_SHARE_OF_SCALE} from '../src/app/core/services/upload-stage';

/** The live backend. Local k3d only — this creates a session and uploads to it. */
const UPSTREAM = process.env['UPLOAD_E2E_UPSTREAM'] ?? 'https://localhost';
const USERNAME = process.env['UPLOAD_E2E_USER'] ?? 'test';
const PASSWORD = process.env['UPLOAD_E2E_PASS'] ?? 'test';

/** ≥50MB, comfortably under the server's 100MB per-file cap. */
const FILE_MB = Number(process.env['UPLOAD_E2E_FILE_MB'] ?? 56);
/**
 * Loopback would otherwise absorb the whole body in a few socket writes and
 * could report one jump straight to `total`. Throttling the browser's upload
 * is what makes intermediate readings *observable* — it does not manufacture
 * them: with no `UploadProgress` events there is nothing to slow down.
 */
const UPLOAD_BYTES_PER_SEC = Number(process.env['UPLOAD_E2E_THROTTLE_BPS'] ?? 8 * 1024 * 1024);

/** The value a fully-uploaded-but-not-yet-sent single file parks at. */
const DONE_PLATEAU = Math.round(UPLOAD_SHARE_OF_SCALE * 100);

interface Reading {
  value: number | null;
  t: number;
}

declare global {
  interface Window {
    __uploadReadings?: Reading[];
  }
}

/**
 * Sign in upstream against the real Keycloak and hand the resulting
 * `srw_session` to the local server, which injects it on proxied requests.
 * Keeping the credential server-side means the browser only ever makes
 * same-origin requests, so no CORS or SameSite behaviour can confound the
 * measurement.
 */
async function signInAndShareSession(page: Page, appOrigin: string): Promise<void> {
  await page.goto(`${UPSTREAM}/auth/login?return_to=/`, {waitUntil: 'domcontentloaded'});

  const username = page.locator('#username');
  if (await username.count()) {
    await username.fill(USERNAME);
    await page.locator('#password').fill(PASSWORD);
    await page.locator('#kc-login, input[type=submit], button[type=submit]').first().click();
    await page.waitForURL((u) => !u.host.startsWith('auth.'), {timeout: 60_000});
  }

  const cookies = await page.context().cookies();
  const session = cookies.find((c) => c.name === 'srw_session');
  expect(
    session,
    `No srw_session cookie after signing in at ${UPSTREAM} as "${USERNAME}". ` +
      'Is the local k3d stack up, and are UPLOAD_E2E_USER/UPLOAD_E2E_PASS right?',
  ).toBeTruthy();

  const shared = await page.request.post(`${appOrigin}/__e2e/session`, {
    data: {cookie: session!.value},
  });
  expect(shared.ok()).toBe(true);
}

test('a real attachment upload reports intermediate progress under an active service worker', async ({
  page,
  baseURL,
}, testInfo) => {
  const appOrigin = baseURL!;
  // Recorded as evidence, never asserted on: a live k3d backend legitimately
  // emits 425s while a session warms up, and the cockpit pod churns under
  // Tilt. Failing the gate on that would make it a flake detector for the
  // dev cluster rather than a check on upload progress.
  const failedResponses: string[] = [];
  page.on('response', (r) => {
    if (r.status() >= 400) failedResponses.push(`${r.status()} ${new URL(r.url()).pathname}`);
  });

  await signInAndShareSession(page, appOrigin);

  // ── A real session, created exactly the way the landing draft creates one
  //    (persistent-chat.service._createFromDraftSession): a minimal body, with
  //    the owner's saved defaults resolving the workspace backend server-side.
  const created = await page.request.post(`${appOrigin}/api/persistent/threads`, {
    headers: {'X-CSRF': '1'},
    data: {title: 'upload progress gate', datasource_ids: []},
  });
  expect(
    created.ok(),
    `Creating a session failed: ${created.status()} ${await created.text()}`,
  ).toBe(true);
  const threadId: string = (await created.json()).thread_id;

  // Generated, not committed — a 56MB fixture has no business in git.
  const scratch = resolve(testInfo.outputDir);
  mkdirSync(scratch, {recursive: true});
  const bigFile = resolve(scratch, 'upload-progress-fixture.zip');
  writeFileSync(bigFile, Buffer.alloc(FILE_MB * 1024 * 1024, 0x7a));

  try {
    await page.goto(`${appOrigin}/sessions/${threadId}`);

    // ── Prove the service worker is not merely registered but CONTROLLING.
    //    ngsw calls clients.claim() on activate; one reload covers the race if
    //    the first load beat activation.
    const controlled = async () =>
      page.evaluate(() => navigator.serviceWorker?.controller?.scriptURL ?? null);
    let controller = await page
      .waitForFunction(() => navigator.serviceWorker?.controller != null, null, {timeout: 60_000})
      .then(controlled)
      .catch(() => null);
    if (!controller) {
      await page.reload();
      await page.waitForFunction(() => navigator.serviceWorker?.controller != null, null, {
        timeout: 60_000,
      });
      controller = await controlled();
    }
    expect(
      controller,
      'No service worker is controlling the page. This gate is meaningless without one: ' +
        'ngsw registers only in a production build (app.config.ts, enabled: !isDevMode()), ' +
        'so check that dist/ came from `npm run build` and not from a dev build.',
    ).toContain('ngsw-worker.js');
    testInfo.annotations.push({type: 'service-worker', description: controller!});
    console.log(`[gate] service worker controlling the page: ${controller}`);

    // ── Attach one large file through the composer's real input.
    const composer = page.locator('textarea.chat-input');
    await expect(composer).toBeVisible({timeout: 120_000});
    await page.locator('input[type="file"]').first().setInputFiles(bigFile);
    await expect(page.locator('.attachment-chip')).toHaveCount(1);
    await composer.fill('Attachment upload progress gate.');

    // Record EVERY distinct aria-valuenow the send indicator publishes. A
    // MutationObserver catches each write; the sampler is a backstop in case a
    // re-render swaps the node rather than patching its attribute.
    await page.evaluate(() => {
      const readings: Reading[] = [];
      window.__uploadReadings = readings;
      const read = () => {
        const el = document.querySelector('.upload-bar[role="progressbar"]');
        if (!el) return;
        const raw = el.getAttribute('aria-valuenow');
        const value = raw === null ? null : Number(raw);
        const last = readings[readings.length - 1];
        if (!last || last.value !== value) readings.push({value, t: Date.now()});
      };
      new MutationObserver(read).observe(document.body, {
        subtree: true,
        childList: true,
        attributes: true,
        attributeFilter: ['aria-valuenow'],
      });
      setInterval(read, 25);
    });

    // Slow the browser's upload so the progress it reports is observable.
    const cdp = await page.context().newCDPSession(page);
    await cdp.send('Network.enable');
    await cdp.send('Network.emulateNetworkConditions', {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: UPLOAD_BYTES_PER_SEC,
    });

    const uploadResponse = page.waitForResponse(
      (r) => r.request().method() === 'POST' && r.url().includes(`/threads/${threadId}/uploads`),
      {timeout: 6 * 60_000},
    );
    await page.locator('button.send').click();

    // ── The bar has to be VISIBLE, not merely correct.
    //
    //    `aria-valuenow` is an attribute: Angular writes it whether or not the
    //    element it is on occupies a single pixel. That blind spot shipped a
    //    real regression — a `display: flex` in the global queued-bubble sheet
    //    (`_chat-queued.scss`, specificity (0,5,0)) beat the component's own
    //    `.upload-stage[_ngcontent]` (0,2,0) and laid the stage out as a ROW,
    //    so the bar sat beside the label with **width 0** while this spec went
    //    green on 25 perfectly good readings. Geometry is the only thing that
    //    can tell those two apart, so measure it while the upload is still
    //    running.
    await page.waitForFunction(
      (ceiling) =>
        (window.__uploadReadings ?? []).some(
          (r) => typeof r.value === 'number' && r.value > 0 && r.value < ceiling,
        ),
      DONE_PLATEAU,
      {timeout: 6 * 60_000},
    );
    const barBox = await page.locator('.upload-bar').first().boundingBox();
    const fillBox = await page.locator('.upload-bar-fill').first().boundingBox();
    const stageBox = await page.locator('.upload-stage').first().boundingBox();
    const geometry =
      `bar=${JSON.stringify(barBox)} fill=${JSON.stringify(fillBox)} ` +
      `stage=${JSON.stringify(stageBox)}`;
    testInfo.annotations.push({type: 'bar-geometry', description: geometry});
    console.log(`[gate] mid-upload ${geometry}`);
    expect(
      barBox?.width ?? 0,
      'The progress bar is in the DOM and publishing aria-valuenow, but it has ZERO WIDTH ' +
        `mid-upload — nothing is drawn (${geometry}). The known cause is a global rule ` +
        'in src/styles/_chat-queued.scss overriding `display` on `.upload-stage`: the ' +
        'global selector outranks the component sheet under emulated encapsulation, and a ' +
        'flex row leaves the bar with no width to fill. `.upload-stage` must stay a block.',
    ).toBeGreaterThan(0);

    const response = await uploadResponse;
    // Snapshot the instant the server answered the upload — everything here is
    // unambiguously *before* the send could be accepted.
    const readings = (await page.evaluate(() => window.__uploadReadings ?? [])) as Reading[];
    const uploadRequestHeaders = await response.request().allHeaders();

    const summary =
      `${response.status()} · ngsw-bypass=${uploadRequestHeaders['ngsw-bypass'] ?? '(absent)'} · ` +
      `readings=${JSON.stringify(readings.map((r) => r.value))}`;
    testInfo.annotations.push({type: 'upload', description: summary});
    console.log(`[gate] upload ${summary}`);
    console.log(`[gate] non-2xx responses seen: ${JSON.stringify(failedResponses)}`);

    expect(
      response.status(),
      `The upload itself failed: ${response.status()} ${await response.text()}`,
    ).toBe(200);

    // The load-bearing opt-out itself. A service worker that answers with
    // respondWith() destroys upload progress, and this header is the only
    // thing keeping ngsw's fetch handler off the request — worth pinning
    // where a real SW is actually installed rather than mocked.
    expect(
      uploadRequestHeaders['ngsw-bypass'],
      'The uploads request reached the network without ngsw-bypass. If progress still ' +
        'worked, the header is not what is protecting it; if it did not, this is the cause.',
    ).toBe('1');

    const intermediate = readings.filter(
      (r) => typeof r.value === 'number' && r.value > 0 && r.value < DONE_PLATEAU,
    );
    expect(
      intermediate.length,
      `The send indicator never took a reading strictly between 0 and ${DONE_PLATEAU}. ` +
        `Observed: ${JSON.stringify(readings.map((r) => r.value))}. ` +
        'That is the signature of an upload reporting no HttpEventType.UploadProgress at all ' +
        '(Angular FetchBackend, or a service worker that answered the request with respondWith()). ' +
        'First diagnostic: switch the uploads request to the ?ngsw-bypass=true query-param form — ' +
        'angular/angular#21191 reports the header being ignored where the param works.',
    ).toBeGreaterThan(0);

    // ── The send is accepted: the queued bubble (and its indicator) is gone.
    //    This is what "before the send is accepted" is measured against — the
    //    readings above were snapshotted while this bubble was still queued.
    await expect(page.locator('.upload-stage')).toHaveCount(0, {timeout: 120_000});
  } finally {
    rmSync(bigFile, {force: true});
    await page.request
      .delete(`${appOrigin}/api/persistent/threads/${threadId}`, {headers: {'X-CSRF': '1'}})
      .catch(() => undefined);
  }
});
