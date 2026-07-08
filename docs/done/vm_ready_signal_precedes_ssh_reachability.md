---
tags:
  - issue
  - done
  - fix-spec
  - jobs
  - workspace-lifecycle
  - vm-backend
  - remote-backend
  - loop
---

# Investigation — VM `ready` signal precedes SSH reachability → loop jobs hard-fail on jobs-repo clone

**Status: DONE (2026-07-08).** Root cause confirmed in code + on the main cluster; two-part
fix designed, implemented, reviewed sound, and unit-verified (369 affected tests green, ruff
clean). Uncommitted on `develop` at time of writing — ships via the normal develop pipeline.
**One residual, non-blocking follow-up:** read the precise agent-vs-orchestrator reachability
delta `(b−a)` from the fix's own instrumentation post-deploy and confirm/adjust the Part 2
budget (preliminary evidence says the ~100 s default is ample — see §Track B results).

Root cause confirmed in code + on the main cluster. Implementation landed in this branch on
2026-07-08 with the two required parts:
1. **Orchestrator-side SSH reachability gate** before `context.vm.status="ready"` — closes
   the dominant failure (VM not yet a live tailnet node) and protects every
   orchestrator-vantage consumer (IDE seed, snapshot, IDE proxy).
2. **Agent-vantage bounded first-connect budget** (VM-sized) in `RemoteBackend` — the
   orchestrator probe canNOT prove the agent's *own* per-pod WireGuard peer path to the VM
   (that is the op that actually failed), so this is a **required companion**, not an
   optional mitigation.
**Verification (2026-07-08):** the affected suites pass (369 tests, ruff clean) — Part 1
covers register→`ssh_pending`, probe-success→`ready`+dispatch, probe-timeout→`ssh_unreachable`
without failing the job, stale-registration rejection, and leader-gating; Part 2 covers the
VM first-connect full-budget-on-timeout + the first-connect-only cap + unchanged container
behavior.

**Budgets are provisional (Track B partial — see §Track B results).** The defaults ship
env-tunable; preliminary live probing supports them but a precise `(b−a)` still wants the
fix's own post-deploy instrumentation. **Correction applied 2026-07-08:** an earlier
revision selected the orchestrator-side probe alone and called it "robust to peer Headscale
propagation lag"; that was an overclaim — the two vantages are different mechanisms, not
just differently timed (§B2).

**Known limitation — leadership change mid-probe (accepted):** the implementation gates the
whole register handler + probe on leadership and re-checks leadership before promoting, so
there is no split-brain promotion. But if the leader loses leadership *during* a probe, the
one-shot `register` broadcast is already consumed, so the new leader never re-probes and the
VM sits in `ssh_pending` until `dispatch_guards` RECYCLEs it (~`VM_PROVISION_TIMEOUT_S`),
costing one wasted VM boot. The implementation deliberately relies on that RECYCLE self-heal
rather than an explicit `ssh_pending` reconciler. Acceptable (leader flaps are rare + it
self-heals); revisit with a reconciler only if leadership churn proves frequent.

**One-line:** the orchestrator marks a freshly-provisioned VM `status="ready"` and
dispatches a job to it the instant the VM daemon registers over **NATS**, but "daemon
registered" does **not** prove the VM is reachable over **SSH-via-Tailscale**. An agent
handed a not-yet-reachable VM tries to clone the project jobs-repo onto it over SSH, the
connect times out, and the workspace-init **F29 hard-fail** (`src/core/workspace.py:415`)
fails the job. The existing SSH-connect retry (built for exactly this boot window) doesn't
save it because its error classifier treats a **timeout** as "ambiguous / give up quickly"
— which is wrong for a VM-over-Tailscale, where a still-booting VM presents as a *timeout*,
not a *connection-refused*.

**Severity:** loop-iteration waste + misleading diagnosis, **not** a loop-killer. The loop
self-heals (advances to the next stage/iteration; F29 is working as intended). But every
hit burns an iteration, bumps `consecutive_failures`, and — because `get_frozen_job`
**synthesizes a `version_upgrade` freeze label** for these rows — sends every future triage
down the wrong path (see §Gotcha). A burst (e.g. right after a deploy, when VMs are
provisioned en masse) can trip the failure cap.

**Related (read these first):**
[`agent_fast_freeze_on_dead_workspace.md`](agent_fast_freeze_on_dead_workspace.md) —
**authored the exact classifier this doc critiques** (its Part 2 table:
`ECONNREFUSED→booting`, `timeout→ambiguous cap 2`). This doc argues that table over-fit to
the *container* topology and is wrong for *VMs*. ·
[`ide_settings_sweeper_probes_stale_workspace_endpoints.md`](ide_settings_sweeper_probes_stale_workspace_endpoints.md)
(the `ide_settings` SSH path that also times out in this incident) ·
[`version_upgrade_drain_masked_by_coincident_error.md`](version_upgrade_drain_masked_by_coincident_error.md)
(the `get_frozen_job` synthesized-label trap — same lesson) ·
[`snapshot_restore_dead_for_jobs.md`](snapshot_restore_dead_for_jobs.md) /
`services/snapshot_service.py` (the pre-release snapshot SSH that also timed out here).

---

## The incident (confirmed)

