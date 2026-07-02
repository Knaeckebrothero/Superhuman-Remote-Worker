# Orchestrator OOM crash-loop: `resolved_config` is duplicated into every audit row, then `/audit/bulk` materializes the whole pile

**Status:** **Fixed.** Write-side fix (A — strip config from per-row audit metadata) **committed `7ea0d798` + deployed to dev**; read-side lean projection (B) shipped as Phase 1 of the debug-view refactor (see `docs/features/debug_audit_view_refactor.md` §0), the Cockpit debug view no longer touches the bulk path at all, and **(2026-06-29) the amplifier itself is gone**: the MCP `get_audit_bulk`/`get_chat_bulk` tools were migrated to the lean/paged `/audit?lean=true` + `/chat?offset&limit` endpoints (MCP was the last bulk consumer), and the `/{audit,chat,graph}/bulk` endpoints + `get_*_bulk` store methods were **deleted** (incl. the now-unreachable copies on the retired `mongodb.py` `MongoDB` store). No read path can materialize 5,000 config-bearing rows anymore. **Verified end-to-end on the main cluster after redeploy (2026-06-29):** reading **this very job (`19707fa1`, 6306 entries, fat un-backfilled metadata still on disk)** through the migrated `get_audit_bulk` MCP tool — including the formerly-fatal `limit=5000` — now returns instantly with **no 504/OOM and the control plane stays up**; the migrated tools clamp to 200 + lean-project at read. **One cosmetic gap remains:** the existing-rows **backfill (#4) ran on local k3d only — NOT dev/prod**, so `19707fa1`'s rows still carry fat `metadata` on disk — but this is now **storage weight, not an OOM vector** (no endpoint bulk-reads them; both the UI and MCP strip at read time). Root cause isolated + **measured on the live main cluster** (below). Originally two distinct defects (write-side bloat + read-side amplifier); either fix alone stops the OOM — now both are addressed.
**Found:** 2026-06-27. Job `19707fa1-1788-4eda-a296-8b108429b108` ("Loop iter 3 · DEVELOPER — Build an ERP system for Hotel Rheinland in Bad Orb"), project `68137e29-6b1f-4f1b-a0c1-4e6dc2be3f9a`, owner `knaeckebrothero` / `operator@redacted.invalid`, **main cluster** (ns `superhuman-remote-worker`, orchestrator image `sha-f0b1188`).
**Severity:** **High.** A single long/loop job silently bloats its audit trail; thereafter *any* client that bulk-reads that job's audit (the Cockpit job/loop view warming its IndexedDB cache, or an MCP `get_job_summary`) makes the orchestrator build a ~½ GB JSON response, blow past its **2 GiB** memory limit, and get `OOMKilled`. Because the orchestrator is the dispatch/heartbeat/status authority, one runaway job's audit view **takes down the entire control plane** (both replicas, CrashLoopBackOff) and wedges dispatch for *all* jobs and sessions cluster-wide. Self-reinforcing: the client retries → fresh pod → OOM again.
**Component:** agent job-metadata build (`src/api/dual_app.py:461-492` + mirror `src/api/app.py:503`) · audit read/serve (`orchestrator/database/audit_store.py:356 get_job_audit_bulk`, `:85 _STITCH_CORE`, `:72 jsonb codec`, `_audit_row_to_doc`) · endpoint (`orchestrator/main.py:11611 /api/jobs/{job_id}/audit/bulk`) · orchestrator memory limit (`helm/templates/orchestrator/deployment.yaml`, `2Gi`)
**Related:** memory topics `project_self_improvement_loop`, `project_loop_repo_compounding` (loop jobs run the most steps → most exposed), `project_cross_pod_checkpointer_d3` (the separate checkpoint-blob bloat, ruled out below), `project_session_multimodal_pdf_context_explosion` (sibling "JSON explodes in memory" failure class) · **companion incident (why this same job paused):** `loop_job_workspace_lost_wedged_in_recovery.md` · sibling incident same job-family/day: `docs/issues/loop_ran_codex_spark_not_selected_model_then_hung_on_cooldown.md` · **spun off from the 2026-06-29 audit of this same job:** `snapshot_restore_dead_for_jobs.md` (open — likely superseded by PVC-reattach), `agent_workspace_pod_resource_headroom.md` (revised recommendation for build/test jobs), `workspace_reattach_ephemeral_ip_reconnect_churn.md` (FIXED `7fb9e9e2`)

---

## Symptom

The job `19707fa1` appears "failed" to the operator. In reality it is `paused` — its workspace was lost mid-run and it is wedged in `workspace_unavailable` recovery (see companion doc `loop_job_workspace_lost_wedged_in_recovery.md`) — and the *actual* outage is that **both orchestrator replicas are in `CrashLoopBackOff`, `OOMKilled` (exit 137)** against the 2 GiB limit. While dying they fail the liveness/readiness probes (`connection refused` / `context deadline exceeded`), so the public API and MCP endpoint return intermittent `502 Bad Gateway` / "token expired". The orchestrator was healthy for ~8 h, then began OOM-looping ~2 h before observation — i.e. some time *after* the job had already paused (it was not the job's own execution that OOMed; it was something *reading* the job).

Memory profile is the tell: each fresh pod sits **flat at ~190 MiB** and only spikes (faster than `kubectl top`'s resolution) when the job's bulk audit is actually fetched. Idle → stable; someone opens the job/loop view → OOM.

## TL;DR — one bloat, one amplifier

| | Defect | Effect |
|---|---|---|
| **A (root cause, write-side)** | The per-job `metadata` dict — built once at dispatch and **including the full ~127 kB `resolved_config` blob** — is recorded on **every** `agent_audit` `pre` event. | This job's audit `metadata` is **476 MB of the same config duplicated ~3,600×**. The audit *content* (`payload`) is trivial: **292 bytes/entry**. |
| **B (amplifier, read-side)** | `/api/jobs/{id}/audit/bulk` selects up to 5,000 rows **including `metadata`**, `conn.fetch()`-materializes them all, parses each `metadata` to a Python dict (jsonb codec = `json.loads`), then FastAPI re-serializes the list to JSON. | One call returns **all ~3,600 config-bearing rows = ~476 MB JSON** → parsed object graph (several× overhead) + a re-serialized copy coexist → **2–3 GB transient** in a **2 GiB** container → `OOMKilled`. |

A makes the data half a gig; B loads and re-serializes the whole half-gig at once. Remove the config from audit metadata **or** stop shipping `metadata` in the bulk projection and the OOM is gone.

## Runtime evidence (all measured on the live cluster, 2026-06-27)

**The job (Postgres `jobs` row):**

| field | value |
|---|---|
| status / assigned_agent_id | `paused` / `NULL` (workspace lost → wedged in `workspace_unavailable` recovery; see companion doc) |
| freeze_data / error_message | **empty / empty** (did not self-freeze or error) |
| lifespan | `13:39:21Z → 17:39:43Z` = **4 h 00 m 22 s** |
| total_requests / total_tokens_used | `0` / `0` (accounting never finalized — consistent with orphan, not clean stop) |
| resolved_config size | **126 kB** |
| context size | 2,692 bytes |

**Orchestrator pods:** `srw-orchestrator-678cf75b89-{d9h24,pnflt}`, both `Last State: Terminated, Reason: OOMKilled, Exit Code 137`, `RestartCount 5`. `resources.requests.memory=256Mi`, `limits.memory=2Gi`. Both replicas' **last logged request before each OOM was for this job** (`/audit`, `/version`, `/loop`); the fatal request itself never logs (process is SIGKILL'd mid-handler).

**Where the bytes are — `agent_audit` for this job (`srw-auditdb`, 8,808 rows):**

| column | avg | max | sum |
|---|---|---|---|
| `payload` | 292 B | 1,288 B | ~2.5 MB |
| `metadata` | **130 kB** | **130 kB** | **476 MB** |

`metadata` is uniform (avg == max) because it's a constant blob stamped on every row. Its largest key:

```
metadata_key      size
resolved_config   127 kB      ← the entire job config, on every row
kickoff_message   1.7 kB
repositories      267 B
... everything else < 300 B
```

**Not the cause — `checkpoint_blobs` (main DB, thread `19707fa1`):** 4,915 rows / **447 MB** on disk, but `messages` channel is **2,451 separate version-snapshots**, max single blob **551 kB**. Restoring a checkpoint loads only the *latest* version per channel ≈ **600 kB**, and the orchestrator only ever **deletes** these (`postgres.py:1045 delete_checkpoint_thread`), never deserializes them. This is a real *storage*-bloat issue but is **not** the OOM trigger.

**Context — `llm_requests` for this job:** 1,202 rows / 433 MB (avg `request` 237 kB, max 521 kB). Not on the `/audit/bulk` path, but the same "full context per row" storage pattern; the month partition `llm_requests_p2026_06` is **2.1 GB**, `agent_audit_p2026_06` is **1.8 GB** (all jobs).

## Root cause

### A — write side: config duplicated into every audit row

`src/api/dual_app.py:461-492` builds one `metadata` dict at job start and hands it to the agent:

```python
metadata: Dict[str, Any] = {"description": description}
...
if resolved_config:
    metadata["resolved_config"] = resolved_config      # ← 127 kB, line 482
...
final_state = await _agent.process_job(job_id, metadata, stream=True)   # line 502
```

`resolved_config` legitimately belongs in *this* dict: the agent reads it **once at startup** to hydrate its config from the blob (`src/agent.py:1438` `load_config_from_resolved(metadata["resolved_config"])`). The defect is that the agent's audit writer then persists this same job-level `metadata` onto **every** `pre` event, so a job with ~3,600 steps stores the 127 kB config ~3,600 times. Audit metadata and "config the agent needs to boot" are conflated.

### B — read side: bulk endpoint ships and re-serializes all of it

`GET /api/jobs/{id}/audit/bulk` → `audit_store.get_job_audit_bulk()` (`audit_store.py:356`):

```python
rows = await conn.fetch(_STITCH_CORE + " ORDER BY f.id ASC OFFSET $2 LIMIT $3", job_id, offset, limit)
entries = [_audit_row_to_doc(r) for r in rows]   # builds N docs in memory
return {"entries": entries, ...}                  # FastAPI re-serializes the whole list
```

- `_STITCH_CORE` (`audit_store.py:85`) selects `f.metadata` for every row.
- The jsonb codec (`audit_store.py:72`, `decoder=json.loads`) parses each 127 kB `metadata` into a **Python dict tree** on read (object overhead is several× the JSON text).
- `_audit_row_to_doc` copies `metadata` straight onto the wire doc (`if r["metadata"] is not None: doc["metadata"] = r["metadata"]`).
- `conn.fetch()` is **not** a cursor — all ~3,600 rows materialize at once. With `hasMore` false (3,600 < 5,000 limit), one call returns the entire 476 MB.

So at peak the process holds: the fetched rows (~476 MB JSON parsed into dict trees) **+** the `entries` list of docs referencing them **+** the FastAPI JSON re-serialization buffer (~476 MB bytes) **+** the ~190 MB baseline. Several hundred-MB-to-GB copies coexist → **2–3 GB → OOMKilled**.

### Why "it's just JSON, Wikipedia is 100 GB" is a false comparison

The data at rest is not the problem. Wikipedia's 100 GB is *compressed text on disk*. This is ~½ GB of JSON **decoded into a live Python object graph and then re-serialized**, several copies resident simultaneously, inside a 2 GiB cgroup. The multiplier is `(config duplicated per row) × (fetch-all materialization) × (Python dict/list/str overhead) × (re-serialize)`, not the byte count of the audit content (which is 292 bytes/entry).

## Blast radius

- It's the **whole orchestrator**, not one job: dispatch, heartbeats, lifecycle reconciler, sessions, MCP — all down while it crash-loops.
- **Loop / self-improvement jobs are the most exposed** by design: many iterations and long DEVELOPER phases ⇒ thousands of steps ⇒ the worst duplication. Directly threatens `project_self_improvement_loop` / `project_loop_repo_compounding`.
- The trigger is *viewing* a big job, so it fires exactly when an operator opens the job to see what went wrong — and keeps the pod down via retry while they're looking.

## Fix

**1. Primary (A) — stop persisting heavy job-level blobs into per-row audit metadata.** ✅ **Done** (`7ea0d798`, deployed to dev). Separate "config the agent boots from" from "audit metadata". Strip a denylist (`resolved_config`, `config_override`, `datasources`, `repositories`) from whatever job-level `metadata` the agent records per audit event — store them **once per job** (they already live in `jobs.resolved_config` etc.). This alone drops the job's audit from ~480 MB to ~2.5 MB.

**2. Defense-in-depth (B) — don't ship `metadata` from the bulk/list path.** ✅ **Done** — went further than this option: rather than just slimming the bulk projection, the lean `_STITCH_LEAN` projection (`lean=true` on `get_job_audit` / `/audit`) drops `metadata` + `tool.arguments` + `state` + `error.traceback`, **and the bulk endpoints were deleted outright** once their last consumer (MCP) moved to the lean/paged path (2026-06-29). The per-page ceiling is now 200 (was 5,000).

**3. Capacity (mitigation, not a fix).** `requests.memory=256Mi` / `limits.memory=2Gi` is low for an ~11.5k-line FastAPI app that serves bulk endpoints. Raise it to widen the margin — but without #1/#2 a large-enough job still OOMs, so this is not sufficient alone. *(Not pursued — #1+#2 removed the OOM vector; capacity left as-is.)*

**4. Cleanup / backfill.** ⏳ **Partial** — run on local k3d only, NOT dev/prod. Existing rows already carry the duplicated config (partitions are GB-scale). Either a one-time `UPDATE agent_audit SET metadata = metadata - 'resolved_config' - 'config_override' - 'datasources' - 'repositories'` (per live partition), or let monthly partition rotation age it out. **No longer urgent** now that #2 removed every path that bulk-reads these rows — it's a disk-space cleanup, not a crash fix. Separately, prune this thread's 447 MB checkpoint blobs (cancelling the job fires `delete_checkpoint_thread`).

## Immediate ops mitigation (before code fix)

- **Cancel / terminate job `19707fa1`** — it's `paused`/orphaned, unresumable at reasonable cost, and the dispatcher keeps eyeing it. Cancelling also fires `delete_checkpoint_thread` (reclaims 447 MB).
- Close the Cockpit tab polling the job/loop view (removes the trigger).
- Optionally bump the orchestrator memory limit to stabilize the control plane while #1/#2 land.

## Secondary finding (not the OOM) — now its own issue

The `ide_settings` sync serially SSH-probes dozens of stale, never-evicted workspace endpoints (`10.42.x.x:30022`, "No route to host", ~3 s each) — cleanup debt, unrelated to this OOM. **Promoted to its own issue:** `ide_settings_sweeper_probes_stale_workspace_endpoints.md`.

## Verification commands (reproduce the measurements)

```bash
CTX=main NS=superhuman-remote-worker JOB=19707fa1-1788-4eda-a296-8b108429b108

# OOM signal
kubectl --context=$CTX -n $NS get pods -l app.kubernetes.io/component=orchestrator \
  -o custom-columns='NAME:.metadata.name,RESTARTS:.status.containerStatuses[0].restartCount,LASTSTATE:.status.containerStatuses[0].lastState.terminated.reason'

# The bloat: per-row metadata vs payload (auditdb)
kubectl --context=$CTX -n $NS exec -i srw-auditdb-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
SELECT count(*),
  pg_size_pretty(avg(length(payload::text))::bigint)  AS avg_payload,
  pg_size_pretty(avg(length(metadata::text))::bigint) AS avg_metadata,
  pg_size_pretty(sum(length(metadata::text))::bigint) AS sum_metadata
FROM agent_audit WHERE job_id='$JOB';
SQL

# What's in the 130 kB metadata
kubectl --context=$CTX -n $NS exec -i srw-auditdb-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<SQL
SELECT k, pg_size_pretty(length((metadata->k)::text)::bigint)
FROM (SELECT metadata FROM agent_audit WHERE job_id='$JOB' AND metadata IS NOT NULL ORDER BY id DESC LIMIT 1) s,
     LATERAL jsonb_object_keys(metadata) k ORDER BY length((metadata->k)::text) DESC LIMIT 5;
SQL
```
