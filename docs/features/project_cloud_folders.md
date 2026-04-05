---
tags:
  - feature
  - architecture
  - projects
  - cloud-infrastructure
  - nextcloud
aliases:
  - project cloud folders
  - nextcloud integration
  - project file storage
related:
  - "[[projects]]"
  - "[[project_knowledge_base]]"
  - "[[sso_and_cloud_storage]]"
  - "[[datasources]]"
---

# Project & Session Cloud Folders — Automatic Nextcloud Integration

Automatically provision Nextcloud folders at two scopes:

1. **Project folders** — a Group Folder (Team Folder) per project. Users upload reference material; agents get `cloud_*` tool access. Personal projects map to the user's Nextcloud home directory.
2. **Session folders** — a per-session folder shared between the user and the persistent agent. Workspace files sync bidirectionally so user and AI can exchange files in real time.

**Status:** All phases complete (1-7). Nextcloud init, NextcloudAdmin service, DB migration, lifecycle wiring, settings + override, cockpit UI, session cloud folders, session UI, backfill script.

**Depends on:** [[sso_and_cloud_storage]] (Keycloak SSO + Nextcloud deployment + WebDAV datasource type).

## The Problem

Today, getting files to an agent requires uploading them through the cockpit one-by-one during job creation. There is no persistent, browsable file space shared between project members and agents. Users cannot:

- Maintain a living document library that all jobs in a project draw from
- Organize reference material in folders before any job exists
- Give the agent a place to drop files that project members can browse natively
- Share a file space across project members without configuring datasources manually

The WebDAV datasource exists, but it requires manual setup: create a Nextcloud folder, configure sharing, create a datasource record with the right URL and credentials, attach it to jobs. This is too many steps for what should be automatic.

## The Solution

Wire project lifecycle events to Nextcloud Group Folders so that every project automatically gets a shared folder, and every job in that project automatically gets WebDAV access to it.

### End-to-End Flow

```
User creates project in cockpit
  │
  ├─→ Orchestrator creates Keycloak group `project-{id}` (existing)
  ├─→ Orchestrator pre-creates matching Nextcloud group via Provisioning API
  ├─→ Orchestrator creates Nextcloud Group Folder named `{project-name}`
  ├─→ Orchestrator grants the Nextcloud group + `srw-agents` group access to the folder
  └─→ Orchestrator creates a project-scoped `webdav` datasource pointing to the folder
        │
        ▼
User opens "Project Files" button → Nextcloud folder (SSO, no second login)
User uploads documents, creates subfolders, organizes files
        │
        ▼
User creates job in project
  │
  ├─→ Datasource resolution: project-scoped `webdav` datasource inherited (existing mechanism)
  ├─→ Agent receives `cloud_*` tools (cloud_list, cloud_read, cloud_info, cloud_write, cloud_delete)
  └─→ Agent can browse, read, and (if allowed) write to the project's Nextcloud folder
```

### Personal Projects

Every user has a default project (`is_default = true`). For personal projects, there is no Group Folder to create — the user's Nextcloud home directory already exists (provisioned automatically on first Keycloak SSO login).

The orchestrator creates a project-scoped `webdav` datasource pointing to `{NEXTCLOUD_URL}/remote.php/dav/files/{username}/`. This gives the agent access to the user's entire Nextcloud home directory.

**Trade-off:** Full root access means the agent could touch unrelated files. This is acceptable for simplicity — users can organize sensitive files into folders they don't point the agent at, and the read-only toggle (see below) provides a safety net. A scoped subfolder (e.g., `~/SRW/`) can be added later as an option without breaking anything.

### Shared Projects

For non-default projects, the orchestrator provisions a Nextcloud Group Folder:

1. **Pre-create Nextcloud group** via the Provisioning API (`POST /ocs/v2.php/cloud/groups`) — this ensures the group exists in Nextcloud before any member has logged in via OIDC (see "Group Sync Gotchas" below)
2. **Create Group Folder** via the Group Folders API (`POST /index.php/apps/groupfolders/folders`)
3. **Grant group access** with full permissions (`POST /index.php/apps/groupfolders/folders/{id}/groups`)
4. **Grant `srw-agents` group access** — the service account group for agent WebDAV access (see "Agent WebDAV Access Strategy")
5. **Create project-scoped datasource** with `connection_url` pointing to the Group Folder's WebDAV path

Keycloak group membership is already managed by the existing project member system (`keycloak_admin.py`). When a user is added to or removed from a project, their Keycloak group membership updates, and Nextcloud syncs on the user's next OIDC login.

```
Nextcloud/
├── Group Folders/
│   ├── Project Alpha/          ← Group Folder, accessible to `project-{alpha-id}` group
│   │   ├── research-papers/    ← User-managed subfolder structure
│   │   ├── datasets/
│   │   └── specs.pdf
│   ├── Project Beta/           ← Another project's folder
│   │   └── ...
│
├── admin/                      ← Personal files (admin user)
│   └── ...
├── jane/                       ← Personal files (user jane)
│   └── ...                     ← Default project datasource points here
```

No prescribed subfolder structure — users organize files however they want. The agent can navigate the entire folder tree via `cloud_list`.

## Agent WebDAV Access Strategy

Group Folders are accessible via WebDAV at two paths:

| Path | What it shows |
|------|---------------|
| `/remote.php/dav/files/{username}/{FolderName}/` | The Group Folder appears as a top-level folder inside the user's regular file tree |
| `/remote.php/dav/groupfolders/{username}/` | Lists **only** Group Folders the user has access to (no personal files) |

**Important:** `__groupfolders/{id}` is an internal *filesystem* path on disk, not a WebDAV path. It is not accessible via the WebDAV API.

Both WebDAV paths require the authenticated user to be a member of a group that has been granted access to the folder. There is no admin-bypass — even admin users must be in a group with folder access.

### Chosen Approach: Dedicated `srw-agents` Group

Create a Nextcloud group `srw-agents` (pre-created during initialization). The agent service account (`agent-service` Nextcloud user) is a member of this group. When a Group Folder is created, `srw-agents` is also granted access alongside the project's Keycloak group.

