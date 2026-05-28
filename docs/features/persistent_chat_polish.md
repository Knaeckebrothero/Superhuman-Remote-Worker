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

_To be filled in as items ship._
