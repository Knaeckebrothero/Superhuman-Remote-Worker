---
tags:
  - cloud-infrastructure
  - storage
  - architecture
  - decision
status: discussion
related:
  - "[[features/sso_and_cloud_storage]]"
  - "[[features/project_cloud_folders]]"
  - "[[cloud_workspace]]"
  - "[[datasources]]"
---

# Cloud Storage Alternatives

A decision document for evaluating the file-exchange backend used between users and agents. The goal is to answer one question before we invest more engineering into the current choice: **is Nextcloud the right tool for what we are actually using it for, or is a lighter backend a better fit?**

This document is a sibling to `docs/features/sso_and_cloud_storage.md`, which describes the *current* Nextcloud design. This one is explicitly about tradeoffs and alternatives — no implementation decision has been made yet.

## 1. Problem / Topic

Nextcloud was introduced as the shared file-exchange layer between users and agents: users drop files in a project folder, agents read them, agents write results back, users see them. That was the requirement.

Since then, the integration has grown:

- Per-project Group Folders provisioned on project creation
- Per-user home directories mapped via Keycloak OIDC
- Per-session folders for persistent agent threads
- Automatic WebDAV datasource wiring
- Session-level bidirectional workspace sync
- Admin service accounts, Nextcloud-specific env vars, its own Postgres database, its own OIDC client, its own container image

Meanwhile, we still use **zero** of Nextcloud's value-add apps: no Collabora (document editing), no Calendar, no Contacts, no Talk (video), no Deck, no Mail, no Notes. Our feature set is a strict subset of WebDAV: `list`, `read`, `write`, `delete`, `info`, plus "share a folder with a user".

The concern is that we are paying the operational cost of a full collaborative platform to get a file bucket with a web UI. Before we push new work into this layer (session-folder lifecycle, quota management, trash/versioning UX, OIDC subclaim handling), we should check whether a lighter backend would serve the real requirement at lower total cost — or whether Nextcloud's included features are actually worth the overhead and we should commit to it.

## 2. Current Implementation State

### 2.1. What we use Nextcloud for

| Capability | Who uses it | Where it lives |
|---|---|---|
| File list / read / write / delete / info | Agents (as tools) | `src/tools/cloud/webdav.py` (242 lines) |
| Per-project Group Folder provisioning | Orchestrator (on project create) | `orchestrator/services/nextcloud_admin.py` (528 lines) |
| Per-user home directory + OIDC identity mapping | End users via browser | Nextcloud `user_oidc` app + Keycloak |
| Per-session folder for persistent threads | Orchestrator + agent | `src/services/workspace_sync.py` |
| Folder sharing (user ↔ agent service account) | Orchestrator | `NextcloudAdmin` via OCS Share API |
| WebDAV datasource wiring for agents | Orchestrator + agent | `DS_TOOL_MAP["webdav"]` in `orchestrator/main.py`, connection in `src/agent.py` |

### 2.2. What we do not use

- Collabora / OnlyOffice document editing
- Calendar, Contacts, Mail
- Talk (audio/video calls)
- Deck, Notes, Tasks
- Nextcloud clients (desktop / mobile sync)
- Federation
- End-to-end encryption
- Talk bots / push notifications
- Activity feed, comments, tags

### 2.3. Integration layers

The integration sits at four distinct layers:

1. **Agent tool layer (clean, backend-agnostic).**
   `src/tools/cloud/webdav.py` exposes `cloud_list`, `cloud_read`, `cloud_write`, `cloud_delete`, `cloud_info`. The module docstring is explicit: *"Provides file access to WebDAV (Nextcloud, ownCloud, or any WebDAV server) attached as a datasource."* These tools are tied to the WebDAV *protocol*, not to Nextcloud.

2. **Datasource type (clean, protocol-level).**
   The orchestrator's `DS_TOOL_MAP` registers `webdav` as a first-class datasource type alongside PostgreSQL, MongoDB, Neo4j. Read-only flag toggles write-tool injection. The schema uses a generic `webdav` type; there is no Nextcloud-specific column except `projects.nextcloud_folder_id` (tracked in `orchestrator/database/schema.sql` and `postgres.py`).

