---
title: Unified Workspace Provisioning (jobs + sessions)
status: Implemented + k3d-verified — 2026-06-15
related:
  - "[[unified_instance_lifecycle]]"
  - "[[headless_persistent_sessions]]"
  - "[[stuck_thread_workspace_pods]]"
---

# Unified Workspace Provisioning (jobs + sessions)

## Status

Design proposal — 2026-05-27. Captured after a production incident on the dev
cluster: a persistent session (`05220a87…`, "Building a RAG Chatbot Demo")
crash-looped its agent with `RuntimeError: No workspace container provisioned
for thread` (`src/api/persistent_app.py:643`) and never recovered. Root cause
was a workspace container left in a non-`ready` state by the
orchestrator K8s-client 401 outage (a `kubernetes` 36.0.0 regression), with **no session-side
reconcile to re-create it** — the recovery logic exists only for jobs.

This is a deferred slice of [[unified_instance_lifecycle]]: that redesign
unified the lifecycle *skeleton* (drift / health / idle / drain / delete /
snapshot / restore) across agents, workspaces, and VMs, but explicitly left
**provisioning/binding "in the dispatcher"**, and the
`persistent_provisioner.py` migration was deferred. Jobs have a dispatcher that
re-provisions workspaces; sessions never got the equivalent.

**Update 2026-06-15:** the deferred `ready`-but-pod-missing drift recovery (the
`ensure_workspace` table row at `:142`, unit row `:211`) and the graceful agent
exit for a *dead* workspace (the `WorkspaceUnavailableError` half of §D) have
shipped and are k3d-verified, and `/prepare` now also calls
`ensure_session_workspace` — see `[[session_resume_dead_workspace_drift]]` (now
in `docs/done/`). Step 4 (remove the back-compat shims) is done too —
`release_workspace` is now owner-keyed and the thread-CRUD shims are gone — so
this design is fully realized and archived here in `docs/done/`.

## Problem

Two parallel, near-identical implementations of the same workspace pod, with
the lifecycle logic wrapped around only one of them.

1. **Duplicated provisioning.** `ContainerProvisioner.create_workspace(job_id)`
   (`container_provisioner.py:153`) and `create_thread_workspace(thread_id)`
   (`:809`) are copy-paste duplicates — the thread version's docstring says
   *"Same as create_workspace() but stores context in threads.metadata"*, and
   it even carries a `job_id=thread_id  # Reuse job_id label slot` artifact.
   The actual pod spec (`_build_pod_manifest`, `:630`) is already shared. Only
   four things differ: pod-name prefix (`workspace-` vs `ws-thread-`), the
   `_resolve_network_tier` kind arg, the state store (`jobs.context` vs
   `threads.metadata` via `_set_context` / `_set_thread_context`), and two
   labels. `delete_*` / `*_pvc` / `_set_*context` are duplicated the same way.

2. **Reconcile/retry exists only for jobs.** The job dispatcher
   (`main.py:2120-2256`) inspects `workspace_container.status` every cycle and
   reconciles: missing → `create_workspace`, `suspended` → restore,
   `creating/restoring` → wait, `failed` → fail. For sessions there is **no
   equivalent loop**. Every session re-create/restore call site is gated
   strictly on `status == "suspended"` (`main.py:12378`, `:12476`, `:13386`);
   the `/api/sessions/{id}/prepare` path (`routers/sessions.py:73-228`) binds
   only the agent pod. A workspace stuck at `pending`/`failed` matches none of
   those gates, so nothing re-creates it.

3. **Create races the agent, agent hard-fails.** On thread create, the
   workspace (`create_thread_workspace`) and the session agent
   (`provision_agent(purpose="session")`) launch as independent tasks with no
   dependency edge. The agent polls `_poll_workspace_ready(timeout=120)`
   (`persistent_app.py:3383-3453`) and **raises** if the workspace never goes
   ready (`:643`); with `restartPolicy: Never` the pod dies and is reaped, and
   nothing reprovisions the workspace. Permanent wedge.

The duplication in (1) is *why* (2) happened: when the session path was copied
from the job path, only pod-creation was duplicated — the reconcile wrapper
around it was not.

## Goals

- One workspace-provisioning code path serving both jobs and sessions, keyed by
  an explicit owner — no copy-paste second implementation.
- One idempotent `ensure_workspace` state machine that both modes call, so
  create / restore / recreate / wait behavior is identical by construction.
