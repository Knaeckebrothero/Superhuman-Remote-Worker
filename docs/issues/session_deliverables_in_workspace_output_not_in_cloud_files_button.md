# Session deliverables saved to `output/` aren't reachable from the "Files" (cloud) button — only via the full IDE

**Status:** Filed — root cause confirmed on live session `7692637b-9c60-4698-9875-b57ec34e66a6` (main cluster, cloud-mounted).
**Found:** 2026-06-26. User asked the agent to build Excel lists; the agent produced them, but none appeared under the session's **Files** button — they had to be downloaded through the **IDE**.
**Severity:** Medium. Silent usability gap: the agent's actual deliverables are invisible via the primary "Files" affordance, so a non-technical user concludes nothing was produced. Found in a real customer-style task.
**Component:** agent deliverable convention (`config/templates/instructions.md`, strategic/tactical prompts) · cockpit Files button (`persistent-chat.component.ts` `openSessionFiles()`) · cloud rclone mount vs local workspace dirs

---

## Symptom

The agent built four files for the "Vereine Michelstadt" task and reported success. The user clicked the header **Files** button (cloud icon) — empty / no deliverables. The files were only findable by opening the **IDE** (code-server) and browsing the workspace. The user had to download them manually from there.

## Root cause — two different locations, no bridge

The agent wrote deliverables to the **local workspace `output/` dir**, but the **Files button opens the cloud mount**. These are physically different filesystems on the workspace pod, and nothing copies between them.

**Where the files actually are** (confirmed via `kubectl exec` into `ws-thread-7692637b-9c6`):

```
/home/agent-host/workspace/output/vereine_michelstadt_5km_emails.{csv,xlsx}
/home/agent-host/workspace/output/vereine_michelstadt_erweitert_15km_emails.{csv,xlsx}
```

`/cloud/home/output/` and `/cloud/home/outputs/` (the cloud mount) are **empty**.

**Why the agent wrote there:** that is the documented deliverable convention. `config/templates/instructions.md:48` — *"Write results to workspace files (typically `output/`)"* — and every strategic prompt restates *"Primary deliverable: `output/…`"*. The agent did exactly what it was told. `output/` resolves to `/home/agent-host/workspace/output/` (workspace-local), **not** the cloud mount.

**What the "Files" button opens** — `cockpit/src/app/views/persistent-chat/persistent-chat.component.ts`:

```ts
// button is shown only @if (chat.cloudSessionUrl() || chat.ncSessionFolder())
openSessionFiles(): void {
    const cloudUrl = this.chat.cloudSessionUrl();   // OpenCloud Personal Space web URL
    if (cloudUrl) { window.open(cloudUrl, '_blank'); return; }
    // legacy Nextcloud fallback …
}
```

So **Files = the cloud storage folder** (OpenCloud, backed by the `/cloud/home` rclone mount). It never lists the workspace `output/` dir.

**Why the IDE worked:** the IDE button opens code-server rooted at the whole workspace —
`code_server_url = …/api/ide/{thread_id}/proxy/?folder=/home/agent-host/workspace` (`orchestrator/main.py:17692`) — which includes `output/`. That's the only UI surface that exposes local workspace files.

## Net

For a **cloud-mounted session**, the user's mental model (and the only file button) is the cloud folder, but the agent's deliverable convention targets the workspace-local `output/`. Result: deliverables land somewhere the primary UI can't see. The `cloud_sync` machinery (`src/services/cloud_sync/…`, rclone mount at `/cloud/home`) would surface anything written **into the mount**, but the agent never wrote there.

## Proposed fixes (options — pick one or combine)

1. **Make `output/` reachable from the UI without the full IDE.** Add a lightweight "Workspace files" / output browser (list + download) backed by SFTP of `<workspace>/output/`, alongside or merged into the Files button. Smallest behavior change; works for all sessions (cloud-mounted or not).
2. **For cloud-mounted sessions, point the deliverable convention at the cloud mount.** Inject session-aware output guidance so the agent saves final deliverables to `/cloud/home/output/` (which then syncs and shows under Files). Risk: pollutes the user's cloud space with intermediates; best paired with a clear "final deliverables only" rule.
3. **Auto-export `output/` → cloud session folder** on turn/session boundary (a `cloud_sync` of `output/`), mirroring the job `output/`→cloud export (`docs/done/job_cloud_export.md`) but for sessions. User keeps a clean cloud folder of just deliverables.
4. **At minimum (cheap stopgap):** when a session has produced files only in workspace `output/`, the agent's reply / a UI hint should say "deliverables are in the workspace `output/` folder — open via IDE", instead of implying they're in the cloud Files area. Removes the silent dead-end.

Recommended: **(1)** as the durable fix (a deliverables view that doesn't require driving the IDE), optionally **(3)** for cloud-folder parity with jobs.

## Reproduce

1. Start a cloud-mounted persistent session (Files button visible).
2. Ask the agent to produce a file deliverable (Excel/CSV/report).
3. Agent writes to `output/` per its instructions.
4. Click **Files** → deliverable absent. Open **IDE** → it's under `workspace/output/`.

## Open questions

- Is a session-level `output/`→cloud export intended (parity with `job_cloud_export.md`) but missing, or is the model purely "use the IDE for workspace files"? Decides between fix (1) and (3).
- Should the Files button label/scope distinguish "cloud storage" from "this session's outputs"? Today one button implies both.

## Related

`resumed_session_dead_stream_and_supervised_gate_timeout_as_denial.md` (same session debugging pass) · `docs/done/job_cloud_export.md` (the job-side `output/`→cloud export this lacks for sessions) · memory `project_opencloud_rclone_mounts`
