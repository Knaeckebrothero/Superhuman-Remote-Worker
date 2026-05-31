---
tags:
  - feature
  - sessions
  - cockpit
  - ux
  - backlog
aliases:
  - chat polish
  - persistent chat polish
  - chat ux backlog
related:
  - "[[session_turn_rendering]]"
  - "[[persistent_chat_ui_redesign]]"
  - "[[persistent_chat_visual_refresh]]"
  - "[[session_narration]]"
---

# Persistent Chat — UX Polish Backlog

> Round of UX polish items surfaced by a session test on the dev cluster. The big-rocks rendering work (turn cards, tool-result/thinking durability, streaming reasoning capture) shipped 2026-05-17–19 — this doc captures the residual paper cuts and the next steps that build on top of it.

**Status:** Backlog — triaged, not yet sliced into PRs.
**Filed:** 2026-05-20
**Source:** Self-test of a fresh persistent session after `session_turn_rendering` Phase 1 + reasoning capture went live.

## Motivation

Phase 1 of [[session_turn_rendering]] gave us discrete event cards per turn (thought | text | tool_call), and the streaming reasoning capture made the thought card non-empty for gpt-5.4-mini. With those structural pieces in, real-session usage immediately surfaced a long list of smaller-but-real UX issues: too-wide tool cards, no markdown in reasoning bodies, useless `(0 bytes)` annotations, oversized user messages that should fold, a stale-WS reconnect bug, broken session-create settings, and a handful of missing or unwired chat affordances (paste-to-attach, image previews, `/rewind`, compaction progress).

This doc captures the full list verbatim and triages it into shippable slices so items don't get lost and we can pick off the cheap wins before the structural ones.

## Triage

### Slice 1 — Easy wins (one PR, ~half a day)

Pure renderer/style changes, no schema, no protocol. Verifiable end-to-end in one Playwright pass.

