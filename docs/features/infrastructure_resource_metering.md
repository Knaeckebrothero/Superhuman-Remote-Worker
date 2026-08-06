---
tags:
  - design
  - observability
  - metering
  - billing
  - kubernetes
  - vm
  - storage
---

# Application infrastructure allocation metering

**Status:** Slice 0 and the shadow-only foundation of Slice 1 are implemented as
a dark launch through app migration `0088`. The dedicated Kubernetes Pod
collector, namespace-scoped RBAC, authenticated and generation-fenced ingestion,
exact LIST/WATCH continuity recovery, app-DB workspace Pod intervals, immutable
shadow comparisons, and bounded retention are present; all runtime gates still
default off. Deterministic ordinary-usage segmentation, frozen publication
plans, strict audit insert/verify, and fenced cursor finalization exist as
unwired dark code; no runtime publisher can start. There is no late/correction
publication, interval-tail/rollup handoff, billing cutover, VM collection, or
PVC/PV collection. This is not production billing readiness.

**Triggered by:** adding public-cloud comparison rate cards exposed that the
ledger's existing `vcpu-hour` / `gib-hour` arithmetic is sound, but its resource
coverage and live-window semantics are not complete enough to call the result an
application or platform infrastructure cost.

**Related:**

- [`observability_and_quotas.md`](observability_and_quotas.md) — owns the usage
  ledger and its app-owned metering posture.
- [`cloud_equivalent_usage_pricing.md`](cloud_equivalent_usage_pricing.md) —
  reprices measured quantities without mutating canonical history.
- [`usage_dashboard.md`](usage_dashboard.md) — owns the Cockpit surface.
- [`workspace_pvc_branch_a_implementation.md`](workspace_pvc_branch_a_implementation.md)
  — current workspace/session PVC identities and reclaim rules.
- [`vm_workspace_persistence_reconciliation.md`](vm_workspace_persistence_reconciliation.md)
  — VM/root-disk lifecycle ownership.

## Summary

Build one app-owned metering substrate that accounts for all product-created
pods, VMs, claims, and provisioned volumes, including resources that are still
running. This is an **application allocation** view, not a complete hardware or
provider bill.

Mature cost models distinguish workload allocation, physical assets, idle
capacity, and overhead. This feature records the first accurately and selected
storage assets explicitly; worker-node assets, idle capacity, managed control
planes, and network charges remain separate coverage domains. They must never be
silently inferred from summed Pod requests.

The result must remain truthful about five independent axes:

| Axis | Infrastructure values | Meaning |
|---|---|---|
| Measurement basis | scheduler-request / guest-provisioned / claim-requested / volume-provisioned / actual | What was measured; unlike units are not interchangeable |
| Finality | finalized / confirmed-provisional / unverified projection | Immutable history, confirmed live accrual, or an explicitly non-ledger forecast |
| Attribution | customer / shared-platform / unknown | Who owns the resource, without smearing shared or idle cost across users |
| Cost domain | workload-allocation / physical-asset / idle / overhead | Which layer of infrastructure cost the quantity represents |
| Resource class | Kubernetes Pod / VM / storage class | Which provider pricing transform may legitimately be applied |

The unified API also carries existing LLM values such as `api-consumed`,
`external-service`, and `llm-model`; the table lists the infrastructure branch,
not an exhaustive global enum.

The canonical application-allocation basis is **scheduler-requested or
provisioned capacity × confirmed wall-clock time**. Actual CPU, memory, and used
storage are an optional utilization overlay and must never silently replace the
allocation ledger. Customer billing may consume finalized quantities later, but
this feature does not declare every measured quantity billable.

## Current truth

The current workspace meter does this correctly for one closed sandbox interval:

```text
vcpu_hours = requested_cpu_cores * duration_hours
gib_hours  = requested_memory_bytes / 2^30 * duration_hours
```

A sandbox workspace requesting 8 vCPU and 16 GiB for one hour emits two rows:
`8 vcpu-hour` and `16 gib-hour`. A 4 GiB workspace running for one hour emits
`4 gib-hour` plus its independent CPU quantity.

The gaps are substantial:

1. Cockpit adds vCPU-hours and GiB-hours into one "Compute-hours" number. The
   ledger keeps them separate, but the headline and user/project breakdowns are
   dimensionally invalid.
2. Only **closed** workspace intervals are materialized. Running resources are
   absent from usage and cost estimates until deletion.
3. Any open workspace interval older than 24 hours is blindly capped and closed,
   without proving the pod is gone. A legitimate long-running workspace can be
   undercounted.
4. Agent pods, VMs, platform pods, VM launcher overhead, and resources outside
   the sandbox workspace provisioner are not metered.
5. PVCs are not metered even though they persist and consume provisioned storage
   after their pod is suspended or deleted.
6. The meter records requests, not actual utilization. That is appropriate for
   allocation and many cloud prices, but the UI does not yet explain the
   distinction or show efficiency.

Additional correctness issues discovered while scoping and researching this
work:

- One long interval currently becomes one event timestamped at its end, so a
  multi-day workspace's entire quantity appears on its close day. Stamping a
  `[00:00, next 00:00)` segment at `segment_end` would still put it in the next
  UTC day.
- `usage_events` has no typed period bounds, `resource_class`, or
  `attribution_scope`; `usage_daily` and the breakdown API would therefore lose
  distinctions the new design depends on.
- The existing ledger writer returns an ambiguous zero after either a dedupe or
  a swallowed row failure. The current materializer can then stamp an interval
  materialized even though not every audit row landed.
- Existing lifecycle list helpers commonly turn a Kubernetes/controller error
  into `[]`. That fail-safe behavior is appropriate for reaping, but an empty
  list is not a completeness proof for metering.
- A PVC request is not the same thing as a provisioned disk: delayed binding,
  oversized static PVs, resize lag, and `Retain` reclaim policy all separate the
  two lifetimes and capacities.

## Goals

- Keep vCPU-hours, memory GiB-hours, and storage GiB-hours as independent,
  meaningful quantities everywhere.
- Include live resources in summary and time-series reads without pretending
  their open interval is immutable history.
- Reconcile liveness from authoritative resource inventories; never close a
  resource merely because it is old.
- Meter sandbox workspaces, agent pods, VMs, platform pods, workspace/agent
  PVCs, persistent-session agent PVCs, VM root disks, and shared platform
  storage.
- Preserve both logical PVC demand and provisioned-volume asset quantities
  without adding them into one storage total.
- Preserve user/project/job/thread attribution where it exists and explicitly
  classify shared-platform or unknown capacity.
- Make stored quantities reusable by homelab, STACKIT, AWS, Azure, and future
  rate cards while retaining per-resource interval identity for non-linear
  provider billing rules.
- Optionally overlay actual utilization without using sampled telemetry as the
  billable allocation source.
- Provide coverage diagnostics proving which resource inventories are current
  and whether anything was left unclassified.

## Non-goals

- Importing or reconciling provider invoices. Provider cost APIs remain delayed,
  account-level validation sources, not per-user meters.
- Defining customer markup, wallets, taxes, discounts, or payment collection.
- Allocating shared platform workload, asset, idle, or overhead costs to users.
  That requires a separate and explicit business policy.
- Metering worker-node asset lifetimes, node allocatable capacity, cluster idle,
  managed control planes, load balancers, public IPs, or egress in the first
  implementation. Those remain named asset/idle/overhead gaps, not hidden Pod
  surcharges.
- GPU, ephemeral-storage, IOPS, snapshots, and object-storage metering in the
  first implementation. The taxonomy must leave room for them.
- Reconstructing accurate historical lifetimes from before this feature's
  cutover; Kubernetes does not retain enough lifecycle history to do that safely.

## Locked decisions

| # | Decision | Value |
|---|---|---|
| 1 | Scope | Application allocations and selected provisioned storage assets; never label the result a complete cluster/provider bill |
| 2 | Compute basis | Effective Kubernetes scheduler requests or provisioned VM guest capacity × confirmed wall-clock time |
| 3 | Storage bases | Keep `claim-requested` PVC demand and `volume-provisioned` PV/CSI assets separate and non-additive |
| 4 | Actual utilization | Separate optional telemetry surface; never substituted into allocation quantities or canonical cost |
| 5 | Mutable state | General `resource_intervals` plus per-inventory-scope state in the app DB |
| 6 | Immutable history | Extend existing audit-DB `usage_events`; no second canonical ledger |
| 7 | Event periods | Infrastructure events carry typed half-open `period_start` / `period_end`; `ts=period_start` for partition/day ownership |
| 8 | Live usage | Confirmed provisional tails stop at the source's last complete observation, never unconditionally at `now` |
| 9 | Publication | Strict at-least-once audit insert with deterministic keys; advance the app-DB cursor only after all expected keys are accepted |
| 10 | Reconciliation | A complete scoped LIST is absence proof; explicit hooks and WATCH improve latency but cannot weaken it |
| 11 | Identity | Stable cluster + kind + immutable Pod/VMI/PVC UID; durable volume uses HMAC'd CSI asset identity with PV UID as attachment/fallback |
| 12 | Attribution | Snapshot owner/user/project; shared, unbound, retained, and unknown capacity remains outside customer totals |
| 13 | Storage units | Raw `storage/gib-hour`; logical `claim-hour`; physical fixed fees use `volume-hour`; GiB-month is display-only |
| 14 | Cloud repricing | Match resource class/storage mapping and apply rules at the calculator's declared lifecycle, billing-occurrence, or concurrency-envelope scope before final aggregation |
| 15 | Failure posture | Metering remains non-load-bearing for provisioning, but failures are visible and never reported as zero usage |
| 16 | Shared cost | Idle and overhead stay separate unless a future explicit allocation policy distributes them |
| 17 | Historical coverage | Snapshot completeness and event-history continuity are separate; an expired-watch gap remains partial after relist |
| 18 | Source trust | Fenced leader generation plus authenticated cluster/controller identity is required before absence may finalize usage |

## Units and examples

### Exact arithmetic

Capacity is normalized once, then integrated with integer microsecond overlap.
Implementations must not use binary floating point for quantity or money:

```text
overlap_us    = max(0, min(period_end, query_end) -
                       max(period_start, query_start)) in integer microseconds
vcpu_hours    = Decimal(cpu_millicores) * overlap_us /
                Decimal(1000 * 3_600_000_000)
gib_hours     = Decimal(bytes) * overlap_us /
                Decimal(2^30 * 3_600_000_000)
instance_hour = Decimal(overlap_us) / Decimal(3_600_000_000)
```

Parse Kubernetes quantities with `kubernetes.utils.quantity.parse_quantity`,
then normalize CPU upward to integer millicores and memory/storage upward to
integer bytes, matching scheduler-compatible precision. Preserve the original
quantity strings in diagnostics. Reject negative or unparseable capacity from
customer totals and surface the object as an inventory error; never coerce it to
zero.

Use Decimal precision 50 and `ROUND_HALF_EVEN`. Persist quantity, rate, and
internal cost as `NUMERIC(38,18)`; quantize only after aggregating exact integer-
capacity × microsecond rationals. Infrastructure events retain typed source
capacity (`millicore`, `byte`, or `instance`) so partial-window reads recompute
overlap rather than infer it from a rounded day total. API aggregation rounds
once; adjacent subwindows must reconcile with their whole window within one
`10^-18` storage quantum. Provider/invoice display rounding remains a separate
calculator rule.

Examples:

| Allocation | Runtime | Emitted quantities |
|---|---:|---:|
| 8 vCPU, 16 GiB VM | 1 hour | 8 vCPU-hours + 16 compute GiB-hours |
| 500m CPU, 4 GiB Pod | 2 hours | 1 vCPU-hour + 8 compute GiB-hours |
| 250m CPU, 512 MiB agent | 30 minutes | 0.125 vCPU-hours + 0.25 compute GiB-hours |

CPU and RAM can share a resource lifecycle, but they are never added into a
single `compute-hour` scalar.

### Effective Pod requests

The collector reads the **admitted live Pod**, so LimitRange defaults, admission
mutation, RuntimeClass overhead, injected sidecars, and Pod-level resources are
included. It ports the upstream Kubernetes `PodRequests` behavior instead of
summing only `spec.containers`:

```text
regular = sum(requests of normal containers)
running_restartable_init = 0
init_peak = 0

for init_container in declaration order:
    request = requests(init_container)
    if init_container.restartPolicy == Always:       # native sidecar
        running_restartable_init += request
        init_peak = max(init_peak, running_restartable_init)
    else:
        init_peak = max(init_peak, running_restartable_init + request)

container_derived = max(regular + running_restartable_init, init_peak)
for resource in [cpu, memory]:
    base[resource] = pod_level_request[resource]
                     if that specific request is present
                     else container_derived[resource]
effective_request = base + spec.overhead
```

CPU and memory apply the algorithm independently. Ephemeral debug containers do
not reserve schedulable capacity and are excluded. Limits are not substituted
for missing requests. A missing request contributes zero **and a coverage
diagnostic**; it is not guessed from a limit or observed use. A change to any
effective request closes the old revision and opens a new one.

