---
title: Session resume dials a dead workspace — `ready`-but-pod-missing drift not recovered
status: Resolved — fixed + k3d-verified 2026-06-15
related:
  - "[[unified_workspace_provisioning]]"
  - "[[stuck_thread_workspace_pods]]"
  - "[[session_silent_failure_audit]]"
---

# Session resume dials a dead workspace (`ready`-but-pod-missing drift)

## Symptom

When a persistent session whose **workspace pod no longer exists** is resumed,
the orchestrator re-provisions the *agent* but hands it the **stale workspace
address** from `threads.metadata.workspace_container` (`status: "ready"`, dead
`pod_ip`). The fresh agent then:

1. SSH-retries the dead workspace — `RemoteBackend.connect` (5 attempts,
   `src/core/backends/remote.py:200-211`), wrapped by
   `PersistentSession._setup_workspace`'s 300 s cap
   (`src/api/persistent_session.py:320-364`).
2. After ~5 minutes raises `WorkspaceUnavailableError`
   (`src/api/persistent_session.py:351`).
3. That error is **not caught** by the lifespan handler — which only catches
   `WorkspaceNotReady` (`src/api/persistent_app.py:859`) — so it propagates out
   of `_attach_session` uncaught. The agent's HTTP server (`:8001`) never
   finishes starting → the K8s startup probe fails → the pod **crash-loops**.
   The session never becomes usable; `/api/sessions/{id}/connection` keeps
   returning not-ready and `/input` returns 503.

Observed live 2026-06-15 on local k3d, threads `47a833e4…` and `d19c166e…`:
their `ws-thread-*` pods were destroyed in a cluster restart. On reopen the
re-provisioned agent (`srw-agent-s-94d9071e`) looped
`SSH connect attempt N/5 to 10.42.0.188:30022 … connection refused` against the
dead pod, while `workspace_container` still read
`{"status":"ready","pod_ip":"10.42.0.188","pod_name":"ws-thread-47a833e4-116", …}`.

## Root cause — two gaps, both specified in the design, neither implemented

The design [[unified_workspace_provisioning]] (2026-05-27) specified exactly the
behaviors that would prevent this; the shipped implementation left both out.

### Gap A — orchestrator: a `ready`-but-dead workspace is never recreated

`ensure_workspace` (`orchestrator/services/workspace_lifecycle.py:70-111`) has
**no liveness probe** on `ready`:

```python
# :89-91
# * No drift check (status 'ready' → READY unconditionally) — matches the original
#   dispatcher, which did no live-pod probe on 'ready'. (Drift recovery is a
#   deferred enhancement.)
# :108-109
if s == "ready":
    return EnsureResult(EnsureOutcome.READY, status="ready")
```

But the design's state table (`unified_workspace_provisioning.md:142`) requires
`ready, pod missing (drift) → treat as failed → recreate`. Because of the
missing check, a thread stuck at `ready` with a dead pod escapes **both**
reconcile paths — the event-driven `ensure_session_workspace` *and* the periodic
safety-net (`orchestrator/services/session_provisioner.py`) — since both defer
to `ensure_workspace`, which no-ops on `ready`. (The safety-net only scans
*non-`ready`* workspaces, so it doesn't pick it up either.)

The stale `ready` is actively (re)written by the suspend path: when a snapshot
can't be captured, `suspend_thread_workspace` **reverts status to `ready` and
keeps the workspace alive** (`orchestrator/services/workspace_suspension.py:119`
and `:452` docstrings: *"On failure, reverts status to 'ready' …"*). So the dead
pod's `ready` marker is sticky — it can be set by an out-of-band pod death (no
status update at all) *or* re-asserted by a failed suspend.

### Gap B — agent: the dead-workspace error crash-loops instead of exiting cleanly

The lifespan handles only the "never provisioned" case:

- `WorkspaceNotReady` (no workspace at all; raised `persistent_app.py:1051` when
  `_poll_workspace_ready` times out) → **caught** at `persistent_app.py:859` →
  `_exit_workspace_not_ready` (`:684`) graceful exit so K8s does not restart-loop
  (design §D, `unified_workspace_provisioning.md:167-176`).
- `WorkspaceUnavailableError` (workspace *was* provisioned but the pod is
  dead / SSH fails; raised `remote.py:208`, `persistent_session.py:351`) →
  **not caught** anywhere in `_attach_session` / the lifespan → propagates →
  crash-loop.

The design's "agent exits cleanly + orchestrator rebinds" was implemented for
the first error type only.

### Why it surfaced now

The orphaned-paused-thread reaper added in the session-wedge fix
(`PostgresDB.mark_orphaned_threads_suspended` + the `/connection` self-heal in
`routers/sessions.py`) now reliably re-provisions agents for paused sessions
whose agent died. Previously those sessions 409-looped at `/connection` and
never re-provisioned, which *masked* this workspace drift. With the wedge gone,
resume proceeds far enough to hit the dead workspace.

## Scope / impact

- Any persistent session whose **workspace pod is destroyed out-of-band**
  (cluster/node restart, manual delete, containerd disruption — see
  [[stuck_thread_workspace_pods]]) with **no usable S3 snapshot** (snapshot
  disabled, or the pod was already gone so nothing could be captured).
