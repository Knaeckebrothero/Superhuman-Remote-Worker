---
tags:
  - issue
  - architecture
  - agent-api
  - persistent-sessions
related:
  - "[[session_agent_drift_drain_kills_idle_sessions]]"
  - "[[lifecycle_session_agents_without_thread_never_drain]]"
  - "[[session_config_name_plumbing]]"
  - "[[officer_blind_reads_and_worker_bureaucracy]]"
---

# Three agent API apps, one behavior contract — dual_app re-implements instead of composing

**Status:** 🔴 **OPEN** — structural debt, filed for a deliberate later fix.
Filed 2026-07-30 from the stale-Centurion-pod incident (fourth incident of
this class; receipts below).

## The shape

`agent.py --mode` selects one of three sibling FastAPI app modules that are
supposed to implement the same per-pod behavior contract (registration,
heartbeat + intent handling, stop machinery, job execution, session
hosting):

| Module | Size | Mode | Spawned in production by |
|---|---|---|---|
| `src/api/app.py` | ~45 KB | `worker` | **nothing** — repo-wide, no spawner passes `--mode worker`; it exists only in docs/examples |
| `src/api/persistent_app.py` | ~312 KB | `persistent` | dedicated session pods (`agent_provisioner` `purpose="session"`, `persistent_provisioner`) |
| `src/api/dual_app.py` | ~55 KB | *(default)* | everything else: pool pods, per-job pods, VM agents |

So the **deployed default is dual_app**, while `app.py` is a
production-zombie that still serves as the "reference" other code is ported
from, and `persistent_app.py` is both a standalone app *and* the session
engine dual_app drives.

dual_app's relationship to persistent_app is a **partial composition**:
- Delegated: session attach seeds `pa._agent/_orchestrator_client/...` and
  calls `pa._attach_session()`; `/api/{input,interrupt,approve}` are
  explicit mirrors that forward to `pa`; `_session_ready()` and
  `_detach_session` are imported.
- Re-implemented: the heartbeat-response intent handler
  (`_handle_heartbeat_intents`), pod-state accounting, the stop machinery,
  and the preemption backstop are copies with their own module globals —
  the P0-D backstop even labels itself "Faithful port of the app.py
  backstop" (`dual_app.py:170`).

Nothing — no shared function, no protocol, no cross-app test — forces the
copies to converge again after one of them changes.

## Failure mode

A behavior fix lands in one sibling and silently misses the app that
actually runs in production. Divergence is only ever discovered by
incident. Four receipts:

1. **Session drift-drain (this filing's trigger).** The 2026-06-12 fix
   ([[session_agent_drift_drain_kills_idle_sessions]]) gave `persistent_app`
   clean drain-suspend semantics for sessions (defer while a turn is in
   flight, suspend when parked). `dual_app`'s handler kept worker-only
   semantics: exit if `IDLE`, else "the graph picks this up at the next
   phase boundary" — a boundary a session never reaches. Result on
   2026-07-30: the Better-Resavio Centurion's session, adopted by a
   dual-mode pool pod, survived **every** image roll; the reconciler set
   `should_drain` ticks for hours and the intent dead-lettered. The officer
   ran a full day on a stale image with a pre-P1 tool schema. (The session
   leg was ported into dual_app the same day — by hand, which is itself
   another instance of the pattern this doc is about.)
2. **Detached-session wedge.**
   [[lifecycle_session_agents_without_thread_never_drain]] gap #1: pre-06-10
   dual-mode pods logged the worker "will freeze at next phase boundary"
   line on session pods and survived 5–7 days of image bumps.
3. **config_name plumbing.** [[session_config_name_plumbing]] hole B: the
   fix was verified against `persistent_app`'s `/session/attach`; dual_app's
   mirror route "answered 200 and silently dropped the field" — the first
   live verify missed that the deployed pool runs the other app.
4. **P0-D preemption backstop.** Built and tested in `app.py`, then
   hand-ported to dual_app with a second test class purely to guard the
   port ([[officer_blind_reads_and_worker_bureaucracy]] P0-D). The port was
   necessary only because the handler exists twice.

## Cost

Every heartbeat-intent, lifecycle, or session feature must be designed,
implemented, and tested up to three times; reviewers must know which app a
pod actually runs (spawn-path dependent — the same *thread* gets different
bug sets depending on whether it landed on a dedicated pod or an adopted
pool pod); and the divergences surface as production incidents on the
default app, not as test failures.

## Direction (for the later fix — not a committed design)

- **Option A (preferred): one app module.** Mode becomes a boot capability
  flag that decides which routers mount and which `PodState`s are legal.
  `_handle_heartbeat_intents` exists exactly once and dispatches on pod
  state. `app.py` is deleted (no spawners); `--mode worker` becomes
  dual-with-sessions-disabled.
- **Option B (cheaper): finish the composition dual_app half-does.**
  dual_app stays the only *app*; `persistent_app` shrinks to a session
  engine (loop, attach, suspend) with no FastAPI surface of its own —
  its router is mounted, not mirrored. `--mode persistent` becomes
  dual-with-jobs-disabled. `app.py` deleted either way.

Acceptance for either option:
- Exactly one definition of the heartbeat intent handler in `src/api/`.
- One test suite drives it through every pod state (idle / working /
  session-parked / session-turn-in-flight / attach window) and covers the
  drain, preemption, and guidance-inbox behaviors together.
- No route exists twice ("mirror" comments gone).
- Removal of `--mode worker` + `app.py` noted in README/CLAUDE.md.

## Non-goals

This is not the [[lifecycle_session_agents_without_thread_never_drain]]
orchestrator-side fix (intent-vs-observation status authority) — that gap
survives an app merge and keeps its own doc.
