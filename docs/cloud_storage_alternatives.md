---
tags:
  - cloud-infrastructure
  - storage
  - architecture
  - decision
status: decided
decision: OpenCloud is the new default file-exchange backend; Nextcloud remains the reference implementation for "bring your own cloud" and stays in the stack
related:
  - "[[features/sso_and_cloud_storage]]"
  - "[[features/project_cloud_folders]]"
  - "[[cloud_workspace]]"
  - "[[datasources]]"
---

# Cloud Storage Alternatives

A decision document for choosing the **default** file-exchange backend that ships with the system, given the architectural commitment to swappable cloud backends. The original framing of this document was *"is Nextcloud the right tool?"* — that question has been re-answered by the deep-cloud-integration product thesis (Position 1 in §1). The remaining question is which backend ships as the out-of-the-box default for users who do not bring their own cloud.

## Decision (2026-04-11)

**Default backend: OpenCloud.** Nextcloud remains in the codebase as the reference implementation for users who bring an existing Nextcloud instance. The file-exchange layer will be swappable; OpenCloud and Nextcloud are the first two adapters.

**Why OpenCloud:**
- Web UI feels close to Google Drive — flat tree, fast, modern, immediately legible to non-technical users (verified by hands-on side-by-side test against Seafile on 2026-04-11)
- File-focused scope (not a collaboration platform); matches what we actually need
- Native cross-platform sync clients (desktop + mobile) inherited from the oCIS lineage
- OIDC native (LibreGraph Connect ships by default; Keycloak can be substituted as the IdP)
- Active fork of ownCloud Infinite Scale (oCIS) led by Peer Heinlein's group following Kiteworks' acquisition of ownCloud GmbH; explicit OSS commitment
- Single-folder sync limitation is acceptable — it matches how MS365 OneDrive, iCloud Drive, and Google Drive File Stream all work today

**Why not Seafile (tested side-by-side):** The library-first paradigm is well suited to large organizations with many shared cross-team projects but adds organizational friction for individual / family users. The web UI feels organizational rather than personal; the library list at the top level is a cognitive speed bump that does not exist in Drive, OneDrive, or OpenCloud. Seafile's technical strengths (block-level dedup, sync reliability) do not compensate for the UX mismatch in our target use case.

**Why Nextcloud stays in the stack:** "Bring your own Nextcloud" is a real user story, and Nextcloud is the deepest existing integration. The current `nextcloud_admin.py` becomes the *first* vendor adapter in a swappable backend system rather than the *only* backend.

The rest of this document is the analysis that led to this decision.

## 1. Problem / Topic

The system has a file-exchange layer between users and agents: users drop files in a project folder, agents read them, agents write results back, users see them. Nextcloud has been the implementation of this layer since the start.

Two product positions had to be reconciled:

**Position 1 — deep cloud integration is the product.** AI systems are essentially model wrappers; raw model quality is not a moat (every model gets better, across vendors). The moat is integration depth into the user's existing data and systems. *"A 30b-parameter model with access to all your files beats Opus-4.6 without it."* This position justifies the existing depth of the Nextcloud integration and motivates parallel integrations for Google Workspace, Office 365, and other major cloud systems in the future.

**Position 2 — the default backend should be lighter and more user-friendly.** For users who do not already have Nextcloud (or any other BYO backend), the system should ship a default that is genuinely pleasant to use. The Nextcloud web UI does not feel like a modern consumer cloud; the operational footprint is heavy for what we actually need; the sync client UX is uneven across platforms.

These two positions are not in conflict if the storage layer is **swappable**. Position 1 says: *deep integration is the product, so build a vendor adapter for every important backend.* Position 2 says: *one of those adapters should be the default, and that default should not be Nextcloud.* This document selects which backend becomes the default adapter.

## 2. Current Implementation State

### 2.1. What we use the file-exchange layer for

| Capability | Who uses it | Where it lives |
|---|---|---|
| File list / read / write / delete / info | Agents (as tools) | `src/tools/cloud/webdav.py` (242 lines) |
| Per-project Group Folder provisioning | Orchestrator (on project create) | `orchestrator/services/nextcloud_admin.py` (528 lines) |
| Per-user home directory + OIDC identity mapping | End users via browser | Nextcloud `user_oidc` app + Keycloak |
| Per-session folder for persistent threads | Orchestrator + agent | `src/services/workspace_sync.py` |
| Folder sharing (user ↔ agent service account) | Orchestrator | `NextcloudAdmin` via OCS Share API |
| WebDAV datasource wiring for agents | Orchestrator + agent | `DS_TOOL_MAP["webdav"]` in `orchestrator/main.py`, connection in `src/agent.py` |

