# Agent — `/ready` and the WS handler drift because the agent has three FastAPI apps

**Date:** 2026-05-20
**Status:** Quick fix shipped in `b2e2e9e9` (live-verified on dev cluster 2026-05-20). Proper fix (app unification) pending — design doc to follow.
**Component:** `src/api/{app,dual_app,persistent_app}.py`

## Summary

A persistent session opened against a pool agent flashed "Agent not ready" in the cockpit for ~1 second before the chat became usable, accompanied by 3× `[persistent-chat] backend error: Agent not ready` log lines and a single 504 from a thread-id-bearing URL. On fresh agent pods (where K8s had to spin up a new container) the flash didn't occur — the user saw the provisioning timer instead and the session connected cleanly.

Root cause was a drift bug: the agent's `/ready` probe and the WebSocket handler used **different** definitions of "session ready". The probe flipped true earlier than the WS handler's gate, so the orchestrator opened the upstream WS during a sub-second window where the agent's loop primitives weren't yet initialized — and the agent rejected it with code 4503.

The drift was possible because the agent has **three FastAPI apps** (`app.py`, `dual_app.py`, `persistent_app.py`), each with its own `/ready` endpoint. Two of the three rewrote the same check inline and the third was the canonical WS gate. The quick fix routes all three through one helper. The proper fix is to unify the apps so this class of drift is structurally impossible.

## Symptom (observed 2026-05-20, `develop` at `0a193cca`)

User opens a new persistent session. Console:

```
3main-JUDDRJZT.js:95 [persistent-chat] backend error: Agent not ready
011f8ac7-6618-44f3-b3a5-cb723f677135:1  Failed to load resource: 504 (Gateway Timeout)
2main-JUDDRJZT.js:95 [persistent-chat] backend error: Agent not ready
```

Behavior matrix:

| Scenario | Result |
|---|---|
| Pool agent reused (existing `srw-agent-s-*` pod) | ~1 s "Agent not ready" flash, then session connects normally |
| New agent pod spun up | ~9 s provisioning timer, no flash, session connects normally |

`markSessionReady()` already cleared the stale error from the UI banner (`cockpit/src/app/core/services/persistent-chat.service.ts:1271`), but the underlying WS reconnect storm + the 504 were still happening.

## Root cause

The agent has three readiness checks that don't agree.

| File | Line | In-session check | Status |
|---|---|---|---|
| `src/api/app.py:610` | worker only | n/a (no session mode) | OK (irrelevant path) |
| `src/api/dual_app.py:617` | dual mode (the cluster path) | `_session && llm_with_tools` | **drifted — two-way** |
| `src/api/persistent_app.py:1228` | pure-persistent (standalone) | `_session && llm_with_tools` | **drifted — two-way** |
| `src/api/persistent_app.py:1488` (WS handler) | the canonical gate | `_session && llm_with_tools && _loop_user_queue is not None` | **strict — three-way** |
| `src/api/dual_app.py:1037` (`/session/status`) | unused | strict three-way | dead code |

Inside `_attach_session()` (`src/api/persistent_app.py:502`), the two state variables that matter are set **far apart**:

1. `_session.llm_with_tools` is set at the end of `PersistentSession.setup()` (`src/api/persistent_session.py:482`).
2. `_loop_user_queue = asyncio.Queue()` is set last, at `src/api/persistent_app.py:996`, **after** repo clone, cloud sync `pull_all()`, message restore, and the `thread_status='active'` DB update.

Between those points, multiple awaits — typically hundreds of ms to a few seconds. The dual-mode `/ready` (line 617) reports `ready=true` as soon as (1) lands, but the WS handler (line 1488) requires (2) too. The orchestrator's WS proxy polls `/ready` every 2 s (`orchestrator/main.py:13607`), opens the upstream WS the moment it gets `true`, and the agent rejects the connection with code 4503.

