# Workspace Network Isolation: Egress Hardening, Listener Port Restructuring, and Per-Tenant Tiering — 2026-05-19

The existing workspace NetworkPolicy
(`helm/templates/workspace-network-policy.yaml`) was written with the
intent of "internet + specific in-cluster services, nothing else." Live
verification on the production cluster (2026-05-17) showed the actual
posture is meaningfully looser than that intent: the three wildcard
egress rules (TCP/22, TCP/80, TCP/443 with no destination CIDR) let
workspaces reach the home LAN, K3s node IPs, and arbitrary MetalLB
addresses on those ports.

This doc captures three connected pieces of work that together close
that gap and set up the tenancy story:

1. **Egress hardening** — `ipBlock except` on the three wildcard rules
   so the cluster, pod, service, and (default-tier) home networks are
   denied at the policy layer.
2. **Listener port restructuring** — move workspace SSHD `22 → 30022`
   and code-server `8080 → 38080` so listener ports and egress
   allowlist ports never collide, and so common dev framework defaults
   (Spring Boot, Tomcat, Express, Next.js) stay free for the agent.
3. **Per-tenant network tiering** — a `network_tier` attribute on
   projects, one NetworkPolicy per tier, the orchestrator emits the
   tier label on workspace pods at provisioning time.

The first is the actual security hygiene fix. The second is
architectural cleanup. The third is the multi-tenant story; it
composes with the work tracked in `project_multi_tenancy.md` /
`docs/multi_tenancy.md`.

This doc is **not** about container↔VM policy unification — that's
`docs/features/workspace_network_policy_unification.md`. The two docs
share the `srw.io/component=agent-workspace` selector and the basic
policy shape; this doc tightens the egress and tiering, the
unification doc extends coverage to virt-launcher pods.

It is also **not** about user-managed external network access — that's
`docs/features/user_vpn_networks.md`. Operator tiers (this doc) and
user VPN Networks are siblings, not layers: operator tiers govern
which networks the cluster itself permits workspace pods to reach;
user VPN Networks let individual users tunnel their agents into
external networks the *user* already controls. The two compose at
runtime — a workspace sits on some operator tier *and* may have user
VPN Networks attached — but they have separate control planes and
audiences (operator/admin vs. end user).

---

## Status (2026-05-19)

- **PR 1 — Egress hardening:** ✅ shipped and verified on the dev
  cluster (`superhuman-remote-worker.com`). The new policy renders
  with the six-CIDR `ipBlock except:` block and the three-port
  consolidation; the chart-values comment and the unification doc's
  CNI table were corrected alongside it.
- **PR 2 — Listener port restructuring:** ✅ shipped and verified on
  dev. Live confirmation on a freshly provisioned workspace pod
  (`ws-thread-625e2e0c-c13`, image `sha-56ec68b`): `ss -tlnp` shows
  sshd on `0.0.0.0:30022` and code-server on `0.0.0.0:38080` (no
  listener on 22 or 8080); pod-spec `containerPort` entries and both
  probes match the new ports; a new session reached "Connected"
  status (proving agent → workspace SSH on 30022 end-to-end); the
  IDE opened cleanly through the orchestrator proxy (proving the
  `:38080` path through the proxy and the NetworkPolicy ingress
  rules). Implementation surfaced several files the original PR 2
  plan did not list — see "Discoveries beyond the original file
  list" under PR 2 below.
- **PR 3 — Per-tenant tiering:** ✅ shipped 2026-05-19 (unblocked by
  multi-tenancy M1.A landing 2026-05-16, which delivered the project
  scoping plumbing PR 3 needed). Migration `0016_project_network_tier.sql`
  adds `network_tier` to projects with the closed `('internet-only',
  'home-allowed')` check. The orchestrator's `ContainerProvisioner`
  resolves the tier per workspace (job + IDE + thread paths) and emits
  `srw.io/network-tier: <name>` on each pod; the helm chart renders one
  NetworkPolicy per entry in `workspace.networkPolicy.tiers`, each
  matched by both the existing `srw.io/component=agent-workspace`
  selector and the new tier selector. The cockpit project-settings tab
  gains an admin-gated tier selector; the orchestrator API enforces the
  same gate on `PATCH /api/projects/{id}` so a project owner who isn't
  also an admin cannot widen their own tier. Verified locally via
  `pytest tests/test_container_provisioner.py` (4 new tests, all 55
  pass) and `helm template -s` (2 NetworkPolicies render, `home-allowed`
  drops `192.168.178.0/24` from the `except:` list as expected).
- **PR 2b — docker-compose mode parity:** ⏳ not started. PR 2 left
  docker-compose deployments broken — the workspace image listens on
  30022/38080 but `docker-compose.{yaml,dev.yaml,local.yaml}` port
  mappings, the in-compose healthchecks, and the
  `docker_provisioner.py` default port are still 22/8080. K8s
  deployments (dev + prod) work; compose deployments don't. See the
  PR 2b subsection in the Plan below.

