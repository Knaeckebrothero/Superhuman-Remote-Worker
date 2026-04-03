# Local K3s Deployment

Deploys the full SRW stack to a single-node K3s cluster using a [Kustomize](https://kustomize.io/) overlay. This replaces the Docker Compose setup and is the recommended way to run SRW locally for development, testing, or demos.

For production deployments, see the multi-cluster setup in `deployment/` (managed by Fleet/GitOps).

## Prerequisites

- **K3s** installed and running (`curl -sfL https://get.k3s.io | sh -`)
- **kubectl** configured to talk to the K3s cluster (`export KUBECONFIG=/etc/rancher/k3s/k3s.yaml`, or copy it to `~/.kube/config`)
- A `.env` file in the repo root (copy from `.env.example` and fill in API keys)

### Extend the NodePort range

K3s defaults to NodePort range 30000-32767. The local overlay exposes services on their natural ports (3000, 4000, 8085, etc.), which requires extending this range. This is a one-time setup:

```bash
echo 'kube-apiserver-arg:
  - "service-node-port-range=3000-32767"' | sudo tee /etc/rancher/k3s/config.yaml
sudo systemctl restart k3s
```

Wait ~30 seconds for K3s to come back, then verify:

```bash
kubectl get nodes   # Should show Ready
```

## Quick start

```bash
# 1. Create K8s secrets from your .env file
./deployment/local/create-secrets.sh

# 2. Build all app images, import into K3s, and deploy the full stack
./deployment/local/dev-redeploy.sh --apply

# 3. Watch pods come up
kubectl get pods -n superhuman-remote-worker -w
```

All 16 pods should reach `Running` / `Ready` within a few minutes. Databases start first, then Keycloak/Gitea, then the application services.

## Redeploying after code changes

The `dev-redeploy.sh` script rebuilds Docker images, imports them into K3s, and restarts the affected deployments.

```bash
# Rebuild everything
./deployment/local/dev-redeploy.sh

# Rebuild only specific components
./deployment/local/dev-redeploy.sh orchestrator
./deployment/local/dev-redeploy.sh cockpit agent

# Rebuild + re-apply kustomize (needed if you changed kustomization.yaml or patches)
./deployment/local/dev-redeploy.sh --apply
```

Components: `orchestrator`, `cockpit`, `agent`, `mcp`. The agent image is the largest (PyTorch + Playwright + Chromium) — expect ~5 min for a cold build, faster on subsequent builds due to Docker layer caching.

## Tear down

```bash
kubectl kustomize --load-restrictor=LoadRestrictionsNone deployment/local/ | kubectl delete -f -
```

This removes all resources but preserves PersistentVolumeClaims (your data). To also delete data:

```bash
kubectl delete pvc --all -n superhuman-remote-worker
```

## Accessing services

All user-facing services are exposed as NodePorts on localhost. No port-forwarding required.

| Service | URL | Purpose |
|---------|-----|---------|
| Cockpit | http://localhost:4000 | Web UI |
| Orchestrator API | http://localhost:8085 | REST API |
| Keycloak | http://localhost:8180 | SSO admin console |
| Gitea | http://localhost:3000 | Git server |
| MCP | http://localhost:8055 | MCP server |
| PgAdmin | http://localhost:5050 | PostgreSQL admin |
| Mongo Express | http://localhost:8081 | MongoDB admin |
| Neo4j Browser | http://localhost:7474 | Graph database UI |
| Dozzle | http://localhost:9999 | Live container logs |
| MinIO Console | http://localhost:9001 | S3 storage admin |
| Nextcloud | http://localhost:8800 | File storage |

Databases (PostgreSQL, MongoDB) are ClusterIP-only. If you need direct access for debugging:

```bash
kubectl port-forward svc/srw-postgres 5432:5432 -n superhuman-remote-worker
kubectl port-forward svc/srw-postgres-vector 5433:5432 -n superhuman-remote-worker
kubectl port-forward svc/srw-mongodb 27017:27017 -n superhuman-remote-worker
```

## How it works

### Kustomize overlay

The local deployment is a [Kustomize overlay](https://kubectl.docs.kubernetes.io/references/kustomize/glossary/#overlay) that reuses the production base manifests from `deployment/` and applies patches to adapt them for a single-node environment.

```
deployment/
  *.yaml                          # Base manifests (production)
  local/
    kustomization.yaml            # Overlay: references base + applies patches
    configmap.yaml                # Local ConfigMap (replaces 02-configmap.yaml)
    neo4j.yaml                    # Simplified Neo4j (replaces 14-neo4j.yaml)
    minio.yaml                    # Local MinIO (not in production base)
    create-secrets.sh             # Creates K8s secrets from .env
    dev-redeploy.sh               # Build, import, restart app images
    patches/
      agent.yaml                  # Remove Tailscale sidecar, single replica
      keycloak.yaml               # Local hostname, remove ProtonMail Bridge
      gitea.yaml                  # Local URLs for OIDC
      cockpit-env.yaml            # Localhost URLs in env.js
      orchestrator-schema.yaml    # Temp schema.sql fix (until image rebuild)
```

### What the overlay changes

| Concern | Production | Local |
|---------|-----------|-------|
| Secrets | Vault + External Secrets Operator | `.env` file via `create-secrets.sh` |
| Storage | Longhorn (replicated) | local-path (single node) |
| VPN sidecars | 3 FortiVPN containers | Removed |
| NATS | Hub + leaf across clusters | Removed (same-cluster provisioning) |
| Headscale | Cross-cluster mesh VPN | Removed (same network) |
| Ingress | TLS + cert-manager + Cloudflare | NodePort on localhost |
| Agent | 2 replicas + Tailscale sidecar | 1 replica, no sidecar |
| Neo4j | TLS + LoadBalancer | Plain bolt, NodePort |
| MinIO | External shared instance | Dedicated local pod |
| Fleet | GitOps auto-sync from git | Manual `kubectl apply` |

### Patch types used

The overlay uses two types of Kustomize patches:

**Strategic merge patches** (in `patches/` directory) modify specific fields in a resource by merging. They can add, replace, or delete (`$patch: delete`) fields. Used for complex changes like removing init containers or sidecars.

**JSON6902 patches** (inline in `kustomization.yaml`) use JSON Patch operations (`op: replace/add/remove` with a JSON pointer `path`). Used for targeted changes like swapping storage classes, setting imagePullPolicy, and changing service types to NodePort.

### Why `--load-restrictor=LoadRestrictionsNone`

By default, Kustomize prevents overlays from referencing files outside their directory (security restriction). Since `deployment/local/` references base manifests in the parent `deployment/` directory via `../`, the load restrictor must be disabled. This is standard practice for overlays that live alongside their base.

## Updating

After pulling new changes to the base manifests:

```bash
# Re-render and apply
kubectl kustomize --load-restrictor=LoadRestrictionsNone deployment/local/ | kubectl apply -f -
```

After changing `.env` values:

```bash
# Re-create secrets, then restart affected pods
./deployment/local/create-secrets.sh
kubectl rollout restart deployment -n superhuman-remote-worker
```

## Updating container images

The local overlay overrides the GHCR SHA tags with a `local` tag (via the `images:` section in `kustomization.yaml`). Use `dev-redeploy.sh` to build and import images.

To temporarily use GHCR images instead of local builds (e.g., to test a CI-built image), comment out the `images:` section in `kustomization.yaml` and ensure you have a pull secret:

```bash
kubectl create secret docker-registry ghcr-pull \
  --namespace=superhuman-remote-worker \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GITHUB_USER \
  --docker-password=YOUR_GITHUB_PAT
```

## Troubleshooting

**Pod stuck in Pending**: Check PVC status (`kubectl get pvc -n superhuman-remote-worker`). If a PVC is stuck, the local-path provisioner may need the node to have free disk space.

**Keycloak/Gitea not starting**: These depend on PostgreSQL. Verify Postgres is Running first. On first deploy, if Postgres crashes during init, the SSO databases may not be created. Fix manually:

```bash
kubectl exec -it srw-postgres-0 -n superhuman-remote-worker -- psql -U srw -c "
  CREATE ROLE keycloak WITH LOGIN PASSWORD 'keycloak';
  CREATE DATABASE keycloak OWNER keycloak;
  CREATE ROLE nextcloud WITH LOGIN PASSWORD 'nextcloud';
  CREATE DATABASE nextcloud OWNER nextcloud;
"
```

**Orchestrator CrashLooping**: Check logs (`kubectl logs -n superhuman-remote-worker deploy/srw-orchestrator`). Common cause: database connection issues. Verify `create-secrets.sh` was run and the secrets contain K8s service names (not localhost).

**NodePort not reachable**: Verify the K3s NodePort range was extended (see Prerequisites). Check with: `kubectl get svc -n superhuman-remote-worker` — services should show type `NodePort`.

## Related

- `docs/deployment.md` — Deployment strategy and Kustomize vs Helm comparison
- `deployment/` — Production base manifests (Fleet/GitOps)
- `.env.example` — Full environment variable documentation
