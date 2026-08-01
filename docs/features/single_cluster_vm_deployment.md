---
tags:
  - deployment
  - infrastructure
  - vm
---

# Single-cluster VM workspaces (co-located VM tier)

**Status:** Proposed — capability gap, not yet working
**Date:** 2026-07-31
**Related:** [`vm.md`](vm.md) · [`vm_backend.md`](vm_backend.md) ·
[`vm_agent_cluster_setup.md`](vm_agent_cluster_setup.md) ·
[`external_headscale.md`](external_headscale.md) ·
[`../issues/vm_reliability_assessment.md`](../issues/vm_reliability_assessment.md) ·
[`../issues/vm_daemon_http_transport.md`](../issues/vm_daemon_http_transport.md)

## Goal

Let an operator run the VM workspace tier **on the same cluster as the rest of the stack**, so a
single-node deployment can offer VM workspaces without standing up and maintaining a second
physically separate KubeVirt cluster.

The current shipped topology assumes two clusters: the main stack on one, agent VMs on another,
joined by a NATS leaf and a Headscale tailnet. That was a deliberate MVP choice — physical
isolation to contain the blast radius of autonomous agent workloads
([`vm.md`](vm.md), [`vm_agent_cluster_setup.md`](vm_agent_cluster_setup.md)) — and it remains the
right default for larger or higher-risk installs.

It is the wrong shape for a small business running one box. For them the second cluster is pure
overhead: another machine, another k3s, another set of upgrades, for a workload that may be a
handful of concurrent VMs. Today that operator's only options are "no VM tier" or "buy and run a
second cluster". This is a **packaging and capability gap**, and it is directly revenue-relevant
for the smaller-deployment segment.

## Current state — scaffolded, documented, and non-functional

This is the part worth being precise about, because two existing docs say the capability already
exists.

The chart **already scaffolds it**. `helm/values.yaml:1940` carries:

```yaml
vmController:
  # -- Deploy the VM controller into the same cluster as the orchestrator.
  enabled: false
  # -- Transport: "http" (no NATS, lifecycle only) | "nats" (cross-cluster,
  # full feature set) | "both" (HTTP for lifecycle + NATS for daemon events).
  transport: http
  namespace: agent-vms
```

with a `transport` enum of `http | nats | both` in `values.schema.json:563`, real templates under
`helm/templates/vm-controller/` (deployment, configmap, service, RBAC with `kubevirt.io` verbs,
namespace), and a CI render case at `helm/ci/vm-values.yaml`.

`vm_provisioner.py` likewise advertises two same-cluster transports — HTTP controller and direct
K8s API — and [`vm.md:364`](vm.md) states that "for same-cluster deployments, the orchestrator
uses in-cluster K8s config automatically (direct mode)".

**Neither same-cluster path can currently produce a working VM.** Verified 2026-07-30:

| # | Blocker | Evidence |
|---|---|---|
| 1 | **Direct mode omits four placeholders.** `_render_template` (`vm_provisioner.py:286-299`) substitutes 9 keys; the controller substitutes those plus `${ORCHESTRATOR_ID}`, `${TAILSCALE_AUTH_KEY}`, `${HEADSCALE_URL}`. A direct-mode VM boots with literal `${ORCHESTRATOR_ID}`, which `management-daemon.py:64-69` hard-requires → `sys.exit(1)` → **never registers**. | code |
| 2 | **`helm/`'s VM template omits `ORCHESTRATOR_ID` entirely** — `helm-vm-cluster/` writes it to `/etc/default/management-daemon`, `job-config.json` and `/etc/default/sudo-gated`; `helm/` writes none. Same never-registers outcome. Cause: `4d7616cf` touched only the vm-cluster copy. | rendered-chart diff |
| 3 | **`vmSshPublicKey` defaults to `""`** (`helm/values.yaml`) and renders an **empty `authorized_keys`** silently; `helm-vm-cluster/` hard-fails the same case. | rendered-chart diff |
| 4 | **`${SSH_AUTHORIZED_KEY}` is never substituted by anything** — documented as controller-filled, but `rg SSH_AUTHORIZED_KEY vm/controller/` returns 0 hits. | code |
| 5 | **`transport: http` is lifecycle-only by design** — no daemon events. But daemon register is what writes `ssh_host`, so HTTP-alone can never yield a reachable VM. `both` is the only viable HTTP setting. | values comment + `nats_bridge.py:551-671` |
| 6 | **HTTP transport discards the `rootdisk` field**, so persistent rootdisks silently do not apply; direct mode has no standalone disk at all. `VM_RECYCLE` purging a kept disk is deterministic in HTTP mode. | code |
| 7 | **The entire HTTP server side of the controller is untested** (`controller.py:1065/1117/1154/1173`, `vm_provisioner.py:761 _create_http`). CI renders the transport but no test exercises it. | test audit |

So the accurate status is: *the seams exist and are wired into the chart's value surface; the
path has never worked end to end and is not covered by any test.*