The cockpit's control WS retries with `[500, 1000, 2000, 4000] ms` backoff (`cockpit/src/app/core/services/persistent-chat.service.ts:52`), which produces the 3× error count the user saw. The 504 is most likely the SSE EventSource also stalling at the edge (Cloudflare Tunnel ~100 s / Traefik ~60 s default) during the same race window.

**Why pool reuse triggers the race but cold pods don't:** for a fresh pod, `is_initialized` stays false through the kubelet image pull + agent bootstrap, so `/ready` doesn't even *consider* returning true until the pod has spent enough time inside `_setup_workspace` that the queue init is right behind. For a pool pod, `is_initialized` is already true and `_pod_state` flips to `SESSION` instantly — the race window matches the orchestrator's 2 s poll cadence.

## Quick fix (shipped)

Commit `b2e2e9e9` — "Refactor persistent app readiness logic: centralize `_session_ready` checks".

Added a single helper in `persistent_app.py`:

```python
def _session_ready() -> bool:
    """True when the persistent session is fully attached and the loop
    primitives are ready to accept a WS subscriber.
    """
    return (
        _session is not None
        and _session.llm_with_tools is not None
        and _loop_user_queue is not None
    )
```

Routed the four sites that previously inlined some variant of this check through the helper:

- `persistent_app.py:1245` — pure-persistent `/ready`
- `persistent_app.py:1499` — `handle_persistent_websocket` (was already strict; now uses the helper)
- `dual_app.py:613` — dual-mode `/ready` (SESSION branch)
- `dual_app.py:1029` — `/session/status` (was already strict; now uses the helper)

Net diff: ~80 lines, mostly helper + 4 new tests covering the helper directly. 130 tests pass, ruff clean.

Verified live on the dev cluster 2026-05-20: the "Agent not ready" flash and the 504 are gone on pool-agent reuse. Cold-pod path unchanged (still shows provisioning timer, still connects cleanly).

## Why the quick fix isn't the proper fix

The helper makes the readiness check single-sourced *today*, but the underlying structural problem is still there: **the agent has three FastAPI apps, and the bug was caused by drift between two of them**.

The three apps aren't peers — they're already 80% consolidated under the hood:

| File | Role | Lines | Composition style |
|---|---|---|---|
| `persistent_app.py` | Library (owns module state, `_attach_session`, `handle_persistent_websocket`, `handle_api_*`) | 3423 | Plus its own FastAPI factory |
| `dual_app.py` | Composer — worker routes inline + delegates session routes to `persistent_app` | 1162 | Imports `persistent_app` as a library |
| `app.py` | Worker-only composer — duplicates the worker routes from `dual_app` | 1130 | Independent |

The duplication that enables drift is concentrated in two places:

1. **Health/readiness routes are reimplemented in each composer** — `/health`, `/ready`, `/status`, `/metrics`, `/system/*` all appear two or three times across the files, with no shared base. Today's bug was one of these (`/ready`) drifting.
2. **Worker routes are reimplemented across `app.py` and `dual_app.py`** — `/job/start`, `/job/cancel`, `/job/pause`, `/job/current` exist in both, with `dual_app` adding a `_pod_state` pre-check. Same parallel-implementation pattern; the same kind of drift bug could land there.

Session routes are *not* duplicated — `dual_app` already delegates session handling to `persistent_app`'s module-level functions. That's the pattern to extend.

## Proposed proper fix — unify into one app

Replace all three composers with a single `create_app()` factory parameterized by an `allowed_modes` set:

```python
def create_app(
    config_path: str,
    *,
    modes: set[Literal["worker", "session"]] = {"worker", "session"},
    auto_attach_thread_id: str | None = None,
) -> FastAPI:
    ...
```

- `--mode dual` → `modes={"worker", "session"}` (default; what the cluster uses)
- `--mode worker` → `modes={"worker"}` (refuses `/session/attach` with 409)
- `--mode persistent` → `modes={"session"}` (refuses `/job/start` with 409, optionally `auto_attach_thread_id=…`)

