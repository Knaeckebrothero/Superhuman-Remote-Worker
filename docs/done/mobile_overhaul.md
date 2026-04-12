# Feature: Mobile View Overhaul

## Problem

The mobile view is outdated and broken in several ways:

1. **Separate navigation paradigm** -- `MobileShellComponent` dynamically loads components via `ViewContainerRef` instead of using the same Angular routing as desktop. This creates a fork that drifts out of sync with the desktop experience.

2. **Stale tab lineup** -- The 5 tabs (Builder, Jobs, Projects, Create, Review) don't reflect current navigation. Sessions are missing entirely. "Review" now redirects to Inbox at the route level (`/review` -> `/inbox`), so the tab renders the old standalone `JobReviewComponent` instead.

3. **Outdated header controls** -- The mobile header has a job-context `<select>` dropdown (`mobile-job-select`) that doesn't exist on desktop and serves no current purpose. Session management, which desktop has in its header, is completely absent.

4. **Sidebar doesn't work on mobile** -- `app.ts` gates sidebar rendering with `!viewport.isMobile()`, so the `<app-sidebar>` component never renders on mobile. The `SidebarToggleComponent` used in all mobile page wrappers calls `sidebar.expand()`, but since the sidebar DOM element doesn't exist, nothing happens. Users on mobile have no way to reach secondary pages (Projects, Data Sources, Settings, Debug) except by typing URLs.

5. **Two component systems** -- Mobile uses `ComponentRegistryService` to dynamically instantiate tab content, while desktop uses standard Angular routing. Same components, two loading mechanisms. When one side gets updated, the other doesn't.

6. **Components missing responsive CSS** -- Sessions page, Session Create, and Persistent Chat have zero `@media` queries. Hardcoded `max-width: 800px` on sessions, inflexible grids on session-create (`minmax(180px, 1fr)` too wide for phones), and no mobile optimization on persistent chat headers.

7. **Existing mobile bugs** -- Instruction builder has duplicate media queries (lines 761 and 974). Inbox page creates its own `isMobile` signal via `window.innerWidth` instead of injecting `ViewportService`. Instruction builder uses `calc(100vw - 80px)` for markdown width, leaving only ~64px on iPhone SE. Filter chips are ~20px tall, well below the 44px touch target minimum.

## Goals

- **One navigation system** -- Mobile and desktop both use Angular Router. No dynamic component instantiation for navigation.
- **Feature parity** -- Sessions, Inbox, and all current desktop features accessible on mobile.
- **Remove dead code** -- Eliminate `MobileShellComponent` and the separate mobile navigation paradigm.
- **Sidebar works everywhere** -- Mobile gets the sidebar as a slide-in overlay for secondary navigation.
- **Touch-correct** -- All interactive elements meet 48x48px touch targets (M3 standard).

---

## Architecture Changes

### 1. Sidebar: Always render, overlay on mobile

**File: `cockpit/src/app/app.ts`**

Current behavior:
```typescript
readonly showSidebar = computed(
  () => !this.viewport.isMobile() && this.userService.isAuthenticated() && this.userService.isApproved(),
);
```

Change to:
```typescript
readonly showSidebar = computed(
  () => this.userService.isAuthenticated() && this.userService.isApproved(),
);
```

On mobile, the sidebar starts collapsed (width: 0) and slides over content as a fixed overlay when toggled. Desktop behavior unchanged.

**Template additions to `app.ts`:**
- Backdrop `<div>` shown when sidebar is open on mobile -- clicking it collapses sidebar
- Bottom nav `<nav>` shown on mobile when authenticated (see section 2)

**Sidebar overlay CSS (in `app.ts` styles):**
```scss
@media (max-width: 768px) {
  app-sidebar {
    position: fixed;
    top: 0;
    left: 0;
    z-index: 1000;
    height: 100dvh;
    transform: translateX(-100%);
    transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    will-change: transform;
  }

  app-sidebar:not(.collapsed) {
    transform: translateX(0);
  }

  .sidebar-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(17, 17, 27, 0.6);   /* crust at 60% */
    backdrop-filter: blur(8px);
    z-index: 999;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  }

  .sidebar-backdrop.visible {
    opacity: 1;
    pointer-events: auto;
  }
}
```

Use `cubic-bezier(0.4, 0, 0.2, 1)` (Material Design standard easing) for the slide transition. 300ms duration -- under 400ms feels snappy, over 400ms feels sluggish.

