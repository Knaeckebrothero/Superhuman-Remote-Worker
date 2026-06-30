# Loop job's workspace dies mid-run → wedged forever in VM-shaped recovery, never re-dispatched or failed

**Status:** Investigated on the live main cluster + a 5-agent code audit (2026-06-29). The recovery state machine is now **fully traced at code level** and the mechanism **confirmed** (this supersedes the earlier "to be confirmed / events aged out" framing — several of those guesses were wrong; corrections are called out inline). **Not fixed.** Two concerns were split out: the *upstream trigger* (why the workspace pod died) → `agent_workspace_pod_resource_headroom.md`; the finding that snapshot/restore is *structurally dead for ALL jobs* → `snapshot_restore_dead_for_jobs.md`.
> **Update 2026-06-29 — the Tier-1 wedge-fix is IMPLEMENTED (as "G1") and the Tier-2 fork is SUPERSEDED.** The chosen recovery direction is **PVC reattach** (not S3-snapshot-restore nor blank+checkpoint) — see `docs/features/workspace_pvc_branch_a_implementation.md`. G1 (backend-aware routing + bounded `recovery_attempts` → fail-loud, no more `vm.requested` stamp on pod jobs) + G2 (backend-aware resume preserve) are built and k3d-E2E'd: the wedge is gone, the PVC data is preserved, and **a crashed job now auto-resumes** from its checkpoint on the reattached volume. The ephemeral-IP reconnect race the E2E first surfaced was **fixed** (Option 1 — a stable headless Service the agent dials instead of the pod IP; `workspace_reattach_ephemeral_ip_reconnect_churn.md`, RESOLVED), and the full real-job recovery E2E passed (job `b4025433`). So **this wedge is effectively resolved by the PVC-reattach feature** (Phases 0–3 done/committed). The detailed Tier-1/Tier-2 design below is retained for history but the PVC doc is now authoritative.
**Found:** 2026-06-27. Job `19707fa1-1788-4eda-a296-8b108429b108` (Loop iter 3 · DEVELOPER — "Build an ERP for Hotel Rheinland in Bad Orb"), project `68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a`, owner `knaeckebrothero` / `operator@redacted.invalid`, main cluster (ns `superhuman-remote-worker`). Still `paused` and inert as of 2026-06-29.
**Severity:** **High.** A loop/worker job whose workspace dies mid-run is wedged in `paused`/`recovering` **forever** — it never re-provisions, never fails, holds its project-loop slot (the loop never advances), and is invisible to every stuck-job monitor (all `processing`-only). It recurs on **every** workspace death for a pod-backed job, and the abandoned audit/checkpoint state it leaves behind is what then OOMs the orchestrator (`audit_metadata_config_duplication_ooms_orchestrator.md`) — the two defects compound.
**Component:** `workspace_unavailable` recovery handler (`orchestrator/main.py:10118-10161`) · dispatcher VM pre-filter (`main.py:4151-4199`) · `_job_needs_vm` (`main.py:3103-3114`) · the only `recovering`-clear (`orchestrator/services/nats_bridge.py:481`) · VM lifecycle reconciler (`orchestrator/services/lifecycle/{vm_manager,reconciler}.py`) · loop advance (`main.py:10358`, `orchestrator/services/project_loop_sweeper.py`) · error origin (`src/agent.py:929-958`, `src/core/backends/remote.py:312-315`)
**Related:** `snapshot_restore_dead_for_jobs.md` (Defect B — the restore path this handler should reach is itself dead for jobs) · `agent_workspace_pod_resource_headroom.md` (why the pod died + the instrumentation hole) · `audit_metadata_config_duplication_ooms_orchestrator.md` (the compounding OOM — now fixed) · memory topics `project_self_improvement_loop`, `project_loop_repo_compounding`, `project_cross_pod_checkpointer_d3` (the checkpoint that makes Tier-2 viable) · sibling same-day incident `loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md`

---

## Symptom

Loop job `19707fa1` reads `paused` in the Cockpit and **never resumes**. Its `jobs.updated_at` has been frozen at the pause moment (`2026-06-27T17:39:43Z`) for two days — it is **inert, not hot-looping**. `jobs.context` carries a half-finished recovery state machine:

```json
"vm": { "status": "deleted", "requested": true, "recovering": true,
        "previous_error": "workspace_unavailable", "snapshot_attempts": 5 },
"snapshot": { "status": "available", "source_type": "pod",
              "created_at": "2026-06-27T17:34:41Z", "size_compressed_bytes": 98928276 },
"workspace_container": { "pod_name": "workspace-19707fa1-178", "pod_ip": "10.42.2.77",
                         "status": "deleted", "last_activity": "2026-06-27T17:34:20Z" }
```

