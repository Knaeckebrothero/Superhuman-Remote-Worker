import type { Response } from '@playwright/test';
import { expect, test } from './app.fixture';
import { BASE_ORIGIN } from './environment';
import { expectedReplyWasConsumed } from './provider-control';

const PROVISIONING_TIMEOUT_MS = 180_000;
const REPLY_TIMEOUT_MS = 120_000;

function pathname(response: Response): string {
  return new URL(response.url()).pathname;
}

function expectSuccessfulResponse(response: Response, label: string): void {
  expect(response.status(), `${label} returned HTTP ${response.status()}.`).toBeGreaterThanOrEqual(
    200,
  );
  expect(response.status(), `${label} returned HTTP ${response.status()}.`).toBeLessThan(300);
}

async function waitForEnabledSendOrError(page: import('@playwright/test').Page): Promise<void> {
  const outcome = await page
    .waitForFunction(
      () => {
        if (document.querySelector('[data-testid="chat-error"]')) return 'error';
        const send = document.querySelector<HTMLButtonElement>('[data-testid="chat-send"]');
        return send && !send.disabled ? 'send' : false;
      },
      undefined,
      { timeout: 30_000 },
    )
    .then((handle) => handle.jsonValue());
  if (outcome === 'error') throw new Error('The chat error surface won the race against Send.');
  expect(outcome).toBe('send');
}

