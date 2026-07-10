# Session deliverables saved to `output/` aren't reachable from the "Files" (cloud) button — only via the full IDE

**Status:** Filed — root cause confirmed on live session `7692637b-9c60-4698-9875-b57ec34e66a6` (main cluster, cloud-mounted). **Reconfirmed + deeper root cause filed 2026-07-10** on dev session `e979d520-35a5-4eeb-a9c1-4f6e1be1b2fd` (default project): under the `rclone_mount` driver the shared session folder is an orphan (created + shared, but never mounted or synced) and the Files button points straight at it — see the **Update 2026-07-10** section below. Still unfixed.
**Found:** 2026-06-26. User asked the agent to build Excel lists; the agent produced them, but none appeared under the session's **Files** button — they had to be downloaded through the **IDE**.
**Severity:** Medium. Silent usability gap: the agent's actual deliverables are invisible via the primary "Files" affordance, so a non-technical user concludes nothing was produced. Found in a real customer-style task.
**Component:** agent deliverable convention (`config/templates/instructions.md`, strategic/tactical prompts) · cockpit Files button (`persistent-chat.component.ts` `openSessionFiles()`) · cloud rclone mount vs local workspace dirs · orchestrator session-folder provisioning (`orchestrator/main.py`: `_setup_main_cloud`, `_should_skip_session_folder`, `_build_agent_cloud_mount`, `_resolve_cloud_session_url`) · rclone mount vs legacy sync coordinator (`src/api/persistent_app.py` `cloud_mount_active` gate)

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

So **Files = a cloud storage folder** (an OpenCloud web URL), and it never lists the workspace `output/` dir. ⚠️ **Correction (2026-07-10):** the parenthetical "backed by the `/cloud/home` rclone mount" is wrong under the `rclone_mount` driver. `cloudSessionUrl()` resolves via `_resolve_cloud_session_url`, which returns the **legacy per-session folder** `sessions/{thread[:8]}` — a *different* WebDAV location than the `/cloud/home` user-home mount, and always empty. See the Update 2026-07-10 section below.

**Why the IDE worked:** the IDE button opens code-server rooted at the whole workspace —
`code_server_url = …/api/ide/{thread_id}/proxy/?folder=/home/agent-host/workspace` (`orchestrator/main.py:17692`) — which includes `output/`. That's the only UI surface that exposes local workspace files.

## Net

For a **cloud-mounted session**, the user's mental model (and the only file button) is the cloud folder, but the agent's deliverable convention targets the workspace-local `output/`. Result: deliverables land somewhere the primary UI can't see. The `cloud_sync` machinery (`src/services/cloud_sync/…`, rclone mount at `/cloud/home`) would surface anything written **into the mount**, but the agent never wrote there.

## Update 2026-07-10 — the real root cause: the `rclone_mount` driver + an orphaned session folder

Reconfirmed on dev session `e979d520-35a5-4eeb-a9c1-4f6e1be1b2fd` (default project; the agent used `write_file` → `create_directory` → `move_file` to produce `test.txt` then `output/test.txt`). The write-up above is right about the *symptom* but incomplete about *why*, and wrong about what the Files button opens.

Dev **and** prod default to the **`rclone_mount`** cloud workspace driver (`helm/templates/configmap.yaml`: `CLOUD_WORKSPACE_DRIVER` defaults to `rclone_mount`; validated on dev per `docs/features/rclone_cloud_mount.md`). Under that driver there are **three distinct storage locations**, and the shared session folder is structurally unable to receive anything:

| Location | What it is | What lands there |
|---|---|---|
| `/home/agent-host/workspace/` | workspace-local working dir (ephemeral `emptyDir`) | `write_file`/`move_file`/`create_directory` → `test.txt`, `output/test.txt` |
| `/workspace/cloud` → `/cloud/home` | rclone FUSE mount = owner's **personal home Space** (default project) | only explicit writes to the cloud path |
| `sessions/{thread[:8]}` in `srw-agent-home`, shared to the user | the **"shared session folder"** | **nothing, ever** |

**1. The shared folder is always created under `rclone_mount`.** `_setup_main_cloud` (`orchestrator/main.py:15628`) provisions `sessions/{thread[:8]}` and shares it with the user via a LibreGraph invite (so it appears in their OpenCloud). Normally `_should_skip_session_folder` skips this when the thread already has a working mount — **but it hard-returns `False` under `rclone_mount`** (`main.py:14076-14081`), so the folder is created regardless.

**2. …but it is never mounted or synced (the orphan).**
- *Not mounted:* `_build_agent_cloud_mount` mounts the session folder **only as a fallback** (`main.py:16178-16186`), taken only when a requested `thread_mounts` row can't be represented. For a default-project session the `project_default` user-home mount builds fine, so the fallback is not taken and the session folder is never mounted into the workspace.
- *Not synced:* under rclone the legacy `WorkspaceSyncBase` coordinator (turn-boundary `pull_all`/`push_all`) is **disabled** — `cloud_mount_active=True` forces `cloud_cfg=None` (`src/api/persistent_app.py:1562-1569`), so `_session.workspace_sync` is never built. There is **no** workspace→cloud copy at all. (That turn-boundary push only runs under the default `sync` driver.)

