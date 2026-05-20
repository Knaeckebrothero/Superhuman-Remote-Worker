---
tags:
  - feature
  - cloud-infrastructure
  - jobs
  - cockpit
  - product
aliases:
  - export job to cloud
  - job cloud export
  - publish job results
related:
  - "[[cloud_collaboration_model]]"
  - "[[main_cloud_abstraction]]"
  - "[[project_cloud_folders]]"
  - "[[multi_tenancy]]"
---

# Job Cloud Export — Send Job Results to Cloud Storage

> Jobs today complete inside an isolated workspace and the only way to look at their outputs is Cockpit's job-review tab. Add an explicit **"Export to cloud"** button on that tab that copies the job's results into a subfolder of the job's project cloud folder (or the user's home Space for default-project jobs), so the user can pick up the results where they keep the rest of their files.

**Status:** Design draft (2026-05-18). Not yet implemented.
**Triggered by:** Phase 5 of `docs/done/cloud_collaboration_model.md` was originally a "job staging clone + accept UI" plan; reframed (2026-05-18) as a much smaller "explicit export button" first step. Bigger ambitions (live staging mount, diff view, per-file accept) preserved at the bottom of this doc as v2 follow-ups.
**Scope:** Cockpit job-review component + new orchestrator endpoint. Reuses existing `MainCloudBackend` transport (Phase 1 service-account auth for project folders, Phase 2.1 RFC 8693 token-exchange for user home Space). No new schema. No new agent-side code.

## 1. Motivation

The cloud-mirror foundation (`docs/done/cloud_collaboration_model.md`) made **session** outputs first-class in the user's cloud — agent edits a file, the user sees it immediately in OpenCloud. **Job** outputs are still trapped: they live in the agent's workspace (SSH-accessible scratch space), get snapshotted to Gitea on completion, and are only browsable through Cockpit's job-review tab. To do anything with them — share a chart with a colleague, paste a written summary into the user's documentation system, open a generated PDF — the user has to download files one-at-a-time through Cockpit.

The full "Phase 5 + 6" plan from the closed cloud-collab doc (staging clone at job-start, diff view, per-file accept, conflict UI) was the *right* eventual answer for collaborative job acceptance. But it's a 1-2 week build for a feature the user hasn't validated yet, and most of the value can be unlocked by a much smaller move: **just give the user a button that says "send this to my cloud."**

That's this doc.

## 2. The button

