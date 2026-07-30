# Migrating the bundled Gitea from SQLite to PostgreSQL

> **Status:** **Executed successfully on the local k3d cluster 2026-07-30**
> (5 users / 131 repos / 1 Keycloak login source, all preserved). Every step
> below is the version that actually ran, including the guard firing on the
> live instance and the post-migration smoke tests. Previously rehearsed
> against a byte-copy in an isolated namespace. **The dev/homelab cluster has
> not been migrated yet** — it remains pinned to `sqlite3`.
>
> **Applies to:** any deployment whose Gitea predates the 2026-07-30 chart
> default flip (`gitea.database.type: postgres`) and is therefore pinned to
> `sqlite3` in its values overlay.

## Why this is a data migration, not a config flip

Gitea splits its state across two places:

| What | Where | Affected by this migration? |
|---|---|---|
| Git objects (the actual repositories) | Gitea PVC, `/var/lib/gitea/git/repositories/` | **No** — untouched |
| Users, orgs, repo rows, issues, PRs, tokens, OIDC login source | The metadata DB (SQLite file or Postgres) | **Yes** — this is what moves |

Pointing a populated Gitea at an empty Postgres does not error. Gitea runs its
migrations against the blank database, comes up believing it is a **fresh
install**, and every existing repository sits orphaned on disk — invisible to
the API and the UI. The chart ships a `preflight-db-migration` init container
that detects exactly this state (SQLite file present + zero users in Postgres)
and refuses to start, so the failure mode is a stuck pod rather than a
confusing empty Gitea. Do not remove that guard to "make it start".

## Critical: pgloader must NOT create the schema

The obvious approach — `pgloader` with `include drop, create tables, create
indexes` — **produces a Gitea that cannot start.** pgloader derives the target
schema from SQLite, which yields SQLite-flavoured artifacts Gitea's XORM layer
then cannot reconcile:

```
sync database struct error: pq: cannot drop index idx_16399_sqlite_autoindex_auth_token_1
because constraint idx_16399_sqlite_autoindex_auth_token_1 on table auth_token requires it
```

plus a stream of `db type is TEXT, struct type is VARCHAR(255)` warnings. The
ORM retries 10 times and gives up.

The working sequence is **schema-first, data-only**: let Gitea create its own
schema against the empty Postgres, then load only rows into it. Verified: 110
tables created by Gitea's own migrations, 2369 rows loaded in 0.5 s, 100
sequences reset, zero startup errors, all users and repos visible afterwards.

## Procedure

Throughout, `NS` is the release namespace and `CTX` your kube context.

### 0. Back up (non-negotiable)

```bash
# Snapshot the SQLite file plus its WAL sidecars. -wal holds committed data
# not yet folded into the main file; copying gitea.db alone can lose writes.
for f in gitea.db gitea.db-wal gitea.db-shm; do
  kubectl --context=$CTX -n $NS cp srw-gitea-0:/var/lib/gitea/data/$f ./backup/$f -c gitea
done
# Fold the WAL into the main file so the copy is self-contained.
sqlite3 ./backup/gitea.db "PRAGMA wal_checkpoint(TRUNCATE);"
sqlite3 ./backup/gitea.db "SELECT 'users='||(SELECT COUNT(*) FROM user), 'repos='||(SELECT COUNT(*) FROM repository);"
```

Record those two numbers — they are the acceptance criteria in step 6. Also
take a volume snapshot of the Gitea PVC if your storage class supports it.

### 1. Flip the overlay to Postgres

Remove the `gitea.database.type: sqlite3` pin from your values overlay
(`deployment/values-experimental.yaml`, `deployment/values-local.yaml`, …) so
the chart default applies, add a `databases.gitea.storageSize` matching your
storage class's constraints, and deploy.

