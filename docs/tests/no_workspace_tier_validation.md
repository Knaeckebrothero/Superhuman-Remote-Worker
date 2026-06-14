# No-Workspace Agent Tier — Validation Plan & Remaining Tests

Handoff doc (2026-06-12). Captures what's been built/deployed for the `virtual`
and `none` workspace tiers (`docs/features/no_workspace_agent_mode.md`), what's
confirmed, the current blocker, and the remaining test checklist with recipes —
so testing can resume after a context compaction.

---

## 1. Status snapshot

**Deployed to dev (`develop` → CI → Fleet):**
- `a6a888ac` `refactor(s3-creds): discrete VIRTUAL_WORKSPACE_S3_* vars + snapshot secret rename` → `sha-a6a888a` (deploy bump `342ed9dd`). *(Local SHA was `dee381c4`; rewritten by the push/ruff workflow.)*
- `41921f53` `Skip scholar/critic/curator subjobs for lite workspace backends` → `sha-41921f5` (deploy bump `b75aa34b`).
- Working tree clean. Both commits live on dev.

**Confirmed working:**
- ✅ Orchestrator boots clean post-deploy (serves API, providers `ready`, `create_job` works) → the renamed snapshot env + discrete `VIRTUAL_WORKSPACE_S3_*` did **not** break startup.
- ✅ **Scholar-skip fix works.** Retest job `578b6fe7-98f9-4165-8c51-6c9e1f332040` (backend=virtual) did **not** spawn a "Research phase for:" scholar child — it went straight to virtual dispatch (vs. the earlier `fc1ccee9` which decomposed into a full-workspace scholar `69e29e3d`).

**BLOCKED / not yet validated:**
- ❌ **Virtual job dispatch fails in ~3s** (job `578b6fe7`: `failed`, audit 0, no log file). Root cause not yet pinned — see §3.
- ❓ Real-S3 write to `srw-workspaces/jobs/<id>/` — **never validated** (blocked by the dispatch failure; only ever ran against the `memory` store on k3d).
- ❓ Snapshot rename **functionally** correct — orchestrator didn't crash (necessary) but `snapshot_service` *silently disables* if creds are wrong; not positively confirmed.
- ❓ `none` backend dispatch — untested live.
- ❓ Critic/curator skip — only unit-tested (shared `_is_lite_config_override` helper); no live critic/curator run observed.
- ❓ Cockpit tier picker on **dev** — only validated on local k3d earlier.
- ❓ Persistent **session** (thread) virtual path — never driven live (only jobs).

---

## 2. Provisioning state (verify before testing)

