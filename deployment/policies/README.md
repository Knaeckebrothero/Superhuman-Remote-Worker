# Object-store access policies

Policies for the scoped credentials the chart consumes. They are documentation
and a copy/paste source — nothing applies them automatically, because the
buckets live outside the release (and, on dev, outside the cluster's ownership).

## `minio-pgbackup-policy.json` — CloudNativePG backups

The credential behind `BACKUP_S3_ACCESS_KEY_ID` / `BACKUP_S3_SECRET_ACCESS_KEY`.

**Why it is this narrow.** The key is mounted into every Postgres instance. A
compromised database pod holds it, so it must not be able to reach anything
else: not `srw-snapshots`, not `srw-workspaces`, and not the backups of a
different environment sharing the bucket.

**Why `DeleteObject` is present anyway.** Barman enforces `retentionPolicy` by
deleting expired backups and WAL segments. Without delete the bucket grows
forever and retention silently does nothing. This is the one permission that
makes the credential dangerous, and it is why the prefix scoping matters —
delete is confined to `dev/*`.

**What it deliberately withholds:** `s3:DeleteBucket`, any `PutBucket*`, any
policy or versioning verb, and `ListBucket` outside the prefix. The key cannot
discover what else the bucket holds.

### Applying it (MinIO)

```bash
mc alias set homelab https://<minio-endpoint> <admin-key> <admin-secret>
mc mb --ignore-existing homelab/srw-pgbackup

mc admin policy create homelab srw-pgbackup-dev deployment/policies/minio-pgbackup-policy.json
mc admin user add homelab <access-key-id> <secret-access-key>
mc admin policy attach homelab srw-pgbackup-dev --user <access-key-id>
```

Then put the pair in Vault at `homelab/superhuman-remote-worker/srw-secrets` as
`BACKUP_S3_ACCESS_KEY_ID` and `BACKUP_S3_SECRET_ACCESS_KEY`. ESO extracts that
path wholesale, so no chart change carries them.

### Verifying the scoping actually holds

A policy that is too permissive looks identical to a correct one until the day
it matters. Check the negative cases, not just the positive one:

```bash
mc alias set backup-test https://<minio-endpoint> <access-key-id> <secret-access-key>

mc ls backup-test/srw-pgbackup/dev/                 # expect: works
echo hello | mc pipe backup-test/srw-pgbackup/dev/canary   # expect: works
mc rm backup-test/srw-pgbackup/dev/canary           # expect: works (retention needs it)

mc ls backup-test/srw-snapshots/                    # expect: DENIED
mc ls backup-test/srw-workspaces/                   # expect: DENIED
mc ls backup-test/srw-pgbackup/                     # expect: DENIED (outside the prefix)
mc rb backup-test/srw-pgbackup                      # expect: DENIED
```

### Per environment

The prefix is `dev/`, matching `databases.backup.destinationPath:
s3://srw-pgbackup/dev`. A second environment gets its **own key and its own
policy** with its own prefix — sharing one key across environments means a
compromise in either reaches both, and `DeleteObject` is in this policy.

Barman derives the per-cluster folder from the cluster name underneath that
prefix, so all five databases share one key safely: they are separated by path,
not by credential.

### On AWS S3 rather than MinIO

The same document works as an IAM policy. Add a `Deny` on
`s3:DeleteObjectVersion` and enable bucket versioning with object lock if you
want backups that survive a compromised database pod deliberately deleting
them — MinIO supports object locking too, and it is the honest answer to
"what if the key leaks".
