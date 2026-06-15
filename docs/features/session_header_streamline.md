---
tags:
  - feature
  - cockpit
  - sessions
  - ui
related:
  - "[[job_settings_overhaul]]"
  - "[[builder_to_sessions_consolidation]]"
  - "[[session_turn_rendering]]"
  - "[[design_system_completion]]"
---

# Persistent Session Header Streamline

> The persistent-session header stacks **three full-width rows** before the conversation starts, and two of them show the *same three facts* (model, temperature, mode) at different densities — once as a read-only badge, once as an editable control. Collapse the redundancy: fold the status chips up onto the title row, make the chips the affordance that opens settings, and turn the settings panel from a content-shoving row into an anchored popover. Separately, two of the six "settings" aren't session settings at all — they're device-local view prefs that belong elsewhere.

**Status:** Design / plan of record. Drafted 2026-06-15. Nothing built.
**Scope:** `cockpit/src/app/views/persistent-chat/` only. No backend, no API, no agent changes.

## The real issue: three rows, overlapping data

What reads as "a header plus an extra details panel plus a settings panel" is actually three stacked full-width rows rendered before the transcript:

1. **`.chat-header`** — sidebar toggle · back · 🤖 · title · `0d2be046` · ● Connected | `tune` · Files · Git · IDE · Disconnect
2. **`.status-bar`** — read-only badges: `minimax/minimax-m3` · `temp 1` · `Turn 1` · `Supervised` (+ transient: agent-quiet, compaction)
3. **`.settings-panel`** — collapsible editors: Mode · Narration · Reasoning · Tool calls · Model · Temperature

The header feels heavy not because there's too much information, but because the same fields appear twice. Sort every field into three buckets:

| Bucket | Fields | Notes |
|---|---|---|
| **Mirrored** (badge *and* editor) | Model, Temperature, Mode | Rendered twice — row 2 read-only, row 3 editable |
| **Settings-panel only** | Narration, Reasoning, Tool calls | Of these, Reasoning + Tool calls are *not session config* — see below |
| **Status-bar only** | Turn count, agent-quiet warning, compaction progress | Genuine runtime telemetry, not config |

The redundant bucket is the thing to collapse. The cleanest framing: **the chips are the collapsed view of the settings; the panel is the expanded view.** They should be one conceptual surface backed by one source of truth, not two rows that happen to read the same signals.

## Second finding: two "settings" aren't session settings

`chat-preferences.service.ts` stores **Reasoning** and **Tool calls** in `localStorage` (keys `cockpit:chat:reasoningExpanded`, `cockpit:chat:toolCallsExpanded`). The service's own docstring places them in the same bucket as the theme: *"per-device viewing choices… nothing here is sent to the agent; it only governs how the client renders what the agent already produced."*

So they are miscategorized by sitting between **Model** and **Temperature**, which genuinely change agent behavior. They render reasoning blocks / tool-call runs expanded-by-default; they don't configure the session. That makes them a natural "move it somewhere else" target (a small view menu or user-level display settings), leaving the session-scoped controls as just **Model, Temperature, Mode** — and **Narration**, which *is* session-level (`chat.narrationMode()`).

## Current surface (verified 2026-06-15)

All in `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` (inline template) + `.scss`.

| Piece | Template | SCSS | Backing signal / handler |
|---|---|---|---|
| Header row | `:378–432` | `.chat-header` `:63` | — |
| Title / id / status | `:386–391` | `.header-title` `:93`, `.header-session-id` `:99`, `.status-dot` `:105`, `.status-label` `:122` | `chat.sessionTitle()`, `chat.threadId()`, `connectionClass()`, `connectionLabel()` |
| Settings toggle (`tune`) | `:394–399` | `.settings-btn` `:203` | `showSettings` signal |
| Files / Git / IDE / Disconnect | `:401–430` | `.ide-btn` `:244` | `openSessionFiles()`, `ideStatus()`, `openIde()`, `openCodeServer()`, `disconnectAndLeave()` |
| **Status bar** (chips) | `:434–452` | `.status-bar` `:129` | `chat.modelName()`, `chat.temperature()`, `chat.turnCount()`, `chat.agentSilenceSeconds()`, `chat.compaction()`, `chat.permissionMode()` |
| **Settings panel** | `:454–519` | `.settings-panel` `:219` | `onPermissionModeChange`, `onNarrationModeSelect`, `onReasoningDefaultChange`, `onToolCallsDefaultChange`, `onModelSelect`, `onTemperatureChange` |
| Mobile reflow | — | `@media (max-width:768px)` `:2207` (`.chat-header { flex-wrap: wrap }`) | — |

