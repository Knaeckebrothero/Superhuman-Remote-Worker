---
tags:
  - feature
  - cockpit
  - design
  - readability
  - persistent-sessions
aliases:
  - chat reading width
  - session text line length
  - chat column max-width
related:
  - "[[settings_design]]"
  - "[[session_turn_rendering]]"
  - "[[session_header_streamline]]"
  - "[[session_narration]]"
---

# Session Chat Readability — reading-column width & line length

**Status:** **Proposal — live-measured 2026-06-15, not yet implemented.**
Scope is the persistent-session chat view (`persistent-chat`). This is the
chat-specific companion to the page-container width unification shipped the
same day (the `--content-max-width` / `--content-max-width-wide` tokens in
`_root-tokens.scss` / `_semantic-tokens.scss`), which covered forms/lists/admin
pages but deliberately **excluded** the chat reading column. This doc closes
that gap.

---

## 1. Problem

The assistant/user message text in a session has **no readability cap**. The
scroll container `.messages` has no `max-width` and is not centered, so on a
wide monitor the prose runs nearly wall-to-wall at a small 13px font. Long
answers become very hard to read (eye has to track enormous lines), which is
the opposite failure mode from the New Session form that was just widened — and
a worse one, because chat is the primary *reading* surface in the app.

This was not obvious from eyeballing because short messages don't fill the
width; it only bites on long answers (explanations, tables, lists), which are
exactly the high-value content.

### 1.1 Measured evidence (live, dev cluster via Tilt)

Session `e9699503` ("Explaining Why the Sky Appears Blue", 11 turns), measured
in-browser at a **1920×1080** viewport (the reporter's monitor is ~2000px, i.e.
slightly worse):

| Metric | Measured |
|---|---|
| Chat pane `.messages` clientWidth | **1710px** (`max-width: none`) |
| Assistant text block rendered width | **1470px** |
| `.message` wrapper cap | `max-width: 90%` → 1510px |
| Body font size | **13px** (`line-height: 19.5px`) |
| Avg glyph advance (Inter, measured) | 6.1px |
| **Characters per line** | **≈ 241** |

