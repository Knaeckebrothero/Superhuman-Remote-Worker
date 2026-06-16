---
tags:
  - feature
  - cockpit
  - design
  - readability
  - layout
  - persistent-sessions
aliases:
  - chat reading width
  - session text line length
  - chat column max-width
  - cockpit content width
  - page width tokens
related:
  - "[[settings_design]]"
  - "[[session_turn_rendering]]"
  - "[[session_header_streamline]]"
  - "[[session_narration]]"
---

# Cockpit Content Width & Readability

**Session:** 2026-06-15. Two related pieces of work; one shipped, one proposed.

| Part | Scope | Status |
|---|---|---|
| **A — Page container widths** | Forms / lists / admin / settings pages | ✅ **SHIPPED** — committed, verified live (§2) |
| **B — Session chat reading width** | The persistent-chat transcript | ✅ **IMPLEMENTED (2026-06-16)** — working tree, **uncommitted**; compile (27/27 spec) + layout/CPL verified via mock; live-cluster §6 check still pending (cluster was down) |

**Resume in one line:** Both parts are implemented. **Part B open items:** confirm
on the live stack with real Inter via the §6 script (target the ~90 CPL band),
then commit. Settled decisions + the corrected CPL math are in §3.4/§5.

> **2026-06-16 correction.** The original §3.4 recommendation (760px) assumed
> assistant text loses ~100px to padding. It doesn't — assistant bubbles are
> *flush* (`padding: 4px 0`), so the rendered line ≈ column − 40px (avatar 30 +
> gap 10) only. Measured, **760px → ~102 CPL** (over the 90–100 / 75–95 targets).
> Shipped value is **700px → ~94 CPL** (text ≈ 660px @ 15px Inter). It's one
> token — nudge `--width-chat-content` to taste.

---

## 1. Background — the principle behind both parts

Content width should be **matched to the content type**, not maximised or
copied blindly:

- **App content** (forms, card grids, tables, settings): a centered column
  ~1140–1280px (page ceiling ~1440px) is the modern norm. Full-bleed forms hurt
  usability (huge label→field eye travel; inputs that imply "type a lot").
- **Prose** (reading text, chat): **50–75 characters per line**, 66 ideal,
  **80 max** (WCAG 1.4.8) ≈ `~70ch`. Narrower than app content *on purpose*.
- **Data-dense** (big tables, IDE-like, inline editing): full-width is correct.
- "Wasted" side margins are **intentional whitespace** (focus, grouping, reduced
  cognitive load), which is why every major product (Claude, ChatGPT, Gemini,
  Linear, Notion, GitHub, Stripe) caps content width. The fix for "feels empty"
  is *density / multi-column*, not a wider single column.

