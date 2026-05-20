# Workspace Network Policy: VM Coverage + Container/VM Unification — 2026-04-29

Workspace **containers** in the helm chart are covered by a NetworkPolicy
(`helm/templates/workspace-network-policy.yaml`). Workspace **VMs** — in
both same-cluster (the new HTTP path in `helm/templates/vm-controller/`)
and cross-cluster (`deployment-vms/srw-vm-controller/`) deployments — get
no NetworkPolicy at all today. They run with whatever default the agent
cluster's CNI gives them, which is typically allow-all.

This doc is the plan to (a) close that gap on both clusters and (b)
collapse container-workspace and VM-workspace policy into a single shared
policy where the platform-level differences allow.

---

## Problem

Workspace containers and workspace VMs are interchangeable from the
agent's point of view — same SSH/CDP/IDE surface, same datasource shell
egress, same blast-radius story per-job. The implementation differs
(container vs. KubeVirt VMI), but the **security posture should be
identical**: only the agent can reach SSH/CDP, only the orchestrator can
reach the IDE, no VM-to-VM lateral movement, no general internet.

Today only the container path enforces that posture:

| Layer                     | Container | Same-cluster VM | Cross-cluster VM |
|---------------------------|-----------|-----------------|------------------|
| NetworkPolicy ingress     | yes       | **no**          | **no**           |
| NetworkPolicy egress      | yes       | **no**          | **no**           |
| VM↔VM lateral isolation   | n/a       | **no**          | **no**           |

Cross-cluster has a circumstantial mitigation — the agent cluster
typically runs nothing besides VMs, so VM↔control-plane is empty by
construction. But "the cluster happens to be empty" is not isolation,
and same-cluster VMs lose even that mitigation.

Goal: ship one NetworkPolicy that covers both kinds of workspace, on
both clusters, and matches the existing container posture.

---

## Prior art

This is not novel territory. The official KubeVirt NetworkPolicy guide
states explicitly:

> "VMIs and pods are treated equally by network policies, since labels
> are passed through to the pods which contain the running VMI."

So a single `podSelector: matchLabels: {srw.io/component:
agent-workspace}` selects both regular pods and virt-launcher pods that
carry the label, and the same policy resource enforces the same rules
on both. Red Hat's OpenShift Virtualization docs and Stephen Nimmo's
2025 OpenShift writeup both demonstrate this pattern with the same
NetworkPolicy resource targeting a namespace mixing pods and VMs.

What I did **not** find is a public case study or KubeCon talk
specifically marketing "we use one NetworkPolicy for our pods and our
VMs" as a notable architectural choice — the literature treats it as
the obvious default rather than something to write up. Treat the
absence of war-stories as mild uncertainty rather than confirmation.

---

## What ships now (current state, for reference)

### `helm/templates/workspace-network-policy.yaml`
- Selects `app: srw-workspace`.
- Ingress: agent SSH:22 + CDP:9222, orchestrator IDE:8080, Traefik
  IDE:8080.
- Egress: DNS, TCP 80/443 (any destination — used by browser automation
  and `apt`/package fetches), Gitea:3000, App Postgres:5432, Vector
  Postgres:5432, Neo4j:7688, MongoDB:27017.
- No general internet beyond 80/443; no VM↔VM (vacuously, no VMs).

### `deployment-vms/srw-vm-controller/vm-controller.yaml`
- Defines the controller Deployment, RBAC, and the VM template
  ConfigMap. **No NetworkPolicy.**
- VM template labels: `vm.kubevirt.io/name: agent-vm-${JOB_ID}`,
  `job-id: ${JOB_ID}`. Both per-VM — no fleet-wide selector.

### `helm/templates/vm-controller/configmap.yaml` (new same-cluster path)
- Same VM template lifted from cross-cluster. Same per-VM labels, **no
  fleet-wide selector**, **no NetworkPolicy.**

---

## Design: one selector, one policy

### Stable selector across both implementations

Today workspace pods carry `app: srw-workspace` and VMs carry only
per-VM labels. The proposal is to add a **shared component label** to
both:

```yaml
srw.io/component: agent-workspace
```

