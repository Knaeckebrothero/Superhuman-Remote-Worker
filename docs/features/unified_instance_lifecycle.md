# Unified Instance Lifecycle Management

## Status

Design proposal — 2026-05-09. Captures decisions from the design
discussion in
[`docs/issues/agent_lifecycle_management.md`](../issues/agent_lifecycle_management.md).
Not yet implemented.

## Problem (summary)

Agents, workspace pods, and VMs are three families of stateful instances
that the orchestrator provisions on demand. None can be a Deployment —
each carries identity or state that disqualifies the standard rolling-
update model. Today, lifecycle is handled by a handful of independent
sweepers and provisioner-specific cleanup paths, with three concrete
gaps:

1. **No version-drift reconciliation** for any of the three. When the
   orchestrator's expected image SHA changes, running pods stay on
   whatever SHA they were born with. The agent path has a partial
   `_drain_stale_image_agents` sweep, but it's gated to fully-idle
   agents and the `draining` status it writes is a write-only label
   nothing acts on.
2. **No crash recovery** for workspace pods or VMs. Agents are caught
   within 60s by `reap_pods`; workspace `Unknown`/`Failed` pods
   accumulate for days (documented in
   [`docs/issues/stuck_thread_workspace_pods.md`](../issues/stuck_thread_workspace_pods.md)).
3. **Fragmented control plane.** The orchestrator runs ~9 background
   loops with overlapping concerns and no shared scheduling, ordering,
   or rate-limiting primitives. Bug fixes and feature additions land in
   one loop without considering interactions with the others.

The full analysis, including the industry research that informs this
design, lives in the issues doc above.

## Goals

- One reconciler abstraction that covers agents, workspace pods, and
  VMs uniformly for the **lifecycle skeleton**: enumerate, drift-detect,
  health-check, idle-check, drain, delete.
- Image-drift handling that converges to the orchestrator's expected
  version across all three instance kinds.
- Crash recovery for workspaces and VMs, matching the SLO agents
  already get from `reap_pods`.
- A clean separation between **orchestrator-set intent** and
  **instance-reported observation**, so heartbeats can't accidentally
  overwrite drain commands.
- A migration path that can ship in stages, with each stage independently
  valuable.

## Non-goals

- **Warm pool unification.** Workspaces and VMs are identity-bound to
  specific work (job/thread + datasources + project context); pre-warming
  generic instances saves only pod creation latency, not the per-bind
  setup cost. Existing image-prewarm DaemonSet (`srw-image-prewarm-*`)
  already addresses the relevant cold-start path. Warm pool stays
  agent-specific and lives outside this interface.
- **Heartbeat protocol redesign.** The agent heartbeat endpoint stays
  where it is and writes its own table. The lifecycle reconciler reads
  freshness via `is_healthy()`, not by owning the heartbeat path.
- **Migrating to a full kubebuilder/operator-sdk CRD.** This design
  treats Postgres as the spec store, in line with the existing
  architecture. A future move to a real CRD is not blocked by this
  design but is out of scope.
- **Live session migration across versions.** No system in the research
  attempts this; we won't either. Sessions on stale versions will be
  resolved by user-facing migration (re-hydrate from persistent-thread
  state on next user prompt).

## Design

### Core interface

```python
# orchestrator/services/lifecycle/types.py

@dataclass
class Instance:
    kind: str               # "agent" | "workspace" | "vm"
    id: str                 # provisioner-native id (pod name / vm uuid)
    version: str | None     # build_sha or image tag this instance is on
    bound_to: str | None    # job_id / thread_id, if any
    metadata: dict          # kind-specific extras (labels, ssh host, etc.)

class InstanceLifecycleManager(Protocol):
    """Contract that each instance kind implements."""
    kind: str

    async def list_instances(self) -> list[Instance]: ...
    async def expected_version(self) -> str | None: ...
    async def is_healthy(self, inst: Instance) -> bool: ...
    async def is_idle(self, inst: Instance) -> bool: ...
    async def drain(self, inst: Instance, grace_s: int) -> None: ...
    async def delete(self, inst: Instance, grace_s: int) -> None: ...

class StatefulInstanceManager(InstanceLifecycleManager, Protocol):
    """Adds snapshot/restore for instances with persistent state."""

    async def snapshot(self, inst: Instance) -> str | None: ...
    async def restore(self, inst: Instance, snapshot_ref: str) -> None: ...
```

