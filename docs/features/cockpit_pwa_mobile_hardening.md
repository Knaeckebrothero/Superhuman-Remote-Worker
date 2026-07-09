# Cockpit PWA & mobile hardening

**What this is:** the issue catalog + fix plan from a phone test session (2026-06-27,
Android — Brave browser + Chrome "install as app", against the dev cluster and the
private prod cockpit at `cockpit.srw.works`). Screenshots live in
`issues_with_mobile_brave_and_chrome/` at the repo root (untracked; move next to this
doc if we want them versioned). Codebase facts below were verified against `develop`
on 2026-07-09.

**Status:** pickup items 1–3 implemented + verified locally 2026-07-09 (uncommitted);
items 4–6 open. See "Implementation notes" below for what was verified and what
still needs an on-device check after deploy.

**Headline:** the PWA substrate is largely in place (manifest, maskable icons, app
shortcuts, Angular service worker with update/offline banners, `ngsw-bypass` on all
SSE streams). What's broken is the last mile: the Android install degrades to a
browser shortcut instead of a standalone app, several key screens have no responsive
CSS at phone width, and the service worker has no self-healing story — which matters
more on mobile, where there is no DevTools escape hatch.

---

## Current state (what already exists)

| Piece | Where | Notes |
|---|---|---|
| Manifest | `cockpit/public/manifest.webmanifest` | `display: standalone`, `start_url`/`scope` `/`, icons 72–512 incl. maskable 192/512 + monochrome SVG, shortcuts for Jobs/Create/Sessions |
| Service worker | `cockpit/ngsw-config.json`, registered in `app.config.ts:117-124` | prefetch app shell, lazy `/assets/**`, `freshness` cache for `/api/**` (1h, 5s timeout); `registerWhenStable:30000`, prod only |
| Update/offline UX | `cockpit/src/app/shell/pwa-banner/` | `VERSION_READY` → reload banner; `navigator.onLine` offline notice |
| SSE protection | `notification.service.ts:129`, `sudo.service.ts:155`, `api.service.ts:1160,1175`, `persistent-chat.service.ts:932-935` | per-URL `?ngsw-bypass=true` |
| Mobile layout | `core/services/viewport.service.ts` + `@media (max-width: 768px)` blocks | single responsive layout; sidebar becomes a slide-in drawer |
| Install-prompt strings | `src/assets/i18n/en.json:2254-2258` (`pwa.install.*`) | **defined but never wired up** — no `beforeinstallprompt` handling exists |

**Doc drift found on the way:** CLAUDE.md claims a "mobile-first layout (`simple/`)
with tab-based shell". No such directory exists (`views/` + `shell/` only). Fix
CLAUDE.md regardless of the rest of this plan.

---

## Issues

### 1. Android install falls back to a browser shortcut, not a standalone app

**Symptom** (`chrome_pwa_installed_view.jpg`): the "installed" app opens with a
Custom-Tab-style header — X button, page title "Cockpit", visible URL
`cockpit.srw.works`, ⋮ menu. A properly minted WebAPK opens chrome-less, titled
"SRW" (the manifest `short_name`). The title shown is the page `<title>`, i.e.
Chrome never used the manifest for this install.

**Causes, in likely order of impact:**

1. **Manifest served as `application/octet-stream`.** Verified live:
   `curl -I https://cockpit.srw.works/manifest.webmanifest` → 200 but
   `content-type: application/octet-stream`. The nginx image
   (`docker/Dockerfile.cockpit:39-55`) writes a minimal inline config and stock
   `nginx:alpine` `mime.types` has no `.webmanifest` mapping.
   **Fix:** add to the generated nginx conf:
   ```nginx
   types { application/manifest+json webmanifest; }
   ```
   (or a `location = /manifest.webmanifest { default_type application/manifest+json; }` block).
2. **SW registration is deferred up to 30s+** (`registerWhenStable:30000`,
   `app.config.ts:117-124`). Chrome decides WebAPK-vs-shortcut based on what's
   registered/controlling at install time; a user who installs shortly after first
   load races the SW.
   **Fix:** switch to `registrationStrategy: 'registerImmediately'` (or a short
   `registerWhenStable:5000`). The original 30s deferral was a boot-perf nicety;
   installability is worth more.
3. **`theme_color` mismatch:** manifest says `#9c1f2e`
   (`manifest.webmanifest:9`), index.html metas say `#9c2832`
   (`src/index.html:9`). Cosmetic, but unify (pick the index.html value, it's what
   users have seen).
