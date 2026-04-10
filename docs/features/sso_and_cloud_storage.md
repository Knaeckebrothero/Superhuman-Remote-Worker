---
tags:
  - security
  - infrastructure
  - cloud-infrastructure
  - agent-architecture
---

# SSO via Keycloak & Shared Cloud Storage

Design document for introducing centralized identity management (Keycloak SSO) and a shared cloud storage layer (Nextcloud) so that users authenticate once and collaborate on files with AI agents across all system components.

**Status:** Design phase.

## Problem: Identity Fragmentation

The system is composed of multiple applications that each maintain their own user databases:

| Application | Current Auth | User Experience |
|-------------|-------------|-----------------|
| Cockpit (Angular) | Session-based email/password (orchestrator) | User registers/logs in here |
| Gitea | Gitea-internal accounts | Separate credentials required |
| Nextcloud (planned) | Nextcloud-internal accounts | Would require yet another login |
| pgAdmin | HTTP basic auth | Admin credentials in compose file |
| Mongo Express | HTTP basic auth | Admin credentials in compose file |

**Concrete pain point:** When a user clicks "Show Workspace" in the cockpit, they are redirected to Gitea to view the job's git repository. Unless they know the Gitea admin credentials and have logged in separately, they see a login wall. This pattern repeats for every third-party service the system relies on.

Maintaining per-service accounts creates several problems:

1. **UX friction** — Users must create and remember credentials for each service
2. **Account linking** — Connecting "User A on Cockpit" with "User A on Gitea" requires manual mapping or fragile sync logic
3. **Maintenance burden** — Every new service added to the stack requires building another auth integration
4. **Security risk** — Password reuse across services, inconsistent password policies, no centralized session revocation
5. **Enterprise blocker** — Organizations expect SSO; managing per-app credentials is unacceptable at scale

## Solution: Keycloak as Central Identity Provider

Deploy a **Keycloak** instance as the single source of truth for all user identities. Every application in the stack authenticates against Keycloak via **OpenID Connect (OIDC)**, giving users one login for the entire system.

### Why Keycloak

- **OIDC + SAML support** — Covers both modern (OIDC) and enterprise-legacy (SAML) protocols
- **Self-hosted** — No external dependency, runs alongside the stack in Podman/K8s
- **Native support in target apps** — Gitea, Nextcloud, and pgAdmin all have built-in OIDC client support
- **User management UI** — Admin console for managing users, roles, groups without custom code
- **Enterprise features out of the box** — MFA, social login (Google/GitHub/Microsoft), LDAP federation, brute-force protection, password policies
- **Realm export/import** — Declarative realm config for reproducible deployments

### Architecture

```
                         ┌──────────────────┐
                         │     Keycloak      │
                         │   (IdP / OIDC)    │
                         │                   │
                         │  Realm: srw       │
                         │  Clients:         │
                         │   - cockpit       │
                         │   - gitea         │
                         │   - nextcloud     │
                         │   - pgadmin       │
                         └────────┬──────────┘
                                  │ OIDC
               ┌──────────┬──────┴───────┬──────────┐
               ▼          ▼              ▼          ▼
          ┌─────────┐ ┌────────┐  ┌──────────┐ ┌────────┐
          │ Cockpit │ │ Gitea  │  │Nextcloud │ │pgAdmin │
          │(Angular)│ │        │  │(Files)   │ │        │
          └────┬────┘ └────────┘  └──────────┘ └────────┘
               │
               ▼
          ┌──────────┐
          │Orchestr. │
          │(FastAPI) │
          └──────────┘
```

**Login flow (user perspective):**
1. User opens cockpit → redirected to Keycloak login page
2. User authenticates (email/password, MFA, or social login)
3. Keycloak issues OIDC tokens → cockpit receives them
4. Cockpit sends access token to orchestrator API (Bearer header)
5. User clicks "Show Workspace" → redirected to Gitea → **already authenticated** via Keycloak SSO cookie
6. User clicks "View Files" → opens Nextcloud → **already authenticated** via same SSO session

### OIDC Client Configuration

Each application registers as an OIDC client in the `srw` Keycloak realm:

