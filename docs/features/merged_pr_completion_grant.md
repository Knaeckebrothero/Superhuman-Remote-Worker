---
tags:
  - feature
  - jobs
  - grants
  - git-integration
  - review
status: approved
created: 2026-08-15
related:
  - "[[job_review_delivery_links_and_review_session]]"
  - "[[global_expert_management]]"
  - "[[public_datasources]]"
  - "[[full_autonomy_is_not_actually_terminal]]"
---

# Require a merged pull request before a job can be sealed

**Status:** **implemented on develop `644ca703`, 2026-08-15. Not deployed, not live-gated.**

## 1. What and why

A job whose deliverable is a pull request is not done when the PR is *open* — it is done when
the PR is **merged**. Today nothing enforces that: `approve_job` never consults the forge, and
an `autonomy=full` job seals itself regardless of whether its PR ever landed.

The target user is **not** the platform admin. It is a department member who does not watch
the difference between "the agent pushed a branch" and "the change is in `main`", and who
marks work complete on the strength of a green job screen. The feature exists to stop that
footgun, and to let an operator decide *per person* and *per project* how much rope to give.

## 2. Mechanism: a capability grant

A new `capability_grants` catalog key. **Not** a column on `projects`, and **not** a check
routed through the `evaluate` PDP.

Grants are correct here on three counts:

- **Per-principal trust is the point.** `resolve_grants` resolves user > project > global, so
  one mechanism expresses both "this project's work lands via PR" and "Alice is trusted, Bob
  is not". A `projects` column could only ever express the weaker half.
- **Admin bypass is desired, not a hole.** Operators who accept the risk are unaffected.
- **There is a working precedent.** `public_datasources` is read directly at an HTTP endpoint
  via `user_can_publish_datasource` (`orchestrator/database/postgres.py:22510`) to permit a
  *user action*, raising 403 — it never touches `evaluate` and never sees a config fragment.
  This feature copies that shape exactly.

`evaluate(fragment, grants)` is for config fragments and is not involved.

### 2a. The key

```python
"complete_unmerged_pr": {"type": "bool", "default": False, "restrict_only": True},
```

Permission polarity: the grant *lifts* a restriction. It must not be named `require_...`,
which would invert the meaning of `default: False`.

Deny-by-default. `restrict_only` keeps a child scope from widening past a parent cap, so a
global deny is a hard ceiling and per-user allows work only where no broader scope has set
the key.

### 2b. Blast radius at introduction: zero

The gate can only fire on a job that has a **recorded** `context.pull_request` whose live
state is not `merged`. Verified 2026-08-15 on dev: **zero** jobs carry that record, because
the persist (`63ead51d`) shipped after the only PR-producing job ran. Deny-by-default
therefore changes no existing behaviour and needs no grandfathering — unlike migration
`0030`, which had to backfill to avoid a self-DoS.

### 2c. The admin panel is free

`/admin/grants` builds its rows from `Object.keys(svc.catalog())` and renders `bool` keys as
an Inherit / Allow / Deny tri-state
(`cockpit/src/app/views/admin/grants/admin-grants.component.ts:107,214`). A new bool key gets
a working admin UI with no cockpit change.

## 3. Reading the capability

`user_can_complete_unmerged_pr(user, project_id)` on the postgres layer, modelled line for
line on `user_can_publish_datasource`:

1. `user.get("is_admin")` → `True` (short-circuit).
2. `list_grants_for_scopes(user_id=..., project_ids=[project_id])` — the signature already
   takes a project list (`postgres.py:22382`), which is what supplies project scope.
3. `resolve_grants(user_rows=..., project_rows=..., global_rows=...)`.
4. **Fail closed** on any grant-read error. Consistent with publishing: a capability whose
   read failed is not a capability the caller has.

## 4. Reading the pull request

One predicate beside the parsers it uses, in `orchestrator/services/job_delivery.py`:

```python
async def unmerged_pr_block_reason(job, *, datasources) -> str | None:
    """None = nothing blocks sealing. A string = the reason it is blocked."""
```

Returns `None` when the job has no recorded PR, or the live state is `merged`. Otherwise a
reason naming the state. It chains what already exists and is already composed in
`GET /api/jobs/{job_id}/pull-request`: `parse_job_pull_request` →
`find_pull_request_repository` → `get_pull_request_status`.

**Forge unreachable → blocked.** A state that cannot be read is not a merged state. Nobody is
wedged: a human retries or an admin grants the capability, and the autonomous path degrades
to human review rather than to silent completion. The deliverable gate's five *fail-open*
precedents deliberately do not apply — those exist so a worker is not blocked by
infrastructure it cannot fix, whereas here a human is present or the job is being routed to
one.

## 5. Enforcement — two points, one predicate

**5a. Human approve.** In `approve_job` (`orchestrator/main.py:16909`), beside the existing
`diff_status == 'pending'` gate. **403**, matching the `public_datasources` capability
precedent, with a message naming the PR's actual state so the actionable next step ("merge
it") is obvious to a non-technical reader.

