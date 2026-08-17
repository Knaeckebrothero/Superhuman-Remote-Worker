# Manual validation — PVC single-replica node-loss fallback (Phase 3b)

**Type:** manual / infra validation (NOT a pytest — needs a real multi-node
Longhorn cluster and an ungraceful node loss, which single-node k3d cannot
produce). **Status:** deferred. The discard logic is unit-covered in
`tests/test_container_provisioner.py`; this runbook validates the *trigger* and
the end-to-end recovery on real hardware.

**What it validates:** when a PVC-backed job's workspace pod dies with the
node holding its **single** Longhorn replica, the orchestrator gives up
reattaching after `WORKSPACE_REATTACH_READY_TIMEOUT`, discards the wedged PVC,
provisions a fresh empty one, and the agent recovers by cloning from Gitea +
resuming the Postgres checkpoint (unpushed working-tree files lost). And the
inverse: a *brief* outage (shorter than the timeout) reattaches with the data
**preserved**, never discarding.

**Design + code:** `knowledge-base/knowledge/features/workspace_pvc_branch_a_implementation.md`
(§Phase 3b). Fallback lives in `orchestrator/services/container_provisioner.py`
`create_workspace` (`_create_pvc` status / `_pod_volume_attach_failing` /
`_delete_pvc_and_wait` / the `fresh=True` recursion).

---

## Why this can't run on k3d

`local-path` (k3d) is a hostPath bind-mount on a single node: reattach is
instant and there is no `VolumeAttachment` to go stale, no second node to
reschedule onto. The wedge only forms on **networked RWO** storage
(`longhorn-ephemeral`) across **≥2 nodes**. Hence: homelab only.

## Homelab facts (as of 2026-06-30)

| Thing | Value |
|---|---|
| kube context | `main` (4-node k3s: `node1`/`node2`/`node3` = control-plane+etcd, `node4` = worker `<none>`) |
| SRW namespace (develop/experimental) | `superhuman-remote-worker` (Fleet ← `values-experimental.yaml`) |
| **Do NOT touch** | ns `srw-prod-private` (separate real prod, `v0.0.23`) |
| Workspace storage class | `longhorn-ephemeral` — **`numberOfReplicas: 1`**, Delete reclaim |
| Longhorn default replica count | 2 (platform data on `longhorn`/`longhorn-static`, ≥2 replicas) |
| Node SSH from dev env | **none** — use the in-cluster `nsenter` mechanism below |
| `vm` kube context | a *different* single-node cluster (`node8`) — irrelevant here |

## Prerequisites

1. **PVC mode ON** in `superhuman-remote-worker` (flipped in
   `values-experimental.yaml`, `workspace.pvcEnabled: true`). Confirm live:
   ```bash
   kubectl --context=main -n superhuman-remote-worker get configmap srw-config \
     -o jsonpath='{.data.WORKSPACE_PVC_ENABLED}{"  "}{.data.WORKSPACE_REATTACH_READY_TIMEOUT}{"  "}{.data.WORKSPACE_REATTACH_FRESH_FALLBACK}{"\n"}'
   # expect: true  180  true
   ```
   And that the running orchestrator image includes the 3b code (≥ `sha-868bfd3`).
2. A **PVC-backed worker job** running long enough to checkpoint (a loop/scholar
   job). Confirm its workspace pod exists and note which node it's on:
   ```bash
   kubectl --context=main -n superhuman-remote-worker get pod -l srw.io/component=agent-workspace -o wide
   ```
3. **Pin the workspace pod to `node4`** so only the worker is disrupted (keeps
   etcd quorum on node1-3 untouched). Easiest: soft node-affinity on the job's
   workspace pod, or cordon node1-3 while the workspace pod is (re)scheduled,
   then uncordon. Verify the pod is on `node4`.

## DATA-SAFETY GATE (run before every node disruption)

A node loss must delete **no other deployment's data**. With replica-count 2 and
4 nodes, every platform volume keeps ≥1 replica through a single-node loss — but
confirm nothing single-replica lives on the target node:

```bash
# Must list ONLY your test job's workspace volume (longhorn-ephemeral = 1 replica).
kubectl --context=main -n longhorn-system get volumes.longhorn.io \
  -o jsonpath='{range .items[?(@.spec.numberOfReplicas<2)]}{.metadata.name}{" node="}{.status.currentNodeID}{"\n"}{end}'
```

If any *other* single-replica volume sits on `node4`, **stop** — move it or pick
a different node first.

## Node-disruption mechanism (kubectl-only, self-recovering)

No SSH to the nodes, so get host access via a privileged `nsenter` pod and
schedule the outage as a **transient systemd unit owned by PID1** — it survives
`k3s-agent`/containerd stopping, so the node auto-restarts even though the pod
that scheduled it is gone. `node4` runs `k3s-agent` (workers); `node1-3` run
`k3s` (servers).

