# Cockpit Styles

This is the engineering side of the design system. For brand intent, palette rationale, and "when to use which theme", see [`design/themes/`](../../../design/themes/README.md) at the repo root.

## Layout

```
src/styles/
├── _variables.scss        Static values: spacing, font sizes, breakpoints, z-index, radii, durations.
├── _mixins.scss           Shared SCSS mixins (focus-ring, screen-reader-only, etc.).
└── themes/
    ├── _theme-config.scss   Token maps — one per theme. The source of truth for palette values.
    ├── _themes.scss         apply-app-theme($name) mixin. Emits the map as CSS custom properties.
    ├── _shape-overrides.scss  Roman shape pass: sharp radii, Cinzel display, banded bubbles. Scoped under .theme-* selectors.
    └── _typography.scss     Type-scale mixins.
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

## Active themes

| Key | Mode | Notes |
|---|---|---|
| `travertine` | Light | **Light default + initial paint fallback.** Default when `system` resolves to light. |
| `senate` | Dark | Default when `system` resolves to dark. |
| `praetorian` | Dark, high-contrast | Shadow-less. |

First-run preference is `'system'` — the app respects the OS preference. A pre-paint script in `index.html` resolves the right body class before Angular hydrates so dark-OS users don't flash through the Travertine fallback.

The legacy Catppuccin `dark` / `light` themes were removed; `theme.service.ts` migrates old localStorage values transparently (`dark` → `senate`, `light` → `travertine`).

## How to add a theme

1. **Define the token map** in `_theme-config.scss`. Easiest path: copy `$senate-theme`, rename, change colors. Keep the same keys — components depend on them.
2. **Register it** in the `$themes` map at the bottom of the same file:
   ```scss
   $themes: (
     'travertine': $travertine-theme,
     'senate':     $senate-theme,
     'praetorian': $praetorian-theme,
     'mytheme':    $mytheme-theme,   // <-- here
   );
   ```
3. **Add the body class** in `src/styles.scss`:
   ```scss
   .theme-mytheme { @include theme.apply-app-theme('mytheme'); }
   ```
4. **Extend the type union** in `src/app/core/services/theme.service.ts`:
   ```ts
   export type ConcreteTheme = 'travertine' | 'senate' | 'praetorian' | 'mytheme';
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

**Shadows**: `--shadow-sm`, `--shadow-md`, `--shadow-glow`

**Roman-only**: `--font-display`, `--user-bubble`, `--user-bubble-text`

Don't introduce hex literals in component SCSS. If a needed token is missing, add it to **every** theme map at once — leaving a token undefined for one theme means components break under that theme.

## Shape overrides

`_shape-overrides.scss` is scoped under `.theme-travertine, .theme-senate, .theme-praetorian` and applies the Roman shape language (sharp radii, Cinzel headings, banded bubbles, square avatars, accent left-rule on approval cards). Per-theme tweaks (Travertine's gold inlay, Praetorian's shadow removal) follow in their own scoped blocks.

Selectors target generic structural classes (`.session-message .message-bubble`, `.approval-card`, `.sidebar-brand`). When you add a new component, prefer landing on those existing class names where it makes sense — the shape pass picks them up automatically.

## Verification

When changing themes or tokens, run:

```bash
npm test -- --run        # vitest, including theme.service.spec.ts
npm run build            # full Angular production build
```

The theme service spec covers preference resolution, legacy migration, system-mode listening, and body-class swapping. SCSS errors surface during the production build (the dev server's HMR can hide them).

## Cross-references

- Brand intent + palette rationale: [`design/themes/README.md`](../../../design/themes/README.md)
- Theme service: `src/app/core/services/theme.service.ts`
- Theme picker: `src/app/ui/theme-toggle/theme-toggle.component.ts`
- Brand mark: `src/app/ui/legion-mark/legion-mark.component.ts`
