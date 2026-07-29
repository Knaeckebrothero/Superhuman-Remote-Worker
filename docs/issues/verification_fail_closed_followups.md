---
tags:
  - issue
  - follow-up
  - orchestrator
  - verification
  - critic
---

# Fail-closed verification — follow-ups carried out of the implementation run

**Filed:** 2026-07-29.
**Why this exists:** the fail-closed verification rewrite shipped on `develop`
(32 commits, `c9f3cf1a..928ed60b`, deployed to dev). During the run, 24 items
were triaged and deliberately deferred, plus several settled questions worth not
re-litigating. All of it lived only in a **git-ignored** scratch ledger
(`.superpowers/sdd/…/progress.md`, ignored via `.gitignore:223`) that the
process deletes on completion. This doc is that content, made durable.

**None of the items below is a blocker.** The whole-change review confirmed no
Critical defects and that the fail-closed property is total: every path to an
approved state requires a durably recorded verdict.

Design: `docs/superpowers/specs/2026-07-27-verification-fail-closed-design.md`
Incident: `docs/issues/verification_round_reset_spawns_blind_critic.md`

---

## 1. The one thing that is actually owed: a live gate

Every probe run during implementation was **in-process**. The no-progress guard
(`content_tree`) shipped **inert three times** before it worked, and each time
the covering test passed while the guard could not fire in production:

| Attempt | Mechanism | Why it was inert |
|---|---|---|
| 1 | commit SHA | freeze commits with `--allow-empty`, so HEAD always moves |
| 2 | `HEAD^{tree}` | `output/job_frozen.json` carries a fresh timestamp, staged by `git add -A` |
| 3 | `ls-tree` minus 2 paths | `TodoManager.archive` writes a timestamped `archive/…md` after the capture |
| 4 (shipped) | + `archive/`, + `feedback.md` | — verified by probe over the full round cycle |

Four movers were found by four successive probes. The fifth-mover check was
done by **reading** every other workspace-root writer, not by observing a real
round.

**What settles it:** drive one real job through two verification rounds on dev
and compare the two recorded `content_tree` values in
`jobs.context.verification_rounds`. A real agent turn also writes `plan.md`
(`src/managers/plan.py:93`) and `workspace.md` (`src/managers/memory.py:95`) via
tools — if either churns per round, the guard is narrowed again and **no test in
the suite would catch it**. The failure mode is invisible: an inert guard is
indistinguishable from one with no reason to fire.

**Three other things have never been exercised against a live orchestrator:**
whether a critic actually receives the rendered brief (the template was
discarded since the orchestrator migration and is fixed but never observed
working); whether the agent→orchestrator round-recording call and its 409 path
work from a real agent pod; and whether escalation's wake hook and owner
notification actually fire.

### Standing architectural recommendation

**If a fifth tree-mover ever appears, do not add it to the denylist.** Invert
the design to an **allowlist** — hash `output/` minus the completion files — so
a newly added bookkeeping path defaults to *ignored* rather than to silently
breaking the guard. Four corrections to a denylist whose failure mode is silent
is the signal to change the approach, not to extend the list.

---

## 2. Open follow-ups worth doing

Ranked. None blocks anything.

1. **`_resolve_critic_outcome` trusts the stored per-round verdict instead of
   re-folding the ledger at decision time** (`orchestrator/main.py`, ~12301).
   Recomputing there would make the severity-monotone fold an actual backstop
   and would close the residual cross-replica TOCTOU (below) without an advisory
   lock. Pre-existing from the original implementation.
2. **Residual cross-replica TOCTOU on concurrent round recording.** Two truly
   concurrent recorders can both read `rounds=[]`; the one asserting `approved`
   over its own empty open set records `"approved"` and advances the target
   while the twin's high-severity finding is open. Narrow — a later recorder is
   caught by `validate_dispositions` demanding a disposition for the now-visible
   finding and 409s into a fail-closed escalation — and the duplicate-critic
   guard closes the reachable sequential case. Item 1 above is the clean fix.
3. **`_UNSTICK_REVIEWING_SQL` treats "the newest critic has a ledger row" as
   "the verdict handler acted".** A critic that records a round and is then
   orphaned before its `/complete` leaves the target wedged in `reviewing` with
   the watchdog disarmed. Pre-existing.
4. **`asserted_verdict` has no enum validation**
   (`orchestrator/main.py`, ~18456). An empty or unrecognised assertion with no
   blocking finding open computes `approved`. Reachable only behind
   `X-Internal-Key`.
5. **A low-value round is now reachable:** a `returned` verdict with an empty
   open set resumes the target with a feedback body of just `## Open findings`.
   Correct and terminating, but wasteful.
6. **`get_content_tree` parses `ls-tree` by splitting on the first tab** and
   compares raw paths. Correct for the current ASCII exclusions; a `-z` /
   `--full-name` form would be more robust if the list ever grows to paths git
   would quote.

## 3. Test-coverage gaps

All verified harmless; each is a missing pin rather than a defect.

- No test for a malformed `job_id` on `append_verification_round` (untested
  guard branch, inherited from the helper it clones).
