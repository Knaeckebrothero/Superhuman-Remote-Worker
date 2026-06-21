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
"All statuses" filter, but the cause is generic). **Not a live incident / not a regression** —
a long-standing limitation of using a native `<select>` inside the app's fixed, nested-scroll shell.

> Line numbers were accurate on 2026-06-21 and will drift — re-grep `app-select__field`,
> `overflow: hidden` in `styles.scss` / `app.ts`, and `<app-select` when acting on this.

## Symptom

Open a `app-select` whose view has been **scrolled** (e.g. the Knowledge-tab status filter, which
sits below the stats/search): the option list appears **pinned to the top-left of the layout**
(x≈0), far from the trigger, which is elsewhere (e.g. right side of the toolbar). The list is also
**unstyled OS chrome** (grey panel, blue selection) that ignores the active theme.

Reported with a dark-theme screenshot (`~/Desktop/Screenshot_20260621_112843.png`): the
"All statuses" popup is at the screen's left edge while the focused select (red outline) is on the
right. Reproduced live at 1280px (Travertine): the `<select>` field measured `x=629, w=95` on the
right, yet the popup anchors to the container origin on the left.

## Root cause

`app-select` renders a **native `<select>`** with projected `<option>`s
(`cockpit/src/app/ui/select/select.component.ts:22`). The open list is therefore the **browser/OS
popup**, whose position is computed by Chromium — not by app CSS (which is also why it can't be
themed).

The Cockpit shell is a **fixed, nested-scroll layout** — the document never scrolls; each view
scrolls inside its own container:

- `cockpit/src/styles.scss:24-28` — `html, body { height: 100%; overflow: hidden }`
- `cockpit/src/app/app.ts:83-99` — `.app-container { height: 100dvh; overflow: hidden }` and
  `.content-area { overflow: hidden; position: relative }`
- each view scrolls internally, e.g. project-detail `:host { overflow: auto }` +
  `.page-container { overflow: hidden auto }`

Chromium (Linux) does not account for that inner scroll offset when placing a native `<select>`
popup: it anchors to the **layout/container origin** rather than the live, scrolled position of the
control. So once a view is scrolled, the popup lands top-left, detached. Verified the select's
ancestor chain has **no `transform` / `filter` / `contain` / `content-visibility`** (those are the
*other* classic causes and were ruled out) — it is specifically the document-non-scroll +
inner-scroll-container model.

## Scope

- **App-wide.** Affects **every** `app-select` — **57 usages across 17 files** (`grep -rho '<app-select' src/app`)
  — wherever the enclosing view is scrolled. The narrower/right-aligned a select is, the more
  obviously "wrong" the left-pinned popup looks.
- **Pre-existing**, independent of the mobile-projects polish: the screenshot is the **desktop**
  layout (toolbar row, select narrow on the right), where the mobile-only media-query edits don't
  apply. The KB toolbar's desktop markup/CSS is unchanged by that work.
- **Not fixable in CSS** — native popups aren't positionable.

## Proposed solution (preferred) — replace the native popup with a CDK-Overlay dropdown

Give `app-select` a **custom dropdown panel rendered through `@angular/cdk` Overlay**
(`@angular/cdk` 21.2.8 is already a dep; `cdk/overlay` is present). CDK Overlay positions against the
trigger via a `FlexibleConnectedPositionStrategy` and **reposition-on-scroll** strategy, which
correctly handles nested scroll containers — eliminating the mis-position.

Why this is the right shape:

- **API-compatible → fixes all 57 usages at once with zero per-call edits.** Keep the public
  surface identical: projected `<option>` content, `value` model, `changed`/`focused`/`blurred`
  outputs, `size`/`fullWidth`/`disabled`/`invalid`/`ariaLabel` inputs.
- **Themed.** The panel becomes app DOM, so it inherits the active theme (no more OS chrome).
- **Verifiable.** Being real DOM, the panel can be screenshotted/measured in Playwright (native
  popups can't), so the fix is testable in CI/local review.
- Mirrors the app's existing custom **`app-menu`** pattern (though `app-menu` uses *manual*
  positioning — prefer CDK Overlay here for the scroll-aware strategy).

Implementation considerations / risks (this is a careful shared-component rewrite, not a one-liner):

- Parse projected `<option>`s into `{ value, label, disabled, selected }`; support **dynamic
  options** (`@for`) and re-projection. Keep a hidden native `<select>` for form/value semantics if
  helpful, or fully own state.
- **Accessibility:** `role="combobox"`/`listbox`/`option`, `aria-activedescendant`, `aria-expanded`;
  full **keyboard** support (Up/Down/Home/End, type-ahead, Enter/Space, Esc, Tab-closes) — today's
  `FocusMonitor` + `cdk/a11y` (`ActiveDescendantKeyManager`) can back this.
- Outside-click / scroll / resize close + reposition; `size`/`fullWidth` parity; min-width = trigger
  width; flip when near viewport edges.
- **Test the blast radius:** all 17 consumers (jobs/sessions/project-detail filters & inline forms,
  datasource/repo/member role selects, admin pages, etc.) — run the full vitest suite + live-verify
  a scrolled select on mobile and desktop.

## Alternatives considered

- **Make the document scroll instead of nested containers** (drop `html,body`/`.app-container`
  `overflow: hidden`, let the window scroll): might let native popups anchor correctly, but it's a
  risky shell-wide change to header/sidebar sticky behavior and every view's scroll assumptions, and
  may not fully resolve it. Native popups would also still be unthemed. Not recommended.
- **Leave the native `<select>`**: zero effort, but the popup stays mis-positioned on scrolled views
  and unthemed. The status quo.

## Acceptance criteria

- Opening any `app-select` on a scrolled view (desktop **and** mobile) shows the list **anchored to
  the trigger**, flipping/clamping at viewport edges.
- The panel matches the active theme.
- All 57 existing usages work unchanged (no template edits required); full cockpit unit suite green.
- Keyboard + screen-reader navigation intact.

## References

- `cockpit/src/app/ui/select/select.component.ts` — native `<select>` + projected options
- `cockpit/src/app/ui/select/select.component.scss` — `.app-select__field`, sizes, `data-full-width`
- `cockpit/src/styles.scss:24-28`, `cockpit/src/app/app.ts:83-99` — the fixed nested-scroll shell
- `cockpit/src/app/ui/menu/menu.component.ts` — existing manual-positioning popover (reference)
- `ui_review/projects-review.md` — the review where this was surfaced