---

## Problem

### Posture mismatch (live-verified 2026-05-17)

The current policy egress block has three wildcard rules:

```yaml
- ports: [{protocol: TCP, port: 80}, {protocol: TCP, port: 443}]
- ports: [{protocol: TCP, port: 22}]
```

None of them carry a `to:` clause. NetworkPolicy semantics for a port
rule with no `to:` is "allow on this port to any destination
reachable from the pod network," which on the homelab (Calico
`natOutgoing: Enabled`, K3s node routing to home LAN) means:

| Destination | Port | Verified result | Intended? |
|---|---|---|---|
| Internet (1.1.1.1) | 443 | ✅ HTTP 301 | yes |
| Internet | 8888 | ❌ blocked | yes (port not in allowlist) |
| K3s API server (10.0.50.101) | 6443 | ❌ blocked | yes |
| Kubelet (10.0.50.101) | 10250 | ❌ blocked | yes |
| MetalLB neo4j-bolt (10.0.51.12) | 7687 | ❌ blocked | yes |
| Cross-workspace pod (10.42.3.144) | 22 | ❌ blocked | yes (lateral isolation) |
| Cross-workspace pod | 8080 | ❌ blocked | yes |
| **K3s node (10.0.50.101)** | **443** | ⚠️ HTTP 200 (svclb on host network) | **no** |
| **K3s node SSH (10.0.50.101)** | **22** | ⚠️ TCP connected, SSH banner | **no** |
| **Home LAN (192.168.178.1 — MikroTik admin)** | **443** | ⚠️ HTTP 200 | **no** |

Severity is **defense-in-depth**, not active breach. Pre-auth attack
surface is exposed on infrastructure that shouldn't be reachable from
the workspace network namespace, but:

- K3s SSH is key-only auth — TCP reach ≠ login.
- MikroTik admin has its own auth.
- K3s API server and kubelet are correctly blocked.
- Cross-workspace movement is correctly blocked.

The risk is meaningful when the threat model includes untrusted
prompts (which is the inherent model for an AI agent platform) — a
sufficiently rogue agent run could direct workspace traffic at the
home router admin page, fingerprint K3s node SSH, or probe MetalLB
services on 80/443. For the single-operator dev cluster + thesis
project as it stands today, this is "fix as hygiene, not P0."
For the hosted product / multi-tenant direction in
`docs/multi_tenancy.md`, this becomes "fix before second tenant."

### Listener / egress port aliasing on TCP/22

`22` does dual duty in the current policy:

- **Ingress 22** — agent pods → workspace SSHD (RemoteBackend) and
  orchestrator → workspace SSHD (paramiko SFTP for thread uploads,
  `orchestrator/services/thread_uploads.py`).
- **Egress 22** — workspace → `git@github.com` (git-over-ssh
  datasources), workspace → external remote servers the agent is
  asked to SSH into.

Today both use port `22`. Two consequences:

1. Locking down egress 22 to "0.0.0.0/0 except RFC1918" still works
   because ingress 22 is gated by `from: podSelector`, not by
   destination CIDR. The rules are technically independent. But:
2. The semantic ambiguity is a footgun. A future PR that reads "22 is
   for outbound SSH, let me widen the allowlist" or "22 is for
   workspace ingress, let me remove the destination restriction" has
   no clear signal which side of the rule is load-bearing.

Defense-in-depth value of moving the listener: if egress 22 ever
regresses (drift on `except:`, a "temporary" widening that becomes
permanent, etc.), the workspace's own SSHD wouldn't be cluster-
reachable on 22 because no process listens there. The misconfig
fails closed instead of re-exposing a listener.

### Listener squatting on conventional dev ports

`code-server` listens on `0.0.0.0:8080` inside the workspace
(`docker/Dockerfile.workspace:238`). When an agent runs a dev webapp
inside the workspace, the framework defaults conflict:

| Framework | Default port |
|---|---|
| Spring Boot / Tomcat | 8080 |
| Jenkins, JBoss, many Java app servers | 8080 |
| Many Python/Go HTTP services | 8080 |
| Next.js, CRA, Express, NestJS, Rails | 3000 |

Today, an agent running `mvn spring-boot:run` in a workspace gets
"port 8080 already in use," then either burns turns retrying or works
around with `--server.port=8081`. The workspace stops behaving like a
clean dev environment, and any future "dev preview" feature (agent
runs a webapp, user previews it at
`https://preview-<job>.<domain>`) would have to route to a
non-conventional port that the agent has to discover and report.

`9222` (CDP) does not have this problem — it's not a typical
backend-service port — so the proposal moves only 8080.