1. **Remove `(0 bytes)` from write-file results** — one-line tweak in the tool result formatter; suppress the size annotation when zero (or always, if it's never useful).
2. **Markdown rendering in thought-card body** — the chat already has a markdown pipe for assistant text; apply it to `{{ event.content }}` inside `#thoughtCard`. Gets clickable links + inline code styling inside reasoning blocks for free.
3. **Auto-expand reasoning cards** — flip `[attr.open]` in `#thoughtCard` to always-open (or expose a "collapse reasoning by default" toggle in session settings).
4. **Tool card max-width** — CSS only. `.tool-card { max-width: min(720px, 100%); }` so cards stop spanning the viewport.
5. **Autocollapse long user messages** — wrap `.user-text` in a `<details>` when line count exceeds a threshold (~8 lines feels right); summary shows first line + "[...]" hint.

### Slice 2 — Single-component polish (one PR each, ~1–2 days each)

Each touches one component or one render path. No cross-cutting work.

6. **Code-block styling in markdown output** — basic `<code>`/`<pre>` styling (background, padding, monospaced). Full syntax highlighting (highlight.js / Prism / Shiki) is a separate decision; defer until we see it actually matters in real reasoning text.
7. **Diff view for `edit_file` / `write_file` tool cards** — render a unified diff (use `diff2html` or a small homegrown formatter) instead of the raw post-state. Needs `before` content, which we may or may not have stored — confirm before scoping.
8. **Improve the collapsed-turn summary line** — today the collapsed AssistantTurn shows the last line of content, which is often "Done." or a tool name. Replace with: first text event's first sentence, or "Used N tools, wrote M files" digest. (Builds on Phase 2 of [[session_turn_rendering]].)
9. **Chat bubble visual refresh** — user/assistant avatars, alignment, padding. Cosmetic, but the gap is visible.

### Slice 3 — Sequential-element grouping (1 week-ish)

10. **Group consecutive same-kind events** — render a run of N tool calls as a single "10× tools" collapsible. Critically: a `[tool, tool, thought, tool, tool]` sequence must render as `[2× tools, thought, 2× tools]`, not collapsed into one group. This is Phase 2 of [[session_turn_rendering]] proper — runs entirely in `turn-reducer.ts` and the template.

### Slice 4 — Input/composer affordances (independent track)

11. **Paste to attach** — Ctrl+V (and Cmd+V on macOS) for images and small documents in the composer. Wires `paste` event → clipboard items → existing attachment upload flow.
12. **Image preview chips in attachment list** — render a thumbnail in addition to the filename chip. Keep the path visible (best of both).

### Slice 5 — Chat-flow commands (revisit & cull)

13. **Revisit the command toolset.** User reports only using `/compact`; the rest may be dead weight.
14. **Add `/rewind`** if we decide it's still in scope.
15. **Test `/compact`** end-to-end (it isn't covered) and add a progress indicator — spinner or progress bar — so the user can see compaction is in flight (today it looks frozen).
16. **Read-message** — flow isn't tested and the button needs a rework. Scope TBD.

### Slice 6 — Session-create settings (bugfix bundle)

These are bugs, not polish — call out separately so they don't get bundled into a UX PR.

17. **Model dropdown shows nothing initially** — should default to the model that will actually be used when "Create session" is clicked.
18. **Autonomy level isn't carried into the session** — selecting "auto-accept" still starts the session as "supervised". Plumbing bug between create form and session bootstrap.
19. **In-session "Narration" toggle is a no-op** — either wire it or remove it. See [[session_narration]] for what it would mean.

### Separate workstreams (not part of this doc)

- **Stale WS / dropped-message bug** — user reports: idle for 5–10 min, type a message, hit enter, the indicator flips off "Connected" and the message gets swallowed. This is a reconnect/heartbeat bug in `persistent-chat.service.ts`, not chat UI polish. File as its own issue under `docs/issues/`.
- **Live streaming of run_command stdout** — the user mentioned "we can't see details of a command currently being executed." Tool cards already show `tool(args)` and status. If the real ask is mid-execution stdout streaming, that's a separate infra job (workspace shell → SSE).
- **Syntax-highlighted code in reasoning** — see item 6 above. Worth re-raising once basic markdown + code styling is in and we can judge if it actually helps.

## Original list (verbatim from 2026-05-20 review)

Preserved so nothing is lost, even where it overlaps with the triage above.

- Autocollapse user message when they send more than x lines
- Markup formatting for the reasoning element of the agent
- Autoexpand the reasoning components of the agent
- Tool cards should be shorter and not go over the entire length of the screen
- Tool cards should always show the execution details
    - What tool
    - What arguments?
    - We can't see details of a command currently being executed!
- Add general formatting and highlighting
    - Highlighted (aka underlined) and clickable links
    - Perhaps different colors for code?
- Diff view for edit or write_file cards
- Remove the byte info for files (e.g. `Written: repos/Stadur-Sued-Project/app/__init__.py (0 bytes)`) — 0 bytes doesn't help anyone
- We need to optimize the collapsing of the message — today it only gives you the last line or so
- We can group a series of elements that follow each other
    - 10 tool calls in a row can be collapsed to "10× tools"
    - User can expand it back to view the 10 tool cards
    - But: `[2 tools, reasoning, 8 tools]` must not be summarized to "10× tools"
- Chatbot and user image bubbles should be adapted
- Ctrl+V should be able to add images or documents to the chat
- Make images added to the chat render as previews (also keep the name/path)
- Read-message still isn't tested and the button needs a rework
- We don't have the basic chat flow management tools
    - `/rewind` missing
    - `/compact` not tested
- We need to revisit the chat management tools
    - Check that they flow naturally
    - Check if we need all of the tools we have right now (only using `/compact`)
    - We don't have a progress tracker showing compaction is still in progress
        - Could be a progress bar like Claude Code, or just a spinner
        - Important: user must see compaction is still in progress
- The settings for creating the session have issues
    - Model dropdown shows nothing (should show the model used when the user hits "Create session")
    - Autonomy level is not carried over to the session (auto-accept selected → session starts supervised)
    - In-session settings have Narration, but this setting doesn't do anything
- Session becomes stale after a while, allowing me to type a message which gets swallowed when I hit enter
    - Start a session and have it sit idle for 5–10 minutes (not enough to time out)
    - Type a message into the chatbox and hit enter
    - The system switches from green "Connected" indicator → message lost

## Recommended order

1. **Ship Slice 1** first — half-day PR, immediate visible improvement, no risk.
2. **File Slice 6 bugs** as separate issues (or one issue with three sub-tasks) and fix them before touching new UX — they're "settings don't work" type bugs that erode trust faster than missing polish.
3. **File the stale-WS bug** as `docs/issues/persistent_chat_stale_connection.md` — it's a reliability problem and shouldn't sit in a UX backlog.
4. **Slice 2 next** in whichever order is convenient. Diff view (#7) is the most valuable; bubble refresh (#9) is the most visible.
5. **Slice 3** (grouping) requires Phase 2 of [[session_turn_rendering]] — pick up when ready.
6. **Slices 4 and 5** are independent of the rest; schedule against priority, not dependency.

## Decision log

- **2026-05-29 — Slice 1 shipped.** All five easy wins landed and were verified on the local k3d cluster (Tilt live-reload):
  1. `(0 bytes)` annotation removed from `write_file` / `edit_file` / `file_exists` tool results (`src/tools/workspace/files.py`, `filesystem.py`).
  2. Markdown rendering in the thought-card body (`#thoughtCard` now uses `<markdown [data]="event.content">`).
  3. Reasoning cards auto-expand (`#thoughtCard` `<details … open>`).
  4. Tool-card max-width capped at `min(720px, 100%)`.
  5. Long user messages (>8 lines) auto-collapse to a first-line preview with `[…]` / `▴` hint (verified end-to-end in session `e5d67e12`).

  Items #2/#3/#4 confirmed present in the served bundle + stylesheet; #5 verified visually in a live session; #1 verified in source + unit tests. Changes left uncommitted for the user to land.

- **2026-05-29 — Slice 2 #6 (code styling) implemented.** Investigation found the *basic* `<pre>`/`<code>` styling was already in place (the `.message-body ::ng-deep` markdown block cascades to both the thought card and text events, both of which live inside `.message-body`), and PrismJS is already loaded globally (`angular.json` scripts) so ngx-markdown already emits `.token.*` spans on fenced code in the chat. The only missing piece was the **token colour theme** — the "different colors for code" item the triage had marked *deferred*. Turned out near-free, so wired it.
  - **Placement decision:** put the theme in a new **global** partial `cockpit/src/styles/_code-highlight.scss` (`@use`d from `styles.scss`), **not** in the component SCSS. Rationale: (a) the persistent-chat component stylesheet is already at the documented style-budget ceiling (`docs/issues/persistent_chat_component_style_budget.md` — "don't grow it"), and a component-scoped copy added ~0.7KB to it; the global partial adds zero to the component budget; (b) a Prism token theme is global by nature (that is how Prism themes ship — unscoped `.token.*`); (c) it gives one source of truth so the chat and the instruction builder colour code identically, and sets up a future DRY cleanup (the builder can drop its inline `:host ::ng-deep .token.*` copy and `@use` this partial).
  - Palette mirrors `instruction-builder.component.ts` exactly (VS Code dark). The builder's inline rules keep higher specificity and still win locally, so this is additive — no regression there.
  - **Verified locally:** `styles.scss` compiles clean and emits the token rules; chat component SCSS back to its pre-change size. **Pending:** live visual confirmation on the k3d cluster (cluster was stopped during this work) — open a session/historical thread with a fenced code block and confirm tokens are coloured.

- **2026-05-29 — Slice 2 #7/#8/#9 implemented.** Remaining three items landed; Slice 2 is now code-complete (all verified by `ng build` + unit tests, live visual pending the cluster).
  - **#7 — diff view for `edit_file`/`write_file` cards.** Scoping question (does "before" exist?) resolved: **`edit_file` replace carries `old_string`→`new_string` in the call args**, so we render a real diff with **no backend/schema change**; `write_file`/append/prepend have no "before" and render as all-additions. New dependency-free LCS util `cockpit/src/app/core/util/line-diff.ts` (+ spec, 7 cases). Component `fileEditView()` builds the view from `tc.args` (returns null for non-file tools and for failed calls, so errors still show the result string); render capped at 400 lines (`DIFF_LINE_CAP`) with a "+N more" footer to bound DOM on huge writes. Works for historical cards too (args ride in the AI message's `tool_calls` JSONB). New `chat.diff.*` i18n (EN+DE).
  - **#8 — collapsed-turn headline.** Was the *last* text event (often "Done."). Now `collapsedHeadline()` shows the first sentence of the *first* text event (`firstTextOf` + `firstSentence` added to `turn.model.ts`, + spec), falling back to a `toolCount`/`thoughtCount`/`collapsedEmpty` digest when the turn has no text (reuses existing i18n — no new keys). Removed the orphaned `.turn-headline-empty` rule. Note: `lastTextEvent`/`lastTextOf` are retained — they still feed the TTS "read aloud" button (caught + reverted a premature dead-code removal).
  - **#9 — bubble refresh.** Conservative, since the existing design is already intentional (user speech-bubble w/ tail + asymmetric radius; deliberately flush assistant text). Pinned avatars to the top of tall bubbles (`.message { align-items: flex-start }`) and gave avatars a hairline ring + soft shadow for definition on both themes. **Subjective — best eyeballed live.**
  - **Budget:** `persistent-chat.component.scss` now **34.67 kB** (Angular `anyComponentStyle`: 32 kB warning / 40 kB error). #7's diff styles are genuinely component-local (unlike #6's Prism theme, they can't go global), so they live in the component. Build passes (warning, not error). Budget limit **not** bumped — the tracked Step-2 component split (`docs/issues/persistent_chat_component_style_budget.md`) is the real fix and is now slightly more warranted.

- **2026-05-29 — Slice 2 live-verified on k3d (Playwright) + #6 made theme-aware.** Local cluster had no chat history (`thread_messages` empty), so I seeded one synthetic historical thread (`historyToTurns` renders the same components as live) and drove Playwright. Results:
  - **#9** ✓ avatar ring (`1px` hairline) + `box-shadow` + `.message{align-items:flex-start}` (top-pinned) all computed as expected.
  - **#7** ✓ `write_file` → "Written · app/greet.py" with 2 green `+` lines (indentation preserved); `edit_file` replace → "Diff · app/__init__.py" with the unchanged import as **context** and the new import as a green **add** — LCS diff correct.
  - **#8** ✓ collapsed headline = first sentence of the *first* text ("I'll add a greeting helper, then wire it into the entrypoint."), not the trailing "Done —".
  - **#6** — found and fixed a real bug. The Prism theme (committed `c8f1b315`) was **dark-palette only**, but the active theme is **travertine (light)** with a cream code background; pale-yellow `function`/light-grey `punctuation` tokens were near-invisible (contrast ~1.2–1.3:1). Added a **theme-aware** light palette to `_code-highlight.scss` (VS Code Light+) scoped under `.theme-travertine`; base dark palette stays for `.theme-senate`/default. Re-verified: all tokens now ≥ 5.66:1 contrast on light (keyword `#0000ff` 7.98, function `#795e26` 5.66, string `#a31515` 7.29, punct/op `#383838` 10.89). The instruction-builder's inline `:host ::ng-deep .token.*` copy still forces dark-on-light and has the same latent bug — left as a separate follow-up (its component rules out-specify the global theme). **`_code-highlight.scss` theme-aware update is a new uncommitted change** on top of the committed `c8f1b315`.
  - Verified on the running pod by `kubectl cp`-ing the working-tree files into Tilt's `ng serve` (cluster was started without `tilt up`, and the baked image predated `c8f1b315` so the theme files were missing from the pod). These pod-side copies are **ephemeral** — the durable changes live uncommitted in the working tree.

- **2026-05-31 — Tilt build confirmed; live-streaming verification blocked by infra (not Slice 2).** With `tilt up` running, Tilt rebuilt cockpit + orchestrator from the working tree — the new cockpit *image* contains all Slice 2 + theme-aware changes (durable, not the earlier ephemeral `cp`). Attempted a real live session (Developer expert) to exercise the streaming render path: **agent provisioning + session connect now work** (vs the prior `/ready` timeout), but the first turn failed with **LLM `401 "Incorrect API key provided: not-needed"`** — the local cluster's only configured model (gemma-4 → `RedHatAI/gemma-4-31B-it-FP8-Dynamic`) points at an OpenAI-compatible endpoint with an unset key. So the live stream can't be exercised until a working model key/endpoint is set in `values-local.yaml` / admin LLM. **No Slice 2 gap**: the rendering is fully covered by the seeded-historical verification (identical components) + the turn-reducer unit tests (live event production). Incidental: the duplicate-agent-provision race recurred (2 pods, one stuck → repeated HTTP 425 on `/api/sessions/{id}/connection`), and the session ran **Supervised despite selecting Auto-accept** — reconfirming Slice 6 bug #18 (autonomy not carried into the session). Both pre-existing, separate from this doc's UX items.