When the cluster exposes node-allocatable DRA claim status and the upstream
helper enables it, include any mapped CPU/memory allocation and overhead exactly
once; otherwise record that capability as unsupported rather than interpreting
an extended-resource claim as zero CPU/RAM.

Pod-level resources and in-place resize fields vary by Kubernetes version. Use a
raw/dynamic object path where the pinned Python client cannot model newer status
fields. When status-aware in-place resize is available, mirror upstream
`UseStatusResources`: calculate spec, allocated, and actuated request vectors
with the same init/sidecar rules, use their per-resource maximum, and omit the
new spec from that maximum while `PodResizePending=Infeasible`. Apply the
corresponding Pod-level status maps when supported. If a resize is detected but
required status is unavailable, hold the conservative maximum of the last valid
and new request, mark `capacity_quality=resize-status-unavailable`, and never
pretend a decrease actuated early.

The collector records cluster version/capabilities, `capacity_source`, quality,
and a first-class `measurement_algorithm` such as a pinned
`pod-requests-k8s-<release>-<commit>`. Fixtures are vendored from supported
Kubernetes release tags/commits, not a moving `master`. Golden tests compare the
local implementation for regular containers, init containers, restartable init
sidecars, Pod overhead, per-resource Pod-level fallback, and
feasible/deferred/infeasible in-place resize across the supported cluster
compatibility matrix.

The supported baseline remains Kubernetes 1.28. Capability tests explicitly
cover a 1.28 object, Pod-level requests from 1.34+, container resize status, and
the newer Pod-level resize status shape. The Python client pin is not the
capability boundary: discovery plus raw JSON decoding owns fields the generated
models do not know. Unsupported capabilities stay named in coverage.

### Compute lifetime

A Pod accrues allocation while all of these are true:

1. it has been scheduled (`spec.nodeName` is present or `PodScheduled=True`);
2. it is not in terminal phase `Succeeded` or `Failed`; and
3. an authoritative inventory still proves that UID exists.

Consequences are deliberate: unscheduled Pending Pods do not accrue; scheduled
Pending Pods, image-pull failures, initialization, CrashLoopBackOff, NotReady,
Unknown, and terminating Pods do accrue because node capacity remains reserved.
`deletionTimestamp` is intent, not proof that the Pod has stopped existing.

Preferred start time is the sane `PodScheduled=True.lastTransitionTime`, then the
first complete observation with `spec.nodeName`, then `status.startTime`. Clamp
all inferred starts to the metering cutover and never earlier than object
creation. End at the observed terminal transition, a WATCH `DELETED` event, or
absence from a complete LIST. Record `start_time_source`, `end_time_source`, and
the observation uncertainty; reconciliation cannot invent an exact deletion
instant between two snapshots.

Backdate to the scheduled transition only when an ADDED event or the preceding
complete snapshot proves the Pod was absent before that timestamp. After an
initial cutover or an unresolved collector gap, start at the first trustworthy
observation; the current admitted spec cannot reconstruct earlier resize or
attribution revisions.

A VM compute lifecycle is one KubeVirt **VMI UID**, not the stable VM name. It
starts when the VMI is scheduled and host capacity is reserved and ends when
that VMI is terminal or confirmed absent. Suspend/resume therefore produces
separate compute lifecycles. The provisioned guest vCPU and memory must be
resolved from each admitted VMI (including instancetype/admission effects) and
persisted in inventory; create-time requested size is diagnostic only and
today's defaults are never used to infer historical capacity.

### Logical claims and physical volumes

Storage has two intentionally non-additive measurement bases:

| Basis | Identity | Capacity | Lifetime | Count unit |
|---|---|---|---|---|
| `claim-requested` | PVC UID | `spec.resources.requests.storage` | creation through confirmed PVC deletion | `claim-hour` |
| `volume-provisioned` | keyed hash of cluster + CSI driver + `volumeHandle`; PV UID is an attachment fallback/incarnation | `spec.capacity.storage` | backend-confirmed or Kubernetes-visible asset existence | `volume-hour` |

Both emit `storage/gib-hour`, but every aggregate key also carries measurement
basis and resource class. The two totals describe demand and supplied assets;
they must never be added and presented as one storage number.

```text
storage_gib_hours = capacity_bytes / 2^30 * overlap_hours
claim_hours       = PVC_instances * overlap_hours
volume_hours      = durable_volume_assets * overlap_hours
```

A 20 GiB PVC existing for three hours emits `60 claim-requested gib-hour` and
`3 claim-hour`, even if its Pod ran for ten minutes. Pending claims accrue
logical demand. `WaitForFirstConsumer` can therefore produce claim demand before
any volume asset exists. Once a 25 GiB PV binds, it independently emits
`volume-provisioned gib-hour` and `volume-hour`. If the claim is deleted while a
`Retain` PV remains, claim accrual stops and the unclaimed/shared PV asset keeps
accruing.

Never persist or expose raw `volumeHandle`/CSI attributes. Derive the durable
asset ID with HMAC-SHA-256 and a stable metering identity key; importing the same
retained disk under a new PV UID must resume one asset lifecycle and one fixed
fee, not create a second disk.

Kubernetes alone cannot prove destruction of an external disk after a `Retain`
PV object itself is deleted. Transition the durable asset to
`backend-unverified`, freeze confirmed accrual at the last proof, and expose the
unknown tail separately. Close only from provider/CSI inventory or an audited
operator destruction assertion. For Delete-policy CSI volumes, PV absence is
`backend-confirmed` only when the relevant deletion finalizer was observed and
the cluster/external provisioner version guarantees backend deletion; older
supported clusters remain `kubernetes-visible`. Do not project an orphan into
the ledger forever and do not report disappearance as zero cost.

PVC expansion and PV capacity changes split their respective revisions at the
observed change time. Do not use `PVC.status.allocatedResources` as actual disk
capacity. Map a volume's `(source_cluster, StorageClass, CSI driver)` to a typed
resource class; an unknown mapping remains metered but cannot be priced as a
generic block disk.

`GiB-month` is display-only:

```text
equivalent_gib_months = storage_gib_hours / 720
```

The price card retains the provider's actual calendar/month convention and
decimal-GB versus binary-GiB basis. The 720-hour normalization is only a product
comparison convention.

## Target coverage

| Resource | Source and identity | Attribution | Ledger resource/basis |
|---|---|---|---|
| Sandbox and on-demand IDE Pods | admitted Pod UID | job/thread → user/project | `workspace_pod` / scheduler-request |
| Job/session/persistent agent Pods | admitted Pod UID + agent row | current job/thread → user/project | `agent_pod` / scheduler-request |
| Workspace VM compute | VM-controller VMI UID | job/thread → user/project | `workspace_vm` / guest-provisioned |
| Platform Pods, Jobs, and CronJobs | admitted Pod UID + owner chain | shared platform | `platform_pod:<workload>` / scheduler-request |
| Workspace and session PVC demand | PVC UID, including `pvc-workspace-*`, `pvc-ws-thread-*`, `pvc-agent-s-*`, and `pvc-persistent-*` | job/thread → user/project | typed PVC resource / claim-requested |
| VM root-disk demand | cluster PVC UID; DataVolume/controller only resolves ownership | job/thread → user/project | `vm_rootdisk_claim` / claim-requested |
| Platform and golden-image PVC demand | PVC UID + workload/controller mapping | shared platform | typed PVC resource / claim-requested |
| Provisioned volumes | HMAC durable CSI asset ID, with PV UID attachment/fallback | bound owner, otherwise shared/unknown | typed volume resource / volume-provisioned |
| Unknown object in an included scope | authoritative object UID | explicit unknown | `unclassified_<kind>` / native basis |

Unknown resources are recorded, not dropped. Completeness is more important than
a perfect label. A v1 classification fix splits a forward revision; already
published unknown history stays unknown until the separately audited correction
workflow exists. Omitted capacity would be invisible altogether.

Classification order is deterministic: dynamic product identities first,
controller/DB ownership second, chart labels and owner references third, and
unknown last. Product-created Pods can also carry generic Helm labels, so label
order must not turn an agent into a shared-platform workload. Kubernetes labels
are untrusted attribution hints; resolve referenced jobs/threads in the app DB
before snapshotting user/project. Malformed or dangling identifiers become
unknown, never another customer's usage.

KubeVirt `virt-launcher` Pods are excluded from scheduler-request allocation in
v1 because VM guest capacity is already metered by VMI UID. Launcher overhead
remains a named coverage exclusion until a reviewed host-overhead algorithm can
record only a separate shared delta. VM storage is read once from the root PVC;
the DataVolume is an ownership/provisioning link, not a second disk.

Initial Kubernetes inventory scopes are the Helm release namespace and every
configured workspace/agent namespace. Additional namespaces require an explicit
allowlist plus matching namespace-scoped Roles/RoleBindings. PV inventory is
cluster-scoped and needs a separately reviewed least-privilege permission. The
collector inventories the full scope and classifies client-side; label selectors
must not hide malformed or unknown resources.

## Architecture

```text
Kubernetes LIST/WATCH ───┐
                         ├─> normalized complete inventory snapshots
VM-controller inventory ─┤                 │
confirmed lifecycle hooks┘                 v
                                  resource_intervals (app DB)
                                      │             │
                        strict finalized segments   └─ confirmed live tail
                                      v                         │
                             usage_events (audit DB)             │
                                      │                         │
                             usage_daily_v2 (app DB) ──────────┤
                                                                v
                                                        GET /api/usage/v2
                                                                │
                                                 typed rate-card calculators
```

One leader performs inventory reconciliation and materialization. Explicit
lifecycle hooks remain useful for low-latency opening and for confirmed terminal
events, but inventory is the repair path and source of completeness. A delete
API acknowledgement is not a terminal event: close only after a terminal
status, WATCH `DELETED`, successful GET-not-found, or complete-inventory absence.
A missed hook must not permanently lose usage.

The existing lifecycle/reaper protocol is **not** reused as the metering
inventory contract. Several lifecycle adapters intentionally convert errors to
empty lists to avoid blocking cleanup, and their items omit UID and admitted
capacity. Metering gets a separate typed collector whose empty complete snapshot
is a proof and whose failed attempt is explicitly incomplete.

### Inventory contract

Every collector attempt returns one envelope for one exact scope:

```text
InventorySnapshot {
  collector_id: string
  source_cluster: stable string
  api_resource: string             # e.g. core/v1/pods or kubevirt.io/v1/VMIs
  namespace: string | null         # null only for a reviewed cluster scope
  collection_started_at: timestamp
  collection_completed_at: timestamp
  received_at: timestamp
  source_snapshot_at: timestamp | null
  complete: boolean
  snapshot_id: uuid
  leader_generation: integer
  controller_epoch: string | null
  sequence: integer | null
  resource_version: opaque string | null
  item_count: integer
  item_digest: string | null
  items: InventoryItem[]
  fatal_errors: InventoryError[]
  item_errors: InventoryError[]
}
```

This is a logical wire/typing envelope, not a requirement to accumulate the
whole `items` array in process memory. Kubernetes pages and VM chunks normalize
directly into bounded staging rows; the final envelope metadata commits only
after the declared count and digest verify.

An incomplete Kubernetes LIST publishes metadata and sanitized diagnostics
only: locally observed positive rows may refresh the collector's bounded resize
cache, but no partial item manifest is uploaded. Repeated failures use bounded
exponential retry up to the configured relist interval (capped at five minutes),
preventing a failing scope from generating unbounded non-authoritative rows.

`complete=true` is legal only when the collector has read the whole exact scope.
It cannot accompany fatal identity/decode errors, missing continuation pages, an
interrupted stream, timeout, auth failure, or stale controller sequence. A
capacity/classification error on an otherwise identifiable UID is an item error:
the UID is marked present so it cannot be closed, but its last valid capacity is
not extended and the error does not prevent safe absence reconciliation for
other UIDs. Its capacity coverage is partial until repaired/waived. Unknown but
valid attribution is not an error and opens an `unknown`
interval. The item digest is over sorted `(kind, UID, revision_hash-or-invalid)`
tuples and makes repeated snapshots auditable without retaining every raw object
forever. Errors contain class, scope, and redacted message; they never contain
credentials or full objects.

Canonical accounting observations use the orchestrator/app-DB receipt clock;
remote clocks are evidence only. Retain collection start/completion and optional
source time as uncertainty bounds. Pod terminal end is the DB observation of
terminal status, never one container's `finishedAt`. VMI phase-transition times
may supply a more precise start/end only when parseable UTC, not before creation,
and within configured clock-skew tolerance of receipt; otherwise use DB
observation. A complete-LIST absence ends conservatively at completed receipt
and records uncertainty since the UID's prior proof. Deletion during a slow
multi-page LIST is therefore visible in uncertainty rather than assigned a fake
exact timestamp.

The VM transport publishes separate scoped authorities instead of pretending
VM, VMI, DataVolume, and PVC lists are one atomic Kubernetes snapshot:

- an unfiltered paginated VMI inventory owns compute identity/liveness and
  admitted guest capacity;
- VM plus orchestrator DB mapping supplies run strategy and attribution hints;
  and
- separate PVC/PV inventory scopes in the resource's cluster own storage
  capacity/liveness, with DataVolume only supplying an ownership link.