Single button on the job-review component, label something like **"Export to cloud"** (or **"Save to OpenCloud"**, **"Publish results"** — UX wording TBD with the rest of Cockpit's verbs). Available on completed jobs only (not running, not paused, not failed). Click → spinner → "exported to `<path>`" confirmation with a one-click "open folder" link that deep-links into OpenCloud's web UI at the target folder.

### Destination

| Case | Target |
|---|---|
| Job belongs to a non-default project | `<project's cloud folder>/job-<id>/` (uses the project's existing `main_cloud_folder_handle`; service-account auth, same as Phase 1) |
| Job belongs to the user's **default project** | `<user's home Space>/job-<id>/` (Phase 2 established that default project = user-home; Phase 2.1 RFC 8693 token-exchange auth, since service accounts can't write to user Personal Spaces) |

Both cases reuse plumbing that's already shipped. No new transport, no new auth, no new identity resolution. The orchestrator just walks the job's workspace, picks the export subset, and uploads to the resolved target.

### What gets exported

**Default: `output/` only.** That's the user-meaningful artifacts — generated files, deliverables, the agent's actual product. Scaffolding (`workspace.md`, `plan.md`, `todos.yaml`, `archive/`, `documents/Code_Repository/`, tool intermediate files) is **not** exported by default — that'd clutter the user's cloud with bookkeeping nobody asked for.

**Why default to `output/` instead of full workspace:** the user said "store the **job's results**" — results is the framing. A user clicking "export" isn't asking for the agent's diary; they're asking for the work. If they want the full workspace, that's a different feature (job archive download, which Cockpit may already cover).

**Future opt-in:** a per-export toggle "include scaffolding files" or "export full workspace." Not v1.

### Collision policy

The target folder is `job-<id>/` where `<id>` is a UUID, so within a single project the same job ID cannot collide with itself by accident. The only realistic collision is **re-export of the same job** — user clicks the button, exports, then later clicks it again.

**Recommendation for v1: refuse with a clear message.** Response: `409 Conflict` with body `{ "status": "already_exported", "exported_at": "<iso8601>", "path": "<cloud-path>" }`. Cockpit shows: "Already exported on 2026-05-18. Open folder · Delete and re-export."

**Why refuse rather than overwrite or suffix:**
- **Overwrite** would silently destroy any user-side modifications to the cloud folder. Bad default — the user might have annotated or moved files since the last export.
- **Suffix** (`job-<id>-2/`) would produce ugly listings and lose the canonical "this is where job X's results live" mental model.
- **Refuse** is safe and recoverable. User who actually wants to re-export deletes the folder in OpenCloud first (one click) and tries again.

Future opt-in: a "Re-export (overwrite)" verb in the Cockpit button's dropdown if the refuse path becomes friction. Not v1.

### Idempotency tracking

To detect "already exported," the orchestrator records the export on the job — proposed column `jobs.cloud_export_path TEXT, jobs.cloud_exported_at TIMESTAMPTZ`. Single migration, no JSONB merging needed (these are scalars, not aggregates).

A "delete and re-export" verb (separate endpoint or flag on the export endpoint?) would either:
- Just clear the columns and let the user retry the export, leaving them to delete the cloud folder themselves; OR
- Actually delete the cloud folder on their behalf as part of the call.

Lean toward the first: don't take destructive cloud actions on a user's behalf without explicit confirmation. The Cockpit "Delete and re-export" button can do the delete + re-export in two server calls with a confirmation dialog in between.

## 3. API surface

### `POST /api/jobs/{job_id}/export-to-cloud`

Request body (all optional, all v1-deferred):
```json
{
  "scope": "output",                  // future: "output" | "workspace" | "custom"
  "force": false                      // future: re-export even if already exported
}
```

For v1, request body is effectively empty (`{}`) — defaults handle everything.

Auth: existing `require_job_access` (owner or admin).

Responses:
- `202 Accepted` (export queued asynchronously, returns a task handle to poll) — **recommended for v1**, since exports can be slow (multi-megabyte file uploads to OpenCloud).
- `409 Conflict` (already exported; body contains existing path + timestamp).
- `404` (job not found / no access).
- `409` again or `400` for state errors (job not completed).

The async pattern matches existing slow operations in the orchestrator (e.g. the cloud-folder JIT provisioning). Cockpit polls or subscribes to a progress channel. Simple implementation: write to a `job_exports` table or reuse a generic background-task table.

### `GET /api/jobs/{job_id}` (existing) — add export status

Existing job-detail endpoint returns the new columns:
```json
{
  "id": "<uuid>",
  ...
  "cloud_export_path": "/spaces/drive-xyz/job-<id>/",   // or null
  "cloud_exported_at": "2026-05-18T14:23:11Z"           // or null
}
```

Cockpit renders the button state off these two fields. No new GET endpoint required.

## 4. Cockpit side

The job-review component (location TBD; current Cockpit structure has it under the per-job tab) gains a button next to existing job-action verbs (promote, cancel, etc.).

States:
- **Not exported yet** (`cloud_export_path` is null): button reads "Export to cloud."
- **Already exported** (`cloud_export_path` is set): button reads "Open in OpenCloud" with a kebab menu offering "Re-export" (refuses unless `force=true` or the folder is deleted first).
- **Export in progress** (background task pending): button is disabled with a spinner and progress text.

Deep-link: clicking "Open in OpenCloud" opens a new tab at `https://<opencloud-host>/files/spaces/<space-id>/job-<id>/` (the same URL pattern Phase 2.2's `cloud_session_url` already produces for default-project session threads).

## 5. Open design questions

### Q1. Where in the project cloud folder does `job-<id>/` go?

Three options:

- **Project root** (`<project>/job-<id>/`) — flat, easy to find, will accumulate over time.
- **Under a fixed parent** (`<project>/.srw/jobs/<id>/` or `<project>/Jobs/<id>/`) — keeps the project root clean; user has to know to look under `Jobs/`.
- **Under a user-selected subfolder** — user picks where during the export action. Most flexible, most click-heavy.

**Recommendation:** project root (`<project>/job-<id>/`). Simplest, discoverable, matches the user's "send the results to my project" mental model. If accumulation becomes a problem, the second option becomes a future toggle. Don't gold-plate v1.

### Q2. What about the user-home destination — should it actually be at home root?

For default-project jobs, the target is the user's Personal Space. Three options:

- **Home root** (`~/job-<id>/`) — clutters the user's home directory.
- **Fixed subfolder** (`~/srw/jobs/<id>/` or `~/SuperhumanRemoteWorker/jobs/<id>/`) — keeps home clean; the user has to know about the folder.
- **User-selected** — most flexible, most click-heavy.

**Recommendation:** fixed subfolder at the user-home destination, project root at non-default-project destinations. The asymmetry is justified: the user's home is shared with all their other personal files, so a containment subfolder matters; a project folder is *already* SRW-scoped, so adding another level is redundant nesting.

Proposed names — pick one: `~/SuperhumanRemoteWorker/jobs/<id>/`, `~/SRW/jobs/<id>/`, `~/AI Jobs/<id>/`. The first is most explicit, the third is most user-friendly. UX call.

### Q3. Does the export include the job's audit trail / cost report / metadata?

Open. Three options:

- **Just `output/` files** as currently scoped. Pure deliverables.
- `output/` files + a small `_meta.json` at the top of `job-<id>/` with job ID, completion time, cost, and a few key stats. Helps future-user-self ("which job produced this folder?").
- `output/` files + the full `archive/` + `freeze_data.json`. Full forensic dump.

**Recommendation:** option 2 (`output/` + small `_meta.json`). The metadata file is cheap to write, doesn't clutter the user's cloud listing (one extra file vs. dozens), and gives the future-user-self a way to trace any file back to the SRW job that produced it. Not negotiable.

### Q4. What happens to the export when the job is deleted?

If a user deletes a job in Cockpit, does the cloud folder `job-<id>/` get deleted too?

**Recommendation: NO.** Once exported, the cloud folder belongs to the user; SRW shouldn't reach back into it. Job deletion in Cockpit removes orchestrator records only. The cloud folder stays. The user manages it themselves.

The `cloud_export_path` reference in `jobs` is moot once the job row is gone — that's fine, it's just informational.

## 6. Migrated context from the cloud_collab doc

These questions were originally in `docs/done/cloud_collaboration_model.md` §10 as Q2/Q4/Q5. They're not blocking the v1 export-button design but are pre-considered context for any future "real staging clone" or "diff view + accept" work:

**Job staging clone location (original Q2):** the cloud_collab doc leaned toward a per-user hidden drafts area (`/Drafts/jobs/<id>/`). The export-button design instead puts results at a visible location keyed on the job's project. If the bigger staging clone ever happens, the staging area would be hidden (the user is reviewing, not finalizing) and would only become visible-and-canonical after explicit accept. Different problem, different answer.

**Job accept-time conflicts (original Q4):** per-file conflict UI in the accept flow ("keep mine," "keep theirs," "view both side-by-side"). Not relevant to v1 (no accept flow, just an explicit copy-to-cloud action), but relevant to the v2 ambition below.

**Per-job staging clone retention (original Q5):** 30-day retention then auto-delete. Not relevant to v1 (no orchestrator-managed staging; the exported folder is owned by the user). Relevant to v2 if a hidden staging area ever lands.

## 7. v2 follow-ups (deferred)

The bigger ambitions from the closed cloud_collab doc that v1 explicitly does **not** tackle:

- **Live staging clone** during the job run, so progress is visible mid-job (not just at completion).
- **Diff view** in the job-review tab showing what changed vs. the project's existing files.
- **Per-file accept/reject** instead of all-or-nothing export.
- **Conflict resolution** when the user has been editing project files in parallel.
- **30-day retention** on rejected stagings.

All of these are unlocked by the existence of the v1 export button: once we have a way to materialize a job's outputs into the user's cloud, accept-vs-overwrite, conflict-detection, and selective-merge become natural extensions on the same destination.

If real users demand any of these after v1 ships, file a follow-up doc (`docs/features/job_accept_ui.md` or similar). Don't pre-build it.

## 8. Estimated scope

- **Backend:** new endpoint + ~50 LoC for the export logic (workspace walk + WebDAV upload via existing transport) + idempotency tracking. Schema migration: 1 new file, 2 nullable columns. Async task plumbing if not already generic. ~1 day.
- **Cockpit:** button + state machine + i18n + tests. Probably ~½ day given how often we've added buttons next to job actions.
- **Tests:** unit on the export endpoint (mock backend, mock job workspace), an integration-style test against the local-fs test backend already used by `cloud_sync/`. ~½ day.
- **Verification:** dogfood on the dev cluster — export a real job to a real project folder; export a default-project job to user home Space; collision case; deep link works. ~½ day.

**Total: ~2-3 days.** Roughly 10x smaller than the original "Phase 5 + 6" framing because it's an explicit user action with no live mounting / no diff view / no per-file accept. The cost of that simplicity is the deferred v2 features above.

## 9. What this doesn't change

- Job lifecycle (created → processing → completed → ...) — unchanged.
- Job workspace location (SSH-accessible scratch) — unchanged.
- Gitea snapshot of job workspace on completion — unchanged.
- The orchestrator-determined-final-status authority — unchanged.
- The existing `/promote` endpoint (which means "move job into new project," not "publish results") — unchanged.

This is a purely additive feature: a new verb on completed jobs that materializes their outputs to a user-visible cloud location.
