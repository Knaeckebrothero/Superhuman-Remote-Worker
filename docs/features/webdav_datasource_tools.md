---
tags:
  - cloud
  - main-cloud
  - webdav
  - datasources
  - naming
  - decision-record
aliases:
  - WebDAV datasource tools
  - cloud tools rename
related:
  - "[[main_cloud]]"
  - "[[project_cloud_folders]]"
  - "[[datasources]]"
  - "[[job_cloud_export]]"
---

# WebDAV datasource tools — naming + main-cloud separation

**Status:** ✅ Implemented 2026-06-07 (uncommitted on `develop`). Decision record, not a forward plan.

## What this resolves

The agent speaks WebDAV from two places that were conflated under the name "cloud":

1. **The `cloud_*` tools** (`src/tools/cloud/webdav.py`) — imperative agent file access (`list/read/info/write/delete`) to a **WebDAV datasource**. A datasource concern, dispatched by the generic `DS_TOOL_MAP` exactly like `postgresql`→`sql`.
2. **The main-cloud mirror** (`src/services/cloud_sync/`) — the platform's session/project folder, **passively synced** into the agent's workspace. The agent never reaches it through tools.

The tools were *originally* how a job reached its project cloud folder — hence "cloud." That role was later replaced by **cloning the project folder into the workspace** (Mode-A baseline for jobs; the `projects/` sync mount for sessions). The tool path and its project-folder dispatch were never cleaned up, leaving two problems:

- **Misleading name.** `cloud_*` implies a main-cloud connection; the tools are really generic WebDAV-datasource tools. The name fooled readers into thinking they were part of the main-cloud subsystem.
- **Double-exposure.** Project provisioning auto-created a `Cloud Storage (<project>)` webdav datasource pointed at the project folder (`main.py` `_ensure_project_cloud_resources` + the `init.py` backfill) *and* the same folder was cloned into the workspace — so an agent saw the identical files two ways.

## Decision (and what was rejected)

We **considered** consolidating the agent file tier (mirror + tools) behind one WebDAV transport seam (the "cheaper interim" in `main_cloud.md` Issue 1 / Direction §3). **Rejected** it: the live consumer (the mirror) is *already* cleanly abstracted behind `WorkspaceSyncBase`, and the un-abstracted tool path is a *separate datasource concern*, not main-cloud — so a unifying seam would have solved a non-problem and coupled two deliberately-separate responsibilities.

Instead, the minimal correct fix:

1. **Rename `cloud_*` → `webdav_*`** (tool fns, the `cloud` tool category → `webdav`, `ToolsConfig.cloud` → `.webdav`, the module path, **both** dispatch sides — `orchestrator/main.py` `DS_TOOL_MAP` and `persistent_app.py` `_ds_tool_map` — the `datasources.md` note, and tests). The directory moved `src/tools/cloud/` → `src/tools/webdav/` (`webdav.py` → `tools.py`).
2. **Stop attaching the project working folder as a webdav datasource.** Removed the `Cloud Storage (<project>)` `create_datasource` + `link_datasource_to_project` from both the runtime heal path and the startup backfill, with a code breadcrumb. **Kept** the folder/group provisioning (the clone needs the folder to exist).
3. **Keep the tools for clouds that are NOT cloned** — the user's personal home cloud (`Cloud Storage (Personal)`, `main.py:17419`) and any externally-attached BYO WebDAV datasource.

## Net model after the change

| Surface | Access path |
|---|---|
| **Project working folder** | Cloned into the workspace (Mode-A / `projects/` mount). No tools. |
| **Session folder** | Passively mirrored into the workspace. No tools. |
| **Personal home cloud + BYO external WebDAV** | `webdav_*` tools (datasource), imperative. |

## Verification

`ruff` + `py_compile` both sides clean; import smoke shows the 5 tools register as `webdav_list/read/info/write/delete`; 553 tests green across registry / tool-loading / config / datasource / project / mount suites; no test asserted the removed datasource creation.

## Follow-ups (not done here)

- **Existing rows.** `Cloud Storage (<project>)` webdav datasource rows already created on running clusters persist (the change only stops creating new ones). A one-off cleanup — delete project-folder webdav datasources whose URL matches the project's `main_cloud_folder_handle` — would remove the double-exposure for existing projects. Deferred (dev pragmatism).
- **Historical docs.** `sso_and_cloud_storage.md`, `project_cloud_folders.md`, `multi_datasource_support.md` still describe the old `cloud_*`-tools-for-project-folders model; left as-is (historical), not rewritten.
- **Issue 1 proper** (native non-WebDAV APIs / the data-plane driver seam) remains deferred and unaffected.