4. **No install CTA.** Nothing captures `beforeinstallprompt`, so discovery relies
   on the browser menu. The i18n strings already exist (`pwa.install.*`).
   **Fix:** small service that captures the event + an "Install app" affordance
   (settings page and/or sidebar footer), gated on the event actually firing.

**Verification:** desktop Chrome DevTools → Application → Manifest shows no
installability warnings against prod; on Android, install → app opens standalone
(no URL bar), `chrome://webapks` lists the app with the manifest name/icons.
Locally: k3d serves the same nginx image, so the MIME fix is verifiable with
`curl -sI https://localhost/manifest.webmanifest` through Traefik.

### 2. Session header toolbar collides at phone width

**Symptom** (`Screenshot_…222953`, `…223152`): the Files / Git / IDE / Disconnect
row renders overlapping ("Fil●s Conne❌e●it") — buttons stack on top of each other.

**Current code:** header at `persistent-chat.component.ts:463-508`; the
≤768px block (`persistent-chat.component.scss:1941-1988`) wraps the header and
hides `.ctrl-label` text, but the connection-status chip and button cluster still
collide.

**Fix:** give the chat header a real mobile treatment instead of label-hiding:
keep Disconnect + one overflow menu (⋯) that folds Files / Git / IDE / settings /
citations; truncate the session title with ellipsis; status dot only (no
"Connecting…" text) at narrow width.

### 3. Token/context stats bar overflows off-screen

**Symptom** (`Screenshot_…223152`): the per-turn stats row reads
"`.0k OUTPUT 1.2k REASONING 770`" — the leading chip is clipped at the left edge,
and the CTX gauge crowds the rest.

**Current code:** `.usage-panel` (`persistent-chat.component.ts:1173-1198`) and the
top `.status-bar` (`:511-513`) have **zero** responsive CSS — none of the file's
`@media` blocks touch them.

**Fix:** at ≤768px collapse to a single compact line (e.g. `12.0k ▸ 2%` with the
gauge), full detail behind a tap; ensure `min-width: 0` / `overflow: hidden` on the
flex children so nothing clips off-canvas.

### 4. Admin Usage page has no responsive CSS at all

**Symptom** (`Screenshot_…211924`, `…211928`): the 7d/30d/90d window selector and
Off/10s/30s/1m refresh selector render as giant full-width stacked blocks; KPI
cards are comically oversized.

**Current code:** `views/admin/usage/admin-usage.component.ts` — segmented controls
at `:37-45`, windows `:718-720`, refresh options `:1001-1016`. `grep @media` in the
file: no hits. Same for `views/statistics/statistics.component.ts`.

**Fix:** add a ≤768px block: `.seg` groups stay horizontal at natural size
(`inline-flex`, don't stretch), KPI row becomes a 2-column grid, page controls wrap.
This is CSS-only.

### 5. Composer placeholder shows desktop keyboard hints

**Symptom:** "Type a message... (Enter to send, Shift+Enter for newline)" on a
phone, where those hints are wrong/noise.

**Current code:** `inputPlaceholder()` computed
(`persistent-chat.component.ts:1917-1927`) → `i18n/en.json:1135`.

**Fix:** mobile variant of the idle placeholder ("Type a message…") selected via
`viewport.isMobile()`; state variants (connecting/working/uploading) stay as-is.

### 6. No on-screen-keyboard handling

**Symptom class:** composer/keyboard interplay currently works by luck of the
flex layout. There is **no `visualViewport` usage anywhere in `src/`**, and exactly
one `env(safe-area-inset-*)` (`persistent-chat.component.scss:1976-1979`).
`index.html:7` lacks `viewport-fit=cover`, so safe-area insets are all zero on iOS
anyway. In standalone (installed) mode there is no browser chrome absorbing these
problems — this is where hidden-composer / mis-scrolled-chat bugs surface.

**Fix:**
- add `viewport-fit=cover` to the viewport meta + audit the (few) safe-area usages;
- add `interactive-widget=resizes-content` to the viewport meta (Chrome ≥108
  keyboard behavior) and/or a small `visualViewport` resize listener that keeps the
  composer pinned and the message list scrolled to bottom when the keyboard opens;
- verify on the installed app, not just the browser tab.

### 7. Service worker has no self-healing (and mobile has no escape hatch)

The known failure mode — SW serves a stale app shell that targets removed endpoints,
session UI wedges — is recoverable on desktop (DevTools → unregister SW) and fatal
on an installed phone app. Current gaps:

- **No `checkForUpdate()` anywhere.** Updates are only discovered on SW-default
  navigation checks; a long-lived installed app can run a stale shell for days.
  **Fix:** in `pwa-banner` (or a dedicated service), `checkForUpdate()` on an
  interval (~6h) **and on `visibilitychange`** — the natural "user re-opens the
  installed app" moment.
- **No `UNRECOVERABLE_STATE` handling.** If the SW cache is broken (hash mismatch
  after a partial deploy), Angular emits `unrecoverable` and we ignore it.
  **Fix:** subscribe and hard-reload (`location.reload()`), optionally after a toast.
- **`assets/env.js` is SW-cached.** It's runtime config (loaded in
  `index.html:29`) but matches the `/assets/**` lazy asset group
  (`ngsw-config.json:22-32`), so a redeploy that only changes env.js can serve the
  old config until the next SW version flip.
  **Fix:** exclude it from the asset group (`"!/assets/env.js"`) so it always hits
  the network, or move it to a dataGroup with `freshness`.
