---
tags:
  - plan
  - orchestrator
  - grants
  - security
related:
  - "[[duplicate_expert_bypasses_user_experts_kill_switch]]"
  - "[[resume_job_grant_recheck_fails_open]]"
  - "[[tool_configuration_deferred_findings]]"
---

# Plan — close two grant-boundary holes on the expert/resume paths

Two independent defects, both filed 2026-08-03 out of the tool-configuration
series, both small, both on grant-enforcement seams.

**Branch:** work directly on `develop`. No worktree, no sub-branch — that is a
standing instruction from the repo owner, not an oversight.

## Global Constraints

1. **Line numbers in the two issue docs are STALE.** They were written on
   2026-08-03 and commits have landed since. Verified positions as of today:
   `duplicate_expert` is at `orchestrator/main.py:29234` (doc says 29128);
   `_enforce_expert_save` is at `:5103` (doc says 5058); the `resume_job`
   fail-open `except Exception:` is at `:11593`. **Locate by symbol, never by
   the doc's line number.**
2. **Do not reformat or re-sort anything you did not change.** `orchestrator/main.py`
   is ~30k lines; a diff that touches unrelated lines cannot be reviewed.
3. **Every new test must be mutation-tested.** Revert the fix, confirm the test
   fails, restore. State the result in your report. A test that passes with the
   fix reverted is worse than no test — this exact pattern has produced four
   false-green results in this codebase in the last two days.
4. **Tests run with `python -m pytest <file> -x -q`.** The local env is Python
   3.14 and noisy; CI (3.12) is the real gate. Pre-existing unrelated failures
   in `test_database_phase1` and `test_mcp_capabilities` are environmental
   (`fastmcp` is not installed locally) — do not try to fix them, and do not
   report them as regressions.
5. **No new dependencies. No new endpoints. No signature changes to shared
   helpers** — both fixes are local.

---

## Task 1 — `duplicate_expert` must run the same save gate as the other four routes

**File:** `orchestrator/main.py`, function `duplicate_expert`
(`@app.post("/api/experts/{expert_id}/duplicate")`).

Five routes write an expert. Four call `_enforce_expert_save`; `duplicate` calls
none of them, so the administrator's `user_experts` kill switch does not hold on
that route: a user can mint an owned DB expert while the feature is
administratively disabled. The source row need not be theirs — visibility, not
ownership, is the test in the lookup above.

**The decision has been taken by the repo owner: enforce the FULL gate**, not
just the kill switch. A user who lacks a grant the source config requires must be
refused at copy time (422 naming the grant) rather than getting a copy that fails
later at job dispatch for a config they never authored.

### What to change

Add exactly one call, immediately after the `src["config"] = ...` validation line
and before `return await _create_forked_expert(...)`:

```python
    await _enforce_expert_save(request, src["config"], user=user)
```

Pass `src["config"]` — the just-validated, canonical fragment that will actually
be persisted — not `src.get("config") or {}`. The issue doc suggests the latter;
the validated value is strictly better because it is what gets stored, and
`_validate_expert_fragment` has already normalised it.

Order matters: validate first, then gate. `_enforce_save_grants` evaluates a
config it expects to be canonical.

Add a short comment saying why the gate is here (the kill switch is the point)
and that duplicate is the fifth of five routes — a future reader must not think
it is optional here.

### Tests

In `tests/` (find the file that already covers the expert write boundary — the
tool-configuration series added `TestExpertWriteBoundary`; put these beside it if
that is where they belong, otherwise a new focused file).

**Test A — the class, not the instance.** One parametrised test over **all five**
write routes asserting a **403** while the kill switch is off:

- `POST /api/experts`
- `PUT /api/experts/{id}`
- `POST /api/experts/import`
- `POST /api/expert-defaults/{expert_type}/fork`
- `POST /api/experts/{id}/duplicate`

The issue doc's "Verification owed" is explicit that **no test asserts a 403 from
any expert write route while the switch is off**, so the four routes that already
enforce it are unpinned too. Parametrising closes the class. Name the parameters
after the routes so a failure says which one regressed.

**Test B — the grants half on duplicate specifically.** A user without a grant
the source config requires gets **422**, and **no row is created**. Assert both:
a 422 with no row-creation assertion would pass if the write happened first.

