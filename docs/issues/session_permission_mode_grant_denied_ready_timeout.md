# Session "agent ready check timed out" — a `permission_mode` grant denial laundered into a workspace/ready timeout

**Status:** **ROOT CAUSE CONFIRMED + empirically verified on dev** · quick-fix applied (granted the affected user `permission_mode: auto_accept`) · **Phase 1 (provisioning pre-flight) IMPLEMENTED + k3d-verified, uncommitted on `develop`**; Phases 2–5 pending — layered design + roadmap below · **regression** since `a7ad2be0` (2026-06-18). Affects any **non-admin** who picks `auto_accept`/`autonomous` for a session; admins are unaffected (grant bypass), which masked it.
**Found:** 2026-06-25, investigating session `7c388231` on the main/dev cluster ("agent ready check timed out after 5 min 40 s").
**Component:** `src/api/orchestrator_client.py::get_thread_workspace` (discards the 403 reason) · `src/api/persistent_app.py::_poll_workspace_ready` / `_exit_workspace_not_ready` (misclassifies a permanent denial as transient) · `orchestrator/main.py::_resolve_session_config` + `_enforce_dispatch_grants` (the PDP) · `GET /api/agents/threads/{id}/workspace` (403) · `POST /api/persistent/threads::create_thread` (no create-time check) · migration `0030_capability_grants.sql` (incomplete grandfather)
**Related:** [[2026-06-18-user-defined-experts-slice-2-enforcement]] (introduced the session-path enforcement this bug rides on) · [[codex_session_gateway_baseurl_401]] (same-day session investigation, adjacent) · [[cockpit_session_startup_timers_transient_sse]] (same ready/lifecycle symptom surface) · [[expert_prompts_shadowed_by_family_variants]] (same experts/grants family)

## Symptom

Cockpit session `7c388231-a63f-4d15-9dfb-f7e675b3a5a5` ("Regelprüfer test", owner **mivan.sabri@stud.fra-uas.de**, dev, ns `superhuman-remote-worker`) never starts. The startup card spins and then fails with:

```
agent ready check timed out after waiting 5 minutes and 40 seconds
```

Orchestrator log for the thread is ~165 lines of the cockpit polling `/connection`, all `425 Too Early`, for the full ~5:40:

```
GET /api/sessions/7c388231-…/connection 425 (8ms)      ← every ~2s, 17:53:23 → ~17:58
```

Buried near the top, the actual cause appears **once** and is never surfaced to the user:

```
17:53:22.621  WARNING main  Session attach denied for thread 7c388231-…:
              permission_mode: 'autonomous' exceeds the ceiling      (main.py:2574)
```

The reaped agent pod (`srw-agent-s-8c101565`) tells the misleading half of the story — it boots fine, then exits "cleanly":

```
17:53:31.44  httpx  GET …/api/agents/threads/7c388231-…/workspace "HTTP/1.1 403 Forbidden"
17:53:31.48  src.api.persistent_app  Workspace not ready for thread 7c388231-…
             (No workspace container provisioned for thread. Cannot attach session
             without an isolated workspace.) — exiting cleanly so the orchestrator
             can rebind once the workspace recovers (not a crash).
```

The workspace container **was** provisioned and ready (`Workspace container ready: ws-thread-7c388231-a63 @ 10.42.2.245` at `17:53:27.983`). The "No workspace container provisioned" message is wrong — it is the agent mis-describing a **403 capability-grant denial** as a missing workspace.

## TL;DR

