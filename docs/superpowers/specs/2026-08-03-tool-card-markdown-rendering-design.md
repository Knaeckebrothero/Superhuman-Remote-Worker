# Markdown rendering in tool cards — design

**Date:** 2026-08-03
**Status:** Implemented and live-gated on local k3d (2026-08-03). Uncommitted on `develop`.
**Component:** cockpit tool cards (`cockpit/src/app/core/tools/tool-descriptors.ts`, `cockpit/src/app/ui/tool-card/tool-card.component.{ts,scss}`, new `cockpit/src/app/core/markdown/tool-result-source.ts`)
**Follows:** `docs/features/unified_tool_cards.md` — the descriptor registry and the single presentational card it defines.

---

## Problem

A `read_file` on a `.md` file shows a **`markdown` chip** above a body of grey monospace text with `cat -n` line numbers. The chip is misleading: it is the *language* label produced by `languageFromPath()` (`tool-descriptors.ts:116`), not a render mode. `read_file` is declared `result: {kind: 'code', languageFrom: 'path'}` (`tool-descriptors.ts:142`), so the body takes the `<pre class="tc__result">` branch (`tool-card.component.ts:144`).

The card **already has** a working markdown branch — `@else if (r.kind === 'markdown')` renders `<markdown [data]>` (`tool-card.component.ts:141`) — and ngx-markdown is provided app-wide with the sanitizer plus the citation, math and external-image extensions (`app.config.ts:97`). Only four tools reach that branch: `web_search`, `extract_webpage`, `browse_website`, `kb_read`.

**Goal:** read a markdown file in the transcript as formatted prose, with the exact source one click away.

## Decisions (agreed with the user)

| Question | Decision |
|---|---|
| Default view for a markdown result? | **Rendered**, with a `Rendered \| Raw` toggle in the RESULT header. Raw shows the untouched tool output, line numbers included. |
| Which cards? | `read_file` on markdown files **plus** the four tools that already render markdown, so the control is consistent everywhere markdown renders. Explicitly **not** `search_files` / `list_files` / `kb_search` — a markdown parser mangles grep and `ls` output (`#` becomes a heading, `*` becomes a bullet). |
| How is a long result bounded? | **CSS height cap + fade + Show more.** The parser always receives the complete source, so fences, tables and lists never break mid-structure; only the display is bounded. |
| Does the choice persist? | **No.** Per-card, ephemeral, resets on reload. Rendered is the better default for reading, so Raw is an occasional exception, not a mode to live in. No `ChatPreferencesService` field, no settings row. |

## Two source defects that must be fixed before anything renders

Both are why "just flip the kind" does not work.

1. **Line numbers.** `read_file` formats every line as `f"{i:6}\t{line}"` (`src/tools/workspace/files.py:502` and `:995`). Six leading spaces is an indented code block in CommonMark, so marked would render the **entire document as one grey blob** — strictly worse than today.
2. **YAML frontmatter.** The motivating example (`skills/cite-as-you-write/SKILL.md`) opens with `---`. Marked renders the opening `---` as an `<hr>`, collapses the `name:`/`description:`/`tags:` lines into a single paragraph, and then reads the closing `---` as a **setext H2 underline** for that paragraph. The frontmatter would render as a giant heading — the most visible regression a naive implementation produces.

## Design

### 1. Deciding a result is markdown — `tool-descriptors.ts`

In `buildResult()` (`:296`), when the descriptor declares `kind: 'code'` **and** the path-derived language is `markdown`, emit `kind: 'markdown'` while keeping `language: 'markdown'` so the chip is unchanged.

One conditional at the point where the language is already computed (`:310`). `read_file`'s descriptor is untouched, and no new descriptor field is introduced — a second tool that later gains `languageFrom: 'path'` inherits the behaviour automatically.

Also add `markdown` and `mdx` to `EXT_LANGUAGE` (`:106`); only `md` maps today.

### 2. Cleaning the source — new `core/markdown/tool-result-source.ts`

Pure functions, no Angular, unit-tested on their own.

- **`stripLineNumbers(content): string`** — removes `^ *\d+\t` per line, but only from a block that convincingly *is* numbered output: **at least two numbered lines, strictly increasing, and ≥70% of non-empty lines**. The guard is load-bearing twice over: it stops a markdown file that legitimately contains tabs from being mangled, and it makes the function a safe no-op on `web_search` output, which is never numbered.
  - *Revised during implementation.* The first draft demanded that **every** non-empty line be numbered. Writing the spec for it showed that would refuse to strip exactly the truncated reads that need it most: `read_file` appends **unnumbered** footers (`[Lines 1-200 of 900 …]`, `[TRUNCATED at word limit …]`) after the numbered block. Monotonicity is what actually separates real `cat -n` output from a file that happens to contain tab-separated digits; the share threshold tolerates the footers.
