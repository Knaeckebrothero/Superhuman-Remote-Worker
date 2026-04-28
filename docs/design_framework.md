# UI / Design System Decision

Research notes and final decision for the Cockpit (Angular 21) frontend design system. Captured so the decision has a factual baseline; the decision itself is recorded at the bottom (Part 4).

**TL;DR — the decision (April 2026):** No new framework. Port the proven token architecture from `examples_ui/Advanced-LLM-Chat-develop` (the user's longest-running Angular project), modernize two specific weaknesses, and add the one layer the example was missing — a shared primitive component layer (`<app-button>`, `<app-input>`, …). See Part 4 for the full target architecture and migration plan.

Structure:

1. **Local audit** — what we have today in `cockpit/`
2. **Industry research (April 2026)** — what the Angular ecosystem looks like right now, what other teams are picking, and the best-practice baseline
3. **Implications for our specific situation** — how Parts 1 and 2 land for Cockpit
4. **Decision and target architecture** — what we're actually building

---

## Part 1 — Current state of the frontend

### Stack: zero UI framework

`cockpit/package.json` contains **no component library and no utility CSS framework**. Notable absences:

- No Angular Material, CDK, PrimeNG, ng-bootstrap, ng-zorro, DaisyUI
- No Tailwind, no PostCSS pipeline
- No icon library (Material Symbols font is referenced ad-hoc in component SCSS)

Styling-adjacent packages actually present:

- `prismjs` ^1.30.0 — syntax highlighting
- `clipboard` ^2.0.11 — clipboard utility
- `angular-split` ^20.0.0 — resizable panes

`angular.json` global styles array is just `["src/styles.scss", "node_modules/prismjs/plugins/line-numbers/prism-line-numbers.css"]`. Component schematics default to `inlineStyle: true, style: scss` — every new component carries scoped inline SCSS.

### The "design system" is one file

`cockpit/src/styles.scss` (~160 lines) is the entire global design layer:

- A single hardcoded **Catppuccin Mocha** dark theme exposed as CSS custom properties
  - `--app-bg: #1e1e2e`, `--panel-bg: #181825`
  - `--text-primary`, `--text-secondary`, `--text-muted`
  - `--accent-color: #cba6f7` (purple) plus interactive states
  - `--gutter-color`, `--track-bg`, `--border-color`
- Global resets (box-sizing, body sizing, font smoothing)
- Component-specific overrides for `angular-split` gutters and webkit scrollbars
- Accessibility: `:focus-visible`, 44 px touch targets on mobile, `prefers-reduced-motion`
- Experimental view-transition keyframes

No global SCSS variables file. No mixins. No theme switching. No light mode. No design tokens beyond the CSS variables above.

### No shared UI primitives

There is **no `ui/`, `primitives/`, or `components/` folder for low-level elements**. `shared/components/` holds feature-level components (job-list, agent-list, agent-settings, persistent-chat, etc.), not buttons or form fields.

Result: ~56 components independently redefine the same primitives:

- `.btn`, `.btn-primary`, `.btn-ghost` — duplicated with minor color tweaks across 24+ files
- `.badge` and 10+ badge variants — redefined per page (`pages/admin/users`, `pages/admin/providers`, `pages/project-detail`, …)
- `.form-input` — defined separately in `pages/admin/users`, `pages/admin/providers`, `pages/project-detail`, `simple/pages/session-create`, each with slightly different padding/border
- Hardcoded color hexes (`#f38ba8`, `#a6e3a1`, `#cba6f7`) and spacing literals (`24px`, `12px`, `8px`, `4px`) scattered alongside the CSS variables
- Long inline-style blocks: `pages/project-detail/project-detail.component.ts` has 200+ lines of styles in a single component

Forms use reactive forms with bare `<input class="form-input">` and `<select class="form-input">`. No `form-field`, no `input-group`, no error-message component. Each form reinvents validation styling.

### UI surface is moderate but lopsided

~57 components, ~12–15 user-facing screens across **two parallel layouts**:

- `simple/` (mobile-first) — 10 page components: shell, jobs, create, inbox, chat, datasources, sessions, session-create. **This is what's actually wired into routing and used in production.**
- `pages/` (desktop) — 5 components, mostly admin (users, providers, models) plus project-detail and settings. `layout/sidebar/` exists but is **not integrated into any route** — desktop is largely a skeleton.

Top-level routes (~14): `/`, `/sessions`, `/sessions/:threadId`, `/jobs`, `/create`, `/inbox`, `/projects`, `/projects/:id`, `/datasources`, `/settings`, `/admin/providers`, `/admin/models`, `/admin/users`, `/debug`, plus legacy redirects.

Mobile chrome: `SidebarToggleComponent` (minimal menu button). No bottom tabs, flat routing. Header bar 48 px (44 px breakpoint-adjusted).

Framework-agnostic constraints (none block any choice):

- Cytoscape.js + fcose layout for graph visualization (`debug/graph-timeline`)
- Prismjs syntax highlighting + marked for markdown rendering
- `angular-split` resizable panes
- Real-time streaming UI (agent steps, thinking panels with live status/spinners)

Zero `TODO` / `FIXME` / `HACK` markers around styling. The debt isn't being complained about in comments, it just grew organically.

### Verdict on current state

Pure custom SCSS with a single hardcoded dark theme via CSS custom properties. Lean and dependency-free, but:

- No component library
- No utility CSS framework
- No shared primitives layer
- 56+ components each carrying a local mini-design-system
- Two parallel layouts (mobile is real, desktop is half-built)

A UI framework would collapse most of the redundant inline styles and enforce consistency. **High ROI for adoption.**

---

## Part 2 — Industry research (April 2026)

### Angular 21 changes the baseline

Angular 21 (released Nov 2025) is the version we're on, and three changes affect framework choice:

- **Zoneless by default.** New apps no longer ship `zone.js`. UI libraries that still rely on Zone-based change detection are second-class citizens going forward.
- **Signal Forms (experimental)** and broad signal-input adoption inside the framework. Libraries that haven't migrated to signals look dated.
- **`@angular/aria`** — official headless WAI-ARIA primitives shipped with Angular 21, covering 12 interaction patterns (combobox, menu, toolbar, accordion, tabs, etc.). This is a **brand-new first-party option** that didn't exist a year ago and changes the "headless + Tailwind" calculus. ([angular.dev/guide/aria/overview](https://angular.dev/guide/aria/overview))

Sentiment context: Angular usage held at ~18% in the 2025 StackOverflow survey, State of JS 2024 retention ~54%. The Angular ecosystem is stable but smaller than React's, which keeps UI library activity concentrated in a handful of projects. ([State of JS 2025](https://2025.stateofjs.com/en-US/libraries/front-end-frameworks/))

### Framework landscape — concrete data

| Library | Latest | Angular 21 | Components | Theming | GitHub stars | Status |
|---|---|---|---|---|---|---|
| **Angular Material** | 21.2.8 | Yes (1st-party) | ~40 + CDK | M3 design tokens | 25.0k | Active, default choice |
| **PrimeNG** | 21.1.6 | Yes | 80+ | Token presets (Aura/Material/Lara/Nora) | 12.4k | Active, broad widgets |
| **Spartan UI** | **`0.0.1-alpha.678`** | Yes (signals/zoneless) | ~55 (helm) | Tailwind + CSS vars | 2.6k | **Pre-1.0 alpha** |
| **`@angular/aria`** | 21.x | Yes (1st-party) | 12 headless ARIA patterns | None — bring your own | (in angular/angular) | New, official |
| **NG-ZORRO** | 21.2.2 | Yes | 70+ (Ant Design) | Less + CSS vars | 9.1k | Active |
| **Taiga UI** | v5.x | Yes | Comprehensive | CSS vars | 4.0k | Active, fully signal-migrated |
| **Clarity (`@clr/angular`)** | 17.12.2 | **No (lagging)** | ~30 | CSS vars/Sass | 0.4k | Sleepy, Angular wrapper de-prioritised |
| **Nebular** | 17.0.0 | **No** | ~40 (Eva DS) | Sass themes | 8.1k | **Minimal maintenance, avoid for new** |
| **DaisyUI** | 5.5.19 | n/a (CSS only) | ~60 visual | CSS vars, 35+ themes | 40.8k | Very active |
| **Tailwind CSS** | v4.2 | Works (with caveats) | n/a | Utility-first | n/a | Active, friction with Sass |
| **Kendo / Syncfusion / DevExtreme** | aligned | Yes | 100–145+ | Vendor themes | commercial | **Commercial — out of budget** |

Key takeaways:

- **Material, PrimeNG, NG-ZORRO, Taiga UI** are all healthy, all support Angular 21, all signal-migrated to varying degrees.
- **Nebular** (last meaningful push Jan 2026, frozen at older Angular versions) and **Clarity** (still on `17.x` while Angular is at 21) are no-go for greenfield.
- **Material Design 3** is settled — the M2-vs-M3 transition is over. M3 tokens are the assumed baseline.
- **Spartan's npm version literally being `0.0.1-alpha.678` is the single most important factual data point.** It's been in alpha for ~3 years with daily nightly releases and no firm v1 date.

### Spartan UI: a closer look

Because we'd seriously considered this, it deserves a deeper look.

**The good:**

- ~55 components shipped (Accordion, Alert, Calendar, Combobox, **Data Table**, **Date Picker**, **Sidebar**, etc.) — much fuller than the "shadcn for Angular is missing stuff" narrative suggests.
- Architecture is well-conceived: `brain/*` (headless logic on Angular CDK) + `helm/*` (Tailwind-styled wrappers copied into your repo). Real shadcn-style ownership.
- Signals-based, SSR-compatible, zoneless-ready — exactly aligned with Angular 21.
- Sponsored by Zerops; Robin Götz (`@goetzrobin`) is the primary maintainer with a small core (~15 active contributors).
- Backed by Class Variance Authority for type-safe variants.
- **Escape velocity is reasonable** — because Helm code is copied into your repo, you own the styled components. You could freeze them and walk away; Brain (npm dep) is mostly a thin wrapper over Angular CDK that you could replace.

**The risks:**

- **Pre-1.0 alpha for ~3 years.** GitHub Releases page literally says "There aren't any releases here." Versions ship as nightly npm prereleases (`0.0.1-alpha.NNN`).
- **Weekly migration churn** reported by adopters. Issue [#1281](https://github.com/spartan-ng/spartan/issues/1281) describes button styles regressing on upgrade 614→655. Issue [#532](https://github.com/spartan-ng/spartan/issues/532) describes CLI version mismatches between `brain` packages.
- **Bus factor is real.** Robin Götz is the keystone; if he steps back, trajectory is uncertain. No corporate backstop like Google has for Material or PrimeTek for PrimeNG.
- **No high-profile production case studies.** The most cited ([eo-dna's piece](https://medium.com/eo-dna/why-we-chose-spartanng-over-angular-material-and-primeng-55042d06dd60)) explicitly notes they hadn't deployed yet at time of writing. The rest is internal-tools usage.
- **Tailwind v4 strongly recommended; v3 explicitly "not guaranteed."** Forces a Tailwind v4 setup, which has its own friction (see below).
- One report describes "running production instances of this alpha project and migrating to latest versions basically weekly."

**Verdict on Spartan:** The only credible shadcn-aesthetic option for Angular. Working choice if we accept owning copied components and tracking pre-release iteration. Not the right choice if API stability matters more than aesthetic.

### Tailwind in Angular — the v4 friction problem

Tailwind v4 (released Jan 2025, currently at 4.2) is a structural rewrite: CSS-first config via `@theme`, no JS config file, Rust-based engine. Adopting it in Angular today has known sharp edges that are directly relevant to us because **the cockpit is heavily SCSS-based**:

- **Sass + Tailwind v4 do not work together** per Tailwind's own docs. Migration writeups describe this as a hard incompatibility, not a tweak. ([migration journey](https://medium.com/@dylannnnlee/tailwindcss-v4-migration-guide-for-angular-nx-my-failing-journey-and-how-i-fixed-it-4bdab8bb3f34))
- **`@apply` is no longer global.** Each component SCSS that uses `@apply` needs `@reference "tailwindcss";` or fails. One developer reported `@reference` blowing builds up from 38s to 3m40s. ([discussion #17416](https://github.com/tailwindlabs/tailwindcss/discussions/17416))
- **Cascade-layer mismatch with Angular Material.** Material emits styles in `<head>` `<style>` blocks outside Tailwind's cascade layer, so utilities silently lose to Material styles. Community workaround is `!important`. ([jits.dev writeup](https://jits.dev/blog/tailwind-v4-play-nice-with-angular-material/))
- **Auto-content-detection** scans from workspace root — bloats production CSS in monorepos.

**Practical implication for us:** Adopting Tailwind v4 likely means migrating component styles off SCSS to plain CSS, or pinning Tailwind v3 (still defensible, well-supported). This is a real cost we'd pay before any framework benefit.

### Real-world migration patterns

What other Angular shops are picking, by size:

- **Solo / small projects:** Tailwind + DaisyUI, or Tailwind + Spartan/Radix-NG. The shadcn-style "copy components into your repo" workflow is the rising default for indie Angular devs.
- **Medium teams:** The modal choice is **Angular Material + Tailwind for utilities** ([example](https://dev.to/this-is-angular/my-favorite-angular-setup-in-2025-3mbo)). Material for components, Tailwind for "fine-grained customizations: positioning elements, tweaking margins, utility classes."
- **Enterprise:** Split between **PrimeNG** (data-heavy ops dashboards) and **Angular Material** (consistent UX, accessibility, government). Larger budgets pick paid suites (Kendo, Syncfusion).

Real first-person migration accounts surfaced:

- **Custom + patchwork Material → Spartan UI** ([Merlin Moos / shipping company, 8 internal apps](https://medium.com/eo-dna/why-we-chose-spartanng-over-angular-material-and-primeng-55042d06dd60)): "Maintaining consistency became painful. Each Angular upgrade required additional work." Found PrimeNG had "instability since Angular v17" and Material was "far too rigid." Picked Spartan for Brain/Helm decoupling.
- **Years of switching → settled on PrimeNG** ([Diggibyte](https://diggibyte.com/why-primeng-remains-my-go-to-ui-library-for-angular-19-in-2025/)): pivoted because of data-grid features (multi-column sort, inline edit, server-side pagination, CSV export). Downside flagged as larger API surface and bigger bundle.
- **Bootstrap → Angular Material** ([Coding Latte](https://codinglatte.com/posts/angular/what-i-learned-when-i-switched-to-angular-material/)): "Angular Material doesn't provide a responsive layout by default like Bootstrap." Had to keep Bootstrap loaded during transition.
- **Tried Tailwind, ejected** ([dev.to](https://dev.to/shaman-apprentice/my-journey-and-experienced-trade-off-with-tailwind-css-fdd)): readability of `[&>*]:col-start-2` syntax, ~10s build slowdown, hot-reload glitches, inconsistent naming (`content-center` vs `justify-center`). Concluded "more disadvantages than benefits." (Counterpoint: a much larger silent majority is happy with Tailwind.)

Most-cited regrets across all libraries:

1. **Starting with `::ng-deep` and `!important` to force Material customization.** Angular deprecated `::ng-deep`. Material's late CSS binding and high specificity makes overrides hellish.
2. **Not factoring Angular's release cadence into UI library choice.** PrimeNG 18 not supporting Angular 19 caused real upgrade pain. There's literally a Medium post titled *"Don't Upgrade to Angular 20 If You're Using PrimeNG."*
3. **Tailwind v4 migration mid-project** — see above.
4. **Heavy `@apply` usage in component styles** — even Tailwind's creator reportedly said he'd never have created `@apply` if starting over.

### Industry best practices (framework-independent)

Worth knowing regardless of which framework we pick:

**Design tokens have standardized.** The W3C Design Tokens Community Group (DTCG) reached its first stable spec in **October 2025** (Format Module 2025.10). JSON files (`.tokens.json`), every token has `$value` and `$type`, aliases via `{group.name}` syntax. Adopted by Figma, Style Dictionary, Tokens Studio, Sketch, Framer.

**Three-tier token architecture is the de facto standard:**

1. **Primitive tokens** — raw values (`color.blue.500: #2563eb`). Context-free, never used directly.
2. **Semantic tokens** — purpose-bound (`color.background.surface`, `color.text.danger`). Reference primitives. **This is where theming lives.**
3. **Component tokens** — component-scoped (`button.primary.bg.hover`). Optional, add only when needed.

Tools settled in 2026: **Style Dictionary** (build/transform, v4+ supports DTCG natively), **Tokens Studio for Figma** (Figma↔GitHub bridge). Output **CSS custom properties** as the runtime layer; JSON is the source.

**Theming pattern (2026 consensus):** CSS custom properties on `:root` + a `data-theme` attribute (or class) for explicit overrides + `prefers-color-scheme` for system following. `data-theme` wins over `.dark` because orthogonal modes can stack (`data-theme="dark" data-density="compact" data-brand="acme"`).

**Accessibility floor: WCAG 2.2 AA.** Became ISO/IEC 40500:2025 in October 2025; referenced by the European Accessibility Act (in force since June 2025). **WCAG 3.0** is in early Working Draft — final Recommendation realistically 2028–2030; don't plan around it today. Tooling: `axe-core` (~57% of WCAG issues automated), Lighthouse, Pa11y. Combined automation catches 30–40% of real issues — manual keyboard/screen-reader testing remains mandatory.

**Component API patterns now considered canonical:**

- **Compound components** (Radix-style) — `<Tabs><Tabs.List><Tabs.Trigger/></Tabs.List></Tabs>`
- **Slot-based composition / `asChild`** — `<Button asChild><Link>...</Link></Button>` merges Button's behavior onto Link. (Angular: content projection + structural directives fill this role.)
- **Variant systems** — **CVA (class-variance-authority)** is the de facto utility for type-safe variants; Spartan uses it.
- **Polymorphic components** (`as` prop) — powerful but type-tricky.

**Performance:** Tree-shake with `sideEffects: false` (avoid barrel files). Tailwind v4's Oxide engine emits only used utilities. CSS variables for tokens enable runtime theming with zero JS.

### Anti-patterns to avoid in 2026

**Universal:**

- **SCSS variables as the primary theming mechanism** — they compile away. Use CSS custom properties.
- **JS-driven theming via prop-drilling or context everywhere** — replaced by CSS variables flipped via `data-theme`.
- **Runtime CSS-in-JS in new code** — styled-components is in maintenance mode. Use Tailwind, CSS modules, or zero-runtime CSS-in-JS.
- **`!important` overrides in component consumers** — symptom of weak token discipline.
- **One component per design variant** ("CardLarge", "CardCompact") — variant explosion. Use CVA with compound variants.
- **Component sprawl** — multiple near-duplicates. Governance and a canonical-component registry are now table stakes.
- **Static `prefers-color-scheme` without manual override** — always provide a toggle plus `auto`.

**Angular-specific:**

- **`::ng-deep`** — deprecated, leaks styles. Replace with CSS variables or `:host`/`:host-context`.
- **`ViewEncapsulation.None` on shared components** — breaks isolation app-wide. Reserve for the global stylesheet host only.
- **`NgModule`-based component libraries** — Angular's 2025 strategy pushes **standalone everything**. New libraries should be standalone-first.
- **`input()` instead of `model()` in design system components** — `model()` lets consumers extend via directives, important for composability. ([justangular](https://justangular.com/blog/read-this-if-you-are-building-design-system-components-in-angular/))
- **Wrapping every Material component** — heavy, opinionated, hard to retheme.
- **Mixing `ChangeDetectionStrategy.Default` with signals** — works but loses signal-based CD wins.
- **Global styles in `styles.css` for component visuals** — keep global styles to tokens, resets, and typography base only.

### Contradictory signals (be honest about these)

- **PrimeNG**: Half the community calls it "the best UI lib for Angular," the other half cites "performance bottlenecks with large data grids and complex interfaces" and breaking changes between Angular majors.
- **Angular Material**: Half praise its accessibility and Google-team alignment; the other half complain it "looks generic" and that overrides require ng-deep gymnastics.
- **Tailwind**: Big silent majority is happy; minority who tried and ejected document real readability/build-time pain.
- **Spartan UI**: Both "the most mature shadcn-port for Angular" and "a bit abandoned" are simultaneously true depending on which week you check Discord.

---

## Part 3 — What this means for our specific situation

Connecting the local audit to the industry research:

1. **Mobile-first is production, desktop is half-built.** A face-lift is really "polish what works" + "decide if desktop is worth finishing." Doubling effort across two layouts is a real cost — worth deciding up front.

2. **Heavy component-scoped SCSS is a real Tailwind v4 friction point.** Sass + Tailwind v4 don't compose, `@apply` lost its global scope, and the cockpit's ~57 components each carry inline SCSS. Adopting Tailwind v4 means either migrating component styles off SCSS or pinning Tailwind v3.

3. **The single hardcoded Catppuccin theme via CSS variables is actually well-aligned with 2026 best practice** — CSS custom properties + (eventually) a `data-theme` toggle is exactly the recommended pattern. The base layer doesn't need to change much; it needs to be expanded into a proper semantic-token tier.

4. **Existing fragmentation (`.btn`, `.form-input`, `.badge` redefined per component) is exactly what a UI framework collapses.** Migration is mostly mechanical — replace inline `.btn` with framework button, replace hardcoded hex with token reference. High-leverage, low-semantic-risk cleanup.

5. **The Cytoscape graph, Prismjs syntax highlighting, marked, angular-split, and streaming UI are all framework-agnostic.** No framework is blocked by them.

6. **No paid frameworks and limited budget** — rules out Kendo, Syncfusion, DevExtreme, Ignite UI.

### Updated read on the realistic options

After the research, here's how the field actually looks:

| Option | Honest characterisation |
|---|---|
| **Angular Material + Tailwind utilities** | The de facto enterprise pattern. Boring, validated, safest. "Looks like Google" aesthetic; overrides are painful. Paying Tailwind-v4-friction tax to gain utilities. |
| **PrimeNG** | Worth picking *only* if we genuinely need its data-grid (we don't — we have ~57 components mostly forms, lists, chat, no million-row grids). Otherwise its Angular-version churn is a real liability. |
| **Spartan UI + Tailwind v4** | Closest aesthetic match for what Claude design generates. **But** alpha (`0.0.1-alpha.678`), weekly migration churn, single-maintainer-dominant, no major production case studies. Real risk of becoming an unmaintained dependency over a 5-year horizon. Mitigations: pin a tag, own the helm copies. |
| **`@angular/aria` + Tailwind + DaisyUI (or hand-rolled)** | Newest option. First-party headless primitives + utility CSS + optional DaisyUI for visual scaffolding. Most modern, most DIY, no library lock-in. We'd build our own primitives layer on top of `@angular/aria`. |
| **Taiga UI v5** | Underrated alternative. Comprehensive, fully signal-migrated, modular, modern. Smaller community than Material/PrimeNG but a genuinely serious option. |
| **Tailwind alone** | Maximum flexibility, but we're still designing everything. Ignores the fact that the user doesn't enjoy designing. |
| **DaisyUI on Tailwind** | Lightweight middle ground. Visual components only, no a11y guarantees. Reasonable as a *complement* to `@angular/aria`. |

### Revised initial read

The original "Tailwind + Spartan" recommendation needs an asterisk. Spartan is genuinely the closest aesthetic match for what we want, but its **`0.0.1-alpha.678` reality** plus **Tailwind v4 + SCSS friction** stacks two real risks on top of each other.

Three credible paths, ranked by my honest read:

1. **Angular Material + Tailwind utilities** — boring, safe, validated. Best if "fewer decisions" beats "match a specific aesthetic." The Tailwind+Material cascade-layer issue is annoying but solvable. Material is less rigid in M3 than in M2 — token-based theming actually works now.

2. **`@angular/aria` + Tailwind + (DaisyUI or handcrafted primitives)** — modern, DIY, owned. We bet on Angular team's first-party primitives + Tailwind for styling. Less "buy a kit," more "compose a system." Higher upfront cost; better long-term ownership; matches the Claude-design aesthetic if we generate components that way.

3. **Spartan UI + Tailwind** — closest to Claude-design aesthetic out of the box. Real adoption risk. Best if we want the shadcn vibe immediately and accept tracking alpha churn.

PrimeNG only if we discover a data-grid need we don't currently have. Taiga UI is a legitimate fourth option worth at least one prototype if we're feeling adventurous.

---

## Part 4 — Decision and target architecture

After Part 2, we re-examined the user's longest-running Angular project — `examples_ui/Advanced-LLM-Chat-develop` — and discovered that **the user already arrived at the right architecture organically**. The example codebase shows:

- Token-driven theming via CSS custom properties (`--theme-bg`, `--theme-surface`, `--theme-primary`, `--theme-text`, …) emitted under `.theme-light` / `.theme-dark` body classes
- Material in the codebase but used as a *behavior library only* — `mat-icon`, `mat-menu`, `mat-tooltip`, `mat-icon-button`, `mat-progress-bar`. Zero Material visual styling. Even the chat input is a raw `<textarea>` in a custom container with custom focus rings, custom shadows, custom recording-pulse animation.
- 100% custom component visuals using `var(--theme-*)` for every color, every shadow, every state.

That experience resolved the framework question. We are **not adopting any new component framework**. We will port the proven token system from the example, modernize two specific weaknesses, and add the single layer the example was missing.

### Why this beats every framework option

1. **It's already proven on a project we shipped.** The example codebase has lived through real iteration and the user is happy with it. None of the framework options offers comparable confidence.
2. **It matches how the user actually builds UI.** Every framework attempt in the example was eventually replaced. Imposed aesthetics get overridden; tokens and behavior primitives don't.
3. **AI-assisted coding has shifted the math.** The historical cost of custom was accessibility — keyboard handling, ARIA, focus management, screen-reader semantics. With Claude Code, a primitive button with full a11y is a 30-minute task, not a week. Owning ~10 primitives is no longer a quarter-long project.
4. **Zero alpha churn, zero migration timer.** No `0.0.1-alpha.678`, no "Don't Upgrade to Angular X If You're Using Y" article ever applies to us.
5. **The Tailwind v4 + SCSS friction tax disappears.** SCSS stays. We already know how to build with tokens. No `@reference "tailwindcss"` in every component, no cascade-layer wrestling.
6. **Escape velocity is total.** Nothing to migrate off when fashions change.

### Target architecture — three layers

```
┌──────────────────────────────────────────────────────┐
│  Layer 3 — Feature components                        │
│  pages/*, simple/pages/*, shared/components/*        │
│  Compose primitives. No raw .btn/.form-input/.badge. │
└──────────────────────────────────────────────────────┘
                       ▲ uses
┌──────────────────────────────────────────────────────┐
│  Layer 2 — Primitive components       (NEW)          │
│  src/app/ui/<button|input|select|dialog|...>         │
│  Standalone Angular, signal-based, OnPush.           │
│  Behavior from @angular/cdk + a few Material atoms.  │
│  Visuals from layer-1 tokens only.                   │
└──────────────────────────────────────────────────────┘
                       ▲ uses
┌──────────────────────────────────────────────────────┐
│  Layer 1 — Tokens                     (PORTED)       │
│  src/styles/_variables.scss                          │
│  src/styles/themes/_theme-config.scss                │
│  src/styles/themes/_themes.scss                      │
│  Emits CSS custom properties under .theme-light/dark │
└──────────────────────────────────────────────────────┘
```

### Layer 1 — Tokens (ported from the example)

Direct port of the example's structure, with Catppuccin Mocha values for dark and Catppuccin Latte for light.

```
cockpit/src/styles/
├── _variables.scss              -- spacing, radii, shadows, breakpoints, font sizes
├── _mixins.scss                 -- shared mixins (media queries, focus-ring, etc.)
├── styles.scss                  -- global resets, theme class application, base typography
└── themes/
    ├── _theme-config.scss       -- light + dark theme maps (Sass map of tokens)
    ├── _themes.scss             -- apply-app-theme mixin (emits CSS custom properties)
    └── _typography.scss         -- type scale, font stack
```

**Two-tier token model to start:**

- **Tier 1 — primitive Sass variables** in `_variables.scss` for theme-invariant values: spacing scale (`$space-xs`–`$space-xl`), radii (`$radius-sm/md/lg`), shadows, breakpoints, font sizes. Compiled in at build time.
- **Tier 2 — semantic CSS custom properties** emitted per theme: `--theme-bg`, `--theme-surface`, `--theme-text`, `--theme-text-secondary`, `--theme-primary`, `--theme-border`, `--theme-hover`, `--theme-active`, `--theme-shadow-{sm,md,glow}`, etc. Runtime-swappable.

Component-level tokens (a third tier) deferred — only add when a component genuinely needs scoped overrides.

**Theme switching:** body class swap (`document.body.classList.toggle('theme-dark')`). Persisted in localStorage. `prefers-color-scheme` consulted on first visit. Body-class > `data-theme` attribute for our scope (orthogonal modes like density/brand are not on the roadmap).

**Modernization vs the example:**

- **Drop `mat.m2-define-palette()` and `mat.all-component-colors()`** from `apply-app-theme`. M2 is deprecated, and we don't actually consume the colors Material derives from it — our visuals come straight from `--theme-*`. The mixin in Cockpit emits CSS custom properties only; no Material theme pipeline.
- **Add a paired Catppuccin Latte light theme map** alongside Mocha so dark/light switching works from day one. Current Cockpit hardcodes Mocha only.

### Layer 2 — Primitive components (the missing layer)

The one thing the example lacked. The example's `chat-ui-inputfield.component.scss` is 632 lines because there was no shared `<app-icon-button>` / `<app-file-chip>` to compose. We fix that here.

Initial primitive set (~10–14 components, build over Phase 2):

```
cockpit/src/app/ui/
├── button/                  -- variants: primary | secondary | ghost | danger; sizes: sm | md | lg
├── icon-button/             -- icon-only button with a11y label
├── input/                   -- text, password, number; with label, error, hint slots
├── textarea/                -- auto-grow
├── select/                  -- native or CDK-overlay-based
├── checkbox/
├── radio-group/
├── switch/                  -- toggle
├── dialog/                  -- CDK trap focus + overlay
├── menu/                    -- CDK overlay + ListKeyManager
├── tooltip/                 -- CDK overlay (or wrap mat-tooltip if cheaper)
├── tabs/                    -- ListKeyManager-driven
├── card/
├── badge/                   -- status colors via tokens
├── chip/                    -- removable, clickable variants
├── toast/                   -- CDK overlay + aria-live region
├── spinner/
├── icon/                    -- thin wrapper over mat-icon initially
└── form-field/              -- label + control + error/hint layout
```

**Per-primitive shape:**

- Standalone Angular component (no NgModule).
- `ChangeDetectionStrategy.OnPush`.
- Inputs as `input()`, two-way bindable values as `model()` (per the [justangular guidance](https://justangular.com/blog/read-this-if-you-are-building-design-system-components-in-angular/)).
- No `ViewEncapsulation.None`. Tokens applied via `:host`.
- No `::ng-deep`. No `!important`.
- All visual properties via `var(--theme-*)` — never hardcoded hex/rgb/px-color.
- Variants via `[data-variant]` / `[data-size]` host attributes + Sass selectors. No CVA dependency.
- Accessibility built in: focus-visible ring from CDK `FocusMonitor`, ARIA roles/states, keyboard handling, disabled/loading states, screen-reader labels.

**Behavior dependencies:**

- `@angular/cdk` — overlays, focus traps, focus monitor, list-key navigation, live regions, drag-drop, scroll strategies. Stable, official, free.
- A handful of Material atoms only where rebuilding on CDK has poor ROI — `mat-icon` (until we replace), and pragmatically `mat-tooltip` / `mat-menu` if their CDK reimplementations become a time sink. Used as behavior carriers only; no Material visual classes leak through.
- Zero other UI dependencies.

### Layer 3 — Feature components (existing, refactored)

The 56+ existing feature components stop redefining `.btn` / `.form-input` / `.badge` / `.card` inline. They compose primitives.

Before:

```html
<button class="btn btn-primary" (click)="submit()">Save</button>
<input class="form-input" [(ngModel)]="name" />
<span class="badge badge-success">Active</span>
```

After:

```html
<app-button variant="primary" (clicked)="submit()">Save</app-button>
<app-input [(value)]="name" label="Name" />
<app-badge tone="success">Active</app-badge>
```

Per-feature SCSS shrinks to layout-only (flex/grid arrangement, page-specific spacing). Inline `.btn` / `.form-input` / `.badge` definitions across `pages/`, `simple/pages/`, and `shared/components/` get deleted as they're superseded.

### What we are explicitly NOT adopting

- **Angular Material as a component library.** Used only for `mat-icon` (and possibly `mat-tooltip` / `mat-menu` as pragmatic behavior carriers). No `mat-button`, no `mat-form-field`, no `mat-card` styled as Material.
- **PrimeNG / NG-ZORRO / Taiga UI / Nebular / Clarity.** Reasoned through in Part 2 — all impose aesthetics or are stale.
- **Spartan UI.** Alpha churn is real and unnecessary; we can copy the architectural ideas without taking the dependency.
- **DaisyUI.** Imposes Tailwind themes and visual scaffolding we don't want.
- **Tailwind CSS.** SCSS + tokens + per-component scoped styles is the user's preferred ergonomics. Revisit only if utility-style layouts become real friction.
- **CVA / class-variance-authority.** Native `model()` + attribute selectors handle variants without a JS dependency.
- **`::ng-deep`, `ViewEncapsulation.None` on shared components, `!important` overrides, NgModule libs, `input()` where `model()` fits.** Standard 2026 anti-patterns from Part 2 — applied as guardrails.

### Migration plan

**Phase 0 — port the token layer (~1 day):** ✅ **Complete**

1. ✅ Copied `_variables.scss`, `_mixins.scss`, `themes/_theme-config.scss`, `themes/_themes.scss`, `themes/_typography.scss` from `examples_ui/Advanced-LLM-Chat-develop/src/styles/` into `cockpit/src/styles/`.
2. ✅ Theme-config carries Catppuccin Mocha (current production look) and a paired Catppuccin Latte light variant.
3. ✅ Stripped all `mat.m2-*` calls. `apply-app-theme` emits CSS custom properties only.
4. ✅ `cockpit/src/styles.scss` applies the theme class on `<body>` (`class="theme-dark"` in `index.html`). Global resets, `:focus-visible`, 44 px touch targets, `prefers-reduced-motion` preserved.
5. ✅ No visible regression — same `--theme-*` names used.

**Phase 1 — first primitive end-to-end (~1 day):** ✅ **Complete**

1. ✅ Built `<app-button>` (variants `primary | secondary | ghost | danger | success | warning | info`, sizes `sm | md | lg`, disabled, loading spinner, full-width, ARIA, CDK `FocusMonitor` for keyboard-only focus rings).
2. ✅ Adopted on `simple/pages/sessions`.
3. ✅ Iterated on API — settled on `(clicked)` output (not `click`), `[data-variant]`/`[data-size]` selectors, alias-and-getter pattern for `disabled` clashes with CDK interfaces.

**Phase 2 — primitives sweep (~3–5 days):** ✅ **Complete** (20 of 20 + 1 Phase-4 addition)

Initial set (status as of 2026-04-26):

| # | Primitive | Status | Notes |
|---|---|---|---|
| 1 | `button/` | ✅ | 7 variants incl. semantic (success/warning/info), 3 sizes, loading state |
| 2 | `icon-button/` | ✅ | 3 variants, 3 sizes, tooltip + ariaLabel inputs |
| 3 | `input/` | ✅ | `model<string>` two-way binding, 7 input types, sm/md/lg, invalid state |
| 4 | `textarea/` | ✅ | `model<string>`, rows + resize controls, invalid state |
| 5 | `select/` | ✅ | Wraps native `<select>` (preserves `<optgroup>` via projection), `model<T>` value sync via effect |
| 6 | `checkbox/` | ✅ | `model<boolean>` checked, hidden native input, custom box, label slot |
| 7 | `radio-group/` (radio-group + radio) | ✅ | Two-component pattern (parent + items) like `tab-nav`. `value=model<T \| null>`, horizontal/vertical orientation, optional group-wide `[disabled]`. `FocusKeyManager` orientation-aware arrow-key navigation, roving tabindex (active radio is tabbable; first non-disabled when nothing selected). `role="radiogroup"`/`role="radio"` + `aria-checked`/`aria-disabled`/`aria-orientation`. Custom circle + dot visual scaling on selection (respects `prefers-reduced-motion`). Space/Enter selects. |
| 8 | `switch/` (toggle) | ✅ | `model<boolean>` checked, hidden native input (`role="switch"` + `aria-checked` for SR), pill-shaped track with sliding thumb, sm/md sizes, label slot via projection, `prefers-reduced-motion` honoured. |
| 9 | `dialog/` | ✅ | Inline element pattern (matches existing 4+ sites in codebase, drop-in replacement). `[(open)]` two-way, sm/md/lg/xl sizes, title input + `[appDialogActions]` slot, `cdkTrapFocus` + `cdkTrapFocusAutoCapture`, ESC + backdrop close (configurable), body scroll lock, focus restore on close, ARIA dialog/aria-modal |
| 10 | `menu/` (menu + menu-item + menu-trigger directive) | ✅ | Hand-rolled overlay (matches `tooltip` pattern, no CDK overlay). `<app-menu>` invisible host (`display: contents`); panel rendered to `document.body` on open, positioned via `getBoundingClientRect()` + `position: fixed`. `[appMenuTrigger]="menuRef"` directive on the trigger element wires click+ArrowDown to `open()`/`toggle()`, sets `aria-haspopup="menu"` + `aria-expanded`. Placements `bottom-start`/`bottom-end`/`top-start`/`top-end`, viewport-clamped. `FocusKeyManager` (vertical, wrap, type-ahead) for arrow-key navigation. `<app-menu-item>` is `FocusableOption` with `role="menuitem"`, `[tone]` default/danger, `[disabled]` blocks click, Enter/Space activates. Click-outside + Escape close, focus restored to trigger on close. |
| 11 | `tooltip/` | ✅ | `[appTooltip]` directive (not component — keeps consumer ergonomics terse). Renders to `document.body`, positioned via `getBoundingClientRect()` + `position: fixed`. Placements top/bottom/left/right (default top), default 500ms delay, viewport-clamped. Shows on mouseenter/focus, hides on mouseleave/blur/click/escape/scroll (capture-phase). Sets `aria-describedby` + `role="tooltip"`. Global `.app-tooltip` styles in `styles.scss` (themed via `var(--surface-2)` + `var(--shadow-md)`). `<app-icon-button>` rewired to use it — every existing `[tooltip]` input now gets a styled tooltip for free. |
| 12 | `tabs/` (tab-bar + tab) | ✅ | `FocusKeyManager` roving tabindex, generic `<T>` value, content projection |
| 13 | `card/` | ✅ | Layout/surface primitive: `[variant]` (surface/panel/outlined/ghost) × `[padding]` (none/sm/md/lg). Optional `[interactive]` upgrades to `role="button"` + tabindex 0 + keyboard activation (Enter/Space) with `(activated)` output and `[selected]` (`aria-pressed`). `[disabled]` blocks interaction. Hover/focus-visible accent ring driven by `var(--accent-color)`. Drop-in for the recurring `padding + 1px border + radius-md + var(--surface-0)/var(--panel-bg) + transition` shape across `session-card`/`expert-card`/`stat-card`/etc. |
| 14 | `badge/` | ✅ | Tones (neutral/accent/success/warning/**alert**/info/danger) × appearance (subtle/solid), xs/sm/md sizes, rounded/pill shapes, optional uppercase modifier |
| 15 | `chip/` | ✅ | `default | accent | danger` variants, `selectable` + `selected` inputs, dual outputs (`clicked` + `toggled`) |
| 16 | `toast/` (service + container) | ✅ | Service-driven: `AppToastService` (`providedIn: 'root'`) holds a `signal<ToastEntry[]>`. `show(message, {tone, duration, dismissible})` returns dismissible id; tone helpers `info/success/warning/danger`. Default duration 4000ms (6000ms for `danger`); `duration: 0` disables auto-dismiss. `<app-toast-container>` renders the stack — fixed bottom-right (full-width below `sm` breakpoint). Toasts get `role="status"` + `aria-live="polite"` (or `role="alert"` + `assertive` for danger). Tone-tinted bg + colored border via `--success/-tint`, `--warning/-tint`, `--danger/-tint`, `--info/-tint`. Optional `×` close button. Slide-up entrance animation, `prefers-reduced-motion` honoured. Legacy `core/components/toast/` + `core/services/toast.service.ts` deleted; `app.ts` + 5 consumers (`api.service`, `simple/pages/chat`, `sessions`, `session-create` (dead inject removed), spec) retargeted at the new service (`error()` → `danger()`). |
| 17 | `spinner/` | ✅ | xs/sm/md/lg sizes, tones (inherit/accent/muted), CSS-only spin ring with `prefers-reduced-motion` honoured, `role="status"` + optional `aria-label` |
| 18 | `icon/` | ✅ | Thin wrapper over Material Symbols Outlined font. Content-projected glyph name, sizes xs/sm/md/lg/xl/inherit, optional `[filled]` (variable-font FILL axis), defaults to `aria-hidden="true"` unless `[ariaLabel]` is set (then `role="img"`) |
| 19 | `form-field/` | ✅ | Layout primitive: `[label]` + `[required]` (asterisk) + `[optional]` (inline parenthetical hint) + projected control + `[hint]` (muted helper text) or `[error]` (danger-tinted, `role="alert"`). Vertical default + horizontal grid orientation. Auto-IDs hint/error elements (consumer can wire `aria-describedby`). Drop-in for the existing `.form-group` + `.form-label` + `.required` + `.form-hint`/`.hint-inline` SCSS pattern across ~16 consumers. |
| 20 | `tab-nav/` (tab-nav + tab-nav-item) | ✅ | Navigation-style tabs distinct from `tab-bar` (rounded pills). Vertical/horizontal orientation, flat with edge-anchored active indicator (left-border for vertical, bottom-border for horizontal). `FocusKeyManager` orientation-aware navigation, generic `<T>` value, `FocusableOption` contract. |
| 21 | `theme-toggle/` | ✅ | Phase 4 addition. Segmented control (light / system / dark) bound to `ThemeService`. `role="radiogroup"` + per-option `role="radio"` + `aria-checked`/`[data-active]`. Material Symbols icons (`light_mode` / `contrast` / `dark_mode`); optional `[showLabels]` for full button labels (used in Settings). Tooltips per option. |

Process:

1. Each primitive standalone Angular, OnPush, signal-based, FocusMonitor for keyboard focus.
2. Tested in isolation when behavior warrants (vitest + jsdom).
3. API documented inline; no separate docs site.

**Phase 3 — feature migration (rolling):** ✅ **Active queue complete** (all interactive `simple/pages/*` + 11 of ~13 `shared/components/*`; the remaining 3 components are explicitly deferred or skipped — see the screen table)

**Running tally** (as of 2026-04-26):

- **Simple pages migrated:** sessions, session-create, sudo, inbox (header + badges + dialogs + body). Deferred: shell (custom terminal-like surface). The remaining `simple/pages/{chat,create,datasources,jobs}` are wrapper shells over `shared/components/*`.
- **Shared components migrated:** agent-list, job-list, datasource-list, job-create, job-review, config-editor, todo-list, workspace-browser, chat-history, persistent-chat, instruction-builder, agent-settings.
- **Shared components remaining:** notification-bell + empty-catalog-banner (skipped — tightly-coupled custom widgets). **Active queue is empty.**
- **Inline SCSS deleted (component-by-component, where reported):** inbox-dialogs ~95, inbox-body ~125, agent-list ~200, job-list ~280, datasource-list ~370, job-create ~210, job-review ~165, config-editor ~140, todo-list ~55, workspace-browser ~15, chat-history ~50, persistent-chat ~140, instruction-builder ~90, agent-settings ~95, legacy-toast ~75 — **~2,105 lines** of inline SCSS removed across these migrations alone (older sessions/session-create/sudo/inbox-header/inbox-badges entries had additional removals not enumerated as line counts).
- **`FormsModule` removed from:** inbox, session-create, sudo, datasource-list, job-create, job-review, config-editor (replaced with `[value]/(valueChange)` for textareas/inputs and `[value]/(changed)` for selects).
- **Helpers introduced:** tone-mapping methods (`agentStatusTone`, `jobStatusTone`, `dsTypeTone`, `sudoStatusTone`, `freezeTone`, `ruleActionTone`); typed select-event bridges (`onProjectIdChange`, `onPriorityChange`, `onCloudStorageChange`, `onTypeSelect`, `onGitAuthMethodChange`, `onBooleanValueChange`, `asInputValue`).
- **Toast service consolidated (2026-04-26):** legacy `core/services/toast.service.ts` + `core/components/toast/toast.component.ts` (and the now-empty `core/components/` dir) deleted; `app.ts` + 5 call sites (`api.service` 18 calls, `chat-page` 1 call, `sessions-page` 3 calls + spec mock, `session-create` dead inject removed) retargeted at `AppToastService` (`ui/toast`). API rename: `error()` → `danger()` (10 sites in `api.service`, 4 sites across simple pages). Single canonical toast surface across the app.

Status by screen:

| Screen | Status | Notes |
|---|---|---|
| `simple/pages/sessions` | ✅ Fully primitive-driven | `<app-button>`, `<app-icon-button>`, `<app-tab-bar>`, `<app-input>`, `<app-select>`, `<app-chip>`. Inline SCSS down to layout-only (cards, banner, status dots). |
| `simple/pages/inbox` (header) | ✅ | Filter chips, back-btn, refresh icon-btn migrated. |
| `simple/pages/inbox` (badges) | ✅ | 9 badge sites migrated (mode/resolved/status × 2/rule-action/freeze/risk + helpers `sudoStatusTone()`, `freezeTone()`, `riskTone()` mapping low→success / medium→warning / high→alert / critical→danger). Only `detail-type-badge` (heading + colored icon) still custom — its inline-icon-with-text composite isn't a clean fit. |
| `simple/pages/inbox` (dialogs) | ✅ | Deny dialog and shortcuts dialog migrated to `<app-dialog>` + `<app-textarea>` + `<app-button>`. Removed inline overlay/dialog SCSS (~95 lines). |
| `simple/pages/inbox` (body) | ✅ | Sudo action bars (success/danger/warning/primary buttons), rule form (input + select + button) + rule-row delete (icon-button danger), reply form (textarea + checkbox + info send button), review action bars (3 conditional groups with textareas + tone-mapped buttons), session detail button. ViewChild for replyInput repointed from `ElementRef<HTMLTextAreaElement>` to `AppTextareaComponent`. FormsModule import removed. ~125 lines of inline `.btn`/`.input`/`.select`/`.urgent-check`/`.icon-btn-sm`/`.reply-input`/`.review-notes` SCSS deleted; replaced with a single `app-button kbd { … }` rule for shortcut hints. |
| `simple/pages/session-create` | ✅ | Cancel header button, title input, project chips, footer buttons (cancel + primary with loading state). Expert-card grid kept custom (rich card pattern not yet primitivized). FormsModule import removed. |
| `simple/pages/sudo` | ✅ | Filter select, refresh icon-button, pending count badge, status badge (sudoStatusTone helper), risk badge (`riskTone()` helper, 4-level scale low→success / medium→warning / high→alert / critical→danger), approve/deny buttons, rule form (input + select + add button), rule-action badge (ruleActionTone helper), delete icon-button (danger), deny dialog (`<app-dialog>` + textarea + buttons). FormsModule import removed. |
| `simple/pages/chat`, `create`, `datasources`, `jobs` | n/a | Wrapper shells around `shared/components/*`; no interactive elements of their own. |
| `simple/pages/shell` | ⬜ | Custom terminal-like surface (session-title-btn, session-option, model-title-btn dropdowns) — primitive fit unclear; deferred. |
| `pages/*` (desktop) | ⬜ | Desktop layout still half-built per Part 1; migration scope TBD per "Open decisions still owed" |
| `shared/components/agent-list` | ✅ | Refresh button, toggle button, dialog footer cancel/confirm → `<app-button>` (with `[loading]` for assign). Action buttons (assign/remove) → success/danger variants. 8-state status badge → `<app-badge [tone]="agentStatusTone()">` (helper maps ready→success, working/draining→warning, booting/session→info, completed→success, failed→danger, offline→neutral). Assignment dialog overlay (absolute-positioned in panel) → `<app-dialog>` (now fixed-to-viewport with focus trap + ESC + body scroll lock). Job-option list kept custom (clickable cards with composite content). ~200 lines of inline `.refresh-btn`/`.dialog-*`/`.btn-*`/`.status-badge.status-*`/`.action-btn.*`/`.toggle-btn` SCSS deleted. |
| `shared/components/job-list` | ✅ | Filter chips → `<app-chip>` (count inline, `[selected]` from `activeFilter()`). Refresh → `<app-button variant="secondary" size="sm">`. Status badge (9 statuses) → `<app-badge [tone]="jobStatusTone()" size="sm">` with helper mapping completed→success, processing/pending_review→warning, failed→danger, created/waiting→info, reviewing→accent, cancelled/paused→neutral. All row action buttons → `<app-button>` variants: view/ide/promote→info, workspace/pause→secondary, cancel→warning, confirming-cancel/delete→danger, resume→success, review→warning. `[loading]` replaces `.btn-spinner` text-swap for canceling/ide-starting. Promote-form inputs → `<app-input size="sm">` with `(valueChange)`. Removed: `asInputValue` helper, ~280 lines of inline `.filter-chip*`/`.refresh-btn`/`.status-badge.status-*` (9 variants)/`.action-btn.*` (12 variants)/`.btn-spinner`/`@keyframes pulse-confirm`/`.promote-input` plus matching mobile-media overrides. Kept custom: `.config-badge`, `.delegation-badge`, `.snapshot-badge`, `.expand-btn`, `.user-dot`. |
| `shared/components/datasource-list` | ✅ | Filter chips → `<app-chip>`. Header "new" button → `<app-button variant="success" size="sm">`; refresh → `<app-icon-button>`. Message dismiss buttons → `<app-button variant="ghost" size="sm">`. Form panel: close-btn → `<app-icon-button>`; all 8 form inputs (name, connection_url, cli_hint, default_branch, env-key, env-value, username, password/token) → `<app-input>`; description + ssh-key → `<app-textarea>`; type + auth-method selects → `<app-select>` (preserves `<optgroup>` projection). Env-row delete → `<app-icon-button variant="danger">`; add-env → `<app-button variant="ghost">`. Form footer test/cancel/save → `<app-button>` (secondary/secondary/primary) with `[loading]` replacing spinner-small text-swap. Type badge (6 type variants) → `<app-badge [tone]="dsTypeTone()">` with helper mapping generic→accent, repository/postgresql→info, neo4j→success, webdav→warning, mongodb→neutral. Scope badge → `<app-badge [tone]="ds.job_id ? 'neutral' : 'accent'" size="xs">`. Row icon-buttons (test/edit/delete) → `<app-icon-button>` (ghost/ghost/danger), test uses `[loading]` instead of `.spinner-tiny`. FormsModule + `[(ngModel)]` removed in favor of `[value]/(valueChange)` plain-object binding (and `(changed)` handlers `onTypeSelect`/`onGitAuthMethodChange` for selects to satisfy strict template type-check on `T \| null` outputs). Removed: ~370 lines of `.filter-chip*`/`.action-btn`/`.new-btn`/`.dismiss-btn`/`.close-btn`/`.form-input`/`.form-textarea`/`.form-select`/`.btn-*`/`.btn-add-env`/`.spinner-small`/`.spinner-tiny`/`.type-badge.type-*` (6 variants)/`.scope-badge.*`/`.ro-badge.*`/`.icon-btn.*` SCSS. Kept custom: `.test-result` ok/error tinting, `.inline-test`, `.form-hint`, `.center-state` loading + empty, `.ds-table` layout. |
| `shared/components/job-create` | ✅ | Success/error dismiss buttons → `<app-button variant="ghost" size="sm">`. Project / priority / cloud-storage selects → `<app-select>` (with `(changed)` handlers `onProjectIdChange`/`onPriorityChange`/`onCloudStorageChange` to bridge `T \| null` outputs to typed signals — priority parses string→number, cloud-storage narrows to literal union, project maps `''`→`null`). Description + kickoff textareas → `<app-textarea>` with `[value]/(valueChange)`. File-row remove × → `<app-icon-button variant="danger" size="sm">`. Add-more-files → `<app-button variant="ghost" size="sm" [fullWidth]="true">`. Form footer reset/submit → `<app-button>` (secondary/primary) with `[loading]="isSubmitting() \|\| isUploading()"` replacing the `.spinner-small` text-swap. `<form (ngSubmit)>` → `<form (submit)="$event.preventDefault(); onSubmit()">` since FormsModule is removed. Removed: ~210 lines of `.dismiss-btn`/`.form-input`/`.form-textarea`/`.btn-*`/`.spinner-small`/`.remove-btn`/`.add-more-btn`/responsive `.btn` overrides SCSS. Kept custom: expert-card grid (rich card with icon+name+desc+tags, no primitive fit), file dropzone (drag-drop UI), file-list rows, all `.preset-chip`/`.tool-toggle`/`.ds-option`/etc. styles still defined for the projected `<app-agent-settings>` child layouts. AppInputComponent imported then dropped — no `<input>` elements survived in this template. |
| `shared/components/job-review` | ✅ | Refresh ↻ → `<app-icon-button variant="ghost" size="sm">`. Status badge (6 statuses) → `<app-badge [tone]="jobStatusTone()">` with helper (same mapping as job-list). Workspace browse + IDE-open → `<app-button>` (secondary/info), IDE button uses `[loading]` instead of `.ide-spinner` text-swap. VM-upgrade group: `upgradeToVm` → `<app-button variant="warning">`, `resumeWithoutVm` → `<app-button variant="secondary">`, both with `[loading]`. Phase-boundary continue → `<app-button variant="warning">`. Approve flow folded into one `<app-button variant="success">` whose `(clicked)` toggles between `confirmApprove` and `approveJob` based on `confirmingApprove()` (replaces 2-button if/else with `.confirming` pulse animation). Feedback textarea → `<app-textarea [(value)]="feedbackText" [rows]="4">`; continue-with-feedback → `<app-button variant="warning" [loading]="isResuming()">`. FormsModule + `[(ngModel)]` removed. Removed: ~165 lines of `.refresh-btn`/`.status-badge.status-*` (6 variants)/`.workspace-link*`/`.ide-spinner`/`@keyframes spin` (dup)/`.btn`/`.approve-btn*`/`@keyframes pulse-confirm`/`.continue-btn`/`.feedback-input`/`.upgrade-btn` SCSS. Kept custom: `.confidence-bar` + `.confidence-fill.low/.medium/.high` (visual progress indicator, no primitive fit), `.divider` text-with-line, `.deliverables-list`, `.notes-text`, `.upgrade-info`/`.upgrade-command`/`.upgrade-hint`, `.result-message` ok/error tints. |
| `shared/components/config-editor` | ✅ | Mode toggle (visual/json segmented buttons) → 2× `<app-chip [selected]>`. Section override-count chip → `<app-badge tone="accent" appearance="solid" size="xs" shape="pill">`. Per-field nullable badge → `<app-badge tone="neutral" size="xs">`. Schema-driven field controls: enum select → `<app-select>` (preserves `__null__` sentinel option for "auto-detect" / default), plain string → `<app-input type="text">`, plain number/integer → `<app-input type="number">`, array of strings → `<app-input type="text">` (comma-split), boolean → `<app-checkbox>` with `(changed)` calling new `onBooleanValueChange()` helper (boolean-typed sibling of the original event-based `onBooleanChange`). Slider clear × + checkbox clear × → `<app-icon-button variant="danger" size="sm">`. JSON editor textarea → `<app-textarea [value]="jsonText()" (valueChange)="onJsonEdit($event)" [rows]="20">` with `class="json-editor"` for the JetBrains-Mono font override. Native `<input type="range">` slider kept (no slider primitive yet); `[ngModel]` on it replaced with `[value]/(input)="setSmartOverride(node.path, +asInputValue($event), …)"` via new `asInputValue()` helper. FormsModule + `[ngModel]/(ngModelChange)` removed. `$any()` casts on `[value]` bindings since `getDisplayValue()` returns `unknown` and the primitives are typed `string`/`boolean`. Removed: ~140 lines of inline `.mode-toggle button*`/`.section-badge`/`.nullable-badge`/`.form-input`/`select.form-input`/`input[type=number].form-input`/`.clear-btn*`/`.toggle-field input`/`.json-editor` SCSS. Kept custom: `.section-header` / `.subsection-header` (full-width composite chevron-toggle buttons — no primitive fit yet), `.config-section`/`.section-body`, `.modified-dot`, `.slider-row` + `.form-range` accent-color tweak, `.json-error` notice, `.field-desc`. |
| `shared/components/todo-list` | ✅ | Phase selector → `<app-select size="sm">` (preserves `<optgroup>` for archived phases, value handler `onPhaseChange()` maps `'current'`/null→`showCurrent()`, archive filename→`selectArchive()`). Refresh ↻ → `<app-icon-button variant="ghost" size="sm">`. Error retry → `<app-button variant="danger" size="sm">`. High-priority badge → `<app-badge tone="danger" appearance="solid" size="xs" [uppercase]="true">`. Removed: ~55 lines of `.phase-selector*`/`.refresh-btn*`/`.error-state button`/`.priority-badge` SCSS. Kept custom: `.progress-bar` + `.progress-fill` linear-gradient (no progress primitive yet), `.todo-item` border-left-tinted rows (4 status variants), `.todo-status` emoji indicator, `.todo-notes` border-left list, `.summary-bar` with `.summary-item.completed/.pending` text tints, `.failure-note` warning notice, `.loading-overlay` semi-transparent spinner, `.empty-state` empty markers. |
| `shared/components/workspace-browser` | ✅ | Refresh ↻ → `<app-icon-button variant="ghost" size="sm">`. Removed: ~15 lines of `.refresh-btn*` SCSS. Kept custom: `.crumb` breadcrumb buttons (specialized inline path-segment style — using `<app-button>` here would lose the compact text-only look), `.file-entry` rows (composite icon + name + size layout, click-to-navigate), `.code-block` `<pre>` content view, `.spinner-sm`, empty/loading/no-file states. Lightest migration in this sweep — most surface in this component is specialized navigation/content, not generic interactive primitives. |
| `shared/components/chat-history` | ✅ | Refresh ↻ → `<app-icon-button variant="ghost" size="sm">`. Error retry → `<app-button variant="danger" size="sm">`. Phase badge (strategic/tactical/unknown) → `<app-badge [tone]="phaseTone()" size="xs" [uppercase]="true">` with helper mapping strategic→accent, tactical→success, unknown→neutral. Shell-pane tab-type badge (shell/claude-code/ssh) → `<app-badge [tone]="paneTypeTone()" size="xs" [uppercase]="true">` (shell→success, claude-code→accent, ssh→info). New-output flag → `<app-badge tone="warning" appearance="solid" size="xs" [uppercase]="true">`. Removed: ~50 lines of `.refresh-btn*`/`.error-state button*`/`.phase-badge.strategic/.tactical`/`.tab-type-badge[data-type=…]` (3 variants)/`.new-output-badge` SCSS. Kept custom: `.shell-tab` segmented tab with active border-bottom (specialized terminal-tab UI), `.tool-call-header`/`.reasoning-header`/`.shell-state-header` (`<summary>` elements inside `<details>`, can't replace with primitives), `.input-message`/`.response-message` border-tinted message bubbles, `.terminal-content` `<pre>` rendering, `.idle-badge` (italic muted annotation), `.request-link` underlined-on-hover text link. |
| `shared/components/persistent-chat` | ✅ | Disconnect button → `<app-button variant="ghost" size="sm">`. Status-bar chips (model/temperature/turn/permissionMode) → 4× `<app-badge size="sm">` (model + mode → accent; temperature + turn → neutral). Settings-panel selects (mode / narration / model) → `<app-select size="sm" [fullWidth]="false">` with new typed handlers `onPermissionModeChange()` / `onNarrationModeSelect()` / `onModelSelect()` (replacing the old event-based `onModeChange` / `onNarrationModeChange` / `onModelChange`). Model select preserves `<optgroup>` projection and the override-option for non-listed models. Resume-paused button → `<app-button variant="primary" size="sm" [loading]="isResuming()">` inside a `.resume-btn-wrapper` for centering — `[loading]` replaces the `.resume-btn-spinner` text-swap. Permission request actions (approve / auto-accept / deny) → 3× `<app-button>` (success/info/danger). FormsModule kept (still used by chat-input textarea two-way `[(ngModel)]` and the temperature `<input type="range">`). Removed: ~140 lines of inline `.connect-btn`/`.disconnect-btn`/`.connect-dialog`/`.connect-field`/`.connect-actions`/`.action-btn` (connect-dialog variant — *not* the input-area circular send/stop button) + `.action-btn.secondary`/`.mode-select`/`.settings-select` (incl. mobile override)/`.status-chip`/`.model-chip`/`.resume-btn` + `.resume-btn-spinner`/`.perm-btn` + `.perm-btn.approve/.auto-accept/.deny` SCSS (replaced with a small `.resume-btn-wrapper` + `.resume-icon` shim and a `.status-bar > app-badge { flex-shrink: 0 }` layout rule). Kept custom: `.settings-btn` (custom toggle-active state), `.ide-btn`/`.gitea-btn`/`.ide-spinner` (specific Cloud-blue / Gitea-green branding outside the variant matrix), `.action-btn` circular send/stop/spinner state machine in `.input-card` (3-state visual not representable with `<app-button>`), `.chat-input` textarea (slash menu, autoresize, two-way ngModel), `.error-dismiss` (text link), `.task-bar` task-header chevron toggle, `.slash-menu` slash-command picker, all `.tool-summary`/`.tool-detail-*` `<details>` collapsibles, `.thinking-block`/`.thinking-dot` reasoning chrome, `.code-collapse`/`.code-copy-btn` markdown injection, all `::ng-deep` markdown styles, `.session-divider`. |
| `shared/components/instruction-builder` | ✅ | Workspace-proposal status badge (pending/approved/dismissed) → `<app-badge [tone]="proposalStatusTone()" size="xs" [uppercase]="true">` with helper mapping pending→**alert** (Catppuccin Peach, restored after the `--alert` token landed), approved→success, dismissed→neutral. Workspace-proposal apply / dismiss buttons → `<app-button>` (success/ghost) with `<span class="btn-icon">` material-icon retained inside the slot. Error retry → `<app-button variant="danger" size="sm">`. Error dismiss × → `<app-icon-button variant="ghost" size="sm">` (replaces the prior `<button class="dismiss-btn">close</button>` text-as-icon hack with a proper accessible icon-button + slotted `.error-dismiss-icon` material-symbols span). Input-area send / stop → `<app-icon-button size="lg" variant="primary"/danger">` with slotted `.input-action-icon` material-symbols span (preserves the 40 × 40 chip-style action button). FormsModule kept (chat-input textarea two-way `[(ngModel)]`). Removed: ~90 lines of inline `.proposal-badge` + `.badge-pending/.badge-approved/.badge-dismissed`, `.proposal-btn` + `.proposal-btn .btn-icon` + `.apply-btn` + `.dismiss-btn-ws`, `.retry-btn` + `.dismiss-btn`, `.send-btn` + `.stop-btn` SCSS. Kept custom: `.empty-state` illustration (icon + title + desc + hint), `.tool-call-chip` (informational composite — small green pill with material icon, never interactive), `.workspace-proposal-card` peach-tinted layout + `.proposal-header`/`.proposal-icon`/`.proposal-title`/`.proposal-actions` (specialized diff card), `.diff-section`/`.diff-remove`/`.diff-add`/`.diff-content`/`.diff-details`/`.diff-current`/`.diff-new` (specialized diff visualization), `.message-content` user/assistant bubbles, `.streaming` cursor-blink animation, `.session-loading` text-with-spinner, `.chat-input` textarea (autoresize via `(input)` handler, ngModel two-way), all `::ng-deep .markdown-body` + `.markdown-clipboard-button` + Prism token styles. |
| `shared/components/agent-settings` | ✅ | Tab navigation (`Settings` / `Instructions` / `Advanced`) → `<app-tab-nav>` + `<app-tab-nav-item>`. Orientation flips on `mode()` — vertical for `job` (left-border active indicator + tinted bg), horizontal for `session` (bottom-border indicator). Modified-count chip → `<app-badge tone="accent" size="xs" shape="pill">`. Modified-dot kept inline as a 6×6 accent circle. New `onTabChange()` typed handler bridges the primitive's `T \| null` valueChange to the parent's narrowed `AgentSettingsTab` signal. Removed: ~95 lines of `.tab-nav-vertical`/`.tab-nav-horizontal`/`.tab-btn` (×2 orientation variants × hover/active states)/`.tab-badge`/`.tab-badge-dot` SCSS. Kept custom: `.settings-root` shell border/radius, `.tab-content`/`.tab-panel`/`.tab-hidden` panel layout, `.modified-summary` footer, `.tab-modified-dot`. |
| Other `shared/components/*` | ⬜ | notification-bell + empty-catalog-banner (skip — tightly-coupled custom widgets). **Active queue empty.** |

Approach:

1. Rolling, not big-bang — touch each feature once.
2. Inline `.btn`/`.form-input`/`.badge`/`.card` definitions deleted as superseded.
3. Each migration commits clean tsc + clean build + 271/271 tests.

**Token cleanup (between Phase 2 and 3):** ✅ **Complete**

Added `--success`, `--warning`, `--info`, `--danger` (+ `*-tint` variants) to both Mocha and Latte theme maps in `_theme-config.scss`. Replaced hardcoded `#a6e3a1`/`#f9e2af`/`#89b4fa`/`#f38ba8` colors across `button`, `icon-button`, `chip`, `input`, `select`, `textarea`, `badge` primitives with `var(--…)` references. Removed all `// promote to a token when added` markers. Light theme uses Catppuccin Latte equivalents (`#40a02b` / `#df8e1d` / `#1e66f5` / `#d20f39`).

**`--alert` token follow-up (post Phase 3 active queue):** ✅ **Complete**

Added Catppuccin Peach (`--alert: #fab387` Mocha / `#fe640b` Latte) + `--alert-tint` to both theme maps. Extended `BadgeTone` with `'alert'` and added subtle/solid styling in `badge.component.scss`. Migrated the 3 deferred sites to the new tone: `simple/pages/sudo` risk-badge (4-level), `simple/pages/inbox` risk-badge (4-level), `instruction-builder` workspace-proposal pending tone (was shimmed with `warning`/yellow). New `riskTone()` helper in both inbox and sudo maps low→success / medium→warning / **high→alert** / critical→danger. Removed `~30 lines` of inline `.risk-badge` + 4 variants × 2 components.

**Phase 4 — light-mode + theme toggle (2026-04-26):** ✅ **Shipped**

New `ThemeService` (`core/services/theme.service.ts`) owns appearance: per-device `localStorage` preference (`dark` / `light` / `system`), live `prefers-color-scheme` listener for the `system` mode, body-class swap (`theme-dark` / `theme-light`) via an `effect()` so the resolved theme stays in lockstep with the signal. Decision: **not** persisted to backend — theme is a per-device preference (laptop vs phone), and the body class needs to apply before the API responds for first paint. Backend persistence can be layered on later if desired (mirror `i18n.service`'s pattern).

New `<app-theme-toggle>` primitive (`ui/theme-toggle/`) — segmented control with three icon-button options (`light_mode` / `contrast` / `dark_mode`), `role="radiogroup"` + `aria-checked`, optional labels via `[showLabels]`. Wired into `pages/settings/settings.component.ts` as a new **Appearance** section above Language. Transloco strings added under `settings.appearance.*` in both `en.json` and `de-DE.json`.

Spot-audit of "kept custom" SCSS for hardcoded hex that wouldn't theme-swap:
- `project-detail` `.btn-danger` / `.btn-danger-outline` — `#f38ba8` → `var(--danger)` / `var(--danger-tint)`.
- `pages/settings` test-result OK/ERR + `.form-error` — semantic colors → `var(--success)` / `var(--success-tint)` / `var(--danger)` / `var(--danger-tint)`.
- `instruction-builder` message-user avatar, tool-call status, error pill, workspace-proposal pending header, diff-add/-remove labels — all → semantic tokens.
- `job-list` snapshot-stats / preset-badge / delegation-badge / snapshot-badge — `#cba6f7` → `var(--accent-color)`, `#74c7ec` → `var(--info)`, `#a6e3a1` → `var(--success)`, `#6c7086` → `var(--text-muted)`.

Tests: `theme.service.spec.ts` covers initial state (default + stored + invalid), `setPreference` (dark/light persistence + body class swap), `system` mode (resolves dark/light from MQL, flips on OS-level changes when `pref === 'system'`, ignores OS-level changes when an explicit theme is pinned). 281/281 passing (271 baseline + 10 new).

Remaining hardcoded hex — counted **~125 bare-hex usages** still in feature SCSS, dominated by:
- VS Code-style code-highlight palette in `instruction-builder` (`#6a9955` / `#569cd6` / `#ce9178` / `#dcdcaa` / `#b5cea8` / `#d4d4d4` / `#4ec9b0` / `#9cdcfe`) — intentionally dark-themed; would need a separate light-syntax palette to theme-swap. **Out of scope for Phase 4.**
- Many CSS-variable fallbacks (`var(--token, #fallback)`) where the token *is* defined and will resolve correctly — these are defense-in-depth, harmless.
- Scattered `rgba(<mocha-rgb>, x)` tints that should mechanically map to existing `*-tint` tokens. Opportunistic follow-up; not a blocker for shipping the toggle.

**Phase 5 — desktop pages migration + folder consolidation (planned, not yet started):**

The `pages/` directory was originally framed as "desktop-only" in Part 1, but the architecture has converged: `app.ts` renders one shell with a responsive `<app-sidebar>` (overlay drawer on mobile, fixed column on desktop), all routes are viewport-agnostic, and screens from both `pages/*` and `simple/pages/*` render on both viewports. The `pages/` vs `simple/pages/` split is now historical, not architectural.

Five live screens still live under `pages/` and were skipped during the Phase 3 active queue:

| Page | Route | LOC | Inline `.btn` | Inline `.form-input` | Inline `.badge` | `ngModel` | Hardcoded hex | Primitives present |
|---|---|---|---|---|---|---|---|---|
| `pages/settings` | `/settings` | 2,395 | 21 | 40 | 0 | 75 | 88 | only `<app-theme-toggle>` (Phase 4) |
| `pages/project-detail` | `/projects/:id` (from project-list cards) | 2,012 | 22 | 13 | 8 | 0 | 120 | only `<app-icon>` + `<app-spinner>` (opportunistic sweeps) |
| `pages/admin/providers` | `/admin/providers` (admin-gated) | 679 | 5 | 7 | 1 | 10 | 38 | none |
| `pages/admin/models` | `/admin/models` (admin-gated) | 658 | 4 | 6 | 0 | 7 | 31 | none |
| `pages/admin/users` | `/admin/users` (admin-gated) | 275 | 0 | 0 | 1 | 0 | 18 | none (mostly static — table-only) |

Aggregate unmigrated surface: **~6,000 LOC, 52 inline `.btn`, 66 inline `.form-input`, 10 inline `.badge`, 92 ngModels, 295 hardcoded hex**.

**Per-page process (applied individually, not big-bang):**

1. **Primitive migration** — same Phase 3 pattern: replace inline `.btn`/`.form-input`/`.badge`/`.form-group` with `<app-button>` / `<app-input>` / `<app-textarea>` / `<app-select>` / `<app-checkbox>` / `<app-badge>` / `<app-icon-button>` / `<app-form-field>`; remove `FormsModule` + `[(ngModel)]` in favor of `[value]/(valueChange)` (and typed `(changed)` bridges for selects); replace hardcoded hex with `var(--…)` semantic tokens; delete superseded inline SCSS. Each page lands a clean tsc + clean build + green test run.
2. **Folder consolidation** — move the migrated component out of `pages/` and into the same tree the rest of the app lives under (target: `simple/pages/<name>/` for screens, `shared/components/<name>/` if a screen is really a feature component embedded under a route). Update `app.routes.ts` import path; fix any cross-references; delete the now-empty `pages/<name>/` directory. The `pages/` directory is dissolved as each page is migrated; when the last one lands, `pages/` is removed entirely.

**Suggested order** (smallest → largest, lowest-risk first):

1. **`admin/users`** — already mostly free of legacy classes (1 badge, 0 buttons/inputs, 18 hex). Smallest target; serves as the template for the admin-page shape.
2. **`admin/models`** — small (~660 LOC), CRUD list + form pattern. Apply the same primitive vocabulary as `datasource-list` / `agent-settings`.
3. **`admin/providers`** — similar shape and size to `models`. Should be near-mechanical after the first admin page.
4. **`project-detail`** — 2k LOC, no `ngModel` (no `FormsModule` removal needed), but the heaviest hex burden (120). Mostly read/display surface — easier than its size suggests.
5. **`pages/settings`** — 2.4k LOC, the largest single migration: 21 buttons + 40 inputs + 75 ngModels + 88 hex, plus the `.field-label` pattern that was deferred from the form-field sweep. Save for last so the smaller pages set the conventions and the form-field/i18n integration patterns are settled.

**Migration target paths (proposal):**

| From | To |
|---|---|
| `pages/admin/users/` | `simple/pages/admin-users/` |
| `pages/admin/models/` | `simple/pages/admin-models/` |
| `pages/admin/providers/` | `simple/pages/admin-providers/` |
| `pages/project-detail/` | `simple/pages/project-detail/` |
| `pages/settings/` | `simple/pages/settings/` |

(The `simple/` directory could itself be renamed to something less mobile-coded — e.g. `screens/` — once consolidation is complete, but that is a follow-up housekeeping step, not part of Phase 5.)

**Per-page exit criteria:**

- Zero inline `.btn` / `.form-input` / `.form-group` / `.form-label` / `.badge` / `.status-badge` definitions in the component's SCSS.
- Zero `class="btn …"` / `class="form-input"` / `class="badge …"` markup in the template.
- Zero `FormsModule` / `[(ngModel)]` (replaced with primitive `[value]/(valueChange)`).
- All theme-relevant hex literals replaced with `var(--…)` tokens. Intentionally dark-only palettes (e.g., code-syntax) explicitly noted if retained.
- Component lives under `simple/pages/<name>/` (or appropriate alternative); `pages/<old-name>/` is deleted; `app.routes.ts` updated.
- `npm test` green; `tsc` clean.

### Open decisions still owed

- **Desktop `pages/` layout**: ✅ **Resolved (Phase 5 plan above).** The architecture has already converged on a single responsive shell — the remaining work is migrating the 5 surviving `pages/*` screens through the primitive sweep and dissolving the `pages/` folder.
- **Icon library long-term**: keep `mat-icon` (Material Symbols font) as a stable atom, or migrate to lucide-angular / heroicons once primitives are stable? Defer; mat-icon works.
- **Vitest coverage for primitives**: visual regression (Storybook + Chromatic? Playwright snapshots?) deferred until primitives stabilize.
- **`<app-icon>` rollout (2026-04-26 → 2026-04-27):** ✅ **Complete.** All 21 user-facing + 6 debug consumers migrated. `simple/pages/{sessions,inbox,sudo,shell,session-create}`, `simple/layout/sidebar-toggle`, `layout/sidebar`, `app.ts`, `shared/components/{notification-bell,job-create,datasource-list,agent-steps,instruction-builder,persistent-chat}`, all 5 `shared/components/agent-settings/*` sub-components (instructions-tab, tools-group, execution-group, model-group, datasources-group, advanced-accordion), `pages/project-detail`, and all 6 `debug/components/*` files (request-viewer, agent-activity, memory-panel, timeline, db-table, graph-timeline) now import `AppIconComponent` and use `<app-icon>`. Per-component `font-family: 'Material Symbols Outlined'` SCSS blocks deleted. Reset-button "close" glyphs across agent-settings (~50 sites in advanced-accordion alone) wrapped in `<app-icon size="xs">` via single `replace_all` on `>close</button>`. Several incidental hardcoded hex (`#a6e3a1`/`#f38ba8`/`#89b4fa`/`#fab387`) replaced with `var(--success/-tint/danger/-tint/info/-tint/alert)` along the way. **Two `::ng-deep .code-copy-icon` / `.code-collapse-icon` rules in persistent-chat retained** intentionally — they target imperatively-injected innerHTML (markdown post-processor) where `<app-icon>` cannot apply.
- **`<app-spinner>` rollout (2026-04-27):** ✅ **Complete.** All 18 consumers migrated: `agent-settings/{instructions-tab,datasources-group}`, `job-list`, `job-create`, `job-review`, `agent-list`, `datasource-list`, `todo-list`, `chat-history`, `instruction-builder`, `pages/project-detail`, `shared/pages/project-list`, plus all 6 `debug/components/*` (request-viewer, agent-activity, memory-panel, timeline, db-table, graph-timeline). Replaced `<div class="spinner"></div>` / `<span class="spinner-small"></span>` with `<app-spinner size="lg" tone="accent" />` / `<app-spinner size="sm" />`. Per-component `.spinner` / `.spinner-small` SCSS blocks + `@keyframes spin` deleted (~150 lines).
- **`<app-form-field>` rollout (2026-04-27):** ✅ **Mostly complete** for the `.form-group` + `.form-label` markup pattern. `simple/pages/{sessions,session-create}`, `shared/components/{datasource-list,job-create}` migrated — ~25 form-groups now use `<app-form-field [label] [required]? [optional]? [hint]?>`. Per-component `.form-group` / `.form-label` / `.required` / `.hint-inline` SCSS deleted where no longer used. **One form-group retained** in `job-create` (expert selector with dynamic field-hint based on selectedExpert state — primitive's `[hint]` input is static). **`pages/settings` deferred** — it uses a custom `.field-label` pattern (no `.form-group` wrapper), and the surrounding form-row grid layout makes a wholesale swap intrusive without clear benefit.
- **`<app-menu>` rollout:** ⏸ **No-op for current codebase.** All `<details>` elements are inline expand/collapse content sections (reasoning panels, tool-call accordions, rules section), not dropdowns. The only real dropdowns are in `shell.component` (session + model selectors), but those use page-anchored absolute positioning instead of the document-body overlay pattern `<app-menu>` provides — swapping would be a UX behavior change, not a primitive substitution. Primitive remains available for future kebab/action menus.
- **`<app-card>` rollout:** ⏸ **No-op for current codebase.** Existing `.session-card` is a multi-zone flex container (main click area + per-button actions) that doesn't map to `<app-card [interactive]>`. `.expert-card` is a `<button>` with custom per-instance `--expert-color` for the selected border, incompatible with `[selected]` (which uses `--accent-color`). Other card-classed elements (`stat-card`, etc.) are bespoke layouts. Primitive remains available for future cards that fit the surface+interactive+selected pattern.

### Success criteria

- Every feature component imports primitives instead of redefining buttons/inputs/badges.
- Per-feature SCSS files trend toward layout-only — no element-level color/border/radius hardcoding except deliberate one-off overrides via tokens.
- Adding a new feature component does **not** require redefining a button, input, or badge.
- Theme switching is one body-class swap; no JS re-style logic per component.
- Aesthetic matches the user's bar — close enough to the Claude-design feel that it doesn't read as "default Material" or "default Tailwind."
- WCAG 2.2 AA reachable across all primitives without bolting accessibility on later.

This is the design system. We're not picking one — we're owning one.
