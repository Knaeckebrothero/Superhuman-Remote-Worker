# Migrating the main cloud backend from OpenCloud to Nextcloud

> **Applies to:** the Fleet-managed dev/homelab cluster
> (`superhuman-remote-worker` namespace on the `main` context), which ran the
> bundled OpenCloud as its main cloud from cluster creation until 2026-08-02.
> Local k3d and `srw-prod-private` were already on Nextcloud and are not
> affected by this runbook.

## What actually moves

The chart supports both backends and picks one in `templates/configmap.yaml`:
`opencloud.enabled` wins, then `nextcloud.enabled`, then
`cloud.externalBackend`. So the *config* side of this migration is a flag
flip. What makes it a migration rather than a flag flip is everything keyed to
the old backend:

| What | Where | Survives the flip? |
|---|---|---|
| User files | OpenCloud data PVC + its S3 staging | **No** — copy them out first |
| `cloud.srw.works` routing | Ingress, host is shared by both backends | Re-pointed by the chart |
| `projects.main_cloud_backend` / `main_cloud_folder_handle` | app DB | **No** — dangling OpenCloud Space handles |
| `threads.main_cloud_backend` / `main_cloud_session_handle` | app DB | **No** — same |
| `thread_mounts` rows | app DB | **No** — `webdav_url` points at `/dav/spaces/...` |
| `users.cloud_identity` | app DB | Inert — the column is keyed per backend |
| OpenCloud OIDC clients + `opencloudAdmin`/`opencloudUser` groups | Keycloak realm | **No** — must be deleted by hand |
| OpenCloud data/config PVCs | cluster | **No** — `helm.sh/resource-policy: keep` |

The DB rows are the part that bites. `main_cloud_router.for_backend()` falls
back to the *active* backend when it cannot build the recorded one, logging
"Operations on this row will use the wrong backend" — so a project left on
`main_cloud_backend='opencloud'` does not fail loudly, it quietly addresses a
Nextcloud instance with an OpenCloud Space handle.

## 0. Prerequisite — Vault keys (BLOCKING)

`nextcloud.objectStore.enabled=true` stores user files in MinIO. The Nextcloud
pod reads two keys the OpenCloud-era bundle never had:

- `NEXTCLOUD_S3_ACCESS_KEY`
- `NEXTCLOUD_S3_SECRET_KEY`

Add both to `homelab/superhuman-remote-worker/srw-secrets` **before** the chart
lands. The `secretKeyRef` is not `optional: true`, so a missing key leaves the
pod in `CreateContainerConfigError` — and by then OpenCloud is already gone,
so the cluster has no cloud backend at all.

The other three Nextcloud keys (`NEXTCLOUD_ADMIN_PASSWORD`,
`NEXTCLOUD_AGENT_PASSWORD`, `NEXTCLOUD_OIDC_CLIENT_SECRET`) are already in the
bundle from the shared realm-export era and are reused as-is.

### The MinIO side already exists — do not re-create it

Checked 2026-08-03 against `minio.minio.svc:9000`. Both the bucket and a
correctly-scoped access key are already present from an earlier (2026-04)
bundled-Nextcloud run:

- Bucket `srw-nextcloud` — created 2026-03-19, **un-versioned, no object
  lock**, which is what Nextcloud S3 primary storage requires (Nextcloud keeps
  its own versions; S3 versioning bloats or breaks primary storage).
- Access key `srw-nextcloud` — a service account under `miniroot`, no
  expiry, with an embedded policy matching `srw-snapshots` verbatim in shape.

Note the convention: this MinIO uses **bucket-scoped access keys with embedded
policies**, not IAM users plus named policies — `mc admin user list` and
`mc admin policy list` are therefore empty of anything custom. The
`nextcloud-rw` named-policy recipe in the HomeLab repo's
`docs/superpowers/plans/2026-06-03-nextcloud-ha.md` describes a different
approach and was never applied here.

