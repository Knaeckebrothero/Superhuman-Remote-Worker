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

---

## Task 3 — duplicate copies what the user CAN have, and says what it dropped

**Added 2026-08-04 after measuring Task 1's consequence.** Task 1 made `duplicate`
refuse a source config exceeding the copier's grants. Measured against the real
PDP with default grants (`shell_tools=False`, `delegation=False`), that refuses
**7 of the 11 shipped experts** — `scholar`, `developer`, `critic` (shell +
delegation) and `bughunter`, `designer`, `designer-interactive`, `product-qa`
(shell). `assistant`, `centurion`, `curator`, `general-worker` still pass.

`scholar` is the one the route's own docstring names: *"Fork any visible expert
into an owned copy — 'start from scholar'"*. And grants default `False` for every
user created after migration `0030`, so this is the default new-user experience.

The workflow it broke is real: duplicate `scholar`, strip the shell block, run it.
The copy was useless until edited, but it *could* be edited. Refusing means you
cannot reach step one without an admin granting `shell_tools` — a grant you do not
need and should not get, for a copy you were going to strip anyway.

**The repo owner chose: copy, minus what they cannot have, and name what was
dropped.** Do not re-litigate this.

### What to change

`duplicate_expert` keeps the kill switch and the deny-scan, and replaces the
*grants* refusal with a strip-and-report.

Put the stripping in **`src/core/capability_grants.py`**, beside `evaluate`, not in
`orchestrator/main.py`. The grant → config-path mapping mirrors `evaluate`'s rules
one-for-one and the two must not drift; a copy in the route layer guarantees they
will. Suggested shape (name it as you see fit):

```python
def strip_to_grants(fragment: dict, grants: dict) -> tuple[dict, list[str]]:
    """Return (fragment minus what `grants` forbids, the grant keys dropped)."""
```

**Deleting the offending key is the right operation for every rule**, because
absent means "inherit the base" and the base is the conservative floor. That holds
for `tools.*`, for `delegation.enabled`, for `autonomy`, for
`interactive.permission_mode`, for `workspace.backend` and for model pins.

The nine rules and what each one's violation implicates:

| grant | config path(s) to remove |
|---|---|
| `shell_tools` | `tools.shell` |
| `delegation` | `tools.delegation` **and** `delegation.enabled` |
| `datasource_tools` | `tools.{sql,mongodb,graph,webdav,email,mcp,repo}` |
| `browser` | `tools.browser_direct` |
| `catalog_authoring` | `tools.catalog_authoring` |
| `vm_workspace` | `workspace.backend` |
| `model_selection` | the offending model pins |
| `autonomy_ceiling` | `autonomy` |
| `permission_mode` | `interactive.permission_mode` |

**THE SAFETY PROPERTY, and it is not optional:** after stripping, **re-run
`evaluate`**. If any violation survives, **refuse with the 422 exactly as Task 1
does now**. This is what makes an incomplete strip map impossible to exploit — it
can only ever produce a false refusal, never a permitted escape. Say in a comment
that this is why the re-check exists.

