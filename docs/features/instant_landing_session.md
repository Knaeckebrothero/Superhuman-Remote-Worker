# Instant Landing Session ("type first, provision on send")

**Status**: ✅ SHIPPED — committed to develop as `e374ec07` (2026-07-12) after
k3d live verification. S3 assumed present platform-wide
(`docs/done/s3_object_store_bundled_fallback.md`); platform default = virtual.
See "As built" + "Updates since ship" at the bottom. NB: statements below that
`vm` is rejected at session create are **superseded** by
`docs/features/session_create_on_vm.md` (`fd0525e9`, 2026-07-16) — see the
updates section.
**Scope**: orchestrator (default-settings chain) + cockpit (draft mode + settings UI)
**Related**: `docs/features/no_workspace_agent_mode.md` (virtual/lite tiers),
`docs/features/builder_to_sessions_consolidation.md` (why `/` currently
redirects to the sessions list), workspace tier upgrade spec
(`workspace_tier_upgrade.md`).

## Problem

Opening `srw.works` lands on the sessions list (`'' → redirectTo: 'sessions'`,
`cockpit/src/app/app.routes.ts:33`) — an empty-state card with nowhere to
type. Starting a conversation takes three steps: **New Session** → full
options form → create → wait. Since the builder was removed, the product has
no zero-friction entry point, even though the platform can start a
virtual-backend session in a few seconds via the warm agent pool.

## Goal

Two things, and the second is the foundation for the first:

1. **Default session settings are a first-class, user-configurable concept.**
   Every setting an instant session needs — model, permission mode, and now
   **workspace backend** — resolves through the existing chain: system
   default → user's saved default → per-request override. A user who wants
   every session to start on a container workspace sets that once in
   Settings; a user who is fine with virtual (most) never thinks about it.
2. **Opening the app root lands in a fresh, open chat.** Greeting + enabled
   composer; nothing is created until the user sends. On first send the
   cockpit creates a session with a *minimal* body — no settings hardcoded
   client-side — and the server applies the user's defaults. The message
   rides the existing outbox into the new thread. Sessions that later need
   shell/git use the existing in-place upgrade (backend hot-swap,
   conversation preserved).

## What already exists (verified 2026-07-09/11)

- **Defaults chain (everything except backend)**:
  `GET /api/settings/preferences` returns user prefs plus `_resolved`
  system defaults, including `persistent_agent: {model, permission_mode,
  idle_timeout_minutes, config_name}` (`_resolve_preference_defaults`,
  `orchestrator/main.py:23996`). The Settings UI shows resolved values as
  placeholders and PATCHes overrides into `users.settings.persistent_agent`
  (`UserSettingsUpdate.persistent_agent`, free-form dict, `main.py:5569`).
  `create_thread` merges the saved defaults (model, permission_mode,
  greeting, idle_timeout, command_allowlist, headless_*) into
  `config_override`, request fields winning (`main.py:17624-17681`).
- **Backend is the odd one out**: it only arrives per-request via
  `config_override.workspace.backend` ∈ `sandbox|virtual|none` (`vm`
  rejected at create — start lite, upgrade;
  `_validated_session_workspace_override`, `main.py:2806-2839`). **Omitted →
  sandbox container**, because the provisioning fork keys off
  `_backend_from_override(config_override)`.
- **Virtual = lite = fast**: `backend: virtual` skips workspace-pod
  provisioning entirely and binds pool-first to a warm idle agent
  (`provision_or_assign.py`). Requires the deployment to have an object
  store configured (`VIRTUAL_WORKSPACE_RCLONE_TYPE` env + creds); if absent,
  `_virtual_workspace_rclone_spec()` returns `None` and attach fails with
  `LiteWorkspaceConfigError` — creation is accepted, the session dies later.
- **Deferred-creation UI flow**: the create form navigates to
  `/sessions/_creating` with the body in `history.state`;
  `ChatPageComponent` calls `PersistentChatService.createAndConnect(body)`
  (`persistent-chat.service.ts:712`), which POSTs, connects with
  `{carryOutbox: true}`, and swaps the URL. The composer is already enabled
  while starting; early messages queue in the outbox and flush on
  `markSessionReady()`. Grant enforcement at create
  (`_enforce_session_create_grants`) already validates permission_mode /
  model / workspace.backend / tools.

## Design

### Part A — workspace backend joins the default-settings chain

