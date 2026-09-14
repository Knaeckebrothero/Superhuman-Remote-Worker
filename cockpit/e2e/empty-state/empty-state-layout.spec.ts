import {expect, test} from '@playwright/test';

// Viewports the draft landing must fit without scrolling. 412x915 is the phone
// from the 2026-09-09 usability test; 1280x700 is a laptop with a short window.
const VIEWPORTS = [
  {width: 412, height: 915, name: 'phone'},
  {width: 1280, height: 700, name: 'short laptop'},
  {width: 1660, height: 1000, name: 'desktop'},
];

for (const vp of VIEWPORTS) {
  test(`draft empty state fits without overflow at ${vp.width}x${vp.height} (${vp.name})`, async ({page}) => {
    await page.setViewportSize({width: vp.width, height: vp.height});
    await page.goto('/');

    const overflow = await page.evaluate(() => {
      const m = document.getElementById('messages')!;
      return m.scrollHeight - m.clientHeight;
    });
    expect(overflow, 'the empty state must not overflow its scrollport').toBe(0);

    // The advanced link is the last element; if it is inside the scrollport,
    // every chip above it is too.
    const advancedVisible = await page.evaluate(() => {
      const m = document.getElementById('messages')!.getBoundingClientRect();
      const a = document.getElementById('advanced')!.getBoundingClientRect();
      return a.bottom <= m.bottom;
    });
    expect(advancedVisible, '"Advanced options" must be inside the scrollport').toBe(true);

    // Two columns give each chip's text a ~150px-wide column. `.messages` sets
    // overflow-x: hidden, so a chip whose text is wider than its box clips
    // silently instead of scrolling or failing loudly — and Task 4 rewrites
    // this copy next, which is exactly the kind of change that could push a
    // chip past that width. Check every chip individually so a failure names
    // the string that broke it, not just "expected true".
    const chipOverflow = await page.evaluate(() =>
      [...document.querySelectorAll<HTMLElement>('.suggestion-text')].map((el) => ({
        text: el.textContent?.trim() ?? '',
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
      })),
    );
    for (const chip of chipOverflow) {
      expect(
        chip.scrollWidth,
        `chip text clipped horizontally: "${chip.text}" (scrollWidth ${chip.scrollWidth} > ` +
          `clientWidth ${chip.clientWidth}) — .messages has overflow-x:hidden, so this clips silently`,
      ).toBeLessThanOrEqual(chip.clientWidth);
    }

    // Scrollport backstop: a grid/flex child that can't shrink (e.g. an
    // unbreakable run of text) grows its own box to fit rather than
    // overflowing it — confirmed by deliberately forcing this with
    // `.suggestion-text { white-space: nowrap }` while writing this test: the
    // per-chip check above stayed green (each span's scrollWidth grew right
    // along with its clientWidth) while `#messages` itself measured 683 vs a
    // 412 clientWidth. `.messages` is where `overflow-x: hidden` actually
    // lives, so that's the box that must not be asked to show more than it
    // can — checking it directly is what would have caught that mutation.
    const scrollportOverflow = await page.evaluate(() => {
      const m = document.getElementById('messages')!;
      return {scrollWidth: m.scrollWidth, clientWidth: m.clientWidth};
    });
    expect(
      scrollportOverflow.scrollWidth,
      `the scrollport is wider than it can show: scrollWidth ${scrollportOverflow.scrollWidth} > ` +
        `clientWidth ${scrollportOverflow.clientWidth} (something is forcing #messages wider than the viewport)`,
    ).toBeLessThanOrEqual(scrollportOverflow.clientWidth);

    // Page-level backstop: even if #messages contains everything (its own
    // overflow-x: hidden means it usually will), something outside it — the
    // header, the composer, the shell itself — could still force the whole
    // page wider than the viewport.
    const pageOverflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(
      pageOverflow.scrollWidth,
      `page scrolls horizontally: scrollWidth ${pageOverflow.scrollWidth} > ` +
        `clientWidth ${pageOverflow.clientWidth}`,
    ).toBeLessThanOrEqual(pageOverflow.clientWidth);
  });
}
