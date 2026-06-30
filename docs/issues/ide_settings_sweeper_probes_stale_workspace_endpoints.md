# IDE-settings sweeper serially SSH-probes stale, never-evicted workspace endpoints

**Status:** Filed + diagnosed (2026-06-27, during the loop-job `19707fa1` OOM investigation; promoted out of a sub-note in `audit_metadata_config_duplication_ooms_orchestrator.md`). **Not yet fixed.** The IDE-settings background sweeper rebuilds its worklist from a query that selects workspace records purely on a stale JSONB `status`, with no parent-job/thread-status or pod-liveness filter, and nothing clears that status when a session ends or a pod dies — so dead records accumulate forever and the sweeper serially SSH-dials each one every cycle ("No route to host", ~3 s apiece), on every replica.

**Found:** 2026-06-27. Surfaced on the **main cluster** (ns `superhuman-remote-worker`) while investigating job `19707fa1`'s orchestrator OOM — the logs were full of serial `10.42.x.x:30022` "No route to host" probes against long-dead workspace endpoints. Independent of (and not a cause of) that OOM.

**Severity:** **Low–Medium.** Not a crash — wasted SSH connections + sweeper wall-time. But it is **unbounded and ongoing**: one dead record leaks per ended/crashed session and is never evicted, the per-cycle cost grows linearly with the leak, and because the sweeper is not leader-gated the whole stale set is re-probed on **every** orchestrator replica every cycle. Left alone, a long-lived cluster spends an ever-growing slice of each 600 s cycle dialing addresses that will never answer.

**Component:**
- sweeper loop — `orchestrator/main.py:826-911` (`code_server_settings_sweeper`), interval `IDE_SETTINGS_SYNC_INTERVAL_S` default **600 s** (`main.py:858`), registered `main.py:5680-5682` — **not** `run_when_leader`-gated (contrast its sibling `ide_session_ttl_sweeper`, `main.py:5676-5678`)
- serial probe — `orchestrator/services/ide_settings.py:501-547` (`reconcile_ide_settings`): `for ws in workspaces:` (`:515`) → `await pull_fn(host, port)` per endpoint (`:524`), no `gather`; plus `reconcile_extensions` (`:587-630`) and the S3 capture loop (`main.py:889-901`) = **2–3 SSH dials per endpoint per cycle**
- timeouts — outer `asyncio.wait_for(..., timeout=20)` (`ide_settings.py:342`) + inner SSH `ConnectTimeout=10` (`orchestrator/services/ssh_helpers.py:49`); the observed ~3 s/endpoint is the kernel returning `EHOSTUNREACH` faster than the cap (a black-holed IP would block the full 10 s)
- worklist query (**root cause**) — `orchestrator/database/postgres.py:6529-6576` (`list_active_ide_workspaces`), predicate `:6544-6546` (jobs) / `:6562-6564` (threads)
- stale endpoint resolution — `resolve_ssh_target` (`ide_settings.py:225-252`), dials the raw stored `workspace_container.pod_ip`
- missing eviction — teardown `_release_thread_resources` (`main.py:3647-3673`) never clears the context; the only flip-to-non-ready is the job-only recovery path `main.py:10221-10230`

