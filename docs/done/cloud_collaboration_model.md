---
tags:
  - feature
  - architecture
  - cloud-infrastructure
  - projects
  - sessions
  - jobs
  - product
aliases:
  - cloud collaboration model
  - cloud-mirror workspaces
  - project folder as workspace
  - cloud-native collaboration
related:
  - "[[project_cloud_folders]]"
  - "[[main_cloud_abstraction]]"
  - "[[sso_and_cloud_storage]]"
  - "[[projects]]"
  - "[[sessions]]"
  - "[[ephemeral_workspaces]]"
  - "[[workspace_simplification]]"
  - "[[multi_tenancy]]"
  - "[[subjob_worktree_sharing]]"
  - "[[verification_phase]]"
  - "[[persistent_thread_lifecycle]]"
  - "[[session_folder_placement]]"
---

# Cloud Collaboration Model — Cloud-Mirror Workspaces with Repository Scaffolding

> The agent's workspace is a synced mirror of the user's cloud surfaces — their default project mounts at the workspace root; other attached projects and repos mount under `projects/` and `repos/`. The cloud folder is canonical. Repositories run alongside as **temporary scaffolding** for diff viewing and recovery — not as the primary collaboration surface. Branches and PR-style review are valuable but ship as **v2+ opt-in extensions**, not the v1 foundation. The v1 product story is: "the AI sees what you see, edits where you edit, and any agent run can be diffed or rolled back."

**Status:** ✅ Closed 2026-05-18. Phases 1, 2, 2.1, 2.2, 3, 4 shipped + verified (3b retired — covered by pre-existing `repository`-datasource flow).
**Filed:** 2026-05-16
**Last updated:** 2026-05-18
**Depends on:** [[project_cloud_folders]] (current per-project + per-session folder lifecycle), [[main_cloud_abstraction]] (`MainCloudBackend` Protocol + OpenCloud default).

> **Closure note (2026-05-18):** This doc covered the **session-side** cloud-mirror foundation. It shipped fully. Job-side cloud integration originally lived here as Phase 5 (staging clone) and Phase 6 (accept UI), but the framing changed: a much simpler "export job results to cloud" button is shipping instead, designed in `docs/done/job_cloud_export.md`. The bigger accept-UI / diff-view ambition is deferred to that doc as a follow-up. Phase 7 (per-turn snapshots + session timeline) and Phase 8 (orphan session-folder sweep) remain intentionally deferred — file separately if/when picked up.

## 1. Motivation

The differentiator we keep telling ourselves matters is **deep cloud integration**. Users don't want to copy a file out of OpenCloud, paste it into a chat upload, wait for the agent to produce something, then download the result and put it back. They want to say "use that document, put the output next to it" and have it happen — in the same folder, with versioning, with the ability to review what changed if something looks off.

Copilot-class tools have spent years failing at this. The reason isn't a model limitation; it's that the surrounding system was never designed for *the agent and the user editing the same file space*. We have the surrounding system. We just haven't wired it up to behave that way.

Today, the wiring produces fragmentation:

- Every persistent session creates its **own** cloud folder under `Sessions/<thread-id>/`. Files the agent puts there are visible to the user, but they live in a per-session silo. Across sessions, the user accumulates orphaned folders with no project context. ([`_setup_main_cloud` — `orchestrator/main.py:10479`](../../orchestrator/main.py))
- Every persistent thread also gets its **own** Gitea repo `thread-<id>` ([`_setup_gitea` — `orchestrator/main.py:10445`](../../orchestrator/main.py)). A session has two parallel workspaces (cloud folder + Gitea repo), neither of which is the project's actual file space.
- Projects already have a **project cloud folder** ([[project_cloud_folders]]) but sessions and jobs don't operate *on* it — they operate alongside it.
- A `POST /api/jobs/{job_id}/promote` endpoint exists (`orchestrator/main.py:16762`) but it means "move this job into a *new* project," not "land this job's output into the project it came from." There is no first-class verb for the latter.

The result: the agent's work output is mechanically separated from the user's living file space. The user has to manually pull things across — exactly the copy-paste loop the product is supposed to eliminate.

This document proposes collapsing the topology so the user's project folders *are* the agent's working space, with lightweight repository scaffolding running underneath for diff and recovery.

## 2. Why Cloud-Share, Not Repositories

A repository-first model is tempting and personally I'd prefer it: branches isolate work, PRs make reviews structured, history is durable, conflicts surface explicitly. It's the right model — for the audience that lives in Git.

That audience is not most of our users.

The majority of businesses and individuals collaborate via Google Drive, OneDrive/Teams, Dropbox, or Nextcloud/OpenCloud. They share folders, not branches. They open files by clicking, not by `git checkout`. They resolve conflicts by talking to a coworker, not by reading three-way merge markers. We cannot force this audience to adopt repositories and Markdown as a precondition for using the product. If we try, we lose to the file-share-native experience they already have — even if our agent is better.

So we adopt the file-share model ourselves, and we *complement* it with temporary repositories that run behind the scenes to give us:

- **Diff viewing** — when a job finishes, the user can see exactly what changed before accepting.
- **Recovery** — if a session or job damages files, we can roll back to a snapshot.

That's the v1 role of the repository layer. Not branches. Not PRs. Snapshots and diffs, used by Cockpit and never seen as a "Git repo" by the user.

Once that foundation is in place and proven, we can layer optional repo-style workflows on top — branches per session, PR-style review modes, live-vs-review toggles — for the subset of users who *want* that workflow. That's §7. It's a v2+ extension, not a v1 requirement.

This is the central design call of this document. Everything below follows from it.

## 3. What Already Exists (and what to keep)

The current architecture has more of the right pieces than the fragmentation suggests. The proposed model is mostly a re-wiring, not a rebuild.

| Capability | Where | Reuse as-is? |
|---|---|---|
| Per-project cloud folder | `MainCloudBackend.ensure_project_folder()` — provisioned at project create | **Yes** — canonical state for the project |
| User home folder discovery | `MainCloudBackend.get_user_home()` | **Yes** — used when the default project is attached |
| Per-session cloud folder | `_setup_main_cloud()` → `ensure_session_folder()` | **Deprecate** — sessions mirror attached project folders into the workspace; no separate session folder |
| Per-thread Gitea repo `thread-<id>` | `_setup_gitea()` | **Replace** — Gitea takes per-turn workspace snapshots instead of being a parallel workspace |
| Workspace ↔ cloud bidirectional sync | `src/services/cloud_sync/` (etag-based) | **Yes, extended** — sync runs against multiple cloud surfaces mounted into the workspace |
| Per-subjob git worktrees on shared backend | [[subjob_worktree_sharing]] (shipped) | **Reuse pattern** — proven precedent for the v2+ branch layer |
| Job promotion (move job → new project) | `POST /api/jobs/{job_id}/promote` | **Keep, add sibling** — new "accept job changes into project" verb |
| Workspace files API (`webdav_*` tools) | `src/tools/webdav/` — vendor-neutral WebDAV | **Yes** — no changes needed |
| Critic-driven review primitive | [[verification_phase]] | **Reuse** — the v1 job accept/diff UI builds on this |