3. **Admin provisioning (tightly coupled).**
   `orchestrator/services/nextcloud_admin.py` — 528 lines — is the one place where Nextcloud-specific APIs leak in. It calls:
   - Nextcloud **Group Folders** REST API (non-standard, Nextcloud-only)
   - Nextcloud **OCS Provisioning** API (user/group management)
   - Nextcloud **OCS Share** API (folder sharing semantics)
   - WebDAV `MKCOL` for raw folder creation

   This is the main swap cost. Replacing Nextcloud means rewriting or deleting this file and re-expressing its responsibilities against a different backend.

4. **Deployment (medium coupling).**
   - `docker-compose.yaml` — `nextcloud:31-apache` service on port 8800
   - `docker/nextcloud/setup-nextcloud.sh` — init script
   - `docker/keycloak/setup-nextcloud-oidc.sh` — OIDC wiring
   - `deployment/19-nextcloud.yaml` — K8s manifest
   - ~15 env vars (`NEXTCLOUD_*`) in `.env.example`
   - A dedicated Postgres database + user, provisioned in `init.py`

### 2.4. Swap readiness

The good news: the abstraction boundary is in the right place. Agent code is protocol-level (WebDAV), not vendor-level (Nextcloud). Swapping the backend does *not* require changing agent code beyond a datasource type label — if the replacement also speaks WebDAV, agent code changes approach zero.

The cost surface for a swap is concentrated in three places:

- `nextcloud_admin.py` (528 lines) — full rewrite or delete
- Deployment manifests + env vars — mechanical replacement
- `workspace_sync.py` — re-target WebDAV endpoint or swap client library

What makes the swap *not* a one-line change: the admin class encodes semantics that are Nextcloud-specific (Group Folders as a concept, OCS share model, OIDC home-directory mapping). If we move to a backend without those primitives, we need to redesign those features, not port them.

## 3. What We Actually Need

Before comparing options, nail down the real requirement set. The rows below are ordered roughly by how load-bearing each one is.

| # | Requirement | Load-bearing? | Notes |
|---|---|---|---|
| 1 | Agent read/write/list/delete on files under a namespace | **Hard** | This is the whole reason the layer exists |
| 2 | Per-project scoping | **Hard** | Multi-tenant isolation; already wired into the project lifecycle |
| 3 | Users can upload/download via a web UI | **Hard** | Non-technical users cannot drive WebDAV clients |
| 4 | SSO identity (user logs in once, sees their stuff) | **Soft/Hard** | Nice UX today, mandatory for any enterprise-ish scenario |
| 5 | Users can see agent output live (or near-live) | **Soft** | Polling the web UI is acceptable; push notifications are nice-to-have |
| 6 | User-to-user sharing via UI clicks | **Soft** | Only matters once we have multi-user projects without explicit admin provisioning |
| 7 | Desktop/mobile client sync | **Negligible** | Nobody has asked for this |
| 8 | Versioning / trash / undelete | **Soft** | Safety net; not a feature driver |
| 9 | Collaborative document editing | **Negligible** | We edit in cockpit, not in the cloud-storage UI |

The honest version of the requirement: **a browsable, multi-tenant file bucket with SSO and programmatic access**. That's it.

## 4. Alternatives

Each option is evaluated against the requirements in section 3.

### Option A — Stay with Nextcloud, unchanged

**What it means:** Keep everything. Continue deepening the integration along the path in `docs/features/sso_and_cloud_storage.md` and `docs/features/project_cloud_folders.md`.

**Pros**
- Zero migration work
- Nextcloud covers every requirement in section 3 including the "soft" ones
- OIDC home directories are real and work
- User-facing sharing UX is already built
- Versioning + trash ship for free
- Future "maybe we want Collabora after all" is still on the table

