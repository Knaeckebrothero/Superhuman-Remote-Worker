# Themes

The SRW Cockpit ships three themes. They share token names so any component that resolves `var(--accent-color)`, `var(--panel-bg)`, etc. picks up whichever theme is active without per-component logic.

## Travertine — Light

> Cream travertine + porphyry red + gold ochre. Daytime, formal.

The light default. Built around the cream-and-red palette of Roman travertine stone and porphyry, with gold ochre as the warm semantic accent. Body text is a deep umber rather than near-black so the page reads as parchment, not paper.

| Token | Value | Usage |
|---|---|---|
| `--app-bg` | `#f3ece0` | Travertine cream — page background |
| `--panel-bg` | `#fbf6ec` | Lighter cream — panel surfaces |
| `--accent-color` | `#9c1f2e` | Porphyry red — primary brand color, links, primary buttons |
| `--text-primary` | `#2a1d12` | Deep umber — body text |
| `--warning` | `#a07a1c` | Gold ochre — also used for the topbar inlay |

**When to use**: Daytime / well-lit environments, formal or print-adjacent contexts (reports, summaries).

## Senate — Dark (default)

> Pure blood red on charcoal. The standard dark.

The overall default theme. Near-black charcoal with blood-red as the only saturated color — every other accent is muted. This is the "you're at your desk in the evening" theme; it should feel quiet, not flashy.

| Token | Value | Usage |
|---|---|---|
| `--app-bg` | `#0c0c0d` | Charcoal — page background |
| `--panel-bg` | `#111114` | Slightly lifted charcoal — panel surfaces |
| `--accent-color` | `#a8232a` | Blood red — primary brand color |
| `--text-primary` | `#eceaea` | Off-white — body text |
| `--text-muted` | `#74706e` | Warm gray — secondary text |

**When to use**: Default for most users / sessions. The dark theme picked when the OS prefers dark and `system` is selected.

## Praetorian — Dark, high contrast

> Pure black, crimson, ivory. No mid-grays.

Designed for environments where the standard Senate isn't sharp enough — bright rooms, ambient glare, or users who prefer maximum legibility. Pure black backgrounds, ivory text (`#f5f1e6`, deliberately *not* white to avoid CRT glow), and a brighter crimson. **Shadow-less by design** — separation is achieved through hairline rules, not blur.

| Token | Value | Usage |
|---|---|---|
| `--app-bg` | `#000000` | True black — page background |
| `--panel-bg` | `#0a0a0a` | Off-black — panel surfaces |
| `--accent-color` | `#dc1f2a` | Crimson — primary brand color |
| `--text-primary` | `#f5f1e6` | Ivory — body text |
| `--shadow-md` | hairline rule | All shadows replaced with 1px borders |

**When to use**: Accessibility / high-contrast preference, focus mode, late-night work in bright rooms.

## Shape language

All three themes share a "Roman" shape pass (`cockpit/src/styles/themes/_shape-overrides.scss`):

- **Sharp radii** — `--radius-sm: 0`, `--radius-md: 0`, `--radius-lg: 2px`. No rounded corners.
- **Cinzel display** — `--font-display: 'Cinzel'` for brand mark, panel titles, section headings, primary button labels. Uppercase + letter-spacing.
- **Banded chat bubbles** — top edge has a 5%-tinted accent gradient over the first 8px.
- **Square avatars** — no `border-radius: 50%`.
- **Approval card** — 3px porphyry-red left rule.
- **Travertine** adds a 1px gold-ochre inlay under panel headers.
- **Praetorian** removes shadows entirely.

## Reference files

| File | Status |
|---|---|
| `_theme-config.reference.scss` | Original mockup token maps. The shipped version lives in `cockpit/src/styles/themes/_theme-config.scss` and may have drifted. Treat this as historical intent, not current truth. |
| `_shape-overrides.reference.scss` | Original mockup shape pass. Shipped version: `cockpit/src/styles/themes/_shape-overrides.scss`. |
| `legion-mark.reference.component.ts` | Original mockup of the brand mark. Shipped version: `cockpit/src/app/ui/legion-mark/legion-mark.component.ts`. |

These `*.reference.*` files are kept as historical mockup snapshots — what the design originally proposed before code review and theme-system integration. If you're changing the palette, update `cockpit/src/styles/themes/_theme-config.scss` (the shipped truth), then update the doc above.

## Cross-references

- Engineering docs (how to add/modify themes): [`cockpit/src/styles/README.md`](../../cockpit/src/styles/README.md)
- Theme service (preference resolution + legacy migration): `cockpit/src/app/core/services/theme.service.ts`
- Theme picker UI: `cockpit/src/app/ui/theme-toggle/theme-toggle.component.ts`
