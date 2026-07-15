# Session history opens with the resume card half cut off — the scroll pin enumerates *causes* of height change instead of observing height

**Status:** **DIAGNOSED 2026-07-15 · design HARDENED by research 2026-07-15 · UNBUILT** — awaiting green-light. Research pass (7 agents, web + codebase) refuted four claims in the first draft of this doc; see §"What the research changed". Design is now believed sound and is backed by primary sources + in-browser measurement.
**Found:** 2026-07-14, user-reported (thread `b35a74ef`, "Reviewing Better Resavio Project Setup"). Reproduces on any **ended** session opened from the session list.
**Severity:** **Low as reported** (cosmetic landing position) — but the reported bug is **1 of ~18** untracked height changes inside the transcript, several of which are *guaranteed visible jumps today* (see §"The real scale"). Plus the investigation surfaced a **real iOS bug the app has never had a fix for** (§Fix 4). The value is retiring the bug *class*, not the screenshot.
**Component:** cockpit `PersistentChatComponent` (`cockpit/src/app/views/persistent-chat/persistent-chat.component.ts`, 3240 lines) · `PersistentChatService.connect()` (`cockpit/src/app/core/services/persistent-chat.service.ts:724`) · `persistent-chat.component.scss:315` (`overflow-anchor: none`) · `orchestrator/main.py:21062` (history endpoint)
**Related:** [[persistent_chat_component_style_budget]] — same kitchen-sink component; that doc tracks the *style* symptom, this one the *behavioural* symptom of the same "component owns everything" problem · [[persistent_session_history_windowing_and_compaction]] — the compaction progress block (`:1173`) is on the untracked list below · [[session_turn_rendering]] · [[shared_chat_library]] (relevant only if a second chat view ever appears)

## Symptom

Click a session in the list to read its history. The view opens scrolled to a position where the **"Resume this session?"** card is **half cut off** by the viewport edge — RESUME visible but clipped. Not a clean "scrolled to the last message" landing either.

It is **flaky**: if the thread-meta request resolves fast enough the card is included and the open looks correct. That non-determinism is the fingerprint of the load-order race in §Root cause 3.

## Root cause

### 1. The scroll pin enumerates *causes* of height change, not the effect

The component wants one invariant:

> **If the user is following the bottom, keep the bottom in view — no matter what changed the height.**

Nothing expresses that. Instead there are **six hand-added hooks**, each patching one *known cause*, each with a war-story comment:

| # | Site | Trigger | Comment (abridged) | Fate under the fix |
|---|------|---------|--------------------|--------------------|
| 1 | `:1936` | `effect` on `turns()` / `currentStreamingTurn().events` / `pendingPermission()` → `setTimeout(0)` | "Auto-scroll when turns or in-flight events change" | **DELETE** — RO subsumes |
| 2 | `:1966` | `effect` on `pendingAttachments().length` → `setTimeout(0)` | "Attachment chips grow the composer … shrinking the .messages viewport" | **DELETE** — RO subsumes |
| 3 | `:2150` | `window` `resize` listener | "the on-screen keyboard shrinks the layout viewport, so .messages loses ~40% of its height" | **DELETE** — see §Fix 4; not a regression |
| 4 | `:2217` | **synchronous** re-pin in `autoResizeInput()` | "a deferred re-pin is what made the conversation visibly jump up-then-down on every keystroke" | **KEEP** — hard-won same-frame fix |
| 5 | `:2585` | `jumpToLatest()` | (explicit user action) | **KEEP** — intent, not geometry |
| 6 | `:2601` | `afterNextRender` in `loadOlderHistory()` | scroll-delta restore on prepend | **KEEP** — different invariant |

All six funnel into one primitive (`:2813`):

```ts
private scrollToBottom(): void {
    const el = this.messagesContainer?.nativeElement;
    if (el) {
        el.scrollTop = el.scrollHeight;   // ← also wrong; see §Fix 2
    }
}
```

Because the triggers are a **hand-maintained list of known causes**, every new bottom element and every async height change is a latent bug until someone finds it and adds hook #7.