**Cons**
- Heaviest ops footprint in the stack (PHP + Apache + dedicated Postgres + OIDC app + Group Folders app + Nextcloud upgrade cadence + its own CVE surface)
- ~500 LoC of vendor-specific admin glue to maintain
- Nextcloud major version upgrades are historically painful and block on app compatibility
- We're paying for 50+ features we don't use
- "Integration depth creep" — the temptation to use more Nextcloud features as they become convenient, increasing lock-in

**Swap cost later:** Highest. Every month we stay here, the `NextcloudAdmin` class grows and the session-sync logic calcifies.

### Option B — Stay with Nextcloud, but freeze and minimize

**What it means:** Keep Nextcloud for now, but treat it as a *dumb* WebDAV server. Stop adding Nextcloud-specific code. Don't touch Group Folders if we can avoid it. No new `NextcloudAdmin` methods. Session folders become plain WebDAV folders under a service account, not Group Folders. Sharing becomes pre-generated share links, not API calls.

**Pros**
- Same zero migration cost as Option A
- Forces the integration to stay at the WebDAV-protocol level, which preserves optionality for a later swap
- Keeps the user-facing UI and OIDC that are already working

**Cons**
- Loses the best Nextcloud features (Group Folders ACLs, OCS sharing) that we already built glue for — we'd be regressing capability
- Requires discipline; "just add one more NextcloudAdmin method" is the path of least resistance for every feature request
- Still paying ops cost for a feature subset

**Swap cost later:** Medium. If we keep discipline, the eventual swap is easier than Option A.

### Option C — MinIO (S3) + thin web UI

**What it means:** Deploy MinIO as the storage backend. Agents access files via an S3 client (boto3) — *or* keep a thin WebDAV gateway in front of MinIO so agent code doesn't change. For the user-facing UI, ship a lightweight web UI: options include **Filestash** (self-hosted file manager that speaks S3/WebDAV/SFTP/many backends, ~100MB image), a custom cockpit "Files" tab that hits MinIO presigned URLs directly, or MinIO's own built-in console.

`docs/cloud_workspace.md` already identifies MinIO as the chosen durable-storage layer for workspace checkpoints, so MinIO would consolidate two roles instead of adding a new dependency.

**Pros**
- Dramatically lighter: MinIO idles at ~100MB RAM, no PHP, no Apache, single binary
- S3 is the lingua franca of object storage — every tool, every cloud, every client library supports it
- Already on the roadmap for another role (workspace durability); folding file-exchange into MinIO shrinks total service count
- Presigned URLs give us safe, time-limited user uploads/downloads without running any auth glue
- Bucket policies handle multi-tenant isolation
- Horizontal scale is actually a thing (erasure coding, distributed mode)

**Cons**
- **No built-in user-visible sharing UX** — "share this folder with my teammate" becomes a cockpit feature we have to build, not a free platform feature
- **No OIDC-mapped home directories** — MinIO does support OIDC STS, but the UX of "click login, see your files" needs a separate frontend (Filestash or custom)
- **No versioning by default** (MinIO supports object versioning, but you have to enable and manage it)
- **No trash / undelete** — we'd implement soft-delete in application code
- User-facing UX requires picking *and maintaining* a frontend. Filestash is great but it's one more moving part; MinIO's console is admin-flavored, not user-flavored
- Migration cost: rewrite `NextcloudAdmin`, add an S3 client path to agents (or a WebDAV→S3 gateway), reprovision existing project folders

**Swap cost later:** Low. S3 is the industry standard and porting between S3-compatible backends is mechanical.

### Option D — Seafile

**What it means:** Replace Nextcloud with Seafile. Seafile is a lighter file-sync platform with a web UI, libraries (their name for buckets/folders), sharing, OIDC, and a reasonable API.

**Pros**
- Much lighter than Nextcloud (Go-based backend, not PHP)
- Keeps "user-facing web UI with sharing" as a free platform feature
- Has OIDC support
- Has versioning and trash
- Has a desktop client if it ever becomes relevant

