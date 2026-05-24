# User VPN Networks: User-Managed External Network Access for Agents — 2026-05-19

This doc captures the vision for a user-facing feature that lets users
grant their agents access to external networks they control, via
user-uploaded VPN configurations. It pairs with
`docs/features/workspace_network_isolation.md`, which covers
operator-defined network access at the chart/tier level. The two
address different layers of the same overall problem (agents need
controlled access to non-public networks), but they have different
threat models, different control planes, and different audiences.

**Status:** research complete; design pending. The findings section
below captures a brainstorming research pass on 2026-05-20 (six
parallel subagents — three over the codebase, three on the public
web). The research surfaced a new strategic decision (question 0)
that needs to be resolved before the original eight open questions;
once those are answered, the doc should be rewritten as a proper
design spec.

**Last updated:** 2026-05-21 (research findings appended).

---

## Motivation

Agents in workspaces today are restricted to the public internet plus
a small set of in-cluster services (defined by the workspace
NetworkPolicy). For two distinct user populations, that's not enough:

- **Self-hosted / on-prem deployments** (developer using SRW for
  personal work; on-prem customer deployments) need agents to reach
  networks the cluster sits inside — local intranets, internal git
  servers, internal data warehouses, etc. This is what
  `workspace_network_isolation.md` PR 3 addresses, via operator-defined
  network tiers configured in `helm/values.yaml`. The operator knows
  their network and grants access at deployment time.

- **SaaS users** (cloud-hosted SRW, where the operator runs the
  cluster but is not the user's network admin) need agents to reach
  networks the *user* controls. The operator has no visibility into a
  user's home network or customer infrastructure, and shouldn't —
  that's the user's domain. Concrete example: a user maintains a
  remote server gated behind a VPN; they want their agent to reach
  the server without disclosing the VPN credentials to the platform
  operator or installing VPN client software inside the workspace by
  hand each time.

The operator-tier mechanism doesn't fit the second case. Operators
can't enumerate every user's external networks in helm values; users
can't ask the operator to modify chart values for every new VPN they
need. This feature adds a user-managed primitive for the second case.

---

## Relationship to operator tiers

The two mechanisms are siblings, not layers in a stack:

| Aspect | Operator tiers (`workspace_network_isolation.md`) | User VPN Networks (this doc) |
|---|---|---|
| **Defined by** | Cluster operator, in helm values | End user, in cockpit |
| **Scope** | Per-project or per-deployment | Per-user (and per-session) |
| **Reaches** | Networks the cluster sits inside | Networks the user controls, via tunnel |
| **State location** | Helm values + rendered NetworkPolicy | App database + uploaded VPN config |
| **Security model** | Operator vouches for the destination | User vouches for the destination (they already have access) |
| **Threat surface** | Operator-defined; locked at deploy time | User-defined; runtime-attached, but only to networks the user already reaches |

A workspace can use both mechanisms at once: it sits on some operator
tier (which governs what it can reach at the pod-network layer) and
optionally has user VPN Networks attached (which give it additional
reach via tunneled traffic).

A key property: **user VPN Networks cannot grant access the user
doesn't already have themselves.** A wireguard tunnel goes to an
endpoint the user provides; the user's keypair is required to bring
the tunnel up; the network on the other side is whatever the user's
wireguard peer is configured to expose. The feature is therefore not
a privilege-escalation primitive — it's a delegation primitive (user
delegates their own existing network access to their agent for the
duration of a session). This is the distinction that makes it safe
to expose to end users without the admin involvement that operator
tiers require.

---

## Vision

### What the user sees

A new "Networks" tab in cockpit, modeled after the existing
datasources UX (see `docs/features/datasource_redesign.md`). The user
can:

- Add a Network by uploading a wireguard config (or pasting one in).
- Name and describe it ("home network", "uni servers behind VPN",
  "customer X intranet").
- Optionally restrict which projects or sessions can use it.
- Attach a Network to a job/session at run time (similar to how
  datasources attach today).

The agent in the workspace can then reach hosts on the other side of
the tunnel for the duration of that session. When the session ends,
the tunnel comes down. No persistent state remains on the workspace.

### Initial scope

