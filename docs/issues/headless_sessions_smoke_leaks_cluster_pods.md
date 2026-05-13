# Headless sessions — Phase 5 wake-task smoke leaks real pods on the dev cluster

**Status:** Resolved 2026-05-13 with Option A (runbook callout). The Phase 5 §P5.6 section of `docs/tests/headless_sessions_smoke.md` now opens with a "Dev-cluster heads-up" block explaining the wake helper provisions a real workspace pod + PVC + agent pod when `persistent_provisioner` is configured, and the cleanup snippet at the end of the section includes a label-free `kubectl get | grep ${THREAD_ID:0:8}` discover step plus a piped `kubectl delete --wait=false`. Option B (`HEADLESS_DEV_SKIP_PROVISION` env knob) deliberately deferred — it would need a Fleet-bundle override that doesn't exist today and is best designed once when other smoke flows (idle workspace sweeper, scholar dispatch) need the same knob.

## Symptom (observed 2026-05-13 during Phase 5 smoke against the dev K8s cluster)

The Phase 5 smoke runbook §P5.6 ("Magic-link approve fires wake task on suspended workspace") describes the wake helper as **assertion = log line fired**, not assertion = full restore success. The runbook expects the wake helper to reach `workspace_suspension_service.restore_thread_workspace(...)`, log "magic-link wake: restoring suspended workspace for thread <uuid>", then fail at the actual restore step because the test thread has no S3 snapshot to restore from.

That's what happens on local docker-compose. It is **not** what happens on the dev K8s cluster.

On the cluster (commit sha-0e5994e, namespace `superhuman-remote-worker`, persistent provisioner wired):

```
2026-05-13 16:50:13 INFO main: magic-link wake: restoring suspended workspace for thread 6b9e97f3-...
2026-05-13 16:50:13 INFO services.container_provisioner: Thread workspace created: ws-thread-6b9e97f3-731
2026-05-13 16:50:21 INFO services.container_provisioner: Thread workspace ready: ws-thread-6b9e97f3-731 @ 10.42.2.234
2026-05-13 16:50:22 ERROR services.snapshot_service: Failed to download snapshot for job ...: 404 HeadObject Not Found
2026-05-13 16:50:22 WARNING services.workspace_suspension: Failed to download snapshot for thread ...
2026-05-13 16:50:22 INFO  services.workspace_suspension: Workspace restored from S3 for thread ... (ssh_host=10.42.2.234)
2026-05-13 16:50:22 INFO  services.persistent_provisioner: Agent pod created: persistent-6b9e97f3-731
```

The restore path **succeeded structurally** despite the snapshot download 404 — it created:
- a real workspace pod (`ws-thread-6b9e97f3-731`)
- a real PVC (`pvc-persistent-6b9e97f3-731`, 10Gi longhorn-ephemeral)
- a real agent pod (`persistent-6b9e97f3-731`)

None of these were cleaned up by the smoke runbook's cleanup snippet (which only reverts the DB row's `status`/`awaiting_user_since`/`extend_count`/`metadata` fields). Whoever runs the smoke has to remember to `kubectl delete` the orphans by hand.

## Root cause

The wake helper at `orchestrator/main.py:11678` (`_phase5_wake_if_suspended`) has two gates:

```python
ws_status = ws_ctx.get("status")
if ws_status == "suspended" and workspace_suspension_service.is_enabled:
    # ... restore_thread_workspace(...)
if persistent_provisioner is not None and not thread.get("agent_id"):
    # ... create_agent_pod(...)
```

The smoke runbook sets `metadata.workspace_container.status = 'suspended'` to satisfy gate #1 — that's necessary to exercise the "wake task fired" log assertion. Both gates' "would-fire" preconditions are satisfied on the dev cluster:

- `workspace_suspension_service.is_enabled` → True (S3 client object exists in cluster config)
- `persistent_provisioner is not None` → True
- `thread.agent_id IS NULL` → True for a freshly-minted test thread

On local docker-compose, gate #2's `persistent_provisioner` is typically not configured, so the agent pod creation is a no-op and the snapshot-restore failure is the actual end state. The runbook was written against that environment.

