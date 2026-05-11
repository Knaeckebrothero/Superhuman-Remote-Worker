# Persistent chat — component style budget keeps growing

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
- Pattern of "bump and ship" means we will hit this again on the next feature (likely Dynamic Canvas / audio polish, per `docs/features/dynamic_canvas.md`).

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

## Related

- `docs/features/dynamic_canvas.md` will add another panel into this surface; do this refactor first or it'll land into the same monolith.
- Component-style budget pattern also applies to `instruction-builder.component.ts` and `job-list.component.ts`, but those are below thresholds today.