Net: the agent's files sit in the ephemeral workspace-local dir; the only real cloud surface is the user's home Space; the shared session folder gets neither → permanently empty ("useless").

**3. The Files button points at that empty folder** (this corrects the "Files = `/cloud/home` mount" statement above). `cloudSessionUrl()` comes from `_resolve_cloud_session_url` (`orchestrator/main.py:15811`), which checks the **legacy session-folder handle first** and returns it whenever present (`:15826-15838`); the user-home fallback (`:15840+`) is only reached when there is *no* session handle. Under `rclone_mount` the handle always exists, so the button opens the empty `sessions/...` folder — not the `/cloud/home` user-home Space where the agent's explicit cloud writes actually land. The resolver's own comment assumes the Phase-2 `sync`-driver behavior (folder skipped for default projects), which `rclone_mount` violates.

**Rclone-specific fixes** (in addition to options 1–4 below):

5. **Don't create the orphan.** Under `rclone_mount`, skip provisioning the session folder when a `project_default`/`project`/`repo` mount already exists (drop the unconditional `return False` in `_should_skip_session_folder`), and make `_resolve_cloud_session_url` prefer the *mounted* surface. Cheapest fix — the Files button would then open the user-home Space where files actually are. Directly resolves the "shared folder is empty / Files opens nothing" complaint.
6. **Make the session folder the real surface.** Actually mount `sessions/{thread}` into the workspace (not fallback-only) so writes and/or an `output/` export land there, giving each session its own curated cloud folder distinct from the user's whole home. Heavier, but preserves the per-session-folder model.

## Proposed fixes (options — pick one or combine)

1. **Make `output/` reachable from the UI without the full IDE.** Add a lightweight "Workspace files" / output browser (list + download) backed by SFTP of `<workspace>/output/`, alongside or merged into the Files button. Smallest behavior change; works for all sessions (cloud-mounted or not).
2. **For cloud-mounted sessions, point the deliverable convention at the cloud mount.** Inject session-aware output guidance so the agent saves final deliverables to `/cloud/home/output/` (which then syncs and shows under Files). Risk: pollutes the user's cloud space with intermediates; best paired with a clear "final deliverables only" rule.
3. **Auto-export `output/` → cloud session folder** on turn/session boundary (a `cloud_sync` of `output/`), mirroring the job `output/`→cloud export (`docs/done/job_cloud_export.md`) but for sessions. User keeps a clean cloud folder of just deliverables.
4. **At minimum (cheap stopgap):** when a session has produced files only in workspace `output/`, the agent's reply / a UI hint should say "deliverables are in the workspace `output/` folder — open via IDE", instead of implying they're in the cloud Files area. Removes the silent dead-end.

Recommended: **(1)** as the durable fix (a deliverables view that doesn't require driving the IDE), optionally **(3)** for cloud-folder parity with jobs. For the specific "shared folder is empty / Files button opens nothing" complaint on the current `rclone_mount` default, **(5)** (in the 2026-07-10 update) is the cheapest.

## Reproduce

1. Start a cloud-mounted persistent session (Files button visible).
2. Ask the agent to produce a file deliverable (Excel/CSV/report).
3. Agent writes to `output/` per its instructions.
4. Click **Files** → deliverable absent. Open **IDE** → it's under `workspace/output/`.

## Open questions

- Is a session-level `output/`→cloud export intended (parity with `job_cloud_export.md`) but missing, or is the model purely "use the IDE for workspace files"? Decides between fix (1) and (3).
- Should the Files button label/scope distinguish "cloud storage" from "this session's outputs"? Today one button implies both.
- Under `rclone_mount`: should the per-session folder be **dropped** when a `project_default`/`project` mount already exists (fix 5), or **promoted to the real mounted surface** (fix 6)? Picking one resolves the orphan. (`docs/done/session_folder_placement.md` is the placement design, but it predates the rclone driver — its "Option 3 status quo" assumes the session folder is the sync target, which no longer holds.)

## Related

`resumed_session_dead_stream_and_supervised_gate_timeout_as_denial.md` (same session debugging pass) · `docs/done/job_cloud_export.md` (the job-side `output/`→cloud export this lacks for sessions) · `docs/features/rclone_cloud_mount.md` (the driver that makes the folder an orphan) · `docs/done/session_folder_placement.md` (session-folder placement design, pre-rclone) · `docs/done/cloud_collaboration_model.md` (§9 mount/skip model) · memory `project_opencloud_rclone_mounts`, `srw_session_shared_folder_orphan_rclone`
