# Stuck `ws-thread-*` workspace pods (Unknown / PodFailed)

## Symptom

On the `main` cluster, namespace `superhuman-remote-worker`, multiple persistent
thread workspace pods are sitting in `Unknown` status for days and never get
cleaned up. Observed 2026-05-01:

```
ws-thread-0e06a5b2-daa   0/1 Unknown   5d23h   node4
ws-thread-1d2efe15-27d   0/1 Unknown   5d11h   node4
ws-thread-24db8ca7-b6f   0/1 Unknown   5d      node4
ws-thread-3bf36cb3-fa2   0/1 Unknown   3d10h   node4
ws-thread-44d7ba07-c67   0/1 Unknown   5d5h    node4
ws-thread-5e9e6c73-dfc   0/1 Unknown   3d9h    node4
ws-thread-635a6399-91d   0/1 Unknown   5d22h   node4
ws-thread-6d33bf30-4cb   0/1 Unknown   3d9h    node4
ws-thread-a5684ac6-abe   0/1 Unknown   5d2h    node4
ws-thread-e027aee7-228   0/1 Unknown   5d21h   node4
```

Healthy thread pods that came up afterwards (`ws-thread-c0d78804-a46`,
`ws-thread-c7b460dc-ed6`) landed on node3.

## Investigation 2026-05-01 — node4 walk-through

`kubectl describe node node4` and friends give a clearer picture than the
initial hypothesis:

- **node4 is healthy and not avoided.** No taints, `Unschedulable: false`, all
  conditions `False`/`Ready=True` since `2026-04-22T11:21:28Z` — no kubelet
  restart since then. The srw namespace currently has 19 pods on node4 vs 7 on
  node3 and 5 on node2, so the scheduler is *not* steering away from it. The
  two new healthy thread pods landing on node3 was a coincidence, not a
  pattern.
- **The container actually died, the pod is not just "marked" failed.** The
  stuck pod `ws-thread-0e06a5b2-daa` shows:
  ```
  state.terminated:
    exitCode: 255
    reason: Unknown
    startedAt:  2026-04-25T20:18:01Z
    finishedAt: 2026-04-28T17:41:15Z
  ```
  Exit code 255 + reason `Unknown` is what kubelet writes when it loses track
  of a container's exit state — typically a containerd hiccup/restart that
  kills running containers without giving kubelet a clean exit code.
- **Same instant for all ten.** All ten stuck pods transitioned to
  `Ready=False, reason=PodFailed` at `2026-04-28T17:41:19Z` — i.e. the same
  containerd event killed all of them.
- **Other workloads on node4 *were* killed and recreated.** Most pods on node4
  show "Age 3d2h" (~2026-04-28 18:30 UTC, about an hour after the failure):
  Deployments and StatefulSets respawned themselves. The ten thread pods are
  bare `Pod`s with no owner reference, so nothing recreated them — they just
  sit there as tombstones.

So the actual story is: a containerd-level disruption on node4 around
2026-04-28 17:41 UTC killed every container on the node. Controller-managed
pods got recreated within an hour. The thread workspace pods, which have no
controller, died and stayed dead.

**Likely cause (per operator, 2026-05-01):** rack work — a new server was
being installed and the cluster nodes were powered down to move equipment.
That fits the symptom perfectly: a hard power-off kills every container
without giving kubelet a chance to record exit codes, which is exactly the
`exitCode: 255, reason: Unknown` signature we see. When node4 came back up,
controller-managed workloads were respawned by their controllers; the bare
thread Pods had nothing to recreate them.

So the trigger is understood; no journal dive needed unless we want to confirm
the exact power-on timestamp.

## Workspace data is ephemeral — `pvc-ws-thread-*` does not exist