**Cons**
- Smaller community than Nextcloud — fewer third-party integrations, fewer StackOverflow answers, slower upstream bug fixes
- We'd still have a vendor-specific admin client to maintain, just a different vendor
- Seafile's WebDAV support is a secondary feature (their primary protocol is their own); less battle-tested for agent workloads
- Feels like a sideways move: we swap one self-hosted file platform for another, saving some weight but not escaping the category
- Open-source edition is feature-restricted vs. commercial edition

**Swap cost later:** Medium. Same lock-in shape as Nextcloud, just at a smaller scale.

### Option E — Pure WebDAV server (sabre/dav, Caddy webdav plugin, hacdias/webdav)

**What it means:** Strip everything. Run a minimal WebDAV server (sabre/dav in PHP; Caddy + webdav plugin in Go; hacdias/webdav standalone binary). No web UI, no user management beyond HTTP basic auth or JWT.

**Pros**
- Absolute minimum ops footprint
- Agent code *literally does not change* — it already speaks WebDAV
- Trivially replaceable: swap one WebDAV server for another at any time

**Cons**
- **No user-facing UI.** This is a dealbreaker for requirement #3 unless we build a cockpit "Files" tab
- No sharing model beyond "here is the URL and password"
- No OIDC story — we'd bolt one on via a reverse proxy (oauth2-proxy etc.)
- No versioning, no trash, no quota management
- We'd be building from scratch a lot of what Nextcloud gives for free

**Swap cost later:** Minimal (by definition).

### Option F — Gitea LFS / Git-based storage

**What it means:** We already run Gitea for agent workspaces and code. Use Git repos + LFS as the file-exchange medium.