The job is **pod-backed** (`workspace_container`, snapshot `source_type: "pod"`) yet the recovery state lives entirely in a `vm` block with `snapshot_attempts: 5` — the first tell that pod failure was driven through VM-shaped recovery.

## What actually happened (the agent did NOT crash or error-loop)

The **agent pod stayed healthy** and reported the failure *gracefully*. The agent's SSH/SFTP calls to the workspace pod (`10.42.2.77:30022`) started failing; `RemoteBackend._exec` wrapped the paramiko/socket error as `WorkspaceUnavailableError` (`src/core/backends/remote.py:312-315`), which propagated out of the graph (`src/graph.py:3879-3888`) to the top-level `process_job` handler (`src/agent.py:929-958`). That handler set `error = {message: str(e), type: "workspace_unavailable", recoverable: True}` and returned `should_stop=True`. So the agent was alive and running when its **separate workspace pod** became unreachable — it didn't get killed; it detected the loss and asked for recovery. (1202 LLM calls, all completed, 0 empty responses, phase advanced 23→29 right up to the silence — the "OpenRouter error-loop" hypothesis is refuted by the data.)

`WorkspaceUnavailableError` is **backend-agnostic** — the same SSH backend serves both pod and VM workspaces, and `is_vm_error = isinstance(e, WorkspaceUnavailableError)` (`src/agent.py:933`) labels *every* remote-workspace loss `workspace_unavailable`, pods included. Disambiguating pod-vs-VM is therefore the orchestrator's job — and the handler doesn't do it.

## Root cause — the recovery handler is VM-only; one stamp wedges three systems

`orchestrator/main.py:10118-10161` is the entire recovery path. On `error.type == "workspace_unavailable"` it:

1. Re-entry guard: if `_get_vm_context(job).get("recovering")` is already true → early-return `paused`, **no `_trigger_dispatch()`, no terminal write** (`:10122-10132`).
2. **Unconditionally** stamps `ctx["vm"] = {"requested": True, "recovering": True, "previous_error": "workspace_unavailable"}` — no pod/VM check (`:10141-10145`), persisted via `update_job_context` (a **full-context replace**, `postgres.py:1408-1409`).
3. `if vm_ctx and vm_ctx.get("status") not in ("deleted","deleting"): await vm_provisioner.delete_vm(job_id)` (`:10149-10150`).
4. `pause_job(job_id)` + `_trigger_dispatch()` + return (`:10152-10153`).

That single `vm.requested = True` stamp on a **pod** job poisons three independent systems:

### (a) Dispatch routing flips to the VM arm — permanently
`_job_needs_vm(job)` returns True whenever `ctx["vm"].requested` is truthy (`main.py:3103-3114`). The dispatcher checks `if _job_needs_vm(job):` **first** (`:4124`) and enters the VM provisioning arm (`:4151-4199`), so it **never** reaches the pod arm (`_job_needs_sandbox` → `ensure_workspace`, `:4201-4265`) where restore would happen. On a VM-capable cluster the first tick (status still empty) calls `vm_provisioner.create_vm` (`:4177`) → spawns a **phantom VM** (`vm_name: agent-vm-19707fa1-…`) that never registers ready.

### (b) The VM reconciler adopts the phantom VM and churns `snapshot_attempts`
`VMInstanceManager._fetch_vm_rows` selects `jobs WHERE context->'vm' IS NOT NULL` (`vm_manager.py:363-370`) — the stamped row matches. The reaper finds it reapable (paused) + dirty + unreachable (no `ssh_host`) and calls `record_attempt` → `merge_vm_context({"snapshot_attempts": nxt})` (`reconciler.py:281-282`, `vm_manager.py:255`). That is the **exact source of `snapshot_attempts: 5` on the `vm` side, `0` on the container side** (the pod manager lists only live pods; this pod is gone). After 5, `attempts_exhausted` → `give_up` → `delete_vm` → `vm.status="deleted"` (`reconciler.py:266-267`).

### (c) The dispatcher then dead-stops on `vm.status` — the literal wedge
Once `vm.status` is `"deleting"/"deleted"` (set by `delete_vm`, which runs *after* the status-clearing overwrite at `:10146`), the VM pre-filter has **no branch for it**:
- `if not vm_ctx.get("status")` (`:4152`, the re-provision branch) → **False** (status is truthy).
- `elif vm_ctx.get("status") not in ("ready",): continue` (`:4196`) → **True** → `continue`.

