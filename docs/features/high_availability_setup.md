---
tags:
  - infrastructure
  - reliability
  - operations
  - architecture
  - runbook
aliases:
  - HA
  - high availability
  - failure tolerance
  - node failure recovery
related:
  - "[[orchestrator_ha_scaling]]"
  - "[[db_migration]]"
  - "[[stuck_agent_recovery]]"
---

# High Availability & Failure Tolerance

> The cluster control plane is HA. The application data tier is not. This doc is the umbrella reference for both: what survives node loss today, what doesn't, the runbook for handling node failures (planned and unplanned), and the staged roadmap for closing the remaining gaps. The orchestrator-specific HA work — multi-replica coordination, leader election, NATS queue groups — lives in `orchestrator_ha_scaling.md` and is referenced rather than duplicated here.

**Status:** Living reference. Runbook is usable now; roadmap is design-stage.
**Filed:** 2026-05-20
**Triggered by:** 2026-05-16 incident — pulling `node3` for maintenance took the whole SRW stack offline despite a healthy 3-node etcd quorum and 2-replica Longhorn data redundancy.

## What "HA" means in this project

Two failure classes worth distinguishing, because they need different solutions:

- **Planned maintenance** — operator pulls a node intentionally (kernel upgrade, hardware swap, BIOS update). Workloads should drain off the node *before* it goes away.
- **Unplanned failure** — node dies without warning (PSU, kernel panic, network partition). Workloads should recover on remaining nodes within minutes.

What we explicitly do **not** try to survive in the current scope:

- Loss of 2-of-3 etcd voters — cluster goes read-only, that's the design.
- Loss of *all* Longhorn replicas for a volume — single-disk failures are scoped out; for multi-replica data, see roadmap below.
- Datacenter / site loss — single-site homelab, no geo-replication.
- The `localhost` k3d cluster used for dev — that's a single-node dev environment, HA is irrelevant.

## Current HA inventory

What survives a single-node loss *today*, assuming the failure isn't on the node the component is pinned to:

| Layer | Component | Replicas | Survives unplanned node loss? | Notes |
|-------|-----------|----------|-------------------------------|-------|
| Cluster | k3s control plane | 3 (etcd voters: node1/2/3) | ✓ | Quorum survives 1-of-3 loss; node4 is worker-only. |
| Cluster | API server | per control-plane node | ✓ | Reachable as long as quorum holds. |
| Storage | Longhorn data replicas | 2 (cluster default) | ✓ (data survives) | Data is safe; volume **attachment** is the bottleneck — see "Today's gap". |
| App (stateless) | Orchestrator | `replicas: 1` | ✗ | See [[orchestrator_ha_scaling]]. |
| App (stateless) | Cockpit | `replicas: 1` (configurable) | ✗ | Easy to scale; not the SPOF that matters. |
| App (stateless) | MCP server | `replicas: 1` (configurable) | ✗ | Same. |
| App (stateful) | PostgreSQL | StatefulSet `replicas: 1` | ✗ | Primary data store. **Highest-impact SPOF.** |
| App (stateful) | pgvector | StatefulSet `replicas: 1` | ✗ | Embeddings + memories store. |
| App (stateful) | postgres-keycloak | StatefulSet `replicas: 1` | ✗ | Auth DB. Down ⇒ no login. |
| App (stateful) | MongoDB | StatefulSet `replicas: 1` | ✗ (non-fatal) | Audit trail only; agent + orchestrator degrade gracefully. |
| App (stateful) | Neo4j | StatefulSet `replicas: 1` | ✗ (non-fatal) | Knowledge graph; optional per CLAUDE.md. |
| App (stateful) | Gitea | StatefulSet `replicas: 1` | ✗ | Workspace git server. Fast to recover; not catastrophic. |
| App (stateful) | NATS | StatefulSet `replicas: 1` | ✗ | Coordination plane SPOF. Worth fixing early — see roadmap. |
| App | Keycloak (server) | hardcoded `replicas: 1` | ✗ | Stateless server but DB-backed. Down ⇒ no login until restart. |
| App | OpenCloud | hardcoded `replicas: 1` | ✗ | Single-writer by design (file locking). Accept SPOF. |
| App | vm-controller | hardcoded `replicas: 1` | ✗ | Singleton by design (NATS consumer). |
| Agent fleet | Worker agents | scaled by orchestrator | ✓ | Heartbeat + auto re-dispatch handles loss. |
| Workspaces | `workspace-*` / `ws-thread-*` pods | per-job, scheduler-placed | ✓ | Lost workspace ⇒ orchestrator re-provisions or restores from snapshot. |

