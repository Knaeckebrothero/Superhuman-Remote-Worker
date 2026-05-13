---
tags:
  - feature
  - cockpit
  - ui-refresh
related:
  - "[[dynamic_canvas]]"
---

# Persistent Chat — Visual Refresh

A visual-only refresh of the persistent session view (`/sessions/:id`), drawing inspiration from the static mockup in `improved_sessions/` (folder at repo root, not checked into git as a feature directory). Functional behavior is **unchanged**; only shapes, layout treatments, and a handful of state-specific surfaces are rewritten to match the mockup grammar.

**Status:** All features (F1–F8) shipped as of 2026-05-10. See per-feature **Status** notes for caveats and follow-ups.

## Source

The reference designs live in `improved_sessions/`:

- `sessions-components.jsx` — React components (`Rail`, `PanelHead`, `Subhead`, `ToolCall`, `MileMarker`, `Composer`, `Stream*` state views)
- `sessions.css` — component styles (Material-Symbols-icon-driven, no rounded blobs)
- `Sessions.html` / `Sessions States.html` — entry points (open in browser to see live)
- `screenshots/` — design-canvas overview screenshots
- `uploads/` — earlier iteration screenshots for context

The mockup uses Roman / "legion" naming (Praetor, Legion in the Field, Mile Marker, etc.) — **we discard those names**. We adopt only the shapes.

## Scope

- **Target file:** `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` (template + inline styles).
- **Color tokens:** keep our existing palette (`--success`, `--warning`, `--danger`, `--accent-color`, `--surface-*`, `--text-*`, etc.). Do not import the mockup's color tokens.
- **No functional changes:** keep slash autocomplete, narration modes, streaming interrupt, task bar, tool-only inline rows, settings panel, session divider on resume, system messages, etc.
- **Only one route in scope:** the persistent session chat view. The simple/mobile shell uses a separate `chat-page.component.ts` and is not part of this refresh.

## Out of Scope (deliberately not doing)

These were considered and rejected when reviewing the mockup:

- **Persistent left rail / multi-session navigator.** Sessions trend toward long, agent-driven runs; switching is rare enough that the existing `/sessions` list-then-detail route is fine.
- **Sessions list page rewrite.** Current cards (status dot + title + id + config + meta + per-row actions, filter tabs) cover the use cases the mockup rail covers, plus more (per-row Files/Resume/End/Delete).
- **Header rewrite.** Existing header + status-bar + behind-cog settings panel stays. We do not add cost/elapsed-time metrics to the subhead.
- **Plan Bubble.** No plan mode in the agent today; nothing to render with it.
- **Sticky day dividers in long threads.** Adopting only the jump-to-latest pill from the long-thread treatment.
- **Removing slash commands / streaming interrupt / tune button in header.** Mockup omits these; we keep them — the mockup is an inspiration source, not a replacement spec.

## Features

Each feature is independently shippable. Order is rough effort/risk ascending; feel free to reorder when picking up.

---

### F1 — Empty state with suggestion grid

**Status:** Done (2026-05-08).

**What:** Replace the one-line "emptyPrompt" empty state with a centered hero: brand mark + title + subtitle + 4-up suggestion-chip grid.

**Replaces in current code:** the `<div class="empty-state">` block in the messages container (around the `@empty` branch), plus `.empty-state` / `.empty-state-text` styles.

**Port from mockup:**
- Component: `StreamEmpty` in `sessions-components.jsx`
- Styles: `.empty-inner`, `.empty-mark`, `.empty-eyebrow`, `.empty-title`, `.empty-sub`, `.suggest-grid`, `.suggest` (sessions.css ~line 624 onward)

**Notes:**
- Drop the `LegionMark` SVG; use our existing app icon or `smart_toy` Material symbol.
- Suggestion chips are static placeholders for now (no click handlers wired up). Translation keys in `chat.empty.suggest.*`.
- Suggested chip copy (replaceable): "Plan and execute a research task", "Inspect a repo", "Continue a shared project", "Propose three useful jobs."
- Only render this when `sessionReady() && !messages().length && !isStreaming()`. The startup spinner (F2) covers the not-yet-ready case.

