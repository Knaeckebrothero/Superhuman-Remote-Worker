# Helm Chart Migration Plan

## Context

The system needs to support multiple independent deployments with different update cadences, domains, and configurations. The current `deployment/` directory has hardcoded domains (`superhuman-remote-worker.com` in 8+ places), Vault paths, and infrastructure-specific details. This makes multi-instance deployment impossible without branching, and leaks private infrastructure topology if the repo is ever opened.

The main repo is now private (licensing model under consideration). Deployment artifacts must be distributable separately from source code.

## Target Instances

| Instance | Infrastructure | Update Cadence | Notes |
|----------|---------------|----------------|-------|
| Dev/demo | Home cluster, `superhuman-remote-worker` ns | Every commit on main (Fleet auto) | Current test/demo environment |
| Family | Home cluster, `srw-family` ns | Every commit on main (Fleet auto) | Separate data, shared infra |
| University | University cluster | Pinned chart version, manual | Student projects |
| Customer 1 | Customer's own infra | Pinned chart version, weekly-ish | |
| Customer 2 | Customer's own infra | Pinned chart version, weekly-ish | |

## Architecture: Three Repos

### 1. This Repo (private) — Source Code + Chart Source

All application source stays as-is. A Helm chart is added under `charts/superhuman-remote-worker/`. CI builds container images (already works) AND packages the Helm chart. Both are pushed to GHCR.

### 2. HomeLab Repo (private) — Fleet GitOps

Fleet bundle definitions pointing at the GHCR-hosted Helm chart. Per-instance values files for the home cluster deployments (dev/demo, family). Fleet watches this repo and reconciles.

Since Fleet's targeting model is cluster-level (not namespace-level), deploying to multiple namespaces on the same cluster requires **separate Fleet bundles** — one directory per instance:

```
HomeLab/
  deployments/
    srw-dev/
      fleet.yaml               # defaultNamespace: superhuman-remote-worker
      values.yaml
    srw-family/
      fleet.yaml               # defaultNamespace: srw-family
      values.yaml
```

Each directory gets its own `fleet.yaml` with its own `defaultNamespace`. The existing HomeLab GitRepo resource references both paths. This is a Fleet limitation — `targetCustomizations` works across clusters, not namespaces within one cluster.

### 3. Helm Chart Distribution (for customers/university)

Two options (decide later — both work with GHCR OCI):
- **Option A**: Grant customers GHCR pull access. They do `helm pull oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker --version 1.2.0`
- **Option B**: Separate public repo with chart source + README + example values for self-service

Customers manage their own deployments — we provide the chart, they provide their values.

## Helm Chart Structure

Target **Helm 4** (released November 2025). Key change: server-side apply is the default for new installs, which gives better conflict detection. Chart API version stays `v2` (unchanged).

```
charts/superhuman-remote-worker/
  Chart.yaml
  Chart.lock                         # Lock subchart versions, commit to git
  values.yaml                        # Generic defaults (no domains, no secrets)
  values.schema.json                 # Validates required values at install time
  templates/
    _helpers.tpl                     # Shared naming, labels, selectors
    NOTES.txt                        # Post-install instructions
    configmap.yaml                   # from 02-configmap.yaml (templated)
    external-secret.yaml             # from 01-eso.yaml (conditional)
    secret.yaml                      # Plain K8s secret (conditional, for non-Vault deployments)
    ingress.yaml                     # from 30-ingress.yaml
    init-job.yaml                    # Schema initialization (Helm hook)
    network-policy.yaml              # Per-namespace isolation (conditional)
  templates/orchestrator/
    deployment.yaml
    service.yaml
  templates/agent/
    deployment.yaml
    service.yaml
  templates/cockpit/
    deployment.yaml
    service.yaml
    cockpit-env-configmap.yaml
  templates/mcp/
    deployment.yaml
    service.yaml
  templates/databases/
    postgres.yaml                    # StatefulSet + Service (conditional)
    postgres-vector.yaml             # StatefulSet + Service (conditional)
    mongodb.yaml                     # StatefulSet + Service (conditional)
    neo4j.yaml                       # StatefulSet + Service (conditional)
  templates/services/
    gitea.yaml                       # StatefulSet + Service (conditional)
    keycloak.yaml                    # StatefulSet + Service (conditional)
    nextcloud.yaml                   # StatefulSet + Service (conditional)
  templates/optional/
    vpn.yaml                         # VPN sidecar deployments (conditional)
    # Headscale is NOT bundled — it's deployed out of band as a separate
    # Fleet bundle (HomeLab/deployments_managed/headscale/) so its
    # lifecycle is independent of the SRW release. The chart only
    # consumes its URL via .Values.headscale.url. See
    # docs/features/external_headscale.md.
  ci/
    test-values.yaml                 # Minimal values for CI testing
```

