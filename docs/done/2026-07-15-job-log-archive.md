---
tags:
  - feature
  - logging
  - debugging
  - post-mortem
related:
  - "[[centralized_logging]]"
  - "[[postgres_audit_store_implementation]]"
aliases:
  - job log archive
  - post-mortem logs
  - preserve pod logs
---

# Job Log Archive (post-mortem log.txt)

> Captured 2026-07-15. The lean replacement for the user-facing half of
> [[centralized_logging]]: that doc designs a full continuous-shipping
> pipeline (Alloy → Loki → Grafana) for *ops* observability and stays
> **parked**; this one solves the actual triggering scenario in ~200 lines
> with no new infrastructure. **This doc is the as-built authority.**

**Status: IMPLEMENTED + k3d-verified end-to-end 2026-07-15 — uncommitted**
(awaiting commit + the normal CI/CD → Fleet → homelab deploy; the MCP
`get_thread_log` tool rides the next MCP image build). Decisions settled by
the user: **jobs AND sessions** both in scope (capture is pod-generic; threads
stamp `metadata.log_archive_keys`, jobs stamp `context.log_archive_keys`);
access is **API + MCP only** (`get_job_log` gains an archive fallback, new
`get_thread_log`) — no cockpit surface.

## The scenario it solves

A user runs a job on their SRW instance. The job crashes. They notice hours
later and want to see what happened — but the agent pod has been reaped, and
its logs died with it. All they need is the log, as a plain file they can read
in an IDE or hand to an agent. No dashboards, no query language.

## As built

The whole feature hangs off the one choke point every pod-deletion path shares
— `AgentProvisioner.delete_agent_pod` — plus a fallback in the existing read
endpoint. ~620 lines across 9 files.

**Capture (`orchestrator/services/agent_provisioner.py`).**
`_archive_pod_logs()` is called at the top of `delete_agent_pod`, so every
deletion path — reap sweep, idle scale-down, session end, workspace
suspension, orphan cleanup — archives before the pod (and the log the kubelet
holds for it) disappears. It reads the full `agent` container log via
`read_namespaced_pod_log` (no `tail_lines`, 50 MB `limit_bytes` cap), plus a
best-effort `previous=True` read for the pre-restart/pre-OOM incarnation that
holds the crash that *caused* a restart. Exception-safe by contract: an
archive failure logs and returns, never blocking the delete.

**Store (`snapshot_service.put_blob`).** New explicit-key byte upload
(complements the content-addressed `save_blob`) → the existing snapshot bucket
at `agent_logs/<pod_name>/<utc-ts>.log` (+ `<utc-ts>.previous.log` when a
previous incarnation exists). Timestamped key makes pod-name reuse harmless.

**Stamp at capture time, not resolve at read time
(`_stamp_log_archive_keys`).** One pod serves several jobs sequentially and
`jobs.assigned_agent_id` is `ON DELETE SET NULL`, so resolving pod→job at read
time is fragile. Instead, at capture time the keys are appended to:
- `threads.metadata.log_archive_keys` — via the pod's `srw.io/thread-id` label
  (session pods).
- `jobs.context.log_archive_keys` — via `agents.hostname` = pod name →
  `assigned_agent_id` (worker pods carry no job label; one pod, many jobs).

Both use an atomic JSONB append (`COALESCE(... ,'[]') || $2::jsonb`).

**Read path (`orchestrator/main.py`).**
- `GET /api/jobs/{id}/logs` — unchanged when the live per-job file exists
  (compose/dev shared volume); otherwise falls back to the S3 archive named by
  `context.log_archive_keys`, **scoped to the job's id-tagged lines** so a
  multi-job worker-pod log disaggregates cleanly. `?raw=true` skips
  filtering/tailing and streams the whole pod log as `text/plain` for IDE
  reading or agent handoff. The level filter now matches JSON lines too, not
  just the text formatter.
- `GET /api/persistent/threads/{id}/logs` — new, owner-only, same shape,
  served from the thread's archive keys.
- Shared helpers: `_read_archived_agent_log` (stitch the S3 blobs),
  `_scope_archived_lines` (id-filter, whole-log fallback when nothing matches
  — e.g. `LOG_FORMAT=text` clusters), `_filter_log_lines` (level/grep, text +
  JSON aware).

