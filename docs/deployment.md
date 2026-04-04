# Deployment Strategy

**Status:** Active  
**Last updated:** 2026-04-04

## Summary

The project supports three deployment tiers. All are actively maintained.

1. **Docker Compose** — Full stack in containers with static workspace/agent pools. For development, small deployments, and environments without Kubernetes. See [`docs/docker_compose_mode.md`](docker_compose_mode.md) for the full design.
2. **Kubernetes (single-cluster)** — All services on one K3s node via Kustomize overlay (`deployment-local/`). For testing, demos, and small production deployments.
3. **Kubernetes (multi-cluster)** — Dedicated nodes with Fleet, Vault, Longhorn, and cross-cluster networking (`deployment/`). For production.

## Why Kubernetes is recommended for production

Kubernetes provides dynamic provisioning capabilities that Docker Compose handles via static pools:

| Capability | Kubernetes | Docker Compose |
|------------|-----------|----------------|
| **Agent scaling** | Dynamic pod creation via `CoreV1Api` | Fixed `deploy.replicas` in compose file |
| **Workspace isolation** | Per-job pods created/deleted on demand | Static pool of containers, recycled between jobs |
| **Persistent sessions** | On-demand agent pods per thread | Fixed pool of persistent agents, reassigned between sessions |
| **VM workspaces** | KubeVirt VMs via NATS or direct API | QEMU-in-Docker containers (requires `/dev/kvm`) |
| **Cross-cluster VMs** | NATS + KubeVirt + Headscale mesh | Not supported (same-host only) |
| **Service discovery** | K8s DNS + Services + NetworkPolicies | Docker Compose embedded DNS |

Docker Compose cannot dynamically scale or provision infrastructure, but it provides a fully functional system for smaller deployments where the fixed pool model is sufficient.

## Current state

### Docker Compose (supported)

The orchestrator auto-detects whether Kubernetes is available. When the k8s API is unreachable, it uses `DockerProvisioner` to assign pre-existing workspace containers from a static pool instead of creating pods on demand. See [`docs/docker_compose_mode.md`](docker_compose_mode.md) for architecture details.

| File | Purpose |
|------|---------|
| `docker-compose.yaml` | Full stack with GHCR images, workspace containers, SSH keygen, static agent pools. |
| `docker-compose.local.yaml` | Same but builds images locally. |
| `docker-compose.dev.yaml` | Databases + supporting services only. Orchestrator/agent run on the host for debugging. |

### Kubernetes — Multi-cluster (production)

Raw YAML manifests in `deployment/`, synced to the production cluster by Fleet (Rancher GitOps). Secrets managed by HashiCorp Vault via External Secrets Operator.

Infrastructure dependencies not deployed by SRW:

| Service | Purpose |
|---------|---------|
| Fleet | GitOps — watches `deployment/` in git, applies changes automatically |
| Vault + ESO | Secrets — `ExternalSecret` CRDs pull from Vault into K8s Secrets |
| Longhorn | Replicated block storage across nodes |
| cert-manager | TLS certificates via Cloudflare DNS challenge |
| Traefik | Ingress controller with TLS termination |
| MetalLB | LoadBalancer IPs for direct access (e.g., Neo4j Bolt) |
| NATS | Cross-cluster messaging for VM lifecycle |
| Headscale | Mesh VPN for agent-to-VM SSH across clusters |
| MinIO | S3-compatible storage for snapshots and IDE sessions |
| Cloudflare Tunnel | Public internet access without exposing nodes |

Recommended minimum: 6 nodes (3 per cluster) for HA.

### Kubernetes — Single-cluster (prototype, `deployment-local/`)

A Kustomize overlay that deploys the full stack to a single K3s node. Created 2026-04-03 as a proof of concept. Works but uses `--load-restrictor=LoadRestrictionsNone` and includes a temporary schema fix workaround.

What it changes from production:

| Concern | Production | Single-cluster |
|---------|-----------|----------------|
| Secrets | Vault + ESO | `.env` file via `create-secrets.sh` |
| Storage | Longhorn (replicated) | local-path (single node) |
| VPN sidecars | 3 FortiVPN containers | Skipped (direct LLM access) |
| NATS | Hub + leaf across clusters | Skipped (same-cluster provisioning) |
| Headscale/Tailscale | Cross-cluster mesh VPN | Skipped (same network) |
| Ingress | TLS + cert-manager + Cloudflare | Port-forward or plain Traefik |
| Agents | 2 replicas + PDB | 1 replica |
| Neo4j | TLS + LoadBalancer | Plain bolt, ClusterIP only |
| MinIO | Shared cluster MinIO | Dedicated pod |
| Fleet | GitOps sync | Manual `kubectl apply` |

## Migration plan

### Phase 1: Stabilize single-cluster option (current)

The `deployment-local/` Kustomize overlay works. Immediate cleanup:
- Remove the temporary schema ConfigMap workaround (rebuild images with fixed schema.sql)
- Add GHCR pull secret setup to `create-secrets.sh` or document image import
- Test the full job lifecycle (create job → agent picks up → workspace pod → delivery)
- Write a setup guide in `deployment-local/README.md`

### Phase 2: Evaluate packaging format

The base manifests need to support two deployment profiles cleanly. Three options:

#### Option A: Kustomize overlays (current)

Each profile is an overlay directory that patches a shared set of base manifests.

```
deployment/
  *.yaml                    # Base manifests (production)
  local/                    # Single-cluster overlay
    kustomization.yaml      # References ../*.yaml, applies patches
    patches/
```

