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

### Live gate — run 2026-07-29/30 on dev. Partial pass.

Two jobs were driven through the deployed code:
`7d67d684-633d-4688-9d20-60cb8d7b0a1e` (approved on round 1) and
`6df02f64-b4d7-477e-877d-ba570610d54d` (a fixture engineered to be returned).

**Confirmed working in production:**

- **The critic receives its rendered brief.** Read directly from the critic's
  workspace: `instructions.md` is the full template — target job, deliverables,
  agent summary and confidence, and an `## Open Findings From Previous Rounds`
  section. This had been rendered-and-discarded on every critic since the
  orchestrator migration and had never once been observed working. Every
  downstream correction is visibly present in the text the critic actually read:
  the `findings`/`dispositions` signatures, "you cannot close a finding by
  re-judging it", the "not the same thing as being the first round" wording, and
  "Returning is honoured at ANY severity".
- **The round-recording endpoint works from a real agent pod.** The critic's
  freeze reads `round: 1, verdict: approved, open_findings: [], freeze_type:
  verdict` — a shape that only exists if the agent called the internal endpoint,
  authenticated, received a *computed* verdict and stored the server's value.
- **`content_tree` is captured on real freezes:** `257a850e45be…` and
  `6417fe445bb7…` on the two jobs.
- **Live corroboration of the original defect:** the first job's freeze records
  `head_commit: 3c1757a8`, which is the commit *before* its own "Job completed"
  commit — exactly the pre-commit capture that made the first guard inert, and
  confirmation that abandoning that field was right.

**Measured 2026-08-01 — the guard has zero sensitivity on a verification
round.** This was the open item that made the run a partial pass, and the
answer is worse than the worry. See "Cross-round `content_tree`" below.

**Two new defects found by the live run**, both filed separately, neither
caused by this change:

- `docs/issues/resumed_job_inherits_subjob_git_branch.md` — reported from
  this gate as an `edit_file` append that silently does not survive the
  job-completed → feedback-resume boundary. Since root-caused to something
  wider, and **fixed** (`ensure_job_branch`, `src/agent.py`): a job resumed onto
  a workspace a subjob last occupied keeps the *subjob's* branch, so everything
  it writes afterwards is invisible on `main`. Not append-specific and not
  `edit_file`-specific — see that file for the corrected framing. The fixture's
  one-paragraph fix never landed on the ref the reviewer reads; the reviewer
  correctly kept reporting it missing. This defeats the remediation path
  generally, not just for verification.
- `docs/issues/critic_brief_lands_in_shared_workspace_and_misleads_target.md` —
  the critic inherits the parent's workspace, so its brief is written as
  `instructions.md` into the root the *target* reads from. The target then
  believes it is the reviewer and tries to call verdict tools it does not have.
  Note the shape of this one: fixing brief delivery is what created the
  exposure.

**Confirmed 2026-08-01** (was "inferred, not confirmed"): job 2 escalated on
the **round-limit** branch, not the no-progress branch.
`error_message = "Round limit reached (3) with 2 finding(s) still open
(F1, F2)."`, `status = pending_review`, and its ledger holds three rounds each
with `verdict: returned`. The no-progress branch never fired — for the reason
below.

### Cross-round `content_tree` — measured 2026-08-01, and the guard is inert again

Read from job 2's ledger, which recorded three rounds:

| Round | `content_tree` | `head_commit` |
|---|---|---|
| 1 | `cd0c89c4…` | `09cb3153` |
| 2 | `b4a0b3b8…` | `813c632f` |
| 3 | `037c0bbf…` | `89cbcd62` |

Three rounds, three different hashes, guard silent throughout.

**Rounds 2→3 cannot be used as evidence.** `813c632f` is on
`subjob/50dee4ae/critic` and `89cbcd62` on `subjob/4469a364/critic` — divergent
branches, per the branch-inheritance defect. A diff across them conflates branch
divergence with round progress. Discarded.

**Round 1→2 is a clean linear descent** and is decisive. Diffing the two freeze
commits with the four `NON_DELIVERABLE_PATHS` excluded — i.e. exactly what the
guard hashes — twelve paths moved in a single round:

| Path | Written by | Changes every round? |
|---|---|---|
| `instructions.md` | the critic's brief, re-rendered per round | yes |
| `output/critic_verdict.json` | critic | yes |
| `output/verification_report.json` | critic | yes |
| `output/verification_report_round_N.json` | critic, N in the name | yes |
| `tools/{approve_job,return_job_with_feedback,shell_execute,spawn_subagent}.md` | critic's tool docs | yes |
| `knowledge/<per-round-note>.md` | KB note per round | yes |
| `plan.md` | agent bookkeeping | yes |
| `task_brief.md` | agent bookkeeping | yes |
| `output/glossary.md` | **the deliverable** | only on real progress |

