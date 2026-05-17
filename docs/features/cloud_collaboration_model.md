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

**Status:** Concept. Filed 2026-05-16.
**Filed:** 2026-05-16
**Last updated:** 2026-05-16
**Depends on:** [[project_cloud_folders]] (current per-project + per-session folder lifecycle), [[main_cloud_abstraction]] (`MainCloudBackend` Protocol + OpenCloud default).

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
| Workspace files API (`cloud_*` tools) | `src/tools/cloud/` — vendor-neutral WebDAV | **Yes** — no changes needed |
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

### Phase 1 — Single-project cloud mount (the keystone)

The simplest case: a session attached to one regular (non-default) project mounts that project's cloud folder into the workspace at `projects/<name>/`. Bidirectional sync at turn boundaries, reusing the existing `cloud_sync` machinery retargeted from session folders to project folders.

Per-session cloud folder continues to be created in parallel for back-compat — Phase 4 deprecates it.

- **Deliverable:** a session attached to project X has X's files in `projects/X/`; agent edits flow back to X's cloud folder.
- **Acceptance:** drop a file into project X via OpenCloud → agent reads it next turn. Agent writes a file → user sees it in OpenCloud.
- **Risk:** sync collisions when a turn fires before the previous turn's sync completes. Mitigation: serialize sync per session.

### Phase 2 — Default-project = user-home mount

The first asymmetric case. When the default project is attached, resolve via `get_user_home()` instead of `ensure_project_folder()` and mount at the workspace **root** instead of under `projects/`.

This is the case the entire investigation started from. It ships after Phase 1 because the generic mount mechanism needs to work first; only then does the user-home special case become a one-line wiring change.

- **Deliverable:** a session on the default project sees the user's home folder at workspace root.
- **Acceptance:** drop a file into the OpenCloud home root → agent reads it. Agent writes a file → user sees it in OpenCloud home.
- **Risk:** user home can be very large. May need a size cap or a top-level scope filter ("only sync these subfolders"). Permission scope is broad — agent has full read/write on the user's home; the UX needs to make this deliberate.

### Phase 3 — Multi-surface mounting

A session can have multiple attached projects + repos. Default project (if attached) mounts at root; other projects under `projects/<name>/`; repos under `repos/<name>/`. Path-collision handling per §10.Q3.

- **Deliverable:** a session with the default project + two other projects + a repo has all four mounted correctly and visible to the agent.
- **Acceptance:** end-to-end test with four attachments; mount paths match the documented rule; agent can read/write each surface.

### Phase 4 — Per-session cloud folder deprecation

New sessions no longer call `ensure_session_folder()`. The per-thread Gitea repo (`thread-<id>`) is repurposed as a snapshot store (Phase 7); it no longer carries the working files.

Existing session folders remain readable during a grace period (~3 months) so in-flight users aren't disrupted.

- **Deliverable:** new sessions create zero per-session cloud folders; old sessions still load.
- **Risk:** anyone with bookmarks into old session folders. Acceptable cost.

### Phase 5 — Job staging clone

At job-start, clone attached cloud surfaces into a hidden per-user drafts area (per §10.Q2: e.g., `/Drafts/jobs/<id>/`). The job's workspace mounts the staging area, not the live project folders. Job writes go to staging.

- **Deliverable:** a job runs without touching the live project folder.
- **Acceptance:** start a job; while it runs, project folder is unchanged. Job completes; staging has the job's outputs.

### Phase 6 — Job accept UI + endpoint

Cockpit's job-review tab gains a "Files Changed" view showing staged-vs-baseline diff. Accept → orchestrator applies the staged changes to project folders. Reject → discard staging. 30-day retention on un-acted-on staging clones (§10.Q5).

New endpoint `POST /api/jobs/{id}/accept` (sibling to existing `/promote`, which retains its current "move to new project" verb).

- **Deliverable:** end-to-end "review a job → click accept → files appear in project folder."
- **Acceptance:** accept and reject both work. Per-file conflict UI (§10.Q4) handles cases where the project folder changed while the job ran.

### Phase 7 — Per-turn snapshots + session timeline

The repurposed per-thread Gitea repo (Phase 4) commits workspace state after each sync-back. Cockpit's session UI gains a timeline showing what changed between turns. Rollback verb: "revert this session to turn N" restores files from snapshot into the cloud folders.

- **Deliverable:** a session has a visible turn-by-turn history; user can roll back to any turn.
- **Acceptance:** make an agent edit; roll back; verify the cloud folder reverted.

### Phase 8 — Cleanup

Remove deprecated per-session cloud folder code paths entirely (post grace period). One-time migration script archives surviving old session folders into the user's drafts area for retention.

- **Deliverable:** single code path; the parallel session-folder lifecycle is gone.

### Phase 9+ — V2 extensions (deferred)

See §7. None block Phases 1-8; ship when v1 is stable and there's demand.

## 10. Open Questions

Refocused on the foundation. The pre-rewrite questions about branch placement and Git-canonical semantics are resolved by §2 and §6 and have been dropped.

### Q1. Sync granularity — turn-boundary vs intra-turn

- **Turn-boundary (leaning):** push at turn end, pull at turn start. Simple, predictable, single sync per turn.
- **Intra-turn push during long turns:** if a turn takes 20 minutes (browser research, multi-step tool use), users want to see partial progress in their cloud folder. Could push every N seconds during a turn, or on file-write tool calls specifically.

**Recommendation:** turn-boundary for v1. Add intra-turn push only if user feedback shows it's painful.

### Q2. Job staging clone location

- **In-folder under `.srw/jobs/<id>/`:** visible to the user in their cloud listing, ugly.
- **Per-user hidden drafts area (leaning):** `/Drafts/jobs/<id>/` or similar at the cloud root; not in any project folder. User can browse if they want, but it's not in the way.
- **Cloud-side temporary space outside the user's view:** cleanest but requires admin-owned drives or similar; cross-cloud-backend uncertainty.

**Recommendation:** per-user hidden drafts area. Re-evaluate if a backend can't support it.

### Q3. Mount path collisions

What if an attached project is named `repos`, or two attached projects share a name? Or a project shares a name with an attached repo?

**Recommendation:** disambiguate at mount time (append a numeric suffix: `projects/foo/`, `projects/foo-2/`). Log the renaming so the agent's prompt context shows the actual mount paths. The agent never assumes; it reads its own workspace.

### Q4. Job accept-time conflicts

If a job ran while the user was editing the same file in the project folder, the staged version conflicts with the live version at accept time.

- Show a per-file conflict indicator in the accept UI.
- v1: offer "keep mine," "keep theirs," "view both side-by-side." Don't auto-merge.

**Recommendation:** per-file three-state choice in the accept UI. Real merge tooling deferred.

### Q5. Per-job staging clone retention

- Default 30-day retention on staging clones that are neither accepted nor rejected; auto-archive after.
- Open: where do archived clones go? Tag + delete the working copy? Keep indefinitely behind a "show archived jobs" filter?

**Recommendation:** delete after 30 days; one configurable knob.

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
