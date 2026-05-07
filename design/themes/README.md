# Themes

The SRW Cockpit ships two themes. They share token names so any component that resolves `var(--accent-color)`, `var(--panel-bg)`, etc. picks up whichever theme is active without per-component logic. Both themes are calibrated against a closed four-hue **Imperial palette** (Blood / Gold / Lapis / Laurel) tuned in OKLCH so chips read as a tight family on each background.

## Travertine — Light (default for light OS, initial paint fallback)

> Cream travertine + porphyry red + gold ochre. Daytime, formal.

The light-mode default — picked when `system` resolves to light. Also the body class shipped in `index.html` as the no-JS fallback. Built around the cream-and-red palette of Roman travertine stone and porphyry, with gold ochre as the warm semantic accent. Body text is a deep umber rather than near-black so the page reads as parchment, not paper. All semantic hues land at L≈40 OKLCH (gold at L≈53 — must stay lighter to read as gold rather than mud).

| Token | Value | Usage |
|---|---|---|
| `--app-bg` | `#f3ece0` | Travertine cream — page background |
| `--panel-bg` | `#fbf6ec` | Lighter cream — panel surfaces |
| `--accent-color` | `#9c2832` | Porphyry red — primary brand color, links, primary buttons |
| `--text-primary` | `#2a1d12` | Deep umber — body text |
| `--warning` | `#9a7822` | Gold ochre — also used for the topbar inlay |

**When to use**: Daytime / well-lit environments, formal or print-adjacent contexts (reports, summaries).

## Senate — Dark (default for dark OS)

> Lifted slate base, brighter blood red. Calibrated for legibility without becoming cold.

The dark-mode default — picked when `system` resolves to dark. The base was lifted from the original `#0c0c0d` charcoal to `#141418` slate; body text moved to `#f4f2ee` for ~13:1 contrast, muted text to ~5.5:1. The five-hue palette is tuned to ~L68 OKLCH so every semantic chip reads as a family member on the dark surface. Gold runs slightly higher (~L73) so it doesn't fall to mud.

This lift was the contrast fix that obviated a separate high-contrast theme. The trade-off: Senate is no longer "near-black, only-red-saturated"; it reads more like a refined evening slate than a moonless charcoal.

| Token | Value | Usage |
|---|---|---|
| `--app-bg` | `#141418` | Lifted slate — page background |
| `--panel-bg` | `#1c1c22` | Panel surfaces |
| `--accent-color` | `#cc4647` | Blood red — primary brand color |
| `--text-primary` | `#f4f2ee` | Warm off-white — body text |
| `--text-muted` | `#8c8a87` | Warm gray — secondary text |

**When to use**: Default for most users / sessions. The dark theme picked when the OS prefers dark and `system` is selected.

## Praetorian — *retired*

The earlier high-contrast Praetorian theme was retired. Its mandate (sharper legibility for bright rooms / glare / late-night work) is now met by the lifted Senate values. Users who need maximum contrast for accessibility should prefer the OS-level forced-colors mode, which the cockpit honors.

The Praetorian palette and shape pass are preserved outside this repo for a future rework — when more themes land, Praetorian-the-aesthetic (pure black, crimson, ivory, no mid-grays, hairline rules instead of shadows) will likely return as a refined variant.

Existing localStorage values of `'praetorian'` migrate to `'senate'` transparently on first read.

## The four Imperial slots

| Slot | Token | Travertine | Senate | Use for |
|---|---|---|---|---|
| **Blood** | `--accent-color` / `--danger` | `#9c2832` | `#cc4647` | Brand, primary actions, destructive states. |
| **Gold** | `--warning` | `#9a7822` | `#cdab68` | Cautions, soft warnings, "review needed". |
| **Lapis** | `--info` | `#3f5e8c` | `#7a9bc6` | Tool-call tags, neutral informational chips, IDs. |
| **Laurel** | `--success` | `#446b3e` | `#82b178` | Real completion. Don't use for "loading done" / "saved" — only outcomes. |

Each slot also has a `*-tint` variant (~15–18% alpha) for chip backgrounds. The palette is closed: if a designer asks for "a teal accent" or "a coral status", push back. New hues mean new semantic slots, which means new product meaning, which is a design decision, not a styling one.

The two exceptions:
- `--alert` (copper, `#c2722a` / `#d48a4d`) is retained for hard-stop alerts but should be rare. Treat it as "use only if `--warning` isn't strong enough."
- Chart palettes / data viz follow their own rules — these tokens are for UI chrome, not data encoding.

## Shape language

Both themes share a "Roman" shape pass (`cockpit/src/styles/themes/_shape-overrides.scss`):

- **Sharp radii** — `--radius-sm: 0`, `--radius-md: 0`, `--radius-lg: 2px`. No rounded corners.
- **Cinzel display** — `--font-display: 'Cinzel'` for brand mark, panel titles, section headings, primary button labels. Uppercase + letter-spacing.
- **Banded chat bubbles** — top edge has a 5%-tinted accent gradient over the first 8px.
- **Inset Stamp buttons** — sharp corners, dual inner highlight + shadow, accent-colored drop. Press translates 1px down. Reads "stamped into stone."
- **Square avatars** — no `border-radius: 50%`.
- **Approval card** — 3px porphyry-red left rule.
- **Travertine** adds a 1px gold-ochre inlay under panel headers.
- **Senate** mirrors that with a blood-red 40%-mix inlay.

## Reference files

| File | Status |
|---|---|
| `_theme-config.reference.scss` | Original mockup token maps from the **first** Roman cutover. Predates the Imperial palette calibration and the Senate lift. Treat as historical intent, not current truth. |
| `_shape-overrides.reference.scss` | Original mockup shape pass. Predates the Inset Stamp button treatment. |
| `legion-mark.reference.component.ts` | Original mockup of the brand mark. Shipped version: `cockpit/src/app/ui/legion-mark/legion-mark.component.ts`. |

These `*.reference.*` files are kept as historical mockup snapshots — what the design originally proposed before code review and theme-system integration. The current source of truth is `cockpit/src/styles/themes/_theme-config.scss` and `_shape-overrides.scss`; if you're changing the palette, edit those, then update the doc above.

## Cross-references

- Engineering docs (how to add/modify themes): [`cockpit/src/styles/README.md`](../../cockpit/src/styles/README.md)
- Theme service (preference resolution + legacy migration): `cockpit/src/app/core/services/theme.service.ts`
- Theme picker UI: `cockpit/src/app/ui/theme-toggle/theme-toggle.component.ts`
