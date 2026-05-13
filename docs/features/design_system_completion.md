---
tags:
  - feature
  - cockpit
  - design-system
  - refactor
  - tokens
aliases:
  - shape pass completion
  - design tokens refactor
  - multi-axis theming
  - css variable migration
related:
  - "[[persistent_chat_visual_refresh]]"
  - "[[dynamic_canvas]]"
---

# Design System Completion

> Color theming works. Shape and typography theming does not. The cockpit ships two themes (Travertine, Senate) that successfully swap colors at runtime, but the documented Roman shape language — sharp corners, Inset Stamp buttons, square avatars, Cinzel display type — is dead code: the override file declares CSS variables that no primitive consumes. This feature completes the second axis of theming by promoting shape and typography from compile-time Sass scalars to runtime CSS variables, introduces archetype recipe mixins as the consumption API, and opens the door to composable theme axes (color × shape × density) on independent data attributes.

**Status:** Design / brainstorm. No implementation yet.
**Filed:** 2026-05-13

## Motivation

The cockpit design system is half-built. The half that ships works correctly:

- Two named themes (`travertine`, `senate`), each defined as a Sass map in `cockpit/src/styles/themes/_theme-config.scss:23-139`.
- A theme-emitter mixin `apply-app-theme($theme-name)` in `cockpit/src/styles/themes/_themes.scss:13-27` iterates the map and writes every entry as a CSS custom property under the matching body class.
- A runtime switcher in `cockpit/src/app/core/services/theme.service.ts:120-130` strips and applies the body class, with localStorage migration from the legacy `dark` / `light` / `praetorian` keys.
- All 21 primitives in `cockpit/src/app/ui/` consume color tokens correctly via `var(--accent-color)`, `var(--surface-0)`, etc. Theme switching works end-to-end.

The half that does not ship is the **shape and typography axis**. The intent is documented in `cockpit/design/themes/README.md:62-73`: sharp 0px-radius corners under Roman themes, an "Inset Stamp" button treatment (dual inner shadow + accent drop + 1px press translate), banded chat bubbles, square avatars, Cinzel display type on brand surfaces and primary buttons, a 3px porphyry-red left rule on approval cards. The override file `cockpit/src/styles/themes/_shape-overrides.scss` was authored to deliver all of it — it declares the right CSS variables (`--radius-sm: 0` at line 20, `--font-display: 'Cinzel'` at line 15), scopes them under `.theme-travertine` and `.theme-senate`, and writes the Inset Stamp shadow stack at lines 91-104.

It is dead code. Every primitive reads `border-radius: v.$radius-sm` — a **Sass scalar** baked in at compile time — not `border-radius: var(--radius-sm)`. The CSS variable the override file sets is never read. The shape override file has also drifted: `cockpit/src/styles/themes/_shape-overrides.scss` targets ten selectors (`.agent-avatar`, `.app-brand`, `.app-frame`, `.chat-message`, `.nav-section-label`, `.session-message`, `.session-topbar`, `.tab-button`, `.tool-approval`, `.message-bubble`) that **do not exist** in the current Angular component tree. Forty-three percent of the file targets classes that were renamed during the BEM migration to the primitive library and never updated.

Two consequences follow:

1. **What ships visually does not match what is documented.** A user reading `cockpit/design/themes/README.md` sees a Roman shape language that the cockpit does not render. The "Inset Stamp button" exists only in prose. Primary buttons are rounded sentence-case Inter, not sharp Cinzel-uppercase. Avatars are circles. Approval cards are rounded blobs.
2. **Adding a second axis of theming is structurally impossible.** A future "sharp / soft" theme axis, a density mode (compact / comfortable), or a Travertine variant with different shape language all require a refactor before they can be authored — there is no consumption path from token to primitive.

This feature is the refactor that fixes both. The architecture is small (mirror what already works for colors onto shape, typography, and density), the work is mechanical (rename roughly 70 `v.$radius-*` references to `var(--radius-*)`, plus a feature-level sweep), and the result restores the documented design language while unlocking composable theme axes for everything that comes after.

## What's broken today

Three structural gaps and one cleanup category.

### Gap 1: Shape tokens are compile-time Sass scalars

`cockpit/src/styles/_variables.scss:13-16` defines:

```scss
$radius-sm: 0.25rem;  //  4px
$radius-md: 0.5rem;   //  8px
$radius-lg: 0.75rem;  // 12px
$radius-xl: 1.5rem;   // 24px  (pill / large surface)
```

These are Sass values consumed across primitives at compile time. The Roman shape override at `cockpit/src/styles/themes/_shape-overrides.scss:20-22` then declares:

```scss
--radius-sm: 0;
--radius-md: 0;
--radius-lg: 2px;
```

The two layers share names but never meet. Nothing reads the CSS variable; the Sass scalar is the only path from source to stylesheet. Every primitive that uses radii is affected — counted via grep across `cockpit/src/app/ui/**/*.scss`:

| Primitive | Radius references | File |
|---|---|---|
| badge | 2 | `cockpit/src/app/ui/badge/badge.component.scss:34-35` |
| button | 3 | `cockpit/src/app/ui/button/button.component.scss:56,63,70` |
| card | 1 | `cockpit/src/app/ui/card/card.component.scss:5` |
| checkbox | 2 | `cockpit/src/app/ui/checkbox/checkbox.component.scss:56,63` |
| chip | 2 | `cockpit/src/app/ui/chip/chip.component.scss:40,47` |
| dialog | 2 | `cockpit/src/app/ui/dialog/dialog.component.scss:26,75` |
| icon-button | 3 | `cockpit/src/app/ui/icon-button/icon-button.component.scss:36,43,50` |
| input | 3 | `cockpit/src/app/ui/input/input.component.scss:58,65,72` |
| menu | 2 | `cockpit/src/app/ui/menu/menu.component.scss:20`, `menu-item.component.scss:14` |
| select | 3 | `cockpit/src/app/ui/select/select.component.scss:49,56,63` |
| tab-bar | 1 | `cockpit/src/app/ui/tab-bar/tab.component.scss:8` |
| textarea | 3 | `cockpit/src/app/ui/textarea/textarea.component.scss:62,69,76` |
| theme-toggle | 1 | `cockpit/src/app/ui/theme-toggle/theme-toggle.component.scss:15` |
| toast | 2 | `cockpit/src/app/ui/toast/toast-container.component.scss:27,68` |
| **total** | **30** | |