A missing join becomes unknown attribution/storage linkage and does not
invalidate otherwise complete VMI presence. Derive vCPU topology/instancetype
and guest memory from the admitted VMI, not controller defaults or VM
`printableStatus`. `status.nodeName`, phase, and sane phase-transition timestamps
drive lifetime. Paused and migrating VMIs remain active; a restart creates a new
VMI UID.

Each remote scope carries `schema_version`, authenticated source identity,
request nonce, leader generation, controller epoch, monotonic sequence, snapshot
ID, chunk index/count, total item/byte limits, and a canonical digest. HTTP may
carry one bounded envelope. NATS uses correlated begin/items/end frames and a
temporary reply subscription; lost, duplicate, reordered, stale, unsolicited,
oversized, same-sequence/different-digest, or deadline-expired frames fail the
attempt. The controller retains a bounded sequenced change journal/tombstones
until acknowledged. A current-state snapshot alone cannot reveal a VMI created
and deleted entirely between polls; an unavailable journal creates an explicit
coverage gap rather than a claim of complete history.

Publication from the VM source cannot be enabled over today's unauthenticated
HTTP/NATS posture. Require mTLS or a rotated service token for HTTP, or NATS
accounts/NKeys with publish/subscribe subject ACLs. Bind stable cluster ID to the
authenticated source identity and reject a second installation using it.

### Kubernetes LIST/WATCH algorithm

Each scope key is exactly `(stable cluster ID, API group/version/resource,
namespace)`. Collection follows this state machine:

1. Perform an unfiltered LIST with resource version unset, following every
   continuation token. All pages belong to the same LIST snapshot. If any page
   fails, discard the attempt and close nothing.
2. Normalize each page into bounded staging rows under an incomplete snapshot
   ID. After every page/count/digest validates, one set-based app transaction
   marks the snapshot complete, reconciles returned UIDs, marks intervals with
   `last_seen_snapshot_id`, closes open UIDs absent from this exact scope, and
   saves the opaque LIST resource version. A crash leaves only expirable
   incomplete staging and cannot authorize absence.
3. WATCH from that resource version. `ADDED` and `MODIFIED` open/confirm/split an
   interval; `DELETED` closes it. A BOOKMARK advances watch transport state but
   is not proof that every existing object is still alive. Commit each normalized
   object mutation and the new watch cursor atomically and in stream order.
4. On HTTP 410/expired history, disconnect, perform a new complete LIST, and
   resume. Independently perform periodic full LISTs so a long-lived but damaged
   watch cannot make stale capacity look current.

Never use resource version `0` or a label-filtered LIST as absence proof. Treat
resource versions as opaque and stream-local. `last_confirmed_at` advances for
an object only when that UID is actually present in a complete LIST or object
event; a quiet WATCH connection alone does not extend accrual.

A complete LIST proves the objects present **at that snapshot**, not every
short-lived object during a preceding disconnected watch. Track continuous
coverage epochs. A watch-history loss or missing VM-controller tombstone closes
the old epoch and opens an `unknown` time range; the recovery LIST begins a new
epoch but cannot erase the gap. Daily rollup pauses at the gap until an operator
either backfills it from durable lifecycle hooks/journal or explicitly waives it
as a partial day. Waivers preserve the unknown range in every API/rollup result.

`POST /api/admin/usage/v2/coverage-gaps/{id}/waive` is the v1 waiver operation.
It requires a real fleet-view admin (view-as, project tokens, and ordinary users
are denied), reason plus idempotency key, and records actor/time/reason in the
app audit/change log. It resolves only the rollup blocker: every intersecting day
remains `partial`, the unknown range remains visible, and no quantity is
invented. Durable backfill is a distinct resolution with evidence metadata.

### Inventory state models

App migration `0086` adds metadata for every attempt and the current scope
watermark. Exact names can change, but the state boundary cannot:

```sql
resource_inventory_scopes (
  id                         uuid primary key,
  collector_id               text not null,
  source_cluster             text not null,
  api_resource               text not null,
  namespace                  text,
  unique nulls not distinct
    (collector_id, source_cluster, api_resource, namespace)
)

resource_inventory_scope_epochs (
  id                         uuid primary key,
  scope_id                   uuid not null,
  epoch_number               bigint not null,
  reliable_from              timestamptz,
  required_for_rollup        boolean not null,
  required_from              timestamptz,
  retired_at                 timestamptz,
  coverage_mode              text not null,
  capture_epoch              uuid,
  last_attempt_at            timestamptz,
  last_complete_at           timestamptz,
  last_complete_snapshot_id  uuid,
  last_resource_version      text,
  controller_epoch           text,
  last_sequence              bigint,
  leader_generation          bigint not null,
  continuous_since           timestamptz,
  complete_through           timestamptz,
  snapshot_health            text not null,
  continuity_health          text not null,
  item_health                text not null,
  backend_health             text not null,
  publication_health         text not null,
  consecutive_failures       integer not null,
  last_item_count            integer,
  sanitized_error            jsonb,
  unique (scope_id, epoch_number)
)

resource_inventory_snapshots (
  id                    uuid primary key,
  scope_epoch_id        uuid not null,
  collection_started_at timestamptz not null,
  collection_completed_at timestamptz not null,
  received_at           timestamptz not null,
  source_snapshot_at    timestamptz,
  complete              boolean not null,
  leader_generation     bigint not null,
  resource_version      text,
  controller_epoch      text,
  sequence              bigint,
  item_count            integer not null,
  item_digest           text,
  fatal_errors          jsonb not null,
  item_errors           jsonb not null,
  created_at            timestamptz not null
)

resource_inventory_snapshot_items (
  snapshot_id           uuid not null,
  source_kind           text not null,
  source_uid            text not null,
  revision_hash         text,
  normalized_item       jsonb not null,
  valid_for_metering    boolean not null,
  item_error            jsonb,
  primary key (snapshot_id, source_kind, source_uid)
)

resource_inventory_coverage_gaps (
  id                    uuid primary key,
  scope_epoch_id        uuid not null,
  gap_start             timestamptz not null,
  gap_end               timestamptz,
  reason                text not null,
  resolution            text not null, -- unresolved | backfilled | waived
  resolution_details    jsonb not null,
  resolved_at           timestamptz,
  resolved_by           uuid
)

infra_metering_control (
  singleton             boolean primary key default true,
  leader_generation     bigint not null,
  cutover_state         text not null, -- disabled | preparing | active
  cutover_at            timestamptz,
  updated_at            timestamptz not null
)
```

Keep attempt metadata long enough to diagnose an invoice window; raw Kubernetes
objects are not stored. A configurable stale threshold marks snapshot freshness
stale independently of event continuity, item validity, backend truth, and
publication health. It freezes confirmed live accrual at the last proof and
raises an operator metric; it never closes intervals.

Scope requirements are effective-dated, never a mutable global switch. Start a
new namespace/PV/controller epoch in shadow with `required_for_rollup=false`;
set `reliable_from` after its first continuous proof and normally create a new
required epoch with `required_from` at the next UTC midnight. Retirement also
gets an explicit boundary. A check requires `required_from` exactly when
`required_for_rollup=true`, `required_from >= reliable_from`, and every
retirement after the epoch's other populated boundaries. Each daily seal
evaluates only required epochs whose `[required_from, retired_at)` overlaps that
day, so adding PV coverage today neither blocks history nor claims it existed
yesterday.

The existing advisory leader lease is cancellation, not a fencing token. On
every acquisition, increment `infra_metering_control.leader_generation`; stamp
the generation on collection work and require it to equal the current generation
on every snapshot commit, absence close, interval split, publication-plan/CAS,
day seal, and cutover mutation. Cancel watches on lock-connection loss. A new
leader performs a fresh LIST before absence closure. A delayed prior leader can
replay harmless audit inserts but cannot mutate current app state.

### `resource_intervals` app-DB model

Proposed columns (exact migration naming may vary):

```sql
resource_intervals (
  id                    uuid primary key,
  inventory_scope_id    uuid not null,
  source_cluster        text not null,
  source_kind           text not null,      -- pod | vmi | pvc | volume
  source_uid            text not null,
  source_api_version    text not null,
  source_resource_version text,
  source_lifecycle_id   uuid not null,
  revision_no           bigint not null,
  source_revision       text not null,
  namespace             text,
  name                  text not null,
  category              text not null,      -- compute | storage
  resource              text not null,
  measurement_basis     text not null,
  cost_domain           text not null,
  resource_class        text not null,
  attribution_scope     text not null,      -- customer | shared-platform | unknown
  owner_kind            text,               -- job | thread | platform | unknown
  owner_id               text,
  user_id               uuid,
  project_id            uuid,
  attribution_source    text not null,
  attribution_quality   text not null,
  backing_resource_uid  text,
  lifecycle_confidence  text not null,
  cpu_millicores        bigint,
  memory_bytes          bigint,
  storage_bytes         bigint,
  capacity_source       text not null,
  capacity_quality      text not null,
  measurement_algorithm text not null,
  started_at            timestamptz not null,
  start_time_source     text not null,
  start_uncertainty_us  bigint not null,
  ended_at              timestamptz,
  end_time_source       text,
  end_uncertainty_us    bigint,
  last_seen_at          timestamptz not null,
  last_confirmed_at     timestamptz not null,
  last_seen_snapshot_id uuid,
  materialized_through  timestamptz not null,
  end_reason            text,
  details               jsonb not null,
  created_at            timestamptz not null,
  updated_at            timestamptz not null
)

resource_lifecycle_heads (
  source_lifecycle_id   uuid primary key,
  latest_revision_no    bigint not null,
  current_interval_id   uuid,
  updated_at            timestamptz not null
)

resource_publication_plans (
  id                    uuid primary key,
  source_interval_id    uuid not null,
  source_revision       text not null,
  plan_kind             text not null, -- usage | late-usage | correction
  plan_revision         bigint not null,
  advances_cursor       boolean not null,
  previous_materialized_through timestamptz,
  correction_group_id   uuid,
  period_start          timestamptz not null,
  period_end            timestamptz not null,
  expected_event_count  integer not null,
  payload_schema_version integer not null,
  hash_algorithm        text not null,
  event_set_hash        text not null,
  rate_selection_hash   text not null,
  creator_generation    bigint not null,
  state                 text not null, -- planned | published | conflict
  attempt_count         integer not null,
  last_attempt_at       timestamptz,
  sanitized_error       jsonb,
  published_at          timestamptz,
  created_at            timestamptz not null,
  unique (source_interval_id, period_start, period_end,
          plan_kind, plan_revision)
)

resource_publication_plan_events (
  plan_id               uuid not null,
  ordinal               integer not null,
  source                text not null,
  source_id             text not null,
  unit                  text not null,
  ts                    timestamptz not null,
  event_kind            text not null,
  canonical_rate_version_id uuid,
  row_hash              text not null,
  event_payload         jsonb not null,
  primary key (plan_id, ordinal),
  unique (source, source_id, unit, ts)
)

infra_usage_day_state (
  day                   date primary key,
  state                 text not null, -- open | sealing | sealed
  coverage_status       text,          -- complete | partial
  coverage_revision     text,
  unknown_ranges        jsonb not null,
  sealed_at             timestamptz,
  updated_at            timestamptz not null
)
```

Required invariants:

- A partial unique index permits one open revision per `(source_cluster,
  source_kind, source_uid)`. `(source_lifecycle_id, revision_no)` is unique.
  Reconciliation locks the lifecycle head, allocates the next revision number,
  and validates half-open non-overlap before commit; stale/out-of-order work
  cannot create a duplicate closed interval.
- A spec/capacity change closes the current revision and opens the next; history
  is never updated to the new size retroactively.
- `source_revision` hashes category, resource, basis, domain, class, attribution
  and its provenance, owner/user/project, every capacity, measurement algorithm,
  durable backing identity/confidence, and all price-relevant normalized fields.
  A change to any event-affecting input splits the interval; irrelevant status
  churn does not.
- Capacity columns are non-negative; `ended_at >= started_at`; and
  `started_at <= materialized_through <= coalesce(ended_at,
  last_confirmed_at)`. Invalid objects remain visible in snapshot diagnostics
  but do not create customer quantities.
- Uncertainty values are non-negative. `capacity_quality` and
  `attribution_quality` are closed enums; unknown/invalid is never encoded as a
  plausible zero or null customer dimension.
- `user_id` and `project_id` are attribution snapshots, not foreign keys. Owner
  deletion must not erase or block usage finalization.
- `materialized_through` advances only after the corresponding deterministic
  `usage_events` segment is accepted.
- `source_lifecycle_id` is stable across capacity/attribution revisions of one
  observed Pod, VMI, claim, or durable volume asset. It lets repricing reassemble
  day segments without asserting that every provider groups them identically.
- Publication plans are transactional transport/read state, not a second usage
  ledger. They freeze the exact dimension rows and each row's optional immutable
  canonical rate version before cross-database I/O, survive a crash, and are
  retained until the post-rollup diagnostic horizon. Each plan event contains
  one dedupe key, event kind, row hash, payload, and its rate-version reference;
  `event_set_hash` hashes ordered row hashes and count, while
  `rate_selection_hash` also commits explicit unpriced selections. V2 rate edits
  create a new effective version; they never mutate a version referenced by a
  plan.
