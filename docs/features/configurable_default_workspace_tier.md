# Configurable default workspace tier (global + per-user)

**Status:** Proposed (plan — not yet built)
**Date:** 2026-06-27
**Related:** `docs/features/start_session_on_vm.md` (separable; can ship first),
`project_session_create_backend_dropped.md`, `docs/features/no_workspace_agent_mode.md`

## Goal

Make the default workspace backend for new interactive sessions configurable as a
layered preference, so an operator can (for example) set `virtual` as the
fleet-wide default while individual users override it for themselves and any
session can still pick a backend explicitly.

Today the default is the hardcoded bundled value **`sandbox`**
(`config/persistent_defaults.yaml:35`, `config/defaults.yaml:39`); there is no
per-user or global control.

## Resolution order (most-specific wins)

```
per-session New Session pick          (request_body.config_override.workspace.backend)
  > per-user default                  (users.settings.persistent_agent.workspace_backend)
  > global admin default              (system_settings.default_workspace_backend)
  > bundled config default            (persistent_defaults.yaml → "sandbox")
```

All four layers feed the SAME `config_override.workspace.backend` that the create
path + grant PEP already consume — this is a fallback chain, not a new mechanism.

## Where each layer lives (all infra already exists)

| Layer | Storage | Read/Write | Edit surface |
|---|---|---|---|
| Per-user default | `users.settings.persistent_agent.workspace_backend` (JSONB, `schema.sql:102`) | `GET/PATCH /api/settings/preferences` (`main.py:21090/21105`); model `UserSettingsUpdate.persistent_agent` (`main.py:4941`) | cockpit `settings.component.ts` Persistent-Agent section (`:429-539`) via `settings.service.ts` (`:95/:107`) |
| Global default | `system_settings.default_workspace_backend` (JSONB `{"backend": "virtual"}`, table `schema.sql:1250`) | `get/upsert_system_setting` (`postgres.py:9012/9033`); new admin route `GET/PUT /api/admin/system-settings/default_workspace_backend` mirroring `vm_workspaces` (`main.py:22048/22062`) | admin control (see UI note) |
| Config default | `config/persistent_defaults.yaml:35` | resolved via `config_resolver.resolve_config` | n/a |

## Changes

**Orchestrator**
1. `UserSettingsUpdate` (`main.py:4916`): add `workspace_backend: str | None` inside
   the `persistent_agent` sub-object; validate against the allowed set.
2. New admin system-setting endpoints for `default_workspace_backend` (copy the
   `vm_workspaces` pair at `main.py:22048/22062`).
3. `create_thread` resolution seam (`main.py:14986-15021`): after loading
   `user_settings` and **before** the per-session request override is merged
   (`:15043-15059`), apply the fallback chain — set
   `config_override.workspace.backend` from per-user → global → (leave unset → config
   default). The request override already wins because it merges last.

**Cockpit**
4. Persistent-Agent settings: add a "Default workspace" `<select>` (sandbox / virtual
   / none, plus vm only when permitted), reusing the `canUseVm()` gate already in
   `advanced-accordion.component.ts:935`.
5. Global default: admin control. NB there is currently **no admin UI** for
   `vm_workspaces`/`user_experts` — they're API-only — so v1 can ship the global
   default API-only (one `PUT`) and add a small admin control under the existing
   admin section in `settings.component.ts` as a fast follow.

## Grant clamping (don't persist a default the user can't start)

Only `vm` is grant-gated: `capability_grants.py` CATALOG `vm_workspace`
(default `False`, restrict-only, `:18-44`; gate at `:142`) plus the
`vm_workspaces.enabled` kill-switch + per-user `can_use_vm` (`_check_vm_permission`,
`main.py:3151`). `sandbox`/`virtual`/`none` are implicitly allowed.

- **Set-time validation (preferred):** reject/grey a per-user default the user isn't
  permitted (UI greys `vm` via `canUseVm()`; API re-checks on PATCH). Restrict the
  **global** default to the universally-allowed tiers `{sandbox, virtual, none}` —
  `vm` is privileged/opt-in and a poor fleet default.
- **Graceful resolution fallback:** if an *inherited* default later exceeds grants
  (e.g. a grant was revoked), fall back to the config default rather than 422.
  Reserve the hard 422 (`_enforce_session_create_grants`, `main.py:15073`) for an
  *explicit per-session* pick the user isn't allowed — an inherited default the user
  didn't choose for this session shouldn't hard-fail the create.

## Open design question — expert-pinned backend

The config resolution chain places **expert/project config above user settings**
(`config_resolver.py:75`), but the create_thread seam applies user settings on top.
A user default of `virtual` (no shell) would break an expert that needs shell (e.g.
`developer` → sandbox). Decision needed:

- **(A, recommended)** Treat the user/global default as a *floor*: an expert (or
  project/DB override) that **explicitly pins** `workspace.backend` wins over the
  inherited default; the default only fills in when nothing more specific set one.
  Respects experts that require a tier. Slightly more work (check the resolved config
  for an explicit backend before applying the default).
- **(B)** User default always wins over the expert (simplest; current seam ordering),
  at the risk of a virtual default silently disabling a shell-needing expert.

## Scope (v1)

Interactive **sessions** only (the stated use case — "agent mode to virtual by
default"). Worker **jobs** have their own default (`config/defaults.yaml:39`); a
matching global/job default is a clean follow-up (same `system_settings` key or a
sibling) but out of v1 scope.

## Verification & acceptance criteria

- [ ] Global default = `virtual` ⇒ a new session with no pick boots `virtual`
      (no workspace pod); setting it back to `sandbox` restores prior behavior.
- [ ] A user's per-user default overrides the global default; the per-session New
      Session pick overrides both.
- [ ] A user without `can_use_vm` cannot save `vm` as a personal default (set-time
      reject + UI grey); global default rejects `vm`.
- [ ] A revoked grant degrades an inherited default to the config default (no 422);
      an explicit unauthorized per-session pick still 422s.
- [ ] Expert-pinned backend behaves per the chosen option (A/B) above.
- [ ] No regression to non-session (worker) defaults.

## Relationship to the VM-at-create plan

Independent. This feature is about the *default* tier; `start_session_on_vm.md` is
about *enabling `vm` as a pick/upgrade*. They compose (a user could default to
`virtual` and upgrade to `vm` per session) but neither blocks the other, and this one
carries no HA/VM risk so it can land first.