**A lint rule cannot save this.** [angular-eslint](https://github.com/angular-eslint/angular-eslint) ships `prefer-signals`, `no-uncalled-signals`, `prefer-signal-model` — none detects "this effect should have tracked that signal". It's undecidable: reads are dynamic and the correct dependency set exists only in the author's head. The structural fix is to stop depending on a dependency list for DOM geometry.

### 2. The resume card is one untracked height change out of ~18

The auto-scroll effect (`:1936`) tracks `turns()`, `currentStreamingTurn().events`, `pendingPermission()`. It does **not** read `chat.threadStatus()` / `chat.endedAt()`:

```ts
effect(() => {
    this.chat.turns();                        // ← tracked
    const active = this.chat.currentStreamingTurn();
    if (active) active.events.length;         // ← NOT a signal dep; see below
    this.chat.pendingPermission();            // ← tracked
    // chat.threadStatus() / chat.endedAt() are NOT read here
    if (this.autoScroll) {
        setTimeout(() => { if (this.autoScroll) this.scrollToBottom(); }, 0);
    }
});
```

The "SESSION ENDED" divider and resume card render on exactly those untracked signals (`:1229`), as ordinary in-flow flex children of `.messages-inner` — **not** `position:fixed`/`sticky`, so this is not an overlap/z-index issue.

Two latent fragilities in that effect, worth recording:
- **`active.events.length` (`:1939`) creates no signal dependency at all** — it's a plain property read on a plain array. The subscription works *only* because the reducer (`persistent-chat.service.ts:3035`) replaces `conversation` immutably, so `turns()` re-emits per delta. The comment claims it "keeps the effect subscribed to deltas"; that is not the mechanism.
- **It reads `turns()`, not `visibleTurns()`** — so `windowSize()` changes (`growWindow`/`resetWindow`) never trigger it.

### 3. The `connect()` waterfall widens the race window

`PersistentChatService.connect()` (`:724`) loads history and thread-meta as **two serial round-trips**:

```ts
if (!sameThread) {
    // … ~25 lines of state reset …
    this.threadId.set(threadId);                       // :763
    await this.loadHistory(threadId, generation);      // :764 → dispatch('load_history') → turns() populates
    if (!this._isCurrentConnect(threadId, generation)) return;
    if (opts.carryOutbox) this._redispatchOutboxBubbles();
}
await this.loadThreadMeta(threadId, generation);       // :774 → separate GET, sets threadStatus/endedAt (:1069-1070)
```

Cold-open ordering: history resolves → `turns()` populates → effect fires → `setTimeout(0)` → pin to the bottom **of the messages only** (the card isn't in the DOM). Meta resolves → card mounts → height grows → **nothing re-pins**. Whether meta beats the `setTimeout(0)` macrotask is a **network race** — hence the flakiness.

The waterfall is **independently wrong**: `connect()` decides *"should I open a WebSocket?"* **from** the meta response but fetches meta **last** — so it loads and renders the entire history before discovering it didn't need a socket (`:780` returns `disconnected` for ended threads).

**And the fix is nearly free.** `orchestrator/main.py:21085` (verified):

```python
user, thread = await require_thread_owner(request, postgres_db, thread_id)
```

`thread` is bound and **never referenced again** in the handler body (21085–21138 — every later `thread` hit is the `thread_id` *string* or a method name). `require_thread_owner` (`orchestrator/security/access.py:544`) already did `SELECT * FROM threads WHERE id = $1`. So `status` and `ended_at` are **already in memory, already paid for, and thrown away**. The return dict (`:21133`) is a plain `dict[str, Any]` with **no `response_model`** — adding two keys is additive and zero-query. Today the waterfall runs that same `SELECT * FROM threads` **twice**.

### Why `Promise.all` alone does **not** fix this

The tempting one-liner is to parallelise the two fetches. **It narrows the race; it does not close it.** `loadHistory` and `loadThreadMeta` each dispatch into the store as they individually resolve, so the card still mounts in a **separate render** — just sooner. The open goes from "reliably wrong" to "usually right", which is worse: it hides the defect behind timing.

Only two things close it deterministically: observing height (§Fix 1), or one payload → one dispatch → one render (§Fix 5).

## The real scale: ~18 untracked height changes, not one

A full inventory of `.messages-inner`'s subtree found the resume card is one of **~18** height changes invisible to the effect at `:1936`. The RO catches **every one** — that is the strongest argument for the design. The most damaging, none of them reported:

| Site | What | Why it matters |
|---|---|---|
| `:2821` `collapseCodeBlocks()` | wraps `pre.scrollHeight > 200` in a **closed** `<details>` — ~600px → ~30px — from `ngAfterViewChecked` (`:2180`), i.e. **after** the effect's `setTimeout(0)` already pinned | **A guaranteed visible jump today, on every long code block.** Only an RO can catch it. |
| `:799` `chatPrefs.reasoningExpanded()` | toggles `<details class="thinking-block">` — flips **every reasoning block in the transcript at once** | Massive instantaneous height delta, zero re-pin |
| `:949` | whole-turn collapse (`isTurnCollapsed`, `userTurnCollapsed` + auto-threshold) | untracked |
| `:1173` `chat.compaction()` | compaction progress block + 1s tick reflowing `.compaction-pass` text | untracked ([[persistent_session_history_windowing_and_compaction]]) |
| `:1002` `chat.narrationMode()` | thought-card outlet mounts/unmounts | untracked |
| `:1046` `<app-read-aloud>` | entire phase machine: actions → box → status → player → `<details class="ra-spoken">` | untracked, multi-stage |
| `:1156` `chat.runningTool()` | running-command card | untracked |
| `:1262` | reconnect banner (+ text swaps at `:1267-1272`) | untracked |
| `:1230/:1238` | **end-marker + resume card** | **the reported bug** |
| — | markdown `<img>` — **there is no `.message-body ::ng-deep img` rule**, so no `aspect-ratio`/reserved box | full reflow on load, no re-pin |
| `index.html:39-40` | webfonts `display=swap` (FOUT) + **Material Symbols `display=block` (FOIT, ~3s)** | every `<app-icon>` in every turn reflows on swap |
| `core/markdown/katex.directive.ts:57` | lazy `loadKatex().then(() => typeset())` + `katexDefer` flip | `$$…$$` → tall math boxes, post-render |
| `:2575` `resetWindow()` | removes turns from the top while `autoScroll === true` | effect reads `turns()` not `visibleTurns()` → nothing re-pins today |
| various | `<details>` user toggles: `.compaction-summary` `:855/:1021`, `.user-text-collapsible` `:881`, `.tool-group` `:980`, tool-card `tc` | untracked |

**RO blind spots — exactly one, and it is provably benign.** `.messages` (`:831`) has precisely two children: `.messages-inner` (`:835`, closes `:1281`) and `@if`'d `.jump-latest` (`:1286`). The pill is `position: sticky`, therefore *in-flow*, so it does add ~33px to `.messages`'s scrollHeight without touching `.messages-inner`'s box → RO does not fire. But `autoScroll` and `scrolledAway` are written from the same `nearBottom` expression in the same statement pair (`:2571-2572`), and `jumpToLatest()` writes both atomically (`:2580-2581`), so the invariant **`showJumpToLatest() ⟹ !autoScroll`** holds unconditionally. The pin is gated off exactly when the blind spot is live.

Everything `absolute`/`fixed` in the messages subtree (`.code-copy-btn` scss:1795, `.compaction-segment.active::after` scss:2242) contributes zero height by design. No `contain`, no `content-visibility`, no `will-change` anywhere in the file.

## What the research changed

Recorded because four of these were asserted confidently in this doc's first draft and are **wrong**:

1. **~~"Six hooks collapse into one"~~ → three do.** Hooks #4, #5, #6 are not geometry events. #5 is an intent transition; #6 is scroll *preservation*, a different invariant (RO can't distinguish "grew at top" from "grew at bottom"); #4 is a documented same-frame fix worth keeping as belt-and-braces. Honest framing: **three fragile hooks collapse into one invariant, and ~18 latent bugs die with them.**
2. **~~"The fix is a net code deletion"~~ → it is roughly break-even, and net-positive once the iOS fix lands.** Deleting hooks 1-3 is ~19 lines; the RO + wheel escape + `visualViewport` listener is ~37. The justification is *retiring a bug class and fixing iOS*, **not** "it's smaller". The first draft leaned on that and it doesn't hold.
3. **~~"The code's own comment blames `overflow-anchor: none`, therefore that CSS is the wound"~~ → the comment is technically incorrect.** `:2214-2215` blames `overflow-anchor: none` for the keystroke jump. Scroll anchoring measures the anchor's position **in the scrolling content's coordinate space**; growing the composer resizes the *scrollport*, which doesn't move content in that space → `y1 - y0 = 0` → anchoring does nothing either way. The jump is `scrollTop` **clamping** on scrollport resize, a CSSOM behaviour anchoring has no bearing on. **Keeping `auto` would not have prevented it.** Delete that parenthetical. (The *SCSS* comment at `:314` is well-founded — see §Rejected.)
4. **~~"Put the RO in `ngAfterViewInit`; it would crash under SSR otherwise"~~ → SSR is off, so it wouldn't crash.** Verified: `angular.json:82` `"ssr": false`, `"outputMode": "static"`. The scaffolding exists (`main.server.ts`, `app.config.server.ts`, `provideClientHydration(withEventReplay())`, a `serve:ssr:cockpit` script) but the build doesn't use it. `afterNextRender` is still the right call — it's browser-only *by contract*, guarantees view queries are resolved, matches the existing usage at `:2601`, and is safe if SSR is ever switched on — but it is a robustness call, **not** a correctness gate. *(If SSR/prerendering is ever enabled, re-check: this becomes a hard requirement.)*

Also confirmed, against the first draft: **`ResizeObserver` appears nowhere in the cockpit** and `afterNextRender` exactly once (`:2601`) — a genuinely missing primitive. And the SCSS comment at `:314` promises a *"watchdog"* that **does not exist anywhere in `cockpit/src`**. `overflow-anchor: none` has been disabling the browser's scroll preservation on the promise of a safety net that was never built. **This RO is that watchdog** — worth saying so in the commit.

## Proposed fix

### 1. `ResizeObserver` as the **detector** — this part is consensus

Stop guessing what changes the height; observe the height. **Both** elements, one observer — this is not optional and the reason is asymmetric:

| Target | Fires on | Does **not** fire on |
|---|---|---|
| `.messages-inner` | content growth: turns, streaming deltas, `<img>` decode, webfont swap, code-block collapse, **the `threadStatus` card** | viewport changes — its own box is content-driven |
| `.messages` | viewport shrink: composer autosize, attachment chips, Android keyboard, banners mounting as siblings (`.startup-banner` `:819`, `.error-banner` `:1295`, composer unmount `:1306`) | **new messages** — it's `flex: 1`, a fixed-height viewport; content growth never changes its box |

Observing only `.messages` would **never fire on a new message** — the pin would break entirely. Measured: growing the composer 40→120px fired **only** `messages`; appending a turn fired **only** `inner`. They are layout-independent in opposite directions. When both change in one frame you get **one callback with two entries** → still one pin (an argument *for* one observer over two).

Keep the default `content-box`. Padding is constant and RO detects *changes*, so a fixed offset can't affect detection. Do **not** use `device-pixel-content-box` — `devicePixelContentBoxSize` is unimplemented in Safari ([WebKit 219005](https://bugs.webkit.org/show_bug.cgi?id=219005), open since 2020), so the enum is rejected outright.

### 2. The **handler** — and this is where the first draft was naive

`if (following) el.scrollTop = el.scrollHeight` is the exact line every mature implementation refuses to write.

- **`scrollHeight` is not a valid `scrollTop`.** The target is `scrollHeight - clientHeight` (`use-stick-to-bottom` uses `scrollHeight - 1 - clientHeight`). Browsers clamp, so it appears to work — but you write X and read back Y, and `onMessagesScroll` (`:2570`) then recomputes `autoScroll` from the clamped value. Harmless alone; combined with the race below it's what makes the failure silent. **`scrollToBottom()` at `:2813` has this bug today.**
- **Element/Matrix explicitly rejects the pattern** ([scrolling.md](https://github.com/element-hq/matrix-react-sdk/blob/develop/docs/scrolling.md)): *"setting scrollTop while scrolling tends to not work well, with it interrupting ongoing scrolling and also querying scrollTop reporting outdated values and consecutive scroll adjustments cancelling each out previous ones… worse on macOS"*, and *"reading `scrollTop` can … easily return values that are out of sync with what is on the screen, probably because scrolling can be done off the main thread."*
- **Never wrap the pin in `rAF` or `setTimeout`.** This is measured, not theoretical. RO is broadcast at HTML "update the rendering" **step 16**; rAF callbacks are **step 14**; paint is **step 22**. So a `rAF` scheduled *from inside* an RO callback lands in the **next** frame — the current frame paints un-pinned:

  | pin style | scrollTop at next frame start | actual bottom | |
  |---|---|---|---|
  | direct (in RO callback) | 534 | 534 | pinned ✅ |
  | rAF-deferred | 534 | 694 | **160px stale for one painted frame** ❌ |

  Identical in Chromium and Firefox. That 160px gap *is* the up-then-down jump hook #4's comment describes. **MDN's recommended mitigation for the loop error is precisely the thing that reintroduces the flicker.** The existing `setTimeout(0)` paths (`:1945`, `:1969`) have exactly this defect — which is the real reason they're being deleted.

### 3. The `wheel`/`touch` escape — a **new requirement** the first draft lacked

The dangerous race is the opposite of the obvious one. A correct pin leaves `scrollHeight - scrollTop - clientHeight === 0 ≤ 80` → still following → benign. **The real bug:** user starts scrolling up → an RO tick lands *before* their scroll event → `autoScroll` is still `true` → **the pin yanks them back** → their scroll event now computes at-bottom → `autoScroll` stays `true` → **the user physically cannot read back during streaming.** RO fires on *every* delta while streaming, so this gets *more* likely, not less. It's the bug every one of these projects shipped first ([hermes-webui#677](https://github.com/nesquena/hermes-webui/issues/677)).

`use-stick-to-bottom`'s comment: *"The browser may cancel the scrolling from the mouse wheel if we update it from the animation in meantime. To prevent this, always escape when the wheel is scrolled up."* **Wheel/touch is the only user-intent signal a layout shift cannot forge.** The existing double-`if (this.autoScroll)` + the comment at `:1943-1944` show the team already sensed this race and compensated with a re-check; make it explicit.

```ts
el.addEventListener('wheel', ({ deltaY }) => {
  if (deltaY < 0 && el.scrollHeight > el.clientHeight) this.autoScroll = false;
}, { passive: true });
```

Also worth stealing: an `ignoreScrollToTop`-style guard (record the clamped value read back after a pin, and have `onMessagesScroll` ignore that exact value) so self-inflicted scrolls can't be misread as intent.

### 4. iOS keyboard — deleting hook #3 is safe, and there's a **separate real bug** underneath

Two researchers contradicted each other here; the settlement matters.

- **iOS Safari has never supported `interactive-widget`** ([WebKit 259770](https://bugs.webkit.org/show_bug.cgi?id=259770), NEW since Aug 2023; [standards-positions #65](https://github.com/WebKit/standards-positions/issues/65) still "Needs position"). `index.html:19` sets `interactive-widget=resizes-content` — **inert on iOS**. The code comment at `:2147` is accurate for Android and describes a world that has never existed on iOS.
- On iOS the OSK shrinks only the **visual** viewport; the layout viewport/ICB is unchanged, so `dvh`/`svh`/`lvh` don't move and **`window.resize` does not fire** ([PPK](https://www.quirksmode.org/blog/archives/2017/06/toolbars_keyboa.html): the keyboard *"is undetectable"*). **Hook #3 has always been dead on iOS.** And it would be a no-op anyway: `.messages` didn't resize, so if `autoScroll` is true you're already at the bottom, and the newest turn is hidden behind an overlay outside the document that no container scroll can move.
- **On Android with `resizes-content` the ICB shrinks → `.messages` (flex:1 of a 100dvh column) shrinks → RO fires.** Measured: viewport 845→400 ⇒ `.messages` 785→340, RO fired. And in the superset case — composer 60→160 with no viewport change — **RO fired, `window.resize` did not**. `RO ⊇ window.resize` for this component.

**⇒ Deleting hook #3 is an improvement on Android and a no-op on iOS. Not a regression on either.**

The genuine iOS fix — which the app has **never had** — is a `visualViewport` listener driving a CSS inset. This is a **new bug found by the investigation**, and it's elegant because it self-neutralises:

```ts
const vv = window.visualViewport;
const onViewportGeometry = () => {
  if (!vv || vv.scale !== 1) return;   // guard is load-bearing: iOS has ignored
                                        // user-scalable=no since iOS 10, so pinch-zoom
                                        // shrinks vv.height and would fake a keyboard
  const layoutH = document.documentElement.clientHeight;   // unchanged by the OSK on iOS
  const inset = Math.max(0, layoutH - vv.height - vv.offsetTop);
  document.documentElement.style.setProperty('--kb-inset', `${inset}px`);
};
vv?.addEventListener('resize', onViewportGeometry);
vv?.addEventListener('scroll', onViewportGeometry);   // iOS: offsetTop pans without a resize
```
```css
.chat-container { height: calc(100dvh - var(--kb-inset, 0px)); }
```

- **iOS:** layout viewport stays, visual shrinks → `--kb-inset` = keyboard height → shell shrinks → `.messages` shrinks → **the RO fires and re-pins for free.** iOS finally behaves like `resizes-content`.
- **Android:** `resizes-content` already shrank `layoutH`, so `inset ≈ 0` → **the formula self-neutralises.** No double-handling, no platform branch.

Rejected for this: the **VirtualKeyboard API** / `env(keyboard-inset-height)` is Chromium-only (Safari ✗ through 26.5; Firefox open since 2021, [not an interop 2026 priority](https://zouhir.org/blog/virtual-keyboard-api/)) and per spec `overlaysContent = true` *overrides* `interactive-widget` — do not use both. `100svh` vs `100dvh` is irrelevant: the OSK isn't UA UI, so it affects none of `dvh`/`svh`/`lvh`.

**Scope note:** this is a genuinely separate fix. It can ship after the RO — flag it, don't bundle it silently.

### 5. The `connect()` waterfall — minimal 2b, and mind the landmine

**Do the minimal version: return `status` + `ended_at` from the history handler.** Two lines at `orchestrator/main.py:21133`, zero extra queries, additive to a non-Pydantic plain dict. The only other consumer is MCP (`orchestrator/mcp/client.py:2477`), which does `resp.json()` → formatter and ignores extra keys; the cockpit types it as a structural subset (`:1020`).

Be precise about what that buys: **it un-gates the connect decision from `loadThreadMeta`.** Once `connect()` reads status off the history response, the `threadStatus() === 'ended'` check (`:780`) no longer waits on the meta GET, `_openSse` can start right after `loadHistory`, and meta becomes genuine fire-and-forget enrichment (title/model/turn count/cloud URL). **2a alone cannot do this**, because the WS decision structurally depends on the meta response.

**Skip full 2b** (deleting the meta GET). `loadThreadMeta` also sets `cloudSessionUrl`, computed server-side via `_resolve_cloud_session_url(thread, mounts)` (`main.py:20293`) needing a second `list_thread_mounts` query — and it has two non-connect callers (`:1187` SSE-reconnect refresh, `:1419` `_refreshStatusAfterDrop`), so it can't be deleted anyway.

**⚠️ The 2a landmine — silent and flaky.** If the meta kick-off is hoisted *above* `this.threadId.set(threadId)` (`:763`), its internal guard compares `this.threadId()` (still the **old** thread, or `null`) against `threadId` and **bails silently** → `threadStatus` never set → an ended thread opens a WS it shouldn't. Because the guard runs *after* the await, it's timing-dependent. The kick-off must stay below `:763`:

```ts
if (!sameThread) { /* …reset… */ this.threadId.set(threadId); }   // :763
const metaP = this.loadThreadMeta(threadId, generation);          // safe only here
if (!sameThread) {
    await this.loadHistory(threadId, generation);
    if (!this._isCurrentConnect(threadId, generation)) return;
    if (opts.carryOutbox) this._redispatchOutboxBubbles();        // must stay after loadHistory
}
await metaP;
```

The generation guard itself is sound and parallelising adds no new race: `connect()` bumps `connectGeneration` via `disconnect()` (`:729`, `:1823`) then snapshots (`:730`); both loaders check internally (`:1005`, `:1022`, `:1046`, `:1058`) *and* `connect()` re-checks after each await. Neither loader can reject (both swallow errors at `:1043`/`:1075`), so `Promise.all` can't fast-fail and `allSettled` is unnecessary.

### 6. Recommended code shape (Angular 21, zoneless)

Add `#messagesInner` to `.messages-inner` (`:835`).

```ts
private readonly messagesContainer = viewChild.required<ElementRef<HTMLDivElement>>('messagesContainer');
private readonly messagesInner     = viewChild.required<ElementRef<HTMLDivElement>>('messagesInner');
private readonly destroyRef = inject(DestroyRef);

constructor() {
  // afterNextRender: browser-only by contract, view queries guaranteed resolved.
  // (Matches loadOlderHistory's existing use at :2601. SSR is currently off —
  //  angular.json "ssr": false — but this stays correct if it's ever enabled.)
  afterNextRender(() => {
    const container = this.messagesContainer().nativeElement;
    const inner = this.messagesInner().nativeElement;

    // One observer, two targets. RO reports an element's OWN box, so:
    //   inner     -> content growth (turns, deltas, <img> decode, font swap,
    //                code-block collapse, the threadStatus resume card)
    //   container -> viewport shrink (composer autosize, Android keyboard,
    //                sibling banners) — content growth NEVER changes its box.
    // Both in one frame -> one callback, two entries -> one pin.
    const ro = new ResizeObserver(() => {
      // Pure DOM write: no signal reads, so there is no dependency list to
      // forget — that is the entire point. Runs after layout / before paint,
      // so NEVER defer this into rAF or setTimeout (measured: 160px stale for
      // one painted frame — that IS the historic up-then-down jump).
      if (!this.autoScroll || this.isRestoringScroll) return;
      container.scrollTop = container.scrollHeight - container.clientHeight;
    });

    ro.observe(inner);
    ro.observe(container);
    this.destroyRef.onDestroy(() => ro.disconnect());
  });
}
```

- **No `NgZone.runOutsideAngular`** — zoneless is the v21 default and provides `NoopNgZone` (`runOutsideAngular` is literally `return fn()`). No CD notification needed: it's a pure DOM write, invisible to the template.
- **`autoScroll` (`:1878`) and `isRestoringScroll` (`:1881`) are plain fields, not signals** — so the callback creates no reactive coupling. Keep **both** guards: `isRestoringScroll` is read at exactly one place today (`:2564`) and would *not* otherwise cover the RO. `autoScroll` is false on the prepend path anyway, but rely on the explicit guard, not spooky-action-at-a-distance across `:2567`/`:2571`.
- `viewChild.required` is the recommended query for new code and is safe here (`.messages` is not inside an `@if`). Migrating all five `@ViewChild`s is out of scope.

### 7. Small CSS wins while in here

- **`overscroll-behavior-y: contain`** on `.messages` — unconditional cheap win; stops scroll chaining into the page and disables pull-to-refresh on mobile. Safari 16+, ~94%.
- **Pin `scroll-behavior: auto` explicitly** on `.messages` and use `scrollTo({top, behavior:'instant'})` for pins. Verified: nothing sets `scroll-behavior` anywhere in the cockpit today, so it computes to `auto` and pins are already instant — but this is a latent trap. Per [CSSOM-View](https://drafts.csswg.org/cssom-view/#scrolling), the `scrollTop` setter scrolls *"with the scroll behavior being 'auto'"*, which resolves to the **computed** `scroll-behavior` — so one global `html { scroll-behavior: smooth }` would silently animate every pin. Repeated pins don't queue (step 1 *aborts* any ongoing smooth scroll) but they'd chase a moving target and never settle, and `scrollend` would never fire. Someone already hit this: `canvas-pane.component.scss:333` explicitly sets `scroll-behavior: auto`. Reserve `'smooth'` for `jumpToLatest()`.

### 8. ⚠️ Document the `flex: 1` guarantee — it is load-bearing and invisible

`.messages-inner { flex: 1 }` (scss comment: *"flex:1 lets the empty-state center vertically"*) means that when content is **short**, the inner is stretched to the container height — so appending content does **not** change its box and **the RO does not fire**.

This is safe, and not by luck. It's an identity:

```
inner.height = max( H − 32 − J , C )
    H = .messages clientHeight (border-box; styles.scss:18-21)
    32 = padding 16px × 2 (scss:309)
    J  = .jump-latest height (sticky ⇒ in-flow ⇒ counts; 0 when absent)
    C  = content height (the min-height:auto floor)

scrollable ⟺ inner.height > H − 32 − J
stretched  ⟺ inner.height = H − 32 − J
```

These are **mutually exclusive by construction** — the negation of each other. Every missed fire is a frame where scrolling was impossible. `J` appears on both sides and cancels. `.empty-state` has `flex: 1`, not a `min-height`, so on a short viewport it overflows into the `C` branch (not stretched) and the regime holds. Verified: `min-height` appears at exactly two sites in the SCSS (`:1291` `.chat-input`, `:1539` `.recording-strip`) — **both in the composer, outside `.messages`** — and no child of `.messages-inner` sets one.

**The guarantee rests entirely on `.messages-inner` keeping `overflow: visible` and no explicit `min-height`**, which is what makes `min-height: auto` resolve to the content-based automatic minimum. **If anyone adds `overflow: hidden` / `contain: layout` / `min-height: 0` to `.messages-inner`, the automatic minimum collapses to 0, its box is pinned at `H−32−J` forever, the RO never fires, and content silently clips.** That is the one edit that destroys this design — so it needs a comment in the SCSS, not just this doc.

## Rejected alternatives (with evidence)

- **Adding hook #7** (just tracking `threadStatus()` in the effect). The fix the bug invites; patches 1 of ~18 and leaves the rest live.
- **`flex-direction: column-reverse`.** A spec-level prohibition, not a preference — [css-flexbox-1](https://drafts.csswg.org/css-flexbox/#order-property) Advisement: *"Authors **must not** use `order` or the `*-reverse` values of `flex-flow`/`flex-direction` as a substitute for correct source ordering, **as that can ruin the accessibility of the document**."* Correct visual order requires feeding the array newest-first ⇒ a screen reader reads the conversation backwards, invisible to sighted QA (plausibly [SC 1.3.2 Meaningful Sequence](https://www.w3.org/WAI/WCAG22/Understanding/meaningful-sequence.html) via F1). It would also break this file specifically: measured, the scroll range inverts to `[-180, 0]`, so `nearBottom` (`:2570`) computes 180 at the bottom → `autoScroll` permanently false, and `loadOlderHistory`'s `scrollTop < 120` trigger (`:2566`) is true while parked at the newest message → infinite history loading. Plus sticky breaks ([Chromium 331753412](https://issues.chromium.org/issues/331753412), open). [TanStack Virtual's chat docs](https://tanstack.com/virtual/latest/docs/chat) now say outright: *"You do not need `flex-direction: column-reverse`, inverted transforms, or manual `scrollTop += delta` prepend compensation."*
- **Removing `overflow-anchor: none`.** Keep it. Anchoring only compensates for changes *above* the viewport position — it is **not** a bottom-pin (verified: prepending 100px auto-compensated exactly +100; appending drifted 100px off). More decisively, **shipping Safari has no scroll anchoring at all** — Baseline "limited", no Safari; it landed in [Safari 27 beta](https://webkit.org/blog/17967/news-from-wwdc26-webkit-in-safari-27-beta/) (unreleased). So JS is required regardless, and leaving anchoring on would make Chrome/FF **double-compensate** while Safari compensates once. `none` buys engine-uniform behaviour. The SCSS comment at `:314` is well-founded and can now cite [csswg-drafts#7745](https://github.com/w3c/csswg-drafts/issues/7745) (*"there's no way for a page to override the scroll anchoring heuristics, which seems unfortunate, since that means it's unreliable"*) — Mozilla has proposed removing the feature.
- **CSS Scroll Snap bottom-pin.** [css-scroll-snap-1 §Re-snapping](https://drafts.csswg.org/css-scroll-snap-1/#re-snap) ships a chat log as a **normative worked example** (`scroll-snap-type: y proximity` + `::after { scroll-snap-align: end }`), and it genuinely works with correct disengage semantics. **But it only arms after a *real user scroll*** — a programmatically-pinned app never arms it (verified: armed by programmatic `scrollTop` → no pin; armed by real keyboard scroll → pins). `mandatory` yanks the user to the bottom on any scroll-up. Plus [WebKit #243107](https://bugs.webkit.org/show_bug.cgi?id=243107) (open): Safari re-snapping a live feed *"while the user is doing nothing"*. Unusable here.
- **`scroll-initial-target` / `scroll-start`.** `scroll-start` **never existed** as a property. `scroll-start-target` was real and was **renamed** to `scroll-initial-target`, shipped **Chrome/Edge 133** — but it sets a **one-time initial** scroll position ("much like scrolling to a URL fragment"), not a pin, and is Chrome-only. Not applicable. (`anchor-name` is popover positioning — a naming collision. `interactivity` is the CSS form of `inert`.)
- **The `overflow-anchor` sentinel trick** ([CSS-Tricks](https://css-tricks.com/books/greatest-css-tricks/pin-scrolling-to-bottom/)). Confirmed to work for appends *and* streaming — but requires **every** element between scroller and messages to be `overflow-anchor: none`; this app's centered reading-column wrapper silently breaks it (reproduced). Needs `scrollTop != 0`, any style change on the path silently drops it, and Safari ignores it entirely.
- **`afterRenderEffect` instead of RO.** It would **reproduce this exact bug**. It runs after render *only when its tracked signal dependencies are dirty* (`after_render_effect.ts`: `if (!this.dirty) { return this.signal; }`), so an `afterRenderEffect` that omits `threadStatus()` fails identically. Worse, after-render hooks only run inside `ApplicationRef.tick()`, and in zoneless the tick triggers are an enumerated list — **an `<img>` decoding, a webfont swapping, and `visualViewport` resizing notify the scheduler of none of them.** (`afterEveryRender` runs untracked every tick but still can't see an image load.) `afterNextRender` keeps hook #6 because that job needs a before/after height diff around a *known* Angular render — the one place "Angular caused this and I know it" is the right frame.
- **Angular CDK.** `ViewportRuler` is window `resize`/`orientationchange` only. `ScrollDispatcher`/`CdkScrollable` are scroll events, not size. `cdkObserveContent`/`ContentObserver` is **MutationObserver**-based — wrong tool: fires on mutations that don't change height and misses image/font load entirely (no mutation occurs). A real RO wrapper exists but only at `@angular/cdk/observers/private` — no semver guarantee. `cdk-virtual-scroll-viewport` has no bottom-anchoring ([components#12932](https://github.com/angular/components/issues/12932), closed without it). `@angular/cdk` is already a dep but only `cdk/a11y` is imported; nothing here justifies widening that.
- **MutationObserver alongside RO** (Vercel's `ai-chatbot` does both). Their RO observes `container.children` **once at mount**, so new children are never observed → MO is required to cover `childList`. Observing a stable `.messages-inner` wrapper makes that unnecessary. Their earlier version used `attributes: true` and **hovering the edit button scrolled the chat** ([#878](https://github.com/vercel/chatbot/issues/878)) — an MO pathology RO is immune to by construction.
- **Extracting a shared stick-to-bottom directive.** No second implementation to share with: `views/chat/` is a thin wrapper hosting `<app-persistent-chat>`; `views/chat-history/` is a read-only debug/audit trace viewer whose only scroll handler is *pagination* (`chat-history.component.ts:716` — near-bottom → `loadMore()`, no `scrollTop = scrollHeight` anywhere); no `simple/` chat component. Repo-wide grep for `scrollToBottom|autoScroll|jumpToLatest` hits **`persistent-chat.component.ts` only**. **No duplication ⇒ no abstraction.** Revisit if [[shared_chat_library]] materialises.

## Acceptance criteria

1. Opening an **ended** session lands with the **entire resume card visible** — deterministically. Verify with the thread-meta GET throttled so the *unfixed* order is forced.
2. A session that ends **while being watched** (`threadStatus.set('ended')` at runtime — `persistent-chat.service.ts:2859`, `:2868`) mounts the resume card without leaving it below the fold. Note this is a double height change the RO catches twice: the composer *unmounts* (`:1306`, `.messages` grows) **and** the card *mounts* (`:1230`, `.messages-inner` grows).
3. **A long code block no longer jumps.** `collapseCodeBlocks()` shrinks a `<pre>` ~600px → ~30px in `ngAfterViewChecked` *after* today's pin — this is a guaranteed visible jump today and is the cheapest proof the fix works.
4. Toggling `chatPrefs.reasoningExpanded()` (flips every reasoning block at once) keeps the bottom pinned.
5. **The user can scroll up during active streaming and stay there.** The wheel escape must hold while deltas arrive every frame. This is the regression the fix itself could introduce.
6. **No regressions** in the hand-earned behaviours: multi-line draft doesn't jump up-then-down (hook #4 retained); attachment chips keep the latest turn visible; prepending older history preserves position; "Jump to latest · N new" works; Android keyboard keeps the bottom pinned.
7. `connect()`: existing spec `persistent-chat.service.spec.ts:660` (*"does not open SSE for ended threads — shows resume card instead"*) still passes — its `/messages` mock carries no status, so the meta fallback must survive. Specs at `:736`/`:781` (stale-connect races, synchronised on `aMetaRequested`) and `outbox.spec.ts:185` (redispatch after `load_history`) still pass.
8. `cd cockpit && npx vitest run` green; `ng build` passes the style budget **without a bump** ([[persistent_chat_component_style_budget]]).
9. ~~Net negative line count~~ — **explicitly not a criterion**; see §"What the research changed" #2.

## Verification plan

**Testing is a three-layer split, because jsdom cannot test this.** jsdom has no layout engine at all — `getBoundingClientRect()`, `offsetHeight`, `clientHeight`, `scrollHeight` all return **0**, and it does not implement `ResizeObserver` ([jsdom#3368](https://github.com/jsdom/jsdom/issues/3368), open; maintainer stance in [#2751](https://github.com/jsdom/jsdom/issues/2751): APIs that *"can't be simply replaced by a no-op or a static return value… we don't expose them and require the consumer to mock"*). **Do not add a polyfill** — both `resize-observer-polyfill` and `@juggle/resize-observer` bail on zero-size elements and silently never fire. It buys zero signal.

1. **Extract the decision, unit-test it pure.** `shouldPin(scrollTop, scrollHeight, clientHeight, autoScroll, isRestoring)` needs no DOM. Keep the `nearBottom` 80px threshold (`:2570`) here too. 80 sits squarely in the practitioner band: 70 (`use-stick-to-bottom`), 100 (Vercel), 200 (Element) — Virtuoso's 4px default is the outlier.
2. **Mock RO in `cockpit/src/test-setup.ts`** (26 lines today, **no RO stub** — the first spec touching the new code fails with `ReferenceError: ResizeObserver is not defined`). Capture the callback so specs can fire it and assert wiring/teardown (both targets observed; `disconnect()` on destroy). Note `ResizeObserver` has **no `takeRecords()`** — don't cargo-cult that from IntersectionObserver.
3. **Real geometry needs a real browser.** Walk criteria 1-6 on k3d (`https://localhost/`, thread `b35a74ef`) with Tilt running, driven by Playwright — the cockpit is **zoneless**, so poke component state via `ng.getComponent(el)` rather than expecting CD from synthetic events. Force criterion 1's race with DevTools throttling on `GET /api/persistent/threads/{id}`. Longer term, [Vitest 4 browser mode](https://vitest.dev/guide/browser/) is a small step (already on `vitest@^4.0.8` + Playwright; v21 ships Vitest as the default runner).

Per CLAUDE.md "Plan → Develop → Verify", all of this is local — no dev-cluster round trip.

## Sources

**Specs:** [Resize Observer](https://drafts.csswg.org/resize-observer/) · [HTML "update the rendering"](https://html.spec.whatwg.org/multipage/webappapis.html#update-the-rendering) (RO = step 16, rAF = 14, paint = 22) · [CSSOM-View scrolling](https://drafts.csswg.org/cssom-view/#scrolling) · [css-scroll-anchoring-1](https://drafts.csswg.org/css-scroll-anchoring-1/) · [css-scroll-snap-1 §Re-snapping](https://drafts.csswg.org/css-scroll-snap-1/#re-snap) · [css-flexbox-1 §order](https://drafts.csswg.org/css-flexbox/#order-property) · [css-viewport](https://drafts.csswg.org/css-viewport/)
**Browser:** [MDN ResizeObserver](https://developer.mozilla.org/en-US/docs/Web/API/ResizeObserver) · [MDN VisualViewport](https://developer.mozilla.org/en-US/docs/Web/API/VisualViewport) · [MDN overflow-anchor](https://developer.mozilla.org/en-US/docs/Web/CSS/overflow-anchor) · [WebKit 259770](https://bugs.webkit.org/show_bug.cgi?id=259770) (interactive-widget, unshipped) · [WebKit 219005](https://bugs.webkit.org/show_bug.cgi?id=219005) · [WebKit 243107](https://bugs.webkit.org/show_bug.cgi?id=243107) · [WebKit 245946](https://bugs.webkit.org/show_bug.cgi?id=245946) (RO/IO order, ~Safari 16.2) · [csswg-drafts#7745](https://github.com/w3c/csswg-drafts/issues/7745) · [caniuse interactive-widget](https://caniuse.com/mdn-html_elements_meta_name_viewport_interactive-widget)
**Prior art:** [Element/Matrix scrolling.md](https://github.com/element-hq/matrix-react-sdk/blob/develop/docs/scrolling.md) · [use-stick-to-bottom](https://github.com/stackblitz-labs/use-stick-to-bottom/blob/main/src/useStickToBottom.ts) · [Vercel ai-chatbot hook](https://github.com/vercel/ai-chatbot/blob/main/hooks/use-scroll-to-bottom.tsx) · [TanStack Virtual chat docs](https://tanstack.com/virtual/latest/docs/chat) · [react-virtuoso](https://virtuoso.dev/virtuoso-api/interfaces/VirtuosoProps/)
**Angular:** [afterRenderEffect](https://angular.dev/api/core/afterRenderEffect) · [after_render_effect.ts](https://github.com/angular/angular/blob/main/packages/core/src/render3/reactivity/after_render_effect.ts) · [zoneless guide](https://angular.dev/guide/zoneless) · [signals/effect guide](https://angular.dev/guide/signals/effect) · [queries guide](https://angular.dev/guide/components/queries) · [components#12932](https://github.com/angular/components/issues/12932)
**Testing:** [jsdom#3368](https://github.com/jsdom/jsdom/issues/3368) · [jsdom#2751](https://github.com/jsdom/jsdom/issues/2751) · [Vitest browser mode](https://vitest.dev/guide/browser/)

*Empirical claims marked "measured" were verified in real Chromium + Firefox against this component's actual CSS during the 2026-07-15 research pass. WebKit claims rest on WPT + WebKit source, not a live Safari. The Android keyboard measurement used a desktop Chromium ICB-shrink as a proxy, not an on-device OSK.*