- **Wireguard only** for v1. Other VPN types (OpenVPN, IPSec, Tailscale
  as a managed mesh, …) are possible follow-ups but not in scope.
  Wireguard is small, modern, scriptable, has minimal user-side
  setup, and is the format users most commonly already have a config
  file for (Mullvad, ProtonVPN, self-hosted wireguard on a router,
  Tailscale exit nodes exported as wireguard configs, etc.).
- **Per-user ownership.** Networks are owned by the user who created
  them. Sharing a Network across users is out of scope for v1; the
  multi-tenancy doc's family/visibility model may inform this later.
- **No raw CIDR option for users.** The Networks tab is specifically
  about VPNs. Raw "let my agent reach 192.168.x.y/24 directly" is a
  privilege-escalation vector against the operator's network and
  stays in the operator-tier domain.

### Out of scope for v1

- Bulk import of VPN configs.
- Per-host (vs. per-network) egress granularity inside a tunnel.
- Sharing Networks across users or teams.
- Automatic credential rotation.
- VPN clients other than wireguard.

---

## Research findings (2026-05-20)

Six parallel subagents — three over the codebase, three on the
public web. The load-bearing observations either change the shape
of the original open questions or surface a strategic decision that
should be resolved first (now listed as question 0 below).

### In-tree precedent: the agent tailscale sidecar

The **agent pod** (not the workspace pod, which is the subject of
this feature) already runs a tailscale sidecar configured for
headscale mesh. Implementation lives in
`orchestrator/services/agent_provisioner.py:1037-1097` and
`helm/values.yaml:133-137`. Reusable shape:

- Kernel WireGuard via upstream `tailscale/tailscale:v1.82.5`
- Capabilities scoped to the sidecar: `NET_ADMIN` + `NET_RAW`
  only, not privileged
- Auth key delivered via env var `TS_AUTHKEY` from a K8s Secret
  populated by External Secrets Operator (Vault path
  `homelab/superhuman-remote-worker/srw-secrets`)
- Conditional dual-gating: helm flag `agent.tailscale.enabled`
  AND env var `AGENT_TAILSCALE_ENABLED`
- `emptyDir` 16Mi for tailscale state at `/var/lib/tailscale`
- Resource budget: 64Mi/50m requests, 128Mi/200m limits
- DNS handling: `TS_ACCEPT_DNS=false` hardcoded — no MagicDNS;
  the system uses tailnet IPs directly

Documented gotcha (commit `db8cbd59`): pods with
`restartPolicy: Never` plus a sidecar can stay in `phase=Running`
indefinitely after the main container crashes — the sidecar keeps
the pod alive, so kubelet never promotes to `Failed`. Required a
`_has_dead_agent_container()` check
(`agent_provisioner.py:437-449`) and a "crashed" reaper category
with a 60-second grace window. **Workspace pods adopting a sidecar
will inherit this problem and need the same fix.**

### Datasource pattern is the right UX template

The existing datasource subsystem is the closest in-tree precedent
for "user uploads sensitive blob, attaches to project, agent uses
at runtime."

- **Storage:** `datasources` table; `credentials` JSONB column
  with inner payload encrypted by AES-256-GCM via
  `orchestrator/security/crypto.py`. Format
  `v1:<nonce-b64>:<ct-b64>`. Key from `APP_ENCRYPTION_KEY` env
  var (32 raw bytes, base64, or 32-char alphanumeric).
- **Attachment:** N:M junction `project_datasources` (no
  per-job join). All datasources linked to a job's project are
  auto-resolved at job start via
  `resolve_datasources_for_job()` in
  `orchestrator/database/postgres.py:3257`.
- **Materialization:** `_build_datasources_payload()` in
  `orchestrator/main.py:8813` decrypts at job start and sends
  plaintext via in-cluster agent API; agent writes files to
  workspace filesystem (e.g. `~/.ssh/id_ed25519`,
  `~/.kube/config`) with mode 0600. Cleaned up at job end.
- **UX:** `cockpit/src/app/views/datasources/datasource-list.component.ts`,
  ~128 i18n keys under `datasources.*`, type-aware Angular form
  sections, "trust notice" copy pattern for credential-file types.
  The `kubeconfig`, `ssh_key`, and `generic_file` types are direct
  precedent for "user pastes/uploads text blob → encrypted at rest
  → materialized to FS at runtime."
