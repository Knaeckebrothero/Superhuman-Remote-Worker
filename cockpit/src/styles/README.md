# Cockpit Styles

This is the engineering side of the design system. For brand intent, palette rationale, and "when to use which theme", see [`design/themes/`](../../../design/themes/README.md) at the repo root.

## Layout

```
src/styles/
├── _variables.scss            Sass scalars (spacing, breakpoints, z-index, type scale). Radii live in _root-tokens.scss as CSS variables — there are no Sass `$radius-*` scalars anymore.
├── _mixins.scss               Utility mixins (focus-ring, breakpoints, truncate, visually-hidden).
├── _root-tokens.scss          Primitive CSS variables at :root (--radius-sm/md/lg/xl/full, --font-family-base/display/mono).
├── _semantic-tokens.scss      Role tokens at :root (--radius-control/surface/pill/tag, --font-primary/control/mono).
├── _shape-recipes.scss        Shape archetype mixins (control, surface, pill, tag, circle, stamp).
├── _typography-recipes.scss   Typography recipe mixins (display, eyebrow, mono).
└── themes/
    ├── _theme-config.scss     Token maps — one per theme. Source of truth for palette + on-tokens.
    ├── _themes.scss           apply-app-theme($name) mixin. Emits the map as CSS custom properties.
    ├── _shape-overrides.scss  Roman theme overrides: sharp radii, Cinzel typography, Inset Stamp shadow tokens. Scoped under .theme-* selectors.
    └── _typography.scss       Type-scale Sass maps (legacy; being folded into recipes).
```

The single entry point is `src/styles.scss` — it `@use`s the modules above, defines the `.theme-*` body classes, and exposes Material Symbols + a few global resets.

## Theme architecture

Themes work via **CSS custom properties + body class swap**. There is no per-component theme logic.

1. Each theme is an SCSS map in `_theme-config.scss` (e.g. `$senate-theme`, `$travertine-theme`).
2. Each map is registered in `$themes` keyed by name.
3. `styles.scss` emits a body class per theme:
   ```scss
   .theme-senate     { @include theme.apply-app-theme('senate'); }
   ```
4. `apply-app-theme($name)` walks the map and emits `--<token>: <value>;` for each entry.
5. Components consume `var(--token-name)` and stay theme-agnostic.

Token names are bare (`--accent-color`, `--panel-bg`, `--text-primary`) — no prefix. They're stable across themes, so adding a new theme rarely requires touching components.

## Token tiers

Three layers. Only the middle one is what most primitives actually read.

1. **Primitive scale** — declared in `_root-tokens.scss` at `:root`. Raw values: `--radius-sm/md/lg/xl/full`, `--font-family-base/display/mono`. Themes can override these to retheme the whole system (Roman themes flatten `--radius-md` to `0`, which cascades through every role that aliases it).

2. **Semantic roles** — declared in `_semantic-tokens.scss` at `:root`. Each aliases a primitive: `--radius-control: var(--radius-md)`, `--font-primary: var(--font-family-base)`, etc. Primitives consume these via recipe mixins (`@include shape.control`), never the raw primitive scale directly.

3. **Component-local** (optional, per primitive) — a primitive may declare `--btn-radius: var(--radius-control)` and read its own local var. This is the override surface for one-off variants without writing competing selectors. Bootstrap 5.3 pattern.

Themes override at the tier that gives the right scope:

- **Global retheme** → override the primitive (`--radius-md: 0` in `_shape-overrides.scss` flattens everything that uses md).
- **Role retheme** → override the role (`--radius-control: var(--radius-full)` makes every control pill-shaped without touching the surface or tag scales).
- **One-off** → override the component-local (`--btn-radius: 12px` on a specific button).

## Active themes

| Key | Mode | Notes |
|---|---|---|
| `travertine` | Light | **Light default + initial paint fallback.** Default when `system` resolves to light. |
| `senate` | Dark | Default when `system` resolves to dark. Lifted slate base (was charcoal in earlier revisions — the lift fixed contrast and obviated Praetorian). |

First-run preference is `'system'` — the app respects the OS preference. A pre-paint script in `index.html` resolves the right body class before Angular hydrates so dark-OS users don't flash through the Travertine fallback.

`theme.service.ts` migrates legacy localStorage values transparently:
- `dark` → `senate` (Catppuccin era)
- `light` → `travertine` (Catppuccin era)
- `praetorian` → `senate` (retired high-contrast theme)

## How to add a theme

1. **Define the token map** in `_theme-config.scss`. Easiest path: copy `$senate-theme`, rename, change colors. Keep the same keys — components depend on them.
2. **Register it** in the `$themes` map at the bottom of the same file:
   ```scss
   $themes: (
     'travertine': $travertine-theme,
     'senate':     $senate-theme,
     'mytheme':    $mytheme-theme,   // <-- here
   );
   ```