Main cluster (`superhuman-remote-worker` ns), 2026-07-08, RSI loop for project
`68137e29` ("Hotel Rheinland ERP", MiniMax). Two **parallel-stage** loop iter-10 jobs
failed within 5 s of each other:

| Job | Config | Agent | VM (Tailscale) | Failed |
|---|---|---|---|---|
| `4fbeacda-0af4-41e2-952f-db9545f0e1f6` | scholar | `7c3ae192` (pod `srw-agent-j-8316b93c`) | `100.64.23.133:22` | 08:10:11Z |
| `a0c5fe75-aa6d-47a0-939b-ade252b30299` | product-qa | `1df72eea` (pod `srw-agent-j-8debacdc`) | `100.64.23.132:22` | 08:10:16Z |

**Authoritative DB rows** (`SELECT freeze_data, error_message FROM jobs WHERE id IN (…)`):
both have `freeze_data IS NULL` and
```
error_message = "Failed to clone project jobs repo 'project-68137e29-jobs' — refusing to
fall back to a disconnected git init (work would be lost on teardown). Check jobs-repo URL
reachability from this backend."
```

**Three independent SSH consumers all timed out to those two VMs** (orchestrator logs):
- `08:07:20` / `08:07:26` — `services.ide_settings`: `seed-for-user rc=255 for 100.64.23.132/.133:22 — ssh: connect to host … port 22: Connection timed out` (~3 min **before** the jobs failed — the VMs were unreachable from the start).
- (agent repo clone over SSH — the actual failure; agent logs are gone with the reaped pods, but the clone runs over the same SSH backend to the same host).
- `08:10:10` / `08:10:15` — `services.snapshot_service`: `SSH tar failed … ssh: connect to host … port 22: Connection timed out`.

**Trigger correlation:** the orchestrator had just rolled out to `sha-42e50cc` at
`08:05:34–08:05:59Z`; these jobs (created 08:03, pre-rollout) were dispatched to freshly
provisioned VMs at ~08:07 and gave up at ~08:10. VMs are **created per-job** and destroyed
after (`vm.lifecycle.create.srw-dev` → `created` → … → `delete` in the logs), so a ~5 min
golden-image cold boot sits on the dispatch critical path every iteration.

**Self-heal confirmed:** the follow-on critic `a390ff3e` (VM `100.64.23.142`, which *also*
timed out its `ide_settings` seed at `08:13:38`) got a booted VM and processed normally.

### Gotcha — `get_frozen_job` lies here
The MCP `get_frozen_job` reported `freeze_type: version_upgrade` / "orchestrator drain
intent at phase boundary" for **both** jobs. That is **synthesized and false** — the DB
rows have `freeze_data IS NULL` + the clone error above. Always confirm with
`SELECT freeze_data, error_message FROM jobs …` before pinning a freeze type. (Same lesson
as `version_upgrade_drain_masked_by_coincident_error.md`.) The `version_upgrade` drain fix
is **not** implicated and is working correctly.

---

## Root cause (confirmed in code)

### A — Readiness contract is a proxy that's true too early
`orchestrator/services/nats_bridge.py:448` (`_on_daemon_register`, subject
`agent.vm.*.register`) sets the VM `status="ready"`, records `ssh_host`/`ssh_port`, and
**triggers the dispatch callback (`_on_vm_ready`)** — with **no SSH-reachability probe**.
The daemon reporting its Tailscale IP over NATS proves *outbound NATS* works; it does not
prove *inbound SSH over the Headscale mesh* is up. The very next action in that handler
(`_seed_vm_ide_config` → `services.ide_settings.seed_ide_config_for_user`) SSHes to the VM
and is what logged the `08:07:20/26` timeout — i.e. the handler declares "ready" and then
immediately fails to reach the thing it just called ready.

Dispatch then trusts only that status: `orchestrator/main.py:3822-3970` runs the VM
provisioning decision, `dispatch_guards.py:88-93` maps `status="ready"` to `VM_READY`, and
`main.py:1830-1859` injects the VM SSH config when `status=="ready"` plus `ssh_host` are
present. `get_dispatchable_jobs` itself only gates on job status/assignment/freeze
(`postgres.py:3025-3057`); it does not know VM SSH readiness.

### B — NATS registration and SSH reachability are different planes
The VM daemon is in-repo (`docker/agent-vm-base/files/management-daemon.py`). It waits for
cloud-init's job config and for `tailscale ip -4` for up to 60 s
(`management-daemon.py:398-448`), then publishes
`agent.vm.{orchestrator_id}.{job_id}.register` (`management-daemon.py:223-238`). It does
**not** probe local `sshd`, and it falls through and registers even if the Tailscale wait
times out.

More importantly, NATS is not the same path as SSH. The VM controller writes the **local VM
cluster NATS leaf** URL into cloud-init (`vm/controller/controller.py:152-160`;
`helm-vm-cluster/templates/vm-controller/deployment.yaml:39-42`), and that leaf relays to
the hub (`helm-vm-cluster/templates/nats-leaf/configmap.yaml:17-20`). SSH to the VM uses
the Headscale/Tailscale mesh (`vm/controller/controller.py:17-20`). So daemon register
proves the VM can talk to the NATS leaf/hub and may have a local Tailscale IP; it does
**not** prove the orchestrator/agent peer has a route to `100.64.x.y:22`.