**Beware the free-admin-bypass trap** (`tool_configuration_deferred_findings`
§4.1): a bare `AsyncMock` db makes `await db.get_user()` return an `AsyncMock`
whose `.get("is_admin")` is **truthy**, so `_resolve_runner_grants` returns
`None` = bypass, and all 20 tests in `tests/test_session_tool_groups_endpoint.py`
silently exercise the admin path. Your grants test MUST assert it is running as a
**non-admin** — set `is_admin` explicitly to `False` on the user your stub
returns, and add an assertion or comment proving the non-admin path was taken.
Getting this wrong makes Test B vacuous.

### Done when

- The one call is in place with its comment.
- Both tests pass, and **both are mutation-tested**: remove the new call, Test A's
  `duplicate` parameter and Test B must both fail. Report the exact output.
- No other behaviour changed: `duplicate` still 404s for an invisible expert and
  still returns the forked row on the happy path.

---

## Task 2 — `resume_job`'s grant re-check must fail closed on deterministic errors

**File:** `orchestrator/main.py`, the `except Exception:` at approximately
`:11593`, inside the resume-time PEP block that calls `resolve_config` then
`_enforce_dispatch_grants`. Locate it by the log message
`"Resume PEP: grant re-check failed for job %s; proceeding "`.

`GrantDenied` correctly fails closed with a 403. Every *other* exception logs and
proceeds, on the stated assumption that the "dispatch-time check stands" — which
is untrue in precisely the case this re-check exists for: the owner's grants were
narrowed *after* dispatch. If the re-check cannot run, nothing is standing.

The handler was written for **transient** errors (a DB blip reading the expert or
grant rows) and tolerating those is correct — refusing a resume because Postgres
hiccuped is worse than proceeding on a check that passed minutes ago.

What changed is that `resolve_config` now also raises **deterministically** on a
permanently-malformed stored `tools` value (`core: null`, `core: "str"`,
`core: 5`, `core: {}` each raise `ToolPolicyError` where they silently resolved to
`[]` before). One bad row therefore turns a flakiness tolerance into a **latch**:
the re-check is skipped on every resume of every job using that expert, forever,
and the endpoint returns success.

### What to change

Split the deterministic case out of the broad handler: catch `ToolPolicyError`
**before** the bare `except Exception:` and fail closed on it. Keep the broad
handler's tolerate-and-proceed behaviour for genuinely transient errors, and
leave its log line intact.

Choose the status code deliberately and say why in your report. A malformed
stored config is not the caller's fault, so `409` or `422` is more honest than
`403` (which means "your grants forbid this") — but a `403` is defensible if you
argue that an unverifiable grant must be treated as a denied one. Either is
acceptable; an unexplained choice is not. The **response body must name the real
cause** — the stored config cannot be resolved, so the grant check could not run
— and must not read like a grants denial if you did not pick 403.

Find where `ToolPolicyError` is defined and import it the way the surrounding
code imports its siblings. Do not add a broad `src.core` import at module scope
if the file's convention is a local import inside the handler.

Also fix the now-wrong comment: the block's own text says "dispatch-time check
stands", which is the false assumption. Say what is actually true after your
change.

**Out of scope, do not touch:** the `await _user_experts_enabled()` gate wrapping
the whole block (the issue doc's "second, quieter half"). That is a separate
decision about whether a kill switch should govern grant enforcement, and it is
not this task's call.

### Tests

- A resume whose `resolve_config` raises `ToolPolicyError` returns your chosen
  status and does **not** put the job back on an agent. Assert the job was not
  dispatched — a status-code-only test would pass if the fix returned the right
  code *after* resuming.
- A resume whose `resolve_config` raises a **transient** error (e.g. a plain
  `RuntimeError` or a connection error) still proceeds, so the fix is a narrowing
  and not a reversal. This test is what proves you did not break the behaviour the
  handler was built for.
- `GrantDenied` still 403s (may already be covered — check before adding).

### Done when

- Deterministic failures fail closed, transient ones still proceed, all three
  tests pass and the first two are mutation-tested.
- The stale comment is corrected.
- Your report states the status code you chose and the one-sentence argument for
  it.

---

## Not in this plan

- **The `jsonb_typeof` scan** (`tool_configuration_deferred_findings` §1.1). It is
  an operational read against a real database; k3d has zero `tools` keys, so
  running it there is non-vacuous but uninformative. Stays owed.
- **`_user_experts_enabled` governing the resume re-check at all.** See Task 2's
  out-of-scope note.
- **The live gate.** Run by the coordinator after both tasks pass review, against
  k3d as a real non-admin — the kill switch is deployment-wide, so it needs the
  isolated namespace, not a "just try it on dev" check.
