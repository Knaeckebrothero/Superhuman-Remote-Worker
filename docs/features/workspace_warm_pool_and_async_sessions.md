---
tags:
  - feature
  - issue
  - infrastructure
  - sessions
  - performance
  - workspace
related:
  - "[[sessions]]"
  - "[[agent_lifecycle]]"
  - "[[ephemeral_workspaces]]"
  - "[[vm_backend]]"
  - "[[pod_runtime]]"
---

# Workspace Warm Pool + Async Session Start

> Cut session-start latency on the cluster from 5–10s to ~1s by (a) keeping a small pool of pre-provisioned workspace pods/VMs warm and (b) decoupling workspace provisioning from session start so the user can type while infrastructure spins up.

**Status:** Design phase.
**Filed:** 2026-05-03

## Problem

Agents are pre-provisioned via the existing warm-pool mechanism (`MIN_AGENTS`,
`AgentProvisioner.ensure_warm_pool()`), so agent assignment is effectively
instantaneous. But the workspace backend — the pod or VM the agent SSHes into
to do real work — is still provisioned **on demand** at session/job start.

Observed latency:

| Environment | First-message-to-response | Bottleneck |
|---|---|---|
| Local Docker Compose | ~1s | None — workspaces are static long-lived containers |
| Kubernetes cluster | ~5–10s | Cold workspace pod creation (image pull, PVC bind, sshd ready) |
| KubeVirt VM | ~30–60s | QEMU boot |

The agent itself is sitting idle and ready. The user is staring at a spinner.
This is the dominant component of perceived session-start cost on the cluster
and the only reason cloud sessions feel "slower" than local ones.

A related issue: there is no observable workspace pool today. With 2 ready
agents, there are 0 ready workspaces — every session pays the full cold-start
cost on its first message.

## What Already Exists

| Capability | Where | Reuse |
|---|---|---|
| Agent warm pool | `orchestrator/services/agent_provisioner.py` (`ensure_warm_pool`, `MIN_AGENTS`/`MAX_AGENTS`/`AGENT_BUFFER`) | Mirror the pattern for workspaces |
| Workspace pod provisioner | `orchestrator/services/container_provisioner.py` | Add a pool-management entrypoint analogous to `ensure_warm_pool()` |
| VM provisioner | `orchestrator/services/vm_provisioner.py` (NATS-driven) | Same shape, with VM-aware grace windows |
| Persistent session WebSocket | `PersistentChatService` + `persistent_graph.py` | Add `session.ready` / `session.degraded` events |
| Frontend session shell | Cockpit `simple/` and chat components | Add input buffer + ready-gated dispatch |
| Job auto-assign loop | `orchestrator/services/job_dispatcher.py` | Add a workspace-readiness check before dispatch |

## Proposal

Two complementary mechanisms. Each ships independently; together they cover
both session and job workloads.

### 1. Workspace Warm Pool (jobs + sessions)

Configurable pool of pre-provisioned, unclaimed workspace pods/VMs per backend.
On session/job start, the orchestrator claims one atomically and immediately
triggers replenishment in the background.

**Config knobs** (mirror the agent pool naming):

```yaml
workspace:
  pool:
    container:
      minWarm: "0"        # 0 = disabled, current behavior
      maxWarm: "4"        # ceiling on idle workspaces
      buffer: "0"         # extra warm capacity above current demand
    vm:
      minWarm: "0"        # VMs are heavier — keep default off
      maxWarm: "2"
      buffer: "0"
```

Env var equivalents: `WORKSPACE_MIN_WARM_POD`, `WORKSPACE_MAX_WARM_POD`,
`WORKSPACE_MIN_WARM_VM`, etc.

**Lifecycle** (one-shot, drain-and-replace — workspaces are stateful):

```
PROVISIONING → READY → CLAIMED → IN_USE → TERMINATED
                ↑                              ↓
                └── replenished by reaper ─────┘
```

A claimed workspace cannot be returned to the pool — once an agent has
attached, the workspace's filesystem state is bound to that session. The pool
is "lease once, then destroy and replenish."

**Implementation surface**:

- New `WorkspaceProvisioner.ensure_warm_pool()` — same shape as
  `AgentProvisioner.ensure_warm_pool()`, called every N seconds by the
  orchestrator's background loop.
- New `WorkspaceProvisioner.claim()` — atomic SELECT-FOR-UPDATE on a `READY`
  pod, transitions to `CLAIMED`, returns connection details.
- New `workspace_pool` table (or extension of existing) tracking
  `id, backend, status, host, port, created_at, claimed_at, claimed_by_session`.
- Reaper extension: deletes `CLAIMED` workspaces whose owning session has ended,
  replenishes pool to `minWarm`.

**Default**: `minWarm: 0` everywhere — opt-in only. Same model as `MIN_AGENTS`.

### 2. Async Session Start (sessions only)

