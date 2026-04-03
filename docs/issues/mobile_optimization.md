# Mobile Optimization Issues & Suggestions

## Quick Fixes Applied

The following issues were fixed directly:

### 1. Global button sizing breaking inline action buttons
**File:** `cockpit/src/styles.scss`
**Problem:** `min-height: 44px; min-width: 44px` applied to ALL `button` elements on mobile, inflating tiny action buttons in the job list table.
**Fix:** Scoped the 44px WCAG touch-target rule to navigation/primary buttons only (`.mobile-tab-bar button`, `.send-btn`, `.stop-btn`, `.btn-primary`, `.btn`).

### 2. Mobile header overflowing viewport (root cause of page-level scrollbar)
**Files:** `cockpit/src/app/simple/layout/mobile-shell/mobile-shell.component.ts`, `cockpit/src/app/app.ts`
**Problem:** The mobile header contained a title (40px), model select (160px), and job select (200px) plus gaps/padding totaling ~456px -- overflowing viewports under ~460px (Pixel 7 at 412px, iPhone SE at 375px). The `content-area` had `overflow: auto` which displayed the horizontal scrollbar.
**Fix:**
- Made both selects flex-shrinkable with `min-width: 0; flex: 1 1 auto` and reduced max-widths
- Added `overflow: hidden` on `.mobile-header` and `.mobile-shell`
- Changed `content-area` from `overflow: auto` to `overflow-y: auto; overflow-x: hidden`
- Reduced header gap/padding from 8px/12px to 6px/10px

### 3. Builder horizontal scrollbar on mobile
**File:** `cockpit/src/app/shared/components/instruction-builder/instruction-builder.component.ts`
**Problem:** Markdown content (code blocks, tables) inside chat messages can overflow horizontally, creating a page-level horizontal scrollbar visible on phones.
**Fix:**
- Added `overflow-x: hidden` on `.builder-container` and `.messages-container`
- Added `min-width: 0` on `.message` and `overflow: hidden` on `.message-body` to contain flex children
- Added `overflow-x: auto` on `.message-content` so code blocks scroll within their bubble
- Added mobile media query: `max-width` constraints on `pre` and `table` elements, iOS zoom prevention (`font-size: 16px` on input), safe-area padding

### 4. Job list table on mobile
**File:** `cockpit/src/app/shared/components/job-list/job-list.component.ts`
**Problem:** Job table showed full UUIDs, a "Created" column, and many action buttons in a single `nowrap` row -- causing horizontal overflow or extreme cramping.
**Fix:**
- Hidden UUIDs (`.job-id`) and "Created" column on mobile
- Set `table-layout: fixed` with column widths: Job 40%, Status 25%, Actions 35%
- Allowed action buttons to wrap (`white-space: normal` on actions cell, `display: inline-block`)
- Reduced padding, font sizes, and filter chip sizes for mobile density

---

## Bigger Redesign Suggestions (Job Component)

The table-based job list works well on desktop but is fundamentally constrained on mobile. The following changes would require more significant refactoring:

### A. Card-based mobile layout for job list
**Priority:** High
**Effort:** Medium

Replace the `<table>` with a card layout on mobile. Each job becomes a stacked card:

```
+-------------------------------------+
| Test Job New                        |
| Cancelled        Mar 31, 15:30      |
| [View] [Workspace] [Resume] [Del]  |
+-------------------------------------+
```

**Why:** Tables are inherently desktop-oriented. A card layout gives full width for the job description, a clear status line, and a dedicated row for actions. Filter chips could become a horizontal scrollable strip.

**Implementation approach:**
- Use `@if (viewport.isMobile())` to conditionally render a card list vs. the table
- Reuse the same `displayRows()` computed signal
- Cards can use the existing status-badge and action-btn styles

### B. Swipe actions on job cards
**Priority:** Low
**Effort:** High

On mobile, primary actions (View, Delete) could be revealed by swiping a job card left/right, reducing visual clutter. Secondary actions could live behind a "..." overflow menu.

### C. Collapsible filter bar
**Priority:** Medium
**Effort:** Low

The 11 filter chips take 3 rows on mobile. Consider:
- A single-row horizontal scroll strip (overflow-x: auto, hide scrollbar)
- Or a dropdown/bottom-sheet filter selector

### D. Bottom sheet for job actions
**Priority:** Medium
**Effort:** Medium

Instead of inline action buttons per row, tapping a job card could open a bottom sheet with full-size action buttons. This pattern is standard on mobile (iOS action sheets, Android bottom sheets).

**Benefits:**
- Cleaner card layout (no action buttons visible per row)
- Larger, easier-to-tap action buttons
- Room for additional context (job ID, full description, timestamps)

### E. Pull-to-refresh
**Priority:** Low
**Effort:** Medium

Replace the "Refresh" button with pull-to-refresh gesture on the job list, which is the expected mobile pattern.

### F. Create page form density
**Priority:** Low
**Effort:** Low

The Create page works reasonably well on mobile already, but the expert cards could be made more compact (horizontal layout with smaller icons) to reduce scrolling.