Plus functional circles (`border-radius: 50%` on `spinner`, `switch` knob, `radio` dot, `icon-button[round]`) — those stay; they encode a shape semantic, not a theme decision.

### Gap 2: Typography display tokens aren't piped to primitives either

`cockpit/src/styles/themes/_shape-overrides.scss:15` declares `--font-display: 'Cinzel', ...` and at lines 24-43 applies it to `.app-brand`, `.sidebar-brand`, `h1.panel-title`, `.session-title`, `.nav-section-label`, `.section-title`. Of those six selectors, four reference classes that don't exist in the codebase. The two that do (`.sidebar-brand`, `.session-title`) live in feature components, not primitives, so the rule does land — but only by coincidence. The shape-pass intent of "Cinzel-uppercase on primary buttons" at lines 107-119 fails the same way as the radii: the Sass-driven `button.component.scss` declares `font-family: inherit` at line 23 and never consults `--font-display`.

### Gap 3: Component-local override surface doesn't exist

Mature CSS-variable design systems (Bootstrap 5.3, MUI v6, Angular Material 3) follow a consistent pattern: a primitive declares a **local** CSS variable that defaults to a semantic token, and consumers override the local one. So:

```scss
// Hypothetical button.component.scss
.btn {
  --btn-radius: var(--radius-control, var(--radius-md));
  border-radius: var(--btn-radius);
}
```

This makes the override surface explicit and discoverable: a one-off button variant overrides `--btn-radius` directly, not by writing competing selectors. Today's primitives have no such surface. Every one-off variant is a new selector with a hardcoded value.

### Cleanup: dead selectors and hardcoded values

`cockpit/src/styles/themes/_shape-overrides.scss` has ten dead selectors (roughly 64 lines of the 153) targeting classes that don't exist. Beyond that, the codebase has 30+ hardcoded `border-radius` values *outside* the primitive library, almost all in component `styles:` arrays inlined in TypeScript files (the Explore agent missed these — they aren't in `.scss` files):

| Location | Count | Notes |
|---|---|---|
| `cockpit/src/app/app.ts:115,149` | 2 | `16px`, `8px` — app shell |
| `cockpit/src/app/shell/sidebar/sidebar.component.ts:264,298,339,386` | 4 | three `6px` (nav links), one `50%` (user avatar) — directly contradicts the documented Roman square-avatar rule |
| `cockpit/src/app/shell/sidebar-toggle/sidebar-toggle.component.ts:31` | 1 | `6px` |
| `cockpit/src/app/shell/notification-bell/notification-bell.component.ts:35,54` | 2 | `6px`, `8px` |
| `cockpit/src/app/views/agent-steps/agent-steps.component.scss:26,112,137,152,196` | 5 | one `8px`, four `6px` |
| `cockpit/src/app/debug/...` | ~20 | request-viewer, db-table, timeline, graph-timeline, layout-picker, menu, memory-panel, panel-header — mix of `6px`, `8px`, `10px`, `12px`, `50%` |

Roughly 35 hardcoded values across feature surfaces. Some of these (timeline node dots, debug-panel chart dots) are functional circles like the primitive form-controls and should stay. The rest are decorative and should consume tokens.

## Architecture: two-tier tokens, recipe mixins, composable axes

The fix mirrors the architecture that already works for colors. Three layers, each shipping independently.

### Layer 1: Promote primitive scales to CSS variables

The `$radius-*`, `$space-*`, `$font-size-*` scales in `_variables.scss` become CSS variables in `:root`, with theme-scoped overrides under `.theme-travertine` / `.theme-senate` and (later) under `[data-shape="sharp"]` for axis composition. Sass scalars are retained behind a deprecation flag during the migration window, then deleted.

```scss
// _variables.scss after Phase 1
:root {
  // Primitive radii — exposed for runtime overrides
  --radius-sm: 0.25rem;
  --radius-md: 0.5rem;
  --radius-lg: 0.75rem;
  --radius-xl: 1.5rem;
  --radius-full: 999px;

  // Primitive typography
  --font-family-base: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
    Oxygen, Ubuntu, sans-serif;
  --font-family-display: var(--font-family-base);  // overridden under Roman themes
  --font-family-mono: ui-monospace, 'SFMono-Regular', Menlo, monospace;
}
```

Theme files override the values they want to change. `_shape-overrides.scss` shrinks dramatically: it stops listing component selectors and instead just resets the tokens:

```scss
.theme-travertine,
.theme-senate {
  --radius-sm: 0;
  --radius-md: 0;
  --radius-lg: 2px;
  --radius-xl: 0;
  --font-family-display: 'Cinzel', 'Cormorant Garamond', 'Times New Roman', serif;
}
```

That's it. The component-by-component listing is no longer needed because primitives read tokens.

### Layer 2: Semantic role tokens

Above the primitive scale, a thin semantic layer assigns roles. Buttons read `--radius-control`, not `--radius-sm`. Surfaces read `--radius-surface`. Pills read `--radius-pill`. This is the EightShapes "Decision tokens" tier and the Radix Themes / Angular Material `mat-sys-*` convention.

```scss
// New file: _semantic-tokens.scss
:root {
  --radius-control: var(--radius-md);   // buttons, inputs, selects, chips, badges
  --radius-surface: var(--radius-md);   // cards, dialogs, panels, menus
  --radius-pill:    var(--radius-full); // pill chips, status pills
  --radius-tag:     var(--radius-sm);   // badges, code tags, small markers

  --font-control:   var(--font-family-base);    // body / default buttons
  --font-display:   var(--font-family-display); // brand, primary buttons in Roman themes
  --font-mono:      var(--font-family-mono);    // code, tool args, debug
}
```

A future theme that wants pills-as-controls flips `--radius-control: var(--radius-pill);` in one place; every control follows. A density mode flips `--space-control-y` / `--space-control-x`. The architecture supports the change without touching primitives.