**5b. Autonomous seal.** In the terminal-status path (`orchestrator/main.py:25048`),
mirroring the cloud-diff downgrade immediately above it at `main.py:25040`: a job that
would become `completed` becomes `pending_review` instead, with an action line recording
why. The principal is the job owner, so an admin-owned job is unaffected.

Both points consult the same predicate and the same capability. Neither duplicates forge
knowledge.

### 5c. Loop jobs are excluded from the downgrade

**Changed during implementation.** The design originally argued no special case was needed,
because the principal is the job owner and admins short-circuit, so an admin-owned loop
bypasses the gate for free. That reasoning holds for the *human* path and is why 5a needs no
loop check.

It is not sufficient for 5b. Admin bypass makes the exclusion unnecessary *only while every
loop is admin-owned*, which is a property of today's deployment rather than of the code. If a
non-admin ever runs a loop, the downgrade would park it in `pending_review` where the loop
advance never fires — a stall, which is the specific failure this project treats as worse than
a bad write. `unmerged_pr_seal_status` therefore excludes loop jobs explicitly, exactly as the
cloud-diff downgrade does (`not _completion_loop_id`, `main.py:25010`), and the pure gate pins
it with a test.

The loop also owns its own delivery and merge (`should_merge_job_contribution` returns early
for loop jobs), so its pull requests are a different lifecycle from a one-shot job's.

## 6. Accepted limitations

**A job with no PR is not gated.** An agent that simply never opens a pull request sails
through. Closing this means tying the requirement to the deliverable contract — a much larger
change to a system that currently contracts file paths, not artefact kinds. Documented, not
built.

**A PR closed without merging blocks approval permanently** while the grant is absent. This is
correct: the remedy for rejected work is to fail the job, not to approve it. Approval is not
the only terminal path.

**`full` autonomy gains a second downgrade condition.** Accepted deliberately; see
[[full_autonomy_is_not_actually_terminal]] for the ladder change that would resolve it.

## 7. Testing

TDD, RED before GREEN. The negative controls carry more weight than the happy path, because
they cover the regressions this feature could plausibly introduce:

- a principal **with** the grant approves exactly as today;
- a job with **no PR record** is never blocked;
- an **admin** is never blocked;
- a job whose PR is **merged** approves;
- `test_catalog_keys_and_defaults` (`tests/test_capability_grants.py:29`) pins the exact key
  set and must be updated in the same commit — its failure is the RED signal that the catalog
  changed.

Live gate on dev: run a job that opens a PR; approval refused while open; merge on GitHub;
approval succeeds. This doubles as the first real render of the Delivery panel, which has
never been exercised against actual data.

## 8. Out of scope

- A `loops` capability grant. A natural sibling and a one-line catalog addition once this
  pattern exists, but not this change.
- Merging pull requests from the cockpit. SRW opens PRs and never merges them; the human
  merge gate is deliberate.
- Any change to the autonomy ladder.

---

## 9. Implementation — 2026-08-15

| piece | where |
|---|---|
| catalog key `complete_unmerged_pr` | `src/core/capability_grants.py` |
| capability read `user_can_complete_unmerged_pr` | `orchestrator/database/postgres.py` |
| live predicate `unmerged_pr_block_reason` | `orchestrator/services/job_delivery.py` |
| shared gate `_unmerged_pr_gate_reason` | `orchestrator/main.py` |
| pure decision `unmerged_pr_seal_status` | `orchestrator/services/completion.py` |
| enforcement 5a (403) | `approve_job`, beside the `diff_status` check |
| enforcement 5b (downgrade) | `_complete_job_legacy`, beside the mode A downgrade |
| tests | `tests/test_merged_pr_completion_grant.py` (24), `tests/test_capability_grants.py` |

Built TDD, RED verified before every GREEN. `approve_job` now binds `user, job` where it
previously discarded the user — the human path needs the principal.

### Two tests worth knowing about

`tests/test_capability_grants.py` contains a completeness test that enumerates `CATALOG` and
demands every key either have a `strip_to_grants` branch or appear in
`_NOT_ENFORCED_BY_EVALUATE_FRAGMENT_PDP` **with a reason**. A new key fails it by default.
That is the design working: this key is excluded there, stating that it gates a terminal
transition against live forge state and has no config fragment to strip.

The negative controls carry the regression risk, not the happy path:

- a job with **no** pull request is refused by nothing and costs zero I/O (asserted on the
  mocks, not merely on the return value);
- a principal **holding** the grant is never blocked;
- a **loop** job is never downgraded.

### Not done

- **Not deployed.** No image build, no dev rollout. Verify from
  `.status.containerStatuses[].image` on the pods, never the Deployment spec.
- **Live gate not run.** The §7 sequence — open a PR, refuse, merge, approve — is unexecuted.
  It needs a job that actually opens a pull request, which also makes it the first real render
  of the Delivery panel.
- **No grant has ever been issued** for this key, so the allow path is unit-tested only.