`3000` is not currently a workspace listener and should stay free for
agent webapps. (The egress allowlist *does* contain a TCP/3000 rule,
but it's `to: podSelector(gitea)` not a wildcard; it does not affect
what the workspace can listen on.)

---

## Current state, for reference

### Network topology (homelab, per `HomeLab/CLAUDE.md`)

```
Home LAN (192.168.178.0/24)
    ↓
MikroTik router (192.168.178.1, BGP ASN 64512)
    ↓
Cluster nodes (10.0.50.0/24)
    ↓
MetalLB IPs (10.0.51.0/24)         ← in-cluster services, incl. Traefik 10.0.51.11
    ↓
Pod CIDR (10.42.0.0/16)            ← K3s default, /24 per node
Service CIDR (10.43.0.0/16)
```

CNI: K3s built-in flannel + the embedded kube-router network-policy
controller. NetworkPolicy enforcement is **active** (verified 2026-
05-17 by observing both allow and deny outcomes on the same pod);
the chart values comment that flannel "does not enforce" is
misleading and should be updated.

### Workspace pod listeners

| Port | Service | Ingress allowed from |
|---|---|---|
| 22 | OpenSSH | `app: srw-agent`, `app: srw-persistent-agent`, orchestrator (SFTP) |
| 8080 | code-server | orchestrator (IDE proxy), Traefik (IDE direct) |
| 9222 | Chromium CDP | `app: srw-agent` |

### Workspace pod egress (current)

| Port | Protocol | Destination | Comment |
|---|---|---|---|
| 53 | UDP/TCP | any | DNS |
| 80 | TCP | **any** | wildcard — leaks to home LAN + node |
| 443 | TCP | **any** | wildcard — leaks to home LAN + node |
| 22 | TCP | **any** | wildcard — leaks to home LAN + node SSH |
| 41641 | UDP | any | Tailscale direct WireGuard |
| 3478 | UDP | any | Tailscale STUN |
| 3000 | TCP | `podSelector: gitea` | in-cluster Gitea |
| 5432 | TCP | `podSelector: postgres` | App DB |
| 5432 | TCP | `podSelector: pgvector` | Vector DB |
| 7688 | TCP | `podSelector: neo4j` | Knowledge graph |
| 27017 | TCP | `podSelector: mongodb` | Audit trail |

### Agent pods have no NetworkPolicy

Confirmed live: `app: srw-agent` pods carry no policy selector in the
chart. Egress is unrestricted. This is **intentional** in the
current architecture — the agent process doesn't execute tool
requests itself; every tool call (browser, shell, file ops, git, …)
is proxied to the workspace pod via SSH. The workspace pod is the
real sandbox, the agent pod is the LangGraph orchestrator. The
agent's egress destinations are predictable (LLM endpoints,
orchestrator REST) and don't carry user-directed code, so the policy
emphasis on the workspace side is correct.

This doc takes that posture as given. If we ever change it (e.g.,
move tool execution into the agent process), the agent pod
needs its own policy and the analysis here transfers.

---

## Design

### 1. Egress hardening — `ipBlock except` on the three wildcards

Replace the bare port rules with destination-bounded versions:

```yaml
- ports:
    - {protocol: TCP, port: 80}
    - {protocol: TCP, port: 443}
    - {protocol: TCP, port: 22}
  to:
    - ipBlock:
        cidr: 0.0.0.0/0
        except:
          - 10.0.50.0/24       # K3s nodes
          - 10.0.51.0/24       # MetalLB IPs (use podSelector for any service we want reachable)
          - 10.42.0.0/16       # pod network (cross-workspace, kubelet, system pods)
          - 10.43.0.0/16       # service network (force ClusterIP traffic through podSelector rules)
          - 192.168.178.0/24   # home LAN (default tier: locked out)
          - 169.254.0.0/16     # link-local / cloud metadata
```

Two design decisions worth flagging:

**Excluding `10.0.51.0/24` (MetalLB)**, not just `10.0.50.0/24`
(nodes). Today workspaces reach Gitea via the in-cluster
`podSelector: gitea` rule at port 3000, not via the Traefik MetalLB
IP. Same for the LLM endpoint at `ai.h4ll.app` — resolved via
in-cluster ClusterIP, not the MetalLB IP. Anything we want reachable
on a MetalLB IP should be carved back in by `podSelector`, which makes
the dependency explicit. (If something *does* currently rely on
hitting an `10.0.51.x` IP directly from a workspace, the lockdown
breaks it — see "Open questions.")

**Excluding `10.42.0.0/16` (pod network) and `10.43.0.0/16` (service
network).** Cross-workspace lateral movement, kubelet, and bypass-
ing the `podSelector` rules by hitting ClusterIP-resolved traffic
on a wildcard port are all undesirable. Specific allowed services
are added back via their own `podSelector` egress rules.

