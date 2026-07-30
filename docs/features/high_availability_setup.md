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

> The cluster control plane, the coordination plane, and every stateless application service are now HA. **The data tier is not, and it is the only structural gap left.** This doc is the umbrella reference: what survives node loss today, what doesn't, the runbook for node failures (planned and unplanned), and the remaining work. The orchestrator-specific coordination design — leader election, NATS queue groups — lives in `orchestrator_ha_scaling.md` and is referenced rather than duplicated here.

**Status:** Living reference. Runbook usable. **App tier: done. Data tier: one open decision (P3).**
**Filed:** 2026-05-20
**Triggered by:** 2026-05-16 incident — pulling `node3` for maintenance took the whole SRW stack offline despite a healthy 3-node etcd quorum and 2-replica Longhorn data redundancy.

**Where this stands (2026-07-30).** Four things shipped today, each live-verified on k3d and then on dev, and they closed every app-tier item:

| | Was | Now |
|---|---|---|
| Keycloak outage handling | any KC error deleted every BFF session → a >15 min outage force-re-logged-in all users, permanently | only a definitive `invalid_grant` deletes a session; unreachable/5xx keeps it and returns a retryable 503 |
| Gitea load | kb_reindex made ~11.4k sequential `contents/` calls a day; `since_ref` commits were broken for SHA refs | one archive download per sweep; `get_commits_between` replaces the compare API; gc cron on; SQLite in WAL |
| Gitea metadata DB | SQLite on a PVC, single-writer, corruption-prone | Postgres (`srw-giteadb`), both clusters migrated with a guard that refuses to start un-migrated |
| Keycloak topology | `start-dev` → `cache=local`, so `replicas: 1` was the only safe setting; a pod kill was a stack-wide auth outage | production mode → Infinispan + `jdbc-ping`, `replicas: 2` on both clusters, **zero auth downtime through a pod kill** |