```python
# On folder creation:
# 1. Grant project members access
await nextcloud.grant_group_access(folder_id, f"project-{project_id}", permissions=31)
# 2. Grant agent service account access
await nextcloud.grant_group_access(folder_id, "srw-agents", permissions=31)
```

The datasource `connection_url` then uses the dedicated groupfolders WebDAV endpoint:

```
{NEXTCLOUD_URL}/remote.php/dav/groupfolders/agent-service/{FolderName}/
```

This is cleaner than the `/remote.php/dav/files/` path because it avoids mixing personal files with project folders.

**Why not just add the service account to every project's Keycloak group?** That would work, but it pollutes the group membership — the agent account would appear as a project member in the cockpit's member list, Keycloak admin console, and anywhere group membership is displayed. A dedicated `srw-agents` group keeps the separation clean.

## Group Sync Gotchas

The `user_oidc` app syncs Keycloak group memberships to Nextcloud, but with important caveats that affect this design:

### 1. Groups Must Exist Before Folder Assignment

The Group Folders API requires the Nextcloud group to exist before it can be granted access to a folder. OIDC group provisioning only creates groups when a member of that group logs in. If we create a project and immediately try to assign its group to a folder, the group may not exist yet in Nextcloud.

**Mitigation:** Pre-create the Nextcloud group via the Provisioning API before calling the Group Folders API:

```bash
curl -u admin:password -X POST \
  '{NEXTCLOUD_URL}/ocs/v2.php/cloud/groups?format=json' \
  -H "OCS-APIRequest: true" \
  -d groupid="project-{uuid}"
```

This is idempotent — if the group already exists (from a prior OIDC login), the call returns a "group exists" response which we ignore.

### 2. Membership Syncs on Login Only

`user_oidc` syncs group membership at OIDC login time, not in real-time. When the orchestrator adds a user to a Keycloak group (via `keycloak_admin.add_user_to_project_group()`), the user won't see the Group Folder in Nextcloud until they log in again.

**Mitigation (optional):** Use the Nextcloud Provisioning API to add the user to the Nextcloud group immediately, in parallel with the Keycloak group addition:

```bash
curl -u admin:password -X POST \
  '{NEXTCLOUD_URL}/ocs/v2.php/cloud/users/{user_id}/groups?format=json' \
  -H "OCS-APIRequest: true" \
  -d groupid="project-{uuid}"
```

This provides immediate access. The next OIDC login will re-confirm the membership from Keycloak.

### 3. OIDC May Remove Users From Non-Token Groups

When `user_oidc` group provisioning is enabled, users are removed from Nextcloud groups that are **not** present in their OIDC token on login. If the `srw-agents` group or any manually-managed group is not in the token, users/service accounts could lose membership.

**Mitigation:** Configure the `--group-whitelist-regex` on the `user_oidc` provider to restrict which groups OIDC manages:

```bash
occ user_oidc:provider "Keycloak" \
  --group-provisioning=1 \
  --group-whitelist-regex='/^project-/'
```

This tells `user_oidc` to only sync groups matching `project-*`. The `srw-agents` group and any other manually-managed groups are left untouched.

### 4. New Members May Not See Existing Group Folders (NC Bug)

