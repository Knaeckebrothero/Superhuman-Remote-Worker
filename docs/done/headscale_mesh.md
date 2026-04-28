---
tags:
  - agent-architecture
  - deployment
  - infrastructure
  - networking
---

# Headscale Mesh VPN for Agent-VM Connectivity

Design document for replacing NodePort-based SSH access with a Headscale (self-hosted Tailscale) mesh VPN, enabling agent pods on the main cluster to SSH into KubeVirt VMs on any remote cluster regardless of network topology.

**Status:** Implemented, then re-architected.

> **Note (current architecture):** The mesh VPN is in production, but the
> headscale server is no longer bundled into the SRW Helm chart. It is
> deployed as an independent Fleet bundle in
> `HomeLab/deployments_managed/headscale/` and the chart consumes it via
> the `headscale.url` value. The chart-embedded sections below
> (`Headscale Deployment` → `Kubernetes Manifest`, the `headscale.enabled`
> toggle, etc.) are preserved for design context but no longer reflect
> what's deployed. See **[`docs/features/external_headscale.md`](../features/external_headscale.md)**
> for the current setup.

## Problem

The current VM SSH path uses NodePort services on the agent cluster:

```
Agent pod (main cluster) ──SSH──► NodePort:3xxxx (agent cluster) ──► VM:22
```

This requires L3 reachability between the main cluster's pod network and the agent cluster's node IPs. It works when both clusters are on the same LAN (`10.0.50.0/24`), but breaks when:

- The VM cluster is in a different data centre or cloud region
- The VM cluster is behind NAT or a firewall with no inbound ports
- A customer deploys their VM cluster on-premise while the main cluster is hosted

NodePort also has operational downsides: K8s assigns random ports from a global range (30000-32767), the VM controller needs RBAC for service creation, and leftover services accumulate if cleanup fails.

## Solution: Headscale Mesh VPN

