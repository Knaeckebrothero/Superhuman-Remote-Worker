---
tags:
  - feature
  - cloud-infrastructure
  - jobs
  - cockpit
  - product
aliases:
  - job project diff review
  - job-project workflow
  - diff review for jobs
  - export job to cloud
  - publish job results
related:
  - "[[cloud_collaboration_model]]"
  - "[[main_cloud_abstraction]]"
  - "[[project_cloud_folders]]"
  - "[[multi_tenancy]]"
---

# Job Cloud Workflow — Project-Folder Diff Review (+ loose-job export fallback)

> When a job is attached to a project, automatically mount the project's cloud folder into the agent's workspace at `projects/<project_folder_name>/`. The agent edits in place. On completion, the user sees a PR-style diff in Cockpit and decides accept (writes propagate back to the cloud folder) or reject (changes discarded). For loose jobs with no project, an "Export to shared folder" button — the existing shared-session-folder pattern, applied to jobs — gives the user a one-click path to materialize results.

**Status:** Partial implementation. **Mode B (loose-job shared-folder export) ✅ shipped + live-verified on dev cluster 2026-05-20.** Mode A (project-attached diff/accept) still in design; ready to start. **Replaces an earlier draft** of this doc that scoped the feature too narrowly (export button only — see §10).
**Triggered by:** Phase 5/6 of `docs/done/cloud_collaboration_model.md` got the broader shape right but was deferred as "too big." The earlier rewrite of this doc swung too far the other way (export-only). The actual user-friction lives in both halves: jobs can't *read* project folders without ugly WebDAV-datasource tooling, and they can't *write back* without producing a sibling-file that the user has to manually swap.
**Scope:** Project-attached jobs get auto-mount + diff/accept. Loose jobs get an export-to-shared-folder button. New orchestrator endpoints + new Cockpit diff-review UI + new agent-side job-start hook to mount the project folder before the agent's first turn. Reuses everything from the cloud-mirror foundation (Phase 1 service-account WebDAV transport, Phase 2.1 token-exchange for user-home destinations, Phase 3a collision-safe slugger, Gitea per-job snapshot plumbing).

## 0. Implementation log

### Slice 1 — Mode B end-to-end (✅ 2026-05-20)

Schema + backend + cockpit + cluster verification, all in one ship.

**Backend (Python):**
- `orchestrator/database/migrations/app/0017_jobs_cloud_diff.sql` — 4 new columns on `jobs`: `cloud_diff_baseline_commit TEXT`, `diff_status TEXT CHECK IN (pending|accepted|rejected|NULL)`, `exported_folder_handle TEXT`, `exported_at TIMESTAMPTZ`. Partial index `idx_jobs_diff_status_pending`. Migration applied at orchestrator startup 2026-05-20T08:51:53 in 12 ms.
- `orchestrator/database/postgres.py` — `get_job` / `get_jobs` / `get_visible_jobs` now surface the new fields. New helpers `update_job_cloud_diff(...)` and `update_job_exported_folder(job_id, *, handle)`.
- `orchestrator/services/cloud/base.py` — new `put_session_file(handle, *, path, content, content_type)` on the `MainCloudBackend` Protocol.
- `orchestrator/services/cloud/opencloud.py` + `nextcloud.py` — concrete `put_session_file` impls (MKCOL of parents one segment at a time, then PUT; bearer auth on OpenCloud, basic auth on Nextcloud).
- `orchestrator/services/gitea.py` — new `get_file_bytes(repo_name, file_path, ref)` — binary-safe sibling of `get_file_content` so PUT preserves PDF / image / archive bytes.
- `orchestrator/main.py` — `POST /api/jobs/{job_id}/export-to-shared-folder` endpoint. Gates: `status == 'completed'`, `!project_id` (Mode A would intercept), `!exported_folder_handle` (idempotency refuse). Cloud + Gitea readiness checks. Walks Gitea `output/` recursively via `list_contents` + `get_file_bytes`, calls `put_session_file` per file. Stamps `exported_folder_handle = handle.to_db()` + `exported_at = NOW()` on success. Returns `{job_id, files_copied, folder: {name, browser_url, webdav_url}}`.

