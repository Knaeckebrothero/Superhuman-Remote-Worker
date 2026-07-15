# Session history opens with the resume card half cut off — the scroll pin enumerates *causes* of height change instead of observing height

**Status:** **DIAGNOSED 2026-07-15, UNBUILT** — root cause traced and fix designed below; awaiting green-light before implementation.
**Found:** 2026-07-14, user-reported from the cockpit UI (thread `b35a74ef`, "Reviewing Better Resavio Project Setup"). Reproduces on any **ended** session opened from the session list.
**Severity:** **Low as reported** (cosmetic landing position on ended-session open) — but the reported bug is the **7th instance of one design defect** in this component, and instances **8–10 are already broken today and unreported** (async image loads, code-block collapse, live session-end). The value here is retiring the bug *class*, not the screenshot. The fix is a **net code deletion**.
**Component:** cockpit `PersistentChatComponent` (`cockpit/src/app/views/persistent-chat/persistent-chat.component.ts`, 3240 lines) · `PersistentChatService.connect()` (`cockpit/src/app/core/services/persistent-chat.service.ts:724`) · `persistent-chat.component.scss:315` (`overflow-anchor: none`)
**Related:** [[persistent_chat_component_style_budget]] — same kitchen-sink component; that doc tracks the *style* symptom, this one the *behavioural* symptom of the same "component owns everything" problem · [[persistent_session_history_windowing_and_compaction]] — the compaction banner is another bottom-of-transcript element, i.e. a **future instance #11** unless this is fixed structurally · [[session_turn_rendering]]

## Symptom

Click a session in the list to read its conversation history. The view opens scrolled to a position where the **"Resume this session?"** card at the bottom of the transcript is **half cut off** by the viewport edge — the RESUME button is visible but clipped. It isn't a clean "scrolled to the last message" landing either; it lands in an in-between position that looks like a rendering glitch.

It is **flaky**: if the thread-meta request happens to resolve quickly enough, the card is included in the scroll and the open looks correct. That non-determinism is itself a fingerprint of the root cause (a load-order race, below).

## Root cause

### 1. The scroll pin enumerates *causes* of height change, not the effect

The component wants exactly one invariant:

> **If the user is following the bottom, keep the bottom in view — no matter what changed the height.**

Nothing in the code expresses that. Instead there are **six hand-added hooks**, each one someone discovering a *distinct cause* of a height change and patching that specific cause. Each carries a war-story comment:

| # | Site | Trigger | Comment (abridged) |
|---|------|---------|--------------------|
| 1 | `persistent-chat.component.ts:1936` | `effect` on `turns()` / `currentStreamingTurn().events` / `pendingPermission()` → `setTimeout(0)` | "Auto-scroll when turns or in-flight events change" |
| 2 | `persistent-chat.component.ts:1966` | `effect` on `pendingAttachments().length` → `setTimeout(0)` | "Attachment chips grow the composer … shrinking the .messages viewport" |
| 3 | `persistent-chat.component.ts:2150` | `window` `resize` listener | "the on-screen keyboard shrinks the layout viewport, so .messages loses ~40% of its height" |
| 4 | `persistent-chat.component.ts:2217` | **synchronous** re-pin inside `autoResizeInput()` | "a deferred re-pin is what made the conversation visibly jump up-then-down on every keystroke" |
| 5 | `persistent-chat.component.ts:2585` | `jumpToLatest()` | (explicit user action) |
| 6 | `persistent-chat.component.ts:2601` | `afterNextRender` in `loadOlderHistory()` | scroll-delta restore on prepend |

All six funnel into one three-line primitive (`persistent-chat.component.ts:2813`):

```ts
private scrollToBottom(): void {
    const el = this.messagesContainer?.nativeElement;
    if (el) {
        el.scrollTop = el.scrollHeight;
    }
}
```

Because the triggers are a **hand-maintained list of known causes**, every *new* element at the bottom of the transcript — and every async height change — is a latent bug until somebody finds it and adds hook #7.

The tell is in the code's own words at `persistent-chat.component.ts:2214`, which explicitly blames the component's own CSS:

