# User VPN Networks: User-Managed External Network Access for Agents — 2026-05-19

This doc captures the vision for a user-facing feature that lets users
grant their agents access to external networks they control, via
user-uploaded VPN configurations. It pairs with
`docs/features/workspace_network_isolation.md`, which covers
operator-defined network access at the chart/tier level. The two
address different layers of the same overall problem (agents need
controlled access to non-public networks), but they have different
threat models, different control planes, and different audiences.

**Status:** vision capture only. No implementation plan, no detailed
UX, no runtime decisions made yet. Open design questions are listed
below and explicitly deferred. The next pass on this doc should turn
the open questions into design decisions before any code lands.

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

## Open design questions (to resolve before implementation)

These need answers before code lands but don't need to be locked
down for this vision doc:

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