The snapshot 404 was specifically *not* treated as a hard failure — `workspace_suspension.restore_thread_workspace` logs a warning and continues with a clean workspace. That's correct prod behavior (a missing snapshot just means "boot fresh"), but it means the smoke can't rely on "snapshot 404 ⇒ no pods created."

## Impact

- **Smoke runs leak cluster resources** unless the operator hand-cleans. 3 objects per P5.6 run, plus whatever the agent pod boot does on its way to discovering it has no thread to attach to.
- **Operator pattern fragility**: anyone running the smoke without reading this issue doc first will assume the runbook's cleanup snippet is sufficient and walk away with orphans. Repeated runs compound.
- **Not a prod issue**: real magic-link wake-after-suspend flows on the cluster are the intended path — provisioning real pods is exactly what should happen. The bug is "smoke against a fully-wired cluster" not "behavior in prod."

## Fix

Three options, increasing in scope.

### A — Runbook note (cheapest, recommended first)

Add a §P5.6 callout: "When running this against a K8s dev cluster with `persistent_provisioner` configured, the wake helper will provision a real workspace pod, PVC, and agent pod. After the assertion logs, delete them:"

```bash
kubectl -n superhuman-remote-worker delete \
  pod/ws-thread-${THREAD_ID:0:8}-XXX \
  pod/persistent-${THREAD_ID:0:8} \
  pvc/pvc-persistent-${THREAD_ID:0:8} --wait=false
```

Operator burden, but zero code change. 5-minute fix.

### B — `HEADLESS_DEV_SKIP_PROVISION` env knob

Add an env var that short-circuits the provisioner call in `_phase5_wake_if_suspended` after logging:

```python
if ws_status == "suspended" and workspace_suspension_service.is_enabled:
    logger.info("magic-link wake: restoring suspended workspace for thread %s", thread_id)
    if os.environ.get("HEADLESS_DEV_SKIP_PROVISION") == "1":
        logger.info("magic-link wake: dev-skip enabled, not calling provisioner")
        return
    ok = await workspace_suspension_service.restore_thread_workspace(thread_id)
```

Operator sets it via deployment env override before running the smoke, unsets afterward. But: per `[[feedback_fleet_secrets]]` we don't manually patch K8s deployments. This option doesn't fit the dev-cluster workflow without further infra (a separate "smoke-mode" Fleet bundle, or a chart value).

### C — Mark wake helper smoke-aware via a header or query param

Have the smoke POST set `X-Smoke-Mode: 1` on `/magic/approve/{token}` and have the wake helper read that off a thread-local or request-context to skip provisioning. Adds API surface for testing — fragile, not recommended.

## Recommendation

**Ship A now** — runbook note. It's already-known information that just needs to land in the doc.

Defer B until we have a story for "dev cluster mode" more broadly. The same gap will bite other smoke flows that touch the provisioner (idle workspace sweeper, scholar dispatch, etc.) — those will all want the same knob, and we should design it once.

## Related code

- `orchestrator/main.py:11678` — `_phase5_wake_if_suspended`, the gating logic.
- `orchestrator/services/workspace_suspension.py` — `restore_thread_workspace` (succeeds-on-404-snapshot behavior).
- `orchestrator/services/persistent_provisioner.py` — `create_agent_pod` (fire-and-forget call from wake helper).
- `docs/tests/headless_sessions_smoke.md` §P5.6 — runbook section to amend with the cleanup callout.

## Resolution (2026-05-13)

Shipped Option A unchanged from the proposal. The cleanup snippet uses `kubectl get pods,pvc -o name | grep "${THREAD_ID:0:8}" | xargs ...` rather than naming the resources explicitly — the workspace-pod random suffix isn't predictable, but all three resources (`ws-thread-…`, `persistent-…`, `pvc-persistent-…`) share the first 8 chars of the thread UUID, so a single grep catches everything. The list-then-delete shape also handles the docker-compose case gracefully: nothing matches, the delete xarg becomes a no-op, no error.
