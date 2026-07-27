# Session deliverables saved to `output/` aren't reachable from the "Files" (cloud) button — only via the full IDE

**Status:** Filed — root cause confirmed on live session `7692637b-9c60-4698-9875-b57ec34e66a6` (main cluster, cloud-mounted). **Reconfirmed + deeper root cause filed 2026-07-10** on dev session `e979d520-35a5-4eeb-a9c1-4f6e1be1b2fd` (default project): under the `rclone_mount` driver the shared session folder is an orphan (created + shared, but never mounted or synced) and the Files button points straight at it — see the **Update 2026-07-10** section below. **Reconfirmed again 2026-07-20** on dev session `accfbc56`: the orphan is *not always empty* — a transient pre-mount sync can leave a **frozen partial snapshot** (there, `documents/` + `skills/`), which is worse UX than empty; current line refs re-verified. See **Update 2026-07-20**. Still unfixed.
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
| `sessions/{thread[:8]}` in `srw-agent-home`, shared to the user | the **"shared session folder"** | **usually nothing** — but a pre-mount sync window can leave a frozen partial snapshot (see Update 2026-07-20) |

**1. The shared folder is always created under `rclone_mount`.** `_setup_main_cloud` (`orchestrator/main.py:15628`) provisions `sessions/{thread[:8]}` and shares it with the user via a LibreGraph invite (so it appears in their OpenCloud). Normally `_should_skip_session_folder` skips this when the thread already has a working mount — **but it hard-returns `False` under `rclone_mount`** (`main.py:14076-14081`), so the folder is created regardless.

**2. …but it is never mounted or synced (the orphan).**
- *Not mounted:* `_build_agent_cloud_mount` mounts the session folder **only as a fallback** (`main.py:16178-16186`), taken only when a requested `thread_mounts` row can't be represented. For a default-project session the `project_default` user-home mount builds fine, so the fallback is not taken and the session folder is never mounted into the workspace.
- *Not synced:* under rclone the legacy `WorkspaceSyncBase` coordinator (turn-boundary `pull_all`/`push_all`) is **disabled** — `cloud_mount_active=True` forces `cloud_cfg=None` (`src/api/persistent_app.py:1562-1569`), so `_session.workspace_sync` is never built. There is **no** workspace→cloud copy at all. (That turn-boundary push only runs under the default `sync` driver.)

Net: the agent's files sit in the ephemeral workspace-local dir; the only real cloud surface is the user's home Space; the shared session folder gets neither → usually empty ("useless"). *(Refinement 2026-07-20: not strictly "permanently empty" — a transient pre-mount `workspace_sync` push can seed a frozen partial snapshot into it. See Update 2026-07-20.)*

**3. The Files button points at that empty folder** (this corrects the "Files = `/cloud/home` mount" statement above). `cloudSessionUrl()` comes from `_resolve_cloud_session_url` (`orchestrator/main.py:15811`), which checks the **legacy session-folder handle first** and returns it whenever present (`:15826-15838`); the user-home fallback (`:15840+`) is only reached when there is *no* session handle. Under `rclone_mount` the handle always exists, so the button opens the empty `sessions/...` folder — not the `/cloud/home` user-home Space where the agent's explicit cloud writes actually land. The resolver's own comment assumes the Phase-2 `sync`-driver behavior (folder skipped for default projects), which `rclone_mount` violates.

**Rclone-specific fixes** (in addition to options 1–4 below):

5. **Don't create the orphan.** Under `rclone_mount`, skip provisioning the session folder when a `project_default`/`project`/`repo` mount already exists (drop the unconditional `return False` in `_should_skip_session_folder`), and make `_resolve_cloud_session_url` prefer the *mounted* surface. Cheapest fix — the Files button would then open the user-home Space where files actually are. Directly resolves the "shared folder is empty / Files opens nothing" complaint.
6. **Make the session folder the real surface.** Actually mount `sessions/{thread}` into the workspace (not fallback-only) so writes and/or an `output/` export land there, giving each session its own curated cloud folder distinct from the user's whole home. Heavier, but preserves the per-session-folder model.

## Update 2026-07-20 — the orphan isn't always empty: it can hold a *frozen partial snapshot* (dev session `accfbc56-e32c-4e30-a732-98b8333b4a5a`)

**What prompted it:** the user clicked the cloud button on dev session `accfbc56` (default project "knaeckebrothero's Project", supervised, `gpt-5.6-sol`) and landed in the shared folder — but it showed **only `documents/` + `skills/`**, "nothing else," and asked why it "only shares parts of the workspace." Same orphan bug as the 07-10 update, but the folder was **not empty**. This refines the "permanently empty / nothing, ever" claim above: the session folder receives a **one-time partial WebDAV push during the brief window at session start before the rclone mount goes active**, then freezes forever. It is the `sync`-driver code path firing as a transient startup fallback — **not** a designed selective share.

### Live evidence (captured 2026-07-20; via `kubectl --context main`, ns `superhuman-remote-worker`, pod `ws-thread-accfbc56-e32`)