There is an [open Nextcloud bug](https://github.com/nextcloud/server/issues/57445) (confirmed NC 28+, NC 31) where adding a user to a group that already has a Group Folder does not propagate the share to the new member. The user cannot see the folder until the share is refreshed.

**Mitigation:** After adding a user to a project group, trigger a share refresh. The least invasive approach is to temporarily revoke and re-grant the group's access to the folder:

```python
async def refresh_group_folder_access(self, folder_id: int, group_id: str, permissions: int = 31):
    """Workaround for NC server#57445 — re-grant group access to propagate to new members."""
    await self._delete(f"/index.php/apps/groupfolders/folders/{folder_id}/groups/{group_id}")
    await self._post(f"/index.php/apps/groupfolders/folders/{folder_id}/groups", {"group": group_id})
    await self._post(f"/index.php/apps/groupfolders/folders/{folder_id}/groups/{group_id}", {"permissions": permissions})
```

This should be called when a new member is added to a project. Monitor the upstream issue — once fixed, this workaround can be removed.

**Alternative**: Combine this with the Provisioning API approach from gotcha #2 (add user directly to Nextcloud group). Test which approach reliably resolves the issue. The double-wield (direct Nextcloud group add + folder access re-grant) is the safest bet until the bug is fixed.

## Read/Write Toggle

Cloud storage access is configurable as read-only or read-write, with project-level defaults and job-level overrides. This follows the same inheritance pattern as `default_config_name` / `config_override` on projects and jobs.

### Project-Level Default

Every project has a `cloud_storage_read_only` setting (default: `false` — read-write). This is set during project creation and can be changed in the project settings UI.

```sql
-- Migration: Add cloud storage read-only default to projects table
DO $$ BEGIN
    ALTER TABLE projects ADD COLUMN cloud_storage_read_only BOOLEAN NOT NULL DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
```

When the project-scoped WebDAV datasource is created (at project creation time), its `read_only` flag is set from this project default.

### Job-Level Override

Jobs can override the project default. The job creation form includes an optional toggle for cloud storage access mode:

- **Inherit from project** (default) — uses whatever the project's `cloud_storage_read_only` is set to
- **Read-only** — agent can browse and download, but not upload, modify, or delete
- **Read-write** — agent has full access

Implementation: the orchestrator's `resolve_datasources_for_job()` already returns one datasource per type with job > project > global precedence. For the job-level override:

- If the job has its own `webdav` datasource (job-scoped) → that wins (existing behavior)
- If not, the project-scoped `webdav` datasource is used, but the `read_only` flag can be overridden at dispatch time via the job's metadata

This override is stored in the job's `context` JSONB field:

```json
{
  "cloud_storage_read_only": true
}
```

The orchestrator checks `job.context.cloud_storage_read_only` when building the datasource payload. If present, it overrides the datasource's own `read_only` flag for that job. If absent, the datasource's flag (which reflects the project default) applies.

### Tool Filtering

The existing `DS_TOOL_MAP` already handles read-only vs. read-write:

```python
DS_TOOL_MAP = {
    "webdav": {
        "category": "cloud",
        "read": ["cloud_list", "cloud_read", "cloud_info"],
        "write": ["cloud_list", "cloud_read", "cloud_info", "cloud_write", "cloud_delete"],
    },
}
```

When `read_only=true`, only `read` tools are injected. When `read_only=false`, `write` tools are also available. No changes needed to this mechanism.

## Cockpit UI Changes

### Project Detail Page

Add an **"Open Project Folder"** button that deep-links to the project's Nextcloud folder:

- **Personal projects:** `{NEXTCLOUD_PUBLIC_URL}/apps/files/?dir=/` (user's home)
- **Shared projects:** `{NEXTCLOUD_PUBLIC_URL}/apps/files/?dir=/{FolderName}` (Group Folder mount point appears at the root of the user's file tree)

The button opens in a new tab. Keycloak SSO means the user is already authenticated — no second login.

### Project Settings

Add a **Cloud Storage** section to the project settings page:

```
Cloud Storage
─────────────────────────────────────
Folder:       Project Alpha                    [Open in Nextcloud ↗]
Agent access: ◉ Read & Write  ○ Read Only
              Jobs inherit this setting unless overridden.
─────────────────────────────────────
```

The toggle updates `projects.cloud_storage_read_only` via `PATCH /api/projects/{id}`.

### Job Creation Form

Add an optional **Cloud Storage Access** selector when creating a job in a project that has a cloud folder:

```
Cloud Storage Access
  ◉ Inherit from project (currently: Read & Write)
  ○ Read Only
  ○ Read & Write
```

This maps to `context.cloud_storage_read_only` in the job creation payload.

### Job Detail / Sidebar

Show a small indicator when a job has cloud storage attached:

```
Datasources:  PostgreSQL (analytics-db)  ·  Cloud Storage (Project Alpha) [R/W]
```

The "Cloud Storage" chip links to the Nextcloud folder.

## API Changes

### Project Endpoints

**`POST /api/projects`** — Extended to provision the Nextcloud folder and WebDAV datasource automatically after creating the project and Keycloak group.

**`PATCH /api/projects/{id}`** — Accepts `cloud_storage_read_only` field. When changed, updates the project-scoped WebDAV datasource's `read_only` flag to match.

**`DELETE /api/projects/{id}`** — Extended to delete the Nextcloud Group Folder (via Group Folders API) and the project-scoped WebDAV datasource.

**`GET /api/projects/{id}`** — Response includes `cloud_storage_read_only` and `cloud_storage_url` (deep link to Nextcloud folder) for the cockpit to render the button.

### New: Nextcloud Admin Service

New orchestrator service `orchestrator/services/nextcloud_admin.py` that wraps the Nextcloud Group Folders API and Provisioning API:

```python
class NextcloudAdmin:
    """Manages Nextcloud Group Folders and groups for project integration.

    Uses two Nextcloud APIs:
    - Group Folders API: /index.php/apps/groupfolders/folders (requires groupfolders app)
    - Provisioning API: /ocs/v2.php/cloud/ (built-in, for group/user management)

    All requests require admin auth and the OCS-APIRequest: true header.
    Append ?format=json to all URLs for JSON responses.
    """

    def __init__(self, base_url: str, admin_user: str, admin_password: str):
        self.base_url = base_url  # e.g., http://nextcloud:80
        self.auth = (admin_user, admin_password)
        self.headers = {"OCS-APIRequest": "true"}

    async def ensure_initialized(self) -> bool:
        """Check that Nextcloud is reachable and the groupfolders app is installed."""

    # --- Group Management (Provisioning API) ---

    async def ensure_group(self, group_id: str) -> None:
        """Create a Nextcloud group if it doesn't exist.
        POST /ocs/v2.php/cloud/groups {groupid: group_id}"""

    async def add_user_to_group(self, user_id: str, group_id: str) -> None:
        """Add a user to a Nextcloud group (immediate, doesn't wait for OIDC login).
        POST /ocs/v2.php/cloud/users/{user_id}/groups {groupid: group_id}"""

    async def remove_user_from_group(self, user_id: str, group_id: str) -> None:
        """Remove a user from a Nextcloud group.
        DELETE /ocs/v2.php/cloud/users/{user_id}/groups {groupid: group_id}"""

    # --- Group Folder Management (Group Folders API) ---

    async def create_project_folder(
        self, project_id: UUID, project_name: str, group_id: str
    ) -> int:
        """Create a Group Folder and grant access to the project group + srw-agents.
        Returns the Nextcloud Group Folder ID.

        Steps:
        1. POST /index.php/apps/groupfolders/folders {mountpoint: project_name}
        2. POST /index.php/apps/groupfolders/folders/{id}/groups {group: group_id}
        3. POST /index.php/apps/groupfolders/folders/{id}/groups/{group_id} {permissions: 31}
        4. POST /index.php/apps/groupfolders/folders/{id}/groups {group: "srw-agents"}
        5. POST /index.php/apps/groupfolders/folders/{id}/groups/srw-agents {permissions: 31}
        """

    async def delete_project_folder(self, folder_id: int) -> None:
        """Delete a Group Folder.
        DELETE /index.php/apps/groupfolders/folders/{folder_id}"""

    async def refresh_group_folder_access(
        self, folder_id: int, group_id: str, permissions: int = 31
    ) -> None:
        """Workaround for NC server#57445 — re-grant group access to propagate to new members.
        Removes and re-adds the group to the folder."""

    async def get_folder_mount_point(self, folder_id: int) -> str:
        """Return the mount point name for a Group Folder.
        GET /index.php/apps/groupfolders/folders/{folder_id} → .mount_point"""

    # --- URL Generation ---

    def get_folder_webdav_url(self, folder_name: str) -> str:
        """Return the WebDAV URL for agent access to a Group Folder.
        Uses the dedicated groupfolders endpoint: /remote.php/dav/groupfolders/{service_user}/{folder_name}/"""
        return f"{self.base_url}/remote.php/dav/groupfolders/agent-service/{folder_name}/"

    def get_folder_browser_url(self, folder_name: str, public_url: str) -> str:
        """Return the browser URL for deep-linking to a Group Folder.
        Group Folders appear at the root of the user's file tree."""
        return f"{public_url}/apps/files/?dir=/{folder_name}"

    def get_user_home_webdav_url(self, username: str) -> str:
        """Return the WebDAV URL for a user's home directory (personal projects)."""
        return f"{self.base_url}/remote.php/dav/files/{username}/"
```

### Group Folders API Reference

The [Group Folders app](https://github.com/nextcloud/groupfolders) (renamed "Team Folders" in the App Store, internal ID still `groupfolders`) exposes a REST API at `/index.php/apps/groupfolders/folders`. **Note:** this is *not* under the standard `/ocs/v2.php/` path — see [groupfolders#1019](https://github.com/nextcloud/groupfolders/issues/1019).

All requests require:
- `OCS-APIRequest: true` header (CSRF protection)
- Admin Basic Auth (`Authorization: Basic ...`)
- `?format=json` query param for JSON responses (default is XML)

| Operation | Method | Endpoint | Body |
|-----------|--------|----------|------|
| List folders | `GET` | `/index.php/apps/groupfolders/folders` | — |
| Create folder | `POST` | `/index.php/apps/groupfolders/folders` | `mountpoint={name}` |
| Get folder | `GET` | `/index.php/apps/groupfolders/folders/{id}` | — |
| Delete folder | `DELETE` | `/index.php/apps/groupfolders/folders/{id}` | — |
| Rename folder | `PUT` | `/index.php/apps/groupfolders/folders/{id}` | `mountPoint={name}` (camelCase) |
| Grant group | `POST` | `/index.php/apps/groupfolders/folders/{id}/groups` | `group={group_id}` |
| Revoke group | `DELETE` | `/index.php/apps/groupfolders/folders/{id}/groups/{group}` | — |
| Set permissions | `POST` | `/index.php/apps/groupfolders/folders/{id}/groups/{group}` | `permissions={bitmask}` |
| Set quota | `POST` | `/index.php/apps/groupfolders/folders/{id}/quota` | `quota={bytes}` (`-3` = unlimited) |
| Toggle ACL | `POST` | `/index.php/apps/groupfolders/folders/{id}/acl` | `acl={bool}` |

Permission bitmask: `1` (read) + `2` (update) + `4` (create) + `8` (delete) + `16` (share) = `31` (all).

Full OpenAPI spec: [groupfolders/openapi.json](https://petstore.swagger.io/?url=https://raw.githubusercontent.com/nextcloud/groupfolders/master/openapi.json)

The app is compatible with Nextcloud 31 (version 19.x). It was renamed to "Team Folders" in the App Store but the app ID and all API paths remain `groupfolders`.

## Database Changes

### Projects Table

```sql
-- New column: cloud storage read-only default for the project
DO $$ BEGIN
    ALTER TABLE projects ADD COLUMN cloud_storage_read_only BOOLEAN NOT NULL DEFAULT FALSE;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

-- New column: Nextcloud Group Folder ID (NULL for personal/default projects)
DO $$ BEGIN
    ALTER TABLE projects ADD COLUMN nextcloud_folder_id INTEGER;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
```

`nextcloud_folder_id` stores the Nextcloud Group Folder ID returned by the API on creation. Used for deletion, rename sync, and folder detail lookups. `NULL` for personal/default projects (they use the user's home directory, not a Group Folder).

### No Changes to Other Tables

- **`datasources`** — no schema changes. The project-scoped WebDAV datasource is a regular row.
- **`jobs`** — no schema changes. The `context` JSONB field already exists and handles the read-only override.

## Nextcloud Setup

### Group Folders App Installation

The Group Folders app must be installed and enabled during Nextcloud initialization. Add to the existing OIDC setup hook (`deployment/19-nextcloud.yaml` for K8s, or a docker-compose init script):

```bash
# Install and enable the Group Folders app
php occ app:install groupfolders
php occ app:enable groupfolders
```

For docker-compose, add to the same init sequence that configures `user_oidc`. The K8s deployment already has a `before-starting` hook in the `srw-nextcloud-hooks` ConfigMap — extend it.

### OIDC Group Provisioning Configuration

Enable group provisioning with a whitelist regex so only `project-*` groups are OIDC-managed (protects `srw-agents` and other manually-managed groups from being purged):

```bash
php occ user_oidc:provider "Keycloak" \
  --group-provisioning=1 \
  --group-whitelist-regex='/^project-/'
```

This ensures:
- Groups matching `project-*` are auto-created and synced from Keycloak tokens
- The `srw-agents` group and any other non-project groups are left alone
- Users are not removed from non-project groups on OIDC login

### Service Account Setup

Create the `agent-service` Nextcloud user and `srw-agents` group during initialization:

```bash
# Create the agent service group
php occ group:add srw-agents

# Create the agent service account
export OC_PASS="$(openssl rand -base64 32)"
php occ user:add --password-from-env --group=srw-agents agent-service

# Generate an app password for WebDAV access (more secure than the main password)
# App passwords are created via the OCS Provisioning API or Nextcloud UI
```

The service account credentials are stored in datasource records. For dev/testing, the admin account can be used instead (the admin has access to user files via `/remote.php/dav/files/{admin}/` but NOT to Group Folders unless explicitly granted).

### Environment Variables

No new required env vars. The existing Nextcloud configuration from `docker-compose.yaml` provides everything:

| Variable | Source | Used For |
|----------|--------|----------|
| `NEXTCLOUD_URL` | docker-compose / .env | Internal API base URL (`http://nextcloud:80`) |
| `NEXTCLOUD_PUBLIC_URL` | .env (defaults to `http://localhost:8800`) | Browser-facing URL for deep-links |
| `NEXTCLOUD_ADMIN_USER` | docker-compose | Group Folders API auth |
| `NEXTCLOUD_ADMIN_PASSWORD` | docker-compose | Group Folders API auth |

```bash
# Optional overrides in .env
NEXTCLOUD_URL=http://nextcloud:80                  # Internal (container-to-container)
NEXTCLOUD_PUBLIC_URL=http://localhost:8800          # Browser-facing (deep-links)
```

## Lifecycle Events

### Project Created

```python
async def create_project(name, user_id, ...):
    # 1. Insert project row (existing)
    project = await db.create_project(name, ...)

    # 2. Create Keycloak group (existing)
    group_name = f"project-{project.id}"
    await keycloak_admin.ensure_project_group(project.id)
    await keycloak_admin.add_user_to_project_group(user_id, project.id)

    # 3. NEW: Pre-create Nextcloud group (so it exists before folder assignment)
    await nextcloud_admin.ensure_group(group_name)

    # 4. NEW: Create Nextcloud Group Folder (skip for personal/default projects)
    if not project.is_default:
        folder_id = await nextcloud_admin.create_project_folder(
            project_id=project.id,
            project_name=name,
            group_id=group_name
        )
        await db.update_project(project.id, nextcloud_folder_id=folder_id)

        # 5. NEW: Create project-scoped WebDAV datasource
        webdav_url = nextcloud_admin.get_folder_webdav_url(name)
        await db.create_datasource(
            name=f"Cloud Storage ({name})",
            description=f"Shared file storage for project '{name}'",
            type="webdav",
            connection_url=webdav_url,
            credentials={"username": "agent-service", "password": AGENT_SERVICE_PASSWORD},
            read_only=project.cloud_storage_read_only,  # default: False
            project_id=project.id
        )
```

### User's Default Project Initialized

When a user is created and their default project is set up:

```python
async def setup_default_project(user_id, username, project_id):
    # Create project-scoped WebDAV datasource pointing to user's Nextcloud home
    webdav_url = nextcloud_admin.get_user_home_webdav_url(username)
    await db.create_datasource(
        name="Cloud Storage (Personal)",
        description="Personal Nextcloud file storage",
        type="webdav",
        connection_url=webdav_url,
        credentials={"username": NEXTCLOUD_ADMIN_USER, "password": NEXTCLOUD_ADMIN_PASSWORD},
        read_only=False,
        project_id=project_id
    )
```

Note: personal projects use the admin account to access the user's files (Nextcloud admins can access any user's files via `/remote.php/dav/files/{admin}/` or the Files app API). For production, per-user OAuth token delegation should replace this.

### Project Deleted

```python
async def delete_project(project_id):
    project = await db.get_project(project_id)

    # Delete Nextcloud Group Folder (if not a personal project)
    if project.nextcloud_folder_id:
        await nextcloud_admin.delete_project_folder(project.nextcloud_folder_id)

    # Delete Keycloak group (existing)
    await keycloak_admin.delete_project_group(project_id)

    # Cascade: project-scoped datasources deleted via ON DELETE CASCADE
    await db.delete_project(project_id)
```

### Member Added to Project

```python
async def add_project_member(project_id, user_id, role):
    # 1. Insert project_members row (existing)
    await db.add_project_member(project_id, user_id, role)

    # 2. Add to Keycloak group (existing)
    await keycloak_admin.add_user_to_project_group(user_id, project_id)

    # 3. NEW: Add to Nextcloud group directly (immediate access, don't wait for OIDC login)
    nc_username = await get_nextcloud_username(user_id)
    group_name = f"project-{project_id}"
    await nextcloud_admin.add_user_to_group(nc_username, group_name)

    # 4. NEW: Refresh folder access (workaround for NC server#57445)
    project = await db.get_project(project_id)
    if project.nextcloud_folder_id:
        await nextcloud_admin.refresh_group_folder_access(
            project.nextcloud_folder_id, group_name
        )
```

### Member Removed from Project

```python
async def remove_project_member(project_id, user_id):
    # 1. Remove from project_members (existing)
    await db.remove_project_member(project_id, user_id)

    # 2. Remove from Keycloak group (existing)
    await keycloak_admin.remove_user_from_project_group(user_id, project_id)

    # 3. NEW: Remove from Nextcloud group directly (immediate revocation)
    nc_username = await get_nextcloud_username(user_id)
    await nextcloud_admin.remove_user_from_group(nc_username, f"project-{project_id}")
```

### Cloud Storage Setting Changed

```python
async def update_project_cloud_storage(project_id, read_only: bool):
    # Update project column
    await db.update_project(project_id, cloud_storage_read_only=read_only)

    # Update the project-scoped WebDAV datasource to match
    ds = await db.get_project_datasource(project_id, type="webdav")
    if ds:
        await db.update_datasource(ds.id, read_only=read_only)
```

## Agent Perspective

From the agent's point of view, nothing changes architecturally. The agent receives a WebDAV datasource in its `JobStartRequest.datasources` list and gets `cloud_*` tools — exactly the same as a manually configured WebDAV datasource. The automation is entirely on the orchestrator side.

What the agent can do with the project folder:

```
cloud_list("/")                          → browse folder structure
cloud_list("/research-papers/")          → list files in a subfolder
cloud_read("/specs.pdf")                 → download file to workspace
cloud_info("/datasets/training.csv")     → check file size, modified date
cloud_write("output/report.pdf", "/")    → upload from workspace to cloud (if read-write)
cloud_delete("/old-draft.docx")          → delete file (if read-write)
```

The agent sees all files and subfolders within the project's shared folder. Users manage the folder structure — the agent navigates whatever is there.

## Graceful Degradation

All Nextcloud operations in project lifecycle events must be **non-blocking and fail-soft**. If Nextcloud is unreachable or the Group Folders app is not installed:

- Project creation still succeeds (the project just won't have a cloud folder)
- `nextcloud_folder_id` remains `NULL`, `cloud_storage_url` is not generated
- No WebDAV datasource is created → jobs in this project don't get `cloud_*` tools
- The "Open Project Folder" button is hidden in the cockpit when `cloud_storage_url` is absent
- Log a warning so admins know the integration is inactive

This follows the same pattern as `KeycloakGroupSync` — it logs warnings when Keycloak is unavailable but doesn't block project operations.

A health check on startup (`nextcloud_admin.ensure_initialized()`) validates connectivity and app availability, logging the result. The cockpit's project settings page can show a status indicator:

```
Cloud Storage: ⚠ Nextcloud unavailable — cloud folders disabled
```

## Session Cloud Folders (Persistent Agent)

Persistent agent sessions (interactive chat via WebSocket, stored in the `threads` table) get their own Nextcloud folder that mirrors the session's workspace. This gives users a drag-and-drop file exchange surface with the AI — upload a PDF in Nextcloud, the agent sees it; the agent writes a report, the user downloads it from Nextcloud.

### How Sessions Differ from Jobs

| Aspect | Worker Job | Persistent Session |
|--------|-----------|-------------------|
| **Lifecycle** | Finite: created → processing → completed | Long-lived: created → active ↔ idle → ended |
| **Interaction** | Autonomous, user reviews output after | Interactive, real-time chat via WebSocket |
| **Workspace** | `/workspace/job_{uuid}/` | `/workspace/{thread_id}/` |
| **File exchange** | Upload at creation time, view output in Gitea | Currently: only via agent tools (`read_file`/`write_file`) |
| **Cloud folder need** | Low (project folder covers reference material) | **High** (user and agent need real-time file exchange) |

Worker jobs benefit from the project-level cloud folder (shared reference material). Sessions need something more: a **private, per-session folder** where the user and agent actively exchange working files during the conversation.

### Design

When a persistent session starts, the orchestrator creates a personal Nextcloud folder for that session and shares it with the session's user. The agent's workspace is synced bidirectionally with this folder.

```
User creates/resumes session in cockpit
  │
  ├─→ Orchestrator creates Nextcloud folder in agent-service's home:
  │     /remote.php/dav/files/agent-service/sessions/{thread_short_id}/
  ├─→ Orchestrator shares the folder with the user via Nextcloud Share API
  ├─→ Session metadata stores the folder path + share token
  │
  ▼
User opens "Session Files" button in chat UI → Nextcloud folder (SSO)
User drops files into the folder
  │
  ▼
Workspace sync picks up new files → agent sees them in workspace
Agent writes output files → sync pushes to Nextcloud → user sees them
```

### Why Not a Group Folder?

Group Folders are overkill for sessions:
- Sessions are 1:1 (one user, one agent) — no group to share with
- Sessions are ephemeral — Group Folders are designed for persistent team storage
- Group Folder creation/deletion has overhead (admin API, group management)

Instead, use a **regular folder + Nextcloud Share API**:
1. Create folder in the `agent-service` user's home directory via WebDAV
2. Share it with the session's user via the OCS Share API (`POST /ocs/v2.php/apps/files_sharing/api/v1/shares`)
3. Delete folder when session ends

This is lightweight: one `MKCOL` WebDAV call + one OCS share call. No Group Folders app dependency for this part.

### Workspace Sync

The agent works on its local workspace (fast, reliable). A background sync process mirrors changes between the workspace and the Nextcloud folder:

```
┌─────────────┐          sync          ┌─────────────────┐
│  Workspace   │  ←──────────────────→  │  Nextcloud       │
│  /workspace/ │     (bidirectional)    │  /sessions/{id}/ │
│  {thread_id} │                        │                  │
└─────────────┘                         └─────────────────┘
      ↑                                        ↑
      │ local filesystem                       │ WebDAV / browser
      │                                        │
   Agent tools                              User
   (read_file, write_file,                  (drag & drop,
    run_command, etc.)                       browse, download)
```

**Sync mechanism: `WorkspaceSyncService`**

A lightweight service running inside the persistent agent process (not a separate container):

```python
class WorkspaceSyncService:
    """Bidirectional sync between local workspace and Nextcloud folder.

    Uses WebDAV for Nextcloud access. Sync is triggered:
    - After each agent turn (agent → Nextcloud): push new/modified workspace files
    - On a polling interval (Nextcloud → agent): pull new/modified user uploads
    - On explicit user action via chat command: /sync
    """

    def __init__(self, workspace_path: Path, webdav_client: Client, interval: int = 15):
        self.workspace_path = workspace_path
        self.client = webdav_client
        self.interval = interval  # seconds between pull checks
        self._local_state: dict[str, float] = {}   # path → mtime
        self._remote_state: dict[str, str] = {}     # path → etag

    async def push(self) -> list[str]:
        """Push locally modified files to Nextcloud. Returns list of pushed paths."""

    async def pull(self) -> list[str]:
        """Pull remotely modified files to workspace. Returns list of pulled paths."""

    async def full_sync(self) -> tuple[list[str], list[str]]:
        """Bidirectional sync. Returns (pushed, pulled)."""

    async def start_background_poll(self):
        """Start background polling loop for user uploads."""

    async def stop(self):
        """Stop background polling."""
```

**Sync rules:**
- **Agent → Nextcloud (push):** triggered after each completed turn. Compares workspace mtimes against last-known state. Uploads new/modified files. Skips internal files (`.git/`, `tools/`, `todos.yaml`, `archive/`).
- **Nextcloud → workspace (pull):** background poll every 15 seconds (configurable). Uses WebDAV `PROPFIND` with `ETag` comparison to detect changes. Downloads new/modified files to workspace `documents/` directory.
- **Conflict resolution:** last-write-wins. If both sides modified the same file since last sync, the more recent modification wins. For the initial implementation this is sufficient — true conflict resolution (merge, rename) is deferred.
- **Ignore patterns:** internal workspace files are excluded from sync: `.git/`, `tools/`, `todos.yaml`, `archive/`, `chunks/`, `candidates/`, `requirements/`. These are agent-internal and would confuse users.

**Sync integration in `persistent_graph.py`:**

```python
# After each turn completes (in run_persistent_loop):
if workspace_sync:
    pushed = await workspace_sync.push()
    if pushed:
        logger.info(f"Synced {len(pushed)} files to Nextcloud")

# Pull runs as background task, started in session setup:
# asyncio.create_task(workspace_sync.start_background_poll())
```

### Nextcloud Share API

Per-session folders use the standard Nextcloud sharing API (no Group Folders needed):

```bash
# Create the session folder via WebDAV MKCOL
curl -u agent-service:password -X MKCOL \
  '{NEXTCLOUD_URL}/remote.php/dav/files/agent-service/sessions/{thread_short_id}/'

# Share the folder with the user
curl -u agent-service:password -X POST \
  '{NEXTCLOUD_URL}/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json' \
  -H "OCS-APIRequest: true" \
  -d path="/sessions/{thread_short_id}" \
  -d shareType=0 \
  -d shareWith="{nc_username}" \
  -d permissions=15  # read + update + create + delete (no reshare)
```

Share types: `0` = user, `1` = group, `3` = public link. We use type `0` (direct user share) for privacy.

The shared folder appears in the user's Nextcloud file tree at the root level (e.g., as `Session abc123` or the session title). The user can rename it locally without affecting the agent's access.

### Session Lifecycle Integration

**Session Created / Agent Pod Starts:**

```python
async def setup_session_workspace(session: PersistentSession):
    # ... existing workspace setup ...

    # NEW: Create Nextcloud session folder + share with user
    if nextcloud_admin.available:
        folder_path = f"sessions/{session.thread_id_short}"
        await nextcloud_admin.create_session_folder(folder_path)
        share_id = await nextcloud_admin.share_with_user(
            folder_path, session.user_nc_username, permissions=15
        )
        session.metadata["nc_session_folder"] = folder_path
        session.metadata["nc_share_id"] = share_id

        # Start workspace sync
        session.workspace_sync = WorkspaceSyncService(
            workspace_path=session.workspace_path,
            webdav_client=session.nc_webdav_client,
        )
        # Initial push: sync existing workspace files to Nextcloud
        await session.workspace_sync.push()
        # Start background pull for user uploads
        asyncio.create_task(session.workspace_sync.start_background_poll())
```

**Session Ended (`/done` command):**

```python
async def archive_session(session: PersistentSession):
    # ... existing archive logic (memory extraction, title gen, git push) ...

    # NEW: Final sync + cleanup
    if session.workspace_sync:
        await session.workspace_sync.full_sync()  # ensure everything is up to date
        await session.workspace_sync.stop()

    # Delete the Nextcloud session folder (user has their copies via Nextcloud)
    if session.metadata.get("nc_session_folder"):
        await nextcloud_admin.delete_folder(session.metadata["nc_session_folder"])
```

**Session Idle → Resumed:**

When a session goes idle and the pod is reclaimed, the Nextcloud folder persists (it's on Nextcloud storage, not the pod). When the session resumes on a new pod:

1. Workspace is reconstructed from git (existing behavior)
2. `WorkspaceSyncService` reconnects to the existing Nextcloud folder
3. Pull any files the user uploaded while the session was idle
4. Resume background polling

### Database Changes

```sql
-- Add Nextcloud folder metadata to threads table
DO $$ BEGIN
    ALTER TABLE threads ADD COLUMN nc_session_folder TEXT;
EXCEPTION WHEN duplicate_column THEN null;
END $$;

DO $$ BEGIN
    ALTER TABLE threads ADD COLUMN nc_share_id INTEGER;
EXCEPTION WHEN duplicate_column THEN null;
END $$;
```

`nc_session_folder` stores the folder path within the `agent-service` user's home (e.g., `sessions/abc123`). `nc_share_id` stores the Nextcloud share ID for cleanup on session end.

### Cockpit Chat UI

Add a **"Session Files"** button in the chat header bar:

```
┌────────────────────────────────────────────────────┐
│  🔵 Session: Research Assistant     [Session Files ↗]  [⚙ Settings]  │
├────────────────────────────────────────────────────┤
│                                                    │
│  User: Can you analyze the dataset I just uploaded? │
│                                                    │
│  Agent: I see you've uploaded `sales_2025.csv` to  │
│  the session folder. Let me analyze it...          │
│                                                    │
```

The button links to `{NEXTCLOUD_PUBLIC_URL}/apps/files/?dir=/{FolderName}` (the shared folder as it appears in the user's file tree).

When the sync service pulls a new file from Nextcloud, the agent can optionally be notified via a system message injected into the conversation:

```
[System: User uploaded "sales_2025.csv" to the session folder]
```

This gives the agent context about new files without the user having to explicitly mention them in chat.

### Sync Ignore Patterns

Internal workspace files excluded from sync (both push and pull):

```python
SYNC_IGNORE_PATTERNS = [
    ".git/",          # git internals
    "tools/",         # auto-generated tool docs
    "todos.yaml",     # agent-internal task tracking
    "archive/",       # phase archives
    "chunks/",        # document chunking artifacts
    "candidates/",    # evaluation working files
    "requirements/",  # requirements analysis working files
    "workspace.md",   # agent memory (internal, rewritten frequently)
    "plan.md",        # agent plan (internal)
]
```

Everything else syncs: `documents/`, `output/`, user-created files/folders, analysis results, etc.

### Relationship to Project Cloud Folders

Session folders and project folders coexist and serve different purposes:

| Aspect | Project Cloud Folder | Session Cloud Folder |
|--------|---------------------|---------------------|
| **Scope** | All jobs + sessions in the project | One session only |
| **Lifetime** | Permanent (until project deleted) | Ephemeral (until session ended) |
| **Access** | All project members | Session owner + agent |
| **Content** | Reference material, shared datasets | Working files, drafts, uploads |
| **Mechanism** | Group Folder + project-scoped datasource | Personal folder + share + workspace sync |
| **Agent access** | `cloud_*` tools (explicit read/write) | Transparent via workspace sync |

A persistent session in a project has access to **both**: the project folder via `cloud_*` tools (for reference material) and the session folder via direct workspace access (for working files). The agent can pull a file from the project folder (`cloud_read`) into the workspace, work on it, and the result syncs to the session folder for the user to review.

## Scope and Deferral

### In Scope (This Feature)

**Project Cloud Folders:**
- Nextcloud Group Folder creation/deletion on project lifecycle events
- Project-scoped WebDAV datasource auto-creation
- Personal project → user home directory WebDAV mapping
- `cloud_storage_read_only` project column with job-level override via `context` JSONB
- Nextcloud group pre-creation and direct member management (bypass OIDC-only sync)
- Workaround for NC `server#57445` (re-grant group access on member add)
- OIDC group whitelist regex configuration (`/^project-/`)
- `srw-agents` group + `agent-service` account for WebDAV access
- Cockpit: "Open Project Folder" button on project detail page
- Cockpit: Cloud storage access toggle in project settings
- Cockpit: Cloud storage access override in job creation form
- `NextcloudAdmin` orchestrator service for Group Folders + Provisioning API
- Graceful degradation when Nextcloud is unavailable

**Session Cloud Folders:**
- Per-session Nextcloud folder creation via WebDAV + Share API
- Bidirectional workspace sync (`WorkspaceSyncService`)
- Sync ignore patterns for internal workspace files
- `nc_session_folder` and `nc_share_id` columns on `threads` table
- New file notification injection into conversation context
- Session folder cleanup on `/done`
- Folder persistence across idle → resume cycles
- Cockpit: "Session Files" button in chat header
- `NextcloudAdmin` extension: `create_session_folder()`, `share_with_user()`, `delete_folder()`

### Deferred

- **Auto-upload of job deliverables to cloud folder** — jobs produce output in `workspace/job_{id}/output/`. Syncing this to the Nextcloud folder on completion is useful but adds complexity (which files? overwrite policy? naming conflicts?). Defer until there's user demand.
- **Per-user OAuth token delegation** — the initial implementation uses a service account for WebDAV. Production should use scoped per-user credentials. Defer to [[sso_and_cloud_storage]] Phase 5 hardening.
- **Quota management** — the Group Folders API supports per-folder quotas (`POST .../quota`). Useful for multi-tenant production, not needed for dev/testing.
- **Folder rename sync** — if a project is renamed in the cockpit, the Group Folder mount point goes stale. Can be fixed with `PUT /index.php/apps/groupfolders/folders/{id}` `{mountPoint: newName}`. Minor UX issue, defer.
- **Subfolder-scoped personal projects** — mapping personal projects to `~/SRW/` instead of `/` for safety. Defer unless users report issues.
- **Real-time sync via Nextcloud Activity API** — replace polling with event-driven sync using Nextcloud's server-sent events or Activity app. Would reduce the 15-second polling interval to near-instant. Defer until polling latency becomes a UX issue.
- **Conflict resolution (merge/rename)** — current last-write-wins is sufficient for 1:1 sessions. If sessions become multi-user, proper conflict handling (rename on conflict, 3-way merge for text) would be needed.
- **Session folder archival** — instead of deleting the folder on `/done`, move it to an archive subfolder so users can revisit old session files. Adds storage cost, defer unless requested.

## Implementation Phases

### Phase 1: Nextcloud Init + Service Account

1. Extend Nextcloud init hook to install/enable `groupfolders` app
2. Configure `user_oidc` group provisioning with `--group-whitelist-regex='/^project-/'`
3. Create `srw-agents` group and `agent-service` user during init
4. Add `NEXTCLOUD_PUBLIC_URL` env var to docker-compose files

### Phase 2: NextcloudAdmin Service + DB Migration

5. Add `cloud_storage_read_only` and `nextcloud_folder_id` columns to `projects` table
6. Implement `orchestrator/services/nextcloud_admin.py` (Group Folders + Provisioning API wrapper)
7. Wire `create_project_folder()` into the project creation endpoint
8. Wire `delete_project_folder()` into the project deletion endpoint
9. Auto-create project-scoped WebDAV datasource on project creation
10. Auto-create personal WebDAV datasource on default project setup
11. Wire Nextcloud group management into member add/remove endpoints

### Phase 3: Settings + Override

12. Add `cloud_storage_read_only` to `PATCH /api/projects/{id}` — sync to datasource `read_only`
13. Add `cloud_storage_url` (deep-link) to `GET /api/projects/{id}` response
14. Implement job-level override: check `job.context.cloud_storage_read_only` in datasource resolution
15. Add the override to `POST /api/jobs` (job creation) payload handling

### Phase 4: Cockpit UI

16. Add "Open Project Folder" button to project detail page
17. Add cloud storage access toggle to project settings
18. Add cloud storage access override to job creation form
19. Add cloud storage indicator to job detail datasource chips
20. Add "Nextcloud unavailable" status indicator when applicable

### Phase 5: Session Cloud Folders

21. Add `nc_session_folder` and `nc_share_id` columns to `threads` table
22. Extend `NextcloudAdmin` with `create_session_folder()`, `share_with_user()`, `delete_folder()`
23. Implement `WorkspaceSyncService` (push, pull, background polling)
24. Integrate sync into `persistent_graph.py` (push after turn, background pull)
25. Wire folder creation into session startup (`persistent_app.py` lifespan)
26. Wire folder cleanup into session end (`/done` handler)
27. Handle idle → resume: reconnect sync to existing folder, pull user uploads

### Phase 6: Session UI + Polish

28. Add "Session Files" button to chat header in cockpit
29. Add new-file notification injection (system message on pull)
30. Add session folder link to session list page

### Phase 7: Backfill + Testing

31. Migration script for existing projects: create Group Folders + datasources for projects that predate this feature
32. Integration tests: project creation → folder exists → job gets tools → agent can read/write
33. Integration tests: session start → folder shared → sync works → session end → folder cleaned up
34. Edge cases: Nextcloud down (graceful degradation), folder name collision, concurrent folder creation, idle session with user uploads

## Related

- [[sso_and_cloud_storage]] — Parent feature: Keycloak SSO + Nextcloud deployment + WebDAV datasource type
- [[projects]] — Project infrastructure (database, API, members, repositories)
- [[project_knowledge_base]] — The other project-scoped shared resource (knowledge graph vs. file storage)
- [[datasources]] — Datasource connector pattern and scope resolution
- [[sessions]] — Persistent agent session architecture
- [[persistent_agent_assessment]] — Session gap analysis and priorities
- [nextcloud/groupfolders](https://github.com/nextcloud/groupfolders) — Group Folders (Team Folders) app repository and API spec
- [nextcloud/server#57445](https://github.com/nextcloud/server/issues/57445) — Open bug: group share not propagated to new members
- [nextcloud/user_oidc](https://github.com/nextcloud/user_oidc) — Official OIDC app with group provisioning support
