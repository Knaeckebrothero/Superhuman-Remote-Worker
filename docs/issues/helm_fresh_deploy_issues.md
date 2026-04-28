# Helm Fresh Deploy Issues — 2026-04-16

After nuking the `superhuman-remote-worker` namespace and letting Fleet
recreate everything from the Helm chart, the deployment is partially broken.
This document catalogs all observed issues, their root causes, and fixes.

## Current State Summary

| Pod | Status | Blocked By |
|-----|--------|------------|
| srw-postgres-0 | Running | — |
| srw-postgres-vector-0 | Running | — |
| srw-mongodb-0 | Running | — |
| srw-neo4j | Running | — |
| srw-codex-proxy | Running | — |
| srw-dozzle | Running | — |
| srw-mongo-express | Running | — |
| srw-pgadmin | Running | — |
| srw-reloader | Running | — |
| srw-vpn-cluster | Running | — |
| srw-vpn-research | Running | — |
| srw-vpn-workstation | Running | — |
| **srw-keycloak** | **CrashLoopBackOff** | Issue 1 |
| **srw-gitea-0** | **Init:0/1** (wait-for-keycloak) | Issue 1 → 2 |
| **srw-orchestrator** | **Init:3/5** (wait-for-gitea) | Issue 1 → 2 → 3 |
| **srw-cockpit** | **Init:0/1** (wait-for-orchestrator) | Issue 1 → 2 → 3 |
| **srw-mcp** | **Init:0/1** (wait-for-orchestrator) | Issue 1 → 2 → 3 |
| **srw-opencloud** | **Init:0/1** (wait-for-keycloak) | Issue 1 |
| srw-agent | 0/0 replicas | Issue 4 |
| srw-nextcloud | 0/0 replicas | Issue 5 |

---

## Issue 1: Postgres init scripts skipped — keycloak DB/role missing

**Severity**: Critical (blocks entire dependency chain)

**Symptom**: Keycloak CrashLoopBackOff with:
```
FATAL: password authentication failed for user "keycloak"
DETAIL: Role "keycloak" does not exist.
```

**Root cause**: Postgres first log line:
```
PostgreSQL Database directory appears to contain a database; Skipping initialization
```

The `init_sso_dbs.sh` script is mounted at
`/docker-entrypoint-initdb.d/` and correctly creates the `keycloak` role
and database. However, the official postgres image **only runs initdb
scripts when the data directory is empty**. Since the data directory had
existing data (see Issue 6), the script was silently skipped.

**Why the data directory had data**: The PVCs are only 21 minutes old (new
namespace), but postgres logs show crash recovery from a previous unclean
shutdown. This means the Longhorn volume backing the new PVC contains data
from a prior incarnation. Longhorn with `Retain` reclaim policy keeps volume
data even after PVC deletion — and when a new PVC with the same name is
created, Longhorn may provision a volume that re-attaches existing replica
data from the node.

**Fix options**:

1. **Quick (manual SQL)**: `kubectl exec` into `srw-postgres-0` and run the
   init SQL manually:
   ```bash
   kubectl --context main -n superhuman-remote-worker exec -it srw-postgres-0 -- \
     psql -U srw -d postgres -c "
       DO \$\$
       BEGIN
         IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'keycloak') THEN
           CREATE ROLE keycloak WITH LOGIN PASSWORD '<KC_DB_PASSWORD from vault>';
         END IF;
       END\$\$;
       SELECT 'CREATE DATABASE keycloak OWNER keycloak'
       WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak') \gexec
     "
   ```
   Keycloak will pick it up on the next CrashLoopBackOff retry.

2. **Nuclear (delete PVC)**: Delete `srw-postgres-data` PVC to force a truly
   empty data directory. All postgres data (orchestrator tables, users, jobs)
   will be lost. The orchestrator's `init.py` will re-create schemas on first
   boot.

3. **Chart fix (long-term)**: Add a dedicated init container to the postgres
   StatefulSet that runs `init_sso_dbs.sh` on every start (idempotent), not
   relying on `docker-entrypoint-initdb.d/` which only fires on first init.

---

## Issue 2: Cascading init-container deadlock

**Severity**: Critical (consequence of Issue 1)

**Symptom**: Multiple pods stuck in `Init` state, forming a dependency chain:

```
keycloak (CrashLoopBackOff)
  ← gitea (wait-for-keycloak on port 8080)
    ← orchestrator (wait-for-gitea on port 3000, 4th of 5 init containers)
      ← cockpit (wait-for-orchestrator on port 8085)
      ← mcp (wait-for-orchestrator on port 8085)
  ← opencloud (wait-for-keycloak on port 8080)
```

**Root cause**: The `nc -z <service> <port>` init containers have no timeout
and will wait indefinitely. When keycloak can't start (Issue 1), everything
downstream is permanently blocked.

**Fix**: Resolving Issue 1 will unblock the entire chain. Keycloak will start
→ gitea init passes → orchestrator init passes → cockpit/mcp init pass.

**Chart improvement consideration**: Add a timeout to the init container
`nc` loops (e.g., `timeout 300 sh -c 'until nc -z ...; do sleep 2; done'`)
so pods fail with a clear error instead of hanging forever.

---

## Issue 3: Orchestrator init container ordering

**Severity**: Info (not broken, but worth noting)

The orchestrator has 5 init containers in sequence:
1. `wait-for-postgres` — completed
2. `wait-for-postgres-vector` — completed
3. `wait-for-mongodb` — completed
4. `wait-for-gitea` — **stuck** (gitea blocked by keycloak)
5. `wait-for-keycloak` — not started yet

The orchestrator waits for gitea before keycloak, but gitea itself waits for
keycloak. This means the orchestrator indirectly waits for keycloak twice.
Not a bug, but the ordering could be optimized.

---

## Issue 4: Agent deployment scaled to 0

**Severity**: Expected (not a bug)

`srw-agent` has `replicas: 0`. This is correct — the chart default is 0
agent replicas. Agents are scaled up on demand by the orchestrator's
container provisioner (Kubernetes mode) or via manual scaling.

No action needed.

---

## Issue 5: Nextcloud deployment scaled to 0

**Severity**: Expected (not a bug)

`srw-nextcloud` has `replicas: 0`. The values file has:
```yaml
opencloud:
  enabled: true
nextcloud:
  enabled: false
```

OpenCloud replaced Nextcloud as the cloud storage provider. The nextcloud
deployment is correctly disabled. The template likely renders a 0-replica
deployment rather than omitting it entirely when `enabled: false`.

No action needed.

---

## Issue 6: 48 orphaned Released PersistentVolumes

**Severity**: Low (storage waste, no functional impact)

There are **48 Released PVs** from previous namespace deletions still present
on the cluster, all with `Retain` reclaim policy (Longhorn default). These
consume Longhorn storage capacity but serve no purpose.

Examples of accumulated volumes per resource:
- `srw-postgres-data`: 6 Released PVs (60 GiB)
- `srw-postgres-vector-data`: 5 Released PVs (50 GiB)
- `srw-nextcloud-data`: 7 Released PVs (70 GiB)
- `srw-workspace`: 4 Released PVs (80 GiB)
- `srw-mongodb-data`: 5 Released PVs (25 GiB)
- `srw-neo4j-data`: 4 Released PVs (40 GiB)
- `srw-gitea-data`: 4 Released PVs (20 GiB)
- `srw-protonmail-bridge-data`: 5 Released PVs (5 GiB)
- Various others

**Total estimated waste**: ~350+ GiB of Longhorn replica data.

**Fix**: Delete the Released PVs that are no longer needed:
```bash
kubectl --context main get pv --no-headers | \
  grep "superhuman.*Released" | awk '{print $1}' | \
  xargs kubectl --context main delete pv
```

**Long-term**: Consider changing the Longhorn StorageClass reclaim policy
to `Delete` for this namespace, or adding a cleanup CronJob.

---

## Issue 7: No `fullnameOverride` in values

**Severity**: Info (cosmetic, already working)

The chart rendered all resources with `srw-*` prefix because the Fleet
release name produces this via the `_helpers.tpl` logic. This is the
desired naming. However, this naming is implicit — it depends on how Fleet
names the Helm release (derived from the bundle path `deployment`).

For explicitness and resilience against Fleet release-naming changes,
consider adding to `values-experimental.yaml`:
```yaml
fullnameOverride: srw
```

---

## Resolution Order

1. **Fix Issue 1** (create keycloak DB) — unblocks everything
2. **Verify cascade** — keycloak → gitea → orchestrator → cockpit/mcp/opencloud should all start
3. **Clean up Issue 6** (delete orphaned PVs) — reclaim storage
4. **Consider chart fixes** for Issues 2 and 3 (init container timeouts and ordering) — prevent future occurrences