`AgentInstanceManager` implements `InstanceLifecycleManager`.
`WorkspaceInstanceManager` and `VMInstanceManager` implement
`StatefulInstanceManager` (interface inheritance — Workspace/VM IS-A
lifecycle manager, with the snapshot extension).

### The reconciler

A single generic loop iterates registered managers:

```python
class InstanceLifecycleReconciler:
    def __init__(self, managers: list[InstanceLifecycleManager],
                 disruption_budget: DisruptionBudget):
        self._managers = managers
        self._budget = disruption_budget

    async def tick(self):
        for mgr in self._managers:
            expected = await mgr.expected_version()
            instances = await mgr.list_instances()

            for inst in instances:
                if not await mgr.is_healthy(inst):
                    await mgr.delete(inst, grace_s=0)
                    continue

                if expected and inst.version and inst.version != expected:
                    if not self._budget.allow(mgr.kind):
                        continue  # skip this tick; rate-limit
                    if await mgr.is_idle(inst):
                        if isinstance(mgr, StatefulInstanceManager):
                            await mgr.snapshot(inst)
                        await mgr.drain(inst, grace_s=mgr.default_grace_s)
                    else:
                        # Mark drain_pending; will fire on next idle transition.
                        await self._set_drain_pending(inst)
```

What the reconciler owns:

- **Schedule.** A single tick rate (60s) for all managers, with optional
  per-kind jitter to avoid herd.
- **Disruption budget.** Caps concurrent drains per kind. Prevents a
  rollout from emptying a class of instances all at once. Configured
  via Helm values (`lifecycle.disruptionBudget.{agent,workspace,vm}`).
- **Drain ordering.** Snapshot before delete is enforced by the
  reconciler when the manager is `StatefulInstanceManager`. Type
  narrowing avoids a no-op snapshot method on agents.
- **Metrics.** Prometheus counters for `instance_drained_total`,
  `instance_deleted_total`, `instance_drift_detected_total`,
  `instance_unhealthy_total`, all labeled by kind.

What the reconciler does **not** own:

- Heartbeat handling (stays in `agents` heartbeat endpoint).
- Warm pool maintenance (stays agent-specific).
- Adoption / binding (stays in dispatcher).
- Snapshot bucket management (stays in `SnapshotService`).

### Drift detection

Borrows the **Karpenter pattern**: hash the desired spec, annotate every
instance, reconciler compares. For our shape this is simpler — we don't
own the full pod spec the way a Node controller does — so the hash is
just the build SHA from the configured image tag.

- `expected_version()` per manager:
  - Agent: `build_sha` parsed from `AGENT_IMAGE` / `PERSISTENT_AGENT_IMAGE`
    env vars (existing logic at `main.py:1325`).
  - Workspace: `build_sha` parsed from `WORKSPACE_IMAGE`.
  - VM: `build_sha` parsed from `DEFAULT_VM_IMAGE` (or per-job override
    when present).
- `Instance.version` per manager:
  - Agent: `agents.metadata->>'build_sha'` (already populated on
    registration).
  - Workspace: pod annotation (new — `srw/build-sha=...` set at
    creation by `ContainerProvisioner`).
  - VM: vm metadata field (new — populated by each backend at create
    time).

Drift = `inst.version != expected_version()`. If `expected_version()`
returns `None` (local dev with `:latest`), drift detection is skipped.

### Drain semantics

The actuation channel is **standard K8s deletion + graceful shutdown**,
not a status-flag-driven side channel. Per the research, this is the
production-proven path (Knative queue-proxy, Karpenter Eviction).

For all instance kinds:

1. Reconciler decides the instance should drain.
2. Manager calls `delete()` with the kind-appropriate grace period.
3. Kubelet sends SIGTERM, waits for `terminationGracePeriodSeconds`,
   sends SIGKILL.
4. The pod's signal handler refuses new work, finishes in-flight, exits.

Per-kind drain behavior:

- **Agent (worker, mid-job)**: SIGTERM → finish current phase → freeze
  with new `version_upgrade` freeze type → orchestrator re-dispatches
  the job to a fresh agent on the new version. This is the
  "Continue-as-New" pattern from Temporal Worker Versioning, mapped
  onto our existing phase-boundary archive.
