---
tags:
  - agent-architecture
  - deployment
  - infrastructure
---

# Agent VM Cluster Setup (k3s + KubeVirt)

Set up a single-node k3s cluster with KubeVirt on a spare machine for running agent VMs. Physically separate from the main k3s cluster to contain the blast radius of autonomous agent workloads.

For the production path (Harvester on 3 dedicated nodes with HA and live migration), see [vm_harvester_setup.md](./vm_harvester_setup.md).

## Prerequisites

### Hardware

One spare x86_64 machine with:

| Resource | Minimum | Notes |
|----------|---------|-------|
| CPU | 4 cores with VT-x/VT-d | Check: `egrep -c '(vmx|svm)' /proc/cpuinfo` must be > 0 |
| RAM | 16 GB | ~4 GB for k3s + KubeVirt, rest for VMs |
| Disk | 100 GB | OS + containerDisk image cache |
| Network | 1 Gbps, reachable from main cluster | Same LAN |

This leaves room for 2-3 agent VMs at 2 vCPUs / 2-4 GB RAM each.

### Network

- Static IP or static DHCP reservation (IP must not change)
- Must be able to reach the main k3s cluster (for NATS, orchestrator API)
- Main cluster must be able to reach this machine (for kubectl against the agent cluster API)

### On the main k3s cluster

NATS must be accessible from the agent cluster. Expose it via NodePort before starting:

```bash
# On main cluster — check if NATS is already exposed
kubectl get svc -n <nats-namespace>

# If not, patch or create a NodePort service
# Example: expose NATS on port 30422
kubectl patch svc nats -n <nats-namespace> -p '{"spec": {"type": "NodePort", "ports": [{"port": 4222, "nodePort": 30422}]}}'
```

Note the main cluster node IP and NATS NodePort — VMs will connect to `nats://<main-cluster-ip>:30422`.

## Step 1: Install k3s

SSH into the spare machine and install k3s:

```bash
# Install k3s (single node, no traefik — we don't need ingress on this cluster)
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable=traefik" sh -

# Verify
sudo kubectl get nodes
# Should show one node in Ready state

# Copy kubeconfig for remote access (from your workstation)
sudo cat /etc/rancher/k3s/k3s.yaml
# Replace 127.0.0.1 with the machine's actual IP when using remotely
```

To manage this cluster from your workstation, copy the kubeconfig:

```bash
# On your workstation
scp user@agent-machine:/etc/rancher/k3s/k3s.yaml ~/.kube/agent-cluster.yaml
# Edit agent-cluster.yaml: replace 127.0.0.1 with the machine's IP
export KUBECONFIG=~/.kube/agent-cluster.yaml
kubectl get nodes
```

## Step 2: Install KubeVirt

```bash
# Set the kubeconfig to the agent cluster
export KUBECONFIG=~/.kube/agent-cluster.yaml

# Get the latest stable KubeVirt version
export KUBEVIRT_VERSION=$(curl -s https://api.github.com/repos/kubevirt/kubevirt/releases/latest | grep tag_name | cut -d '"' -f 4)
echo "Installing KubeVirt ${KUBEVIRT_VERSION}"

# Install the operator
kubectl apply -f "https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}/kubevirt-operator.yaml"

# Install the KubeVirt custom resource (triggers actual deployment)
kubectl apply -f "https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}/kubevirt-cr.yaml"

# Wait for KubeVirt to be ready (takes 1-3 minutes)
kubectl -n kubevirt wait kv kubevirt --for condition=Available --timeout=300s
```

Verify KubeVirt is running:

```bash
kubectl get pods -n kubevirt
# Should see virt-api, virt-controller, virt-handler pods all Running
```

## Step 3: Install virtctl (Optional but Useful)

`virtctl` is the KubeVirt CLI for interacting with VMs (console access, start/stop, etc.):

```bash
export KUBEVIRT_VERSION=$(curl -s https://api.github.com/repos/kubevirt/kubevirt/releases/latest | grep tag_name | cut -d '"' -f 4)
curl -L -o /usr/local/bin/virtctl "https://github.com/kubevirt/kubevirt/releases/download/${KUBEVIRT_VERSION}/virtctl-${KUBEVIRT_VERSION}-linux-amd64"
chmod +x /usr/local/bin/virtctl
```

## Step 4: Create the Agent VMs Namespace

```bash
kubectl create namespace agent-vms
```

## Step 5: Verify with a Test VM

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: test-vm
  namespace: agent-vms
spec:
  runStrategy: RerunOnFailure
  template:
    spec:
      domain:
        cpu:
          cores: 1
        memory:
          guest: 512Mi
        devices:
          disks:
          - name: rootdisk
            disk:
              bus: virtio
          interfaces:
          - name: default
            masquerade: {}
        machine:
          type: q35
      networks:
      - name: default
        pod: {}
      volumes:
      - name: rootdisk
        containerDisk:
          image: kubevirt/cirros-container-disk-demo
EOF
```

```bash
# Wait for VM to start
kubectl get vmi -n agent-vms -w
# Should transition to Running

# Access the console
virtctl console test-vm -n agent-vms
# Login: cirros / gocubsgo
# Type 'exit' or Ctrl+] to disconnect