**Pros:**
- Already working — the prototype is built
- Built into kubectl, no extra tooling
- Fleet supports Kustomize natively (`kustomize.dir` in `fleet.yaml`)
- Base manifests remain readable plain YAML

**Cons:**
- Overlays are diffs, not declarations. As profiles diverge, patches become hard to read ("what does the single-cluster Neo4j actually look like?" requires mentally applying 3 patches)
- No conditionals — can't express "if NATS is enabled, include these resources." Instead you include/exclude entire files
- Load restrictor workaround needed when overlay references parent directory files
- Adding a third profile (e.g., "single-cluster with VPN") means another overlay directory with duplicated patches

**Best for:** Two profiles that share 90%+ of their manifests with minor differences.

#### Option B: Helm chart

A single chart with `values.yaml` files per profile.

```
chart/
  Chart.yaml
  values.yaml               # Defaults (single-cluster)
  values-production.yaml    # Production overrides
  templates/
    _helpers.tpl
    postgres.yaml
    orchestrator.yaml
    agent.yaml
    nats.yaml               # {{- if .Values.nats.enabled }}
    vpn.yaml                # {{- if .Values.vpn.enabled }}
    headscale.yaml           # {{- if .Values.headscale.enabled }}
    ...
```

**Pros:**
- Single source of truth — every deployment option is a values file, not a patch stack
- Conditionals: `{{- if .Values.nats.enabled }}` cleanly includes/excludes entire resources
- Fleet supports Helm natively (the `fleet.yaml` already has `helm:` configuration)
- Standard distribution format — `helm install srw ./chart -f values-single.yaml` is the universal Kubernetes install UX
- Easier for external users ("company without K8s") to customize — they edit a values file, not YAML patches
- Helm's `--set` flag allows one-off overrides without files
- Rollback support (`helm rollback`)

**Cons:**
- Significant upfront migration — every manifest must be converted to Go templates with `{{ .Values.x }}` references
- Go templates are harder to read and debug than plain YAML. Whitespace handling is notoriously tricky
- The base manifests stop being valid YAML — you can't `kubectl apply` a template directly
- Overkill if there are genuinely only two profiles that won't diverge further
- Helm's dependency model (subcharts) adds complexity if third-party charts are pulled in (e.g., NATS Helm chart)

**Best for:** Multiple deployment profiles with clear feature toggles, especially if external users need to customize deployments.

#### Option C: Hybrid (Helm chart + Fleet Kustomize)

Use Helm as the packaging format. Fleet applies it to production with `fleet.yaml` pointing to the chart. Single-cluster users install with `helm install`. The `deployment/` directory becomes the chart.

Fleet already supports this pattern — `fleet.yaml` can specify `helm.chart`, `helm.releaseName`, and `helm.valuesFiles`. Fleet's `targetCustomizations` can apply different values per cluster label.

```yaml
# fleet.yaml
helm:
  chart: .
  releaseName: srw
  valuesFiles:
    - values-production.yaml
targetCustomizations:
  - name: single-cluster
    clusterSelector:
      matchLabels:
        profile: single-cluster
    helm:
      valuesFiles:
        - values-single.yaml
```

**Pros:** Combines Helm's templating power with Fleet's GitOps workflow. Production uses Fleet to render the chart; dev uses `helm install` directly.

**Cons:** Same Go template downsides as Option B. Adds conceptual complexity (is the source of truth the chart or the values file?).

### Recommendation

If the project stays internal with two profiles, **Kustomize (Option A)** is sufficient and already working. If external deployment becomes a real requirement (the "company without K8s" scenario, open-source distribution, or more than 2 profiles), migrating to a **Helm chart (Option B/C)** is worth the upfront cost.

The decision doesn't need to be made now. The Kustomize overlay is functional. If it starts feeling painful (too many patches, hard to reason about final state), that's the signal to convert to Helm.

### Phase 3: Docker Compose as supported deployment tier

> **Decision reversed (2026-04-04).** Docker Compose is no longer being deprecated.
> It remains a supported deployment mode alongside Kubernetes. See
> [`docs/docker_compose_mode.md`](docker_compose_mode.md) for the full design.

The orchestrator auto-detects whether it's running on Kubernetes (k8s API reachable)
or Docker Compose (k8s unavailable) and adjusts its provisioning behavior:

- **Kubernetes:** Dynamic workspace/VM/agent pod provisioning (current behavior)
- **Docker Compose:** Fixed pool of workspace containers and agents defined in the
  compose file, recycled between jobs via S3 snapshot/restore

This gives smaller deployments a fully functional system without a k8s cluster, while
production deployments retain dynamic scaling and KubeVirt VM support.

## Access control

### Local K3s cluster
Full read-write access. This is the development cluster.

### Production Rancher clusters
Read-only access from developer workstations. All changes go through git → Fleet. The Rancher tokens in `~/.kube/config` should be scoped to a read-only role (Rancher's built-in "Read-Only" cluster role or a custom role with only get/list/watch permissions).

To configure: In the Rancher UI, change the user's cluster role from "Cluster Owner" to "Read-Only" on each of the 3 production clusters (rancher-main, rancher-vms, rancher-local). The existing kubeconfig tokens automatically inherit the reduced permissions.

Admin access for emergency operations should require logging into the Rancher UI directly, creating an audit trail.

## Related documents

- `docs/deployment_roadmap.md` — Production deployment log (2026-03-19/20)
- `docs/deployment_checklist.md` — Original deployment vision
- `docs/issues/local_e2e_testing.md` — Local testing gaps (workspace/VM provisioning)
- `deployment-local/` — Single-cluster Kustomize overlay (prototype)
- `deployment/deploy.sh` — Image tag update script for Fleet deployments
