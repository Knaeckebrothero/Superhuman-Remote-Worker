---
tags:
  - feature
  - vm
  - nats
  - jetstream
  - orchestrator
  - status
  - event-driven
related:
  - "[[vm_live_status_query_shadowed_by_jetstream_stream]]"
  - "[[database_architecture]]"
  - "[[high_availability_setup]]"
aliases:
  - event-driven VM status
  - VM status materialized view
  - VM phase watcher
---

# Event-driven VM status (controller watch → publish → materialized view)

**Status**: Proposed — design only, not built. Filed 2026-06-15 as the
independently-justified follow-up to the shipped fix in
[`docs/done/vm_live_status_query_shadowed_by_jetstream_stream.md`](../done/vm_live_status_query_shadowed_by_jetstream_stream.md).
Effort ~3–5 eng-days. Cross-repo (touches the vm-controller).

## Why

The PubAck bug is fixed, but it exposed a deeper, separate defect: **the
orchestrator's view of VM status is stale by design.**

- The VM controller (`vm/controller/controller.py`) has **no KubeVirt phase
  watcher**. `_publish_status` (controller.py:427) fires **only** inside
  `handle_create` and `handle_delete`. So the orchestrator's stored `vm` context
  only ever transitions `provisioning → deleting` — it **never sees
  `Running`, `Failed`, `WaitingForVolumeBinding`, a crash, or an OOM**.
- The only way to read real infra phase today is the on-demand
  `vm.lifecycle.get` request/reply (`nats_bridge.query_vm_status`) — i.e.
  `?live=true`. That's exactly the query that the PubAck bug shadowed, and it's a
  synchronous cross-cluster round-trip that best practice discourages for status.
- KubeVirt's `VirtualMachine.status.printableStatus` (Stopped / Provisioning /
  Running / …) already transitions continuously inside the vm cluster
  (virt-controller watches the backing pod). **The controller just isn't
  forwarding it.**