- No test pins that a disposition naming an unknown or already-resolved id is a
  no-op.
- No test pins that an `opened` entry with a falsy id is dropped from the fold.
- `_verification_rounds`' string-coercion branch is untested — the asyncpg
  JSONB-as-string path every ledger read depends on. Failure mode is loud and
  fail-closed (empty list → escalate).
- The head-commit-authority tests cover `freeze_data` as dict and `None` but not
  the string-JSON form `_parse_freeze_data` also handles.
- The route wrapper (`@app.post` / `request.json`) is untested; only the impl
  function is exercised.
- `tests/test_autonomy.py`'s three-round test has no vacuity guard asserting an
  `archive/` path actually reached HEAD; its sibling does. The strong test leans
  on the weak one.
- `tests/test_autonomy.py::test_a_real_content_change_moves_the_value` still
  uses `MagicMock()` for the todo manager, against that file's own new
  "no mocks on the workspace side" rule. Harmless (it asserts inequality), but
  inconsistent — and mocking that collaborator is what hid the inert guard.
- `TestCompleteJobCriticStatus::test_critic_returned_gets_waiting` still passes
  but exercises a path no production caller reaches now that a returned verdict
  freezes `completed`.

## 4. Hygiene

- Severity normalisation is duplicated between `is_blocking` and `assign_ids`
  (`verification_ledger.py`). A shared `_normalize_severity()` would stop them
  drifting.
- The stored-round lookup block appears twice, near-verbatim, in
  `_record_verification_round_impl` (early short-circuit and the `appended == 0`
  fallback).
- The critic-child predicate is duplicated across the two `NOT EXISTS` blocks in
  `_UNSTICK_REVIEWING_SQL`; a `WITH critics AS (…)` CTE would express both
  against one row set.
- The round endpoint returns a bare-string `detail` on 400 but
  `{"errors": [...]}` on 409, so callers special-case by status code.
- `_final_phase_data["deliverables"]` names the round report file before
  `write_file` creates it; on I/O failure the reference dangles. Consumers treat
  deliverables as opaque metadata, so it cannot crash.
- Dead, uncalled verification-instructions code remains in
  `src/api/orchestrator_client.py` from before the orchestrator migration.
- Pre-existing and unrelated, found while enumerating approval paths:
  `src/api/orchestrator_client.py:1607` issues `PUT` against a `POST`-only
  route, so `src/agent.py::approve_frozen_job` can never succeed (405).
- `POST /api/jobs/{id}/approve` accepts a job in `reviewing`, so a human can
  approve mid-verification and the ledger keeps no record of it. Legitimate
  authorized consent, but writing a synthetic `{"verdict": "approved", "by":
  "human"}` round would keep the ledger a complete record.

## 5. Settled during the run — do not re-open without new evidence

Recorded because each cost real investigation and the reasoning is not obvious
from the code.

- **Every caller routes findings through `assign_ids`** before storage, so a
  finding cannot reach the ledger without a server-assigned id.
- **`'reviewing'` is unreachable for a critic** — verification never wraps a job
  that has a `parent_job_id`.
- **`'paused'` must stay in the stale-sweeper's reap list.** The LLM-outage
  exemption clause tests `j.status = 'paused'` and only sees rows that already
  passed the top-level status filter; removing `'paused'` makes the exemption
  unreachable dead code.
- **`max_verification_rounds` was deliberately retained** on the critic's
  context stamp while the legacy round-cap branch still existed; deleting it
  would have made that branch fall back to a hardcoded cap of 3, ignoring a
  target configured with `0` (unlimited).
- **The non-terminal-status pre-gate before `_resolve_critic_outcome` is
  required.** Without it, every critic that merely *paused* for an LLM-outage or
  memory retry would escalate its target, breaking existing outage resilience.
- **The idempotent-retry short-circuit must precede validation.** A retry
  validated against a ledger already containing its own findings would be asked
  to disposition findings it had just opened, and 409.
- **`deep_merge` merges dicts by key and replaces lists.** A `config_override`
  that sets `tools.evaluation` *adds* a group and narrows nothing — this is how
  the critic kept `job_complete` and inherited `send_message`. Groups must be
  spelled out explicitly to remove tools.
- **`feedback.md` is excluded from the content hash because it is the round's
  *input*, not its output** — explicitly **not** because it is undelivered. It
  *is* delivered to the user (unlike `archive/`, it is not in
  `SYNC_IGNORE_PATTERNS`). Reading the exclusion list as "the undelivered files"
  would licence excluding real deliverables.

## Related

- `docs/issues/verification_round_reset_spawns_blind_critic.md` — the incident
  and the design rationale.
- Still unfixed, filed separately:
  `docs/issues/drain_freeze_overwrites_critic_verdict.md`,
  `docs/issues/job_finalization_decisions_held_only_in_process_memory.md`,
  `docs/issues/approving_a_critic_wedges_target_in_reviewing.md`,
  `docs/issues/jsonb_isinstance_guard_without_parse_silent_dead_paths.md`.