### B2 — The orchestrator and the agent reach the VM by *different* network paths (confirmed on the live tailnet)
This is the crux for fix *placement*, and it is not "propagation lag on one shared path" —
the two consumers use two different mechanisms:

- **Agent → VM: a fresh per-pod WireGuard peer session between two ephemeral tailnet
  nodes.** Every agent pod gets its own tailscale **sidecar** (injected by
  `orchestrator/services/agent_provisioner.py:1283-1337`, `tailscale:v1.82.5`, ephemeral
  `tailscale-state` emptyDir). `headscale nodes list` confirms each `srw-agent-j-*` is its
  own node (`tag:agent`, `ephemeral=true`, own `100.64.x` IP — e.g. `srw-agent-j-a8821e5d`
  = `100.64.23.138`). The VM is likewise its own ephemeral node (`tag:vm`, e.g.
  `vm-65fd85f1-…` = `100.64.23.144`, brought up by the VM-controller cloud-init
  `tailscale up … --accept-routes=false` — `helm/templates/vm-controller/configmap.yaml:109`).
  So the agent's clone path requires **both** ephemeral nodes to have each other in their
  netmap **and** a WireGuard handshake (direct or DERP-relayed) to complete — established
  freshly, per job, from the agent side.
- **Orchestrator → VM: a stable, shared node/subnet route.** The orchestrator pod is a
  **single container with no tailscale sidecar** (verified live: only `orchestrator` +
  init containers) and does **not** appear in `headscale nodes list` — it is not a tailnet
  node. Its SSH attempts *time out* (packets leave, no reply) rather than *no-route*, so it
  reaches `100.64.x` via a shared node-level/subnet route, independent of any per-agent
  netmap. `deployment/legacy/21-agent.yaml:331` documents the agent-side direct-route
  intent ("so the agent container can route directly to 100.64.x.y addresses").

**Consequence:** orchestrator-vantage reachability ≠ agent-vantage reachability. An
orchestrator SSH probe passing proves the VM is a live tailnet node reachable via the
shared route; it does **not** prove a *freshly-spawned agent sidecar* has completed its
peer session to that VM — which is exactly the operation (`git clone` over the agent's SSH
backend) that failed in this incident. In the incident both vantages failed together
(orchestrator IDE-seed + snapshot *and* the agent clone), which is consistent with "VM not
yet a live node at all" — the case the orchestrator gate does cover — but that is this
incident's shape, not a guarantee for the residual agent-only window. Hence the two-part
fix (§Chosen solution). `‹TODO(Track B): measure the agent-vantage window separately —
time from VM node 'online' in headscale to first successful clone from a *fresh* agent
sidecar; this sizes part 2›`

### C — F29 hard-fail is downstream and correct
`src/core/workspace.py:411-432` (`initialize_project_workspace`): clones the jobs-repo as
the workspace root via `GitManager.clone(url, path, backend=self._backend)`. On `None` it
**raises** rather than silently `git init` (F29 — a disconnected init loses all pushes on
teardown). The *failure mode* is intended; the trigger (unreachable VM) is the bug. Do not
"fix" this by re-enabling the fallback.

`GitManager.clone()` itself is also the wrong retry layer: it runs one remote `git clone`
through `backend.shell_run(..., timeout=120)` and returns `None` on non-zero output or any
exception (`src/managers/git_manager.py:777-802`). `initialize_project_workspace()` does
not clear the remote root before cloning, so a naive clone retry can trip over a partial
clone/non-empty target. The readiness race must be closed before the clone starts.

### D — The connect-retry that should absorb the boot window is mis-tuned for VMs
The retry that's *supposed* to cover the boot window lives one layer down in the backend:

`src/core/backends/remote.py:230` (`RemoteBackend.connect()`) already retries with backoff
"to tolerate the window between daemon registration (NATS) and SSHD readiness", sizing the
budget from `_classify_connect_error` (`remote.py:57`):

| socket error | bucket | budget (`_max_retries=5` default, `connect_timeout=30`, backoff 2→15) |
|---|---|---|
| `ConnectionRefusedError` (ECONNREFUSED) | `booting` | full 5 attempts ≈ **~3 min** |
| **`socket.timeout` / other** (falls through) | `ambiguous` | `min(5, _AMBIGUOUS_RETRY_CAP=2)` = **2 attempts ≈ ~60 s** |
| `socket.gaierror`, `EHOSTUNREACH`/`ENETUNREACH`/`ENETDOWN` | `gone` | 1 attempt, fail fast |

`_ensure_connected` (`remote.py:327`) calls `connect()` **once** (connect owns the whole
budget — the `max_retries²` nesting was already removed).

**The topology mismatch:** the classifier assumes "host up, sshd not listening yet" =
`ECONNREFUSED` (→ `booting`, full budget). That holds for a **container** on the pod
network. But a **VM reached over Tailscale that is still booting has no route yet** — the
SYN black-holes and you get a **connection *timeout***, which buckets as `ambiguous`
(~60 s) — giving up ~4 min before a ~5 min-boot VM is reachable. Even the best-case
`booting` budget (~3 min) is marginal against a 5 min boot. `ECONNREFUSED` for a VM would
require the Tailscale path to *already* be up, which is precisely the state we're waiting
for — so the "good" bucket is nearly unreachable for VMs, and the normal boot signal lands
in the "give up fast" bucket.

