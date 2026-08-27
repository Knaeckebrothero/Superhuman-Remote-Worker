# Manual validation — PVC single-replica node-loss fallback (Phase 3b)

**Type:** manual / infra validation (NOT a pytest — the destructive branch
needs a real multi-node Longhorn cluster and an ungraceful node loss, which
single-node k3d cannot produce). **Status:** fail-closed containment only as of
2026-08-27. The historical discard/recreate design lacks a durable recovery
generation and must not be armed. This runbook retains the real-hardware
scenario for the future reviewed repair; today's executable gate is Scenario 0.

**What it validates now:** a reattach timeout fails closed while
`WORKSPACE_REATTACH_FRESH_FALLBACK=false`: the exact PVC is retained, no
recursive creation generation starts, and no Service/PVC is deleted. The future
destructive gate must additionally prove that a *brief* outage reattaches with
data preserved and that a sustained loss crosses an explicit durable
`fresh_recovery` generation before exact-UID PVC deletion and snapshot/Git
restore.

**Design + code:** `knowledge-base/knowledge/features/workspace_pvc_branch_a_implementation.md`
(§Phase 3b, including the 2026-08-27 correction). The open authority contract
is `knowledge-base/knowledge/issues/workspace_reattach_fresh_fallback_lacks_durable_recovery_authority.md`.
Do not use the historical `_delete_pvc_and_wait` / `fresh=True` recursion as an
acceptance path.

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

1. **PVC mode ON** in `superhuman-remote-worker`, but destructive fresh fallback
   **OFF**. Confirm live:
   ```bash
   kubectl --context=main -n superhuman-remote-worker get configmap srw-config \
     -o jsonpath='{.data.WORKSPACE_PVC_ENABLED}{"  "}{.data.WORKSPACE_REATTACH_READY_TIMEOUT}{"  "}{.data.WORKSPACE_REATTACH_FRESH_FALLBACK}{"\n"}'
   # current safe expectation: true  180  false
   ```
   Stop if the final value is `true`. A future destructive run requires a
   reviewed artifact containing the durable `fresh_recovery` protocol; an old
   3b image is not sufficient.
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

## Scenario 0 — current fail-closed timeout (the only authorized run)

1. Record the workspace Pod, PVC, Service and their exact UIDs. Plant an
   unpushed sentinel.
2. Exercise a bounded attach-failure/timeout using a disposable owner. Do not
   change the fallback flag from false.
3. Assert the operation reports retryable/unavailable rather than success; no
   recursive workspace creation begins; the PVC and Service retain their exact
   UIDs; the sentinel remains; and no `workspace_reset` success projection is
   written.
4. Restore the substrate and prove ordinary reattach resumes on the same PVC.
5. Remove only the disposable fixture through supported lifecycle operations.

Single-node k3d may cover the configuration/default and zero-mutation contract,
but cannot manufacture the real Longhorn attach-failure trigger.

## Scenario A — future sustained loss → durable fresh recovery (BLOCKED)

**Do not execute this section until the `fresh_recovery` issue is implemented,
reviewed, deployed, and explicitly authorized.** The historical environment
flag alone is not authorization.

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
4. **Assert the future contract (not the historical log/JSON marker):**
   - one durable `fresh_recovery` generation binds the old Pod/PVC UID,
     process-zero proof, verified snapshot, and attach-failure evidence;
   - exact old PVC deletion reaches observed 404 before the next creation
     reservation starts;
   - the replacement publishes and settles under that same recovery generation;
   - the job returns to `processing`, with the documented restored/lost state
     accurately surfaced and no overlapping cleanup/create claimant;
   - a crash/lost response resumes the generation without a second deletion or
     second runtime.
   The eventual runbook must query the authoritative recovery ledger added by
   that repair. Do not accept the legacy `workspace_reset` JSON marker as
   proof.

## Scenario B — future brief reboot → reattach, data preserved (the guard)

This guard belongs to the future authorized Scenario A run. Ordinary reattach
may still be tested independently while fallback remains false.

1. Plant the same sentinel.
2. Run the outage with a **short** `DOWN` (e.g. `DOWN=90`) so node4 returns
   before the recovery recreate's 180s reattach window elapses.
3. **Assert:**
   - **No** `reattach wedged` / discard log; **no** `workspace_reset` flag.
   - The job resumes and the **sentinel SURVIVES** (the volume reattached; its
     replica disk came back with the node).

> Timing is observational only; it never grants deletion authority. The exact
> `DOWN` thresholds depend on reconnect, recreate and
> `WORKSPACE_REATTACH_READY_TIMEOUT`, but crossing a duration must still leave
> the PVC intact unless the separate durable recovery contract is active.

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

Keep `WORKSPACE_REATTACH_FRESH_FALLBACK=false` /
`workspace.freshFallback=false`. This retains PVC-backed ordinary reattach while
disabling the data-destructive reset. `workspace.pvcEnabled: false` is a wider
storage-mode rollback and is not required merely to contain this issue. Do not
claim a mixed fleet safe for destructive fallback until every lifecycle owner
understands the future recovery generation.
