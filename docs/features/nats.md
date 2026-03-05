---
tags:
  - agent-architecture
  - deployment
  - infrastructure
---

# NATS Messaging Layer

Design document for introducing NATS as the messaging backbone connecting the main application cluster and the agent VM cluster.

## Problem

The current agent↔orchestrator communication is entirely HTTP request/response. This works for single-cluster local development where everything is on localhost, but breaks down when agents run on a separate cluster:

**1. Cross-cluster routing is painful with HTTP**

The orchestrator pushes jobs to agents via `POST http://{pod_ip}:{port}/job/start`. In a single cluster, pod IPs are routable. Across clusters, they're not — the orchestrator on Cluster A can't reach a pod IP on Cluster B. Workarounds (NodePort per VM, HostPort mapping, VPN tunnels) are fragile and don't scale.

**2. Bidirectional communication requires both sides to be reachable**

The orchestrator needs to push to agents (job start, pause, cancel, freeze, resume). Agents need to push to the orchestrator (heartbeats, status, sudo requests). With HTTP, both sides need a routable address. With a message broker, both sides just connect outbound to the broker.

**3. No real-time event streaming**

The current architecture polls — orchestrator polls the DB for job completion, heartbeats arrive every 60 seconds. For VM management (monitoring dashboards, freeze/resume, sudo approval), we need lower-latency event delivery. Pub/sub fits this naturally.

**4. Future: multiple consumers per event**

When the cockpit wants to show live VM status, it subscribes to the same heartbeat stream the orchestrator uses. With HTTP, you'd need to fan-out events manually. With pub/sub, any number of consumers subscribe to the same subject.

## Why NATS

| Requirement | NATS | Alternative |
|-------------|------|-------------|
| Lightweight | ~16 MB binary, <20 MB RAM idle | RabbitMQ (~200 MB), Kafka (~1 GB+) |
| Multi-cluster | Leaf nodes (dial-out only, works through NAT) | Requires VPN or service mesh |
| Persistence | JetStream (built-in, optional) | Separate system (Redis, Kafka) |
| Pub/sub | Native, subject-based routing | RabbitMQ (exchange/queue model, more complex) |
| Request/reply | Native (`msg.respond()`) | HTTP-like but through the broker |
| K8s deployment | Official Helm chart, single `helm install` | Similar for RabbitMQ, heavier for Kafka |
| Client libraries | Python (`nats-py`), Go, JS, Rust, etc. | Comparable |

NATS is the right size for this project — much lighter than Kafka or RabbitMQ, but has persistence (JetStream) when we need it.

## Architecture

### Topology: Hub + Leaf Node

```
Main k3s cluster (hub)                  Agent k3s cluster (leaf)
┌─────────────────────────┐             ┌──────────────────────────┐
│                         │             │                          │
│  NATS cluster (3 nodes) │◄────leaf────│  NATS leaf node (1 node) │
│  + JetStream            │  connection │  (no JetStream)          │
│                         │  (outbound) │                          │
│  orchestrator ──► nats  │             │  agent VMs ──► nats-leaf │
│  cockpit ──► nats       │             │                          │
└─────────────────────────┘             └──────────────────────────┘
```

- **Hub** (main cluster): 3-node NATS cluster with JetStream for persistence. All message history, stream storage, and consumer state lives here.
- **Leaf** (agent cluster): Single NATS node, no JetStream, no local storage. Connects outbound to the hub. VMs and management daemons connect to this leaf via the cluster-internal service (`nats://nats-leaf.nats.svc.cluster.local:4222`).

The leaf node **dials out** to the hub — the hub doesn't need to reach the agent cluster. This works through NAT, firewalls, and across network boundaries. If the connection drops, the leaf reconnects automatically and clients experience a brief interruption.

### Why leaf node, not gateway

Gateways create a peer topology where both clusters are equal. Our clusters aren't peers — the main cluster is the hub (orchestrator, databases, cockpit), the agent cluster is a satellite. Leaf node reflects this hierarchy and is simpler to configure.

### Ports