`RemoteBackend` construction does not currently distinguish the two topologies. The
orchestrator injects `workspace.backend="vm"` and VM SSH coordinates in
`main.py:1830-1859`, but no `connect_timeout`/`max_retries` override. Containers use the
same `RemoteBackend` branch (`main.py:1861-1888`). The agent builds `RemoteBackend` for
both `sandbox` and `vm` in the same path (`src/agent.py:1816-1850`) and passes no retry
overrides, so both use `connect_timeout=30`, `max_retries=5`. The loader preserves the
remote dict, but the agent currently ignores retry keys even if the orchestrator supplied
them.

---

## The design question this doc exists to answer

Retry-budget tuning **as the *sole* fix** (widen the VM connect budget to swallow a whole
cold boot, change nothing else) is a **mitigation, not the proper fix**, because:
1. It masks a wrong readiness signal by making the agent *tolerate* it.
2. Sized to a *cold boot* with no upstream gate, it re-introduces the dead-workspace-hang
   that `agent_fast_freeze_on_dead_workspace.md` deliberately prevented — a genuinely
   dead/never-booting VM also times out, so a wide standalone VM timeout budget burns
   ~6 min per iteration before failing and *hides* broken VMs.
3. A cold-boot-sized budget is a magic number pinned to today's ~5 min golden-image boot —
   a moving target.

(Note: Part 2 below is *also* an agent-side budget, but it is **not** this — it is paired
with the Part 1 gate, so it covers only the short post-gate peer-session window, not a cold
boot, which dissolves objections 2 and 3.)

The **proper fix is to make `ready` mean SSH-reachable**, closing the race at the source —
but because the orchestrator and the agent reach the VM by **different paths** (§B2), no
single vantage's probe is sufficient. The fix is therefore **two parts, both required**:

- **Part 1 — orchestrator/consumer-side SSH gate** between daemon register and
  `status="ready"`. This is necessary and high-value: it holds a VM out of dispatch until
  it is a live tailnet node reachable via the shared route, and it protects every
  orchestrator-vantage consumer (IDE seed, snapshot, IDE proxy) — none of which a
  dispatch-only or agent-only fix would cover. It is **not sufficient on its own** for the
  agent clone, whose path is the agent sidecar's fresh peer session (§B2).
- **Part 2 — agent-vantage bounded first-connect budget** in `RemoteBackend`, VM-sized to
  cover the residual "VM online → *this* agent sidecar's peer session established" window.
  This is the only check performed from the vantage that actually clones. Because Part 1
  guarantees the agent is only handed an already-live VM, Part 2's residual wait is short
  and bounded (peer-session setup, not a cold boot), so it does **not** reintroduce the
  dead-workspace hang the classifier guards against.

A daemon-side self-check (wait for local sshd + `tailscale status`) is a useful *third*,
optional hardening — it trims obviously-not-ready registers — but it cannot observe peer
netmap propagation, so it substitutes for neither part.

### Chosen solution — Part 1: register → `ssh_pending` → probe → `ready`

On `agent.vm.*.register`, `_on_daemon_register` should **not** write
`context.vm.status="ready"` and should **not** trigger dispatch immediately. Instead:

1. Merge a non-dispatchable VM context, e.g.
   `{"status": "ssh_pending", "ssh_host": ..., "ssh_port": 22, "hostname": ...,
   "daemon_pid": ..., "recovering": false, "registered_at": now,
   "ssh_registration_id": uuid}`. `dispatch_guards.vm_provisioning_decision()` already
   treats every non-`ready` status as `VM_WAIT` until `provisioned_at` exceeds
   `VM_PROVISION_TIMEOUT_S`, then recycles, so `ssh_pending` fits the existing state
   machine.
2. Start an idempotent async probe task from the orchestrator process (or an equivalent
   mesh-enabled probe pod if a deployment ever makes the orchestrator's route differ from
   the agent path). Use the existing `orchestrator/services/ssh_helpers.py`
   command-building pattern, but probe with an authenticated, cheap command
   (`ssh ... BatchMode=yes ... true`) rather than just `nc`, so the gate proves route +
   sshd + key authorization. Add a helper such as
   `wait_for_agent_ssh(ssh_host, ssh_port, *, deadline_s, connect_timeout_s, interval_s)`
   to avoid ad-hoc shelling in `nats_bridge.py`.
3. On probe success, promote the **same registration** to `status="ready"` and add
   `ssh_verified_at`, `ssh_probe_attempts`, and clear `ssh_probe_error`. Then run the
   existing leader-gated IDE seed and call `_on_vm_ready()` to poke dispatch.
4. On probe timeout, leave the VM non-dispatchable (`ssh_pending` or
   `ssh_unreachable`) with `ssh_probe_error`/`ssh_probe_timed_out_at`. Do not fail the job
   directly. Let the existing dispatcher timeout/recycle/park budget handle a VM that
   never becomes reachable.