3. **Add the body class** in `src/styles.scss`:
   ```scss
   .theme-mytheme { @include theme.apply-app-theme('mytheme'); }
   ```
4. **Extend the type union** in `src/app/core/services/theme.service.ts`:
   ```ts
   export type ConcreteTheme = 'travertine' | 'senate' | 'mytheme';
   ```
   And add `'mytheme'` to `VALID_PREFERENCES`.
5. **Add it to the picker** — `OPTIONS` in `src/app/ui/theme-toggle/theme-toggle.component.ts`. Pick a `group` (`'light'` or `'dark'`) so it lands in the right `<optgroup>`.
6. **Document the design intent** in `design/themes/README.md` — palette story, when to use it, what it's *for*.
7. **Test the picker test** — `theme.service.spec.ts` should already cover the new theme via the generic body-class swap test, but add a smoke test if your theme has special semantics.

If your theme departs from the Roman shape language (rounded corners, different display font, etc.), you'll also need to either:
- Override the relevant tokens (`--font-display`, `--radius-md`) inside the map, or
- Add a `.theme-mytheme { ... }` block to `_shape-overrides.scss` that resets/overrides the shared Roman overrides.

## Token catalog

The current token set:

**Surfaces**: `--app-bg`, `--panel-bg`, `--panel-header-bg`, `--timeline-bg`, `--surface-0`, `--surface-1`, `--surface-2`

**Borders**: `--border-color`

**Text**: `--text-primary`, `--text-secondary`, `--text-muted`

**Accent**: `--accent-color`, `--accent-hover`

**Tracks/gutters** (split panes, sliders): `--track-bg`, `--gutter-color`, `--gutter-hover`

**Interactive overlays**: `--hover`, `--active`

**Semantic colors** (each with a matching `-tint` variant): `--success`, `--warning`, `--alert`, `--info`, `--danger`

**On-tokens** (foreground on solid-fill variants, WCAG-AA-tuned per theme): `--on-accent`, `--on-warning`, `--on-success`, `--on-danger`, `--on-info`

**Shadows**: `--shadow-sm`, `--shadow-md`, `--shadow-glow`

**Shape primitives** (`_root-tokens.scss`, `:root`): `--radius-sm/md/lg/xl/full`

**Shape roles** (`_semantic-tokens.scss`, `:root`): `--radius-control/surface/pill/tag`

**Typography primitives**: `--font-family-base/display/mono`

**Typography roles**: `--font-primary`, `--font-control`, `--font-mono`

**Roman-only**: `--font-display` (legacy alias for `--font-family-display`), `--letter-spacing-display`, `--text-transform-display`, `--stamp-highlight/shadow/drop/press/press-shadow`, `--user-bubble`, `--user-bubble-text`

Don't introduce hex literals in component SCSS. If a needed color token is missing, add it to **every** theme map at once — leaving a token undefined for one theme means components break under that theme.

## Authoring a primitive

Three rules:

1. Never hardcode `border-radius` — consume a recipe mixin (`shape.control`, `shape.surface`, etc.).
2. Never hardcode `font-family` for primary or display text — use `type.display`, `type.eyebrow`, or `type.mono`.
3. Component-local override surfaces are opt-in. Primitives with realistic one-off needs (button, input, card, dialog) declare `--<name>-radius`. Primitives with a single site (spinner, switch, radio) consume the role directly.

### Picking the right recipe

| Component archetype | Shape recipe |
|---|---|
| Button, input, select, chip, badge, tab, icon-button | `@include shape.control` |
| Card, dialog, menu, toast, panel | `@include shape.surface` |
| Pill chip, status pill | `@include shape.pill` |
| Checkbox tile, small inline tag | `@include shape.tag` |
| Radio dot, switch knob, spinner, avatar, round icon-button | `@include shape.circle` |
| Primary / secondary button (full Inset Stamp) | + `@include shape.stamp` |
| Tinted button: warning / info / success / danger (soft stamp) | + `@include shape.stamp('soft')` |

| Typographic role | Recipe |
|---|---|
| Primary button label, brand text, panel title | `@include type.display` |
| Section label, kicker, eyebrow | `@include type.eyebrow` |
| Code, tool args, debug surfaces | `@include type.mono` |

### Worked example — button

```scss
@use '../../../styles/shape-recipes' as shape;
@use '../../../styles/typography-recipes' as type;

.app-button__btn {
  --btn-radius: var(--radius-control);   // opt-in local override surface
  border-radius: var(--btn-radius);
  @include shape.stamp;

  &[data-variant='primary'] {
    @include type.display;
    background: var(--accent-color);
    color: var(--on-accent);
  }

  &[data-variant='warning'] {
    @include shape.stamp('soft');
    background: var(--warning);
    color: var(--on-warning);
  }
}
```

