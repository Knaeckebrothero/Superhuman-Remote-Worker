---
tags:
  - issue
  - orchestrator
  - experts
  - grants
  - security
related:
  - "[[tool_configuration_defects_and_fix_roadmap]]"
  - "[[tool_configuration_deferred_findings]]"
  - "[[global_expert_management]]"
---

# `POST /api/experts/{id}/duplicate` skips the combined expert save gate — Duplicate still creates an owned expert while user-defined experts are administratively disabled

**Status:** OPEN, filed 2026-08-03. Found while closing the expert *write*
boundary in the tool-configuration series' fix wave — that fix landed on this
route too (it had no tool-override validation at all, and no credential
deny-scan), and this second gap on the same route was left because it is a
policy decision rather than a shape one.
**Severity:** medium. Not a cross-user privilege escalation — the copied config
still passes `_validate_expert_fragment`, and the copy acts as its new owner. The
harm is that an administrator's kill switch does not hold, on the one
configuration where the grant PDP is also off.
**Component:** `orchestrator/main.py:29128` (`duplicate_expert`), which calls
`_create_forked_expert` (`:28999`) at `:29155`; `_enforce_expert_save` (`:5058`);
`_user_experts_enabled` (`:4924`).
**Reasoned from code, verified by reading every call site; not exercised against
a deployment with the switch off.**

## The defect

Five routes write an expert. Four of them run the combined save gate:

| Route | `_enforce_expert_save` |
|---|---|
| `POST /api/experts` (create) | yes — `:29037` |
| `PUT /api/experts/{id}` (update) | yes — `:29080` |
| `POST /api/experts/import` | yes — `:29191` |
| `POST /api/experts/{id}/fork-default` | yes — `:29383` |
| **`POST /api/experts/{id}/duplicate`** | **no** |

`duplicate_expert` runs `_require_experts_db()`, `require_approved_user(...)`, a
visibility lookup, and `_validate_expert_fragment(...)` — then calls
`_create_forked_expert`, which writes straight to `postgres_db.create_expert`
with no further gate.

`_enforce_expert_save` is three checks in one:

```python
    if not await _user_experts_enabled():
        raise HTTPException(status_code=403,
            detail="User-defined experts are disabled by the administrator")
    await _scan_raw_request_fragment(request)
    await _enforce_save_grants(config, user=user)
```

Only the first matters much here, and it is the one with no mirror anywhere else.

## Why the kill switch is the sharp end

`_user_experts_enabled()` is a runtime switch (decision 8) whose docstring states
its intent plainly: *"When disabled, DB-expert creation + grant enforcement are
off."* Two halves, meant to move together. `duplicate_expert` breaks the first
half — a user can still mint an owned DB expert while the feature is
administratively disabled — and the second half is exactly what makes the
resulting row consequential rather than inert:

- **Job dispatch still applies DB experts when the switch is off.** The resolve is
  gated on `_is_experts_db_enabled()` (the deployment feature flag,
  `orchestrator/main.py:~3013`), while only the **PDP** call is gated on
  `_user_experts_enabled()` (`:3058`). Same shape for the resume re-check
  (`:3275`, `:11500`).
- **Sessions do ignore it.** `_resolve_session_config` returns `None` — status
  `"disabled"` — when either flag is off (`:1669`), so a session falls back to the
  legacy `config_name` + `config_override` path and the expert is not applied.

So on a deployment with the DB-experts feature on but the user-experts switch
off, the sequence is: duplicate any *visible* expert (visibility, not ownership,
is the test — the source row may be another principal's), name the copy on a job,
and the job resolves it with the dispatch PDP skipped. Nothing here grants more
than that deployment already grants to every other expert on the same path — the
PDP is off for all of them by design — which is why this is a
policy-integrity defect rather than an escalation. But the administrator asked for
no new user experts and got one.

## The grants half, for completeness

`_enforce_save_grants` is the check with a mirror: `_enforce_dispatch_grants` runs
at job dispatch, at resume and at session create, so a duplicated config that
exceeds the copier's own grants is denied *later* rather than at save. That
defers an error instead of escalating a capability — the disposition recorded
during the run, and the reason this route was not treated as a blocker. It is
still worse than the other four routes: the user gets a clear 422 there and a
confusing dispatch-time denial here, for a config they never authored.

`_scan_raw_request_fragment` is close to vacuous on this route — duplicate takes
no body, so there is no user-authored raw fragment to deny-scan. The source
config is instead covered by `_validate_expert_fragment`, which the fix wave
added here precisely because a fork is *a new write by a new principal* over a
row that may not be theirs.

## The fix

One line, and one decision.

```python
    await _enforce_expert_save(request, src.get("config") or {}, user=user)
```

placed after the source row is resolved and before `_create_forked_expert` — i.e.
where the other four routes call it, with the *source's* config as the fragment,
since that is what will be persisted.

The decision is whether the grants half should apply to a duplicate at all. Two
defensible answers: enforce it, and a user cannot copy an expert whose config
exceeds their grants (clear, and consistent with the other four routes); or
enforce only the kill switch, and accept that a copy can carry a config its owner
cannot run, because dispatch will say so. Pick one deliberately — the current
state is neither.

## Verification owed

- No test asserts a 403 from any expert write route while the switch is off, so
  the four routes that *do* enforce it are unpinned too. One parametrised test
  over all five routes would close the class rather than the instance.
- Not exercised: flip `user_experts` off in `system_settings` on an isolated
  namespace, duplicate a bundled expert, and confirm both that the row is created
  today and that it is refused after the fix. The switch is deployment-wide, so
  this needs the same isolation as
  `docs/tests/session_tool_groups_legacy_and_error_paths_verification.md` —
  not a "just try it on dev" check.