> **RESOLVED 2026-08-04 — `pvc-ws-thread-*` is now created.** The gate this
> section correctly suspected in point 2 below ("the PVC creation is gated behind
> a config flag that is not set") was real: `WORKSPACE_PVC_ENABLED`, which at the
> time also carried an `owner.kind == "job"` check that skipped sessions
> entirely. That check has been removed. Under the flag a session now gets
> `pvc-ws-thread-<tid[:12]>` for its workspace pod plus `pvc-agent-s-<tid[:12]>`
> for its agent pod, both reclaimed only when the `threads` row is hard-deleted.
> `delete_thread_workspace_pvc()` is therefore no longer dead code. Everything
> below still describes the emptyDir behavior of the 2026-04-28 incident, which
> is what any pod provisioned with the flag **off** still does — including the
> data-impact conclusion.
> Design: [`workspace_pvc_branch_a_implementation.md`](../features/workspace_pvc_branch_a_implementation.md).

While verifying PVC survival, I found that **none of the `pvc-ws-thread-*`
PVCs exist in the namespace.** All ten lookups returned `NotFound`. Inspecting
the actual pod spec:

```
spec.volumes:
  - name: workspace-data
    emptyDir: { sizeLimit: 10Gi }
  - name: ssh-pubkey
    secret: { secretName: srw-vm-ssh-key, ... }
  - name: kube-api-access-...
    projected: ...
```

Both stuck *and* healthy thread pods mount `emptyDir` at `/home/agent-host` for
the workspace. There is no PV. This means:

1. **Force-deleting these zombies does not lose any data — it is already
   lost.** When the container died on 2026-04-28, the emptyDir went with it.
2. The `delete_thread_workspace_pvc()` code path at
   `orchestrator/services/container_provisioner.py:891` (which expects a PVC
   named `pvc-ws-thread-<id[:12]>`) is dead code in this deployment. Either
   the manifest produced by `create_thread_workspace` has stopped including
   the PVC, or the deployed orchestrator image is older/newer than the source
   tree, or the PVC creation is gated behind a config flag that is not set on
   `main`. Worth tracing.
3. The earlier "PVCs survive force-delete" note in this doc was wrong and has
   been corrected below. Persistent thread workspaces are *not actually
   persistent* on this cluster.

## Why they are not getting cleaned up

- `ws-thread-*` pods are created in
  `orchestrator/services/container_provisioner.py:789` (`create_thread_workspace`)
  and named `ws-thread-{thread_id[:12]}`.
- They are deleted by `release_thread_workspace()`
  (`container_provisioner.py:316`), which is invoked **only on explicit thread
  close** from Cockpit. The function tries to snapshot to S3 first, then
  deletes the pod and its PVC (`pvc-ws-thread-<id>`, line 891).
- There is **no garbage collector** for thread pods stuck in `Unknown` /
  `Failed`. Nothing scans `srw/component=thread-workspace` and reaps zombies.
  They accumulate indefinitely after every node disruption.

## Data impact

The `workspace-data` volume is `emptyDir` (10Gi limit), not a PVC. Workspace
data was lost on 2026-04-28 17:41 UTC when the containers died. Force-deleting
the zombie pod objects now is purely cosmetic from a data standpoint — there
is nothing to preserve.

*(2026-08-04: true for this incident and for any emptyDir-era pod. It is no
longer a safe general rule — with `workspace.pvcEnabled` on, a stuck
`ws-thread-*` pod backs onto `pvc-ws-thread-*` and force-deleting the pod
preserves the data; the volume reattaches by name. Check the pod's actual
`spec.volumes` before assuming there is nothing to lose.)*

Snapshotting from a zombie pod is not possible either — `release_thread_workspace`
needs a live pod IP and `Ready` status (line 334-347). For the current ten
zombies, "snapshot then delete" degrades to "just delete", and even a live
snapshot would only have captured emptyDir contents that are already gone.

## Proposed remediation

### 1. Manual cleanup (now)

Force-delete the ten zombies. PVCs stay intact:

```bash
kubectl --context=main -n superhuman-remote-worker delete pod \
  ws-thread-0e06a5b2-daa ws-thread-1d2efe15-27d ws-thread-24db8ca7-b6f \
  ws-thread-3bf36cb3-fa2 ws-thread-44d7ba07-c67 ws-thread-5e9e6c73-dfc \
  ws-thread-635a6399-91d ws-thread-6d33bf30-4cb ws-thread-a5684ac6-abe \
  ws-thread-e027aee7-228 --force --grace-period=0
```

### 2. Root-cause node4 disruption — RESOLVED

The 2026-04-28 17:41 UTC failure was caused by a rack-level power-down (operator
was installing a new server and had to move equipment). All cluster nodes went
off and came back. That matches the `exitCode: 255, reason: Unknown` symptom
exactly — hard power-off kills containers without kubelet recording exit codes.

No further action needed on the trigger itself; the open follow-up is the
*why-do-we-have-zombies-after-any-power-event* class of bug, addressed by
items 3 and 4 below.

### 3. Add a thread-workspace reaper (code fix)

In `container_provisioner.py`, add a periodic task that:

1. Lists pods with label selector
   `srw/component=thread-workspace,srw/thread-id` in the namespace.
2. For each pod whose `status.phase` is `Failed` or whose
   `status.reason == "PodFailed"` or whose `Ready` condition has been `False`
   for > N minutes (e.g. 15 min), look up the corresponding thread record in
   Postgres:
   - **Thread still alive** → force-delete the pod, keep the PVC. Next
     interactive session will spawn a fresh pod that re-mounts the volume.
   - **Thread deleted/archived** → call `release_thread_workspace(thread_id)`
     so the PVC also gets cleaned up.
3. Log every reap with thread_id, age, last condition reason.

Cadence: every 5 minutes is fine. Wire into the same scheduler that runs the
existing orchestrator background tasks.

### 4. (Optional) Pod GC policy

Worth checking whether setting `restartPolicy: OnFailure` and/or an owner
reference on these pods would let Kubernetes' built-in pod GC handle the
cleanup. Currently each thread pod looks like a bare `Pod` with no owner — that
is *why* it sits forever once it fails.

## Status

- [x] Diagnosis written up
- [x] Step 2: node-side investigation (no taint, scheduler not avoiding node4,
      container exited with code 255 reason `Unknown` — consistent with hard
      power-off)
- [x] Trigger identified: rack-level power-down on 2026-04-28 17:41 UTC while
      operator was installing a new server. No journal dive needed.
- [x] Force-deleted the ten zombie pods on 2026-05-01 (data already lost via
      emptyDir; purely cosmetic cleanup of the API objects).
- [x] Marked the ten orphaned `threads` rows in srw Postgres as `ended` with
      `ended_at = NOW()` on 2026-05-02. Chat history / audit rows preserved;
      Cockpit will no longer show these as live sessions.
- [x] Reconcile `pvc-ws-thread-*` code path — **resolved 2026-08-05 in favour of
      (a): thread workspaces are really persistent.** The reason no PVC existed
      was never dead code; it was the live gate
      `container_provisioner.py` `if self._pvc_enabled and owner.kind == "job"`,
      which forced sessions onto `emptyDir` even with `WORKSPACE_PVC_ENABLED=true`.
      A session now gets `pvc-ws-thread-<tid[:12]>` for its workspace pod and
      `pvc-agent-s-<tid[:12]>` for its agent pod, reclaimed only when the
      `threads` row is genuinely deleted — an `ended` thread is resumable and
      keeps its volumes. Shipped in `52c1ba80`, live on the dev cluster
      (claims verified `Bound`). See
      [`workspace_pvc_backed_migration.md`](../features/workspace_pvc_backed_migration.md).
      **Caveat:** durable storage does not by itself stop workspace content from
      being lost — see
      [`session_workspace_wiped_by_agent_clone_on_attach.md`](../issues/session_workspace_wiped_by_agent_clone_on_attach.md),
      which is still open.
- [ ] Implement thread-workspace reaper that catches both pod-side zombies
      (`Failed`/`Unknown`) and DB-side orphans (threads with `status` in
      `{created, active, idle}` whose pod has been gone for > N minutes).
- [ ] Decide on owner-reference / restartPolicy change so future power events
      don't leave bare-Pod tombstones in the first place.