- **`fenceFrontmatter(content): string`** — when the content opens with a `---` fence closed by a later `---`, wrap that block in a ` ```yaml ` fence so it renders as a small code block instead of an `<hr>` plus a setext heading.
- **`toRenderableMarkdown(content): string`** — composes the two, in that order (numbers must go before frontmatter can be recognised at position 0).

The tool's own bracketed decorations — `[IMAGE: …]`, `[AUDIO: …]`, `[TRUNCATED at word limit …]`, `[Lines X-Y of Z …]` — are left alone. They render as ordinary paragraphs, which is correct: they are notes to the reader, and they carry real information about what was truncated.

### 3. The toggle — `tool-card.component.ts`

- `mode = signal<'rendered' | 'raw'>('rendered')`, local to the card instance.
- A segmented `Rendered | Raw` control in the RESULT header (`:116`), next to the language chip and left of the copy button, rendered **only** when `r.kind === 'markdown'`.
- Rendered → `<markdown appKatex [data]="renderableSource(r)">`, where `renderableSource()` is a thin component method over `toRenderableMarkdown()`. Raw → falls through to the existing `<pre class="tc__result">` branch, **with its 200-line cap intact**, so raw shows exactly the bytes the model saw.
- `visibleContent()`, `hiddenLineCount()` and `isCapped()` (`:284`–`:299`) currently special-case `kind === 'markdown'` to disable the line cap. They become mode-aware: in raw mode a markdown result caps exactly like a code result.
- **Copy follows the visible mode** — raw copies verbatim, rendered copies the cleaned source. Copying six spaces and a tab before every line is a papercut worth not shipping.

`appKatex` is added (chat already applies it — `persistent-chat.component.ts:980`) so math in a read file renders instead of showing raw `$…$`. `appCitationRef` is deliberately **not** added: a file read should not have its `[1]`-looking text rewritten into citation links.

### 4. Height cap — `tool-card.component.scss`

- `.tc__md--capped { max-height: 24rem; overflow: hidden; }` with a `::after` fade to the card background, and the existing `.tc__more` button for Show all / Show less.
- The fade **must** end at `var(--surface-0)` — the card's own background, set on `.tc`. The first implementation faded to a guessed `var(--surface-raised, rgba(0,0,0,0.35))`; that token does not exist, so the fallback painted a dark smudge across the light (`travertine`) theme. Caught by the live gate, not by any test — component styles are unreachable from jsdom (see `cockpit_theme_tokens_body_scope_and_cat_ramp`). Verified in both themes: the gradient terminates at `rgb(36,36,44)` under `senate` and at the cream surface under `travertine`.
- The existing `showFull` signal drives **both** caps — the height cap in rendered mode, the 200-line cap in raw mode. One expansion state, so a card expanded in one mode stays expanded when the user flips to the other. Two independent signals would make the toggle silently collapse the body.
- A `ResizeObserver` on the rendered element sets an `overflowing` signal, so the button appears **only when content is genuinely hidden**. A source-line-count heuristic was rejected: it lies in both directions — sixty lines of dense prose overflow, sixty lines of short bullets do not.
- This is a deliberate exception to the "No max-height: an expanded card grows to its full content height" rule stated at `tool-card.component.scss:180`. That comment is amended in the same commit rather than left contradicting the code.

### 5. i18n

New keys `toolCard.view.rendered`, `toolCard.view.raw`, and `toolCard.showAll` in **both** `cockpit/src/assets/i18n/en.json` and `de-DE.json`. `showAll` is separate from the existing `showMore` because that string interpolates a line count (`"Show {{count}} more lines"`) and the height cap hides pixels, not a countable number of lines.

## Testing

| File | Covers |
|---|---|
| `core/markdown/tool-result-source.spec.ts` | Uniformly numbered block is stripped; a block with any unnumbered non-empty line is left untouched; frontmatter is fenced; content without frontmatter is unchanged; a lone `---` (horizontal rule, never closed) is not treated as frontmatter; truncation footers survive. |
| `core/tools/tool-descriptors.spec.ts` | `read_file` on `.md`/`.markdown`/`.mdx` → `kind: 'markdown'`, `language: 'markdown'`; on `.py` → `kind: 'code'`; a tool with an explicit `kind: 'markdown'` is unaffected. |
| `ui/tool-card/markdown-tool-card.spec.ts` (new file) | Toggle renders for markdown results and for no other kind; raw mode shows the exact numbered source; rendered mode strips the numbers and fences the frontmatter; copy returns the right text per mode. |

Rendering the real card in a spec needs one piece of harness that is worth knowing about. The result header's `<app-icon-button>` declares `ariaLabel = input.required<string>()`; this vitest pipeline drops signal-input metadata, so the binding never lands and reading it throws NG0950, killing the render. `notify-user-tool-card.spec.ts` avoids this by never rendering a result section — not an option here. `TestBed.overrideComponent` cannot help either: it demands an already-resolved component def, and `styleUrl` resolution does not survive `resetTestingModule()`. The working route is `vi.mock('../icon-button', () => import('./icon-button.stub'))`, with the stub in **its own module** (`ui/tool-card/icon-button.stub.ts`) because a class defined inline in a hoisted `vi.mock` factory fails with `__decorateClass is not a function`.

Per `local_test_env_vs_ci_and_ruff`, vitest is the reliable local gate; CI is the authority.

## Known limitations (accepted for v1)

- **Relative image links** (`![](./diagram.png)`) render broken — workspace-relative paths do not resolve from the browser. Absolute/external images continue through `externalImageExtension`.
- **No syntax highlighting inside fenced code blocks** in rendered mode, matching how the four existing markdown cards behave today.
- The **debug audit surface** shares `<app-tool-card>`, so it inherits this behaviour; that is intended and needs no separate work.
