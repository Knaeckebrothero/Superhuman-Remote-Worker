---
tags:
  - security
  - infrastructure
  - cloud-infrastructure
  - agent-architecture
---

# SSO via Keycloak & Shared Cloud Storage

Design document for introducing centralized identity management (Keycloak SSO) and a shared cloud storage layer (MinIO) so that users authenticate once and collaborate on files with AI agents across all system components.

**Status:** Design phase.

## Problem: Identity Fragmentation

The system is composed of multiple applications that each maintain their own user databases:

| Application | Current Auth | User Experience |
|-------------|-------------|-----------------|
| Cockpit (Angular) | Session-based email/password (orchestrator) | User registers/logs in here |
| Gitea | Gitea-internal accounts | Separate credentials required |
| MinIO (planned) | MinIO-internal accounts | Would require yet another login |
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
- **Native support in target apps** — Gitea, MinIO, and pgAdmin all have built-in OIDC client support
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
                         │   - minio         │
                         │   - pgadmin       │
                         └────────┬──────────┘
                                  │ OIDC
               ┌──────────┬──────┴───────┬──────────┐
               ▼          ▼              ▼          ▼
          ┌─────────┐ ┌────────┐  ┌──────────┐ ┌────────┐
          │ Cockpit │ │ Gitea  │  │  MinIO   │ │pgAdmin │
          │(Angular)│ │        │  │          │ │        │
          └─────────┘ └────────┘  └──────────┘ └────────┘
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
6. User opens MinIO file browser → **already authenticated** via same SSO session

### OIDC Client Configuration

Each application registers as an OIDC client in the `srw` Keycloak realm:

| Client | Type | Redirect URI | Notes |
|--------|------|-------------- |-------|
| `cockpit` | Public (SPA) | `http://localhost:4200/*`, `http://localhost:4000/*` | PKCE flow, no client secret |
| `orchestrator-api` | Bearer-only | — | Validates tokens, does not initiate login |
| `gitea` | Confidential | `http://localhost:3000/user/oauth2/keycloak/callback` | Server-side, has client secret |
| `minio` | Confidential | `http://localhost:9001/oauth_callback` | MinIO Console OIDC |
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
KEYCLOAK_URL=http://keycloak:8080
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

#### MinIO

No code changes. Environment configuration:

```bash
MINIO_IDENTITY_OPENID_CONFIG_URL=http://keycloak:8080/realms/srw/.well-known/openid-configuration
MINIO_IDENTITY_OPENID_CLIENT_ID=minio
MINIO_IDENTITY_OPENID_CLIENT_SECRET=<from keycloak>
MINIO_IDENTITY_OPENID_CLAIM_NAME=policy
MINIO_IDENTITY_OPENID_SCOPES=openid,profile,email
MINIO_IDENTITY_OPENID_REDIRECT_URI=http://localhost:9001/oauth_callback
```

Map Keycloak roles/groups to MinIO policies (e.g., `srw-user` role → `readwrite` policy on project buckets).

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
    KC_HOSTNAME_STRICT: "false"
    KC_HTTP_ENABLED: "true"
    KEYCLOAK_ADMIN: admin
    KEYCLOAK_ADMIN_PASSWORD: admin
  volumes:
    - ./keycloak/realm-export.json:/opt/keycloak/data/import/srw-realm.json:ro
  ports:
    - "8080:8080"
  depends_on:
    - postgres
```

A `keycloak/realm-export.json` file contains the full `srw` realm definition (clients, roles, scopes, default users) for reproducible one-command setup. This file is committed to the repository and updated via Keycloak admin UI export when the realm configuration changes.

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
- `roles` (realm + client roles, for MinIO policy mapping)

**Default Users (dev):**
- `admin` / `admin` — Realm admin, mapped to orchestrator admin
- Matches current `ADMIN_EMAIL` / `ADMIN_PASSWORD` defaults for zero-friction migration

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

## Solution: MinIO as Shared Cloud Storage

Deploy **MinIO** (S3-compatible object storage) as the shared file layer. Users upload/download via the cockpit or MinIO Console. Agents read/write via S3-compatible tools.

### Why MinIO

- **S3-compatible API** — Massive ecosystem, every language has a client
- **Self-hosted** — Runs alongside the stack, no external dependency
- **OIDC support** — Authenticates against Keycloak (see SSO section above)
- **Web Console** — Built-in file browser at port 9001, no custom UI needed initially
- **Already in the architecture** — Referenced in `cloud_workspace.md` for checkpoint archival
- **Lightweight** — Single binary, runs well in Podman and K8s

### Bucket Structure

```
minio/
├── project-<uuid>/              # One bucket per project
│   ├── shared/                  # User-uploaded shared files
│   │   ├── reference-data/
│   │   ├── templates/
│   │   └── datasets/
│   ├── jobs/                    # Agent job outputs (auto-synced)
│   │   ├── <job-uuid>/
│   │   │   ├── output/
│   │   │   ├── workspace.md
│   │   │   └── plan.md
│   │   └── <job-uuid>/
│   └── knowledge/               # Knowledge base exports
└── system/                      # System bucket (backups, checkpoints)
    ├── checkpoints/
    └── backups/