| Port | Purpose | Where |
|------|---------|-------|
| 4222 | Client connections | Both clusters (ClusterIP service) |
| 7422 | Leaf node connections | Hub cluster only (NodePort or LoadBalancer for cross-cluster) |
| 6222 | Intra-cluster routing | Hub cluster only (between the 3 NATS nodes) |
| 8222 | Monitoring / metrics HTTP endpoint | Hub cluster (used by Prometheus, health checks) |

## Subject Namespace

All subjects live under the `agent.` prefix, split into two namespaces: `agent.vm.{job_id}.*` for VM management daemon communication (Phase 2) and `agent.{agent_id}.*` for direct agent↔orchestrator communication (Phases 3–4).

```
# Management daemon → orchestrator
agent.vm.{job_id}.register         VM is booted and ready
agent.vm.{job_id}.heartbeat        Periodic health + resource report
agent.vm.{job_id}.status           State changes (running, frozen, completed, failed)
agent.vm.{job_id}.sudo.request     Privilege escalation request (future)

# Orchestrator → management daemon
agent.vm.{job_id}.control          Commands: start, freeze, resume, terminate
agent.vm.{job_id}.sudo.response    Approve/deny sudo (future)
agent.vm.{job_id}.job.config       Job configuration push

# Agent → orchestrator (replaces HTTP heartbeat)
agent.{agent_id}.heartbeat         Agent heartbeat (same data as current HTTP heartbeat)
agent.{agent_id}.status            Agent status changes

# Orchestrator → agent (replaces HTTP job push)
agent.{agent_id}.job.assign        Job assignment (replaces POST /job/start)
agent.{agent_id}.job.pause         Pause request (replaces POST /job/pause)
agent.{agent_id}.job.cancel        Cancel request (replaces POST /job/cancel)
```

### JetStream Streams

Only the subjects that need persistence or replay get streams. Pure fire-and-forget subjects use core NATS.

| Stream | Subjects | Retention | Replicas | Purpose |
|--------|----------|-----------|----------|---------|
| `VM_EVENTS` | `agent.vm.>` | Limits (24h, 1 GB) | R1 | VM lifecycle events, audit trail |
| `AGENT_HEARTBEATS` | `agent.*.heartbeat` | Limits (1h, 100 MB) | R1 | Recent heartbeat history for dashboard |
| `JOB_ASSIGNMENTS` | `agent.*.job.>` | Work queue | R1 | Job dispatch with at-least-once delivery |

R1 (single replica) is sufficient for the home lab. Upgrade to R3 when running a 3-node JetStream cluster in production.

Note on subject filters: `agent.*.heartbeat` uses NATS single-token wildcard (`*`), so it matches `agent.{agent_id}.heartbeat` (3 tokens) but **not** `agent.vm.{job_id}.heartbeat` (4 tokens). VM heartbeats are captured only by `VM_EVENTS` (`agent.vm.>`), agent heartbeats only by `AGENT_HEARTBEATS` — no overlap between streams.

## Deployment

### Hub (Main Cluster)

```bash
helm repo add nats https://nats-io.github.io/k8s/helm/charts/
helm install nats nats/nats -n nats --create-namespace -f deployment/nats/nats-hub-values.yaml
```

`deployment/nats/nats-hub-values.yaml`:
```yaml
config:
  cluster:
    enabled: true
    replicas: 3
  jetstream:
    enabled: true
    fileStore:
      pvc:
        size: 2Gi
  leafnodes:
    enabled: true
  # Expose leaf node port for agent cluster connection
  # The leaf on the agent cluster dials into this
service:
  enabled: true
  ports:
    leafnode:
      enabled: true

# Expose leafnode port externally for cross-cluster leaf connection
# Option A: NodePort
extraServices:
  - name: nats-leafnode-external
    spec:
      type: NodePort
      selector:
        app.kubernetes.io/name: nats
      ports:
        - name: leafnode
          port: 7422
          targetPort: 7422
          nodePort: 30742
```

### Leaf (Agent Cluster)

```bash
helm install nats-leaf nats/nats -n nats --create-namespace -f deployment/nats/nats-leaf-values.yaml
```