### 2.2. What we deliberately do not use

- Collabora / OnlyOffice document editing
- Calendar, Contacts, Mail
- Talk (audio/video calls)
- Deck, Notes, Tasks
- Federation, end-to-end encryption, activity feed, comments, tags

The Nextcloud-specific primitives we *do* use are: **Group Folders, OCS Provisioning, OCS Share, OIDC home directories**. These four primitives are what the future `CloudBackendAdmin` interface needs to express across vendors.

### 2.3. Integration layers

The integration sits at four distinct layers, ordered from most reusable to most coupled:

1. **Agent tool layer (clean, backend-agnostic).**
   `src/tools/cloud/webdav.py` exposes `cloud_list`, `cloud_read`, `cloud_write`, `cloud_delete`, `cloud_info`. The module docstring is explicit: *"Provides file access to WebDAV (Nextcloud, ownCloud, or any WebDAV server) attached as a datasource."* These tools are tied to the WebDAV *protocol*, not to Nextcloud. **For OpenCloud, this layer continues to work unchanged** — OpenCloud also speaks WebDAV.

2. **Datasource type (clean, protocol-level).**
   The orchestrator's `DS_TOOL_MAP` registers `webdav` as a first-class datasource type alongside PostgreSQL, MongoDB, Neo4j. Read-only flag toggles write-tool injection. The schema uses a generic `webdav` type; the only vendor-named column is `projects.nextcloud_folder_id`, which can be renamed or generalized.

3. **Admin provisioning (vendor-coupled).**
   `orchestrator/services/nextcloud_admin.py` — 528 lines — is the one place where Nextcloud-specific APIs leak in. It calls:
   - Nextcloud **Group Folders** REST API (non-standard, Nextcloud-only)
   - Nextcloud **OCS Provisioning** API (user/group management)
   - Nextcloud **OCS Share** API (folder sharing semantics)
   - WebDAV `MKCOL` for raw folder creation

   This is the file that becomes the *first vendor adapter* in the swappable design. Adding OpenCloud means writing a sibling `opencloud_admin.py` against a shared `CloudBackendAdmin` interface that both implement.

4. **Deployment (mechanical).**
   - `docker-compose.yaml` — `nextcloud:31-apache` service on port 8800
   - `docker/nextcloud/setup-nextcloud.sh` — init script
   - `docker/keycloak/setup-nextcloud-oidc.sh` — OIDC wiring
   - `deployment/19-nextcloud.yaml` — K8s manifest
   - ~15 env vars (`NEXTCLOUD_*`) in `.env.example`
   - A dedicated Postgres database + user, provisioned in `init.py`

   For OpenCloud, sibling files are added at this layer; the existing Nextcloud files do not have to disappear.

### 2.4. Migration shape

The abstraction boundary is in the right place. Agent code is protocol-level (WebDAV), not vendor-level (Nextcloud). Adding OpenCloud as the new default is **additive**, not a rip-and-replace:

1. Define a `CloudBackendAdmin` interface capturing the four primitives in §2.2
2. Refactor `nextcloud_admin.py` to implement that interface — mechanical, no behavior change
3. Write `opencloud_admin.py` as a sibling implementation against OpenCloud's APIs (~300–500 LoC)
4. Add OpenCloud sibling deployment artifacts in `docker-compose.yaml` and the K8s manifests
5. Add a backend selector (env var, defaulting to OpenCloud for new installs)

What this *does not* require: changing agent code, changing the WebDAV tool surface, changing the datasource model, or migrating existing user data (Nextcloud installs stay on Nextcloud).

## 3. What We Actually Need

The requirement set splits cleanly into agent-side and user-side. The agent side is well-served by any WebDAV-capable backend. The user side is where the choice between backends actually matters.

### 3.1. Agent-side requirements

| # | Requirement | Load-bearing? |
|---|---|---|
| A1 | List / read / write / delete / info on files under a namespace | **Hard** |
| A2 | Per-project scoping | **Hard** |
| A3 | Programmatic provisioning of folders and users | **Hard** |
| A4 | Folder sharing API (folder ↔ user / service account) | **Hard** |
| A5 | OIDC identity mapping (so user-uploaded files appear under the right home) | **Hard** |