| Client | Type | Redirect URI | Notes |
|--------|------|-------------- |-------|
| `cockpit` | Public (SPA) | `http://localhost:4200/*`, `http://localhost:4000/*` | PKCE flow, no client secret |
| `orchestrator-api` | Bearer-only | — | Validates tokens, does not initiate login |
| `gitea` | Confidential | `http://localhost:3000/user/oauth2/keycloak/callback` | Server-side, has client secret |
| `nextcloud` | Confidential | `http://localhost:8800/apps/user_oidc/code` | Nextcloud `user_oidc` app (official) |
| `pgadmin` | Confidential | `http://localhost:5050/oauth2/authorize` | Optional |

### What Changes in the Codebase

#### Orchestrator (Backend)

**Remove:**
- Password hashing (`orchestrator/security/password.py`) — Keycloak manages passwords
- Registration endpoint (`/api/auth/register`) — Keycloak handles registration
- Login endpoint (`/api/auth/login`) — Keycloak handles login
- Email verification (`/api/auth/verify`, `/api/auth/resend-verification`) — Keycloak handles this
- Password reset (`/api/auth/forgot-password`, `/api/auth/reset-password`) — Keycloak handles this
- Session management (`orchestrator/security/auth.py`) — Replace with token validation
- SMTP email service (`orchestrator/services/email.py`) — Keycloak sends its own emails
- `auth_tokens` table — No longer needed
- `sessions` table — No longer needed (stateless JWT validation)

**Add:**
- OIDC token validation middleware (validate Keycloak JWTs via JWKS endpoint)
- JIT user provisioning (create local `users` row on first valid token, keyed by Keycloak `sub` claim)
- Token-to-user mapping (extract `sub`, `email`, `preferred_username`, `realm_access.roles` from JWT)
- Keycloak admin API client (optional, for programmatic user/role management)

**Keep:**
- `users` table (still needed for app-specific data: `default_project_id`, `avatar_color`, project memberships)
- `mcp_tokens` table (API tokens for non-browser clients like Claude Code CLI)
- User CRUD endpoints (but sync display name / email from Keycloak on login)
- CSRF protection (still needed for cookie-based flows, can be relaxed for Bearer token requests)

**New environment variables:**
```bash
KEYCLOAK_URL=http://localhost:8180
KEYCLOAK_REALM=srw
KEYCLOAK_CLIENT_ID=orchestrator-api
# Optional: for admin API calls
KEYCLOAK_ADMIN_CLIENT_ID=admin-cli
KEYCLOAK_ADMIN_CLIENT_SECRET=<secret>
```

#### Cockpit (Frontend)

**Remove:**
- Login component (`pages/login/`)
- Registration component
- Password reset component
- Email verification component
- Password-related UserService methods (`login()`, `register()`, `verifyEmail()`, etc.)

**Add:**
- `keycloak-angular` adapter (official Keycloak JS adapter for Angular)
- APP_INITIALIZER that initializes Keycloak before app bootstrap
- Auth interceptor sends `Authorization: Bearer <access_token>` instead of cookie
- Silent token refresh (Keycloak JS adapter handles this automatically)
- Logout redirects to Keycloak end-session endpoint

**Keep:**
- Auth guard (checks Keycloak authentication state instead of session)
- UserService (wraps Keycloak user profile + local orchestrator user data)

#### Gitea

No code changes. Configuration only:

```ini
# gitea/app.ini
[oauth2]
ENABLE = true

[oauth2_client]
OPENID_CONNECT_SCOPES = openid profile email

# Registered via Gitea admin UI or API:
# Provider: OpenID Connect
# Client ID: gitea
# Client Secret: <from keycloak>
# Discovery URL: http://keycloak:8080/realms/srw/.well-known/openid-configuration
# Auto-discover: true
```

Auto-create Gitea accounts on first OIDC login. Map Keycloak roles to Gitea org membership if needed.

#### Nextcloud

Nextcloud OIDC is configured via the `user_oidc` app (official Nextcloud first-party app, preferred over the third-party `oidc_login` for Nextcloud 28+):