**Frontend (Angular):**
- `cockpit/src/app/core/models/api.model.ts` — `Job` interface extended with the 4 new fields.
- `cockpit/src/app/core/services/api.service.ts` — `exportJobToSharedFolder(jobId)` with translated toast on success/failure.
- `cockpit/src/app/views/jobs/job-list.component.ts` — "Export to cloud" button next to Promote on rows where `status === 'completed' && !project_id && !exported_at`; replaced by a green "Exported" badge once `exported_at` is set. New `exportingJobIds` signal for the spinner state. On success the response's `browser_url` is opened in a new tab.
- `cockpit/src/assets/i18n/{en,de-DE}.json` — 7 new keys in each locale (`jobs.action.exportToCloud`, `jobs.action.exported`, `jobs.tooltip.exportToCloud`, `jobs.tooltip.alreadyExported`, `toasts.jobs.exportedToCloud`, `errors.jobs.exportFailed`). `i18n:check` parity-clean.

**Verified end-to-end on the dev cluster 2026-05-20:**
- Migration applied (orchestrator log).
- Endpoint registered (`POST /api/jobs/.../export-to-shared-folder` returns 401 unauth, not 404).
- Cockpit button rendered correctly: appears only on `completed && !project_id`; not shown on project-attached completed jobs.
- Real export run against the loose critic subjob `3b5f7c72-752b-4a4f-a982-1db04313fa33` → HTTP 200, `files_copied=0` (critic produces no `output/`), folder `job-3b5f7c72752b` created at `sessions/job-3b5f7c72752b` under the agent-home Space, shared with the calling user, deep-link opens cleanly in OpenCloud (Shares → Shared with me → job-3b5f7c72752b).
- DB stamped: `exported_folder_handle={backend:opencloud, native_id:sessions/job-3b5f7c72752b, vendor_meta:...}`, `exported_at=2026-05-20T09:19:54.546548Z`.
- UI state transition after refresh: button → green "Exported" badge. Re-export gated at the UI layer; API layer would 409 if forced.
- **Untested in this slice:** the actual WebDAV PUT loop (the test job had no `output/`). MKCOL + share path is exercised. PUT is straight-line `_client.put()` against the same authenticated transport that MKCOL succeeded on — low risk, but a fresh standalone job with files in `output/` would close the gap.

### Slice 2 — Mode A clone + diff capture (pending)

See §3.1–§3.3 and §7. Requires new agent-side mount helper, Gitea baseline-commit hook at job-start, diff computation + status transition at completion, two read endpoints. Status enum value `pending` is already accepted by the migration's CHECK constraint.

### Slice 3 — Mode A review + apply (pending)

See §3.4–§3.6 and §5. Accept/reject endpoints + Cockpit diff-review UI (Monaco).

## 1. Motivation

The cloud-mirror foundation made **sessions** first-class on cloud storage: agent edits a file, user sees it immediately, both surfaces stay in sync. **Jobs** are still a different animal:

- **Read access today: webdav datasource only.** A user who wants the agent to see their project folder during a job has to attach the project folder as a `webdav` datasource. That gives the agent a tools-based API for browsing/reading via WebDAV verbs — not filesystem access. The agent is much better at filesystem ops (`read_file`, `list_directory`, shell `grep`, etc.) than at remembering WebDAV verbs, so this is friction-without-payoff.
- **Write access today: doesn't exist.** When a job needs to *modify* a file in the project folder — "rewrite this report," "update this config," "regenerate this slide deck with new data" — the agent has no way to edit-in-place. It produces a new file in `output/`. The user gets `<project>/output/report.md` next to `<project>/report.md` and has to manually swap. This defeats the point of cloud integration for the most natural "agent does work on your stuff" use case.

The diff/PR model fits the actual workflow: agent gets the project folder mounted, edits in place, user reviews the full set of changes at the end like a pull request, accepts or rejects. Sessions do live mirror because the user is collaborating in real time; jobs need *review before landing* because the user is delegating and wants to see the result before it goes into their working files.

