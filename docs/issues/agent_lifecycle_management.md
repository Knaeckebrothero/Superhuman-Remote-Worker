# Agent lifecycle management — fragmented reconcilers, write-only `draining`

## Symptom (observed 2026-05-09 on dev cluster)

Namespace `superhuman-remote-worker`. Orchestrator running on `sha-7a1c4c5`,
`PERSISTENT_AGENT_IMAGE=...:sha-4c64a30`. Agent pods still alive after
~50 deploy rollouts:

| Pod | build_sha | Age | Agent status | Thread? |
|---|---|---|---|---|
| `srw-agent-j-4bdcf4e6` | `b4d7fd1` | 5d21h | `session` | yes |
| `srw-agent-j-883ed471` | `e50ea0c` | 5d17h | `session` | yes |
| `srw-agent-s-330c67ef` | `e50ea0c` | 5d19h | `session` | yes |
| `srw-agent-s-3be3c320` | `b994086` | 3d17h | `session` | yes |
| `srw-agent-j-9ea244d6` | `f50eade` | 3d    | `working`  | job  |

All five are heartbeating every 5s, none are eligible for any existing
cleanup path, and the version-aware drain we added writes a status nothing
reads.

## Current architecture

Two parallel 60s reconciler loops handle agent lifecycle, plus dispatch-time
checks, plus in-pod watchdogs. They overlap, share no contract, and each
has a different mental model of "stale".

### Reconciler 1: `stale_agent_detector` (`orchestrator/main.py:381`)

DB-side. Operates on the `agents` and `threads` tables only — no K8s
awareness.

1. `mark_stale_agents_offline(timeout=3min)` — flip non-heartbeating agents
   to `offline`.
2. `mark_stuck_working_agents_ready` — flip agents reporting `working` with
   `current_job_id IS NULL` back to `ready` (defense-in-depth for the
   `_reset_to_idle` failure mode).
3. `mark_stuck_session_agents_ready` (added 2026-05-08, commit `de52290`) —
   flip agents reporting `session` whose bound thread has been `ended` for
   ≥2min back to `ready` and clear `thread_id`. Heartbeat-independent on
   purpose: zombies heartbeat normally.
4. `mark_orphaned_threads_ended` — flip `created`/`active` threads bound
   to offline agents → `ended`.
5. `recover_orphaned_jobs` — re-pause jobs assigned to offline agents.
6. `gc_offline_agents(retention=24h)` — delete `offline` agent rows.

### Reconciler 2: `agent_pool_reconciler` (`orchestrator/main.py:450`)

K8s-side. Talks to the kube API and reads/writes the `agents` table.