`deployment/nats/nats-leaf-values.yaml`:
```yaml
config:
  cluster:
    enabled: false
  jetstream:
    enabled: false
  leafnodes:
    enabled: true
    remotes:
      - url: "nats-leaf://<main-cluster-node-ip>:30742"

replicaCount: 1

# Minimal resources — leaf is just a proxy
container:
  env:
    GOMEMLIMIT: "58MiB"
  resources:
    requests:
      cpu: 50m
      memory: 64Mi
    limits:
      memory: 64Mi
```

### Verification

```bash
# On main cluster — check NATS pods
kubectl get pods -n nats

# On agent cluster — check leaf is connected
kubectl logs -n nats deploy/nats-leaf | grep "Leafnode connection"

# Publish from agent cluster, subscribe on main cluster
# (requires nats CLI tool: https://github.com/nats-io/natscli)
# Terminal 1 (main cluster):
nats sub "agent.vm.>"
# Terminal 2 (agent cluster):
nats pub "agent.vm.test.heartbeat" '{"status": "ok"}'
# Terminal 1 should receive the message
```

## Implementation Roadmap

The current HTTP communication doesn't need to be ripped out immediately. NATS can be adopted incrementally across four phases. Each phase is independently deployable and the system remains functional at every boundary. Phases 3 and 4 are optional — HTTP coexists with NATS indefinitely.

### Phase 1: Deploy NATS Infrastructure

**Goal:** NATS hub + leaf running, cross-cluster messaging verified.

**Steps:**

1. **Create Helm values files**
   - `deployment/nats/nats-hub-values.yaml` — 3-node cluster + JetStream (see Deployment section above)
   - `deployment/nats/nats-leaf-values.yaml` — single leaf node, no JetStream
   - Optional: add TLS for the leaf→hub connection (generate certs with `nats-server --tlsgen`). Not required for the home lab where both clusters are on the same LAN — see Authentication section for the recommended NKey approach

2. **Deploy hub on main cluster**
   ```bash
   helm install nats nats/nats -n nats --create-namespace -f deployment/nats/nats-hub-values.yaml
   ```

3. **Create JetStream streams**
   - Write a `deployment/nats/setup-streams.sh` script using `nats stream add` commands
   - Create `VM_EVENTS`, `AGENT_HEARTBEATS`, and `JOB_ASSIGNMENTS` streams as defined above

4. **Deploy leaf on agent cluster**
   ```bash
   helm install nats-leaf nats/nats -n nats --create-namespace -f deployment/nats/nats-leaf-values.yaml
   ```

5. **Verify connectivity**
   - Leaf connects to hub (check logs for "Leafnode connection")
   - Pub/sub across clusters works (use `nats pub`/`nats sub` from each cluster)
   - JetStream streams are visible from the leaf side

6. **Add `NATS_URL` to environment config**
   - Main cluster services: `nats://nats.nats.svc.cluster.local:4222`
   - Agent cluster VMs: `nats://nats-leaf.nats.svc.cluster.local:4222`
   - Add `NATS_URL` to `.env.example`

7. **Add local dev support**
   - `docker-compose.dev.yaml`: add a single-node NATS service (`nats:2-alpine`, port 4222)
   - Local `NATS_URL` defaults to `nats://localhost:4222`
   - Optional: mock daemon script for testing VM NATS subjects without a real VM

**Deliverables:** Helm values in `deployment/nats/`, stream setup script, NATS running on both clusters, local dev NATS in docker-compose, verified cross-cluster messaging.

### Phase 2: VM Management Daemon over NATS

**Goal:** Management daemon communicates with orchestrator exclusively via NATS. No HTTP changes to the agent↔orchestrator path.

**Status:** The daemon stub (`deployment/harvester/packer/files/management-daemon/daemon.py`) already implements NATS pub/sub for register, heartbeat, status, and control commands.

**Steps:**

1. **Add orchestrator-side NATS subscriber**
   - New module: `orchestrator/services/nats_bridge.py`
   - On startup, connect to NATS hub and subscribe to `agent.vm.>` (wildcard)
   - Route incoming messages to existing orchestrator logic:
     - `agent.vm.{job_id}.register` → log VM registration, update job metadata
     - `agent.vm.{job_id}.heartbeat` → store VM health metrics (new `vm_heartbeats` table or append to existing metrics)
     - `agent.vm.{job_id}.status` → update job status on completion/failure
   - Wire into FastAPI lifespan (`app.on_event("startup")` / `asynccontextmanager`)