- Ordinary and cursor-advancing late plans use `plan_revision=0`, require
  `advances_cursor=true`, require the exact prior cursor, and leave correction
  fields null. Correction plans do not advance an interval cursor, allocate a
  monotonic revision for the affected interval/range, and use their own plan ID
  as `correction_group_id`; all of their signed delta rows publish atomically.
- Planning locks/checks the corresponding `infra_usage_day_state`. Before a day
  is rolled up, the leader seals it only after every overlapping interval cursor
  reaches day end/its earlier close, no plan remains unpublished, and required
  coverage is complete or explicitly waived partial. Normal v2 usage planning
  for a sealed day is rejected. Newly discovered missing usage may use a typed
  non-negative `late-usage` plan with discovery evidence; it dirties and marks
  the day partial. Changing an already-published row needs the correction path.
- Retain interval identity for at least the longest supported comparison and
  billing horizon. Deleting it earlier makes exact non-linear repricing
  impossible even though aggregate ledger quantities remain.
- App-local scope/snapshot/gap/interval/plan relationships use `ON DELETE
  RESTRICT` foreign keys. Only user/project/owner snapshots deliberately remain
  soft cross-database/lifetime references.

### Usage-ledger schema extension

Audit migration `0003` adds nullable typed fields so legacy LLM events remain
valid:

```text
period_start, period_end               # half-open [start, end)
measurement_basis, cost_domain
resource_class, attribution_scope, measurement_algorithm
source_capacity_value, source_capacity_unit
source_cluster, source_kind, source_uid
source_lifecycle_id, source_interval_id
event_kind                                 # usage | late-usage | correction
corrects_source, corrects_source_id, corrects_unit, corrects_ts
correction_group_id, correction_reason, correction_actor_id, discovered_at
payload_hash
```

Infrastructure v2 events require the typed identity/period/hash fields;
correction metadata is conditional on event kind. Point events such as LLM
requests keep null period bounds. Add checks for paired bounds and positive
periods. Audit checks also require `ts=period_start` and every typed dimension/
hash for infra v2, prevent a segment from extending past the next UTC midnight,
require non-negative ordinary/late usage, and require the full original key plus
reason/actor/group on corrections. `usage_daily_v2` keys `(day, user, project, category, resource, unit,
measurement basis, resource class, attribution scope, cost domain, measurement
algorithm)` using `NULLS NOT DISTINCT` uniqueness.
The companion table avoids replacing the old conflict target under rolling
replicas; a later migration can consolidate the read models.

Its measures are `quantity`, nullable `cost_usd`, `priced_quantity`,
`unpriced_quantity`, `priced_events`, `unpriced_events`, and total `events`.
Aggregate cost is null when no row was priced; it is the sum of known cost when
some were priced. Derive `priced`, `partially-priced`, or `unpriced` from the
coverage measures. A present zero rate is genuinely free and counts as priced;
a missing rate never becomes zero through `COALESCE`.

```sql
usage_daily_v2 (
  day date,
  user_id uuid, project_id uuid,
  category text, resource text, unit text,
  measurement_basis text, resource_class text,
  attribution_scope text, cost_domain text, measurement_algorithm text,
  quantity numeric(38,18), cost_usd numeric(38,18),
  priced_quantity numeric(38,18), unpriced_quantity numeric(38,18),
  priced_events bigint, unpriced_events bigint, events bigint,
  updated_at timestamptz,
  unique nulls not distinct
    (day, user_id, project_id, category, resource, unit,
     measurement_basis, resource_class, attribution_scope, cost_domain,
     measurement_algorithm)
)
```

The API must retain category in every key: compute-memory `gib-hour` and storage
`gib-hour` cannot collide. It also retains basis: claim-requested and
volume-provisioned storage are non-additive even though both use `gib-hour`.

Do not update immutable legacy audit rows merely to fill new columns. The v2
rollup/query adapter applies source-specific compatibility dimensions: legacy
LLM rows become `api-consumed / external-service / llm-model`; legacy workspace
rows become `scheduler-request / workload-allocation / kubernetes-pod`.
Attribution scope derives from the legacy owner snapshot where unambiguous and
otherwise stays `unknown`. `usage_daily_v2` aggregates both legacy and v2 rows so
the typed endpoint has one closed-day source.

Legacy workspace rows remain `legacy-end-stamped` because their canonical audit
schema has no typed period and used float arithmetic. Validated detail timestamps
may power a clearly labeled approximation, but v2 does not mutate or claim exact
partial-window/day reconstruction before each source's `reliable_from` cutover.

Canonical event charging for new infrastructure, if configured, resolves from
an empty-by-default typed `usage_rates_v2` table with immutable UUID versions and
exact selectors and non-overlapping effective intervals:

```text
usage_rates_v2 (
  id, cost_domain, measurement_basis, category, resource_class,
  resource, unit, effective_from, effective_to,
  usd_per_unit NUMERIC(38,18), source, source_version, created_at
)
```

Rates are non-negative, USD, and linear per canonical measured unit. Rate terms
are immutable; the only update is setting an open version's `effective_to` once
before inserting its successor. Absence means unpriced. A database exclusion
constraint prevents two
versions from covering the same exact selector and instant. This table never
reuses the legacy all-compute wildcard for new classes. It snapshots
customer/internal ledger cost; public-cloud scenario cards and their non-linear
calculators remain the separate versioned system below.

### Strict cross-database publication

The current best-effort ledger method cannot be used by this materializer. Add a
strict batch API with this protocol:

1. In a short app-DB transaction, select a bounded interval batch with `FOR
   UPDATE SKIP LOCKED`, calculate the next deterministic day/rate-boundary
   segment, query every applicable immutable canonical rate version directly
   (never the current five-minute cache), and insert/reuse its
   `resource_publication_plans` row. Commit before network/audit I/O; never hold
   an app row lock across databases.
2. From the frozen plan, verify the target monthly audit partition exists. The
   existing `audit_partitions.py` maintenance owner alone performs low-lock
   CREATE/ATTACH DDL; a missing leaf leaves the plan pending and alerts. In one
   audit-DB transaction,
   bulk insert every row with `ON CONFLICT DO NOTHING`, then select every
   expected dedupe key and compare its row hash. A missing key or different
   payload fails the whole attempt. There is no per-row fallback and no swallowed
   exception.
3. Commit audit DB. In one app-DB transaction, mark the plan published and
   for each cursor-advancing plan compare-and-set the interval from the exact old
   revision/cursor to `segment_end`. Correction-only plans dirty their affected
   days but never move an interval cursor.
4. If the process crashes between commits, replay finds and verifies the same
   frozen plan/audit rows, then advances the app cursor. It cannot adopt a newer
   rate, create a duplicate, or falsely mark an absent row complete.

Plans are irrevocable delivery intent. Leadership loss prevents new planning and
causes the old leader's app CAS to fail its generation fence, but it does not
cancel/delete a committed plan; the new leader adopts it, verifies any audit row
the old leader committed, and advances exactly once.

The deterministic audit key remains `(source, source_id, unit, ts)`. Ordinary
and late rows use `source=infra-allocation-v2`, `ts=period_start`, and a
`source_id` derived from `(source_interval_id, period_start, period_end)`.
Correction deltas use `source=infra-allocation-correction-v2` and a `source_id`
derived from `(correction_group_id, ordinal)`, so they cannot collide with the
immutable original; the correction plan UUID is its group ID. The payload hash
covers all typed dimensions, attribution,
quantity, rate, and cost. Negative quantities are allowed only for a typed
`event_kind=correction` row that references the full original `(source,
source_id, unit, ts)` key, uses its old dimensions, shares a
`correction_group_id` with any positive replacement delta, and carries an
operator/reason audit trail; `usage` and `late-usage` are non-negative. V1
supports automatic late usage backed by a durable hook/journal, but not manual
alteration of an existing event. Such a conflict blocks/marks coverage for
operator resolution until the reviewed correction workflow ships; nobody
mutates or deletes a published row.

Hashing uses SHA-256 over a versioned ordered field list. Timestamps are UTC
RFC3339 at exactly microsecond precision; UUIDs are lowercase; Decimal values
are non-exponent fixed-point with trailing fractional zeros removed and negative
zero normalized away; nulls are explicit; and allowlisted `details` use RFC 8785
canonical JSON. Sort row hashes by full dedupe key before calculating the set
hash. A pre-existing plan/dedupe key with a different revision, count, or hash is
a hard operator-visible conflict, never “already done.”

Collector, reconciler, materializer, and cutover actions all run under the
existing leader lease. Database idempotency remains mandatory because leader
handover can replay work.

### Segmenting and provisional math

Every interval is half-open. The materializer splits at UTC midnight, capacity
or attribution revision, and canonical-rate effective boundary:

```text
publishable_end = ended_at                                  if closed
                  floor_utc_midnight(last_confirmed_at)     if open

segment_start = materialized_through
segment_end   = min(next UTC midnight,
                    next rate boundary,
                    publishable_end)
quantity      = capacity * (segment_end - segment_start)
source_id     = resource_interval_id + segment_start + segment_end
ts            = segment_start
```

If `segment_start >= publishable_end`, there is no publish plan. Thus an open
resource publishes only complete confirmed UTC days; rate boundaries split
inside those days but do not make the live current-day fragment final. A closed
resource may publish its final partial day through `ended_at`. The read model
separates:

- **finalized:** `[started_at, materialized_through)`;
- **confirmed provisional:** `[materialized_through,
  min(ended_at, last_confirmed_at, query_end))`; and
- **unverified projection:** optional `[last_confirmed_at, now)`, never written
  to the ledger or included in the default quantity.

An open interval can therefore finalize completed confirmed days while its
current remainder stays provisional. A closed but not-yet-published remainder is
also provisional until strict publication succeeds. Closing replaces it with
finalized quantity without changing the total.

For days newer than the app-DB rollup watermark, usage reads derive v2
infrastructure quantities from `resource_intervals`, canonical finalized cost
from published app-side plans, and **exclude raw v2 audit rows**. This avoids
double counting or early cost visibility during the valid crash window after
the audit commit but before the app cursor advances. Legacy/LLM raw events still
use the existing raw tail. Once a UTC day is in `usage_daily_v2`, reads use that
daily row and stop deriving interval/plan values for the day. The rollup
watermark and daily replacement commit atomically in the app DB, so a reader
sees exactly one side of the handoff.

Raw window queries prorate infrastructure quantities by overlap with
`[query_start, query_end)`; they do not include an entire event merely because
its `ts` falls inside the window. Existing point events retain timestamp
semantics.

The usage-rollup watermark must not advance a day until resource materialization
declares it complete **and** every enabled required inventory scope has a
complete watermark beyond the day end with no unresolved coverage gap touching
the day. This prevents a continuously running Pod or a stale VM controller from
disappearing from a closed daily rollup. A reviewed waiver materializes the day
as `partial` with its unknown ranges, then permits later days to progress; it
never relabels the day complete. Optional sources do not block the rollup but
remain named coverage exclusions for that day.

Audit migration `0003` also adds
`usage_rollup_dirty_days(day date primary key, revision bigint, updated_at)`;
app `0086` adds
`usage_rollup_day_state(day date primary key, applied_audit_revision bigint,
coverage_status, unknown_ranges, rolled_at)`. A statement-level transition-table
trigger—or an explicit same-transaction batch upsert—increments each distinct
affected UTC day once per audit insert statement, avoiding one hot-row update per
LLM dimension.

The v2 rollup reads a dirty revision and that day's complete aggregate from one
repeatable-read audit snapshot, then in one app transaction full-replaces
`usage_daily_v2`, records the applied audit revision plus coverage status/unknown
ranges, and advances the contiguous watermark where allowed. If another event or
correction commits concurrently, its higher revision remains dirty for the next
pass. This re-closes an arbitrarily old day safely; a fixed seven-day lookback is
not a correct late-event protocol.

Migration seeds every retained audit day as dirty, rebuilds all v2 daily rows,
and reconciles them with raw aggregates before v2 reads enable. The empty-ledger
shortcut cannot advance v2 across an infrastructure day lacking an explicit
seal/coverage decision.

Do not delete publication plans in the rollup handoff. Keep them for a fixed
post-rollup safety/diagnostic horizon, then clean only plans whose day revision
is still applied and whose audit partition is retained. Each usage request reads
the app watermark, daily rows, plans, intervals, and coverage in one read-only
`REPEATABLE READ` app transaction. That prevents a concurrent day handoff from
mixing the old boundary with the new read model.

V1 applies no automatic deletion to resource intervals, lifecycle heads, or
coverage gaps; correctness/non-linear repricing wins until an archival design is
reviewed. Retain publication plans and snapshot metadata for at least 400 days.
Complete normalized snapshot items may expire after 7 days and abandoned
incomplete staging after 24 hours once their metadata/digest remains. Operators
may lengthen these horizons, not shorten them below the active ledger/comparison
window.

## The six workstreams

### 1. Stop combining incompatible units

- Expand the backend breakdown key to `(category, measurement_basis,
  resource_class, resource, unit, attribution_scope, cost_domain)`. A convenient
  JSON map keyed by unit is not valid because the same unit legitimately occurs
  on several axes.