Why two tiers and not three: the EightShapes guidance — *"start within, then promote across components"* — applies. Component tokens (`--button-bg`, `--button-radius`) are appropriate when ≥2 components need to evolve independently; today's primitives align tightly with their roles. Bootstrap 5.3 uses **component-local** CSS variables as the override surface (`.btn { --bs-btn-bg: …; }`) without elevating them to the global tier, and that's the pragmatic choice here. Component-local variables live in the primitive's own SCSS file; the global tier stays small.

### Layer 3: Archetype recipe mixins

Tokens own *values*. Recipes own *structural decisions* — which token does a button consume, with what fallback, plus the shadow stack and press behavior that make a button look like a button. Sass mixins are the right home: they're authored once, the consumer `@include`s, and runtime tokens drive the result.

```scss
// New file: cockpit/src/styles/_shape-recipes.scss
@use 'variables' as v;

// Control archetype — buttons, inputs, selects, chips, badges, icon-buttons.
// Reads --control-radius (component-local) which defaults to --radius-control.
@mixin shape-control($size: 'md') {
  --control-radius: var(--radius-control, var(--radius-#{$size}));
  border-radius: var(--control-radius);
}

// Surface archetype — cards, dialogs, panels, menus, popovers.
@mixin shape-surface {
  --surface-radius: var(--radius-surface, var(--radius-md));
  border-radius: var(--surface-radius);
}

// Pill archetype — chips with pill shape, status pills.
@mixin shape-pill {
  border-radius: var(--radius-pill, var(--radius-full));
}

// Functional circle — radio dots, switch knobs, spinner ring, avatar (when round).
// Theme-invariant; encodes a semantic, not a decoration.
@mixin shape-circle {
  border-radius: 50%;
}

// Inset Stamp — the Roman primary-button treatment. The recipe is theme-aware:
// non-Roman themes set --stamp-* to neutral values and the visual collapses to
// a flat button. Roman themes set the stamp shadows in _shape-overrides.scss.
@mixin shape-stamp($variant: 'default') {
  position: relative;
  transition: transform 60ms ease, box-shadow 120ms ease,
              background-color 120ms ease;
  box-shadow:
    inset 0 1px 0 0 var(--stamp-highlight, transparent),
    inset 0 -1px 0 0 var(--stamp-shadow, transparent),
    0 1px 0 0 var(--stamp-drop, transparent);

  &:active:not(:disabled) {
    transform: translateY(var(--stamp-press, 0));
    box-shadow:
      inset 0 1px 2px 0 var(--stamp-press-shadow, transparent),
      0 0 0 0 transparent;
  }
}
```

```scss
// Updated button.component.scss
@use '../../../styles/shape-recipes' as shape;

.app-button__btn {
  @include shape.shape-stamp;

  &[data-size='sm'] { @include shape.shape-control('sm'); }
  &[data-size='md'] { @include shape.shape-control('md'); }
  &[data-size='lg'] { @include shape.shape-control('lg'); }

  &[data-variant='primary'] {
    background: var(--accent-color);
    color: var(--on-accent, var(--timeline-bg));
    font-family: var(--font-primary, var(--font-control));
  }
}
```

The button no longer hardcodes which token to read. The recipe encapsulates "what's a button," and themes parametrize via `--radius-control` and `--stamp-highlight` etc. The Roman shape pass file becomes a small set of token overrides:

```scss
// _shape-overrides.scss after Phase 2 — radically smaller
.theme-travertine,
.theme-senate {
  // Shape tokens — sharp Roman
  --radius-sm: 0;
  --radius-md: 0;
  --radius-lg: 2px;
  --radius-xl: 0;

  // Typography
  --font-family-display: 'Cinzel', 'Cormorant Garamond', 'Times New Roman', serif;
  --font-primary: var(--font-family-display);
  --letter-spacing-display: 0.16em;
  --text-transform-display: uppercase;

  // Inset Stamp shadow stack — picked up by shape.shape-stamp mixin
  --stamp-highlight: color-mix(in srgb, var(--text-primary) 8%, transparent);
  --stamp-shadow:    color-mix(in srgb, #000 18%, transparent);
  --stamp-drop:      color-mix(in srgb, var(--accent-color) 18%, transparent);
  --stamp-press:     1px;
  --stamp-press-shadow: color-mix(in srgb, #000 30%, transparent);

  // Solid-variant readable foreground (replaces the --timeline-bg hack)
  --on-accent: #fff;
}
```

### Layer 4 (optional, Phase 5+): Composable theme axes

Once shape and typography are tokenized, the door is open to **decoupling** color from shape from density. The Radix Themes / Panda CSS / Bootstrap 5.3 pattern uses independent data attributes on the root element:

```html
<html data-theme="travertine" data-shape="sharp" data-density="comfortable">
```

```scss
[data-theme="travertine"] { --accent-color: …; --surface-0: …; }
[data-theme="senate"]     { --accent-color: …; --surface-0: …; }

[data-shape="sharp"]      { --radius-sm: 0; --radius-md: 0; … }
[data-shape="soft"]       { --radius-sm: 4px; --radius-md: 8px; … }

[data-density="compact"]  { --space-control-y: 4px; --space-control-x: 8px; }
[data-density="cozy"]     { --space-control-y: 8px; --space-control-x: 12px; }
```

Each axis is orthogonal. A user could in principle pick Travertine colors with soft shapes; a future "minimal" theme variant inherits shape from `data-shape` without redefining anything. This is what every modern design system (post-2024) has converged on, and the path from today's `theme-X` body-class model is straightforward — rename the class to a data attribute and move the shape/typography tokens to their own `[data-shape]` selector.

This layer is **optional**. It unlocks the architecture for future themes and density modes. The cockpit ships fine with just Layers 1-3, where shape is baked into each theme. Treat Layer 4 as a follow-up unlocked by the rest.

## Decision: 2-tier tokens, recipe mixins, axis composition deferred

Three things chosen and one explicitly deferred.