The three protocol/port pairs share one egress block — the
restrictions are identical. UDP/41641, UDP/3478, DNS, and the
specific `podSelector` rules stay unchanged.

### 2. Listener port restructuring

| Port | From | To | Reason |
|---|---|---|---|
| Workspace SSHD | 22 | **30022** | Remove egress/ingress alias; defense-in-depth against future egress drift |
| code-server | 8080 | **38080** | Free 8080 for agent dev webapps (Spring Boot, Tomcat) |
| Chromium CDP | 9222 | (keep) | 9222 is not a conventional service port; no conflict |

Choice of port numbers is convention (high range, K8s NodePort range
for visual recognition). Nothing enforces this on container ports.

The orchestrator side is already parameterized — `ssh_port` flows
through `vm_ctx` and defaults to 22 (e.g.
`orchestrator/services/workspace_suspension.py:346`,
`orchestrator/services/thread_uploads.py:115`,
`orchestrator/main.py:1119,1283`). Container provisioner sets
`ssh_port=22` at workspace registration and exposes
`containerPort: 22` in the pod manifest
(`orchestrator/services/container_provisioner.py:301,352,636`). All
of these become `30022`.

code-server's port is hard-coded in fewer places: the bind in
`docker/Dockerfile.workspace:238` and the health-check URL in
`orchestrator/services/ide_session.py:610`.

### 3. Per-tenant network tiering (concept, defers behind multi-tenancy P2)

Add a `network_tier TEXT NOT NULL DEFAULT 'internet-only'` column on
`projects` (or `users` — project-scoped is the right granularity for
the multi-tenancy work that just landed; cross-project access in the
same tier composes cleanly with the family/visibility rules).

This is the **operator-side** control plane — the cluster operator
decides which networks workspaces in a tier can reach at the
pod-network layer. The user-side counterpart — end users granting
their agents access to external networks via VPN tunnels they
control — lives in `docs/features/user_vpn_networks.md`. The two
mechanisms compose at runtime (tier governs unencrypted pod-network
egress; user VPNs are tunneled traffic riding over allowed UDP); see
the sibling doc for the threat-model breakdown.

Tiers initially:

| Tier | TCP 80/443/22 `except:` list |
|---|---|
| `internet-only` (default) | All four cluster CIDRs + home LAN + link-local |
| `home-allowed` | All four cluster CIDRs + link-local; **home LAN allowed** |
| (no third tier yet) | — |

The chart emits one NetworkPolicy per tier, each selecting on
`srw.io/network-tier: <tier-name>` in addition to the existing
`srw.io/component: agent-workspace`. Tier label is set by the
orchestrator in
`ContainerProvisioner._build_workspace_labels(job_id)` using the
job's owning project's `network_tier`.

For homelab use today: the operator's project gets `home-allowed`
(DWH and internal git become reachable on whatever port they expose,
not constrained to 80/443/22 destinations because we'd add an
`ipBlock` rule with `192.168.178.0/24` in `to:` for that tier rather
than relying on the wildcard ports). Everyone else's projects stay
`internet-only`.

Tiering is **explicitly not in scope for the immediate fix** — the
hardening + port move stand alone. Tiering can ship later when
multi-tenancy P2 (G2-G5) provides the project-scoping plumbing it
needs. Without tiering, the default policy with the `ipBlock except`
list above is also the operator's policy — for the homelab today,
the operator would need to either temporarily add `192.168.178.0/24`
back to the egress allowlist on their own machine's workspaces (not
ideal, leaks to anyone with workspace access on the same cluster), or
expose internal services via `*.h4ll.app` so they're reachable
through the existing in-cluster Traefik path. The latter is the
cleaner answer for any internal HTTPS service; the former is only
needed for direct TCP services on non-standard ports.

---

## Plan

### PR 1 — Egress hardening (security hygiene, ships standalone)

**Status:** ✅ shipped 2026-05-19; see the Status section above for
verification details.

Files:
- `helm/templates/workspace-network-policy.yaml` — replace the three
  wildcard egress rules with the `ipBlock except` block above.
- `helm/values.yaml` — update the `workspace.networkPolicy` comment
  to soften the "Flannel does not enforce it" wording, since K3s's
  embedded kube-router controller does enforce.
- `docs/features/workspace_network_policy_unification.md` — same
  comment correction in the CNI compatibility table.

Verification:
- Repeat the live-test table from above on a workspace pod after the
  policy upgrade. Expect the three ⚠️ rows (K3s node 22/443, MikroTik
  admin 443) to flip to ❌. Other rows unchanged.
- Smoke-test agent → workspace SSH still works (ingress 22 is
  unaffected by egress changes).
- Smoke-test workspace → `gitea.h4ll.app` resolves and reaches the
  Gitea pod (in-cluster path, not MetalLB IP).