Sources: [Baymard – line length](https://baymard.com/blog/line-length-readability),
[Baymard – form fields](https://baymard.com/blog/form-field-usability-matching-user-expectations),
[NN/g – web forms](https://www.nngroup.com/articles/web-form-design/),
[IxDF – white space](https://ixdf.org/literature/article/the-power-of-white-space),
[UX Planet – whitespace](https://uxplanet.org/the-power-of-whitespace-a1a95e45f82b),
[boxed vs full-width dashboards](https://www.bootstrapdash.com/blog/boxed-or-full-width-layout),
[content max-width 2025](https://www.allianceinteractive.com/blog/website-dimensions/).

---

## 2. Part A — Page container width unification ✅ SHIPPED

### 2.1 Problem
Each page hardcoded its own content `max-width` (800 / 900 / 1000 / 1100 /
1200px) with `margin: 0 auto`. The narrowest (800px — New Session, Settings,
Sessions) used <50% of a ~2000px monitor and read as "made for a phone." Values
were inconsistent with no shared token. (The app shell `app.ts` gives every page
full width; the caps were purely per-component.)

### 2.2 Fix — two CSS-custom-property tokens + swap consumers
CSS variables (not Sass scalars) because `angular.json` sets
`inlineStyleLanguage: "scss"` but the inline-`styles` components don't `@use`
the Sass partials — only a CSS var reaches both inline-styled and `.scss`-file
components.

```scss
// cockpit/src/styles/_root-tokens.scss  (primitive)   — lines ~52–53
--width-content: 1280px;
--width-content-wide: 1440px;

// cockpit/src/styles/_semantic-tokens.scss  (role)     — lines ~26–27
--content-max-width:      var(--width-content);       // forms, settings, lists, grids
--content-max-width-wide: var(--width-content-wide);  // table/grid-dense pages
```

### 2.3 Consumers swapped (10)
**Standard `var(--content-max-width)` (1280px):**
`session-create.component.ts:176`, `settings/settings.component.ts:1033`,
`sessions/sessions-page.component.ts:228`, `projects/project-list.component.ts:124`,
`admin/users/admin-users.component.ts:172`, `admin/config/admin-config.component.ts:276`,
`admin/llm/admin-llm.component.ts:67`, `settings/api-keys/api-keys-page.component.ts:239`,
`automations/automations-page.component.scss:11`.
**Wide `var(--content-max-width-wide)` (1440px):** `project-detail/project-detail.component.ts:861`.

Mobile `@media (max-width: 768px)` → `max-width: 100%` overrides left intact.
**Left full-width on purpose** (already correct, data-dense / container-query):
Jobs list, Data Sources, Create (job), Inbox. Chat = Part B.

### 2.4 Verification (done this session)
- `npx sass src/styles.scss` compiles clean (exit 0); tokens emit with the
  primitive→role cascade.
- Confirmed **live** on the running cockpit (Tilt `ng serve`): served
  `http://127.0.0.1:4000/styles.css` contained `--width-content: 1280px` and
  `--content-max-width: var(--width-content)`.
- New Session form measured ~800→1280px; expert grid ~4→6 columns.

### 2.5 Commit state
All 12 files are **committed and clean** in the working tree. They landed in
`76771eeb` (bundled under an unrelated commit message — "feat(memory): overhaul…"
— because of parallel bulk-commit activity in this repo; commit message does NOT
reflect the width work). The doc itself first landed in `bf50bb00`. Per local
refs these are already on `origin/develop`. **No pending Part-A changes.**

---

## 3. Part B — Session chat reading width 📋 PROPOSED (open work)

### 3.1 Problem
The persistent-chat transcript has **no readability cap**. `.messages` (scroll
container) has no `max-width` and isn't centered; the only control is
`.message { max-width: 90% }` of the full-width pane, at a small **13px** font.
On a wide monitor, long answers run nearly wall-to-wall. Worse failure mode than
the form had, because chat is the app's primary *reading* surface. The composer
below is already centered at 880px — so the transcript sprawls wider than its own
input box (visibly inconsistent).

### 3.2 Measured evidence (live, dev cluster, 2026-06-15)
Session `e9699503` ("Explaining Why the Sky Appears Blue", 11 turns — good prose
+ a markdown table), measured in-browser at **1920×1080** (reporter's monitor is
~2000px, i.e. slightly worse):

| Metric | Before | Preview (820px col + 15px, injected then reverted) |
|---|---|---|
| `.messages` pane width | 1710px (`max-width: none`) | — |
| Assistant text block width | **1470px** | 688px |
| Body font | 13px (`line-height 19.5px`) | 15px |
| Avg glyph advance (Inter, measured) | 6.1px @13px | ~7.0px @15px |
| **Characters per line** | **≈ 241** | **≈ 98** |

241 CPL ≈ 3× the WCAG hard cap (80) and ~3.6× the 66 ideal. The preview (a
centered column + larger font) is a 2.5× improvement and code/table stayed
contained.

### 3.3 Root cause (code) — `persistent-chat.component.scss`
- `.chat-container` (~L12) — full width, no cap.
- `.messages` (~L289) — `flex:1; overflow-y:auto; padding:16px;` flex column,
  `gap:16px`. **No max-width, not centered.** ← the miss.
- `.message` (~L511) — only `max-width: 90%`; assistant `align-self:flex-start`,
  user `flex-end`.
- `.message-body` (~L556) — `font-size:13px; line-height:1.5`. Assistant variant
  (~L574) overrides only padding/colour → all prose is 13px.
- `.composer` (~L1428) — already centered `max-width:880px; margin:0 auto`.
- Tool surfaces already self-limit: `.message.tool-only` (~L651) `max-width:none`
  (full-bleed, intentional); `.tool-card` (~L753) `max-width: min(720px,100%)`.

### 3.4 Design

**New token** (prose wants a *narrower* cap than app content — keep separate):
```scss
// _root-tokens.scss (primitive)
--width-chat-content: 700px;   // session reading column
// _semantic-tokens.scss (role)
--chat-content-width: var(--width-chat-content);
```

**Centering — inner wrapper** (keeps scrollbar at window edge). Keep `.messages`
full-width (padding + overflow); wrap the message list in a centered column.
**Requires a small template edit** — wrap the message `@for` in
`<div class="messages-inner">` in `persistent-chat.component.ts`:
```scss
.messages-inner {
  width: 100%;
  max-width: var(--chat-content-width);
  margin-inline: auto;
  display: flex; flex-direction: column; gap: 16px;  /* moved off .messages */
}
```
`.message` `max-width: 90% → 100%` (wrapper now bounds it); keep `align-self`.

**Width / font targets** (avg glyph 6.1px@13px → ~7.04px@15px; assistant text ≈
`column − 40px` = 30px avatar + 10px gap — **assistant bubbles are flush, no side
padding**, so the rendered line is wider than a padded estimate would suggest.
Verified by mock + canvas measure, 2026-06-16):

| Option | `--chat-content-width` | Body font | ≈ text px | ≈ CPL (15px Inter) | Feel |
|---|---|---|---|---|---|
| Tight | 660px | 15px | ~620 | ~88 | Closest to ideal; tighter with tables |
| Snug | 680px | 15px | ~640 | ~91 | A hair under the rec |
| **Comfortable (shipped)** | **700px** | **15px** | ~660 | **~94** | Matches Claude/ChatGPT/Gemini |
| Roomy | 720px | 15px | ~680 | ~97 | A touch wide |
| (doc's old rec) | 760px | 15px | ~720 | ~102 | Over target — see correction above |

Production chat UIs run ~90–100 CPL (trade strict 66 for room for code/lists/
tables). **Shipped: 700px + 15px (~94 CPL).** The 13→15px bump is a coordinated
but separable lever (13px is small for primary reading; bigger font also lowers
CPL). Absolute CPL must be confirmed on the live stack (real Inter) via §6.

**Keep wide / don't cap:** fenced code & `pre` → ensure `overflow-x:auto`
(scroll inside the column, don't widen it); markdown tables → block + overflow-x;
`.tool-card` (720px, leave); `.message.tool-only` → §5 decision.

**Composer:** point `.composer` at the same token so input aligns under the
transcript (currently 880px; minor/reversible — §5).

### 3.5 Implementation sketch (files)
1. `_root-tokens.scss` — add `--width-chat-content: 760px`.
2. `_semantic-tokens.scss` — add `--chat-content-width: var(--width-chat-content)`.
3. `persistent-chat.component.ts` (template) — wrap message loop in
   `<div class="messages-inner">`.
4. `persistent-chat.component.scss`:
   - `.messages` — drop flex/gap (move to `.messages-inner`); keep padding + overflow.
   - add `.messages-inner` (§3.4).
   - `.message` — `max-width: 90% → 100%`.
   - `.message-body` — `font-size 13px → 15px`, `line-height 1.5 → 1.6`.
   - verify/add `pre`/`code`/table `overflow-x: auto`.
   - `.composer` — `880px → var(--chat-content-width)` (if §5 agrees).
   - mobile `@media (max-width:768px)` (~L2207) — confirm column collapses to full width.

Effort ~1–2h, all cockpit/CSS + one template wrap. Tilt `ng serve` HMR (~5s).

### 3.6 Edge cases / risks
Code blocks (scroll not squeeze), markdown tables (the sky session has one),
user bubbles (right-align within narrower column — fine), tool-only full-bleed
(§5), streaming/narration (same containers — unaffected), mobile (no regression),
font bump → slightly fewer messages per screen (acceptable; only prose changes,
tool/debug text keep their 11px).

---

## 4. (reserved)

---

## 5. Decisions (Part B) — RESOLVED 2026-06-16
1. **Target width** — **700px** (shipped). Was 760; corrected down after measuring
   that flush assistant text → ~102 CPL at 760 (see §3.4 correction). 700 ≈ 94 CPL.
2. **Font bump 13→15px** — **done** (`.message-body` 13→15px, line-height 1.5→1.6).
3. **Composer** — **aligned** to `var(--chat-content-width)` (was 880px), so the
   input sits under the transcript at the same width.
4. **`.message.tool-only` full-bleed** — **moot**. The class isn't applied in the
   current template (tools render inside the assistant `.turn-bubble`; `.tool-card`
   self-caps at 720px ≈ the column). The `.message.tool-only` SCSS is vestigial and
   left as-is. Nothing renders wall-to-wall, so no breakout was needed.
5. **Empty-state** (`.empty-inner`, 850px) — **aligned by consequence**: it now sits
   inside `.messages-inner`, so the suggestion grid is capped to the 700 column
   instead of 850. Cosmetic, more consistent; revert by moving `.empty-state`
   outside the wrapper if the grid feels cramped.

**How it was built** (differs slightly from §3.5): the wrapper encloses the whole
message *flow* (the `@for…@empty` plus the transient mile/compaction/end-marker/
resume/reconnect cards), not just the `@for`, so those cards align in the column
too. `.jump-latest` is kept OUTSIDE the wrapper (it's `position:sticky;
align-self:center` and must stay a direct child of the `.messages` flex column).
`.messages` keeps `display:flex;flex-direction:column` (so `.messages-inner{flex:1}`
lets the empty-state center vertically); only its `gap:16px` moved to the wrapper.
Markdown tables changed `width:100%` → `display:block; width:max-content;
max-width:100%; overflow-x:auto` so wide tables scroll inside the column. `pre`
already had `overflow-x:auto`.

---

## 6. Measurement & verification tooling (reusable)

**Access:** `https://localhost/` (k3d + Tilt running), log in **test / test**
(Keycloak). Good measurement target: session **`e9699503`** (sky-blue, prose +
table). Set a wide viewport (the reporter is ~2000px) before measuring.

**In-browser CPL measurement** (Playwright `browser_evaluate`, or paste in
DevTools console) — returns pane width, widest assistant block, font, and
canvas-measured characters-per-line:
```js
() => {
  const out = { viewport: innerWidth + 'x' + innerHeight };
  const m = document.querySelector('.messages');
  if (m) { out.messages_clientWidth = Math.round(m.clientWidth);
           out.messages_maxWidth = getComputedStyle(m).maxWidth; }
  const bodies = [...document.querySelectorAll('.message-assistant .message-body')];
  const widest = bodies.reduce((a,b)=> b.getBoundingClientRect().width > (a?.getBoundingClientRect().width||0) ? b : a, null);
  if (widest) {
    const cs = getComputedStyle(widest);
    const para = [...widest.querySelectorAll('p,li,div')].find(e => (e.textContent||'').trim().length > 80) || widest;
    const w = para.getBoundingClientRect().width;
    const sample = (para.textContent||'').trim().slice(0,400);
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.font = cs.fontSize + ' ' + cs.fontFamily;
    Object.assign(out, {
      assistant_body_width_px: Math.round(widest.getBoundingClientRect().width),
      font: cs.fontSize, line_height: cs.lineHeight,
      paragraph_width_px: Math.round(w),
      chars_per_line: Math.round(w / (ctx.measureText(sample).width / sample.length)),
    });
  }
  return out;
}
```
**Preview without committing:** inject a `<style>` capping `.messages`
(`max-width` + `margin-inline:auto`) and `.message-body{font-size:15px}`, screenshot,
then remove the style element. (Used for the §3.2 preview row.)

**Confirm a CSS change is live on the cluster** (Tilt syncs `cockpit/src/**`,
`ng serve` HMR ~5s):
```bash
kubectl --context=k3d-srw -n srw exec deploy/srw-cockpit -- \
  sh -c "wget -qO- http://127.0.0.1:4000/styles.css | grep -oE '\-\-chat-content-width[^;]*;|\-\-content-max-width[^;]*;'"
```
**Gotcha:** use `127.0.0.1`, not `localhost`, inside the pod — `localhost`
resolves to IPv6 `::1` but `ng serve` binds IPv4 `0.0.0.0` → empty response.
(Cost me two false "it's not live" readings this session.)

---

## 7. Resume checklist (Part B, cold start)
1. Re-read §1 + §3. Confirm Part A still live (§2.4 grep, swap token name).
2. **Settle §5 decisions** with the user (mainly width + font bump).
3. Implement §3.5 (tokens → template wrap → scss; remember `overflow-x:auto` on
   code/tables).
4. Verify with §6 script on `e9699503` at ~1920px **and** ~2560px → target
   **75–95 CPL**, no horizontal page scroll, code/table scroll (not squeeze).
5. Smoke-sweep: live streaming turn, tool cards, user bubbles, narration, resume
   card, mobile (<768px).
6. Commit only after local pass (CLAUDE.md Plan→Develop→Verify). NB: this repo
   has heavy parallel bulk-commit activity — check `git status` before committing
   so unrelated working-tree changes don't get swept in.

---

## 8. Out of scope
Per-user width/font preference (token makes it trivial later); Builder/job views
(covered by Part A tokens); markdown/typography restyle beyond size + line-height.
