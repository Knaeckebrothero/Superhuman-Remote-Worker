# Orchestrator Failover — Behavior & Chaos Test

> Companion to `docs/features/orchestrator_ha_scaling.md` (Milestone M0). The
> orchestrator runs `replicas: 1`. This documents what happens when that pod
> dies and how to verify the failover hardening (preStop drain, startupProbe,
> PodDisruptionBudget) actually works.

## Expected behavior on pod death

| Event | Mechanism | User-visible effect |
|---|---|---|
| Pod evicted / OOM / image roll | Kubernetes reschedules; the new pod's `startupProbe` waits out migrations (up to ~150s) before liveness can act. | REST 5xx/timeout for the rolling-restart window (single-digit seconds with a warm node; longer on a cold image pull). |
| Graceful termination (drain, rollout) | `preStop` sleeps `preStopDrainSeconds` (15s) after the pod leaves Service endpoints, so in-flight REST drains. `terminationGracePeriodSeconds` (60s) bounds total shutdown. | In-flight requests finish; no truncation. |
| In-flight jobs | Agents heartbeat-timeout (3 min); `recover_orphaned_jobs` flips them to `paused`; the new orchestrator re-dispatches. | Jobs resume automatically; no loss. |
| Persistent sessions | The agent pod keeps running and keeps writing `thread_events`; cockpit reconnects and replays from `Last-Event-ID`. | Session reattaches after the bounce. |
| Open sudo prompts | Reply subject is persisted (`sudo_approval_requests.nats_reply_subject`); the decision is delivered on resolve. | Prompt survives; may need a cockpit refresh. |
| Node drain | `kubectl drain` is allowed because `orchestrator.pdb.minAvailable: 0`. | Pod reschedules onto another node. |

## Chaos test (run on dev, not prod)

Destructive. Run against the dev cluster with a warm agent pool. Measure
observable REST downtime and confirm clean recovery.

```bash
CTX=--context=dev          # your dev kube-context
NS=superhuman-remote-worker

# 0. Baseline: orchestrator healthy, one replica.
kubectl $CTX -n $NS get pods -l app.kubernetes.io/component=orchestrator -o wide

# 1. In one terminal, poll the API to measure downtime.
API=https://api.dev.example.com/api/health
while true; do
  printf '%s ' "$(date +%T)"
  curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" "$API" || echo "FAIL"
  sleep 1
done

# 2. Set up the three in-flight conditions:
#    (a) a job mid-dispatch, (b) an open sudo prompt, (c) a session mid-turn.
#    (Drive these from cockpit / the MCP as appropriate for your setup.)

# 3. In a second terminal, delete the orchestrator pod under load.
kubectl $CTX -n $NS delete pod -l app.kubernetes.io/component=orchestrator --wait=false

# 4. Watch recovery.
kubectl $CTX -n $NS get pods -l app.kubernetes.io/component=orchestrator -w
```

### Pass criteria

- Polling loop returns to `200` within the rolling-restart window (record the count of non-200 seconds).
- The new pod becomes Ready after its `startupProbe` passes (no crash-loop, even if migrations run).
- The mid-dispatch job ends up `paused` then re-dispatched — exactly once.
- The persistent session reattaches after refresh; no duplicated assistant turn.
- The sudo prompt resolves (after refresh if needed); the agent is not wedged.

### Drain test (verifies the PDB)

```bash
NODE=$(kubectl $CTX -n $NS get pod -l app.kubernetes.io/component=orchestrator -o jsonpath='{.items[0].spec.nodeName}')
kubectl $CTX cordon "$NODE"
kubectl $CTX drain "$NODE" --ignore-daemonsets --delete-emptydir-data --timeout=10m
# Expected: drain proceeds (PDB minAvailable:0 does not block it); orchestrator
# reschedules onto another node. Contrast: minAvailable:1 at replicas:1 would
# hang the drain — that is why M0 ships minAvailable:0.
kubectl $CTX uncordon "$NODE"
```

## Notes

- Sub-second failover requires `replicas: 2`, which is **not safe until M1**
  (leader election) — dispatch double-assign + IMAP double-poll. Do not raise
  `orchestrator.replicas` before M1. See `docs/features/orchestrator_ha_scaling.md`.