- A session-side reconcile (event-driven + a periodic safety-net) so a wedged
  session workspace self-heals the way a job's does — no user re-prompt needed.
- Remove the workspace-vs-agent race; a session whose workspace isn't ready
  yet must not permanently crash.
- **Net-negative `main.py`**: new logic lands in focused modules; `main.py`
  delegates and shrinks.

## Non-goals

- **No `persistent_provisioner.py` migration** into the lifecycle reconciler
  (the larger redesign workstream stays deferred).
- **No change to the snapshot-capture SSH failures** (`snapshot_service: SSH
  tar failed …`), tracked separately — but called out as a dependency: restore
  cannot succeed for a workspace that was never snapshotted. See
  [[stuck_thread_workspace_pods]].
- **No behavior change for the job path.** The job dispatcher keeps its current
  semantics; it simply calls the extracted shared function.
- Creation/binding stays *out* of the lifecycle reconciler — that boundary from
  [[unified_instance_lifecycle]] is preserved (reconciler = skeleton/teardown;
  provisioning = dispatcher-equivalent).

## Design

### A. Owner abstraction — `orchestrator/services/workspace_lifecycle.py` (new)

```python
@dataclass(frozen=True)
class WorkspaceOwner:
    kind: Literal["job", "session"]
    id: str

    @property
    def pod_name(self) -> str:
        prefix = "workspace" if self.kind == "job" else "ws-thread"
        return f"{prefix}-{self.id[:12]}"

    @property
    def label_key(self) -> str:
        return "srw/job-id" if self.kind == "job" else "srw/thread-id"

    @property
    def component(self) -> str:
        return "workspace" if self.kind == "job" else "thread-workspace"

    @property
    def network_tier_kind(self) -> str:        # arg to _resolve_network_tier
        return "job" if self.kind == "job" else "thread"
```

`ContainerProvisioner` collapses to owner-keyed methods —
`create_workspace(owner)`, `delete_workspace(owner)`, `get_workspace_status(owner)`,
`_set_context(owner, updates)` — each with a single implementation. The
state-store dispatch (`merge_workspace_context` vs `merge_thread_workspace_context`)
moves behind the owner. `_build_pod_manifest` is already shared and takes the
owner's name/label/tier. Existing call sites keep working via thin shims during
rollout (e.g. `create_thread_workspace(tid) → create_workspace(WorkspaceOwner("session", tid))`),
removed once all callers are migrated.

### B. `ensure_workspace(owner)` state machine — same new module

Extract the logic the job dispatcher inlines (`main.py:2120-2256`) into one
idempotent, unit-testable function consumed by **both** modes:

| observed `workspace_container.status` | action |
|---|---|
| absent / `none` / `deleted` / `failed` | `create_workspace(owner)` |
| `suspended` | `restore_workspace(owner)` (snapshot → pod) |
| `creating` / `restoring` | in-progress → no-op (let it converge) |
| `ready`, pod exists | no-op |
| `ready`, pod missing (drift) | treat as `failed` → recreate |

Returns the resolved `WorkspaceState`. Idempotent and safe to call repeatedly /
concurrently (guarded by the `creating`/`restoring` states + a per-owner lock).
This single function replaces the `status == "suspended"`-only gates in the
session paths — the exact gates that blocked recovery of a `failed`/`pending`
workspace.

### C. Session reconcile — `orchestrator/services/session_provisioner.py` (new)

The dispatcher-equivalent for sessions (the piece the design assumes exists but
was never built). Two entry points, both delegating to `ensure_workspace`:

- **Event-driven:** `create_thread`, `resume`, and `/prepare` call
  `ensure_session_workspace(thread_id)`. Because `ensure_workspace` is
  idempotent, these are safe and replace today's bespoke suspended-only logic.
- **Periodic safety-net:** a lightweight tick (extend the existing
  `auto_assign_dispatcher` loop, or a sibling loop in this module) that finds
  threads with a **connecting/active** session but a non-`ready` workspace and
  re-runs `ensure_session_workspace`. This is what auto-recovers a workspace
  wedged by a transient failure (e.g. the 401 outage) without a user re-prompt.

This module owns the session-side provisioning currently scattered through
`main.py`; the HTTP layer (`routers/sessions.py`) calls into it.

### D. De-race workspace vs. agent

- `/prepare` (and create) invoke `ensure_session_workspace` **before/alongside**
  spawning the agent, so the workspace is reliably being created/restored
  during the agent's poll window — and the safety-net retries on failure.
