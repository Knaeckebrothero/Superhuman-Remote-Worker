# Persistent chat — component style budget keeps growing

**Canvas outcome (2026-07-13):** Dynamic Canvas did not add its pane to this
component. It landed as a sibling under `ChatPageComponent`, with shared thread
transport exposed through a narrow bridge. The component-style budget problem
described here remains independently open.

## Symptom (observed 2026-05-11, CI build of `aa722c5`)

The cockpit Docker build fails in `npm run build` with:

```
ERROR  angular:styles/component:scss;…/persistent-chat/persistent-chat.component.ts
exceeded maximum budget. Budget 32.00 kB was not met by 527 bytes with a total of 32.53 kB.
```

Workaround applied today: bumped `anyComponentStyle` budget in `cockpit/angular.json` from `24kB / 32kB` (warning/error) to `32kB / 40kB`. CI unblocked, but this is the **fourth** budget bump for the same reason in roughly two months.

## Budget bump history

| Commit | Date | Warning → Error | Reason in message |
|--------|------|-----------------|--------------------|
| (pre `63b4e66`) | — | 12kB → 16kB | initial |
| `63b4e66` | 2026-03-31 | 16kB → 20kB | "increase warning and error thresholds" |
| `9e5810f` | (earlier) | 16kB → 20kB+ | "update Angular component style limits" |
| `95edbd4` | 2026-05-08 | 24kB → 32kB | bundled into "collapse idle into ended" |
| (this doc) | 2026-05-11 | 32kB → 40kB | unblock CI after audio-viz commit |

## Root cause

`persistent-chat.component.ts` is a kitchen-sink component:

- 4161 lines total
- inline `styles: [...]` array spans ~1980 lines (`persistent-chat.component.ts:983` onward)
- compiled SCSS now 32.53 kB, having grown ~63% since `95edbd4` three days ago

Recent inflation by commit:

| Commit | Insertions | Feature |
|--------|-----------:|---------|
| `aa722c5` | +1139 | audio visualization + device capability detection |
| `07d5dbd` | +374 | composer refactor + locale updates |
| `5b5f05b` | +87 | "Jump to Latest" feature |
| `3ce776d` | +86 | WebSocket reconnect engine + banner |

Every new feature lands as more inline SCSS in the same component. The component currently handles: transport (WS reconnect engine), composer, message list, audio recorder + visualizer, image preview, drag-and-drop, file uploads, startup card, thread metadata, "Jump to Latest", drop overlay. It is the de-facto persistent-chat shell.

## Why repeated budget bumps are bad

- Hides the real signal — budgets exist precisely to flag "this file is too big."
- The bigger the inline style block, the slower the change-detection / template compile feedback loop in dev.
- Style isolation per-component is the only Angular-native lever; once you stop respecting the budget, you also lose tree-shakeable, scoped style optimization gains.
- Pattern of "bump and ship" means another persistent-chat feature can hit this
  again. Dynamic Canvas avoided adding to the monolith by landing as a sibling;
  audio and composer work still exercise this budget.

## Proposed fix

Refactor `persistent-chat.component.ts` into a shell + child components, each with its own style budget.

### Step 1 — extract inline styles to file

- Move the inline `styles: [...]` array to `persistent-chat.component.scss`, switch to `styleUrls: ['./persistent-chat.component.scss']`.
- Pure mechanical move, no behavior change. Lets reviewers see SCSS diffs cleanly going forward.
- Same budget will still apply, but visibility improves.

### Step 2 — split sub-features into child components

Candidate splits, each independently styleable:

| Proposed component | Owns |
|--------------------|------|
| `persistent-chat-shell` (current file, slimmed) | layout, drop overlay, thread header, routing into children |
| `persistent-chat-composer` | textarea, send/stop buttons, attachments, key bindings |
| `persistent-chat-message-list` | rendering loop, jump-to-latest, scroll anchoring |
| `persistent-chat-audio` | recorder UI, visualizer canvas, device capability bits added in `aa722c5` |
| `persistent-chat-image-preview` | dialog + zoom UI (already separated as `app-dialog` — extend) |
| `persistent-chat-startup-card` | empty-state suggestions, model picker entry point |