- **Validation:** `orchestrator/security/credential_files.py`
  enforces ≤5 files per datasource, ≤64 KB each, writable-root
  allowlist (`/home/srw`, `/tmp`, `/run`, `/workspace`), blocked
  paths (`/etc`, `/proc`, `/var`).

A Networks feature mirroring this becomes: new datasource type
`wireguard_network` (or sibling `vpn_networks` table); `config`
JSONB stored encrypted; attached via `project_vpn_networks`
junction; materialized into a sidecar at job start. Database/UX
shape is essentially a clone with field renames and trust-notice
text changes.

### Workspace pod integration point

`orchestrator/services/container_provisioner.py:_build_pod_manifest()`
(line 630-762) is the single pod-spec constructor. Three callers:
`create_workspace()` for jobs, `create_thread_workspace()` for
threads, `create_ide_pod()` for IDE sessions. Today the workspace
pod has a single `workspace` container — no sidecars yet.

Security context highlights:
- `allowPrivilegeEscalation: true` (required for SSHD setuid)
- Drops ALL caps, selectively re-adds: CHOWN, DAC_OVERRIDE,
  FOWNER, SETGID, SETUID, NET_BIND_SERVICE, SYS_CHROOT, KILL,
  AUDIT_WRITE
- `seccompProfile: RuntimeDefault`
- `restartPolicy: Never`, `terminationGracePeriodSeconds: 120`

PR 3 (shipped 2026-05-19) added labels
`srw.io/component=agent-workspace` and
`srw.io/network-tier=<tier>` to all workspace pods.
`_resolve_network_tier(work_id, kind)` resolves DB → label. The
NetworkPolicy fail-closed fallback policy
(`helm/templates/workspace-network-policy.yaml`
`workspace-fallback-deny`) catches drift cases. **No dynamic K8s
Secret creation at pod time today** — a per-session VPN sidecar
would be the first feature to do that.

### Industry consensus (the strategic surprise)

Ten analogs surveyed: Coder, Gitpod, GitHub Codespaces, GitHub /
GitLab / CircleCI / Buildkite self-hosted runners, Replit,
Cloudflare Tunnel, Twingate, NetBird, Tailscale, Headscale,
ProtonVPN's server-side handling. **Nobody implements "user uploads
`wg0.conf`, platform stores encrypted, mounts into pod."** Every
modern story uses one of two patterns:

1. **Mesh-VPN with ephemeral identities.** Canonical reference:
   GitHub Codespaces + Tailscale via the `tailscale/codespace`
   devcontainer feature. User creates a tagged ephemeral auth key
   on their own tailnet, ships it via Codespaces secrets (libsodium
   sealed boxes — GitHub never sees plaintext outside the running
   container), container runs `tailscale up --accept-routes
   --auth-key=$TS_AUTH_KEY`. ACL tags on the tailnet scope what
   the workload can reach. Ephemeral nodes auto-evict within
   30-60 min of idle.
2. **User runs the connector on their side.** Cloudflare Tunnel /
   Twingate / NetBird pattern. User installs a lightweight
   connector inside their network making outbound-only connections
   to a control plane; control plane brokers identity-authenticated,
   port/host-scoped access from clients. NetBird's docs are
   explicit: *"The private key, generated by the NetBird client,
   never leaves the machine."* Only public keys + setup tokens
   transit the SaaS.

Reasons against the plain-`wg0.conf` flow are documented:
- Coder docs warn template parameters leak in cleartext UIs,
  audit logs, Terraform state: "We show parameters in cleartext
  around the product. Assume anyone with view access to a
  workspace can also see its parameters."
- wg-portal issue #420 calls server-side WG private key storage
  a vulnerability — "If the database is ever leaked or an admin
  account is compromised, every client's identity can be spoofed,
  turning a single-point compromise into a full VPN takeover."
- WireGuard ships `contrib/extract-keys/` — a kprobe tool that
  pulls live session keys from any node with root. **Decrypt-to-
  tmpfs-then-wipe is theatre against root attackers.** Once the
  WG interface is up, the static private key sits in
  `struct wg_device` in kernel memory and is reachable via
  `/dev/mem`, `/proc/kcore`, or kmod loading.