The readability target is **50–75 CPL** (66 ideal), **80 max** per WCAG 1.4.8.
At ~241 CPL the chat is ~3× over the hard cap. See the design discussion and
sources in the conversation that produced this doc; key references:
[Baymard – line length](https://baymard.com/blog/line-length-readability),
[NN/g – web forms](https://www.nngroup.com/articles/web-form-design/),
[IxDF – white space](https://ixdf.org/literature/article/the-power-of-white-space).

### 1.2 Live preview of the fix (same session, injected then reverted)

Centered column at 820px + 15px font:

| Metric | Before | Preview |
|---|---|---|
| Text block width | 1470px | **688px** |
| Font size | 13px | **15px** |
| **Characters per line** | **241** | **≈ 98** |

A centered ~760–820px column with a 15px body reads like a document instead of
a billboard; code blocks and the markdown table stayed contained. 98 CPL is
still above the textbook ideal but matches what production chat UIs actually
ship (see §4.3) — and is a 2.5× improvement.

---

## 2. Root cause (code)

All in `cockpit/src/app/views/persistent-chat/persistent-chat.component.scss`:

- `.chat-container` (≈ L12) — full width, no cap.
- `.messages` (≈ L289) — `flex: 1; overflow-y: auto; padding: 16px;` flex column
  with `gap: 16px`. **No `max-width`, not centered.** This is the miss.
- `.message` (≈ L511) — the only width control is `max-width: 90%` of the
  full-width container. Assistant `align-self: flex-start`, user `flex-end`.
- `.message-body` (≈ L556) — `font-size: 13px; line-height: 1.5;`. Assistant
  variant (L574) only overrides padding/colour, so all prose is 13px.
- For contrast, the **composer is already centered**: `.composer` (≈ L1428)
  `max-width: 880px; margin: 0 auto;`. So the input box is constrained but the
  transcript above it is not — visibly inconsistent.
- Tool surfaces already self-limit: `.message.tool-only` (≈ L651)
  `max-width: none` (intentional full-bleed); `.tool-card` (≈ L753)
  `max-width: min(720px, 100%)`.

---

## 3. Goal & acceptance criteria

1. Assistant/user **prose** renders at a comfortable line length on wide
   screens — target band **75–95 CPL** (see §4.3 for the width/CPL math).
2. **Code blocks, tables, and tool output keep their width** (or scroll
   horizontally) — the prose cap must not strangle monospace/structured content.
3. The transcript column is **centered** and visually aligns with the composer.
4. One source of truth (a token), themeable, mirroring the page-width work.
5. Mobile (<768px) stays effectively full-width (no regression).

---

## 4. Design

### 4.1 Token (mirror the page-width work)

Chat prose wants a *narrower* cap than app content (`--content-max-width: 1280px`),
because line length, not screen real-estate, governs reading text. Add a
dedicated token rather than reusing the page one:

```scss
// _root-tokens.scss  (primitive)
--width-chat-content: 760px;   // reading column for session prose

// _semantic-tokens.scss  (role)
--chat-content-width: var(--width-chat-content);
```

This keeps the "match the cap to the content type" principle explicit: app
pages = 1280/1440, chat prose = 760, and it's tunable in one place.

### 4.2 Centering approach — inner wrapper (scrollbar stays at window edge)

`.messages` is the scroll container; capping it directly would float the
scrollbar in the middle. Instead keep `.messages` full-width (padding +
`overflow-y`) and wrap the message list in a centered inner column:

```scss
.messages-inner {
  width: 100%;
  max-width: var(--chat-content-width);
  margin-inline: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;            /* move the gap here from .messages */
}
```

Requires a **small template change**: wrap the message `@for` block in
`<div class="messages-inner">` (in `persistent-chat.component.ts`/`.html`).
`.message { max-width: 90% }` → `max-width: 100%` (the wrapper now bounds it);
keep `align-self` so user bubbles right-align and assistant text left-aligns
within the column (ChatGPT/Claude pattern).

### 4.3 Width / font targets (with measured CPL)

Measured avg glyph advance: 6.1px @13px → ~7.0px @15px. Assistant text width ≈
`column − ~100px` (16px padding ×2 + 30px avatar + 10px gap). So:

| Option | Column (`--chat-content-width`) | Body font | ≈ text width | ≈ CPL | Feel |
|---|---|---|---|---|---|
| Tight (readability-strict) | 660px | 15px | ~560px | **~80** | Closest to 66–75 ideal; can feel cramped with tables |
| **Comfortable (recommended)** | **760px** | **15px** | ~660px | **~90** | Balanced; matches Claude/ChatGPT/Gemini |
| Roomy | 820px | 15px | ~690px | ~98 | Preview shown; a touch wide |

Production chat UIs (Claude, ChatGPT, Gemini) effectively run ~90–100 CPL —
they trade the strict 66-char ideal for room to fit code/lists/tables. **760px
+ 15px** lands in that band while being a 2.5× improvement over today.

**Font bump (13→15px) is a coordinated but separable lever.** 13px is small for
primary reading content; bumping it both improves legibility and lowers CPL at a
given width. If we keep 13px, the column would need to be ~560px to hit ~80 CPL,
which is too narrow once a table appears. Recommendation: bump to 15px.

### 4.4 What stays wide (do NOT cap)

- **Fenced code / `pre`** inside prose → ensure `overflow-x: auto` so wide code
  scrolls *within* the column instead of forcing it wider. (Verify current rule.)
- **Markdown tables** → `display: block; overflow-x: auto` wrapper, same idea.
- **`.tool-card` / tool output** → already capped at 720px; leave as-is.
- **`.message.tool-only`** → decision in §6 (constrain to column vs full-bleed).

### 4.5 Composer alignment

Point `.composer` at the same token (`max-width: var(--chat-content-width)`) so
the input lines up under the transcript. (Currently 880px; aligning is the
cleaner look but is a minor, reversible call — see §6.)

---

## 5. Implementation sketch

Files (all cockpit):

1. `src/styles/_root-tokens.scss` — add `--width-chat-content: 760px`.
2. `src/styles/_semantic-tokens.scss` — add `--chat-content-width: var(--width-chat-content)`.
3. `src/app/views/persistent-chat/persistent-chat.component.ts` (template) —
   wrap the message loop in `<div class="messages-inner">`.
4. `src/app/views/persistent-chat/persistent-chat.component.scss`:
   - `.messages` — drop `gap`/flex-column (move to `.messages-inner`); keep
     padding + overflow.
   - add `.messages-inner` (§4.2).
   - `.message` — `max-width: 90%` → `100%`.
   - `.message-body` — `font-size: 13px → 15px`, `line-height: 1.5 → 1.6`.
   - verify/add `pre`, `code`, and markdown-table `overflow-x: auto`.
   - `.composer` — `max-width: 880px → var(--chat-content-width)` (if §6 agrees).
   - mobile `@media (max-width: 768px)` (≈ L2207) — confirm `.messages-inner`
     collapses to full width (max-width 760 already > small viewports, so it's a
     no-op there; add `max-width: 100%` only if needed).

Effort: ~1–2 hours incl. the template wrap and overflow checks. Pure
cockpit/CSS + one small HTML change → Tilt `ng serve` HMR (~5s) loop.

---

## 6. Open questions / decisions

1. **Target width** — Tight 660 / **Comfortable 760 (rec)** / Roomy 820.
2. **Font bump 13→15px** — recommended; confirm acceptable (slightly fewer
   messages visible per screen).
3. **Composer** — align to `--chat-content-width` (rec) or keep 880px.
4. **`.message.tool-only` full-bleed** — keep tool output able to exceed the
   prose column (good for wide command output/diffs), or constrain it to the
   column for visual consistency? Leaning: keep tool output free to be wider,
   cap only prose.
5. **Empty-state** (`.empty-inner`, 850px) — leave as-is or align to the token?
   (Cosmetic.)

---

## 7. Verification plan

- **Re-measure** with the same in-browser script (pane width, widest
  `.message-assistant .message-body`, font, canvas-measured CPL) on session
  `e9699503` at 1920px and ~2560px. Target: 75–95 CPL; no element forces
  horizontal page scroll.
- **Visual**: screenshot the same session before/after; confirm code block +
  table still readable (scroll, not squeeze).
- **Regression sweep** of the README smoke path session view: streaming a live
  turn, tool cards, user bubbles, narration, resume card, mobile width (<768px).
- Tilt loop for iteration; commit only after local pass (per CLAUDE.md
  Plan→Develop→Verify).

---

## 8. Out of scope

- Per-user width/font preference (possible later; token makes it trivial).
- Builder/job views (different content classes; covered by page-width tokens).
- Markdown/typography restyle beyond size/line-height (separate effort).
