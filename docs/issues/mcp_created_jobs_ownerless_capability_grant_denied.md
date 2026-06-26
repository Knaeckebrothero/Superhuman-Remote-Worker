---
tags:
  - issue
  - mcp
  - jobs
  - capability-grants
  - dispatcher
  - security
  - user-defined-experts
  - attribution
---

# MCP-created jobs are ownerless → denied at dispatch by capability grants

**Filed:** 2026-06-25, found while trying to verify the D3 cross-pod checkpointer
([`cross_pod_resume_cold_starts_checkpoint_not_replicated.md`](cross_pod_resume_cold_starts_checkpoint_not_replicated.md))
by creating a worker job through the orchestrator MCP `create_job` tool.

## Symptom

A job created via the MCP `create_job` tool **fails at dispatch**, before any
agent runs, with:

```
config exceeds your capability grants: shell_tools: tools.shell requires the
shell_tools grant; delegation: delegation requires the delegation grant;
autonomy_ceiling: autonomy 'full' exceeds the ceiling
```

This affects **every** worker expert — `developer`, `scholar`, `critic`,
`bughunter`, `designer`, `curator`, and even `assistant` all inherit the shell
toolset, and most use delegation + autonomy `full`. So MCP-created worker jobs
can never dispatch. The job (or its scholar/critic subjobs) goes straight to
`failed`; the checkpointer / agent code is never reached.

## Root cause: the job has no owner

The dispatch policy enforcement point `_enforce_dispatch_grants`
(`orchestrator/main.py:3069`) resolves grants for the **runner = `job['user_id']`**
and evaluates the merged config against them
(`src/core/capability_grants.evaluate`). Two facts combine:

1. **MCP-created jobs have `user_id = NULL`.** The MCP `create_job` path does not
   attribute the job to the token's principal. (On dev there is no `mcp_tokens`
   table at all — the MCP auth model here doesn't map a token to a user — so the
   job is created ownerless.) Verified: both test jobs had `user_id IS NULL`, and
   the owner lookup returned 0 rows.

2. **No owner → deny-by-default grants, and no admin bypass.**
   `resolve_grants_for(user_id=None, …)` finds no `user`/`project`/`global` grant
   rows, so every key falls to its **catalog default**
   (`src/core/capability_grants.py:18`), which is deny-by-default for the security
   keys:

   | key | default |
   |---|---|
   | `shell_tools` | **False** |
   | `delegation` | **False** |
   | `vm_workspace` | False |
   | `autonomy_ceiling` | **"review"** |
   | `permission_mode` | "supervised" |
   | `datasource_tools` / `browser` | True |

   And `_enforce_dispatch_grants` only bypasses when a **real user with
   `is_admin=True`** is resolved — a `None` runner gets no bypass.

So an ownerless job running any normal worker config (shell + delegation +
autonomy `full`) trips all three gates and is denied. Migration 0030
grandfathered *existing approved users* (giving them `shell_tools`+`delegation` —
21 such users exist on dev), but an ownerless job belongs to none of them.

## Evidence (dev, 2026-06-25)

- Jobs `0d2d4243…` (config `default`, which spawned scholar subjob `033ef61f…`)
  and `96a731a9…` (config `default` + `config_override: {"autonomy":"review", …}`).
  Both: `user_id IS NULL`.
- Orchestrator log: `Dispatch denied for job … : shell_tools … delegation …
  autonomy_ceiling: autonomy 'full' exceeds the ceiling` (`main.py:2097`).
- The `autonomy: review` override **cleared exactly the autonomy_ceiling
  violation**, leaving only `shell_tools` + `delegation` — confirming the runner
  is on the default grant set (ceiling = `review`).
- `capability_grants`: 0 rows for the (null) owner, 0 global rows; 21 user rows
  each for `shell_tools=true` / `delegation=true` (the 0030 grandfather backfill).

## Effects

- **MCP cannot launch worker jobs.** Any MCP-driven job creation (testing,
  automation, agent-to-agent) is blocked for every privileged expert.
- **Lost attribution, not just grants.** An ownerless job also escapes per-user
  attribution — usage/quota accounting, project-scope access checks, and audit
  ownership all key off `user_id`. This is a security/governance gap beyond the
  grant denial.
- **Inconsistent with the cockpit path**, where a job carries the authenticated
  user, so grants (or admin bypass) resolve correctly and jobs dispatch.
- **Fails late and opaquely** — the job is created "successfully," then dies at
  dispatch; the MCP caller sees a `failed` job, not a clear up-front rejection.

## Proposed fix

The core decision is **what principal an MCP-created job should run as** (a
product/security call), then plumb it through. Ordered:

1. **Attribute MCP jobs to the token's principal.** Give MCP tokens an
   owning user (a dedicated service account, or the connecting user) and have
   `create_job` set `job.user_id` to it. Grants + admin-bypass then resolve
   normally. This is the proper fix and also restores attribution/quota/audit.
2. **Define a least-privilege MCP service user** with an explicit, auditable
   grant set (e.g. `shell_tools` + `delegation` + a chosen `autonomy_ceiling`)
   rather than admin — so MCP automation is scoped, not all-powerful.
3. **Fail loud at creation, not dispatch.** Have `create_job` (and the MCP tool)
   evaluate grants up front and reject with the violation list, instead of
   creating a job that silently dies at dispatch. (Same "surface the denial
   early" gap as the session issue below.)
4. **(Dev-only stopgap)** Global-scope grants
   (`set_grant(scope_kind='global', key='shell_tools'|'delegation', value=true)` +
   `autonomy_ceiling='full'`) apply to ownerless principals and unblock MCP jobs —
   but they lift deny-by-default for **everyone**. Acceptable for a dev soak, not
   for prod.

## Verification (when fixed)

- An MCP-created job has a non-null `user_id` equal to the token's principal.
- Dispatch resolves that principal's grants (or admin bypass); a privileged
  worker job dispatches and runs (agent log shows it starting, not
  `Dispatch denied`).
- Usage/audit rows for the job carry the correct owner.

## Related

- [`session_permission_mode_grant_denied_ready_timeout.md`](session_permission_mode_grant_denied_ready_timeout.md)
  — sibling in the same capability-grant family (non-admin principal hits a
  deny-by-default key the 0030 backfill didn't cover; also wants fail-loud-at-create
  + surface-the-403-reason).
- `docs/done/global_expert_management.md` — the capability-grant design
  (decisions 8, 9, 19, 21–23: deny-by-default, restrict-only, admin bypass,
  0030 grandfather).
- [`cross_pod_resume_cold_starts_checkpoint_not_replicated.md`](cross_pod_resume_cold_starts_checkpoint_not_replicated.md)
  — D3, whose live dev verification this blocked (MCP couldn't launch the test job).
