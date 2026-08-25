import { expect, test, type Page } from '@playwright/test';
import {
  banner,
  dialog,
  fixtureState,
  openReview,
  openSession,
  releaseApply,
  resetFixture,
  shot,
  surface,
} from './harness';

/**
 * Protected-cloud review surface, driven against a real production build.
 *
 * The fixture thread is ENDED and no WebSocket is served, so
 * `chat.isConnected()` is false for every assertion below. That is the point:
 * PC-25 hid a genuine staged diff behind the connection-gated status bar, and
 * the only escape was resuming a session with known stale-input replay.
 *
 * Nothing here can reach a real orchestrator or the preserved epoch-5 evidence
 * thread 34743d6c-9224-4866-94a9-18c3828b8b29 — the fixture server is the only
 * backend, and apply/reject are mocked.
 */

const opened = (page: Page): Promise<string[]> =>
  page.evaluate(() => (window as unknown as { __opened?: string[] }).__opened ?? []);

for (const theme of ['travertine', 'senate'] as const) {
  test.describe(`${theme} theme`, () => {
    test('surfaces a pending review on an ended session and resolves it', async ({ page }) => {
      await openSession(page, theme);

      // --- 1. Discovery, with no live agent anywhere in the picture ---------
      await expect(page.locator('.status-bar')).toHaveCount(0);
      await expect(banner(page)).toBeVisible();
      await expect(banner(page)).toContainText('4 cloud changes are waiting for your review');
      await expect(banner(page)).toContainText('Nothing has been written to your cloud yet');
      // The project's own name, not the workspace mount path.
      await expect(banner(page)).toContainText('Protected Docs');
      await expect(banner(page)).not.toContainText('· cloud ·');
      await shot(page, theme, '1-banner-ended-session');

      // --- 2. The review surface -------------------------------------------
      await openReview(page);
      const review = surface(page);
      // The project's own name, resolved and cross-checked against the
      // summary's protected_mount; the raw mount path stays in the details.
      await expect(review.locator('.review__title')).toHaveText('Protected Docs');
      await expect(review.locator('.review__meta')).toContainText('cloud');
      await expect(review.locator('.review__tally')).toHaveCount(3);
      // Session wording, not job wording.
      await expect(review).toContainText('changed in this session');
      await expect(review).not.toContainText('in this job');
      // PC-19: the project folder action, resolved and cross-checked.
      await expect(review.locator('.review__folder-link')).toHaveAttribute(
        'href',
        'https://cloud.example.invalid/apps/files/?dir=/Protected%20Docs',
      );
      // A text file gets the real diff editor. Generous timeout: Monaco is a
      // lazily-fetched chunk and this is the first mount of the run.
      await expect(review.locator('.review__monaco .monaco-diff-editor')).toBeVisible({
        timeout: 25_000,
      });
      await shot(page, theme, '2-review-text-diff');

      // --- 3. Binary placeholders ------------------------------------------
      await review.getByRole('option', { name: /edit-me\.docx/ }).click();
      await expect(review.locator('.review__binary')).toContainText('Word-processor document');
      await expect(review.locator('.review__monaco')).toHaveCount(0);
      await shot(page, theme, '3-binary-docx');

      // The PDF the summary calls text: the client sniff must still catch it.
      await review.getByRole('option', { name: /new-report\.pdf/ }).click();
      await expect(review.locator('.review__binary')).toContainText('PDF document');
      await expect(review.locator('.review__monaco')).toHaveCount(0);
      await expect(review).not.toContainText('/Type /Catalog');
      await shot(page, theme, '4-binary-pdf-mislabelled');

      // --- 4. Keyboard model ------------------------------------------------
      const options = review.locator('[role="option"]');
      // Only one option is in the tab order, and it is the selected one — so
      // that is the element a keyboard user can actually reach.
      await expect(review.locator('[role="option"][tabindex="0"]')).toHaveCount(1);
      await review.locator('[role="option"][tabindex="0"]').focus();
      await page.keyboard.press('Home');
      await expect(options.nth(0)).toHaveAttribute('aria-selected', 'true');
      await page.keyboard.press('ArrowDown');
      await expect(options.nth(1)).toHaveAttribute('aria-selected', 'true');
      await expect(options.nth(0)).toHaveAttribute('tabindex', '-1');
      await expect(options.nth(1)).toHaveAttribute('tabindex', '0');
      // Re-select the text file so the apply confirmation below is exercised
      // from the same state in both themes.
      await page.keyboard.press('Home');

      // --- 5. Two-step decision --------------------------------------------
      await review.getByRole('button', { name: 'Apply to cloud' }).click();
      await expect(review.locator('.review__decision-copy--armed')).toContainText(
        'writes all 4 reviewed changes to Protected Docs',
      );
      await shot(page, theme, '5-apply-confirmation');

      // Escape cancels the confirmation without closing the dialog, and hands
      // focus back to the control that armed it.
      await page.keyboard.press('Escape');
      await expect(dialog(page)).toBeVisible();
      await expect(review.getByRole('button', { name: 'Apply to cloud' })).toBeFocused();

      await review.getByRole('button', { name: 'Apply to cloud' }).click();
      await review.getByRole('button', { name: 'Yes, apply to cloud' }).click();

      // --- 6. Durable-on-screen outcome -------------------------------------
      await expect(review.locator('.review__state--receipt')).toBeVisible();
      await expect(review).toContainText('Applied to your cloud');
      await expect(review).toContainText('3 written, 1 deleted');
      // overlay_reset:false is surfaced, not swallowed.
      await expect(review).toContainText('may stage the same changes again');
      await expect(review).toContainText('recorded in this browser only');
      await shot(page, theme, '6-receipt-overlay-not-reset');

      expect((await fixtureState(page))['applyCalls']).toBe(1);

      // --- 7. The result is reachable again after dismissing ----------------
      await review.getByRole('button', { name: 'Done' }).click();
      await expect(dialog(page)).toHaveCount(0);
      // Nothing is pending, so the pending banner is gone — but the browser's
      // record of what happened is not a dead end any more.
      await expect(banner(page)).toContainText('Last cloud review: applied');
      await expect(banner(page)).toContainText('this browser only');
      await banner(page).getByRole('button', { name: 'View result' }).click();
      await expect(surface(page)).toContainText('Applied to your cloud');
      await shot(page, theme, '8-receipt-reentry');
    });

    test('renders the conflict gate without a force option', async ({ page }) => {
      await openSession(page, theme, 'conflict');
      await openReview(page);
      const review = surface(page);
      await review.getByRole('button', { name: 'Apply to cloud' }).click();
      await review.getByRole('button', { name: 'Yes, apply to cloud' }).click();

      await expect(review.locator('.review__notice--conflict')).toBeVisible();
      await expect(review.locator('.review__notice--conflict')).toContainText('Edited externally');
      await expect(review.getByRole('button', { name: 'Apply to cloud' })).toBeDisabled();
      await expect(review.getByRole('button', { name: 'Re-check' })).toBeEnabled();
      await shot(page, theme, '7-conflict');
    });
  });
}