The honest summary: **the control plane and the storage *bytes* are HA, but every persistent application service is single-replica.** Node loss survives data-wise but not service-wise.

## Today's gap: why a single-node loss takes everything down

Confirmed by the 2026-05-16 incident on `node3`. The chain:

1. Node goes away (kubelet stops posting at T+0).
2. K8s waits out the taint-eviction timer (`node.kubernetes.io/unreachable:NoExecute` with `tolerationSeconds: 300`).
3. At T+5min, pods on the dead node get `deletionTimestamp` set — they enter `Terminating`.
4. **K8s refuses to fully delete a pod while it has a `VolumeAttachment` to an unreachable node.** This is a safety feature: if the node comes back, deleting the pod could allow a second writer to attach the same RWO volume, corrupting data.
5. Stuck `Terminating` pods hold their PVC attachments. New StatefulSet replicas can't start (RWO can only attach once).
6. Orchestrator's `wait-for-postgres` / `wait-for-mongodb` / `wait-for-keycloak` init containers block forever.
7. Cockpit waits for orchestrator. Stack offline.

The K8s API server, etcd, and the rest of the cluster are all fine throughout. The control plane delivered on its HA promise. The application architecture is what failed.

## Node-failure runbook

### Planned maintenance — always drain first

This is the procedure that would have prevented the 2026-05-16 incident entirely. Cost: zero. Time: 2-5 minutes.

```bash
# 1. Cordon: mark node unschedulable so nothing new lands there.
kubectl --context=main cordon node3

# 2. Verify Longhorn has the data elsewhere BEFORE proceeding.
#    Every SRW PVC should show ≥1 replica on a node other than the one
#    you're about to drain.
for pvc in $(kubectl --context=main get pvc -n superhuman-remote-worker \
    -o jsonpath='{.items[*].spec.volumeName}'); do
  echo "=== $pvc ==="
  kubectl --context=main get replicas.longhorn.io -n longhorn-system \
    -l longhornvolume="$pvc" \
    -o custom-columns=NAME:.metadata.name,NODE:.spec.nodeID,RUNNING:.status.currentState
done

# 3. Drain. --ignore-daemonsets is required (Longhorn manager, kube-proxy,
#    flannel are all DaemonSets). --delete-emptydir-data is required if any
#    pod uses emptyDir.
kubectl --context=main drain node3 \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --timeout=10m

# 4. Wait for drain to finish. The StatefulSet pods will move to other
#    nodes; their RWO volumes get cleanly detached and re-attached. This
#    is the step that's broken on ungraceful failure — here, K8s does the
#    detach work because the node is reachable.

# 5. Do the maintenance. Pull power, reboot, swap drives, whatever.

# 6. Bring the node back. Wait for it to register as Ready.
kubectl --context=main get nodes -w

# 7. Uncordon.
kubectl --context=main uncordon node3
```

If you want this scripted, drop a `scripts/drain-node.sh` that wraps these steps and refuses to proceed if any PVC has < 2 replicas elsewhere.

### Unplanned failure — out-of-service taint

