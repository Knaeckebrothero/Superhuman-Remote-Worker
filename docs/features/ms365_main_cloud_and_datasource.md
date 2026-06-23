---
tags:
  - feature
  - cloud
  - main-cloud
  - datasources
  - microsoft-365
  - graph-api
aliases:
  - Microsoft 365 main cloud
  - M365 backend
  - SharePoint / OneDrive / Teams backend
  - MS Graph cloud adapter
related:
  - "[[main_cloud_abstraction]]"
  - "[[rclone_cloud_mount]]"
  - "[[webdav_datasource_tools]]"
  - "[[project_cloud_folders]]"
  - "[[done/cloud_storage_alternatives]]"
  - "[[job_cloud_export]]"
---

# Microsoft 365 as Main Cloud + Datasource

**Status:** 📝 Design — not started. This is the detailed design for **Phase 5** of
[[main_cloud_abstraction]] (scoped there in §8 as "DEFERRED: MS365 + Google
Workspace adapters, and the agent-side `src/tools/webdav/` refactor the first
non-WebDAV adapter forces"), expanded to also cover the **datasource** surface.

**One-line:** Add Microsoft 365 (OneDrive for Business, SharePoint, Teams files)
as (a) a swappable **main-cloud backend** behind the existing `MainCloudBackend`
protocol and (b) a new **datasource type** the agent can attach BYO — both over
the Microsoft **Graph API**, the system's first non-WebDAV cloud.

## 0. Why M365 first (and why it's not as alien as it looks)

OpenCloud's backend already speaks **LibreGraph**, a deliberate subset of
Microsoft Graph. `opencloud.py` is built around `_graph_get/_graph_post/
_graph_delete` helpers, a role catalog, drives, and `/invite` permissions
(`orchestrator/services/cloud/opencloud.py:1371-1413`, `:1567-1591`). So the
**management half** of an M365 backend is largely a port of the OpenCloud
adapter with different endpoints, auth, and a real (not subset) permission model.

The config plumbing is **already half-built** — this is genuinely a fill-in-the-
class job for Phase A:

- `MS365Settings` (`tenant_id` / `client_id` / `client_secret` / `site_id`) —
  `orchestrator/services/cloud/config.py:70-79`
- The loader already branches on `backend_id == "ms365"` and reads
  `MS365_TENANT_ID` / `MS365_CLIENT_ID` / `MS365_CLIENT_SECRET` / `MS365_SITE_ID`
  — `config.py:297-306`
- The discriminated union already includes `MS365Settings` — `config.py:82-85`
- Required-secret enforcement already lists `ms365` → `MS365_CLIENT_SECRET` —
  `config.py:338-340`
- The registry has a **reserved-but-commented slot**:
  `# "ms365": MS365Backend,  # Phase 5` — `orchestrator/services/cloud/__init__.py:54`

The opaque-handle design was explicitly built with this day in mind —
[[main_cloud_abstraction]] §4.1: *"the single biggest forward-compatibility lever
for Microsoft 365 — the day MS Graph lands, we add a `GraphAddress` payload to the
handle and the rest of the system does not change."*

**The genuinely new work** is in three places, in increasing order of difficulty:
1. **File byte-ops over Graph REST** instead of WebDAV (OpenCloud does bytes over
   bearer-authenticated WebDAV; M365 cannot — see §3.2).
2. **The rclone mount** must use rclone's `onedrive` backend, not `webdav` — and
   that backend does **not** support the `bearer_token_command` trick OpenCloud
   relies on (§5). This is the central risk.
3. **OAuth against an external SaaS identity** (Entra ID), which breaks the
   "internal everything" bundling model (§2).

## 1. Goals & non-goals

**Goals**
- M365 selectable as the deployment's single main cloud via
  `MAIN_CLOUD_BACKEND=ms365` (same switch as nextcloud/opencloud), driving the
  full project/session folder lifecycle in [[main_cloud_abstraction]] §2.
- M365 attachable as a **datasource** (`type='ms365'`) so an agent can read/write
  a specific SharePoint library / OneDrive folder the user attaches, scoped per
  datasource and gated by the existing `project_datasources.read_only` flag.
- Cover OneDrive for Business, SharePoint document libraries, and Teams files in
  **one adapter** — Teams channel files *are* SharePoint document libraries, and
  OneDrive-for-Business is the same Graph drive model, so a single Graph client
  serves all three.
- App-only (daemon) auth — no per-user interactive consent in the main-cloud path.

**Non-goals (v1)**
- Google Drive / Workspace (separate, harder adapter — tracked in
  [[main_cloud_abstraction]] §8; Drive's ID-based, non-path model and native-doc
  export diverge most).
- Provisioning a Team or SharePoint site **per project** (Teams branding). v1 uses
  one shared site — see D-topology in §6.
- B2B guest sharing to identities outside the tenant.
- Exporting/round-tripping Google-native or Office-online-only artifacts beyond
  what Graph returns as file content.
- Replacing Keycloak SSO. We map identities, not migrate them (§4).

## 2. Prerequisite: Entra ID app registration (breaks "internal everything")

Unlike Nextcloud/OpenCloud, **M365 cannot be self-hosted or bundled**. A demo or
deployment needs a real tenant. Per [[feedback_prototype_deployment_pattern]] our
default is "internal everything"; M365 is the explicit exception — it is always
the *customer's* (or a dev) tenant, integrated, not bundled.

**App registration** (single- or multi-tenant) with **application permissions**
(admin-consented — these are app-only, not delegated):

| Graph permission | Why | Least-privilege note |
|---|---|---|
| `Sites.Selected` | Access the SRW SharePoint site's document library | Preferred. Must also grant the app on the specific site via `POST /sites/{id}/permissions`. |
| `Sites.ReadWrite.All` | Simpler alternative to `Sites.Selected` | Tenant-wide; fine for a PoC, avoid in prod. |
| `Files.ReadWrite.All` | User-home mounts (`/users/{id}/drive`) | Needed only if we mount users' OneDrives (§6). |
| `User.Read.All` | `resolve_user_identity()` (email → Entra user) | Read-only. |
| `Group.ReadWrite.All` + `GroupMember.ReadWrite.All` | Only if we mirror the project-group RBAC model (§6, D-groups) | Skippable if we grant users directly. |

- **Secret**: client secret (or, better, a certificate) stored in Vault/ESO,
  referenced by name — the `credentials_ref` / `__secret_fields__` convention in
  `config.py:184-199` already supports `env:MS365_CLIENT_SECRET`.
- **No local k3d e2e.** The k3d smoke path (README) cannot exercise M365.
  Integration testing uses a **free Microsoft 365 Developer tenant**; unit tests
  mock Graph (mirrors the OpenCloud `FakeMainCloudBackend` contract-test pattern,
  [[main_cloud_abstraction]] §1.5).

## 3. Main-cloud backend: `orchestrator/services/cloud/ms365.py`

A new `MS365Backend` implementing the `MainCloudBackend` protocol
(`orchestrator/services/cloud/base.py:110-332`) and, conditionally,
`SupportsRcloneMount` (`base.py:335-354`). Registered by uncommenting
`__init__.py:54`.

### 3.1 Auth — app-only, simpler than OpenCloud

OpenCloud needs Keycloak `client_credentials` **plus** RFC 8693 token exchange to
impersonate users for Personal Space mounts (`opencloud.py:174-217`). **M365 needs
neither impersonation nor OBO**: application permissions (`Files.ReadWrite.All` /
`Sites.*`) let the app act on any user's drive and the service site directly. This
is a real simplification over the OpenCloud path.

- Token endpoint: `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`,
  `grant_type=client_credentials`, `scope=https://graph.microsoft.com/.default`.
- Port OpenCloud's cached-token pattern verbatim (`opencloud.py:1321-1370`
  `_get_service_token`): cache `access_token` + `expires_at`, refresh ~30–60 s
  early, guard with an `asyncio.Lock`.