**A1. System default.** `_resolve_preference_defaults().persistent_agent`
gains `workspace_backend: "virtual"`. An S3-compatible object store is an
assumed platform prerequisite (decision 2026-07-12) — no availability
probing, no fallback logic; store-less installs are addressed once at the
deployment layer by `docs/done/s3_object_store_bundled_fallback.md`. A
store-less install that ignores that still fails fast with the actionable
`LiteWorkspaceConfigError` message at attach.

**A2. User override.** `settings.persistent_agent.workspace_backend`
(values `sandbox|virtual|none`; `vm` invalid, same rule as per-request).
Settings UI: a "Default workspace" selector in the existing persistent-agent
section, styled like the permission-mode dropdown, showing the resolved
system default when unset:
- *Virtual* — starts in seconds; files in cloud storage; no shell. Can
  upgrade to a container mid-session.
- *Container (sandbox)* — full shell/git from the start; ~40 s to provision.
- *None* — chat only, no files.

**A3. Create-time merge.** In `create_thread`'s user-settings block: if the
user has `workspace_backend` (else the resolved system default), seed
`config_override.workspace.backend` — *before* the request-override section,
so the New Session form's explicit Backend selector still wins unchanged.
The existing grant enforcement then validates the effective backend as it
already does for explicit ones. Net effect: **no session is implicitly
sandbox anymore; an omitted backend means "the user's default"** — this also
fixes the quiet inconsistency where leaving the form's selector untouched
meant sandbox regardless of what the user would have picked.

Note: this changes behavior for *all* creators that omit a backend (REST,
MCP `create_persistent_thread`) — they get the owner's default instead of
sandbox. That is the point, but it ships as its own commit so it can be
observed in isolation.

### Part B — the draft landing page

**B1. Root route.** `'' → ChatPageComponent` (authGuard,
`data: {draft: true}`) replacing the redirect. `/sessions` (list),
`/sessions/new` (full form), `/sessions/:threadId` unchanged; the `**`
wildcard now lands on the draft chat. Always a fresh draft — no auto-resume
(past sessions are one click away).

**B2. Draft mode.** `PersistentChatService.enterDraft()` resets view state
and sets an `isDraft` signal; entering draft detaches a still-connected
session the same way switching threads does (server-side session survives).
`canCompose` additionally true in draft. Draft empty state: centered
greeting above the existing composer + an "Advanced options" link to
`/sessions/new`. Minimal CSS; style budget flat.

**B3. First send.** Push the message to the outbox (as `sendMessage` already
does), then `createAndConnect` with the minimal body:

```jsonc
{
  "title": "<first message, single line, ~60 chars>",
  "project_ids": ["<user's default project>"]   // if one exists, fetched non-blocking
}
```

No model, no permission mode, no backend — the server's defaults chain
(Part A) supplies them. Navigate to `/sessions/{id}` on success; back button
returns to a fresh draft. Attachments stay disabled until connected
(unchanged).

### Out of scope (follow-ups)

- LLM auto-titling (truncated first message is v1).
- Suggestion chips / recent-sessions rail on the draft page.
- Per-user default expert/config selection UI (the `config_name` slot
  already exists in the chain; UI is a separate discussion).
- Surfacing the mid-session "upgrade to workspace" affordance (exists
  server-side; cockpit chip is the workspace-tier-upgrade project).
- Dead inline `showCreate` dialog cleanup in `sessions-page.component.ts`.

## Acceptance criteria

1. Settings shows a "Default workspace" selector under the session-defaults
   section; unset displays the resolved system default (virtual).
2. `POST /api/persistent/threads` with no workspace override provisions the
   owner's default backend; the New Session form's explicit selector still
   wins; `vm` still rejected at create *(criterion relaxed 2026-07-16:
   explicit `vm` is now creatable — see "Updates since ship"; the invariant
   that survives is that `vm` can never be an implicit or saved default)*.
3. Opening `/` while authenticated shows a greeting + enabled composer, zero
   clicks; landing there creates nothing server-side.
4. Typing and sending creates a session with the user's defaults; on a
   deployment with virtual + warm pool the agent responds within seconds;
   the first message is never lost.
5. A user who sets Container as their default workspace gets a sandbox
   session from the landing flow (slower; startup card shows progress).
6. `/sessions`, `/sessions/new`, `/sessions/:id`, deep links unchanged;
   entering `/` never kills a connected session server-side.
7. Works in the mobile layout (responsive CSS only).

## Implementation slices

- **S1 orchestrator — defaults chain** (~0.5 d + tests): A1 resolved
  default, A2 accept/validate `workspace_backend` in
  `persistent_agent` settings, A3 create-time merge. Own commit.
- **S2 cockpit — settings UI** (~0.5 d + vitest): Default-workspace selector
  in Settings.