### Why Not Subcharts for Databases?

The Bitnami Helm charts (PostgreSQL, MongoDB, etc.) moved behind a Broadcom subscription in September 2025. The public versions are in a legacy repo with no updates or security patches. Our database templates are simple StatefulSets — writing them directly is safer than taking a dependency on a licensing-uncertain upstream. If HA/replication is needed later, CloudNativePG (CNCF project, free) is the better path for PostgreSQL.

Keycloak and Gitea have maintained official charts, but our deployments are simple enough that templated StatefulSets work fine and avoid subchart version management overhead.

### Template Conventions

**Naming**: Every resource name includes `{{ include "srw.fullname" . }}` to prevent collisions when multiple releases share a cluster. One resource per file.

**Namespaces**: Never hardcode `namespace:` in template metadata. Helm sets it via `--namespace` at install time. Only use `{{ .Release.Namespace }}` for explicit cross-namespace references.

**Labels**: All resources get standard labels via `_helpers.tpl`:
```yaml
app.kubernetes.io/name: {{ include "srw.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/component: {{ $component }}        # orchestrator, agent, etc.
```

**Quoting**: Always `{{ .Values.foo | quote }}` for env vars and any string value that might look like a boolean or number. YAML treats `true`, `false`, `yes`, `no`, bare numbers as non-strings.

**Required values**: Use `required` for critical fields instead of allowing silent empty deployment:
```yaml
value: {{ required "global.domain is required" .Values.global.domain }}
```

**Immutable selectors**: Lock down `selector.matchLabels` early — these cannot change between `helm upgrade` runs.

## Values Structure