- Smoke-test workspace → `git@github.com:22` git-clone still works
  (public destination, allowed).
- Smoke-test workspace → `ai.h4ll.app` LLM endpoint still reachable.

Estimated size: ~15 lines of YAML + 2 comment fixes. One PR.

### PR 2 — Listener port restructuring (architectural cleanup)

**Status:** ✅ shipped 2026-05-19; see the Status section above for
verification details. The list of files below is the *originally
planned* set; the actually-changed set is larger — see "Discoveries
beyond the original file list" at the end of this subsection.

Files:
- `docker/Dockerfile.workspace` — sshd `Port 30022` directive in the
  `agent.conf` block at line 192; code-server `bind-addr:
  0.0.0.0:38080` at line 238; `EXPOSE` line 280 to `30022 38080 9222`.
- `orchestrator/services/container_provisioner.py` — `ssh_port=22 →
  30022` at lines 301 and 352; `containerPort` in the pod manifest at
  lines 636-638 to `30022`, `38080`, `9222`.
- `orchestrator/services/ide_session.py` — `:8080` → `:38080` in the
  health-check URL at line 610.
- `helm/templates/workspace-network-policy.yaml` — ingress rules at
  lines 33-67 update the agent SSH/CDP and orchestrator-SFTP/IDE +
  Traefik-IDE port numbers to match.
- New workspace image tag (rebuild + chart image-tag bump).

Verification:
- New workspace pod comes up with sshd on 30022 and code-server on
  38080. Confirm with `kubectl exec` + `ss -tlnp`.
- Agent SSH connection through orchestrator's RemoteBackend reaches
  the workspace on the new port (covered by the existing job
  smoke tests, but worth one targeted manual run).
- code-server reachable via the orchestrator IDE proxy and via the
  Traefik IngressRoute on the new target port.
- Thread upload via paramiko SFTP (orchestrator → workspace 30022)
  works.
- Inside a workspace, `python -m http.server 8080` succeeds (port is
  free) and is reachable from another shell in the same pod.
- Egress TCP/22 to `github.com` still works (this is now
  unambiguously the "outbound git-over-ssh" port).

Estimated size: ~30 lines across 4 files + image rebuild. One PR.

**Discoveries beyond the original file list:**

The four-file plan above was incomplete. Implementation surfaced the
following additional changes needed in the K8s/container path:

1. **`docker/workspace-entrypoint.sh:45`** — code-server's operational
   `--bind-addr 0.0.0.0:8080` flag (in the entrypoint, which overrides
   the `config.yaml` baked into the image) had to flip to `:38080`.
   Just changing the config file in the Dockerfile was insufficient.
2. **`orchestrator/main.py:7745, 7826`** — IDE HTTP proxy and
   WebSocket proxy upstream URL construction also use `:8080` and had
   to flip to `:38080`.
3. **`orchestrator/services/container_provisioner.py:686, 691`** —
   readinessProbe and livenessProbe `tcpSocket.port` had to flip from
   22 to 30022 (separate from the `containerPort` entries already in
   the plan).
4. **Runtime gap in `src/api/persistent_app.py:3207`** — the agent's
   workspace remote-config builder used `ws.get("pod_port") or 22`,
   but `container_provisioner.py` never wrote any port field to the
   workspace context at all. Without the fix, PR 2 would have created
   pods with SSHD on 30022 and then handed the agent a remote config
   with `port: 22` — and the SSH connection would have failed. Fix:
   `container_provisioner` now writes `"port": 30022` to both the job
   and thread workspace contexts (matching the field-name convention
   `docker_provisioner` already uses — `orchestrator/main.py:10299`
   normalizes `port` ↔ `pod_port` between provisioners on the read
   side); the default in `persistent_app.py:3207` was also flipped to
   30022 as defense-in-depth.
5. **`orchestrator/services/lifecycle/workspace_manager.py:168`** —
   `ssh_port=22` for `source_type="pod"` snapshot capture had to flip
   to 30022. VM-path callers stay on 22.
6. **Test updates:** `tests/test_container_provisioner.py` (the
   port-assertion test `test_manifest_container_spec` + a docstring
   that still claimed "required for port 22"), and
   `tests/test_persistent_app.py` (the workspace-config default-port
   assertion in `test_returns_container_config_when_ready`). The
   container_provisioner test was the one CI caught on first push.
7. **Cosmetic comment fixes in `container_provisioner.py`:** the
   security-hardening comments claiming "(required for port 22 …)"
   and "NET_BIND_SERVICE: bind to port 22" were reworded for accuracy
   — running SSHD as root is still needed for session management, and
   `NET_BIND_SERVICE` is now redundant on 30022. The capability is
   kept for now; dropping it could be a separate one-line security
   hardening PR.