### 3.2 Management ops — close port of OpenCloud (Graph endpoints)

| Protocol method | Graph implementation |
|---|---|
| `resolve_user_identity(email)` | `GET /users?$filter=mail eq '{e}' or userPrincipalName eq '{e}'` → `user.id` (GUID) as `UserId`. |
| `ensure_user(...)` | **Return `None`** — M365 users are tenant-managed; we do not provision. Protocol explicitly supports this (`base.py:151-159`: backends that can't create users return `None`, callers fall back to resolve-at-share-time). Clean fit. |
| `get_user_home(user_id)` | `GET /users/{id}/drive` → `UserHome(webdav_url=None, browser_url=drive.webUrl, handle=…)`. |
| `ensure_group` / `add`/`remove_user_to/from_group` | Entra group via `/groups`, members via `/groups/{id}/members/$ref`. **Optional** — see D-groups (§6). |
| `ensure_project_folder(project_name, group_id)` | `POST /drives/{lib}/items/root/children` with a `folder` facet + `@microsoft.graph.conflictBehavior=fail`→treat 409 as exists (idempotent). Then grant via `POST /drives/{lib}/items/{id}/invite`. Returns `ProjectFolderHandle(native_id=item_id, vendor_meta={drive_id, web_url})`. |
| `delete_project_folder` | `DELETE /drives/{drive}/items/{item}`; 404 + `if_exists` → no-op. |
| `refresh_project_folder_access` | No-op (the Nextcloud-specific bug it works around doesn't apply — `base.py:197`). |
| `get_project_folder_browser_url` | `vendor_meta["web_url"]` (the `webUrl` Graph returns). |
| `get_project_folder_webdav_url` | **`None`** (no WebDAV). |
| `share_session_folder(handle, user_id)` | `POST /drives/{drive}/items/{item}/invite` `{recipients:[{objectId:user_id}], roles:["write"], requireSignIn:true, sendInvitation:false}` → `ShareHandle(native_id=permission_id, vendor_meta={drive_id,item_id})`. |
| `revoke_session_share(share)` | `DELETE /drives/{drive}/items/{item}/permissions/{permission_id}`. |
| `webdav_credentials` | `{}` (already the OpenCloud-style escape hatch, `base.py:327-332`). |

`_map_http_error` (port `opencloud.py:1414-1483`): 404→`NOT_FOUND`,
403→`PERMISSION_DENIED`, 401→`AUTHENTICATION_FAILED`, 429→`THROTTLED` (honor
`Retry-After`), 507→`QUOTA_EXCEEDED`. The 429 leaky-bucket + `retry.py`
infrastructure from Phase 1.5 already exists and applies.

### 3.3 File byte-ops — the real divergence (Graph REST, not WebDAV)

OpenCloud implements `list_project_folder` / `get|put_project_folder_file_bytes`
/ `put_session_file` over **bearer-authenticated WebDAV** (`PROPFIND`, `GET`,
`PUT` against `/dav/spaces/...` — `opencloud.py:747-1003`). **M365 has no usable
WebDAV**: SharePoint Online's legacy WebDAV endpoint is deprecated and does not
accept Graph tokens. These five methods must be reimplemented over Graph:

| Method | Graph |
|---|---|
| `list_project_folder` (recursive `PROPFIND`, `base.py:211-227`) | `GET /drives/{d}/items/{id}/children`, paged via `@odata.nextLink`, recursed; sort by path to match the contract. |
| `get_project_folder_file_bytes` | `GET /drives/{d}/items/{id}/content` (or `/root:/{path}:/content`). |
| `put_project_folder_file_bytes` | <4 MB: `PUT /drives/{d}/root:/{path}:/content?@microsoft.graph.conflictBehavior=replace`. ≥4 MB: `createUploadSession` + chunked `PUT` (defer large-file to Phase E). Parent folders auto-created via `children` POST with `folder` facet (the MKCOL equivalent). |
| `delete_project_folder_file` | `DELETE /drives/{d}/items/{id}` by path-addressing; 404 + `if_exists` → no-op. |
| `put_session_file` | Same simple/upload-session logic into the session folder. |

These byte-ops back the **job cloud-export Mode A/B + diff-review** flow
([[job_cloud_export]]) for M365 — they're not on the agent's live-work hot path
(that's the mount, §5). So Phase A can ship them as `NOT_SUPPORTED` and Phase B
fills them in (§7).

## 4. Identity / SSO mapping

`resolve_user_identity` maps an **SRW user's email** (from Keycloak today) to an
**Entra user**. This works cleanly **only when emails/UPNs line up** between the
SRW IdP and the tenant. Two deployment shapes:

- **Entra is the IdP** (federated into Keycloak, or Keycloak replaced): emails
  match by construction. This is the natural single-customer-tenant case and the
  recommended target.
- **Keycloak with separate user store**: requires emails to match Entra, or a
  mapping table (out of scope v1 — document the constraint).

Because `ensure_user` returns `None` (§3.2), there is **no first-login
provisioning race** to design around — the OpenCloud `ensure_user` complexity
(`opencloud.py:399-465`) simply isn't needed.

## 5. The rclone mount — central risk (`onedrive` backend ≠ `webdav` backend)

This corrects an over-optimistic reading of the mount layer. The agent-side mount
manager **is** generic in that `rclone config create` writes `source_type` +
every `source_config` key verbatim (`src/services/cloud_mount/__init__.py:510-521`).
**But** the *auth* handling in `_mount_script` is not generic — it has exactly two
branches (`src/services/cloud_mount/__init__.py:488-502`):

```python
if auth.get("type") == "basic" and auth.get("password"):
    source_config["pass"] = auth["password"]
elif auth.get("type") in _KEYCLOAK_AUTH_TYPES:
    ...                       # bearer_token_command → agent mints keycloak bearers
```

OpenCloud's mount is `source_type="webdav"` + `bearer_token_command`
(`opencloud.py:239-247`). **rclone's `bearer_token_command` is a `webdav`-backend
mechanism.** M365 must mount via rclone's **`onedrive`** backend (it natively
handles OneDrive **and** SharePoint document libraries via `drive_id` +
`drive_type`), and the onedrive backend authenticates via an OAuth `token` blob
or app-only `client_credentials` — **not** `bearer_token_command`. So OpenCloud's
mount auth path does not carry over.

Resolve `drive_id` (the library drive) and `drive_type` (`business` /
`documentLibrary`) via Graph at mount-spec build time and embed them in
`source_config`. Two auth wirings:

- **M-1 — rclone-native `client_credentials` (cheap, zero agent change).**
  Emit `source_config={drive_id, drive_type, client_id, tenant, client_credentials:"true", ...}`
  + the client secret. rclone (≥1.66 — **verify against the deployed build**;
  set `min_rclone_version` on the spec) self-refreshes app-only tokens. The
  agent's generic `config create` writes it verbatim → **no `cloud_mount` change**.
  **Risk:** the **tenant-powerful app client secret reaches the workspace pod**
  (rclone-obscured only). That is a strictly worse posture than today's Nextcloud
  basic-auth password (which is a scoped service account) because this secret can
  unlock every drive the app permissions cover.

- **M-2 — orchestrator-minted token, agent rewrites the onedrive token (secure
  parity).** Add a new auth type (e.g. `ms_graph_oauth`) to `_KEYCLOAK_AUTH_TYPES`'
  sibling handling: the orchestrator mints a short-lived Graph token, the agent's
  existing token-refresh loop (`__init__.py:348-378`) writes it into the onedrive
  remote's token via `rclone config update` (instead of a bearer file) and bounces
  the mount / lets rclone re-read on 401. Only short-lived tokens reach the pod;
  the secret stays in the orchestrator — matching OpenCloud's security posture.
  **Cost:** a real, bounded change to `src/services/cloud_mount/__init__.py`, and
  rclone-onedrive token-refresh has nuance (it expects a self-refreshing `token`
  with a `refresh_token`; supplying bare access tokens needs care). **This needs a
  spike.**

**Recommendation:** prototype **M-1 on a dev tenant first** to de-risk the rclone
onedrive integration end-to-end, then harden to **M-2 for any real deployment**
(the app secret is too powerful to ship into workspace pods). Track the M-2 spike
as the gating unknown for the mount phase. See [[rclone_cloud_mount]] for the
mount-manager internals M-2 touches.

## 6. Folder topology — where project/session folders live

**D-topology (recommended): one dedicated SharePoint site.** A single "SRW" site
with one document library; project folders = top-level folders under the library
root; session folders under a `sessions/` folder. Sharing per-folder via Graph
`/invite`. The app is granted on this **one** site via `Sites.Selected` (least
privilege). This is the closest analog to OpenCloud's Spaces and avoids the slow,
high-privilege per-project provisioning of sites/Teams. `site_id` is already a
config field (`config.py:79`).

User-home mounts → the user's **OneDrive** (`/users/{id}/drive`, `drive_type=
business`), reachable app-only with `Files.ReadWrite.All`.

Rejected/deferred: **Team-per-project** (Teams branding + channels — heavier,
slower, needs `Group.ReadWrite.All` + Teams provisioning); **site-per-project**.

**D-groups:** OpenCloud grants project access to an Entra/OpenCloud **group**.
For M365 we can either mirror that (an Entra group per project, grant the group on
the folder) or **grant users directly** on the folder via `/invite` and skip
groups entirely (drops the `Group.ReadWrite.All` permission). Recommend
**direct-grant for v1** (fewer permissions, simpler), revisit groups if project
membership churn makes per-user invites unwieldy.

## 7. Datasource surface: M365 as BYO attachment

The user explicitly wants M365 as a datasource too. Today `type='webdav'` →
`webdav3` client + `webdav_*` tools, with per-datasource creds in the encrypted
`credentials` JSONB, dispatched by `DS_TOOL_MAP` (`orchestrator/main.py:11223-11262`)
and gated read-vs-write by `project_datasources.read_only`
([[webdav_datasource_tools]]). M365 needs a new `type='ms365'` because WebDAV +
basic-auth doesn't apply.

**D-datasource:** two implementation shapes —

- **DS-tools (recommended, v1).** New agent tool family `m365_*`
  (`list/read/info/write/delete`) under `src/tools/ms365/`, backed by an agent-side
  Graph client, registered like webdav (`src/tools/registry.py:456-469`) and
  dispatched via a new `DS_TOOL_MAP` entry. Per-datasource OAuth config lives in
  `credentials` JSONB (encrypted; client_id/secret/tenant + drive/site/folder ref,
  or a delegated refresh token). Fits the existing datasource contract exactly —
  per-ds credential scoping, the `read_only` flag selecting the read vs read/write
  tool set, project linking. This is the smallest change that fits the model. It
  **does** add a provider-specific tool family — acceptable here because
  datasources are inherently tool-accessed (unlike the main cloud, which is
  mounted and uses plain filesystem tools).

- **DS-mount (convergence, later).** Make a datasource *mountable* — reuse the
  rclone onedrive mount (§5), agent uses ordinary filesystem tools, no new tools.
  Elegant and unifies the two surfaces, but "mountable datasource" is a new
  concept (`thread_mounts` has no datasource `source_kind` today —
  `migrations/app/0013_thread_mounts.sql`) and the mount-payload builder must pull
  creds from the datasource row, not the main-cloud backend. Defer until the
  main-cloud M365 mount (M-2) is proven, then converge.

**Shared code:** the Graph file-ops logic (list/get/put/delete, upload sessions,
error mapping) is the same for the backend (orchestrator, `ms365.py`) and the
datasource tools (agent, `src/tools/ms365/`). They live in different processes so
can't share a module, but should share design + helper shape — exactly as
orchestrator WebDAV (httpx) and agent WebDAV (`webdav3`) parallel each other today.

## 8. Schema & config touch-points

- **Datasource type enum** — add `'ms365'` to the documented `type` set
  (`orchestrator/database/schema.sql:906` comment is reference-only; the column is
  free-text TEXT, so no migration needed for the value itself, but the cockpit
  datasource form + validation must learn it).
- **`DS_TOOL_MAP`** — new `ms365` entry (`orchestrator/main.py:11223`), and the
  datasource-payload builder's `managed_types` set (`main.py:11323`) so read-only
  credential-withholding works the same way.
- **`create_datasource_connection`** — new `elif ds_type == "ms365":` branch
  (`src/core/datasource_setup.py`, next to the webdav branch at `:602-613`).
- **Config** — no new settings classes; `MS365Settings` + loader already exist
  (§0). Set `MS365_TENANT_ID/CLIENT_ID/CLIENT_SECRET/SITE_ID`; Vault/ESO for the
  secret.
- **Cockpit** — Admin → Cloud Storage gains M365 as a backend option (the
  settings UI from [[main_cloud_abstraction]] Phase 4); the datasource create form
  gains an M365 type with its credential fields.

## 9. Phasing

Mirrors the [[main_cloud_abstraction]] phase style. **A + B are pure orchestrator**
(testable with mocked Graph, no agent change); **C + D touch the agent.**

| Phase | Scope | Unlocks |
|---|---|---|
| **A — backend skeleton** | `ms365.py`: app-only token, identity resolve, ensure/delete project + session folders, share/revoke, browser URLs, `webdav_credentials={}`. Register in `REGISTRY`. Byte-ops + mount raise `NOT_SUPPORTED`. | `MAIN_CLOUD_BACKEND=ms365` boots; project/session folder lifecycle + cockpit deep-links work. |
| **B — file byte-ops** | The five Graph byte methods (§3.3), simple upload first. | Job cloud-export Mode A/B + diff-review ([[job_cloud_export]]) on M365. |
| **C — rclone mount** | Resolve `drive_id`/`drive_type`; `build_rclone_mount_spec` (onedrive); **M-1** wiring then the **M-2 spike** (§5). | Live project + user-home mounts in the workspace. Agent uses filesystem tools (no new tools). |
| **D — datasource** | `type='ms365'`, `m365_*` tools (DS-tools), `DS_TOOL_MAP` + `datasource_setup` + cockpit form. | BYO SharePoint/OneDrive attachments. |
| **E — hardening** | `Sites.Selected` least-privilege + per-site grant; **M-2** secret-out-of-pod; large-file upload sessions; 429/`Retry-After`; dev-tenant e2e; docs. | Production-ready. |

## 10. Testing

- **Unit / contract**: mock Graph (httpx transport), reuse the
  `FakeMainCloudBackend` contract-test harness ([[main_cloud_abstraction]] §1.5) so
  M365 satisfies the same protocol tests as nextcloud/opencloud.
- **Integration**: a free **Microsoft 365 Developer tenant** with the app
  registration from §2. Cannot run in k3d/CI — gate behind an opt-in env-flagged
  test target, document the tenant setup runbook (mirror the OpenCloud bootstrap
  runbook).
- **Mount (Phase C)**: validate rclone onedrive against the deployed rclone build
  early — the `client_credentials` support + token-refresh nuance (§5) is the
  highest-uncertainty item; spike it before committing to M-2.

## 11. Open decisions (need alignment before build)

1. **D-topology** — one shared SharePoint site (recommended) vs Team-per-project?
2. **D-mount-auth** — accept M-1's secret-in-pod for the PoC, with M-2 mandatory
   before any real deployment? (Recommended.) Or M-2 from the start?
3. **D-datasource** — DS-tools for v1 (recommended) vs go straight to DS-mount?
4. **D-groups** — direct per-user grants (recommended, drops a permission) vs
   mirror the Entra-group RBAC model?
5. **SSO alignment** — is the target deployment Entra-as-IdP (emails line up by
   construction), or Keycloak-with-matching-emails? Decides how much
   `resolve_user_identity` has to defend against mismatches.

## 12. Risks

- **rclone onedrive auth/refresh** (§5) — highest uncertainty; spike first.
- **App secret blast radius** — `Files.ReadWrite.All` is tenant-wide; prefer
  `Sites.Selected` + direct OneDrive access scoping, and keep the secret out of
  pods (M-2).
- **Graph throttling** — heavier than OpenCloud at scale; honor `Retry-After`,
  reuse the existing leaky-bucket.
- **No in-cluster instance** — slower iteration than nextcloud/opencloud; every
  e2e needs the external dev tenant.
- **Identity drift** — email/UPN mismatch between SRW IdP and Entra silently
  breaks sharing; fail loud in `resolve_user_identity`.
