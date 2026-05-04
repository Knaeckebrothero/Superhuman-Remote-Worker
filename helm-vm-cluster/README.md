# Superhuman Remote Worker — VM Cluster Helm Chart

Cross-cluster VM-side companion to the main `superhuman-remote-worker` chart.
Packages the NATS leaf + KubeVirt vm-controller that the orchestrator (chart 1)
talks to over a NATS hub-leaf bridge.

- **Chart:** `oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker-vm-cluster`
- **Pairs with:** `oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker` (same version)
- **Source:** <https://github.com/knaeckebrothero/Superhuman-Remote-Worker>
- **License:** see [LICENSE](https://github.com/knaeckebrothero/Superhuman-Remote-Worker/blob/main/LICENSE) — you must accept the terms (`license.acceptTerms: true`).

---

## What gets deployed

| Component | Purpose | Toggle |
|---|---|---|
| `nats-leaf` | NATS relay dialing the orchestrator-cluster's NATS hub | always on |
| `vm-controller` | KubeVirt VM lifecycle manager (NATS-driven) | always on |
| `vm-template` | KubeVirt VirtualMachine template (cloud-init) | always on |
| `network-policy` | Per-VM egress lockdown (DNS, 80/443, Tailscale UDP) | always on |
| Namespace | Optional chart-managed namespace | `namespace.create` |

---

## Architecture

```
[orchestrator cluster]                        [vm cluster — this chart]
  superhuman-remote-worker chart
    ├── orchestrator                            superhuman-remote-worker-vm-cluster chart
    └── nats hub (nats.internal=true)             ├── nats-leaf (dials hub via NodePort)
        └── leafnode NodePort  ◀───────leaf───── │
                                                  └── vm-controller
                                                        ├── KubeVirt VMs (per job)
                                                        │     └── tailscale daemon
                                                        │           └── joins headscale tailnet
                                                        │                 ◀───── orchestrator's
                                                        │                       agent.tailscale sidecar
                                                        └── headscale API
```

---

## Prerequisites

- **Kubernetes** 1.28+ with KubeVirt operator + CDI operator installed (cluster-scoped infra; this chart does NOT install them)
- **Nodes with hardware virtualization** (`vmx` or `svm` in `/proc/cpuinfo`) and the KubeVirt feature gates enabled
- **The main chart** (`superhuman-remote-worker`) installed with `nats.internal=true` AND `nats.leafNodePort` set to a reachable NodePort on the orchestrator cluster
- A **CNI that enforces NetworkPolicy** (Calico, Cilium, OVN-Kubernetes, Antrea — NOT Flannel)
- A **shared SSH keypair** in Vault (or inline): the orchestrator chart holds the private key (its `externalSecrets.vmSshKeyVaultPath`), this chart authorizes the matching public key into VMs (`ssh.publicKey` or `ssh.publicKeyVaultPath`)
- A **Headscale API key** for the controller to register provisioned VMs (`headscale.apiKeySecret` or `headscale.apiKeyVaultPath`)
- A **Headscale pre-auth key** if `tailscale.enabled=true` so VMs join the tailnet (`tailscale.authKeySecret` or `tailscale.authKeyVaultPath`)

Optional:
- **External Secrets Operator** + a backing store (Vault, etc.) for the `*VaultPath` options

---

## Quick start

Minimum required values (assumes pre-existing K8s Secrets):

```bash
helm install srw-prod-vm \
  oci://ghcr.io/knaeckebrothero/charts/superhuman-remote-worker-vm-cluster \
  --namespace srw-prod-vm-controller \
  --create-namespace \
  --set license.acceptTerms=true \
  --set namespace.create=true \
  --set orchestratorId=srw-prod \
  --set nats.hubUrl="nats://10.0.50.101:30743" \
  --set ssh.publicKey="ssh-ed25519 AAAA... srw-prod-vm-access" \
  --set headscale.url="https://headscale.h4ll.app" \
  --set headscale.apiKeySecret="hs-api-key" \
  --set tailscale.enabled=true \
  --set tailscale.authKeySecret="ts-auth-key"
```

Or with a values file (recommended for production — see chart README for
external-secrets-managed examples).

---

## Verifying the install

```bash
# All resources up?
kubectl get all -n srw-prod-vm-controller

# NATS leaf connected to the hub?
kubectl logs -n srw-prod-vm-controller deploy/srw-prod-vm-nats-leaf | grep -i "leafnode connection"

# VM controller subscribing?
kubectl logs -n srw-prod-vm-controller deploy/srw-prod-vm-vm-controller | grep -i "subscribed"

# Trigger a VM-requiring job from the orchestrator and watch:
kubectl get vm -n srw-prod-vm-controller
kubectl get vmi -n srw-prod-vm-controller
```

---

## Uninstall

```bash
# Removes all chart-managed resources except the namespace (which carries
# helm.sh/resource-policy: keep when namespace.create=true).
helm uninstall srw-prod-vm -n srw-prod-vm-controller

# Full cleanup (VMs + namespace):
kubectl delete vm,vmi,dv -n srw-prod-vm-controller --all
kubectl delete namespace srw-prod-vm-controller
```