**Body scroll lock** when sidebar is open on mobile:
```typescript
effect(() => {
  if (this.viewport.isMobile() && !this.sidebar.collapsed()) {
    document.body.style.overflow = 'hidden';
    document.body.style.touchAction = 'none';
  } else {
    document.body.style.overflow = '';
    document.body.style.touchAction = '';
  }
});
```

**File: `cockpit/src/app/layout/sidebar/sidebar.component.ts`**

Auto-collapse sidebar when any nav link is clicked on mobile:
```typescript
private router = inject(Router);
private viewport = inject(ViewportService);

constructor() {
  this.router.events.pipe(
    filter(e => e instanceof NavigationEnd),
    takeUntilDestroyed()
  ).subscribe(() => {
    if (this.viewport.isMobile()) {
      this.sidebar.collapse();
    }
  });
}
```

**Sidebar max-width:** M3 specifies 280dp for mobile drawers. The current sidebar is 200px -- keep it, it's already narrower than the guideline and leaves content visible behind the backdrop.

```
+-------------------------------+        +-------------------------------+
| Desktop                       |        | Mobile (sidebar open)         |
|                               |        |                               |
| +--------+------------------+ |        | +--------+------------------+ |
| |Sidebar |  Content         | |        | |Sidebar | Backdrop (blur)  | |
| | 200px  |  (router-outlet) | |        | |(fixed  |                  | |
| |        |                  | |        | | 200px) |                  | |
| +--------+------------------+ |        | +--------+------------------+ |
|                               |        |           + Bottom Nav        |
+-------------------------------+        +-------------------------------+
```

### 2. Mobile bottom nav bar

**File: `cockpit/src/app/app.ts`**

Route-based bottom navigation using `routerLink` (same mechanism as desktop sidebar):

```
+----------+----------+------+-------+--------+
|  Builder | Sessions | Jobs | Inbox | Create |
+----------+----------+------+-------+--------+
```

| Tab      | Icon         | Route       | routerLinkActiveOptions   |
|----------|--------------|-------------|---------------------------|
| Builder  | construction | `/`         | `{ exact: true }`         |
| Sessions | chat         | `/sessions` | default (matches children)|
| Jobs     | work         | `/jobs`     | default                   |
| Inbox    | inbox        | `/inbox`    | default                   |
| Create   | add_circle   | `/create`   | default                   |

**Why these 5:** M3 and Apple HIG agree on 3-5 tabs maximum. Sessions and Inbox are the two highest-use features missing from mobile. Projects, Data Sources, Settings, and Debug are accessible via the sidebar overlay. `exact: true` only on the root route `/` so it doesn't match every path. Sessions tab stays highlighted on `/sessions/:threadId` child routes.

**Accessibility:** Add `ariaCurrentWhenActive="page"` on each tab link for screen readers.

**Sizing and spacing (per M3 specs + WCAG):**

| Property                    | Value                                  |
|-----------------------------|----------------------------------------|
| Bar height                  | 56px content + `env(safe-area-inset-bottom)` |
| Tab touch target            | 48x48px minimum (icon can be 24px, padded) |
| Icon size                   | 24px                                   |
| Label font-size             | 10px, weight 500                       |
| Gap between icon and label  | 4px                                    |
| Bar background              | `var(--timeline-bg, #11111b)` (mantle) |
| Bar border-top              | `1px solid var(--border-color, #313244)` |
| Active tab color            | `var(--accent-color, #cba6f7)`         |
| Inactive tab color          | `var(--text-muted, #6c7086)`           |
| Bar z-index                 | 900 (below sidebar overlay at 1000)    |
| Position                    | `fixed`, bottom: 0                     |

**Active state indication** -- use two visual differentiators per M3 guidance:
1. Color change: accent color on active, muted on inactive
2. Font weight change: 600 on active label, 500 on inactive

**Body padding:** Add `padding-bottom: calc(56px + env(safe-area-inset-bottom, 0px))` to `.content-area` on mobile to prevent content from being hidden behind the fixed bottom nav.

**Visibility:** Only shown when `viewport.isMobile() && isAuthenticated && isApproved`. Hidden on pending-approval screen.

**Template structure:**
```html
@if (showMobileNav()) {
  <nav class="mobile-nav">
    <a routerLink="/" routerLinkActive="active"
       [routerLinkActiveOptions]="{ exact: true }"
       ariaCurrentWhenActive="page" class="nav-tab">
      <span class="nav-tab-icon">construction</span>
      <span class="nav-tab-label">Builder</span>
    </a>
    <!-- ... remaining tabs ... -->
  </nav>
}
```

