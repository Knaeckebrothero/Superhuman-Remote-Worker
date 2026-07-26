---
tags:
  - feature
  - cockpit
  - ux
  - reasoning
---
# Reasoning-Aware Progress Indicator — "Thinking 3m35s · max effort"

When the agent is waiting on the **model** (a reasoning step), cockpit currently shows a **warning** badge reading "Working — no output for 215 s". A slow-but-healthy `effort=max` think therefore reads as a frozen/broken session. This feature reframes that in-flight status into a positive, ticking signal — the way Claude Code / Codex show "Thinking 35 s · high effort" — so a long reason step is legible as *progress*, not a hang.

**Motivating incident:** session `accfbc56` (2026-07-23) — a `gpt-5.6-sol effort=max` turn took **15 minutes** to respond (the upstream ChatGPT/codex backend was degraded, but the response was within the 2245 s client timeout, so nothing errored). The UI showed "Working — no output for 215 s" as a warning; the user read it as stuck. Root cause of the slowness is a separate concern (see *References*); this feature is purely about **making an in-flight reason step look like progress**, even when no reasoning text is available.

## What already exists (the substrate)

Everything this needs is already client-side — no backend work.

- **Render site:** `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts:768` —
  `<app-badge tone="warning" size="sm">{{ 'chat.status.agentQuiet' | transloco:{ seconds: chat.agentSilenceSeconds() } }}</app-badge>`.
- **Copy:** `cockpit/src/assets/i18n/en.json` (+ `de-DE.json`) → `chat.status.agentQuiet` = "Working — no output for {{seconds}}s".
- **Elapsed timer:** `chat.agentSilenceSeconds()` already ticks (seconds since last output).
- **Reasoning effort:** the resolved session config carries `reasoning_level` (`cockpit/src/app/core/models/api.model.ts:615,633`); values `low|medium|high|max`.
- **Turn model:** `cockpit/src/app/core/models/turn.model.ts` — `AssistantTurnStatus='streaming'`, per-event `startedAt`, `tool_call` events, in-flight-turn id (~:145). Reducer `turn-reducer.ts`. So "waiting on tool vs. model" and "answer streaming vs. not" are already derivable.
- **Thinking-bubble styling:** `persistent-chat.component.scss:934,986` (the dots) — reuse for visual consistency.

## Locked decisions

- **Cockpit-only.** No backend/transport changes for v1.
- **Reframe, don't add.** Replace the single `agentQuiet` warning badge with a wait-state-aware badge.
- **Tone: `warning` → `info`.** The warning tone is itself part of the "looks broken" signal for a normal wait.
- **Copy:** "Thinking {{time}} · {{effort}} effort" while waiting on the model (approved). Neutral "Working {{time}}" otherwise. Drop the "no output for" framing.
- **Time as `3m35s`, not `215s`.**
- **Out of scope (v1):**
  - Streaming the *actual* reasoning text ("live thoughts"). The live "Thinking" bubble already exists for paths where capture works; this feature does not touch that fragile codex-capture chain.
  - Any "taking unusually long" / degradation escalation or fail-fast — that belongs with the cooldown/degradation-pause work, not here.

## Architecture

### Wait-state derivation
A computed selector (in `persistent-chat.service` / off the turn reducer) classifies the in-flight turn:

- **`reasoning`** — turn `streaming`, **no `tool_call` step active**, **no answer text yet** (since last output), and resolved `reasoning_level` is set (≠ null/`none`).
- **`tool`** — a `tool_call` step is active → **badge hidden** (tools render their own progress; the quiet badge must not fire here).
- **`generating`** — answer text streaming → no badge.
- **`working`** — in-flight, non-reasoning model / unknown `reasoning_level`.

Only `reasoning` and `working` produce a badge, and only once `agentSilenceSeconds()` crosses the existing show threshold.

### Badge reframe (`persistent-chat.component.ts:768`)
| wait state | tone | text (new transloco key) |
|---|---|---|
| `reasoning` | `info` | `chat.status.agentThinking` = "Thinking {{time}} · {{effort}} effort" |
| `working` | `info` | `chat.status.agentWorking` = "Working {{time}}" |
| `tool` / `generating` | — | (no badge) |

`agentQuiet` is removed/repurposed; add `agentThinking` + `agentWorking` to `en.json` **and** `de-DE.json`. If `reasoning_level` is null/`none`, drop the suffix → "Thinking {{time}}".

### Effort label
Map `reasoning_level` → "{{level}} effort" (`max`→"max effort", …), centralized so it matches the settings-UI wording.

### Time formatting
Small pure fn / pipe on the `{{time}}` slot: `< 60 s` → "{{n}}s"; `≥ 60 s` → "{{m}}m{{s}}s" (e.g. 215 → "3m35s").

### Frontend touch points
- `persistent-chat.component.ts` (badge template + wait-state binding)
- `persistent-chat.service.ts` / `turn-reducer.ts` (wait-state selector)
- `assets/i18n/en.json` + `de-DE.json` (new keys)
- a duration formatter + the effort-label map (co-located util)

## Testing
- **Unit — wait-state selector:** turn-model fixtures assert transitions (`reasoning → tool → generating → done`), "reasoning only while no tool active and no answer yet", and "null `reasoning_level` → working".
- **Unit — effort-label map** and **duration formatter** (59 s, 60 s, 215 s→"3m35s", 3600 s).
- Tone/text template change is covered by the component spec + the selector tests. Keep `test:e2e:canvas` / the persistent-chat spec green (old-key assertions).

## Open questions
- Confirm `agentSilenceSeconds()` (since last output) reads correctly as "thinking time" during a pre-answer reason step (it should: last output = turn start / last tool result).
- Confirm the resolved session config (`reasoning_level`) is in scope at the badge site in `persistent-chat.component`.

## References
- Root cause of the 15-min think (upstream degradation, *not* a code hang): memory `srw_codex_stream_stall_no_timeout`.
- Live-thoughts (Tier B, out of scope) fragility: `docs/issues/reasoning_capture_regressions_on_routing_and_factory_changes.md`, `docs/issues/langchain_responses_api_streaming.md`.
- Degradation / fail-fast (separate workstream): memory `srw_cooldown_pause_feature`.
