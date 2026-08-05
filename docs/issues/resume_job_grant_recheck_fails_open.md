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

**Status:** **FIXED 2026-08-04** (`b71aee72`, widened by `915cb49a`, annotated by
`4fd13de7`). Filed 2026-08-03. The policy decision this doc deferred — that
narrowing the handler starts refusing resumes which succeed today — was taken:
**narrow, do not invert.** The line drawn is *"the stored configuration is
unusable"* (fail closed, **409**) versus *"we could not reach storage"* (still
proceed), NOT "known exception type" versus "unknown". A Postgres blip must still
not block a resume; that availability property was explicitly preserved and is
pinned by a test asserting the transient path still proceeds.
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
| `resume_job`'s PEP re-check | ~~**fails open**~~ → **409, fails closed** (fixed 2026-08-04). The `_resume_job_on_agent` row above is the unfixed twin. |

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

---

## How it was fixed, and the two rounds it took

**Round 0** caught `ToolPolicyError` only — which is what the task brief asked
for, and it was too narrow for the brief's own intent. Review found two more
equally permanent failures in the same `try` block:

- `json.JSONDecodeError` from a `config_override` stored as a malformed JSON
  string — note this raises at `_rco = json.loads(_rco)`, **before**
  `resolve_config` is called at all;
- `FileNotFoundError` from an unresolvable `base_config_name` or `$extends`
  (`src/core/loader.py:261` unguarded `open()`, and `:276-279` raising it
  deliberately).

**Round 1** widened to `(ToolPolicyError, ValueError, FileNotFoundError)`.
`ToolPolicyError` is itself a `ValueError` subclass, so listing it is redundant at
runtime and kept for self-documentation; `json.JSONDecodeError` is covered as a
`ValueError` subclass and pinned by a test that asserts exactly that rather than
relying on the reader knowing it.

### Chosen status: 409

The caller's request is well-formed; the job's **stored** config is in a
conflicting state that blocks re-verification. `403` would falsely claim the
grants forbade it — they were never consulted — and `422` would blame the request
body rather than server-side state. Verified no consumer switches on this
endpoint's status codes: cockpit's `resumeJob` routes through a generic
`catchError` that **prefers the server's `detail`** over the per-status string, so
the user sees the real cause.

### Reachability, corrected

An earlier framing of these findings as *"empirically confirmed reachable"* was
overconfident and is corrected here. `jobs.config_override`, `experts.config`,
`experts.prompts` and `users.settings` are all `jsonb` columns written via
`json.dumps(...)::jsonb`, so Postgres validates JSON **syntax** on every write —
a syntactically-malformed string is not reachable through any application path,
and the confirmation was a direct dict-poke in a test rather than a stored row.

What remains **fully reachable regardless**: the original `tools.core: null` case
(semantically bad, syntactically valid JSON, which `jsonb` stores happily) and the
`FileNotFoundError` / missing-required-field cases, which are about file paths and
structural completeness rather than JSON syntax. `FileNotFoundError` alone
justifies the widening.

### One known overlap, accepted

Catching bare `ValueError` also covers `_enforce_dispatch_grants` →
`list_grants_for_scopes`, which `json.loads` the `capability_grants.value_json`
column. A corrupted **global** grant row would therefore 409 every resume in that
scope while blaming this job's own config. Accepted rather than restructured
around: that column is `jsonb NOT NULL` written only through `json.dumps`, so
reaching it needs DB-level corruption. The durable risk is that this audit is a
**snapshot** — a call added to that `try` block which raises `ValueError` for a
transient reason would silently convert a tolerated blip into a refused resume.
The code comment says so and names the structural fix (a second `try` scoped
around the grant check) for whoever needs it.

## Correction: the PEP exists TWICE, and only the endpoint was fixed

This doc, its sibling plan and the fix commits all speak as if there is one site.
There are two. `_resume_job_on_agent` (`orchestrator/main.py` ~`:3267`) is a
near-verbatim copy of the endpoint block and still catches **only `GrantDenied`**,
so the dispatcher path swallows an unusable stored config and re-queues the job
every tick, forever, logging a message that does not name the cause.

It is not a security regression — the exception is raised before the POST to the
agent, so that path fails closed by accident. Filed as
[[dispatcher_resume_pep_twin_still_fails_open]], where the real fix (de-duplicate
into one helper returning a verdict, rather than two divergent copies) is written
up. Found by the whole-branch review, which is the only place it could have been
found: each task review saw one site and had no reason to look for a twin
8,000 lines away.

## No recovery path for a 409 caused by the job's own config

Worth stating plainly: no endpoint can change `jobs.config_name` or
`jobs.config_override`. The only job mutators are `/cancel`, `/pause`,
`/agent-release` and `/snapshot/pin`. So a job whose stored config is permanently
unresolvable is unresumable until an operator edits the row, or the user abandons
it and creates a new one.

Reachable today, not hypothetically: six bundled expert directories deleted in
this repo's history (`researcher`, `debugger`, `unrestricted`, `writer`, `coder`,
`doc_writer`) still resolve to `config/<name>.yaml` paths that no longer exist →
`FileNotFoundError` → 409 on every resume.

This is not a regression the fix introduced. Before it, the endpoint swallowed the
error and `_resume_job_on_agent` then hit the identical `resolve_config` and
returned `False`, so the job was re-queued rather than resumed. The 409 replaces an
unbounded silent requeue with an honest refusal — but "honest" is not "recoverable",
and a repair route is the missing piece.

## Still open, deliberately

- **`yaml.YAMLError`** from a malformed **bundled on-disk** config still falls to
  the tolerant handler. It is not a `ValueError` subclass and
  `load_and_merge_config`'s `yaml.safe_load` is unguarded. Left because a deploy
  artifact is different in kind from a stored DB row — a broken base config would
  also break fresh dispatch and session create, so it is unlikely to surface first
  on resume. But note the asymmetry: a broken `worker_base.yaml` has a **far
  larger blast radius** than one bad expert row, so "less likely" is not "less
  severe if it happens".
- **The `await _user_experts_enabled()` gate wrapping the whole re-check** — on a
  deployment with user experts off, the resume-time re-check does not run at all.
  Untouched here; it is a separate decision about whether a kill switch should
  govern grant enforcement.
- **No live gate.** Staging needs a job in a resumable state against a
  permanently-unusable stored config. Covered by unit tests asserting
  **non-dispatch** (not merely the status) in both directions.