- The production failure mode that has actually bitten this
  pattern (TensorFlow supply-chain compromise via self-hosted
  runner, Shai-Hulud worm) is non-ephemeral compute becoming a
  persistent foothold inside customer LANs. Tailscale's ephemeral
  nodes + ACL tags are essentially the formalization of that
  lesson.

### WG-in-pod technical specifics (if we keep the uploaded-config model)

- **Kernel WG vs userland:** kernel needs `NET_ADMIN` + kmod on
  node; userland (`wireguard-go` / `boringtun`) needs `NET_ADMIN`
  + `/dev/net/tun` access — does NOT escape the capability
  requirement, only the kmod. Userland is 30-50% slower
  (Cloudflare BoringTun benchmarks). Fedora CoreOS nodes have the
  kmod built in since 5.6.
- **K8s native sidecar (v1.28+, `initContainers` with
  `restartPolicy: Always`) is preferred over plain initContainer.**
  Gets ordered startup before main container and restart
  semantics if the WG process dies.
- **DNS leak is the load-bearing risk.** Naive `DNS = ...` in
  `wg0.conf` overwrites kubelet-injected pod DNS and breaks
  `.cluster.local`. Fix: run a tiny `dnsmasq` in the sidecar
  that forwards only the user's specific suffix into the tunnel
  and everything else to cluster CoreDNS.
- **MTU:** 1380 for VXLAN underlay (1500 MTU − 80 bytes WG
  overhead − VXLAN headers). Default WG 1420 breaks under
  overlay CNIs. Cilium issues #42837 and #36491 document this.
- **`AllowedIPs`:** must be the user's specific CIDR, NEVER
  `0.0.0.0/0`. Use fwmark + `suppress_prefixlength` policy
  routing if we need split-tunnel:
  `wg set wg0 fwmark 51820 / ip route add default dev wg0
  table 51820 / ip rule add not fwmark 51820 table 51820 /
  ip rule add table main suppress_prefixlength 0`.
- **OOM kill = no FIN.** WireGuard protocol has no disconnect
  notification. Pod OOM-killed mid-session → peer sees half-open
  tunnel until `Reject-After-Time` (3 × 180s = 9 min) expires
  the session, or until persistent-keepalive times out.
  Mitigation: instruct users to set persistent-keepalive=25s on
  their peer side.
- **Pod restart loses tunnel state by design.** WG static private
  key in kernel memory persists until the interface is removed;
  ephemeral session keys reset on restart and trigger
  re-handshake.
- **IPv6 leak risk:** pods default to dual-stack on many
  clusters. If user's WG peer only advertises an IPv4 inner
  network, must disable v6 in the netns (`sysctl -w
  net.ipv6.conf.all.disable_ipv6=1`) or blackhole `::/0`, or v6
  egresses unencrypted.
- **Endpoint leak:** kernel stores the peer's public endpoint
  IP/port in the WG socket; visible in `/proc/net/udp` for the
  netns. Restrict outbound NetworkPolicy to that one endpoint to
  contain exfiltration if the workspace is compromised.

### Key storage realities

- **Envelope encryption is the industry baseline.** Per-row DEK
  + KEK in KMS / Vault / separate trust boundary. Defends
  against the realistic threats: DB dump leak, curious operator
  with read access, compromised app process with bounded blast
  radius.
- **`APP_ENCRYPTION_KEY` is currently the only KEK we have**
  — single key, in the orchestrator pod's env. Sufficient for
  "encrypted at rest in Postgres" but lacks per-tenant
  separation and rotation hooks.
- **Kernel residency means in-memory protection is theatre
  against root attackers.** Once the WG interface is up,
  `extract-keys` recovers the static private key from any root
  context on that node. The defensible position is: **don't
  co-tenant tunnels for unrelated users on one node** +
  decrypt-on-demand only in the orchestrator + audit log every
  decrypt with `tenant_id, key_id, pod, ts, purpose`.
- **Public-key-only is the zero-trust ideal** (Tailscale,
  NetBird, Headscale) but only works if the platform side
  generates the keypair — doesn't fit "user uploads their
  existing wg config to reach their existing network."

### Sources

Web research URLs (load-bearing):
- GitHub Codespaces private network docs:
  <https://docs.github.com/en/codespaces/developing-in-a-codespace/connecting-to-a-private-network>