- Workspace pods: add this alongside the existing `app: srw-workspace`
  (keep `app:` for back-compat with anything that already references
  it; treat `srw.io/component` as the canonical fleet-wide selector).
- VM template: add the same label to **`spec.template.metadata.labels`**
  (the VMI template), **not** the top-level VirtualMachine metadata.
  This is the well-documented gotcha — labels on the top-level VM
  metadata do not propagate to virt-launcher pods. Both
  `helm/templates/vm-controller/configmap.yaml` and
  `deployment-vms/srw-vm-controller/vm-controller.yaml` need the label
  in the right place.

A single NetworkPolicy with `podSelector.matchLabels.srw.io/component:
agent-workspace` then covers both implementations.

Avoid using KubeVirt-internal labels (`kubevirt.io/created-by`,
`kubevirt.io/domain`) as selectors — `created-by` carries a per-VMI UID
(not stable), and `domain` is documented to be copied from the *pod*
not the VM CR, which surprises operators in subtle ways.

### Why one policy works

**Ingress unifies cleanly.** Both kinds of workspace expose the same
external surface — SSH, CDP, IDE — and the same callers (agent,
orchestrator, Traefik). One ingress block fits both.

**Egress unifies — no platform carve-outs needed.** This is the part
the research clarified. virt-launcher pods do **not** need pod-network
egress to the `kubevirt` namespace: virt-handler reaches *into* the
launcher pod via a shared-hostPath Unix socket at `/var/run/kubevirt/`,
and libvirtd runs inside the launcher pod itself. The trust model is
one-way (handler→launcher), so the launcher never originates traffic
to virt-api / virt-controller / virt-handler over the pod network.
Live migration (post KubeVirt v0.45, Sept 2021) flows via
virt-handler↔virt-handler on the host network, not virt-launcher↔
virt-launcher — so even cross-node migration doesn't need a launcher
egress carve-out.

The container egress list (DNS, 80/443, Gitea, postgres, pgvector,
neo4j, mongodb) therefore works for VMs unchanged. Ship one egress
block, both implementations are happy.

**Tailscale needs a small egress addition** (applies to both pod and
VM workspaces — see next section).

**VM↔VM and container↔container lateral isolation** falls out for free:
the policy doesn't list `srw.io/component: agent-workspace` as an
allowed source, so workspaces can't reach each other regardless of
implementation.

### Tailscale / Headscale traffic through the policy

Workspace **containers** run Tailscale in the same network namespace as
the application — every packet crosses pod-level NetworkPolicy.
Workspace **VMs** run their own Tailscale daemon **inside** the VM, on
the VM's emulated NIC. KubeVirt's masquerade networking NATs that NIC
through the virt-launcher pod's veth, so the pod-level policy only
sees the encrypted WireGuard envelope.