1. **Two token tiers (primitive scale + semantic roles), not three.** Bootstrap 5.3's component-local CSS variables provide the third tier *inside each component* without polluting the global namespace. Adding a global third tier is premature at our 21-primitive scale; revisit if the cockpit grows past ~50 primitives or if multiple components need to override the same shape independently.
2. **Recipe mixins as the consumption API.** Primitives `@include shape.control` rather than hardcode `border-radius`. The mixin owns the recipe (which token, with what fallback, plus structural extras like the Inset Stamp shadow stack); themes own the value. This is the user's original intuition, validated by Bootstrap 5.3 and Angular Material's `overrides` mixins.
3. **CSS-variable-first, Sass for authoring only.** Sass remains for module organization, maps, mixins, and math. Every value reachable at runtime by a theme switch lives in CSS variables. This matches every major design system that shipped or rewrote in 2024-2025 (shadcn/ui, Radix Themes, Tailwind v4, MUI v6, Angular Material 3, Bootstrap 5.3, Chakra v3, Mantine, Primer).
4. **Deferred: composable axes via data attributes.** The architecture supports it. The migration that adds `data-shape="sharp"` is mechanical once Layers 1-3 land. But we ship value (the documented Roman shape language actually renders) without it; composability is the second-PR-after-this-feature, not the first.

### Why not the alternatives

- **Style Dictionary / W3C DTCG format.** Tooling overkill at our scale. We have one platform (web), one design tool (Figma, lightly used), no need for iOS / Android output. Add later if scope grows; SCSS-to-CSS-variable emission is sufficient today.
- **Radix-style scale + factor pattern (`--radius-1` … `--radius-6` × `--radius-factor`).** Cleaner for systems with many discrete radius steps shared across many components. Our radii are role-driven (control / surface / pill / tag) and we have 4 sizes, not 6. The role-tier abstraction reads better here.
- **Tailwind v4 `@theme` directive.** We're not on Tailwind. Bringing it in just to author tokens is a bigger refactor than the one we're fixing.
- **CSS-in-JS / Emotion / Styled Components.** Outside Angular's idiom; we use component-scoped SCSS via Angular's view encapsulation. No reason to change.
- **`@property` for typed tokens.** Useful where tokens are animated (drawer corners, modal radii). Not needed for the radii we have today. Add per-token where animation calls for it; don't apply broadly.

## Token model

Concrete vocabulary, naming convention, and where each lives.

### Primitive tokens (Tier 1)

Numeric scales. Theme-invariant by default; themes override the value of the scale step they want to change.

| Token | Default | Lives in | Purpose |
|---|---|---|---|
| `--radius-sm` | `4px` | `:root` | small surfaces (badges, code tags) |
| `--radius-md` | `8px` | `:root` | controls (buttons, inputs) |
| `--radius-lg` | `12px` | `:root` | large surfaces (dialogs) |
| `--radius-xl` | `24px` | `:root` | pills, large-radius surfaces |
| `--radius-full` | `999px` | `:root` | pill / circle fallback |
| `--font-family-base` | system sans | `:root` | body text |
| `--font-family-display` | inherits base | `:root` | brand / display |
| `--font-family-mono` | system mono | `:root` | code / tool args |
| `--space-2xs` … `--space-2xl` | TBD scale | `:root` | (out of scope for v1; documented but deferred) |

### Semantic tokens (Tier 2)

Role-driven aliases. The contract surface for primitives.

| Token | Default | Used by |
|---|---|---|
| `--radius-control` | `var(--radius-md)` | button, input, select, chip, badge, icon-button, textarea, tab |
| `--radius-surface` | `var(--radius-md)` | card, dialog, menu, toast, panel |
| `--radius-pill` | `var(--radius-full)` | pill-shaped chips, status pills |
| `--radius-tag` | `var(--radius-sm)` | small inline markers, checkbox tile |
| `--font-primary` | `var(--font-family-base)` | primary-variant buttons (Roman themes override to `--font-family-display`) |
| `--font-control` | `var(--font-family-base)` | body of buttons, inputs, labels |
| `--on-accent` | `#fff` | text/icon color on accent fill |
| `--on-warning` | dark per theme | text/icon color on warning fill (gold ochre needs dark text for contrast) |
| `--on-success` | `#fff` | text/icon color on success fill |
| `--on-danger` | `#fff` | text/icon color on danger fill |
| `--on-info` | `#fff` | text/icon color on info fill |

### Component-local tokens (Tier 3, inside primitive only)

Each primitive declares one or more local variables defaulting to a Tier 2 token. Consumers override the local one for one-offs. Example pattern:

```scss
.app-button__btn {
  --btn-radius: var(--radius-control);
  --btn-padding-x: var(--space-md, 1rem);
  --btn-padding-y: 0;
  --btn-font-family: var(--font-primary, inherit);

  border-radius: var(--btn-radius);
  padding: var(--btn-padding-y) var(--btn-padding-x);
  font-family: var(--btn-font-family);
}
```

Component-local tokens are never globally documented; they're discoverable from the component's own SCSS file. This is Bootstrap 5.3's exact pattern.

### Naming convention