---

### F2 — Provisioning step list

**Status:** Done (2026-05-08). Per-step elapsed time included; the mockup's "~12s remaining" sub-header omitted (no ETA source).

**What:** Replace the simple spinner+phase-label with a step-list card showing each provisioning phase as a row with status icon, label, and per-step elapsed time. Time tracking lives in the component: a phase-transition effect records start/duration as `chat.startupPhase()` advances, a 1s tick drives the live elapsed display on the active row.

**Replaces in current code:** `.startup-spinner-container` + `.startup-spinner` + the inline `@switch (chat.startupPhase())` block (used in two places: the empty-list case and the resume case).

**Port from mockup:**
- Component: `StreamProvisioning` in `sessions-components.jsx`
- Styles: `.provisioning`, `.prov-card`, `.prov-head`, `.prov-spinner`, `.prov-title`, `.prov-sub`, `.prov-steps`, `.prov-step` (with `.done` / `.active` / `.todo` modifiers) (sessions.css ~line 576)

**Notes:**
- Map our existing startup phases (`creating`, `provisioning`, `booting`, `connecting`) to step rows. Each step: icon (`check_circle` for done, `progress_activity` for active, `radio_button_unchecked` for todo) + label + elapsed time (omit timing if we don't track it yet — the mockup shows it but it's optional).
- Reuse for both first-mount provisioning *and* resume-spinner cases — same component, different starting step.
- Phase ordering in the component: Thread created → Pod scheduled → Booting agent runtime → Connecting WebSocket → Loading project context. Align with what `chat.startupPhase()` actually emits.

---

### F3 — Resume card for ended sessions

**Status:** Done (2026-05-08). Combined with a state-model collapse: the thread lifecycle dropped `'idle'` (collapse migration `0002_collapse_thread_status.sql`), so idle-timeout now flips straight to `'ended'` and the manual end button was removed. The chat view fetches thread metadata before deciding to open the WS — `'ended'` skips the WebSocket entirely and renders the resume card, fixing the silent auto-reconnect on direct URL access. `session.ended` and `session.idle_timeout` events also flip the in-memory `threadStatus` so a live session transitions to the resume card without a refresh. The mockup's "Export thread" button is omitted (no export endpoint yet).

**What:** When a thread is in the `ended` status, render a read-only marker line ("Session ended {date}") followed by a Resume card (eyebrow + title + body + Resume / Export buttons), in place of the streaming + composer area.

**Replaces in current code:** currently there's no dedicated ended-state surface — ended threads still show the composer (just with the disconnect path) and only paused sessions get the system-message resume button. This adds a real ended state.

**Port from mockup:**
- Component: `StreamEnded` in `sessions-components.jsx`
- Styles: `.end-marker`, `.end-line`, `.end-tag`, `.resume-card`, `.resume-card .resume-eyebrow`, `.resume-card h3`, `.resume-card p`, `.resume-actions` (sessions.css ~line 697)

**Notes:**
- Trigger condition: `chat.threadStatus() === 'ended'` (or whatever the equivalent signal is in `PersistentChatService`).
- Hide the composer when ended.
- Resume button → existing `resumeSession()` flow on `SessionsPageComponent` (move helper into chat component or share via service).
- "Export thread" button is a placeholder for now; wire it later if we add a thread export endpoint, otherwise drop it.
- Keep the existing `historical` session-divider for resumed sessions — that's a different concept (mid-stream resume marker).

---

### F4 — Disconnected banner