**Lesson:** The original PR 2 plan was scoped by code-search on
`:22` / `:8080` literals in the four files I'd identified up front;
it missed the entrypoint script, the IDE proxy URLs, the probes, the
runtime-context publishing gap, and the lifecycle snapshot path. The
test failure CI surfaced (`test_manifest_container_spec`) was useful
not just as a stale-assertion fix but as a tripwire that exposed the
deeper `persistent_app.py` gap. Always run the full test suite
locally before declaring "mechanical" port-move work complete.

### PR 2b — docker-compose mode parity (follow-up to PR 2)

**Status:** ⏳ not started.

PR 2 changed the workspace image (SSHD → 30022, code-server → 38080)
and updated all the K8s/container provisioning paths. Docker-compose
deployments were left untouched and are currently broken: the
workspace container listens on 30022/38080 but the compose
port-mappings, the in-compose healthchecks, and the
`docker_provisioner.py` default port still assume 22/8080. K8s
deployments (the dev cluster, prod) work fine; docker-compose
deployments (single-host installs, local dev compose) do not.

Files:
- `docker-compose.yaml` — workspace service `ports:` block:
  `"…:22"` → `"…:30022"`, `"…:8080"` → `"…:38080"`.
- `docker-compose.dev.yaml` — same `ports:` flips on the five
  `workspace-N` services, plus the `healthcheck:` command's
  `/dev/tcp/localhost/22` → `/dev/tcp/localhost/30022`.
- `docker-compose.local.yaml` — same as the dev compose file.
- `orchestrator/services/docker_provisioner.py:161, 163` — the
  default port applied when `WORKSPACE_HOSTS` entries have no
  explicit `:port` suffix flips from 22 to 30022.
- `tests/test_docker_provisioner.py` — assertion-style port
  references on lines 145, 153, 164–166, 181 (and 269 for the same
  default-port shape) flip from 22 to 30022. Tests that use explicit
  `host:port` strings to verify the parser preserves the explicit
  value can stay as-is (they test parser behavior, not the default).

Estimated size: ~15 lines across 5 files + ~10 test-assertion
updates. One PR.

### PR 3 — Per-tenant tiering

**Status:** ✅ shipped 2026-05-19 (single PR including cockpit UI).

Tier names finalized as `internet-only` (default) and `home-allowed`
— closing the bikeshed flagged in the original Open questions list.
The set is closed at three layers (DB CHECK constraint, helm tier
list, cockpit `ProjectNetworkTier` type) so adding a tier later
requires a coordinated change across all three.

Shipped files:
- `orchestrator/database/migrations/app/0016_project_network_tier.sql`
  — `network_tier TEXT NOT NULL DEFAULT 'internet-only'` with a
  `NOT VALID` CHECK pattern that's validated separately to avoid
  blocking concurrent writes during the scan.
- `orchestrator/services/container_provisioner.py` — new module-level
  `DEFAULT_NETWORK_TIER` constant; `_resolve_network_tier(work_id,
  kind)` async helper joins through `projects` for jobs and threads
  (returning the default on any failure — DB unavailable, project
  unmapped, exception); `_build_workspace_labels` and
  `_build_pod_manifest` now accept the tier and emit
  `srw.io/network-tier`; all three call sites (workspace pod, IDE pod,
  thread workspace pod) resolve their tier before manifest build.
- `orchestrator/database/postgres.py` — `get_workspace_network_tier`
  (the JOIN helper above); `network_tier` added to the
  `get_project` SELECT and the `update_project` allowed-field set.
- `orchestrator/main.py` — `ProjectUpdate` Pydantic model gains
  `network_tier`; the `PATCH /api/projects/{id}` route requires
  admin when the body contains a `network_tier` field (project
  owners who aren't admins can edit other fields normally but
  cannot widen their own tier).
- `orchestrator/init.py` — new `_seed_operator_network_tier(db)`
  reads `OPERATOR_HOME_ALLOWED_EMAIL` and flips the matching user's
  default project to `home-allowed` if it's still at the migration
  default. Idempotent across reboots; one-way (never demotes).