```

Files are scoped per **project** — shared across all jobs within that project. This matches the existing project model in the orchestrator (`projects` table, project membership).

### Integration Points

#### Orchestrator

New endpoints for file management (proxying to MinIO):

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/projects/{id}/files` | GET | List files in project bucket |
| `/api/projects/{id}/files/{path}` | GET | Download file |
| `/api/projects/{id}/files/{path}` | PUT | Upload file |
| `/api/projects/{id}/files/{path}` | DELETE | Delete file |
| `/api/projects/{id}/files/presign` | POST | Generate presigned upload/download URLs |

Alternatively, the cockpit can talk to MinIO directly using presigned URLs generated by the orchestrator, avoiding the orchestrator as a file proxy for large uploads.

**New environment variables:**
```bash
MINIO_URL=http://minio:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
# Or with OIDC: MinIO uses Keycloak tokens directly, no static keys needed for user requests
```

#### Agent Tools

New tool category `cloud_storage` (or extend existing `workspace` tools):

| Tool | Purpose |
|------|---------|
| `cloud_list(prefix)` | List files in the project's shared bucket |
| `cloud_read(path)` | Download a file from shared storage to workspace |
| `cloud_write(path, workspace_path)` | Upload a workspace file to shared storage |
| `cloud_sync(direction)` | Sync workspace outputs to/from shared storage |

These tools use the MinIO Python SDK (`minio`) with credentials injected via environment or the job's datasource config. The agent accesses only its own project's bucket.

#### Cockpit

**Option A (simple, initial):** Link to MinIO Console (port 9001). User is already authenticated via Keycloak SSO. MinIO Console provides a full file browser with upload/download/preview.

**Option B (integrated, later):** Build a file browser component into the cockpit. Uses presigned URLs from the orchestrator for direct S3 uploads/downloads. Provides tighter integration with the job list (e.g., "view files for this job").

Recommend starting with Option A to get value quickly, then building Option B when UX requirements become clearer.

#### Job Lifecycle Integration

- **Job start:** Agent receives the project's MinIO bucket path in its config/metadata. Optionally auto-downloads `shared/` contents to `documents/`.
- **Job completion:** Agent's output files are auto-synced to `jobs/<job-uuid>/` in the project bucket.
- **Job workspace viewer:** "Show Workspace" button can link to MinIO Console filtered to `jobs/<job-uuid>/` instead of (or in addition to) Gitea.

### Access Control

MinIO policies are mapped from Keycloak roles:

| Keycloak Role | MinIO Policy | Access |
|---------------|-------------|--------|
| `admin` | `consoleAdmin` | Full access to all buckets |
| `user` | Custom per-project | Read/write to `project-<uuid>/` buckets they're a member of |
| `viewer` | Custom per-project | Read-only to project buckets |

Policy assignment happens via Keycloak token claims. When a user authenticates to MinIO via OIDC, MinIO reads the `policy` claim from the JWT and applies the corresponding policy.

For agents (non-browser), the orchestrator generates scoped temporary credentials (MinIO STS with AssumeRoleWithWebIdentity) or uses a service account with per-bucket policies.

---

## Implementation Roadmap

### Phase 1: Keycloak Foundation
1. Add Keycloak to `docker-compose.dev.yaml`
2. Create `srw` realm with default clients, roles, and dev users
3. Export realm config to `keycloak/realm-export.json` for reproducible setup
4. Configure Gitea as OIDC client → **solves the immediate workspace viewer pain point**
5. Update `init.py` to initialize Keycloak database alongside existing databases

### Phase 2: Cockpit + Orchestrator OIDC Migration
6. Add `keycloak-angular` to cockpit, replace login page with Keycloak redirect
7. Add OIDC token validation middleware to orchestrator (dual-mode: accept both sessions and OIDC during transition)
8. Add `keycloak_sub` column to `users` table, implement JIT provisioning
9. Remove legacy auth code (sessions, password hashing, email verification, SMTP)

### Phase 3: MinIO Cloud Storage
10. Add MinIO to `docker-compose.dev.yaml`
11. Configure MinIO OIDC client in Keycloak realm
12. Add orchestrator endpoints for file management (or presigned URL generation)
13. Create `cloud_storage` agent tool category
14. Wire job lifecycle: auto-sync outputs to project bucket on completion

### Phase 4: Cockpit File Integration
15. Link "Show Workspace" to MinIO Console (quick win, SSO makes this seamless)
16. Build integrated file browser component in cockpit (if needed beyond MinIO Console)
17. Add drag-and-drop upload to project shared storage from cockpit

### Phase 5: Hardening
18. Per-project MinIO policies mapped from Keycloak groups
19. Audit logging for file access (MinIO audit log → MongoDB)
20. Presigned URL expiration and rate limiting
21. Quota management per project bucket

## Service Ports (Updated)

| Service | Port |
|---------|------|
| Keycloak | 8080 |
| MinIO API | 9000 |
| MinIO Console | 9001 |
| *(all existing ports unchanged)* | |

## Related

- [[cloud_workspace]] — K8s workspace architecture, references MinIO for checkpoint archival
- [[projects]] — Project model that scopes file storage buckets
- [[datasources]] — Datasource connector pattern (MinIO could become a datasource type)
- [[vm_backend]] — VM workspace backend, could use MinIO for workspace seeding