**Status:** Done (2026-05-10). Shipped with a real reconnect engine in `PersistentChatService` (exponential backoff 1s→2s→4s→8s→16s→cap 30s, ±10% jitter, 12-attempt cap → "manual reconnect" state). Banner is mutually exclusive with the F3 resume card and the F2 startup card via an `isShowingReconnectBanner` computed (`disconnected + threadStatus==='active' + sessionReady + messages>0`). Last message dims via `.message.dimmed`. Composer stays disabled with an honest "Disconnected — reconnecting…" placeholder (the spec's "messages will queue" copy was aspirational — actual queueing is out of scope for v1). Bottom error-banner is suppressed while the reconnect banner is up to avoid doubling. **Caveat:** only handles server-initiated drops (orchestrator restart, agent crash, idle timeout). Silent client-side network blips do not fire `onclose` until the OS-level TCP keepalive trips (minutes), so the banner appears late or not at all in that case — see `docs/issues/persistent_chat_silent_disconnect.md`.

**What:** When the WebSocket drops mid-session, replace the bottom error-banner with an inline reconnect card placed in the message stream, and dim the previous tool/assistant turn that was in flight.

**Replaces in current code:** `.error-banner` styling for the disconnect case (the banner stays for actual errors). Also adds dimming for the last in-flight assistant message during disconnect.

**Port from mockup:**
- Component: `StreamDisconnected` in `sessions-components.jsx`
- Styles: `.reconnect-banner`, `.reconnect-banner > .ms`, `.rb-body`, `.msg.dimmed` (sessions.css ~line 672)

**Notes:**
- Card content: `cloud_off` icon + "Lost contact — reconnecting…" title + secondary line with attempt count + Retry-now button.
- Keep behavior tied to existing connection-state signals (`chat.isConnected()`, retry counters in the service).
- The composer stays disabled with the existing `Reconnecting — messages will queue…` placeholder.
- The error-banner element keeps its current role for non-connection errors.

---

### F5 — Jump-to-latest pill

**Status:** Done (2026-05-10). Pill is `position: sticky; bottom: 16px` inside the existing `.messages` flex column — no wrapper element needed. Visibility: `scrolledAway() && newMessageCount() > 0`. Tracking: an effect watches `chat.messages().length` and increments `newMessageCount` only when growth happens while `scrolledAway === true`; the count resets when the scroll handler reports near-bottom (within the existing 80px auto-scroll threshold), on click, and when the message list shrinks (thread switch). Sharp corners + uppercase display font keep it consistent with F4's visual vocabulary.

**What:** Add a floating pill anchored to the messages scroll container that appears when the user has scrolled up while new messages arrive. Shows "Jump to latest · N new" and scrolls to bottom on click.

**Adds to current code:** new overlay element inside `.messages` container; new scroll-position tracking signal.

**Port from mockup:**
- Element: `.jump-latest` button at the end of `StreamLong` in `sessions-components.jsx`
- Styles: `.jump-latest` (sessions.css ~line 742)

**Notes:**
- Trigger: when `messagesContainer.scrollTop` is more than ~200px above bottom AND new messages arrived since last bottom-anchor. Hide once user scrolls back to bottom.
- Reuse existing `onMessagesScroll()` handler in the component.
- Position: bottom-center of the messages area, above the composer, with shadow to lift off the chat.
- Skip the mockup's sticky day-divider (`.day-divider.sticky`) — we're not adopting day dividers.

---

### F6 — Tool call card visual upgrade

**Status:** Done (2026-05-10). Each expanded tool item is now a sharp-cornered `.tool-card` with a `.tool-head` strip (icon + optional approval badge + name in accent mono + `(args)` muted + right-aligned `.tool-status` pill with status-tinted icon) and a `.tool-body` containing the result `<pre>`. Status icon mapping: `check_circle` (success), `progress_activity` (warning, spin via `animation: spin 0.7s linear infinite`), `block` (muted) for denied, `radio_button_unchecked` (muted) for pending. Parent `.tool-summary` "Used N tools: …" header kept as-is. The streaming branch's per-tool list (`completedOnly()` results inside the live turn) now also gets the leading tool icon for parity with the historical branch — previously it had only a name+args+status row. The dead `.tool-preview` line (rendered inside `<details>` but hidden in both states by a `details[open] >` rule + browser default collapse) was removed; the head strip already carries enough context for the collapsed state. KV-shaped bodies for select tools are not implemented in v1 — same pre-formatted result as before. The approval-badge inside the head still has 4px-rounded corners; F7 will redo that surface holistically rather than nibble at it here.

**Error variant (2026-05-11):** Closed the original `'error'` caveat. The `ToolCallStatus` union now includes `'error'`, populated by an `is_error: bool` flag piped through `PersistentLoopCallbacks.on_tool_result` (both transport paths — `persistent_app.py` and `dual_app.py` — append it to the `tool.completed` WS payload). The two error branches in `persistent_graph.py` (tool-not-found, `ainvoke` exception) set it `True`; success path sets it `False`. Cockpit: `tool.completed` handler branches on `params['is_error']` to set `'error'` instead of `'completed'`, `statusIcon('error') → 'error'` (Material Symbol), `.tool-status.status-error` colors the pill `--danger`, and `.tool-card.tool-error` adds a 3px danger left border + tinted body so errored calls are visible even when collapsed. Errored cards also default to `open` (same treatment as denied), so the failure result is immediately readable. Summary helpers (`toolSummaryStatus`, `hasCompletedTools`, `completedOnly`, `completedToolCount`) now include `'error'` as a settled state. **Limitation:** historic-reload still surfaces errored calls as `'completed'` — the AI message's `tool_calls` JSON in `thread_messages` doesn't carry an `is_error` flag and the tool result content sits on a separate row, so error detection on reload would require joining tool rows back into the owning AI row at history-read time. Not done today; live sessions and same-session scrollback render correctly.

**What:** Restyle expanded tool-detail items to match the mockup's `.tool` card shape: header strip with icon + tool name (mono, accent color) + args (muted) + status pill (with status-tinted icon: completed green, running animated spinner, denied muted-block, error red); body with structured/preformatted result. Card has sharp corners, hairline border, and a consistent grid — replacing the current rounded `<details>` rows.

**Replaces in current code:** the `.tool-summary` details/summary structure and per-tool `.tool-detail-item` styling. Keep the same data — args, status, decision badge, result `<pre>`, preview line. Keep the `<details>` collapsing behavior. Keep tool-only inline rows for assistant messages without text.

**Port from mockup:**
- Component: `ToolCall` (and `ToolSkeleton` for streaming bodies) in `sessions-components.jsx`
- Styles: `.tool`, `.tool-head` (with `.name`, `.args`, `.status` and `.status.running/.rejected/.error` modifiers), `.tool-body`, `.tool-error`, `.tool-skeleton` (sessions.css ~line 430)
- Optional KV body shape from the mockup's `dl.kv` examples — we may use it for select tool results that already have structured fields (e.g., `create_worker_job` showing Job/Worker/Targets), but the default body remains the existing preformatted result text.

**Notes:**
- Keep the parent `.tool-summary` "Used N tools: ..." collapsible header — this is our pattern, the mockup doesn't have a multi-tool summary line. The card upgrade applies to each tool *inside* the expanded list.
- Map status colors to our tokens: completed → `--success`, running → `--warning` (with spin animation), denied/rejected → `--text-muted`, error → `--danger`.
- The mockup uses sharp corners / `border-radius: 0` — match that for the card chrome to break visually from the existing rounded-blob look.

---

### F7 — Approval card three-state visual upgrade

**Status:** Done (2026-05-10). Pending state is now `.mile`: sharp-cornered surface-0 card with a 3px accent-color left border, "Permission required" eyebrow (reuses `chat.permission.title`), human-readable title via `permissionTitle(perm)` — which delegates to the existing `toolLabel()` so per-tool i18n labels and contextual hints (filename, tool args) flow through unchanged — a mono detail line showing tool icon + `<code>tool</code>` + `<code>(args)</code>`, and the existing 3-button row (Approve / Auto-accept / Deny). The mockup's "Discuss" button is dropped — no equivalent flow today. Resolved trail markers (`.mile-resolved`) render persistently in the message stream below the tool-summary collapse, one per tool with `tc.decision` set: leading icon (`check_circle` for approved, `block` for denied), uppercase label (reuses `chat.approval.badge.{approved,denied}` — copy stays "Denied" rather than the spec's "Rejected" to match domain language), tool name, right-aligned timestamp from `msg.timestamp`. Approved strips get a `--success` left border + label color, denied strips stay muted. The streaming branch renders the same strip without a timestamp (transient view, becomes historical on persist). The small `approval-badge` chip inside the `tool-card` head (introduced by F6) is kept as a redundant inline marker — both the chip and the standalone strip render, matching the spec's "before/after" framing. **Caveats:** The `approval-badge` chip retains its 4px rounded corners — sharp-cornering it in isolation looked too aggressive next to the chip's tiny font size; revisit if the visual mismatch becomes load-bearing.