[Headscale](https://github.com/juanfont/headscale) is a self-hosted implementation of the Tailscale coordination server. It orchestrates WireGuard peer-to-peer tunnels between nodes. Each node gets a stable IP on a virtual overlay network (the "tailnet"), and nodes find each other through the coordination server regardless of their physical network.

```
Main k3s cluster                              Agent k3s cluster (any location)
┌───────────────────────────┐                ┌──────────────────────────────┐
│                           │                │                              │
│  Headscale (coord server) │                │  KubeVirt VMs                │
│       ▲           ▲       │                │    ┌─────────────────────┐   │
│       │           │       │                │    │ agent-vm-job-abc123 │   │
│       │           │       │                │    │ tailscale ─► 100.64.x.y │
│  ┌────┴───┐  ┌────┴───┐  │                │    └─────────────────────┘   │
│  │ agent  │  │ agent  │  │  WireGuard      │    ┌─────────────────────┐   │
│  │ pod    │  │ pod    │──┼──tunnel (p2p)───┼──► │ agent-vm-job-def456 │   │
│  │ ts=    │  │ ts=    │  │                │    │ tailscale ─► 100.64.x.z │
│  │100.64. │  │100.64. │  │                │    └─────────────────────┘   │
│  │ a.b    │  │ a.c    │  │                │                              │
│  └────────┘  └────────┘  │                └──────────────────────────────┘
│                           │
│  NATS (control plane)  ◄──┼─── leaf connection (unchanged) ──────────────┘
└───────────────────────────┘
```

Key properties:
- **Dial-out only** — both agent pods and VMs connect outbound to Headscale. No inbound ports needed on either cluster.
- **Peer-to-peer** — after coordination, traffic flows directly between agent and VM via WireGuard, not through the Headscale server.
- **Stable IPs** — each node gets a `100.64.0.0/10` address that persists across restarts.
- **NAT traversal** — built-in DERP relay falls back to relayed connections when direct p2p fails (strict NAT, symmetric NAT).
- **Self-hosted** — no external dependency, all coordination data stays on the main cluster.

## Architecture

### Component Overview

| Component | Runs on | Role |
|-----------|---------|------|
| Headscale server | Main cluster (StatefulSet + PVC) | Coordination server, key exchange, ACLs |
| Tailscale sidecar | Agent pods (main cluster) | Joins tailnet, provides SSH route to VMs |
| Tailscale daemon | Inside each KubeVirt VM | Joins tailnet on boot, exposes VM on overlay |
| VM Controller | Agent cluster | Creates VMs, generates auth keys via Headscale API, passes to VM |
| NATS bridge | Main cluster (orchestrator) | Daemon registration now reports Tailscale IP instead of NodePort |

### What Changes, What Stays

| Concern | Before (NodePort) | After (Headscale) |
|---------|-------------------|-------------------|
| SSH target | `<node_ip>:<random_nodeport>` | `100.64.x.y:22` (stable Tailscale IP) |
| Cross-cluster requirement | L3 reachability to node IPs | Only outbound HTTPS to Headscale (port 443 or custom) |
| NodePort services | Created/deleted per VM | Eliminated |
| VM Controller RBAC | Needs `services` create/delete | No longer needed (for SSH) |
| NATS subjects | `vm.query.pod-ip` returns NodePort | Returns Tailscale IP (or eliminated entirely) |
| RemoteBackend | paramiko to NodePort | paramiko to Tailscale IP (minimal change) |
| Auth | SSH key volume mount | SSH key (unchanged) + Tailscale auth key (new) |

### NATS Remains the Control Plane

Headscale replaces only the **SSH data path**. NATS continues to handle:
- VM lifecycle (create, delete, status)
- Daemon registration and heartbeats
- Freeze/resume/terminate control commands
- Sudo approval gate

The daemon registration message (`agent.vm.*.register`) will report the VM's Tailscale IP instead of relying on `vm.query.pod-ip` for a NodePort endpoint.

## Headscale Deployment

### Kubernetes Manifest

Headscale runs as a single-replica **StatefulSet** on the main cluster. StatefulSet (not Deployment) because Headscale uses SQLite — the only supported database backend (PostgreSQL is deprecated). SQLite requires a single writer; running multiple replicas causes database corruption.

> **HA note:** Headscale does not support active-active replication. For resilience, use LiteFS + Consul for automatic SQLite replication with ~15s failover, or rely on frequent PVC snapshots for disaster recovery.

**Images:** `ghcr.io/juanfont/headscale:0.28.0` or `docker.io/headscale/headscale:0.28.0`. Debug variant with shell: `headscale/headscale:0.28.0-debug`.

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: headscale
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: headscale-config
  namespace: headscale
data:
  config.yaml: |
    server_url: https://headscale.<domain>    # MUST be HTTPS in production
    listen_addr: 0.0.0.0:8080
    metrics_listen_addr: 0.0.0.0:9090
    grpc_listen_addr: 0.0.0.0:50443           # Admin API (internal only)
    private_key_path: /var/lib/headscale/private.key
    noise:
      private_key_path: /var/lib/headscale/noise_private.key
    prefixes:
      v4: 100.64.0.0/10
      v6: fd7a:115c:a1e0::/48
    derp:
      server:
        enabled: true                          # Built-in DERP relay for NAT traversal
        stun_listen_addr: 0.0.0.0:3478
      # verify_clients defaults to true — DERP queries Headscale to validate
      # connecting clients, denying unknown nodes
    ephemeral_node_inactivity_timeout: 30m     # Default; minimum 65s
    database:
      type: sqlite                             # Only supported backend (PostgreSQL deprecated)
      sqlite:
        path: /var/lib/headscale/db.sqlite
    dns:
      magic_dns: false                         # Not needed — we use IPs directly
    policy:
      path: /etc/headscale/acl.hujson
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: headscale-acl
  namespace: headscale
data:
  # HuJSON format (JSON with comments and trailing commas)
  acl.hujson: |
    {
      // Tag owners — empty arrays mean only pre-auth keys can assign these tags
      "tagOwners": {
        "tag:agent": [],
        "tag:vm": [],
      },
      // Deny-by-default when acls array is non-empty
      "acls": [
        {
          "action": "accept",
          "src": ["tag:agent"],
          "dst": ["tag:vm:22"],    // SSH only
        },
      ],
    }
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: headscale
  namespace: headscale
spec:
  serviceName: headscale
  replicas: 1
  selector:
    matchLabels:
      app: headscale
  template:
    metadata:
      labels:
        app: headscale
    spec:
      containers:
        - name: headscale
          image: headscale/headscale:0.28.0
          command: ["headscale", "serve"]
          ports:
            - containerPort: 8080    # Client coordination API
            - containerPort: 9090    # Metrics (Prometheus)
            - containerPort: 50443   # gRPC admin API (internal only, do NOT expose)
            - containerPort: 3478    # STUN (UDP, for NAT traversal)
              protocol: UDP
          volumeMounts:
            - name: data
              mountPath: /var/lib/headscale
            - name: config
              mountPath: /etc/headscale/config.yaml
              subPath: config.yaml
            - name: acl
              mountPath: /etc/headscale/acl.hujson
              subPath: acl.hujson
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health
              port: 8080
            periodSeconds: 10
          resources:
            requests:
              memory: "128Mi"
              cpu: "100m"
            limits:
              memory: "256Mi"
              cpu: "500m"
      volumes:
        - name: config
          configMap:
            name: headscale-config
        - name: acl
          configMap:
            name: headscale-acl
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: 1Gi
---
apiVersion: v1
kind: Service
metadata:
  name: headscale
  namespace: headscale
spec:
  type: ClusterIP    # Exposed externally via Ingress or NodePort
  selector:
    app: headscale
  ports:
    - name: http
      port: 8080
    - name: metrics
      port: 9090
    - name: stun
      port: 3478
      protocol: UDP
```

Headscale must be reachable from the VM cluster (Tailscale clients connect outbound to the coordination server). Options:
- **Ingress** with TLS (recommended for production, required for `server_url: https://...`)
- **NodePort** on the main cluster (simplest for homelab)
- **LoadBalancer** if available

The gRPC admin port (50443) should NOT be exposed externally — it's for CLI access from within the cluster only.

### User and Auth Key Management

Headscale uses "users" (formerly "namespaces") to group nodes. When using ACLs with tags, user borders no longer apply — all machines can communicate as long as ACL rules permit. Create one user for the SRW system:

```bash
headscale users create srw
```

Pre-auth keys are generated via the Headscale CLI or REST API. Keys are bcrypt-hashed at rest (since v0.28) — the full key is only shown once at creation.

**VM keys** — short-lived, single-use, ephemeral. Generated by the VM controller for each VM:

```bash
# CLI (for reference)
headscale preauthkeys create --user srw --reusable=false --ephemeral --expiration 10m --tags tag:vm

# REST API (what the VM controller will call)
# Authentication: Bearer <HEADSCALE_API_KEY>
POST /api/v1/preauthkey
{
  "user": "<user_id>",
  "reusable": false,
  "ephemeral": true,
  "expiration": "2026-03-26T19:00:00Z",
  "aclTags": ["tag:vm"]
}
# Response includes the full key (hskey-auth-<prefix>-<secret>), shown only once
```

> **Note:** The REST API `user` field takes a user ID (uint64), not the user name. Look up the ID via `GET /api/v1/user` or `headscale users list`.

**Agent keys** — long-lived, reusable, stored in Vault/ESO:

```bash
headscale preauthkeys create --user srw --reusable=true --expiration 365d --tags tag:agent
```

**API key for the VM controller** — used to authenticate REST API calls:

```bash
headscale apikeys create --expiration 365d
# Default expiry is 90 days; the key cannot be retrieved after creation
```

**Swagger UI** available at `https://<headscale>/swagger` for API exploration.

## Agent Pod Integration

Tailscale runs as a **sidecar container** in the agent pod. This keeps the agent image unchanged and isolates VPN concerns.

> **Important:** The Tailscale Kubernetes Operator is NOT compatible with Headscale (it requires Tailscale's proprietary OAuth API — tracked in [headscale#3086](https://github.com/juanfont/headscale/issues/3086)). Manual sidecar deployment is the only option.

### Kernel Mode vs Userspace Mode

| Mode | Capabilities Required | Outbound to Tailnet | Performance |
|------|----------------------|---------------------|-------------|
| **Kernel** (recommended) | `NET_ADMIN`, `NET_RAW` | Direct IP routing | Better |
| **Userspace** | None (unprivileged) | SOCKS5/HTTP proxy only | Lower |

**Kernel mode is required.** In userspace mode, outbound connections to tailnet addresses (`100.64.x.y`) require routing through a SOCKS5 proxy (`localhost:1055`), which paramiko doesn't natively support. Kernel mode creates a `tailscale0` tun interface in the shared pod network namespace, making tailnet IPs directly routable from the agent container.

### Sidecar Configuration

```yaml
# Addition to deployment/21-agent.yaml
containers:
  - name: tailscale
    image: ghcr.io/tailscale/tailscale:v1.96.3
    securityContext:
      capabilities:
        add: ["NET_ADMIN", "NET_RAW"]    # Required for kernel-mode WireGuard tun
    env:
      - name: TS_AUTHKEY
        valueFrom:
          secretKeyRef:
            name: srw-secrets
            key: TAILSCALE_AUTH_KEY
      - name: TS_EXTRA_ARGS
        value: "--login-server=$(HEADSCALE_URL)"
      - name: HEADSCALE_URL
        valueFrom:
          configMapKeyRef:
            name: srw-config
            key: HEADSCALE_URL
      - name: TS_KUBE_SECRET
        value: "tailscale-state"         # Persist state across pod restarts
      - name: TS_ACCEPT_DNS
        value: "false"                   # We use IPs, not MagicDNS
      - name: TS_HOSTNAME
        valueFrom:
          fieldRef:
            fieldPath: metadata.name     # Pod name as Tailscale hostname
    resources:
      requests:
        memory: "64Mi"
        cpu: "50m"
      limits:
        memory: "128Mi"
        cpu: "200m"
```

> **State persistence:** `TS_KUBE_SECRET` stores Tailscale node state in a Kubernetes Secret. Without it, every pod restart registers a new node, accumulating stale entries in Headscale. The pod's ServiceAccount needs RBAC to read/write the named Secret.

The agent container routes to `100.64.0.0/10` through the `tailscale0` interface created by the sidecar in the shared pod network namespace. No proxy configuration needed — paramiko connects directly to `100.64.x.y:22`.

> **MagicDNS caveat:** Even with `TS_ACCEPT_DNS=true`, MagicDNS only reliably works in the sidecar container itself, not the main application container (its `/etc/resolv.conf` points to cluster DNS). Since we use IP addresses directly, this doesn't affect us.

## VM Integration

Tailscale is installed in the VM base image and configured via cloud-init at boot.

### VM Base Image Changes

Add to the Packer/cloud-init build for the VM base image:

```bash
# Install Tailscale (one-liner from tailscale.com, or package manager)
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable tailscaled
```

### Cloud-Init (Per-VM)

The VM controller passes the auth key and login server URL through cloud-init user data:

```yaml
#cloud-config
runcmd:
  - - tailscale
    - up
    - --auth-key=${TAILSCALE_AUTH_KEY}
    - --login-server=${HEADSCALE_URL}
    - --hostname=vm-${JOB_ID_SHORT}
    - --accept-routes=false
    - --ssh
```

`--ssh` enables Tailscale SSH (optional — can also use standard SSH with key auth as today). Tags are applied automatically from the pre-auth key's `aclTags`, not via `--advertise-tags`.

### VM Controller Changes

The VM controller's `_on_create` method changes:

1. **Generate a pre-auth key** via Headscale API (single-use, ephemeral, short-lived, tagged `tag:vm`)
2. **Inject the key into cloud-init** user data when creating the KubeVirt VM
3. **Stop creating NodePort services** — the `_create_ssh_service` call is removed
4. **Cleanup on delete**: call Headscale API to remove the node when the VM is torn down

```python
# Pseudocode for VM controller changes

async def _on_create(self, msg):
    # ... existing VM creation logic ...

    # NEW: Generate Headscale pre-auth key
    auth_key = await self._create_headscale_auth_key(job_id)

    # Inject into cloud-init
    cloud_init = self._render_cloud_init(
        job_id=job_id,
        headscale_url=HEADSCALE_URL,
        tailscale_auth_key=auth_key,
    )

    # Create VM with cloud-init (existing KubeVirt API call)
    vm_spec = self._build_vm_spec(vm_name, cloud_init=cloud_init)
    self.custom_api.create_namespaced_custom_object(...)

    # REMOVED: self._create_ssh_service(vm_name, job_id)

async def _on_delete(self, msg):
    # ... existing VM deletion ...

    # NEW: Remove node from Headscale
    await self._remove_headscale_node(job_id)
```

### Daemon Registration Change

The daemon inside the VM reports its Tailscale IP instead of its LAN IP:

```python
# In vm/sudo-daemon (Go) — registration payload
{
    "job_id": "abc123",
    "hostname": "vm-abc123",
    "ip": "<tailscale_ip>",    # 100.64.x.y instead of 10.0.2.2
    "pid": 1234
}
```

The daemon can obtain the Tailscale IP from:
```bash
tailscale ip -4    # Returns the 100.64.x.y address
```

### NATS Bridge Change

`_on_daemon_register` in `orchestrator/services/nats_bridge.py` simplifies:

- The `vm.query.pod-ip` request/reply is no longer needed (no NodePort to look up)
- The daemon's self-reported IP is the Tailscale IP, which is directly reachable from agent pods
- Fallback logic (NodePort → pod IP → daemon IP) collapses to: use the daemon-reported IP

```python
async def _on_daemon_register(self, msg):
    data = json.loads(msg.data.decode())
    job_id = data.get("job_id")

    # Daemon reports its Tailscale IP — directly reachable from agent pods
    ssh_host = data.get("ip")
    ssh_port = 22    # Standard SSH port, no NodePort randomness

    await self._set_vm_context(job_id, {
        "status": "ready",
        "ssh_host": ssh_host,
        "ssh_port": ssh_port,
        ...
    })
```

### RemoteBackend

No changes needed. It already connects to whatever `host:port` the config provides. The only difference is the IP changes from a node IP + random NodePort to a Tailscale IP + port 22.

## Headscale API Integration

The VM controller needs a Headscale API client. Headscale exposes a gRPC API (and REST via grpc-gateway).

### Required API Calls

| Operation | Endpoint | When |
|-----------|----------|------|
| Create pre-auth key | `POST /api/v1/preauthkey` | VM creation |
| List nodes | `GET /api/v1/node` | Health checks, debugging |
| Delete node | `DELETE /api/v1/node/{id}` | VM teardown |
| Get node by name | `GET /api/v1/node?name=vm-{job_id}` | Lookup for cleanup |

### Authentication

Headscale API uses API keys:

```bash
headscale apikeys create --expiration 365d
```

Store the API key in Vault, inject via ESO as `HEADSCALE_API_KEY` into the VM controller.

## ACL Policy

Headscale ACLs control which nodes can talk to which. The policy is minimal:

```json
{
  "tagOwners": {
    "tag:agent": [],
    "tag:vm": []
  },
  "acls": [
    {
      "action": "accept",
      "src": ["tag:agent"],
      "dst": ["tag:vm:22"]
    }
  ]
}
```

- Agents can reach VMs on port 22 only (SSH)
- VMs cannot reach agents (no reverse path)
- VMs cannot reach other VMs (job isolation)

## Rollout Plan

### Phase 1: Headscale Deployment
- Deploy Headscale on main cluster (StatefulSet + VolumeClaimTemplate + ConfigMap)
- Expose via Ingress (HTTPS) or NodePort for cross-cluster access
- Create `srw` user and generate agent auth key + API key
- Store agent auth key and Headscale API key in Vault
- Verify Headscale health: `/health` endpoint, Swagger at `/swagger`

### Phase 2: Agent Sidecar
- Add Tailscale sidecar (kernel mode) to agent deployment
- Add RBAC for `tailscale-state` Secret (state persistence)
- Verify agent pods join tailnet and get `100.64.x.y` addresses
- Verify `tailscale0` interface is visible from the agent container
- No functional change yet — agents still use NodePort for existing VMs

### Phase 3: VM Integration
- Install Tailscale in VM base image (Packer rebuild)
- Update VM controller: generate auth keys, inject via cloud-init, skip NodePort creation
- Update daemon: report Tailscale IP in registration
- Update NATS bridge: simplify `_on_daemon_register`, remove `vm.query.pod-ip` fallback
- Update VM controller cleanup: remove Headscale node on VM delete

### Phase 4: Cleanup
- Remove `_create_ssh_service` / `_delete_ssh_service` from VM controller
- Remove `services` RBAC from VM controller Role
- Remove `vm.query.pod-ip` subscription from VM controller
- Remove NodePort handling from NATS bridge
- Update documentation

## Configuration

### New Environment Variables

| Variable | Component | Source | Description |
|----------|-----------|--------|-------------|
| `HEADSCALE_URL` | VM controller, agent sidecar | ConfigMap | Headscale coordination server URL |
| `HEADSCALE_API_KEY` | VM controller | Vault/Secret | API key for pre-auth key generation and node management |
| `TAILSCALE_AUTH_KEY` | Agent pods | Vault/Secret | Long-lived reusable auth key for agents (tag:agent) |

### New Vault Secrets

| Path | Key | Description | Rotation |
|------|-----|-------------|----------|
| `srw/headscale` | `api_key` | Headscale API key for VM controller | Default 90-day expiry, rotate before expiration |
| `srw/headscale` | `agent_auth_key` | Reusable pre-auth key for agent pods (tag:agent) | Create with 365-day expiry, rotate annually |

## Security Considerations

- **Ephemeral keys**: VM auth keys are single-use and expire in 10 minutes. A leaked key is useless after the VM claims it. Keys are bcrypt-hashed at rest (since v0.28).
- **Ephemeral nodes**: VMs register as ephemeral nodes — Headscale automatically removes them after `ephemeral_node_inactivity_timeout` (default 30 minutes, minimum 65 seconds).
- **ACL enforcement**: Tags restrict traffic — agents can only reach VMs on port 22, nothing else. ACLs are deny-by-default when the rules array is non-empty.
- **DERP client verification**: Enabled by default — the embedded DERP relay queries Headscale to validate connecting clients, denying unknown nodes and preventing bandwidth abuse.
- **No MagicDNS**: DNS resolution stays off. The system uses IP addresses, avoiding DNS-based attacks.
- **WireGuard encryption**: All traffic between agent and VM is encrypted (WireGuard), even on the same LAN.
- **Key rotation**: The Headscale API key defaults to 90-day expiry — rotate periodically. The agent auth key should be rotated annually. VM keys are inherently rotated (new key per VM).
- **gRPC admin port**: Port 50443 must NOT be exposed externally — restrict to cluster-internal CLI access only.
- **HTTPS required**: `server_url` must use HTTPS in production. Tailscale clients refuse plain HTTP coordination servers.

## Known Issues and Gotchas

- **Ephemeral node `lastSeen` bug** ([headscale#2006](https://github.com/juanfont/headscale/issues/2006)): `lastSeen` may not update for some nodes, causing premature ephemeral node deletion. Mitigation: the daemon heartbeat via NATS is the primary health signal; Headscale node cleanup is defense-in-depth, not the primary lifecycle mechanism.
- **No Tailscale K8s Operator support** ([headscale#3086](https://github.com/juanfont/headscale/issues/3086)): The official Tailscale operator requires OAuth credentials against Tailscale's API. Manual sidecar deployment is required with Headscale.
- **Single-writer SQLite**: Never run more than one Headscale replica against the same PVC. StatefulSet with `replicas: 1` enforces this.
- **Sequential upgrades**: Headscale v0.28 does not support direct upgrades from databases older than v0.25. Upgrade through each stable release sequentially.
- **High node churn**: Many nodes with frequent map changes cause resource usage spikes. Monitor Headscale memory/CPU during periods of heavy VM creation/deletion.

## Capacity and Performance

- **Headscale overhead**: Minimal — coordination is lightweight (key exchange + keepalives). A single Headscale instance handles thousands of nodes.
- **WireGuard throughput**: Near line-speed for SSH file operations. The bottleneck remains SFTP overhead, not the tunnel.
- **DERP relay**: If p2p fails (strict NAT on both sides), traffic relays through the built-in DERP server on the main cluster. Adds latency but maintains connectivity. Disabling Tailscale's public DERP servers (which we do by using Headscale) makes the embedded DERP a single point of failure — ensure it's reachable.
- **Startup time**: Tailscale authentication adds ~2-3 seconds to VM boot. Cloud-init runs Tailscale in parallel with other setup.