2. **Add orchestrator-side NATS publisher for control commands**
   - Extend `nats_bridge.py` with `publish_control(job_id, action)` method
   - Wire into existing freeze/resume/cancel endpoints — when a job is VM-based, publish to `agent.vm.{job_id}.control` instead of HTTP

3. **Test the full VM lifecycle**
   - `register` → `heartbeat` (periodic) → `control: freeze` → `control: resume` → `status: completed`
   - Verify the orchestrator processes each message correctly
   - Use the local dev NATS from Phase 1 for integration testing

**Files touched:**
- New: `orchestrator/services/nats_bridge.py`
- Modified: `orchestrator/main.py` (lifespan, inject NATS bridge into control endpoints)

**Deliverables:** Orchestrator subscribes to VM events over NATS, can send control commands over NATS, local dev NATS available.

### Phase 3: Agent Heartbeats over NATS (optional)

**Goal:** Replace the HTTP heartbeat loop in `OrchestratorClient` with NATS publish. Enables real-time dashboard and reduces polling.

**Steps:**

1. **Add NATS transport to `OrchestratorClient`**
   - New class `NatsTransport` in `src/api/nats_transport.py`
   - Wraps `nats-py` client, connects on init, reconnects automatically
   - Methods: `publish_heartbeat(agent_id, payload)`, `publish_status(agent_id, status)`
   - Env-gated: when `NATS_URL` is set, use NATS; otherwise fall back to HTTP (preserves local dev without NATS)

2. **Modify `OrchestratorClient.run_heartbeat_loop()`**
   - Currently at `src/api/orchestrator_client.py:257` — sends HTTP POST to `/api/agents/{agent_id}/heartbeat` every 60s
   - When `NatsTransport` is available, publish to `agent.{agent_id}.heartbeat` instead
   - Keep HTTP registration (`/api/agents/register`) — NATS heartbeats need the `agent_id` from registration

3. **Add orchestrator-side NATS consumer for agent heartbeats**
   - Extend `orchestrator/services/nats_bridge.py` to subscribe to `agent.*.heartbeat`
   - Route to existing heartbeat logic in `orchestrator/main.py` (`agent_heartbeat` endpoint updates `last_heartbeat`, agent status, and current job)
   - The HTTP `/api/agents/{agent_id}/heartbeat` endpoint remains active as fallback

4. **Cockpit real-time dashboard (future)**
   - Cockpit subscribes to `agent.*.heartbeat` via `nats.ws` (WebSocket transport)
   - Requires NATS WebSocket port exposed (port 443 or 8443)
   - Replaces current polling of `/api/agents` endpoint

**Files touched:**
- New: `src/api/nats_transport.py`
- Modified: `src/api/orchestrator_client.py` (conditional NATS heartbeat)
- Modified: `orchestrator/services/nats_bridge.py` (add agent heartbeat subscription)

**Deliverables:** Agents heartbeat over NATS when available, HTTP fallback preserved, orchestrator consumes both.

### Phase 4: Job Dispatch over NATS (optional)

**Goal:** Replace HTTP job push (`POST http://{pod_ip}:{port}/job/start`) with NATS publish. Eliminates the need for the orchestrator to know agent pod IPs.

**Steps:**

1. **Agent subscribes to its assignment subject**
   - On startup (after registration), subscribe to `agent.{agent_id}.job.assign`
   - The subscription handler replaces the HTTP `/job/start` endpoint in `src/api/app.py`
   - Also subscribe to `agent.{agent_id}.job.pause` and `agent.{agent_id}.job.cancel`

2. **Orchestrator publishes job assignments to NATS**
   - `orchestrator/main.py:234` currently does `POST http://{pod_ip}:{port}/job/start`
   - When NATS is available, publish to `agent.{agent_id}.job.assign` via `JOB_ASSIGNMENTS` stream (at-least-once delivery)
   - Use request/reply pattern: agent acknowledges receipt, orchestrator confirms assignment
   - Same for pause (`orchestrator/main.py:348`) and cancel (`orchestrator/main.py:1096`)