- **Per-URL `ngsw-bypass` is fragile** — every new streaming endpoint must remember
  the query param or it gets buffered by the `/api/**` freshness cache. Already
  audited as finding #3 in
  `docs/issues/session_reliability_investigation_index.md` (carve-out fix open
  there; the `/connection` handshake, binary downloads, and IDE proxy are still
  cached). Fixing that carve-out belongs to that doc; noted here because it is the
  main "wedged phone" risk.

### Seen in the screenshots, tracked elsewhere (out of scope here)

- **Duplicate user bubbles** ("Hello?" rendered twice, `Screenshot_…223152`) — the
  known epoch duplicate-render bug on session resume; diagnosed separately.
- **"Provisioning agent 6m33s+"** (`Screenshot_…211852/211902`) — session-attach /
  provisioning reliability, see `docs/issues/session_reliability_investigation_index.md`
  finding #1. Not a PWA problem, but it's the first thing a phone user sees.

---

## Bigger bets (separate design docs when picked up)

1. **Web push notifications.** SSE dies the moment the app is backgrounded — on a
   phone that's ~always. The natural mobile pattern for this product is "kick off a
   job, get pinged on `pending_review` / completion / sudo request". Needs: VAPID
   keys, subscription storage + endpoints on the orchestrator, a push handler in a
   custom SW extension alongside ngsw, notification preferences UI. This is the
   feature that makes the installed app genuinely useful rather than a bookmark.
2. **Bottom tab shell for phones.** The layout CLAUDE.md already imagines: a
   phone-width shell with Sessions / Jobs / Inbox tabs instead of the hamburger
   drawer. Decide whether it's a separate shell or a CSS mode of the existing one.

---

## Suggested pickup order

