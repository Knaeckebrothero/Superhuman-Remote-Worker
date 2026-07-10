# Instant Landing Session ("type first, provision on send")

**Status**: PROPOSED (design for alignment, 2026-07-09)
**Scope**: cockpit (primary) + one small orchestrator capability flag
**Related**: `docs/features/no_workspace_agent_mode.md` (virtual/lite tiers),
`docs/features/builder_to_sessions_consolidation.md` (why `/` currently
redirects to the sessions list), workspace tier upgrade spec
(`workspace_tier_upgrade.md`).

## Problem

Opening `srw.works` lands on the sessions list (`'' → redirectTo: 'sessions'`,
`cockpit/src/app/app.routes.ts:33`). For a new or returning user that page is
an empty-state card — there is nowhere to type. Starting a conversation takes
three steps: **New Session** → full options form (`/sessions/new`) → create →
wait for provisioning. Since the builder (the old default chat surface) was
removed, the product has no zero-friction entry point.

Meanwhile the platform can now start a session in a few seconds: the virtual
workspace backend needs no workspace container (~40 s saved) and the warm
agent pool usually has an idle pod ready to attach. The UX just doesn't
exploit it.

## Goal

Opening the app root lands in a **fresh, open chat** — greeting + enabled
composer, exactly like the big AI apps. Nothing is created until the user
sends. On first send, the cockpit creates a **virtual-backend session** with
sensible defaults and the message rides the existing outbox into the new
thread. If the session later needs shell/git, the existing in-place
upgrade-to-workspace path applies (backend hot-swap, conversation preserved).

Explicitly **not** a new chat surface: this is a *draft mode* on the existing
`ChatPageComponent`/`PersistentChatComponent`/`PersistentChatService` stack.

## What already exists (verified 2026-07-09)

The machinery is ~80 % built; this feature is mostly a new entry path.

- **Deferred creation flow**: `SessionCreateComponent.createSession()` does
  not call the API. It navigates to `/sessions/_creating` with the create body
  in `history.state`; `ChatPageComponent.ngOnInit`
  (`cockpit/src/app/views/chat/chat-page.component.ts:32-45`) detects the
  sentinel and calls `PersistentChatService.createAndConnect(body)`
  (`persistent-chat.service.ts:712-745`), which POSTs
  `POST /api/persistent/threads`, connects with `{carryOutbox: true}`, and
  `replaceUrl`-navigates to the real thread id.
- **Type-before-ready**: the composer is already enabled while starting
  (`canCompose = isConnected || isStartingSession`,
  `persistent-chat.component.ts:1754`). Messages sent early queue in the
  **outbox** (`persistent-chat.service.ts:537`, `sendMessage` → `_flushOutbox`)
  and auto-flush on `markSessionReady()` (`:2736`). The outbox is carried
  across `createAndConnect` — a message typed during creation survives into
  the new thread.
- **Server defaults**: `ThreadCreateRequest` (`orchestrator/main.py:17195`)
  defaults `config_name` → user settings → `persistent_defaults`, and
  `permission_mode`/`model` fall back to the user's saved
  `settings.persistent_agent`. A minimal body is a valid session.
- **Virtual = lite = fast**: `config_override.workspace.backend: "virtual"`
  makes `create_thread` skip workspace-pod provisioning entirely
  (`orchestrator/main.py:17440-17452`); the agent gets object-store mounts
  injected at attach (`_inject_lite_workspace_config`, `main.py:2883-2932`)
  and binds pool-first via `provision_or_assign`
  (`orchestrator/services/provision_or_assign.py:144-178`) against the warm
  pool (`agent_provisioner.ensure_warm_pool`, `MIN_AGENTS`/`AGENT_BUFFER`).
- **Upgrade path**: `POST /api/agents/threads/{id}/upgrade-to-workspace`
  (`orchestrator/main.py:16790-16879`) hot-swaps virtual → sandbox in place;
  `vm` delegates to the operator-gated VM path. Sessions cannot *start* on
  `vm` (rejected at `main.py:2749-2760`) — consistent with "start lite,
  upgrade when needed".

**The one hard gap**: virtual availability is deployment-dependent. If
`VIRTUAL_WORKSPACE_RCLONE_TYPE` is unset, `_virtual_workspace_rclone_spec()`
(`main.py:2840`) returns `None` and a virtual session fails at attach with
`LiteWorkspaceConfigError` — creation succeeds, then the session dies. The
cockpit currently has no way to know this in advance.

## Design

### D1 — Root route becomes a draft chat

`app.routes.ts`: replace `'' → redirectTo: 'sessions'` with
`'' → ChatPageComponent` (authGuard, `data: {draft: true}`). The `**`
wildcard keeps cascading to `''` and now lands on the draft chat. `/sessions`
(list), `/sessions/new` (full options form), and `/sessions/:threadId` are
unchanged. The sessions-list empty state's CTA can point at `/` instead of
the form (polish).