**Reject → Stop rewire (2026-05-11):** The third button was renamed from "Deny" / "Ablehnen" to "Stop" / "Stoppen" and its handler swapped from `chat.deny()` to a new `chat.stop()` that chains `deny()` + `interrupt()`. The deny still goes out (so the backend's `permission_check` await isn't stranded — the agent's loop would otherwise block forever), but `interrupt` fires right after so the next loop iteration bails out instead of acting on the denial. The user then holds the floor and types a follow-up clarification, which lands in a fresh turn with the denied ToolMessage in context. This replaces the mockup's "rejection reason" capture flow — talking back to the agent is more honest than a single-shot "no, because…" field that nobody fills out properly. Closes the prior reason-capture follow-up. The `.resolved-reason` slot stays unused; if a future flow ever wants to surface "why" alongside the trail marker, it can read from the user's next message. The persisted `decision` field still records `'denied'` (data-model state); the resolved trail marker still labels it "Denied" — the action verb ("Stop") and the result state ("Denied") are deliberately different vocabulary. **Limitation:** during the brief window between the WS `deny` and `interrupt` messages, the agent could in theory advance one LLM call before checking the interrupt flag. Acceptable — that next call is just reasoning, no side effects.

**What:** Replace the existing `.approval-card` with the mockup's MileMarker shape, and add the two **resolved** states (approved / rejected) as inline trail markers that appear in place of the resolved decision badge on the tool row.

**Replaces in current code:**
- Pending state: `.approval-card`, `.approval-card-head`, `.approval-card-tool`, `.approval-card-actions` block.
- Resolved state: today, decisions render as a small `approval-badge` (`approval-approved` / `approval-denied`) prefixed onto the tool-detail header. We continue rendering that badge inside the tool card (F6), AND additionally render a standalone resolved trail marker in the message stream when the decision is freshly made, so the approval flow has a coherent before/after.

**Port from mockup:**
- Component: `MileMarker` (with `state="pending"` / `"approved"` / `"rejected"`) in `sessions-components.jsx`
- Styles: `.mile`, `.mile .label`, `.mile .title`, `.mile .detail`, `.mile .actions`, `.mile-resolved` (with `.approved` and `.rejected` modifiers), `.mile-resolved .resolved-label/.resolved-title/.resolved-reason/.resolved-time` (sessions.css ~line 498)

**Notes:**
- Pending card content: eyebrow ("Permission required" — pick a non-Roman label), title (the action being requested, can include inline `<code>` highlights for path/tool), detail line (tool + target + scope hints), Approve / Auto-accept / Stop buttons. Keep our existing 3-button set; the mockup's "Discuss" button is not adopted — the new "Stop" affordance covers the discuss-then-retry case by halting and yielding the floor to the user.
- Resolved trail marker is a single-line collapsed strip with leading icon + label ("Approved" / "Rejected") + title + (for rejected) quoted reason + timestamp, with a left-border accent in success or muted color.
- Sharp corners + hairline borders match the rest of the refresh; no rounded blob.
- Keep existing semantics for `chat.pendingPermission()`, `chat.approve()`, `chat.deny()`, and the auto-accept variant.

---

### F8 — Composer redesign

**Status:** Done (2026-05-10). Replaced the rounded `.input-card` pill with a stacked `.composer` (sharp-cornered surface-0 card with hairline border and accent `outline` on `:focus-within`), wrapped in a `.composer-wrap` that owns the top divider and outer padding. Textarea is now a full-width 56px-min block above a `.composer-row` strip containing `Attach` / `Mention` placeholders (icon + label, `aria-disabled="true"`, tooltips reuse `chat.composer.attachComing` / `mentionComing`), a flex `spacer`, and the round Send button. The mockup's `tune` button in the row is dropped — settings live in the header cog already, no duplication. The round send button keeps its existing Send ↔ Stop ↔ Spinner morph (renamed from `.action-btn` to `.send` to match the mockup vocabulary). Slash-menu positioning is preserved by giving `.composer` `position: relative` so `bottom: 100%` still anchors to the composer card. Per-state placeholder text (`inputPlaceholder()` computed) is untouched — all five branches (reconnecting / connect / sessionStarting / stopping / working / default) continue to flow through unchanged. On the mobile breakpoint (≤600px) `.composer-wrap` tightens its padding and the `Attach` / `Mention` labels collapse to icons-only via `.ctrl-label { display: none; }`. **Caveats:** Attach and Mention are visual placeholders only — no click handlers, no underlying flow; they fire the title-tooltip to set expectations. The focused-state `outline: 2px solid var(--accent-color)` is more aggressive than the previous box-shadow ring; it visually matches the rest of the F4/F6/F7 sharp-cornered surfaces.

**What:** Replace the current rounded pill `.input-card` with the mockup's stacked composer: a tall textarea on top, an action row below it containing **Attach** (placeholder, no-op), **Mention** (placeholder, no-op), spacer, and the round Send button. Drop the in-composer settings/tune affordance (settings stays in the header cog). Keep the slash-command autocomplete, the streaming-interrupt morph (Send ↔ Stop ↔ spinner), and per-state placeholder text.

**Replaces in current code:** `.input-card`, `.chat-input`, `.action-btn` block in the `<div class="input-area">` template + styles.

**Port from mockup:**
- Component: `Composer` in `sessions-components.jsx`
- Styles: `.composer-wrap`, `.composer`, `.composer textarea`, `.composer-row`, `.composer-row .ctrl`, `.composer-row .send`, `.composer.disabled` (sessions.css ~line 787)

**Notes:**
- Attach / Mention buttons render with their icons (`attach_file`, `alternate_email`) and labels but have no click handlers and an `aria-disabled="true"` (or just plain disabled with cursor-default styling). Translation keys for tooltips: `chat.composer.attachComing`, `chat.composer.mentionComing`.
- Drop the mockup's `tune` button in the composer row — settings live in the header cog already; don't duplicate.
- Keep the slash menu's existing positioning logic (`position: absolute; bottom: 100%`); the new composer chrome shouldn't break it.
- The Send button stays round and morphs to Stop during streaming — that's our pattern, the mockup doesn't have it but we keep it.
- Keep the focused-state border highlight (currently `--accent-color` ring); the mockup's `:focus-within` rule maps directly.

---

## Suggested ordering

1. ~~**F1** Empty state~~ — done 2026-05-08
2. ~~**F2** Provisioning step list~~ — done 2026-05-08
3. ~~**F5** Jump-to-latest pill~~ — done 2026-05-10
4. ~~**F3** Resume card for ended sessions~~ — done 2026-05-08 (bundled with thread-status collapse)
5. ~~**F4** Disconnected banner~~ — done 2026-05-10 (with reconnect engine; silent-drop heartbeat tracked separately as a follow-up)
6. ~~**F6** Tool card visual upgrade~~ — done 2026-05-10
7. ~~**F7** Approval three-state~~ — done 2026-05-10
8. ~~**F8** Composer redesign~~ — done 2026-05-10 (stacked composer with Attach/Mention placeholders; mockup `tune` button dropped)

## Cross-cutting notes

- Whenever a feature introduces a new surface, add transloco keys under the `chat.*` namespace and update both `cockpit/src/assets/i18n/en.json` and `de.json`.
- Preserve all `aria-label` / tooltip behavior on existing buttons; the mockup does not annotate accessibility, so we have to fill in.
- When touching the messages container styles, watch the `historical` / `tool-only` / `session-divider` cases — they're easy to break with naive replacements.
- Take before/after screenshots at desktop width (~1280px) for each feature PR; the mockup is desktop-first and the refresh is too. Check the mobile breakpoint after each feature lands but expect minor follow-ups (e.g., shrinking the resume card's two-column action row to stacked).