Two existing design docs frame the current behaviour:

- [[project_cloud_folders]] introduced the per-project + per-session split. That split was intentional when projects were optional. Now that every thread has a project (the default project if nothing else), the per-session tier no longer earns its weight.
- [[main_cloud_abstraction]] gave us a vendor-neutral `MainCloudBackend` interface. The new model is implementable against the same interface — no further vendor-specific work.

[[session_folder_placement]] asks the question this document answers ("where do session folders live?") and is superseded by §4 below — see §12.

## 4. The Foundation: Cloud-Mirror Workspaces (v1)

**Sessions don't have folders. They have *mounts*.**

When a session starts, the orchestrator looks at the projects (and repos) attached to it and mounts each into the agent's workspace at a deterministic path. Inside the workspace, the agent operates on ordinary files. Behind the scenes, those files are kept in sync with the user's cloud folders.

### Workspace layout

```
/workspace/
├── <user's default-project files>     ← if default project is attached, mounts at root
│
├── projects/
│   ├── <project-A>/                   ← other attached projects
│   └── <project-B>/
│
├── repos/
│   ├── <repo-1>/                      ← attached Git repos (existing pattern)
│   └── <repo-2>/
│
└── .srw/                              ← orchestrator-managed; not user-touchable
    └── snapshots/                     ← per-turn Gitea snapshots (recovery + diff)
```

Two reasons for "default project at root, others under `projects/`":

1. **Ergonomics.** The default project is the user's personal working space. Common sessions have only the default project attached. Putting it at the root means the agent doesn't have to write `projects/default/foo.md` when there is only one possible project — paths stay short.
2. **Parity with the user's cloud view.** In OpenCloud the user sees their home folder at the root and shared project folders under `Shared/` or similar. The workspace mirrors that mental model.

### What syncs, and when

Bidirectional sync at **turn boundaries**:

- **At turn start**: pull any cloud-side changes the user (or another session) made since the last sync. The agent sees those edits before forming its next response.
- **At turn end**: push the agent's edits to the cloud. The user sees them as soon as the turn completes.

The existing `cloud_sync` machinery (etag-based pull, mtime-based push) does this today against a single folder; v1 extends it to drive multiple mounts in parallel.

### Default project = no special case