Each new component:
- gets its own `anyComponentStyle` budget allocation
- can have its own spec file (the parent is currently untestable in isolation)
- isolates re-render scope — composer keystrokes won't dirty message-list change detection

### Step 3 — re-lower the budget

Once split, revert `anyComponentStyle` to a reasonable `16kB / 24kB` so future drift is caught early. The shell component, free of feature-specific styles, should comfortably fit.

## Acceptance criteria

- [ ] No single `persistent-chat-*` component exceeds 20 kB compiled SCSS
- [ ] `anyComponentStyle` budget restored to `16kB / 24kB` in `cockpit/angular.json`
- [ ] Each new sub-component has a `.scss` file (no inline `styles:`)
- [ ] Existing persistent-chat behavior unchanged: send/receive, audio record, image preview, drag-drop, reconnect banner, jump-to-latest, startup card
- [ ] No regression in Lighthouse / first-paint metrics on `/sessions/<id>`

## Effort estimate

Step 1 (move to file): ~30 min, fully mechanical.
Step 2 (split components): ~1 day per sub-component split if done carefully — five splits ≈ one engineering week including review.
Step 3 (budget revert): trivial.

## Interim escape hatch — global partial (2026-07-28)

Until Step 2 lands, new chat styling goes into a **global partial** under
`cockpit/src/styles/` (`@use`d from `styles.scss`) rather than into this
component. First use: `src/styles/_chat-queued.scss` for the stalled-send
affordances (see `docs/features/session_reliability_and_transport_simplification.md`).
That kept the component's compiled SCSS byte-identical; inlining the same
~35 lines had pushed it 443 bytes over the 36 kB warning, which would have
been bump number five.

**Gotcha that cost two attempts — read before using this hatch.** A global
partial silently loses the cascade to a component rule it is trying to
override. Angular's emulated encapsulation rewrites component selectors with
an `[_ngcontent-…]` attribute, so

```
.message-user.queued .avatar-icon          /* in the component: reads as (0,3,0) */
.message-user.queued[_ngcontent-ng-cNNN] .avatar-icon[_ngcontent-ng-cNNN]   /* actually (0,5,0) */
```

A plain global `.message-user.stalled .avatar-icon` is (0,3,0) and loses.
Worse, an *equal*-specificity global rule also loses: Angular injects the
component `<style>` after the global sheet, so ties go to the component.
Beating it needed `.message.message-user.queued.stalled .avatar .avatar-icon`
— (0,6,0), with the `.avatar` step that looks redundant but is load-bearing.

Rule of thumb: **add roughly one extra class per encapsulation attribute** —
+1 for a host selector, +2 for a descendant one. Prefer stacking classes the
element already carries over `!important`.

**Neither `ng build` nor unit tests catch this.** The template compiles, the
selector exists in `document.styleSheets`, the element matches it, the design
tokens resolve — and `getComputedStyle` still returns the component's value.
Only a browser against the running app shows it (here: `opacity: 0.65`
instead of 1, `--text-secondary` instead of `--danger`). Budget a
computed-style assertion into any k3d check that touches cross-file CSS:

```js
getComputedStyle(el).color                       // the truth
[...document.styleSheets].flatMap(s => { try { return [...s.cssRules] } catch { return [] } })
  .filter(r => r.selectorText?.includes('avatar-icon'))
  .map(r => r.selectorText + ' { ' + r.style.cssText + ' }')   // shows the [_ngcontent] rewrite
```

This hatch is a stopgap, not a resolution — it relocates bytes without
splitting the component, and each use adds a specificity wart that Step 2
should delete.

## Related

- `docs/features/dynamic_canvas.md` added a sibling panel under
  `ChatPageComponent`, not inside `PersistentChatComponent`; its transport bridge
  is a useful boundary for later extractions.
- Component-style budget pattern also applies to `instruction-builder.component.ts` and `job-list.component.ts`, but those are below thresholds today.