**Every 30 s dispatcher tick hits `continue` before any DB write** → `updated_at` frozen, inert forever. This is THE wedge.

### (d) `recovering` can never clear for a pod
The **only** reset of `recovering` to `False` is `nats_bridge.py:481`, in `_on_daemon_register` (subject `agent.vm.*.register` — a **VM management daemon** registering). A re-provisioned **pod** never runs that daemon, so even if (a)–(c) were fixed, `recovering` stays true and the re-entry guard (`:10122`) would skip future attempts as `"duplicate skipped"`.

### (e) No terminal state anywhere → loop stalls, monitoring blind
There is **no recovery-attempt cap** in this handler. `give_up` in both managers tears down the *instance* but **never touches the `jobs` row** — no `failed`, no `freeze_data`, no `error_message`, no `recovering` clear (only a `logger.warning`, `reconciler.py:273-280`). Because the job never reaches a terminal status: the loop-advance hook (`main.py:10358`, terminal-only) and the safety-net sweeper (`project_loop_sweeper.py:88`, terminal-only) never fire → `current_job_id` stays pinned → the loop is stalled indefinitely. And `get_stuck_jobs` / `recover_orphaned_jobs` are **`processing`-only** (`postgres.py:2069`, `:2451`) → a `paused`/`recovering` job is invisible to monitoring no matter how old.

### Corrections to earlier guesses (recorded so they aren't re-chased)
- **`delete_vm` did NOT fire on the first pass.** The `if vm_ctx …` guard (`:10149`) reads `vm_ctx` *before* the stamp, so for a pod job it's `{}` and `delete_vm` is **skipped**. The `vm_name`/`vm.status=deleted` came from the dispatcher's **own** later `create_vm` + the reaper's `give_up`, not the handler.
- **`restore_workspace` itself routes pods correctly** (`if ws_ctx: … elif vm_ctx:`, `workspace_suspension.py:280/284`) — the poison is purely **upstream at the dispatch predicate** `_job_needs_vm`; restore is simply never reached.
- **`snapshot_attempts: 5` is reap-snapshot attempts on the phantom VM**, not restore retries.
- **Adjacent landmine:** the dispatch VM arm runs `_check_vm_permission` (`:4138`). A pod-backed job whose owner lacks `can_use_vm` would be **hard-failed** with *"User is not permitted to use VM workspaces"* (`:3363`) — a nonsensical denial for a job that never requested a VM. This incident's owner is privileged, which masked it.

## What's already built (reuse, don't rebuild)

- **An exact bounded-retry→fail-loud precedent sits 5 lines below the bug:** the `memory_unavailable` path (`main.py:10166-10196`) — atomic counter `increment_job_memory_retry` (`postgres.py:1479`), cap `MEMORY_RETRY_CAP` (`completion.py:253`), `paused` under cap / **`failed` at cap** (`completion.py:349-358`).
- `update_job_status(status="failed", error_message=…, freeze_data=…)` (`postgres.py:972`) — terminal status + operator reason + freeze blob in one call.
- `_advance_project_loop` already treats `failed` correctly (`main.py:9871-9872` — increments `consecutive_failures`, rotates or stops) → a terminal `failed` **auto-unblocks the loop**.
- `merge_vm_context` / `merge_workspace_container_context` (`postgres.py:1507/1620`) — atomic sub-context writes (no full-column clobber).
- Reliable backend identity at failure time: `context.snapshot.source_type` (`"pod"`/`"vm"`), a real `workspace_container` block (`_get_container_context`, `:3180`) vs a real `vm` block (`_get_vm_context`, `:3126`); evaluate `_job_needs_vm(job)` **before** any stamp to capture original intent.

## Fix

### Tier-1 — un-wedge + fail loud + instrument (commit to this; pure rewiring, ~40-60 lines)

In the `workspace_unavailable` handler (`main.py:10118-10161`):