# Clean up
kubectl delete vm test-vm -n agent-vms
```

If the test VM starts and you can access the console, KubeVirt is working.

## Step 6: Set Up RBAC for Orchestrator Access

The orchestrator on the main cluster needs a kubeconfig to create/delete VMs on the agent cluster. Create a service account with minimal permissions:

```bash
cat <<'EOF' | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: orchestrator-vm-manager
  namespace: agent-vms
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: vm-manager
  namespace: agent-vms
rules:
- apiGroups: ["kubevirt.io"]
  resources: ["virtualmachines", "virtualmachineinstances"]
  verbs: ["get", "list", "watch", "create", "update", "delete"]
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list"]
- apiGroups: ["subresources.kubevirt.io"]
  resources: ["virtualmachineinstances/console", "virtualmachineinstances/vnc"]
  verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: orchestrator-vm-manager
  namespace: agent-vms
subjects:
- kind: ServiceAccount
  name: orchestrator-vm-manager
  namespace: agent-vms
roleRef:
  kind: Role
  name: vm-manager
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: v1
kind: Secret
metadata:
  name: orchestrator-vm-manager-token
  namespace: agent-vms
  annotations:
    kubernetes.io/service-account.name: orchestrator-vm-manager
type: kubernetes.io/service-account-token
EOF
```

Extract the token and build a kubeconfig for the orchestrator:

```bash
# Get the token
TOKEN=$(kubectl get secret orchestrator-vm-manager-token -n agent-vms -o jsonpath='{.data.token}' | base64 -d)

# Get the CA cert
CA=$(kubectl get secret orchestrator-vm-manager-token -n agent-vms -o jsonpath='{.data.ca\.crt}')

# Agent cluster API server address
AGENT_API="https://<agent-machine-ip>:6443"

# Generate kubeconfig
cat <<EOF > orchestrator-vm-kubeconfig.yaml
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: ${CA}
    server: ${AGENT_API}
  name: agent-cluster
contexts:
- context:
    cluster: agent-cluster
    namespace: agent-vms
    user: orchestrator-vm-manager
  name: agent-vms
current-context: agent-vms
users:
- name: orchestrator-vm-manager
  user:
    token: ${TOKEN}
EOF

echo "Kubeconfig written to orchestrator-vm-kubeconfig.yaml"
echo "Copy this to the orchestrator and set AGENT_CLUSTER_KUBECONFIG=/path/to/this/file"
```

Test from your workstation:

```bash
KUBECONFIG=orchestrator-vm-kubeconfig.yaml kubectl get vm -n agent-vms
# Should work (empty list)

KUBECONFIG=orchestrator-vm-kubeconfig.yaml kubectl get nodes
# Should be FORBIDDEN (scoped to agent-vms namespace, VM resources only)
```

## Network Architecture

```
Main k3s cluster (your home lab)          Agent k3s cluster (spare PC)
┌──────────────────────────────┐          ┌───────────────────────────┐
│ orchestrator                 │          │ KubeVirt                  │
│ cockpit                      │          │                           │
│ PostgreSQL, MongoDB, Neo4j   │          │ ┌───────────────────────┐ │
│                              │          │ │ agent-vm-job-abc123   │ │
│ NATS ──── NodePort :30422 ──────────────│─│  └─ management daemon │ │
│                              │          │ │     └─ connects NATS  │ │
│ orchestrator ── kubectl ─────────────── │ ├───────────────────────┤ │
│  (vm-kubeconfig)             │          │ │ agent-vm-job-def456   │ │
└──────────────────────────────┘          │ └───────────────────────┘ │
                                          └───────────────────────────┘
         Same LAN (e.g., 192.168.1.0/24)
```

Two connections cross the cluster boundary:
1. **NATS** (NodePort on main cluster) — management daemons inside VMs connect to publish heartbeats, register, receive control commands
2. **kubectl** (orchestrator → agent cluster API) — orchestrator creates/deletes VirtualMachine resources using the scoped kubeconfig

## Optionally: Import into Rancher

If you want the agent cluster visible in Rancher (not required, but nice for monitoring):

```bash
# On the agent cluster
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable=traefik" sh -
# Then in Rancher UI: Cluster Management → Import Existing → Generic
# Copy the registration command and run it on the agent machine
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `virtctl` or KubeVirt pods fail | Check VT-x/VT-d: `egrep -c '(vmx|svm)' /proc/cpuinfo`. Must be > 0. Enable in BIOS if needed. |
| VM stuck in Scheduling | Not enough resources. Check `kubectl describe vmi <name> -n agent-vms` for events. |
| containerDisk image pull fails | Node can't reach Docker Hub. Check DNS and proxy settings. |
| VM can't reach NATS | Check NodePort is exposed on main cluster. From VM: `curl -v telnet://<main-ip>:30422`. |
| orchestrator kubeconfig doesn't work | Check token hasn't expired. Verify API server address and CA cert. |
| KubeVirt install hangs | Check `kubectl get pods -n kubevirt`. If pods are in ImagePullBackOff, node can't reach ghcr.io/kubevirt. |

## Related

- [[vm]] — VM architecture design document
- [[vm_harvester_setup]] — Production path: Harvester on dedicated hardware
- [[cloud_workspace]] — k3s deployment, NATS communication
- [[deployment]] — main cluster k8s manifests