- The session's `config_override.interactive.permission_mode = autonomous`. The owner is a **non-admin** with **no `permission_mode` grant** at any scope, so his ceiling is the catalog default **`supervised`**. `autonomous` (or `auto_accept`) exceeds it → `GrantDenied` on **every** attach.
- The denial is a `403` from `GET /api/agents/threads/{id}/workspace`, carrying the real reason (`config exceeds your capability grants: permission_mode: 'autonomous' exceeds the ceiling`). The agent's client **throws that reason away** (`return None` on any non-200), so the agent can't distinguish "policy denial (permanent)" from "workspace still booting (transient)" → treats it as transient → raises `WorkspaceNotReady` → `os._exit(0)` "to be rebound".
- A grant denial is **permanent**; no rebind fixes it. So the loop is: pod boots → 403 → exits → (no new pod helps) → cockpit polls `425` until the **5:40** ready-check timeout. The user sees a content-free timeout instead of "autonomous mode isn't permitted for you."
- **Regression.** Session-path grant enforcement shipped 2026-06-18 (`a7ad2be0`). Migration `0030` grandfathered `shell_tools`/`delegation`/`vm_workspace` for existing users but **not** `permission_mode`/`autonomy_ceiling`, silently capping everyone at `supervised`. Admins bypass grants entirely, so their autonomous sessions kept working and hid the regression.

## Root cause

### 1. The policy decision (correct, by itself)

`permission_mode` is a restrict-only **ceiling** (`src/core/capability_grants.py`):

```python
_PERMISSION_ORDER = ["supervised", "auto_accept", "autonomous"]   # line 16
"permission_mode": {"type": "enum", "default": "supervised", "restrict_only": True, ...}  # line 31
```

`resolve_grants` returns the catalog **default `supervised`** for any principal with no `permission_mode` row at user/project/global (verified: the owner has only `shell_tools=true` + `delegation=true`; there are **zero** `permission_mode` grants anywhere on the cluster). `evaluate()` then flags the session:

```python
if _enum_exceeds(inter.get("permission_mode"),            # "autonomous"
                 grants.get("permission_mode","supervised"),  # ceiling "supervised"
                 _PERMISSION_ORDER):                       # index 2 > 0 → True
    v.append(f"permission_mode: '{inter.get('permission_mode')}' exceeds the ceiling")  # :172
```

`evaluate()` short-circuits for admins (`if is_admin: return []`, `:127`), which is why admin-owned autonomous sessions are fine. The owner here is non-admin → no bypass → `GrantDenied`.

This fires from **two** enforcement points, both reached via `_resolve_session_config → _enforce_dispatch_grants` (`orchestrator/main.py:1128`, `:3069`):

- **Warm-pool attach** `_send_session_attach` (`main.py:2570-2575`): catches `GrantDenied`, logs the `WARNING` above, returns `False` → falls through to a dedicated pod.
- **Workspace endpoint** `GET /api/agents/threads/{id}/workspace` (`main.py:13780-13792`): raises `HTTPException(403, _grant_violations_detail(gd.violations))`.

### 2. The agent misclassifies the 403 (the actual bug)

The dedicated agent pod boots, registers, then asks the orchestrator for its workspace. The client collapses **every** non-200 — including the information-rich 403 — to `None`:

```python
# src/api/orchestrator_client.py:436-443  get_thread_workspace
response = await self._client.get(url)
if response.status_code == 200:
    return response.json()
return None                      # ← 403 grant-denial reason discarded here
```

`_poll_workspace_ready` (`persistent_app.py:4563-4566`) bails to `None` on the first falsy poll, and `_attach_session` raises the wrong exception:

```python
workspace_override = await _poll_workspace_ready(...)   # → None
if workspace_override: ...
else:
    raise WorkspaceNotReady(
        "No workspace container provisioned for thread. "     # persistent_app.py:1116
        "Cannot attach session without an isolated workspace.")
```

`_exit_workspace_not_ready` (`persistent_app.py:702`) then treats it as a recoverable infra hiccup — deregister + `os._exit(0)` "so the orchestrator can rebind once the workspace recovers." But a grant denial is **deterministic and permanent**: the rebind hits the identical 403, and meanwhile the cockpit just keeps polling `/connection` (`425`) until its own **5 m 40 s** budget expires.

### 3. Nothing fails loud at create