Transloco namespaces: `chat.header.*` (buttons), `chat.status.*` (chips), `chat.settings.*` (panel labels/options).

**Redundancy, concretely:** the status-bar's model/temp/mode badges read `chat.modelName()` / `chat.temperature()` / `chat.permissionMode()` — the *identical* signals the settings panel's Model / Temperature / Mode controls bind to.

## Proposed design

### Phase 1 — fold the status bar into the title row (low-risk, high-payoff)

Removes a permanent full-width row and ends the model/temp/mode duplication.

1. **Collapse the connection label to a dot when connected.** `● Connected` is pure noise in the 99% connected case. Keep the colored dot always; only render `connectionLabel()` text on `connecting` / `disconnected` / `error`. This frees the horizontal space the next steps need.
2. **Truncate the title.** Auto-generated titles run long ("Exploring the softDsim development repository"). Add `max-width` + `text-overflow: ellipsis` to `.header-title` so it can't crowd out the chips; full title stays available via `title=` tooltip.
3. **Move the config chips onto the title row.** Render `model · temp · mode · turn` after the title behind a thin divider (`.header-left`, after the status dot). The dedicated `.status-bar` row is deleted.
4. **Make the chip cluster the settings affordance.** Clicking the cluster opens settings. Now chips = collapsed settings, panel = expanded settings — one source of truth, no duplicated facts. The standalone `tune` button (`:394–399`) becomes redundant; drop it, or keep it wired to the same `showSettings` toggle (see open question).
5. **Keep the transient badges distinct.** Agent-quiet (`:444`) and compaction progress (`:447`) are genuine, temporary, semi-actionable status — not config. They should be able to surface on their own (inline in the chip row while active, or a slim ephemeral strip), and must not justify a permanent always-on row.

Result: header goes from **three rows → one row** (+ the on-demand settings + the existing task-bar, which is unchanged).

### Phase 2 — settings as an anchored popover, and re-home the view prefs

1. **Popover instead of a content-shoving row.** Today `.settings-panel` is a full-width block that pushes the transcript down every time it toggles (layout shift on every tweak). Convert it to a popover/dropdown anchored under the chip cluster, floating over the conversation. Layout stays stable.
2. **Re-home Reasoning + Tool calls.** These are device-local *view* prefs (`chat-preferences.service.ts`, localStorage). Move them out of the session popover into a small "view" menu or user-level display settings. The session popover then holds only what changes agent behavior: **Model, Temperature, Mode, Narration.**

### Responsive degradation

`.chat-header` already has `flex-wrap: wrap` at ≤768px. The chip cluster wraps below the title on narrow widths — i.e. it gracefully degrades back toward today's stacked layout when there isn't room for one line. No separate mobile design needed; just make sure the chip cluster is its own wrappable flex group.

## Open question (drives the markup)

**Is the chip cluster *the* settings affordance, or do we keep a distinct gear?**

- **Chips-as-affordance (recommended):** chips are clickable and open the popover; drop the `tune` button. Fewest elements, clearest "what you see is what you can edit" model. Risk: a few users expect a gear icon; mitigate with hover affordance + cursor.
- **Keep the gear:** chips stay purely informational; `tune` stays as the opener. More conventional, but keeps an extra control and a weaker chip→panel mental link.

This choice determines whether chips get a click handler + button semantics or stay as plain `app-badge`s, so it's worth settling before sketching the template.

## Out of scope / not changing

- The right-side action cluster (Files / Git / IDE / Disconnect) — it works and is well-placed. Disconnect already reads as separated from the open-surface trio.
- The task bar (`:521`).
- Any backend, API, or `chat.*` signal contracts. This is a pure presentation refactor of one component's template + SCSS.

## Related work

- [[job_settings_overhaul]] introduces a shared settings surface for job **and** session creation (resolved defaults, progressive disclosure, presets). The popover here should reuse that surface's controls/visual language rather than invent a parallel one — coordinate so session-create settings and in-session settings look like one system.
- [[session_turn_rendering]] owns the Reasoning / Tool-calls *rendering* that the view prefs toggle; re-homing those prefs (Phase 2) touches the same area.
