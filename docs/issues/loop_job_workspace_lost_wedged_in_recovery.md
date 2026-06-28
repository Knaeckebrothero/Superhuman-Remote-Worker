# Loop job's workspace is lost mid-run → wedged in `workspace_unavailable` recovery, never re-dispatched

**Status:** Investigated on the live main cluster. The *recovery* state machine is understood and is where the job is stuck; *why the workspace first became unavailable* is **not** isolated (k8s events have aged out). Not fixed. Distinct from — but mutually compounding with — the orchestrator OOM in `audit_metadata_config_duplication_ooms_orchestrator.md`.
**Found:** 2026-06-27. Job `19707fa1-1788-4eda-a296-8b108429b108` (Loop iter 3 · DEVELOPER — "Build an ERP for Hotel Rheinland in Bad Orb"), project `68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a`, owner `knaeckebrothero` / `overlygenericaddress@pm.me`, main cluster (ns `superhuman-remote-worker`).
**Severity:** **Medium-High.** A loop-iteration job whose workspace dies mid-run gets stuck in `paused` with `recovering: true` **indefinitely** instead of cleanly re-provisioning or failing loudly. It holds the loop slot, never advances, and the abandoned audit/checkpoint state it leaves behind is exactly what then OOMs the orchestrator — the two defects feed each other.
**Component:** `workspace_unavailable` recovery handler (`orchestrator/main.py:9894-9937`) · recovery-flag clear-on-reregister (`orchestrator/services/nats_bridge.py:481`) · workspace/VM lifecycle (`orchestrator/services/lifecycle/{workspace_manager,vm_manager}.py`) · pod snapshot/restore · the upstream workspace-pod loss (cause unknown)
**Related:** `audit_metadata_config_duplication_ooms_orchestrator.md` (the OOM that prevents recovery from completing) · `agent_workspace_pod_resource_headroom.md` (candidate upstream cause: pod eviction under resource pressure) · memory topics `project_self_improvement_loop`, `project_workspace_tier_upgrade`, `project_start_session_on_vm` (NATS VM-register HA hole) · sibling incident same job-family/day `loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md`

---

## Symptom

Loop job `19707fa1` reads `paused` in the Cockpit and never resumes — it looks like a generic pause, but the `jobs.context` carries a workspace-recovery state machine frozen mid-flight:

```json
"vm": { "status": "deleted", "requested": true, "recovering": true,
        "previous_error": "workspace_unavailable", "snapshot_attempts": 5 },
"snapshot": { "status": "available", "source_type": "pod",
              "created_at": "2026-06-27T17:34:41Z", "size_compressed_bytes": 98928276 },
"workspace_container": { "pod_name": "workspace-19707fa1-178", "pod_ip": "10.42.2.77",
                         "status": "deleted", "last_activity": "2026-06-27T17:34:20Z" }
```

`recovering: true` with both the workspace pod and the VM `deleted`, a ~99 MB snapshot `available`, and `snapshot_attempts: 5` and climbing → recovery was kicked off but **never completed**, and the job is wedged.

## Timeline (reconstructed from audit + context, all UTC)