`POST /api/persistent/threads::create_thread` (`main.py:14698`) copies the requested mode straight into the persisted config and the `threads.permission_mode` column:

```python
if request_body.permission_mode:
    config_override.setdefault("interactive", {})["permission_mode"] = request_body.permission_mode
# … persists; NO _enforce_dispatch_grants / evaluate() anywhere in create_thread
```

So a config the runner can never start is accepted without complaint; enforcement is deferred to attach, where it's swallowed (§2). The cockpit dropdown compounds this — it offers `autonomous`/`auto_accept` to every user regardless of grant (`cockpit/src/app/views/sessions/sessions-page.component.ts:123`).

## Why it's a regression / blast radius

- Session-path enforcement landed in **`a7ad2be0` "Add scoped capability grants with deny-by-default policy" (2026-06-18)** — it added the `_enforce_dispatch_grants` call inside `_resolve_session_config` and the workspace-endpoint `403`.
- Migration `0030_capability_grants.sql` grandfathered existing approved users for `vm_workspace`, `shell_tools`, `delegation` only (its own comment: *"the operator base ships shell + delegation enabled for every job…"* — it reasoned about **job** defaults, not **session** permission modes). `permission_mode`/`autonomy_ceiling` were never backfilled, so every pre-existing user's ceiling silently dropped to `supervised`.
- Evidence the breakage is enforcement-gated, not config-specific:
  - Non-admin **issam.kharrat** ran `autonomous` sessions successfully on **06-14 / 06-15** (14 & 16 messages) — **before** the 06-18 enforcement.
  - Admins **peter.hofmann** / **maximilian.jurkowski** (`is_admin=true`) run `autonomous` sessions with 15–72 messages throughout — grant bypass.
  - Non-admin **mivan.sabri** (06-25, post-enforcement) is the first to hit it: thread stuck `created`, 0 messages.
- **Blast radius:** any **non-admin** who selects `auto_accept` or `autonomous` for a session, on any cluster with `EXPERTS_DB_ENABLED=true` and `user_experts` not disabled (dev today). Prod ships experts **off**, so prod is currently unaffected — but this is a landmine for the prod enablement.

## Reproduction / how to verify

1. As a non-admin user with no `permission_mode` grant, create a session with permission mode `auto_accept` or `autonomous`.
2. Orchestrator logs the one-shot `WARNING … permission_mode: '…' exceeds the ceiling` (`main.py:2574`), then a steady stream of `GET …/connection 425`.
3. The session's agent pod logs a `403` on `GET …/workspace` followed by `Workspace not ready … No workspace container provisioned … exiting cleanly`, and the pod goes `Completed` (exit 0).
4. Cockpit fails at ~5:40 with "agent ready check timed out".

Confirm the policy state directly:

```sql
-- the owner's grants: only shell_tools + delegation, NO permission_mode
SELECT scope_kind, scope_id, key, value_json FROM capability_grants
WHERE scope_kind='global' OR scope_id='<user_id>';
-- cluster-wide: zero permission_mode grants anywhere
SELECT count(*) FROM capability_grants WHERE key='permission_mode';   -- → 0 (pre-fix)
```

A non-admin with an explicit ceiling grant (or any admin) does **not** repro — isolating the cause to the missing grant, not the session machinery.

## Fix

### Quick fix (applied 2026-06-25)
Granted the affected user `permission_mode: auto_accept` at user scope via Admin → grants:
```sql
INSERT INTO capability_grants (scope_kind, scope_id, key, value_json)
VALUES ('user', '<user_id>', 'permission_mode', '"auto_accept"')
ON CONFLICT (scope_kind, scope_id, key) DO NOTHING;
```
`permission_mode` is a ceiling, so this lets the user pick `supervised` **and** `auto_accept` (grant `'"autonomous"'` to also allow autonomous). Note: the **existing** session `7c388231` is pinned to `autonomous`, so `auto_accept` won't revive that thread — it needs a new session at supervised/auto_accept, or a bump to `autonomous`.

