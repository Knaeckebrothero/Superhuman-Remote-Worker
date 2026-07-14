---
tags:
  - operations
  - dynamic-canvas
  - postgres
  - security
related:
  - "[[dynamic_canvas]]"
  - "[[dynamic_canvas_slice3c_verification]]"
---

# Dynamic Canvas gateway database provisioning

This runbook provisions the dedicated PostgreSQL login and Kubernetes Secret
used by the public Dynamic Canvas gateway. It is an initial-production
provisioning procedure, not permission to enable the viewer: the hosted edge,
PSL, raw-path, and browser acceptance gates remain independent.

Production Helm renders never receive an application-database administrator
credential and never create this role. The operator applies the packaged
least-privilege SQL out of band, then points
`canvas.livePreview.viewer.database.credentials.existingSecret` at a Secret
containing only the restricted username and password.

## Preconditions

- Application migrations through `0062_canvas_bootstrap_exchange.sql` are
  complete on the exact target database.
- The database owner has revoked `CREATE` on schema `public` from `PUBLIC`, and
  any nonstandard database-level `CREATE` grant from `PUBLIC`. The reconciler
  and gateway both fail closed if the restricted role still receives either
  capability, including through `PUBLIC`.
- `psql` can authenticate as the database owner/admin through explicit libpq
  variables. Do not place its password in a command-line database URL.
- For direct Secret creation, `kubectl` has the target context and namespace,
  and both are named explicitly. The script never uses the implicit current
  context and never creates a namespace.

## Prepare the restricted credential

Create the password in a private file. This example writes it without showing
it in the terminal or shell history:

```bash
umask 077
install -d -m 700 "$HOME/.config/srw"
openssl rand -hex 32 > "$HOME/.config/srw/canvas-gateway-password"
chmod 600 "$HOME/.config/srw/canvas-gateway-password"
```

Store or import the same value into the production secret manager before
removing the local file. Do not commit the file or add these credentials to the
shared SRW application Secret.

Set explicit targets. Use `PGPASSFILE`, an existing service definition, or a
temporarily exported `PGPASSWORD` for the database-owner authentication; the
Canvas password remains file-backed.

```bash
export PGHOST=postgres.internal.example
export PGPORT=5432
export PGDATABASE=srw
export PGUSER=srw_database_owner
export CANVAS_VIEWER_POSTGRES_PASSWORD_FILE="$HOME/.config/srw/canvas-gateway-password"
export CANVAS_VIEWER_POSTGRES_USER=srw_canvas_gateway
```

## Preflight and provision

The command without an apply flag checks database connectivity and the exact
Slice-3 schema marker without changing state:

```bash
./scripts/provision-canvas-gateway-database.sh
```

For a secret-manager/ExternalSecret deployment, reconcile and verify only the
database role, then create the dedicated Kubernetes Secret through that
operator:

```bash
./scripts/provision-canvas-gateway-database.sh --apply
```

The homelab deployment uses this path. Its operator-owned manifest is
`HomeLab/deployments_managed/canvas-edge/15-external-secret.yaml`; it maps only
the `username` and `password` properties from the dedicated Vault KV path
`homelab/superhuman-remote-worker/canvas-gateway-db` into
`srw-canvas-gateway-db`. Populate that path with the same password file used by
the role reconciler through the Vault UI or another secret-safe operator
workflow before Fleet reconciles it. Do not place the gateway credential in the
shared `srw-secrets` bundle and do not use `--apply-secret` when ESO owns the
target Secret.

For an operator-managed native Kubernetes Secret, set an explicit target and
apply both surfaces:

```bash
export KUBE_CONTEXT=production-cluster
export KUBE_NAMESPACE=srw
export CANVAS_VIEWER_SECRET_NAME=srw-canvas-gateway-db
./scripts/provision-canvas-gateway-database.sh --apply-secret
```

The workflow:

1. verifies the exact migrated viewer schema;
2. applies `helm/files/canvas-viewer-role.sql` as the database owner;
3. removes stale direct relation, sequence, schema, and database grants;
4. restores only the documented column allowlist, database `CONNECT`, and
   `public` schema `USAGE`;
5. reconnects as the restricted login and verifies its authenticated identity
   plus the database/schema create boundary; and
6. when requested, sends the Secret to the API through a pipe using private
   temporary files. Neither password is put in argv or printed.

The gateway performs the complete effective-privilege attestation again before
serving, including exact column grants, unrelated relations, sequences,
memberships, elevated attributes, and switched roles.

## Helm values

Reference the dedicated Secret using the exact keys created above:

```yaml
canvas:
  livePreview:
    viewer:
      database:
        username: srw_canvas_gateway
        credentials:
          create: false
          existingSecret: srw-canvas-gateway-db
          usernameKey: username
          passwordKey: password
        provisionRole: false
```

Production validation rejects `credentials.create=true`,
`provisionRole=true`, an empty existing Secret, or reuse of the shared
application Secret.

Verify only Secret metadata, never its `.data` values:

```bash
kubectl --context "$KUBE_CONTEXT" --namespace "$KUBE_NAMESPACE" \
  get secret "$CANVAS_VIEWER_SECRET_NAME" \
  -o jsonpath='{.metadata.name}{"\n"}'
```

For the homelab ESO path, also require a Ready condition without displaying
Secret data:

```bash
kubectl --context main --namespace superhuman-remote-worker \
  wait externalsecret/srw-canvas-gateway-db \
  --for=condition=Ready --timeout=2m
```

## Rotation and rollback

The initial dark deployment has no live database connections, so provisioning
the role before the Secret is safe. A later password rotation is not atomic
across PostgreSQL, a secret manager, Kubernetes projection, and gateway
replicas. Disable public admission or use a maintenance window, update the role
and Secret with the same credential, wait for every gateway replica to restart
and pass startup attestation, then restore admission. Do not delete the old
Secret or role while a gateway replica is still running.

If provisioning fails, keep the viewer disabled. The SQL transaction rolls
back grant changes on failure; rerunning with the same password file is
idempotent. A failed Kubernetes apply leaves the already-valid database role in
place, so fix the context/Secret-manager error and rerun rather than generating
a different credential blindly.

## Local k3d

The normal local overlay keeps the viewer disabled, so no Canvas gateway role,
credential Secret, Job, or Deployment is expected. When a development-only
edge test is intentionally enabled against bundled PostgreSQL, use the chart's
`credentials.create=true` plus `provisionRole=true` path instead of pretending
the local cluster is a production operator-managed deployment.