> ⚠️ If the auto-restart timer ever fails, `node4` stays down with no kubectl
> recovery path from here — have **out-of-band (console/IPMI/physical) recovery
> for node4 ready** before running this. `systemd-run` presence was verified on
> node4 (2026-06-30). The machine is only ever `k3s-agent`-stopped, never wiped,
> so Longhorn replicas on its disk survive the outage.

```bash
# Schedule an outage of $DOWN seconds on node4, auto-restarting via host systemd.
DOWN=480   # 8 min → exceeds the ~5 min taint-eviction AND the 180s reattach wait
cat <<EOF | kubectl --context=main apply -f -
apiVersion: v1
kind: Pod
metadata: {name: node4-outage, namespace: default, labels: {srw.io/3b-probe: "true"}}
spec:
  nodeName: node4
  hostPID: true
  restartPolicy: Never
  tolerations: [{operator: Exists}]
  containers:
  - name: t
    image: busybox
    securityContext: {privileged: true}
    command: ["nsenter","--target","1","--mount","--uts","--ipc","--net","--pid","--","sh","-c",
      "systemd-run --on-active=15s --unit=srw-3b-outage --description='SRW 3b node outage' /bin/sh -c 'logger -t SRW3B outage-start; systemctl stop k3s-agent; sleep ${DOWN}; systemctl start k3s-agent; logger -t SRW3B outage-end'; echo scheduled"]
EOF
# The pod schedules the timer and exits; ~15s later node4 goes NotReady for $DOWN s.
```

Physical/IPMI power-cycle of node4 is an equivalent alternative (and a stronger
test of *permanent* loss if you also wipe the replica — not needed for v1).

## Scenario A — sustained loss → discard + fresh volume (the headline)

1. Plant an **un-pushed sentinel** in the running job's workspace:
   ```bash
   POD=$(kubectl --context=main -n superhuman-remote-worker get pod -l srw.io/component=agent-workspace -o name | head -1)
   kubectl --context=main -n superhuman-remote-worker exec $POD -- sh -c 'echo SENTINEL-$(date -u +%FT%TZ) > /home/agent-host/workspace/E2E_SENTINEL.txt'
   ```
2. Run the outage with `DOWN=480` (8 min) so node4 is still down when the
   recovery recreate's reattach hits its 180s timeout.
3. **Watch** (orchestrator logs on the leader replica):
   ```bash
   kubectl --context=main -n superhuman-remote-worker logs -l app.kubernetes.io/component=orchestrator -f \
     | grep -iE "reattach wedged|capacity|fresh|recovery|workspace_unavailable|$JOB_ID"
   ```
4. **Assert:**
   - Log: `Workspace … reattach wedged — volume unattachable … Discarding the PVC and recovering onto a fresh volume`.
   - Job context `workspace_container.workspace_reset == true`.
   - Job returns to `processing` (clone from Gitea + resume the checkpoint), **not** `failed`, and the recovery cap is not exhausted.
   - The sentinel is **GONE** (fresh volume) — but the checkpoint-restored work + last-pushed git state are present.
   ```bash
   kubectl --context=main -n srw-... exec -i srw-postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
     -c "SELECT status, context->'workspace_container'->>'workspace_reset' FROM jobs WHERE id='$JOB_ID';"
   ```

## Scenario B — brief reboot → reattach, data preserved (the guard)

1. Plant the same sentinel.
2. Run the outage with a **short** `DOWN` (e.g. `DOWN=90`) so node4 returns
   before the recovery recreate's 180s reattach window elapses.
3. **Assert:**
   - **No** `reattach wedged` / discard log; **no** `workspace_reset` flag.
   - The job resumes and the **sentinel SURVIVES** (the volume reattached; its
     replica disk came back with the node).

> Timing is what we're measuring — the exact `DOWN` thresholds that flip between
> reattach and discard depend on the agent reconnect loop (~2.7 min) + the
> recreate latency + `WORKSPACE_REATTACH_READY_TIMEOUT` (180 s). Record the
> observed boundary; it may motivate tuning the timeout.

## Cleanup

```bash
kubectl --context=main -n default delete pod -l srw.io/3b-probe=true --ignore-not-found
# Confirm node4 recovered and nothing lingers on it:
#   nsenter probe: systemctl is-active k3s-agent  → active
#                  systemctl reset-failed srw-3b-outage 2>/dev/null
kubectl --context=main get nodes   # node4 Ready
# Delete the throwaway test job + its workspace PVC if not auto-GC'd.
```

## Rollback (disable PVC mode)

`workspace.pvcEnabled: false` in `values-experimental.yaml` (or
`WORKSPACE_REATTACH_FRESH_FALLBACK: false` to keep PVCs but disable *only* the
data-destructive discard). New pods revert to emptyDir; existing PVC pods finish
and GC normally — mixed fleet is safe.