### Proper solution — layered design (defense-in-depth)

**Framing.** There are two kinds of site in this flow and the fix belongs at both:

- **Write paths** — where intent is expressed: `POST /api/persistent/threads::create_thread` (`main.py:14698`) and the mid-session `PATCH /api/agents/threads/{id}/config` (`main.py:14064`, stamps `permission_mode` at `:14132` with **no** check). Neither validates grants today.
- **The enforcement boundary** — where it is actually checked: attach/dispatch via `_resolve_session_config → _enforce_dispatch_grants` (`main.py:1128` / `:3069`). This *correctly* denies; the bug is that the denial is swallowed.

A create-time check **cannot replace** the boundary: a session created `supervised` can be PATCHed to `autonomous` later, and grants can be revoked after creation (sessions are long-lived **and** mutable). So the boundary stays; every layer is made to fail *fast and legibly* instead of silently. The PDP (`capability_grants.evaluate` / `_enforce_dispatch_grants`) is the single source of truth, called at each layer with boundary-appropriate error surfacing.

- **Layer 1 — UI greying (cockpit, UX).** Reuse the **existing** `GET /api/users/me/capabilities` (`main.py:21920`) — it already returns the caller's resolved grants + catalog *"to drive editor greying"* (admins → `grants:null` = unrestricted; the expert editor already consumes it). Wire it into the New-Session dropdown (`cockpit/src/app/views/sessions/sessions-page.component.ts:123`) and the settings dropdown: hide/disable modes above the user's `permission_mode` ceiling, with a tooltip ("autonomous requires a grant — ask an admin"). **UX only — stale-able and API-bypassable, so never the sole fix.**

- **Layer 2 — fail loud at the write paths (orchestrator, authoritative + friendly).** Call `_enforce_dispatch_grants` (`main.py:3069`, already raises `GrantDenied` with the violation list) in `create_thread` (`main.py:14698`) and the config PATCH (`main.py:14064`); translate `GrantDenied → HTTP 422` with `_grant_violations_detail(...)`. The user is rejected at click time with the reason, not after a 5:40 timeout. The same helper is the remedy for the sibling [[mcp_created_jobs_ownerless_capability_grant_denied]] — implement once, apply to both create paths.

- **Layer 3 — provisioning pre-flight (orchestrator) — the "no doomed pod / no 5-min wait" fix.** `provision_or_assign` and `_do_prepare` *already detect* the denial — `_send_session_attach` catches `GrantDenied`, logs the warning, returns a bare `False` (`main.py:2573-2575`) — then **discard the reason and spawn a dedicated pod** (`provision_or_assign.py:130`) that boots ~10 s only to hit the same 403 and exit. Instead, run the PDP **once up front**, before pool-attach or pod-spawn: on denial → `lifecycle_emit(uid, tid, "failed", reason=<violation>)` and return. No pod, no 5:40 poll, real reason on the startup card. **This is the most direct answer to "don't wait 5 minutes for a setting that was never going to work."**