> **Under Tilt (local k3d), the values edit does NOT auto-deploy.** The
> Tiltfile's `k8s_custom_deploy` sets `deps=[]` — only image rebuilds drive the
> loop — and `tilt trigger srw` runs `helm uninstall` first, which you do not
> want mid-migration. Apply by invoking Tilt's own script with the image tags
> the release is already running, so a bare `helm upgrade` cannot revert them
> to the stale `ghcr` defaults (see
> `reference_tilt_helm_upgrade_reverts_image`):
>
> ```bash
> IMGFLAGS=$(helm -n srw get values srw | python3 -c "
> import yaml,sys; v=yaml.safe_load(sys.stdin); out=[]
> for k,s in v.get('image',{}).items():
>     if isinstance(s,dict):
>         if s.get('repository'): out.append(f'--set image.{k}.repository={s[\"repository\"]}')
>         if s.get('tag'): out.append(f'--set image.{k}.tag={s[\"tag\"]}')
> print(' '.join(out))")
> RELEASE_NAME=srw CHART=./helm NAMESPACE=srw scripts/tilt-helm-apply.sh \
>   --take-ownership --values=deployment/values-local.yaml \
>   --values=deployment/values-tilt.yaml $IMGFLAGS
> ```
>
> Confirm with a `--dry-run` first that every `image:` line still carries a
> `tilt-*` tag.

> **Expect a stack-wide pod bounce.** Adding `GITEA_DB_PASSWORD` to the Secret
> makes Stakater Reloader restart everything annotated
> `reloader.stakater.com/auto` (orchestrator, Keycloak, MCP, …). Harmless, but
> it churns for a few minutes — let it settle before smoke-testing.

Expected, and **not** a failure: `srw-giteadb` comes up healthy while
`srw-gitea` sits in `Init:CrashLoopBackOff` with the guard's refusal message.
That is the checkpoint proving the guard works. Nothing has been modified yet;
reverting the pin at this point restores service immediately.

### 2. Let Gitea build the schema

The guard blocks on *SQLite file present + empty Postgres*, so briefly remove
the first condition:

Scale Gitea to zero so the RWO volume is free, then park the file with a
throwaway pod that mounts the same PVC (this is what was actually run — it
avoids trying to `exec` into a pod stuck in `Init:Error`):

```bash
kubectl --context=$CTX -n $NS scale statefulset/srw-gitea --replicas=0
# wait for the pod to be gone, then:
cat << 'EOF' | kubectl --context=$CTX -n $NS apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: gitea-dbpark
spec:
  restartPolicy: Never
  containers:
    - name: shell
      image: busybox:1.36
      command: ["sleep", "600"]
      volumeMounts:
        - {name: data, mountPath: /var/lib/gitea}
  volumes:
    - name: data
      persistentVolumeClaim: {claimName: srw-gitea-data}
EOF

# Park the SQLite file AND its WAL sidecars (do NOT delete — this is the rollback).
kubectl --context=$CTX -n $NS exec gitea-dbpark -- sh -c '
  mv /var/lib/gitea/data/gitea.db /var/lib/gitea/data/gitea.db.migrating
  for s in wal shm; do
    [ -f /var/lib/gitea/data/gitea.db-$s ] && \
      mv /var/lib/gitea/data/gitea.db-$s /var/lib/gitea/data/gitea.db.migrating-$s
  done'
kubectl --context=$CTX -n $NS delete pod gitea-dbpark
kubectl --context=$CTX -n $NS scale statefulset/srw-gitea --replicas=1
```

Wait until Gitea is serving, then confirm it built its schema:

```bash
kubectl --context=$CTX -n $NS exec srw-giteadb-0 -- \
  env PGPASSWORD=$PW psql -U gitea -d gitea -tAc \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'"
# expect ~110 on Gitea 1.22
```

> The DB password lives in the chart Secret under `GITEA_DB_PASSWORD`. Note the
> Secret's name is the release name (`srw` on k3d), not `srw-secrets`:
> `kubectl -n $NS get secret srw -o jsonpath='{.data.GITEA_DB_PASSWORD}' | base64 -d`.
> An empty password makes pgloader fail with a cryptic
> `The value NIL is not of type STRING when binding STRING`.

### 3. Load the data

Scale Gitea down so nothing writes during the load, then run pgloader from a
scratch pod with the backup mounted or copied in:

```bash
kubectl --context=$CTX -n $NS scale statefulset/srw-gitea --replicas=0
kubectl --context=$CTX -n $NS run migrator --image=dimitri/pgloader:latest \
  --restart=Never --command -- sleep 3600
kubectl --context=$CTX -n $NS cp ./backup/gitea.db migrator:/tmp/gitea.db
```

```bash
kubectl --context=$CTX -n $NS exec migrator -- sh -c "
cat > /tmp/gitea-data.load << 'EOF'
LOAD DATABASE
  FROM sqlite:///tmp/gitea.db
  INTO postgresql://gitea:\$PW@srw-giteadb:5432/gitea

WITH data only, truncate, disable triggers, reset sequences,
     workers = 4, concurrency = 1

SET work_mem to '16MB', maintenance_work_mem to '128MB';
EOF
pgloader /tmp/gitea-data.load"
```

`data only` is the load-bearing clause — it keeps Gitea's schema and inserts
rows into it. `reset sequences` is required, otherwise the first new repo or
user collides with an existing primary key. Expect `0` in the errors column on
every table.

> **Air-gapped / flaky-DNS clusters:** if the in-cluster pull of
> `dimitri/pgloader` fails to resolve `registry-1.docker.io`, pull on the host
> and side-load it (`docker pull dimitri/pgloader:latest` then
> `k3d image import dimitri/pgloader:latest -c srw`), and run the pod with
> `--image-pull-policy=Never`.

### 4. Start Gitea on Postgres

```bash
kubectl --context=$CTX -n $NS scale statefulset/srw-gitea --replicas=1
kubectl --context=$CTX -n $NS logs srw-gitea-0 -c preflight-db-migration
# expect: "Postgres already holds N users — migration complete, proceeding."
```

The guard now takes its "already migrated" branch — Postgres has users, so the
parked SQLite file no longer blocks startup.

### 5. Restore the parked SQLite file (still as rollback)

```bash
kubectl --context=$CTX -n $NS exec srw-gitea-0 -c gitea -- \
  mv /var/lib/gitea/data/gitea.db.migrating /var/lib/gitea/data/gitea.db
```

Leaving it in place is deliberate: it costs a few MB and keeps the one-command
rollback available. Delete it only after step 6 passes and you have lived with
the Postgres backend for a while.

### 6. Verify

```bash
# Counts must match the numbers recorded in step 0.
kubectl --context=$CTX -n $NS exec deploy/srw-orchestrator -c orchestrator -- sh -c \
  'curl -s -u "$GITEA_ADMIN_USER:$GITEA_ADMIN_PASSWORD" \
   "http://srw-gitea:3000/api/v1/repos/search?limit=1" -D - -o /dev/null | grep -i x-total-count'
kubectl --context=$CTX -n $NS exec srw-gitea-0 -c gitea -- gitea admin user list
```

Then exercise the paths that actually matter for SRW. All four were run on k3d
after the 2026-07-30 migration and passed:

- **Log in to the Gitea web UI via Keycloak** (`https://git.localhost/` → *Sign
  in with Keycloak* → lands on `test - Dashboard`) — proves the `login_source`
  row survived; without it OIDC users cannot authenticate. Verify the source is
  present and active first:
  ```bash
  kubectl --context=$CTX -n $NS exec srw-giteadb-0 -- \
    env PGPASSWORD=$PW psql -U gitea -d gitea -tAc "SELECT name, type, is_active FROM login_source"
  # expect: Keycloak|6|t
  ```
  A first-time OIDC user is auto-registered as a *new* Gitea account, so the
  user count legitimately grows by one here — not a migration fault.
- **Read a job's repo through the Cockpit** — proves repo rows resolve to git
  objects still on the PVC:
  `GET /api/jobs/{id}/repo/commits` (and the `?since_ref=<sha>` variant) should
  return real commits.