| # | Work | Size | Files | Status |
|---|---|---|---|---|
| 1 | nginx MIME fix + theme_color unify + SW `registerImmediately` | XS | `docker/Dockerfile.cockpit`, `manifest.webmanifest`, `app.config.ts` | ✅ 2026-07-09 |
| 2 | Responsive triage: admin-usage (#4), usage panel (#3), chat header (#2), placeholder (#5) | S–M | `admin-usage.component.ts`, `persistent-chat.component.{ts,scss}`, `en.json`, `de-DE.json` | ✅ 2026-07-09 |
| 3 | SW self-healing (#7): checkForUpdate + unrecoverable + env.js carve-out | S | `pwa-banner.component.ts`, `ngsw-config.json` | ✅ 2026-07-09 |
| 4 | Install CTA via `beforeinstallprompt` (#1.4) | S | new service + settings/sidebar hook | open |
| 5 | Keyboard/safe-area pass (#6) | M | `index.html`, chat scss | ✅ 2026-07-09 round 2 (keyboard; `viewport-fit=cover` still open) |
| 6 | Push notifications / tab shell | L | separate docs | open |

## Implementation notes (2026-07-09, items 1–3)

- **Item 1**: nginx `location = /manifest.webmanifest { default_type application/manifest+json; }`
  in the Dockerfile-inline conf — verified against a live `nginx:alpine` container
  (200 + correct type, SPA fallback intact). Manifest `theme_color` → `#9c2832`.
  `registrationStrategy: 'registerImmediately'`.
- **Item 2**: chat header on mobile folds Settings/Citations/Files/Git/IDE into an
  `app-menu` overflow (kebab, same idiom as job-list), Disconnect stays; status
  text label + decorative header icon hidden ≤768px; status-bar scrolls instead of
  spilling; usage panel collapses to Output + CTX gauge (`usage-chip--input/--reasoning`
  hidden); mobile placeholder `chat.input.defaultMobile` (en + de-DE); admin-usage
  got a full ≤768px block (controls unstretch, tables + bar chart scroll in place).
  Two latent bugs found while verifying at 300px: flex `min-width:auto` overflow via
  unbreakable model slugs in the chart legend (fixed with `min-width: 0` on
  `.ts-side`/`.legend-item`) and the throughput bar chart's nowrap date labels
  (fixed with `overflow-x: auto` + 34px min bars).
- **Item 3**: `pwa-banner` now handles `unrecoverable` (console.warn + reload) and
  polls `checkForUpdate()` every 6h + on `visibilitychange` (5-min throttle);
  `env.js` excluded from the SW asset group and moved to a `runtime-config`
  freshness dataGroup (1d fallback cache, 3s timeout) — verified in the generated
  `dist/.../ngsw.json`.
- **Verified**: 843/843 vitest, production build green, nginx container probe,
  Playwright at 300px CSS width on k3d (admin/usage contained, chat header +
  overflow menu + placeholder + live usage panel during a real gemma-4-moe turn).
- **Still needs on-device confirmation after deploy** (SW is prod-only; Tilt runs
  `ng serve`): standalone install on Android (`chrome://webapks`), update banner
  within one `visibilitychange` after a redeploy, and the theme-color of the
  installed window.

**Acceptance for items 1–5:** on an Android phone against dev — install from
Chrome yields a standalone window (no URL bar, `chrome://webapks` entry); session
header, stats bar, and Usage page render un-clipped at 360–412px width; redeploy
while the installed app is open surfaces the update banner within one
`visibilitychange`; composer stays visible with the keyboard open. Locally,
layout items are verifiable in ng serve at 375px + the MIME fix via
`curl -sI https://localhost/manifest.webmanifest` on k3d.

## Round 2 (2026-07-09, after on-device test of round 1)

On-device result of round 1: standalone install **works** (round-1 deploy
`sha-9eb6f7b`). New issues from the second batch of phone screenshots
(`issues_with_mobile_brave_and_chrome/`, 13:2x set; the 11:23 jobs shot predates
the deploy):

1. **Composer slides under the keyboard.** Chrome ≥108 defaults to
   `interactive-widget=resizes-visual`: the keyboard shrinks only the *visual*
   viewport, the 100dvh flex column keeps its height, and the browser
   scroll-hacks the focused textarea alone into view — the attach/send row
   stays under the keyboard. **Fix:** `interactive-widget=resizes-content` on
   the viewport meta (`index.html:12`) so the layout viewport shrinks and the
   column re-lays above the keyboard, plus a `window` resize listener in
   `persistent-chat` that re-pins the transcript to bottom while following
   (`onViewportResize`). `viewport-fit=cover` (iOS safe-areas) deliberately
   NOT added yet — needs its own inset audit.
2. **Permanent scrollbar inside the textbox.** Two causes: (a) the app styles
   scrollbars globally (`styles.scss`), and styled scrollbars on Android are
   permanent, not overlay — any 1px overflow shows a track; (b) the empty
   textarea overflows its 56px min-height whenever a long placeholder ("Type
   your message while the session starts...") wraps, and a capped long draft
   (180px max-height) legitimately scrolls. **Fix:** an effect re-runs
   `autoResizeInput()` when `inputPlaceholder()` changes (Blink's scrollHeight
   includes the placeholder — verified live, box grows to 74px, zero
   overflow); `.chat-input` gets `scrollbar-width: none` ≤768px; max-height
   becomes `min(180px, 30dvh)` on mobile so a big draft can't eat the
   keyboard-shrunk viewport (measured 177.6px cap, upward growth: composer
   top 465→344 with bottom pinned).
3. **Markdown headings render at UA scale in chat.** No h1–h6 rules existed
   for message markdown, so `# Title` rendered at 2em = 30px — billboard-sized
   at phone width (screenshot: "Role responsibilities in the improved loop").
   **Fix:** em-based heading scale under `.message-body ::ng-deep`
   (h1 1.3em … h4+ 1em), all widths, scales with the per-device text-size pref.
4. **Touch targets + sizing.** Composer `.ctrl` buttons were 26px tall and
   `.send` 30px — both under the 44px floor. Mobile block now makes ctrl
   40×40 and send 44×44 (app-button already floors 44px on mobile). Jobs
   table "ACTIONS" header clipped mid-word in its 14% column → hidden via
   `font-size: 0` (needs the full `.job-table th.col-actions` selector —
   emulated encapsulation stamps `[_ngcontent]` on every part, so a shorter
   selector loses specificity to the base `.job-table th`). Global mobile
   scrollbar tracks made transparent (thumb stays) so panes stop growing a
   themed track column.
5. Drive-by: removed the unused `DecimalPipe` import in `persistent-chat`
   (pre-existing NG8113 warning on every rebuild).

**Round-2 verification:** 843/843 vitest, prod build green, live k3d Playwright
at 288 CSS px (browser zoom made it extra narrow): placeholder-fit, upward
growth, hidden scrollbar, 40/44px targets, headings ~16px, jobs header clean,
no horizontal overflow anywhere. **Needs on-device check after deploy:** the
actual keyboard interplay (resizes-content only observable with a real IME) —
composer above keyboard while typing, transcript re-pin on keyboard open/close.

**Still open after round 2:** item 4 (install CTA), `viewport-fit=cover` +
safe-area audit (iOS), push notifications / tab shell (item 6). The 11:23
screenshot's hamburger-only top row on list pages wastes a full row at phone
width — that's the tab-shell bet, not patched piecemeal.

## Round 3 (2026-07-09, composer semantics after round-2 on-device test)

Round 2 confirmed working on-device. Remaining reports: send button dead while
Enter works; Enter should mean newline on phones; token counters overflow.

1. **Dead send button (the actual bug, all platforms)**: `canSend` was a
   `computed()` reading the plain `inputText` ngModel field — typing never
   invalidated it, so on an otherwise-idle session it stayed cached at the
   empty-text value and the button stayed `[disabled]`. Enter worked because
   `send()` checks the field directly; on desktop unrelated signal churn
   (streaming states) kept re-evaluating it, which is why it looked
   mobile-only. Fix: plain method delegating to the exported pure helper
   `canSendMessage()` — event bindings schedule CD, so it re-reads the field
   every keystroke. Regression-guarded in the spec.
2. **Enter = newline on touch devices** (`shouldSendOnEnter()` helper):
   physical keyboards keep Enter-to-send / Shift+Enter-newline; on
   UA-detected mobile (`DeviceCapabilitiesService.isMobile`, not viewport
   width — a narrow desktop window still has a real keyboard) Enter falls
   through as a newline and the send button is the send affordance.
3. **Mic ↔ send morph** (`isMicMode()` helper): standalone mic ctrl removed;
   the round action button shows the mic while the composer is empty (no
   text/attachments, no turn in flight) and flips to send on the first
   keystroke — standard messenger behavior, desktop + mobile. Recording
   strip/flow unchanged. Tooltip fixed ("Record voice message" — it was
   never hold-to-record).
4. **Keyboard stays open on send**: `(pointerdown)="$event.preventDefault()"`
   on the action button suppresses the focus steal, so tapping send/mic no
   longer blurs the textarea → no keyboard dismiss → no resizes-content
   reflow mid-tap. Verified: focus remains on the textarea after a click-send.
5. **Usage panel on phones**: even the round-2 OUTPUT+CTX line pressed against
   the viewport edges. ≤768px now hides `.usage-tokens` entirely and the CTX
   gauge (the one actionable number, compaction-anchored) flexes across the
   freed width. Desktop keeps all three chips.
6. **Same-page tap closes the mobile drawer**: the drawer only collapsed via a
   `NavigationEnd` subscription, and the router emits nothing for a same-URL
   navigation — so the intuitive dismiss gesture (tap the page you're on) did
   nothing and users had to find the chevron or the backdrop. A click handler
   delegated on the sidebar root now collapses the drawer on any `a[href]` tap
   when mobile; buttons (bell, logout, collapse) are exempt. Desktop rail
   unaffected (viewport guard).
7. **Mobile drawer sizing**: the 200px/13px desktop rail read cramped as a
   phone overlay. ≤768px the drawer is `min(300px, 84vw)` (backdrop stays
   tappable), nav links 15px/44px with bigger icons, brand and footer scaled
   to match. Desktop keeps 200px/13px.

**Round-3 verification:** 856/856 vitest (+13 helper specs), prod build green,
live k3d Playwright at 288 CSS px: restored-draft enables send; clearing →
mic morph; first keystroke → enabled send (the bug); `isMobileDevice=true` +
Enter → newline only (nothing sent, box grew 56→74px); click-send delivered
end-to-end (agent replied) with focus retained; usage row = full-width gauge
at 77% warn with zero horizontal overflow; 1280px desktop shows all chips +
64px gauge unchanged. On-device: confirm keyboard stays open after send.