```yaml
# values.yaml — generic defaults, no environment-specific values

global:
  domain: ""                          # REQUIRED — base domain (e.g. "example.com")
  # All subdomains derived: api.{domain}, auth.{domain}, git.{domain}, etc.
  imagePullSecrets: []                # For private GHCR access on customer clusters

image:
  orchestrator:
    repository: ghcr.io/knaeckebrothero/superhuman-remote-worker-orchestrator
    tag: latest
    pullPolicy: IfNotPresent
  agent:
    repository: ghcr.io/knaeckebrothero/superhuman-remote-worker-agent
    tag: latest
    pullPolicy: IfNotPresent
  cockpit:
    repository: ghcr.io/knaeckebrothero/superhuman-remote-worker-cockpit
    tag: latest
    pullPolicy: IfNotPresent
  mcp:
    repository: ghcr.io/knaeckebrothero/superhuman-remote-worker-mcp
    tag: latest
    pullPolicy: IfNotPresent
  workspace:
    repository: ghcr.io/knaeckebrothero/superhuman-remote-worker-workspace
    tag: latest
    pullPolicy: IfNotPresent
  vpn:
    repository: ghcr.io/knaeckebrothero/superhuman-remote-worker-vpn
    tag: latest
    pullPolicy: IfNotPresent

orchestrator:
  replicas: 1
  resources:
    requests:
      memory: "512Mi"
      cpu: "250m"
    limits:
      memory: "2Gi"
      cpu: "2000m"
  extraEnv: {}                        # Additional env vars as key-value map

agent:
  replicas: 2
  mode: dual                          # dual | worker | persistent
  resources:
    requests:
      memory: "1Gi"
      cpu: "500m"
    limits:
      memory: "4Gi"
      cpu: "4000m"

cockpit:
  replicas: 1

mcp:
  enabled: true
  replicas: 1

ingress:
  enabled: true
  className: traefik                  # traefik | nginx | ...
  tls:
    enabled: true
    issuerName: ""                    # cert-manager issuer name
  annotations: {}                     # Extra annotations per environment

# --- Secrets: three modes ---
# Mode 1: ESO (Vault-backed) — for our own cluster
# Mode 2: Chart-created Secret from values — for dev/testing only
# Mode 3: Pre-existing K8s Secret — for customers who manage secrets themselves
secrets:
  # Set exactly one of these three approaches:
  existingSecret: ""                  # Mode 3: name of pre-created K8s Secret
  create: false                       # Mode 2: if true, chart creates Secret from values below
  values:                             # Only used when create=true
    databaseUrl: ""
    vectorDbUrl: ""
    giteaAdminPassword: ""
    # ... other secret values

externalSecrets:
  enabled: false                      # Mode 1: ESO
  refreshInterval: "1h"
  secretStoreRef: ""                  # ClusterSecretStore name
  vaultPath: ""                       # e.g. "homelab/superhuman-remote-worker/srw-secrets"

# --- VPN sidecars (optional) ---
vpn:
  cluster:
    enabled: false
  research:
    enabled: false
  workstation:
    enabled: false

# --- Headscale mesh VPN (external — chart consumes URL only) ---
# Headscale itself is deployed out of band. Set the URL of an existing
# server here; agent pre-auth key flows in via Vault as TAILSCALE_AUTH_KEY.
# See docs/features/external_headscale.md.
headscale:
  url: ""

# Agent tailscale sidecar gate. Off by default — enable when the agent
# pods need to join a tailnet to reach KubeVirt VMs.
agent:
  tailscale:
    enabled: false

# --- Databases ---
# Set enabled=true + internal=true to deploy StatefulSets within the chart.
# Set enabled=true + internal=false to use an external database (provide externalUrl).
# Set enabled=false to skip entirely.
databases:
  postgres:
    enabled: true
    internal: true
    storageClass: ""                  # Empty = cluster default
    storageSize: "10Gi"
    externalUrl: ""                   # Used when internal=false
  vector:
    enabled: true
    internal: true
    storageClass: ""
    storageSize: "10Gi"
    externalUrl: ""
  mongodb:
    enabled: true
    internal: true
    storageClass: ""
    storageSize: "5Gi"
    externalUrl: ""
  neo4j:
    enabled: true
    internal: true
    storageClass: ""
    storageSize: "10Gi"
    externalUrl: ""

# --- Supporting services ---
gitea:
  enabled: true
  internal: true
  storageClass: ""
  storageSize: "10Gi"

keycloak:
  enabled: true
  internal: true
  storageClass: ""

nextcloud:
  enabled: true
  internal: true
  storageClass: ""
  storageSize: "20Gi"

# --- Email integration ---
email:
  enabled: false
  smtpHost: ""
  smtpPort: 587
  imapHost: ""
  imapPort: 993
  domain: ""
  from: ""

# --- S3-compatible object storage ---
s3:
  endpoint: ""                        # e.g. "http://minio.minio.svc:9000"

# --- NATS messaging ---
nats:
  url: ""                             # e.g. "nats://nats.nats.svc.cluster.local:4222"

# --- Network policies (for multi-tenant clusters) ---
networkPolicy:
  enabled: false                      # Restrict cross-namespace traffic

# --- Schema initialization ---
initJob:
  enabled: true                       # Run init.py as a post-install/post-upgrade hook
```

### Secret Reference Pattern (in templates)

All templates reference secrets the same way regardless of which mode is active:

```yaml
env:
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secrets.existingSecret | default (include "srw.fullname" .) }}
        key: database-url
```

When ESO is enabled, the ExternalSecret template creates a K8s Secret with the same name as the chart fullname. When `secrets.create` is true, the chart creates it directly. When `existingSecret` is set, it references whatever the user pre-created. All three modes produce a Secret with the same expected keys.

### Schema Initialization as Helm Hook

The current `init.py` (database schema setup) becomes a Helm hook Job:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: {{ include "srw.fullname" . }}-init
  annotations:
    "helm.sh/hook": post-install,post-upgrade
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation
```

This runs after install/upgrade, before the main pods start accepting traffic. The `before-hook-creation` policy deletes the previous Job before creating a new one on upgrade.

## Example: HomeLab Fleet Configuration

### srw-dev/fleet.yaml

```yaml
defaultNamespace: superhuman-remote-worker
helm:
  repo: "oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker"
  version: "0.1.0"
  releaseName: srw-dev
  takeOwnership: true
  valuesFiles:
    - values.yaml