All evaluated backends meet A1–A5. The implementation differs (REST vs OCS vs LibreGraph) but the capability is present.

### 3.2. User-side requirements

| # | Requirement | Load-bearing? | Why |
|---|---|---|---|
| U1 | Web UI as close to Google Drive as possible | **Hard** | Production users include non-technical family on the user's homelab; a clunky UI is a non-starter |
| U2 | Reliable cross-platform sync client (Linux, macOS, Windows, iOS, Android) | **Hard** | Files have to make it onto user devices to be useful |
| U3 | OIDC / Keycloak compatibility | **Hard** | Single sign-on is non-negotiable in this stack |
| U4 | File-focused scope (not a collaboration platform) | **Hard** | We only want files; we are not adopting a calendar/chat/notes suite |
| U5 | Self-hosted, OSS, healthy upstream | **Hard** | Vendor independence and community velocity |
| U6 | Sharing UX a non-technical user can drive | **Soft** | "Click to share with my partner" |
| U7 | Versioning / trash | **Soft** | Safety net |

The two requirements that initially appeared to pull in opposite directions are U1 (Drive-like UX, which favors flat-tree paradigms) and U2 sync arbitrariness (which historically favored library-based paradigms like Seafile's). The decision below trades U2's sync arbitrariness for U1's UX quality, on the basis that the single-root-folder sync limitation matches how most consumer cloud clients actually work today.

## 4. Alternatives

Each option is evaluated against the user-side requirements in §3.2. Agent-side requirements are met by every option in this section.

### Option A — Nextcloud (status quo / reference implementation)

**What it means:** Keep Nextcloud as the default and continue deepening the integration.

**Pros**
- Zero migration work
- Already covers every requirement, including the soft ones
- OIDC home directories are real and work
- Sharing UX is built
- The strategic deep-integration play (Position 1 in §1) wants Nextcloud as one of the supported backends regardless

**Cons**
- Web UI does not feel like a modern consumer cloud (the original motivation for this document)
- Heaviest ops footprint in the stack (PHP + Apache + dedicated Postgres + multiple apps)
- Major version upgrades have historically been painful and block on app compatibility
- We pay for 50+ features we don't use
- Sync client UX is uneven across platforms

**Status:** Stays in the stack as the reference implementation for users who already run Nextcloud or want the full collaboration suite. **Not the new default.**

### Option D — Seafile (hands-on tested)

**What it means:** Replace Nextcloud's default role with Seafile. Seafile is a Go-backed file-sync platform with libraries (their name for top-level buckets), block-level dedup, OIDC, and a desktop client with a strong sync reputation.

**Pros**
- Much lighter than Nextcloud
- Block-level dedup and delta sync are best-in-class
- Has OIDC support
- Active commercial development with a clear OSS edition
- Native desktop and mobile clients
- Versioning and trash built in

**Cons**
- **Library-first paradigm.** Seafile organizes everything around "libraries" rather than a single tree. Each library is a separate root with its own ACLs, history, and (optionally) encryption. This is technically excellent for large organizations with many shared cross-team projects but adds organizational friction for individual / family users who want one home directory and a few subfolders.
- **Web UI does not feel like Google Drive.** Hands-on testing (Seafile 11.0.13, latest `seafileltd/seafile-mc:latest`) confirmed that the UI feels organizational rather than personal. The library list at the top level is a cognitive speed bump that does not exist in Drive, OneDrive, or OpenCloud.
- WebDAV support is a secondary feature in Seafile (their primary protocol is SeafileHTTP). For the agent-side this matters less than originally feared because we will ship vendor-specific adapters for major clouds anyway, but it is an extra integration cost.
- Open-source edition is feature-restricted vs. commercial edition

**Verdict from hands-on testing (2026-04-11):** **Rejected.** The library-first paradigm is the wrong shape for the target user (individuals and small households on the user's homelab). Seafile would be the correct choice for a large organization with many shared cross-team projects, but that is not the deployment we are optimizing for.

### Option G — OpenCloud (chosen)

**What it means:** OpenCloud is an active fork of ownCloud Infinite Scale (oCIS), led by Peer Heinlein's group (mailbox.org) after Kiteworks acquired ownCloud GmbH. It is Go-backed, file-focused, ships with the LibreGraph Connect IdP (replaceable with Keycloak), and presents a flat-tree web UI that is the closest self-hosted match to Google Drive currently in active development.

**Pros**
- **Web UI is close to Google Drive.** Hands-on testing (latest `opencloudeu/opencloud-rolling`) confirmed it. The home view shows files and folders directly with no library cognitive overhead. Drag-and-drop, breadcrumbs, search, and quick actions are where a modern user expects them.
- **File-focused scope.** Not a collaboration platform; not trying to be a Google Workspace replacement. The scope matches our requirements exactly.
- **Active fork with credible stewardship.** Peer Heinlein's group has a track record in self-hosted infrastructure (mailbox.org). The fork was a response to the Kiteworks acquisition and reflects an explicit OSS commitment.
- **Native cross-platform sync clients** inherited from the oCIS lineage (desktop + mobile).
- **OIDC native.** Ships with LibreGraph Connect; Keycloak can be substituted as the IdP via standard OIDC configuration.
- **Lighter ops footprint than Nextcloud.** Single Go binary, no PHP, no required dedicated database for the basic install.

**Cons**
- **Single-folder sync root** on the desktop client. Initially flagged as a blocker, but accepted in context: it matches how MS365 OneDrive, iCloud Drive, and Google Drive File Stream all work today. Users who want arbitrary folder sync are in the minority and can fall back to manual organization.
- Younger upstream than Nextcloud; smaller community; fewer third-party integrations and tutorials.
- Vendor-specific admin API to learn (LibreGraph + OpenCloud extensions).
- Integration with our Keycloak is new work (replacing the bundled IdP with our SSO).

**Verdict from hands-on testing (2026-04-11):** **Chosen.** The UX is meaningfully better than both Nextcloud and Seafile for the target user, the swappable backend plan covers the lock-in concern, and the single-root sync limitation is consistent with mainstream consumer cloud conventions.

### Briefly considered, not pursued

- **ownCloud Infinite Scale (oCIS).** OpenCloud's parent project. Post-Kiteworks acquisition the upstream has more uncertainty than the OpenCloud fork; OpenCloud is the better bet.
- **Pydio Cells.** Strong file management, but commercial-first focus and the community edition is feature-restricted. UI is good but not better than OpenCloud.
- **Cozy Cloud.** Personal-cloud focus aligns with the use case but the project has lower momentum and a smaller community.
- **FileRun.** Closed source.
- **Alist / OpenList.** File-aggregator style, not a backend in its own right.
- **FileBrowser Quantum.** Too minimal; no sharing model worth speaking of.
- **MinIO + thin web UI.** Ruled out by Position 1 in §1: MinIO is reserved for technical systems (Gitea, snapshots), and the user-facing cloud is intentionally a separate concern from the object store.
- **Pure WebDAV server / Gitea LFS.** The user-facing UI gap is too large to close without building a frontend, and a Git-based UX is a non-starter for non-technical users.

## 5. Comparison Matrix

| Criterion | A: Nextcloud | D: Seafile | G: OpenCloud (chosen) |
|---|---|---|---|
| Web UI feel (Drive-like) | Functional, dated | Library-first, organizational | Flat-tree, modern, closest to Drive |
| Idle ops footprint | Heavy (PHP + Apache + Postgres) | Light (Go backend) | Light (single Go binary) |
| Dedicated database | Yes (Postgres) | Yes (MariaDB or SQLite) | Optional (embedded by default) |
| OIDC / Keycloak | Built-in (`user_oidc` app) | Built-in | Built-in (LibreGraph Connect, replaceable) |
| Native sync client (desktop + mobile) | Yes, uneven | Yes, strong | Yes (oCIS lineage) |
| Sharing UX | Mature | Library + folder shares | Flat-tree shares, modern UX |
| Versioning / trash | Built-in | Built-in (per-library) | Built-in |
| Agent code changes to support | None (current) | None (WebDAV) or vendor adapter | None (WebDAV) initially; vendor adapter later |
| Vendor adapter LoC | ~528 (current) | New (similar size) | New (~300–500) |
| Upgrade pain | High historically | Medium | Unknown (young project) |
| Lock-in shape | Heavy (collab features) | Library paradigm | Light (file-focused) |
| Community / upstream health | Largest | Medium, commercial-led | Young, active fork with explicit stewardship |
| Status in our stack | Stays as BYO reference | Rejected | New default |

## 6. Decision Rationale

The decision was driven by three observations:

1. **The deep cloud integration thesis (§1, Position 1) means Nextcloud is not going anywhere.** It stays as the reference implementation for users who bring their own. So this decision is not "Nextcloud or alternative" — it is "what default ships when there is no BYO backend."

2. **Hands-on UX testing was decisive.** Both Seafile (port 8900, `seafileltd/seafile-mc:latest`) and OpenCloud (port 9200, `opencloudeu/opencloud-rolling`) were spun up locally and tested side-by-side on 2026-04-11. Seafile's library-first paradigm felt organizational; OpenCloud's flat-tree felt personal. For the target user (individuals on a homelab, including non-technical family), OpenCloud won unambiguously.

3. **The single-folder-sync limitation that initially looked like a blocker is actually how most modern consumer clouds work.** MS365, iCloud, Drive File Stream — none of them sync arbitrary folders. The limitation is a small pain point, not a dealbreaker.

## 7. Implementation Plan

The migration to OpenCloud as the default is not a rip-and-replace. It is additive: OpenCloud joins the stack as a sibling of Nextcloud, and the swappable-backend interface is built up over time. Ordered by priority:

1. **Stand up OpenCloud as a parallel service** in `docker-compose.yaml` (port TBD; see open question §8) and a sibling K8s manifest. Wire it to Keycloak as the OIDC provider in place of LibreGraph Connect.
2. **Define a `CloudBackendAdmin` interface** in `orchestrator/services/cloud_admin.py` capturing the four primitives in §2.2: provision project folder, provision user, share folder, map OIDC identity.
3. **Refactor `nextcloud_admin.py` to implement that interface.** Mechanical change; no behavior change. Add tests if missing.
4. **Implement `opencloud_admin.py`** against the same interface, using OpenCloud's LibreGraph + extension APIs.
5. **Add a backend selector** (env var `CLOUD_BACKEND=opencloud|nextcloud`, default `opencloud` for new installs). Existing installs keep `nextcloud`.
6. **Smoke-test the agent tool path against OpenCloud's WebDAV endpoint.** This should be a no-op for tool code; verify it.
7. **Run a real end-to-end workflow** (job creation → folder provision → user upload → agent read/write → user view) against OpenCloud on the homelab.
8. **Update `docs/features/sso_and_cloud_storage.md`** to document the swappable-backend design and the OpenCloud-as-default decision.
9. **Park `nextcloud_admin.py` as a maintained-but-frozen reference adapter.** No new features unless specifically for the BYO-Nextcloud user story.

Out of scope for this initiative:
- Migrating any existing Nextcloud data to OpenCloud (existing installs stay on Nextcloud)
- Building Google Workspace, Office 365, or other vendor adapters (separate workstreams that benefit from the same `CloudBackendAdmin` interface)
- Decommissioning Nextcloud from the codebase (it stays as a first-class adapter)

## 8. Open Questions

- **What port does OpenCloud get in `docker-compose.yaml`?** Nextcloud is on 8800. OpenCloud could go on 8810 (close to Nextcloud) or 8900 (currently used by the disposable Seafile test instance).
- **Does OpenCloud's LibreGraph Connect cleanly hand off to Keycloak as the IdP, or are there subclaim mismatches similar to the ones we hit with Nextcloud?** (See `docs/issues/nextcloud_oidc_username.md`.)
- **Does OpenCloud have an equivalent of Group Folders, or do we model project shares as user-shared folders under a service account?** This affects the `CloudBackendAdmin` interface design.
- **How does OpenCloud handle quota?** Nextcloud has per-user and per-Group-Folder quotas. If OpenCloud only has per-user, the project-folder model has to change.
- **Backup strategy for OpenCloud data on the homelab.** Likely the same large-S3-store solution that is planned for the user-facing cloud's backup role; needs to be confirmed.
- **OpenCloud upstream stability.** Young fork; we should pin a tag rather than `rolling` for the production install and follow upstream releases closely for the first 6–12 months.

## 9. Related Documents

- `docs/features/sso_and_cloud_storage.md` — current Nextcloud + Keycloak design (status: design phase). To be updated to reflect the swappable-backend plan.
- `docs/features/project_cloud_folders.md` — project/session folder provisioning design
- `docs/cloud_workspace.md` — workspace architecture
- `docs/datasources.md` — datasource system overview
- `docs/issues/nextcloud_oidc_username.md` — known OIDC username resolution issue (relevant precedent for OpenCloud OIDC integration)
