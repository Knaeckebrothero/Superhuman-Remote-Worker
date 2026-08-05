# The canvas/settings pane slides over the chat header's buttons — the header keys its layout off the *viewport*, not off the pane it actually lives in

**Status:** **SHIPPED + PUSHED 2026-08-04 · on develop in `b92f5e68`, deployed to dev from 2026-08-05 (`sha-42a9be2`).** Both halves are in: the flexbox shrink chain (SCSS) and the measured fold into the existing `⋮` overflow menu (`headerCompact`). Browser-verified on k3d 2026-08-04 (§Verification); unit tests cover the fold decision; production build clean. Re-checked 2026-08-05 — the header rules survived `aec2e5da` untouched and the cockpit suite is green at 1737 tests.

**Filed under `done/` because the WORK shipped.** The one loose end is not a defect: `HEADER_LEFT_RESERVE_PX = 380` is *tuned by measurement against the current header chrome*, not derived from it. If the left group ever gains another permanent element, re-measure — see §Verification "Not covered here".

**Caveat on the commit:** `b92f5e68` is a `feat(cloud)` commit that swept this fix up with unrelated cloud-export work — it was authored by a concurrent session running `git add -A` over a shared tree. Nothing was lost, but `git log` for this fix reads misleadingly; search by `shouldFoldHeaderActions`, not by commit subject.
**Found:** 2026-08-04, user-reported with two screenshots — the same session at two split positions, `Disconnect` clipped mid-word in one and three more buttons gone in the other.
**Severity:** **Medium.** Not cosmetic: the clipped controls are *unreachable*, and one of them is `Disconnect`. There was no scrollbar, no ellipsis, no overflow menu — the buttons rendered outside the pane and the split area's `overflow: hidden` ate them, so the UI gave no hint anything was missing.
**Component:** cockpit `PersistentChatComponent` (`cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` header block + `.scss` `.chat-header`) · lives inside `ChatPageComponent`'s `as-split` (`cockpit/src/app/views/chat/chat-page.component.ts:48`)
**Related:** [[cockpit_session_scroll_pin_misses_late_height_changes]] — same component, same shape of fix (observe the effect, don't enumerate the causes), and this reuses that file's two-target ResizeObserver idiom.

## Symptom

Open a session, then open the Canvas **or** the Session-settings pane. Both land in the right area of the `as-split`, so the chat pane gets narrower. The chat header's action row does not react: it keeps its full width and simply extends past the pane's right edge, where it is clipped. Drag the gutter further left and more buttons disappear.

The user's words: *"the canvas or settings can extend beyond the buttons in the header without the buttons moving or resizing."* That is exactly right, and "without moving or resizing" is the diagnosis, not just the complaint.

## Root cause

### 1. The header's only responsive rule was a *viewport* media query

The header already knew how to be narrow — `@media (max-width: 768px)` hides the session id and the status label, and the template swaps the button row for an `app-icon-button` + `app-menu` overflow (`⋮`) branch gated on `viewport.isMobile()`.

`ViewportService.isMobile` is `window.matchMedia('(max-width: 768px)')`. It describes the **window**. The header lives in a split pane that the canvas can shrink to any width at all, so on a 1920px desktop the header can be handed 680px and still be told it is "desktop". Every degradation the component owned was unreachable in exactly the situation that needed it.

### 2. `.header-left` could not shrink, so the actions were pushed out rather than squeezed

```scss
.header-left { display: flex; align-items: center; gap: 8px; }   /* min-width: auto */
```

A flex item's automatic minimum size is its **min-content** size. `.header-left` contains `.header-title`, which is `white-space: nowrap` (both on the span and inside `app-inline-editable-text`), so its min-content size *is the entire session title*. `.header-left` therefore refused to shrink by even a pixel, no matter how narrow the pane got — and since `.header-right` was also `flex-shrink: 1` with buttons that cannot shrink, the overflow spilled off the end edge.

The ellipsis on `.header-title` never fired because nothing ever asked the title to be smaller. The `min-width: 0` that would have unlocked it existed **only inside the `max-width: 768px` block** — the mobile path had learned this lesson; the desktop path never did.

Measured in the running app (k3d, session `490c07b8`, pane pinned to 740px, actions unfolded), by re-applying the pre-fix rules over the fixed ones:

| | header `scrollWidth` / `clientWidth` | action row's right edge vs pane edge | `.header-left` |
|---|---|---|---|
| pre-fix CSS | **863** / 740 → **123px spills** | **x=1063** vs pane end **x=940** | 542px — holds the full title, never shrinks |
| fixed CSS | 740 / 740 → nothing spills | x=924 — inside the pane | 403px — title ellipsized |

## Fix

Two halves. Neither works alone: the CSS alone just ellipsizes the title down to nothing and *then* clips, and the fold alone still lets the title shove the buttons out above the fold threshold.

**a. Let the title yield, and never squeeze the actions** (`.scss`)

```scss
.chat-header { gap: 12px; }
.header-left  { flex: 1 1 auto; min-width: 0; overflow: hidden; }
.header-right { flex: 0 0 auto; }
```

`min-width: 0` is the load-bearing line. `overflow: hidden` guards the remaining fixed chrome once the title has given up everything it has.

**b. Fold on the header's own width, measured** (`.ts`)

`headerCompact` is now `viewport.isMobile() || headerActionsOverflow()`, where the second half is set by a `ResizeObserver`. The existing mobile `⋮` branch is reused verbatim — this adds no new markup, it just makes the branch reachable on a desktop with a narrow pane. Compact mode also drops the `Connected` text label (the dot carries it, and now has the `title`).

Two observation targets, deliberately asymmetric — the same idiom as the scroll pin:

* `.chat-header` → the space **available** (pane resize, gutter drag, sidebar collapse, window resize).
* `.header-right` → the space **demanded**. The header's own box never changes when the IDE button appears once the workspace boots, so observing only the header would fold too late or not at all.

The decision itself is a pure exported function, `shouldFoldHeaderActions(headerInnerWidth, actionsNaturalWidth, folded)`, unit tested — jsdom has no layout engine, so every geometry read there is 0 and this cannot be tested through the DOM.

**Why it is not a breakpoint.** A fixed pane width would be wrong for half the sessions: the action row is *dynamic* (Files, Git and IDE each appear conditionally, citations add a fourth, i18n changes the widths). The knob is `HEADER_LEFT_RESERVE_PX = 380` and it means "fold once the title would be squeezed below ~120px", scaling with whatever the row currently costs.

**Why `folded` is an input.** Folding shrinks the row from ~293px to ~104px. Feed that back in and it "proves" there is room → unfold → overflow → fold. So the natural width is only sampled while unfolded, and unfolding demands `HEADER_FOLD_HYSTERESIS_PX = 24` of slack on top.

## Verification

k3d, Tilt live-reload, Playwright, session `490c07b8`, viewport 1780×800, action row 293px natural (`tune`, `visibility`, `Files`, `Git`, `Disconnect`).

**Fold + unfold with no flicker.** Sweeping the chat pane down through the boundary and back up, `folded` flips exactly once in each direction, and the two thresholds are 24px apart:

| pane width | 660 | 700 | 710 | 720 | 740 | 760 | ← 740 | 720 | 710 | 700 |
|---|---|---|---|---|---|---|---|---|---|---|
| folded | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ |

Folds below 705px, unfolds above 729px, and **no width flips twice** — matching the pure function exactly (`293 > paneW − 32 − 380` ⇒ `paneW < 705`; `293 + 24 ≤ paneW − 32 − 380` ⇒ `paneW ≥ 729`).

**Nothing clips at any width.** `scrollWidth === clientWidth` at every point in the sweep, folded and unfolded.

**The fold is reachable the way the user hits it.** Clicking the header's `tune` button opens the settings pane, the header goes 1224px → 681px, the row folds to 104px, and the `⋮` menu opens carrying `Session settings` / `View options` / `Files`.

**Not covered here:** the reserve is tuned against the current chrome by measurement, not derived — if the left group grows a new permanent element, re-measure. And the RO wiring itself (both targets) is a browser check, not a unit test.

## Not affected

`canvas-pane.component.scss` already uses `flex: 1 1 auto; min-width: 0` for its growing parts and `flex: 0 0 auto` for its action groups throughout, so the canvas pane's own header does not have the mirror-image bug.
