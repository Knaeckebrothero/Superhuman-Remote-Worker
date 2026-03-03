---
tags:
  - agent-architecture
  - deployment
  - infrastructure
---

# Harvester Cluster Setup Guide

Step-by-step guide for standing up the 3-node Harvester cluster that hosts agent VMs. This cluster is physically separate from the main k3s cluster (which runs the orchestrator, cockpit, and databases).

## Prerequisites

### Hardware (per node)

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 8 cores, x86_64 with VT-x/VT-d | 16+ cores |
| RAM | 32 GB | 64 GB |
| Disk | 250 GB SSD/NVMe (5000+ IOPS) | 500 GB+ NVMe |
| NIC | 1 Gbps, 1 port minimum | 10 Gbps, 2 ports |

All three machines **must have identical CPU models** for live migration to work. Mixed CPUs will break maintenance operations.

### Network

- All 3 nodes on the same L2 network segment (same switch/VLAN)
- Switch ports configured as **trunk ports** (tagged VLAN traffic)
- One free IP for the cluster VIP (virtual IP for management endpoint)
- Static DHCP reservations or static IPs for all 3 nodes — IPs must never change
- All nodes must reach the Rancher server on TCP 443
- NTP server accessible from all nodes

### Software

- Harvester v1.7.1 ISO: `https://releases.rancher.com/harvester/v1.7.1/harvester-v1.7.1-amd64.iso`
- Rancher v2.13.x (already running on the main k3s cluster)
- A USB drive or IPMI/iDRAC virtual media for booting the ISO

## Installation

### Node 1 (First Node)

1. Boot from the Harvester ISO
2. Select **Harvester Installer**
3. Choose **Create new cluster**
4. Set password for the `rancher` user (this is your SSH login)
5. Select the OS disk (the entire disk will be used)
6. Set hostname: `harvester-01`
7. Configure network:
   - Select the management NIC(s) — Harvester creates a bond `mgmt-bo`
   - Assign a static IP or use DHCP with static reservation
8. Set the **cluster VIP** — a separate IP on the same subnet, not assigned to any node
9. Set a **cluster token** — save this, nodes 2 and 3 need it to join
10. Configure NTP server (default: `0.suse.pool.ntp.org`)
11. Optionally import SSH keys (e.g., from `https://github.com/<username>.keys`)
12. Confirm and wait for installation (~10-15 minutes)

After reboot, the console shows the management URL: `https://<cluster-VIP>/`. **Wait until the UI is accessible before proceeding to node 2.** The first node takes 15-20 minutes to fully initialize. Joining too early causes cluster formation failures.

### Node 2

1. Boot from the Harvester ISO
2. Select **Join existing cluster**
3. Set the cluster VIP and cluster token from node 1
4. Choose role: **Default** (will be auto-promoted to management)
5. Set hostname: `harvester-02`
6. Configure network (same subnet as node 1)
7. Confirm and wait

### Node 3

Same as node 2, hostname `harvester-03`.

Once all 3 nodes have joined, you have a 3-node HA cluster with:
- 3 management nodes (etcd quorum, fault tolerance = 1)
- Longhorn distributed storage (3 replicas by default)
- KubeVirt ready for VM workloads

## Import into Rancher

Rancher v2.13.x includes Harvester integration by default.

1. In Rancher, open the hamburger menu → **Virtualization Management**
2. Click **Import Existing**
3. Enter a name (e.g., `harvester-agents`) and click **Create**
4. Rancher shows a registration URL — copy it
5. In the Harvester UI (`https://<cluster-VIP>/`), go to **Settings** and paste the registration URL into the Rancher registration field
6. Wait for the `cattle-cluster-agent` pod to start on the Harvester cluster
7. The cluster appears in Rancher's Virtualization Management view

After import, you can manage VMs, images, volumes, and networks from Rancher.

## Verify: Create a Test VM

Quick sanity check that the cluster works before building custom images.

### From the Harvester UI

1. Go to **Images** → **Create**
2. Upload or provide a URL for Ubuntu 24.04 cloud image:
   `https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img`