Always a *fresh* draft — no auto-resume of the last session (matches
ChatGPT/Claude landing behavior; past sessions are one click away in the
sidebar).

### D2 — Draft mode in the chat stack

`ChatPageComponent.ngOnInit`: on `data.draft`, call `chat.enterDraft()`
instead of the current `router.navigate(['/sessions'])` fallback.

`PersistentChatService`:
- `enterDraft()` — reset view state to an empty conversation and set an
  `isDraft` signal. If a previous session is still connected, detach it the
  same way switching to another thread does today (the server-side session
  survives; it's resumable from the list).
- `sendMessage()` in draft state — push to outbox as today, then build the
  default create body (D3) and run the **existing** `createAndConnect(body)`
  path with `{carryOutbox: true}`; on success navigate to
  `/sessions/{id}` (no `replaceUrl` — back returns to a fresh `/` draft).
  From the user's perspective: they typed, hit send, the startup card plays,
  the message flushes when the agent is ready.

`PersistentChatComponent`:
- `canCompose` additionally true in draft.
- Draft empty state: centered greeting ("What can we do for you?") above the
  existing composer. Minimal CSS — the persistent-chat style budget stays
  flat. A small "Advanced options" link routes to `/sessions/new` for users
  who want expert/model/datasource control up front.

### D3 — Default create body

```jsonc
{
  "title": "<first message, single line, truncated ~60 chars>",
  "config_override": {"workspace": {"backend": "virtual"}},   // if available, see D4
  "project_ids": ["<user's default project>"]                  // if one exists
}
```

Everything else is omitted deliberately: the orchestrator already applies the
user's saved config/model/permission defaults server-side. Default project
is included to match the create form's auto-select behavior (KB/notes
routing); fetched non-blocking on draft entry, dropped if the fetch hasn't
resolved by send time. Attachments stay disabled in draft (they're gated on
`isConnected` today; unchanged).

### D4 — Virtual availability flag + fallback

Orchestrator: `GET /api/users/me/capabilities` (`main.py:25123`) gains
`"virtual_workspace_available": bool` computed as
`_virtual_workspace_rclone_spec() is not None`. Cheap, env-derived, no DB.

Cockpit: `CapabilitiesService` exposes it. Draft send includes the
`workspace.backend: virtual` override only when available; otherwise the
override is omitted and creation follows the normal sandbox path — slower
(startup card communicates progress, as today) but never broken. This keeps
the landing flow working on deployments without an object store (some local
k3d setups).

### Out of scope (follow-ups)

- LLM auto-titling of sessions (truncated first message is v1).
- Suggestion chips / recent-sessions rail on the draft page.
- Making `virtual` the default in the full create form.
- A visible "upgrade to workspace" affordance in the session UI (upgrade
  exists server-side; surfacing it is the workspace-tier-upgrade project).
- Removing the dead inline `showCreate` dialog in
  `sessions-page.component.ts` (unrelated cleanup, noted while mapping).

## Acceptance criteria

1. Opening `/` while authenticated shows a greeting + **enabled composer**
   with zero clicks. No thread row, agent bind, or any server-side artifact
   is created by merely landing there.
2. Typing a message and sending creates a session; on a deployment with
   virtual workspace + warm pool, the agent responds within a few seconds.
   The first message is never lost (outbox flush on ready).
3. On a deployment without `VIRTUAL_WORKSPACE_RCLONE_TYPE`, the same flow
   works via the sandbox fallback (slower, startup card visible, no error).
4. `/sessions`, `/sessions/new`, `/sessions/:id`, and deep links behave
   exactly as before; entering `/` while another session is connected does
   not kill that session server-side.
5. Draft → send → back button → fresh draft (no `_creating` corpse routes).
6. Works in the mobile layout (same components; responsive CSS only).

## Implementation slices

- **S1 orchestrator** (~0.5 h + test): capability flag in
  `/api/users/me/capabilities`.
- **S2 cockpit core** (~0.5–1 d + vitest): route change, `enterDraft()` +
  draft send-to-create in `PersistentChatService`, `canCompose` gate, draft
  hero template. This slice alone ships the feature.
- **S3 polish** (~0.5 d): title from first message, default-project attach,
  "Advanced options" link, sessions-list empty-state CTA → `/`.
- **Verify** (k3d): open `/` → type → send → live session in seconds; unset
  virtual env → fallback path; smoke `/sessions/*` regressions; mobile
  viewport pass.

## Open questions

1. **Default project on instant sessions** — attach it (form parity, KB
   routing) or keep instant sessions project-less until the user files them?
   Design assumes attach-if-exists.
2. **Draft entry while a session is running** — design says detach and show
   a fresh draft (session stays alive server-side). Alternative: root shows
   the connected session and only shows a draft when nothing is connected.
   Design assumes always-fresh.
3. **Sandbox fallback vs. hard requirement** — is it acceptable that
   deployments without an object store silently get the slower sandbox path,
   or should the landing experience require virtual and surface a config
   warning to admins?