**MCP (`orchestrator/mcp/{client,server}.py`, `services/formatters.py`).**
`get_job_log` transparently serves archived logs; new `get_thread_log` for
sessions. Formatter headers mark archived logs ("archived — pod is gone,
served from S3").

**Retention.** `DELETE /api/jobs/{id}` and permanent thread deletion delete
the archive objects. Both re-read the row first, because the workspace/pod
teardown that runs *inside* those same handlers is what stamps the freshest
keys. A bucket lifecycle rule on `agent_logs/` (e.g. 90d) is the backstop for
any object a delete path misses.

## Why capture-at-deletion is the right mechanism here

[[centralized_logging]] argues "teardown-pull is the wrong mechanism" — but
its objections attack **SSH-dependent** pulls (unreachable workspace pods) and
streams with **no per-job teardown** (orchestrator, vm-controller). Neither
applies here: `read_namespaced_pod_log` goes through the k8s API and works on
crashed, OOM-killed, and completed containers — the kubelet keeps the log
until the *pod object* is deleted, and we control that deletion. Capturing
immediately before our own `delete_agent_pod` is exactly the right hook for
"job crashed, user reads the log later."

## Starting point (why this was ~200 lines, not a pipeline)

Most of the machinery already existed before this feature:
- **Per-job log file** — every job attaches a `FileHandler` writing
  `workspace/logs/job_<id>.log` (full JSON, job_id-tagged from Slice 0 of
  [[centralized_logging]]); `src/api/dual_app.py`.
- **Read API + MCP tool** — `GET /api/jobs/{id}/logs` with tail/grep/level
  filtering, and `get_job_log` on top.
- **Reap-time capture** — `_capture_agent_logs_before_reap` already pulled pod
  logs via the k8s API, but truncated to 500 lines and echoed into the
  orchestrator's own ephemeral stdout (a diagnostic, not an archive).
- **S3 wiring** — `snapshot_service` (boto3, `S3_ENDPOINT`/`S3_BUCKET`,
  auto-bucket-create).

The only real gap was durability: the per-job file lived on the *agent pod's*
disk while the read API read the *orchestrator's* path (a compose-era
shared-volume leftover), so on k8s the file 404'd the moment the pod was gone.
This feature closes that gap and generalizes capture to every deletion path.

## Verification (k3d, 2026-07-15)

Drove a real worker job (`3de6989f`) that spawned a critic subjob on a second
pod, on a hot-synced orchestrator:
1. Both pods were reaped by the reconciler → two `Archived agent pod logs`
   events; parent and child were each stamped with **their own** pod's key
   (multi-pod attribution via `agents.hostname` confirmed).
2. `GET /api/jobs/{id}/logs` returned `archived: true` with 347 job-scoped
   lines out of a 1 780-line shared pod log **after the pod was gone**;
   `?grep=` filtering and `?raw=true` (264 KB full pod log) both worked.
3. Thread endpoint verified against a stamped thread (`archived: true`;
   whole-log fallback exercised — this cluster runs `LOG_FORMAT=text`, and
   scoping still worked because the agent prefixes each line with the job id).
4. Retention: deleting the child job removed its 190 KB S3 object (confirmed
   via `head_object` before/after).

Unit coverage: `tests/test_agent_provisioner.py::TestArchivePodLogs` (capture,
per-pod stamping, upload-failure = no stamp, archive-failure never blocks
delete) + `tests/test_job_log_archive.py` (read-path helpers, formatters). 81
tests green; ruff + format clean.

**Deploy/ops note:** hot-syncing `main.py` into a running pod for verification
requires temporarily relaxing the orchestrator liveness `failureThreshold` — a
uvicorn `--reload` cycle of the ~23k-line `main.py` exceeds the default
3×10 s probe tolerance, and a probe-triggered restart reverts `kubectl cp`'d
files. Irrelevant to the real CI/CD path (fresh image, no reload); only a
gotcha for in-pod iteration.

## Accepted gaps (explicitly not solved)

- **Hard node failure** — kubelet gone, log gone. Rare; acceptable for a
  debugging feature.
- **Kubelet rotation** — k3s caps container logs (~10 Mi/file default); a very
  long chatty job can lose its earliest lines. Raise
  `--kubelet-arg container-log-max-size` if it ever bites.
- **Pods deleted outside our code path** — manual `kubectl delete`, node-drain
  eviction GC. Nothing captures those.
- **Orchestrator / vm-controller streams** — ops concerns, not the user
  post-mortem; the semantic trail is already durable in the audit store
  (`agent_audit`, `llm_requests`, `chat_history`). If cross-pod ops forensics
  becomes a daily pain, un-park [[centralized_logging]].