- **Agent (persistent session)**: SIGTERM → mark thread `ended` → exit.
  On the user's next prompt, the dispatcher provisions a fresh agent
  on the current version and rehydrates from the persistent-thread
  Postgres rows + workspace files. This is Anthropic's Hybrid Sessions
  pattern.
- **Agent (idle, warm pool)**: SIGTERM → exit immediately. Eager
  refresh.
- **Workspace pod**: snapshot to S3 first (via `Stateful.snapshot`),
  then SIGTERM → unmount → exit. On rebind, restore from snapshot.
- **VM**: snapshot to S3 first, then backend-specific shutdown
  (NATS/HTTP/K8s/Docker).

### Finalizers as the cleanup hook

Each managed pod gets a Kubernetes finalizer
(`srw.io/lifecycle-cleanup`). The lifecycle reconciler is the only
remover of this finalizer, after running per-kind cleanup:

- Agent: flip DB row to `terminated`, clear `current_job_id`, audit log
  entry.
- Workspace: snapshot completion check, S3 reference written to
  `jobs.context.workspace_container.last_snapshot`.
- VM: backend-specific deregistration (NATS unsubscribe, HTTP DELETE,
  KubeVirt removal).

This guarantees that even if a pod is force-deleted out of band, the
finalizer holds it in `Terminating` until our cleanup runs. It also
means we can rely on Kubernetes garbage collection rather than building
parallel "did this thing get cleaned up?" sweepers.

### Spec / status split

The fundamental fix for Bug 2 in the issues doc — heartbeat overwriting
orchestrator-set status. Two changes:

1. **Add an intent table** (or an `agents.intents` JSONB field):
   `{should_drain: bool, target_version: str, drain_reason: str}`.
   Heartbeat handler is **forbidden** from writing here.
2. **Heartbeat response carries an `actions` array** when intents are
   non-default. The agent reads them on each heartbeat and reacts —
   though for *this* design, the primary actuation is still SIGTERM,
   not heartbeat-action; the actions field is for soft hints (e.g.
   "you'll be drained at next phase boundary, plan accordingly").

Status remains agent-reported. Intent is orchestrator-owned. They live
in different columns and never conflict.

## Per-implementation specifics

### `AgentInstanceManager`

- `kind = "agent"`
- `list_instances()` queries `agents` table joined with K8s pod list
  (cross-checks DB and reality; emits a warning on divergence).
- `is_healthy()` → `last_heartbeat > now - 3min` AND pod phase Running.
- `is_idle()` → `status='ready' AND current_job_id IS NULL AND
  thread_id IS NULL`.
- `drain()` → for `working` agents, set `drain_pending` intent and let
  the next phase boundary trigger Continue-as-New freeze. For `session`
  agents, mark thread `ended` (forces in-pod watchdog from `4c64a30` to
  exit). For `ready` agents, delete pod with grace_s=30.
- `delete()` → `kubectl delete pod` with the configured grace period.
- Wraps existing `AgentProvisioner` for the actual pod operations.

Subsumes: `_drain_stale_image_agents`, parts of `stale_agent_detector`,
parts of `reap_pods`.

### `WorkspaceInstanceManager`

- `kind = "workspace"`
- `list_instances()` queries K8s with label selector
  `srw.io/component=agent-workspace`.
- `is_healthy()` → pod phase Running AND SSH ping succeeds (caches
  result for 30s to avoid spamming SSH).
- `is_idle()` → bound job/thread is in `paused`/`pending_review`/
  `waiting_for_reply` AND `last_activity > WORKSPACE_IDLE_TIMEOUT`.
- `snapshot()` → existing `WorkspaceSuspensionService.suspend_*` logic,
  factored to return the S3 snapshot reference.
- `restore()` → existing `restore_workspace` logic.
- `drain()` → snapshot then delete with grace_s=10.
- `delete()` → `kubectl delete pod`.

Subsumes: `workspace_idle_sweeper`, plus a new crash detector that
catches `Unknown`/`Failed` pods (the gap in
`stuck_thread_workspace_pods.md`).

### `VMInstanceManager`