3. Wait for the image to download and become Active
4. Go to **Virtual Machines** → **Create**
5. Set name: `test-vm`, CPU: 2, Memory: 2 Gi
6. Under Volumes, select the Ubuntu image as root disk, set size to 10 Gi
7. Under Networks, keep the default management network
8. Under Advanced Options → User Data, add:
   ```yaml
   #cloud-config
   password: testpass
   chpasswd:
     expire: false
   ssh_pwauth: true
   ```
9. Click Create
10. Once Running, open the VNC console from the VM detail page
11. Log in as `ubuntu` / `testpass`

If this works, the cluster is healthy. Delete the test VM.

### Via kubectl

From a machine with `kubectl` access to the Harvester cluster (Rancher provides a kubeconfig download):

```yaml
# test-vm.yaml
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: test-vm
  namespace: default
spec:
  runStrategy: RerunOnFailure
  template:
    spec:
      domain:
        cpu:
          cores: 2
        memory:
          guest: 2Gi
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
```

```bash
kubectl apply -f test-vm.yaml
kubectl get vmi  # Wait for Running
virtctl console test-vm  # Or use VNC from Harvester UI
```

Clean up: `kubectl delete vm test-vm`

## Network Architecture

```
                    Internet
                        │
                   ┌────┴────┐
                   │ Router   │
                   └────┬────┘
                        │
          ┌─────────────┼─────────────┐
          │             │             │
    ┌─────┴─────┐ ┌────┴─────┐ ┌────┴──────┐
    │ Main k3s  │ │ Harvester│ │ Harvester │ ...
    │ cluster   │ │ node 1   │ │ node 2    │
    │           │ │          │ │           │
    │ orchestr. │ │  VMs ──────── NATS ───────→ orchestrator
    │ cockpit   │ │          │ │           │
    │ databases │ │          │ │           │
    │ NATS      │ │          │ │           │
    └───────────┘ └──────────┘ └───────────┘
```

The Harvester cluster's internal VM network must be able to reach the main k3s cluster's NATS service for management daemon communication. This can be achieved via:
- Pod network routing between clusters (if on the same L2 network)
- NodePort or LoadBalancer service exposing NATS on the main cluster
- VPN/tunnel between clusters (more complex, more isolated)

The simplest approach for a home lab: expose NATS via NodePort on the main k3s cluster, and VMs connect to `<main-cluster-node-ip>:<nats-nodeport>`.

## Storage Notes

- Longhorn is built into Harvester and provides distributed block storage
- Default replica count is 3 (one per node in a 3-node cluster)
- Losing 1 node: existing volumes degraded (2/3 replicas), new volumes can still be created with 2 replicas
- Losing 2 nodes: cluster loses etcd quorum, becomes unavailable
- For agent VMs using containerDisk (ephemeral boot): no Longhorn volume needed for the root disk
- For persistent agent data: use DataVolume or PVC backed by Longhorn

## Maintenance

- **Always drain a node** before shutting down for maintenance: Harvester UI → Host → Maintenance Mode
- **Upgrades**: Harvester supports rolling upgrades from the UI. Back up cluster config first.
- **Support bundles**: Harvester UI → System → Support Bundle → Download (captures all node logs)

## Troubleshooting

| Issue | Fix |
|-------|-----|
| UI not accessible after node 1 install | Wait 15-20 minutes. Check console for errors (F12 toggles shell). |
| Node 2/3 fails to join | Ensure node 1 is fully ready. Check VIP is reachable. Verify cluster token. |
| VMs fail to start | Check Longhorn health (Harvester UI → Longhorn). Verify sufficient resources. |
| Image pull fails | Ensure nodes can reach the image URL or registry. Check proxy settings. |
| `cattle-cluster-agent` ImagePullBackOff | Nodes can't reach Docker Hub. Pre-load the image or configure registry mirror. |
| Graphics issues during install | Add `vga=792` to kernel boot line (press E at GRUB). |

## Related

- [[vm]] — VM architecture design document
- [[cloud_workspace]] — k3s deployment, NATS communication
- [[deployment]] — main cluster k8s manifests
