---
tags:
  - issue
  - cockpit
  - ui
  - app-select
related:
  - "[[persistent_chat_component_style_budget]]"
---

# Cockpit `app-select` native dropdown opens in the wrong place (detached, top-left) under the scrolled shell

**Filed:** 2026-06-21, from the mobile **Projects** UI review (spotted on the Knowledge-tab
"All statuses" filter, but the cause is generic). **Refined:** 2026-06-21 after web + codebase research
(4 agents — Angular dropdown landscape, modern platform fixes, consumer-side API audit, provider-side
readiness). **Not a live incident / not a regression from our work** — but the *underlying* defect **is**
a tracked **Chromium regression** (see Root cause). The app's fixed nested-scroll shell only makes it
visible.

> Line numbers were accurate on 2026-06-21 and will drift — re-grep `value = model`, `FocusMonitor`,
> `appearance` in `select.component.ts`; `overflow: hidden` in `styles.scss` / `app.ts`; and
> `<app-select` when acting on this.

## Recommendation (TL;DR)

**Adopt `appearance: base-select` (the standardized customizable `<select>`) as progressive
enhancement — this is the better first move than the full CDK-Overlay rewrite the earlier draft
preferred.** It fixes **both** the mis-position *and* the un-themeable OS chrome on our primary
Chromium target, costs **~0 bundle**, requires **zero changes to the ~61 call sites** (it stays a
native `<select>`), and **degrades gracefully** to the normal native popup where unsupported — which
happens to be exactly the browsers (Firefox/Safari) that don't exhibit the bug anyway. The full
custom **CDK-Overlay control is the escalation path**, justified only if/when we need *themed*
dropdowns on Firefox/Safari too, or behavior a native `<select>` can't do. See
[Options](#options-considered) and [Recommended path](#recommended-path-appearance-base-select).

## Symptom

Open an `app-select` whose view has been **scrolled** (e.g. the Knowledge-tab status filter, which
sits below the stats/search): the option list appears **pinned to the top-left of the layout**
(x≈0), far from the trigger, which is elsewhere (e.g. right side of the toolbar). The list is also
**unstyled OS chrome** (grey panel, blue selection) that ignores the active theme.

Reported with a dark-theme screenshot (`~/Desktop/Screenshot_20260621_112843.png`): the
"All statuses" popup is at the screen's left edge while the focused select (red outline) is on the
right. Reproduced live at 1280px (Travertine, Chromium/Linux): the `<select>` field measured
`x=629, w=95` on the right, yet the popup anchored to the container origin on the left.

## Root cause

Two compounding facts:

**1. The mis-position is a tracked Chromium regression (Linux, notably Wayland).** It is *not* a
layout mistake on our side. The native `<select>` popup is OS-rendered chrome whose screen position
is computed by Chromium; recent Chrome (≈139/140) regressed that calculation so the popup lands at
the viewport/layout origin (top-left) instead of at the trigger. Plain `<select>` reproduces it with
no app CSS involved. Tracking issues:

- [issues.chromium.org/441008122](https://issues.chromium.org/issues/441008122) — `<select>` opens at top-left, Chrome 139, Ubuntu 22.04
- [issues.chromium.org/358041219](https://issues.chromium.org/issues/358041219) — dropdowns/tooltips render in top-left corner (Linux/Wayland)
- [issues.chromium.org/438116244](https://issues.chromium.org/issues/438116244) — dropdown positioning broken after Chrome update (also hits Popper/Floating-UI consumers)

**2. Our fixed nested-scroll shell maximizes the blast radius.** The document never scrolls; each
view scrolls inside its own container, so a control is almost always at a non-zero scroll offset when
opened — which is precisely the state the regression mishandles:

- `cockpit/src/styles.scss:24-28` — `html, body { height: 100%; overflow: hidden }`
- `cockpit/src/app/app.ts:83-99` — `.app-container { height: 100dvh; overflow: hidden }` and
  `.content-area { overflow: hidden; position: relative }`
- each view scrolls internally, e.g. project-detail `:host { overflow: auto }`

We verified the select's ancestor chain has **no `transform` / `filter` / `contain` /
`content-visibility`** (the *other* classic popup-clipping causes — ruled out). And **the popup is
unthemeable by design** regardless of position: it's OS chrome, outside our DOM/CSS scope.

**Platform reality:** on non-Linux platforms the native popup may still position correctly today (the
regression is platform-specific), but it stays **unthemed everywhere**. Our dev environment and the
Chromium-embedded IDE context are the worst-affected, so this hits the primary usage path. There is
**no CSS property, attribute, or flag** that fixes native `<select>` popup placement inside a scroll
container — the only real fixes change *what renders the list*.

## Scope

- **App-wide.** **61 `<app-select>` usages across 17 files** (corrected from the earlier "57"; the
  file count was right). Wherever the enclosing view is scrolled, the popup mis-positions; the
  narrower/right-aligned the select, the more obviously "wrong" the left-pinned popup looks.
  Heaviest consumers: `settings` (18), `project-detail` (10), `persistent-chat` (7). Inventory in
  the consumer audit below.
- **Pre-existing**, independent of the mobile-projects polish: the screenshot is the **desktop**
  layout, untouched by the mobile-only media-query edits.
- **Platform-skewed:** position defect is Chromium-Linux/Wayland-primary; the theming defect is
  universal across all browsers/OSes.

## Options considered

| # | Option | Fixes position | Fixes theming | Cross-browser | Effort | Bundle | Call-site churn |
|---|--------|:---:|:---:|---|---|---|---|
| **A** | **`appearance: base-select` (progressive enhancement)** | ✅ Chromium | ✅ Chromium | Chromium now; FF/Safari fall back to native (unthemed, but **unaffected by the bug**) | **Low** | **~0** | **None** (stays native `<select>`) |
| **B** | **CDK-Overlay custom control** (on `@angular/aria`) | ✅ all | ✅ all | ✅ all | **High** | +30–55 kB | None *iff* projected-`<option>` API + `model()` value preserved |
| **C** | Third-party (`mat-select` / `@ng-select` / PrimeNG) | ✅ | ✅ | ✅ | Med–High | Med–Heavy | **High** (array-options API → rewrite 61 sites) |
| **D** | Make the document scroll (drop nested-scroll shell) | maybe | ❌ | — | High/risky | — | shell-wide |
| **E** | Status quo (native `<select>`) | ❌ | ❌ | — | 0 | 0 | none |

Why C/D/E are not recommended:

- **C — third-party kit:** all three render their own popup (positioning quality is theirs, not
  ours; still need `cdkScrollable`-equivalent care for nested scroll). Crucially they expose an
  **`options` array** data API, but our `app-select` has **no `options` input** — every call site
  hand-authors projected `<option>` markup (often `@for` + transloco + conditional text). Adopting a
  kit therefore means **rewriting all 61 sites** plus taking on a heavy theming runtime (Material /
  PrimeNG) or an aggressive-major upgrade treadmill (`@ng-select` bumped its Angular floor in v22/v23).
  Overkill for one control.
- **D — document-scroll shell:** *might* let native popups anchor correctly, but it's a risky
  shell-wide change to header/sidebar sticky behavior and every view's scroll assumptions, **doesn't
  fix theming**, and may not fully resolve the regression. Not recommended.
- **E — status quo:** zero effort, but the popup stays mis-positioned on scrolled views and unthemed.

## Recommended path: `appearance: base-select`

`appearance: base-select` is the standardized successor to the abandoned `<selectlist>`/`<selectmenu>`
prototypes. It **keeps a real native `<select>`** but, once opted in, renders the option list in the
**top layer** (popover semantics) positioned via **implicit CSS anchor positioning** against the
trigger, and exposes every part to CSS.

Why it's the right shape for us:

- **Fixes the bug structurally.** Top-layer + anchor-to-trigger placement is immune to the
  nested-scroll/top-left regression (the OS popup path is no longer used) and can't be clipped by
  `overflow:auto` ancestors.
- **Fixes theming.** The picker becomes stylable DOM: `::picker(select)` (panel), `option` +
  `option:checked` + `option::checkmark`, `selectedcontent` (closed-state label), `select::picker-icon`
  (the arrow), `select:open` (open state, with `transition … allow-discrete`). No more OS chrome.
- **Zero API churn.** It's still a native `<select>` with projected `<option>`/`<optgroup>`, so the
  entire `app-select` surface is preserved automatically: the `value` `model<T|null>()` two-way
  binding, the **string round-trip** + sentinels (`''`, `__null__`, `'true'`/`'false'`, numeric
  strings), `<optgroup>` (6 sites), per-option `[disabled]` (todos), the async **imperative
  re-selection** when `@for` options arrive (15 sites), **native mobile OS picker**, and built-in
  **type-ahead/keyboard a11y** — all for free. None of the 61 call sites change.
- **~0 bundle.** CSS + minor trigger markup, gated behind `@supports`. No new dependency, no CDK
  Overlay chunk — important given the **tight bundle budget** (initial main ≈ 1.91 MB against a
  2.25 MB warn / 2.75 MB error budget in `angular.json:86-96`; ~0.3 MB headroom to warn).
- **Graceful fallback.** In non-supporting browsers the enhancement markup is ignored and you get
  the ordinary native `<select>`. Those browsers (Firefox/Safari) don't have the position regression,
  so the only thing they "lose" is the new theming — an acceptable, non-broken fallback.

**Browser status (verified mid-2026):**

| Feature | Chrome/Edge | Firefox | Safari | Baseline |
|---|---|---|---|---|
| `appearance: base-select` | **135+** (Apr 2025), Android 135, Opera 120, Samsung 29 | Nightly (flag) | Tech Preview only | **Limited (Chromium-only)** |
| Popover API (the primitive) | 114+ | 125+ | 17.4+ | Widely available |
| CSS Anchor Positioning (the primitive) | 125+ | 147+ | 26+ | Baseline 2026 |

Implementation considerations for this path:

- **Opt in inside `app-select` only** — set `appearance: base-select` on the `<select>` *and* its
  picker, gated behind `@supports (appearance: base-select) { … }`, in `select.component.scss`. Add
  the `<button><selectedcontent></selectedcontent></button>` trigger structure as the first child.
  Because the change is localized to the shared component, all 61 consumers inherit it.
- **Hydration spike (do this first).** MDN warns the customizable-`<select>` structure can break
  under SSR/hydration. Cockpit is **CSR-first** (`angular.json`: `outputMode: static`, `ssr: false`,
  no runtime SSR server) **but hydration is on** (`provideClientHydration(withEventReplay())`,
  `app.config.ts:70`; SSR/prerender scaffolding + deps still present). Risk is *reduced* (the selects
  live in authenticated, data-driven views that don't meaningfully prerender) but **not zero** —
  verify the projected `<button>/<selectedcontent>` survives Angular's renderer + hydration with no
  mismatch before rollout.
- **Value safety:** the submitted value comes from each option's trimmed `textContent`. We already
  set explicit `value=""` on most options; **audit that every `<option>` has an explicit `value`**
  before adding rich (icon/multi-line) content, so the value contract is unaffected.
- **A11y:** `::checkmark`/`::picker-icon` aren't in the accessibility tree; mark any decorative
  option icons `aria-hidden="true"`. The trigger button is `inert` by default (single-control
  semantics preserved).
- **Theme it with existing tokens** (so it matches the rest of the UI): panel `--surface-2` +
  `--border-color` + `--shadow-md` + surface radius; rows hover `--hover`, selected `--active` (or
  `color-mix(... var(--accent-color) ...)`); text `--text-primary`/`--text-muted`; focus ring via the
  `focus-ring` mixin (`_mixins.scss:22-35`, `:focus-visible` only). Mirror `menu.component.scss` /
  `menu-item.component.scss` for a consistent look.

## Escalation path: CDK-Overlay custom control

If cross-browser **themed** dropdowns become a hard requirement (Firefox/Safari must match, not just
fall back), or we need behavior a native `<select>` can't do (multi-select w/ checkboxes, inline
filter/search, async-loaded grouped lists), build a custom control — but keep it **API-compatible**
so the 61 call sites stay untouched.

Key facts gathered for this path:

- **Use `@angular/aria` (Angular 21, developer-preview)** for the hard a11y parts: `ngCombobox`
  (select-only/readonly mode) + `ngListbox`/`ngOption` provide roles, `aria-expanded`,
  `aria-activedescendant`/roving focus, keyboard, and type-ahead — designed to pair with
  `cdkConnectedOverlay`. Signals-native. **Risk:** developer-preview API may shift; mitigate by
  isolating it in the one wrapper, or fall back to `mat-select` behind the same wrapper for a stable
  (but heavier) a11y engine.
- **`cdkScrollable` is mandatory, not optional.** CDK's `RepositionScrollStrategy` only watches the
  **document** by default; in our fixed nested-scroll shell it silently no-ops and the panel detaches
  — **reproducing this very bug**. Every inner `overflow:auto` container (ideally the shared
  scroll-container) must carry `CdkScrollable`. Pair with `FlexibleConnectedPositionStrategy` for
  viewport-edge flip/clamp and `STANDARD_DROPDOWN_BELOW_POSITIONS`.
- **API surface to preserve (must stay drop-in):** `value` as a writable **two-way `model<T|null>`**
  (5 sites use `[(value)]`, 1 uses `(valueChange)` — demoting it to a plain input silently breaks
  them); `changed`/`focused`/`blurred` outputs; `size`/`fullWidth` (default **true**, `false` honored
  by ~19 sites)/`disabled`/`required`/`invalid`/`ariaLabel`; `focus()` method; **projected
  `<option>`/`<optgroup>` as the data API** (no `options` input — must parse projected children);
  per-option `[disabled]`; **string round-trip + sentinels**; **async re-selection** when options
  arrive late.
- **Infra is a fresh bootstrap.** CDK Overlay is **not** wired anywhere today — only `cdk/a11y`
  (`FocusMonitor`, `FocusKeyManager`) and one `CdkTrapFocus` are in use. You'd import `OverlayModule`
  + add `@angular/cdk/overlay-prebuilt.css` (or hand-roll the container CSS). The in-house
  **`app-menu`** (`menu.component.ts:57-171`) is a good reference (it uses **manual** positioning +
  body-append + a documented measurement hack) and its `FocusKeyManager` + `app-menu-item`
  (`FocusableOption`, hover/selected styling) are reusable for option rows.
- **Bundle:** `cdk/a11y` already bundled (~free to extend); `@angular/cdk/overlay` adds **~30–55 kB
  raw** + ~2.1 kB prebuilt CSS — won't trip the budget alone but erodes the ~0.3 MB warn headroom and
  stacks with future dep bumps.
- **Testing:** vitest + jsdom; the panel renders **outside** the component subtree (in
  `cdk-overlay-container`/`document.body`) so assert against the overlay container; **jsdom has no
  layout** (`getBoundingClientRect()` → 0) so positioning is **unit-untestable** — cover state with
  bare-injection specs (the repo convention, `copy-field.component.spec.ts`) + one TestBed spec over
  the overlay container, and verify positioning manually / via Playwright. `app-menu` has no spec, so
  there's no existing precedent for a body-appended panel — you'd establish it.
- **Effort/risk: Medium** (well-scoped, good in-repo precedents; top risks = bundle headroom,
  `cdkScrollable` footgun, overlay test ergonomics).

## Acceptance criteria

- Opening any `app-select` on a scrolled view (desktop **and** mobile), on Chromium, shows the list
  **anchored to the trigger**, flipping/clamping at viewport edges — verified at the Knowledge-tab
  status filter (the original repro).
- The open list **matches the active theme** (no OS chrome) on Chromium.
- **All 61 existing usages work unchanged** — no template edits; the `value` `model()`, string
  round-trip/sentinels, `<optgroup>`, per-option disabled, and async re-selection all still behave.
- Full cockpit vitest suite green; bundle stays under the `angular.json` warn budget.
- Keyboard + screen-reader navigation intact; mobile OS picker still works (base-select path).
- **(base-select path)** No hydration mismatch in a dev build with hydration enabled.

## References

### Codebase
- `cockpit/src/app/ui/select/select.component.ts` / `.scss` — current native `<select>` wrapper,
  `value = model<T|null>()`, `FocusMonitor`, `.app-select__field` / `.app-select__chevron`
- `cockpit/src/styles.scss:24-28`, `cockpit/src/app/app.ts:83-99` — the fixed nested-scroll shell
- `cockpit/src/app/ui/menu/menu.component.ts:57-171` + `menu-item.component.ts` — manual-positioning popover (reference / reusable parts)
- `cockpit/src/styles/themes/_theme-config.scss`, `_mixins.scss:22-35`, `_shape-recipes.scss:26-33` — theme tokens, focus-ring, radius recipes
- `cockpit/angular.json:86-96` — bundle budgets; `cockpit/src/app/app.config.ts:70` — hydration
- `ui_review/projects-review.md` — the review where this surfaced

### Chromium regression (the underlying defect)
- <https://issues.chromium.org/issues/441008122> · <https://issues.chromium.org/issues/358041219> · <https://issues.chromium.org/issues/438116244>
- <https://www.smashingmagazine.com/2026/03/dropdowns-scrollable-containers-why-break-how-fix/> — escape-the-container (top layer/portal) is the general fix

### `appearance: base-select` (recommended)
- <https://developer.chrome.com/blog/a-customizable-select> · <https://developer.mozilla.org/en-US/docs/Learn_web_development/Extensions/Forms/Customizable_select>
- <https://web-platform-dx.github.io/web-features-explorer/features/customizable-select/> · <https://caniuse.com/customizable-select> · <https://una.im/select-updates/>

### CDK Overlay / `@angular/aria` (escalation)
- <https://angular.dev/guide/aria/combobox> · <https://angular.dev/guide/aria/listbox> · <https://angular.dev/guide/aria/select>
- <https://briantree.se/angular-cdk-overlay-tutorial-scroll-strategies/> — `cdkScrollable` + reposition strategy
- <https://www.w3.org/WAI/ARIA/apg/patterns/combobox/examples/combobox-select-only/> — APG select-only combobox
- <https://material.angular.dev/components/select/overview> — `mat-select` (stable fallback a11y engine)