test.describe('discovery and permissions', () => {
  test('the review is reachable with no status bar at all', async ({ page }) => {
    // Regression guard for PC-25 in the built artefact, not just the source.
    await openSession(page, 'senate');
    await expect(page.locator('.status-bar')).toHaveCount(0);
    await openReview(page);
    await expect(dialog(page)).toBeVisible();
  });

  test('names a permission failure instead of reporting "no changes"', async ({ page }) => {
    await openSession(page, 'senate', 'forbidden');
    // A 403 on the hidden probe leaves the count unknown — which must present
    // as an unanswered question with a way in, not as an empty folder.
    await expect(banner(page)).toContainText("Couldn't check for pending cloud changes");
    await banner(page).getByRole('button', { name: 'Open review' }).click();
    const review = surface(page);
    await expect(review).toContainText("You can't review these changes");
    await expect(review).toContainText('Only the owner of this session');
    // The false statement this replaces.
    await expect(review).not.toContainText('No changes to review');
    await expect(review).not.toContainText('Nothing staged for review');
    // A refusal offers no decision controls.
    await expect(review.getByRole('button', { name: 'Apply to cloud' })).toHaveCount(0);
  });

  test('keeps an ended session recoverable after a failed count check', async ({ page }) => {
    // One transient probe failure used to leave a protected ended session with
    // no entry point to its review and no way to ask again.
    await openSession(page, 'senate', 'probeFail');
    await expect(banner(page)).toContainText("Couldn't check for pending cloud changes");
    await expect(banner(page)).not.toContainText('waiting for your review');
    await banner(page).getByRole('button', { name: 'Check again' }).click();
    // The second probe succeeds, and the real pending review appears.
    await expect(banner(page)).toContainText('4 cloud changes are waiting for your review');
    await openReview(page);
    await expect(surface(page).locator('[role="option"]')).toHaveCount(4);
  });
});