**Related:** `audit_metadata_config_duplication_ooms_orchestrator.md` (origin — this was its "Secondary finding") · `workspace_reattach_ephemeral_ip_reconnect_churn.md` + `workspace_pvc_branch_a_implementation.md` (same **stale-pod-IP** family; the stable headless Service `workspace_manager.py:477-481` fixes recovery addressing but this path doesn't use it) · `agent_workspace_pod_resource_headroom.md` (the OOM that kills the pods whose records then leak)

---

## Symptom

On a cluster that has run sessions for a while, the orchestrator logs show the IDE-settings sweeper serially SSH-dialing dozens of dead workspace endpoints — `10.42.x.x:30022`, "No route to host", ~3 s per dial — every cycle, indefinitely. The addresses belong to workspace pods that were torn down (session ended) or recreated (new pod → new IP) long ago. Nothing ever removes them from the sweeper's worklist, so the cost is paid again every `IDE_SETTINGS_SYNC_INTERVAL_S` (600 s), and — because the sweeper isn't leader-gated — once per replica.

## Root cause — the worklist selects on a stale status that nothing ever clears

There is no `ide_settings` / `ide_session` table; IDE and workspace state is JSONB on existing rows (`jobs.context.{ide_session,workspace_container,vm}`, `threads.metadata.{...}`). The sweeper's worklist comes from `list_active_ide_workspaces` (`postgres.py:6529-6576`), whose predicate (`:6544-6546` for jobs, `:6562-6564` for threads) is purely:

```sql
WHERE context->'ide_session'->>'status'         IN ('active','idle')
   OR context->'workspace_container'->>'status'  = 'ready'
   OR context->'vm'->>'status'                   = 'ready'
```

There is **no parent job/thread status filter and no pod-liveness check**. An `ended` thread whose `metadata.workspace_container.status` is still `'ready'` matches just as readily as a live one.

And nothing clears that status:
- **Create** writes `{"status":"ready","pod_ip":...,"port":30022}` (`container_provisioner.py:338`).
- **Teardown** `_release_thread_resources` (`main.py:3647-3673`) deletes the workspace + agent pods but **never resets the context** — confirmed by the comment at `main.py:10213-10216` ("delete_workspace's 404/'already deleted' branch does NOT set the status").
- The **only** code that flips a container to non-ready is the **job-only** dispatch-recovery path (`main.py:10221-10230`, `{"status":"deleted","pod_ip":None,...}`).
- The IDE TTL sweeper `ide_session_ttl_sweeper` / `check_ttl_all` (`main.py:762-784`, `ide_session.py:310-385`) expires by max-lifetime / idle on the **`ide_session` branch only** — it never inspects `workspace_container.status` or whether the pod is alive.

Net: ended sessions and dead-pod records keep `status='ready'` + a stale `pod_ip` **indefinitely**, and `list_active_ide_workspaces` hands them to the serial prober every cycle.

## Same stale-pod-IP family as workspace-recovery

`resolve_ssh_target` (`ide_settings.py:225-252`) builds the SSH target from the stored context, preferring `workspace_container.pod_ip` (`:236-242`) — a **raw pod IP** (`10.42.x.x` = the k3d pod CIDR). When a pod is recreated the IP moves, so even a record for a still-wanted session goes stale. This is the same ephemeral-pod-IP churn fixed for workspace-recovery via a **stable headless Service** (`workspace_manager.py:477-481`) — but this path dials the raw stored IP, not the Service DNS, so a record both **goes stale** and is **never auto-corrected**.

## Fix options (ranked; not implemented)

1. **True eviction at the source (recommended).** Add a parent-status filter (and optionally `pod_ip IS NOT NULL` / an age bound) to `list_active_ide_workspaces` (`postgres.py:6544`) so ended/terminal parents drop out of the worklist. The sibling query `list_threads_needing_workspace` (`postgres.py:2810-2835`, used by `reconcile_session_workspaces` `session_provisioner.py:74-96`) already filters `status='active'` and excludes `'ready'` — mirror it. This is the real fix: it makes the leak impossible rather than merely cheaper to probe.
2. **Clear the context on teardown.** Have `_release_thread_resources` (`main.py:3647`) reset `workspace_container` / `ide_session` the way the job-recovery path already does at `main.py:10221-10230` (via `merge_thread_workspace_context`, `container_provisioner.py:1575`). Stops the leak at write-time; complements (1).
3. **Defense-in-depth on the probe.** Parallelize `reconcile_ide_settings` (`ide_settings.py:515`) with `asyncio.gather` + a short connect timeout so a stale set can't dominate the cycle, and wrap the sweeper in `run_when_leader` (like `ide_session_ttl_sweeper`, `main.py:5676-5678`) so it doesn't run N× on multi-replica.
4. **Dial the stable Service DNS** instead of the raw `pod_ip` in `resolve_ssh_target` (reuse the PVC workspaces' headless Service, `workspace_manager.py:477-481`) so a *live* session's record survives pod recreation. Addresses the "never auto-corrected" half; orthogonal to eviction.

A liveness gate — `get_workspace_status` (`container_provisioner.py:531-571`, returns `None` on a 404) — can back either (1) or (2): check the pod before probing, evict on 404.

## Acceptance criteria

- An ended thread / dead-pod workspace record no longer appears in `list_active_ide_workspaces` (filtered out by the query or its context cleared on teardown).
- The IDE-settings sweeper no longer emits repeated `No route to host` probes for dead sessions; per-cycle SSH dials track the count of *live* workspaces, not the all-time count.
- On a multi-replica orchestrator the sweep runs once per cycle, not once per replica.

## Repro

```bash
CTX=k3d-srw NS=srw
# 1. Start a session (or worker job) so a workspace pod + a ready record exist.
# 2. End the session (or force-delete its workspace pod) — the pod goes away…
# 3. …but the record persists with status='ready' + the old pod_ip:
kubectl --context=$CTX -n $NS exec -i srw-postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  -c "SELECT id, status,
             metadata->'workspace_container'->>'status' AS ws,
             metadata->'workspace_container'->>'pod_ip' AS ip
        FROM threads
       WHERE metadata->'workspace_container'->>'status'='ready' AND status='ended';"
# 4. Watch the sweeper re-probe the dead endpoint every IDE_SETTINGS_SYNC_INTERVAL_S (600s):
kubectl --context=$CTX -n $NS logs deploy/srw-orchestrator -c orchestrator | grep -E "No route to host|pull_ide|reconcile_ide"
```
