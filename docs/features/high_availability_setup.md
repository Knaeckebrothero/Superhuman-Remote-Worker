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
**Refreshed:** 2026-06-24 — inventory re-verified against the Helm chart (the live path via Fleet); added two newer single-replica Postgres SPOFs (`postgres-audit`, `postgres-litellm`); confirmed no roadmap item has started.
**Refreshed:** 2026-07-30 — inventory re-verified after the orchestrator HA landing (M0–M2, `replicas: 2` chart default) and the NATS move to an external 3-node hub. MongoDB and `postgres-litellm` rows dropped (both removed from the chart). Added the prioritized [scalability & HA checklist](#scalability--ha-checklist-2026-07-30) consolidating open work across both HA docs plus new findings (Keycloak BFF refresh bug, `start-dev` blocker, Gitea load fixes).
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
| App (stateless) | Orchestrator | `replicas: 2` (chart default) | ✓ | **HA since 2026-06** — M0–M2 shipped (leader election, PDB, preStop drain); live-verified. See [[orchestrator_ha_scaling]]. |
| App (stateless) | Cockpit | `replicas: 1` (configurable) | ✗ | Easy to scale; not the SPOF that matters. |
| App (stateless) | MCP server | `replicas: 1` (configurable) | ✗ | Same. |
| App (stateful) | PostgreSQL | StatefulSet `replicas: 1` | ✗ | Primary data store. **Highest-impact SPOF.** |
| App (stateful) | pgvector | StatefulSet `replicas: 1` | ✗ | Embeddings + memories store. |
| App (stateful) | postgres-keycloak | StatefulSet `replicas: 1` | ✗ | Auth DB. Down ⇒ no login. |
| App (stateful) | postgres-audit | StatefulSet `replicas: 1` | ✗ | Audit store (`AUDIT_BACKEND`). Non-fatal but a SPOF. |
| App (stateful) | Neo4j | StatefulSet `replicas: 1` | ✗ (non-fatal) | Knowledge graph; optional per CLAUDE.md. |
| App (stateful) | Gitea | StatefulSet `replicas: 1` | ✗ | Workspace git server on **SQLite** + local PVC. Fast to recover; load fixes in the checklist below. |
| App | NATS | external 3-node hub (`nats.internal: false` default) | ✓ | **Coordination plane HA'd 2026-06** via BYO hub. In-chart NATS (internal mode) remains single-replica. |
| App | Keycloak (server) | hardcoded `replicas: 1` | ✗ | DB-backed and HA-capable upstream (KC 26), **but the chart runs `start-dev`** (local-only caches) — prod-mode switch required before >1 replica. See checklist P2. |
| App | OpenCloud | hardcoded `replicas: 1` | ✗ | Single-writer by design (file locking). Accept SPOF. |
| App | vm-controller | hardcoded `replicas: 1` | ✗ | Singleton by design (NATS consumer). |
| Agent fleet | Worker agents | scaled by orchestrator | ✓ | Heartbeat + auto re-dispatch handles loss. |
| Workspaces | `workspace-*` / `ws-thread-*` pods | per-job, scheduler-placed | ✓ | Lost workspace ⇒ orchestrator re-provisions or restores from snapshot. |

The honest summary as of 2026-07-30: **the control plane, the storage *bytes*, the coordination plane (external NATS hub), and the orchestrator are HA — but every persistent data service is still single-replica.** Node loss survives data-wise but not service-wise; the gap has narrowed to the data tier (plus the small stateless singletons).

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

> **Status (2026-07-30): not started.** No `cnpg` / CloudNativePG resources exist in the SRW deployment trees — all Postgres instances are plain single-replica StatefulSets. Scope shrank back to **four** instances: `postgres-litellm` was removed along with the LiteLLM gateway, and MongoDB left the chart (audit now lives on `postgres-audit`). This is the **M3** item in [[orchestrator_ha_scaling]] — the acknowledged remaining SPOF now that the orchestrator itself is HA. The BYO alternative (managed/HA Postgres via `databases.*.externalHost`, zero chart work) is equally valid, and likely the right answer for SaaS.

All four Postgres instances (`postgres`, `postgres-vector`/pgvector, `postgres-keycloak`, `postgres-audit`) would migrate to [CloudNativePG](https://cloudnative-pg.io/). Standard answer in the K8s ecosystem; gives synchronous replication, automatic failover, point-in-time recovery, and operator-managed backups.

**Pooler constraint (either path):** orchestrator leader election holds a *session-scoped* Postgres advisory lock (`orchestrator/services/leader_election.py`). A transaction-mode pooler (PgBouncer/pgcat txn mode) in front of the orchestrator silently breaks it — orchestrator connections must be direct or session-pooled.

- Four separate `Cluster` resources, one per logical DB.
- Multi-replica with at least one synchronous standby.
- Existing chart's `databases.postgres.externalUrl` etc. already supports pointing at an external (or operator-managed) DB — no chart changes needed beyond setting those values.
- Migration path: dump from current StatefulSet → restore into new `Cluster` → flip `externalUrl` → tear down old StatefulSet. See `docs/db_migration.md` for the migration tooling.

### Priority 2: NATS → multi-replica

> **Status (2026-07-30): RESOLVED — differently than planned.** The coordination plane moved to an **external 3-node NATS hub** (`nats.internal: false` is the chart default; the cluster connects as a leaf/client). In-chart NATS still exists for internal mode and remains single-replica, but it's no longer the deployed path. In-chart clustering stays deferred/BYO.

### Priority 3 (optional): MongoDB → ReplicaSet

> **Status (2026-07-30): OBSOLETE.** MongoDB was removed from the chart; the audit trail moved to the Postgres audit store (`postgres-audit`), which is covered by Priority 1.

### Out of scope (accept SPOF + document recovery)

- **Neo4j community edition** — no clustering without enterprise license. Stays single-replica. Recovery is restore-from-volume.
- **Gitea** — clustering is awkward (shared FS + Redis + external DB, upstream support experimental) and rarely worth it. Single-replica with fast recovery is fine — but see checklist P1: the *measured* Gitea problems are self-inflicted load and SQLite, not topology.
- **OpenCloud** — single-writer by design (file locking). Don't fight the product.
- **Keycloak server** (the pod, not the DB) — HA-capable upstream and *nearly* free on KC 26 (persistent user sessions in the DB since 26.0, jdbc-ping discovery via the shared DB since 26.1 — no new infra). Not "trivial" in our chart though: it runs `start-dev`, which is single-node-only. Promoted from "out of scope" to checklist P2.
- **vm-controller** — singleton by design (NATS consumer); leader election would be the fix, similar pattern to `orchestrator_ha_scaling.md` Phase 1.

## Application tier

Multi-replica orchestrator (and the coordination work that requires) is fully specified in [[orchestrator_ha_scaling]] — **shipped and live-verified as of 2026-06-29** (M0 active-passive hardening, M1 leader election, M2-L4 NATS replica-safety, background-loop sweep). The original execution order is done through step 3 (drain discipline → Track 1 → NATS); what remains is the data tier (Priority 1 above) and the optional active-active polish (L2/L3), both captured in the checklist below.

## Scalability & HA checklist (2026-07-30)

Consolidated, prioritized work list from the 2026-07-30 scalability review. Ordering principle: cheap code fixes that shrink outage blast radius first, measured load problems second, topology last. Items are checked off here (with a date) rather than deleted, so this section doubles as the progress record.

### P0 — Keycloak outage must not destroy login sessions (app code, ~hours)

- [x] **(2026-07-30)** Stop deleting BFF sessions when Keycloak is merely *unreachable*. `_refresh_session_in_place` (`orchestrator/security/auth.py`) used to delete the session row on **any** `KeycloakClientError`, but `kc_client.py` raised that same error for network failures and definitive rejections alike — so a Keycloak outage longer than one access-token lifespan (15 min) permanently destroyed every active session. **Fixed:** `KeycloakClientError` now carries `status_code` + `oauth_error`; only a definitive rejection (400 `invalid_grant` — refresh token expired/revoked/SSO ended) deletes the row. Unreachable, 5xx, malformed bodies, and client-misconfig (`invalid_client`) keep the row and fail the request with a retryable 503 (WS handshakes map it to a 4401 close, row kept). Verified on k3d end-to-end: refresh 200 with KC up → scaled KC to 0 → refresh 503 + `/api/auth/me` still 200 + all session rows intact → KC back → same session refreshes 200 without re-login. Unit coverage in `tests/test_bff_session_auth.py`. Residual (accepted): a JWKS cold cache (orchestrator restarted mid-outage) makes cookie validation raise `PyJWKClientError` → 500s until KC returns, but never deletes sessions; PyJWT's lru-cached signing keys cover the non-restart case.

Keycloak is *not* in the per-request hot path (BFF validates JWTs locally via cached JWKS, `orchestrator/security/oidc.py`; KC only sees login/logout/refresh), so this one fix buys most of the practical resilience before any replica work.

### P1 — Gitea: cut self-inflicted load before touching topology (measured 2026-07-29)

Gitea is **not** resource-starved (98m CPU against a 2000m limit); the observed slowness is our own call patterns. Keep it single-replica (see "Out of scope") and fix the load:

- [x] **(2026-07-30) kb_reindex N+1**: was ~11,450 sequential `GET /contents/knowledge/**` calls per day, one REST call per note. **Fixed:** `_GiteaSnapshot` (`orchestrator/services/kb_git_source.py`) gained a `prefetch()` that downloads the snapshot once as `archive/<ref>.tar.gz` (new `GiteaClient.download_repo_archive`, streamed to a temp file, cleaned on snapshot exit) and serves `get_file` from the tarball; the reindexer calls it when the planned batch is ≥ `KB_REINDEX_ARCHIVE_THRESHOLD` (default 25) changed notes, so small incremental sweeps keep the cheap per-file path. Failure falls back to per-file REST, never raises. Verified in-pod against live Gitea: archive reads byte-identical to REST reads, temp file cleaned. *(Residual: the same N+1 shape in `main.py::_copy_tree` — Mode-B cloud export — is untouched.)*
- [x] **(2026-07-30) compare/`stat=true` cost**: `get_compare` now passes `stat/verification/files=false` — but measurement showed Gitea 1.22 **ignores `stat`** on the compare endpoint (honored from 1.23) *and* 404s on SHA bases (`BaseNotExist`), which meant the `since_ref` path of `/api/jobs/{id}/repo/commits` was both slow-by-design and broken for SHA refs. **Fixed properly:** that path now uses the new `GiteaClient.get_commits_between` — commits-endpoint pagination (which honors `stat=false`, ~155×) with a client-side cut at `since_ref`. Verified live: 200/40ms with a SHA base that previously 404'd.
- [x] **(2026-07-30) `cron.git_gc_repos` enabled** — `@weekly`, via `GITEA__cron_0X2E_git_gc_repos__*` env in the chart. Verified registered in `/api/v1/admin/cron` on k3d.
- [x] **(2026-07-30) SQLite WAL** — `GITEA__database__SQLITE_JOURNAL_MODE=WAL` in the chart; `-wal`/`-shm` files confirmed live on k3d. The Postgres migration remains the right pre-SaaS move (tracked below) but WAL removes the worst single-writer/corruption risk now.
- [ ] **SQLite → Postgres migration** — deferred; a real data-migration operation (dump/convert/restore per instance), do it deliberately pre-SaaS rather than as a drive-by.
- [ ] **Cache adapter** is `memory` (default) — nothing survives a restart. Low priority; a persistent adapter means Redis (new infra), accept restart-loss for now.

SaaS-scale note: the first hard wall is **inodes, not CPU** — ~277 inodes/repo means 100k repos ≈ 28M inodes, which exhausts default ext4 before disk fills. That's a filesystem/provisioning decision for later, recorded here so it isn't rediscovered.

### P2 — Keycloak production mode + `replicas: 2` (chart, small)

Prerequisites are already in place: external Postgres (`keycloakdb`), prod-shaped hostname/proxy env (`KC_HOSTNAME`, `KC_PROXY_HEADERS=xforwarded`). KC 26 makes multi-replica cheap: user sessions persist to the DB by default (26.0+) and cluster discovery defaults to jdbc-ping through that same DB (26.1+) — **no new infra**.

- [ ] Switch `start-dev` → `start` (`helm/templates/services/keycloak.yaml:616`) and re-verify realm behavior (dev mode currently also relaxes theme/hostname checks).
- [ ] Verify the realm-import and postStart kcadm session-lifespan hooks are idempotent when two pods run them concurrently.
- [ ] Expose `keycloak.replicas` as a chart value, default 2.

Caveat: the availability win is partly gated on P3 — a 2-replica Keycloak on a 1-replica `keycloakdb` just moves the SPOF down one layer. Do P0 first; do this when convenient.

### P3 — Data tier HA (the remaining real SPOF)

- [ ] Decide the path: **BYO managed/HA Postgres** via existing `databases.*.externalHost` values (zero chart work; right answer for SaaS) vs **in-chart CloudNativePG** (Priority 1 above; right answer for self-contained homelab/on-prem).
- [ ] Whichever path: respect the pooler constraint (session-scoped advisory lock for leader election — no transaction-mode pooling on orchestrator connections).
- [ ] Postgres connection budget: per-agent checkpointer connections + 2 orchestrator replicas share `max_connections` (arithmetic documented in `helm/values.yaml`, orchestrator section). This is the first hard Postgres *throughput* limit as the agent fleet grows; a session-mode pooler for agent/checkpointer traffic is the likely fix.

### P4 — Deferred until volume demands (tracked, not scheduled)

- [ ] **L2 dispatch throughput**: job dispatch is a leader-gated singleton loop; the `SELECT … FOR UPDATE SKIP LOCKED` multi-replica design exists in [[orchestrator_ha_scaling]]. Only needed at job volumes far above current.
- [ ] **L3 cross-replica SSE/notification fan-out**: session streams are already replica-safe (`thread_events` in Postgres is the source of truth); the gap is the low-volume notification/sudo channels, which are replica-local today.
- [ ] **Cockpit / MCP `replicas: 2`**: stateless, cheap; only matters for node-loss tolerance.
- [ ] **Agent heartbeat batching**: one REST call + DB write per agent per 5 s — fine into the hundreds of agents; batch or move to NATS beyond that.

## Decision log

- **2026-05-20:** Filed after the 2026-05-16 node-pull incident. Choice: document operational discipline (drain) as the *first* HA investment, ahead of any infrastructure work. Rationale: planned maintenance is far more frequent than unplanned hardware failure in this homelab, and `kubectl drain` costs nothing.
- **2026-05-20:** Decided to keep this doc as the umbrella and reference `orchestrator_ha_scaling.md` rather than merging them. The orchestrator HA work is a *consumer* of data-tier HA, not the same problem.
- **2026-05-20:** Postgres-tier migration prioritized over MongoDB. Rationale: Mongo loss is non-fatal per existing graceful-degradation contract; Postgres loss takes down jobs, auth, and the cockpit.
- **2026-07-30:** Added the scalability & HA checklist after a full-stack review. Key calls: (1) the Keycloak BFF refresh bug (sessions deleted on KC-unreachable) is P0 — a code fix beats any topology work on blast radius per effort; (2) Keycloak multi-replica moved from "out of scope" to P2 — KC 26 made it near-free (DB-persisted sessions + jdbc-ping), the only real blocker is our `start-dev` invocation; (3) Gitea stays single-replica — its measured problems are self-inflicted call patterns (kb_reindex N+1, `stat=true`, no gc) and SQLite, not resources or replica count; (4) data tier remains the top structural SPOF, with BYO managed Postgres acknowledged as the likely SaaS path over CloudNativePG.

## Related code

- `helm/templates/databases/postgres.yaml` — current single-replica StatefulSet
- `helm/templates/databases/postgres-vector.yaml` — same
- `helm/templates/databases/postgres-keycloak.yaml` — same
- `helm/templates/databases/postgres-audit.yaml` — same
- `helm/values.yaml` — `orchestrator.replicas: 2` default; cockpit/mcp still 1; Postgres connection-budget arithmetic
- `helm/templates/services/keycloak.yaml` — `start-dev` invocation + hardcoded `replicas: 1` (checklist P2)
- `helm/templates/services/gitea.yaml` — hardcoded `sqlite3` (checklist P1)
- `orchestrator/security/auth.py` / `orchestrator/security/kc_client.py` — BFF session refresh path (checklist P0)
- `orchestrator/services/leader_election.py` — session-scoped advisory lock (pooler constraint)
- `docs/features/orchestrator_ha_scaling.md` — application-tier HA design (shipped M0–M2; open L2/L3)
- `docs/db_migration.md` — migration tooling (relevant for the CloudNativePG cutover)

## Sources

- [Kubernetes: Non-Graceful Node Shutdown Handling](https://kubernetes.io/docs/concepts/architecture/nodes/#non-graceful-node-shutdown) — the `node.kubernetes.io/out-of-service` taint.
- [Longhorn: Node Failure](https://longhorn.io/docs/latest/high-availability/node-failure/) — replica behavior when a node dies.
- [CloudNativePG](https://cloudnative-pg.io/) — Postgres operator referenced in the roadmap.
