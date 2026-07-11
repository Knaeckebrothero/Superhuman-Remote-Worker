# Instant Landing Session ("type first, provision on send")

**Status**: PROPOSED v2 (reframed 2026-07-11 after design discussion)
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

**A1. System default (resolved server-side, availability-aware).**
`_resolve_preference_defaults().persistent_agent` gains
`workspace_backend`: `"virtual"` when the deployment has an object store
(`_virtual_workspace_rclone_spec() is not None`), else `"sandbox"`. This is
the whole "is virtual available" problem solved in one line, at the one
place that knows the env — no cockpit capability flag, no client-side
fallback logic. Deployments without an object store simply have a sandbox
system default.

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
   section; unset displays the resolved system default (virtual on
   deployments with an object store, sandbox otherwise).
2. `POST /api/persistent/threads` with no workspace override provisions the
   owner's default backend; the New Session form's explicit selector still
   wins; `vm` still rejected at create.
3. Opening `/` while authenticated shows a greeting + enabled composer, zero
   clicks; landing there creates nothing server-side.
4. Typing and sending creates a session with the user's defaults; on a
   deployment with virtual + warm pool the agent responds within seconds;
   the first message is never lost.
5. On a deployment without an object store the same flow works (sandbox
   system default; startup card shows progress; no error).
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
- **Verify** (k3d): settings default resolution both with and without
  `VIRTUAL_WORKSPACE_RCLONE_TYPE`; `/` → type → send → live session;
  explicit form selector still wins; `/sessions/*` regression pass; mobile
  viewport.

## Resolved questions (2026-07-11 discussion)

- Backend selection is a **user default, not a cockpit hardcode** — the
  landing flow sends a minimal body; the server resolves defaults. Users can
  configure "always container" in Settings.
- Virtual availability is handled **server-side in the resolved system
  default** (A1), not via a client capability flag.

## Open questions

1. **Platform default** — `virtual` when available (recommended: matches
   "most sessions don't need shell", and container is one Settings click or
   one upgrade away), or keep `sandbox` and let users opt into virtual?
2. **Default project on instant sessions** — attach the user's default
   project (assumed yes: form parity, KB routing) or leave project-less?
3. **Draft entry while a session is connected** — always fresh draft
   (assumed) vs. showing the connected session at `/`.