- `helm/values.yaml` — new `workspace.networkPolicy.tiers` list of
  `{name, except: [cidr...]}` objects; new
  `workspace.networkPolicy.operatorHomeAllowedEmail` (empty default,
  set to the operator's email on homelab installs).
- `helm/templates/workspace-network-policy.yaml` — `{{- range }}`
  over the tier list, emitting one `NetworkPolicy` per entry. Each
  selects on both `srw.io/component=agent-workspace` and
  `srw.io/network-tier=<name>`. Plus a fail-closed
  `workspace-fallback-deny` policy targeting every workspace pod
  with empty ingress/egress rule sets (see "Fail-closed fallback"
  below).
- `helm/templates/configmap.yaml` + `helm/templates/orchestrator/deployment.yaml`
  — wire `OPERATOR_HOME_ALLOWED_EMAIL` from values into the
  orchestrator pod env.
- `cockpit/src/app/views/project-detail/project-detail.component.ts`
  — admin-gated "Workspace Network" settings group with a select
  bound to `settingsNetworkTier`; `onNetworkTierChange` PATCHes the
  project and reverts the signal on API error.
- `cockpit/src/app/core/models/api.model.ts` — new
  `ProjectNetworkTier` type union; `Project.network_tier` +
  `ProjectUpdateRequest.network_tier` fields.
- `cockpit/src/assets/i18n/{en,de-DE}.json` — `workspaceNetwork`,
  `networkTier`, `networkTier{InternetOnly,HomeAllowed}`,
  `networkTierDesc` strings.
- `tests/test_container_provisioner.py` — 4 new tests covering the
  resolver fallback paths (no DB, unmapped project, DB exception,
  happy path) plus a new pod-manifest test asserting the tier label
  propagates correctly when `network_tier='home-allowed'` is set.

Operator bootstrap. On homelab installs set
`workspace.networkPolicy.operatorHomeAllowedEmail` in the chart override
to the operator's email; the orchestrator's init step then flips that
user's default project to `home-allowed` on first boot (idempotent).
SaaS deployments leave it empty — every project starts and stays at
`internet-only` until an admin elevates it through the cockpit.

Fail-closed fallback. The chart also renders a single
`workspace-fallback-deny` NetworkPolicy that targets every pod with
`srw.io/component=agent-workspace`, regardless of tier label, with
`policyTypes: [Ingress, Egress]` and no rules of its own. K8s
NetworkPolicy union semantics: the tier-specific policies above keep
contributing their allows when a pod's tier label matches one of
them, so normal traffic is unaffected. The fallback only changes
behavior for pods whose tier label *doesn't* match any rendered tier
(DB ↔ helm-values drift, pre-PR-3 pod that survived an upgrade,
label-stripping accident). Without the fallback, such a pod would be
policy-less and therefore fully unrestricted by K8s default; with
it, the pod is isolated in both directions instead. Closes the only
real failure mode of the closed-tier design.

Dev cluster lock-down. `deployment/values-experimental.yaml` (the
dev cluster overlay) pins `workspace.networkPolicy.operatorHomeAllowedEmail`
to the empty string explicitly, so a future chart-default change can't
silently grant dev workspaces home-LAN access. Home-LAN reachability
is exercised only on the private prod deployment, where the
corresponding values overlay sets that field to the operator's email.

Admin gate rationale. A project owner-only path would re-introduce
the original problem: any user with project ownership could grant
their own workspaces access to the homelab LAN, defeating the
operator-side control plane. Admin-gating writes (while leaving
reads open to anyone who can see the project) preserves the
operator/user split the doc design intended.

---

## Open questions

- **Does anything currently rely on a workspace reaching a MetalLB IP
  directly?** PR 1 cuts `10.0.51.0/24` out of `except:`. The
  rationale is that in-cluster service discovery handles every
  intentional path. If a test, an agent config, or a tool somewhere
  hardcodes `10.0.51.x` and PR 1 breaks it, the right fix is to
  switch the consumer to the in-cluster ClusterIP name, not to
  re-open the MetalLB CIDR. Worth a `grep -r "10\.0\.51\." src/
  orchestrator/ config/ tests/` before shipping PR 1.
- **Move 9222 (CDP) eventually?** Currently keep, since 9222 is not
  a conventional service port and the agent is the only client. If
  any agent workflow ever needs an outbound CDP-style connection on
  9222 to a non-workspace target (improbable), the listener should
  move at that point. Not preemptive.
- ~~**Tier naming.**~~ Resolved 2026-05-19: `internet-only` (default)
  and `home-allowed`. The set is closed in the DB CHECK constraint,
  the helm `workspace.networkPolicy.tiers` list, and the cockpit
  `ProjectNetworkTier` union — widening requires a coordinated
  migration + helm + cockpit change.
- **VM workspaces — same port restructuring?** PR 2 changes only
  container workspaces. VM workspaces have their own sshd inside the
  VM image; today they listen on 22. The unification doc
  (`workspace_network_policy_unification.md`) shares the egress
  block with the container policy, so the egress hardening (PR 1)
  applies to VMs unchanged. But the listener port move is a VM
  image change separate from this work; flag it for a follow-up if
  the VM path becomes load-bearing again.
- **VPN-tunneled traffic is opaque to pod-level policy — by design.**
  A wireguard, Tailscale, or OpenVPN tunnel running from inside a
  workspace looks like UDP to a node IP from the NetworkPolicy's
  perspective; whatever flows inside the tunnel is invisible to the
  tier mechanism. This is intentional in the broader architecture —
  it's the foundation of the user VPN Networks feature
  (`docs/features/user_vpn_networks.md`), which formalizes
  platform-managed VPN tunnels as a user-attachable primitive. Tier
  policy and user VPN Networks compose at runtime: tier rules govern
  pod-network egress, user VPNs ride over the tunnel-handshake UDP
  that the tier already permits as part of basic internet egress.

---

## Affected files

**PR 1 — egress hardening:**
- `helm/templates/workspace-network-policy.yaml`
- `helm/values.yaml`
- `docs/features/workspace_network_policy_unification.md` (comment fix)

**PR 2 — listener port restructuring (as actually shipped, including
post-plan discoveries):**
- `docker/Dockerfile.workspace`
- `docker/workspace-entrypoint.sh` *(not in original plan)*
- `orchestrator/services/container_provisioner.py`
- `orchestrator/services/ide_session.py`
- `orchestrator/main.py` *(IDE proxy URLs — not in original plan)*
- `orchestrator/services/lifecycle/workspace_manager.py` *(snapshot
  ssh_port — not in original plan)*
- `src/api/persistent_app.py` *(workspace-context port default — not in
  original plan; closes runtime gap)*
- `helm/templates/workspace-network-policy.yaml`
- `tests/test_container_provisioner.py` *(port assertions + docstring)*
- `tests/test_persistent_app.py` *(workspace-config default-port
  assertion)*
- Image rebuild + chart tag bump (handled by CI; verified on
  `sha-56ec68b`)

**PR 2b — docker-compose mode parity (pending):**
- `docker-compose.yaml`
- `docker-compose.dev.yaml`
- `docker-compose.local.yaml`
- `orchestrator/services/docker_provisioner.py`
- `tests/test_docker_provisioner.py`

**PR 3 — per-tenant tiering (as shipped):**
- `orchestrator/database/migrations/app/0016_project_network_tier.sql`
- `orchestrator/database/postgres.py` *(get_workspace_network_tier
  helper + network_tier in get_project SELECT + update_project
  allowed-fields set)*
- `orchestrator/services/container_provisioner.py` *(tier resolver +
  label propagation across all three pod-creation paths)*
- `orchestrator/main.py` *(ProjectUpdate model + admin gate on
  network_tier in the PATCH route)*
- `orchestrator/init.py` *(_seed_operator_network_tier — homelab
  bootstrap)*
- `helm/values.yaml` *(workspace.networkPolicy.tiers list +
  operatorHomeAllowedEmail)*
- `helm/templates/workspace-network-policy.yaml` *(range over tiers,
  one NetworkPolicy per tier)*
- `helm/templates/configmap.yaml`
- `helm/templates/orchestrator/deployment.yaml`
- `cockpit/src/app/views/project-detail/project-detail.component.ts`
- `cockpit/src/app/core/models/api.model.ts`
- `cockpit/src/assets/i18n/en.json`
- `cockpit/src/assets/i18n/de-DE.json`
- `tests/test_container_provisioner.py` *(tier resolver + label
  propagation tests)*
- `deployment/values-experimental.yaml` *(dev-cluster lock-down:
  explicit empty operatorHomeAllowedEmail with rationale)*
- `cockpit/` (admin UI)

---

## References

### Internal
- Existing workspace policy:
  `helm/templates/workspace-network-policy.yaml`
- Policy unification (containers + VMs):
  `docs/features/workspace_network_policy_unification.md`
- User-managed VPN networks (sibling feature, user-side control plane):
  `docs/features/user_vpn_networks.md`
- Multi-tenancy plumbing this composes with:
  `docs/multi_tenancy.md`
- Auth BFF (related multi-tenant gating):
  `docs/features/auth_bff_and_api_tokens.md`
- Workspace pod manifest: `orchestrator/services/container_provisioner.py`
- Workspace image: `docker/Dockerfile.workspace`
- code-server health check: `orchestrator/services/ide_session.py`
- SFTP thread uploads: `orchestrator/services/thread_uploads.py`
- Network topology: `HomeLab/CLAUDE.md` "Network Architecture" section
- Calico install (VMs cluster only): `HomeLab/infrastructure/calico-custom-resources.yaml`

### External
- K3s built-in network-policy controller (kube-router embedded):
  <https://docs.k3s.io/networking/networking-services#network-policies>
- NetworkPolicy `ipBlock except` semantics:
  <https://kubernetes.io/docs/concepts/services-networking/network-policies/#networkpolicy-resource>
- Calico `natOutgoing` behavior:
  <https://docs.tigera.io/calico/latest/networking/ipam/ip-pool-natoutgoing>
- Common dev framework default ports:
  - Spring Boot: <https://docs.spring.io/spring-boot/docs/current/reference/html/application-properties.html#application-properties.server.server.port>
  - Next.js: <https://nextjs.org/docs/app/api-reference/cli/next>