Because `agent.vm.*.register` is a broadcast subject, this must be stale-safe. The
implemented choice is **leader-only registration handling**: followers ignore the register;
the leader writes `ssh_pending`, starts the probe, and promotes only if it still holds
leadership. If the leader dies after writing `ssh_pending`, the existing dispatcher
`VM_PROVISION_TIMEOUT_S` path recycles the abandoned non-ready VM. Promotion uses
`merge_vm_context_if_current()` / `merge_thread_vm_context_if_current()` to merge only when
`ssh_registration_id` still matches, preventing a slow probe from an old VM incarnation
from marking a freshly re-provisioned VM ready. `VMProvisioner._fresh_provision_ctx()`
also clears `ssh_registration_id` and probe metadata at the start of every reprovision, so
old probe tasks cannot match after recycle.

This placement (gate the *status*, not just dispatch) protects every consumer that already
treats `vm.status=="ready"` as the source of truth: dispatch (`main.py:1830-1859`,
`2225-2242`, `3822-3970`), IDE session restore (`services/ide_session.py:474-490`,
`800-816`), IDE proxy (`services/ide_proxy.py:104-108`), IDE settings seeding, snapshot
capture, and workspace suspension — all of which reach the VM from the **orchestrator
vantage**, so the orchestrator's own probe is a faithful proxy for them. A dispatch-only
check would leave those other consumers racing.

**But Part 1 does not close the agent clone by itself.** The gate proves the VM is
reachable from the orchestrator's shared route; the agent clones over its *own* fresh
per-pod tailscale sidecar (§B2), whose peer session to the VM the orchestrator cannot
observe. Part 2 (below) covers that residual window.

### Chosen solution — Part 2: agent-vantage bounded first-connect budget

In `RemoteBackend`, a VM-backed workspace's **first** `connect()` gets a budget sized to
the agent-sidecar peer-session window (Track B `‹TODO›`), while later reconnects keep the
existing bounded behavior. The implemented topology signal is
`remote.retry_timeouts_as_booting=true`, injected only for VM workspace config. The
orchestrator also injects env-tunable retry knobs:
`VM_REMOTE_CONNECT_TIMEOUT_S` (default `10`) and `VM_REMOTE_CONNECT_MAX_RETRIES` (default
`6`). The agent plumbs those through `WorkspaceConfig.remote` to `RemoteBackend`.
Container behavior is unchanged — timeouts still cap at two attempts unless the VM-only
flag is present (§Root cause D). Because Part 1 guarantees the VM is already a live node
when the job is dispatched, this budget covers peer-session setup, not a cold boot, so it
stays small and cannot mask a genuinely dead VM.

---

## Open questions (static answers + remaining dynamic work)

