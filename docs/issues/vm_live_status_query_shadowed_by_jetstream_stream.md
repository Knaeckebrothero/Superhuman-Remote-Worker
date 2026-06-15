---
tags:
  - issue
  - vm
  - nats
  - jetstream
  - orchestrator
---

# `GET /api/vms/{id}?live=true` returns a JetStream PubAck instead of VM status

**Status**: **Resolved (immediate fix shipped 2026-06-15)** — guard + stream-subject narrowing applied and verified live; **low severity / latent** (no UI consumer today). Filed 2026-06-15; reproduced live on dev 2026-06-13. The deeper architectural improvement (event-driven VM status) is tracked separately in [`docs/features/event_driven_vm_status.md`](../features/event_driven_vm_status.md).

## Resolution (2026-06-15)

Shipped the **C + A** combination (guard + narrow the stream), per the multi-agent
investigation below — *not* the B subject-rename, because the vm-controller is a
separate chart + Fleet GitRepo on a different cluster with prod-private lockstep,
so an infra/orchestrator-only fix is far lower-risk for a latent bug.

- **C (guard), orchestrator** — `nats_bridge.query_vm_status` now rejects
  ack-shaped replies (`stream`+`seq`, no `job_id`) → returns `None` so the
  endpoint reports the honest `live_error` instead of a PubAck. Unit tests added
  (`tests/test_nats_bridge.py::TestQueryVmStatus::test_query_rejects_jetstream_ack`
  + `test_query_allows_reply_with_seq_field`).
- **A (narrow stream), live hub** — `nats stream edit VM_EVENTS --subjects
  "vm.lifecycle.create.>,vm.lifecycle.delete.>,vm.lifecycle.status.>"` applied to
  the shared `nats` hub (drops the `vm.lifecycle.get.*` coverage **and** the dead
  `vm.status.>`). Fixes dev **and** prod-private at once (shared stream).
- **A (sources of truth)** — `helm/templates/nats/streams-job.yaml` narrowed +
  switched to an `add || nats stream edit -f` upsert (so subject changes actually
  apply on upgrade — the gotcha #2 landmine); HomeLab
  `deployments_managed/nats/README.md` updated with the narrowed subjects + a
  warning never to use the `vm.lifecycle.>` wildcard.

**Verified live (same probe that failed before):** `vm.lifecycle.get.srw-dev`
now returns the controller's real reply
(`{vm_name, ready, phase:"WaitingForVolumeBinding", created:true}`) on create and
the honest `404 / NotFound` after delete — no more `{stream, seq}`. Create→delete
cycle still round-trips clean.

**Deferred to the follow-up doc:** B's `vm.rpc.*` namespace convention (lands for
free when the event-driven watcher touches the controller) and making the
now-unconsumed `VM_EVENTS` stream load-bearing.

## Multi-agent research findings (2026-06-15)

A four-agent investigation (codebase audit + NATS best-practices + comparative
architecture + design) established:

- **This is documented NATS behavior, not a NATS bug.** A stream replies to a
  `request()` on a covered subject with a storage PubAck *by design* ("mandatory
  when archiving messages which have a reply subject set"); the hub-local ack
  beats a remote leaf responder. So mixing core request/reply with
  stream-covered subjects is the canonical footgun.
- **`vm.lifecycle.get` is the *only* collision in the system.** The codebase has
  exactly two request/reply subjects: this one, and `sudo.request.*` — and no
  stream covers `sudo.request.*`, so it's safe. Everything else is publish/push.
- **All three JetStream streams are unconsumed** (no consumers, no
  pull-subscribe; every consumer is a *core* push sub). `JOB_ASSIGNMENTS` has no
  producer at all; `AGENT_HEARTBEATS` captures nothing (worker heartbeats are
  HTTP, VM heartbeats are 5-token); `vm.status.>` had no producer. So `VM_EVENTS`
  cost us nothing today *except* this bug → narrowing it is zero-risk.
- **`no_ack: true` was rejected.** It would suppress the PubAck in one line, but
  it's stream-wide and sacrifices publish-confirmation for every event — a
  correct-but-lossy stopgap, not the right fix.
- **Best practice = structural subject segregation** (keep RPC out of an event
  stream's wildcard), and for *status specifically*, push events + a materialized
  view rather than synchronous cross-leaf request/reply. That's the follow-up.

## Summary

The orchestrator's live VM-status query (`vm.lifecycle.get.{oid}`, NATS
request/reply) is silently shadowed by the `VM_EVENTS` JetStream stream. The
stream is bound to `vm.lifecycle.>`, which also matches the *query* subject, so
JetStream returns a publish-ack (`{"stream":"VM_EVENTS","seq":N}`) on the
request's reply inbox before the VM controller's real reply can arrive
cross-cluster. The endpoint surfaces that ack verbatim as the "live" status.

Two defects are filed here:
1. **(primary)** the query subject lives under an event-stream wildcard;
2. **(landmine)** the chart's stream-provisioning Job can't actually apply a
   subjects change to an existing stream, so a naive fix won't take effect.

## How it was found / verified

While confirming the VM backend works on dev (it does — full create/delete
cycle round-tripped 2026-06-13, see the `project_vm_backend_disabled_on_dev`
memory note), a throwaway-job status probe never returned controller status:

- `nc.request("vm.lifecycle.get.srw-dev", …)` from the orchestrator pod returned
  `{'stream': 'VM_EVENTS', 'seq': N}` on **30/30** calls — reliably wrong, not
  flaky. The seq incremented every call (the queries are being *persisted* into
  the event stream).
- The live JetStream config on the shared hub (`nats` ns, 3-node):
  `VM_EVENTS  subjects=['vm.lifecycle.>', 'vm.status.>']`.

## Root cause

`vm.lifecycle.>` is a wildcard over the whole `vm.lifecycle.*` namespace, which
mixes **events** (fire-and-forget publishes we *want* persisted) with a
**query** (request/reply we do *not*):

| Subject | Caller | Pattern | Under `vm.lifecycle.>` | Affected |
|---|---|---|---|---|
| `vm.lifecycle.get.{oid}` | `nats_bridge.query_vm_status` | `nc.request` (reply) | ✅ | ✗ **shadowed** |
| `vm.lifecycle.create.{oid}` | `request_vm_create` | `nc.publish` | ✅ | ✓ (no reply to race) |
| `vm.lifecycle.delete.{oid}` | `request_vm_delete` | `nc.publish` | ✅ | ✓ |
| `vm.lifecycle.status.{oid}` | controller→orch (subscribe) | push | ✅ | ✓ |
| `agent.vm.{oid}.{job}.control` (freeze/resume/terminate) | `send_control` | `nc.publish` | ❌ (`agent.vm.*`) | ✓ |
| `sudo.request.{oid}.>` | sudo gate | request/reply | ❌ | ✓ |

When a message is published to a stream-covered subject **with a reply inbox**
(which `nc.request` sets), JetStream acks the store to that inbox. The VM
controller *also* has a core subscription on `vm.lifecycle.get.{oid}` and
replies to the same inbox — but it lives on the **vm cluster** (leaf node), so
its reply loses the race to the hub-local JetStream ack every time.

Create/delete are unaffected because they use `nc.publish` (no reply inbox → no
ack race); they're still persisted in the stream as intended. Control commands
are doubly safe (publish, and on the `agent.vm.*` prefix that no stream covers).

## Code path

- Endpoint: `orchestrator/main.py:6285` `get_vm_status`, live branch
  `:6308-6316`:
  ```python
  live_status = await vm_provisioner.query_status(job_id)
  if live_status:                 # the PubAck dict is truthy →
      result["live"] = live_status   # returns {"stream":"VM_EVENTS","seq":N}
  else:
      result["live_error"] = "No response from VM controller"   # never reached
  ```
- `vm_provisioner.query_status` (`orchestrator/services/vm_provisioner.py:436`) →
  NATS mode → `nats_bridge.query_vm_status`.
- `nats_bridge.query_vm_status` (`orchestrator/services/nats_bridge.py:322`):
  `response = await self._nc.request(self._subj("vm.lifecycle.get"), …)`
  then `return json.loads(response.data.decode())` — **no validation**, so the
  ack is returned as-is.
- Controller responder that *should* answer:
  `vm/controller/controller.py:328` `handle_status_query` → `_do_status`
  (`:274`, returns `{job_id, vm_name, ready, phase, created}`); subscribed at
  `:485` (`vm.lifecycle.get{suffix}`).
- Stream definition: `helm/templates/nats/streams-job.yaml:55-56`
  (`--subjects "vm.lifecycle.>,vm.status.>"`) and the equivalent manual command
  in the HomeLab repo `deployments_managed/nats/README.md:57-58`.

## Effects

- `GET /api/vms/{job_id}?live=true` returns a meaningless `"live"` object
  (`{stream, seq}`) instead of real KubeVirt status, and never reports the
  honest "No response from VM controller" fallback.
- **Real-world impact today ≈ nil**: the cockpit never calls `/api/vms/{id}` or
  `?live=true` (grep of `cockpit/src` is empty); the default `?live=false` path
  reads VM context from the DB and is fine. This is a latent trap for the first
  consumer of the live query (a future UI panel, admin `curl`, or MCP/automation
  client).
- Minor waste: every `?live=true` call writes a junk record into `VM_EVENTS`.

## Two gotchas for whoever fixes it

1. **On dev the stream is HomeLab-managed, not this chart.** `nats.internal:
   false` on dev (`deployment/values-experimental.yaml` only sets `nats.url`;
   chart default `helm/values.yaml:1256`), so the chart's `streams-job` is *not*
   rendered — the dev hub is the shared `nats` ns StatefulSet provisioned from
   the HomeLab repo. The in-repo `streams-job.yaml` only runs for
   `nats.internal: true` deploys (local k3d, prod-private).
2. **`nats stream add` won't update an existing stream.** The Job comment claims
   idempotency, but `nats stream add` against an already-existing `VM_EVENTS`
   with changed subjects no-ops/errors (it does not edit). A subjects fix must
   use `nats stream edit`/`update` (or delete+recreate) on the live hub — editing
   only the template string will silently not take effect on upgrade.

## Proposed fix

Pick one of A/B to restore function; C is cheap defense-in-depth that should
land regardless.

**A — Narrow the stream subjects (infra, smallest change).** Exclude the query
subject from `VM_EVENTS`, e.g.
`vm.lifecycle.create.>, vm.lifecycle.delete.>, vm.lifecycle.status.>, vm.status.>`.
Then `vm.lifecycle.get.*` is no longer stream-covered → controller reply wins.
- Apply to the **live** dev hub: `nats stream edit VM_EVENTS --subjects "…"`.
- Update both sources of truth: HomeLab `deployments_managed/nats/README.md`
  **and** `helm/templates/nats/streams-job.yaml`; while there, switch
  `nats stream add` → an upsert (`add || nats stream update`) so future subject
  edits actually apply (fixes gotcha #2).
- No image rebuild/redeploy. Risk: a future `vm.lifecycle.X` event subject must
  be added to the list explicitly, and the wildcard could be reintroduced by a
  later edit.

**B — Move the query off the event namespace (code, durable invariant).** Rename
the request/reply subject from `vm.lifecycle.get.{oid}` to e.g.
`vm.query.get.{oid}` in `nats_bridge.query_vm_status` + the controller
subscription (`vm/controller/controller.py`). A query subject that no event
stream wildcards can't be re-broken by a stream edit.
- Requires rebuilding **orchestrator + vm-controller** images and a coordinated
  redeploy (controller rolls via HomeLab `deployment-vms`). During skew the
  query degrades to the honest "No response" path (already broken, so no
  regression).

**C — Guard against ack-shaped replies (code, ~3 lines, complements A/B).** In
`query_vm_status`, treat a reply that has `stream`/`seq` but no `job_id` as
not-a-reply → return `None`, so the endpoint reports "No response from VM
controller" instead of surfacing a PubAck. Does not restore function on its own
(the ack always wins the race), but guarantees we never hand a JetStream ack to
a caller as VM status.

**Recommendation** (✅ actioned 2026-06-15 — see Resolution above): **C + A**.
C (trivial, stops garbage forever) + A (one `nats stream edit` restores real
function and clears the queries-as-events waste). **B** was *not* taken
standalone — the `vm.rpc.*` namespace convention is deferred into the
event-driven follow-up, where the controller is touched anyway.

## Acceptance

- `GET /api/vms/{job_id}?live=true` on a job with a real VM returns controller
  status (`vm_name`/`ready`/`phase`/`created`), or the explicit
  `live_error: "No response from VM controller"` — never a `{stream, seq}` ack.
- `?live=true` calls no longer append records to `VM_EVENTS`.
- Create / delete / control (freeze/resume/terminate) / status-push dispatch
  remain unchanged (regression check: the create→delete cycle still round-trips).
- If A is taken: the chart's stream Job applies subject changes on upgrade to an
  existing stream (no longer a silent no-op).

## Notes

Sibling concern (✅ fixed here): `helm/templates/nats/streams-job.yaml` documented
itself as idempotent but wasn't for config changes — now uses an `add || nats
stream edit -f` upsert for all three streams (`VM_EVENTS`, `AGENT_HEARTBEATS`,
`JOB_ASSIGNMENTS`).

Follow-up: the underlying reason `?live=true` exists is that the controller has
no KubeVirt phase watcher, so the orchestrator's stored VM status is stale
between create and delete. The event-driven redesign that fixes that (and makes
the unconsumed `VM_EVENTS` stream load-bearing) is its own doc:
[`docs/features/event_driven_vm_status.md`](../features/event_driven_vm_status.md).