## 2. Architecture overview

Two modes depending on whether the job has a project attached:

### Mode A — Project-attached job (the primary workflow)

| Stage | What happens |
|---|---|
| Job-start | Orchestrator clones the project's cloud folder into the agent's workspace at `projects/<project_folder_name>/`. Snapshot the workspace state to Gitea as the diff baseline. |
| During job | Agent reads + writes freely. No cloud-side reflection. Changes accumulate in the workspace. |
| Job-completion | Compute diff: workspace state vs. Gitea baseline. File tree with statuses (modified / added / deleted). Job goes to `pending_review` instead of `completed`. |
| Cockpit review | File tree + per-file diff view. Two buttons: **Accept** (write changes back to cloud, job → `completed`) or **Reject** (discard, job → `completed` with no cloud changes). |
| Accept-time conflict | If cloud folder was modified externally during the job run, refuse with "cloud folder was modified externally — manual merge required." User reconciles manually, then accepts. |

### Mode B — Loose job (no project attached)

| Stage | What happens |
|---|---|
| Job-start | No project folder to clone — agent works in scratch workspace as today. |
| During job | Agent does its thing; `output/` accumulates artifacts. |
| Job-completion | Job goes straight to `completed` (no pending_review — there's nothing to review against). |
| Cockpit review | A button: **"Export to shared folder."** Click → orchestrator creates a shared cloud folder using the existing `ensure_session_folder` + `share_session_folder` pattern (`Jobs/<job-id-prefix>/` location convention), copies `output/` into it, returns a deep link. |
| Re-export | Refuse if already exported (same collision policy as the original export-button draft); user can delete the shared folder and re-export. |

Same backend WebDAV transport for both modes. Only the orchestrator's job-start hook and the post-completion flow differ.

## 3. Mode A in detail

### 3.1 Job-start: clone + baseline

The agent-dispatch payload already carries datasources, repos, etc. Add the project-folder mount as a new payload field:

```json
{
  "project_folder_mount": {
    "target_path": "projects/<slug>",
    "webdav_url": "<resolved per Phase 1 or 2.1 auth>",
    "auth": {
      "type": "keycloak_client_credentials" | "keycloak_user_impersonation",
      ...
    }
  }
}
```

Same payload shape as `cloud_sync.mounts[]` in Phase 1. The agent's job-start hook (parallel to `src/api/persistent_app.py:574` for repository datasources) clones the project folder into `projects/<slug>/` before the first turn. Default-project jobs: use Phase 2.1 token-exchange to authenticate as the project owner.

**Slug:** same `_slugify_mount_name` collision-safe helper that session mounts use (Phase 3a). One project = one mount = no collision possible within a single job, so suffix logic never fires — but use the same helper for symmetry.

**Baseline snapshot:** after the clone completes, the orchestrator instructs the agent (or runs directly against the Gitea backend) to commit the workspace state. This commit hash is the diff baseline, stored on the job (proposed column: `jobs.cloud_diff_baseline_commit TEXT`). Reuses the existing per-job Gitea repo plumbing — no new snapshot infrastructure.

### 3.2 During the job

Agent reads + writes through normal filesystem tools (`read_file`, `write_file`, shell). The clone is just a directory in the workspace; it has no special semantics from the agent's perspective. **No mid-job push to cloud** — the cloud folder stays untouched until accept.

This means a job that runs for an hour can do a thousand edits, and the user sees zero of them in the cloud until they accept. That's the point: review-before-landing.

### 3.3 Job-completion: diff computation

When the agent freezes the job (`job_complete` freeze type), orchestrator:

1. Takes another Gitea commit of the workspace final state (already happens for general workspace snapshot — reuse).
2. Runs `git diff <baseline_commit>..<final_commit> -- 'projects/<slug>/**'` (scoped to the mounted project folder; other workspace files are not part of the diff).
3. Stores the diff result on the job: file list with statuses, file paths, hash before/after. Per-file diff content is computed on-demand from the two commits — no need to store unified-diff blobs in Postgres.
4. Sets job status to `pending_review` instead of `completed`.

If the job has no diff (agent made no changes inside `projects/<slug>/`), it goes straight to `completed` — no review needed.

### 3.4 Cockpit review UX

Job-review tab gains a new section when status is `pending_review` with a non-empty diff:

```
┌──────────────────────────────────────────────┐
│ Pending review — 12 files changed            │
│                                              │
│ ┌── File tree ──────────────────────────────┐│
│ │ M  report.md                             ││
│ │ M  data/forecast.csv                     ││
│ │ +  charts/q3_revenue.png                 ││
│ │ +  charts/q3_costs.png                   ││
│ │ -  draft.md                              ││
│ │ ...                                       ││
│ └───────────────────────────────────────────┘│
│                                              │
│ ┌── Selected file: report.md ──────────────┐│
│ │ [Monaco diff editor view: left = old,    ││
│ │  right = new]                            ││
│ └───────────────────────────────────────────┘│
│                                              │
│ [ Accept all changes ]  [ Reject all ]       │
└──────────────────────────────────────────────┘
```

- **File tree:** flat list of changed paths, status indicator (M / + / −), click to load that file's diff.
- **Diff view:** Monaco diff editor (Cockpit doesn't have a diff renderer today — Monaco is the obvious choice; it's already a transitive dep via the IDE session view).
- **Accept all** / **Reject all** — v1 is all-or-nothing. Per-file accept is a v2 follow-up.

### 3.5 Accept: write-back

Clicking Accept:

1. Orchestrator detects external modification — re-PROPFIND the cloud folder, compare against `cloud_diff_baseline_commit`'s file inventory. If any file in the cloud was modified externally during the job, refuse the accept: return a `409 Conflict` with a clear message and which files diverged. Cockpit shows "Cloud folder was modified externally. Resolve manually before accepting." User options:
   - Pull the external changes into the workspace manually (download → reconcile → re-upload) and re-accept.
   - Reject the job and start over.
   - (v2: three-way merge UI.)
2. If no external mod, orchestrator walks the diff and applies each change to the cloud folder via existing WebDAV PUT/DELETE (service-account auth for non-default projects, Phase 2.1 token-exchange for default-project / home Space).
3. Job status → `completed`. The accepted Gitea commit becomes the new canonical state.
4. Cleanup: workspace can be retained or pruned per normal job-completion rules.

### 3.6 Reject: discard

Clicking Reject:

1. No cloud write-back.
2. Job status → `completed` with a flag indicating the diff was rejected (proposed column: `jobs.diff_rejected BOOLEAN DEFAULT FALSE`, or an enum on a single `jobs.diff_status` column).
3. Workspace cleanup as usual.
4. The Gitea commits stay around (cheap; they're the audit trail of "what the agent tried to do").

## 4. Mode B in detail (loose-job export)

> **Implementation note (✅ shipped 2026-05-20):** Sections 4.1–4.3 describe shipped behavior. Decisions made during implementation that differ from the design sketch are called out inline.

### 4.1 Folder provisioning

Loose jobs reuse the existing **shared-session-folder pattern** that Phase 4 preserved as a fallback for unattached sessions. **Decision (shipped):** kept the existing `ensure_session_folder` + `share_session_folder` primitives directly rather than introducing parallel `ensure_job_folder` / `share_job_folder` verbs. Naming separation wasn't worth the duplicated WebDAV+OCS code on both backends; folder-name disambiguation is sufficient.

Concretely:

- `ensure_session_folder(session_id=f"job-{job_id.replace('-', '')[:12]}")` → creates `sessions/job-<12-hex>/` under the agent-home Space (OpenCloud) or under the agent-service user's home (Nextcloud). The `job-` prefix is collision-free vs. thread-session folders (UUID hex never contains `j`/`b`).
- `ensure_user(sub, issuer, email, display_name, preferred_username)` to synchronously provision the cloud user record (avoids racing the JIT autoprovision task on first-time use).
- `share_session_folder(handle, resolved_user_id)` → grants the user access.
- Walk Gitea `output/` recursively (`list_contents` + per-dir recursion) and `get_file_bytes` each file. PUT each via the new `put_session_file(handle, path=entry_path, content=file_bytes)` Protocol method, which MKCOLs missing parent collections one segment at a time then PUTs the body.
- Stamp the job: `update_job_exported_folder(job_id, handle=folder_handle.to_db())` writes `exported_folder_handle` + `exported_at=NOW()`.

### 4.2 Endpoint contract

`POST /api/jobs/{job_id}/export-to-shared-folder`. Auth via `require_job_access`. Gates (all return 409 on violation):

1. `status != 'completed'` — only completed jobs can be exported.
2. `project_id` is set — project-attached jobs use Mode A.
3. `exported_folder_handle` already set — idempotency refuse; user must delete the cloud folder to re-export.

Pre-flight 503s: cloud backend not initialized; Gitea not initialized.

Success response:

```json
{
  "job_id": "<uuid>",
  "files_copied": <int>,
  "folder": {
    "name": "job-<12hex>",
    "browser_url": "https://cloud.<host>/f/<fileId>",
    "webdav_url":  "https://cloud.<host>/dav/spaces/<drive>%21<item>/sessions/job-<12hex>/"
  }
}
```

Failure paths: partial-copy failures (502, no stamp — safe to retry, MKCOL/PUT are idempotent against the same folder); `CloudBackendError` from any cloud call → 502; missing file from Gitea → 502.

### 4.3 Cockpit UX (shipped)

On the jobs list, rows with `status === 'completed' && !project_id`:

- **Not yet exported (`!exported_at`):** "Export to cloud" button next to the existing "Promote" button. Click → calls the endpoint, shows a translated toast (`Exported {{count}} file(s) to your cloud`), opens `folder.browser_url` in a new tab via `window.open`.
- **Already exported (`exported_at` set):** green "Exported" badge replaces the button.

**Decision (shipped):** the "Open in OpenCloud / Re-export" kebab from the earlier design sketch was NOT implemented. The badge is a terminal state. Discovery path for the existing folder is the OpenCloud Shares list, which the user already had to authenticate against on first export. If we see demand for in-cockpit "open exported folder" navigation, we can surface the persisted `browser_url` later — the handle is already on the row.

No diff view for loose jobs — there's nothing to diff against. This is purely a copy-out action.

## 5. API surface

### Mode A endpoints

| Verb | Path | Purpose |
|---|---|---|
| `GET` | `/api/jobs/{id}/diff` | Returns file-tree + statuses for a job in `pending_review`. No diff content. |
| `GET` | `/api/jobs/{id}/diff/<file_path>` | Returns the per-file diff (unified or before/after) for one file. Lazy-load. |
| `POST` | `/api/jobs/{id}/accept` | Apply all diff changes to cloud folder. Detects external mod → 409 if dirty. |
| `POST` | `/api/jobs/{id}/reject` | Discard diff, mark job rejected, no cloud write. |

Auth: `require_job_access` (owner or admin) on all.

### Mode B endpoints

| Verb | Path | Purpose | Status |
|---|---|---|---|
| `POST` | `/api/jobs/{id}/export-to-shared-folder` | Loose jobs only. Creates shared folder, copies output/, returns handle + browser URL. | ✅ shipped 2026-05-20 |

Auth: same.

### Existing endpoints extended (✅ shipped 2026-05-20)

`GET /api/jobs/{id}` and the list endpoints (`/api/jobs`, `/api/jobs?status=...`) now surface:
- `cloud_diff_baseline_commit` (string | null) — Mode A only; null for loose jobs and pre-Mode-A jobs
- `diff_status` (enum: `pending` | `accepted` | `rejected` | null — null until Mode A captures a diff)
- `exported_folder_handle` (string | null — set on successful Mode B export)
- `exported_at` (timestamp | null — paired with the handle)

List queries surface `diff_status` and `exported_at` (the two badge-trigger fields); full `get_job` returns all four. Cockpit reads these to drive the review-UI state.

## 6. DB schema

> **Implementation note (✅ shipped 2026-05-20):** Migration `orchestrator/database/migrations/app/0017_jobs_cloud_diff.sql` applies the schema below. Applied at orchestrator startup on the dev cluster on 2026-05-20T08:51:53 in 12 ms.

Single migration. New columns on `jobs` (transactional, metadata-only ALTERs):

```sql
ALTER TABLE jobs
  ADD COLUMN cloud_diff_baseline_commit TEXT,
  ADD COLUMN diff_status TEXT,
  ADD COLUMN exported_folder_handle TEXT,
  ADD COLUMN exported_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE jobs
  ADD CONSTRAINT jobs_diff_status_check
    CHECK (diff_status IS NULL
        OR diff_status IN ('pending', 'accepted', 'rejected')) NOT VALID;
ALTER TABLE jobs VALIDATE CONSTRAINT jobs_diff_status_check;

CREATE INDEX IF NOT EXISTS idx_jobs_diff_status_pending
  ON jobs (id)
  WHERE diff_status = 'pending';
```

`NULL` is allowed for `diff_status` (and is the dominant case — Mode B jobs and pre-Mode-A jobs). The partial index narrows the cockpit "needs review" scan to a few rows in practice.

No new tables. Per-file diffs are computed on-demand from Gitea commits — no need to persist them.

## 7. Agent-side changes

In `src/api/persistent_app.py` and the worker-job equivalent (`src/agent.py:_setup_*` helpers):

- New `project_folder_mount` setup helper, parallel to `setup_repository_datasource`. Clones the project's cloud folder into `projects/<slug>/` at job/session start.
- Hook runs **after** repository datasources are cloned, so any `mount_kind='repo'`-equivalent positioning under `repos/` and project-folder mount under `projects/` don't trip over each other.
- For sessions: this is already plumbed via Phase 1 `thread_mounts`. The Phase 1 path already handles `projects/<slug>/` mounting. Mode A's Mode-A clone for jobs is basically "do what sessions already do, but for jobs and via the worker code path."

Open implementation question: does Mode A reuse the existing `thread_mounts` table (jobs get rows in `thread_mounts` despite the column name "thread_id"), or add a parallel `job_mounts` table? Lean toward reusing `thread_mounts` and either (a) renaming the column to `attachment_id` covering both jobs and threads or (b) adding a discriminator. Decision deferred to implementation start; not blocking the design.

## 8. Open design questions

Most decisions locked in §3-7 above. Residual:

- **Naming on the user-facing side:** "Pending review" vs "Awaiting your review" vs "Changes pending." UX call.
- **What happens to `pending_review` jobs that are never reviewed?** Today `pending_review` exists as a status but doesn't have a TTL. If a user starts 50 jobs and never reviews them, cloud folders are never updated. Probably not a v1 problem; revisit if dogfood shows abandonment.
- **Does the diff view show binary files (images, PDFs)?** v1: "Binary file — view in OpenCloud" link. Don't try to render diffs for binary content.
- **What if the project's `main_cloud_folder_handle` is null** (a project that existed before cloud folders were a thing, or where provisioning failed)? Same fallback as Phase 2's observable-state gate: no mount, no diff workflow, job falls through to loose-job behavior (export-button at completion).

## 9. v2 follow-ups (deferred)

- **Per-file accept/reject.** v1 is all-or-nothing. Per-file UI is a meaningful expansion (need three-state per file: accept / reject / unresolved; need rollup logic). File a follow-up doc if user demand surfaces.
- **Three-way merge UI** when the cloud folder was modified externally during the job. v1 refuses with manual-resolve message; v2 could offer side-by-side keep-mine / keep-theirs / view-both per file.
- **Live progress view during long jobs.** v1 has no mid-job visibility into changes. v2 could push intermediate Gitea snapshots to a "in-progress diff" view accessible from Cockpit.
- **30-day retention on rejected diffs.** v1 keeps the Gitea commits indefinitely (cheap). v2 could prune.
- **Multi-project jobs.** If a job ever supports multiple attached projects, mount paths follow Phase 3a's collision-safe scheme. Doesn't change the diff workflow; just the mount layout.

## 10. What this rewrite changed vs. the earlier draft

The original `job_cloud_export.md` (filed 2026-05-18) proposed a single "Export to cloud" button on completed jobs as a one-shot copy of `output/` → `<project>/job-<id>/`. That design:

- Solved the "send job results to cloud" half of the gap.
- Did **not** solve the "agent can't read project files during the job" half, and explicitly created the "manual replace" friction the user called out — a job that rewrites a document produces a sibling file, not an edit.
- Was scoped at ~2-3 days but addressed only one of the two real user problems.

The rewrite (this doc, 2026-05-20) covers both halves: auto-mount on job-start gives read+write access, diff/accept at completion gives review-before-landing. The original export-button verb persists as Mode B for loose jobs — same shape, narrower use case, now a fallback rather than the primary flow.

Open questions Q2/Q4/Q5 migrated from `docs/done/cloud_collaboration_model.md` are addressed inline in §3.5 and §9:
- Original Q2 (staging clone location): the agent's existing workspace **is** the staging clone. No separate staging area needed.
- Original Q4 (accept-time conflicts): refuse-on-divergence for v1, manual reconcile.
- Original Q5 (staging retention): N/A — Gitea commits are cheap, retain indefinitely.

## 11. Estimated scope

| Piece | Estimate | Status |
|---|---|---|
| Backend — DB migration (0017), helpers, field plumbing | ~½ day | ✅ shipped 2026-05-20 |
| Backend — `put_session_file` on Protocol + OpenCloud + Nextcloud + `get_file_bytes` on Gitea | ~½ day | ✅ shipped 2026-05-20 |
| Backend — Mode B: export-to-shared-folder endpoint (reuses session-folder pattern) | ~½ day | ✅ shipped 2026-05-20 |
| Cockpit — Mode B export button + "Exported" badge + i18n (en + de-DE) | ~½ day | ✅ shipped 2026-05-20 |
| Backend — Mode A: job-start clone hook | ~½ day (reuses repository-datasource pattern) | Slice 2 |
| Backend — Mode A: baseline Gitea commit on clone | ~½ day (reuses per-job Gitea plumbing) | Slice 2 |
| Backend — Mode A: diff computation at completion + status transition | ~1 day | Slice 2 |
| Backend — Mode A: accept endpoint with external-mod detection + write-back | ~1.5 days | Slice 3 |
| Backend — Mode A: reject endpoint | ~½ day | Slice 3 |
| Cockpit — diff file tree | ~1 day | Slice 3 |
| Cockpit — per-file diff view (Monaco) | ~2-3 days | Slice 3 |
| Cockpit — accept/reject buttons + confirmation + external-mod handling UI | ~1 day | Slice 3 |
| Cockpit — state management + i18n + tests for Mode A | ~1 day | Slice 3 |
| Tests + cluster verification for Mode A | ~1-2 days | Slice 3 |

**Total: ~1.5-2 weeks.** Slice 1 (Mode B) consumed ~2 days. Slices 2 + 3 (Mode A) are the remaining ~1.5 weeks.

## 12. What this doesn't change

- Job lifecycle status flow — extended (adds `pending_review` for jobs with a non-empty diff) but not restructured.
- Job workspace location (SSH-accessible scratch) — unchanged.
- The `webdav` datasource type — stays for users who attach non-project WebDAV servers as datasources (e.g., a personal Nextcloud). Just no longer the **only** way for a job to access a project folder.
- The `repository` datasource flow — unchanged; repos still clone into `repos/<slug>/`.
- The `/promote` endpoint ("move job into new project") — unchanged.
- The cloud-mirror session foundation (Phases 1-4 in `docs/done/cloud_collaboration_model.md`) — unchanged.

This is additive: a new mount type, a new completion sub-status, a new review UI, a new accept verb. The existing pieces all keep working.
