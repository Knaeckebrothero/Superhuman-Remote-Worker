# Lifecycle reconciler — manual test plan

## What this document covers

End-to-end validation of the unified instance lifecycle reconciler
(Phases 1 + 2 + 3 + 1e of the redesign in
[`docs/features/unified_instance_lifecycle.md`](../features/unified_instance_lifecycle.md)).
The reconciler manages three instance kinds — agents, workspace pods,
and VMs — and is responsible for image-drift drain, crash recovery, and
the version-upgrade Continue-as-New flow for busy workers.

The unit tests cover every branch of the reconciler tick, every manager
predicate, and every drain/snapshot path in isolation. What can't be
unit-tested is the **runtime behaviour against a live K8s cluster
under real conditions** — image rollouts, container crashes, and
mid-job version upgrades. That's what this plan is for.

This is a living document. Tick boxes off as you complete each test;
add notes for any unexpected behaviour. The orchestrator's
`docs/issues/agent_lifecycle_management.md` was the original problem
statement; if anything here regresses, that's the doc to update first.

## Feature recap (what you're testing)

The lifecycle reconciler runs every 60s inside the orchestrator. On
each tick, for each registered manager (`agent`, `workspace`, `vm`),
it:

1. Lists live instances.
2. For each instance failing `is_healthy()`, force-deletes it
   (**crash recovery**).
3. For each instance whose `version` ∉ `expected_versions()`, fires
   `signal_drain_pending(inst)` — for agents this writes
   `agents.intents.should_drain=true` so the in-pod heartbeat callback
   can react at the next safe boundary; for workspaces and VMs this
   is a no-op (they get drained when the bound work pauses).
4. If the drifted instance is also `is_idle()`, calls
   `drain(inst, grace_s=0)` — for stateful kinds this snapshots to S3
   first, then deletes the pod. On next dispatch, the suspension
   service rehydrates a fresh-version pod from S3.

Per-tick stats land in the orchestrator log:

```
Lifecycle tick kind=agent {'listed': 2, 'drift': 1, 'skipped_busy': 1}
Lifecycle tick kind=workspace {'listed': 9, 'drift': 1, 'drained': 1}
```

Counters present in every report: `listed`, `unhealthy`, `drift`,
`drained`, `skipped_busy`. Stats lines are only logged when at least
one non-`listed` counter is non-zero.

### Continue-as-New (worker version upgrade)

For busy workers, the reconciler can't just kill the pod — that would
abort an in-flight job. Instead the path is:

1. Reconciler writes `should_drain=true` to `agents.intents`.
2. Agent's heartbeat callback (`src/api/dual_app._handle_heartbeat_intents`)
   sets a process-local flag.
3. At the next phase boundary, `src/graph.py:handle_transition`
   detects the flag and freezes with `freeze_data.freeze_type =
   "version_upgrade"`, sets `should_stop = true`.
4. Orchestrator's `services/completion.determine_job_status` routes
   `version_upgrade` to job status `paused`.
5. Auto-assign dispatcher re-picks-up the same job context onto a
   fresh-version agent pod. Workspace state is preserved (workspace
   files live on remote SSH, not the agent pod).

## Pre-flight checks

Run these before every test session. All should be green.

### 1. Orchestrator running on the lifecycle code

```bash
kubectl get pod -n superhuman-remote-worker -l app.kubernetes.io/component=orchestrator \
  -o custom-columns='NAME:.metadata.name,IMAGE:.spec.containers[0].image'
```

Image SHA must contain the lifecycle module — anything from `8404d32`
or later is fine.

### 2. `agents.intents` column exists

```bash
kubectl exec -n superhuman-remote-worker deploy/srw-orchestrator -- python3 -c "
import asyncio, os, sys
sys.path.insert(0, '/app')
async def m():
    from database.postgres import PostgresDB
    os.environ['DATABASE_URL'] = f\"postgresql://{os.environ.get('POSTGRES_USER','srw')}:{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}:5432/{os.environ['POSTGRES_DB']}\"
    db = PostgresDB(); await db.connect()
    cols = await db.fetch(\"SELECT column_name FROM information_schema.columns WHERE table_name='agents' AND column_name='intents'\")
    print('intents column:', 'present' if cols else 'MISSING — migration 0003 not applied')
    await db.disconnect()
asyncio.run(m())
"
```

### 3. Reconciler ticking

```bash
kubectl logs -n superhuman-remote-worker deploy/srw-orchestrator --tail=300 \
  | grep "Lifecycle tick" | tail -5
```