- The agent's 120s poll stays, but on timeout it **exits cleanly (non-error)**;
  the session reconcile re-binds a fresh agent once the workspace reaches
  `ready`. No more `RuntimeError`/`exit 3` permanent wedge. (`persistent_app.py`
  `_attach_session` / `_poll_workspace_ready` change from "raise" to "graceful
  exit + let orchestrator rebind".)

## Module structure & `main.py` decomposition

Per the maintainability goal, all new logic is in focused modules and `main.py`
loses code:

| Module | Responsibility | Source today |
|---|---|---|
| `services/workspace_lifecycle.py` *(new)* | `WorkspaceOwner`, `ensure_workspace` state machine | inlined in `main.py:2120-2256` |
| `services/session_provisioner.py` *(new)* | session ensure entry points + periodic safety-net reconcile | scattered across `main.py` (create_thread, resume_thread, the suspended-gates `:12378/:12476/:13386`) |
| `services/container_provisioner.py` *(edit)* | owner-keyed `create/delete/get/_set_context` (de-duplicated) | duplicated `*_workspace` / `*_thread_workspace` |
| `routers/sessions.py` *(edit)* | thin HTTP layer → calls `session_provisioner` | partly here already |
| `main.py` *(shrinks)* | job dispatcher calls `ensure_workspace`; session workspace blocks moved out | net deletion |

Net effect: the workspace-ensure logic and the session-provisioning logic both
leave `main.py` for testable service modules; `main.py` keeps only wiring.

## Rollout (incremental, each step shippable)

1. **Owner refactor (pure dedup, zero behavior change).** Add `WorkspaceOwner`,
   collapse `ContainerProvisioner` to owner-keyed methods with back-compat
   shims. Job and session paths behave exactly as before.
2. **Extract `ensure_workspace`; point the job dispatcher at it.** Still no
   behavior change for jobs — pure extraction + test coverage.
3. **Wire sessions + safety-net (the fix).** `session_provisioner` event hooks
   + periodic reconcile; replace suspended-only gates; make the agent exit
   gracefully and rebind. This is the step that closes the gap.
4. **Remove shims** once all call sites use the owner API. ✅ Done 2026-06-15:
   `release_workspace` collapsed to one owner-keyed method (was
   `release_workspace(job_id)` + `release_thread_workspace(thread_id)`); the
   thread-CRUD shims (`create_thread_workspace` etc.) were already removed.

## Testing

CI (Python 3.12) is the gate; the local pytest env is noisy and not authoritative.

- Unit: `ensure_workspace` transition table — every status → expected action,
  incl. `failed`/`pending`/`none` → recreate and `ready`-but-pod-missing drift.
- Unit: `WorkspaceOwner` — pod name, labels, network-tier kind, state-store
  dispatch for both kinds.
- Unit: idempotency / concurrency — repeated `ensure_workspace` while
  `creating` is a no-op (lock honored).
- Regression: reproduce the incident — thread workspace `failed`, no live pod →
  session reconcile recreates → agent binds → session active. Asserts the
  pre-fix path crash-loops and the post-fix path recovers.
- Job-path parity: existing dispatcher tests stay green unchanged (proves step
  1–2 are behavior-preserving).

## Risks / open questions

- **Restore depends on a snapshot existing.** If the snapshot-SSH capture issue
  left no snapshot, `restore` for a `suspended` workspace will fail → falls
  through to recreate (empty workspace). Acceptable degradation; flagged as the
  separate dependency.
- **Concurrency:** `/prepare` event + safety-net tick could both call
  `ensure_workspace` for one owner — handled by the per-owner lock + the
  in-progress (`creating`/`restoring`) no-op states.
- **Periodic-loop placement:** extend `auto_assign_dispatcher` vs. a dedicated
  loop. Leaning on extend (reuses trigger/rate-limit infra, matches "provisioning
  in the dispatcher"); final call at plan time.

## References

- [[unified_instance_lifecycle]] — parent design; this completes its deferred
  session-provisioning slice.
- Orchestrator K8s-client 401 outage (2026-05-27) — the `kubernetes` 36.0.0
  regression (fixed in `sha-35757b1`) that wedged the workspace.
- [[stuck_thread_workspace_pods]] — related workspace-pod lifecycle issues.
- Incident forensics: thread `05220a87`, agent `srw-agent-s-e72c4d49`
  (2026-05-27), `persistent_app.py:643` raise.