Even with a warm pool, fast typers can outrun pool replenishment, and
operators may not want the steady-state cost of warm VMs at all. So: don't
make the user wait — start the agent immediately, provision the workspace in
the background, and let the frontend buffer the user's typing until both are
ready.

**Why sessions only**: Jobs read input files in their first action, so they
need workspace + agent ready simultaneously. Sessions have a natural human
pause (typing) that hides the provisioning latency.

**Flow**:

```
User clicks "new session"
    ↓
Orchestrator: agent.claim() → INSTANT (warm pool already covers this)
              workspace.provision() → ASYNC, background task
    ↓
Frontend opens WebSocket → receives session.created (with session_id)
    ↓
User starts typing into composer  ← buffered locally, send disabled
    ↓
Orchestrator: workspace ready, attaches agent → emits session.ready
    ↓
Frontend: enables send button. User hits send.
    ↓
Message flows normally.
```

**Two events, not one** (avoid races):

- `agent.ready` — agent process is bound to the session, can accept the
  WebSocket but cannot serve a turn yet.
- `workspace.ready` — workspace is bound and reachable from the agent.
- `session.ready` — emitted by orchestrator only when **both** are true.
  Frontend only enables `send` on this event.

**New session state**:

A `sessions.workspace_status` column with values
`provisioning | ready | failed | terminated`. The persistent channel can
open at `provisioning`, but message dispatch is gated until `ready`.

**Failure surfacing**:

If workspace provisioning fails, the orchestrator emits `session.degraded`
with a reason. The frontend shows a non-destructive banner and a "retry"
action that re-runs provisioning without losing the typed buffer. This is
the main UX regression vs. today's behavior — today, workspace failure
aborts session creation outright, so the user sees the error before
investing typing effort.

**Frontend changes** (cockpit):

- Composer accepts input regardless of session readiness.
- Send button disabled (or shows "starting…") until `session.ready`.
- On `session.degraded`, show banner with retry; keep buffer intact.
- On `session.ready` arriving with a non-empty buffer, optionally
  auto-send (configurable; safer default is "enable button, let user
  hit send").

## Compatibility Matrix

| `workspace.pool.*.minWarm` | Async session decouple | Behavior |
|---|---|---|
| `0` (default) | Off | Current sync behavior — no change |
| `0` | On | Sessions feel instant for slow typers; jobs unchanged |
| `>0` | Off | Workspaces ready instantly when claimed; sync semantics preserved |
| `>0` | On | Best case: instant for everyone; warm pool absorbs fast-typer / job demand |

Operators can run any quadrant. Defaults preserve current behavior.

## Out of Scope

- **Workspace snapshots / restore** — already covered by `SnapshotService`
  (see `[[ephemeral_workspaces]]`). Warm pool is provisioned blank;
  snapshot restore is a separate post-claim step.
- **Job-side async decouple** — won't work, agents read inputs immediately.
- **Pool autoscaling on demand metrics** — start with static `minWarm` /
  `maxWarm`. Add demand-aware sizing later if pool churn becomes a problem.
- **Cross-backend pool sharing** — pod pool and VM pool are independent.

## Verification

1. **Unit tests** — `tests/test_workspace_provisioner.py` mirroring the
   shape of `tests/test_agent_provisioner.py`:
   - `ensure_warm_pool` creates up to `minWarm`
   - `claim` returns a `READY` workspace and marks it `CLAIMED`
   - Concurrent `claim` calls don't double-bind
   - Reaper reclaims orphaned `CLAIMED` workspaces.

2. **Frontend tests** — composer buffers input pre-`session.ready`, send is
   disabled, `session.degraded` shows banner without losing buffer.

3. **Latency benchmark** — measure first-message-to-response on:
   - Local Compose (baseline ~1s, expect unchanged).
   - K8s cold (current ~5–10s, expect ~1s with `minWarm=2`).
   - K8s with `minWarm=0` and async decouple (expect ~1s for slow typers,
     ~5–10s for instant senders — i.e., bounded by current cold-start).

4. **End-to-end on cluster**:
   - Set `workspace.pool.container.minWarm: 2`, deploy, confirm two
     unclaimed workspace pods come up alongside the agent warm pool.
   - Start a session, confirm one is claimed, confirm pool replenishes.
   - Kill a workspace mid-session, confirm `session.degraded` surfaces.

## Open Questions

- **Workspace pool naming in DB** — extend `workspace_attachments` or add a
  dedicated `workspace_pool` table? Probably the latter, to keep claim/lease
  semantics separate from session attachment.
- **VM pool sizing default** — KubeVirt VMs are expensive; `minWarm: 0`
  default is safe but means VM sessions stay slow until operators opt in.
  Worth a doc note.
- **Auto-send on `session.ready`** — safer default is "enable button, let
  user hit send" (preserves intent if user changed their mind mid-typing).
  But "auto-send if buffer non-empty for >2s" is a quality-of-life win.
  Punt to follow-up.