test.describe('in-flight and refused decisions', () => {
  test('shows progress and refuses every dismissal while applying', async ({ page }) => {
    // The apply is HELD by the fixture until released below, so the in-flight
    // window is deterministic rather than a race against a timer.
    await openSession(page, 'senate', 'holdApply');
    await openReview(page);
    const review = surface(page);
    await review.getByRole('button', { name: 'Apply to cloud' }).click();
    await review.getByRole('button', { name: 'Yes, apply to cloud' }).click();

    // An explicit progress state, not two disabled buttons.
    await expect(review.locator('.review__decision-copy--running')).toBeVisible();
    await expect(review).toContainText('Applying your changes to the cloud');
    await expect(review).toContainText('Keep this open');
    await expect(surface(page)).toHaveAttribute('aria-busy', 'true');
    // No decision control is mounted at all, so a second submit is impossible.
    await expect(review.getByRole('button', { name: 'Yes, apply to cloud' })).toHaveCount(0);

    // Every dismissal path is inert while the write is in flight (PC-20).
    await page.keyboard.press('Escape');
    await expect(dialog(page)).toBeVisible();
    await expect(page.locator('.app-dialog__close')).toHaveCount(0);
    await page.locator('.app-dialog__backdrop').click({ position: { x: 5, y: 5 } });
    await expect(dialog(page)).toBeVisible();

    await releaseApply(page);
    await expect(review.locator('.review__state--receipt')).toBeVisible({ timeout: 15_000 });
    expect((await fixtureState(page))['applyCalls']).toBe(1);
  });

  test('reloads instead of leaving stale controls after a refused reject', async ({ page }) => {
    await openSession(page, 'senate', 'rejectStale');
    await openReview(page);
    const review = surface(page);
    await review.getByRole('button', { name: 'Reject staged changes' }).click();
    await review.getByRole('button', { name: 'Yes, discard them' }).click();

    // Nothing was discarded, so nothing may claim it was...
    await expect(review).not.toContainText('Staged changes discarded');
    // ...and the surface is showing a freshly read diff, not the refused one.
    await expect(review.getByRole('button', { name: 'Reject staged changes' })).toBeEnabled();
    expect(Number((await fixtureState(page))['summaryCalls'])).toBeGreaterThan(2);
  });

  test('reports a refused reject where the controls are', async ({ page }) => {
    await openSession(page, 'senate', 'rejectRefused');
    await openReview(page);
    const review = surface(page);
    await review.getByRole('button', { name: 'Reject staged changes' }).click();
    await review.getByRole('button', { name: 'Yes, discard them' }).click();
    await expect(review.locator('.review__decision-copy--error')).toBeVisible();
    await expect(review).not.toContainText('Staged changes discarded');
  });
});

test.describe('per-file reads', () => {
  test('retry re-requests the file rather than doing nothing', async ({ page }) => {
    await openSession(page, 'senate', 'fileFlaky');
    await openReview(page);
    const review = surface(page);
    await expect(review).toContainText("Couldn't load this file");
    const before = Number((await fixtureState(page))['fileCalls']);
    await review.getByRole('button', { name: 'Try again' }).click();
    await expect(review.locator('.review__monaco')).toBeVisible({ timeout: 25_000 });
    expect(Number((await fixtureState(page))['fileCalls'])).toBeGreaterThan(before);
  });

  test('explains a path that left the staged set', async ({ page }) => {
    await openSession(page, 'senate', 'fileGone');
    await openReview(page);
    await expect(surface(page)).toContainText('it is not any more');
  });

  test('explains staged content that cannot be read', async ({ page }) => {
    // The case the old copy got wrong for every reviewer who hit it.
    await openSession(page, 'senate', 'fileUnreadable');
    await openReview(page);
    await expect(surface(page)).toContainText('stored copy cannot be read');
    await expect(surface(page)).not.toContainText('the session re-staged');
  });
});