test('first message creates a durable session and renders the reply', async ({ app }) => {
  const { page, runId, runToken, expectedReply, chatModel } = app;
  const message = `${runToken} first message through the live application`;

  app.setPhase('pre-navigation');
  const threadsBeforeNavigation = await app.listThreadIds();

  app.setPhase('landing');
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  const landingUrl = new URL(page.url());
  expect(landingUrl.origin).toBe(BASE_ORIGIN);
  expect(landingUrl.pathname).toBe('/');

  const composer = page.getByTestId('chat-composer');
  await expect(composer).toBeVisible();
  await expect(composer).toBeEnabled();
  await expect(page.getByTestId('chat-error')).toHaveCount(0);

  const threadsAfterNavigation = await app.listThreadIds();
  expect([...threadsAfterNavigation].sort()).toEqual([...threadsBeforeNavigation].sort());

  // Empty composer intentionally renders the microphone action. Fill first,
  // then require the real Send action to become enabled.
  await composer.fill(message);
  await waitForEnabledSendOrError(page);
  const send = page.getByTestId('chat-send');

  // Parse and ledger the id in the response continuation itself. UI evidence
  // below is deliberately concurrent; if it fails, it must not win a race
  // that leaves a successfully-created server resource unregistered.
  const createdThreadPromise = page
    .waitForResponse(
      (response) =>
        response.request().method() === 'POST' && pathname(response) === '/api/persistent/threads',
      { timeout: PROVISIONING_TIMEOUT_MS },
    )
    .then(async (response) => {
      expectSuccessfulResponse(response, 'first-send thread creation');
      const created = (await response.json()) as { thread_id?: unknown };
      expect(typeof created.thread_id).toBe('string');
      const threadId = created.thread_id as string;
      app.registerThread(threadId);
      return threadId;
    });
  void createdThreadPromise.catch(() => undefined);
  const inputResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      /^\/api\/persistent\/threads\/[^/]+\/input$/.test(pathname(response)),
    { timeout: PROVISIONING_TIMEOUT_MS },
  );
  // Avoid an unhandled rejection if creation itself fails and the input
  // observer is consequently cancelled during fixture teardown.
  void inputResponsePromise.catch(() => undefined);

  app.setPhase('creating');
  await send.click();

  const optimisticUser = page.getByTestId('chat-message-user').filter({ hasText: runToken });
  const immediateUiEvidence = Promise.all([
    expect(optimisticUser).toHaveCount(1, { timeout: 10_000 }),
    expect(optimisticUser.getByText(message, { exact: true })).toHaveCount(1),
    expect(page.getByTestId('chat-startup')).toBeVisible({ timeout: 30_000 }),
  ]).then(
    () => null,
    (error: unknown) => error,
  );

  const threadId = await createdThreadPromise;
  const admittedTopology = await app.threadTopology(threadId);
  expect(admittedTopology.executionLane).toBe(app.expectedExecutionLane);
  expect(admittedTopology.workspaceBackend).toBe(app.workspaceBackend);
  const uiEvidenceError = await immediateUiEvidence;
  if (uiEvidenceError) throw uiEvidenceError;

  await expect(page).toHaveURL((url) => url.pathname === `/sessions/${threadId}`, {
    timeout: PROVISIONING_TIMEOUT_MS,
  });

  app.setPhase('turn');
  const inputResponse = await inputResponsePromise;
  expect(pathname(inputResponse)).toBe(`/api/persistent/threads/${threadId}/input`);
  expect(
    inputResponse.status(),
    'The single-send P0 path must reject a duplicate/in-flight 409 even though ' +
      'the UI tolerates it.',
  ).not.toBe(409);
  expectSuccessfulResponse(inputResponse, 'first queued input');

  const assistant = page.getByTestId('chat-message-assistant').filter({ hasText: expectedReply });
  await expect(assistant).toHaveCount(1, { timeout: REPLY_TIMEOUT_MS });
  await expect(assistant.getByText(expectedReply, { exact: true })).toHaveCount(1);
  await expect(page.getByTestId('chat-startup')).toHaveCount(0);
  await expect(page.getByTestId('chat-error')).toHaveCount(0);
  await expect(composer).toBeVisible();
  await expect(composer).toBeEnabled();
  await expect(composer).toHaveValue('');

  await expect
    .poll(() => app.threadTopology(threadId), {
      timeout: 30_000,
      intervals: [250, 500, 1_000],
      message: 'The selected E2E topology must be observable after turn completion.',
    })
    .toEqual({
      executionLane: app.expectedExecutionLane,
      workspaceBackend: app.workspaceBackend,
      workspaceStatus: app.workspaceBackend === 'sandbox' ? 'ready' : null,
      workspaceProvisioner: app.workspaceBackend === 'sandbox' ? 'k8s' : null,
    });

  // Assistant text can reach the DOM one event before turn.completed. A
  // harmless fill/clear makes the dynamic action render and proves it has
  // returned to enabled Send semantics rather than remaining Stop/pending.
  await composer.fill(`${runToken} idle-state probe`);
  const idleSend = page.getByTestId('chat-send');
  await expect(idleSend).toBeVisible();
  await expect(idleSend).toBeEnabled();
  await expect(idleSend).toHaveAccessibleName(/^(Send|Senden)$/);
  await composer.fill('');
  await expect(composer).toHaveValue('');

  expect(app.network.responses('POST', '/api/persistent/threads')).toHaveLength(1);
  expect(app.network.responses('POST', `/api/persistent/threads/${threadId}/input`)).toHaveLength(
    1,
  );

  app.setPhase('reload');
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect(page).toHaveURL((url) => url.pathname === `/sessions/${threadId}`);
  await expect(composer).toBeVisible({ timeout: PROVISIONING_TIMEOUT_MS });
  await expect(composer).toBeEnabled({ timeout: PROVISIONING_TIMEOUT_MS });
  app.setPhase('hydration');

  const hydratedUser = page.getByTestId('chat-message-user').filter({ hasText: runToken });
  const hydratedAssistant = page
    .getByTestId('chat-message-assistant')
    .filter({ hasText: expectedReply });
  await expect(hydratedUser).toHaveCount(1);
  await expect(hydratedUser.getByText(message, { exact: true })).toHaveCount(1);
  await expect(hydratedAssistant).toHaveCount(1);
  await expect(hydratedAssistant.getByText(expectedReply, { exact: true })).toHaveCount(1);
  await expect(page.getByTestId('chat-error')).toHaveCount(0);

  const sessionsLink = page.getByRole('link', { name: /^(Sessions|Sitzungen)$/ });
  await expect(sessionsLink).toHaveCount(1);
  app.setPhase('list-navigation');
  await sessionsLink.click();
  await expect(page).toHaveURL((url) => url.pathname === '/sessions');
  app.setPhase('sessions-list');

  const matchingCard = page.getByTestId('session-card').filter({ hasText: runToken });
  await expect(matchingCard).toHaveCount(1, { timeout: 30_000 });
  await expect(matchingCard).toHaveAttribute('data-thread-id', threadId);

  await expect
    .poll(
      async () => {
        const state = await app.provider.state(runId);
        const overview = await app.provider.overview();
        return {
          run_id: state.run_id,
          required_reply_consumed: expectedReplyWasConsumed(state, runId, chatModel),
          unexpected_count: state.unexpected_count,
          unscoped_unexpected_calls: overview.unscoped_unexpected_calls,
          armed_run_ids: overview.runs.map((run) => run.run_id),
        };
      },
      { timeout: 30_000, intervals: [250, 500, 1_000] },
    )
    .toEqual({
      run_id: runId,
      required_reply_consumed: true,
      unexpected_count: 0,
      unscoped_unexpected_calls: 0,
      armed_run_ids: [runId],
    });
});
