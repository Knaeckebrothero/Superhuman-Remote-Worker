# Workspace snapshot capture fails over SSH (wrong port / stale pod IP)

## Status

Open — observed 2026-05-27 on the dev cluster (`superhuman-remote-worker`).
Root cause identified below; fix not yet implemented. Tracked **separately**
from the workspace-provisioning unification work (see
`docs/features/unified_instance_lifecycle.md` and the session-workspace
reconcile gap) — it is an independent connectivity bug in the suspend path.

## Symptom

Every idle-suspension cycle, the orchestrator emits a burst of SSH `tar`
failures — one per idle workspace — each immediately followed by the suspend
being aborted ("keeping workspace alive"):

```
ERROR services.snapshot_service: SSH tar failed for thread b9b6c2be-…: ssh: connect to host 10.42.3.149 port 22: Connection refused
WARNING services.workspace_suspension: Snapshot capture failed for thread b9b6c2be-… — keeping workspace alive
ERROR services.snapshot_service: SSH tar failed for thread 2cae45a9-…: ssh: connect to host 10.42.2.216 port 22: No route to host
WARNING services.workspace_suspension: Snapshot capture failed for thread 2cae45a9-… — keeping workspace alive
ERROR services.snapshot_service: SSH tar failed for job 692f00d5-…: ssh: connect to host 10.42.3.101 port 22: Connection refused
WARNING services.workspace_suspension: Snapshot capture failed for job 692f00d5-… — keeping workspace alive
```

Scope: ~6+ workspaces fail on **every** suspension cycle (both `job` and
`thread` workspaces), some idle for weeks (the `692f00d5` job had been idle
~22 days). Two distinct SSH errors appear — `Connection refused` and
`No route to host` — both on **`port 22`** against **pod-network IPs**
(`10.42.x.x`).

## Root cause

The suspend path SSHes to the **wrong port** for container/pod workspaces.

`WorkspaceSuspensionService.suspend_workspace` resolves the snapshot SSH
target as (`orchestrator/services/workspace_suspension.py:126-127`):

```python
ssh_host = ws_ctx.get("pod_ip") or ws_ctx.get("host") or vm_ctx.get("ssh_host")
ssh_port = ws_ctx.get("port", vm_ctx.get("ssh_port", 22))   # ← VM-shaped default
```

Container workspace pods run SSHD on **30022** — `container_provisioner.py:675`
(`containerPort: 30022`), and the provisioner writes `port: 30022` into the
workspace context, but **only on the `ready` transition**
(`container_provisioner.py:216` for jobs, `:878` for threads). When
`workspace_container.port` is **absent** from the stored context, the lookup
falls through to the VM default **22** — which nothing listens on inside a
workspace pod → `Connection refused`. The snapshot routine reused for both
kinds, `snapshot_service.capture_vm_snapshot` (`snapshot_service.py:238`,
SSHes `agent-host@{host}:{port}` and pipes `tar -cf -`), is VM-shaped; 22 is
its natural default.

The logs confirm the default is being hit: `port 22` appears against
**pod-CIDR IPs** (`10.42.x.x` — container workspace pods, not VMs, whose IPs
live on the headscale tailnet). The provisioner never writes `22`, so a `22`
in the SSH command can only come from the missing-`port` fallback.

**Second failure mode** (`No route to host`): workspaces whose stored
`pod_ip` is stale — the pod was rescheduled or deleted and the IP is gone.
For these, no port would help; the snapshot source no longer exists.

## Why it persists, and impact

On snapshot failure the suspend is intentionally aborted and the status
reverted to `ready` ("keeping workspace alive",
`workspace_suspension.py:154-163`). So affected idle workspaces:

- are **never suspended** → their pods live indefinitely (resource waste,
  and directly feeds the `ws-thread-*` pod accumulation in
  `docs/issues/stuck_thread_workspace_pods.md`);
- **re-fail every cycle** → continuous ERROR log spam;
- make **suspend/restore unreliable** for container workspaces generally —
  relevant to the session-workspace lifecycle work, since the restore half
  can't be trusted if capture silently never runs.

No data-loss risk: the failure path keeps the workspace alive rather than
deleting it without a snapshot.

## Not the cause

- **Independent of the kubernetes-client 401 outage** (the unpinned
  `kubernetes` v36.0.0 bug). That broke the K8s API (pod create/list/reap);
  this is SSH-to-workspace connectivity and persists after auth recovery.
- **Not a snapshot-streaming regression.** Capture already streams; commit
  `ef27477b` fixed the *restore* memory path (`stream_extract_snapshot` in
  `ssh_helpers.py`). The failure here is purely connectivity (wrong port /
  gone host), before any bytes flow.

## Suggested fix direction

1. **Resolve the SSH port by workspace kind**, not a VM-shaped default:
   container/pod → `30022`, VM → `22`. Make one source of truth (the
   provisioner already knows the port); stop defaulting pod snapshots to 22.
2. **Tolerate / backfill missing `port`** in existing workspace contexts —
   default-by-kind, or re-derive from the live pod spec at suspend time.
3. **Detect gone/unreachable pods** (`No route to host`): if the workspace
   pod no longer exists, do not retry the snapshot forever — treat the
   workspace as already gone and clean it up (mark suspended-without-snapshot
   or delete) instead of "keeping alive" indefinitely. Distinguish permanent
   (gone) from transient failures.
4. Consider folding unreachable-workspace handling into the lifecycle
   reconciler's crash-recovery so dead workspaces get reaped rather than
   re-attempted each cycle.

## References

- `orchestrator/services/workspace_suspension.py:126-163` — SSH host/port
  resolution and keep-alive-on-failure.
- `orchestrator/services/snapshot_service.py:238-331` — `capture_vm_snapshot`
  (SSH `tar`, `agent-host@{host}:{port}`).
- `orchestrator/services/container_provisioner.py:216,675,878` — workspace
  SSH port `30022` (containerPort + the `ready`-transition context write).
- `orchestrator/services/ssh_helpers.py`, commit `ef27477b` — recent SSH
  refactor touching this area (restore-side streaming).
- Related: `docs/issues/stuck_thread_workspace_pods.md`,
  `docs/features/unified_instance_lifecycle.md`.
