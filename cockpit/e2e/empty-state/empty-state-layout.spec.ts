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
  });
}
