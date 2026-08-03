---
tags:
  - issue
  - security
  - orchestrator
  - jobs
  - config-resolution
  - grants
related:
  - "[[tool_configuration_defects_and_fix_roadmap]]"
  - "[[tool_configuration_deferred_findings]]"
  - "[[tool_config_policy_vs_membership]]"
---

# `resume_job`'s grant re-check fails open — a malformed stored `tools` value latches the control off while the endpoint keeps returning success

**Status:** OPEN, filed 2026-08-03. **Not fixed**, deliberately — narrowing the
handler is a policy decision that would start refusing resumes that succeed
today.
**Severity:** security, medium. Nothing crashes and nothing is escalated
*directly*; a control that exists to catch a specific escalation is silently
skipped. Exploitation requires the ability to store a malformed expert
fragment, which is an authenticated, approved user.
**Component:** `orchestrator/main.py:11500-11535` (`resume_job`'s PEP block),
`src/core/tool_policy.py` (`ToolPolicyError`), `_enforce_dispatch_grants`.
**Pre-existing** — the handler predates the tool-configuration series. What the
series changed is that the exception is now reachable from **stored data** rather
than only from infrastructure flakiness.

## The defect

`resume_job` re-runs the grant check before putting a job back on an agent:

```python
        except GrantDenied as gd:
            logger.warning("Resume denied for job %s: %s", job_id, gd)
            raise HTTPException(
                status_code=403, detail=_grant_violations_detail(gd.violations)
            )
        except Exception:
            logger.exception(
                "Resume PEP: grant re-check failed for job %s; proceeding "
                "(dispatch-time check stands)",
                job_id,
            )
```

`GrantDenied` fails closed with a 403, which is correct. Every *other* exception
logs and proceeds — including any raised by the `resolve_config` call two lines
above, which is what produces the merged fragment the check evaluates.

**The comment names the assumption and the assumption is the one that fails.**
"Dispatch-time check stands" is precisely untrue in the case this re-check was
built for: the owner's grants were narrowed *after* dispatch. If the re-check
cannot run, there is nothing standing.

## Why it is reachable now

The handler was written for transient errors — a database blip while reading the
expert row or the grant rows. Those are worth tolerating: refusing a resume
because Postgres hiccuped is worse than proceeding on a check that passed
minutes ago.

Since the tool-policy normaliser landed, `resolve_config` also raises
**deterministically** on a permanently-malformed stored `tools` value.
`core: null`, `core: "str"`, `core: 5` and `core: {}` each resolved to `[]` in
silence before and now raise `ToolPolicyError`. The stored populations that reach
this path are `experts.config` and `project_experts.config_override`, neither of
which was shape-validated on write until this series (the expert half now is;
see [[tool_configuration_deferred_findings]] §1).

So one bad row turns a tolerance for flakiness into a **latch**: the re-check is
skipped on *every* resume of *every* job using that expert, for as long as the row
exists. The endpoint returns success. The only trace is one `logger.exception`
line, in a message that reads like a transient warning.

**Second, quieter half:** the whole block is gated on `await
_user_experts_enabled()`. On a deployment with user-defined experts switched off,
the resume-time re-check does not run **at all**. That is defensible — the expert
layer is not in play — but it means the control's coverage depends on a kill
switch nobody associates with grant enforcement.

## Blast radius, for context

`ToolPolicyError` out of `resolve_config` does not behave uniformly. Re-read from
source per call site (the first version of this table had 4 of 8 rows wrong, every
error understating risk):

| Call site | Outcome |
|---|---|
| session create, `agent_create_thread`, expert-default-set | **500** — hard fail |
| `_dispatch_job_to_agent` | degrades: logs, sets `resolved_config=None`, continues |
| `GET /tool-groups` | degrades: `source: "error"` |
| session attach | degrades: falls back to `config_name`, the expert is lost |
| `_resume_job_on_agent` | returns `False`, which `resume_job` reads as "queue for auto-dispatch" → **HTTP 200** |
| `resume_job`'s PEP re-check | **fails open** — this issue |

Three of eight hard-fail, three degrade silently, one fails soft to 200, one fails
open. Only the last is a security control.

## The minimal fix

Recorded during the run, and it keeps the property the handler was written for:

**Catch `ToolPolicyError` separately and fail closed** — 422 or 403 with the
policy error's own message, which already names the category and the bad shape —
**and keep `except Exception` tolerating transient errors.** A defect that
reproduces on every single call is not a flake, and treating it as one is what
turns a skipped check into a latched-off check.

Two things to decide with it, which is why this is a ticket rather than a patch:

1. **It will start refusing resumes that currently succeed.** Any job whose expert
   holds a malformed fragment becomes unresumable until the row is fixed. Run the
   read-only `jsonb_typeof` scan from [[tool_configuration_deferred_findings]] §1.1
   on the target database first; a non-zero count is the population that breaks.
2. **A skipped check should be visible as a skipped check.** `logger.exception`
   with a message about the dispatch-time check standing reads as reassurance. If
   the tolerance stays for transient errors, the log line should say the control
   did not run, and ideally the job should carry that fact.

## Verification owed

- No test covers the `except Exception` branch of this block, in either
  direction. A test that makes `resolve_config` raise a non-`GrantDenied`
  exception and asserts the endpoint's behaviour would pin whichever policy is
  chosen.
- The scenario has never been exercised end to end: narrow an owner's grants
  after dispatch, plant a malformed `tools` value on the expert, resume, and
  confirm from the agent's bound toolset that the narrowed grant was not applied.
  That is the demonstration that turns this from reasoned-from-code into observed.