- Restrict `group_by=model` to `category=llm`; infrastructure rows never acquire
  a fake model dimension. Generalize timeseries beyond today's token-only shape
  and return typed series descriptors.
- Replace Cockpit `Compute-hours` with separate `vCPU-hours` and `Memory
  GiB-hours` KPIs and split the user/project columns the same way.
- Present logical claim storage and physical volume storage as separate cards.
  Add current allocated/provisioned GiB, instance counts, and optional equivalent
  GiB-month. Never present their sum as total storage.
- Preserve an additive compatibility summary only for rows with the exact same
  full dimension tuple. New UI code consumes the row-based representation.
- Add tests that fail if different categories, bases, classes, currencies, or
  units enter one unlabeled scalar. Fix the existing frontend accumulator tests
  before any new resource writes are enabled.

### 2. Include currently running resources

- Extend reads with the overlap-clipped tail only through each interval's
  `last_confirmed_at`. Return `finalized_quantity`,
  `confirmed_provisional_quantity`, and their sum as default `quantity`.
  Existing LLM rows naturally have no provisional quantity.
- Return `as_of`, `data_through`, and per-source watermarks. `data_through` is the
  minimum complete watermark of required enabled sources, not the web request
  time.
- If the operator asks for a wall-clock forecast, return
  `unverified_projected_quantity` separately. Freeze the confirmed number when a
  collector is stale; never age an unverified projection into finalized usage.
- Canonical `cost_usd` covers accepted ledger events. A live cloud-equivalent
  estimate may use finalized + confirmed provisional quantities and must expose
  that basis. It cannot mutate canonical event cost.
- Split provisional capacity by UTC day, rate boundary, and exact selected
  window. Do not assign a live resource's entire tail to the request time.
- Cockpit labels confirmed live contributions, shows inventory freshness, and
  displays a warning rather than zero when a required source is stale.

### 3. Replace the blind 24-hour cap with liveness reconciliation

- Remove age-only closure from `workspace_metering.materialize_and_reconcile`.
- Explicit terminal/DELETED/not-found hooks close quickly. A successful delete
  request alone does not close a Pod, VMI, PVC, or PV.
- A complete inventory updates `last_seen_at`, `last_confirmed_at`, and snapshot
  marker for present resources and closes missing resources at the snapshot
  completed-receipt time. It only compares objects in the exact same inventory
  scope and retains the collection window as uncertainty.
- An API timeout, authorization error, partial controller response, or lost
  cluster connection is `unknown`: update health diagnostics, close nothing.
- A live pod/VM/PVC can remain open indefinitely. Age is an alert signal, never
  evidence of deletion.
- Run full LIST reconciliation periodically even with a healthy WATCH, handle
  resource-version expiry by relisting, and expose list/watch restart metrics.
- Remote VM inventory carries controller epoch, monotonic sequence, expected
  count, and digest so an incomplete NATS/HTTP response cannot masquerade as an
  empty cluster.
- Keep metering errors non-load-bearing for create/delete workflows, but emit
  structured logs and health metrics; `[]` and `0 usage` are not error values.

### 4. Meter VM, agent, and platform compute

- Add the separate metering collector described above; do not expand lifecycle
  `list_active()` until it accidentally becomes an accounting API.
- Read every admitted Pod, including sandbox workspaces, on-demand IDEs, job
  agents, session agents, legacy `persistent-*` agents, and short platform
  Job/CronJob Pods. Attribute first, then calculate effective requests from the
  returned Pod.
- Persist entity type (`job` or `thread`), VM/VMI identity, and root ownership
  mapping in controller state and both transports; current create paths must not
  collapse all owners to a generic job ID. Compute capacity/schedule/terminal
  truth comes from the admitted VMI inventory, with create-time size retained
  only as a diagnostic comparison.
- Inventory every configured product namespace without a label selector.
  Classify platform Pods through their complete owner chain and aggregate rolling
  Pod UIDs under stable Deployment/StatefulSet/Job/CronJob components in reads.
- Agent attribution follows the current thread or job binding. Warm/unbound
  agents are shared platform capacity until bound; do not retrospectively assign
  their warm time to the first user.
- Platform components stay in a separate shared-workload section. They are
  visible to admins but excluded from normal user-scoped usage. Their Pod rows
  remain `cost_domain=workload-allocation` with
  `attribution_scope=shared-platform`; only control-plane/support charges use the
  `overhead` domain.
- Exclude `virt-launcher` Pods in v1 and expose them as a named coverage
  exclusion. Meter each VMI incarnation once as guest-provisioned compute.
- Add detailed `by_resource` quantities to the usage API. Category+unit alone is
  insufficient once Pod, VM, platform, and storage pricing differ.

### 5. Meter PVC GiB-hours and claim-hours

- Inventory all PVCs in configured product namespaces, VM-controller root-disk
  references, and cluster-scoped PVs. The PVC and PV collectors publish separate
  bases.
- Start a PVC interval from its authoritative creation/allocation timestamp and
  stop only on confirmed PVC deletion. Pending, unmounted, and detached claims
  still represent demand; Pod status is irrelevant.
- Use requested capacity, storage class, access modes, volume mode, and ownership
  labels in interval details.
- Recognize current identities: `pvc-workspace-*`, `pvc-ws-thread-*`,
  `pvc-agent-s-*`, `pvc-persistent-*`, VM root PVCs linked through DataVolumes,
  golden-image PVCs, and chart-managed platform claims.
- Split on expansion; Kubernetes does not support shrinking a bound claim, but
  the model handles any observed revision rather than assuming that forever.
- Emit claim-requested `gib-hour` + `claim-hour` by PVC UID. Emit independent
  volume-provisioned `gib-hour` + `volume-hour` by PV/CSI identity. A static
  oversized PV, delayed binding, resize lag, and retained volume must produce the
  expected divergence rather than an accidental duplicate.
- When a PV is unbound, released, or retained, change attribution to shared or
  unknown and continue its physical-asset interval. Do not charge that tail to
  the deleted claim owner.
- If a Retain PV object disappears without backend deletion proof, detach the PV
  incarnation but leave the durable asset `backend-unverified`, freeze confirmed
  accrual, and open a coverage gap. Provider/CSI inventory or an audited operator
  assertion—not wall-clock projection—resolves it.
- Resolve resource class from cluster, StorageClass, CSI driver, volume mode,
  and relevant topology/tier fields. An unmapped class is quantity-only and
  appears in price coverage gaps.
- Replication overhead and physically used bytes are utilization/asset details,
  not invented multipliers on logical claim demand.

### 6. Add optional actual-utilization overlays

- Historical utilization requires a configured Prometheus-compatible store.
  Kubernetes Metrics API is current/autoscaling telemetry and is not a fallback
  for integrating a billing window. If Prometheus is absent, the historical
  overlay is unavailable while allocation metering continues normally.
- CPU consumed hours are `sum(increase(container_cpu_usage_seconds_total)) /
  3600`, applying `increase` per counter before aggregation so restarts reset
  safely. Memory working-set GiB-hours use time-weighted integration; also expose
  covered-window average and peak.
- Add recording rules that attach stable cluster + Pod UID before data is stored
  or queried. Namespace/Pod name is not a durable identity after recreation.
  Record rule version and `reliable_from`; pre-cutover series cannot be joined
  retroactively.
- PVC used bytes depend on kubelet CSI `NodeGetVolumeStats`. Detached and raw
  block volumes may have no sample, and RWX reports must be deduplicated per
  volume rather than summed across nodes.
- Calculate ratios only over the intersection of allocation and telemetry
  coverage. Return covered seconds/fraction and gap reasons. Missing samples are
  `unavailable`, never zero use.
- Require the Prometheus-compatible query layer's configured HA replica
  deduplication (or deduplicate the replica label before counter integration);
  two scrapers must not double CPU consumption.
- Keep utilization endpoints/fields separate enough that a future customer plan
  cannot accidentally switch from requested-capacity billing to sampled-use
  billing through a UI toggle.
- Useful efficiency ratios include consumed/allocated CPU-hours, average and
  peak memory/allocated memory, and used/provisioned PVC bytes.

## API shape

All usage windows are UTC half-open `[start, end)`. Infrastructure rows are
overlap-prorated; point-in-time LLM events keep timestamp membership. The v2
typed row array is authoritative and lives at a versioned route; changing the
existing endpoint's numbers to decimal strings would be a wire break.

`GET /api/usage/v2` is the new primary summary and returns explicit dimensions,
finality, price coverage, and freshness:

The Slice 0 dark-launch implementation intentionally exposes only existing
point-event ledger rows. It reports partial coverage and a null `data_through`;
it does not infer absent Pods/VMs/PVCs as zero or prorate legacy end-stamped
workspace rows. Slice 1 switches complete UTC days to `usage_daily_v2` and adds
confirmed interval tails at partial boundaries.

```json
{
  "schema_version": 2,
  "window": {
    "start": "2026-08-05T00:00:00Z",
    "end": "2026-08-06T00:00:00Z",
    "as_of": "2026-08-05T12:05:00Z",
    "data_through": "2026-08-05T12:00:00Z"
  },
  "rows": [
    {
      "category": "compute",
      "measurement_basis": "scheduler-request",
      "cost_domain": "workload-allocation",
      "resource_class": "kubernetes-pod",
      "measurement_algorithm": "pod-requests-k8s-1.35-<commit>",
      "resource": "workspace_pod",
      "unit": "vcpu-hour",
      "attribution_scope": "customer",
      "quantity": "12.5",
      "finalized_quantity": "10.0",
      "confirmed_provisional_quantity": "2.5",
      "unverified_projected_quantity": null,
      "ledger_cost": {
        "status": "unpriced",
        "currency": "USD",
        "amount": null,
        "priced_quantity": "0",
        "unpriced_quantity": "12.5"
      },
      "events": 7
    }
  ],
  "coverage": {
    "status": "partial",
    "includes_provisional": true,
    "required_sources_ok": 3,
    "required_sources_total": 4,
    "unknown_ranges": [
      {"start": "2026-08-05T10:00:00Z", "end": "2026-08-05T10:03:00Z"}
    ],
    "excluded_domains": ["node-assets", "idle", "network", "control-plane"]
  }
}
```

Quantities and money are decimal strings at the v2 boundary. Canonical
`ledger_cost` retains the existing USD contract and is distinct from
cloud-equivalent estimates, which are grouped by source currency and never sum
EUR with USD. A null cost means unpriced, not free.

`events` counts finalized ledger rows touching the window and is never
duration-prorated. It is not a resource count; current/open instance counts and
claim/volume instance-hours are explicit fields/measures.

Normal user/project reads include only their `customer` rows and an aggregate
coverage status. Admin reads may request `shared-platform` and `unknown` rows and
receive per-scope health, last attempt/complete times, object counts,
unclassified counts, and sanitized errors. Do not expose fleet object names,
cross-tenant owner IDs, or controller diagnostics to ordinary users.

`GET /api/usage/v2/timeseries` returns bucket start/end plus the same typed
series dimensions; it is no longer token-only. Every bucket carries
`coverage_status=complete|partial|unavailable` and authorized unknown ranges. An
absent point may be zero-filled only in a complete bucket; partial/unavailable
renders unknown, not zero. `GET /api/usage/v2/resources` is
a paginated, authorized interval/lifecycle drilldown. Summary endpoints do not
return one row per Pod UID. Customer detail uses an opaque resource ID and omits
cluster, namespace, Kubernetes name/UID, controller diagnostics, and internal
workload labels; admins may request those operational fields.

`GET /api/usage/v2/current` is the point-in-time allocation surface promised by
the dashboard. It returns its own `as_of`/`data_through` plus rows keyed by the
same dimensions with `capacity`, `capacity_unit` (`vcpu` or `gib`), and
`instances`. It does not derive a current count from ledger event count and does
not mix claim-requested GiB with volume-provisioned GiB.

`GET /api/usage/v2/utilization` owns sampled actual metrics. It exposes
Prometheus source, requested window, telemetry-covered window, covered
seconds/fraction, authorized opaque resource identity, aggregation method, and
gap reasons.

### Compatibility and authorization

The existing `/api/usage`, `/api/usage/breakdown`, and
`/api/usage/timeseries` response keys and numeric types remain unchanged for at
least two releases after Cockpit and MCP clients migrate. Their compatibility
query preserves the pre-v2 point-event categories (`llm`, `tts`, and `stt`) plus
legacy/v2 `workspace_pod` scheduler-request CPU/RAM only; new VM, agent,
platform, PVC, and PV classes are excluded rather than folded into invalid
wildcard totals. V1 cloud cards receive only that exact workspace subset and
report excluded v2 coverage. Remove v1 only after access telemetry and a
documented deprecation show no remaining callers.

All routes and the internal price query use one `UsageVisibility` value resolved
by the existing access layer before any app/audit/Prometheus query:

| Principal/effective view | Customer aggregates | Customer resource detail | Shared/unknown + collector detail |
|---|---|---|---|
| Approved user | Own rows plus rows in visible projects | Same scope, opaque IDs | No; aggregate freshness only |
| Project-scoped token/MCP | Exact project only | Exact project if the capability permits | No |
| Admin, fleet view | All customer rows | All customer rows | Yes, explicitly requested |
| Admin, view-as-user | Exactly the approved-user scope | Exactly the approved-user scope | No |