> "… worsened by `.messages { overflow-anchor: none }`, which strips the browser's own scroll-preservation."

`overflow-anchor: none` (`persistent-chat.component.scss:315`, comment: *"We own the scroll in script (autoScroll + watchdog)"*) was set so the browser wouldn't fight the manual pinning. The price was having to hand-maintain every re-pin the browser previously did for free. Note the comment references a **"watchdog" that no longer exists** in the file — the machinery has already decayed.

### 2. The resume card is cause #7, and nothing tracks its trigger

The auto-scroll effect (`persistent-chat.component.ts:1936`) tracks `turns()`, `currentStreamingTurn().events`, and `pendingPermission()`. It does **not** read `chat.threadStatus()` or `chat.endedAt()`:

```ts
effect(() => {
    this.chat.turns();                        // ← tracked
    const active = this.chat.currentStreamingTurn();
    if (active) active.events.length;         // ← tracked
    this.chat.pendingPermission();            // ← tracked
    // chat.threadStatus() / chat.endedAt() are NOT read here
    if (this.autoScroll) {
        setTimeout(() => { if (this.autoScroll) this.scrollToBottom(); }, 0);
    }
});
```

But the "SESSION ENDED" divider **and** the resume card render on exactly those untracked signals (`persistent-chat.component.ts:1229`):

```html
@if (chat.threadStatus() === 'ended') {
  <div class="end-marker">…SESSION ENDED {{ … }}…</div>
  <div class="resume-card">…Resume this session?… [RESUME]…</div>
}
```

Both are ordinary in-flow flex children of `.messages-inner`, inside the `.messages` scroll container — **not** `position: fixed`/`sticky`. So they genuinely sit below the scrolled viewport; this is not an overlap/z-index issue. Strings live at `cockpit/src/app/assets/i18n/en.json` under `chat.ended` (`eyebrow: "Resumable"`, `title: "Resume this session?"`).

Signal set, card mounts, content height grows, **no effect re-fires**, and `overflow-anchor: none` has disabled the browser's own compensation. The container stays parked at the old bottom.

### 3. The `connect()` waterfall widens the race window

`PersistentChatService.connect()` (`persistent-chat.service.ts:724`) loads history and thread-meta as **two serial awaited round-trips**:

```ts
if (!sameThread) {
    // … ~25 lines of state reset …
    this.threadId.set(threadId);
    await this.loadHistory(threadId, generation);      // :764  → dispatch('load_history') → turns() populates
    if (!this._isCurrentConnect(threadId, generation)) return;
    if (opts.carryOutbox) this._redispatchOutboxBubbles();
}
await this.loadThreadMeta(threadId, generation);       // :774  → separate GET /persistent/threads/{id}
```

`loadThreadMeta` (`:1053`) is the *only* thing that sets the card's trigger (`:1069–1070`):

```ts
this.threadStatus.set((thread.status as ThreadStatus) || null);
this.endedAt.set(thread.ended_at || thread.last_activity || null);
```

Cold-open ordering:

1. `loadHistory` resolves → `turns()` populates → effect fires → `setTimeout(0)` → `scrollToBottom()` pins to the bottom **of the messages only** (`scrollHeight` excludes the card, which isn't in the DOM yet).
2. `loadThreadMeta` resolves → `threadStatus = 'ended'` → end-marker + resume card append → content height grows.
3. Nothing re-pins → **card below the fold**.

Whether step 2 beats the step-1 `setTimeout(0)` macrotask is a **network race** — hence the flakiness.

This waterfall is **independently wrong**, regardless of scrolling: `connect()` decides *"should I even open a WebSocket?"* **from** the meta response, but fetches meta **last** — so it loads and renders the entire history before discovering it didn't need a socket at all (`persistent-chat.service.ts:775` onward returns `disconnected` for ended threads).

### Why `Promise.all` alone does **not** fix this

The tempting one-line fix is to parallelise the two fetches. **It narrows the race window; it does not close it.** `loadHistory` and `loadThreadMeta` each dispatch into the store *as soon as they individually resolve*, so the card still mounts in a **separate render** from the turns — just sooner. The open would go from "reliably wrong" to "usually right", which is worse: it hides the defect behind timing.

Only two things actually close it deterministically: observing the height (§Fix 1), or collapsing to a **single payload → single dispatch → single render** (§Fix 2b). This distinction is the main reason this doc exists.

## Instances already broken today, unreported

The same defect has live victims nobody has filed:

- **Code-block collapse.** `ngAfterViewChecked` (`:2179`) → `collapseCodeBlocks()` (`:2821`) wraps tall `<pre>` blocks in collapsed `<details>` *after* the scroll has fired — **shrinking** content height with no re-pin.
- **Async images / attachments.** Markdown images and attachment thumbnails load later and change height; there is no image-`onload` re-pin anywhere.
- **Live session-end.** `threadStatus.set('ended')` also fires at runtime (`persistent-chat.service.ts:2859`, `:2868`) when a session ends *while you are watching it*. The resume card mounts mid-view with no re-pin — so this is **not only a cold-open bug**.
- **Future: the compaction banner** ([[persistent_session_history_windowing_and_compaction]]) is another bottom-of-transcript element, i.e. instance #11 in waiting.

## Proposed fix

### 1. Replace the enumerated triggers with a `ResizeObserver` — the actual fix

Stop guessing what changes the height; **observe the height**. One observer, one rule, guarded by the `nearBottom` state that already exists (`persistent-chat.component.ts:2570`, 80px threshold):

```ts
// Add alongside the existing @ViewChild('messagesContainer') at :1672
@ViewChild('messagesInner') messagesInner!: ElementRef<HTMLDivElement>;

private stickObserver?: ResizeObserver;

ngAfterViewInit(): void {
    this.stickObserver = new ResizeObserver(() => {
        // One rule, every cause: if the user is following the bottom, keep it in view.
        if (this.autoScroll && !this.isRestoringScroll) this.scrollToBottom();
    });
    this.stickObserver.observe(this.messagesContainer.nativeElement); // viewport height: keyboard, composer autosize
    this.stickObserver.observe(this.messagesInner.nativeElement);     // content height: turns, resume card, images, code collapse
}

ngOnDestroy(): void {
    this.stickObserver?.disconnect();   // add to the existing ngOnDestroy at :2184
}
```

This **subsumes hooks 1–4** and fixes the resume card, the image loads, the code-block collapse, and the live session-end — *without needing to know about any of them*. Net effect on the file: delete two `effect`s (`:1936`, `:1966`), the `window` resize listener (`:2150` + its `ngOnInit`/`ngOnDestroy` add/remove at `:2155`/`:2186`), the synchronous re-pin in `autoResizeInput()` (`:2217`), and every `setTimeout(0)` dance. **The principled fix is smaller than what it replaces** — which is what makes this an easy call rather than a trade-off.

Correctness notes:
- **No feedback loop.** The handler sets `scrollTop` only; it never mutates size, so it cannot re-trigger the observer (no `ResizeObserver loop completed with undelivered notifications`).
- **No flicker.** RO callbacks are delivered after layout and **before paint**, which is precisely the timing hook #4 hand-rolled.
- **No collision with the prepend restore** (hook 6, `:2594`). `loadOlderHistory` only runs when the user is near the *top* (`scrollTop < 120`, `:2566`), where `autoScroll` is `false` — so the `if (this.autoScroll)` guard excludes it by construction. `isRestoringScroll` (`:1881`) is kept as belt-and-braces.
- **Keep hooks 5 & 6.** `jumpToLatest()` is an explicit user action, and the prepend restore is a *different* invariant (preserve position, not follow bottom). Both stay.

**The one site to verify by hand rather than assume:** the composer autosize (hook #4). Its comment insists on a same-frame re-pin, and RO's pre-paint timing *should* satisfy it — but that comment was earned the hard way, so type a multi-line draft and watch for the up-then-down jump before declaring it done.

### 2. Fix the `connect()` waterfall — independent, do it too

Not a scroll fix; a **latency + correctness** fix that happens to share a symptom. Two options:

- **(2a) Parallelise.** Hoist the `loadThreadMeta` call to start concurrently with `loadHistory`, await both. Respects the existing `!sameThread` structure (meta runs on every connect; history only on the cold path). Kills the serial round-trip. **Does not** make the render deterministic — §"Why `Promise.all` alone does not fix this". Acceptable *because* Fix 1 makes render order irrelevant.
- **(2b) Single payload.** Have the history endpoint return `status` + `ended_at`, so it's one round-trip → one dispatch → one render. Deterministic, and lets `connect()` decide about the WebSocket *before* loading anything. Backend change (`orchestrator/main.py`).

**Recommendation: 2a now** (small, local, no API churn), with 2b noted as the better end state if the sessions-open path ever needs the latency. Fix 1 is what makes either safe.

### Deliberately **not** doing

- **Extracting a shared stick-to-bottom directive.** Verified there is no second implementation to share with: `views/chat/` is a thin wrapper hosting `<app-persistent-chat>`; `views/chat-history/` is a **read-only debug/audit trace viewer** whose only scroll handler is *pagination*, not pinning (`chat-history.component.ts:716` — a near-bottom check that calls `loadMore()`, with no `scrollTop = scrollHeight` anywhere); and there is no `simple/` chat component. A repo-wide grep for `scrollToBottom|autoScroll|jumpToLatest` hits **`persistent-chat.component.ts` only**. **No duplication ⇒ no abstraction.** Revisit only if [[shared_chat_library]] materialises.
- **Touching `overflow-anchor: none`** (`persistent-chat.component.scss:315`). With RO as a single coherent owner it becomes a *defensible* choice rather than a hack compensated for in six places, and the prepend path handles itself. Don't churn it.
- **Adding hook #7** (i.e. just adding `chat.threadStatus()` to the effect's tracked reads). This is the "fix" the bug invites and it is the wrong one: it patches instance #7 and leaves #8–#11 live.

### Aside worth noting

`ResizeObserver` appears **nowhere** in the cockpit today, and `afterNextRender` exactly once (`:2601`). This is a genuinely missing primitive, not a pattern the codebase already knows — expect it to be reusable.

## Acceptance criteria

1. Opening an **ended** session from the session list lands with the **entire resume card visible** — deterministically, not by timing luck. Verify with the network throttled (slow the thread-meta GET) so the *unfixed* order is forced; it must still be correct.
2. A session that ends **while being watched** (`threadStatus` → `'ended'` at runtime) mounts the resume card **without** leaving it below the fold.
3. A transcript with tall code blocks (collapsed by `collapseCodeBlocks`) and with images lands at a true bottom after collapse/load settle.
4. **No regressions** in the hand-earned behaviours the deleted hooks encode:
   - typing a multi-line draft does **not** make the conversation jump up-then-down (hook #4);
   - adding an attachment chip keeps the latest turn visible (hook #2);
   - the mobile on-screen keyboard opening keeps the bottom pinned (hook #3);
   - scrolling up to read older history is **not** yanked back down, and "Jump to latest · N new" still works (`:2560`, `:2579`);
   - prepending older history preserves position without jumping (hook #6).
5. Net **negative** line count in `persistent-chat.component.ts`, and the component-style budget is **not** bumped ([[persistent_chat_component_style_budget]]).
6. `cd cockpit && npx vitest run` green; `ng build` passes the budget.

## Verification plan

Per CLAUDE.md "Plan → Develop → Verify", this is verified **locally on k3d** before commit — no dev-cluster round trip:

- `cd cockpit && npx vitest run src/app/views/persistent-chat/…` for unit coverage of the pin guard.
- Tilt running → `https://localhost/` → open the exact reported thread (`b35a74ef`, "Reviewing Better Resavio Project Setup") and walk criteria 1–4 by hand. Drive with Playwright; note the cockpit is **zoneless**, so poke component state via `ng.getComponent(el)` rather than expecting change detection from synthetic events.
- Force criterion 1's race with DevTools network throttling on `GET /api/persistent/threads/{id}`.