Tailscale's documented egress requirements
(<https://tailscale.com/kb/1082/firewall-ports>):

| Port           | Protocol | Purpose                                         |
|----------------|----------|-------------------------------------------------|
| `*:41641`      | UDP      | Direct WireGuard peer-to-peer                   |
| `*:3478`       | UDP      | STUN (NAT discovery)                            |
| `*:443`        | TCP      | DERP relay fallback + control plane             |
| `*:80`         | TCP      | Captive portal detection                        |

The current container policy already allows TCP 80/443. Adding
**UDP/41641 + UDP/3478** is the only new egress required for both
container and VM workspaces. If UDP is denied (some clusters block all
UDP egress), Tailscale silently falls back to DERP-relay-over-443 —
slower but functional. So the UDP allow is "nice to have for direct
mode," not strictly required for connectivity.

**Important consequence for VMs:** once the tunnel is up, *all*
VM-internal traffic flows inside the encrypted WireGuard tunnel and is
opaque to pod-level NetworkPolicy. The egress restriction on
virt-launcher pods is therefore meaningful for **boot-time and
out-of-tunnel** traffic (DNS, package mirrors, Tailscale handshake)
and meaningless for everything the agent does inside the tunnel. We
document this honestly rather than overstating what the policy
enforces.

### Where unification breaks down: cross-cluster

On cross-cluster deployments, the orchestrator and agent pods aren't on
the agent cluster, so `from: podSelector: app: srw-agent` matches
nothing. Cross-cluster ingress arrives via Tailscale, which means:

- The packet on the destination cluster's pod network is the
  *underlay* — i.e. the encrypted UDP/41641 from a node IP, or TCP/443
  to a DERP relay. The Tailscale CGNAT IP (`100.64.0.0/10`) only exists
  *inside* the WireGuard tunnel, after decryption by `tailscaled` on
  the destination side.
- An `ipBlock: 100.64.0.0/10` rule is therefore **fantasy at the
  pod-level NetworkPolicy layer** — those packets never hit the CNI
  with a CGNAT source. (Tailscale's own GitHub tracks this confusion;
  e.g. issue #11024.)
- The Tailscale K8s operator preserves source identity at the
  application layer (HTTP headers, mTLS) but **not** at L3 — kube-proxy
  / SNAT typically rewrites to the operator-proxy pod IP or the node
  IP before NetworkPolicy evaluation.

So cross-cluster ingress restriction at the pod-level NetworkPolicy
layer cannot meaningfully filter by mesh source IP. The right answer
is:

1. **Tailscale/Headscale ACLs are the L3/L4 gate.** This is the layer
   that has the cross-cluster identity. We already use it.
2. **App-level auth at the workspace** (SSH keys, IDE token) does the
   actual authentication. We already use this too.
3. **Cross-cluster NetworkPolicy on the destination cluster** is
   limited to: VM↔VM denial (lateral isolation) and egress restriction.
   Ingress stays open at the policy layer because we cannot
   meaningfully restrict it without Cilium ClusterMesh-style shared
   identity (which we don't run).

The cross-cluster policy is therefore a **subset** of the unified one:
egress restriction + VM↔VM denial, ingress unrestricted. We document
this honestly rather than pretend cross-cluster ingress is policy-gated.

---

## CNI compatibility

NetworkPolicy enforcement on virt-launcher pods depends on the CNI:

| CNI                | NetworkPolicy on virt-launcher? | Notes                                              |
|--------------------|---------------------------------|----------------------------------------------------|
| Calico             | yes                             | Standard pod policy applies to virt-launcher veth. |
| Cilium             | yes                             | Same. Issue #37669 tracks a service-IP edge case.  |
| OVN-Kubernetes     | yes                             | Used by OpenShift Virtualization.                  |
| Antrea             | yes                             | Reported working.                                  |
| **Flannel** (alone) | **no**                         | Flannel itself doesn't enforce NetworkPolicy — needs a paired policy controller (kube-router, etc.). |
| **K3s** (default)  | yes                             | Ships Flannel + the embedded kube-router controller, which enforces by default. |
| Kube-OVN           | partial                         | Has KubeVirt-specific bugs (issue #5337).          |
| Weave              | unconfirmed                     | Could not find clear data.                         |

Operators on Flannel without a policy add-on (kube-router, etc.) see
the policy applied with no effect — the YAML is accepted but does
nothing. README must call this out explicitly so nobody gets a false
sense of isolation.

For secondary networks (Multus, SR-IOV) the standard NetworkPolicy
resource only enforces on the **primary** interface — secondary nets
need OVN-Kubernetes' `MultiNetworkPolicy` CRD. We use masquerade on
the primary interface only, so this is informational, not a blocker.

---

## Plan

### 1. Promote the shared selector

- `deployment/legacy/21d-workspace-network-policy.yaml` and
  `helm/templates/workspace-network-policy.yaml`: switch
  `podSelector.matchLabels` from `app: srw-workspace` to
  `srw.io/component: agent-workspace`.
- Wherever workspace pods are created (orchestrator's
  `container_provisioner.py`, persistent agent provisioner, the
  Compose workspace template), emit both labels: keep `app:
  srw-workspace` for compatibility, add `srw.io/component:
  agent-workspace`.
- VM template (`helm/templates/vm-controller/configmap.yaml` and
  `deployment-vms/srw-vm-controller/vm-controller.yaml`): add
  `srw.io/component: agent-workspace` to
  **`spec.template.metadata.labels`** (the VMI template), not the
  top-level VM metadata.

### 2. Extend the helm policy to cover VMs

`helm/templates/workspace-network-policy.yaml`:

- Selector → `srw.io/component: agent-workspace`.
- Egress: add UDP/41641 (Tailscale direct) and UDP/3478 (STUN). No
  `kubevirt` namespace carve-out is needed (control plane is via
  hostPath sockets, not pod network).
- Update the comment header to call out that it covers both containers
  and VMs.
- Keep the existing `workspace.networkPolicy.enabled` flag — same flag
  governs both implementations. Document that disabling it removes the
  policy for both.

### 3. Ship a NetworkPolicy on the cross-cluster install

New file `deployment-vms/srw-vm-controller/02-network-policy.yaml`:

- Same selector (`srw.io/component: agent-workspace`).
- Ingress: empty (or "from anywhere") — see "Where unification breaks
  down" above. The file's header comment documents *why* this isn't
  policy-gated (Tailscale ACLs + SSH key auth are the actual gate, and
  CGNAT IPs aren't visible at this layer).
- Egress: same shape as the helm policy (DNS, 80/443, UDP/41641,
  UDP/3478) **minus** the in-cluster DB egress (databases live in the
  orchestrator cluster — VMs reach them via Tailscale, which is opaque
  to pod-level NetworkPolicy).
- Default-deny VM↔VM by not listing the workspace component label as
  an allowed source.

### 4. Helm values + README

- `vmController.networkPolicy.enabled` is **not** added — same flag
  governs both. (If we later need separate VM-only carve-outs, we can
  split.)
- `helm/values.yaml` `workspace.networkPolicy` block gets a comment
  noting it now covers VMs too.
- `helm/README.md` "Same-cluster VMs (optional)" section gets:
  - An isolation note: container and VM workspaces share one policy,
    VM↔VM is denied, ingress is policy-gated only on same-cluster.
  - A CNI compatibility note pointing at the table above. Explicitly
    call out **Flannel does not enforce NetworkPolicy at all** —
    operators on Flannel see the YAML applied with no effect.

### 5. Verify

- `helm template` on `vmController.enabled=true,
  workspace.networkPolicy.enabled=true` → policy renders with the
  unified selector and the Tailscale egress additions.
- Smoke test: existing workspace-container e2e still passes (the
  selector change is a relabel, not a behavioral change). Manual
  check: confirm a workspace pod created post-deploy carries both
  `app: srw-workspace` and `srw.io/component: agent-workspace`.
- VM smoke test (manual, in a same-cluster setup with KubeVirt + a
  policy-enforcing CNI): VM created → SSH from agent works → SSH from
  another workspace VM denied → `apt update` inside VM works → curl
  to a non-allowed pod denied → Tailscale brings up tunnel (UDP/41641
  direct, fall back to DERP/443 if cluster blocks UDP).
- Validate label placement on virt-launcher pods after VM creation:
  `kubectl get pod -l srw.io/component=agent-workspace` should list
  the launcher pods, not zero.

---

## Open questions

- **VM↔VM might need to be allowed for some workflows.** Two jobs in
  the same project reaching each other's VMs is currently impossible
  because we don't expose the route. If we ever do (e.g. multi-VM
  jobs, distributed test fixtures), the policy needs an explicit
  `from: srw.io/component: agent-workspace` allow with restricted
  ports. Not in scope here — flagging.
- **Per-VM Service for HTTP daemon transport** (see
  `docs/issues/vm_daemon_http_transport.md`): if option 2A lands
  (orchestrator → daemon HTTP, daemon listens inside the VM), the
  policy needs an extra ingress rule for that port from
  `srw.io/component: orchestrator`. Cross-cutting with that issue —
  whichever lands first updates this policy.
- **Default-deny breaking colocated services.** Real-world cautionary
  tale: OpenShift hypershift PR #3680 had to fix a virt-launcher
  NetworkPolicy that blocked VM access to colocated control-plane
  services (oauth, ignition-server-proxy). Pattern: a strict
  default-deny without carving out same-namespace required services
  silently breaks boot. Our policy is allow-list-shaped, not default-
  deny-then-carve, so this exact failure mode doesn't apply — but it's
  a reminder to test the VM lifecycle end-to-end on a real cluster
  before declaring done, not just `helm template`.
- **Should we deprecate `app: srw-workspace`?** Once everything
  references `srw.io/component`, the legacy label is dead weight.
  Probably yes, but a separate cleanup PR after at least one release
  with both labels.

### Decided (not open)

- ~~`ipBlock: 100.64.0.0/10` for cross-cluster ingress restriction.~~
  **No.** Confirmed via Tailscale docs and operator source: Tailscale
  CGNAT IPs only exist *inside* the WireGuard tunnel; the cluster CNI
  sees only the underlay (node IPs, encrypted UDP/41641 or DERP/443).
  Pod-level NetworkPolicy with a CGNAT ipBlock matches nothing.
  Tailscale ACLs are the correct layer for tailnet-source filtering.

---

## Affected files

**Edits:**
- `helm/templates/workspace-network-policy.yaml` — selector + Tailscale
  UDP egress additions.
- `helm/templates/vm-controller/configmap.yaml` — add
  `srw.io/component: agent-workspace` to VM template labels.
- `helm/values.yaml` — comment update on
  `workspace.networkPolicy.enabled`.
- `helm/README.md` — isolation + CNI compatibility table in the
  same-cluster VMs section.
- `deployment-vms/srw-vm-controller/vm-controller.yaml` — add
  `srw.io/component: agent-workspace` to VM template labels.
- `deployment/legacy/21d-workspace-network-policy.yaml` — selector
  change so the legacy bundle stays consistent (or delete; check
  whether anything still consumes it).
- Workspace pod provisioners (`orchestrator/services/
  container_provisioner.py` and the persistent-agent equivalent) —
  emit both labels on created pods.

**New:**
- `deployment-vms/srw-vm-controller/02-network-policy.yaml` — the
  cross-cluster subset policy.

---

## References

### Internal
- Existing workspace policy: `helm/templates/workspace-network-policy.yaml`
- Same-cluster VM controller: `helm/templates/vm-controller/`
- Cross-cluster VM controller: `deployment-vms/srw-vm-controller/`
- Daemon HTTP transport (related, deferred):
  `docs/issues/vm_daemon_http_transport.md`
- VM design: `docs/features/vm.md`, `docs/features/vm_backend.md`

### External — KubeVirt
- KubeVirt NetworkPolicy guide:
  <https://kubevirt.io/user-guide/network/networkpolicy/>
- Interfaces and binding modes (masquerade/bridge/SRIOV):
  <https://kubevirt.io/user-guide/network/interfaces_and_networks/>
- Live migration (post v0.45 Unix socket model):
  <https://kubevirt.io/user-guide/compute/live_migration/>
- Security fundamentals (one-way trust, hostPath sockets):
  <https://kubevirt.io/2020/KubeVirt-Security-Fundamentals.html>
- MultiNetworkPolicy for Multus secondary nets:
  <https://kubevirt.io/2023/OVN-kubernetes-secondary-networks-policies.html>
- Migration via Unix sockets (PR #6323):
  <https://github.com/kubevirt/kubevirt/pull/6323>
- Real-world virt-launcher policy break+fix (hypershift PR #3680):
  <https://github.com/openshift/hypershift/pull/3680>
- Stephen Nimmo on OpenShift Virtualization NetworkPolicy:
  <https://stephennimmo.com/2025/02/26/setting-up-network-policies-on-a-rhel-9-vm-running-in-openshift-virtualization/>
- Red Hat: native network segmentation for VM workloads:
  <https://developers.redhat.com/articles/2025/05/01/native-network-segmentation-virtualization-workloads>

### External — Tailscale + cross-cluster
- Tailscale firewall ports (official):
  <https://tailscale.com/kb/1082/firewall-ports>
- Tailscale userspace mode + SNAT in containers:
  <https://tailscale.com/docs/concepts/userspace-networking>
- Tailscale K8s operator source IP (issue #11024):
  <https://github.com/tailscale/tailscale/issues/11024>
- Tailscale subnet router masquerading:
  <https://tailscale.com/docs/reference/troubleshooting/network-configuration/disable-subnet-route-masquerading>
- Cilium ClusterMesh NetworkPolicy:
  <https://docs.cilium.io/en/stable/network/clustermesh/policy/>
- Cilium issue #37669 (KubeVirt service IP/DNS edge case):
  <https://github.com/cilium/cilium/issues/37669>