- `tailscale/codespace` devcontainer feature:
  <https://github.com/tailscale/codespace>
- Tailscale ephemeral nodes:
  <https://tailscale.com/docs/features/ephemeral-nodes>
- Coder security best practices:
  <https://coder.com/docs/tutorials/best-practices/security-best-practices>
- Coder + Tailscale KB: <https://tailscale.com/kb/1163/coder>
- GitHub Well-Architected (self-hosted runner private
  networking):
  <https://wellarchitected.github.com/library/architecture/recommendations/hosted-runner-private-networking/>
- Sysdig: self-hosted runners as backdoors:
  <https://www.sysdig.com/blog/how-threat-actors-are-using-self-hosted-github-actions-runners-as-backdoors>
- Praetorian: TensorFlow runner compromise:
  <https://www.praetorian.com/blog/tensorflow-supply-chain-compromise-via-self-hosted-runner-attack/>
- NetBird how-it-works:
  <https://docs.netbird.io/about-netbird/how-netbird-works>
- Cloudflare Tunnel architecture:
  <https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/>
- Twingate architecture: <https://www.twingate.com/docs/architecture>
- Ben Hardill: K8s WireGuard client sidecar (DNS leak post):
  <https://blog.hardill.me.uk/2025/03/11/kubernetes-wireguard-client-sidecar-container/>
- Ezra Celli: WireGuard as K8s sidecar:
  <https://blog.ezracelli.dev/2023/01/10/wireguard-as-a-kubernetes-sidecar/>
- Tailscale sidecar pattern (deepwiki):
  <https://deepwiki.com/tailscale-dev/docker-guide-code-examples/1.2-core-architecture:-the-tailscale-sidecar-pattern>
- Cloudflare BoringTun perf blog:
  <https://blog.cloudflare.com/boringtun-userspace-wireguard-rust/>
- K8s sidecar containers docs:
  <https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/>
- WireGuard `extract-keys` kprobe tool:
  <https://git.zx2c4.com/wireguard-tools/tree/contrib/extract-keys/README>
- wg-portal issue #420 (server-side key storage vuln):
  <https://github.com/h44z/wg-portal/issues/420>
- HashiCorp Vault transit engine:
  <https://developer.hashicorp.com/vault/docs/secrets/transit>
- Kubernetes KMS provider:
  <https://kubernetes.io/docs/tasks/administer-cluster/kms-provider/>
- Cilium MTU issues #42837, #36491:
  <https://github.com/cilium/cilium/issues/42837>,
  <https://github.com/cilium/cilium/issues/36491>

Codebase references (file:line) all relative to
`/home/ghost/Repositories/Superhuman-Remote-Worker`:
- Agent tailscale sidecar:
  `orchestrator/services/agent_provisioner.py:1037-1097`,
  `helm/values.yaml:133-137`,
  `helm/templates/external-secret.yaml:20-21`
- Crashed-pod reaper for sidecar pattern:
  `orchestrator/services/agent_provisioner.py:437-449`
- Datasource encryption: `orchestrator/security/crypto.py`
- Datasource resolution:
  `orchestrator/database/postgres.py:3257`
- Datasource UI:
  `cockpit/src/app/views/datasources/datasource-list.component.ts`
- Credential file validation:
  `orchestrator/security/credential_files.py`
- Workspace pod manifest:
  `orchestrator/services/container_provisioner.py:630-762`
- Network tier resolver:
  `orchestrator/services/container_provisioner.py:608-628`
- PR 3 fail-closed NetworkPolicy:
  `helm/templates/workspace-network-policy.yaml:161-185`
- Headscale URL config:
  `deployment/values-experimental.yaml:106-107`

---

## Open design questions (to resolve before implementation)

The strategic question (0) was surfaced by the 2026-05-20
research; the eight original questions follow.