- `kind = "vm"`
- `list_instances()` dispatches to the configured backend (NATS/HTTP/
  KubeVirt/Docker) for enumeration. Backend selection happens inside
  the manager — the reconciler sees one VM kind.
- `is_healthy()` → backend-specific liveness probe (NATS request,
  HTTP GET, K8s pod phase, Docker SSH ping).
- `is_idle()` → same predicate as workspace (bound job/thread idle past
  threshold).
- `snapshot()` → existing `VMProvisioner.release_vm` snapshot path,
  factored to return the S3 reference without deleting.
- `restore()` → re-create VM and restore from snapshot.
- `drain()` → snapshot then backend-specific shutdown.
- `delete()` → backend-specific delete.

Subsumes: any crash recovery that doesn't yet exist for VMs. The
4-backend dispatch stays internal to `VMInstanceManager`; the lifecycle
interface sees one VM type.

## Rollout plan

Three phases, each independently valuable. After any phase we can stop
without leaving the system worse than today.

### Phase 0 — Stopgap (precedes Phase 1)

A small bridge that makes the existing `_drain_stale_image_agents`
mechanism functionally complete for the *idle agent* case, so the dev
cluster doesn't accumulate zombies in the days between now and Phase 1
landing. ~25 lines, no new dependencies, no schema changes.

- Heartbeat handler preserves `draining` (`postgres.py:1737`): when the
  existing row is `draining`, the agent's reported status doesn't
  overwrite. Closes Bug 2 from the issues doc for the limited case
  where the orchestrator already wrote `draining`.
- `reap_pods` adds a `drained` category that force-deletes pods whose
  agents are in `draining` (`agent_provisioner.py:570`). Closes the
  actuation gap.

**Out of scope for Phase 0**: widening drain eligibility (Bug 1). That
requires the version-pinning logic Phase 1 introduces — without it,
widening would interrupt mid-job work.

### Phase 1 sub-phasing