- **Button target = the session folder, not user-home.** `threads.main_cloud_session_handle` is set: `{"backend":"opencloud","native_id":"sessions/accfbc56","vendor_meta":{"drive_id":"…$5eb179a4-6314-4346-a85b-cecba2d9e54d", …}}`. `_resolve_cloud_session_url` returns on its **first** branch (handle present) → the button opens `sessions/accfbc56` in Space `…$5eb179a4…`, a *different* Space than the user-home mount (`…$78d029f9…`).
- **The folder holds exactly two frozen subdirs.** WebDAV `PROPFIND Depth:1` (user impersonation token via the mount's `bearer-helper.sh`) returns `skills/` (mtime Sun 19 Jul 17:57:28) and `documents/` (18:15:21) — session was created 17:56:28. Nothing else. Both frozen at creation-day while the live workspace has next-day mtimes.
- **The real home Space is fine and totally different.** `/cloud/home` (drive `…$78d029f9…`) lists 14+ items (`Hotel_Rheinland_ERP_Examples/`, `animal_health/`, `documents/`, `greet.py`, `spreewald-programm.{md,pdf}`, `spreewald-urlaub/`, `softDsim-develop.zip`, `uploads/`, …) and has **no `skills/`** — proof the button is *not* showing home.
- **Only one mount exists on the pod.** `mount | grep fuse` → a single `srw-accfbc56-home:` rclone mount on `/cloud/home`; the rclone cache dir has only `…/accfbc56-…/home` (no `…/session`). The session folder was **never** rclone-mounted on this pod.
- **Workspace vs session folder:** workspace `/home/agent-host/workspace` has `documents/ skills/ knowledge/ notes/ output/ repos/ tools/ README.md datasources.md notes.txt`; the session folder has only `documents/ + skills/`.

### Mechanism (current code, line refs re-verified 2026-07-20 on `develop` — the 07-10 refs have all moved)

1. **Handle always stamped under rclone.** `_should_skip_session_folder` (`orchestrator/main.py:17724`) still hard-`return False` when `_cloud_workspace_driver()=="rclone_mount"`, so `_setup_main_cloud` (`main.py:19902`) always calls `ensure_session_folder` + `share_session_folder` and stamps `main_cloud_session_handle`. (Was `main.py:14076`/`15628`.)
2. **Button prefers the handle.** `_resolve_cloud_session_url` (`main.py:20088`) returns the session-folder URL whenever the handle is set; the user-home fallback is only reached when it is absent. (Was `main.py:15811`.)
3. **The partial push comes from the sync coordinator, which runs *only before the mount is active*.** `_build_agent_cloud_sync` (`main.py:20198`) builds a `cloud_sync` payload whose `session_folder` target is derived from the handle (attached as `mount_id="legacy-session"` in `persistent_app.py:_attach_from_cfg`). At attach, `persistent_app.py:1899-1905` sets `cloud_cfg = None if cloud_mount_active or protected_cloud else …get("cloud_sync")`. So:
   - During the **pre-mount window** at session start (`cloud_mount_active=False` — the rclone mount isn't built yet / home mount not yet resolvable), `_session.workspace_sync` **is** built (`persistent_app.py:1938-1952`) and does turn-boundary pushes → the then-existing **non-ignored** dirs land in the session folder. `skills/` (materialized at boot) went at 17:57; `documents/` at 18:15.
   - The moment the rclone home mount comes up (`cloud_mount_active=True`), `cloud_cfg` becomes `None`, `workspace_sync` is never rebuilt, and the session folder **freezes**. Everything created afterward (`knowledge/`, `notes/`, `output/`, `documents/external`) never syncs.
4. **Why *those* dirs and not others.** `SYNC_IGNORE_PATTERNS` (`src/services/cloud_sync/base.py:26`): `.git/ repos/ projects/ tools/ archive/ chunks/ candidates/ requirements/ todos.yaml workspace.md datasources.md spec_lock.md plan.md` — so `tools/ repos/ datasources.md` are *never* pushed. `knowledge/ notes/ output/` aren't ignored; they're absent purely because they were created after the freeze. Net: "documents + skills" is an accidental snapshot of the pre-mount window minus ignores.

### Impact on the fix
Strengthens **fix (5)**. The partial-snapshot behavior is *worse* UX than "empty": a user seeing `documents/ + skills/` reasonably assumes a live (if incomplete) mirror and concludes their later work was lost, when it's a frozen startup artifact. Cheapest correct behavior stays: under `rclone_mount`, stop stamping the handle (and stop creating/ sharing the folder) when a `project_default`/`project` mount exists, and make `_resolve_cloud_session_url` prefer the mounted surface, so the button opens `/cloud/home`.

### Re-inspect quickly (future pickup)
```
kubectl --context main get pods -n superhuman-remote-worker -o wide | grep ws-thread-<id8>
# handle (table is `threads`, NOT persistent_threads):
#   PGPASSWORD=$POSTGRES_PASSWORD psql -h srw-postgres -U srw -d srw \
#     -c "SELECT main_cloud_session_handle FROM threads WHERE id='<full-uuid>';"
# session-folder contents (user token via the mount helper):
#   TOK=$(/home/agent-host/.cache/srw/rclone/<uuid>/home/bearer-helper.sh)
#   curl -H "Authorization: Bearer $TOK" -X PROPFIND -H "Depth: 1" \
#     "http://srw-opencloud:9200/dav/spaces/<drive-with-%24>/sessions/<id8>/"
# mounts on the pod (expect ONLY /cloud/home):
#   kubectl --context main exec -n superhuman-remote-worker <pod> -- mount | grep fuse
```

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