**Pros**
- Zero new services
- Gitea is already behind Keycloak SSO
- Versioning is the native model (it's Git)
- Per-project scoping is a repo; permissions come for free

**Cons**
- **Users are not expected to drive Git.** Uploading a PDF for an agent to process via `git add && git commit && git push` is a UX non-starter for non-developer users
- LFS is designed for large binaries in dev workflows, not for general file exchange
- "Show me the files in this project" becomes "browse the repo on Gitea" — possible but clunky
- Agents would need Git operations for every file change, which is heavier than WebDAV PUT/GET
- Repo size grows unbounded if not pruned; Git was not designed for ephemeral file churn

**Swap cost later:** High, because we'd be entangling file storage with source control.

## 5. Comparison Matrix

| Criterion | A: Nextcloud as-is | B: Nextcloud frozen | C: MinIO + UI | D: Seafile | E: Pure WebDAV | F: Gitea LFS |
|---|---|---|---|---|---|---|
| Idle RAM footprint | ~1–2 GB | ~1–2 GB | ~100 MB | ~300 MB | ~20 MB | (already running) |
| Dedicated database | Yes (PG) | Yes (PG) | No | Yes (SQLite/MySQL) | No | (already running) |
| User-facing web UI | Built-in, polished | Built-in | Needs Filestash or cockpit tab | Built-in | None | Gitea web UI (clunky for files) |
| OIDC identity mapping | Built-in | Built-in | Via STS + frontend | Built-in | Via reverse proxy | Built-in |
| User-to-user sharing UX | Built-in | Built-in (pre-gen links only) | Build it yourself | Built-in | None | Git permissions |
| Versioning / trash | Built-in | Built-in | Opt-in (object versioning) | Built-in | None | Native (Git) |
| Agent code changes | None | None | None (if WebDAV gateway) or small (if direct S3) | None | None | Significant |
| Vendor-specific admin code | ~528 LoC | ~528 LoC (frozen) | New, smaller | New, similar size | None (~50 LoC) | None |
| Upgrade pain | High | High | Low | Medium | Minimal | N/A |
| Lock-in | High | Medium | Low | Medium | None | High |
| Already in the roadmap | Yes | Yes | Yes (workspace durability) | No | No | Yes (for git) |
| Migration effort from today | 0 | ~1 day (discipline + refactor) | ~1–2 weeks | ~1–2 weeks | ~3–5 days + UI build | ~2 weeks + UX gamble |

## 6. Decision Points

Questions to answer before committing. Each one meaningfully changes the recommendation.

1. **How important is "user clicks a button to share a folder with a teammate"?**
   - If critical → Nextcloud (A/B) or Seafile (D) are the only turnkey options
   - If acceptable to build in cockpit → MinIO (C) becomes viable

2. **Is the multi-user story "admins provision projects" or "users self-serve"?**
   - Admin-provisioned → sharing can be pre-wired at creation time; MinIO works
   - Self-serve → need a native sharing UX; Nextcloud or Seafile

3. **Do we expect to ever want Collabora / document editing?**
   - Yes → Nextcloud is already integrated, stay
   - No → Nextcloud's main differentiator is gone

4. **How load-bearing is the existing session-folder sync (`workspace_sync.py`)?**
   - If it works and users depend on it → migration risk is real
   - If it's still experimental → migrate now before more code depends on it

5. **Is MinIO going to be deployed anyway for workspace durability (per `docs/cloud_workspace.md`)?**
   - If yes → MinIO becomes "free" as a file-exchange backend and Option C becomes the obvious win
   - If no → Option C costs us a new service, changing the math

6. **Who is the target user for the file-exchange UI?**
   - Non-technical end users → need a polished UI (Nextcloud, Seafile, Filestash)
   - Technical users / developers → cockpit tab + presigned URLs is fine

7. **What's our appetite for maintenance?**
   - "Less is more" → C or E
   - "Use what works, don't rewrite" → A

## 7. Recommendation

Two honest takes depending on the answer to decision point #5:

**If MinIO is going to be deployed anyway for workspace checkpoints:** go with **Option C (MinIO + thin UI)**. The marginal cost of using it for file exchange is near zero, we consolidate on one storage primitive, and we shed ~1.5GB of RAM, a database, and 500 lines of vendor glue. The main investment is building a cockpit "Files" tab (or dropping in Filestash) to replace Nextcloud's UI. We keep WebDAV tools unchanged via a gateway, so agent code is untouched.

**If MinIO is not already on the deployment path:** go with **Option B (Nextcloud, frozen)**. Stop adding to `NextcloudAdmin`, treat Nextcloud as a WebDAV+UI appliance, and revisit in a quarter when the roadmap is clearer. This preserves optionality without funding either migration or further lock-in.

**Not recommended as a first move:** Options D (Seafile) and F (Gitea LFS). Seafile is a sideways trade — we swap vendors without escaping the category. Gitea LFS forces a Git-based UX on users who shouldn't have to think about Git.

**Not recommended yet:** Option E (pure WebDAV). The user-facing UI gap is too large to close without building our own frontend, and if we're going to do that we may as well get the S3 ecosystem for free via Option C.

The call between C and B hinges on the MinIO workspace-durability question. That should be resolved first; the file-exchange decision follows from it.

## 8. Open Questions

- What is MinIO's actual deployment status today — planned, committed, or already running? (See `docs/cloud_workspace.md` for the workspace-checkpoint story.)
- How many existing projects have live Nextcloud Group Folders with real user data? Migration cost scales with this.
- Does the `workspace_sync.py` bidirectional sync have users in production, or is it still experimental?
- What is the enterprise story — do we have a prospect/customer who has explicitly asked for Nextcloud-the-product vs. "a file exchange that works with our SSO"?
- Is there an appetite to build a cockpit "Files" tab regardless of backend, to avoid embedding a third-party UI in the primary UX?

## 9. Related Documents

- `docs/features/sso_and_cloud_storage.md` — current Nextcloud + Keycloak design (status: design phase)
- `docs/features/project_cloud_folders.md` — project/session folder provisioning design
- `docs/cloud_workspace.md` — workspace architecture (mentions MinIO for durable storage)
- `docs/datasources.md` — datasource system overview
- `docs/issues/nextcloud_oidc_username.md` — known OIDC username resolution issue