Phase 1 ships in three sub-phases (see commits and the `[completed]`
markers below for what's landed):

- **Phase 1a — Foundation + cleanup** (✅ shipped): lifecycle module
  skeleton, `agents.intents` JSONB migration, `srw/build-sha` pod
  annotation, removed static `srw-agent` Helm Deployment.
- **Phase 1b — Manager + reconciler wiring** (✅ shipped):
  `AgentInstanceManager` + reconciler `tick()` algorithm wired into
  `lifespan`, `list_pods()` startup reconciliation, Helm RBAC for
  `pods/finalizers`, removed `_drain_stale_image_agents` and inline
  SHA writes at dispatch sites.
- **Phase 1c — Agent-side drain reaction** (✅ shipped): heartbeat
  response carries intents, `orchestrator_client.heartbeat()` returns
  the response, `run_heartbeat_loop` exposes an `on_response` callback,
  persistent agents react to `should_drain` (detach + exit), worker
  agents react when idle (exit) and expose `is_drain_requested()` to
  the graph when busy, `version_upgrade` freeze type pauses the job
  for re-dispatch by the auto-assign dispatcher.
- **Phase 1e — Soft drain signal** (✅ shipped, 2026-05-10): the
  reconciler tick now fires `manager.signal_drain_pending(inst)` on
  every drifted instance — idle and busy. Closes a gap discovered
  during cluster verification: previously the busy path just
  `skipped_busy += 1`'d without writing the intent, so the in-pod
  Phase 1c handler never had anything to react to. Agent manager's
  `signal_drain_pending` writes the `should_drain` intent without
  flipping status; `drain` continues to handle the idle case
  (intent + status flip in one statement). Workspace and VM managers
  implement `signal_drain_pending` as a no-op since they have no
  in-pod hook for soft signals.
- **Phase 1d — Worker graph Continue-as-New** (✅ shipped):
  `src/graph.py` `handle_transition` checks `_is_drain_requested()` at
  every phase boundary; on True, writes `output/job_frozen.json` with
  `freeze_type=version_upgrade` and sets `should_stop=True`. End-to-end
  drift drain now works for busy workers too: lifecycle reconciler →
  heartbeat intent → in-pod flag → graph freeze at next phase boundary
  → orchestrator pauses → auto-assign re-dispatches to a fresh-version
  pod with the same job context.
- **Phase 2a — Workspace manager + drift** (✅ shipped):
  `WorkspaceInstanceManager` (StatefulInstanceManager) joins K8s pods
  with jobs/threads context, snapshot delegates to `SnapshotService`,
  drain → delete (snapshot already done by reconciler), restore via
  `WorkspaceSuspensionService`. Workspace pods now carry an
  `srw/build-sha` label and are registered with the lifecycle
  reconciler — drift drain works end-to-end for both job and thread
  workspaces.
- **Phase 2b — Crash recovery** (✅ shipped): the reconciler tick now
  checks `is_healthy()` per instance and force-deletes unhealthy ones
  before drift consideration. Closes the gap from
  `docs/issues/stuck_thread_workspace_pods.md` where Unknown/Failed
  workspace pods accumulated for days. Defensive: an exception in
  `is_healthy` is treated as healthy to avoid a flaky check
  triggering mass deletes.
- **Phase 3 — VM manager** (✅ shipped): `VMInstanceManager`
  (StatefulInstanceManager) iterates `jobs.context.vm` and
  `threads.metadata.vm` JSONB rows (no fleet K8s object — VMs aren't
  enumerable via label selector). The 4-backend dispatch (NATS / HTTP
  controller / direct KubeVirt / Docker pool) stays inside
  `VMProvisioner`; the reconciler sees one `vm` kind. Drift detection
  reads SHA from `vm.vm_image`. `is_healthy` flags `vm.status='failed'`
  for crash recovery. Snapshot via `SnapshotService.capture_vm_snapshot`
  with `source_type='vm'`. Drain → delete via
  `vm_provisioner.delete_vm` / `delete_thread_vm`.

### Deferred from Phase 1

- **`persistent_provisioner.py` migration.** Still has 6 active call
  sites in `main.py`. Out-of-scope for Phase 1.
- **Crash recovery consolidation into the lifecycle reconciler.**
  `reap_pods` still owns crash detection. Folding it in is a Phase 2
  concern (alongside `WorkspaceInstanceManager`).

### Phase 1 — Interface + agent migration

- Add `orchestrator/services/lifecycle/{types,reconciler}.py` with the
  Protocol definitions and the generic reconciler.
- Implement `AgentInstanceManager` wrapping the existing
  `AgentProvisioner`.
- Add `agents.intents` JSONB column (DB migration) and the heartbeat
  handler write-protection (no-op against `intents`, preserve
  orchestrator-set status).
- Wire `should_drain` intent through to the existing in-pod watchdog
  (`src/api/persistent_app.py`) so persistent agents react.
- Add `version_upgrade` freeze type and the phase-boundary
  Continue-as-New flow.
- Replace `_drain_stale_image_agents` and the inline SHA checks at
  dispatch time with calls into the new manager.
- **Set `srw/build-sha` annotation** on every provisioned pod
  (`agent_provisioner.py:_build_pod_manifest`). Reconciler can
  enumerate stale pods directly by label.
- **Add `list_pods()` startup reconciliation**: on orchestrator boot,
  list pods with `srw/managed-by=agent-provisioner` and reconcile
  in-memory state from K8s reality before accepting heartbeats.
- **Remove `srw-agent` Helm Deployment** (`helm/templates/agent/deployment.yaml`)
  and the corresponding `agent.replicas` value. Static fallback is gone.
- ~~Delete `orchestrator/services/persistent_provisioner.py`~~ —
  deferred. Real callers exist; migration is a separate workstream.
- **Helm RBAC**: add `pods/finalizers` (and any missing `pods/status`)
  to the orchestrator `Role` so the reconciler can manipulate finalizers.
- Keep `agent_pool_reconciler` — it still owns the warm pool — but
  delegate drift/drain to the new reconciler.
- **Ship value**: the dev-cluster zombie problem stops recurring.
  Fixes Bug 1 and Bug 2 from the issues doc, plus the bootstrap gap
  and the dormant-code cleanup.

### Phase 2 — Workspace migration + crash recovery

- Implement `WorkspaceInstanceManager` wrapping `ContainerProvisioner`
  and `WorkspaceSuspensionService`.
- Add the missing crash detector (handles `Unknown`/`Failed` pods that
  `docs/issues/stuck_thread_workspace_pods.md` documents).
- Add finalizer-based snapshot-before-delete enforcement.
- Add image-drift detection for workspace pods.
- Migrate `workspace_idle_sweeper` to delegate to the manager.
- **Ship value**: workspace crash backlog gets cleaned up; image drift
  on workspaces stops accumulating; cleaner failure semantics.

### Phase 3 — VM migration

- Implement `VMInstanceManager` with the 4-backend dispatch internal.
- Add image-drift detection across all VM backends.
- Add health-probe paths that don't already exist (NATS daemon ping,
  HTTP controller health endpoint).
