---
tags:
  - issue
  - security
  - sessions
  - cloud
related:
  - "[[session_config_drift_resume]]"
---

# Protected RO mount is attributed to the caller, not the thread owner

**Status:** Open. Pre-existing — blame `07b9c6b3` (2026-07-11). Found while
reviewing the session-config-drift work (2026-08-09), which did **not** touch
this code.

## What happens

`resume_thread` (`orchestrator/main.py`, the `_schedule_protected_engage` call)
passes the **caller's** id as `user_id`:

```python
_schedule_protected_engage(thread_id, user_id=str(user["id"]), mount_rows=...)
```

`require_thread_owner` lets admins through for threads they do not own, so
`user` is not necessarily the thread owner. When an admin resumes another
user's protected-cloud thread, `engage_ro_mount(user_key=user_id,
user_id=user_id)` mints the per-user read-only reader and writes the
`cloud_ro_mounts` row under the **admin's** account.

The grant is therefore attributed to — and revocable via — the wrong identity.

## Why it is Minor, not urgent

- No new data reach: the admin already had full access to the thread, and the
  folder handle comes from the thread's own mount row.
- The only `user_id`-keyed reader of that table, `list_ro_mounts_for_user`, has
  no callers today.
- It needs all of: protected-cloud mode enabled, no active grant on record, and
  a non-owner admin resuming.

## Why it is worth fixing

It is the same shape as a defect fixed in the config-drift work, where drift was
computed as the caller but enforced as the owner. That one produced a
permanently dead session. The general rule this points at:

> Anything a handler does **on behalf of a thread** must key off
> `thread["user_id"]`, not the authenticated caller. `require_thread_owner`
> authorizes the request; it does not tell you whose resources are in play.

Worth auditing other `require_thread_owner` handlers for the same conflation.

## Decision needed before fixing

Who should own a protected mount when an admin acts for an owner? Attributing
it to the owner is the obvious answer, but it means an admin's action mints a
grant under an account that never asked for it — so the reconciler's revocation
semantics need to be checked against that before the change lands.