- **S3 cockpit — draft landing** (~1 d + vitest): route, `enterDraft()`,
  draft send → `createAndConnect`, hero, "Advanced options" link,
  sessions-list empty-state CTA → `/`.
- **Verify** (k3d): settings selector round-trip; `/` → type → send → live
  virtual session in seconds; user default = sandbox honored; explicit form
  selector still wins; `/sessions/*` regression pass; mobile viewport.

## Resolved questions

- Backend selection is a **user default, not a cockpit hardcode** (2026-07-11)
  — the landing flow sends a minimal body; the server resolves defaults.
  Users can configure "always container" in Settings.
- **S3 store is an assumed platform prerequisite** (2026-07-12) — no
  capability flag, no fallback logic; bundled-store fallback parked in
  `docs/done/s3_object_store_bundled_fallback.md`.
- **Platform default = `virtual`** — most sessions don't need shell tools;
  container is one Settings change or one in-place upgrade away.
- **Default project** — instant sessions attach the user's default project
  when one exists (form parity, KB routing).
- **Draft entry while a session is connected** — always a fresh draft;
  the connected session stays alive server-side and resumable from the list.

## As built (2026-07-12)

All three slices implemented as designed; deltas and findings:

- **Pre-existing bug found & fixed**: `create_thread`'s user-defaults merge
  read `user.get("settings")`, but no auth path ever selects the `settings`
  column (`get_user_by_keycloak_sub` returns identity/admission fields only)
  — so every saved persistent_agent default (model, permission_mode, …) was
  silently dead at create time. Unit tests never caught it because they mock
  the user dict *with* settings; the live k3d smoke exposed it. Fix:
  explicit `postgres_db.get_user_settings()` fetch in `create_thread`.
- Naming: the service signal is `isDraftSession` / `enterDraftSession()` —
  "draft" alone already means the composer's persisted text draft.
- Draft→session URL move is an `effect` in `ChatPageComponent` watching
  `threadId` (service stays Router-free); the destination instance skips
  reconnecting via a same-thread guard extended to cover `isStartingSession`.
- Extra polish: sessions-list empty state gained a "Start chatting" CTA → `/`.
- Verified: 8794 pytest green (1 unrelated env-dependent failure:
  `test_database_phase1::test_connect_disconnect` needs a live local
  Postgres), 892 vitest green, ruff clean, prod build clean. Live k3d:
  resolved default surfaces `virtual`; create matrix (no setting → virtual ·
  saved sandbox → sandbox · explicit `none` beats saved sandbox · invalid
  PATCH → 422); UI flow `/` → type → Enter → thread created with title from
  message + default project attached, message rode the outbox, agent replied
  — with a cold agent pod (no warm pool locally) in well under a minute; the
  425 `/connection` console entries during startup are the normal readiness
  poll. `/sessions`, `/sessions/new`, `/sessions/:id` regressions clean.

## Updates since ship (as of 2026-07-16)

- **Shipped**: all three slices + the create_thread settings-fetch fix landed
  in one commit, `e374ec07` (2026-07-12) on develop. The landing flow is the
  live behavior of `/`.
- **S3 prerequisite issue archived**: the bundled-fallback proposal moved to
  `docs/done/s3_object_store_bundled_fallback.md` (`c51a4cf0`, 2026-07-14);
  references here were repointed.
- **VM-at-create supersedes the "vm is upgrade-only" rule**
  (`docs/features/session_create_on_vm.md`, `fd0525e9`, 2026-07-16): the New
  Session form's explicit VM option is now honored at create (operator-gated,
  extended readiness budgets). The design's split survives as two constants:
  `SESSION_CREATE_WORKSPACE_BACKENDS` (create-time allowlist, includes `vm`)
  vs `SESSION_WORKSPACE_BACKENDS` (defaults chain + settings-PATCH set,
  excludes `vm`). Consequence for this feature: **unchanged** — an instant
  session can never implicitly land on a VM; the defaults chain still
  resolves request > saved default > `virtual`, and `vm` is rejected as a
  saved `workspace_backend` value. Pinned by
  `test_vm_not_in_default_chain_set` in `tests/test_session_config_plumbing.py`.
- **Draft mode survived the chat-page canvas rework**: `ChatPageComponent`
  was rebuilt around the dynamic-canvas split pane, but the landing contract
  is intact — the draft route still enters via `chat.enterDraftSession()`
  and the service's `isDraftSession` first-send → `createAndConnect` path is
  unchanged (`persistent-chat.service.ts`).
