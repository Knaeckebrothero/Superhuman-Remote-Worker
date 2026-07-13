# Debug page → Imperial palette (categorical ramp + full Catppuccin sweep)

- **Date:** 2026-07-13
- **Status:** **Implemented** · 2026-07-13 · develop `764e0997..b33e2396` (10 commits; plan `e24713d5`). 966 cockpit specs green, `tsc -p tsconfig.app.json --noEmit` clean, both Catppuccin gates (hex + full-palette rgba) zero across `cockpit/src`. Live theme-flip eyeball of the Cytoscape graph on both themes still owed.
- **Scope owner:** Cockpit frontend (`cockpit/src`)

## Problem

The Debug grid page (`cockpit/src/app/debug`) was originally styled in **Catppuccin
Mocha** (`#cba6f7` mauve, `#f38ba8` pink, `#a6e3a1` green, `#89b4fa` blue…). When the
Imperial theme system (Travertine light / Senate dark) was introduced, the components
were retrofitted with `var(--token, #catppuccin-fallback)` wrappers rather than fully
migrated. The result is three buckets of color debt:

- **Bucket A — dead fallbacks** (`var(--panel-bg, #181825)`): resolve to Imperial when
  the theme class is on `<body>` (it always is), so they render correctly today. Cosmetic
  debt only. ~25 files across the app carry these.
- **Bucket B — semantic hardcodes**: error text `#f38ba8`, success `#a6e3a1`, the memory
  score heatmap green/yellow/red, `agent-steps` complete/error. These have real meaning
  but are frozen to Catppuccin and visibly clash with the Imperial reds/greens.
- **Bucket C — categorical palettes** (the design problem): four rainbow sets that need
  8–10 distinguishable colors, hardcoded to Catppuccin —
  - agent event-type / tool-category **badges** (`agent-activity`)
  - graph **node types** (`graph-styles.ts`, Cytoscape)
  - memory **types & sources** (`memory-panel`)
  - the layout-picker **thumbnails** (`layout-preview`, SVG)

The debug page is the **only** surface with visible Bucket-C rainbows; elsewhere it's
Bucket-A (invisible) plus a few small Bucket-B strays.

## Goals

1. Replace the debug page's categorical rainbows with a **theme-aware, on-brand
   categorical ramp** that stays distinguishable and shifts with light/dark.
2. Map all semantic hardcodes to the existing semantic tokens so status colors are
   consistent app-wide.
3. Full sweep: remove the dead Catppuccin fallbacks and stray semantic hardcodes so the
   theme config becomes the single source of color truth.

## Non-goals

- No functional/logic changes — this is style-only.
- No redesign of the Imperial semantic palette itself.
- No new theme beyond Travertine/Senate.

## Design

### 1. Token layer — the Imperial categorical ramp

Add eight per-theme tokens **`--cat-1 … --cat-8`** to `$travertine-theme` and
`$senate-theme` in `cockpit/src/styles/themes/_theme-config.scss`, so they flow through
`apply-app-theme` exactly like the existing semantic hues. Calibrated to the **same
OKLCH lightness band** the Imperial palette already uses (~L40 on Travertine, ~L68 on
Senate).

Structure: slots **2/3/6 reuse** the existing Imperial hues (copper/gold/lapis); slots
**1 & 4 are offset cousins** of danger/success (terracotta ≠ blood-red, olive ≠
laurel-green) so a nominal category chip never masquerades as a status; slots **5/7/8
are new** harmonized hues.

| Slot | Name       | Senate (dark) | Travertine (light) | Origin              |
|------|------------|---------------|--------------------|---------------------|
| 1    | Terracotta | `#c8674e`     | `#a8492f`          | offset of danger    |
| 2    | Copper     | `#d48a4d`     | `#c2722a`          | = alert             |
| 3    | Gold       | `#cdab68`     | `#9a7822`          | = warning           |
| 4    | Olive      | `#a7b06a`     | `#6e7534`          | offset of success   |
| 5    | Slate-teal | `#5fb0a8`     | `#2f7d74`          | new                 |
| 6    | Lapis      | `#7a9bc6`     | `#3f5e8c`          | = info              |
| 7    | Violet     | `#a98fc4`     | `#6f5591`          | new                 |
| 8    | Mauve      | `#c98aa3`     | `#8f4d63`          | new                 |

Sizing: the largest nominal set is layout thumbnails (8) and graph node types (8 real +
`Default`→muted), so 8 covers everything; smaller sets use a prefix of the ramp. Final
slot→category assignment is chosen so that **adjacent categories in a given set land on
contrasting hues** (not necessarily sequential slots).

> Exact hex values are the approved v1 from the visual companion. During implementation,
> run them through the `dataviz` skill's contrast validator and nudge only if a swatch
> fails AA against its surface or against an adjacent swatch; keep the hue identities.

### 2. Assignment rules (applied to every color site)

**Rule 1 — semantic categories pull the real token.** Anything that means
error/success/warning/deleted uses the existing semantic tokens, never the ramp:

| Category                                                              | Token          |
|-----------------------------------------------------------------------|----------------|
| step `error`, memory `error_solution`, memory source `tool_error`, graph `deleted`, heatmap `< 0.5`, `agent-steps.error` | `--danger`  |
| heatmap `≥ 0.8`, graph `created`, `agent-steps.complete`              | `--success`    |
| heatmap `0.5–0.8`                                                     | `--warning`    |
| graph `unchanged`, graph `Default` node                              | `--text-muted` |