Do not remove empty parent dicts if that changes meaning; do not mutate the input
fragment in place (the caller's `src` dict is reused).

### The response

`duplicate` returns whatever `_create_forked_expert` returns. Add a key naming
what was dropped — the owner's chosen shape was:

```json
{"id": "...", "dropped": ["shell", "delegation"]}
```

Report the **grant keys or the config paths**, whichever reads better to a user;
pick one and be consistent. Additive, so no consumer breaks. Do **not** change the
other four routes' responses.

### Tests

1. **Each of the nine rules strips.** Parametrised over the rules: a fragment that
   violates exactly one grant comes back clean, with that grant reported.
2. **Completeness, derived from the PDP — not a hand-written list.** A test that
   iterates the grant spec (the same source `evaluate` reads) and asserts every
   grant key which `evaluate` can flag is handled by the strip map. A hand-listed
   test cannot catch a rule added later; this is the same hole
   `TestGrantMapMatchesThePDP` had and had to be fixed for.
3. **The safety re-check fires.** Force the strip map to miss (monkeypatch it to a
   no-op, or feed a violation it does not handle) and assert the route still 422s
   rather than creating a row.
4. **End-to-end on the real shipped configs:** duplicating `scholar` as a
   default-grants non-admin returns **200**, the stored row has no `tools.shell`
   and no `tools.delegation`, and the response names both. This is the test that
   proves the reported defect is fixed — the other three prove the mechanism.
5. **A user WITH the grants gets an unmodified copy** — `dropped` empty and the
   config byte-identical to the source. Otherwise stripping could be firing for
   everyone.
6. Task 1's kill-switch test must still pass unchanged: stripping happens *after*
   the 403, not instead of it.

### Done when

- Strip-and-report is in `capability_grants.py`, the route calls it, the safety
  re-check is in place.
- All six test groups pass; 1, 3 and 4 are mutation-tested.
- `duplicate` of each of the 7 previously-refused shipped experts returns 200 as a
  default-grants non-admin. State the measured before/after in your report.

---

## Task 4 — fork-as-default strips and reports too

**Added 2026-08-05.** Task 3 made `duplicate` strip-and-report. The owner has now
ruled the same for `POST /api/expert-defaults/{expert_type}/fork`
(`fork_my_expert_default`), which is `duplicate` plus "select the copy as my
default" and carries the identical 7-of-11 exposure on the same shipped experts.

Why it matters that a personal default is involved: `set_user_expert_default`
requires a UUID and enforces `owner_id == caller`
(`orchestrator/database/postgres.py:9256`), so a personal default **cannot** point
at a bundled expert. Fork is the only one-step way to base a default on `scholar`.

### What to change — server

`fork_my_expert_default` currently calls `_enforce_expert_save(request,
source["config"], user=user)`, which refuses. Route it through the **same
strip-and-report path Task 3 added for duplicate** — reuse those helpers, do not
write a second copy of the logic, and keep the safety re-check (strip, then
`evaluate` again, and 422 if anything survives).

Both source paths must be covered: a **bundled** source
(`_bundled_expert_bundle`) and a **DB** source (`resolve_root_expert`).

Return `dropped` alongside the existing `{"default": …, "source": "user"}`.
Additive.

**Two gates must still fire first, in this order:**
1. `personal_defaults_allowed` → 403 *"Your administrator has disabled personal
   default experts"*. This is a **different** switch from `user_experts`; do not
   merge them.
2. The `user_experts` kill switch inside the save prelude → 403.

`tests/test_tool_override_boundary.py::test_kill_switch_403s_every_write_route`
includes this route and **must still pass unchanged**. Stripping happens after
both 403s, never instead of either.

### What to change — cockpit, and read this before designing it

`settings.component.ts:2807` calls `forkPersonalExpertDefault` and on success
**navigates immediately to `/experts/{id}/edit`**. A banner on the settings page
would flash and be gone. So a naive "add a toast" reintroduces exactly the silent
stripping this task exists to prevent.

Preferred: carry `dropped` through the navigation (router state) and surface it
**once** on the expert editor the user lands on. The editor already reads the
resolved toolset as of `68ac7bde`, so the notice explains *why* the toolset it is
showing is narrower than the source's.

Acceptable fallback if router state turns out awkward: when `dropped` is
non-empty, **do not auto-navigate** — render the notice on the settings page with
the edit action still available. When it is empty, navigate exactly as today.

Either way:
- `forkPersonalExpertDefault`'s return type is `Observable<unknown>` and the
  handler reads `result?.default?.id` off an `any`. Give it a real type.
- i18n key in **both** `en` and `de-DE`; `npm run i18n:check` enforces parity and
  bans hardcoded user-facing strings.
- Name the grant keys, matching how `admin-grants.component.ts` renders them and
  how Task 3's duplicate banner does.

### Tests

1. A default-grants non-admin forks `scholar` (bundled source) → **200**, `dropped`
   names `shell_tools` and `delegation`, and the **stored** row has neither key.
   Assert the stored config, not just the response — a route reporting `dropped`
   while persisting the original would be worse than the refusal it replaced.
2. Same for a **DB** source, so the `resolve_root_expert` branch is covered.
3. A user **with** the grants gets an unmodified fork, `dropped` empty.
4. `personal_defaults_allowed` false → **403**, before any strip, no row.
5. Kill switch off → **403**, before any strip, no row.
6. The safety re-check still fires on this route: force the strip map to miss and
   assert 422 with no row.
7. The default really was set — the fork's whole point. Assert the personal default
   now points at the new row.
8. Cockpit: the notice renders when `dropped` is non-empty and does not when empty.

Mutation-test 1, 5 and 6.

### Done when

- Both source paths strip, `dropped` is returned and reaches the user.
- Both 403s still precede stripping; the existing kill-switch test passes unchanged.
- `npx tsc --noEmit`, `npx vitest run`, `npm run i18n:check` and
  **`npx ng build --configuration production`** all pass. The template compiler is
  the only thing in this repo that validates an Angular binding.
- Report the measured before/after for forking each of the 7 shipped experts.