### 3. Remove MobileShellComponent and the desktop/mobile fork

**File: `cockpit/src/app/simple/pages/shell/shell.component.ts`**

Remove the conditional branching -- always render the same content:

```typescript
// BEFORE
@if (viewport.isMobile()) {
  <app-mobile-shell />
} @else {
  <div class="page">...</div>
}

// AFTER -- single layout, responsive via CSS
<div class="page">
  <header class="page-header">
    <app-sidebar-toggle />
    <!-- session controls (dropdown) -->
    <div class="header-spacer"></div>
    <!-- model controls (dropdown) -->
    <!-- streaming badge -->
  </header>
  <main class="page-content">
    <app-instruction-builder />
  </main>
</div>
```

Remove `MobileShellComponent` import. Remove `ViewportService` injection (no longer needed in this component).

**Files to delete:**
- `cockpit/src/app/simple/layout/mobile-shell/mobile-shell.component.ts`
- `cockpit/src/app/simple/pages/review/review-page.component.ts` (unreachable -- `/review` redirects to `/inbox`)

### 4. Responsive header controls in shell

**File: `cockpit/src/app/simple/pages/shell/shell.component.ts`**

The desktop header has session and model dropdown buttons. Make these responsive:

```scss
@media (max-width: 768px) {
  .page-header {
    gap: 4px;
    padding: 0 8px;
    height: 44px;           /* slightly compact */
  }

  .session-title-btn,
  .model-title-btn {
    font-size: 11px;
    padding: 4px 8px;
    min-height: 36px;
  }

  .session-title-btn {
    max-width: 140px;
  }

  .model-title-btn {
    max-width: 120px;
  }

  /* Dropdowns become full-width bottom-anchored panels on mobile */
  .session-dropdown,
  .model-dropdown {
    position: fixed;
    left: 8px;
    right: 8px;
    bottom: calc(56px + env(safe-area-inset-bottom, 0px) + 8px);
    top: auto;
    min-width: unset;
    max-height: 50vh;
    border-radius: 12px;
    box-shadow: 0 -4px 16px rgba(17, 17, 27, 0.4);
  }
}
```

Dropdowns anchor above the bottom nav bar on mobile (using `bottom: calc(nav height + 8px)`). This follows the bottom sheet pattern -- content appears near the thumb zone rather than at the top of the screen.

### 5. Keep thin page wrappers, they now work

