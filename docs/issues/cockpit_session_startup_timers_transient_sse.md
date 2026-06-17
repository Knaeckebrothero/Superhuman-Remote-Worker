# Session-startup step timers blank out when an SSE drop races startup

**Status**: OPEN — cosmetic, not yet fixed. Found 2026-06-17 on k3d while testing
user-defined experts: the "Starting session" card's per-step timers vanished
during orchestrator hot-reload churn, then came back on their own once the
orchestrator stayed up. **Not** a regression in the timing code (last touched
2026-05-15, `314248f7`); a transient event-delivery gap.

**Severity**: Low — purely cosmetic. The session still provisions, connects, and
runs correctly; only the elapsed-time labels on the startup card are affected.

## Symptom

The "Starting session" card shows four steps: **Creating thread → Provisioning
agent → Booting agent runtime → Establishing connection**. When a session starts
during / just after an orchestrator restart:

- "Creating thread" shows its duration (e.g. `3.8s`).
- "Provisioning agent" (and sometimes "Booting agent runtime") show **no number**
  ("–").
- The card appears to **jump** straight from "Provisioning agent" into the live
  session, skipping the intermediate steps.

## Root cause

The cockpit times the server-driven steps from **transient `session.lifecycle`
SSE events** (`provisioning` → `booting` → `ready`) emitted by the orchestrator
in `_do_prepare`. The chain:

- orchestrator `_emit(state)` → `session_lifecycle.emit()` → SSE notification feed
  (`orchestrator/routers/sessions.py`: `provisioning` up front, then `booting`,
  then `ready`).
- cockpit `notification.service.ts` receives `session.lifecycle` frames and stores
  **only the latest** in a signal: `lifecycleEvent = signal<…|null>(null)` (~L73,
  `.set()` at ~L206).
- `persistent-chat.service.ts` maps `provisioning`/`booting` → `startupPhase`,
  `ready` → `connecting` (~L233-242).
- `persistent-chat.component.ts` records a step's **start time only when
  `startupPhase()` explicitly equals that phase** (timing `effect`, ~L1831/1842).
  The *display* (`startupSteps`) falls back to a `completedCount` floor to mark a
  step active even with no live phase signal (~L1657) — but a step shown via that
  fallback has **no recorded start → no duration → "–"**.

So whenever the SSE feed is **down or reconnecting** at the instant the
orchestrator emits `provisioning`/`booting` (which is right at session startup),
the cockpit **misses** those frames, `startupPhase` never passes through them, and
the steps render untimed. "Creating thread" is immune because it is set
**client-side** (`startupPhase.set('creating')`), so a dropped feed can't lose it.

The 2026-06-17 trigger was self-inflicted: editing the orchestrator under uvicorn
`--reload` (tilt live_update) caused repeated WatchFiles reloads whose graceful
shutdown ("waiting for connections to close") hung on long-lived SSE/WS
connections → liveness-probe kill → pod restart → SSE feed dropped during a
session's startup window.

## Why it matters beyond the dev loop

The same loss happens for **any** session whose startup races an orchestrator
restart — including a real **prod deploy / rolling restart / OOM-restart**. A user
who opens a session during a deploy window sees the same blank timers + jump:
the progress card looks broken precisely when the system is changing. The session
itself is fine; it is only the indicator that misleads.

Secondary (unconfirmed) fragility on the same design: the latest-value
`lifecycleEvent` signal coalesces a fast burst of frames — if
`provisioning`/`booting`/`ready` land within one change-detection flush, the
glitch-free consumer may observe only the final state. The restart / SSE-drop path
is the demonstrated trigger; the burst path is plausible but not proven.

## Hardening options (someday — not now)

1. **Durable / replayed phase** — have `/api/sessions/{id}/connection` (which the
   cockpit already polls) also return the *current* lifecycle phase, so a late or
   reconnecting subscriber reconstructs which steps are done instead of depending
   on having caught every transient frame. *Recommended:* fixes both the
   restart-race and the burst-coalesce paths and rides the existing poll.
2. **Ordered queue instead of latest-value** — deliver `session.lifecycle` as a
   queue the consumer drains, so a burst can't coalesce.
3. **Backfill durations** — when `ready`/`connecting` arrives, retro-record start
   times for any skipped phase so steps show real / near-zero times, not "–".
4. **Dev-only:** tune the `--reload` shutdown (shorter grace / drop SSE on SIGTERM)
   so reloads stop tripping the liveness probe. Removes the dev-loop noise but does
   nothing for the prod-deploy case.

## Pointers

- `orchestrator/routers/sessions.py` — `_do_prepare` `_emit(...)` sequence.
- `orchestrator/services/session_lifecycle.py` — `emit()`.
- `cockpit/src/app/core/services/notification.service.ts` — `lifecycleEvent`
  latest-value signal (~L73, set ~L206).
- `cockpit/src/app/core/services/persistent-chat.service.ts` — lifecycle →
  `startupPhase` mapping (~L218-244).
- `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts` —
  `STARTUP_PHASE_ORDER` (~L1624), `startupSteps` display + fallback (~L1637-1671),
  timing `effect` (~L1804-1858).