```php
// config/config.php (or via occ commands during init)
// 1. Install and enable the app
//    occ app:install user_oidc
//    occ app:enable user_oidc
// 2. Register the Keycloak provider
//    occ user_oidc:provider:create "Keycloak" \
//        --clientid="nextcloud" \
//        --clientsecret="<from keycloak>" \
//        --discoveryuri="http://keycloak:8080/realms/srw/.well-known/openid-configuration" \
//        --unique-uid=0 \
//        --check-bearer=1

// Optional: auto-redirect to Keycloak (skip Nextcloud login page)
'allow_user_to_change_display_name' => false,
'lost_password_link' => 'disabled',
```

Auto-create Nextcloud user accounts on first OIDC login. Map Keycloak groups to Nextcloud groups for shared folder access per project.

#### Docker Compose

Add Keycloak service to `docker-compose.dev.yaml`:

```yaml
keycloak:
  image: quay.io/keycloak/keycloak:26.2
  command: start-dev --import-realm
  environment:
    KC_DB: postgres
    KC_DB_URL: jdbc:postgresql://postgres:5432/keycloak
    KC_DB_USERNAME: keycloak
    KC_DB_PASSWORD: keycloak
    KC_HOSTNAME: "http://localhost:${KEYCLOAK_PORT:-8180}"
    KC_HOSTNAME_BACKCHANNEL_DYNAMIC: "true"
    KC_HTTP_ENABLED: "true"
    KEYCLOAK_ADMIN: admin
    KEYCLOAK_ADMIN_PASSWORD: admin
  volumes:
    - ./docker/keycloak/realm-export.json:/opt/keycloak/data/import/srw-realm.json:ro
  ports:
    - "${KEYCLOAK_PORT:-8180}:8080"
  depends_on:
    postgres:
      condition: service_healthy
```

**Port note:** Default host port is `8180` (not `8080`) to avoid conflict with the VPN cluster service. Override via `KEYCLOAK_PORT` in `.env`.

**Hostname resolution:** `KC_HOSTNAME` sets browser-facing URLs to `localhost:8180` so OIDC redirects work from the user's browser. `KC_HOSTNAME_BACKCHANNEL_DYNAMIC` allows container-to-container traffic (e.g., Gitea token exchange at `keycloak:8080`) to work without hostname conflicts. Services that need split URLs (browser vs. server-to-server) use custom endpoint overrides — see `docker/keycloak/setup-gitea-oidc.sh`.

A `docker/keycloak/realm-export.json` file contains the full `srw` realm definition (clients, roles, scopes, default users) for reproducible one-command setup. This file is committed to the repository and updated via Keycloak admin UI export when the realm configuration changes.

**Database prerequisites:** Both Keycloak and Nextcloud need their own PostgreSQL databases on the shared `postgres` container. These are created during `init.py` setup (alongside the existing `orchestrator` and `vector` databases):

```sql
CREATE DATABASE keycloak;
CREATE USER keycloak WITH PASSWORD 'keycloak';
GRANT ALL PRIVILEGES ON DATABASE keycloak TO keycloak;

CREATE DATABASE nextcloud;
CREATE USER nextcloud WITH PASSWORD 'nextcloud';
GRANT ALL PRIVILEGES ON DATABASE nextcloud TO nextcloud;
```

### Keycloak Realm Design

**Realm:** `srw`

**Roles:**
| Role | Purpose |
|------|---------|
| `admin` | Full system access, user management |
| `user` | Standard access to cockpit, Gitea repos, own project files |
| `viewer` | Read-only access (future, for stakeholder dashboards) |

**Client Scopes:**
- `openid` (standard)
- `profile` (name, avatar)
- `email` (email, email_verified)
- `roles` (realm + client roles, for Nextcloud group mapping and access control)

**Realm Settings:**
- `Registration: ON` — Self-registration enabled (see Account Creation below)
- `Email as username: ON` — Consistent with current system
- `Verify email: ON` — Keycloak sends verification emails via its own SMTP config
- `Login with email: ON`

**Default Users (dev):**
- `test` / `test` — Realm admin, mapped to orchestrator admin
- Matches current `ADMIN_EMAIL` / `ADMIN_PASSWORD` defaults for zero-friction migration

### Account Creation & User Management

With Keycloak as IdP, account creation is handled entirely outside the cockpit's codebase. There are two paths:

#### Self-Registration (users sign up themselves)

Keycloak's built-in registration is enabled on the `srw` realm. When a user opens the cockpit and is redirected to Keycloak's login page, a "Register" link is shown. Keycloak handles the full registration flow:

1. User opens cockpit → redirected to Keycloak login page
2. User clicks "Register" → Keycloak registration form (name, email, password)
3. Keycloak sends verification email (configured via Keycloak's own SMTP settings)
4. User verifies → can now log in
5. First login to cockpit → JIT provisioning creates local `users` row with defaults

Keycloak manages password policies, CAPTCHA, brute-force protection, and email verification — none of this needs to be built in the cockpit or orchestrator.

#### Admin-Managed Accounts (admin creates users)

The cockpit keeps its existing user management page (list, create, edit, delete) but rewires it to the **Keycloak Admin REST API** via the orchestrator:

| Cockpit Action | Current Implementation | New Implementation |
|----------------|----------------------|-------------------|
| Create user | `POST /api/users` → insert into `users` table | `POST /api/users` → orchestrator calls Keycloak Admin API (`POST /admin/realms/srw/users`) + creates local `users` row |
| List users | `GET /api/users` → query `users` table | `GET /api/users` → query local `users` table (synced from Keycloak on login) |
| Edit user | `PUT /api/users/{id}` → update `users` table | `PUT /api/users/{id}` → orchestrator updates both Keycloak (name, email) and local table (avatar, project) |
| Delete user | `DELETE /api/users/{id}` → delete from `users` table | `DELETE /api/users/{id}` → orchestrator deletes from Keycloak + local table |
| Reset password | `POST /api/auth/reset-password` | `PUT /api/users/{id}/reset-password` → orchestrator calls Keycloak Admin API (`PUT /admin/realms/srw/users/{kc_id}/reset-password-email`) |
| Assign role | *(not implemented)* | `POST /api/users/{id}/roles` → orchestrator calls Keycloak Admin API to assign realm roles |

The cockpit UI stays the same from the user's perspective. Admins don't need to leave the cockpit or learn the Keycloak admin console (though it remains available at `:8180/admin` for advanced configuration like MFA policies, social login setup, LDAP federation, etc.).

**Orchestrator requirements for admin API access:**
```bash
# Service account for Keycloak Admin API calls
KEYCLOAK_ADMIN_CLIENT_ID=admin-cli
KEYCLOAK_ADMIN_CLIENT_SECRET=<secret>
```

The orchestrator authenticates to Keycloak's Admin API using a confidential client (`admin-cli`) with service account enabled. This is a server-to-server flow — no user interaction needed.

#### What the Cockpit No Longer Needs

These pages/components are removed since Keycloak owns the flows:

- **Login page** → Keycloak login page (redirect)
- **Registration page** → Keycloak registration page (link on Keycloak login)
- **Email verification page** → Keycloak handles verification
- **Forgot password page** → Keycloak "Forgot password?" link on login page
- **Password reset page** → Keycloak reset flow

The cockpit becomes a pure **application UI** — it never touches credentials directly.

### MCP Token Compatibility

The existing MCP token system (`srw_*` tokens) remains unchanged. MCP tokens are for **non-browser, programmatic access** (Claude Code CLI, scripts, CI/CD). They authenticate via the `X-MCP-Token` header and bypass OIDC entirely. The orchestrator validates them against the `mcp_tokens` table as before.

This gives two auth paths:
- **Browser (humans):** Keycloak OIDC → Bearer token
- **CLI/API (machines):** MCP token → direct DB validation

### Migration Path

For existing deployments with users already in the orchestrator database:

1. Deploy Keycloak with realm import
2. Create matching Keycloak users (scripted via Keycloak admin API, reading from `users` table)
3. Add `keycloak_sub` column to `users` table
4. Deploy updated orchestrator with dual-mode auth (accept both session cookies and OIDC tokens)
5. Deploy updated cockpit with Keycloak adapter
6. Once all users have logged in via Keycloak (linking their `keycloak_sub`), remove legacy session auth

---

## Problem: No Shared File Space

Currently, files flow one-way into agent jobs:

1. User uploads files via cockpit → staged in `workspace/uploads/<upload_id>/`
2. Agent copies files into `workspace/job_<uuid>/documents/`
3. Agent produces output files in the job workspace
4. User can only see output via Gitea workspace viewer (which requires separate auth — see above)

There is no shared file space where users and agents can both read and write. Users cannot:
- Browse files across jobs in a unified view
- Upload reference material that multiple jobs can access
- Download agent outputs without going through Gitea
- Collaborate on files with running agents in real time

## Solution: Nextcloud as Shared Cloud Storage

Deploy a standalone **Nextcloud** instance as the shared file layer. Users interact with Nextcloud's native UI (Google Drive-like experience). Agents access files via Nextcloud's **WebDAV API**, integrated as a datasource through the existing datasource system.

This is a **dev/testing stack**. Enterprises will bring their own cloud storage — Google Drive, OneDrive, Dropbox, or another WebDAV-compatible service. The agent tools depend on the datasource abstraction, not on Nextcloud specifically.

### Why Nextcloud (Not MinIO + Filestash)

We evaluated several self-hosted cloud storage solutions including MinIO, Filestash, oCIS, Seafile, Pydio Cells, and Cloudreve. The key insight is that the **agent API protocol matters more than the storage backend** — and enterprise cloud storage does not converge on S3:

| Enterprise Service | Primary Client API | S3 Compatible? | WebDAV? |
|-------------------|-------------------|----------------|---------|
| Google Drive | Google Drive REST API | No | No |
| OneDrive / SharePoint | Microsoft Graph API | No | No |
| Dropbox | Dropbox REST API | No | No |
| Box | Box REST API | No | Partial |
| AWS S3 | S3 | Yes | No |
| Azure Blob | REST + S3 compat | Partial | No |
| Nextcloud / ownCloud | **WebDAV** + OCS REST | No (backend only) | **Yes, native** |
| Seafile | Seafile REST API | No (backend only) | Yes |

No single protocol covers all enterprise storage. Each will eventually need its own datasource connector type. The question is which to build **first** — and WebDAV is the right choice because:

1. **Nextcloud is the dev/testing stack** — it's the most common self-hosted cloud drive, users already know the UI
2. **WebDAV is a standard protocol** — if an enterprise uses ownCloud, Seafile, or any WebDAV-capable storage, the same connector works without changes
3. **Nextcloud offers expansion potential** — messaging (Nextcloud Talk), collaborative editing (Nextcloud Office), calendar, mobile/desktop sync clients. We may not need these now, but they're there if we do.
4. **Enterprise proprietary APIs get added later as separate datasource types** — `google_drive`, `onedrive`, `s3`, each with their own connector, following the exact same datasource pattern
5. **The datasource abstraction protects us** — agent tools call `cloud_list`, `cloud_read`, `cloud_write` regardless of which connector is behind it

**Alternatives considered but rejected:**

| Solution | Verdict |
|----------|---------|
| **MinIO + Filestash** | S3 API is great for infrastructure but not what enterprise cloud drives expose. Two containers for what Nextcloud does in one. Filestash is a UI skin, not a storage system. |
| **oCIS (ownCloud Infinite Scale)** | Strong technically, but ownCloud was acquired by Kiteworks (proprietary) in late 2024. Community fork (OpenCloud) not yet production-ready. |
| **Seafile CE** | Block-level storage, most performant, but files not accessible via standard API from agents. |
| **MinIO alone** | No end-user UI. Admin console is not user-friendly for file browsing. |

### Architecture

```
                    ┌──────────────┐
                    │   Keycloak   │
                    │   (OIDC)     │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌───────────┐ ┌─────────┐ ┌──────────┐
        │ Nextcloud │ │ Cockpit │ │  Agent   │
        │ (Web UI)  │ │         │ │ (WebDAV) │
        │ :8800     │ │         │ │          │
        └───────────┘ └────┬────┘ └──────────┘
                           ▼
                      ┌──────────┐
                      │Orchestr. │
                      │(FastAPI) │
                      └──────────┘
```

- **Users** go to Nextcloud (`:8800`), log in via Keycloak SSO, browse/upload/download/share files
- **Agents** access files via Nextcloud's WebDAV endpoint, connected as a `webdav` datasource
- **Cockpit** deep-links to Nextcloud for "view files" actions (SSO means no second login)

### Nextcloud Deployment

Single-container standalone deployment for dev/testing:

```yaml
# docker-compose.dev.yaml
nextcloud:
  image: nextcloud:31-apache
  environment:
    NEXTCLOUD_ADMIN_USER: admin
    NEXTCLOUD_ADMIN_PASSWORD: admin
    POSTGRES_HOST: postgres
    POSTGRES_DB: nextcloud
    POSTGRES_USER: nextcloud
    POSTGRES_PASSWORD: nextcloud
    # OIDC configured via occ commands in init script (see below)
  volumes:
    - nextcloud_data:/var/www/html
  ports:
    - "8800:80"
  depends_on:
    - postgres
    - keycloak
```

`user_oidc` app configured during initialization via `occ` commands (see Nextcloud section above).

Nextcloud stores its data on a Docker volume (`nextcloud_data`). For production/K8s, this would be a PVC or external object storage (Nextcloud supports S3 as a backend for its own storage).

### Folder Structure

Nextcloud organizes files per user. For project-scoped collaboration, we use **Group Folders** (Nextcloud app):

```
Nextcloud/
├── Group Folders/
│   ├── Project: <project-name>/        # One group folder per project
│   │   ├── reference-data/             # User-uploaded shared files
│   │   ├── templates/
│   │   ├── datasets/
│   │   └── ...                         # User-organized folders
```

Agent deliverables stay in the job's `output/` folder (viewed via Gitea/cockpit). Nextcloud is for **user → agent** file sharing, not the other way around. Write-back can be added later if needed.

Group folder membership is managed via Keycloak groups → Nextcloud groups (synced on OIDC login). Users see only the project folders they're members of.

### Cloud Storage as a Datasource

The cloud storage integration follows the **existing datasource pattern** — same lifecycle, same cockpit UX, same tool injection mechanism. This is the most natural fit because:

- Users already know how to attach datasources to jobs (checkboxes in job creation)
- The orchestrator already handles datasource resolution (job > project > global)
- Tool injection/stripping already works per datasource type
- Connection testing already has an endpoint (`/api/datasources/{id}/test`)
- Read-only flag already controls which tools are available

#### New Datasource Type: `webdav`

| Field | Value |
|-------|-------|
| **Type** | `webdav` |
| **Connection URL** | `http://nextcloud:80/remote.php/dav/files/USERNAME/` (or group folder path) |
| **Credentials** | `{ "username": "agent-service", "password": "app-password" }` |
| **Read-only** | `true` (default — agent reads user-provided files, deliverables stay in job output folder) |

Stored in the existing `datasources` table. No schema changes needed — `type` is a free-text field, `credentials` is JSONB.

#### DS_TOOL_MAP Entry

Added to the orchestrator's `DS_TOOL_MAP` alongside `postgresql`, `neo4j`, `mongodb`:

```python
DS_TOOL_MAP = {
    # ... existing entries ...
    "webdav": {
        "category": "cloud",
        "read": ["cloud_list", "cloud_read", "cloud_info"],
        "write": ["cloud_list", "cloud_read", "cloud_info", "cloud_write", "cloud_delete"],
    },
}
```

#### Agent Tools (`src/tools/cloud/`)

New tool category `cloud` with WebDAV-backed implementations. Initial scope is **read-only** — agents pull user-provided reference files into the workspace. Deliverables remain in the job's `output/` folder (accessed via Gitea or cockpit).

| Tool | Purpose | Phase |
|------|---------|-------|
| `cloud_list(path, recursive)` | List files/folders at path | **Initial** |
| `cloud_read(path, target)` | Download file from cloud storage to workspace | **Initial** |
| `cloud_info(path)` | Get file metadata (size, modified, content type) | **Initial** |
| `cloud_write(workspace_path, target_path)` | Upload workspace file to cloud storage | Future (when user-directed upload is needed) |
| `cloud_delete(path)` | Delete file from cloud storage | Future |

Implementation uses `webdavclient3` (Python WebDAV client library, added to `requirements.txt`).

The tool factory receives the already-connected WebDAV client from `ToolContext` — the same pattern used by `create_graph_tools`, `create_sql_tools`, and `create_mongodb_tools`:

```python
# src/tools/cloud/webdav.py
from webdav3.client import Client

def create_cloud_tools(context: ToolContext) -> list:
    # context.get_datasource() returns the Client instance created by
    # _create_datasource_connection() — NOT the raw config dict
    client: Client = context.get_datasource("webdav")

    @tool
    def cloud_list(path: str = "/", recursive: bool = False) -> str:
        """List files and folders in cloud storage."""
        return client.list(path, get_info=True)

    @tool
    def cloud_read(path: str, target: str = "") -> str:
        """Download a file from cloud storage into the workspace."""
        local_path = context.workspace_manager.resolve_path(target or os.path.basename(path))
        client.download_sync(path, local_path)
        return f"Downloaded {path} to {local_path}"

    # ... cloud_write, cloud_delete, cloud_info ...
```

#### Tool Registry Integration

Cloud tools register in `src/tools/registry.py` following the existing pattern:

```python
# In load_tools() — alongside graph, sql, mongodb blocks
if "cloud" in tools_by_category:
    if not context.has_datasource("webdav"):
        logger.warning("Cloud tools require a webdav datasource — skipping")
    else:
        from src.tools.cloud import create_cloud_tools
        cloud_tools = create_cloud_tools(context)
        all_tools.extend(cloud_tools)
```

#### Connection Factory

Added to `src/agent.py` `_create_datasource_connection()`:

```python
elif ds_type == "webdav":
    from webdav3.client import Client
    creds = ds.get("credentials") or {}
    client = Client({
        "webdav_hostname": url,
        "webdav_login": creds.get("username"),
        "webdav_password": creds.get("password"),
    })
    client.list("/")  # Connection test
    return client
```

#### Default Datasource via Environment

```bash
# .env
DEFAULT_DS_WEBDAV_URL=http://nextcloud:80/remote.php/dav/files/agent-service/
DEFAULT_DS_WEBDAV_USERNAME=agent-service
DEFAULT_DS_WEBDAV_PASSWORD=<app-password>
DEFAULT_DS_WEBDAV_NAME=Default Cloud Storage
DEFAULT_DS_WEBDAV_READ_ONLY=true
```

Seeded as a global datasource by `init.py`, same pattern as `DEFAULT_DS_NEO4J_*`.

#### Nextcloud Service Account

A dedicated Nextcloud user (`agent-service`) is created during initialization with an **app password** (not the user's main password). This service account:

- Is used by agents for WebDAV file operations
- App password is stored in the datasource `credentials` JSONB field

**Scoping concern:** Using a single admin-level service account would give every agent access to every project's files. To enforce project isolation, there are two options:

| Approach | Pros | Cons |
|----------|------|------|
| **Per-project service account** — create a Nextcloud user per project, add to that project's group | Clean isolation, agent only sees its project's files | Operationally heavier, more accounts to manage |
| **Scoped WebDAV URL** — single service account, but the datasource `connection_url` points to the specific group folder path | Simple, one account | Agent could theoretically navigate outside the path (server-dependent) |

For **dev/testing**, a single `agent-service` user with a scoped URL path is fine. For **production**, per-project service accounts (created automatically when a project is created in the cockpit) provide proper isolation.

### Cockpit Integration

The cockpit does not need its own file browser. Instead:

- **"View Files" button** on job cards → deep-links to Nextcloud at the job's output folder. User is already authenticated via Keycloak SSO.
- **"Project Files" tab** on project page → links to Nextcloud's group folder for that project
- **Upload during job creation** → continues to use the existing orchestrator upload API (staged to `workspace/uploads/`), which is lightweight and doesn't require Nextcloud
- **Datasource attachment** → same checkbox UX as PostgreSQL/Neo4j/MongoDB. User selects the cloud storage datasource when creating a job.

### Access Control

Nextcloud's native permissions model handles access control:

| Keycloak Group | Nextcloud Group | Access |
|---------------|----------------|--------|
| `project-<uuid>-members` | Same (synced via OIDC) | Read/write to project's group folder |
| `project-<uuid>-viewers` | Same | Read-only to project's group folder |
| `admin` | Nextcloud admin | Full access |

Keycloak group membership → Nextcloud group membership is synced automatically on OIDC login. When a user is added to a project in the cockpit, the orchestrator adds them to the corresponding Keycloak group, which flows through to Nextcloud.

### Future: Additional Cloud Storage Datasource Types

The datasource pattern makes it straightforward to add connectors for enterprise cloud storage later:

| Datasource Type | Client Library | When to Build |
|----------------|---------------|---------------|
| `webdav` | `webdavclient3` | **Phase 3 (now)** — Nextcloud, ownCloud, any WebDAV server |
| `s3` | `boto3` / `minio` | When an enterprise needs raw S3 bucket access |
| `google_drive` | `google-api-python-client` | When an enterprise uses Google Workspace |
| `onedrive` | `msgraph-sdk-python` | When an enterprise uses Microsoft 365 |
| `dropbox` | `dropbox` SDK | When an enterprise uses Dropbox Business |

All connectors implement the same abstract tool interface (`cloud_list`, `cloud_read`, `cloud_write`, `cloud_info`, `cloud_delete`). The agent doesn't know or care which backend is behind the tools — the datasource type determines which connector is loaded, identical to how `sql_query` works the same whether the datasource is a local PostgreSQL or a cloud-hosted RDS instance.

---

## Implementation Roadmap

### Phase 1: Keycloak Foundation
1. Add Keycloak to `docker-compose.dev.yaml`
2. Update `init.py` to create `keycloak` database on the shared PostgreSQL container
3. Create `srw` realm with default clients, roles, and dev users
4. Export realm config to `docker/keycloak/realm-export.json` for reproducible setup
5. Configure Gitea as OIDC client → **solves the immediate workspace viewer pain point**

### Phase 2: Cockpit + Orchestrator OIDC Migration
6. Add `keycloak-angular` to cockpit, replace login page with Keycloak redirect
7. Add OIDC token validation middleware to orchestrator (dual-mode: accept both sessions and OIDC during transition)
8. Add `keycloak_sub` column to `users` table, implement JIT provisioning
9. Remove legacy auth code (sessions, password hashing, email verification, SMTP)

### Phase 3: Nextcloud + WebDAV Datasource
10. Add Nextcloud to `docker-compose.dev.yaml`, create `nextcloud` database on shared PostgreSQL
11. Configure Nextcloud OIDC client in Keycloak realm (`user_oidc` app via `occ` commands)
12. Create `agent-service` Nextcloud user with app password
13. Add `webdavclient3` to `requirements.txt`
14. Add `webdav` to `DS_TOOL_MAP` in orchestrator (`orchestrator/main.py`)
15. Implement `src/tools/cloud/webdav.py` (`create_cloud_tools`) + register in `src/tools/registry.py`
16. Add WebDAV connection factory branch to `src/agent.py` `_create_datasource_connection()`
17. Add `DEFAULT_DS_WEBDAV_*` env var handling to `init.py` / `orchestrator/init.py`

### Phase 4: Cockpit Integration
18. Add "View Files" deep-links from job cards to Nextcloud (SSO makes this seamless)
19. Add "Project Files" tab linking to Nextcloud group folder for the project
20. Add `webdav` as a datasource type option in the cockpit datasource management UI

### Phase 5: Hardening
21. Keycloak group → Nextcloud group sync for per-project access control
22. Per-project service accounts for production isolation (auto-created with projects)
23. `cloud_write` / `cloud_delete` tools (user-directed upload from agent to cloud storage)
24. Production domain configuration (`KC_HOSTNAME`, cookie domain, HTTPS termination)
25. Audit logging for file access
26. Quota management per project group folder
27. Additional cloud storage datasource types (S3, Google Drive, OneDrive) as needed

## Service Ports (Updated)

| Service | Port | Notes |
|---------|------|-------|
| Keycloak | 8180 | Avoids conflict with VPN cluster on 8080; override via `KEYCLOAK_PORT` |
| Nextcloud | 8800 | |
| *(all existing ports unchanged)* | | |

**Production note:** In dev, SSO works because all OIDC redirects route through Keycloak on `localhost:8080`. In production with separate subdomains (`cockpit.example.com`, `git.example.com`, etc.), `KC_HOSTNAME` and cookie domain settings need explicit configuration. Deferred to Phase 5.

## Related

- `docs/cloud_workspace.md` — K8s workspace architecture (MinIO remains relevant for checkpoint archival, separate from user-facing cloud storage)
- `docs/features/projects.md` — Project model that scopes Nextcloud group folders
- `docs/datasources.md` — Datasource connector pattern, `webdav` is the new type
- `docs/features/vm_backend.md` — VM workspace backend, could sync files from cloud storage on job start