`persistent_app.py`'s module-level state and handlers (`_session`, `_loop_user_queue`, `_subscribers`, `_attach_session`, `handle_persistent_websocket`, `handle_api_*`) move into a `session_runtime` library module. `_pod_state` (currently in `dual_app.py`) moves with them so worker-only and persistent-only modes share the same state machine, just walking different subsets of it.

After the refactor:

- One `/ready` implementation. One `/health`. One `/status`. One `/ws/chat` route.
- ~1100 lines deleted (`app.py` and `dual_app.py` both replaced; the giant `persistent_app.py` shrinks to library shape with no FastAPI factory).
- New endpoints have one place to live.
- The drift class is structurally impossible (you cannot rewrite a check that doesn't exist anywhere else).

## Open decisions

Three calls worth making before drafting the design doc:

**1. Routes inline vs. router modules.** Either:
- **(a)** Keep routes inline in the single `app.py` (~1500-1800 lines, linear, matches today's style). Low-risk delete-and-collapse.
- **(b)** Extract `routes/health.py`, `routes/worker.py`, `routes/session.py` as `APIRouter`s. Cleaner long-term, and would set the precedent for the same pattern in `orchestrator/main.py` (see `docs/issues/orchestrator_main_py_monolith.md`).

**2. State machine scope.** `_pod_state` (`IDLE`/`WORKING`/`SESSION`) currently lives in `dual_app.py`. Two options:
- Move it to `session_runtime` unchanged. Worker-only mode just never transitions to `SESSION`. Simplest.
- Restrict the enum at construction time by mode set. More precise, more code.

Recommendation: keep the three-state enum, accept that worker-only mode walks an `IDLE ↔ WORKING` subset.

**3. `--mode` CLI flag fate.** Once modes are a set, the flag is doing very little. Options:
- Keep `--mode worker|persistent|dual` as ergonomic shorthand that maps to a set. Preserves backward compat.
- Replace with `--modes worker,session`. More honest, slightly more verbose.
- Drop entirely; default to dual; add `--no-worker` / `--no-session` opt-outs.

The flag has consumers in CLAUDE.md, docker-compose, and local dev. Backward compat is cheap.

## Risks to verify before drafting

- **Lifespan auto-attach.** Pure-persistent mode currently calls `_attach_session(_thread_id)` from `lifespan()` at startup (`src/api/persistent_app.py:386-394`). The new app needs a lifespan hook that fires this iff `auto_attach_thread_id` is set.
- **`agent_provisioner.py` pod command.** Verify how the K8s pod spec invokes `agent.py` (`--mode dual`?) and whether anything depends on app-specific behavior.
- **Test migration.** `test_persistent_app.py` has ~130 tests, most patching module-level state directly (`patch("src.api.persistent_app._session", ...)`). They keep working if state moves to `session_runtime` with imports renamed. The ~3 tests that import `create_persistent_app` directly need their factory call updated.

## Proposed scope

Mirrors how other structural cleanups have been handled in this repo (e.g. `orchestrator_main_py_monolith.md`):

1. **PR 0 — design doc** (`docs/features/agent_app_unification.md`): decisions above + endpoint matrix + state-machine matrix + test migration list. ~1 day.
2. **PR 1 — refactor**: single PR, no behavior change, net deletion. ~2-3 days.
3. **Verification**: roll to dev cluster; exercise all three modes (dual via k8s, worker via docker-compose, persistent via `--thread-id` locally).

## Related

- `docs/issues/persistent_session_dual_mode_phase1_gap.md` — the previous round of consolidation between `dual_app` and `persistent_app` (WS handler + REST handlers).
- `docs/issues/orchestrator_main_py_monolith.md` — analogous structural cleanup for `orchestrator/main.py`; if decision (1) lands on router modules, the precedent set here informs that work.
