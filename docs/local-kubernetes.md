# Local Kubernetes with k3d

This guide runs the SRW Helm chart on a single-node k3d cluster. It exercises
the same Kubernetes provisioning, ingress, identity, agent, and workspace paths
used by a larger deployment, with development credentials and non-HA storage.

This is an evaluation and development topology, not a production deployment.
The values file contains published credentials, the databases are single-node,
and the bundled object store has no independent failure domain.

## Prerequisites

The maintained helper targets Linux. Allocate at least:

- 8 vCPU;
- 16 GiB RAM available to the cluster;
- enough local disk for container images and persistent volumes; and
- host ports 80, 443, and 5005.

Install these tools on the host:

- Docker Engine
- kubectl
- Helm 3.12 or newer
- k3d
- mkcert
- OpenSSL
- `ssh-keygen`

Use each project's installation instructions for your distribution. On Fedora,
the core packages can be installed with:

```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager addrepo \
  --from-repofile=https://download.docker.com/linux/fedora/docker-ce.repo
sudo dnf -y install \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

sudo dnf -y install kubernetes-client helm mkcert nss-tools openssl
```

Start a new login shell after changing Docker group membership. Install k3d
from the [official instructions](https://k3d.io/stable/#installation).

Create and trust one local certificate authority. The second command installs
the same CA into the system trust store rather than creating another root-owned
CA:

```bash
mkcert -install
sudo env CAROOT="$(mkcert -CAROOT)" mkcert -install
```

Sanity-check the tools before continuing:

```bash
docker run --rm hello-world
k3d version
kubectl version --client
helm version
mkcert -CAROOT
```

## 1. Clone and configure

```bash
git clone https://github.com/Knaeckebrothero/Superhuman-Remote-Worker.git
cd Superhuman-Remote-Worker

cp deployment/values-local.yaml.example deployment/values-local.yaml
$EDITOR deployment/values-local.yaml
```

Add at least one of `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `GROQ_API_KEY`
under `secrets.values`. A Tavily key is optional and enables the corresponding
web-search tools.

The copied file is gitignored because it may contain real provider keys. Its
remaining credentials are fixed development values. Never reuse it for a
deployment reachable by other people.

The local overlay deliberately selects:

- the chart's non-HA, single-replica database posture;
- Nextcloud as the local cloud backend;
- chart-managed development secrets;
- on-demand agent and workspace capacity; and
- localhost ingress with an mkcert-issued certificate.

It does not require CloudNativePG. Install CNPG separately only when testing the
HA database path from the [Helm guide](../helm/README.md).

The source chart uses moving `latest` component tags for the development path.
That is convenient for this disposable evaluation, but it is not reproducible.
For a durable deployment, use a tagged OCI chart and override every component
image with the matching release tag or a verified digest, as described in the
[production install guide](../helm/README.md#production-install-bring-your-own).

## 2. Bootstrap the cluster

```bash
./scripts/local-dev-up.sh
```

The script is idempotent. It:

1. creates or starts the `srw` k3d cluster;
2. installs the pinned cert-manager release;
3. uploads the mkcert CA and creates a ClusterIssuer;
4. creates the `srw` namespace;
5. creates the session-router JWT and local VM SSH Secrets if absent;
6. maps the local ingress hostnames through cluster DNS; and
7. downloads the Helm dependencies pinned by `Chart.lock`.

Re-running it preserves existing Secrets and persistent volumes.

## 3. Install SRW

```bash
helm install srw ./helm \
  --namespace srw \
  --kube-context k3d-srw \
  --values deployment/values-local.yaml
```

Watch startup:

```bash
kubectl --context k3d-srw --namespace srw get pods --watch
```

The orchestrator takes longest because its startup is gated on databases,
identity, git, and initialization work. Stop watching after the enabled
workloads are ready.

## 4. Log in

Open <https://localhost/> and use:

| Username | Password | Roles |
|---|---|---|
| `test` | `srw-k3d-dev-test` | admin and user |

The account is seeded by the local Keycloak configuration. Its email is
pre-verified and it maps to the local Gitea administrator.

The local overlay also enables published multi-user test accounts:

| Username | Password | Roles |
|---|---|---|
| `dev-admin-1` | `srw-k3d-dev-adm1` | admin and user |
| `dev-admin-2` | `srw-k3d-dev-adm2` | admin and user |
| `dev-user-1` | `srw-k3d-dev-usr1` | user |
| `dev-user-2` | `srw-k3d-dev-usr2` | user |
| `dev-user-3` | `srw-k3d-dev-usr3` | user |
| `dev-user-4` | `srw-k3d-dev-usr4` | user |

> [!WARNING]
> Anyone who has read this repository knows these passwords. The chart default
> keeps development users disabled; only the local overlay enables them.

## Local endpoints

| URL | Service |
|---|---|
| <https://localhost/> | Cockpit |
| <https://api.localhost/> | Orchestrator API |
| <https://auth.localhost/> | Keycloak |
| <https://git.localhost/> | Gitea |
| <https://cloud.localhost/> | Nextcloud |
| <https://mcp.localhost/> | MCP server |

The bootstrap adds in-cluster DNS for service-to-ingress flows. On the host,
the reserved `*.localhost` names resolve to loopback without editing
`/etc/hosts` on the maintained Linux setup.

## Smoke test

Run these checks after a fresh install or cluster recreation.

### Cockpit and identity

Open <https://localhost/>, sign in as `test`, and confirm the Sessions page
loads without a refresh loop.

### Interactive session

Choose **Sessions → New Session**, keep the default Assistant, and create the
session. The UI should advance through thread creation, agent provisioning,
runtime startup, and WebSocket connection. Send a short prompt and confirm a
streamed reply.

The platform path can be healthy even if the reply reports an invalid provider
key. Correct the key in `deployment/values-local.yaml`, then run the upgrade
command below.

Confirm that agent and workspace pods were created when the selected workspace
requires them:

```bash
kubectl --context k3d-srw --namespace srw get pods \
  -l app.kubernetes.io/component=agent
kubectl --context k3d-srw --namespace srw get pods \
  -l app.kubernetes.io/component=workspace
```

### Background job

Choose **Create → Job**, provide a small concrete goal, select an expert, and
create it. The job should move from `created` to `processing`, provision or
claim a worker runtime, and eventually reach a terminal or review state.

### Gitea and Nextcloud SSO

- Open <https://git.localhost/>, choose **Sign In**, then **Sign in with
  Keycloak**. It should land on the `test` dashboard without another password.
- Open <https://cloud.localhost/> and use the Keycloak login. A cheaper service
  check is `curl -sk https://cloud.localhost/status.php`; it should report
  `"installed": true`.

## Fast development with Tilt

Tilt is optional for evaluation and recommended while editing the repository.
After installing Tilt and creating `deployment/values-local.yaml`, run:

```bash
./scripts/local-dev-tilt-up.sh
```

The wrapper runs the base bootstrap and starts Tilt in the foreground. See the
[development guide](development.md#fast-inner-loop-with-tilt) for live-update
behavior and component-specific verification.

## Optional VM workspaces

The same k3d cluster can run KubeVirt workspaces when the host exposes KVM to
the k3d node. Install and smoke-test KubeVirt and CDI with:

```bash
./scripts/local-kubevirt-up.sh
kubectl --context k3d-srw --namespace srw create secret generic \
  srw-vm-lifecycle-hmac \
  --from-literal=VM_LIFECYCLE_HMAC_SECRET="$(openssl rand -hex 32)"
```

Then configure the `vm` and `vmController` values described in
[VM workspaces on your cluster](../helm/README.md#vm-workspaces-on-your-cluster).
VMs add substantial CPU, memory, and disk requirements.

## Daily lifecycle

Stop and restart the cluster without deleting its volumes or Helm release:

```bash
k3d cluster stop srw
k3d cluster start srw
k3d cluster list
```

After changing the local values or chart templates:

```bash
helm upgrade srw ./helm \
  --namespace srw \
  --kube-context k3d-srw \
  --values deployment/values-local.yaml
```

Some ConfigMap changes require an explicit workload restart. Prefer letting
the chart or Tilt own rollouts; use a manual restart only while diagnosing a
local change.

## Teardown

Choose the smallest teardown that matches what you intend to remove:

```bash
# Remove the Helm release. Resources annotated "keep", including PVCs, remain.
helm uninstall srw --namespace srw --kube-context k3d-srw

# Delete the namespace and all namespaced data, including retained PVCs.
kubectl --context k3d-srw delete namespace srw

# Delete the complete local cluster and its local image registry.
k3d cluster delete srw
```

The last two commands destroy local application data. They are not required to
stop the cluster for the day.

## Troubleshooting

### Browser reports an untrusted certificate

Make sure the browser trusts the same CA that the cluster uses. Do not run a
plain root-owned `mkcert -install`, which creates a second CA. Repeat:

```bash
mkcert -install
sudo env CAROOT="$(mkcert -CAROOT)" mkcert -install
```

Then completely restart the browser; NSS trust databases can remain cached for
the life of the process.

### Port 80 or 443 is already in use

Another local server or cluster owns the port. Stop that process or the other
k3d cluster before starting `srw`. `k3d cluster stop srw` releases SRW's host
ports when it is not in use.

### ImagePullBackOff

First confirm the requested repository and tag exist and that the installed
chart matches the intended release. If you deliberately use a private image,
create a registry Secret and reference it through `global.imagePullSecrets`.

### A PVC cannot be shrunk

Kubernetes does not permit reducing an existing PVC's requested capacity.
Restore the previous size, or delete that specific disposable PVC only after
confirming its data can be lost.

### Keycloak reports missing Secret keys

Realm import can reference variables for disabled clients. Compare the local
values file with the current example and restore any required keys rather than
guessing values from pod logs.

### Helm says another install, upgrade, or rollback is in progress

Inspect the release before changing it:

```bash
helm history srw --namespace srw --kube-context k3d-srw
```

The Tilt apply helper clears an abandoned `pending-upgrade` after its stale
threshold. For a manual installation, prefer completing or rolling back the
pending revision. Deleting Helm release Secrets by hand is a last resort and
must target only the confirmed pending revision.

### localhost returns 404 after restarting k3d

Traefik endpoint discovery can be stale after a long stop/start cycle:

```bash
kubectl --context k3d-srw --namespace kube-system \
  rollout restart deployment/traefik
```

### Login loops between Cockpit and Keycloak

The local overlay uses same-origin API routing so the BFF session cookie is
first-party. Confirm `auth.bff.sameOriginApi: true`, clear cookies for the
localhost sites, and restart the login. Orchestrator logs showing a successful
callback immediately followed by `/api/auth/me` returning 401 indicate the
cookie was not retained.

### A session stays on “Provisioning agent”

Inspect the orchestrator and agent pods first. If the browser still requests a
legacy WebSocket route, unregister the Cockpit service worker, clear its cache,
and hard-reload. Also confirm `srw-session-jwt` exists:

```bash
kubectl --context k3d-srw --namespace srw get secret srw-session-jwt
```

Re-running `scripts/local-dev-up.sh` creates the Secret if it is absent without
rotating an existing value.

### Source and images appear out of sync

The source-based workflow can move ahead of a previously published image. Use a
matching source tag and explicitly pinned component images for a reproducible
evaluation, or follow the development guide to build and import the affected
images locally. Do not diagnose version skew by repeatedly deleting application
data.