- **Layer 4 — agent-side disambiguation (agent + client, defense in depth).** Closes the residual race (grant revoked *between* the Layer-3 pre-flight and the agent's workspace fetch). `get_thread_workspace` (`orchestrator_client.py:436-443`) currently collapses every non-200 to `None`; make it distinguish a permanent `403` grant-denial (carry the body) from transient not-ready. `_attach_session` then raises a **non-retryable** error (not `WorkspaceNotReady`, `persistent_app.py:1116`) → terminal exit with reason, instead of the optimistic "will rebind" `os._exit(0)` (`persistent_app.py:702`).

- **Layer 5 — policy decision (orthogonal, operator call).** Granting mivan `auto_accept` already chose "deny-by-default, grant per-user" over a broad grandfather. **Recommended:** keep deny-by-default and grant the few students who need it; skip the grandfather migration (small, known user set). If instead the pre-06-18 "everyone may choose any mode" is desired, set one `global`-scope `permission_mode` + `autonomy_ceiling` grant — but that quietly re-widens policy, so do it deliberately, not as a "bugfix".

## Implementation roadmap

Ordered max-value / least-code first, matched to the dev/thesis risk surface (not gold-plated). Phases are independent unless noted; each maps to a design layer above. Per CLAUDE.md, TDD + verify on k3d before any push to dev.

**Phase 0 — done.** Quick unblock applied (user grant); issue filed.

**Phase 1 — provisioning pre-flight (Layer 3). ✅ DONE (develop, uncommitted; k3d-verified).** ★ highest value — kills the 5:40 wait + the wasted pod.
- *Built:* `main._session_grant_violations(thread) -> list[str]` (wraps `_resolve_session_config`; returns `GrantDenied.violations`, else `[]` — parses `thread.metadata` itself). Called in the **about-to-provision** branch of `provision_or_assign` (`services/provision_or_assign.py`, the `else` after the pool/in-flight checks, using the under-lock `cur`) and `_do_prepare` (`routers/sessions.py`, top of the `if not thread.get("agent_id")` block, before workspace reconcile). On violations → `lifecycle_emit(... "failed", reason=_grant_violations_detail(...))` and return *before* any pool-attach / `provision_agent` / `ensure_session_workspace`. Gated on the unbound/provisioning path so warm reconnects to a live bound session are never re-denied.
- *Acceptance:* denied-mode session emits `session.lifecycle: failed{reason}` immediately; **no** `srw-agent-s-*` pod created; cockpit card shows the violation. ✓
- *Tests:* `test_provision_or_assign_lifecycle.py::test_grant_denied_fails_fast_without_pool_or_pod` + `test_sessions_router_prepare.py::test_do_prepare_grant_denied_fails_fast_without_provisioning` (RED→GREEN: assert `states==[provisioning,failed]`, reason carries the violation, `provision_agent`/`_find_idle`/`ensure_session_workspace` **not** awaited). 453 passed across related suites; ruff clean.
- *Verified (k3d, live orchestrator with synced code):* `_session_grant_violations` returns `["permission_mode: 'autonomous' exceeds the ceiling"]` for a non-admin (`legacy1`) + autonomous, the same for `auto_accept`, `[]` for `supervised`, and `[]` for an admin (bypass) — i.e. the exact configs that caused the 5m40s timeout now fail-fast, allowed/admin configs are untouched.
- *Not yet covered:* full cockpit create→`failed`-card repro as a non-admin (needs a non-admin Keycloak login); the helper (the integration-risky part) is live-verified and the emit wiring is unit-tested. The session **resume** path (`POST …/resume`) shares the same gap and was left for a follow-up (lower priority; the incident was create + 425-fallback prepare, both covered).

**Phase 2 — UI greying (Layer 1).** ★ the "take away the option" UX. Independent of Phase 1.
- *Build:* `CapabilitiesService` → `GET /api/users/me/capabilities`; New-Session (`sessions-page.component.ts:123`) + settings dropdowns derive options from `grants.permission_mode` (admins unrestricted); tooltip on disabled options.
- *Acceptance:* a `supervised`-ceiling user sees only `supervised` selectable; `auto_accept`-granted user sees two; admin sees all three.
- *Tests:* vitest — options derived from a mocked capabilities payload.
- *Verify (k3d):* as mivan (`auto_accept`) → only `supervised` + `auto_accept` selectable.

**Phase 3 — fail loud at write paths (Layer 2).** Belt-and-suspenders for API/bypass/stale-UI.
- *Build:* `_enforce_dispatch_grants` in `create_thread` (`main.py:14698`) + config PATCH (`main.py:14064`); `GrantDenied → 422` + `_grant_violations_detail`. Shared helper with [[mcp_created_jobs_ownerless_capability_grant_denied]].
- *Acceptance:* POST/PATCH with a mode above ceiling → 422 carrying the violation; nothing persisted. Valid mode still succeeds (regression guard).
- *Tests:* endpoint tests for both paths (422 body + happy path).

**Phase 4 — agent-side disambiguation (Layer 4).** Safety net for the post-provision grant-revocation race.
- *Build:* typed 403-vs-not-ready in `get_thread_workspace`; non-retryable terminal exit + reason in `_attach_session` / `_exit_workspace_not_ready`.
- *Why last:* Phase 1 stops the doomed pod from ever being spawned, so this only covers grant-revoked-after-provision; lower urgency.
- *Acceptance:* grant revoked after provision → agent exits logging the grant-denial reason (not "No workspace container provisioned"); cockpit surfaces it.

**Phase 5 — policy (Layer 5).** Operator decision; no code unless a grandfather is chosen (then a backfill migration mirroring `0030`).

**Cross-cutting:** one shared `GrantDenied → user-facing 4xx / lifecycle reason` translation helper, reused by sessions **and** MCP jobs (the sibling bug). Phases 1 + 2 alone fully resolve the reported symptom (no doomed pod, no 5-min wait, option removed); 3 + 4 are the correctness/robustness follow-through.

## Open questions / adjacent findings

- **Should non-admins ever get `autonomous` by default?** The restrict-only ceiling says no — not without a grant. That is the Layer 5 call: keep deny-by-default (recommended), so the real work is Layers 1–3 (discoverability + fast-fail), **not** a backfill migration. Needs an operator/product decision.
- **`auto_accept` is denied identically to `autonomous`** for a `supervised`-ceiling user — both exceed index 0. Worth stating in any user-facing error so users don't assume only "autonomous" is gated.
- **`GET /api/persistent/threads/<8-char-prefix>` raises a raw 500** (asyncpg invalid-UUID) on a truncated id — same input-validation gap noted in [[codex_session_gateway_baseurl_401]]. Cosmetic, unrelated.

## Appendix — facts established this investigation

- Session `7c388231-a63f-4d15-9dfb-f7e675b3a5a5`, project `1feeb7b8-939b-473b-87ef-81b5356dc412`, owner `c8acca63-0abf-462a-8337-c2ebad70f30c` (mivan.sabri@stud.fra-uas.de, `is_admin=false`, `is_approved=true`). Status stuck `created`, 0 messages, workspace now `deleted`.
- `config_override.interactive.permission_mode = autonomous`; `config_name = persistent_defaults`; no `expert_id`.
- Owner's grants: `shell_tools=true`, `delegation=true` only. Cluster-wide `permission_mode`/`autonomy_ceiling` grants: **none**.
- Working autonomous owners: `7241eaa3` (peter.hofmann, admin), `48de2860` (maximilian.jurkowski, admin); pre-enforcement non-admin: `6004d385` (issam.kharrat, 06-14/06-15).
- Timeline (2026-06-25): `17:53:19` workspace container created · `17:53:22.621` attach-denied WARNING · `17:53:27.983` workspace ready @ `10.42.2.245` · `17:53:28` agent pod `srw-agent-s-8c101565` (10.42.0.201, agent id `dcc54b92-…`) boots + registers · `17:53:31.44` two `403`s on `…/workspace` · `17:53:31.48` "Workspace not ready" + `os._exit(0)` · `17:54:58` pod reaped (Completed, exit 0) · `425` polling through ~5:40.
- Orchestrator pod `srw-orchestrator-5878f4564b-rhdfh`; `EXPERTS_DB_ENABLED=true`, `SKILLS_DB_ENABLED=true`.
- Enforcement origin commit `a7ad2be0` (2026-06-18, on `develop`). Migration `0030_capability_grants.sql` grandfather: `vm_workspace`/`shell_tools`/`delegation` only.