## The upside nobody has written down

Co-locating the VM tier **dissolves the single worst defect family in the VM stack**.

Today the orchestrator is not a tailnet node, while a VM workspace only has a `100.64.0.0/10`
address. Every orchestrator→VM operation is therefore structurally dead: snapshot capture, IDE
config seed, snapshot extract, SSH readiness probe. That one root cause has been filed as six
separate issues, and it is currently costing **89.7% of VM workspace snapshots**
(130 of 145 attempts lost the workspace; last successful VM snapshot 2026-07-10 — see
[`../issues/vm_reliability_assessment.md`](../issues/vm_reliability_assessment.md) P0-1).

On a single cluster the orchestrator pod and the VM sit on the same network. The orchestrator can
reach the VM directly, so:

- **Headscale and the tailscale sidecar become optional**, not load-bearing. That removes the
  headscale-latch failure mode, the keyless-VM class, and one external dependency from the
  small-deployment story entirely.
- Snapshot capture, IDE seed and the SSH readiness probe **work as originally designed**, with no
  VM-side push daemon needed.
- The NATS leaf collapses into the in-chart hub (currently dormant — see the NATS topology notes),
  removing the cross-cluster leaf link.

Stated plainly: the single-node topology is not a degraded version of the cross-cluster one. On
the axis that currently hurts most, **it is the more reliable shape** — it removes the network
partition that the cross-cluster design has to work around. The tradeoff it accepts is weaker
isolation, which is precisely the tradeoff a small single-box operator should be allowed to make.

## Options

**Option A — `transport: both`, controller co-located (recommended).**
Keep the VM controller as the sole owner of KubeVirt RBAC and the VM template, deploy it into the
same cluster, use HTTP for lifecycle and the in-chart NATS hub for daemon events. Fixes blockers
2–5 in the chart plus the `rootdisk` plumbing in the HTTP transport. Preserves one renderer and
one RBAC boundary, so cross-cluster and single-cluster stay on the same code path and the drift
that caused blockers 2–4 stops being possible.

**Option B — direct K8s mode, no controller.**
Orchestrator calls KubeVirt directly. Fewest moving parts, but it needs blocker 1 fixed, gives the
orchestrator `kubevirt.io` RBAC in its own namespace, has no standalone rootdisk support, and
creates a *second* renderer to keep in sync — the exact failure mode that produced blockers 2–4
and the description-escaping bug before it. **Not recommended.**

**Option C — do nothing; document the second cluster as a hard requirement.**
Honest and free. Costs the small-deployment segment the VM tier entirely.

Recommendation: **A**. It is mostly deletion of drift rather than new architecture, it keeps a
single renderer, and it converges the two topologies instead of forking them.

## Prerequisites and non-goals

- **KubeVirt and CDI installation stays out of chart scope** — the chart does not install them and
  should not start. They are an operator prerequisite, as with the separate cluster today. The
  chart should *detect* their absence and fail clearly rather than rendering a VM that cannot
  schedule.
- **Hardware virtualization is required on the node.** Nested virtualization if the node is itself
  a VM. Worth an explicit preflight check, since the failure mode otherwise surfaces as an
  unschedulable VMI.
- **Isolation is genuinely weaker.** Agent VMs share a node and a network with the control plane.
  NetworkPolicy scoping and a dedicated namespace (already the chart default) are the mitigations;
  they are not equivalent to physical separation. This should be stated in the operator docs, not
  papered over.
- **Non-goal: HA.** Single-node means single point of failure by construction. No live migration,
  no distributed storage.
- **Non-goal: changing the default.** `vmController.enabled` stays `false`; cross-cluster remains
  the default topology.

## Open questions

1. Does the small-deployment target need persistent rootdisks on day one, or is an ephemeral VM
   tier an acceptable first cut? (Answering "ephemeral is fine" removes blocker 6 from the
   critical path.)
2. Should Headscale be *disabled* in the single-cluster profile, or left on so one code path
   serves both topologies? Leaving it on costs a dependency; turning it off creates a second
   reachability path to test.
3. What is the realistic concurrency target for one node? Current sizing defaults are 8 vCPU /
   16 GiB per VM, so memory binds first — roughly 6–7 concurrent VMs on the current dev node.
4. Does this ship as a documented values profile, or as a named preset (`profile: single-node`)
   that sets the whole group coherently? The latter is harder to misconfigure.

## First step

Independent of which option is chosen, the **rendered-manifest contract test** proposed in
[`../issues/vm_reliability_assessment.md`](../issues/vm_reliability_assessment.md) (P2-11 Tier 1,
~0.5–1 day) catches blockers 2, 3 and 4 directly and prevents the chart drift from recurring.
`helm-vm-cluster/` currently has no `ci/` directory and is never linted or rendered, which is why
the two charts diverged unnoticed. That work is a prerequisite for trusting any single-cluster
profile, and it is worth doing whether or not this feature proceeds.