- Lowercase, hyphen-separated.
- Role-prefix when the token has a role (`--radius-control`, `--font-primary`), scale-suffix when it's a primitive (`--radius-md`, `--font-size-sm`).
- Component-local tokens prefix with the primitive's short name (`--btn-`, `--input-`, `--card-`).
- No global `--app-` / `--ds-` prefix today (we don't ship as a library; nothing else to collide with). Add a prefix if/when the design system is vendored externally.

## Implementation phases

Each phase is independently shippable; each one improves the system even if the next never lands.

### Phase 0 — Cleanup

The lowest-risk, immediate-value PR. Removes dead code so the next phases work against an honest baseline.

- [ ] Delete dead selectors from `cockpit/src/styles/themes/_shape-overrides.scss`: `.agent-avatar`, `.app-brand`, `.app-frame`, `.chat-message`, `.nav-section-label`, `.session-message`, `.session-topbar`, `.tab-button`, `.tool-approval`, `.message-bubble`. Roughly 64 lines.
- [ ] Update `cockpit/design/themes/README.md` to mark the unimplemented Roman shape rules as "Phase 2: in flight" rather than as shipped behavior.
- [ ] Audit `_shape-overrides.scss` selectors against actual class names; flag any remaining drift in a follow-up issue.
- [ ] No visible changes ship in this phase; it's all dead-code removal and docs honesty.

### Phase 1 — Token tier promotion

Move shape and typography from Sass to CSS variables. Backward-compatible so existing Sass references keep working during the migration.

- [ ] Add `--radius-sm/md/lg/xl/full` to `:root` in `cockpit/src/styles/_variables.scss` (alongside the existing Sass scalars during the migration window).
- [ ] Add `--font-family-base/display/mono` to `:root`.
- [ ] Create `cockpit/src/styles/_semantic-tokens.scss` with the Tier 2 role tokens (`--radius-control`, `--radius-surface`, `--radius-pill`, `--radius-tag`, `--font-primary`, `--font-control`, `--on-accent`).
- [ ] Update `cockpit/src/styles/themes/_shape-overrides.scss` to set tokens at the body-class level — Roman themes set `--radius-sm: 0; --radius-md: 0; --radius-lg: 2px;` etc.
- [ ] Add per-variant on-tokens (`--on-accent`, `--on-warning`, `--on-success`, `--on-danger`, `--on-info`) to both theme maps in `_theme-config.scss`. Warning gets a dark foreground in both themes (gold ochre on white fails contrast); others get `#fff`. Replaces the current `--timeline-bg` hack on solid-variant text.
- [ ] Add the Inset Stamp shadow tokens (`--stamp-highlight`, `--stamp-shadow`, `--stamp-drop`, `--stamp-press`, `--stamp-press-shadow`) to both Roman theme overrides. Tinted variants (warning, info, success, danger) get a softer set — `--stamp-drop: transparent`, `--stamp-press: 0` — so the inner shadow doesn't read muddy on translucent fills.
- [ ] No primitive changes in this phase. Token layer ships first so subsequent phases can rely on it.

### Phase 2 — Recipe mixins

Author the consumption API.

- [ ] Create `cockpit/src/styles/_shape-recipes.scss` with `shape-control($size)`, `shape-surface`, `shape-pill`, `shape-circle`, `shape-stamp($variant)`.
- [ ] Create `cockpit/src/styles/_typography-recipes.scss` with `type-display`, `type-eyebrow`, `type-mono` mixins (Cinzel-uppercase-letterspaced, small uppercase, monospace tool args).
- [ ] Document recipe vocabulary inline as block comments in each file.
- [ ] Add `cockpit/src/styles/README.md` section "Authoring a primitive" with worked examples.
- [ ] No primitive changes in this phase; just the API.

### Phase 3 — Primitive migration

Replace `v.$radius-*` with `@include shape.*` across the 14 primitives that have shape. One PR per logical group (controls / surfaces / etc.) for reviewability.

- [ ] **Controls group**: button, icon-button, input, select, textarea, chip, badge, tab. Replace `border-radius: v.$radius-md` with `@include shape.shape-control('md')`. Add component-local `--btn-radius` etc.
- [ ] **Surfaces group**: card, dialog, menu, toast. Replace with `@include shape.shape-surface`.
- [ ] **Functional circles**: switch (knob), radio (dot), spinner, icon-button (round variant). Replace with `@include shape.shape-circle`.
- [ ] **Button primary**: apply `@include shape.shape-stamp` and `font-family: var(--font-primary, inherit)`. Verify Roman themes render Cinzel-uppercase; non-Roman themes render plain Inter.
- [ ] **Checkbox**: uses `--radius-tag` for the small square box; keep checked-state radius matching.
- [ ] Update `cockpit/src/app/ui/button/button.component.scss` to use `var(--on-accent)` instead of `var(--timeline-bg)` on solid variants (the documented but inconsistent hack today).
- [ ] Visual smoke pass on every primitive in both themes.
- [ ] Component tests pass without changes (radius is presentational; no behavior changes).

### Phase 4 — Feature-level sweep

Replace hardcoded `border-radius` in feature components with tokens.

- [ ] **Sidebar** (`cockpit/src/app/shell/sidebar/sidebar.component.ts:264,298,339,386`): four hardcoded values. Nav links and collapse button use `@include shape.shape-control`. User avatar stays circular via `@include shape.shape-circle` (resolved: industry convention wins; the documented Roman square-avatar rule is superseded for user-photo affordances — document the deviation in `cockpit/design/themes/README.md` during Phase 0). Brand mark / legion-mark in the sidebar header stays square.
- [ ] **App shell** (`cockpit/src/app/app.ts:115,149`): two hardcoded values.
- [ ] **Notification bell** (`cockpit/src/app/shell/notification-bell/notification-bell.component.ts:35,54`).
- [ ] **Sidebar toggle** (`cockpit/src/app/shell/sidebar-toggle/sidebar-toggle.component.ts:31`).
- [ ] **agent-steps view** (`cockpit/src/app/views/agent-steps/agent-steps.component.scss:26,112,137,152,196`): five hardcoded values.
- [ ] **Debug panel** (`cockpit/src/app/debug/`): roughly 20 hardcoded values across request-viewer, db-table, timeline, graph-timeline, layout-picker, menu, memory-panel, panel-header. Many are functional circles (timeline node dots) and stay; the rest become tokens.
- [ ] **Approval-card classes**: confirm the approval-badge chip's 4px corners — keep or sharpen per design call (see `persistent_chat_visual_refresh.md` F7 note).
- [ ] grep regression check: `border-radius: \d+px` returns only the functional-circle cases after this phase.

### Phase 5 — *deferred*

Composable axes via `<html data-theme data-shape data-density>` was originally planned here. Deferred from this feature (resolved 2026-05-13 — no third theme or density mode on the 6-month roadmap). The architecture documented in *Layer 4* above stays intact for whoever picks it up later. Full task list lives in the **Future work** section below.

### Phase 6 — Sass deprecation and removal

After Phases 1-4 land and the system has lived in CSS-variable form for a release cycle.

- [ ] Audit remaining Sass `v.$radius-*` references; should be zero in primitives and feature components.
- [ ] Remove the Sass scalars from `_variables.scss` (or keep with a `@deprecated` comment for one more release).
- [ ] Update `cockpit/src/styles/README.md` to document the CSS-variable-first model.
- [ ] Add a stylelint rule (`declaration-property-value-disallowed-list`) that disallows raw `\d+px` values on `border-radius` outside `_variables.scss`. Allowed values: `var(--*)`, `0`, `50%`, `999px`. Prevents regression after the Sass scalars are gone.

### Phase 7 — Visual regression backstop

Optional but recommended once Phases 0-4 are in.

- [ ] Add Storybook (`@storybook/angular`) with stories for every primitive × every variant × both themes.
- [ ] Configure Chromatic (or Percy) for diff review on PR.
- [ ] Wire to CI as a non-blocking check initially; promote to blocking once baseline stabilizes.
- [ ] Adds confidence for future theme additions and density modes.

## Migration strategy

Two-pass: compat layer first, then codemod.

### Pass 1: Compat layer (Phase 1)

`_variables.scss` keeps the Sass scalars and *also* emits CSS variables. Both forms work; consumers can use either.

```scss
// During the migration window
$radius-sm: 0.25rem;
$radius-md: 0.5rem;
$radius-lg: 0.75rem;
$radius-xl: 1.5rem;

:root {
  --radius-sm: #{$radius-sm};
  --radius-md: #{$radius-md};
  --radius-lg: #{$radius-lg};
  --radius-xl: #{$radius-xl};
}
```

Nothing breaks. Existing `v.$radius-sm` keeps compiling to `4px` literally. New code uses `var(--radius-sm)`.

### Pass 2: Codemod (Phase 3)

Mechanical find-and-replace across primitive SCSS files. Two approaches:

**Approach A: PostCSS plugin.** Write a small PostCSS transform that walks CSS declarations and replaces `v.$radius-sm` with `var(--radius-sm)`. Atlassian's `@hypermod/cli` and Back Market's case study show this scales to thousands of references with ~minutes of run time.

**Approach B: grep + manual review.** At our scale (~30 references), a careful sed script + diff review is faster than tooling. The math expressions to watch are `calc(v.$radius-md - 1px)` style compounds — manually rewrite to `calc(var(--radius-md) - 1px)`.

Given our scope, **Approach B is correct here.** Reach for PostCSS if the feature-level sweep (Phase 4) finds dozens more references and the team wants a repeatable migration.

### Pass 3: Removal (Phase 6)

After a release cycle in compat mode, remove the Sass scalars. Add a stylelint rule that disallows `\$radius-` in non-`_variables.scss` files to prevent regression.

### Visual regression during migration

The risk is silent regressions: a primitive that *looks* the same after migration because Roman theme overrides happen to render. Mitigations:

- **Spot screenshots before/after each phase**, at desktop width, in both themes, of the views most exercised: persistent-chat, jobs list, sessions list, debug timeline, sidebar.
- **A11y check**: focus rings should match across both themes; primary-button contrast should hold with `--on-accent`.
- **Phase 7 (Storybook + Chromatic)** is the proper backstop. If a team member picks up the feature with appetite for it, do Phase 7 alongside Phase 3 instead of after.

## What stays untouched

- **The 21 primitives' public API.** No template changes; only SCSS changes. Inputs to each primitive (`size`, `variant`, etc.) stay identical. Downstream consumers of the primitive library see no breaking change.
- **Color tokens.** They already work. `_theme-config.scss` stays as-is, modulo adding `--on-accent`.
- **`theme.service.ts` runtime logic.** Phase 5 changes the DOM attribute from `class` to `data-theme`, but the migration mapping logic and signal computation are unchanged. Until Phase 5, no changes at all.
- **Legacy localStorage migration.** Praetorian / dark / light keys keep mapping to senate / senate / travertine.
- **Component templates and behavior.** Buttons still click. Modals still open. Slash-commands still autocomplete. This is a styling-layer refactor, not a feature change.

## Out of scope

- **Density modes.** Compact / cozy / comfortable spacing variants are documented as a *future* axis enabled by the architecture, but the v1 of this feature does not author them. Phase 5's `[data-density]` infrastructure unlocks them; the actual spacing scale audit and density-mode tokens are a follow-up feature.
- **A third theme.** A "minimal" or "softened" theme that uses Roman colors with non-Roman shape is *possible* after this feature, but is not part of it.
- **Color-token restructure.** The existing color tokens (`--accent-color`, `--surface-0`, etc.) already work; renaming or restructuring them (e.g., to a strict `--color-*` namespace) is a separate effort with its own migration cost. Out of scope.
- **CSS Container Queries.** Several primitives could benefit from container-query-driven sizing instead of media-query breakpoints. Unrelated to theming; track separately.
- **Style Dictionary / W3C DTCG token files.** No multi-platform export need today. Add later if cockpit tokens are vendored for iOS / Android.
- **Storybook setup**, unless Phase 7 is rolled in. Recommended but not required to ship Phases 0-4.
- **The simple/ (mobile) shell.** Uses the same primitives, so they get the upgrade for free, but the simple/ shell's own components (`chat-page.component.ts` etc.) are out of scope for the feature-level sweep — handled as a follow-up.

## Resolved decisions

The eight calls flagged during the spec phase, all resolved 2026-05-13:

1. **Avatars are round.** User-photo affordance follows industry convention; the documented Roman square-avatar rule is superseded for avatars specifically. The brand mark stays square (it's a logo, not an avatar). Document the deviation in `cockpit/design/themes/README.md` during Phase 0.

2. **`--on-accent` is per-variant.** Five tokens × two themes = 10 entries: `--on-accent`, `--on-warning`, `--on-success`, `--on-danger`, `--on-info`. Warning gets a dark foreground in both themes (gold ochre on white text fails WCAG contrast); the other four are `#fff`. Replaces the existing `--timeline-bg` hack on solid-variant text.

3. **Inset Stamp is parameterized by variant.** `shape-stamp($variant)` applies the full treatment (top highlight + bottom shadow + accent drop + 1px press) to primary and secondary; softer treatment (highlight only, no drop, no press) to tinted variants (warning, info, success, danger). Tinted variants don't carry the inner shadow cleanly because they're not opaque enough — the softer set avoids the muddy look.

4. **Component-local tokens are opt-in.** Primitives with realistic one-off override needs (button, input, card, dialog, menu, chip) declare locals (`--btn-radius`, `--input-radius`, etc.). Primitives with a single site (spinner, switch, radio, toast) consume the role token directly. Override surface stays discoverable where it matters; the rest stay concise.

5. **Cinzel and Cormorant Garamond are loaded.** Verified `cockpit/src/index.html:28` pulls both from Google Fonts (weights 500/600/700 each, `display=swap`). No action needed; fonts are available when Phase 3 lands. The brief font-swap window is the only visible fallback period.

6. **Approval-badge chip stays at 4px.** Documented exception per the F7 work in `persistent_chat_visual_refresh.md`. Sharpening at the chip's font size (~10-11px) reads as broken rather than Roman. Add to `cockpit/design/themes/README.md` exceptions section during Phase 0.

7. **Phase 5 (composable axes) is future work.** No third theme or density mode on the 6-month roadmap. The architecture (Layer 4 above) documents how composability would slot in; the implementation phase is deferred. Phases 0-4 + Phases 6-7 ship the broken-theming fix on their own. Phase 5 slot is intentionally left in the phase sequence as a marker — see **Future work** below.

8. **Stylelint rule for hardcoded radii — yes.** Added to Phase 6 (Sass removal). `declaration-property-value-disallowed-list` on `border-radius`, allowing only `var(--*)`, `0`, `50%`, `999px`. Prevents regression after the compat layer is gone.

## Future work

Architecture pieces documented for completeness but deferred from this feature.

### Composable theme axes (was Phase 5)

Decouples shape from color so the cockpit can grow a third theme axis (density, sharp/soft, brand variants) without redoing the work. The Layer 4 architecture above shows the target shape; this section captures the implementation tasks for whoever picks it up.

- [ ] Rename `.theme-travertine` / `.theme-senate` body classes to `<html data-theme="travertine">` etc.
- [ ] Add `data-shape` attribute and its selector block (`[data-shape="sharp"] { … }`, `[data-shape="soft"] { … }`). Move the Roman shape token overrides from `.theme-*` selectors to `[data-shape="sharp"]`.
- [ ] Update `cockpit/src/app/core/services/theme.service.ts:120-130` to apply attributes on `<html>` instead of body class.
- [ ] Add `data-density` attribute and its selector block (`compact`, `cozy`, `comfortable`); migrate spacing scale to be density-aware (`--space-control-y`, `--space-control-x`).
- [ ] Update legacy localStorage migration logic to map old body-class values to attribute pairs.
- [ ] Document valid combinations and intentional incompatibilities in `cockpit/design/themes/README.md`.

Trigger to revisit: any of (a) a third color theme is requested, (b) a density mode is requested (e.g., compact for power users), (c) a "softened Roman" variant is requested, (d) the cockpit is white-labeled.

## ADR: alternatives considered, not adopted

- **Three-tier tokens with global component tokens.** EightShapes / Fowler recommend three tiers (primitive, semantic, component) for larger systems. Our 21-primitive scale and Bootstrap 5.3's precedent (component-local CSS variables inside the primitive's own SCSS, not elevated to the global tier) make two tiers sufficient. Revisit at ~50 primitives or when multiple components need independent shape evolution.
- **Radix Themes scale + factor (`--radius-1` … `--radius-6` × `--radius-factor`).** Cleaner for systems where many components share a discrete radius scale and a global multiplier is meaningful. Our radii are role-driven (control / surface / pill / tag); the role-tier abstraction reads better.
- **Tailwind v4 / @theme directive.** Brings in a full utility framework. Out of proportion to the actual problem; we'd need to migrate template class consumption across the cockpit too.
- **CSS-in-JS (Emotion / Styled Components / stitches.js).** Outside Angular idiom; we use SCSS via Angular view encapsulation. No compelling reason to change.
- **`@property` on every token.** Useful for animated tokens. None of our radius tokens are animated today (the only animation involving them is `transform` on the Inset Stamp press, not the radius itself). Add per-token where animation calls for it.
- **Pre-composed combined themes (Roman-Sharp-Compact as one class).** Combinatorial explosion: 2 colors × 2 shapes × 3 densities = 12 classes. Composable axes (Phase 5) cost 2+2+3 = 7 selector blocks. Adopt the composable model from the start of Phase 5.
- **Drop SCSS entirely (go pure CSS like shadcn).** Shadcn's approach is excellent for new builds. We have an existing SCSS codebase, Angular's component-style integration assumes SCSS, and the migration cost is higher than the value. Keep SCSS for authoring; emit CSS variables for runtime.
- **Style Dictionary / W3C DTCG JSON tokens.** Right answer when shipping tokens across platforms (iOS / Android / web). We ship to web only. Add later if needed.

## Related code

Files that change during this feature:

- `cockpit/src/styles/_variables.scss` — add CSS variable emission alongside Sass scalars (Phase 1); remove Sass scalars (Phase 6).
- `cockpit/src/styles/_mixins.scss` — unchanged; the existing focus / breakpoint / a11y mixins stay.
- `cockpit/src/styles/_shape-recipes.scss` — **new file** (Phase 2).
- `cockpit/src/styles/_typography-recipes.scss` — **new file** (Phase 2).
- `cockpit/src/styles/_semantic-tokens.scss` — **new file** (Phase 1).
- `cockpit/src/styles/themes/_theme-config.scss` — add `--on-accent` and any per-variant on-tokens (Phase 1).
- `cockpit/src/styles/themes/_themes.scss` — unchanged; `apply-app-theme` mixin already emits CSS variables.
- `cockpit/src/styles/themes/_shape-overrides.scss` — radically smaller after Phase 1; only token overrides, no component-class selectors.
- `cockpit/src/styles/themes/_typography.scss` — fold useful bits into recipes; remove if redundant.
- `cockpit/src/app/ui/*/*.component.scss` — 14 primitives migrate to recipe mixins (Phase 3).
- `cockpit/src/app/core/services/theme.service.ts:120-130` — Phase 5 only; switch body class to data attributes on `<html>`.
- `cockpit/design/themes/README.md` — update implementation status (Phase 0); document the new architecture (Phase 6).
- `cockpit/src/styles/README.md` — document the recipe / token model (Phase 2 + Phase 6).

Files referenced for cleanup (Phase 4 sweep):

- `cockpit/src/app/shell/sidebar/sidebar.component.ts:264,298,339,386`
- `cockpit/src/app/shell/sidebar-toggle/sidebar-toggle.component.ts:31`
- `cockpit/src/app/shell/notification-bell/notification-bell.component.ts:35,54`
- `cockpit/src/app/app.ts:115,149`
- `cockpit/src/app/views/agent-steps/agent-steps.component.scss:26,112,137,152,196`
- `cockpit/src/app/debug/components/{request-viewer,db-table,timeline,graph-timeline,layout-picker,menu,memory-panel}/`
- `cockpit/src/app/debug/layout/panel-header/panel-header.component.ts:136`

## Decision log

- **2026-05-13:** Two-tier tokens (primitive scale + semantic roles) chosen over three-tier. Component-local CSS variables inside each primitive provide a third tier where needed (Bootstrap 5.3 pattern). Revisit at ~50 primitives.
- **2026-05-13:** Recipe mixins (`@mixin shape-control`, etc.) chosen as the consumption API over per-component hardcoded `border-radius`. Tokens own values; recipes own structural decisions.
- **2026-05-13:** CSS-variable-first, Sass for authoring only. Matches every major design system that shipped or rewrote in 2024-2025.
- **2026-05-13:** Two-pass migration: compat layer (Sass scalar + CSS variable emit) → codemod (replace `v.$radius-*` with `var(--radius-*)`) → removal (delete Sass scalars after release). Approach B (grep + manual review) chosen over PostCSS at our scale.
- **2026-05-13:** Composable axes via data attributes on `<html>` (Phase 5) chosen as the future-facing architecture; ship Phases 0-4 first as standalone value.
- **2026-05-13:** Style Dictionary / DTCG tokens not adopted today. Web-only output; add later if multi-platform need arises.
- **2026-05-13:** `@property` typed tokens not adopted broadly. Add per-token only where animation requires.
- **2026-05-13:** Storybook + Chromatic visual regression noted as recommended (Phase 7) but not blocking the core refactor.
- **2026-05-13:** Avatars resolved as round, not square. User-photo affordance follows industry convention; the documented Roman square-avatar rule is superseded for avatars specifically. Brand mark stays square.
- **2026-05-13:** `--on-accent` resolved as per-variant (`--on-accent`, `--on-warning`, `--on-success`, `--on-danger`, `--on-info`). Five tokens × two themes. Warning gets a dark foreground to meet contrast on gold ochre. Replaces the existing `--timeline-bg`-as-foreground hack.
- **2026-05-13:** Inset Stamp parameterized by variant. Primary and secondary get the full treatment; tinted variants (warning, info, success, danger) get a softer set (highlight only) to avoid muddy inner-shadow rendering on translucent fills.
- **2026-05-13:** Component-local CSS variables resolved as opt-in. Primitives with realistic override needs declare locals; primitives with a single site consume role tokens directly.
- **2026-05-13:** Cinzel and Cormorant Garamond verified loaded via Google Fonts in `cockpit/src/index.html:28` (weights 500/600/700 each, `display=swap`). No font-loading work needed.
- **2026-05-13:** Approval-badge chip stays at 4px corners as a documented exception. Sharpening at the chip's ~10-11px font size reads as broken rather than Roman.
- **2026-05-13:** Phase 5 (composable axes via `<html data-theme data-shape data-density>`) deferred to Future work. No third theme, density mode, or shape variant on the 6-month roadmap. Architecture stays documented for future implementer.
- **2026-05-13:** Stylelint rule for hardcoded `border-radius` adopted. Added to Phase 6 alongside Sass scalar removal.

## Sources

- [Design Token-Based UI Architecture (Martin Fowler / Diana Mounter)](https://martinfowler.com/articles/design-token-based-ui-architecture.html) — three-tier model rationale.
- [Naming Tokens in Design Systems (Nathan Curtis / EightShapes)](https://medium.com/eightshapes-llc/naming-tokens-in-design-systems-9e86c7444676) — "start within, then promote across components" guidance.
- [Design Tokens Community Group format spec (2025.10)](https://www.w3.org/community/design-tokens/2025/10/28/design-tokens-specification-reaches-first-stable-version/) — DTCG JSON format reached first stable version 28 October 2025. Not adopted today; documented for portability.
- [Radix Themes — Radius](https://www.radix-ui.com/themes/docs/theme/radius) — scale + factor pattern (`--radius-1` through `--radius-6`).
- [shadcn/ui Theming](https://ui.shadcn.com/docs/theming) — single `--radius` base + calc-derived scale; CSS-variable-first, no Sass.
- [Tailwind CSS v4 — Theme variables](https://tailwindcss.com/docs/theme) — `@theme` directive, CSS-first config.
- [GitHub Primer — Design Tokens Guide](https://github.com/primer/primitives/blob/main/DESIGN_TOKENS_GUIDE.md) — pattern-compression naming convention.
- [Bootstrap 5.3 — CSS variables](https://getbootstrap.com/docs/5.3/customize/css-variables/) — component-local CSS variable override surface; Sass-to-CSS-variable bridge.
- [Material UI v6 — CSS theme variables](https://mui.com/material-ui/customization/css-theme-variables/overview/) — CssVarsProvider, `theme.vars`, FOUC-elimination motivation.
- [Angular Material — System variables](https://material.angular.dev/guide/system-variables) — `mat.define-theme` mixin emitting `--mat-sys-*` tokens; `overrides` mixin pattern.
- [SLDS Global Styling Hooks](https://developer.salesforce.com/docs/platform/lwc/guide/create-components-css-design-tokens.html) — SLDS 2 rename of "design tokens" to "global styling hooks."
- [Panda CSS — Multiple Themes](https://panda-css.com/docs/guides/multiple-themes) — composable axes via `conditions` and data attributes.
- [Atlassian — Migrate to tokens](https://atlassian.design/tokens/migrate-to-tokens/) — codemod-assisted migration playbook.
- [Steve Dodier-Lazaro — Automate design token migrations with codemods (Back Market case study)](https://medium.com/@stevedodierlazaro/automate-design-token-migrations-with-codemods-a21cf8bbd53b) — 4000+ refs across 2500 files; codemod authoring vs run-time trade-off.
- [Spencer Miskoviak — CSS codemods with PostCSS](https://www.skovy.dev/blog/css-codemods-with-postcss) — PostCSS codemod template.
- [web.dev — @property baseline](https://web.dev/blog/at-property-baseline) — `@property` reached Baseline July 2024; typed custom property contract.
- [Smashing — CSS Custom Properties In The Cascade](https://www.smashingmagazine.com/2019/07/css-custom-properties-cascade/) — cascade semantics for CSS variables.