Full detail per item in the [checklist](#scalability--ha-checklist) below. Earlier refresh notes: *2026-06-24* re-verified the inventory against the chart; *2026-07-30* dropped the MongoDB and `postgres-litellm` rows (both removed from the chart) and folded the old three-priority roadmap into the checklist.

## What "HA" means in this project

Two failure classes worth distinguishing, because they need different solutions:

- **Planned maintenance** — operator pulls a node intentionally (kernel upgrade, hardware swap, BIOS update). Workloads should drain off the node *before* it goes away.
- **Unplanned failure** — node dies without warning (PSU, kernel panic, network partition). Workloads should recover on remaining nodes within minutes.

What we explicitly do **not** try to survive in the current scope:

- Loss of 2-of-3 etcd voters — cluster goes read-only, that's the design.
- Loss of *all* Longhorn replicas for a volume — single-disk failures are scoped out; for multi-replica data, see §P3 below.
- Datacenter / site loss — single-site homelab, no geo-replication.
- The `localhost` k3d cluster used for dev — that's a single-node dev environment, HA is irrelevant.

## Current HA inventory

What survives a single-node loss *today*, assuming the failure isn't on the node the component is pinned to. Replica counts below are the **live dev values**, re-read from the cluster on 2026-07-30, not the chart defaults (they differ where dev opts in early):

| Layer | Component | Replicas | Survives unplanned node loss? | Notes |
|-------|-----------|----------|-------------------------------|-------|
| Cluster | k3s control plane | 3 (etcd voters: node1/2/3) | ✓ | Quorum survives 1-of-3 loss; node4 is worker-only. |
| Cluster | API server | per control-plane node | ✓ | Reachable as long as quorum holds. |
| Storage | Longhorn data replicas | 2 (cluster default) | ✓ (data survives) | Data is safe; volume **attachment** is the bottleneck — see "The failure chain". |
| App (stateless) | Orchestrator | `replicas: 2` (chart default) | ✓ | **HA since 2026-06** — M0–M2 shipped (leader election, PDB, preStop drain); live-verified. See [[orchestrator_ha_scaling]]. |
| App (stateless) | Cockpit | `replicas: 1` (configurable) | ✗ | Easy to scale; not the SPOF that matters. |
| App (stateless) | MCP server | `replicas: 1` (configurable) | ✗ | Same. |
| App (stateful) | PostgreSQL | StatefulSet `replicas: 1` | ✗ | Primary data store. **Highest-impact SPOF.** |
| App (stateful) | pgvector | StatefulSet `replicas: 1` | ✗ | Embeddings + memories store. |
| App (stateful) | postgres-keycloak | StatefulSet `replicas: 1` | ✗ | Auth DB. Down ⇒ no login. |
| App (stateful) | postgres-audit | StatefulSet `replicas: 1` | ✗ | Audit store (`AUDIT_BACKEND`). Non-fatal but a SPOF. |
| App (stateful) | Neo4j | StatefulSet `replicas: 1` | ✗ (non-fatal) | Knowledge graph; optional per CLAUDE.md. |
| App (stateful) | Gitea | StatefulSet `replicas: 1` | ✗ | Workspace git server; single-replica by choice (clustering needs shared FS + Redis, upstream support experimental). Metadata now in Postgres; git objects always on the PVC. Fast restart, and the orchestrator degrades gracefully when it's down. |
| App (stateful) | giteadb | StatefulSet `replicas: 1` | ✗ | **New 2026-07-30.** Gitea's metadata Postgres — both clusters migrated off SQLite. Same single-replica SPOF as the other four; covered by P3. |
| App | NATS | external 3-node hub (`nats.internal: false` default) | ✓ | **Coordination plane HA'd 2026-06** via BYO hub. In-chart NATS (internal mode) remains single-replica. |
| App | Keycloak (server) | **2 on dev** (chart default 1) | ✓ at 2 | **HA since 2026-07-30** — production mode + Infinispan/`jdbc-ping` + DB-persisted sessions. Zero auth downtime through a pod kill. Chart default stays 1 for the fresh-install `--import-realm` race; overlays opt in. Its DB is still single-replica (P3). |
| App | OpenCloud | hardcoded `replicas: 1` | ✗ | Single-writer by design (file locking). Accept SPOF. |
| App | vm-controller | hardcoded `replicas: 1` | ✗ | Singleton by design (NATS consumer). |
| Agent fleet | Worker agents | scaled by orchestrator | ✓ | Heartbeat + auto re-dispatch handles loss. |
| Workspaces | `workspace-*` / `ws-thread-*` pods | per-job, scheduler-placed | ✓ | Lost workspace ⇒ orchestrator re-provisions or restores from snapshot. |

The honest summary as of 2026-07-30: **the control plane, the storage *bytes*, the coordination plane, the orchestrator, and now Keycloak are HA — every remaining ✗ is a database or a deliberate singleton.** Five single-replica Postgres instances (`postgres`, `pgvector`, `keycloakdb`, `auditdb`, `giteadb`) are the whole structural gap; Gitea, OpenCloud, Neo4j and vm-controller are single-replica *by choice*, with recovery rather than redundancy as the answer.

This is worth stating plainly because it changes the shape of the remaining work: **there is no more app-tier HA to build.** Making Keycloak survive a node loss no longer helps if `keycloakdb` doesn't, and the same is now true for the orchestrator and Gitea. Everything routes through P3.

## The failure chain: why a single-node loss takes everything down

Confirmed by the 2026-05-16 incident on `node3`. **This chain is still live** — the app-tier work above shortens it (a lost orchestrator or Keycloak pod is now covered by its peer) but does not break it, because step 6 depends on databases that remain single-replica. Breaking it is exactly what P3 is for.

The chain:

1. Node goes away (kubelet stops posting at T+0).
2. K8s waits out the taint-eviction timer (`node.kubernetes.io/unreachable:NoExecute` with `tolerationSeconds: 300`).
3. At T+5min, pods on the dead node get `deletionTimestamp` set — they enter `Terminating`.
4. **K8s refuses to fully delete a pod while it has a `VolumeAttachment` to an unreachable node.** This is a safety feature: if the node comes back, deleting the pod could allow a second writer to attach the same RWO volume, corrupting data.
5. Stuck `Terminating` pods hold their PVC attachments. New StatefulSet replicas can't start (RWO can only attach once).
6. Orchestrator's `wait-for-postgres` / `wait-for-keycloak` init containers block forever.
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

## P3 — the data tier: the one open decision

Everything else is done or deliberately out of scope, so this section carries the detail needed to actually decide. **Nothing has started:** no `cnpg` resources exist in any SRW deployment tree; all five Postgres instances are plain single-replica StatefulSets. This is the **M3** item in [[orchestrator_ha_scaling]].

**Scope: five instances.** `postgres` (control plane), `pgvector` (embeddings/memories), `keycloakdb` (auth), `auditdb` (observability), `giteadb` (git metadata, added 2026-07-30). Not all are equal — see the tiering below, which matters because the cheapest credible plan does not treat them uniformly.

### The two paths

**Option A — BYO managed/HA Postgres.** Point `databases.*.externalHost` at a managed or externally-operated HA Postgres. **Zero chart work; the seams already exist and are already exercised** (`databases.gitea.internal: false` was built and render-tested during the Gitea migration). Cost moves from operator complexity to money and/or an existing DBA-ish setup. This is almost certainly the right answer for a SaaS deployment, and it is the only path that also removes the *backup* problem rather than relocating it.

**Option B — in-chart CloudNativePG.** Five `Cluster` resources, each multi-replica with a synchronous standby. Gives automatic failover, PITR, and operator-managed backups, and keeps the "one `helm install` and you have everything" property that self-hosters get today. Costs a new operator dependency, materially more RAM (5 clusters × ≥2 instances), and real operational surface. The doc's long-standing position — worth re-examining now, not assuming — is that this is more machinery than a homelab warrants ([[feedback-dev-vs-prod-pragmatism]]).

These are not mutually exclusive: the chart can keep single-replica StatefulSets as the batteries-included default while production overlays point at external HA Postgres. That is what the Gitea work already set up.

### Constraints that bind either path

- **No transaction-mode pooler in front of the orchestrator.** Leader election holds a *session-scoped* advisory lock (`orchestrator/services/leader_election.py`); PgBouncer/pgcat in txn mode silently breaks it and can stall autovacuum. Direct or session-mode only. This is the single most likely way to get a subtly-broken HA setup.
- **Migration is per-instance and offline-ish**: dump → restore into the new cluster → flip `externalHost` → retire the StatefulSet. `docs/db_migration.md` has the tooling. The Gitea SQLite→Postgres run (`docs/operations/gitea_sqlite_to_postgres.md`) is a good template for the discipline required — especially *verify every table, not the headline counts*.
- **Connection budget.** Orchestrator replicas + per-agent checkpointer connections already share `max_connections` (arithmetic in `helm/values.yaml`). Any pooling added to fix that must respect the constraint above.

### Not all five are equally worth it

| Instance | Down ⇒ | HA worth paying for? |
|---|---|---|
| `postgres` | jobs, auth resolution, cockpit — total outage | **Yes.** Highest impact by far. |
| `keycloakdb` | no login anywhere; now also caps the 2-replica Keycloak | **Yes** — it is the layer the P2 work just exposed. |
| `pgvector` | no memories/KB search; agents degrade but run | Probably — after the first two. |
| `giteadb` | Gitea unusable; orchestrator degrades gracefully | Lower — Gitea is already accepted as single-replica. |
| `auditdb` | audit writes fail; **non-fatal by contract** | No. Accept and document. |

That ordering is the useful part of this decision: "HA the data tier" is not one project, and doing `postgres` + `keycloakdb` first captures most of the availability for a fraction of the cost.

### Out of scope (accept SPOF + document recovery)

- **Neo4j community edition** — no clustering without enterprise license. Stays single-replica. Recovery is restore-from-volume.
- **Gitea** — clustering is awkward (shared FS + Redis + external DB, upstream support experimental) and rarely worth it. Single-replica with fast recovery is fine — but see checklist P1: the *measured* Gitea problems are self-inflicted load and SQLite, not topology.
- **OpenCloud** — single-writer by design (file locking). Don't fight the product.
- **vm-controller** — singleton by design (NATS consumer); leader election would be the fix, similar pattern to `orchestrator_ha_scaling.md` Phase 1.
- ~~**Keycloak server**~~ — no longer out of scope; **done 2026-07-30**, see P2.
- ~~**NATS**~~ — resolved 2026-06, differently than planned: the coordination plane moved to an external 3-node hub (`nats.internal: false` is the chart default). In-chart NATS remains single-replica but is no longer the deployed path.
- ~~**MongoDB**~~ — obsolete; removed from the chart, audit moved to `auditdb`.

## Application tier

Fully specified in [[orchestrator_ha_scaling]] and **complete**: M0 active-passive hardening, M1 leader election, M2-L4 NATS replica-safety, and the background-loop sweep all shipped and live-verified by 2026-06-29. The original cross-doc execution order (drain discipline → Track 1 → NATS → data tier → Track 2) is done except for the data tier (P3) and the optional active-active polish (L2 dispatch throughput, L3 cross-replica SSE), which are parked in P4 until volume demands them.

## Scalability & HA checklist

Prioritized work list from the 2026-07-30 review. Ordering principle: cheap code fixes that shrink outage blast radius first, measured load problems second, topology last. Items are checked off with a date rather than deleted, so this doubles as the progress record.

**P0–P2 are complete. P3 is the open decision; P4 is parked.**

### P0 — Keycloak outage must not destroy login sessions — **DONE (2026-07-30)**

- [x] **(2026-07-30)** Stop deleting BFF sessions when Keycloak is merely *unreachable*. `_refresh_session_in_place` (`orchestrator/security/auth.py`) used to delete the session row on **any** `KeycloakClientError`, but `kc_client.py` raised that same error for network failures and definitive rejections alike — so a Keycloak outage longer than one access-token lifespan (15 min) permanently destroyed every active session. **Fixed:** `KeycloakClientError` now carries `status_code` + `oauth_error`; only a definitive rejection (400 `invalid_grant` — refresh token expired/revoked/SSO ended) deletes the row. Unreachable, 5xx, malformed bodies, and client-misconfig (`invalid_client`) keep the row and fail the request with a retryable 503 (WS handshakes map it to a 4401 close, row kept). Verified on k3d end-to-end: refresh 200 with KC up → scaled KC to 0 → refresh 503 + `/api/auth/me` still 200 + all session rows intact → KC back → same session refreshes 200 without re-login. Unit coverage in `tests/test_bff_session_auth.py`. Residual (accepted): a JWKS cold cache (orchestrator restarted mid-outage) makes cookie validation raise `PyJWKClientError` → 500s until KC returns, but never deletes sessions; PyJWT's lru-cached signing keys cover the non-restart case.

Keycloak is *not* in the per-request hot path (BFF validates JWTs locally via cached JWKS, `orchestrator/security/oidc.py`; KC only sees login/logout/refresh), so this one fix buys most of the practical resilience before any replica work.

### P1 — Gitea: cut self-inflicted load before touching topology — **DONE (2026-07-30)**, one optional item left

Gitea is **not** resource-starved (98m CPU against a 2000m limit); the observed slowness is our own call patterns. Keep it single-replica (see "Out of scope") and fix the load:

- [x] **(2026-07-30) kb_reindex N+1**: was ~11,450 sequential `GET /contents/knowledge/**` calls per day, one REST call per note. **Fixed:** `_GiteaSnapshot` (`orchestrator/services/kb_git_source.py`) gained a `prefetch()` that downloads the snapshot once as `archive/<ref>.tar.gz` (new `GiteaClient.download_repo_archive`, streamed to a temp file, cleaned on snapshot exit) and serves `get_file` from the tarball; the reindexer calls it when the planned batch is ≥ `KB_REINDEX_ARCHIVE_THRESHOLD` (default 25) changed notes, so small incremental sweeps keep the cheap per-file path. Failure falls back to per-file REST, never raises. Verified in-pod against live Gitea: archive reads byte-identical to REST reads, temp file cleaned. *(Residual: the same N+1 shape in `main.py::_copy_tree` — Mode-B cloud export — is untouched.)*
- [x] **(2026-07-30) compare/`stat=true` cost**: `get_compare` now passes `stat/verification/files=false` — but measurement showed Gitea 1.22 **ignores `stat`** on the compare endpoint (honored from 1.23) *and* 404s on SHA bases (`BaseNotExist`), which meant the `since_ref` path of `/api/jobs/{id}/repo/commits` was both slow-by-design and broken for SHA refs. **Fixed properly:** that path now uses the new `GiteaClient.get_commits_between` — commits-endpoint pagination (which honors `stat=false`, ~155×) with a client-side cut at `since_ref`. Verified live: 200/40ms with a SHA base that previously 404'd.
- [x] **(2026-07-30) `cron.git_gc_repos` enabled** — `@weekly`, via `GITEA__cron_0X2E_git_gc_repos__*` env in the chart. Verified registered in `/api/v1/admin/cron` on k3d.
- [x] **(2026-07-30) SQLite WAL** — `GITEA__database__SQLITE_JOURNAL_MODE=WAL` in the chart; `-wal`/`-shm` files confirmed live on k3d. The Postgres migration remains the right pre-SaaS move (tracked below) but WAL removes the worst single-writer/corruption risk now.
- [x] **(2026-07-30) SQLite → Postgres: chart support shipped, `postgres` is now the default.** `gitea.database.type` (`postgres` | `sqlite3`, invalid values fail the render) selects the backend; `databases.gitea` adds a bundled `srw-giteadb` StatefulSet on the same pattern as the Keycloak DB, or points at a managed server via `externalHost`/`sslMode`. `GITEA_DB_PASSWORD` is generate-and-preserve in the chart-managed Secret. A `preflight-db-migration` init container refuses to start a Postgres-configured Gitea whose metadata is still an un-migrated SQLite file, so the flip cannot silently orphan repos. Verified on k3d in an isolated namespace: all four render paths + `helm lint`, guard blocks/passes correctly against real Postgres, and a byte-copy of the dev instance (5 users / 131 repos) migrated cleanly and served through the authenticated API.
- [x] **(2026-07-30) Local k3d migrated** — executed the runbook end-to-end against the live instance: guard fired and blocked as designed, Gitea rebuilt its schema (110 tables), data-only pgloader moved 2369 rows / reset 100 sequences with zero errors, and all acceptance criteria held (5 users, 131 repos, the Keycloak `login_source`). Post-migration smoke passed: Gitea SSO via Keycloak lands on `test - Dashboard`, Cockpit reads job repo commits (including the `since_ref` path), and a new repo got a fresh id (sequences correct). SQLite file retained on the PVC as the one-step rollback.
- [x] **(2026-07-30) dev/homelab migrated.** Both gates cleared (Vault key added by the operator; chart `0.0.0-dev.sha-5252d18` published once a `helm/` commit landed un-batched), then the runbook ran end-to-end: guard blocked as designed, Gitea built its schema (110 tables), data-only pgloader loaded 20,492 rows in 4.1 s, and **all 23 non-empty tables matched SQLite exactly** — 25 users, 443 repos, 2665 releases, the Keycloak `login_source`. Post-checks: repo rows resolve to git objects, a new repo got a fresh id (sequences reset), Gitea initiates the OIDC flow correctly. SQLite retained on the PVC as rollback.

  Two failure modes surfaced here that k3d could not, both now written into the runbook: `kubectl cp` silently truncated the 31 MB backup to 29 MB (printing only `Dropping out copy after 0 retries`, and the short file still opened cleanly in sqlite3 — use `tar` + md5 + `integrity_check`), and four over-long `release.title` values failed the COPY batch and dropped **all 2665 release rows** while users and repos came out perfect. The lesson generalised: verify *every* non-empty table, because a whole-table loss is invisible in the headline counts. Also note Fleet reverts a manual `kubectl scale` within ~90 s, and the guard cannot serve as the lock during the load (the schema-build step's postStart hook creates the bootstrap admin, so the guard reads "1 user — migration complete").
- [ ] **Cache adapter** is `memory` (default) — nothing survives a restart. Low priority; a persistent adapter means Redis (new infra), accept restart-loss for now. Note the HomeLab Gitea (`git.h4ll.app`) already runs Postgres + Redis and is a useful reference config.

SaaS-scale note: the first hard wall is **inodes, not CPU** — ~277 inodes/repo means 100k repos ≈ 28M inodes, which exhausts default ext4 before disk fills. That's a filesystem/provisioning decision for later, recorded here so it isn't rediscovered.

### P2 — Keycloak production mode + `replicas: 2` — **DONE (2026-07-30)**

- [x] **`start-dev` → `start`.** Confirmed from the binary itself, not docs: dev mode pins `cache=local` and `hostname-strict=false` via *classpath* defaults, so a second replica was never going to share sessions. Production mode defaults to `cache=ispn` with the **`jdbc-ping`** discovery stack — peers found through the Keycloak database we already run, so **no headless Service, no DNS_PING, no new infra** — and KC 26 auto-enables mTLS between cluster members. Everything production mode requires was already configured (`KC_HOSTNAME`, `KC_HTTP_ENABLED`, `KC_PROXY_HEADERS`, Postgres). Note `--features` is build-time, so the first start after the switch re-augments the image (~40 s); the existing 300 s startupProbe budget covers it.
- [x] **`keycloak.replicas` exposed**, default **1** — deliberately not 2: a fresh install runs `--import-realm` on every replica at once against an empty DB, and that concurrent-bootstrap path is untested. Existing installs have no import to race, so they opt in via their overlay. Same soak-on-dev-first pattern the orchestrator used for M1.
- [x] **postStart concurrency verified.** Two pods running the kcadm hook simultaneously left the realm clean: 18 clients, no duplicate `cockpit-bff`, 7 users, zero kcadm warnings. The hook's operations are idempotent updates and its create-if-missing paths found the client present.
- [x] **Live-verified on k3d then dev.** k3d at `replicas: 2`: both pods joined one cluster view (`ISPN000094`, 2 members), full browser login worked under `hostname-strict`, and a session **plus its Keycloak token refresh survived deleting both pods in turn** — including the one that served the login. Gitea SSO through the same realm still lands on its dashboard. Dev then took production mode at the chart default, came up clean (KC 26.2.5, realm intact, branded login page served correctly through Cloudflare + Traefik), and was opted into `replicas: 2`: 2-node cluster formed, and **deleting a pod produced zero auth downtime** (10/10 probes returned 200 through the kill) where previously that was a stack-wide auth outage.

Two things worth carrying forward: no NetworkPolicy selects the Keycloak *server* pods, so JGroups 7800 is unrestricted — which k3d could not have proven since k3s ships no policy enforcement by default. And the availability win is still partly gated on P3: a 2-replica Keycloak in front of a single-replica `keycloakdb` has moved the SPOF down one layer, not removed it. A PodDisruptionBudget for Keycloak is the obvious follow-up, but note the trap documented for the orchestrator — `minAvailable: 1` blocks all voluntary evictions at `replicas: 1`, so it has to be conditional on the replica count.

### P3 — Data tier HA — **THE OPEN DECISION** (analysis: [§P3 above](#p3--the-data-tier-the-one-open-decision))

Now the binding constraint on everything: the orchestrator, Keycloak and Gitea are each as available as the database underneath them, and that database is a single pod.

- [ ] **Decide the path** — BYO managed/HA Postgres (zero chart work, seams already built and exercised) vs in-chart CloudNativePG (batteries-included, new operator + real RAM cost). Not mutually exclusive; the chart can keep single-replica defaults while overlays point elsewhere.
- [ ] **Decide the scope** — all five instances, or just `postgres` + `keycloakdb` (most of the availability, a fraction of the cost). `auditdb` is explicitly not worth it: non-fatal by contract.
- [ ] Whichever path: respect the pooler constraint — session-scoped advisory lock for leader election, so no transaction-mode pooling on orchestrator connections.
- [ ] Postgres connection budget: per-agent checkpointer connections + 2 orchestrator replicas share `max_connections` (arithmetic in `helm/values.yaml`). The first hard *throughput* limit as the fleet grows; a session-mode pooler is the likely fix, subject to the constraint above.

**Cheap wins available regardless of the decision** (worth doing even if P3 is deferred, since they shorten the failure chain without new infrastructure):

- [ ] **Keycloak PodDisruptionBudget** — so a node drain can't take both replicas. Must be conditional on replica count: `minAvailable: 1` blocks *all* voluntary evictions at `replicas: 1`, the trap already documented for the orchestrator PDB.
- [ ] **Cockpit / MCP `replicas: 2`** — stateless, cheap, currently 1 on dev.
- [ ] **Verify a real node drain** end-to-end now that the app tier is HA. The runbook above is written but has not been re-exercised since the orchestrator, Keycloak and Gitea changes landed. This is the honest test of whether the failure chain actually shortened.

### P4 — Deferred until volume demands (tracked, not scheduled)

- [ ] **L2 dispatch throughput**: job dispatch is a leader-gated singleton loop; the `SELECT … FOR UPDATE SKIP LOCKED` multi-replica design exists in [[orchestrator_ha_scaling]]. Only needed at job volumes far above current.
- [ ] **L3 cross-replica SSE/notification fan-out**: session streams are already replica-safe (`thread_events` in Postgres is the source of truth); the gap is the low-volume notification/sudo channels, which are replica-local today.
- [ ] **Agent heartbeat batching**: one REST call + DB write per agent per 5 s — fine into the hundreds of agents; batch or move to NATS beyond that.

## Decision log

- **2026-05-20:** Filed after the 2026-05-16 node-pull incident. Choice: document operational discipline (drain) as the *first* HA investment, ahead of any infrastructure work. Rationale: planned maintenance is far more frequent than unplanned hardware failure in this homelab, and `kubectl drain` costs nothing.
- **2026-05-20:** Decided to keep this doc as the umbrella and reference `orchestrator_ha_scaling.md` rather than merging them. The orchestrator HA work is a *consumer* of data-tier HA, not the same problem.
- **2026-05-20:** Postgres-tier migration prioritized over MongoDB. Rationale: Mongo loss is non-fatal per existing graceful-degradation contract; Postgres loss takes down jobs, auth, and the cockpit.
- **2026-07-30:** Added the scalability & HA checklist after a full-stack review. Key calls: (1) the Keycloak BFF refresh bug (sessions deleted on KC-unreachable) is P0 — a code fix beats any topology work on blast radius per effort; (2) Keycloak multi-replica moved from "out of scope" to P2 — KC 26 made it near-free (DB-persisted sessions + jdbc-ping), the only real blocker is our `start-dev` invocation; (3) Gitea stays single-replica — its measured problems are self-inflicted call patterns (kb_reindex N+1, `stat=true`, no gc) and SQLite, not resources or replica count; (4) data tier remains the top structural SPOF, with BYO managed Postgres acknowledged as the likely SaaS path over CloudNativePG.
- **2026-07-30 (end of day):** P0–P2 shipped and live-verified on k3d and dev. Two decisions worth recording because they set precedent: **(a) new HA capabilities ship with a conservative chart default and are opted into per-overlay** — `keycloak.replicas` defaults to 1 (the fresh-install `--import-realm` race is untested) exactly as `orchestrator.replicas` did before M1 soaked. Dev opts in; the default flips once soaked. **(b) A destructive default flip must ship with a guard, not a warning** — the Gitea Postgres default could have silently orphaned hundreds of repos, so it ships with a preflight init container that refuses to start in that state. Both migrations then found real bugs *because* they were run live rather than reasoned about (truncated `kubectl cp`, a varchar overflow dropping an entire table, a guard that misreported credential faults as migration state).
- **2026-07-30:** Reframed the roadmap around the observation that **there is no app-tier HA left to build** — every remaining ✗ is a database or a deliberate singleton, so all remaining availability work routes through P3. Also split P3 into a path decision *and* a scope decision, since `postgres` + `keycloakdb` capture most of the benefit and `auditdb` is explicitly not worth HA (non-fatal by contract).

## Related code

- `helm/templates/databases/postgres.yaml` — current single-replica StatefulSet
- `helm/templates/databases/postgres-vector.yaml` — same
- `helm/templates/databases/postgres-keycloak.yaml` — same
- `helm/templates/databases/postgres-audit.yaml` — same
- `helm/templates/databases/postgres-gitea.yaml` — same (`srw-giteadb`, added 2026-07-30)
- `docs/operations/gitea_sqlite_to_postgres.md` — validated migration runbook for existing Gitea instances
- `helm/values.yaml` — `orchestrator.replicas: 2` default; cockpit/mcp still 1; Postgres connection-budget arithmetic
- `helm/templates/services/keycloak.yaml` — production-mode invocation + `keycloak.replicas` (P2, done)
- `helm/templates/services/gitea.yaml` — backend conditional + the `preflight-db-migration` guard (P1, done)
- `orchestrator/security/auth.py` / `orchestrator/security/kc_client.py` — BFF session refresh path (checklist P0)
- `orchestrator/services/leader_election.py` — session-scoped advisory lock (pooler constraint)
- `docs/features/orchestrator_ha_scaling.md` — application-tier HA design (shipped M0–M2; open L2/L3)
- `docs/db_migration.md` — migration tooling (relevant for the CloudNativePG cutover)

## Sources

- [Kubernetes: Non-Graceful Node Shutdown Handling](https://kubernetes.io/docs/concepts/architecture/nodes/#non-graceful-node-shutdown) — the `node.kubernetes.io/out-of-service` taint.
- [Longhorn: Node Failure](https://longhorn.io/docs/latest/high-availability/node-failure/) — replica behavior when a node dies.
- [CloudNativePG](https://cloudnative-pg.io/) — Postgres operator referenced in the roadmap.