`ref_id` first authorizes access to the referenced job/thread, then applies the
same row scope; a guessed UUID must not become a presence oracle. Utilization
filters use the same authorized interval IDs before constructing fixed PromQL—no
caller-supplied label matcher is interpolated. The price engine calls the shared
query service with this resolved visibility object; it never scrapes the admin
detail HTTP endpoint or widens scope for pricing convenience. Golden tests cover
every route × principal × view-as × attribution-scope combination.

## Cloud-equivalent pricing integration

The current comparison estimator's category+unit wildcard is safe only while
all compute is the same workspace class. Matching expands to:

```text
(provider card/version, target service/region, applicability,
 cost domain, measurement basis, category, resource class,
 resource, unit, provider-effective interval)
```

Source cluster/region describes where usage was observed; target service/region
describes the hypothetical provider comparison. They are never inferred to be
the same.

App `0086` introduces immutable successor tables rather than stretching the v1
`sum|max` rows:

```text
usage_rate_card_versions_v2 (
  id, card_id, provider, target_service, target_region, currency,
  pricing_basis, calculator, aggregation_scope, shape_change_policy,
  provider_effective_from, provider_effective_to,
  source_published_at, observed_at, source_version, source_checksum,
  applicability, calculator_config, created_at
)

usage_rate_components_v2 (
  version_id, component, source_sku, source_meter,
  billing_unit, unit_size, unit_price,
  tier_min, tier_max, included_quantity, source_metadata
)
```

`applicability` is validated typed data for service, OS/architecture, purchase
model, disk family, redundancy, and storage topology; `calculator_config` has a
versioned schema. A refresh inserts a complete card version plus every component
in one transaction, then makes that immutable version selectable. It never
mixes CPU from one source fetch with RAM from another or updates a referenced
version.

Selection is fail-closed and deterministic. `historical-public-list` chooses a
version whose provider-effective interval covers the charge period; a missing
period stays unpriced. `current-price-scenario` chooses one explicit current/as-
of version for the whole scenario. There is no automatic historical/current or
per-component fallback, and ambiguous applicable versions are an error.

Database rows contain price data; versioned code contains pricing behavior. Use
a closed calculator enum, never an arbitrary DB expression:

| Calculator | Intended behavior |
|---|---|
| `linear_v1` | Independent unit rate with no provider shape or minimum |
| `exact_flavor_v1` | Map a VM lifecycle to an exact/next valid provider flavor |
| `reference_dominant_share_v1` | Concurrent fleet-envelope share of a reference node; never summed per lifecycle |
| `fargate_v1` | Supported task CPU/RAM shape and provider minimum duration per derived task occurrence |
| `aci_container_group_v1` | Container-group CPU and memory rounding per derived group occurrence |
| `block_volume_v1` | Per-volume capacity plus fixed volume-hour fee |
| `azure_managed_disk_v1` | Choose and price a managed-disk tier per volume lifecycle |

Every calculator declares `aggregation_scope=lifecycle` or
`aggregation_scope=concurrency-envelope`. Lifecycle calculators derive one or
more provider/card-specific `billing_occurrence_id` values from a
`source_lifecycle_id` and its revisions. Their explicit shape-change policy is
`continue`, `restart`, or `unsupported`; a Pod resize cannot silently inherit one
Fargate/ACI minimum if that provider would require a new task/group. VM flavor
and disk rules follow the same declared contract. Shape selection, capacity
rounding, minimum duration, tier selection, and fixed-resource fees happen
**before** aggregation.
Applying a 60-second minimum independently to every day fragment or rounding
fleet-wide summed RAM would be wrong. Open lifecycles are priced through
confirmed time and marked provisional.

`reference_dominant_share_v1` is the deliberate exception. Build change-point
slices from all concurrent eligible allocation intervals and integrate:

```text
slice_fractional_nodes = max(sum(slice_cpu) / flavor_cpu,
                             sum(slice_memory) / flavor_memory)
reference_cost += slice_fractional_nodes * slice_duration * flavor_hourly_rate
```

With complete eligible workload coverage this is a theoretical fractional lower
bound before integer nodes, bin packing, topology, DaemonSets, and headroom. With
partial coverage it is only a `modeled_reference`. Summing dominant share per
Pod is invalid because complementary Pods may fit the same node.

Windowed estimates load the complete retained lifecycle context, not just events
whose timestamp falls in the query. Shape-rounded linear charges are clipped to
the requested window. Any minimum-duration uplift is one deterministic
one-time charge assigned to the billing-occurrence start, so disjoint windows
remain additive and two short occurrences each incur their own minimum. For an
open occurrence, recompute the uplift against confirmed elapsed time and label it
provisional. Return charge period and assumptions; do not disguise minimum
billing as extra resource allocation.

Examples:

- AWS Fargate and Azure Container Instances consume scheduler-request Pod
  lifecycles only after their supported shape/rounding rules are satisfied.
- STACKIT/AWS/Azure exact-flavor cards consume guest-provisioned VMI lifecycles.
  The separate STACKIT node-share comparison consumes concurrent eligible Pod
  slices and is `lower_bound` only with complete coverage, never `exact`.
- Block-storage cards consume volume-provisioned lifecycles and can price both
  capacity and fixed `volume-hour`. A provider claim/control-plane fee may use
  `claim-hour`, but claim demand is never silently priced as a physical disk.
- Azure managed disks select a tier per volume; aggregating all GiB first cannot
  reproduce that bill.
- Missing mappings or rates return an excluded quantity and reason. They never
  become zero-cost coverage.

Storage calculators declare continuous versus started-unit/calendar billing,
minimum duration, capacity-tier rounding, month proration, and fixed per-volume
components separately. Measured `volume-hour` never changes to disguise a
rounded billed duration. A disk SKU whose price also depends on deferred IOPS or
throughput inputs reports partial coverage rather than `exact`.

Every price version additionally retains source URL/API, unit convention (GB
versus GiB), tax posture, and calculator code version. Decimal money rounds only
at the documented provider/display boundary.

Do not collapse estimate truth into one quality word. Every component and the
overall envelope report independent axes:

```text
model_fidelity = exact | modeled | lower_bound
input_coverage = complete | partial | unavailable
finality       = finalized | includes_confirmed_provisional
rate_status    = fresh | stale | missing
```

The response includes card/version/calculator IDs, currency/amount, measured
inputs, provider-rounded billed quantities, charge periods, SKU/meter, unit
rate, excluded inputs with reason codes, and assumptions. Per-occurrence detail
is available only through the authorized drilldown; summaries aggregate it
without losing excluded quantity. Applying today's price to history is an
explicit `current-price-scenario`, not an invoice reconstruction.

Shared fixed costs remain separate from usage-derived estimates. A control-plane
monthly fee is a platform baseline, not a vCPU-hour rate. Node assets, idle,
network, and managed-service overhead remain separate components so the UI can
eventually show:

```text
workload allocation + shared assets + idle + overhead = modeled platform cost
```

This feature initially supplies the first component plus selected volume assets;
it does not claim the equality is complete.

## Attribution rules

1. Workspace Pod/PVC → validated job or thread, then snapshot user/project.
2. Session/persistent agent Pod/PVC → validated thread; job agent while bound →
   validated job.
3. Warm/unbound agent → shared platform.
4. VMI/root PVC → controller-persisted job or thread; DataVolume only resolves
   the root PVC and is not metered again.
5. Bound PV → inherit the validated current claim attribution for the bound
   portion; unbound/released/retained PV → shared platform or unknown.
6. Chart/platform resource and golden-image PVC → shared platform.
7. Missing, malformed, dangling, or cross-tenant labels → explicit unknown,
   still included in authorized fleet totals.

Attribution changes split intervals. For example, binding a warm agent closes
its shared interval and opens an attributed interval at the binding time; it does
not rewrite warm capacity as user usage. A PVC rebind/owner correction or PV
release follows the same forward-only rule. Historical corrections require
append-only correction events and an audit trail; operators never edit usage
rows in place.

Database checks enforce the enum contract: `customer` requires a DB-validated
job/thread owner plus a user snapshot (project may be null); `shared-platform`
and `unknown` carry no customer user/project IDs. Membership changes after the
snapshot do not rewrite history. Labels that claim another tenant, fail owner
kind validation, or disagree with the authoritative DB become unknown and raise
a security/coverage diagnostic.

## Code impact map

Prefer a focused `orchestrator/services/infrastructure_metering/` package over
adding more branches to the legacy workspace file:

| Area | Expected responsibility |
|---|---|
| `types.py` | Inventory envelopes/items, typed dimensions, health/finality enums |
| `quantities.py` / `pod_requests.py` | Kubernetes parsing, effective requests, Decimal integration |
| `collectors/kubernetes.py` | Per-scope LIST/WATCH state machines for Pods/PVCs/PVs |
| `collectors/vm_controller.py` | HTTP/NATS VM snapshot validation and normalization |
| `reconciler.py` | Transactional snapshot reconciliation and interval revisions |
| `materializer.py` | Day/rate segmentation and strict ledger publication |
| `queries.py` | Rolled-day plus interval-tail read model and coverage |

Existing integration points and their intended deltas are explicit:

| Existing surface | Implementation delta |
|---|---|
| `orchestrator/services/workspace_metering.py` | Legacy barrier/drain adapter; remove age-only reconciliation after cutover |
| `orchestrator/services/usage_ledger.py` | Strict frozen-batch insert/verify API alongside compatible best-effort callers |
| `orchestrator/services/usage_rollup.py` | Typed v2 dimensions, dirty-day revisions, seals, and interval/plan handoff |
| `orchestrator/services/cloud_pricing.py` | Versioned typed selectors/calculators; retain v1 workspace-only adapter during compatibility |
| `orchestrator/services/leader_election.py` | Allocate/check metering generation and propagate cancellation on lease connection loss |
| `orchestrator/services/nats_bridge.py` + `vm/controller/controller.py` | Replace one-reply VM listing for metering with authenticated, bounded begin/items/end inventory framing |
| `orchestrator/main.py` | Leader-started loops, v2 routes, visibility resolution, and admin waiver operation |
| Pod/agent/session/VM provisioners | Persist missing stable owner/capacity hints and nudge reconciliation; never write ledger rows independently |
| `cockpit/src/app/core/services/admin-usage.service.ts` + `cockpit/src/app/views/admin/usage/admin-usage.component.ts` | V2 decimal models, dimensional cards, coverage/finality, current allocation, and estimate quality |
| `helm/` | Dedicated collector Deployment/ServiceAccount, scoped RBAC, NetworkPolicies, values validation, and scrape/alert rules |

App/audit migrations, both Cockpit i18n files, and the corresponding Python,
Vitest, transport, chart, and integration tests are in scope. Reference schema
snapshots remain generated/reference artifacts and are not hand-edited.

## Configuration, RBAC, and operations

Proposed Helm values keep rollout state explicit:

```yaml
infrastructureMetering:
  collectorEnabled: false
  shadowEnabled: false
  publicationEnabled: false
  stableClusterId: ""
  deploymentMode: dedicated
  namespaceAllowlist: []
  pvInventoryEnabled: false
  relistIntervalSeconds: 300
  staleAfterSeconds: 900
  listPageSize: 500
  scopeConcurrency: 2
  watchQueueSize: 10000
  maxSnapshotItems: 50000
  maxSnapshotBytes: 67108864
  snapshotItemRetentionDays: 7
  diagnosticRetentionDays: 35
  cleanupIntervalSeconds: 300
  networkPolicy:
    enabled: false
    allowUnrestrictedEgress: false
    apiServerCidrs: []
  materializerBatchSize: 100
  utilization:
    prometheusUrl: ""
```

The stable cluster ID is operator-configured, bound to authenticated source
identity, and survives endpoint/credential rotation. Values validate that
publication cannot start without collectors, non-empty cluster identity,
migrated audit capability, and at least one complete required snapshot.

The production chart uses a dedicated read-only collector Deployment and
ServiceAccount rather than granting the internet-facing orchestrator broad Pod
and PV visibility. The collector has no audit/app DB credentials; it sends only
normalized allowlisted fields to an authenticated internal ingestion stream.
The orchestrator leader supplies nonce/generation/committed cursor and ACKs only
after the cursor+mutation transaction. Local development may run the same typed
collector in-process behind an explicit non-production value.

Namespace collectors get least-privilege `get/list/watch` for Pods and PVCs in
each allowlisted namespace. Storage mapping additionally needs reviewed
cluster-scoped read of StorageClasses and, when enabled, CSIDrivers. PV inventory
is cluster-scoped and remains behind its own flag/ClusterRole. The VM collector
needs read-only VM/VMI/DataVolume/PVC verbs in its namespace. No collector needs
Secret read. Enabling the collector requires an egress NetworkPolicy with
explicit API-server CIDRs unless the operator sets the named unrestricted-egress
break-glass acknowledgement. The normal policy allows only cluster DNS, the
Kubernetes API, and authenticated ingestion. The ingestion Secret is required
at orchestrator startup while collection is enabled, and Secret-aware rolling
reload keeps the collector and orchestrator on the same rotated key. Store only
normalized allowlisted fields; never persist Pod environment, Secret refs,
arbitrary annotations, StorageClass parameters, CSI credentials/attributes/
handles, or raw controller payloads.