- **Sequence check** — create and delete a throwaway repo. A missed
  `reset sequences` surfaces here as a primary-key collision:
  ```bash
  curl -u $ADMIN -X POST -H 'Content-Type: application/json' \
    -d '{"name":"migration-seq-check","private":true}' \
    http://srw-gitea:3000/api/v1/user/repos     # expect a fresh id, then DELETE it
  ```
- **Let the stack settle before judging** — the Reloader bounce (step 1) means
  transient `Terminating`/`Init:` pods are expected for a few minutes.

### Rollback

At any point before you delete the SQLite file:

1. Re-add the pin to the values overlay:
   ```yaml
   gitea:
     database:
       type: sqlite3
   ```
2. Deploy. Gitea restarts on the SQLite file exactly as before.

The `srw-giteadb` PVC carries `helm.sh/resource-policy: keep`, so a failed
attempt leaves the half-migrated Postgres around for inspection rather than
silently vanishing. Delete it explicitly before retrying so step 2's schema
creation starts clean.

## Prerequisites for the Fleet-managed dev/homelab cluster

That cluster does not use the chart-managed Secret, so two gates must be
cleared **before** un-pinning `gitea.database.type` there. Both were verified
open on 2026-07-30.

1. **`GITEA_DB_PASSWORD` must exist in Vault.** The `srw` Secret is ESO-synced
   with `dataFrom: extract` from `homelab/superhuman-remote-worker/srw-secrets`
   (KV v2). The chart's generate-and-preserve fallback only applies when
   `secrets.create=true`, which this cluster does not use — so a missing key is
   not filled in, it just leaves `srw-giteadb` in
   `CreateContainerConfigError`. Add a fresh 32-char random value, then let ESO
   refresh (1 h interval) or force it:
   ```bash
   kubectl -n superhuman-remote-worker annotate externalsecret srw \
     force-sync=$(date +%s) --overwrite
   kubectl -n superhuman-remote-worker get secret srw \
     -o jsonpath='{.data.GITEA_DB_PASSWORD}' | base64 -d   # must be non-empty
   ```

2. **A chart containing the Postgres support must be published and pinned in
   `fleet.yaml`.** `develop.yml`'s `deploy-experimental` job publishes only when
   `changes.outputs.chart == 'true'` or an image build succeeded — and change
   detection looks at the pushed tip. A `helm/` commit batched *underneath* a
   docs-only tip therefore produces no chart (this happened to `22c07e5f`).
   Landing any commit that touches `helm/` publishes the chart and auto-bumps
   `fleet.yaml` in the same CI commit. Confirm before proceeding:
   ```bash
   helm show chart oci://ghcr.io/knaeckebrothero/charts/srw-dev \
     --version 0.0.0-dev.sha-<sha>
   ```

Landing the chart *before* the Vault key is safe: `values-experimental.yaml`
still pins `sqlite3`, and `postgres-gitea.yaml` is gated on that, so nothing
new renders until the pin is dropped. Drop the pin only once both gates pass.

**Plan the window.** Steps 2–4 take Gitea offline for several minutes, and
agents push to Gitea throughout a job. Check for live work first
(`kubectl get pods | grep -E 'agent-j|workspace'`) and pick a quiet slot. The
dev instance is ~10× k3d's (443 repos, 31 MB SQLite), so expect the pgloader
step to take tens of seconds rather than one.

## Notes for external Postgres

`databases.gitea.internal: false` points the bundled Gitea at a managed server
(`externalHost` / `externalPort` / `externalDb` / `sslMode`), with credentials
from the Secret: `GITEA_DB_USER` and `GITEA_DB_PASSWORD`. In ESO deployments
both keys must exist in the Vault bundle — the chart's generate-and-preserve
fallback only applies to the chart-managed Secret. The same migration procedure
applies; only the connection target changes.

## Related

- `docs/features/high_availability_setup.md` — HA checklist P1 (Gitea load) and
  the reasoning for keeping Gitea single-replica.
- `helm/templates/services/gitea.yaml` — the backend conditional and the
  preflight guard.
- `helm/templates/databases/postgres-gitea.yaml` — the bundled `srw-giteadb`.