0. **Mesh-VPN via headscale, or stick with uploaded `wg0.conf`?**
   This is the load-bearing decision and should be resolved first.
   We already run headscale (`deployment/values-experimental.yaml`
   `headscale.url`), and the agent pod already has a tailscale
   sidecar. A mesh-VPN pivot would:
   - Eliminate at-rest custody of user private keys (we issue
     ephemeral auth keys per session instead)
   - Eliminate DNS leak / MTU / OOM / half-open / kernel
     key-residency risks (handled by `tailscaled` and tailnet
     mesh semantics)
   - Reuse the existing agent sidecar shape almost directly
   - Trade-off: requires the user to run a Tailscale subnet
     router (`tailscale up --advertise-routes=192.168.x.0/24`)
     on a host they control inside the target network. One-time
     setup per network. Trivial for self-hosters; a real ask
     for SaaS users vs. "paste a wg config."

   The doc's existing scope (per-user, per-session, no
   cross-user sharing in v1) survives unchanged either way. The
   pivot changes *what* a Network is from "an uploaded wg
   config" to "a Tailscale ACL tag plus an ephemeral auth-key
   issuer." The vision UX (Networks tab in cockpit, attach to
   project/session) is the same either way.

1. **Where does the wireguard interface actually run?** Three
   options, each with real tradeoffs:
   - *Inside the workspace pod.* Needs `CAP_NET_ADMIN` (or a device
     plugin); weakens the sandbox. Simplest control plane.
   - *As a sidecar in the workspace pod.* Sidecar holds the
     capability, workspace egresses through the sidecar's network
     namespace. Cleaner separation of concerns.
   - *As a separate per-user VPN gateway pod.* Workspace egresses to
     a gateway pod via in-cluster networking; the gateway proxies
     into the tunnel. Best sandbox isolation, most infra complexity,
     and the only model that supports multi-workspace sharing of one
     tunnel cleanly. Decision deferred.

2. **How is the VPN config stored at rest?** Wireguard private keys
   are sensitive. Options: encryption-at-rest with per-user key
   derivation; sealed-secret pattern; an external KMS. Whatever we
   pick should make plaintext access by the platform operator
   require explicit elevation, not be the default for any
   database read.

3. **Attachment model: per-project or per-session?** Per-project
   means a user attaches a Network to a project and every job in
   that project gets it. Per-session means the user picks Networks
   at session start. Likely both: project as the default, session
   as override.

4. **Concurrent VPNs.** Can one job attach multiple Networks
   simultaneously? Probably yes (different VPNs for different
   destinations). Routing precedence — which tunnel handles
   `0.0.0.0/0` if two Networks both claim it — needs spec'ing.

5. **VM workspaces.** The same mechanism should conceptually work
   for VM-based workspaces (they have their own network namespace
   by definition), but the placement of the wireguard interface
   differs (inside the VM image vs. on the host vs. in a paired
   pod). Revisit when VM workspaces become a primary path; see
   `docs/features/workspace_network_policy_unification.md`.

6. **Interaction with operator tiers.** If the user's project is on
   the `internet-only` tier (no LAN reach), can the user still
   attach a Network? The wireguard handshake is a public UDP packet
   to a public endpoint, which `internet-only` permits, so the
   tunnel comes up. Traffic *inside* the tunnel is opaque to
   pod-level NetworkPolicy and goes to whatever the user's wireguard
   peer exposes. So yes — the tier governs pod-network egress; the
   VPN tunnel rides over allowed UDP. This needs to be explicit in
   the spec so users don't read tier and VPN as independent
   dimensions.

7. **Operator disable switch.** Operators in highly regulated
   deployments (or for the student-facing university deployment) may
   want to disable the feature entirely. A helm-values flag like
   `features.userVpnNetworks.enabled` should gate the Networks tab
   in cockpit and the runtime entirely.

8. **Auditability.** Each tunnel bring-up/teardown should produce an
   audit record (which user, which Network, which session, when, for
   how long). The mongodb-backed audit trail is the natural place.

---

## References

### Internal

- Parent doc (operator network access):
  `docs/features/workspace_network_isolation.md`
- UX precedent for user-managed entities attached to jobs:
  `docs/features/datasource_redesign.md`,
  `docs/features/credential_file_datasources.md`,
  `docs/features/multi_datasource_support.md`
- Multi-tenancy context (informs future sharing model):
  `docs/multi_tenancy.md`
- VM/container policy unification (relevant if VM workspaces gain
  parity): `docs/features/workspace_network_policy_unification.md`

### External

- WireGuard: <https://www.wireguard.com/>
- WireGuard quickstart: <https://www.wireguard.com/quickstart/>
- WireGuard cryptokey routing model:
  <https://www.wireguard.com/#cryptokey-routing>