When the node is already gone and pods are stuck `Terminating`, the documented K8s recovery path is to taint the dead node as out-of-service. This tells the control plane "this node is not coming back; it's safe to break the volume locks."

**Pre-flight: verify data is on other nodes.** This is non-negotiable — if the dead node was the only host of a Longhorn replica for some volume, you have a data problem, not a scheduling problem.

```bash
# 1. Confirm the failing node is the one you think it is.
kubectl --context=main get nodes
kubectl --context=main describe node node3 | grep -E "(Status|Taints)"

# 2. List Longhorn replicas to confirm data exists on surviving nodes.
#    Look for: each SRW PVC has ≥1 replica with state=running on a
#    node OTHER than the failing one.
kubectl --context=main get replicas.longhorn.io -n longhorn-system \
  -l longhornvolume \
  -o custom-columns=PVC:.metadata.labels.longhornvolume,NODE:.spec.nodeID,STATE:.status.currentState \
  | sort

# 3. List stuck VolumeAttachments to confirm the diagnosis.
kubectl --context=main get volumeattachment | grep <dead-node>

# 4. Apply the out-of-service taint. K8s will:
#    - Force-delete pods that were on the node (skip the volume unmount step)
#    - Mark VolumeAttachments to the node as released
#    - StatefulSet controller creates new pods on healthy nodes
#    - Longhorn engine re-attaches using existing replicas
kubectl --context=main taint nodes node3 \
  node.kubernetes.io/out-of-service=nodeshutdown:NoExecute

# 5. Watch the recovery. New pods should appear within 30-60s.
kubectl --context=main get pods -n superhuman-remote-worker -w

# 6. When the node returns, remove the taint BEFORE the node rejoins
#    workloads, otherwise nothing will schedule there.
kubectl --context=main taint nodes node3 \
  node.kubernetes.io/out-of-service=nodeshutdown:NoExecute-
```

### Verification commands (useful during any incident)

```bash
# What's running where
kubectl --context=main get pods -n superhuman-remote-worker -o wide

# Stuck Terminating pods (and how long they've been stuck)
kubectl --context=main get pods -n superhuman-remote-worker \
  --field-selector=status.phase!=Running,status.phase!=Succeeded

# Stale VolumeAttachments on a specific node
kubectl --context=main get volumeattachment | grep <node>

# Longhorn volume state for a specific PVC
kubectl --context=main get volume.longhorn.io -n longhorn-system <pvc-volume-name> \
  -o jsonpath='{.status.state}{" "}{.status.robustness}{" attached-to="}{.status.currentNodeID}{"\n"}'

# What init containers are blocking a pod
kubectl --context=main get pod <pod> -n superhuman-remote-worker \
  -o jsonpath='{range .status.initContainerStatuses[*]}{.name}{": "}{.state}{"\n"}{end}'
```

## Roadmap: closing the data-tier gap

The data tier is where the real SPOFs live. Each item below is its own discrete piece of work; do them in priority order, not as a single big-bang migration. The general principle is: **don't pay for HA on services where the cost (resources + operator complexity) exceeds the cost of brief downtime**, given this is a dev cluster.

### Priority 1: PostgreSQL → CloudNativePG