```

### srw-dev/values.yaml

```yaml
global:
  domain: superhuman-remote-worker.com

image:
  orchestrator:
    tag: sha-bc02b09
  agent:
    tag: sha-bc02b09
  cockpit:
    tag: sha-bc02b09
  mcp:
    tag: sha-bc02b09

agent:
  replicas: 3
  mode: dual

ingress:
  className: traefik
  tls:
    enabled: true
    issuerName: cloudflare-dns-issuer

externalSecrets:
  enabled: true
  secretStoreRef: vault-backend
  vaultPath: homelab/superhuman-remote-worker/srw-secrets

vpn:
  cluster:
    enabled: true
  research:
    enabled: true
  workstation:
    enabled: true

# External headscale — server is its own deployment in HomeLab/.
headscale:
  url: "https://headscale.h4ll.app"

agent:
  tailscale:
    enabled: true

databases:
  postgres:
    storageClass: longhorn
  vector:
    storageClass: longhorn
  mongodb:
    storageClass: longhorn
  neo4j:
    storageClass: longhorn

email:
  enabled: true
  smtpHost: protonmail-bridge.protonmail-bridge.svc
  smtpPort: 587
  imapHost: protonmail-bridge.protonmail-bridge.svc
  imapPort: 993
  domain: superhuman-remote-worker.com
  from: srw@superhuman-remote-worker.com

s3:
  endpoint: http://minio.minio.svc:9000

nats:
  url: nats://nats.nats.svc.cluster.local:4222
```

### srw-family/fleet.yaml

```yaml
defaultNamespace: srw-family
helm:
  repo: "oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker"
  version: "0.1.0"
  releaseName: srw-family
  takeOwnership: true
  valuesFiles:
    - values.yaml
```

### srw-family/values.yaml

```yaml
global:
  domain: family.example.com

image:
  orchestrator:
    tag: sha-bc02b09
  agent:
    tag: sha-bc02b09
  cockpit:
    tag: sha-bc02b09

agent:
  replicas: 1
  mode: dual

ingress:
  className: traefik
  tls:
    enabled: true
    issuerName: cloudflare-dns-issuer

externalSecrets:
  enabled: true
  secretStoreRef: vault-backend
  vaultPath: homelab/superhuman-remote-worker/srw-family-secrets

vpn:
  cluster:
    enabled: false
  research:
    enabled: false
  workstation:
    enabled: false

# External headscale — leave url empty to skip the sidecar entirely.
headscale:
  url: ""

agent:
  tailscale:
    enabled: false

email:
  enabled: false

databases:
  postgres:
    storageClass: longhorn
  vector:
    storageClass: longhorn
  mongodb:
    storageClass: longhorn
  neo4j:
    enabled: false
```

### Example: Customer Values (they fill this in)

```yaml
global:
  domain: ai.customer-company.com
  imagePullSecrets:
    - name: ghcr-pull-secret           # They create this with the GHCR PAT we provide

image:
  orchestrator:
    tag: sha-abc1234                    # Pinned to specific release
  agent:
    tag: sha-abc1234
  cockpit:
    tag: sha-abc1234

agent:
  replicas: 2

ingress:
  className: nginx
  tls:
    enabled: true
    issuerName: letsencrypt-prod

# Customer manages their own secrets
secrets:
  existingSecret: srw-secrets           # They create this manually with expected keys

databases:
  postgres:
    internal: false
    externalUrl: "postgres://..."       # Their managed DB
  vector:
    internal: true
  mongodb:
    internal: true
  neo4j:
    enabled: false

# Most optional components disabled
vpn:
  cluster:
    enabled: false
  research:
    enabled: false
  workstation:
    enabled: false
headscale:
  url: ""
agent:
  tailscale:
    enabled: false
mcp:
  enabled: false
