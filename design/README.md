# Cockpit Design System

This folder is the **design source of truth** for the SRW Cockpit UI. It captures *intent* — the themes, brand language, and visual principles. The code that implements them lives in `cockpit/src/`.

## Contents

| Path | What it holds |
|---|---|
| [`themes/`](./themes/) | Theme catalog: palette rationale, mockup reference SCSS, brand-mark component, when to use which theme. |
| [`asset-pack/`](./asset-pack/) | PWA asset pack (favicons, icons, manifest, OG image, microcopy) — historical mockup. Shipped versions live in `cockpit/public/` and `cockpit/src/assets/`. The cockpit registers a service worker via `@angular/service-worker` with config in `cockpit/ngsw-config.json`; the offline + update-available banner lives at `cockpit/src/app/shell/pwa-banner/`. |

## Two-tier docs (this is by design)

We split design docs into two layers, each with a different audience:

- **`design/`** (this folder) — **why** we made these choices. Read this first if you want to understand the brand voice, the palette story, or whether a new screen should pick light or dark.
- **[`cockpit/src/styles/README.md`](../cockpit/src/styles/README.md)** — **how** to apply them in code. Token names, the `apply-app-theme` mixin, where to add a new theme, what `_shape-overrides.scss` does. Read this if you're writing or changing a component.

If you're tempted to copy a hex value out of a mockup into a component file, stop and check `cockpit/src/styles/themes/_theme-config.scss` for a token. The tokens are the contract — the hex literals in this folder are the historical mockup values.

## Themes

The cockpit currently ships three Roman-themed appearances. The pre-Roman Catppuccin Mocha/Latte themes were removed; legacy `dark` / `light` localStorage values migrate transparently to Senate / Travertine on first read.

| Key | Mode | Personality | Default |
|---|---|---|---|
| **Travertine** | Light | Cream stone, porphyry red, gold ochre. Daytime, formal. | **Light default + initial paint fallback** |
| **Senate** | Dark | Pure blood red on charcoal. The standard dark. | Dark default |
| **Praetorian** | Dark, high-contrast | Pure black, crimson, ivory. No mid-grays. | Accessibility / focus mode |

First-run preference is `system` — the app follows OS dark/light. `system` resolves to **Senate** when the OS prefers dark and **Travertine** when it prefers light.

See [`themes/README.md`](./themes/README.md) for the palette breakdown and the rationale behind each theme's color choices.

## Adding a new theme

The short version (full instructions in `cockpit/src/styles/README.md`):

1. Add a token map to `cockpit/src/styles/themes/_theme-config.scss` and register it under `$themes`.
2. Add `.theme-<name> { @include theme.apply-app-theme('<name>'); }` in `cockpit/src/styles.scss`.
3. Extend `ConcreteTheme` and `VALID_PREFERENCES` in `cockpit/src/app/core/services/theme.service.ts`.
4. Add an entry to `OPTIONS` in `cockpit/src/app/ui/theme-toggle/theme-toggle.component.ts`.
5. Drop a palette doc into `design/themes/` so future devs know what your theme is *for*, not just what colors it uses.

## Component primitives & screens

Component-level visual specs (chat composer, approval card, sidebar, etc.) currently live as comments in their respective component SCSS files and as theme-scoped overrides in `cockpit/src/styles/themes/_shape-overrides.scss`. If the primitive library grows past ~20 components, plan to migrate to Storybook — the markdown docs in this folder transplant cleanly into MDX.