| time | event | source |
|---|---|---|
| 13:39:21 | job dispatched onto pod workspace `workspace-19707fa1-178` (10.42.2.77:30022) | jobs / context |
| 13:39–17:25 | **healthy, productive run** (see next section) | `llm_requests` / audit |
| 17:25:09 | last successful LLM call (#1202) and last tool call | audit |
| 17:25–17:30 | async memory housekeeping (store/verdict/retrieve) | audit |
| **17:30:23** | last audit event — iter 1203 `memory_inject`, **mid-step, right before LLM call**; then silence | audit |
| 17:34:20 | workspace pod's last activity | context |
| 17:34:41 | orchestrator snapshots the pod (~99 MB, `available`) | context |
| ~17:34 | workspace pod **deleted** | context |
| **17:39:43** | job flipped to **`paused`**, `assigned_agent_id` cleared | jobs |
| after | `recovering: true`, `previous_error: workspace_unavailable`, `snapshot_attempts: 5` — re-provision keeps failing | context |

## The agent was NOT in an error loop (recorded so nobody re-chases this)

The natural hypothesis — "the LLM provider (OpenRouter) stopped serving ~16:00, the agent error-looped to ~6k steps" — is **refuted by the data**:

- **1202 LLM calls, all completed**: audit `llm` pre = 1202, post = 1202. Tool pre = 1300, post = 1300. No pre-without-post, no hung/failed calls.
- **Steady, healthy cadence through and past 16:00**: ~40–70 calls / 10 min, latency 5–16 s, **0 / 1202 empty responses**, right up to 17:25.
- **Real, coherent work**: final responses are genuine `AIMessage`s doing TDD — e.g. 17:25 *"…the previous test_cov_run29.txt only has `EXIT=1` repeated… Let me try with explicit redirection"* → `pytest …`; 16:05 *"Tactical Phase 23 (Green for AC-5) starting"* → `read_file repo/tests/test_rechnung.py`.
- **Strategic progress, not a spin**: phase advanced **23 (16:05) → 29 (17:25)**.
- The "error"/"payment"/"credit" substrings in responses (111 / 33 of 1202) are the **agent's own content** — debugging its failing tests and building a hotel ERP (invoices/`Rechnung`/payments) — not API error envelopes (`choices`: 0/1202).
- **"6k steps" is normal volume**: ~1202 iterations × ~7 audit rows each (llm pre/post + tool pre/post + memory ops) ≈ 8,800 rows (`get_job` reports ~6,306, counted slightly differently). Not runaway retries.

**Blind spot:** `llm_requests`/audit record only the agent's *completed* execute attempts. Transient 402/429s retried *below* the audit layer (LiteLLM/httpx) and ultimately succeeding would be invisible — but there is no terminal-failure signature (no empty responses, no latency blow-up, continued progress). Whether OpenRouter billed the account after ~16:00 is answerable only on OpenRouter's side and, either way, is **not** what stopped the job.

## Root cause

### What we know — the recovery state machine wedges

`orchestrator/main.py:9894-9937`, in the job-completion path:

```python
if isinstance(error, dict) and error.get("type") == "workspace_unavailable":
    vm_ctx = _get_vm_context(job)
    if vm_ctx and vm_ctx.get("recovering"):          # 9899: re-entry guard
        return {... "new_status": "paused", "actions": ["vm recovery: duplicate skipped"]}
    ctx["vm"] = {"requested": True, "recovering": True,
                 "previous_error": "workspace_unavailable"}   # 9917-9921
    await postgres_db.update_job_context(job_id, ctx)
    if vm_ctx and vm_ctx.get("status") not in ("deleted", "deleting"):
        await vm_provisioner.delete_vm(job_id)         # 9926
    await postgres_db.pause_job(job_id)                # 9928: clears agent, re-queues
    _trigger_dispatch()
```

On `workspace_unavailable`: set `recovering=true`, delete the old workspace, pause + re-queue. The `recovering` flag is **cleared only on successful re-registration** of the new workspace (`orchestrator/services/nats_bridge.py:481` `"recovering": False  # Clear recovery guard`). So if the re-provisioned workspace **never comes up / never re-registers**, the flag is never cleared, the re-entry guard (9899) then short-circuits every subsequent attempt to `"duplicate skipped"`, and the job sits in `paused`/`recovering` forever. `snapshot_attempts: 5` is the restore having been retried and failed five times with no terminal state.

### Likely defect — VM recovery path fired for a POD-backed workspace

This job's workspace was a **pod** (`workspace_container`, snapshot `source_type: "pod"`, `pod_name: workspace-19707fa1-178`). Yet the handler that fired is the **VM** recovery path — it writes `ctx["vm"]` and calls `vm_provisioner.delete_vm(job_id)`. The resulting context shows a `vm` block (`vm_name: agent-vm-19707fa1-…`, `status: deleted`) **and** a `workspace_container` block, with `snapshot_attempts` tracked on the `vm` side (5) but `0` on the container. Strong indication the pod-workspace failure is being driven through VM-shaped recovery — to be confirmed, but it would explain why restore never succeeds (wrong provisioner / wrong artifact).

### What we DON'T know — why the workspace first became unavailable

k8s events for `workspace-19707fa1-178` have aged out. Candidates, unproven: node eviction / memory pressure (the agent's context is large — 237 kB avg request, growing), node drain, or pod crash. The agent going silent **mid-step at 17:30:23, immediately before an LLM call**, is consistent with the pod/workspace being yanked out from under it (it had been healthy the prior second). See `agent_workspace_pod_resource_headroom.md` for the headroom angle. The dead IP `10.42.2.77:30022` is also one of the stale endpoints in the `ide_settings` "No route to host" flood noted in the OOM doc.

## Why it stays stuck (compounding with the OOM)

Completing recovery is multi-step orchestrator work: delete → re-provision a pod → **restore the 99 MB snapshot** → re-register → clear `recovering` → re-dispatch. The orchestrator is OOM-crash-looping whenever this job's audit is viewed (see the OOM doc), so it cannot reliably drive that sequence to completion. Net: a control plane that dies every couple of minutes cannot finish a recovery that needs sustained uptime — so the recovery never clears and the job never resumes.

## Fix / recommendations

1. **Bound the recovery.** Cap `snapshot_attempts` (it is at 5 with no terminal state). After N failed re-provision/restore attempts, **fail the job loudly** (freeze + operator-visible reason) instead of leaving it `paused`/`recovering` forever.
2. **Time-box / clear the `recovering` flag.** The re-entry guard (9899) permanently skips once `recovering` is stuck-true. Add a deadline (or clear-on-terminal-failure) so a wedged recovery can't silently swallow every retry.
3. **Fix the pod-vs-VM recovery mismatch** (if confirmed): route pod-workspace `workspace_unavailable` through the container/workspace manager + pod-snapshot restore, not `vm_provisioner`.
4. **Surface it in the UI.** Workspace-loss + recovery is currently invisible (the job just shows `paused`). The Cockpit loop/job view should show `recovering` / `previous_error` / attempt count.
5. **Address the upstream trigger** (workspace pod loss) — node headroom / eviction protection for long DEVELOPER jobs with large contexts; see `agent_workspace_pod_resource_headroom.md`.

## Open questions

- Why did pod `workspace-19707fa1-178` (10.42.2.77) become unavailable — eviction, OOM, or drain? (events gone; would need to reproduce or catch it live next time)
- Is the VM-recovery-handling-a-pod-workspace path a genuine bug or intended fallback?
- Is `snapshot_attempts` bounded anywhere, or unbounded?

## Verification commands

```bash
CTX=main NS=superhuman-remote-worker JOB=19707fa1-1788-4eda-a296-8b108429b108
# the wedged recovery state
kubectl --context=$CTX -n $NS exec -i srw-postgres-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  -c "SELECT jsonb_pretty(context) FROM jobs WHERE id='$JOB';"
# prove the agent ran clean (no error loop): pre==post for llm & tool
kubectl --context=$CTX -n $NS exec -i srw-auditdb-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  -c "SELECT step_type,event_phase,count(*) FROM agent_audit WHERE job_id='$JOB' AND step_type IN ('llm','tool') GROUP BY 1,2 ORDER BY 1,2;"
```