```

## Fleet Authentication for Private GHCR

Fleet uses `helmSecretName` on the GitRepo resource (not in fleet.yaml). The secret must be `kubernetes.io/basic-auth` type in the `fleet-default` namespace.

```bash
# Create a GitHub PAT with read:packages scope
kubectl create secret generic ghcr-helm-secret \
  -n fleet-default \
  --type=kubernetes.io/basic-auth \
  --from-literal=username=knaeckebrothero \
  --from-literal=password=ghp_YOUR_PAT
```

Reference it in the GitRepo:

```yaml
apiVersion: fleet.cattle.io/v1alpha1
kind: GitRepo
metadata:
  name: srw-deployment
  namespace: fleet-default
spec:
  repo: https://git.example.com/knaeckebrothero/HomeLab.git
  branch: main
  paths:
    - deployments/srw-dev
    - deployments/srw-family
  helmSecretName: ghcr-helm-secret
  helmRepoURLRegex: "oci://ghcr\\.io/knaeckebrothero/.*"
```

**Important**: `helmSecretName` credentials are sent to ALL Helm repos in that GitRepo by default. The `helmRepoURLRegex` field restricts them to matching URLs only — always set this to avoid leaking the PAT to public chart repos.

Since we use Vault + ESO, the PAT itself can be synced from Vault into `fleet-default` via an ExternalSecret, keeping it out of git entirely.

## Fleet Gotchas

- **`chart` field must be empty for OCI repos.** When using `helm.repo: oci://...`, omit the `chart` field entirely. Setting both causes silent failure — no bundle is created, no error in logs.
- **valuesFiles merging is shallow.** Fleet merges values at the top level only, not deep-merge. If both a base file and overlay file define `orchestrator:`, the entire object from the overlay replaces the base. Structure values files accordingly — don't split a single component's config across files expecting deep merge.
- **No auto-polling for new chart versions in GitRepo mode.** Fleet only re-evaluates when the git content changes. Our CI handles this by committing updated tags to the HomeLab repo.
- **Silent failures are common.** When OCI config is wrong, Fleet often fails silently. After changes, always verify: `kubectl get bundles -n fleet-default`.

## CI Changes

### Chart Publishing (`.github/workflows/production.yml`)

Add after image builds:

```yaml
  publish-chart:
    needs: [lint, test]               # Chart can publish in parallel with image builds
    runs-on: ubuntu-latest
    permissions:
      packages: write
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: azure/setup-helm@v4

      - name: Login to GHCR
        run: echo "${{ secrets.GITHUB_TOKEN }}" | helm registry login ghcr.io -u ${{ github.actor }} --password-stdin

      - name: Lint chart
        run: helm lint charts/superhuman-remote-worker

      - name: Package chart
        run: helm package charts/superhuman-remote-worker

      - name: Push chart to GHCR
        run: helm push superhuman-remote-worker-*.tgz oci://ghcr.io/knaeckebrothero/charts
```

### Auto-Deploy to HomeLab (cross-repo update)

```yaml
  update-homelab:
    needs: [publish-chart, build-orchestrator, build-agent, build-cockpit, build-mcp]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          repository: knaeckebrothero/HomeLab
          token: ${{ secrets.HOMELAB_PAT }}

      - name: Update image tags for dev and family instances
        run: |
          SHA=$(echo "${{ github.sha }}" | cut -c1-7)
          sed -i "s/tag: sha-.*/tag: sha-${SHA}/" deployments/srw-dev/values.yaml
          sed -i "s/tag: sha-.*/tag: sha-${SHA}/" deployments/srw-family/values.yaml

      - name: Commit and push
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add .
          git commit -m "deploy: update SRW image tags to sha-${SHA}" || echo "No changes"
          git push
```

This commit to HomeLab triggers Fleet reconciliation, which deploys the new images.

### Chart Versioning Strategy

- **Releases**: Bump `version` in `Chart.yaml` manually following semver (`1.0.0`, `1.1.0`, `2.0.0`). Tag the git commit as `v1.0.0`. CI publishes the chart on tag push.
- **Dev builds**: CI also publishes a `0.0.0-sha.XXXXXXX` pre-release version on every main commit. The HomeLab values files can reference either the semver release or the dev SHA version.
- **OCI tags**: There is no "latest" concept for Helm OCI charts — `--version` is mandatory on install/pull. Always specify an explicit version.

