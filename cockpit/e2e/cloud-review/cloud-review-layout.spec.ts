import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  dialog,
  ensureFilesHidden,
  ensureFilesVisible,
  openReview,
  openSession,
  shot,
  surface,
} from './harness';

/**
 * Composition gate for the review surface, run at every viewport the config
 * declares (375x667 in English and German, 768x720, 1280x720, 1440x900,
 * 1920x1080).
 *
 * The rule under test is one sentence: **a reviewer must keep a meaningful
 * view of the selected file before deciding.** At 375x667 the previous
 * revision left the viewer about 79px in the normal state, about 63px with an
 * English confirmation armed, nothing at all with a German one, and nothing at
 * all in the conflict state. Each of those is a gate that cannot be passed
 * honestly, so each is asserted here rather than described in a note.
 */

/** Floor for "meaningful view". Roughly six lines of diff plus its header —
 *  below this the pane is a label, not a view. */
const VIEWER_MIN_PX = 120;

async function box(locator: Locator): Promise<{ top: number; height: number; bottom: number }> {
  const rect = await locator.boundingBox();
  expect(rect, 'element has no layout box').not.toBeNull();
  return { top: rect!.y, height: rect!.height, bottom: rect!.y + rect!.height };
}

/**
 * The three invariants, checked together so a failure names the state.
 *
 * The viewer check is against the height actually **on screen**, not the
 * element's own height. Those diverged: with a conflict notice open, the
 * German phone layout gave the viewer a full 144px that was scrolled entirely
 * out of view — an element the gate was happy with and a reviewer could not
 * see. Measuring the intersection is what makes the assertion mean what it
 * says.
 */
async function assertComposition(page: Page, state: string): Promise<void> {
  const viewport = page.viewportSize()!;
  const viewer = await box(surface(page).locator('.review__viewer'));
  const visible = Math.min(viewer.bottom, viewport.height) - Math.max(viewer.top, 0);
  expect(visible, `${state}: viewer not on screen at ${viewport.width}x${viewport.height}`)
    .toBeGreaterThanOrEqual(VIEWER_MIN_PX);

  // The decision controls must be on screen without scrolling: they are the
  // point of the surface.
  const decision = await box(surface(page).locator('.review__decision'));
  expect(decision.height, `${state}: decision bar missing`).toBeGreaterThan(0);
  expect(decision.bottom, `${state}: decision bar below the fold`).toBeLessThanOrEqual(
    viewport.height + 1,
  );

  // Wide content (paths, diffs, conflict lists) scrolls inside its own pane;
  // the page itself must never scroll sideways.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  );
  expect(overflow, `${state}: horizontal page overflow`).toBeLessThanOrEqual(1);
}