- Migrate VM idle suspension to delegate to the manager.
- **Ship value**: drift handling for VMs; uniform crash recovery
  semantics across all instance kinds.

### Out-of-band cleanup (one-time)

The 5 zombie agents on the dev cluster have to be deleted manually
before Phase 1 helps them — they don't have the in-pod watchdog code
from `4c64a30`, so they can't react to any new intent we ship. Document
this in the rollout playbook so the same trap doesn't bite future
deployments.

## Decisions

Resolved during design discussion (2026-05-09):

1. **Intent representation**: `agents.intents` JSONB column. Lightest
   migration, matches the existing `agents.metadata` pattern. Becomes a
   separate table later if it outgrows the column.
2. **Reconciler home**: `orchestrator/services/lifecycle/`. Co-located
   with the provisioners the managers wrap.
3. **Reconciler tick rate**: 60s default for all kinds, Helm-overridable
   per kind. Open to a faster drift tick (~15s) if rollouts feel slow.
4. **Disruption budget**: `max(1, total // 4)` per kind, Helm-configurable.
5. **`srw-agent` Helm Deployment**: removed in Phase 1. Already
   `replicas: 0`; the dynamic provisioner is the source of truth.
6. **`persistent_provisioner.py`**: NOT actually dormant (initial
   inventory was wrong — it has 6 active call sites in `main.py` for
   thread-bound persistent agent lifecycle, plus a real test file).
   Migrating its callers to `agent_provisioner` is a separate workstream;
   deferred out of Phase 1 scope. Tracked as a follow-up.
7. **`srw/build-sha` pod annotation**: added in Phase 1 at provisioning.
   Lets the reconciler enumerate stale instances by label without
   joining the DB. Carried forward to workspaces in Phase 2.
8. **Bootstrap-on-restart**: a `list_pods()`-based startup reconciliation
   ships in Phase 1, closing the gap where the orchestrator forgets
   about live pods until they heartbeat.
9. **RBAC**: orchestrator ServiceAccount gains `pods/finalizers` patch
   permission via Helm chart change in Phase 1.

Still empirical (tune with production data):

- Drain grace periods per kind. Defaults: agent (worker) =
  existing `terminationGracePeriodSeconds=180s`, agent (session) = 60s,
  workspace = 30s post-snapshot, VM = 60s post-snapshot.
- `version_upgrade` freeze type semantics. Adds a new `freeze_data.type`
  value; the orchestrator's completion service re-dispatches the same
  job context onto a fresh-version agent.

## References

- [`docs/issues/agent_lifecycle_management.md`](../issues/agent_lifecycle_management.md)
  — full problem analysis + research
- [`docs/issues/stuck_thread_workspace_pods.md`](../issues/stuck_thread_workspace_pods.md)
  — workspace crash recovery gap
- [`docs/features/agent_lifecycle.md`](agent_lifecycle.md) — prior
  agent-only design (deferred items still open)
- [`docs/features/ephemeral_workspaces.md`](ephemeral_workspaces.md) —
  workspace snapshot/restore design (already shipped)
- [Karpenter disruption](https://karpenter.sh/docs/concepts/disruption/)
  — drift detection + provision-replace-terminate pattern
- [Temporal Worker Versioning](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning)
  — Pinned vs Auto-Upgrade, Continue-as-New
- [Anthropic Claude Agent SDK hosting](https://code.claude.com/docs/en/agent-sdk/hosting)
  — Hybrid Sessions pattern
- [Knative request flow](https://knative.dev/docs/serving/request-flow/)
  — SIGTERM-as-drain-signal reference
- [Kubebuilder good practices](https://book.kubebuilder.io/reference/good-practices)
  — spec/status split, observedGeneration