### Worked example — card (surface, no override needed)

```scss
@use '../../../styles/shape-recipes' as shape;

.app-card {
  @include shape.surface;
  background: var(--surface-0);
  box-shadow: var(--shadow-md);
}
```

### Worked example — switch knob (functional circle)

```scss
@use '../../../styles/shape-recipes' as shape;

.app-switch__knob {
  @include shape.circle;
  background: var(--surface-0);
}
```

### Component-local override consumers

A primitive that exposes `--btn-radius` (as in the button example above) can be re-shaped by any consumer without rewriting selectors:

```scss
// In a feature component that wraps the primitive:
.my-special-page .app-button__btn {
  --btn-radius: 12px;  // one-off; doesn't affect any other button
}
```

## Shape overrides

`_shape-overrides.scss` is scoped under `.theme-travertine, .theme-senate` and declares the **token overrides** that produce the Roman shape language: sharp radii (flattening the primitive scale), Cinzel as the display family, and the Inset Stamp shadow stack (`--stamp-highlight/shadow/drop/press/press-shadow`). Per-theme tweaks (Travertine's gold inlay under panel headers, Senate's blood-red equivalent) follow in their own scoped blocks.

The Inset Stamp recipe lives in `_shape-recipes.scss` as `@mixin stamp($variant)`. The token contract: Roman themes set the `--stamp-*` family; non-Roman themes leave them unset and the recipe falls back to `transparent`, collapsing to a flat button. Tinted button variants (warning, info, success, danger) get `stamp('soft')` to avoid the muddy inner shadow on translucent fills.

Legacy component-class selectors (`.btn`, `.session-message .message-bubble`, `.approval-card`) in `_shape-overrides.scss` predate the recipe model and target classes that have largely been renamed during the BEM migration. They're being removed as primitives migrate to recipes (`docs/features/design_system_completion.md` Phase 3). New shape rules belong in a recipe mixin, not as a body-class-scoped selector.

## Verification

When changing themes or tokens, run:

```bash
npm test -- --run        # vitest, including theme.service.spec.ts
npm run build            # full Angular production build
npm run lint:styles      # stylelint on src/**/*.scss
```

The theme service spec covers preference resolution, legacy migration, system-mode listening, and body-class swapping. SCSS errors surface during the production build (the dev server's HMR can hide them).

## Stylelint

`.stylelintrc.json` extends `stylelint-config-standard-scss` with cockpit-specific overrides. The rule that exists *because of this design system* is the `border-radius` regression guard:

```json
"declaration-property-value-disallowed-list": {
  "border-radius": ["/\\d+px/"],
  "border-top-left-radius": ["/\\d+px/"],
  "border-top-right-radius": ["/\\d+px/"],
  "border-bottom-left-radius": ["/\\d+px/"],
  "border-bottom-right-radius": ["/\\d+px/"]
}
```

`var(--*)`, `0`, `50%`, and shorthand asymmetric values like `0 var(--radius-control) var(--radius-control) 0` are all allowed. Raw `Npx` values are blocked. If you genuinely need a one-off px value — don't; consume `--radius-control` or declare a component-local override (`--btn-radius: var(--radius-control)` then override that). The rule is the safety net that protects the token consistency built over Phases 1-4.

**Scope:** the lint script only runs on `*.scss` files, not on Angular inline `styles:` arrays in `.ts` files. Inline styles ship through Angular's SCSS preprocessor but stylelint has no Angular-aware processor to extract them. Inline-TS radii are not lint-enforced; a periodic grep (`grep -rnE "border-radius: *[0-9]+px" src/`) is the manual backstop.

A few standard-scss rules are disabled — see `.stylelintrc.json` comments-in-spirit:
- `value-keyword-case` (would lowercase `BlinkMacSystemFont` and break font convention)
- `scss/comment-no-empty` (flags `// --- Section ---` divider comments)
- `color-function-alias-notation` (keeps `rgba()` legal alongside `rgb()` with alpha)
- A handful of cosmetic rules (`declaration-block-single-line-max-declarations`, etc.) that don't add signal at this scale.

## Cross-references

- Brand intent + palette rationale: [`design/themes/README.md`](../../../design/themes/README.md)
- Theme service: `src/app/core/services/theme.service.ts`
- Theme picker: `src/app/ui/theme-toggle/theme-toggle.component.ts`
- Brand mark: `src/app/ui/legion-mark/legion-mark.component.ts`