Expect a line every ~60s. If silent, either the lifecycle module
didn't load (check for import errors at startup) or all kinds genuinely
have nothing actionable — try `kubectl logs ... | grep -i lifecycle`
without the tick filter to see startup messages.

### 4. Helm image tags

```bash
kubectl exec -n superhuman-remote-worker deploy/srw-orchestrator -- env \
  | grep -E "AGENT_IMAGE|PERSISTENT_AGENT_IMAGE|WORKSPACE_IMAGE|DEFAULT_VM_IMAGE"
```

Note the SHAs — these are what the reconciler uses as `expected_versions`.
For drift testing you'll need to change these.

---

## Tests still TODO

### Test A — Live container crash → unhealthy delete

**Goal**: prove the reconciler force-deletes a workspace pod that's
actually in `Failed`/`Unknown` phase. Closes the gap from
[`stuck_thread_workspace_pods.md`](../issues/stuck_thread_workspace_pods.md).

**Why probing isn't enough**: `is_healthy()` is unit-tested and the
runtime path was verified by exec'ing into the orchestrator and
calling the manager directly. What's *not* verified is that an
actual K8s pod going to `Failed` reliably triggers the delete, which
is sensitive to label-selector matching, pod-list pagination, and
race conditions between the reconciler tick and the kubelet's status
update.

**Setup options** (pick one):

| Option | How | Risk | Notes |
|---|---|---|---|
| A1: Self-crashing container | Build a one-shot test workspace image whose entrypoint exits non-zero after 30s | Low — fully isolated | Most controlled. Requires a test image build. |
| A2: Node-level signal | `kubectl debug node/<node>` → `crictl exec` into a workspace container, kill from outside the PID namespace | Medium — node access | Works on any pod, no rebuild |
| A3: KubeVirt VM with self-poweroff | Same as A1 but for VMs | Low | Only useful if `Test D` is in scope |

**Steps (option A1)**:

1. Build and push a test image:

   ```dockerfile
   FROM ghcr.io/knaeckebrothero/superhuman-remote-worker-workspace:<current-sha>
   ENTRYPOINT ["sh", "-c", "sleep 30 && exit 1"]
   ```

2. Create a one-off pod against the workspace label set:

   ```bash
   kubectl run -n superhuman-remote-worker test-crash-ws \
     --image=<your-test-image> \
     --restart=Never \
     --labels='app=srw-workspace,srw/component=workspace,srw.io/component=agent-workspace,srw/job-id=00000000-0000-0000-0000-deadbeef0001'
   ```

3. Wait ~45s for the container to exit. Confirm phase:

   ```bash
   kubectl get pod -n superhuman-remote-worker test-crash-ws \
     -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,REASON:.status.reason'
   ```

   Expect `PHASE=Failed`.

4. Tail orchestrator logs and wait for next reconciler tick:

   ```bash
   kubectl logs -f -n superhuman-remote-worker deploy/srw-orchestrator \
     | grep -E "kind=workspace|test-crash-ws"
   ```

5. **Pass criteria**:
   - Within 60s of the pod hitting `Failed`, log shows
     `Lifecycle tick kind=workspace {... 'unhealthy': 1 ...}`.
   - The pod is deleted within ~30s of that log line.
   - No errors in the orchestrator log.

6. **Cleanup**: `kubectl delete pod -n superhuman-remote-worker
   test-crash-ws --ignore-not-found` (the reconciler should already
   have done this).

**Failure modes to watch**:
- Pod stays in `Failed` and reconciler doesn't notice → check that the
  label selector `srw.io/component=agent-workspace` matches your
  test pod's labels.
- Reconciler logs `unhealthy=1` but pod isn't actually deleted → the
  manager's `delete_thread_workspace`/`delete_workspace` may be
  raising; check log for tracebacks.
- Tick fires but pod isn't in the listing → label mismatch (most
  likely cause), or k8s pod-list pagination issue (rare).

### Test B — Real workspace drift drain

**Goal**: prove that bumping `WORKSPACE_IMAGE` actually causes the
reconciler to drain stale workspace pods. The probe (Test 3 in the
ad-hoc verification on 2026-05-10) confirmed the manager identifies
candidates correctly with a synthetic env var; this test does the
real bump and watches the snapshot+delete+restore flow fire.

**Pre-conditions**:
- At least one workspace pod with the `srw/build-sha` label (i.e.
  created post-Phase-2a). Verify:

  ```bash
  kubectl get pods -n superhuman-remote-worker \
    -l srw.io/component=agent-workspace \
    -o custom-columns='NAME:.metadata.name,SHA:.metadata.labels.srw/build-sha'
  ```

  At least one pod must show a non-empty SHA.