The embedded policy on `srw-nextcloud`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:ListBucketMultipartUploads"],
      "Resource": ["arn:aws:s3:::srw-nextcloud"] },
    { "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListMultipartUploadParts", "s3:PutObject",
                 "s3:AbortMultipartUpload", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::srw-nextcloud/*"] },
    { "Effect": "Allow",
      "Action": ["s3:CreateBucket"],
      "Resource": ["arn:aws:s3:::srw-nextcloud"] }
  ]
}
```

The bucket-scoped `s3:CreateBucket` is deliberate and matches `srw-snapshots`:
the chart hardcodes `OBJECTSTORE_S3_AUTOCREATE=true`
(`helm/templates/services/nextcloud.yaml`), and the grant is narrow enough that
the key still cannot touch any other bucket.

### Two things that do still need doing

1. **The secret half of the key is unknown.** MinIO cannot reveal it, the
   Vault bundle has no `NEXTCLOUD_S3_*` entries, and the repo carries only
   commented placeholders (`.env.example`) and `"stub"` (`values-local.yaml`).
   Rotate it in place — this preserves the key name and its embedded policy:

   ```bash
   mc admin accesskey edit local srw-nextcloud --secret-key '<new-secret>'
   ```

   Then write `NEXTCLOUD_S3_ACCESS_KEY=srw-nextcloud` and the new secret into
   Vault.

2. **The bucket is not empty** — 207 objects / 114 MiB dated 2026-04-06, in
   Nextcloud's native `urn:oid:<fileid>` layout. Those are orphans: the object
   key is `urn:oid:` plus a row id from Nextcloud's `oc_filecache`, and the
   bundled Nextcloud comes up on a **fresh SQLite DB on a new PVC**, so its
   fileid counter restarts at 1. It will write `urn:oid:1`, `urn:oid:2`, …
   straight over the old objects, while every higher-numbered orphan lingers
   forever. Empty the bucket before cutover:

   ```bash
   mc rm --recursive --force local/srw-nextcloud
   ```

ESO refreshes hourly; force it rather than waiting:

```bash
kubectl --context=main -n superhuman-remote-worker \
  annotate externalsecret srw force-sync="$(date +%s)" --overwrite
kubectl --context=main -n superhuman-remote-worker \
  get secret srw -o go-template='{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}' \
  | grep NEXTCLOUD_S3
```

## 1. Copy the data out

Whatever is in OpenCloud is not migrated by any step below. Pull it before the
PVCs are deleted (the 2026-08-02 run skipped a formal export — the content was
disposable test data that had already been copied off).

## 2. Land the chart change

Two commits' worth of change, both on `develop`:

- `deployment/values-experimental.yaml` — `opencloud.enabled: false`,
  `nextcloud.enabled: true` (+ `replicas`, `storageClass`, `objectStore`).
- `helm/templates/ingress.yaml` — a Nextcloud Ingress. The chart previously
  rendered one **only** for OpenCloud, so without this `cloud.srw.works` goes
  dark on the flip. It is gated `not opencloud.enabled` because both backends
  claim the same host.
- `helm/templates/services/keycloak.yaml` — the OpenCloud client bootstrap is
  now gated on `opencloud.enabled`. It used to key off "is
  `OPENCLOUD_KEYCLOAK_CLIENT_SECRET` non-empty", and that secret outlives the
  backend in Vault, so the postStart hook would re-create the five OpenCloud
  clients — and re-register `opencloudUser` as a realm **default** group — on
  every Keycloak restart, undoing step 4 forever.

CI on `develop` publishes the chart as `0.0.0-dev.sha-<hash>` and commits the
`deployment/fleet.yaml` version bump itself. Fleet then reconciles. Do not
hand-apply anything to the namespace.

Fleet removes the OpenCloud Deployment, Service, Ingress and cleanup CronJob.
It does **not** remove the PVCs (step 3) or the cert Secret.

## 3. Reclaim the OpenCloud storage

Both PVCs carry `helm.sh/resource-policy: keep`, so they outlive the release
and keep consuming Longhorn capacity until deleted by hand:

```bash
kubectl --context=main -n superhuman-remote-worker delete \
  pvc/srw-opencloud-data pvc/srw-opencloud-config
kubectl --context=main -n superhuman-remote-worker delete \
  secret/srw-opencloud-tls          # cert-manager reissues for Nextcloud
```

`srw-opencloud-data` is on the `longhorn` StorageClass, whose reclaim policy is
`Retain` — deleting the PVC releases the PV but does not free the replicas.
Delete the released PV (or clean it up in the Longhorn UI) to actually recover
the 16Gi.

## 4. Clear the Keycloak leftovers

Only needed once, after the gating in step 2 is live — otherwise the next
Keycloak restart puts them back:

```bash
KC=/opt/keycloak/bin/kcadm.sh
POD=$(kubectl --context=main -n superhuman-remote-worker \
        get pod -l app.kubernetes.io/component=keycloak -o name | head -1)
kubectl --context=main -n superhuman-remote-worker exec "$POD" -- sh -c "
  $KC config credentials --server http://localhost:8080 --realm master \
    --user \$KEYCLOAK_ADMIN --password \$KEYCLOAK_ADMIN_PASSWORD
  for c in opencloud-web opencloud-orchestrator OpenCloudDesktop \
           OpenCloudAndroid OpenCloudIOS; do
    ID=\$($KC get clients -r srw -q clientId=\$c --fields id --format csv --noquotes | tail -1)
    [ -n \"\$ID\" ] && $KC delete clients/\$ID -r srw
  done"
```

Then drop `opencloudUser` from the realm's default groups (Realm settings →
User registration → Default groups) and delete the `opencloudAdmin` /
`opencloudUser` groups. Leaving them is cosmetic, but `opencloudUser` is
auto-assigned to every new user, so it keeps growing.

## 5. Reset the stale cloud bindings in the app DB

This is the step that decides whether projects come back. Clearing both the
backend and the handle makes `_heal_project_cloud` (`orchestrator/main.py`)
treat the project as folder-less and re-provision it on the active backend the
next time the project is opened; clearing only the handle would re-provision
it **on OpenCloud**, because `for_project()` dispatches on the row's
`main_cloud_backend`.

State on the dev cluster immediately before the 2026-08-02 cutover: 33
projects, 159 threads, 206 `thread_mounts`, 23 `users.cloud_identity` entries.

```sql
-- Projects re-provision on Nextcloud when next opened.
UPDATE projects
   SET main_cloud_backend       = NULL,
       main_cloud_folder_handle = NULL,
       nextcloud_folder_id      = NULL
 WHERE main_cloud_backend = 'opencloud';

-- Session folders and their mounts: no Nextcloud equivalent exists, and a
-- stale row makes the session try to mount a dead /dav/spaces/ URL.
DELETE FROM thread_mounts WHERE backend_id = 'opencloud';
UPDATE threads
   SET main_cloud_backend        = NULL,
       main_cloud_session_handle = NULL,
       main_cloud_share_handle   = NULL,
       nc_session_folder         = NULL,
       nc_share_id               = NULL
 WHERE main_cloud_backend = 'opencloud';

-- Inert (the column is keyed per backend) but it is dead weight.
UPDATE users SET cloud_identity = cloud_identity - 'opencloud'
 WHERE cloud_identity ? 'opencloud';
```

Existing *jobs* need nothing: `job_cloud_baseline` reads the project row, and a
project with no handle logs "no cloud folder; skipping baseline seed (will run
as loose job)" rather than failing.

## 6. Verify

```bash
# Backend selection
kubectl --context=main -n superhuman-remote-worker \
  get cm srw-config -o jsonpath='{.data.MAIN_CLOUD_BACKEND}{"\n"}'   # nextcloud

# Pod + install state
kubectl --context=main -n superhuman-remote-worker \
  exec deploy/srw-nextcloud -- php /var/www/html/occ status
kubectl --context=main -n superhuman-remote-worker \
  logs deploy/srw-nextcloud | grep nc-setup
#   expect: groupfolders installed/updated/enabled, srw-agents group,
#           agent-service user, OIDC provider 'Keycloak' registered

# Edge
curl -sI https://cloud.srw.works/ | head -1        # 302 -> /login
curl -s  https://cloud.srw.works/status.php        # installed:true
```

Then walk the app path: open a project (expect a fresh Group Folder to be
provisioned), start a session (expect its mount to resolve), and confirm
`groupfolders` is at or above 20.1.2 — the protected-cloud RO mount refuses to
engage below that CVE floor (`check_version_floors` in
`orchestrator/services/cloud/ro_probe.py`). The setup hook runs
`occ app:update groupfolders` on every start for exactly this reason.

## Rollback

Revert the `values-experimental.yaml` block and let Fleet reconcile.
This restores OpenCloud's Deployment/Service/Ingress, but **not** its data —
the PVCs are gone after step 3, so OpenCloud comes back empty and re-inits.
Past step 5 the old handles are gone from the DB too. Realistically the
rollback window closes at step 3; after that, forward-fix.
