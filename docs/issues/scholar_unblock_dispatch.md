# Parent Job Not Dispatched After Scholar Completion

**Date:** 2026-03-27
**Job:** `7cb05915-0dff-4c2a-92fe-6bf14dfeee5e` ("Verein")
**Scholar:** `90d084fc-cc5c-47b6-8a22-a93b976401be` ("Research phase for: Verein")
**Status:** `failed` (should have been automatically resumed after scholar completed)

## Symptoms

1. Scholar subjob completed successfully at 11:16
2. Orchestrator logged `Scholar 90d084fc completed — unblocking parent 7cb05915`
3. Parent job was never picked up by the dispatcher — sat idle for ~3 hours
4. Manual "Resume" at 14:09 triggered crash recovery, which failed with `database disk image is malformed`
5. Job status set to `failed`

## Timeline

| Time | Event |
|------|-------|
| 10:10:45 | Job `7cb05915` created via API, scholar `90d084fc` spawned in same request |
| 10:10:47 | Parent set to `waiting` status (`main.py:4015`). `assigned_agent_id` = NULL at this point (not yet dispatched) |
| ~10:48 | Orchestrator pod restarted (new instance `srw-orchestrator-8648d46fd9-znlwv`) |
| 10:55:16 | Stale agent detector recovers orphaned jobs. Scholar `90d084fc` resumed on agent `a4a9969e`. Parent `7cb05915` in `waiting` — skipped by both orphan recovery (`WHERE status = 'processing'`) and dispatcher (`WHERE status IN ('created', 'paused')`) |
| 10:55:16 | Validator job `44d0a9a7` also dispatched (priority 10) |
| 11:16:06 | Scholar `90d084fc` completed |
| 11:16:21 | `_handle_scholar_completion` sets parent status to `created`, calls `_trigger_dispatch()` |
| 11:16:21–14:09:55 | **~3 hours**: dispatcher runs repeatedly but never picks up `7cb05915` |
| 12:19–13:04 | Dispatcher repeatedly dispatches `44d0a9a7` (priority 10) to various agents — but never `7cb05915` |
| 14:09:55 | Manual "Resume" from cockpit |
| 14:09:55 | Agent detects `status=created` as crash recovery, attempts snapshot restore from phase 0 |
| 14:09:56 | Snapshot checkpoint.db is corrupted: `database disk image is malformed` |
| 14:09:56 | Job fails |

## Root Cause 1: `assigned_agent_id` Not Cleared on Unblock (CONFIRMED)

### The bug

The dispatcher query (`get_dispatchable_jobs` in `postgres.py:1576`) requires:

```sql
WHERE status IN ('created', 'paused')
  AND assigned_agent_id IS NULL
```

When `_handle_scholar_completion` (`main.py:4146`) sets the parent back to `created`, it does **not clear `assigned_agent_id`**. If the parent had been assigned to an agent before going to `waiting`, the stale `assigned_agent_id` persists and the dispatcher silently skips it.

### When does it trigger?

The bug only manifests when the parent was **dispatched to an agent before the scholar spawn**. There are two scholar creation paths:

| Path | When | `assigned_agent_id` at `waiting` | Affected? |
|------|------|-----------------------------------|-----------|
| **Job creation** (line 2150-2165) | Scholar spawned during `POST /api/jobs` | NULL (never dispatched yet) | No — works correctly |
| **Agent-triggered** (via orchestrator API during execution) | Agent requests scholar spawn mid-run | Set to the dispatched agent | **Yes — bug triggers** |

### Verification: "verein 2" vs "Verein"