Both `postgres` and `postgres-vector` (pgvector) and `postgres-keycloak` would migrate to [CloudNativePG](https://cloudnative-pg.io/). Standard answer in the K8s ecosystem; gives synchronous replication, automatic failover, point-in-time recovery, and operator-managed backups.

- Three separate `Cluster` resources, one per logical DB.
- Multi-replica with at least one synchronous standby.
- Existing chart's `databases.postgres.externalUrl` etc. already supports pointing at an external (or operator-managed) DB — no chart changes needed beyond setting those values.
- Migration path: dump from current StatefulSet → restore into new `Cluster` → flip `externalUrl` → tear down old StatefulSet. See `docs/db_migration.md` for the migration tooling.

### Priority 2: NATS → multi-replica

Cheap, low-risk, high-value. NATS clusters horizontally out of the box; the chart already runs a StatefulSet, just bump replicas and add the cluster routes config. Coordination plane stops being a SPOF, which matters more once the orchestrator goes multi-replica (per `orchestrator_ha_scaling.md` Phase 4).

### Priority 3 (optional): MongoDB → ReplicaSet

Audit data is non-fatal — CLAUDE.md explicitly notes "MongoDB/Neo4j failures are non-fatal." Brief downtime here is acceptable. Only do this if/when the audit trail starts being load-bearing for compliance.

### Out of scope (accept SPOF + document recovery)

- **Neo4j community edition** — no clustering without enterprise license. Stays single-replica. Recovery is restore-from-volume.
- **Gitea** — clustering is awkward and rarely worth it. Single-replica with fast recovery is fine.
- **OpenCloud** — single-writer by design (file locking). Don't fight the product.
- **Keycloak server** (the pod, not the DB) — stateless, but the chart hardcodes `replicas: 1`. Trivial to expose as a chart value if needed; not currently a real problem.
- **vm-controller** — singleton by design (NATS consumer); leader election would be the fix, similar pattern to `orchestrator_ha_scaling.md` Phase 1.

## Application tier

Multi-replica orchestrator (and the coordination work that requires) is fully specified in [[orchestrator_ha_scaling]]. The data-tier work in *this* doc is a **prerequisite** for that doc's Phase 2+ — running multiple orchestrator replicas against a single-replica PostgreSQL is fine for active-passive failover (Track 1) but starts to bottleneck in active-active (Track 2).

Suggested execution order across both docs:
1. Drain discipline (this doc) — operational, zero cost.
2. `orchestrator_ha_scaling.md` Track 1 (active-passive) — orchestrator survives its own crashes.
3. NATS multi-replica (this doc, Priority 2) — coordination plane stops being SPOF.
4. PostgreSQL → CloudNativePG (this doc, Priority 1) — data tier HA.
5. `orchestrator_ha_scaling.md` Track 2 (active-active, leader election, etc.) — horizontal scale.

## Decision log

- **2026-05-20:** Filed after the 2026-05-16 node-pull incident. Choice: document operational discipline (drain) as the *first* HA investment, ahead of any infrastructure work. Rationale: planned maintenance is far more frequent than unplanned hardware failure in this homelab, and `kubectl drain` costs nothing.
- **2026-05-20:** Decided to keep this doc as the umbrella and reference `orchestrator_ha_scaling.md` rather than merging them. The orchestrator HA work is a *consumer* of data-tier HA, not the same problem.
- **2026-05-20:** Postgres-tier migration prioritized over MongoDB. Rationale: Mongo loss is non-fatal per existing graceful-degradation contract; Postgres loss takes down jobs, auth, and the cockpit.

## Related code

- `helm/templates/databases/postgres.yaml` — current single-replica StatefulSet
- `helm/templates/databases/postgres-vector.yaml` — same
- `helm/templates/databases/postgres-keycloak.yaml` — same
- `helm/templates/databases/mongodb.yaml` — same
- `helm/templates/nats/statefulset.yaml` — single replica today
- `helm/values.yaml` — default `replicas: 1` for orchestrator/cockpit/mcp
- `docs/features/orchestrator_ha_scaling.md` — application-tier HA design
- `docs/db_migration.md` — migration tooling (relevant for the CloudNativePG cutover)

## Sources

- [Kubernetes: Non-Graceful Node Shutdown Handling](https://kubernetes.io/docs/concepts/architecture/nodes/#non-graceful-node-shutdown) — the `node.kubernetes.io/out-of-service` taint.
- [Longhorn: Node Failure](https://longhorn.io/docs/latest/high-availability/node-failure/) — replica behavior when a node dies.
- [CloudNativePG](https://cloudnative-pg.io/) — Postgres operator referenced in the roadmap.