### Chart Testing in CI

```yaml
  chart-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: azure/setup-helm@v4

      # Syntax + schema validation
      - name: Lint
        run: helm lint charts/superhuman-remote-worker

      - name: Template with test values
        run: |
          helm template test-release charts/superhuman-remote-worker \
            -f charts/superhuman-remote-worker/ci/test-values.yaml \
            --debug

      # Validate generated YAML against K8s API schemas
      - name: Install kubeconform
        run: |
          curl -sL https://github.com/yannh/kubeconform/releases/latest/download/kubeconform-linux-amd64.tar.gz \
            | tar xz -C /usr/local/bin

      - name: Validate
        run: |
          helm template test-release charts/superhuman-remote-worker \
            -f charts/superhuman-remote-worker/ci/test-values.yaml \
            | kubeconform -kubernetes-version 1.31.0 -summary -strict

      # Unit tests (optional but recommended)
      - name: Install helm-unittest
        run: helm plugin install https://github.com/helm-unittest/helm-unittest

      - name: Run unit tests
        run: helm unittest charts/superhuman-remote-worker
```

## What Changes in This Repo

### Removed
- `deployment/fleet.yaml` — moves to HomeLab
- All hardcoded domains, Vault paths, infra details from deployment manifests

### Added
- `charts/superhuman-remote-worker/` — the Helm chart (templates, values, schema)
- CI jobs for chart linting, testing, packaging, and GHCR push
- CI job for cross-repo HomeLab tag updates

### Kept As-Is
- `deployment-local/` — coexists during transition. Eventually replaced by a `values-local.yaml` in the chart + docker-compose for non-K8s dev.
- `docker-compose*.yaml` — stays for non-K8s development workflows
- All source code, tests, application configs — unchanged

### Migration of Current `deployment/`

Each existing manifest becomes a Helm template. The transformation:

1. Replace hardcoded values with `{{ .Values.x.y }}` or `{{ include "srw.fullname" . }}` references
2. Wrap optional components in `{{- if .Values.component.enabled }}`
3. Derive subdomain-based URLs: `api.{{ .Values.global.domain }}`, `auth.{{ .Values.global.domain }}`
4. Support all three secret modes via conditionals
5. Make storage classes, resource limits, replica counts configurable
6. Remove `namespace:` from all resource metadata
7. Add standard labels via `_helpers.tpl` includes

The numbered file naming (00-, 10-, 20-) is dropped. Helm handles resource ordering natively (Namespace -> ServiceAccount -> ConfigMap -> Secret -> Deployment -> Service -> Ingress). For the schema init job, use Helm hooks with weight annotations.

## Execution Steps

1. **Create chart skeleton**: `Chart.yaml`, `values.yaml`, `values.schema.json`, `_helpers.tpl`, `NOTES.txt`
2. **Template all manifests**: Convert each `deployment/*.yaml` into `templates/**/*.yaml`
3. **Validate locally**: `helm lint` + `helm template` with test values
4. **Add CI steps**: Chart lint, test, package, GHCR push
5. **Test install**: Deploy to a test namespace on the home cluster with `helm install`
6. **Set up HomeLab Fleet bundles**: `srw-dev/` and `srw-family/` directories with fleet.yaml + values
7. **Create GHCR auth secret**: In `fleet-default` namespace for Fleet to pull private OCI chart
8. **Cut over**: Update HomeLab GitRepo to point at the new bundle paths. Fleet deploys via Helm chart.
9. **Verify**: Both namespaces running independently, no cross-contamination
10. **Clean up**: Archive old `deployment/` directory (keep `deployment-local/` for now)

## Open Questions

- **Customer image access**: Grant GHCR pull access per-customer via GitHub org invite, or set up a pull-through cache on their side?
- **Database separation for family instance**: Separate StatefulSets per namespace (clean, more resources) vs shared PostgreSQL with different databases (efficient, couples lifecycle)?
- **Chart distribution**: Start with GHCR OCI only. Evaluate whether a separate public chart repo is needed once the first customer is onboarded.
- **Image tag strategy refinement**: The cross-repo sed approach works but is fragile. Consider Renovate Bot or a dedicated GitHub Action for multi-repo image tag syncing if it becomes a maintenance burden.