1. `ensure_warm_pool()` — keep `MIN_AGENTS` warm pods.
2. `reap_pods()` (`agent_provisioner.py:517`) — single-pass GC over
   provisioner-owned pods. Categories:
   - `completed` — pod phase `Succeeded`/`Failed`.
   - `crashed` — pod phase `Running` but `agent` container terminated
     ≥60s ago.
   - `stale` — pod phase `Running` and the agent's DB row is `status =
     'offline'` (heartbeat ≥10min stale).
   - `unstartable` — pod `Pending` with terminal waiting reason
     (`ImagePullBackOff` etc.) older than 5min.
3. `scale_down_idle()` — drop excess warm pods.
4. `_drain_stale_image_agents()` — the version-aware piece. Compares
   `agents.metadata->>'build_sha'` to `_expected_agent_shas()` (parsed
   from `AGENT_IMAGE` / `PERSISTENT_AGENT_IMAGE` env vars on the
   orchestrator pod). Mismatch → `UPDATE agents SET status = 'draining'`.

### Dispatch-time SHA check (3 sites)

Each writes the same `UPDATE agents SET status = 'draining'`:

- `_find_idle_persistent_agent` (`main.py:1393`) — when looking for an
  agent for a new persistent thread.
- Worker dispatcher (`main.py:2080`) — when matching jobs to ready agents.
- `_drain_stale_image_agents` (`main.py:516`) — the reconciler pass.

All three filter on the same predicate (varies slightly): `status='ready'`,
`current_job_id IS NULL`, `thread_id IS NULL`. The check is
`_agent_sha_is_current(metadata)` (`main.py:1337`).

### In-pod watchdogs (`src/api/persistent_app.py`, added 2026-05-08, commit `4c64a30`)

Live inside the agent process — only present in pods spawned from `≥4c64a30`.

- `_boot_ws_watchdog` — exit if `/ws/chat` doesn't connect within 600s
  of attach (default).
- `_thread_status_watchdog` — poll `GET /api/threads/{id}/lifecycle`
  every 60s; exit if status leaves `{created, active}`.

These are the only mechanism that *physically terminates* a session-
bound pod from inside. The orchestrator has no such mechanism.

### Heartbeat handler (`postgres.py:1673`)

Every 5s the agent POSTs its self-reported status. The handler runs
unconditionally:

```sql
UPDATE agents
SET status = $1, current_job_id = $2, last_heartbeat = CURRENT_TIMESTAMP
WHERE id = $3
```

No merge, no precedence rule. Whatever the agent says wins. The HTTP
response is a bare `{"status": "ok"}` — no commands, no pending intents,
no version hints. There is no channel for the orchestrator to push
anything back to the agent on the heartbeat path.

### Pod manifest and capacity controls

`AgentProvisioner._build_pod_manifest()` (`agent_provisioner.py:795`)
constructs the pod with:

- Name: `srw-agent-{j|s}-{8-char-hex}` (`j`=job, `s`=session) — the `j/s`
  prefix is the only thing distinguishing job-mode from session-mode pods
  externally; both run the same image with different CLI args.
- Labels: `app=srw-agent`, `srw/component=agent`,
  `srw/managed-by=agent-provisioner`, `srw/purpose={job|session}`. Helm
  selector labels (`app.kubernetes.io/name|instance|component`) are
  propagated when `AGENT_LABEL_NAME` / `AGENT_LABEL_INSTANCE` are set
  (commit `878192f`). Session pods additionally carry
  `srw/thread-id={thread_id[:12]}`.
- `metadata.annotations`: `build_sha` is **not** set as a label or
  annotation on the pod itself — it lives only on the agent's first
  heartbeat in `agents.metadata` JSON. Pod-only enumeration cannot
  filter by SHA.
- No `ownerReferences`, no finalizers. Bare pod.
- `terminationGracePeriodSeconds: 180` (allows final heartbeat + audit
  flush).
- Probes: liveness/readiness/startup all on `:8001`. Startup allows
  100s.
- Capacity envelope: `MAX_AGENTS=10`, `RESERVED_SESSION_SLOTS`,
  `RESERVED_JOB_SLOTS` — provisioner can evict idle agents of one
  purpose to honor the other purpose's reservation
  (`_try_evict_for_reservation`, `agent_provisioner.py:717`).
- Helm `srw-agent` Deployment exists (`helm/templates/agent/deployment.yaml`)
  but `helm/values.yaml:119` sets `replicas: 0` with the comment
  *"0 = dynamic provisioning via AgentProvisioner"*. The static
  Deployment is a vestigial fallback, not the source of truth.

### Other reconciler loops in the orchestrator (for context)

The orchestrator runs **at least 9 independent background loops**, each
on its own timer. Agent lifecycle is split across two of them, but the
broader pattern of "many small sweepers, each with its own predicate
and period" matters for the unification discussion below:

| Loop | File:line | Period | Concern |
|---|---|---|---|
| `stale_agent_detector` | `main.py:381` | 60s | DB-side agent reconciliation |
| `agent_pool_reconciler` | `main.py:450` | 60s | K8s-side agent pods + warm pool |
| `sudo_expiration_sweeper` | `main.py:523` | 15s | Sudo approval timeouts |
| `ide_session_ttl_sweeper` | `main.py:547` | 30s | IDE session expiry |
| `workspace_idle_sweeper` | `main.py:572` | 30s | Workspace pod idle suspension |
| `snapshot_gc_sweeper` | `main.py:604` | varies | Workspace snapshot GC |
| `delegation_timeout_sweeper` | `main.py:6365` | 30s | Delegated job timeouts |
| `imap_poll_loop` | `main.py:679` | 30s | IMAP integration polling |
| `quiet_hours_digest_loop` | `main.py:631` | 5min | Notification digest scheduling |

### Bootstrap gap (orchestrator restart)

There is **no startup reconciliation** that lists existing agent pods
in K8s and rebuilds the in-memory view. The orchestrator's
`agents`-table view is rebuilt purely from incoming heartbeats. If a
pod is up but hasn't heartbeated since the orchestrator restarted, it
appears to not exist; if a pod was up and exited before its first
heartbeat after the orchestrator restarted, its row is orphaned. Combined
with bare-pod (no ownerReference) provisioning, an orchestrator crash
mid-creation can leave a pod with no DB record at all — only the
`reap_pods` `unstartable` path catches it, and only if the pod failed
to start.

## Bug 1: drain eligibility is too narrow

All three drain call sites filter to the same predicate — fully idle:

```sql
WHERE status = 'ready' AND current_job_id IS NULL AND thread_id IS NULL
```

Of the 5 zombies, 0 match: 4 are `session`, 1 is `working`. They never
become drain candidates.

`mark_stuck_session_agents_ready` is supposed to be the bridge: flip
zombie `session` agents back to `ready`, after which the next reconciler
tick sees them and drains. The bridge requires `thread.status='ended'`
with `ended_at` ≥2min old. Two ways that gets set:

1. Agent calls `_detach_session()` → writes thread `ended`. Old agents
   never call this for the abandoned cases (no watchdog code).
2. `mark_orphaned_threads_ended` flips threads of *offline* agents.
   These agents aren't offline — they heartbeat fine.

So nothing flips the threads to `ended`, the bridge never fires, the
agents stay `session`, and the drain never sees them.

## Bug 2: `draining` is a write-only label

Grep across `orchestrator/` and `src/api/`:

- 3 `UPDATE agents SET status = 'draining'` writes (the drain sites).
- 0 reads of `status = 'draining'` in the orchestrator.
- Agent-side `draining` writes (`src/api/dual_app.py:181`,
  `src/api/app.py:139`) only fire on the agent's *own* shutdown path —
  the agent never reads its row to check whether the orchestrator marked
  it.
- `reap_pods` does not consider `draining` agents — only `offline`
  rows for the `stale` category.

So three things follow:

1. Marking `draining` doesn't trigger pod deletion. No K8s call, no SIGTERM,
   nothing. The pod keeps running.
2. The agent doesn't notice it was marked draining. It keeps heartbeating
   `ready`. Five seconds after the drain write, the heartbeat handler at
   `postgres.py:1737` overwrites `draining` → `ready`.
3. Dispatch refuses to send fresh work to the draining/stale agent — that
   *one* purpose works — but the slot stays held forever.

Net effect: the version-aware drain is, in practice, an "ignore from
dispatch" filter with a `draining` flicker that gets clobbered every 5s.

## Architectural smell

Three loci of responsibility, each with its own predicate:

| Concern | Owner | Predicate |
|---|---|---|
| Heartbeat freshness | `mark_stale_agents_offline` | `last_heartbeat < now - 3min` |
| Self-reported inconsistency | `mark_stuck_*_ready` | `status` ⨯ FK state |
| Pod liveness | `reap_pods` | k8s phase ⨯ `agents.status='offline'` |
| Image freshness | `_drain_stale_image_agents` | `metadata.build_sha ∉ expected` |
| Orphan-from-inside | in-pod watchdogs | thread lifecycle poll |

The `agents.status` enum (`booting | ready | working | session |
draining | completed | failed | offline`) tries to encode all of these,
but heartbeat overwrites it on a 5s cadence and there's no precedence
rule for orchestrator-set vs agent-reported.

Two reconciler loops (`stale_agent_detector` + `agent_pool_reconciler`)
both run every 60s, both touch agents, but neither is authoritative for
the full state machine. `_drain_stale_image_agents` is misplaced inside
`agent_pool_reconciler` (it's a DB-only operation that happens to sit
next to k8s ones).

## Other lifecycle systems in this codebase

A parallel system exists for **workspace pods** (`workspace-*`,
`ws-thread-*`) and **VMs** managed by `vm_provisioner.py`. They've solved
some of these problems differently — and failed in some of the same
places. Worth a side-by-side look before designing anything new.

### Workspace pods (K8s) — `services/container_provisioner.py`

- One pod per job (`workspace-{job_id[:12]}`) or per thread
  (`ws-thread-{thread_id[:12]}`), labels `srw/job-id`, `srw/thread-id`,
  `srw.io/component=agent-workspace`. No ownerReferences, same as agents.
- **Idle suspension exists** — `workspace_idle_sweeper` (`main.py:572`,
  every 30s) finds workspaces older than `WORKSPACE_IDLE_TIMEOUT` (default
  30 min) attached to paused/pending-review/waiting jobs, snapshots
  `/home/agent-host` to S3 via `WorkspaceSuspensionService.suspend_*`,
  then deletes the pod. On resume, `restore_workspace` re-creates a fresh
  pod and restores from S3. End-to-end "ephemeral with persisted state"
  pattern, production-ready (commit lineage in
  `docs/features/ephemeral_workspaces.md`).
- **No crash detector.** `Unknown` / `Failed` workspace pods accumulate
  indefinitely. `docs/issues/stuck_thread_workspace_pods.md` documents 10
  such pods sitting for 5+ days after a node power event on 2026-04-28.
  No reaper exists for this case (item 3 in that doc, status: not done).
- **No version drift reconciliation.** Image is pinned at creation via
  `WORKSPACE_IMAGE`; nothing rotates running pods on a new build.

### VMs — `services/vm_provisioner.py`

Four backends with priority ordering: NATS > HTTP controller > direct
KubeVirt > Docker pool. `release_vm` / `release_thread_vm` snapshot to S3
then delete; the same idle sweeper drives suspension. There is **no
heartbeat from the VM daemon** — the orchestrator polls SSH/API on demand
and can't detect silent VM death until the next operation times out.

### Compose-mode workspaces — `services/docker_provisioner.py`

Static pool from `WORKSPACE_HOSTS`. Release performs SSH-based reset
(`rm -rf ~/workspace/*; mkdir -p`) instead of pod delete — the container
stays alive, the slot is recycled.

### Side-by-side

| Concern | Agent pod | Workspace pod | VM |
|---|---|---|---|
| Created by | `AgentProvisioner` | `ContainerProvisioner` | `VMProvisioner` (4 backends) |
| Identity | DB row + pod labels | DB context JSON + pod labels | DB context JSON only |
| Idle reclaim | None (heartbeat-only liveness) | **Yes — snapshot+suspend, 30s sweeper** | **Yes — same sweeper** |
| Crash recovery | `reap_pods` 60s, catches `Failed`/`crashed`/heartbeat-stale | **None** — Unknown pods sit forever | **None** — unreachable VMs orphan |
| Image drift | Drain logic exists (broken — see Bug 1/2) | **None** | **None** |
| Snapshot before delete | None | **Yes** (`release_workspace`, S3) | **Yes** (`release_vm`, S3) |
| Heartbeat in | 5s, write-through | None (orchestrator polls) | None |
| Owner refs | No | No | No |

### Patterns this codebase has already invented (worth borrowing)

- **Snapshot-then-delete with S3-backed restore.** Workspace path has
  this. Agents don't. For long-running session pods this is the natural
  way to evacuate a pod onto a new image without losing state — exactly
  the same problem we have with stale-image session agents.
- **Provisioner dispatch by context** — `WorkspaceSuspensionService`
  reads `workspace_container.provisioner` and dispatches to the right
  backend. Agents don't have a backend abstraction yet but will if we
  ever support multiple workspace topologies.
- **Idle-sweeper-as-a-pattern.** A separate sweeper per concern, all
  60s/30s loops over predicates. We already have ~9 of them (table
  above) — a unification pass is overdue.

### Patterns conspicuously missing in **both** systems

- **Image-drift reconciliation.** Workspaces have the same problem as
  agents and the same lack of a fix.
- **Crash recovery for non-agent pods.** Agents are caught within
  60–180s; workspace pods are not caught at all.
- **Owner references / finalizers.** Both systems are bare-pod with
  no K8s-native cleanup hook.
- **Heartbeat from the workload.** Workspaces and VMs don't heartbeat,
  so silent failures are invisible until something tries to use them.

## Industry references

Pulled from a research pass over Argo / Tekton / KEDA / Karpenter /
Knative / Kueue / Crossplane (K8s patterns) and Temporal / LangGraph /
Anthropic / OpenAI / Modal / Ray / Fly / GitHub Actions (similar agent
runtimes).

### K8s control-plane patterns for non-Deployment workloads

| Pattern | Production proof | Applies to us? |
|---|---|---|
| **Owner references on a custom CRD** | Argo Workflows, Karpenter NodeClaim, Kueue Workload | Yes — would replace bare-pod + DB row with a real CR. Bigger lift. |
| **Finalizers as the cleanup hook** | Karpenter (`karpenter.sh/termination`), Crossplane MR | Yes — gives us a guaranteed pre-delete callback to flip DB row, archive logs, drop SSH. Adoptable independent of CRDs. |
| **TTL-after-finished controller** | K8s `ttlSecondsAfterFinished` on Job | Limited — only for one-shot work, not session agents. |
| **Drift detection + provision-replace-terminate** | **Karpenter** ([disruption docs](https://karpenter.sh/docs/concepts/disruption/)) | **Direct fit.** Hash desired pod spec → annotate pods → reconciler flags drift → spawn replacement → `Ready` gate → graceful delete of old. Disruption budgets cap rollout rate. |
| **Revision-based replacement** | Knative ([rolling-out](https://knative.dev/docs/serving/rolling-out-latest-revision/)) | Heavy for our shape. Knative makes every spec change a new immutable Revision; gradual traffic shift. Closer to our model than Deployment rollout but still designed for stateless request handling. |
| **Scale-to-zero on stale, scale-up with new** | KEDA ScaledJob | Workable for *idle* workers; useless for in-progress sessions. |
| **Graceful drain via SIGTERM + preStop** | Knative queue-proxy ([request-flow](https://knative.dev/docs/serving/request-flow/)) | **Direct fit.** Standard kubelet path: orchestrator issues `delete pod`, agent has SIGTERM handler that refuses new work and finishes in-flight, then exits. Replaces our broken `draining` write. |
| **CRD status with `observedGeneration`** | Kubebuilder convention | Yes — separates orchestrator-set *intent* from agent-reported *observed*. Solves Bug 2's "heartbeat overwrites draining". |

**Direct prescription for our shape (bare pods, long-running, mixed
job/session, heartbeat already present):**

1. **Stop using `draining` as the actuation channel.** It's fine as a
   human-readable status field, but actuation must be standard K8s:
   orchestrator issues `delete pod` with a long
   `terminationGracePeriodSeconds`; the agent has a SIGTERM handler that
   refuses new work, finishes the current task, exits. This is the
   Knative queue-proxy / Karpenter Eviction-API model.
2. **Add a finalizer for cleanup hooks.** Either on the pod directly or
   on a per-agent CR. Guarantees DB-row cleanup, log archive, workspace
   release before the pod disappears from etcd. Doesn't require migrating
   to a full CRD/operator if we put the finalizer on the pod and our
   reconciler removes it after cleanup.
3. **Hash-based drift detection (Karpenter pattern).** Compute a hash
   of the desired pod spec (image SHA + relevant env), annotate every
   created pod with it. Reconciler tags pods with stale hashes for
   replacement. Cap concurrent drains via a disruption budget so a
   rollout doesn't empty the warm pool.
4. **Spec/status split with `observedGeneration`.** Add an
   orchestrator-owned intent field (`should_drain`, `target_image_sha`)
   that the heartbeat handler is **forbidden** from overwriting. Agent
   reads this on each heartbeat response and reacts. Heartbeat continues
   to write only the agent-owned fields (`status` if you keep it as
   "what the agent thinks it is", `last_heartbeat`, `metrics`).
5. **Don't migrate to a full kubebuilder/operator-sdk CRD just for this.**
   Adopt the *primitives* — finalizers, graceful delete, drift hash,
   intent/observed split — without inverting the architecture. Postgres
   playing the role of etcd is a legitimate (if off-piste) operator
   pattern.

**Pitfalls to avoid:**

- **Two parallel control planes** (DB-as-signal + K8s-as-signal). Argo
  explicitly avoided this; we drifted into it. Pick one channel for each
  concern and stick with it.
- **Manually removing finalizers** when something gets stuck is
  destructive — Crossplane's troubleshooting guide warns about this.
  Cleanup logic must be idempotent.
- **`terminationGracePeriodSeconds` is a contract, not a guarantee.**
  Force-delete bypasses it, node eviction can SIGKILL early. Agent
  shutdown must be partial-failure-tolerant.
- **Race: heartbeat-after-delete.** Orchestrator must check pod is not
  `Terminating` before applying any heartbeat-derived state change.
  `deletionTimestamp` is the "ignore further input" marker.
- **Thundering herd on rollout.** Without a disruption budget, drift
  detection across N agents starts N replacements at once. Karpenter,
  Knative, and the Eviction API all rate-limit for a reason.

### Similar systems (long-running agent runtimes)

The most directly analogous systems and what they actually do:

- **Temporal Worker Versioning** (the closest fit). Workers register a
  `Worker Deployment Version` (deployment name + build ID). Workflow
  Types are declared either `PINNED` (entire execution stays on its
  starting version, even for years) or `AUTO_UPGRADE` (drifts to current).
  Old versions transition `Active → Draining → Drained` and are GC'd
  only when their open-pinned-workflow count hits zero. The hard case —
  "a workflow that processes a few requests a day" — is explicitly
  called out and addressed by an **Upgrade-on-Continue-as-New** pattern
  (GA 2025): workflows checkpoint via Continue-as-New and opt into the
  current version at that boundary.
  ([worker-versioning](https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning),
  [continue-as-new GA](https://temporal.io/blog/ga-worker-versioning-public-preview-upgrade-on-continue-as-new))
- **Anthropic Claude Agent SDK** is explicit: *"An agent session will
  not timeout, but consider setting a 'maxTurns' property"*. Recommends
  **Pattern 3 (Hybrid Sessions)** — ephemeral containers hydrated from
  persisted history, spun up on user return and torn down on idle. **No
  attempt to migrate sessions across versions** — restart safety lives
  in the SDK's session resumption feature.
  ([hosting](https://code.claude.com/docs/en/agent-sdk/hosting))
- **LangGraph Platform** is architecturally closest (API + dedicated
  queue workers + durable checkpoints). Workers acquire leases on runs;
  on worker death, another worker resumes from the last checkpoint.
  Public docs are thin on lease timeout / drain semantics. Versioning
  is at the "assistant" level, not the worker.
- **OpenAI Assistants** locks the Thread when a Run is `in_progress`;
  Code Interpreter sessions are TTL-bounded at 1 hour. **OpenAI is
  sunsetting the entire stateful Assistants API in August 2026** in
  favor of the stateless Responses API — a strong product-level vote
  *against* maintaining server-side stateful sessions.
- **Modal** cold-starts new containers on `modal deploy` *before* old
  ones stop accepting new requests; old containers continue serving
  in-flight with no documented max drain. Version pinning lives at the
  *client* side via semver. ([managing-deployments](https://modal.com/docs/guide/managing-deployments))
- **Ray Serve** offers in-place config updates plus **Incremental
  Upgrade Strategy**: stand up a new RayCluster, shift traffic in
  `stepSizePercent` increments, then tear down the old. Detached actors
  survive job exits but the standard "rolling upgrade of named actors"
  answer is "kill and recreate." ([incremental upgrade](https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/rayservice-incremental-upgrade.html))
- **GitHub Actions self-hosted runners** mandate self-update within 30
  days. The community recommends *not* relying on self-update — bake
  versioned images and replace runners externally.

### Patterns that recur across these systems

- **Version-as-label, not version-as-binding.** Temporal, Modal, Ray,
  LangGraph all attach a version label to the *work*, then route on it.
  Pods are not bound to a version; the routing decision is per-task.
- **Pin long-lived state to its starting version; let new state pick the
  new version.** Temporal Pinned, Fly bluegreen for sessions, Anthropic's
  hybrid pattern.
- **Durable checkpoints make worker death survivable.** Temporal
  histories, LangGraph checkpoints, Anthropic session resumption.
  Heartbeats are a liveness signal; durability is the real safety. Our
  graph already does this — `freeze_data` + workspace artifacts is the
  substrate.
- **Drain ≠ deadline.** Most systems drain "until done" and use operator
  intervention (Temporal `wake_up` Signal, Continue-as-New) to nudge
  stragglers, rather than a hard kill.
- **Two-tier scaling.** API/dispatch tier and execution tier scale
  independently. Same shape as our orchestrator + agent split.

### Anti-patterns called out

- **In-place self-update of the running worker** (GitHub Actions) —
  fragile; orphaned jobs and stuck workers.
- **Unbounded session lifetime with no idle reclamation** (early
  Assistants API, plain Celery) — sessions pile up indefinitely. **This
  is exactly our current state.**
- **No-op SIGTERM** (early Airflow Helm, generic Celery) — pod gets
  killed mid-task because grace period elapses before drain finishes.
  Fix: `cancel_consumer` + extended `terminationGracePeriodSeconds`.
- **Long-running pinned workflows that never checkpoint** — Temporal
  calls this out as defeating deployment goals.
- **Live migration of stateful sessions across versions** — *nobody does
  this*. OpenAI is *deleting* the stateful Assistants API rather than
  maintain it.

### Synthesized prescription for our shape

Given long-running jobs (minutes–hours) + interactive sessions
(hours–days) + a 5s heartbeat + persistent-thread state already in
Postgres + workspace files already on remote SSH, the research suggests
a **Temporal Pinned + Anthropic Hybrid** combination:

1. **Tag every agent pod with `deployment_version`** (the build SHA, on
   pod registration). Persist it on each job and persistent thread row
   when work is dispatched.
2. **Long-running jobs: pin to original version, drain to completion.**
   On rollout, orchestrator stops dispatching *new* work to old-version
   agents. In-flight jobs finish on their pinned version. Borrow the
   Continue-as-New idea: at the agent's existing phase boundary, if
   `current_version != agent_version`, freeze with a new
   `version_upgrade` freeze type and re-dispatch to a fresh agent on
   the new version. Converts a multi-hour pinned job into bounded
   re-dispatches.
3. **Interactive sessions: explicit user-facing migration.** Don't try
   live migration. Surface "a new agent version is available — finish
   your turn and we'll resume on the new version"; on next prompt, spin
   up a fresh agent on the new version, hydrate from the persistent-
   thread Postgres rows + workspace files. The substrate already exists.
4. **Idle reclamation: heartbeat timeout (have it) + `version_deprecated`
   intent field.** Old-version agent with no work and no session
   self-exits on the next heartbeat tick when intent says deprecated.
   Eager refresh for the easy case, lazy refresh for the hard one.
5. **Version-skew defense at the heartbeat protocol.** New orchestrator
   rejects too-old agent heartbeats with a "please drain and exit"
   response code. Standard capability negotiation.

This is what the original "Proposed direction" below was groping toward;
the research clarifies the vocabulary (Pinned vs Auto-Upgrade,
Continue-as-New, Hybrid Sessions) and shows that the stated bug fixes
are actually the correct *first step* but not the full answer.

## Proposed direction (initial sketch — predates research above)

Open to discussion before implementing. Some pieces below are still
correct (heartbeat respecting `draining`, widening eligibility); some
need to be re-examined against the research synthesis above
(particularly the "make `draining` actionable" actuation channel — the
research suggests SIGTERM + finalizer is the right pattern, not
heartbeat-response action codes).

### Make `draining` actionable, not advisory

Three concrete changes:

1. **Heartbeat handler respects `draining`.** If the row is currently
   `draining`, don't overwrite. Treat the agent's reported status as
   advisory until the pod is gone. (`postgres.py:1737` — add `WHERE
   status != 'draining'` or merge logic.)
2. **Agent reads its own state.** Either (a) the heartbeat *response*
   carries an `action` field and the agent reacts (`drain` →
   self-detach + exit), or (b) the in-pod thread-status watchdog
   already added in `4c64a30` is generalized to also check the agent's
   own row and exit on `status='draining'`. (a) is cleaner — it folds
   into existing 5s heartbeat traffic.
3. **`reap_pods` reaps `draining` pods that have idled.** Once the agent
   has self-detached, its status returns `ready` (or it exits on its
   own); if neither happens within a grace window, force-delete the pod
   via `delete_agent_pod`.

### Widen drain eligibility

Drop the `thread_id IS NULL AND current_job_id IS NULL` filter from
`_drain_stale_image_agents`. Stale-image agents that *are* on a session
or job get a different treatment:

- Working on a job: don't drain mid-job. Set a `drain_pending=true` hint;
  drain on next `working → ready` transition.
- Holding a session: same idea — `drain_pending` until thread ends, then
  drain. The agent watchdogs in `4c64a30` already exit cleanly on thread
  end, so for new-image agents this is already automatic; the flag is
  only needed for legacy agents that lack the watchdog.

### Centralize into one reconciler

Collapse `stale_agent_detector` and `agent_pool_reconciler` into a single
`agent_lifecycle_reconciler` with explicit phases:

```
1. heartbeat freshness  → mark_stale_agents_offline
2. consistency repair   → mark_stuck_working_ready, mark_stuck_session_ready
3. propagate to threads → mark_orphaned_threads_ended
4. propagate to jobs    → recover_orphaned_jobs
5. version freshness    → _drain_stale_image_agents (widened)
6. pod GC               → reap_pods (incl. draining)
7. retention GC         → gc_offline_agents
8. capacity             → ensure_warm_pool, scale_down_idle
```

Order matters: status repairs (2) must run before drain (5), and drain
before pod GC (6), so a session agent on a stale image follows
`session → ready (after thread ends) → draining → pod deleted` in one
60s tick rather than three.

### Centralize the SHA check too

`_find_idle_persistent_agent` and the worker dispatcher currently
duplicate `_agent_sha_is_current(meta) → UPDATE … draining` inline.
Move to a single helper that the dispatchers call as a side-effect-free
predicate, and let the reconciler own the `draining` write. Avoids race
conditions where two paths both try to drain the same agent.

## Open questions raised by the research

Decisions to make before writing code. Each has a defensible answer in
the research; listing them here so the discussion is concrete.

1. **Actuation channel for drain: SIGTERM or heartbeat-response?** The
   K8s research strongly favors SIGTERM + `terminationGracePeriodSeconds`
   + an in-pod signal handler (Knative queue-proxy pattern). Our initial
   sketch used a heartbeat-response action field (simpler, no extra
   surface). Tradeoff: SIGTERM is idiomatic and composes with kubelet
   eviction, but requires the agent to install a signal handler and
   refactor the existing `_detach_session` / shutdown paths. Heartbeat-
   response is faster to ship and reuses the existing 5s loop but adds
   a parallel control plane the K8s research warns against.

2. **Spec/status split: where does intent live?** Currently `agents.status`
   is a single column overwritten by every heartbeat. The research is
   uniform: orchestrator-set *intent* and agent-reported *observed* must
   be separate fields with separate writers. Concrete options:
   (a) add `agents.desired_status` and `agents.observed_status`,
   (b) keep `agents.status` as observed and add `agents.intents` JSONB
   (`{should_drain: true, target_image_sha: "..."}`),
   (c) move to a real K8s CRD with the proper status subresource. (b) is
   the lightest lift; (c) is the most idiomatic but a much bigger change.

3. **Pinning policy for in-flight work.** Temporal's Pinned vs
   Auto-Upgrade decision applies to us:
   - Jobs (minutes–hours): drain to completion at phase boundary, then
     re-dispatch to fresh version (Continue-as-New analog).
   - Interactive sessions (hours–days): explicit user-facing migration,
     re-hydrate from persistent-thread state.
   - Warm-pool agents (idle): eager replacement.

   Are these the right cuts, or do we want a single uniform policy?

4. **Reconciler unification scope.** We have 9 background loops; agent
   lifecycle spans 2 of them. Options:
   (a) collapse only the agent-lifecycle pair into one ordered loop
       (Bug 1+2 fix, minimum scope),
   (b) collapse all sweepers into a generic "reconciler scheduler" with
       phase ordering (much bigger change, addresses the systemic smell),
   (c) leave the loops separate but add explicit dependencies / ordering
       between them.

5. **Owner references / finalizers.** Independent of (4), we can add a
   pod-level finalizer for guaranteed cleanup hooks without migrating to
   a CRD. Worth doing now or defer until the CRD discussion?

6. **Bootstrap problem.** Whatever we ship only takes effect on agents
   spawned with the new image. The 5 zombies on the dev cluster have to
   be deleted by hand once. How do we surface the bootstrap requirement
   in the rollout playbook so this doesn't bite us again?

7. **Workspace lifecycle convergence.** Workspaces have idle suspension
   we don't, agents have crash detection workspaces don't, both lack
   image-drift handling. Should the unified design cover both, or just
   agents now and workspaces later? Subagent 2's report makes a case
   that doing both together costs less than doing them sequentially,
   since the same primitives apply.

8. **Should we expose pod metrics?** No Prometheus counters exist for
   `reaped_*`, `drained_*`, `evicted_*` events. The research notes
   thundering-herd as a real risk; without metrics we won't see one
   coming. Add as part of the fix or defer?

## Immediate cleanup (separate from the fix)

The 5 zombie pods on dev have to be deleted by hand — none of them have
the `4c64a30` watchdog code, and even after the fixes above ship, the
new logic only takes effect on the live orchestrator's *next* tick
against agents that respect the heartbeat-response signal (which they
won't until they're respawned with a new image). Bootstrap problem.

Plan: `kubectl delete pod` the 4 `session` pods (skip
`srw-agent-j-9ea244d6` — it has a real job assigned), let the
provisioner respawn fresh ones on the current image. Confirm with the
user before deleting.

## References

- Reconcilers: `orchestrator/main.py:381`, `orchestrator/main.py:450`
- Drain: `orchestrator/main.py:479`, `1351`, `2060`
- SHA helpers: `orchestrator/main.py:1325`, `1337`
- Heartbeat: `orchestrator/database/postgres.py:1673`
- Stuck-agent repair: `orchestrator/database/postgres.py:2229`, `2259`
- Pod GC: `orchestrator/services/agent_provisioner.py:517`
- In-pod watchdogs: `src/api/persistent_app.py` (commit `4c64a30`)
- Schema: `agents.status` constraint in `orchestrator/database/schema.sql:688`