- The `VM_EVENTS` JetStream stream is currently **unconsumed dead weight** (no
  consumers anywhere; see the issue doc's research findings). An event-driven
  design is what would finally make it load-bearing.

## Goal / non-goals

**Goal**: the orchestrator maintains an always-fresh, restart-tolerant view of
each VM's phase, sourced from pushed events rather than on-demand polling.
`?live=false` becomes accurate; `?live=true` becomes an optional force-refresh.

**Non-goals**: re-litigating the PubAck fix (done); changing the provisioning
(create/delete) path; in-VM agent/daemon status (already covered by the
`agent.vm.{oid}.*.status`/heartbeat push subs).

## Design

Mirrors the standard Kubernetes operator shape — **watch → reconcile → publish
observed status** — extended across the cluster boundary via NATS.

### 1. Controller: KubeVirt watch loop (the missing piece)

Add an informer/watch loop in `vm/controller/controller.py` (new method, e.g.
`_watch_vms`, started from `run()` alongside the existing NATS transports). Use
`kubernetes.watch.Watch` over the VirtualMachine/VMI custom objects in
`VM_NAMESPACE`. On every `ADDED/MODIFIED/DELETED`, derive `{job_id, vm_name,
phase, ready, error}` (reuse the `_do_status` mapping at controller.py:289-295)
and call the existing `_publish_status` (controller.py:427) →
`vm.lifecycle.status.{oid}`.

- Include the resource UID + a monotonic `resourceVersion`/sequence so consumers
  can detect gaps.
- Handle watch expiry/reconnect with a resourceVersion resync (the cross-cluster
  analog of an informer cache resync).
- Needs KubeVirt **list/watch RBAC** in the vm-controller's chart
  (`helm-vm-cluster/` / `deployment-vms`).

### 2. Orchestrator: materialized view

`_on_vm_lifecycle_status` (nats_bridge.py:391) already persists status fields
into the job's `vm` JSONB via `_set_vm_context`. Extend the `updates` dict to
also carry `phase`/`ready`. Then:

- `?live=false` (the DB read) is always fresh — surfaces Running/Failed, not just
  provisioning/deleting.
- **No DB migration**: `context->'vm'` is freeform JSONB; new keys need no schema
  change.

### 3. Durability: durable JetStream consumer (the "proper" step)

Have the orchestrator bind a **durable consumer on `VM_EVENTS`** for
`vm.lifecycle.status.{oid}` instead of (or alongside) the current *core* push
sub (nats_bridge.py:164). This is what makes the stream load-bearing:

- **Pro**: at-least-once — status emitted while the orchestrator is restarting
  (exactly the gap a watcher widens) is replayed from the last cursor on
  reconnect.
- **Cost**: durable-consumer lifecycle per `{oid}` (create/bind, explicit
  `msg.ack()`), and `_set_vm_context` must be idempotent under redelivery (it
  already overwrites, so it's safe). This splits the bridge's subscription model
  (the `agent.vm.*` / `session.events.*` subs stay core push).

> ⚠️ **Stream subjects gate this.** `VM_EVENTS` was narrowed by the quickfix to
> `vm.lifecycle.create/delete/status.>`, so `vm.lifecycle.status.{oid}` **is**
> captured — good, the durable consumer will see it. Do not re-broaden to
> `vm.lifecycle.>` (that re-arms the PubAck bug for `vm.lifecycle.get`).

### 4. Self-healing: periodic full reconcile

The controller lists **all** VMIs every N minutes and publishes a snapshot, so
any missed/at-least-once-duplicated events self-correct. This is the
level-triggered safety net that lets the design tolerate missed watch events and
restarts — the same principle as controller-runtime's periodic resync.

### 5. `?live=true` demoted to a fallback (where B lands)

With the view always fresh, `?live=true` (`get_vm_status`, main.py) becomes:

- default → DB read (the materialized view); and
- an **optional** on-demand force-refresh that keeps the request/reply path —
  but moved to a dedicated **`vm.rpc.get.{oid}`** subject (option B from the
  issue doc), so it structurally can never be covered by an event stream again.
  This is the right moment for B, because we're already rebuilding + redeploying
  the controller for the watcher.

## Best-practice grounding

- **Push + materialized view over on-demand polling.** CQRS/materialized-view is
  the canonical "read live status without re-querying the source" pattern
  ([Azure: Materialized View](https://learn.microsoft.com/en-us/azure/architecture/patterns/materialized-view));
  HashiCorp Nomad explicitly **replaced blocking-query polling with an event
  stream** for exactly these reasons
  ([Building on Nomad's Event Stream](https://www.hashicorp.com/en/blog/building-on-top-of-hashicorp-nomad-s-event-stream)).
- **Operator idiom**: informer → workqueue → reconcile → write observed state to
  the `/status` subresource; **level-triggered reconciliation** + periodic resync
  is "why Kubernetes survives missed events, network partitions, and controller
  restarts"
  ([Red Hat: Operator Best Practices](https://www.redhat.com/en/blog/kubernetes-operators-best-practices),
  [Chainguard: The Principle of Reconciliation](https://www.chainguard.dev/unchained/the-principle-of-reconciliation)).
- **KubeVirt specifics**: `status.printableStatus`/phase is maintained by
  virt-controller watching the backing pod
  ([kubevirt#5195](https://github.com/kubevirt/kubevirt/issues/5195),
  [KubeVirt architecture](https://kubevirt.io/user-guide/architecture/)).
- On-demand query remains legitimate only as a **complement** (cold reads, drift
  detection, debugging) — never the source of truth.

## Phasing

| Phase | Scope | Outcome |
|---|---|---|
| **P1 (minimal-proper)** | Controller watch loop (§1) + orchestrator view (§2) | Fresh status; stops being stale between create/delete. Core push sub is fine. |
| **P2 (durability)** | Durable JetStream consumer (§3) + periodic reconcile (§4) | Survives orchestrator restarts + missed events; `VM_EVENTS` becomes load-bearing. |
| **P3 (cleanup)** | `?live=true` → DB read + `vm.rpc.get` fallback (§5) | Removes the synchronous cross-leaf RPC from the hot path; structurally retires the PubAck-collision class. |

## Deployment coupling & risk

- **Cross-repo, cross-cluster**: §1/§5 change `vm/controller/controller.py` →
  separate image build + `helm-vm-cluster/` chart bump + separate Fleet GitRepo
  reconcile on the vms cluster, with **prod-private lockstep** (controller and
  orchestrator must be upgraded together or VM provisioning drops silently).
- **Risks**: watch reconnect/resync loops; durable-consumer ack/dedup bugs;
  KubeVirt list/watch RBAC must be granted. All standard operator concerns, but
  they're why P2/P3 are separate from P1.

## Relationship to the shipped fix

The quickfix (guard + narrowed stream) is **complete and sufficient for the
PubAck bug** and is *not* superseded by this work — the subject hygiene it
established is a precondition here (the durable consumer in §3 relies on
`vm.lifecycle.status` being in the stream while `vm.lifecycle.get` is not). This
doc is purely about eliminating the **staleness** that made `?live=true`
necessary in the first place.
