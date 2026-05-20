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

**Status:** Design draft (rewrite 2026-05-20). Not yet implemented. **Replaces an earlier draft** of this doc that scoped the feature too narrowly (export button only — see §10).
**Triggered by:** Phase 5/6 of `docs/done/cloud_collaboration_model.md` got the broader shape right but was deferred as "too big." The earlier rewrite of this doc swung too far the other way (export-only). The actual user-friction lives in both halves: jobs can't *read* project folders without ugly WebDAV-datasource tooling, and they can't *write back* without producing a sibling-file that the user has to manually swap.
**Scope:** Project-attached jobs get auto-mount + diff/accept. Loose jobs get an export-to-shared-folder button. New orchestrator endpoints + new Cockpit diff-review UI + new agent-side job-start hook to mount the project folder before the agent's first turn. Reuses everything from the cloud-mirror foundation (Phase 1 service-account WebDAV transport, Phase 2.1 token-exchange for user-home destinations, Phase 3a collision-safe slugger, Gitea per-job snapshot plumbing).

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

Loose jobs follow the existing **shared-session-folder pattern** that Phase 4 preserved as a fallback for unattached sessions. Reuse the same orchestrator hooks:

- `ensure_session_folder(session_id=job_id[:8])` (or a new `ensure_job_folder` if naming separation matters) → creates a folder at the configured location (e.g., `Jobs/<job-id-prefix>/` in the user's accessible cloud space).
- `share_session_folder(handle, user_id)` → grants the user access.
- Copy job's `output/` files into the new folder.
- Store the resulting handle on the job (proposed columns: `jobs.exported_folder_handle TEXT`, `jobs.exported_at TIMESTAMPTZ`).

Endpoint: `POST /api/jobs/{job_id}/export-to-shared-folder`.

Cockpit button on completed loose jobs:
- **Not exported:** "Export to shared folder"
- **Already exported:** "Open in OpenCloud" with kebab → "Re-export" (refuses unless folder is deleted first, same policy as original export-button draft).

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

| Verb | Path | Purpose |
|---|---|---|
| `POST` | `/api/jobs/{id}/export-to-shared-folder` | Loose jobs only. Creates shared folder, copies output/, returns handle + browser URL. |

Auth: same.

### Existing endpoints to extend

`GET /api/jobs/{id}` (existing job detail) returns new fields:
- `cloud_diff_baseline_commit` (string | null)
- `diff_status` (enum: `pending` | `accepted` | `rejected` | null — null for loose jobs or jobs without a project mount)
- `exported_folder_handle` (string | null — for loose jobs)
- `exported_at` (timestamp | null)

Cockpit reads these to drive the review-UI state.

## 6. DB schema

Single migration. New columns on `jobs`:

```sql
ALTER TABLE jobs
  ADD COLUMN cloud_diff_baseline_commit TEXT,
  ADD COLUMN diff_status TEXT
    CHECK (diff_status IN ('pending', 'accepted', 'rejected')),
  ADD COLUMN exported_folder_handle TEXT,
  ADD COLUMN exported_at TIMESTAMPTZ;
```

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

| Piece | Estimate |
|---|---|
| Backend — Mode A: job-start clone hook | ~½ day (reuses repository-datasource pattern) |
| Backend — Mode A: baseline Gitea commit on clone | ~½ day (reuses per-job Gitea plumbing) |
| Backend — Mode A: diff computation at completion + status transition | ~1 day |
| Backend — Mode A: accept endpoint with external-mod detection + write-back | ~1.5 days |
| Backend — Mode A: reject endpoint | ~½ day |
| Backend — Mode B: export-to-shared-folder endpoint (reuses session-folder pattern) | ~½ day |
| Backend — DB migration, status enum, field plumbing | ~½ day |
| Cockpit — diff file tree | ~1 day |
| Cockpit — per-file diff view (Monaco) | ~2-3 days |
| Cockpit — accept/reject buttons + confirmation + external-mod handling UI | ~1 day |
| Cockpit — Mode B export button + states | ~½ day |
| Cockpit — state management + i18n + tests | ~1 day |
| Tests + cluster verification | ~1-2 days |

**Total: ~1.5-2 weeks.** Roughly Phase 5+6 from the closed cloud-collab doc — the original ballpark was correct; the export-only rewrite undershoot was wrong.

## 12. What this doesn't change

- Job lifecycle status flow — extended (adds `pending_review` for jobs with a non-empty diff) but not restructured.
- Job workspace location (SSH-accessible scratch) — unchanged.
- The `webdav` datasource type — stays for users who attach non-project WebDAV servers as datasources (e.g., a personal Nextcloud). Just no longer the **only** way for a job to access a project folder.
- The `repository` datasource flow — unchanged; repos still clone into `repos/<slug>/`.
- The `/promote` endpoint ("move job into new project") — unchanged.
- The cloud-mirror session foundation (Phases 1-4 in `docs/done/cloud_collaboration_model.md`) — unchanged.

This is additive: a new mount type, a new completion sub-status, a new review UI, a new accept verb. The existing pieces all keep working.