| Job | Scholar creation path | `assigned_agent_id` at unblock | Dispatched after unblock? |
|-----|----------------------|-------------------------------|--------------------------|
| "Verein" (`7cb05915`) | Created at 10:10 during job creation, but parent was also dispatched to an agent before going to `waiting` (orchestrator restart at 10:48 complicates the timeline — parent was in `waiting` when the new orchestrator came up, but the previous agent's `assigned_agent_id` was already set) | Stale agent ID | **No** — stuck for 3 hours |
| "verein 2" (`d12ac4b3`) | Created at 14:37 during `POST /api/jobs` — scholar spawned before any dispatch | NULL | **Yes** — dispatched in 25ms |

### Deeper analysis: orchestrator restart interaction

The orchestrator restarted around 10:48. Before the restart:
- Parent `7cb05915` was in `waiting` status with `assigned_agent_id` pointing to agent `3d502640` (which ran it initially)
- The agent was killed/crashed at 10:16:09 (log just stops mid-execution)

After the restart:
- **Orphan recovery** (`recover_orphaned_jobs` in `postgres.py:1520`) only recovers jobs where `status = 'processing'` — `waiting` jobs are skipped
- **Dispatcher** only finds jobs where `status IN ('created', 'paused') AND assigned_agent_id IS NULL` — `waiting` jobs are skipped
- Result: the parent job in `waiting` is invisible to all recovery mechanisms

When the scholar completed at 11:16 and the parent was set to `created`:
- `assigned_agent_id` was still `3d502640` (the dead agent)
- Dispatcher query filters it out due to `AND assigned_agent_id IS NULL`
- Job stuck permanently until manual intervention

### Affected code paths

| Location | Function | Line | Issue |
|----------|----------|------|-------|
| `main.py` | `_spawn_scholar_subjob` | 4015 | Sets status to `waiting` without clearing `assigned_agent_id` |
| `main.py` | `_handle_scholar_completion` | 4146 | Sets status to `created` without clearing `assigned_agent_id` |
| `main.py` | `_handle_delegation_child_completion` | 4239 | Same bug — sets status to `created` without clearing `assigned_agent_id` |
| `postgres.py` | `recover_orphaned_jobs` | 1538 | Only recovers `processing` jobs — `waiting` jobs with stale agents are invisible |
| `postgres.py` | `update_job_status` | 710-772 | Only updates `assigned_agent_id` if explicitly passed — silent no-op otherwise |

### Fix

The fix must clear `assigned_agent_id` when transitioning to states where the agent is no longer needed:

```python
# In _spawn_scholar_subjob (line 4015) — parent no longer needs an agent while waiting:
await postgres_db.update_job_status(job_id, status="waiting", assigned_agent_id="")
# Note: update_job_status already supports assigned_agent_id param; passing "" sets to NULL

# In _handle_scholar_completion (line 4146) — ensure parent is dispatchable:
await postgres_db.update_job_status(target_id, status="created", assigned_agent_id="")

# In _handle_delegation_child_completion (line 4239) — same fix:
await postgres_db.update_job_status(target_id, status="created", assigned_agent_id="")
```

Additionally, `recover_orphaned_jobs` (postgres.py:1538) should also recover `waiting` jobs whose assigned agent is offline:

```sql
WHERE status IN ('processing', 'waiting')
  AND (
      assigned_agent_id IS NULL
      OR assigned_agent_id IN (
          SELECT id FROM agents WHERE status = 'offline'
      )
  )
```

For `waiting` jobs, recovery should set them to `waiting` (not `paused`) and clear the agent:

```sql
-- Only clear the stale agent, don't change status for waiting jobs
UPDATE jobs SET assigned_agent_id = NULL WHERE status = 'waiting' AND ...
```

## Root Cause 2: Corrupted SQLite Checkpoint (CONFIRMED)

When the job was finally resumed manually at 14:09, crash recovery tried to restore from the phase 0 snapshot. The snapshot's `checkpoint.db` was corrupted.

### The mechanism

1. **Snapshot creation** (`phase_snapshot.py:235`): Uses `shutil.copy2()` — a raw file copy with no SQLite awareness
2. **Checkpointer is active**: The `AsyncSqliteSaver` (langgraph) holds an open `aiosqlite.Connection` during the entire graph execution (`agent.py:452`)
3. **Snapshot called mid-execution**: `create_snapshot()` is called from the `archive_phase` graph node (`graph.py:1626`) while the checkpointer connection is still open and active
4. **Race condition**: If the checkpointer is writing to the DB file at the moment `shutil.copy2` reads it, the copy will contain an inconsistent state → corruption

### Code trace

| Step | File | Line | What happens |
|------|------|------|--------------|
| 1 | `agent.py` | 452 | `aiosqlite.connect(checkpoint_path)` — opens connection, stays open for entire job |
| 2 | `agent.py` | 454-455 | Wraps in `AsyncSqliteSaver` for langgraph checkpointing |
| 3 | `graph.py` | 1626 | `snapshot_manager.create_snapshot()` called from `archive_phase` node |
| 4 | `phase_snapshot.py` | 235 | `shutil.copy2(checkpoint_path, snapshot_dir / "checkpoint.db")` — **raw copy while DB is open** |
| 5 | `phase_snapshot.py` | 420 | On restore: `shutil.copy2(snapshot_checkpoint, checkpoint_path)` — overwrites with corrupted copy |
| 6 | `phase_snapshot.py` | 83-84 | `discover_thread_id_from_checkpoint()` opens the restored copy with `sqlite3.connect()` — fails: `database disk image is malformed` |

### Why this isn't always a problem

The snapshot is taken at a phase boundary (end of `archive_phase` node). At this point, the checkpointer has already committed the current state. The race window is narrow — it only corrupts if the checkpointer happens to be doing housekeeping writes or if SQLite WAL/journal files are in an intermediate state at the exact moment of the copy.

### Fix options

1. **Best**: Pass the checkpointer connection to the snapshot manager and use `sqlite3.Connection.backup()` for a consistent copy (not currently used anywhere in the codebase — confirmed by search)
2. **Good**: Before copying, open a second synchronous `sqlite3.connect()` to the checkpoint file, issue `PRAGMA wal_checkpoint(TRUNCATE)`, then copy
3. **Minimum**: Add a try/except around the copy with integrity verification (`PRAGMA integrity_check`) on the snapshot copy, and retry on failure

## Issue 3: Gitea File Delete Bug (CONFIRMED)

### The bug

`orchestrator/services/gitea.py:540-543` passes `json=delete_payload` to `httpx.AsyncClient.delete()`:

```python
resp = await client.delete(
    f"{self._url}/api/v1/repos/{self._user}/{repo_name}/contents/{file_path}",
    json=delete_payload,  # <-- httpx DELETE does not support json= parameter
)
```

The `httpx.AsyncClient.delete()` method does NOT accept a `json` parameter (unlike `post()`, `put()`, `patch()`). The Gitea API endpoint `DELETE /repos/{owner}/{repo}/contents/{filepath}` expects a JSON body with `sha` and `message`, but httpx requires using `content=` with manual JSON serialization for DELETE bodies.

### Impact

Every file deletion after a subjob merge fails silently (caught and logged as WARNING). The branch merge via PR squash still succeeds, but leftover files from the subjob branch are not cleaned up individually. Non-fatal but produces log noise.

### Affected callers

| Location | Context |
|----------|---------|
| `main.py:224-229` | Pre-merge cleanup for subjob files |
| `main.py:239-244` | Pre-merge cleanup for subjob directory contents |
| `main.py:3818-3822` | Job approval cleanup (removing `job_frozen.json`) |

### Fix

```python
import json as json_module

resp = await client.delete(
    f"{self._url}/api/v1/repos/{self._user}/{repo_name}/contents/{file_path}",
    content=json_module.dumps(delete_payload),
    headers={"Content-Type": "application/json"},
)
```

### Other HTTP methods in the file

All other methods use the correct kwargs:
- `client.post(json=...)` — correct (POST supports `json=`)
- `client.put(json=...)` — correct (PUT supports `json=`)
- `client.patch(json=...)` — correct (PATCH supports `json=`)
- `client.delete()` without body — correct (3 other delete calls pass no body)
- Only `delete_file()` at line 540 is affected

## Issue 4: `_dispatch_job_to_agent` vs `_resume_job_on_agent` Mismatch

### The issue

When a parent job is unblocked after scholar completion, it transitions from `waiting` → `created`. The dispatcher (line 1066-1069) routes based on status:

```python
if job["status"] == "paused":
    success = await _resume_job_on_agent(job, agent)  # → POST /job/resume
else:
    success = await _dispatch_job_to_agent(job, agent)  # → POST /job/start
```

Since the unblocked parent has `status='created'`, it takes the `else` branch and sends a **`/job/start`** request instead of `/job/resume`. This means:
- The agent starts the job from scratch, not from the checkpoint
- All previous work (phases, todos, workspace files) from before the scholar was spawned is lost
- The agent has to redo work, and if the checkpoint is corrupted, it can't recover

### Impact

For "verein 2" (where `assigned_agent_id` was NULL and dispatch worked), the job was dispatched as a brand new job via `/job/start`. The agent started fresh — which may work if the scholar results are available via Gitea, but it loses all progress the parent made before the scholar spawn.

### Suggested fix

The unblock should set status to `paused` (not `created`) so the dispatcher uses the resume path:

```python
# In _handle_scholar_completion:
await postgres_db.update_job_status(target_id, status="paused", assigned_agent_id="")
```

This way the dispatcher calls `_resume_job_on_agent` → `/job/resume`, which preserves the checkpoint and workspace state.

## Additional Observations

- The `memories` relation does not exist on the K8s vector database, causing non-fatal warnings throughout the job run. This suggests the vector DB schema hasn't been fully initialized (`orchestrator/database/vector_schema.sql`).
- tiktoken encoding downloads fail inside the cluster (no outbound HTTPS to `openaipublic.blob.core.windows.net`), falling back to approximate token counting. Consider pre-caching tiktoken data in the agent container image.
- Agent `3d502640` (which originally ran "Verein") was killed mid-execution at 10:16:09 — the log stops abruptly during iteration 20 with no completion signal. This was likely caused by the pod restart/rescheduling that happened around 10:48. The heartbeat-based stale detection did eventually mark the agent offline, but the `waiting` job was already invisible to recovery.

## Summary of Required Fixes

| # | Severity | Fix | File(s) |
|---|----------|-----|---------|
| 1 | **Critical** | Clear `assigned_agent_id` when setting status to `waiting` or `created` in scholar/delegation handlers | `orchestrator/main.py:4015,4146,4239` |
| 2 | **High** | Extend orphan recovery to also clean stale `assigned_agent_id` on `waiting` jobs | `orchestrator/database/postgres.py:1520-1550` |
| 3 | **High** | Use `paused` instead of `created` for unblocked parents so they resume (not restart) | `orchestrator/main.py:4146,4239` |
| 4 | **Medium** | Use SQLite backup API instead of `shutil.copy2` for checkpoint snapshots | `src/core/phase_snapshot.py:235,420` |
| 5 | **Low** | Fix `httpx.delete(json=...)` to use `content=` with manual JSON serialization | `orchestrator/services/gitea.py:540-543` |
