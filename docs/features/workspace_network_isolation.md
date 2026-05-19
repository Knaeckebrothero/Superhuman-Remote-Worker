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

### PR 3 — Per-tenant tiering (deferred, tracked separately)

Sequence after multi-tenancy P2 G2-G5 lands. Files:
- `orchestrator/database/migrations/app/NNNN_project_network_tier.sql`
  — column add.
- `orchestrator/services/container_provisioner.py` — read the tier
  from the job's project, emit `srw.io/network-tier: <value>`.
- `helm/templates/workspace-network-policy.yaml` — split into one
  policy per tier (or templatized via `range` over a values list).
- `helm/values.yaml` — new `workspace.networkPolicy.tiers` structure
  (list of `{name, except: [cidr...]}` objects).
- Cockpit admin UI — surface the tier selector on project settings.

Estimated size: ~150 lines + cockpit work. Separate PR.

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
- **Tier naming.** `internet-only` vs `default`, `home-allowed` vs
  `intranet-allowed` vs `lan-allowed` — bikeshed worth resolving
  before PR 3.
- **VM workspaces — same port restructuring?** PR 2 changes only
  container workspaces. VM workspaces have their own sshd inside the
  VM image; today they listen on 22. The unification doc
  (`workspace_network_policy_unification.md`) shares the egress
  block with the container policy, so the egress hardening (PR 1)
  applies to VMs unchanged. But the listener port move is a VM
  image change separate from this work; flag it for a follow-up if
  the VM path becomes load-bearing again.
- **Tailscale-in-workspace consequence for tiering.** If a workspace
  runs Tailscale (mesh VPN to a customer's intranet, for instance),
  the WireGuard tunnel is opaque to pod-level NetworkPolicy — once
  the tunnel is up, all traffic looks like UDP/41641 to a node IP.
  Tiering at the pod-network layer therefore can't gate traffic
  inside the tunnel; that's a Tailscale ACL concern. Same caveat as
  the unification doc, repeated here for completeness.

---

## Affected files

**PR 1 — egress hardening:**
- `helm/templates/workspace-network-policy.yaml`
- `helm/values.yaml`
- `docs/features/workspace_network_policy_unification.md` (comment fix)

**PR 2 — listener port restructuring:**
- `docker/Dockerfile.workspace`
- `orchestrator/services/container_provisioner.py`
- `orchestrator/services/ide_session.py`
- `helm/templates/workspace-network-policy.yaml`
- Image rebuild + chart tag bump

**PR 3 — tiering (deferred):**
- `orchestrator/database/migrations/app/NNNN_project_network_tier.sql`
- `orchestrator/services/container_provisioner.py`
- `helm/templates/workspace-network-policy.yaml`
- `helm/values.yaml`
- `cockpit/` (admin UI)

---

## References

### Internal
- Existing workspace policy:
  `helm/templates/workspace-network-policy.yaml`
- Policy unification (containers + VMs):
  `docs/features/workspace_network_policy_unification.md`
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