3. **Use JetStream for delivery guarantees**
   - `JOB_ASSIGNMENTS` stream with work queue retention ensures the message isn't lost if the agent is temporarily disconnected
   - Consumer with `ack_wait=30s` — if agent doesn't ack within 30s, message redelivers

4. **Remove pod IP dependency**
   - `get_agent_ip()` in `src/api/orchestrator_client.py` becomes unnecessary for job dispatch
   - Agent registration still reports IP (useful for debugging, direct access), but job routing uses NATS subjects
   - The `pod_ip` column in agents table becomes informational rather than operational

**Files touched:**
- Modified: `src/api/app.py` (add NATS subscription handlers alongside HTTP endpoints)
- Modified: `orchestrator/main.py` (publish to NATS instead of HTTP POST for job dispatch)
- Modified: `orchestrator/services/nats_bridge.py` (add job publish methods)

**Deliverables:** Jobs dispatched over NATS with at-least-once delivery, pod IP no longer required for routing, HTTP endpoints kept as fallback.

### Phase Summary

| Phase | Depends On | Scope | Risk |
|-------|-----------|-------|------|
| 1 — NATS infrastructure | Nothing | Infra only, no code changes | Low — additive, nothing breaks |
| 2 — VM daemon over NATS | Phase 1 | New module in orchestrator | Low — VM path is new, no existing behavior changes |
| 3 — Agent heartbeats | Phase 1 | Modify heartbeat loop | Low — HTTP fallback preserved, env-gated |
| 4 — Job dispatch | Phase 1 | Modify job assignment flow | Medium — core dispatch path, needs careful testing |

Each phase can be merged and deployed independently. Phases 3 and 4 are optional and can be deferred indefinitely — HTTP coexists with NATS at every stage.

## Durability Considerations

The Jepsen analysis of NATS 2.12.1 (December 2025) found that NATS Server flushes writes to disk lazily by default (every ~2 minutes). A power failure or OS crash can lose acknowledged-but-unflushed messages.

For our use case this is acceptable — heartbeats and status events are transient. If we lose a few heartbeats during a crash, the orchestrator just sees a gap and marks the agent as unhealthy. Job state lives in PostgreSQL, not in NATS.

If stronger durability is needed later, enable synchronous writes:
```yaml
config:
  jetstream:
    fileStore:
      syncAlways: true  # fsync every write — slower but durable
```

## Authentication

For the home lab, NATS runs without auth. Before exposing the leafnode port beyond the local network, add authentication to the leaf→hub connection.

**Recommended: NKey authentication**

NKeys are ed25519 keypairs — no shared secrets, no token rotation. Generate with the `nk` tool:

```bash
nk -gen server -pubout  # hub server identity
nk -gen user -pubout    # leaf node credential
```

Hub config:
```yaml
config:
  leafnodes:
    enabled: true
    authorization:
      users:
        - nkey: UABC...  # leaf node's public NKey
```

Leaf config:
```yaml
config:
  leafnodes:
    remotes:
      - url: "nats-leaf://<hub-ip>:30742"
        credentials: /etc/nats/leaf.nk
```

For multi-tenant setups (multiple agent clusters), use separate accounts per cluster with subject import/export rules. This is not needed for the initial deployment.

## Client Libraries

| Component | Language | Library |
|-----------|----------|---------|
| Management daemon | Python | `nats-py` (>= 2.9.0) |
| Orchestrator | Python | `nats-py` |
| Cockpit (future) | TypeScript | `nats.ws` (WebSocket transport) |

The management daemon already imports `nats-py` in its stub implementation (`deployment/harvester/packer/files/management-daemon/daemon.py`).

## Related

- [[vm]] — VM architecture, management daemon design
- [[vm_agent_cluster_setup]] — Agent cluster setup (k3s + KubeVirt)
- [[cloud_workspace]] — Original cloud workspace architecture spec
- [[deployment]] — Main cluster k8s manifests