The fix this whole document started from — "if a session is on the default project, seed the workspace from the user's home folder" — becomes a single line of the mounting rule: *if the default project is attached, mount its folder (the user's home) at the workspace root.* There is no separate code path, no asymmetry, no "default project mode."

### What the user sees

In OpenCloud: their personal home folder and any shared project folders, exactly as today.

In Cockpit's session UI: a small "Project files" panel listing which projects/repos this session has mounted, with a deep link to each in OpenCloud.

The agent and the user are touching the same files. That is the entire product story for v1.

## 5. Operating Modes (v1)

Only two modes, deliberately:

### Session: live mirror

- Workspace is a synced mirror of attached cloud surfaces.
- Agent writes propagate to the cloud at turn end.
- User writes propagate into the agent's workspace at the next turn start.
- No review, no promote, no accept. The user trusts what's happening (it's their session, they're watching).

This is the default and only mode for sessions in v1. Power-user opt-ins (review mode, branch isolation) are deferred to §7.

### Job: isolated clone + accept-on-completion

- At job-start, clone the attached cloud surfaces into a job-scoped staging area (a hidden cloud-side folder, e.g., `/Drafts/jobs/<job-id>/` — exact location is an open question, §10).
- The job's workspace mounts the staging area, not the live project folders.
- Job writes go to the staging clone. The user's live files are untouched while the job runs.
- On job completion, Cockpit shows a diff (staging vs project folders) and an "Accept" button.
  - **Accept** → orchestrator applies the staged changes to the project folders.
  - **Reject** → discard the staging clone.
  - **No action** → staging clone retained for a retention window (§10), then auto-archived.

This is the user's "save to project folder" review flow, with the file-share model preserved on both sides of the boundary.

## 6. The Repository Layer (v1, Supporting Role)

Gitea earns its keep in v1 by being the durable side-channel for diff and recovery. The user never opens it as a Git repo. Cockpit reads from it.

### Per-session snapshot stream

- Each session has a per-turn snapshot repo. After every turn's sync-back, the workspace state is committed as a snapshot.
- These snapshots are the source for:
  - **Session timeline UI** — "what changed between turn 7 and turn 12?"
  - **Rollback** — "revert to the state before turn 9" restores files from snapshot back into the cloud folders.
- Snapshots are pruned aggressively (e.g., keep last 100 turns, then squash). They are scaffolding, not a long-term archive.

### Per-job diff source

- The job's staging clone is committed to a per-job repo at job-start and at each meaningful agent transition.
- The "Accept" diff view in Cockpit reads from this repo — comparing the staged tip against the project-folder baseline (which is itself a snapshot at job-start).
- After accept/reject, the repo is retained for the retention window and then pruned.

### What Gitea is *not* in v1

- Not the source of truth.
- Not a user-facing collaboration surface.
- Not branched per session in a way the user would ever see.
- Not used for merge conflict resolution (cloud-side last-write-wins is the v1 conflict model — §8).

Per-subjob git-worktrees on a shared backend ([[subjob_worktree_sharing]]) are still relevant — they handle internal agent-subagent isolation and are unchanged by this design. They're not what the user sees.

## 7. Future Extensions (v2+, Opt-In)

Everything below ships *after* §4-§6 is solid in production and only for users / projects that explicitly want it.

### Branched sessions

Per-session branches in the project's Gitea repo, materialized as worktrees the agent works on. Same precedent as [[subjob_worktree_sharing]], generalized from subjobs to all sessions.

Useful for:
- Sessions on shared team projects where the user wants agent writes held back until reviewed.
- Experimental sessions that should not affect the canonical cloud folder until accepted.

### Review-mode sessions

Same as the job accept flow, but for a session: changes accumulate on a branch; user clicks "Promote to Project Folder" to land them. Live mode remains the default; review mode is a per-session setting.

### PR-style cross-team review

When a session/job in a multi-member project produces changes, allow another member to review/approve before merge. Builds on [[verification_phase]] (the critic primitive).

### Selective acceptance

In the job accept UI, allow "land these 3 of 5 files" instead of all-or-nothing. Cherry-pick semantics.

### Conflict resolution UI

Real three-way merge UI for the cases where last-write-wins isn't acceptable. Probably needed alongside review-mode sessions on shared projects.

None of these are required for v1. All build on §4-§6 without rearchitecting it.

## 8. Concurrency Model

**v1: last-write-wins, like Drive/Teams.**

If two sessions are both attached to Project A and both write the same file:

- Whichever writes back to the cloud last persists.
- The earlier write is overwritten without a merge attempt.
- If the cloud backend (OpenCloud, Nextcloud) keeps file versions, the overwritten content is recoverable through native version history.

This is the exact behaviour users get from Google Drive, OneDrive, and Nextcloud today. We do not invent stricter semantics for v1.

**What we add: a passive conflict signal.**

If a turn-start pull detects that a file the agent edited last turn was modified externally before our push, surface a small notification in the session UI ("This file was changed outside this session — your version overwrote the change. View history.") Linking the user to the cloud's native version history is enough.

**What we don't do in v1:**

- Locking.
- Per-file ownership.
- Optimistic concurrency control with retry.
- Three-way merge.

These belong with the v2+ branch model — when isolation is the user's explicit choice, conflict UX becomes worth the cost.

For jobs the question doesn't arise: jobs operate on isolated staging clones, so concurrent jobs on the same project never write the same file. Conflicts can only happen at *accept* time (the staged version conflicts with edits made to the project folder while the job ran). The accept UI handles this case explicitly — see §10.Q4.

## 9. Roadmap

Foundation first, then incremental additions. Each phase is independently shippable and delivers user-visible value. The ordering puts the simplest validation case before the asymmetric ones, and defers cleanup until the new code path has stabilized.

### Phase 1 — Single-project cloud mount (the keystone) — ✅ shipped 2026-05-17

The simplest case: a session attached to one regular (non-default) project mounts that project's cloud folder into the workspace at `projects/<name>/`. Bidirectional sync at turn boundaries, reusing the existing `cloud_sync` machinery retargeted from session folders to project folders.

Per-session cloud folder continues to be created in parallel for back-compat — Phase 4 narrows it to a fallback (only created when no mount exists).

- **Deliverable:** a session attached to project X has X's files in `projects/X/`; agent edits flow back to X's cloud folder. **✅ Live-verified on dev cluster 2026-05-17.**
- **Acceptance:** drop a file into project X via OpenCloud → agent reads it next turn. Agent writes a file → user sees it in OpenCloud. **✅ Both directions confirmed end-to-end** (project `Create chatbot for Sadur Süd`, OpenCloud Space, agent's `write_file` of marker text appeared in the user-visible cloud folder; user's marker file dropped beforehand was read by the agent on turn 1).
- **Risk:** sync collisions when a turn fires before the previous turn's sync completes. Mitigation: serialize sync per session.

#### Locked design decisions (planning pass 2026-05-17)

- **Schema: new `thread_mounts` table is the canonical store for project attachments.** Migration `0013_thread_mounts.sql` (transactional, `app/`). Fields: `id`, `thread_id` (FK → threads, `ON DELETE CASCADE`), `mount_kind` ('project' | 'project_default' | 'repo'), `target_path` (relative, e.g. `projects/alpha`), `source_kind` ('project_folder' | 'user_home'), `source_ref` (UUID → projects.id when applicable), `backend_id`, `cloud_handle`, `webdav_url`, `created_at`. Indexes on `thread_id`; unique constraint on `(thread_id, target_path)`. UUIDs use `uuid_generate_v4()` (codebase convention; `pgcrypto` is not loaded — only `uuid-ossp` per `0001_initial.sql:40`).

- **`metadata.project_ids` deprecated.** Phase 1 stops writing this JSONB key and removes its three readers (`orchestrator/main.py:9933`, `:9982`, `orchestrator/services/formatters.py:2419`) plus the writer at `orchestrator/main.py:10615`. **`thread_mounts` becomes the source of truth.** Runtime `project_ids: list[str]` continues to flow to downstream consumers (RAG scoping in `src/persistent_graph.py`, tool context in `src/tools/context.py`, knowledge tools in `src/tools/knowledge/knowledge_tools.py`, etc.) — but it is **derived from `thread_mounts` rows at payload-build time**, not read from metadata JSONB. The list-of-strings shape stays the contract for runtime; only the persistence layer changes. `visible_project_ids` (multi-tenancy access control) is a separate concept and is **not** affected by this deprecation.

- **Agent payload contract: `cloud_sync` bumped to `version: 2`.** `_build_agent_cloud_sync()` returns `{version: 2, session_folder: {...legacy single-folder cfg...}, mounts: [{mount_id, target_path, backend, webdav_url, auth}, ...]}`. v1 payloads (no `mounts` key) remain supported indefinitely — Phase 4's narrowed scope keeps the legacy session-folder path live as a fallback. Runtime consumers that still read `project_ids` will migrate to `mounts` opportunistically; the dual contract stays.

- **Integration: Option A — transport-level callbacks** (`_loop_on_turn_start` / `_loop_on_turn_complete` in `src/api/persistent_app.py`). `await coordinator.pull_all()` inside turn-start, `await coordinator.push_all()` inside turn-complete (replacing the fire-and-forget `asyncio.create_task` at `persistent_app.py:2319`). Session-start `start_background_poll()` call at `persistent_app.py:917` becomes a single blocking initial pull. **Zero changes to `src/persistent_graph.py`** — the callbacks pattern already awaits, blocking semantics emerge from the await.

- **Failure policy: raise-and-block.** A failed `pull_all()` or `push_all()` at a turn boundary raises and blocks the next turn from accepting input until resolved. Loud signal during early dogfood — broken backends surface fast instead of silently corrupting state. Reconsider for v2 after observed behaviour shows whether this is too aggressive in practice.

- **Cockpit hook: `mounts: [...]` field exposed on `GET /api/persistent/threads/{id}`.** Cockpit reads it to render a "Project files" panel showing which surfaces are mounted, with deep links to each in OpenCloud. UI design out of scope here; the event/field contracts are stable.

- **Sync coordinator:** new `src/services/cloud_sync/coordinator.py` holding a `WorkspaceSyncCoordinator` that wraps N `WorkspaceSyncBase` instances. `pull_all()` / `push_all()` run them via `asyncio.gather(..., return_exceptions=True)`. Per-mount error policy: aggregate exceptions, raise if any pull/push failed (per the failure policy above).

- **Coexistence with legacy per-session folder:** new project mount under `projects/<name>/`, legacy session folder stays at workspace root. `projects/` added to the legacy sync's `SYNC_IGNORE_PATTERNS` (`src/services/cloud_sync/base.py`) so the two don't shadow each other.

- **Estimated scope:** ~850 LoC across 4 new files + 5 modified, including unit tests in `tests/cloud_sync/` and a `LocalFsWorkspaceSync` test double so integration tests don't need a live WebDAV server.

#### Shipped (implementation pass 2026-05-17)

Landed under develop, deployed to the dev cluster, live-verified end-to-end:

- **New files:** `orchestrator/database/migrations/app/0013_thread_mounts.sql`, `src/services/cloud_sync/coordinator.py` (`WorkspaceSyncCoordinator` + `CloudSyncError`), `tests/cloud_sync/_local_fs.py` (`LocalFsWorkspaceSync` test double), `tests/cloud_sync/test_coordinator.py`, `tests/cloud_sync/test_payload_routing.py`.
- **Modified:** `orchestrator/database/postgres.py` (`add_thread_mount` / `list_thread_mounts` / `remove_thread_mount` / `replace_thread_mounts`), `orchestrator/main.py` (`_build_agent_cloud_sync` → v2 + mounts, `_thread_project_ids` w/ lazy backfill from legacy `metadata.project_ids`, `_build_thread_mount_rows`, `_slugify_mount_name`, GET `/api/persistent/threads/{id}` returns `mounts` + derived `project_ids`), `orchestrator/services/formatters.py`, `src/services/cloud_sync/__init__.py` + `base.py` (added `mount_subdir` + `strict` mode; `projects/` added to `SYNC_IGNORE_PATTERNS`), `src/services/cloud_sync/{opencloud,nextcloud}.py` (forwarding `mount_subdir`), `src/api/persistent_app.py` (`_build_sync_coordinator`, blocking `pull_all()` at session start replaces `start_background_poll()`, awaited `pull_all()` at turn-start and `push_all()` at turn-complete, no more background polling). Test fixture: `tests/test_mcp.py` updated to assert `project_ids` at top level rather than under metadata.
- **Lazy backfill:** `_thread_project_ids()` materializes missing `thread_mounts` rows on first read for pre-migration threads. Self-healing; designed to be removed one release after Phase 1.
- **Tests:** 45 cloud_sync tests pass (14 new + 31 existing). Broader suite unchanged. Lint clean.
- **Live verification on dev cluster (2026-05-17):**
  - Cloud → agent: marker file `phase1_test_input.md` dropped in OpenCloud was read by the agent on turn 1 and quoted back verbatim — confirms `pull_all()` at turn-start runs before the agent's first response.
  - Agent → cloud: agent's `write_file(projects/create_chatbot_for_sadur_süd/phase1_test_output.md)` with content `AGENT-WROTE-THIS-IS-PUSH-TEST-XYZ123` propagated to OpenCloud within ~30s of the turn ending — confirms `push_all()` at turn-complete fires.
  - `thread_mounts` row inspected via `GET /api/persistent/threads/{id}`: one row with `mount_kind=project`, `target_path=projects/create_chatbot_for_sadur_süd`, `source_kind=project_folder`, `source_ref=<project uuid>`, `backend_id=opencloud`. `metadata.project_ids` absent from the response — confirms the JSONB write is gone.
  - Slugifier preserved `ü` in `süd` — non-ASCII project names mount and sync correctly over OpenCloud Spaces and the SSH workspace path.
- **Known unrelated issue observed during testing:** mid-turn WebSocket disconnect (the `persistent_chat_silent_disconnect` issue from prior memory). A fresh session on the same project completed without recurrence; not introduced by Phase 1.

### Phase 2 — Default-project = user-home mount — ✅ shipped 2026-05-17 (superseded auth path by Phase 2.1)

The first asymmetric case. When the default project is attached, resolve via `get_user_home()` instead of `ensure_project_folder()` and mount at the workspace **root** instead of under `projects/`.

This is the case the entire investigation started from. It ships after Phase 1 because the generic mount mechanism needs to work first; only then does the user-home special case become a one-line wiring change.

- **Deliverable:** a session on the default project sees the user's home folder at workspace root.
- **Acceptance:** drop a file into the OpenCloud home root → agent reads it. Agent writes a file → user sees it in OpenCloud home.
- **Risk:** user home can be very large. May need a size cap or a top-level scope filter ("only sync these subfolders"). Permission scope is broad — agent has full read/write on the user's home; the UX needs to make this deliberate.

#### First implementation pass — broken (2026-05-17)

First Phase 2 pass landed (commit `b984968`). End-to-end live test failed: the agent received the `project_default` mount but `PROPFIND /dav/spaces/<personal-space>/` returned 404 from OpenCloud. Diagnosis via cluster logs (`srw-opencloud` access log + `authprovider.go` line showing `type:USER_TYPE_SERVICE authenticated`): **OpenCloud Personal Spaces are single-owner**, the agent's service-account bearer token has no WebDAV access to them. My earlier claim that "both backends authenticate as service accounts, so they can read/write any user's home Space" was wrong — service accounts get LibreGraph admin reads (which is why `get_user_home()` returned a valid Space ID) but NOT WebDAV data access on user-owned Spaces.

This invalidated the user-home mount approach as built. Phase 2.1 (below) replaces the auth path.

### Phase 2.1 — User-scoped tokens via Keycloak token-exchange (RFC 8693) — ✅ shipped + live-verified 2026-05-17

Replaces the broken service-account auth path. The agent obtains its own client_credentials token as before, then exchanges it for a **user-scoped** token impersonating the owner of the default project. WebDAV calls then run as the user, who naturally has access to their own Personal Space.

#### Keycloak prereq (manual admin step, one-time)

Token-exchange requires:

1. **Realm feature flag.** Already advertised in `grant_types_supported` on this cluster's `srw` realm — verified via `/realms/srw/.well-known/openid-configuration` against `srw-keycloak`. Nothing more to do at the realm level.
2. **Impersonation role on the agent's service-account user.** In the Keycloak admin console:
   - Realm `srw` → Clients → `<the client the agent uses to talk to OpenCloud>` → "Service accounts roles" tab
   - Assign realm-management role `impersonation`
   - Save

#### Manual verification recipe (run before agent code rollout)

After granting the role, verify the flow with curl. From inside the orchestrator pod (which has `httpx`):

```python
import httpx
ISSUER = "http://srw-keycloak:8080/realms/srw"
CLIENT_ID = "<srw oidc client>"
CLIENT_SECRET = "<from helm values / secret>"
TARGET_SUB = "<keycloak sub of a real user>"

# Step 1: service-account token
svc = httpx.post(
    f"{ISSUER}/protocol/openid-connect/token",
    data={"grant_type": "client_credentials", "client_id": CLIENT_ID,
          "client_secret": CLIENT_SECRET, "scope": "openid"},
).json()["access_token"]

# Step 2: exchange for user-scoped token
user = httpx.post(
    f"{ISSUER}/protocol/openid-connect/token",
    data={"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
          "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
          "subject_token": svc,
          "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
          "requested_subject": TARGET_SUB,
          "scope": "openid"},
).json()

# `user["access_token"]` should now decode (jwt.io) with `sub = TARGET_SUB`.
```

If Keycloak returns `403` with `Client not allowed to exchange`, the impersonation role isn't applied to the right service account. If it returns `400 invalid_request`, double-check the `requested_subject` value matches the user's Keycloak ID.

#### Implementation details

- **DB schema:** migration `0014_thread_mounts_target_user_sub.sql` adds nullable `target_user_sub TEXT` to `thread_mounts`. Only populated for `mount_kind='project_default'`. Phase 1 rows stay NULL and continue using service-account auth.
- **Orchestrator (`orchestrator/main.py`):** `_build_default_project_mount_row` looks up the project owner's `keycloak_sub` via `postgres_db.get_user(owner.user_id)`. If the owner hasn't completed an SSO login yet (no `keycloak_sub`), the function returns `None` and the caller falls back to the legacy session folder — same lenient policy as the rest of Phase 2.
- **Agent payload:** `_backend_cloud_cfg` emits a new `auth.type = "keycloak_user_impersonation"` shape carrying `issuer + client_id + client_secret + target_user_sub` when `target_user_sub` is set on the row. Other mounts keep the existing `keycloak_client_credentials` shape.
- **Agent sync (`src/services/cloud_sync/opencloud.py`):** `OpenCloudWorkspaceSync` accepts `target_user_sub`. `_get_token` becomes a two-step flow when set — `_fetch_service_token()` + `_exchange_for_user_token()`. The cached `_access_token` is the *exchanged* user-scoped token. 401-retry path forces a full re-fetch + re-exchange.
- **Factory (`src/services/cloud_sync/__init__.py`):** `build_workspace_sync` recognizes the new `keycloak_user_impersonation` auth type and routes `target_user_sub` to the OpenCloud sync constructor. A payload of that type without `target_user_sub` is rejected (returns `None` with a warning) rather than silently falling back to service-account mode — that fallback would re-introduce the 404 this whole phase is fixing.

#### Tests

- `tests/cloud_sync/test_opencloud.py` — 7 new cases covering impersonation: exchange request shape (verifies all RFC 8693 params), token caching across the two-step flow, refresh-after-expiry re-does both steps, 401-retry re-does the full chain, repr marks the mode, service-account mode unaffected.
- `tests/cloud_sync/test_factory.py` — 3 new cases: factory builds OpenCloud sync in impersonation mode, rejects impersonation payloads missing `target_user_sub`, client_credentials mode leaves `target_user_sub=None`.
- `tests/test_thread_mount_rows.py` — extended fixtures to include `user_id` on owner records and mock `postgres_db.get_user(...)` returning a `keycloak_sub`. New case asserts the row carries `target_user_sub`. New case covers "owner has no keycloak_sub → row not produced."

#### Phase 2 locked decisions (implementation pass 2026-05-17)

- **Row shape:** default-project attachment → `mount_kind='project_default'`, `source_kind='user_home'`, `target_path=''` (workspace root), `webdav_url` from `MainCloudBackend.get_user_home(resolve_user_identity(owner_email))`. ⚠️ **Corrected:** the original text here claimed both backends "can read/write any user's home Space without per-user share grants." That holds for **Nextcloud** (the backend authenticates as an *admin* over basic-auth, which can access any user's files) but is **false for OpenCloud** — its service account gets only LibreGraph admin *metadata* reads, not WebDAV *data* access to single-owner Personal Spaces, which is exactly why the Phase 2.1 token-exchange/impersonation exists (see the correction at `:320`).
- **Owner discovery:** `postgres_db.get_project_members(project_id)` → pick the first row with `role == 'owner'`; resolve `owner_email + owner_display_name.lower()` through `MainCloudBackend.resolve_user_identity()`. This mirrors the existing user-home URL resolution at `orchestrator/main.py:get_project`.
- **Session-folder collision policy: observable-state gate, not "default-project attached".** `_setup_main_cloud()` reads `thread_mounts` and skips `ensure_session_folder()` only when a `project_default` row with a non-null `webdav_url` is already present. If user-home resolution failed (transient backend hiccup, owner not yet provisioned, etc.) the legacy session folder is provisioned as a fallback so the thread never ends up with zero sync targets. This is the safer of the two policies I considered — strict gating on attachment would risk an empty workspace on a flaky backend.
- **`_project_ids_from_mounts()` accepts `project_default`** alongside `project`, so the derived `project_ids` list stays consistent for datasource resolution and visibility checks. A default project is still a project attachment for downstream purposes.
- **Lazy backfill continues to work:** the Phase 1 `_thread_project_ids()` backfill calls `_build_thread_mount_rows()`, which now handles defaults. Pre-Phase-2 threads with a default project in `metadata.project_ids` will materialize a `project_default` row on first access — no separate migration needed.
- **`SYNC_IGNORE_PATTERNS` already correct:** the default-project mount at root would otherwise upload `projects/`, `repos/`, `archive/`, `workspace.md`, `plan.md`, `todos.yaml`, `tools/` back into the user's home — but those patterns are already in the ignore list from Phase 1.

#### Shipped (Phase 2 implementation pass 2026-05-17)

- **Modified:** `orchestrator/main.py` — new `_build_default_project_mount_row()`; `_build_thread_mount_rows()` handles `is_default` projects instead of skipping; `_project_ids_from_mounts()` includes `project_default`; `_setup_main_cloud()` short-circuits session-folder provisioning when `project_default` mount is observable.
- **New tests:** `tests/test_thread_mount_rows.py` (6 cases — happy-path default-project row, no-owner / unresolvable-user-home / uninitialized-backend fallbacks, mixed default + non-default thread, `_project_ids_from_mounts` includes `project_default`); extended `tests/cloud_sync/test_payload_routing.py` with `test_v2_project_default_at_workspace_root`.
- **Tests:** all 6 new + 5 extended Phase 2 cases pass; 52-test cloud_sync suite green; 99-test thread/project/MCP suite green. Lint + format clean.
- **Live verification on dev cluster (2026-05-17, via Phase 2.1 auth path):**
  - Cloud → agent: file dropped into the user's OpenCloud Personal Space root appeared in the agent's workspace at root within ~30s of session start. Confirms `pull_all()` runs the impersonation token-exchange, hits the user's Personal Space (not the service account's empty home), and stages files at workspace root rather than under `projects/`.
  - Agent's WebDAV calls authenticated as the user, not the service account — verified via `srw-opencloud` access logs showing `user_id=<owner-keycloak-sub>` on PROPFIND.
  - `thread_mounts` row carried `mount_kind='project_default'`, `target_path=''`, `target_user_sub=<owner-sub>`, `webdav_url` resolving to the owner's home Space ID.

### Phase 2.2 — Cloud button URL synthesis for default-project threads — ✅ shipped + live-verified 2026-05-17

After Phase 2.1 fixed end-to-end file sync, Cockpit's session header was still missing the cloud-folder button for default-project threads. Root cause: `_resolve_cloud_session_url` only knew how to derive a URL from the **legacy** `nc_session_folder` / `main_cloud_session_handle` columns. Default-project threads have no session folder (correctly — Phase 2 short-circuits provisioning), so both fields are null and the helper returned `None`. The `cloud_session_url` field on the thread API response was therefore null, and Cockpit's `openSessionFiles(thread)` handler suppresses the button when the field is missing.

#### Fix (orchestrator-only, no Cockpit changes)

- **`orchestrator/main.py:_resolve_cloud_session_url`** now accepts an optional `mount_rows: list[dict]` argument. When the legacy session-folder fields are empty, it walks the rows looking for `mount_kind='project_default'` and synthesizes a URL from that row's `backend_id` + `cloud_handle` via `backend.get_project_folder_browser_url(ProjectFolderHandle.from_db(handle, backend=backend_id))`. Returns the first usable URL or `None` if nothing resolves.
- **`get_thread`** already loaded mounts via `_build_thread_mount_rows`; just threads them into the helper call.
- **`list_persistent_threads`** had to start fetching mount rows per thread (an N+1 vs. the legacy bulk query). Acceptable cost for the per-user thread list — the list is short and per-user, not per-tenant, and the lookup is a single indexed query on `thread_id`. If the list grows large enough to matter we can batch-fetch all rows in one query keyed on `thread_id IN (...)` later.

#### Tests

- 129 existing tests covering `cloud_session_url` resolution still green. No new tests added — the change is a passthrough fallback with no new state; the existing "thread has no cloud session URL" and "thread has a session folder → URL is folder browser URL" tests still apply, and the new path is exercised end-to-end by manual cluster verification.

#### Live verification on dev cluster (2026-05-17)

- `GET /api/persistent/threads/<thread-id>` for a default-project thread now returns a non-null `cloud_session_url` pointing to the user's OpenCloud Personal Space (Spaces UI route).
- Cockpit's cloud-folder button appears in the session header for default-project threads, and clicking it opens the user's Personal Space in a new tab.
- Legacy non-default-project threads (still using the session-folder code path) continue to return the session-folder URL as before — no regression.

### Phase 3 — Multi-surface mounting — ✅ shipped + live-verified 2026-05-17

A session can have multiple attached projects + repos. Default project (if attached) mounts at root; other projects under `projects/<name>/`; repos under `repos/<name>/`. Path-collision handling per §10.Q3.

Originally split into 3a (multi-project mount path collision fix) and 3b (repo mounts). 3a shipped today. **3b retired — repo attachment is already covered by the pre-existing `repository`-datasource flow** (see 3b section below). The doc earlier mis-framed repos as a future first-class `mount_kind`; the right framing is that they're first-class through a *different abstraction* (datasources), and that abstraction predates this design doc by enough to be invisible from inside it.

#### Phase 3a — Multi-project mount path collisions — ✅ shipped + live-verified 2026-05-17

`_build_thread_mount_rows` was iterating multiple project_ids correctly but emitting `target_path = f"projects/{slug}"` with no collision handling — two attached projects whose names slugify identically (including case-insensitive, since the slugifier lowercases) would both get the same `target_path` and trip `UNIQUE (thread_id, target_path)` at `replace_thread_mounts` persistence time. Multi-project attachment was therefore a latent crash on a real but probably-rare input.

- **Fix:** track a `used_paths: set[str]` across the row-building loop. On collision, suffix the candidate with `-2`, `-3`, ... until unique. Also dedupe input `project_ids` (same UUID appearing twice → one row, not a phantom suffix).
- **Default project unaffected:** `project_default` rows mount at `target_path=''` (workspace root); the non-default `projects/*` namespace doesn't intersect, so the default row's empty path doesn't push non-defaults around.
- **Modified:** `orchestrator/main.py:_build_thread_mount_rows` (+20/-1 LoC).
- **New tests:** `tests/test_thread_mount_rows.py` — 6 cases (two same-named → `alpha`+`alpha-2`; case-insensitive Alpha/alpha → same; three same-named → `alpha`/`alpha-2`/`alpha-3`; unique names unaffected; repeated id dedup; default + two colliding non-defaults).
- **Live verification on dev cluster:** exercised the deployed function (commit `4e038b7`, image `sha-4e038b7`) inside the orchestrator pod with synthetic project rows + mocked backend — all four expected behaviors confirmed (collision, case-insensitive, dedup, no-op for unique names). No real cluster data touched.

#### Phase 3b — Retired: repo attachment already shipped via `repository` datasources

This sub-phase as originally specified was fictional — it proposed building a new `mount_kind='repo'` row, a new `cloud_sync/gitea.py` transport, new attach-repo API endpoints, and a Cockpit repo picker. **All of that already exists, via the `repository`-type datasource abstraction.** The work shipped before this design doc was filed.

**How it actually works:**

- User-facing: Cockpit's session/job creation dialog has the datasource picker; user selects repos with `type='repository'`.
- Datasources attach via the existing project_datasources / `link_datasource_to_project` plumbing.
- Orchestrator delivers `repository` datasources in the agent's dispatch payload alongside other datasource types.
- Agent-side auto-clone:
  - `src/api/persistent_app.py:574-579` separates `type == "repository"` datasources from the rest at session start.
  - `src/api/persistent_app.py:770-772` clones them via `GitManager.clone()` against the **remote workspace backend** (not the agent pod's local FS — relevant for cluster-mode workspaces).
  - Per-repo clone logic: `src/core/datasource_setup.py:setup_repository_datasource` (line 308).
  - Worker-job equivalent: `src/agent.py:_setup_repository_datasource` (line 2182).
- Result: each attached repo at `repos/<slug>/` with `.git/` intact. **Real Git checkout** — agent runs branches, commits, pushes, opens PRs via existing shell tools. No new transport, no new mount type, no new orchestrator plumbing was needed.

**Schema note:** Migration `0013_thread_mounts.sql` accepts `mount_kind='repo'` and `source_kind='repo'`, but no code path writes such rows today. These enum values are **reserved but currently dead** — kept in the schema for cheap future-namespace, not because anything reads them. Don't propose to populate them without first checking whether the datasource path already covers the new use case.

**One genuinely-open question (deferred, not blocking Phase 3 done):** auto-fetch / auto-pull on attached repos at turn-start, so user-pushed commits upstream show up mid-session without the agent having to remember to `git fetch`. Today the agent pulls on demand. Could be added as opt-in if dogfood shows it matters. Not Phase 3b; not Phase 5; not even necessarily worth filing — log it as a TODO if a real user hits the pain.

#### Phase 3 acceptance — closed ✅

- **Deliverable:** a session with the default project + two other projects has both project surfaces mounted via `thread_mounts` rows at the documented paths (`projects/<name>/` with collision suffixing), plus any attached `repository` datasources auto-cloned into `repos/<slug>/`.
- **Live verification on dev cluster (2026-05-17):** 3a's `_build_thread_mount_rows` collision logic verified inside the deployed pod with synthetic project rows. The repository-datasource flow has been in production for months and was the path the user used to clone repos through the whole Phase 1-4 dogfooding effort.

### Phase 4 — Per-session cloud folder becomes fallback-only — ✅ shipped + live-verified 2026-05-17

**Revised 2026-05-17.** Earlier framing said "deprecate session folders entirely" — that was wrong. The session folder remains a valid surface; it just shouldn't be created when the session already has a user-visible cloud surface via Phase 1/2/3 mounts. This phase formalizes the *fallback* policy and removes the unconditional eager creation.

#### Why the framing changed

Not every session has a mount. Real cases that need a cloud surface but have none from mounts:

- A user starts a thread without attaching any project, and doesn't have a default project (legacy account, default not yet auto-provisioned).
- A user explicitly detaches their default project for throwaway work and doesn't attach anything else.
- Default-project user-home resolution fails (Keycloak hiccup, owner not yet SSO-onboarded, backend down at the moment of provisioning) — Phase 2's observable-state gate already routes to session folder in this case.

Without a session-folder fallback, those sessions end up with zero cloud surfaces and the agent's output is invisible to the user. So keep the fallback.

#### Policy

- **No mounts observable** → provision session folder. Identical to today's fallback path inside `_setup_main_cloud()`, just generalized.
- **Any mount observable** (Phase 1 `project`, Phase 2 `project_default`, or Phase 3 `repo`) → skip `ensure_session_folder()`. The mount(s) are the user-visible surface.
- **User-controlled override (future, opt-in):** a per-thread "always create a session folder for this thread" toggle in the new-thread dialog, for the throwaway-work case where the user wants a clean per-session surface even though a project is attached. Not blocking on Phase 4 — can land as a follow-up if real demand surfaces.

#### Code changes

- `_setup_main_cloud()` short-circuit gate (currently keyed on `project_default` only at `orchestrator/main.py:10978`) widens to: skip when *any* observable mount row has a non-null `webdav_url`. The fallback branch (provision session folder when nothing is observable) stays intact.
- No removal of `ensure_session_folder()` or the legacy `nc_session_folder` columns. Cleanup of *orphaned* old session folders is deferred to Phase 8.

#### Per-thread Gitea repo (separate concern)

The per-thread Gitea repo (`thread-<id>`) was previously paired with the session folder. Phase 7 repurposes it as a snapshot store independently. Whether we still provision it eagerly for every thread or only for sessions that want snapshots is a Phase 7 question, not a Phase 4 one. Phase 4 doesn't touch the Gitea side.

- **Deliverable:** new sessions skip the session folder when any mount exists; sessions with no mounts still get a fallback folder as today; existing session folders unchanged.
- **Acceptance:** start a session attached to a project → no session folder created. Start a session with nothing attached → session folder created exactly like today.

#### Shipped (implementation pass 2026-05-17, commit `7e6eb72`, image `sha-7e6eb72`)

- **New helper:** `_should_skip_session_folder(mounts) -> bool` in `orchestrator/main.py`. Mount-kind agnostic — any row with a non-empty `webdav_url` short-circuits. Returns `False` on empty mounts or rows with failed transports, preserving the fallback semantics for unattached sessions and transient resolution failures.
- **Two call-site rewires:**
  - `_setup_main_cloud()` (create-time gate): previously hard-coded to `project_default + webdav_url`; now calls the helper. Threads attached to any non-default project now also skip the redundant session folder.
  - `resume_thread`'s `needs_full_provision` calculation: also gated on the helper now. Closes a hole where a default-project thread that was ended and resumed grew a session folder on resume that it hadn't had at create-time. The `needs_share_only` branch is untouched — it only fires on existing folders, so it remains the recovery path for legacy folders that lost their share record.
- **Tests:** 6 new cases in `tests/test_thread_mount_rows.py` covering each `mount_kind` × `webdav_url-present|absent` combo plus mixed-row short-circuit and empty-list fallback. Full suite: 174/174 pass.
- **Live verification on dev cluster:**
  - Predicate-level (2026-05-17): exercised the deployed helper inside the orchestrator pod with synthetic mount-row inputs — all 7 cases (including forward-compat `repo` rows) matched expected behavior. Both call sites confirmed wired via grep on `/app/main.py`.
  - End-to-end (2026-05-18): user confirmed via Cockpit dogfood — a session attached to a non-default project receives the project's cloud folder mounted in the workspace and **no redundant `Sessions/<thread-id>/` folder is created in OpenCloud**. The fallback path still fires for unattached sessions (unchanged from pre-Phase-4 behavior).

### Phase 5 — Job staging clone — superseded 2026-05-18

Original framing: clone attached cloud surfaces into a hidden per-user drafts area (`/Drafts/jobs/<id>/`) at job-start; job workspace mounts staging instead of live project folders.

**Superseded by a simpler approach:** a "export job results to cloud" button on the job review component (no live mounting, no per-turn sync — explicit user action puts job outputs into `<project>/job-<id>/` or the user's home Space for default-project jobs). Design lives in `docs/done/job_cloud_export.md`. The full staging-clone vision is preserved there as a deferred follow-up.

### Phase 6 — Job accept UI + endpoint — deferred 2026-05-18

Diff view + `POST /api/jobs/{id}/accept` + per-file conflict UI. Deferred along with Phase 5 — listed in `docs/done/job_cloud_export.md` as the "v2" extension after the export button lands.

### Phase 7 — Per-turn snapshots + session timeline — deferred (still relevant)

Genuinely deferred — not retired, not superseded. The Gitea per-thread repo plumbing exists (`thread-<id>`); the missing pieces are the per-sync commit hook, the timeline UI, and the rollback verb.

- **Plan:** per-thread Gitea repo commits workspace state after each sync-back. Cockpit's session UI gains a timeline showing what changed between turns. Rollback verb: "revert this session to turn N" restores files from snapshot into the cloud folders.
- **Deliverable:** a session has a visible turn-by-turn history; user can roll back to any turn.
- **Acceptance:** make an agent edit; roll back; verify the cloud folder reverted.
- **Status:** no doc yet. File `docs/features/session_snapshots.md` when picked up.

### Phase 8 — Cleanup — deferred (small)

**Scope narrowed 2026-05-17.** Since Phase 4 keeps the session folder as a fallback (rather than removing it), this phase no longer kills the code path — it just sweeps orphaned cloud folders that the old eager-creation policy left behind.

- **One-time archive sweep:** identify session folders for closed threads that no longer have a live thread referencing them; offer to archive them into the user's drafts area (or leave them in place behind a "show archived sessions" filter). Decision deferred to when we actually run the sweep — it depends on how many orphans exist by then.
- **No code-path removal:** `ensure_session_folder()` and the `nc_session_folder` columns stay. They remain the fallback for unattached sessions and the recovery path for transient mount-resolution failures (Phase 2 / Phase 4).
- **Deliverable:** orphaned session folders from pre-Phase-1 eager creation are catalogued and dispositioned. The active code path is unchanged.
- **Status:** small enough to be a scripted one-off; doesn't need its own design doc.

### Phase 9+ — V2 extensions (deferred)

See §7. None block Phases 1-8; ship when v1 is stable and there's demand.

## 10. Open Questions

Refocused on the foundation. The pre-rewrite questions about branch placement and Git-canonical semantics are resolved by §2 and §6 and have been dropped.

### Q1. Sync granularity — turn-boundary vs intra-turn

- **Turn-boundary (leaning):** push at turn end, pull at turn start. Simple, predictable, single sync per turn.
- **Intra-turn push during long turns:** if a turn takes 20 minutes (browser research, multi-step tool use), users want to see partial progress in their cloud folder. Could push every N seconds during a turn, or on file-write tool calls specifically.

**Recommendation:** turn-boundary for v1. Add intra-turn push only if user feedback shows it's painful.

### Q2-Q5 — Migrated to job_cloud_export.md

Job-flavored questions (staging clone location, accept-time conflicts, per-job retention) moved to `docs/done/job_cloud_export.md` as pre-considered context for that doc's design.

### Q3. Mount path collisions — ✅ resolved by Phase 3a

Disambiguate at mount time with numeric suffixes (`projects/foo/`, `projects/foo-2/`). Implemented in `_build_thread_mount_rows`; case-insensitive (slugifier lowercases).

### Q6. Snapshot retention

How aggressive is pruning? Per §6, "keep last 100 turns then squash" is a starting point. Open whether to expose retention as a per-project setting.

**Recommendation:** fixed at 100 turns for v1; revisit if users complain about losing rollback granularity.

### Q7. Multi-tenancy interaction

The §4 mount rule is per-session, and session ↔ project membership is governed by [[multi_tenancy]]. Open question: when a user is removed from a project mid-session, do we unmount the project mid-session (interrupts agent), or let the session finish and only block future sessions?

**Recommendation:** let the current session finish; block future. Revisit if there's a compliance reason to be stricter.

## 11. Out of Scope (for this doc)

- Concrete API design and endpoint shapes (deferred to Phase 1-2 implementation docs).
- Schema changes (likely small: a `mount_kind` column on `thread_attachments` or similar, deferred).
- Multi-cloud routing beyond what [[main_cloud_abstraction]] already provides.
- Synchronization between the project folder and *external* clouds the user attached as plain datasources (e.g., a personal Google Drive) — those stay as datasources, not mounts.
- Granular per-file permissions inside a project folder.
- All of §7. (That's a separate concept-stage doc to write when v1 lands.)

## 12. Related Work

### Direct dependencies

- [[main_cloud_abstraction]] — the `MainCloudBackend` Protocol this design runs on (no changes needed).
- [[project_cloud_folders]] — current per-project + per-session split; this doc deprecates the per-session half.
- [[sso_and_cloud_storage]] — Keycloak ↔ cloud user provisioning (unchanged).

### Superseded

- [[session_folder_placement]] (2026-04-23) — asked "should session folders be project-bound, per-user, agent-owned, or hybrid?" Answer per §4: **none of the above — there is no separate session folder. Sessions mirror attached project folders into the workspace directly.** That doc should be marked superseded and pointed at this one.

### Precedent / reused primitives

- [[subjob_worktree_sharing]] (shipped) — proves the git-worktree-on-shared-backend pattern at production scale. The v2+ branch layer (§7) generalizes this from subjobs to all sessions.
- [[verification_phase]] — the critic-driven review primitive that the v1 job-accept UI (§5) and v2+ cross-team review (§7) build on.

### Adjacent

- [[projects]] — project entity & lifecycle (the unit this design is anchored on).
- [[sessions]] — persistent agent session model (the consumer of the mount model).
- [[ephemeral_workspaces]] — workspace lifecycle (the agent runs on an isolated workspace; this doc says what's *mounted into* that workspace).
- [[workspace_simplification]] — broader workspace reduction effort; this is a piece of it.
- [[multi_tenancy]] — access control governing which projects a session can mount.
- [[persistent_thread_lifecycle]] — session lifecycle (suspend/resume); mounts are torn down on session end, recomposed on resume.