The server validates the exact normalized Pod shape recursively before JSONB
staging; a generic JSON-safe mapping or denylist is not sufficient. Signed
content-digest headers are authenticated before the ASGI body is read, the
exact bounded body is verified afterward, and the public Traefik ingress always
denies `/api/internal/infrastructure-metering` while collection is enabled. Any
tunnel that bypasses the chart ingress must mirror that path deny at its edge.

LIST staging is bounded by page/item/byte limits, client QPS/burst, jittered
scope concurrency, and `Retry-After` aware 429 backoff. A full watch queue marks
event continuity broken and forces relist; it never drops silently. Coalesce
MODIFIED events whose metering revision/lifecycle state is unchanged, batch
confirmation writes, and keep one ordered watch per scope. Required indexes are
the partial unique open-UID index, open scope/snapshot absence index,
materializer-eligibility index, plan state/day index, and lifecycle/time index.

Bounded-cardinality metrics include:

```text
srw_infra_inventory_attempts_total{collector,scope,result}
srw_infra_inventory_last_complete_timestamp_seconds{collector,scope}
srw_infra_inventory_objects{collector,scope,kind}
srw_infra_unclassified_objects{collector,scope,kind}
srw_infra_open_intervals{cluster,kind,basis,attribution_scope}
srw_infra_coverage_gaps{scope_id,reason,state}
srw_infra_watch_queue_utilization{scope_id}
srw_infra_api_throttles_total{collector}
srw_infra_reconcile_duration_seconds{collector,result}
srw_infra_leader_generation
srw_infra_backend_unverified_volumes
srw_infra_payload_conflicts_total
srw_infra_publication_lag_seconds
srw_infra_materializer_batches_total{result}
srw_infra_rollup_complete_through_timestamp_seconds
```

Alerts cover required-source staleness, repeated incomplete snapshots, growing
publication lag, watch-history/queue gaps, API throttling, invalid/missing
capacity, backend-unverified volumes, payload conflict, capacity-weighted
unknown/unpriced ratio, absent future audit partitions, and rollup lag. `scope`
labels are bounded opaque scope ID/class—not namespace/customer—and resource
UID/name is never a metric label.

## Migration and cutover

The heads immediately before implementation were app `0085` and audit `0002`.
Slice 0 therefore uses app `0086`, audit `0003` for the nullable/trigger
expansion, audit `0004` for separately validated checks plus retained-day
bootstrap seeding, and audit `0005` for the raw project/window index. PostgreSQL
16 cannot build that partitioned-parent index concurrently; `0005` documents
its blocking maintenance-window requirement for large retained ledgers.

The shadow-only Slice 1 foundation adds app `0087`: one-use ingestion tickets,
transport replay nonces, WATCH sessions/events, recovery-epoch metadata,
workspace comparison diagnostics, and database-enforced retention terminals.
App `0088` makes snapshot byte accounting use logical JSON bytes rather than
TOAST-dependent physical sizes. Neither migration adds an audit publication or
cutover path.

1. **Expand audit first.** Add nullable v2 event fields/checks and create future
   partitions. Existing writers/readers continue to work. A runtime capability
   probe keeps all v2 writes off until required columns, constraints, dirty-day
   trigger/state, indexes, and target leaf partitions are present; startup
   currently initializes app migrations before audit, so do not assume
   one-process migration order is the gate.
2. **Expand app DB.** Add inventory scope/snapshot and resource interval tables,
   immutable canonical/cloud rate versions, day/plan state, and a
   `usage_daily_v2` companion read model. A companion avoids dropping the old
   `usage_daily` unique index while older replicas still use its conflict target.
   Consolidation is a later cleanup.
3. **Deploy compatible code and RBAC with all flags off.** Update usage response
   models/Cockpit dimensional handling and hard-gate v1 wildcard cloud cards to
   the workspace subset before any new class rows exist. Leader-gate/fence the
   collector and materializer; the existing workspace meter is not leader-only
   today and must not be copied as-is.
4. **Bootstrap the v2 read model.** Seed every retained audit day dirty, rebuild
   `usage_daily_v2`, compare raw quantities/cost coverage, and enable v2 reads
   only after reconciliation. No empty shortcut crosses an unsealed infra day.
5. **Run collection shadow.** Reconcile snapshots and intervals but publish no
   events. Compare current workspace Pod quantities object-by-object and explain
   every difference (admission sidecar, overhead, start semantics, parse error,
   or collector gap). Require healthy complete snapshots through an observation
   window and zero unexplained objects.
6. **Audit legacy integrity.** For every legacy row marked materialized, verify
   both expected audit keys actually exist; the old best-effort writer could
   stamp after a dropped row. Repair with deterministic events before cutover.
7. **Enter crash-resumable `preparing`.** In one fenced app transaction choose
   barrier `T`, disable legacy opens, close every legacy open at `T`, and open
   matching workspace plus newly covered agent/VMI/PVC/volume v2 revisions at
   `T`. Job/thread owners remain distinct. Existing uncovered resources do not
   backdate because admitted revision history is unavailable.
8. **Drain, verify, then activate.** Strictly publish and verify every legacy
   interval with `ended_at <= T`, including those just closed; dirty/rebuild all
   affected days. Missing events/attribution block for operator resolution. Only
   then transition `preparing -> active` and permit v2 publication. A restart
   resumes the durable phase rather than choosing another barrier.
9. **Enable strict publication by class.** Workspace Pods first, then claims,
   volumes, agents, VMIs, and shared platform. At each gate, reconcile interval
   math, ledger keys, rollup totals, unknowns, and cloud price coverage.
10. **Rollback safely.** Disable publication while collectors continue preserving
   interval/cursor state. Do not delete state/events and do not restart the
   legacy writer after the durable cutover. Resume strict publication from the
   same cursor after fixing the cause.

Existing immutable events are never repriced, updated, or deleted. Cleanup of
legacy tables, compatibility response fields, and `usage_daily` happens only
after one full retention/invoice window of verified v2 operation.

## Delivery slices

### Slice 0 — schemas and dimensional UI correction

**Implemented (dark launch).** Production reads still require the feature flag,
complete runtime capability probes, and a durably reconciled bootstrap state.

- Nullable audit expansion, app state/read-model tables, capability probes, and
  feature gates.
- Row-based API models; distinct CPU/memory and claim/volume Cockpit cards.
- Immutable rate-card version schema, calculator interface, match/exclusion
  contract, and v1 workspace-only wildcard gate.
- No v2 event publication.

Exit: schema/trigger/bootstrap reconciliation, v1/v2 API golden compatibility,
auth matrix, free/unpriced/partial cost tests, and legacy price exclusion pass.

### Slice 1 — Kubernetes collector and workspace cutover

**Partially implemented (shadow foundation plus unwired publication
mechanics).** The deployed runtime remains incapable of publishing usage
events:

- A dedicated, database-free Pod collector Deployment and ServiceAccount, with
  namespace-scoped `get/list/watch` Pod RBAC, bounded normalized payloads, and
  fail-closed egress configuration (or an explicit unrestricted-egress
  break-glass acknowledgement).
- HMAC-authenticated internal POST ingestion with replay nonces, short-lived
  one-use tickets, leader-generation fencing, pre-body signed-digest
  authentication, request/body/spool bounds, strict server-side Pod payload
  allowlisting, public-ingress denial, coordinated Secret reload, and idempotent
  event identities.
- Paginated LIST, including exact relists at an existing server-committed
  resource version, serial WATCH event/cursor transactions, typed
  history/queue/protocol/size/ambiguity gaps, and fail-closed recovery relists.
  Only a complete LIST proves absence.
- Incomplete LISTs upload metadata/diagnostics but no partial item rows and back
  off to the periodic relist bound, avoiding non-authoritative staging growth.
- Kubernetes-compatible Pod effective-request normalization, with a bounded
  in-process prior-request cache for conservative resize fallback; cache loss on
  restart remains a visible invalid observation rather than an optimistic
  decrease. Complete snapshot reconciliation, shadow-only workspace Pod
  intervals, DB-validated ownership, and immutable object-level comparisons
  with the legacy meter are also present.
- Leader-fenced bounded cleanup. Defaults retain sealed normalized snapshot
  items for at least 7 days and WATCH/session plus shadow-comparison diagnostics
  for 35 days; abandoned incomplete staging manifests become cleanup-eligible
  after 24 hours while their metadata remains authoritative.
- Unwired strict publication primitives split ordinary workspace Pod intervals
  at UTC day and immutable-rate boundaries, freeze exact rate/unpriced choices
  and canonical hashes in app-DB plans, bulk insert plus verify the complete
  audit batch, and generation-fence the app cursor CAS. Missing partitions stay
  pending, hash conflicts become terminal, and replay after the audit commit is
  idempotent. The constructor gate defaults off and no lifespan task starts it.

**Remaining before Slice 1 exit:** prove a healthy shadow observation window
and resolve every unexplained comparison; complete late/correction publication,
interval-tail reads, source-aware rollup handoff, legacy integrity repair/drain,
the crash-resumable cutover barrier, publisher runtime wiring, and removal of the
legacy blind 24-hour closure. Publication and cutover gates must remain off until
those checks pass.

Depends on Slice 0. Exit: fenced shadow coverage, legacy integrity/drain,
cutover crash matrix, and workspace quantity reconciliation pass.

### Slice 2 — claim and volume storage

- PVC collectors, attribution, `gib-hour`/`claim-hour`, and UI.
- Separately gated PV collector, CSI/StorageClass mapping,
  `gib-hour`/`volume-hour`, and retained/backend-unverified behavior.

Depends on Slice 1. Quantity may ship explicitly unpriced; a storage card ships
only with its typed adapter/provenance and provider fixtures.

### Slice 3 — agents and VMs

- All agent/persistent/IDE Pod identities and bind/unbind splitting.
- Complete VM-controller envelope, VMI-incarnation compute, and one root-PVC
  mapping for jobs and threads.

Depends on Slice 1 and authenticated VM transport for VM publication. New
classes remain excluded/unpriced until matching adapters exist.

### Slice 4 — shared platform completeness

- Full configured namespace coverage, workload classification, short Jobs,
  golden-image storage, unknown bucket, and admin-only coverage/overhead views.

Depends on Slices 1–3 for the resource types enabled in that deployment.

### Slice 5 — provider calculator hardening

- Lifecycle-preserving typed calculators, shape/minimum/tier tests, price
  provenance, estimate quality, and explicit excluded quantities.

Depends on Slice 0's framework and the corresponding metered resource slice.
Adapters can land alongside Slices 2–4; this slice closes the initial provider
matrix and concurrency-envelope model.

### Slice 6 — actual-utilization overlay

- Prometheus recording rules/queries, telemetry coverage semantics, efficiency
  ratios, and Cockpit allocated-versus-actual presentation.

Depends on resource identity from Slices 1–4 and a configured historical store.
Schema/API-only work uses its own exit checks; a resource write slice cannot
enable until schema capability, collector coverage, shadow comparison, security
prerequisites, and rollback drill pass.

## Acceptance criteria

### Arithmetic and Pod semantics

- An 8-vCPU/16-GiB resource running one hour produces 8 vCPU-hours and 16
  compute GiB-hours, never `24 compute-hours`; a 4-GiB Pod produces 4 memory
  GiB-hours per hour.
- `100m`, `0.1`, `1Gi`, `1G`, and `512Mi` parse and normalize correctly without
  float drift. Invalid/negative quantities create diagnostics, not zero rows.
- Effective requests match upstream cases for regular containers, serial init
  containers, restartable init sidecars, Pod overhead, admission defaults, and
  version-supported Pod-level requests. Ephemeral containers and limits do not
  inflate allocation.
- Compatibility fixtures cover Kubernetes 1.28 baseline, per-resource Pod-level
  fallback, and supported desired/allocated/actuated resize combinations:
  increase, decrease, deferred, in-progress, and infeasible. Unsupported newer
  status fields decode through the raw path or fail coverage visibly.
- An unscheduled Pending Pod emits no compute. Scheduled Pending, image-pull,
  initializing, CrashLoopBackOff, NotReady, Unknown, and terminating Pods accrue
  until terminal/deletion/complete absence.
- Half-open window/day/rate boundaries neither overlap nor leave a microsecond
  gap. A resize or attribution change applies each revision only to its side.
- Adjacent partial queries reconcile to the whole within one `10^-18` quantity
  quantum. Repeated current-day confirmations increase provisional quantity but
  create no audit rows until UTC midnight; finalization causes no total jump.

### Storage semantics

- A 20-GiB PVC existing three hours emits 60 claim-requested GiB-hours and 3
  claim-hours regardless of Pod runtime.
- `WaitForFirstConsumer` produces claim demand before volume supply. A 20-GiB
  claim bound to a 25-GiB static PV reports 20 logical and 25 physical GiB-hours
  separately.