> **As-built carve-out (Okabe-Ito) — amends the graph rows above.** The graph
> change-state borders (`created` / `modified` / `deleted`) are **not** mapped to
> `--success` / `--danger` as the table originally proposed. They keep the
> intentional Okabe-Ito colorblind-safe palette (`#0072B2` blue / `#E69F00` amber
> / `#D55E00` vermillion) already in the code; recoloring them to theme red/green
> would regress colorblind accessibility and let a *status* border read as a
> *nominal* hue. Only graph `unchanged` → `--text-muted` from Rule 1 applies to
> the graph. This carve-out was flagged during planning (plan §"Deviation from
> spec") and confirmed at implementation.

**Rule 2 — nominal categories cycle the ramp by stable index.** Event step types, tool
categories, memory types/sources, graph node types, layout thumbnails map to `--cat-N`
via a fixed key→index map (stable = a given key keeps its color across renders). The
per-set maps live where they do today (component-local `Record<Type,string>` objects),
but the values become `var(--cat-N)` (DOM surfaces) or a resolver lookup (Cytoscape).

### 3. Consumption mechanics (three surface types)

- **DOM / CSS surfaces** (agent-activity badges, memory-panel chips): the color maps hold
  `'var(--cat-N)'` strings applied via `[style.color]` / `[style.background]` /
  `color-mix()` in component styles. Theme-aware for free.
- **SVG surface** (`layout-preview`): switch the rect binding from `[attr.fill]`
  (presentation attribute — does **not** support `var()`) to **`[style.fill]`** (CSS
  `fill` property — does). Same for stroke. Values become `var(--cat-N)` / `var(--surface-1)`.
- **Cytoscape surface** (`graph-styles.ts` → `graph-timeline`): Cytoscape needs concrete
  color strings and cannot read `var()`. Introduce a small resolver that reads the tokens'
  computed values via `getComputedStyle(document.documentElement).getPropertyValue('--cat-N')`,
  builds the stylesheet from those, and **rebuilds inside an `effect()` on
  `themeService.resolved()`** so the graph recolors when the user flips light/dark. This is
  the only non-mechanical code in the change. `CHANGE_COLORS`, `LABEL_COLORS`, and
  `cytoscapeStyles` become functions of the resolved token set instead of static hex.

### 4. Full-sweep cleanup

- **Debug-page Bucket B** → tokens per Rule 1.
- **Dead Catppuccin fallbacks (Bucket A), ~25 files:** drop the fallback entirely —
  `var(--panel-bg)` not `var(--panel-bg, #181825)`. The theme class is always applied on
  `<body>`, so the theme config is the single source of truth. (If any surface renders
  before the body class lands, that's a pre-existing issue out of scope here.)
- **Stray semantic hardcodes elsewhere:** `agent-steps.component.scss`
  (`.complete`/`.error`), the `#f97316→#fab387` step-icon gradient, and the
  `user.service.ts` default avatar color `#89b4fa` → an Imperial value (`--cat-6` lapis or
  `--accent-color`; pick lapis so avatars don't all read as the accent).

## In-scope file inventory

**Debug page (11 files with color):**
`components/agent-activity`, `components/memory-panel`, `components/request-viewer`,
`components/graph-timeline/{graph-timeline,graph-styles}`, `components/timeline`,
`components/db-table`, `components/layout-picker/{layout-picker,layout-preview}`,
`layout/panel-header`, `components/menu`, `components/placeholders/*`.

**Theme config:** `styles/themes/_theme-config.scss` (add `--cat-1..8` to both maps).

**Non-debug sweep (~25 files):** Bucket-A fallback removal across `app/shell/*`,
`app/views/*`, `app/ui/*`, plus the named Bucket-B strays above. Bulk of this is
mechanical find/replace of `, #<catppuccin>)` → `)`.

## Verification

- **Visual:** toggle Travertine⇄Senate via the theme toggle and eyeball every debug panel
  and the graph (exercises the reactive-recolor `effect()` path). Confirm the offset
  cousins read as distinct from status (terracotta ≠ error, olive ≠ success).
- **Grep gate:** zero Catppuccin signature hex left in `cockpit/src/app` and
  `cockpit/src/styles` (`#cba6f7|#f38ba8|#a6e3a1|#89b4fa|#f9e2af|#fab387|#94e2d5|#f5c2e7|
  #89dceb|#74c7ec|#b4befe|#cdd6f4|#a6adc8|#6c7086|#313244|#1e1e2e|#181825|#11111b|#45475a`).
- **Tests:** existing vitest suite stays green (style-only; no logic touched).
- **Contrast:** `dataviz` validator over the final ramp on both surfaces (AA vs surface,
  distinct vs neighbors).

## Risks / notes

- **Cytoscape recolor** is the one place a bug can hide (stale colors after theme flip, or
  colors read before the body class is applied). Resolve tokens lazily inside the effect,
  not at module load.
- **`color-mix` support:** badges use `color-mix()` for tint backgrounds; already used
  elsewhere in the app (`_shape-overrides.scss`), so browser support is a settled question.
- The ~25-file fallback sweep is low-risk but a large diff with mostly invisible payoff;
  it can be split into its own commit so review stays legible.