- Ideally that pod is bound to a `paused` or `pending_review` job (so
  `is_idle()` returns True). Check via:

  ```bash
  kubectl exec -n superhuman-remote-worker deploy/srw-orchestrator -- python3 -c "
  import asyncio, os, sys; sys.path.insert(0, '/app')
  async def m():
      from database.postgres import PostgresDB
      os.environ['DATABASE_URL'] = f\"postgresql://{os.environ.get('POSTGRES_USER','srw')}:{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}:5432/{os.environ['POSTGRES_DB']}\"
      db = PostgresDB(); await db.connect()
      rows = await db.fetch(\"SELECT id, status, context->'workspace_container'->>'pod_name' AS pod FROM jobs WHERE status IN ('paused','pending_review')\")
      for r in rows: print(dict(r))
      await db.disconnect()
  asyncio.run(m())
  "
  ```

- A snapshot bucket configured (`SnapshotService.is_available == True`).
  If not, the snapshot returns `None` and the drain skips deletion to
  avoid data loss. Check the orchestrator startup log for snapshot
  availability.

**Steps**:

1. Note the current `WORKSPACE_IMAGE` env tag and pick a target stale
   pod from the candidate list.

2. Edit `helm/values.yaml`:

   ```yaml
   image:
     workspace:
       repository: ghcr.io/knaeckebrothero/superhuman-remote-worker-workspace
       tag: sha-<NEW-SHA>     # different from current
   ```

   Commit and push. Wait for Fleet to sync (typically 1–3 min).

3. Confirm new env on the orchestrator:

   ```bash
   kubectl exec -n superhuman-remote-worker deploy/srw-orchestrator -- env \
     | grep WORKSPACE_IMAGE
   ```

4. Tail logs:

   ```bash
   kubectl logs -f -n superhuman-remote-worker deploy/srw-orchestrator \
     | grep -E "kind=workspace|<target-pod-name>"
   ```

5. **Pass criteria** (within 2 reconciler ticks ≈ 120s):
   - `Lifecycle tick kind=workspace {... 'drift': N, 'drained': M ...}`
     with M ≥ 1.
   - Target pod is deleted (`kubectl get pod ...` returns NotFound).
   - S3 snapshot was captured before deletion. Check snapshot service
     log: `Captured snapshot for job=<job-id>` (or whatever the actual
     log message is — grep `capture_vm_snapshot`).
   - The bound job's DB row shows
     `context.workspace_container.status='suspended'`.

6. Resume the bound job through the cockpit (or PATCH
   `/api/jobs/{id}` with `status=processing`). Verify a fresh
   workspace pod is created on the new image:

   ```bash
   kubectl get pod -n superhuman-remote-worker -l srw/job-id=<job-id> \
     -o custom-columns='NAME:.metadata.name,IMAGE:.spec.containers[0].image,SHA:.metadata.labels.srw/build-sha'
   ```

   Pod's image SHA should match the new `WORKSPACE_IMAGE`. The
   workspace contents should be hydrated from the S3 snapshot
   (verify by exec'ing in and checking `/home/agent-host/workspace`
   has expected files).

**Failure modes**:
- Snapshot fails → manager's `drain` calls `delete_workspace` anyway
  (current behaviour: the reconciler tick already called `snapshot()`
  and got `None`, then proceeded to `drain()`). Data loss possible if
  snapshot misconfigured. Mitigation: confirm `SnapshotService.is_available`
  before running this test.
- New pod doesn't restore from S3 → `restore_workspace` log will say
  `no snapshot found`. Check S3 bucket directly.
- Drain logged but pod stays → check that the pod's `srw/job-id` /
  `srw/thread-id` label resolves to a real DB row; if the join fails,
  `is_idle()` returns False conservatively.

### Test C — Real Continue-as-New on a busy worker

**Goal**: prove the full version-upgrade flow end-to-end on a real
job. This is the test that exercises every Phase 1c + 1d + 1e path
in production.

**Pre-conditions**:
- An agent pod running new code (sha containing `04a2e5d` or later —
  any agent post Phase 1c heartbeat callback). Verify by checking the
  current ready agent's image and that its in-pod `_drain_intent_received`
  global exists.