- The *normal* suspend on a **live** pod (S3 enabled) is unaffected: it
  snapshots → `suspended` → restore recreates the pod on resume. The MinIO
  integration (commit `a5833e05`) makes that path more reliable but does nothing
  for the out-of-band case, where the `ready` marker is stale and no snapshot
  exists.

## Proposed fix (reuse existing machinery)

1. **Gap A — implement the deferred drift check** in `ensure_workspace`: on
   `ready`, probe pod liveness; if the pod is missing, fall through to the
   existing session `failed → _create` branch (`workspace_lifecycle.py:97-99`).
   This is `unified_workspace_provisioning.md:142` verbatim and the unit row
   called for at `:211`.
   - *Cheaper interim:* when the suspend-without-snapshot branch confirms the
     pod is gone, set status to `failed`/`none` instead of reverting to `ready`,
     so the existing recreate path fires. Less complete than a probe (it doesn't
     cover an out-of-band death with no suspend), but a small, low-risk start.
2. **Gap B — catch `WorkspaceUnavailableError` agent-side** the same way as
   `WorkspaceNotReady`: a graceful `_exit_workspace_not_ready`-style exit so K8s
   doesn't crash-loop and the orchestrator session-reconcile rebinds a fresh
   agent once the workspace is recreated (design §D).
3. **Wire `/prepare` to the workspace ensure.** `_do_prepare`
   (`routers/sessions.py`) provisions only the agent today; call
   `ensure_session_workspace(thread_id)` there too (design §C/§D, "before/
   alongside spawning the agent"), so the cockpit cold-start path reconciles the
   workspace, not just the `resume` path.

## Testing

- Unit: `ensure_workspace` — `ready` + pod-missing → recreate (the row currently
  missing from the transition test, `unified_workspace_provisioning.md:211`).
- Unit: agent lifespan — `WorkspaceUnavailableError` raised from `setup()` →
  graceful exit (status 0), not an uncaught crash.
- Regression: a thread at `workspace_container.status=ready` whose pod does not
  exist → resume → fresh workspace provisioned → agent binds → session active.
  Assert the pre-fix path crash-loops and the post-fix path recovers (mirrors the
  incident, parallel to the design's existing `failed`-path regression).

## References

- [[unified_workspace_provisioning]] — the design this completes: its deferred
  `ready`-drift slice (`:142`, `:211`) and the `WorkspaceUnavailableError` half
  of §D (`:167-176`).
- [[stuck_thread_workspace_pods]] — how `ws-thread-*` pods get destroyed
  out-of-band (the upstream trigger) and the still-open thread-workspace reaper.
- Session-wedge fix (2026-06-15): `mark_orphaned_threads_suspended` reaper +
  `/connection` self-heal — re-provisions paused-orphan agents, which is what
  now surfaces this drift.
- Live forensics 2026-06-15: threads `47a833e4-1161-4438-abbb-8af7ce6e0b06`,
  `d19c166e-657f-4ed9-aeab-048de6d77b5d` on k3d (`k3d-srw`/`srw`); agent
  `srw-agent-s-94d9071e` startup-probe fail on `:8001`, SSH loop to dead
  `10.42.0.188:30022`.

## Resolution (2026-06-15)

Fixed with the probe-based approach (not the cheaper status-on-abandon interim),
so a stale `ready` is caught regardless of how it got there:

- **Gap A** — new `ContainerProvisioner.workspace_pod_live(owner)` (live
  `read_namespaced_pod`: `True`=Running/Pending, `False`=404 / terminal
  tombstone, `None`=can't-tell). `ensure_workspace`'s `ready` branch recreates
  only on a confirmed-dead pod and trusts `ready` otherwise — it never
  false-recreates on a non-k8s backend or a transient probe blip.
- **Gap B** — the agent lifespan now catches `WorkspaceUnavailableError`
  alongside `WorkspaceNotReady` and routes it through `_exit_workspace_not_ready`
  (clean exit 0 + rebind) instead of crash-looping.
- **`/prepare`** — `_do_prepare` fires `ensure_session_workspace` on the unbound
  (cold-start / reopen) branch, so the drift probe runs on the cockpit reopen
  path, not only `resume`.

Unit tests: `ensure_workspace` ready-drift transitions, `workspace_pod_live`
True/False/None cases, and the `/prepare` cold-start reconcile (full suite green
bar a pre-existing unrelated flake; ruff clean).

**k3d end-to-end verified:** reopened `47a833e4` (suspended, unbound, stale
`ready` whose pod was 404) → `/connection 425` → `/prepare` → the drift probe
recreated `ws-thread-47a833e4-116` → a fresh agent attached to the *live*
workspace (no SSH-loop, no crash) → thread `suspended → active`, history restored
→ sent a message (`/input 200`, was `503` pre-fix) → coherent resumed answer. The
exact session that SSH-looped a dead workspace before now recovers cleanly.

## Status

- [x] Diagnosed (orchestrator Gap A + agent Gap B); file:line confirmed 2026-06-15
- [x] Gap A: `ready`-drift recovery — `workspace_pod_live` probe + recreate (unit-tested)
- [x] Gap B: catch `WorkspaceUnavailableError` → graceful exit + rebind
- [x] `/prepare` wires `ensure_session_workspace` (unit-tested)
- [x] k3d end-to-end verified — full incident reproduction exercised live (Gap B's
      lifespan/`os._exit` path isn't unit-testable, so it's covered by the k3d run)
