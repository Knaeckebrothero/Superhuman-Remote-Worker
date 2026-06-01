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

**Status:** Slices 1–4 ✅ shipped + verified (local k3d + dev cluster). The collapsed-turn headline (#8) was **reworked 2026-06-01** to render the full final answer when a turn is collapsed (folding only the lead-up) — **live-verified on a real streaming session** on local k3d. **Slice 4 (input/composer affordances) shipped 2026-06-01**: #11 paste-to-attach (new) + #12 image preview chips (found already shipped) — paste-to-attach live-verified on k3d (image chip + thumbnail; text paste falls through). **Slices 5–6 (incl. confirmed bug #18) and the separate workstreams remain backlog.** See the decision log for per-item detail.
**Filed:** 2026-05-20
**Source:** Self-test of a fresh persistent session after `session_turn_rendering` Phase 1 + reasoning capture went live.

## Motivation

Phase 1 of [[session_turn_rendering]] gave us discrete event cards per turn (thought | text | tool_call), and the streaming reasoning capture made the thought card non-empty for gpt-5.4-mini. With those structural pieces in, real-session usage immediately surfaced a long list of smaller-but-real UX issues: too-wide tool cards, no markdown in reasoning bodies, useless `(0 bytes)` annotations, oversized user messages that should fold, a stale-WS reconnect bug, broken session-create settings, and a handful of missing or unwired chat affordances (paste-to-attach, image previews, `/rewind`, compaction progress).

This doc captures the full list verbatim and triages it into shippable slices so items don't get lost and we can pick off the cheap wins before the structural ones.

## Triage

### Slice 1 — Easy wins (one PR, ~half a day) — ✅ SHIPPED

Pure renderer/style changes, no schema, no protocol. Verifiable end-to-end in one Playwright pass.

1. **Remove `(0 bytes)` from write-file results** — one-line tweak in the tool result formatter; suppress the size annotation when zero (or always, if it's never useful).
2. **Markdown rendering in thought-card body** — the chat already has a markdown pipe for assistant text; apply it to `{{ event.content }}` inside `#thoughtCard`. Gets clickable links + inline code styling inside reasoning blocks for free.
3. **Auto-expand reasoning cards** — flip `[attr.open]` in `#thoughtCard` to always-open (or expose a "collapse reasoning by default" toggle in session settings).
4. **Tool card max-width** — CSS only. `.tool-card { max-width: min(720px, 100%); }` so cards stop spanning the viewport.
5. **Autocollapse long user messages** — wrap `.user-text` in a `<details>` when line count exceeds a threshold (~8 lines feels right); summary shows first line + "[...]" hint.

### Slice 2 — Single-component polish (one PR each, ~1–2 days each) — ✅ SHIPPED

Each touches one component or one render path. No cross-cutting work. **All four (#6–#9) shipped + verified** locally (seeded historical thread) and live on the dev cluster (`gpt-5.4-mini` streaming session) on 2026-05-31. #6's Prism theme was made theme-aware (light/travertine palette) during verification. The one tiny follow-up (#8 `firstSentence()` not stripping a leading code-fence) was fixed afterward — see the decision log.

6. **Code-block styling in markdown output** — basic `<code>`/`<pre>` styling (background, padding, monospaced). Full syntax highlighting (highlight.js / Prism / Shiki) is a separate decision; defer until we see it actually matters in real reasoning text.
7. **Diff view for `edit_file` / `write_file` tool cards** — render a unified diff (use `diff2html` or a small homegrown formatter) instead of the raw post-state. Needs `before` content, which we may or may not have stored — confirm before scoping.
8. **Improve the collapsed-turn summary line** — today the collapsed AssistantTurn shows the last line of content, which is often "Done." or a tool name. Replace with: first text event's first sentence, or "Used N tools, wrote M files" digest. (Builds on Phase 2 of [[session_turn_rendering]].)
9. **Chat bubble visual refresh** — user/assistant avatars, alignment, padding. Cosmetic, but the gap is visible.

### Slice 3 — Sequential-element grouping (1 week-ish) — ✅ SHIPPED (verified on local k3d)

Built render-time in ~half a day, not the estimated week (the existing flat event model + array-taking `#toolDetails` template made it a pure view-model step). Unit-tested, build-clean, and live-verified on the local k3d cluster (seeded historical thread, all five behaviors incl. the no-merge-across-thought constraint). Deploy then confirmed live on the dev cluster (running bundle serves the grouping code; service worker not stale). See the decision log.

10. **Group consecutive same-kind events** — render a run of N tool calls as a single "10× tools" collapsible. Critically: a `[tool, tool, thought, tool, tool]` sequence must render as `[2× tools, thought, 2× tools]`, not collapsed into one group. This is Phase 2 of [[session_turn_rendering]] proper — runs entirely in `turn-reducer.ts` and the template.

### Slice 4 — Input/composer affordances (independent track) — ✅ SHIPPED

11. **Paste to attach** — ✅ Ctrl+V / Cmd+V for images and documents in the composer. A `(paste)` listener diverts file-kind clipboard items into the existing attachment flow; text pastes fall through untouched.
12. **Image preview chips in attachment list** — ✅ **already shipped** (landed with the camera/voice-attachment UI). The attachment chip renders an `<img>` thumbnail for image MIME types and keeps the filename + size (best of both).

Both items are component+service only — no schema/protocol/backend change. The attachment flow they build on: state in `PersistentChatService` (`pendingAttachments` `FilePreview[]` signal `:365`, `addAttachments()` `:1135`, `removeAttachment()` `:1143`); files upload to `ChatAttachment[]` (`:74`) **on send** (`:~1155-1202`); the File→`FilePreview` conversion (incl. the image data-URL preview) is `FileHandlingService.createFilePreviews()`. See the 2026-06-01 decision-log entry for the implementation + live verification.

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

> **Progress (2026-06-01):** Slices 1–4 ✅ shipped + verified; #8 collapsed-turn reworked + live-verified; Slice 4 (#11 paste-to-attach + #12 preview chips) shipped + live-verified. **Slices 5 & 6 (incl. the confirmed bug #18) + the stale-WS workstream remain.** The original sequencing below is preserved as rationale.

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

- **2026-05-31 — Slice 2 fully live-verified on the dev cluster.** After deploy to `cockpit.superhuman-remote-worker.com`, ran a real Developer/gpt-5.4-mini session (LLM works there). Live streaming path confirmed end-to-end: thought cards streamed **markdown** (auto-expanded), the `write_file` tool cards rendered live **"Written" diffs** (green `+` lines, real tool calls), the final reply showed a **syntax-coloured** `python` code block, and collapsing the turn produced a headline. The dev cluster also defaults to **travertine (light)**, so this confirms the **theme-aware `_code-highlight.scss` is deployed and legible** (keyword `#0000ff`, string `#a31515`, etc.). Avatar ring/shadow + `align-items:flex-start` confirmed. (#7 replace/old→new diff was covered by the earlier seed; gpt-5.4-mini happened to use `write_file` twice rather than `edit_file`.)
  - **Minor follow-up found (#8):** `firstSentence()` strips leading `#`/`>`/`-`/`*` markers but **not** code-fence backticks, so a turn whose *first* text is a fenced code block collapses to a headline like `` ```python def greet(name): … ``` ``. Cosmetic, edge-case (agents usually emit prose before code); fix = strip a leading ```` ``` ```` fence in `firstSentence`. Not blocking. **Fixed 2026-05-31:** `firstSentence()` now strips an opening *and* closing code fence (`` ```lang … ``` ``) before the existing marker/whitespace pipeline, so a code-first turn headlines as the code's first line. +2 spec cases (10 pass).
  - Bug #18 (Auto-accept → Supervised) reproduces on dev too. Test session deleted afterward; only `cockpit.superhuman-remote-worker.com` state touched (no infra changes — Fleet owns it).

- **2026-05-31 — Slice 3 (#10, consecutive-element grouping) implemented.** Built as a **render-time** concern, not a reducer change — the model contract already says *"the renderer is free to merge them visually without distorting the data"* (`turn.model.ts`), and the shared `#toolDetails` template already takes an array of tools (we previously passed it one-element arrays). So the reducer/SSE stream is untouched; grouping is a pure view-model step. (The doc's earlier "runs in `turn-reducer.ts`" line was wrong — coalescing in the reducer would distort the event stream and complicate streaming/replay.)
  - **`groupEvents(events): EventGroup[]`** added to `turn.model.ts` — walks the flat event list, coalescing *consecutive* `tool_call`s into one `tools` run, broken by any thought/text. So `[tool,tool,thought,tool,tool]` → `[tools(2), thought, tools(2)]`, never one merged group. Pure function, 6 spec cases incl. that exact constraint.
  - **Template:** the expanded turn loop now iterates `groupedEvents(turn)`. A run of **≥ 4** (`TOOL_GROUP_THRESHOLD`, the chosen knob) renders as a native `<details class="tool-group">` "N× tool calls" disclosure (with a `groupToolCallsHuman` name summary) that expands to the existing `#toolDetails` list; runs of 1–3 render inline exactly as before; thought/text unchanged. The group **auto-opens when any member errored or was denied** (`toolGroupHasProblem`), so failures are never hidden behind a collapsed run.
  - **i18n:** `chat.turn.toolGroup` ("{{count}}× tool calls" / "{{count}}× Werkzeugaufrufe"). **SCSS:** lean `.tool-group*` block (chevron rotates 90° on open, mirroring `.tool-summary-line`); component stylesheet now 35.40 kB — over the 32 kB *warn* (already was, at 34.67 kB) but under the 40 kB *error* cap. **Budget not bumped** (per the style-budget memory; the split refactor is the real fix).
  - **Verified (unit/build):** `turn.model.spec.ts` 16/16; full cockpit suite **418/418**; `npm run build` clean (template type-check passes — Angular 21 narrows the `EventGroup` discriminated union in `@if`/`@switch`). Changes uncommitted.
  - **Verified (live on local k3d, 2026-05-31).** Confirmed the pod ran the synced code (`groupEvents` in the pod source, `ng serve` clean compile), seeded a historical thread (`aaaaaaaa-…-0001`, owner = local `test`) covering every branch, and drove it via Playwright (0 relevant console errors — only the pre-existing `index.html markLoaded` noise). All five behaviors confirmed in the rendered UI:
    1. **Run ≥4 collapses** to "N× tool calls" with a `groupToolCallsHuman` summary — a 5-tool turn → "5× tool calls" ("Reading graph.py x3, Grep, List dir").
    2. **Run <4 stays inline** — a 2-tool turn rendered two separate cards, no group wrapper.
    3. **No-merge-across-thought** — `[tool×4, thought, tool×4]` rendered as `[4× group][thought card][4× group]`, *not* one "8× tools" group (the doc's critical constraint).
    4. **Collapsed by default**, and a collapsed group **expands on click** (native `<details>`) to the full card list.
    5. **Auto-open on problem** — a 4-tool group containing a *denied* `run_command` auto-opened, surfacing the "Denied" card.
    Screenshot at repo root `slice3-tool-grouping.png`. Seed fixture left in the local DB for inspection (delete with `DELETE FROM thread_messages/threads WHERE … = 'aaaaaaaa-…-0001'`). Live-streaming *formation* of groups during an in-flight turn wasn't separately exercised (local LLM key still 401) — but it runs the identical `turn.events → groupEvents` path the reducer feeds, which is unit-covered.
  - **Deploy confirmed live on the dev cluster (2026-05-31).** After the push + redeploy, probed `cockpit.superhuman-remote-worker.com` via Playwright: the deployed i18n assets serve the new `chat.turn.toolGroup` keys (EN + DE), and the running, **service-worker-served** main bundle (`main-YKSDDQT3.js`) contains the grouping markers (`chat.turn.toolGroup`, `tool-group`, `toolGroupHasProblem`) — i.e. the `ngsw` SW is **not** serving a stale bundle (the prod-only trap). Only console error is the same harmless `index.html markLoaded` noise. A *live streaming* session wasn't run on dev — non-deterministic (needs the agent to emit a 4+ consecutive run) and the render path is already proven identical + reducer-unit-tested, so it'd spend tokens/pods for marginal confidence. Considered fully verified.

- **2026-06-01 — #8 reworked: collapsed turns keep the full final answer.** The headline-only collapse shipped 2026-05-29 hid the agent's *closing prose* along with the work — a long turn that auto-collapses (>8 events) showed only a truncated first-sentence headline, so the actual answer was a "half a sentence" tease (the user's report). Fix: the collapsed view now folds only the **lead-up** (opening text, reasoning, tool calls) and renders the **final answer** — the trailing run of text events after the last tool/thought — in full markdown. The chevron + count badge still signal the hidden work.
  - New pure helper **`trailingText(turn)`** in `turn.model.ts`: walks from the end collecting contiguous text events, returns '' when the turn ends on a tool/thought (no closing prose). Component gains `finalAnswer(turn)`; the template's `@if (isCollapsed)` branch renders `<markdown [data]="finalAnswer(turn)">` (class `.event-text.turn-final-answer`), falling back to the one-line `collapsedHeadline()` only when `finalAnswer` is empty. `firstSentence`/`firstTextOf`/`collapsedHeadline` retained for that fallback. **Render-only** — no schema/reducer/SSE change. SCSS budget **unchanged (35.40 kB)**: the collapsed answer reuses the existing `.event-text` markdown path (the new class carries no rule).
  - **Verified (unit/build):** `turn.model.spec.ts` 23/23 (+7 `trailingText` cases incl. ends-on-tool, ends-on-thought, multi-block join, fold-intermediate-text), full cockpit suite **425/425**, `npm run build` clean (Angular 21 template type-check passes the `@let answer` narrowing).
  - **Verified (live on local k3d, Playwright, 2026-06-01).** Synced via `tilt up` (`ng serve` live_update — confirmed `finalAnswer`/`trailingText` in the pod source). Seeded a long closing-answer turn (`aaaaaaaa-…-0001` turn 9: reasoning + opening text + 8 tools + a rich-markdown answer = 11 events). Confirmed in the rendered UI: (1) **turn 9 auto-collapses**, lead-up folded behind the `◐1 ▶8` badge, and the **full** "Root cause" answer (h2 + 3-item list + inline code + closing sentence) renders — not a truncated headline; (2) a turn ending on tool calls (turn 3) falls back to the `8 tool calls` digest; (3) **manual-collapsing** a short turn (turn 1) folds its 5× tool group and shows the trailing text in full — DOM-asserted `.turn-final-answer` present + renders markdown, `.turn-headline` absent, tool group hidden. Only console noise is the pre-existing `markLoaded` + pre-login 401s. Screenshot `collapsed-final-answer.png` at repo root. Seed turns 8/9 left in the local DB (idempotent re-seed; delete with `DELETE FROM thread_messages WHERE thread_id='aaaaaaaa-0000-4000-8000-000000000001' AND turn_number IN (8,9)`). Changes uncommitted.
  - **Verified (live streaming session, real LLM, 2026-06-01).** Once the homelab LLM endpoint was healthy, drove a real Developer session (`5d3cc2b8`, model `RedHatAI/gemma-4-31B-it-FP8-Dynamic` via the `ai.h4ll.app` router) and prompted a 10-tool task ("create 5 files, read them back, summarise"). On completion the turn **auto-collapsed** (>8 events): the **full final answer** rendered (`<h2>` "Summary" + 5-item `<ul>`) while the 10 tool calls folded behind the `▶ 10` badge — `.turn-final-answer` present, `toolCards: 0` (folded). Expanding showed the consecutive run as a single **"10× tool calls"** group (Slice 3, "Writing note1.txt x5, Reading note1.txt x5") with the final answer still at the bottom. Screenshots `live-collapsed-final-answer.png` + `live-expanded-tool-group.png` at repo root. This exercises the full live path (turn-reducer → `turn.events` → `groupEvents`/`trailingText` → render), closing the one gap the historical-thread verification couldn't.
  - **Infra note (not a chat-polish item):** the live test was initially blocked by a 401 unrelated to this feature — persistent sessions pin the **main** `llm.model` to the default `RedHatAI/gemma-4-31B-it-FP8-Dynamic` (`config/persistent_defaults.yaml:15`), which was **absent from the `models` catalog** after an LLM swap, so dispatch (`main.py:920-938`) attached no endpoint creds → `OPENAI_API_KEY=not-needed`. Worked around locally by adding a catalog alias row for that name → the LocalRouter endpoint. Proper fix is to align the config default with a registered catalog model (or register the default name). Also re-confirmed Slice 6 bug #18 (sessions start Supervised) + the duplicate-agent-provision race.

- **2026-06-01 — Slice 4 shipped (input/composer affordances).** Investigation first: **#12 (image preview chips) was already done** — the attachment-list template (`persistent-chat.component.ts:~977-1015`) already renders an `<img>` thumbnail for image MIME types (`@if (preview.type === 'image' && preview.preview)`), keeps the filename + size, has a `.is-image` chip modifier and an `openImagePreview()` lightbox; it landed earlier with the camera/voice-attachment UI. So Slice 4 reduced to **#11 paste-to-attach**, the one genuinely-missing affordance.
  - **#11 — paste-to-attach.** New pure helper **`extractClipboardFiles(items, now): File[]`** in `persistent-chat.component.ts` (next to the other `pick*`/`is*` exported helpers): keeps only `kind === 'file'` clipboard items (so a text/HTML paste returns `[]` and falls through), and synthesizes a stable, collision-free name (`pasted-<now>-<i>.<ext>`, `.bin` when MIME-less) for nameless clipboard blobs (screenshots). `now` is a param (not `Date.now()` inside) so the selection logic is deterministic + unit-testable. The component handler **`onPaste(event)`** (bound `(paste)` on the composer `<textarea>`) calls it; if it yields files it `preventDefault()`s and routes them through the *exact* existing path — `FileHandlingService.createFilePreviews()` → `chat.addAttachments()` — the same one the Attach button and drag-drop use. **No new upload path, no schema/protocol/backend change, no new i18n** (reuses the existing chip UI + its keys). **No SCSS change** → component stylesheet stays **35.40 kB**.
  - **Verified (unit/build):** +7 `extractClipboardFiles` cases in `persistent-chat.component.spec.ts` (text-only→[], null/undefined→[], named-file kept verbatim, nameless blob→`pasted-<now>-0.png`, mixed text+image→only the image, multi-image→`-0`/`-1` indices, MIME-less→`.bin`). Component spec **23/23**, full cockpit suite **432/432**, `npm run build` clean (budget unchanged).
  - **Verified (live on local k3d, Playwright, 2026-06-01).** Tilt `ng serve` live_update synced the change (confirmed `extractClipboardFiles`/`onPaste` in the pod source + a live-reload push). Resumed session `5d3cc2b8` to get an **enabled composer** (the composer enables in the "starting" state — no agent/LLM needed). Dispatched a real `ClipboardEvent('paste')` carrying a 1×1 PNG onto the textarea: the handler fired and **`preventDefault`'d** (`defaultPrevented: true`), nothing leaked into the textarea, and the **chip rendered with its `<img>` thumbnail** (`data:image/png;base64,…`), filename `screenshot.png`, size `69 Bytes`, and remove button — DOM-asserted + screenshot `slice4-paste-image-chip.png` (repo root). **Negative case** confirmed in the same pass: a text-only paste reported `defaultPrevented: false` and added **no chip** (falls through to native paste). Only console noise is the pre-existing 425 provision-race + `markLoaded`. Changes uncommitted.