test.describe('project folder navigation (PC-19)', () => {
  test('stays available outside the review, before and after a decision', async ({ page }) => {
    await openSession(page, 'senate');

    // Two unambiguous actions, neither of them a guess.
    const project = page.getByRole('button', { name: 'Project files' });
    const session = page.getByRole('button', { name: 'Session files' });
    await expect(project).toBeVisible();
    await expect(session).toBeVisible();

    await project.click();
    expect(await opened(page)).toEqual([
      'https://cloud.example.invalid/apps/files/?dir=/Protected%20Docs',
    ]);

    // Resolve the diff, then confirm the action survives the decision AND a
    // full reload — the previous fix lived only inside the open review.
    await openReview(page);
    await surface(page).getByRole('button', { name: 'Apply to cloud' }).click();
    await surface(page).getByRole('button', { name: 'Yes, apply to cloud' }).click();
    await surface(page).getByRole('button', { name: 'Done' }).click();
    await expect(page.getByRole('button', { name: 'Project files' })).toBeVisible();

    await page.reload({ waitUntil: 'domcontentloaded' });
    await expect(page.getByRole('button', { name: 'Project files' })).toBeVisible();
    await page.getByRole('button', { name: 'Project files' }).click();
    expect(await opened(page)).toEqual([
      'https://cloud.example.invalid/apps/files/?dir=/Protected%20Docs',
    ]);
  });
});

test.describe('escape ownership', () => {
  test('cancels the confirmation with focus outside any control', async ({ page }) => {
    await openSession(page, 'senate');
    await openReview(page);
    const review = surface(page);
    await review.getByRole('button', { name: 'Apply to cloud' }).click();
    // Arming removes the pressed button; blur whatever has focus so the key
    // is dispatched with <body> as the target — the case a host-element
    // listener silently misses.
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    await page.keyboard.press('Escape');
    // The confirmation is cancelled and the dialog is still open.
    await expect(dialog(page)).toBeVisible();
    await expect(review.getByRole('button', { name: 'Apply to cloud' })).toBeVisible();
    // A second Escape, with nothing armed, belongs to the dialog.
    await page.keyboard.press('Escape');
    await expect(dialog(page)).toHaveCount(0);
  });
});

/**
 * Job context on /jobs/review. Two jobs at once: it proves the shared surface
 * still hosts inline (no dialog, no WebSocket) after the redesign, and it is
 * the only host reachable in a pre-redesign build — which is what makes the
 * matched before/after capture possible.
 */
for (const theme of ['travertine', 'senate'] as const) {
  test(`${theme}: job-context review renders inline with job wording`, async ({ page }) => {
    await page.addInitScript((value) => {
      try {
        localStorage.setItem('cockpit:theme', value);
      } catch {
        /* private mode */
      }
    }, theme);
    await page.goto('/jobs/review', { waitUntil: 'domcontentloaded' });
    await expect(page.locator(`body.theme-${theme}`)).toHaveCount(1);

    const review = page.locator('app-job-diff-review');
    await expect(review).toBeVisible({ timeout: 20_000 });
    if (process.env['CLOUD_REVIEW_LEGACY'] === '1') {
      // Pre-redesign build: capture whatever it renders and stop. None of the
      // assertions below describe it — that is the point of the comparison.
      await page.waitForTimeout(1500);
      await shot(page, theme, '0-job-review-surface');
      return;
    }
    await expect(review).toContainText('changed in this job');
    await expect(review).not.toContainText('in this session');
    await expect(review.getByRole('button', { name: 'Accept all changes' })).toBeVisible();
    await expect(review.locator('[role="option"]')).toHaveCount(4);
    await shot(page, theme, '0-job-review-surface');
  });
}

/**
 * Pre-redesign capture only (CLOUD_REVIEW_LEGACY=1, pointed at a build of the
 * parent commit). Documents the PC-25 state at the same viewport as the
 * `after-*-1-banner-ended-session` shots: an ended protected session with a
 * genuine four-file staged diff and no way whatsoever to reach it.
 */
if (process.env['CLOUD_REVIEW_LEGACY'] === '1') {
  for (const theme of ['travertine', 'senate'] as const) {
    test(`${theme}: legacy ended session offers no review affordance`, async ({ page }) => {
      await openSession(page, theme);
      await resetFixture(page, 'pending');
      await page.waitForTimeout(1500);
      await expect(page.locator('app-cloud-review-banner')).toHaveCount(0);
      await expect(page.locator('.status-bar')).toHaveCount(0);
      await expect(page.getByText('Cloud changes')).toHaveCount(0);
      await shot(page, theme, '1-banner-ended-session');
    });
  }
}