1. **Branch on backend before stamping.** Compute `was_vm` from `_job_needs_vm(job)` (pre-stamp) and/or `context.snapshot.source_type` / a real `vm.vm_name`. **Do not stamp `ctx["vm"]` for pod-backed jobs** — that single change removes the routing poison (a), the phantom-VM churn (b), and the dead-stop (c).
2. **Bounded recovery cap → fail loud** (mirror the `memory_unavailable` block right below). Add `context.…recovery_attempts` (atomic increment helper modeled on `increment_job_memory_retry`). Under cap → re-dispatch for recovery; **at cap → `update_job_status(failed, error_message="workspace unavailable; recovery exhausted after N attempts", freeze_data={"freeze_type":"workspace_unavailable", …})` and clear `recovering`.** Terminal `failed` becomes operator-visible, drops out of `get_dispatchable_jobs` (freeze_data set), and fires `_advance_project_loop` → the loop rotates. This also closes the latent re-entry guard (`:10122`), which today has no cap or terminal path.
3. **Clear `recovering` on the pod path.** There is no pod analogue of `nats_bridge.py:481`; rely on `workspace_container.status` transitions (or clear it when the pod goes `ready`) rather than the VM-daemon signal.
4. **Capture the failure cause** (closes the instrumentation hole; see `agent_workspace_pod_resource_headroom.md` §Update): persist `error.get("message")` (the discriminating SSH string) into the context/`error_message` instead of the hardcoded literal, and append it to the existing warning log (`:10134`). Optionally read `terminated.reason/exit_code/signal` from the dead pod (reuse `agent_provisioner.py:689-707`).

Tier-1 alone un-sticks `19707fa1`, unblocks the loop, makes the state operator-visible, and instruments the next death — **without** deciding how the work is recovered.

### Tier-2 — actually recover the work (DECISION DEFERRED to the fix session; both options below)

This is the only architectural fork. It is the **same decision** as `snapshot_restore_dead_for_jobs.md`.

- **Option A — revive snapshot-restore for jobs.** Route the pod recovery path to set `workspace_container.status="suspended"` so the already-wired `ensure_workspace` → `restore_workspace` (`workspace_lifecycle.py:101-103`) rehydrates the 99 MB snapshot into a fresh pod. No data loss. Cost: it's the half-built path (see Defect B) and inherits the checkpoint↔`main` state-vs-artifact reconciliation question.
- **Option B — re-provision clean + resume from the Postgres checkpoint** (worker/loop jobs only; reserve restore for sessions). The cross-pod checkpointer is live, default-on, worker-only, and fresh-pod-verified (`project_cross_pod_checkpointer_d3`), so reasoning/progress (messages, todos, phase) survives. **Sound but leaky for artifacts:** git only persists at *phase-boundary pushes* (`src/core/phase.py:983-1027`; per-todo commits are local-only, `src/managers/todo.py:519`), so up to ~one phase of un-pushed code is lost, and a freshly-cloned `main` can lag what the checkpoint thinks is done. **Guardrails required** (per the checkpoint audit): force a fresh clone of `branch_name` into the blank workspace (existing re-clone gates key on the agent-pod-local path, not a blanked remote box — `src/agent.py:1835-1889`); rewind to the last *pushed* phase boundary, not the last checkpoint super-step; restrict to loop-execution roles (`is_loop_execution_role`, work-on-`main`); gate on "first checkpoint exists" (`preemption_before_first_checkpoint_replays_job_opening.md`); add the "did `main` actually advance?" backstop.

## Open questions

- **Why did pod `workspace-19707fa1-178` become unavailable?** k8s events aged out; the discriminating SSH string was discarded. Leading hypothesis: container OOMKill on the hardcoded 4 Gi limit under the build/pytest workload (`restartPolicy: Never` makes it permanent), or node-pressure eviction. See `agent_workspace_pod_resource_headroom.md`.
- **Tier-2 fork** (Option A vs B) — to decide in the fix session.

## Verification commands

```bash
CTX=main NS=superhuman-remote-worker JOB=19707fa1-1788-4eda-a296-8b108429b108
# the wedged recovery state (vm.status=deleted + recovering=true + snapshot_attempts=5)
kubectl --context=$CTX -n $NS exec -i srw-postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  -c "SELECT status, updated_at, jsonb_pretty(context) FROM jobs WHERE id='$JOB';"
# prove the agent ran clean (pre==post for llm & tool — no error loop)
kubectl --context=$CTX -n $NS exec -i srw-auditdb-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  -c "SELECT step_type,event_phase,count(*) FROM agent_audit WHERE job_id='$JOB' AND step_type IN ('llm','tool') GROUP BY 1,2 ORDER BY 1,2;"
```

## Acceptance criteria

- A pod-backed job that loses its workspace is **never** stamped `vm.requested` and is re-dispatched through the pod path (or failed loud).
- After N failed recovery attempts the job reaches **terminal `failed`** with an operator-visible `error_message`/`freeze_data`, and the project loop **advances** (slot freed).
- `recovering` cannot remain stuck-true for a pod-backed job.
- The failure cause (SSH error string and/or pod `terminated.reason`) is persisted somewhere queryable.
- A non-admin owner's pod-backed job is never denied with "not permitted to use VM workspaces".
