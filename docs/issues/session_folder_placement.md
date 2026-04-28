# Session folder placement: project-bound vs cross-project — 2026-04-23

## Context

Session folders (per-persistent-thread folders in the main cloud) currently
live in the orchestrator's `srw-agent-home` Space at
`sessions/{thread_id[:8]}`, regardless of which project(s) the session is
scoped to. They are shared with the session owner via a LibreGraph invite.

Meanwhile, the app already distinguishes projects at multiple layers:

- **DB (schema.sql:640)**: `threads.project_id UUID REFERENCES projects` —
  singular FK, nullable. One thread ↔ one project.
- **Agent runtime (`src/api/persistent_session.py:89`)**: `project_ids:
  List[str]` — the session can be scoped to multiple projects at runtime
  (knowledge base, RecallStore, tool context all accept the list).
- **Cloud (`orchestrator/main.py:12833` `_ensure_project_cloud_resources`)**:
  each project gets its own Space (`main_cloud_folder_handle` on the
  `projects` row) — a drive separate from `srw-agent-home`.

So there is already tension: the DB says "one project per thread", the agent
runtime says "many projects per session", and the cloud layout is currently
agnostic to both (everything in one shared Space).

This surfaced during the OpenCloud share-subfolder investigation (see
adjacent `opencloud_share_bugs.md` — or the sibling fix PR): the share role
and item-lookup logic was written assuming session folders are subfolders of
a project-like Space, but the actual placement is flat.

## Question

Should session folders be:

1. **Project-bound** — live inside the project's Space, inheriting that
   Space's ACLs; session folder is just `{project_space}/sessions/{thread}`.
2. **Global per-user** — live in the user's personal Space (the "Test User"
   drive in my OpenCloud dump), not in any project Space.
3. **Global agent-owned (status quo)** — all session folders in
   `srw-agent-home/sessions/`, explicitly shared per-session with the owner.
4. **Hybrid** — project-bound by default; support an explicit
   "cross-project / scratch" session mode that falls back to a global or
   per-user Space.

## Trade-offs

### Option 1: Project-bound

**Pros:**
- ACL model "just works": project members already see the Space; new team
  sessions are visible without per-session invites.
- Natural hierarchy in the Files UI: `Project X → sessions → thread-abc`.
- No extra share call per session — fewer API round-trips, no invite race.

**Cons:**
- Forces a 1:1 thread ↔ project commitment at folder-creation time. The
  runtime's current `project_ids: List[str]` can't be honored — the folder
  lives in exactly one Space.
- A session that later broadens scope (user adds a second project
  mid-thread) has a stale folder location; moving WebDAV folders
  cross-drive is not atomic.
- Cross-project / scratch sessions (no project selected) have nowhere to
  go without a fallback.

### Option 2: Global per-user

**Pros:**
- Single owner = single ACL, no sharing needed — the user sees it in
  their own "Home" drive by default.
- Cross-project sessions work naturally.
- Shareable with collaborators later via normal OpenCloud invites.

**Cons:**
- No project grouping in the UI — 200 sessions all flatten under `Home →
  sessions/`.
- Project teammates don't auto-see each other's session folders; the
  orchestrator would still need to share on behalf of project membership.
- Breaks the current "agent curates content, user receives it" mental
  model — the agent writes into user-owned storage.

### Option 3: Global agent-owned (status quo, once the bugs are fixed)

**Pros:**
- Simplest: one Space, one admin, known placement.
- Agent owns the lifecycle (GC, quota) centrally.
- Matches the mental model: the agent "produces" session data, then
  shares it with consumers (primarily the session owner, optionally
  project teammates).

**Cons:**
- Every session needs an explicit invite per user — races on first login
  (what we just hit), extra API calls, no inherited project ACL.
- Folder name `sessions/b3f9eb0b` is opaque in the UI — no project
  context.
- Sharing with project teammates requires enumerating project members
  and inviting each; no group-based shortcut.

### Option 4: Hybrid

**Pros:**
- Most-common case (thread tied to one project) gets the cheap ACL-inherit
  path.
- Cross-project / scratch sessions still possible via the global fallback.
- Can be rolled out incrementally — default to hybrid on new threads,
  leave existing threads on the global path.

**Cons:**
- Two code paths in `_setup_main_cloud` — more to test, more error modes.
- UX: user has to learn that "some of my sessions live under Project X,
  others under Home/scratch" — naming/discovery complicates.
- DB: `threads.project_id` stays singular but agent runtime still accepts
  `project_ids: List[str]` — the mismatch doesn't go away, we just pick
  the first one for folder placement.

## Open questions (for design)

1. **Multi-project at the data model**: Do we resolve `threads.project_id`
   (singular) vs `project_ids: List[str]` (list) by making the DB
   multi-valued (join table), keeping singular + runtime-only list, or
   dropping multi-project at the session level entirely? This decision
   gates options 1 and 4.
2. **Sharing with project teammates**: Today the folder is only shared
   with the owner. Should a project-bound session folder also be visible
   to all project members? If yes, we need a "share with project group"
   primitive — likely a Keycloak group or a LibreGraph group mapped from
   `project_members`.
3. **GC and quota**: If folders move from one central Space to per-project
   Spaces, quota accounting and cleanup (orphaned folders, deleted
   projects) shifts too. Worth sketching before we pick a layout.
4. **Migration**: Existing session folders (all under `srw-agent-home`)
   would either need to stay (status quo for old, new policy for new) or
   be moved. Moving is disruptive; staying fragments the layout.
5. **Does the agent write into the cloud folder directly, or does
   everything go through the workspace + a publish step?** Today the
   agent's workspace is SSH/SFTP on a container; cloud sync is… unclear.
   If the agent writes directly, the folder placement matters more; if
   it's a post-hoc publish, the cloud layout is mostly cosmetic and
   option 2 or 3 is fine.

## Recommendation (tentative — to discuss)

Lean **Option 4 (hybrid)** with **Option 1 (project-bound) as the default**
once the DB multi-project question is resolved. Rationale:

- The majority case — user starts a thread inside a project — gets the
  cheap ACL-inherit path, solves the share race, matches the UI mental
  model.
- Scratch / cross-project sessions stay possible via a fallback to the
  global `srw-agent-home` Space (or per-user Home, depending on how Q2
  and Q5 land).
- Single-code-path simplicity of Option 3 is appealing but we've already
  seen that the "share on every session" path is fragile and the folder
  layout is opaque in the UI.

That said, the current investigation surfaced that the existing global-
agent-owned path has multiple latent bugs (see `opencloud_share_bugs.md`).
Even if we don't pick Option 3 as the long-term layout, fixing the
immediate bugs is a prerequisite for Option 4's fallback branch. So:

## What blocks what

- **Immediate (does not require this decision)**: fix the two OpenCloud
  share bugs (role weight, `_resolve_item_id` path) so status quo works.
- **Near-term (requires decision on Q1)**: if we go multi-project, add a
  join table; if we stay singular, delete or deprecate `project_ids:
  List[str]` in the runtime.
- **Medium-term (requires Q2, Q3)**: design `ensure_session_folder`'s new
  placement logic and the project-member sharing primitive.
- **Deferred**: migration of existing folders — we can defer by
  grandfathering old threads to the current layout.

---

Write-up for discussion, not a decision. No code changes from this doc.