Done by the user on the dev cluster's MinIO + Vault:
- Bucket **`srw-workspaces`** created on the same MinIO that backs snapshots (`minio.minio.svc`). Plain bucket — no versioning, no object-lock.
- A **dedicated, bucket-scoped** MinIO key for the agent (separate from the snapshot key). Policy = get/put/delete objects + list on `srw-workspaces` only (NOT `s3:*`):
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation","s3:ListBucketMultipartUploads"],"Resource":"arn:aws:s3:::srw-workspaces"},
      {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":"arn:aws:s3:::srw-workspaces/*"}
    ]
  }
  ```
- **Vault** (dev bundle, synced via ESO `dataFrom: extract` → K8s Secret keys == Vault field names verbatim):
  - Added: `VIRTUAL_WORKSPACE_S3_ACCESS_KEY_ID`, `VIRTUAL_WORKSPACE_S3_SECRET_ACCESS_KEY` (the scoped key).
  - Renamed: `S3_ACCESS_KEY`→`SNAPSHOT_S3_ACCESS_KEY_ID`, `S3_SECRET_KEY`→`SNAPSHOT_S3_SECRET_ACCESS_KEY`.

**⚠️ Likely NOT done — the prime suspect for the blocker:** the non-secret **Fleet dev overlay** values (these come from chart values, NOT Vault):
```yaml
virtualWorkspace:
  rclone:
    type: s3            # ← empty ("") DISABLES the tier → virtual jobs fail at dispatch
    root: srw-workspaces
  s3:
    endpoint: "http://<dev-minio-endpoint>:9000"   # region/provider default us-east-1/Minio
```
If `virtualWorkspace.rclone.type` is empty on dev, `_virtual_workspace_rclone_spec()` returns `None` and dispatch raises `LiteWorkspaceConfigError`. **Check this first.**

**OWED (separate, for later):** before the chart reaches the **prod** cut, prod-private's Vault needs the same `S3_*`→`SNAPSHOT_S3_*` rename, or prod snapshots break on deploy.

---

## 3. IMMEDIATE BLOCKER — diagnose the ~3s virtual dispatch failure

Two fast-fail causes at dispatch; distinguish them:

| Cause | Mechanism | Fix |
|---|---|---|
| **(a) Tier disabled** | `VIRTUAL_WORKSPACE_RCLONE_TYPE` empty → `_inject_lite_workspace_config` raises `LiteWorkspaceConfigError` | Set the Fleet values in §2, redeploy |
| **(b) Repository datasource** | Default project attaches a `repository` datasource; lite tiers reject those at dispatch (§7) | Use a project/job with no repo datasource, or detach it |

**Diagnostic steps (in order):**
1. **Check the Fleet dev overlay** for `virtualWorkspace.rclone.type/root` + `virtualWorkspace.s3.endpoint`. If `type` isn't `s3` → cause (a), set them + redeploy. *(Fastest check.)*
2. **Differential `none` test** — create a `none`-backend job (recipe in §4). `none` does **not** need the object store, but **both** lite tiers reject repo datasources:
   - `none` **dispatches OK** → the virtual failure is object-store config → cause **(a)**.
   - `none` **also fails at dispatch** → cause **(b)** (repo datasource) — check `list_project_datasources`.
3. **Confirm the error string** — MCP couldn't surface it (`get_job` hides `error_message`; audit trail is empty for dispatch-failed jobs). To read it: orchestrator pod logs (`kubectl logs <orchestrator>` grep the failed job id / `LiteWorkspaceConfigError`), or check the orchestrator env `VIRTUAL_WORKSPACE_RCLONE_TYPE`.

---

## 4. Remaining test checklist

Use the orchestrator MCP (points at the **remote dev** cluster). Agents cold-start ~75–90s (dev pool `minAgents: 0`).

### T1 — Virtual job E2E → real S3 write *(headline; currently blocked by §3)*
- **Run:** `create_job(description="…write notes/hello.md…", config_override={"workspace":{"backend":"virtual"}})`. Wait ~80s.
- **Success signals:**
  - No "Research phase for:" child in `list_jobs` (scholar skipped). ✅ already confirmed.
  - `get_job_log(grep="backend")` shows `Lite workspace backend ready (backend=virtual, no workspace pod)`.
  - `get_job_log(grep="rclone")` shows writes with **no** `AccessDenied` / `BucketNotFound` / create-bucket errors.
  - `get_workspace_overview` lists `notes/hello.md`; bytes land under `srw-workspaces/jobs/<id>/` (verify in MinIO console/`mc ls`).
- **If rclone `AccessDenied` on first write:** the scoped key can't create the bucket — confirm the bucket exists and that `no_check_bucket=true` is in effect (it's baked into `_virtual_workspace_rclone_spec()`; the policy intentionally omits `CreateBucket`).

### T2 — `none` backend dispatch
- **Run:** `create_job(config_override={"workspace":{"backend":"none"}}, description="trivial: just say done")`.
- **Success:** dispatches to a single agent (no workspace pod, no scholar); file tools are absent (capability-gated). Doubles as the §3 differential test.

### T3 — Snapshot rename is functional (not silently disabled)
- The rename is the risky live-secret change. Orchestrator-health is necessary but **not sufficient**.
- **Confirm:** orchestrator logs show `Snapshot service ready: endpoint=… bucket=srw-snapshots` (NOT `… S3_ENDPOINT not set — disabled` / connection failed). Or run a **sandbox** job to completion and confirm a snapshot is captured (snapshots fire on workspace teardown).
- If disabled → the `SNAPSHOT_S3_ACCESS_KEY_ID`/`_SECRET_ACCESS_KEY` Vault fields don't match what `snapshot_service.py` reads.

### T4 — Critic/curator skip for lite (live)
- Only unit-tested so far. To exercise live: run a `virtual` job through to completion (needs T1 unblocked) with a config that has verification/curation enabled; confirm no critic/curator subjob spawns and logs show `Critic skipped …`/`Curation skipped …`.
- Low risk (same `_is_lite_config_override` gate as the confirmed scholar skip).

### T5 — Cockpit tier picker on dev
- Drive the dev cockpit (Playwright) → Create Job → Advanced → Workspace → select `virtual`/`none`; confirm the dependent controls grey out and the emitted `config_override` is clean. (Validated on local k3d already; re-confirm on dev image.)

### T6 — Persistent session (thread) virtual path
- Never driven live. Create a persistent thread with `workspace.backend: virtual`; confirm it attaches as a single agent (no workspace pod) and writes to `threads/<id>/` in `srw-workspaces`. The session seam shares `_inject_lite_workspace_config`, so it should behave like jobs.
- Known cosmetic gap (§12 #8): cockpit still shows code-server/workspace links for lite sessions.

### T7 — Capability gating sanity (likely already covered by S3 slice)
- For a `virtual` job, confirm shell/git/browser_direct tools are NOT bound and file/web tools ARE; for `none`, file tools also dropped. (Validated on k3d per design doc §12 #4/#6; re-confirm on dev if convenient.)

---

## 5. Testing recipes, tooling & gotchas

- **MCP target:** the orchestrator MCP hits the **remote dev** cluster (not local k3d). Internal-key drives it.
- **Pacing:** agents cold-start ~75–90s (`minAgents: 0`). Use a background `sleep` then inspect; don't rapid-poll.
- **Job inspection:** `get_job_summary` (status + workspace + last tool calls), `get_job_log(grep=…, lines=…)` (grep is a single case-insensitive substring — one term per call), `list_jobs`, `get_workspace_overview`.
- **`get_job_log` "Log file not found"** = no agent pod ran yet (still `waiting`, or failed at dispatch). Not an error per se.
- **`cancel_job` returns "Not authenticated"** via MCP (reads + `create_job` work; cancel needs user-session auth). Cancel test jobs from the **cockpit** instead.
- **Dispatch-failed jobs have empty audit trails** and `get_job` doesn't surface `error_message` — read the orchestrator pod logs for the failure reason.
- **Output masking:** `Bash`/`rg` tool output redacts some identifiers (e.g. "scholar"→"ln", secret-ish strings). Use the `Read` tool for true file contents.
- **Scholar decomposition (now fixed):** the default config used to spawn a "Research phase for: …" scholar subjob that ran in a **full** workspace (shell/git/leftover repos) and dropped the parent's `workspace.backend`. `41921f53` skips scholar/critic/curator for lite tiers.

---

## 6. Key code & config references

- `orchestrator/main.py`:
  - `_virtual_workspace_rclone_spec()` — builds the rclone spec from discrete `VIRTUAL_WORKSPACE_S3_*` env (`no_check_bucket=true` baked in); returns `None` if `…_RCLONE_TYPE` empty.
  - `_inject_lite_workspace_config()` — raises `LiteWorkspaceConfigError` for `virtual` when the spec is `None`; strips mounts for `none`; forces `git_versioning` off.
  - `_is_lite_config_override()` — the subjob gate (`backend in LITE_BACKENDS` = `{virtual, none}`).
  - `_spawn_scholar_subjob` / `_trigger_verification_on_complete` (critic) / `_trigger_curation_final_pass` (curator) — each guarded by `_is_lite_config_override`.
  - `_graft_subjob_output()` — Gitea-branch graft (why lite subjobs are incompatible; an object-store handoff is the v2 path).
- `orchestrator/services/snapshot_service.py` — reads `SNAPSHOT_S3_ACCESS_KEY_ID`/`_SECRET_ACCESS_KEY` (boto3).
- `helm/values.yaml` — `virtualWorkspace.rclone.{type,root}` + `virtualWorkspace.s3.{endpoint,region,provider}`; `s3:` snapshot block.
- `helm/templates/configmap.yaml` + `helm/templates/orchestrator/deployment.yaml` — env wiring (discrete vars + `SNAPSHOT_S3_*`).
- `tests/test_lite_workspace_dispatch.py` — unit coverage (`TestVirtualWorkspaceRcloneSpec`, `TestLiteSubjobGating`, dispatch/roundtrip).
- `src/core/backends/{factory,rclone,virtual,scratch,object_store}.py` — agent-side lite backends. `LITE_BACKENDS` defined in `factory.py`.

---

## 7. Deferred (NOT in scope to test now)

- **v2 — lite-compatible scholar/critic handoff:** object-store copy instead of git graft so virtual jobs can regain a research/verification phase. (Currently skipped for lite.)
- **v2 — change tracking & cloud diff/approve-revert** (no_workspace §8/§10).
- **v3 python executor, v4 browser pool, v5 user-cloud surfaces** (roadmap §10).
- **Snapshot/objectstore env-var rename to full `<PURPOSE>_S3_*` parity** (only the secret creds were renamed; `S3_ENDPOINT/BUCKET/REGION`, `OBJECTSTORE_S3_*`/`NEXTCLOUD_S3_*` left as-is).
- **prod-private rollout** of the discrete-vars + snapshot rename (needs the Vault `S3_*`→`SNAPSHOT_S3_*` rename first).