test.describe('review composition', () => {
  test('normal, confirmation and binary states keep a usable viewer', async ({ page }, info) => {
    await openSession(page, 'senate');
    await openReview(page);
    // A review is conducted with the chooser settled, not mid-pick.
    await ensureFilesHidden(page);
    const review = surface(page);
    const label = info.project.name.replace('layout-', '');

    // --- normal --------------------------------------------------------
    await expect(review.locator('.review__monaco')).toBeVisible({ timeout: 25_000 });
    await assertComposition(page, 'normal');
    await shot(page, label, 'vp-1-normal');

    // --- confirmation --------------------------------------------------
    // Language matters here: German confirmation copy runs roughly 1.6x
    // English and is what used to leave no viewer at all on a phone.
    const apply = review.getByRole('button', { name: /Apply to cloud|In die Cloud übernehmen/ });
    await apply.click();
    await expect(review.locator('.review__decision-copy--armed')).toBeVisible();
    await assertComposition(page, 'confirmation');
    await shot(page, label, 'vp-2-confirmation');
    await page.keyboard.press('Escape');
    await expect(review.locator('.review__decision-copy--armed')).toHaveCount(0);

    // --- binary placeholder --------------------------------------------
    await ensureFilesVisible(page);
    await review.getByRole('option', { name: /edit-me\.docx/ }).click();
    await ensureFilesHidden(page);
    await expect(review.locator('.review__binary')).toBeVisible();
    await assertComposition(page, 'binary');
    await shot(page, label, 'vp-3-binary');
  });

  test('the conflict gate does not consume the viewer', async ({ page }, info) => {
    await openSession(page, 'senate', 'conflict');
    await openReview(page);
    await ensureFilesHidden(page);
    const review = surface(page);
    await review.getByRole('button', { name: /Apply to cloud|In die Cloud übernehmen/ }).click();
    await review
      .getByRole('button', { name: /Yes, apply to cloud|Ja, in die Cloud übernehmen/ })
      .click();
    await expect(review.locator('.review__notice--conflict')).toBeVisible();
    await assertComposition(page, 'conflict');
    await shot(page, info.project.name.replace('layout-', ''), 'vp-4-conflict');
  });

  test('the receipt state fits and stays dismissible', async ({ page }, info) => {
    await openSession(page, 'senate');
    await openReview(page);
    const review = surface(page);
    await review.getByRole('button', { name: /Apply to cloud|In die Cloud übernehmen/ }).click();
    await review
      .getByRole('button', { name: /Yes, apply to cloud|Ja, in die Cloud übernehmen/ })
      .click();
    await expect(review.locator('.review__state--receipt')).toBeVisible();

    const viewport = page.viewportSize()!;
    const done = await box(review.getByRole('button', { name: /Done|Fertig/ }));
    expect(done.bottom, 'receipt: Done below the fold').toBeLessThanOrEqual(viewport.height + 1);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
    await shot(page, info.project.name.replace('layout-', ''), 'vp-5-receipt');
  });

  test('the phone composition swaps the file list for a chooser', async ({ page }) => {
    const width = page.viewportSize()!.width;
    await openSession(page, 'senate');
    await expect(page.locator('app-cloud-review-banner .crb')).toBeVisible();
    await page.locator('app-cloud-review-banner .crb').getByRole('button').first().click();
    await expect(dialog(page)).toBeVisible();
    const review = surface(page);

    if (width < 768) {
      // Below the md breakpoint the always-open list is replaced by a
      // collapsed disclosure, and the details fold away — the three changes
      // that buy the viewer its height back.
      await expect(review.locator('.review__files-toggle')).toBeVisible();
      await expect(review.locator('[role="listbox"]')).toHaveCount(0);
      await expect(review.locator('details.review__tech')).not.toHaveAttribute('open', '');
      // The viewer's box before and after expanding must be identical: the
      // chooser overlays it rather than pushing it out of view, which is what
      // kept the German conflict state reviewable at 375x667.
      // Visibility can arrive during the dialog's translate/scale entrance.
      // Compare settled geometry on both sides of the chooser interaction.
      await dialog(page).evaluate(async element => {
        await Promise.all(element.getAnimations().map(animation => animation.finished));
      });
      const before = await box(review.locator('.review__viewer'));
      await review.locator('.review__files-toggle').click();
      await expect(review.locator('[role="listbox"]')).toBeVisible();
      const during = await box(review.locator('.review__viewer'));
      // Within a pixel, not exactly: the disclosure's own chevron and shadow
      // move sub-pixel boundaries around. What matters is that the viewer does
      // not move — displacement here was measured in hundreds of pixels.
      expect(Math.abs(during.top - before.top)).toBeLessThanOrEqual(2);
      expect(Math.abs(during.height - before.height)).toBeLessThanOrEqual(2);

      await review.getByRole('option').nth(2).click();
      // Picking closes it again.
      await expect(review.locator('[role="listbox"]')).toHaveCount(0);
      await assertComposition(page, 'phone-after-pick');
    } else {
      await expect(review.locator('[role="listbox"]')).toBeVisible();
      await expect(review.locator('.review__files-toggle')).toHaveCount(0);
    }
  });
});