The existing page wrappers (`jobs-page`, `create-page`, `datasources-page`, etc.) already follow the correct pattern: `<app-sidebar-toggle />` + shared component. Once the sidebar renders on mobile (change #1), the toggle buttons actually work. No changes needed.

### 6. Route transitions (optional enhancement)

**File: `cockpit/src/app/app.config.ts`**

Add `withViewTransitions()` for subtle cross-fade between route changes. This is progressive enhancement -- browsers without support get instant swaps:

```typescript
import { provideRouter, withViewTransitions } from '@angular/router';

provideRouter(routes, withViewTransitions())
```

Global CSS for the transition:
```css
::view-transition-old(root) {
  animation: 150ms ease-out forwards fade-out;
}
::view-transition-new(root) {
  animation: 150ms ease-in forwards fade-in;
}
@keyframes fade-out { to { opacity: 0; } }
@keyframes fade-in { from { opacity: 0; } }
```

### 7. Sidebar mobile start state

**File: `cockpit/src/app/core/services/sidebar.service.ts`**

On mobile, sidebar should start collapsed. Currently defaults to `false` (expanded). Either:
- Initialize based on viewport: `collapsed = signal(window.matchMedia('(max-width: 768px)').matches)`
- Or handle purely in CSS (sidebar is always rendered, transform handles visibility on mobile regardless of signal state)

The CSS approach is cleaner -- on mobile the sidebar is off-screen via `transform: translateX(-100%)` when `.collapsed`, so the existing `collapsed = signal(false)` just means on desktop it starts visible (current behavior). On mobile it starts off-screen via CSS. No service change needed if CSS is authored correctly:

```scss
/* Desktop: collapsed class controls width */
app-sidebar.collapsed { width: 0; }

/* Mobile: always position fixed, collapsed = off-screen */
@media (max-width: 768px) {
  app-sidebar { transform: translateX(-100%); }
  app-sidebar:not(.collapsed) { transform: translateX(0); }
}
```

But `collapsed` starts `false` on mobile, meaning the sidebar would slide in immediately. Fix: auto-collapse on mobile at startup. In `sidebar.service.ts`:

```typescript
constructor() {
  if (typeof window !== 'undefined' && window.matchMedia('(max-width: 768px)').matches) {
    this.collapsed.set(true);
  }
}
```

---

## Existing Code Fixes (discovered during codebase audit)

These should be addressed as part of the overhaul since they affect mobile quality:

### Fix 1: Instruction builder duplicate media queries

**File: `cockpit/src/app/shared/components/instruction-builder/instruction-builder.component.ts`**

Lines 761-783 and 974-986 contain duplicate `@media (max-width: 768px)` blocks. Merge into one block, keeping the rules from both.

### Fix 2: Instruction builder markdown width

**File: same**

Replace `calc(100vw - 80px)` with `min(95vw, calc(100% - 20px))`. Current value leaves only 64px total margin on iPhone SE (375px), which clips content.

### Fix 3: Inbox local isMobile signal

**File: `cockpit/src/app/simple/pages/inbox/inbox-page.component.ts`**

Lines 1743-1745 create a local `isMobile = signal(window.innerWidth <= 768)` instead of injecting `ViewportService`. Replace with:
```typescript
private viewport = inject(ViewportService);
readonly isMobile = this.viewport.isMobile;
```

This also fixes a bug: the local signal doesn't update on resize (no event listener), while `ViewportService` does.

### Fix 4: Touch target sizes on filter chips

**File: `cockpit/src/styles.scss`**

The global mobile override at line 123 only enforces 44px on `.mobile-tab-bar button`, `.send-btn`, `.stop-btn`, `.btn-primary`, `.btn`. Add filter chips and other small interactive elements:

```scss
@media (max-width: 768px) {
  button, a.nav-link, .filter-chip, .tab-button, [role="button"] {
    min-height: 44px;
    min-width: 44px;
  }
}
```

---

## Component Mobile Readiness

Audit results from codebase exploration:

| Component | Has @media? | Has isMobile? | Two-panel collapse? | Score |
|-----------|:-----------:|:-------------:|:-------------------:|-------|
| Instruction Builder | Yes (with bugs) | No | N/A | Needs fixes |
| Job List | Yes | No | N/A | Good |
| Job Create | Yes (container queries) | No | N/A | Good |
| Project List | Yes | No | N/A | Good |
| Sessions Page | **No** | **No** | N/A | Needs work |
| Session Create | **No** | **No** | N/A | Needs work |
| Chat Page | Delegates | Delegates | Delegates | Good |
| Inbox Page | Yes | Yes (local bug) | Yes | Good (fix signal) |
| Persistent Chat | **No** | **No** | N/A | Needs work |

### Sessions page responsive fixes needed

**File: `cockpit/src/app/simple/pages/sessions/sessions-page.component.ts`**

- Remove or reduce `max-width: 800px` -- use `100%` with padding on mobile
- Reduce container padding from 24px to 12px on mobile
- Add flex-wrap on filter tabs to prevent horizontal overflow
- Session cards already use flex and should scale, but verify button sizes

### Session Create responsive fixes needed

**File: `cockpit/src/app/simple/pages/session-create/session-create.component.ts`**

- Expert grid `minmax(180px, 1fr)` -- reduce to `minmax(140px, 1fr)` or single column on mobile
- Reduce `max-width: 800px` form container on mobile
- Stack form action buttons vertically on narrow screens

### Persistent Chat responsive fixes needed

**File: `cockpit/src/app/shared/components/persistent-chat/persistent-chat.component.ts`**

- Add `@media (max-width: 768px)` for header buttons (may overflow in flex row)
- Reduce `.settings-select { max-width: 220px }` on mobile
- Make permission request blocks full-width on mobile
- Reduce messages padding from 16px to 10px on mobile
- Ensure input has `font-size: 16px` (prevents iOS auto-zoom)

---

## Design Reference (from M3, Apple HIG, and dark theme research)

### Surface elevation (Catppuccin Mocha)

Use surface brightness instead of shadows for depth on dark backgrounds:

| Elevation  | Token    | Hex       | Use for                         |
|------------|----------|-----------|---------------------------------|
| Lowest     | Crust    | `#11111b` | App background, nav bars        |
| Low        | Mantle   | `#181825` | Side panels, bottom nav         |
| Base       | Base     | `#1e1e2e` | Main content area               |
| Raised     | Surface0 | `#313244` | Cards, inputs, list items       |
| Higher     | Surface1 | `#45475a` | Dropdowns, bottom sheets        |
| Highest    | Surface2 | `#585b70` | Tooltips, drag handles          |

### Touch feedback on dark backgrounds

White-based overlays (not dark ripples):

| State          | Overlay                              |
|----------------|--------------------------------------|
| Hover          | `rgba(205, 214, 244, 0.08)` (text color at 8%) |
| Focus-visible  | `rgba(205, 214, 244, 0.12)` (12%)   |
| Active/pressed | `rgba(205, 214, 244, 0.16)` (16%)   |
| Accent active  | `rgba(203, 166, 247, 0.24)` (mauve at 24%) |

Disable hover on touch devices:
```css
@media (hover: none) {
  .interactive:hover { background: transparent; }
}
```

### Animation reference

| Interaction              | Duration | Easing                              |
|--------------------------|----------|-------------------------------------|
| Button press             | 80-100ms | `cubic-bezier(0.4, 0, 0.2, 1)`     |
| Dropdown open            | 200ms    | `cubic-bezier(0, 0, 0.2, 1)` (decelerate) |
| Sidebar slide            | 300ms    | `cubic-bezier(0.4, 0, 0.2, 1)`     |
| Route transition         | 150ms    | ease-in / ease-out                  |
| Backdrop fade            | 300ms    | `cubic-bezier(0.4, 0, 0.2, 1)`     |

Never exceed 400ms for direct interactions. Use `transform` and `opacity` only -- never animate `width`, `height`, `padding`, or `margin` (triggers expensive layout recalculation).

### Reduced motion

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
```

Use `0.01ms` instead of `0s` to preserve `animationend`/`transitionend` event firing.

### Notification badges (for bottom nav tabs)

```css
.badge-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #f38ba8;          /* Catppuccin red */
  position: absolute;
  top: 2px;
  right: -4px;
  box-shadow: 0 0 0 2px #11111b; /* crust border for separation */
}
```

---

## Files Changed

| File | Action | Description |
|------|--------|-------------|
| `app.ts` | Modify | Add bottom nav, sidebar backdrop, change sidebar visibility, body scroll lock, mobile content padding |
| `shell.component.ts` | Modify | Remove mobile fork, remove MobileShellComponent import, add responsive header CSS |
| `sidebar.component.ts` | Modify | Auto-collapse on mobile after navigation (router event listener) |
| `sidebar.service.ts` | Modify | Auto-collapse on mobile at startup |
| `styles.scss` | Modify | Expand touch target enforcement, add reduced-motion rules, add view transition CSS |
| `app.config.ts` | Modify | Add `withViewTransitions()` (optional) |
| `instruction-builder.component.ts` | Fix | Merge duplicate media queries, fix markdown width calc |
| `inbox-page.component.ts` | Fix | Replace local isMobile signal with ViewportService injection |
| `sessions-page.component.ts` | Fix | Add responsive CSS for mobile |
| `session-create.component.ts` | Fix | Add responsive CSS for mobile |
| `persistent-chat.component.ts` | Fix | Add responsive CSS for mobile |
| `mobile-shell.component.ts` | **Delete** | Replaced by router-based bottom nav |
| `review-page.component.ts` | **Delete** | Unreachable, route redirects to inbox |

## Not Changed

| File | Reason |
|------|--------|
| `app.routes.ts` | Routes already correct, redirects already in place |
| `viewport.service.ts` | 768px breakpoint works as-is |
| `job-list.component.ts` | Already has responsive CSS |
| `job-create.component.ts` | Already has container queries |
| `project-list.component.ts` | Already has responsive CSS |
| Page wrappers (jobs, create, datasources) | Sidebar toggle now works with overlay sidebar |

## Migration

UI-only change. No API, database, or backend changes.

- Routes stay the same -- no bookmark breakage
- Shared components stay the same -- no logic changes
- Page wrappers stay the same -- sidebar toggle now functional on mobile

## Not in Scope

- Gesture support (swipe-to-open sidebar) -- follow-up
- Scroll-aware bottom nav hiding -- follow-up (only useful for content-heavy feed views)
- PWA / offline support
- Bottom sheet conversion for dropdowns beyond the shell header
- Mobile-specific features not on desktop
- Changing the 768px breakpoint