- PVC expansion and delayed PV resize split independently. A retained PV
  continues volume-hours after claim deletion under shared/unknown attribution.
- Deleting a Retain PV freezes its durable asset as backend-unverified; importing
  the same HMAC'd CSI asset under a new PV UID does not create a second lifecycle
  or fixed fee. Delete-policy tests distinguish older clusters without backend
  finalizer proof from capable newer clusters, and an authenticated provider/
  operator destruction assertion closes exactly once.
- A VM root PVC is counted once; its DataVolume is not a second disk. Pending,
  raw-block, RWX, platform, persistent-session, and golden-image cases are
  classified or explicitly unsupported/unknown.

### Inventory and lifecycle

- A complete empty LIST closes missing resources; a timeout, forbidden response,
  failed continuation page, fatal identity/decode error, incomplete VM frame set,
  or stale controller sequence closes none. A valid UID with invalid capacity is
  marked present, freezes that UID's capacity proof, and does not block safe
  reconciliation of unrelated UIDs.
- WATCH replay is idempotent; a 410 causes relist; BOOKMARK alone does not extend
  object confirmation; periodic LIST repairs missed ADDED/DELETED events.
- A 410 or lost VM tombstone journal records a historical unknown range. A new
  complete snapshot does not erase it; backfill or an audited partial-day waiver
  is required before the rollup crosses it.
- A Pod created and deleted wholly during an expired-watch gap remains missing
  usage under a partial range—it can never disappear behind `complete` coverage.
- A delayed old-leader empty snapshot cannot close resources after the fenced
  generation changes. Object mutation and watch cursor commit atomically across
  crash/replay, and the new leader LISTs before absence closure.
- Resource recreation under the same name opens a new UID lifecycle. A Pod/VMI
  running longer than 24 hours continues confirmed accrual.
- A delete request does not close usage before the object is terminal, not found,
  DELETED, or absent from complete inventory.
- VM suspend/resume closes one VMI UID and opens another without root-PVC gaps or
  compute overlap.
- VMI instancetype/admission capacity, paused state, live migration, missing PVC
  join, and phase-timestamp skew follow the admitted VMI authority. Replayed,
  unauthenticated, bad-digest, duplicate/reordered/oversized, stale-epoch, and
  competing-controller VM frames are rejected; no empty response can close VMs.
- A slow 50k-object paginated LIST stays within configured memory/transaction
  bounds. 429 backoff and watch-queue overflow produce visible partial coverage,
  not dropped events.

### Publication and reads

- Injected failure before audit commit advances no cursor; failure after audit
  commit but before app CAS replays, verifies, and advances without duplicates.
  A conflicting dedupe key with a different payload is a hard visible error.
- A missing target partition leaves the immutable plan pending for maintenance;
  a warm stale rate cache cannot alter plan-time SQL selection; a new leader
  adopts an old-generation plan without accepting a different row/set hash.
- Leader handover, duplicate snapshots, and duplicate events are idempotent.
- A live resource moves from confirmed provisional to finalized without a total
  jump. Stale collectors freeze confirmed quantity and expose optional
  projection separately.
- A query cutting through a daily event receives only overlap quantity. The
  rolled-day + interval-tail result equals a pure interval calculation, including
  the audit/app commit race.
- No UTC day rolls closed until every required source is complete beyond its end
  and every eligible interval segment is published.
- Sealing races with audit publication cannot omit a plan. A concurrent late
  event/correction leaves a higher dirty revision, and a retained historical day
  outside the old seven-day window is full-replaced on the next pass.
- All-unpriced, all-free-zero, and mixed-priced daily rows preserve null/partial
  cost status and quantities. Scope activation/retirement epochs affect only the
  days they overlap.
- A crash after every `preparing` cutover phase resumes the same barrier and
  cannot activate before newly closed legacy rows are strictly verified.

### Coverage, security, and pricing

- Every Pod/PVC in configured namespaces and every enabled PV/VMI is classified
  or counted unknown. Simultaneous inventory capacity reconciles with metered
  capacity within the declared observation uncertainty.
- User/project reads contain only authorized customer rows. Shared/unknown fleet
  details and sanitized collector errors are admin-only; stored objects and logs
  contain no secret material.
- V1 numeric routes and v2 decimal routes satisfy golden compatibility. Every
  principal/view-as/ref/resource/utilization combination matches the auth matrix;
  partial timeseries buckets render unknown rather than zero.
- Coverage waiver is fleet-admin-only, idempotent, audited with a required
  reason, and leaves the day partial; non-admin/project/view-as attempts fail.
- Warm agent time and retained-volume time are not assigned to the next/previous
  customer. `virt-launcher` remains excluded and visible as a gap.
- Provider cards never apply container rates to VM rows or claim demand to disk
  assets. Missing classes are excluded, not free.
- Fargate minimum/shape, ACI group rounding, per-disk tier/fixed fee, and
  exact-flavor fixtures apply at their declared lifecycle or derived
  billing-occurrence scope before final aggregation and retain their
  currency/provenance/quality. Dominant-share fixtures instead use concurrent
  change-point slices; complementary Pods prove per-Pod share sums are rejected.
- Two separate 30-second Fargate lifecycles each incur one provider minimum; a
  200-GiB Azure disk selects its documented provisioned tier; and STACKIT's
  published 730-hour storage example reconciles to 386,900 GB-hours plus 2,920
  disk-hours without treating those as PVC claim-hours.
- A 30-second Pod resize follows the card's explicit continue/restart/unsupported
  billing-occurrence policy. Atomic card refresh, strict historical-price gaps,
  explicit current-price scenarios, stale rates, and ambiguous applicability
  all fail or label coverage as designed; EUR and USD remain separate.
- Missing Prometheus or partial telemetry returns unavailable/partial coverage,
  never zero utilization or a change to allocation quantities.
- Two Prometheus HA replicas do not double CPU, and UID recording-rule coverage
  begins only at its declared reliable cutover.

## Verification plan

- Pure unit/property tests for quantity parsing/ceiling, upstream Pod request
  fixtures, Decimal overlap integration, half-open UTC/rate splitting, revision
  hashes, attribution, and all calculator versions.
- Async app/audit DB tests for constraints, snapshot transactionality, strict
  batch verification, crash points, leader replay, cursor CAS, typed rollup,
  exact-window proration, plan/hash canonicalization, day sealing/dirty revisions,
  scope epochs, price coverage, historical bootstrap, and every cutover phase.
- Fake Kubernetes watch tests for pagination, auth/timeout/error paths, 410,
  BOOKMARK, duplicate/out-of-order events, name reuse, resizes, terminal states,
  and reconnect/relist repair.
- Transport-mocked VM tests for controller epoch/sequence/count/digest, job and
  thread owners, authentication/nonces/chunks, admitted VMI capacity, VMI
  recreation/migration, controller disconnect, and root PVC join independence.
- Rendered Helm/RBAC/NetworkPolicy tests plus `kubectl auth can-i` matrix: only
  the collector gets required namespace/storage/VMI reads, nobody gets Secrets,
  and a 403 is isolated to its exact scope.
- API golden tests for v1/v2 wire types, authorization matrix, cost coverage,
  point-in-time current rows, partial buckets, waiver audit, and cloud-estimate
  quality/provenance/exclusions.
- k3d gate:
  1. start a workspace and compare admitted effective requests with provisional
     CPU/RAM;
  2. inject an admission sidecar/overhead and verify the revision;
  3. cross a UTC boundary, delete the Pod, and verify stable total/finality;
  4. create a delayed-binding PVC/PV, delete its Pod and claim, and verify both
     independent lifetimes including Retain;
  5. break RBAC/API/watch access and prove no absence closure;
  6. recreate a name with a new UID and compare inventory totals.
- VM-cluster gate with an 8-vCPU/16-GiB VMI and persistent root disk through
  suspend/resume and controller disconnect.
- Prometheus fixture with counter reset, Pod name reuse, missing interval, RWX
  duplicate, HA replicas, recording-rule cutover, detached PVC, and raw-block
  unavailable cases.
- Load gates for a 50k-object staged LIST, sustained status churn/429/queue
  overflow, one million retained intervals, materializer catch-up SLO, and
  leader handover without unbounded memory or lock duration.
- Cockpit Vitest coverage, both-language i18n check, production build, and a
  running-app visual check at narrow and desktop widths.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Application allocation is mistaken for the provider bill | Named cost domains, explicit excluded assets/idle/overhead, estimate quality |
| Inventory outage is interpreted as deletion | Complete/incomplete envelope; only scoped complete absence closes; stale freezes accrual |
| Admitted Pod request diverges from create arguments | Calculate from live admitted object with upstream semantics and algorithm version |
| Audit/app DB split commit duplicates a live tail | Strict verify/CAS plus interval-derived unrolled reads that exclude raw v2 rows |
| Rollup closes a day before live resources are segmented | Minimum required-source watermark and publication completion gate |
| v1/v2 writers overlap during rollout | Durable cutover barrier, legacy audit verification, mutually exclusive gates |
| Pod/VM/PVC name reuse corrupts history | Stable cluster + kind + immutable UID; VMI incarnation for compute |
| VM guest plus launcher Pod double counts | `virt-launcher` excluded and exposed as a named v1 gap |
| Claim demand and volume supply are added | Measurement basis is a required aggregate dimension and separate UI card |
| Provider rules use the wrong aggregation scope | Retained identity plus versioned lifecycle/concurrency-envelope calculators |
| High cardinality or sensitive inventory leaks | Grouped summaries, paginated authorized detail, bounded metrics, normalized fields only |
| Sampled utilization is mistaken for billable usage | Separate API/source/coverage; allocation ledger remains canonical |
| Unknown labels hide cost or cross tenants | DB-validated owner resolution; unknown stays in fleet totals and alerts |

## Deferred extensions, not v1 blockers

- KubeVirt launcher overhead stays a visible exclusion until a stable host-delta
  algorithm is reviewed.
- Prometheus is optional; without it there is no historical actual-utilization
  overlay and no Metrics API approximation.
- Snapshots, object storage, ephemeral storage, GPU, IOPS, egress, IPs, load
  balancers, node assets, idle, and managed control planes get new typed meters or
  asset domains later. None is inferred from current rows.
- Provider invoices and actual-cost APIs can later validate estimates without
  changing canonical allocation history.
- Allocation of shared/idle cost to customers requires a separate explicit
  policy and is not implied by this feature.

## Authoritative references

Kubernetes behavior:

- [Upstream `PodRequests` helper](https://github.com/kubernetes/kubernetes/blob/master/staging/src/k8s.io/component-helpers/resource/helpers.go)
- [Pod resource requests and Pod-level resources](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)
- [Native sidecar/init-container accounting](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)
- [Pod overhead](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-overhead/)
- [Pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/)
- [In-place Pod resource resize](https://kubernetes.io/docs/tasks/configure-pod-container/resize-container-resources/)
- [Kubernetes API LIST/WATCH and resource versions](https://kubernetes.io/docs/reference/using-api/api-concepts/)
- [Kubernetes quantity definition](https://kubernetes.io/docs/reference/kubernetes-api/definitions/quantity-resource/)
- [Python Kubernetes quantity parser](https://github.com/kubernetes-client/python/blob/master/kubernetes/utils/quantity.py)
- [Persistent Volumes and reclaim lifecycle](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
- [StorageClasses and `WaitForFirstConsumer`](https://kubernetes.io/docs/concepts/storage/storage-classes/)
- [CSI `NodeGetVolumeStats` specification](https://github.com/container-storage-interface/spec/blob/master/spec.md)
- [Kubernetes RBAC good practices](https://kubernetes.io/docs/concepts/security/rbac-good-practices/)
- [KubeVirt VMI API reference](https://kubevirt.io/api-reference/)
- [KubeVirt VM/VMI lifecycle](https://kubevirt.io/user-guide/user_workloads/lifecycle/)
- [NATS authentication and authorization](https://docs.nats.io/running-a-nats-service/configuration/securing_nats)

Cost and telemetry models:

- [OpenCost specification](https://opencost.io/docs/specification/)
- [FinOps Open Cost and Usage Specification 1.0](https://focus.finops.org/focus-specification/v1-0/)
- [Kubernetes resource metrics pipeline](https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/)
- [Metrics Server scope and limitations](https://github.com/kubernetes-sigs/metrics-server)
- [Prometheus counter/range functions](https://prometheus.io/docs/prometheus/latest/querying/functions/)
- [Prometheus staleness behavior](https://prometheus.io/docs/prometheus/latest/querying/basics/)
- [kube-state-metrics Pod metrics](https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/workload/pod-metrics.md)

Provider pricing behavior and provenance:

- [STACKIT SKE storage billing example](https://docs.stackit.cloud/products/runtime/kubernetes-engine/basics/storage/)
- [AWS Fargate pricing and minimum duration](https://aws.amazon.com/fargate/pricing/)
- [AWS service price-list format](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/reading-service-price-list-file-for-services.html)
- [Azure Container Instances pricing](https://azure.microsoft.com/en-us/pricing/details/container-instances/)
- [Azure managed disk types and tiers](https://learn.microsoft.com/en-us/azure/virtual-machines/disks-types)
- [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)