1. **What exactly is not-ready during the window?** The daemon registered *reporting its
   Tailscale IP* and reached NATS, yet nothing could route to `:22`. Which is it:
   - (i) **sshd not up yet** inside the VM (daemon process started before sshd) → a
     **daemon-side self-check** (don't register until sshd listens) fixes it;
   - (ii) **Headscale netmap/route-propagation lag** — sshd is up but peers (orchestrator,
     agent) don't yet have a route to the VM's node → only a **consumer-vantage probe**
     (orchestrator or agent) can confirm reachability; a daemon self-check can't observe
     peer netmap state;
   - (iii) **Tailscale interface/route not up on the VM itself** → daemon-side check on
     `tailscale status` fixes it.
   **Static/live answer:** the fix does not hinge on choosing exactly one *cause*, but §B2
   (confirmed live) establishes there are **two consumer vantages**, not one — the
   orchestrator's shared node/subnet route and the agent's fresh per-pod tailscale peer
   session — so a single consumer-vantage probe is insufficient. Part 1 covers the
   orchestrator vantage; Part 2 covers the agent vantage. Pinning (i)/(ii)/(iii) still
   sharpens root-cause precision and tells us whether optional daemon-side hardening (§Sol #3)
   is worthwhile. `‹TODO: determine (i)/(ii)/(iii) from VM daemon + headscale + boot logs —
   see Search B2/B3›`
2. **Does the daemon reach NATS over Tailscale or a different path?** If NATS is reached
   over Tailscale, `register` implies the tailnet is at least partially up on the VM (argues
   against (iii), toward (ii)). If NATS uses a node-local / non-Tailscale path, `register`
   says nothing about Tailscale. **Resolved statically:** the VM controller injects the
   VM-cluster NATS leaf URL; the leaf relays to the hub. Register does not prove the
   Headscale SSH path to the orchestrator/agent peer.
3. **Is `RemoteBackend` for VMs constructed with the default `max_retries=5`, or an
   override?** **Resolved statically:** default `connect_timeout=30`, `max_retries=5`.
   VM and container jobs share the same `RemoteBackend` construction path in the agent; no
   VM-specific retry override is plumbed today.
4. **Is there any warm-pool / pre-boot mechanism today, or strictly create-per-job?** Logs
   show create-per-job; confirm no pool exists before proposing one. **Resolved
   statically:** KubeVirt/NATS/HTTP/direct VMs are create-per-job. The VM controller can
   pre-warm golden image DataVolumes, but that only removes image import/clone work; guest
   boot + daemon-register remains on the critical path. Docker Compose has a static QEMU
   host pool, but that is not the incident topology.
5. **Boot-window magnitude:** what is the real distribution of "daemon-register →
   `:22`-answers" wall-clock across recent VM jobs? Needed to size any bounded probe/budget
   and to justify a warm pool. `‹TODO: Search B4›`

---

## Search instructions for follow-up

Track A is complete enough to select the fix design (both parts). Track B is still needed
to (1) size **both** budgets — the Part 1 probe and the Part 2 agent-vantage first-connect —
via B1's per-vantage timings and the (b−a) delta, (2) confirm §B2's two-path finding
quantitatively, and (3) decide whether daemon-side hardening or a future warm pool is worth
it. Dynamic work needs `kubectl --context=main` + `--context=vm` on the homelab; skip/flag
if unavailable.

### Track A — codebase (completed; retained as provenance)

- **A1 — Readiness handler.** Read `orchestrator/services/nats_bridge.py:448-540`
  (`_on_daemon_register`, `_seed_vm_ide_config`). Confirm: (a) what marks the VM
  dispatchable, (b) whether `_on_vm_ready` → the dispatch poke has *any* reachability gate.
  Trace `_on_vm_ready` to its dispatcher callback (grep `on_vm_ready` across
  `orchestrator/`). Document the exact status/field the auto-assign dispatcher reads to
  consider a VM job dispatchable.
- **A2 — Dispatch predicate.** In `orchestrator/main.py`, find the auto-assign dispatcher
  (`grep -n "using VM workspace\|get_dispatchable\|assigned_agent_id IS NULL" orchestrator/main.py`;
  the incident logged `main.py:4451` "using VM workspace" and `main.py:2269` "injected VM
  workspace config"). Document what gates VM dispatch and where an SSH probe would insert.
- **A3 — Clone + hard-fail path.** Re-read `src/core/workspace.py:406-432` and
  `src/managers/git_manager.py:753-800` (`clone`). Confirm no retry/cleanup between
  attempts and that a non-empty target would break a naive retry (idempotency note for any
  agent-side retry option).
- **A4 — The classifier + budget.** `src/core/backends/remote.py`:
  `_classify_connect_error:57`, buckets `_AMBIGUOUS_RETRY_CAP:53`, `connect():230-286`,
  `_ensure_connected:327`, `_GONE_ERRNOS:54`. Verify the wall-clock math per bucket and
  whether `socket.timeout`/`TimeoutError` is truly the fall-through (`ambiguous`). Note
  whether paramiko raises `socket.timeout` vs `OSError(ETIMEDOUT)` for "connection timed
  out" — the bucket hinges on the exact type.
- **A5 — VM backend construction (Open Q3).** `grep -rn "RemoteBackend(" src/ orchestrator/`
  and trace how a **VM** job's backend is built vs a **container** job's — where do `host`,
  `port`, `username`, `key_path`, and (crucially) `max_retries`/`connect_timeout` come
  from? Is there any VM-vs-container branch? This is where a backend-aware budget/flag would
  live.
- **A6 — VM provisioning + any pool (Open Q4).** `orchestrator/services/vm_provisioner.py`
  and `services/nats_bridge.py` publish sites: `grep -rn "vm.lifecycle\|agent.vm" orchestrator/`.
  Determine create-per-job vs pool, and where "VM is ready to dispatch to" is decided.
- **A7 — The VM-side daemon source (Open Q1/Q2).** Find what **publishes**
  `agent.vm.*.register`: `grep -rn "agent.vm\|\.register\b" --include=*.py --include=*.sh .`
  and check the golden-image build (`docker/`, `grep -rn "register" docker/ scripts/`,
  and any `vm`/`daemon`/`cloud-init`/`ignition` assets). If the daemon source lives in this
  repo, a **daemon-side self-check before register** is viable; if it's baked into an
  external image, note that. Document: does the daemon start sshd itself / know when sshd is
  listening / know `tailscale status`?
- **A8 — Prior art.** Re-read `agent_fast_freeze_on_dead_workspace.md` §"Design (Tier B)"
  Part 2 and its tests T2/T3 in `tests/` (`grep -rln "classify_connect\|_AMBIGUOUS_RETRY_CAP\|EHOSTUNREACH" tests/`).
  Any change to the classifier must update those tests; cite the test file/class.

### Track B — preliminary results (2026-07-08, partial)

Attempted the live `(b−a)` measurement; **no clean boot-window sample was catchable** in the
session (the loop was between jobs — the only active VM job was a *resume/reattach*, which
does not re-`register`, and the next fresh VM completed + was torn down before probing). What
was measured:

- **Fresh agent tailscale sidecar reaches `Running` + DERP-connected in ~3 s** (pod
  `srw-agent-j-5ae6edff`, sidecar log `NoState→NeedsLogin→Starting→Running` 11:08:50→11:08:53).
  So the **agent-vantage-only join cost is O(seconds), not minutes** — Part 2's residual
  window is small, which means the provisional ~100 s VM first-connect budget is comfortably
  sufficient and **Part 2 is cheap insurance, not a big lever**. (The two-path *architecture*
  still holds — the orchestrator pays no such per-pod join — the *magnitude* is just small.)
- **Dual-vantage probe agreement:** probing a VM `:22` from the orchestrator pod and from a
  warm agent pod (shares its sidecar's netns) returned identical results both times looked
  at — i.e. no agent-vs-orchestrator *divergence* observed; unreachability showed up on both
  vantages together (VM-side / not-up), the mode Part 1 targets. NB one sample was against an
  already-`deleted` VM (`context.vm.status=deleted`, node lingering in headscale as stale
  `online`) — a caution that headscale "online" ≠ reachable, and that the probe correctly
  saw the dead VM as unreachable from both vantages.
- **Verdict:** Part 1 is the load-bearing fix (dominant failure = VM not up, seen on both
  vantages); Part 2 is correctly scoped as small insurance. Keep the provisional budgets.
- **Still TODO — precise `(b−a)` post-deploy:** once the fix ships, read it straight from the
  fix's own instrumentation: the Part 1 probe logs `VM SSH ready … after N attempts`
  (orchestrator-vantage time-to-reachable), and the agent's first-connect retries log per
  attempt (agent-vantage time-to-reachable). The delta across a batch of fresh VM boots is
  the real `(b−a)`; only tune the Part 2 budget down/up if it disagrees with the ~seconds
  estimate above. Full live procedure retained below for that pass.

### Track B — live cluster procedure (for the post-deploy pass)

> All `psql` via: `kubectl --context=main -n superhuman-remote-worker exec srw-postgres-0 -- psql -U srw -d srw -At -F'|' -c "…"`.
> VMs run in a **separate** cluster: `--context=vm`, namespace `agent-vms` (KubeVirt:
> `virt-launcher-agent-vm-<jobid>`, controller `srw-vm-vm-controller`, `srw-vm-nats-leaf`).
> Headscale control plane: `--context=main -n headscale`.

- **B1 — Reproduce the readiness gap from BOTH vantages (sizes Part 1 *and* Part 2).**
  Provision a fresh loop VM (or watch the next one) and, after
  `Daemon registered for job … (ssh=100.64.23.x:22)` (orchestrator log), time when `:22`
  first answers **from each path separately**: (a) the **orchestrator vantage** — from the
  orchestrator pod / its shared node route (`kubectl … exec deploy/srw-orchestrator -c orchestrator -- sh -c 'while ! nc -w2 -z 100.64.23.x 22; do date; sleep 1; done'`); (b) the
  **agent vantage** — from a *fresh* pod that carries its own tailscale sidecar (the only
  faithful proxy for the agent's WireGuard peer path), e.g. exec into a just-provisioned
  `srw-agent-j-*` and loop `nc`/`ssh -o ConnectTimeout=5`. **The delta (b − a) is the
  residual agent-only window Part 2 must cover;** (a) sizes the Part 1 probe. If (b) ≈ (a),
  the divergence is negligible in practice (Part 2 can be minimal); if (b) ≫ (a), Part 2 is
  load-bearing. Answers Open Q5 + validates §B2 quantitatively.
- **B2 — Distinguish (i)/(ii)/(iii) (Open Q1).** During that window, from the VM guest
  console/`virsh`/`virtctl console` (or KubeVirt `virtctl console agent-vm-<id> -n agent-vms`):
  is `sshd` listening (`ss -tlnp | grep :22`)? Is `tailscale status` "Running" with a
  route? Cross-check from a peer: does `tailscale status` on an agent/orchestrator node show
  the VM node as reachable, and *when* does the route appear vs when sshd came up? Route
  appears **after** sshd → (ii) netmap lag. sshd comes up **after** register → (i).
- **B3 — Headscale propagation (Open Q1-ii).** `kubectl --context=main -n headscale logs deploy/headscale --since=…`
  around a VM registration — look for node-register → netmap-push latency to peers. Also
  check whether VM nodes use ephemeral auth keys and how fast they appear in
  `headscale nodes list`.
- **B4 — Boot-window distribution (Open Q5).** From orchestrator logs over the last N loop
  iterations, extract per-job `Daemon registered` → first successful SSH (or → failure)
  deltas. `‹TODO: paste a table›`. This sizes any bounded probe and justifies (or not) a pool.
- **B5 — NATS path (Open Q2).** Inspect the VM daemon's NATS leaf config
  (`kubectl --context=vm -n agent-vms … srw-vm-nats-leaf`, and the daemon's connect URL on
  the VM) — is the leaf reached over Tailscale or a node route? Determines whether
  `register` implies Tailscale-up.

---

## Solution options (updated after static search)

1. **Required — Part 1: orchestrator/consumer-side SSH readiness gate.** On
   `_on_daemon_register`, write `ssh_pending`, run a bounded authenticated SSH probe, and
   only then promote to `ready` and trigger dispatch. Robust for the **orchestrator-vantage**
   consumers (IDE seed, snapshot, IDE proxy) and for the dominant "VM not yet a live tailnet
   node" failure — but **not sufficient for the agent clone**, which uses the agent's own
   per-pod tailscale peer path the orchestrator can't observe (§B2). Implemented with
   `VM_SSH_READY_DEADLINE_S` defaulting to `VM_PROVISION_TIMEOUT_S` (currently `600`),
   `VM_SSH_READY_CONNECT_TIMEOUT_S=10`, and `VM_SSH_READY_INTERVAL_S=5`; Track B should
   tune those values from live timings.
2. **Required — Part 2: agent-vantage bounded first-connect budget (was "mitigation").**
   Promoted from optional stopgap to a required companion once §B2 established that the
   agent reaches the VM by a different path than the orchestrator. In `RemoteBackend`, a
   VM-backed workspace's first `connect()` gets a budget sized to the agent-sidecar
   peer-session window (Track B); **keep container behavior unchanged** (do NOT globally
   reclassify `timeout→booting` — re-breaks container fast-freeze, §Root cause D). Requires
   a topology/config signal because VM and container `RemoteBackend` construction share a
   backend class; implemented as VM-only `retry_timeouts_as_booting` plus
   `VM_REMOTE_CONNECT_TIMEOUT_S=10` / `VM_REMOTE_CONNECT_MAX_RETRIES=6`. Safe (won't mask a
   dead VM) **only because Part 1 guarantees the VM is already live** — Part 2 without Part
   1 would reintroduce the dead-workspace hang.
3. **Optional daemon-side hardening — local self-check before `register`.** The daemon
   source is in-repo, so we can later wait for local `sshd` listening and stronger
   `tailscale status` health before publishing. Reduces noise and shortens the
   orchestrator-side wait when the local guest is clearly not ready, but it is **not
   sufficient** as a sole fix (or a substitute for Part 1/Part 2) because it cannot prove
   peer route/peer-session propagation.
4. **Strategic — warm VM pool / pre-boot.** Keep a small pool of pre-booted,
   already-on-tailnet VMs so a ~5 min cold boot is off the dispatch critical path; assign
   from the pool (reachable minutes before a job exists) instead of create-per-job. Removes
   the whole failure class but is a larger allocator/lifecycle project. Current code has
   golden DataVolume pre-warm only, not a booted KubeVirt VM pool. Cross-ref
   `project_vm_golden_image`, `project_vm_persistent_rootdisk`, and
   `docs/done/vm_golden_image_boot_acceleration.md`.
5. **Defense-in-depth (distinct from Part 2).** Part 2 sizes the *first* connect (the
   boot/peer-session window); separately keep the existing *bounded* `_ensure_connected`
   reconnect for genuine **mid-session** drops after an established session — not sized to a
   cold boot. Different window, same backend.

**Do NOT** re-enable the F29 git-init fallback (`workspace.py`), and **do NOT** blanket-widen
the `ambiguous` cap for all backends.

### Test plan for the chosen fix

- `tests/test_nats_bridge.py`: daemon register writes `ssh_pending` + SSH coordinates, does
  **not** call `_on_vm_ready`, and does **not** seed IDE config before probe success.
- `tests/test_nats_bridge.py`: successful probe promotes the same registration to
  `ready`, records `ssh_verified_at`, and calls `_on_vm_ready` once. Duplicate register or
  duplicate probe success is idempotent.
- `tests/test_nats_bridge.py`: stale probe success (registration id/host no longer matches
  DB context) must not promote to `ready`.
- `tests/test_nats_bridge.py` or a new `tests/test_ssh_helpers.py`: probe timeout leaves
  the VM non-dispatchable with a recorded probe error; it does not fail the job directly.
- `tests/test_dispatch_guards.py`: `ssh_pending` waits within `VM_PROVISION_TIMEOUT_S` and
  recycles after it, same as other non-ready provisioning states.
- `tests/test_nats_register_seed_gate.py`: IDE seeding remains leader-gated, but moves to
  after SSH probe success.
- **Part 2 (agent-vantage budget) — the regression that would have caught *this*:**
  `tests/test_workspace_backends.py`: a VM-topology `RemoteBackend` whose first `connect()`
  sees N consecutive connection-*timeouts* (booting/peer-session window) does **not**
  exhaust its budget before the configured VM first-connect deadline (i.e. it would have
  survived the incident), while an identical **container** backend still fast-caps a
  timeout (unchanged §Root cause D behavior). Assert the topology signal actually flows
  from the injected VM config → `WorkspaceConfig.remote` → `RemoteBackend`.
- Keep existing F29 and remote-backend tests: `tests/test_workspace_git.py` still proves no
  silent git-init fallback; `tests/test_workspace_backends.py` still proves container
  timeouts stay capped.

---

## Acceptance criteria for the completed doc
- Static questions answered with cited evidence; B1-B4 still needed for budget sizing and
  root-cause precision.
- **Both vantages covered (§B2):** the fix is two-part — Part 1 orchestrator-side probe
  before `ready` (stale-safe promotion at `_on_daemon_register`) **and** Part 2
  agent-vantage VM first-connect budget in `RemoteBackend`. Neither part alone closes both
  the orchestrator-vantage consumers and the agent clone path.
- Part 1 probe budget **and** Part 2 first-connect budget are wired as env-tunable values;
  live measurements (B1's per-vantage timings + the (b−a) delta) remain to finalize the
  tuned defaults.
- Position recorded: the agent-side VM budget is a **required companion (Part 2)**, not an
  optional mitigation (corrected after §B2); warm VM pool is strategic/out of scope for the
  immediate correctness fix.
- Test plan covers register→pending, probe success, probe timeout, stale promotion, IDE
  seed timing, dispatch guard behavior, the **Part 2 agent-vantage budget regression**, and
  the unchanged F29 + container-timeout-cap paths.
- Confirmation the `get_frozen_job` synthesized-`version_upgrade` label is noted wherever
  triage runbooks live (so this isn't re-misdiagnosed).