Eleven of the twelve move whether or not the worker did anything. So on a
verification round the tree is **guaranteed** to move: the critic writes into
the same tree the guard hashes, so it moves the hash by existing. The guard
cannot fire, ever, on the only path it was built for.

It was silent on job 2 for the right reason by luck — `output/glossary.md` did
change each round — but its sensitivity is zero regardless.

Note the two fixes interlock. `instructions.md`, `output/critic_verdict.json`,
`output/verification_report*.json` and `tools/*.md` are in the target's tree
**only** because of the shared workspace
(`critic_brief_lands_in_shared_workspace_and_misleads_target.md`). Giving the
critic its own workspace removes those. It does not remove `plan.md`,
`task_brief.md`, `knowledge/` or `workspace.md`, which are enough on their own
to keep the guard inert.

### Required fix (was a standing recommendation)

**Invert the denylist to an allowlist** — hash `output/` minus the two
completion files, and nothing else. This was written up as *"if a fifth
tree-mover ever appears, do not extend the denylist"*. Eight have now been
observed in three classes, so the trigger has fired. An allowlist makes a newly
added bookkeeping path default to *ignored* rather than to silently breaking the
guard, and it is robust whether or not the critic ever gets its own workspace.

Five corrections to a denylist whose failure mode is silent is the signal to
change the approach, not to extend the list.

### Live gate — re-run 2026-08-01 on `sha-f41970a`. The gate held; the fixture died around it.

Deployed image contains both the rewrite (`928ed60b`) and the branch fix
(`e71f343c`). Target `40efbb39-0890-40fa-a464-6e3d6bd92832`, critic
`245889ac-6d5b-4771-bd5a-5f47fd1b7e31`. Same fixture text and overrides as the
first run.

**The headline goal — observing the loop converge — was not reached, and the
reason has nothing to do with verification.** Zero rounds were recorded.

**What the gate proved, and this is the valuable part:** the fail-closed
property held under a case no test covers. The critic submitted
`return_job_with_feedback` with `findings: "[]"`. The tool rejected it:

```
Error: the verdict was NOT recorded and must be corrected and resubmitted.
verdict rejected:
- Cannot return a job with no findings: `opened` is empty and no findings from
  previous rounds are open. If the deliverable has a problem, describe it as a
  finding in `opened`.
```

Nothing recorded, nothing silently approved, and the rejection names the
correction. `validate_verdict_call` did precisely its job — including the
`open_before` carve-out, which is why this is a rejection of a genuinely
inconsistent call rather than of the common round-2 shape.

**Three defects found, all filed, none in the verification path:**

- `git_push_fails_silently_via_workspace_backend.md` — **the root cause of the
  whole run.** Every `git push` failed for the target's entire 72-minute life,
  26 times, each logging `git push failed: ` with an *empty* reason
  (`_parse_shell_run_output` hardcodes `stderr=""` on the workspace-backend
  path). Gitea holds only `Initial commit`. The job still reported
  `job_complete` at confidence 1.0, then the pod was reclaimed. The critic
  reviewed an empty repository and was right to.
- `rejected_verdict_livelocks_critic_and_wedges_parent.md` — the critic could
  not convert "deliverable absent" into a structured finding, resubmitted the
  same invalid call 5 times, and livelocked: 189 iterations, 41 commits cycling
  two strategic todos, 15 shells, 105 minutes, parent pinned in `reviewing`
  throughout. **`_UNSTICK_REVIEWING_SQL` cannot fire on a live critic** — it
  covers a critic that *died*, not one that will never finish. Cancelled
  manually.
- **Fixture defect, for whoever runs this next:** the target executed *both*
  staged rounds in one pass — its completion summary describes writing the
  glossary *and* appending `## Sources` (Vogels 2009, Fielding 2000) before ever
  completing once. Staged instructions that say "on your first completion omit
  X; when returned, add X" are read as a script to run internally, not as two
  rounds. **A convergence fixture must make round 1's gap something the agent
  cannot close without a real return** — e.g. withhold a fact it must be *given*
  in feedback, rather than asking it to pretend not to know something.

**Still unmeasured after two attempts:** the loop converging (return → fix →
approve). Both runs died before round 2 for unrelated infrastructure reasons.
Fix the silent-push defect first — a third attempt without it will fail the same
way.

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