- A multi-phase test job that will hit at least one phase boundary
  during the test window (a "say hello and write a one-paragraph
  summary" job typically hits 2–3 strategic↔tactical transitions).

**Steps**:

1. Submit the test job through the cockpit. Note the assigned agent
   id and the workspace pod name.

   ```bash
   kubectl exec -n superhuman-remote-worker deploy/srw-orchestrator -- python3 -c "
   import asyncio, os, sys; sys.path.insert(0, '/app')
   async def m():
       from database.postgres import PostgresDB
       os.environ['DATABASE_URL'] = f\"postgresql://{os.environ.get('POSTGRES_USER','srw')}:{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}:5432/{os.environ['POSTGRES_DB']}\"
       db = PostgresDB(); await db.connect()
       jobs = await db.fetch(\"SELECT id, status, assigned_agent_id, context->>'workspace_container' AS ws FROM jobs WHERE status='processing' ORDER BY created_at DESC LIMIT 3\")
       for j in jobs: print(dict(j))
       await db.disconnect()
   asyncio.run(m())
   "
   ```

2. While the job is running (status=processing), bump `image.agent.tag`
   AND `image.persistentAgent.tag` (or `PERSISTENT_AGENT_IMAGE` —
   whichever Helm key drives them) to a new SHA. Commit and push.
   Wait for Fleet to sync the orchestrator.

3. Within ~60s of the orchestrator restart, the lifecycle tick should
   identify the busy worker as drift:

   ```
   Lifecycle tick kind=agent {... 'drift': 1, 'skipped_busy': 1}
   ```

4. Confirm the intent is now in DB:

   ```sql
   SELECT hostname, status, intents::text FROM agents WHERE status='working';
   ```

   Expect: `intents='{"drain_reason": "stale_image", "should_drain": true}'`.

5. Within ~5s the agent's heartbeat callback should set the in-pod
   flag. Verify by tailing the agent's own logs:

   ```bash
   kubectl logs -f -n superhuman-remote-worker <agent-pod-name> \
     | grep -E "Drain intent received|version_upgrade|phase boundary"
   ```

   Expected log: `Drain intent received (reason=stale_image) — will
   freeze at next phase boundary`.

6. At the next phase boundary in the agent's graph, `handle_transition`
   should freeze:

   ```
   [<job-id>] Drain intent at phase boundary — freezing for version_upgrade re-dispatch
   ```

7. Verify the workspace got the marker file:

   ```bash
   kubectl exec -n superhuman-remote-worker <workspace-pod-name> -- \
     cat /home/agent-host/workspace/output/job_frozen.json
   ```

   JSON should have `"freeze_type": "version_upgrade"`.

8. The orchestrator should pause the job:

   ```sql
   SELECT id, status, freeze_data::text FROM jobs WHERE id='<job-id>';
   ```

   Expect: `status='paused'`, `freeze_data->>'freeze_type'='version_upgrade'`.

9. The agent should self-clean (heartbeat to draining → reap_pods
   reaps it). Within a few minutes:

   ```bash
   kubectl get pod -n superhuman-remote-worker <agent-pod-name>
   ```

   Should be `NotFound`.

10. The auto-assign dispatcher should re-pick-up the paused job and
    route it to a fresh-version agent. New agent pod should appear,
    its image SHA should match the new `AGENT_IMAGE`. Job status
    flips back to `processing`. Job continues from where it left off.

11. **Pass criteria**: all 10 steps above complete without manual
    intervention. The job ultimately reaches `completed` status.

**Failure modes**:
- Job freezes at phase boundary but never gets re-dispatched →
  `determine_job_status` may not be returning `paused`. Check
  orchestrator log for `freeze_type=version_upgrade` handling.
- Agent doesn't react to intent → confirm agent's image actually
  contains the heartbeat callback (`grep _handle_heartbeat_intents
  /app/src/api/dual_app.py` inside the pod).
- Re-dispatched job doesn't pick up where it left off → workspace
  state is preserved on the workspace pod (separate from the agent
  pod), so the re-dispatched agent should restore from checkpoint.
  If state is lost, check `/home/agent-host/workspace/checkpoints/`
  on the workspace pod.
- Agent never goes draining/away → reap_pods may not be reaping
  drained agents; check `agent_pool_reconciler` logs.

### Test D — VM crash recovery

**Goal**: same as Test A but for VMs.

**Pre-conditions**:
- At least one VM workload running — see preflight check 5 below.
- Access to the VM backend (NATS / HTTP controller / KubeVirt).

**Pre-flight check 5 — VMs present**:

```bash
kubectl exec -n superhuman-remote-worker deploy/srw-orchestrator -- python3 -c "
import asyncio, os, sys; sys.path.insert(0, '/app')
async def m():
    from database.postgres import PostgresDB
    os.environ['DATABASE_URL'] = f\"postgresql://{os.environ.get('POSTGRES_USER','srw')}:{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}:5432/{os.environ['POSTGRES_DB']}\"
    db = PostgresDB(); await db.connect()
    n = await db.fetchval(\"SELECT count(*) FROM jobs WHERE context->'vm' IS NOT NULL AND context->'vm' <> '{}'::jsonb\")
    print(f'job-bound VMs: {n}')
    n = await db.fetchval(\"SELECT count(*) FROM threads WHERE metadata->'vm' IS NOT NULL AND metadata->'vm' <> '{}'::jsonb\")
    print(f'thread-bound VMs: {n}')
    await db.disconnect()
asyncio.run(m())
"
```

If both are 0, this test is N/A on the current cluster — skip.

**Steps** (assuming VMs are running):

1. Pick a target VM by job_id. Set `vm.status='failed'` in
   `jobs.context.vm` JSONB:

   ```bash
   kubectl exec -n superhuman-remote-worker deploy/srw-orchestrator -- python3 -c "
   import asyncio, os, sys; sys.path.insert(0, '/app')
   async def m():
       from database.postgres import PostgresDB
       os.environ['DATABASE_URL'] = f\"postgresql://{os.environ.get('POSTGRES_USER','srw')}:{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}:5432/{os.environ['POSTGRES_DB']}\"
       db = PostgresDB(); await db.connect()
       await db.merge_vm_context('<JOB-ID>', {'status': 'failed'})
       await db.disconnect()
   asyncio.run(m())
   "
   ```

   Or kill the VM via the backend directly (NATS message, KubeVirt
   patch) — whichever is closer to a real failure.

2. Tail orchestrator logs for the next tick:

   ```bash
   kubectl logs -f -n superhuman-remote-worker deploy/srw-orchestrator \
     | grep "kind=vm"
   ```

3. **Pass criteria**:
   - `Lifecycle tick kind=vm {... 'unhealthy': 1 ...}` within 60s.
   - `vm_provisioner.delete_vm(<JOB-ID>)` is called (check log).
   - The VM's K8s/NATS/HTTP representation is gone.

4. **Cleanup**: re-create the VM via normal provisioning if you want
   the bound job to continue.

---

## Already verified (2026-05-10)

For reference / regression baseline:

- ✅ Reconciler liveness (60s ticks, sane stats)
- ✅ Phase 1e fix — busy stale agent has `should_drain` intent in DB
- ✅ Heartbeat response carries intents (orchestrator side)
- ✅ Workspace manager enumerates pods + joins DB context correctly
- ✅ `is_healthy` predicates correct for all pod phases
- ✅ Drift detection identifies the right candidates with a simulated
  `WORKSPACE_IMAGE` bump (1 candidate had `srw/build-sha` label;
  identified correctly as drift+idle)
- ⏭ VM tests skipped — cluster has no VM workloads

The orchestrator and agent pods that were running on 2026-05-10 are:

| Pod | Image SHA | Notes |
|---|---|---|
| `srw-orchestrator-...` | `e5e83ff` | Has the lifecycle module + Phase 1e fix |
| `srw-agent-j-437d4289` | `04a2e5d` | Fresh agent, has heartbeat callback |
| `srw-agent-j-9ea244d6` | `f50eade` | Working pod from before Phase 1c — old code, can't react to intents. Will be cleaned up when its current job finishes. |

The agent on `f50eade` is a known bootstrap-problem leftover; do
**not** try to test Continue-as-New against it (its code doesn't have
the heartbeat callback — Test C will fail no matter what).

## References

- Feature design: [`docs/features/unified_instance_lifecycle.md`](../features/unified_instance_lifecycle.md)
- Original problem statement: [`docs/issues/agent_lifecycle_management.md`](../issues/agent_lifecycle_management.md)
- Workspace crash gap: [`docs/issues/stuck_thread_workspace_pods.md`](../issues/stuck_thread_workspace_pods.md)
- Unit tests: `tests/test_lifecycle_*.py`, `tests/test_drain_intent.py`,
  `tests/test_agent_heartbeat.py`
- Karpenter drift-detection pattern (the inspiration): https://karpenter.sh/docs/concepts/disruption/
- Temporal Worker Versioning (the Continue-as-New analog): https://docs.temporal.io/production-deployment/worker-deployments/worker-versioning
