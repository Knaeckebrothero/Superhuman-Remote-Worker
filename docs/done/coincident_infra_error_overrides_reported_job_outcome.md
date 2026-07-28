---
tags:
  - done
  - jobs
  - orchestrator
  - completion
  - agent-lifecycle
  - workspace
  - vm
  - self-improvement-loop
  - status-integrity
---

# A coincident infrastructure / teardown error overrides the agent's reported job outcome

**Filed:** 2026-07-12, while triaging three "failed" jobs the user flagged on the
Better-Resavio ERP self-improvement loop (project `68137e29`, main cluster).
**Status:** ✅ **RESOLVED — shipped to `develop` 2026-07-12** (commits `8a561f94`
Slices B+C, `254bf2a3` Phase 3 idempotency, `2831202a` Phase 5 Part 1 clone
retry). Slice A was already fixed earlier (`4ff91c7c`). Verified: 346 unit tests
+ a live k3d endpoint drill (see "Implementation roadmap → Phase 4"). Deferred
follow-ups (non-blocking): live-observe the loop after rollout, the full live
drain/teardown drill, and Phase 5 Part 2 — tracked in
`tests/coincident_infra_error_test_coverage.md`. Symbols/line numbers current as
of this date against `orchestrator/services/completion.py`.

> ⚠️ **RESOLVED ≠ class closed. A fourth face was observed live on 2026-07-27**
> (job `e1192a9d`, same project `68137e29`) and is **still open** — tracked in
> `docs/issues/transient_db_error_hard_fails_job_and_destroys_vm.md` (Defect 2).
> Every fix below lives *inside* `determine_job_status`, which is **downstream of
> a gate that can close first**: when something marks the job terminal
> **out-of-band before the agent reports** (there, a Postgres disk-full handler
> writing `status=failed`), `POST /api/jobs/{id}/complete` is rejected at
> `orchestrator/main.py:14248` with `400 {"detail":"Job cannot be completed
> (status: failed)"}`. The report — carrying a `freeze_type=job_complete` freeze —
> never reaches the carve-outs, so `ERROR_IMMUNE_FREEZE_TYPES` cannot help and a
> fully successful job stays `failed` silently. Detection: the freeze artifact is
> the truth, not the DB — `jobs.freeze_data` may be NULL while
> `output/job_frozen.json` in the Gitea jobs repo holds a real `job_complete`
> freeze. Worse variant: if the out-of-band failure also tears down the VM, the
> agent cannot write `job_frozen.json` at all and the artifact must be
> reconstructed before the status can be repaired.

## TL;DR

`determine_job_status` (`orchestrator/services/completion.py:519`) is the single
authority that maps an agent's completion report → DB status. It has a structural
bug: **a coincident `error` in the report can override the outcome the agent
actually reported** — a clean completion or a re-dispatchable freeze — and hard-fail
or mis-route a job that in fact succeeded.

This is *one* weakness with *three* faces:

| Slice | Shape | Result | Status |
|---|---|---|---|
| **A** | top-level job + auto-redispatch freeze (`version_upgrade`/outage) + coincident interrupt error | should pause+re-dispatch; used to `failed` | **FIXED** `4ff91c7c` (`ERROR_IMMUNE_FREEZE_TYPES`, guarded error short-circuit) |
| **B** | **sub**job + drain/outage freeze | `pending_review` instead of pause+re-dispatch | **FIXED** `8a561f94` (`version_upgrade` subjob → `paused`/terminal via `parent_status`) |
| **C** | top-level job + `job_complete` (work done **and merged**) + coincident teardown error | `failed` — overwrites an already-successful completion | **FIXED** `8a561f94` + `254bf2a3` (teardown carve-out + idempotency backstop) |

Slice A's fix (`docs/done/version_upgrade_drain_masked_by_coincident_error.md`)
hoisted freeze-resolution above the `if error` short-circuit but scoped the carve-out
narrowly — `should_stop AND parent_job_id IS NULL AND freeze_type ∈ ERROR_IMMUNE_FREEZE_TYPES`.
That scope leaves B (subjobs) and C (`job_complete`) exposed. All three were observed
live on 2026-07-12.

**This is not a regression from the `runner_kind` / autonomy fix**
(`docs/done/critic_verification_subjobs_fail_systemically.md`, deployed `sha-2a71df3`
on 07-11 13:01Z). That fix eliminated its target class (autonomy-ceiling GrantDenied:
zero new occurrences since deploy — see "Is this a regression?" below). What it *did*
change — Finding #3, surfacing `error_message` in the API/MCP/Cockpit — is why these
long-standing infra failures are now **visible** on failed rows for the first time,
which reads as "more issues" when it is actually "more legible issues."

## Incidents (2026-07-12, main cluster)

| Job | Role | Audits | `merge_status` | `completed_at` | Final status | `error_message` | Slice |
|---|---|---|---|---|---|---|---|
| `e15fab1f` | Loop iter-4 SCHOLAR | 1445 | **merged** | **14:54:11** set | `failed` | `Failed to connect to workspace 100.64.24.193:22 after 2 attempt(s) [timeout]` | **C** |
| `57be4c22` | Loop iter-3 DEVELOPER | 1603 | **merged** | **12:28:36** set | `failed` | `Workspace I/O timed out stat /home/agent-host/workspace/plan.md` | **C** |
| `da9d5917` | SCHOLAR subjob (research phase of `73e68890`) | 227 | grafted | — | `pending_review` | (none — drain freeze) | **B** |
| `73e68890` | designer (parent of `da9d5917`) | 0 | — | — | `failed` | `Failed to clone project jobs repo 'project-68137e29-jobs' …` | infra (see "Infra layer") |

`e15fab1f` and `57be4c22` are the clean proof of Slice C: **the work was merged and
`completed_at` stamped** (the completion side-effects ran), and `freeze_data.status`
is `job_completed` / a partial-done note — yet the row's final `status` is `failed`
because the same report carried a trailing teardown error and the `if error`
short-circuit fired before the completion branch. Both jobs' VMs show
`context.vm.status = deleted`: the VM was reaped on completion, and a trailing
workspace op (SSH reconnect / `stat plan.md`) then timed out against the gone VM.

## Slice B — drained subjob lands in `pending_review` instead of re-dispatch

`da9d5917` is the pre-job research scholar for `73e68890`. It ran to a phase
boundary, observed orchestrator drain intent during the 09:06Z image rollout, and
froze cleanly: `freeze_data = {reason: "orchestrator drain intent at phase boundary",
freeze_type: "version_upgrade", phase: strategic, phase_number: 0}`
(`src/graph.py:3291-3320`). For a top-level job that maps to `paused` + auto-redispatch
(`completion.py:638`). But the **subjob short-circuit fires first**
(`completion.py:576`):

```python
if job.get("parent_job_id") is not None:
    fd_status = fd.get("status")
    if fd_status:
        ...
        return (fd_status, None)
    # No explicit status in freeze_data — infer from goal_achieved
    return ("completed" if goal_achieved else "pending_review", None)   # line 589
```

The drain freeze has `freeze_type=version_upgrade` but **no `status` key**, and
`goal_achieved` is false (drained mid-research), so it falls to line 589 →
`pending_review`. The `version_upgrade → paused` branch 60 lines below is
**unreachable for anything with a parent**. This is the "drained-critic→pending_review"
gap explicitly scoped **out** in the Slice A fix
(`version_upgrade_drain_masked_by_coincident_error.md`, closing paragraph). Confirmed,
still open. Applies to the whole re-dispatchable/outage family for subjobs
(`version_upgrade`, `llm_unavailable`, `memory_unavailable`, `kb_unavailable`), not
just `version_upgrade`.

## Slice C — a teardown error overwrites a completed + merged job

The error short-circuit (`completion.py:546-562`):

```python
if error:
    ...
    if not (
        should_stop
        and job.get("parent_job_id") is None
        and freeze_type in ERROR_IMMUNE_FREEZE_TYPES   # {version_upgrade, memory_unavailable,
    ):                                                 #  kb_unavailable, workspace_upgrade_required,
        return ("failed", error_msg)                   #  llm_unavailable}
```

`ERROR_IMMUNE_FREEZE_TYPES` (`completion.py:291`) does **not** include `job_complete`.
So when a successful completion report also carries a trailing infra error — the
agent's success path ships a `final_state` with both a `job_completed` freeze and a
residual `error` (cf. `dual_app.py` success path, described in the Slice A memory) —
the short-circuit returns `("failed", …)` and never reaches the `is_completion`
branch (`completion.py:594-623`) that would have returned `completed`.

The completion **side-effects still run** (branch merge, `completed_at` stamp), which
is why `e15fab1f`/`57be4c22` show `merged` + `completed_at` set + `status=failed`
simultaneously — an internally contradictory row. For a **loop** job this is doubly
wrong: the loop-mapping logic (`completion.py:613-620`, `717`) deliberately routes a
loop job that stops to `completed` (weak-but-honest; the loop advances on its KB
contributions) rather than a human-review gate — but the error short-circuit fires
*above* that logic, so a loop iteration that fully succeeded is counted as a failure
and bumps `consecutive_failures`.

**Work is usually not lost** (the merge landed; KB notes are written live during the
run, not at teardown) — but the status is corrupted, the operator is misled, and the
loop's failure accounting is poisoned.

## Is this a regression from the `runner_kind` fix? No — the data

Distribution of the **30 most-recent failed jobs** (2026-07-07 → 07-12), by root
cause:

- **Autonomy-ceiling GrantDenied (the class the fix targeted): 4** — `8a83c770`,
  `ced43d22`, `08d284df`, `df4c56c6`. **Every one created before the fix image
  deployed (07-11 13:01Z).** The stragglers are pre-fix *zombie* jobs: their row has
  `runner_kind=user` baked in at creation, so re-dispatching them today still denies.
  New post-fix subjobs got `runner_kind=lifecycle` and ran clean (`da9d5917`,
  `8bba428b`). **Zero new occurrences since deploy → the fix works.**
- **VM / workspace connectivity (SSH timeout, I/O timeout, VM "gone"): 5** —
  `e15fab1f`, `57be4c22` (07-12) plus `779bc57c`, `eff73664` (07-09), `f0b6f263`
  (07-07). **Predates the fix by days.**
- **Jobs-repo clone failure: 5** — 07-08 → 07-12. Predates the fix.
- Others: VM provisioning 409/exhausted (3), product-qa brace `KeyError` (6),
  gpt-5.5 quota cooldown (4), embedding-unavailable (1), vm_upgrade_expired (1),
  subjob-inherits-dead-parent (1).

The failures hurting the loop *now* are VM/git **infrastructure** flakiness, a class
firing since at least 07-07 and untouched by the `runner_kind` change. The perception
of "more issues than before" is driven by Finding #3's `error_message` surfacing:
reasons that were always in the DB but blank in the UI are now printed on every failed
row. **Recommendation to validate quantitatively:** categorize `error_message` for all
failed jobs bucketed by day (before/after 07-11 13:01Z) to get a hard rate, not a
30-row read.

## The infra layer (why coincident errors keep arriving)

The status-resolver weakness only *matters* because transient infra errors keep
arriving at completion time. Two recurring generators:

1. **Post-completion VM teardown race** (Slice C's trigger). The VM is reaped
   (`context.vm.status=deleted`) on completion, but a trailing workspace op — SSH
   reconnect, `stat plan.md` during archive/diff, checkpoint flush — races the
   deletion and times out. The exact caller that issues the trailing op after the VM
   is gone is **not yet pinned** (open question — likely orchestrator-side finalization
   diff/archive read, or a second `/complete`-adjacent teardown handler). Relevant:
   `docs/issues/agent_fast_freeze_on_dead_workspace.md`,
   `docs/issues/reviewing_parent_pod_reaped_under_critic.md`.
2. **Jobs-repo clone with no retry** (`73e68890`). `WorkspaceManager.initialize_project_workspace`
   (`src/core/workspace.py:412-429`) does a single `GitManager.clone`; on `None` it
   correctly refuses a disconnected `git init` fallback (F29 hardening — avoids losing
   work) but does **not** retry, so a transient reachability blip (e.g. during the
   09:06Z rollout, when the parent started its own workspace 5 s after the scholar
   drained and the pod was being torn down) hard-fails the job. Other jobs in the same
   project cloned the same repo fine before and after, confirming the blip was
   transient. Related: `docs/issues/gitmanager_local_git_fallback.md`.

## Proposed fix

### Primary — make the reported outcome win over a coincident infra/teardown error

Restructure `determine_job_status` so the agent's **reported outcome** (a genuine
completion, or a re-dispatchable/outage freeze) is resolved **before** the `if error`
short-circuit and **before** the `parent_job_id` short-circuit — the same move Slice A
already applied to the error branch, extended to cover completions and subjobs.
Concretely:

- **Slice C.** When the report is a completion (`freeze_type == "job_complete"` or
  `fd.status == "job_completed"` or `goal_achieved`) **and** the completion
  side-effects succeeded (branch merged / `completed_at` about to be stamped), a
  coincident **infra/teardown-class** error must not flip it to `failed`. Resolve via
  the `is_completion` branch (→ `completed`/`reviewing`/loop-mapping). Add a
  teardown-error classifier (SSH connect/timeout, workspace I/O timeout, "workspace
  gone") so a *genuine* mid-run crash still fails.
- **Slice B.** Inside (or above) the `parent_job_id` branch, if
  `freeze_type ∈ AUTO_REDISPATCH_FREEZE_TYPES`, return `paused` so the subjob
  re-dispatches like a top-level job. The plumbing already supports it: the dispatcher
  re-picks `paused` subjobs (`get_dispatchable_jobs`, `postgres.py:3313`, needs
  `freeze_data IS NULL` — shed on the pause path), the agent-side auto-continue
  resume-clear (`src/agent.py` `_AUTO_CONTINUE_FREEZE_TYPES`) is parent-agnostic, and
  `resolve-at-dispatch` (`5a6f5a49`) re-resolves the parent workspace. **Guard the
  parent-terminal case**: if the parent is `failed`/`cancelled`/`paused`, the
  dispatcher's cascade guard will never re-dispatch the subjob, so `paused` would be a
  silent wedge — map those to a terminal status (`cancelled`/`completed`) instead.

### Defense-in-depth — terminal-status idempotency

Independent of the resolver refactor: once a job has succeeded (`completed_at` set /
branch merged / status terminal-success), a later completion report or teardown error
**must not** transition it to `failed`. This protects the invariant regardless of
which trailing op raised the error, and is the smallest guard that would have saved
`e15fab1f`/`57be4c22`.

### Related infra hardening (separate, larger track — not blockers)

- **Clone retry** with backoff in `initialize_project_workspace`; classify
  transient-vs-permanent so F29's "don't `git init`" guarantee is preserved.
- **Don't touch the workspace after VM delete** — sequence teardown so no workspace op
  is issued once the VM is scheduled for reaping (fixes the Slice C *trigger* at
  source). Reconcile with `agent_fast_freeze_on_dead_workspace.md`.

### Follow-up question (out of scope here)

Should short-lived **lifecycle** subjobs (pre-job scholar, critic) be drain-*eligible*
mid-flight at all, or should the drain reconciler let them finish since they're
bounded? That would prevent Slice B at source, but it's a drain-policy change keyed on
`runner_kind` — riskier and broader. The completion-routing layer should be robust
regardless.

## Implementation roadmap

**As-built (2026-07-12, uncommitted):** Phases 0-3 IMPLEMENTED + unit-verified.
`orchestrator/services/completion.py` gained `is_teardown_infra_error()` +
`_TEARDOWN_ERROR_PATTERNS` + `_PARENT_TERMINAL_BLOCKING`; `determine_job_status`
now (a) hoists `is_completion`, (b) **Phase 3** — idempotency backstop: a row that
is already `status='completed'` or `merge_status='merged'` returns `(None, None)`
on any error report instead of being downgraded to `failed`, (c) **Slice C** —
widens the error carve-out so a completion whose only error is a teardown blip
keeps its outcome, (d) **Slice B** — re-routes a drain-frozen subjob to
`paused`/terminal via a new `parent_status` kwarg. `orchestrator/main.py:complete_job`
fetches the parent status and passes it. 16 tests in
`tests/test_drain_intent.py::TestCoincidentInfraErrorOverride`. **Verify:** 288
tests green across drain + completion + loop + llm + delegation suites; `ruff check`
+ `ruff format --check` clean; `main.py` compiles. Placement note: Phase 3 landed in
the pure resolver (not the handler as first sketched) — returning `(None, None)`
cleanly no-ops the write and all downstream side-effects, and is unit-testable.

**Phase 4 (k3d endpoint drill) — PASSED 2026-07-12.** Uncommitted code confirmed
synced into the live orchestrator pod (Tilt); real `POST /api/jobs/{id}/complete`
against real Postgres, disposable jobs, all with HTTP 200:
- **C1** completion (`job_completed`) + verbatim SSH-timeout teardown error →
  `completed` (was `failed`). The e15fab1f fix, end-to-end.
- **C2** completion + a real `AssertionError` → `failed`. Carve-out stays narrow.
- **P3** already-`completed` row + late teardown error → stays `completed` (Phase 3).
- **B1** drained subjob (`version_upgrade`) under a live parent → `paused`, and its
  `freeze_data` was shed to NULL. The da9d5917 fix (was `pending_review`).
- **B2** drained subjob under a `failed` parent → `cancelled` (parent-terminal guard).
- Dispatchability check: a paused subjob (freeze NULL, agent NULL) under a
  `processing` parent satisfies the `get_dispatchable_jobs` predicate
  (`base_ok ∧ cascade_ok`) → it genuinely re-dispatches, not a silent wedge.
Drill script: `scratchpad/k3d_drill.sh`. NOT covered (accepted): a full live
drain-mid-subjob and a real VM-teardown race — the *decision* logic is proven above
and the re-dispatch/resume *mechanism* (dispatcher re-pick, resume-clear,
resolve-at-dispatch) is pre-existing code this change does not touch.

**Phase 5 (infra hardening) — Part 1 DONE, Part 2 DEFERRED (2026-07-12).**
- *Part 1 — clone retry:* `src/core/workspace.py` gained `_clone_repo_with_retry`
  (`_CLONE_ATTEMPTS=3`, backoff `(2s, 5s)`, read at call time so tests stub to 0);
  the jobs-repo clone in `initialize_project_workspace` routes through it. F29 hard-
  fail preserved (fires only after retries exhaust). Kills the 73e68890-class
  single-shot clone failure. Tests: `test_workspace_git.py` — updated F29 test
  asserts `_CLONE_ATTEMPTS` calls before raising + new
  `test_project_jobs_clone_succeeds_after_transient_failure`; 17 green, ruff clean.
- *Part 2 — no workspace op after VM delete:* **deferred, deliberately.** Phases
  0-4 already made this trigger *harmless* (the trailing teardown error no longer
  fails the job), so it's now cosmetic (log noise), and the exact trailing-op
  caller is still unpinned (see "The infra layer" §1). Implementing it blind risks
  a regression for no correctness gain. Separate follow-up: pin the caller, then
  sequence teardown so no workspace op is issued once the VM is scheduled for
  reaping. Reconcile with `agent_fast_freeze_on_dead_workspace.md`.

**Remaining:** Phase 6 (commit + deploy + observe); Phase 5 Part 2 follow-up.

Sequenced so every phase is independently verifiable and the risky part (subjob
re-dispatch, teardown races) is gated behind a live drill, not just unit tests.
`determine_job_status` is a pure function on `(job, result)` → phases 0-2 are cheap
and fully unit-testable.

### The one decision that needs sign-off first

**Where is the line between "cleanup hiccup, ignore it" and "real failure, honor
it"?** Everything else is mechanical. Proposed conservative default: an error is
*teardown/infra-class* (ignorable when the agent also reported success or a
re-dispatchable freeze) **only** if it matches a known connectivity/teardown pattern —
SSH connect/timeout, key-exchange timeout, workspace I/O timeout, "workspace gone" /
name-not-resolvable. Anything else stays a genuine failure. Start narrow; widen only
on evidence. This lives in the Phase 1 classifier and is the only judgement call.

### Phase 0 — Pin the scope with failing regression tests (TDD)

Encode the four incidents + the guardrail negatives as fixtures, red against current
code. Extend `tests/test_drain_intent.py` (freeze/redispatch) and the completion
suites (`tests/test_completion_endpoint.py`, `tests/test_complete_job_endpoint.py`).

Invariants to assert:
- **C1** report `{should_stop, freeze.status=job_completed, error=<ssh-timeout>}` → `completed` (not `failed`).
- **C2** same but `is_loop_job` → `completed` (loop advances).
- **C3** genuine mid-run crash `{error=<non-teardown>, should_stop=False}` → still `failed`.
- **B1** subjob `{parent alive, freeze_type=version_upgrade, no fd.status, goal_achieved=False}` → `paused`.
- **B2** same but parent `failed`/`cancelled` → terminal (`cancelled`/`completed`), **never** a wedged `paused`.
- **N1** normal completion, no error → `completed`/`reviewing` unchanged.
- **N2** critic `fd.status=approved`→`completed`, `returned`→`waiting` unchanged.

### Phase 1 — Teardown-error classifier (pure helper)

`is_teardown_infra_error(msg) -> bool` in `completion.py`, matching the Phase-0
decision's patterns, unit-tested against the *verbatim* incident strings
(`e15fab1f`, `57be4c22`, `779bc57c`, `eff73664`, `f0b6f263`). Log at INFO when it
fires so the classifier's real-world hit set is auditable.

### Phase 2 — Restructure `determine_job_status` (Slices B + C)

Resolve the **reported outcome before** the `if error` short-circuit and the
`parent_job_id` short-circuit — generalizing the Slice A restructure. Extract a helper
`resolve_reported_outcome(job, result, fd, freeze_type) -> str | None` that returns a
status when the agent reported a definitive outcome, else `None` (fall through to
today's logic). Decision cascade:

1. If `should_stop` and the report is a **completion** (`freeze_type=="job_complete"`
   or `fd.status=="job_completed"` or `goal_achieved`):
   - no error, or `is_teardown_infra_error(error)` → route to the completion branch
     (`is_completion` → `completed`/`reviewing`/loop-mapping).
   - non-teardown error → `None` (fall through → `failed`).
2. If `should_stop` and `freeze_type ∈ AUTO_REDISPATCH_FREEZE_TYPES` (∪ `llm_unavailable`):
   - parent **not** terminal → `paused` (works for top-level *and* subjob; the
     dispatcher, freeze-shed, resume-clear, and resolve-at-dispatch already handle
     subjobs — see "Proposed fix").
   - parent terminal (`failed`/`cancelled`/`paused`) → terminal status (avoid the
     cascade-guard wedge).
3. Else `None` → unchanged behaviour (critic `fd.status`, genuine-crash `failed`, etc.).

Keep the existing branches intact below the helper so non-matching cases are provably
unchanged (N1/N2).

### Phase 3 — Terminal-status idempotency (backstop, in the `/complete` handler)

Independent of Phase 2, guard the write site in `orchestrator/main.py` (`/complete`
handler, status update ~`main.py:9846`; graft/merge at ~`main.py:477-534`): a job whose
row is already terminal-success (`status` terminal, `completed_at` set, or
`merge_status='merged'`) must **not** be transitioned to `failed` by a later report /
teardown error — log and no-op the downgrade. Phase 2 fixes the single-report
dual-payload case; Phase 3 covers the two-report / late-error case regardless of which
trailing op raised the error. (Confirm during impl whether the graft/merge runs before
or after `determine_job_status` in the handler — it decides whether the guard can also
read `merge_status` inside the resolver.)

### Phase 4 — Verify on k3d (the gate)

Unit + lint green is necessary, not sufficient — re-dispatch and teardown races only
show up live:
- **Slice C drill.** VM-backed loop job → completion; force a trailing workspace op to
  race VM teardown (or inject an SSH timeout at teardown via the /complete curl drill
  inside the orchestrator pod). Expect `completed`, `merge_status=merged`, no `failed`.
- **Slice B drill.** Dispatch a scholar research subjob; set `intents.should_drain` on
  its busy agent; expect `paused` → re-dispatch onto a fresh pod → resume (not
  `pending_review`, not a re-freeze livelock). Parent-terminal variant: fail the parent
  first, expect terminal not wedged-`paused`.
- **Regression.** Normal completion, critic approve/return, genuine crash → unchanged.
- Recipe: craft double-payload `/complete` bodies (`error` + `job_completed` freeze)
  and curl them inside the orchestrator pod (`MCP_INTERNAL_KEY` from pod env, DSN via
  `build_postgres_url`) — same drill as `version_upgrade_drain_masked_by_coincident_error.md`.

### Phase 5 — Infra track (separate, non-blocking; lowers the error *rate*)

Phases 2-3 make coincident errors *harmless*; these make them *rarer*:
- Clone retry with backoff in `initialize_project_workspace` (`src/core/workspace.py:412`),
  preserving the F29 "no disconnected `git init`" guarantee (classify transient vs
  permanent). See `docs/issues/gitmanager_local_git_fallback.md`.
- Teardown sequencing: issue no workspace op once a VM is scheduled for reaping —
  removes the Slice C trigger at source. Reconcile with
  `docs/issues/agent_fast_freeze_on_dead_workspace.md`.

### Phase 6 — Deploy, observe, optional reconciliation

Ship via the normal `develop` pipeline; watch the loop for (a) no more
`merged`+`failed` contradictory rows, (b) no drained subjobs stranded in
`pending_review`. Optional one-time cosmetic reconciliation of existing contradictory
rows (`merged`+`failed` → `completed`) — decide if worth it; the work already landed,
only the status label is wrong.

**Order of value:** Phase 0-2 + Phase 3 retire Slices B and C (the demoralizing part)
and are low-risk/high-leverage — do them first. Phase 5 is the longer infra game and
can trail. The whole of 0-4 is roughly the "~1h code + half-day with the k3d drill"
estimate discussed, plus the Slice C branch.

## Impact

- Loop iterations that fully succeeded (`e15fab1f`, `57be4c22`) are recorded as
  `failed`, wasting the iteration in the operator's view and bumping
  `consecutive_failures` — a burst could trip the loop's failure cap even though every
  iteration actually landed its work. (On 07-12 the loop still self-healed: iter-5
  critic `5d7a3d4c` dispatched normally.)
- Legitimate research subjobs (`da9d5917`) strand on the human-review gate instead of
  resuming on a fresh pod.
- Operators lose trust ("everything's failing") precisely because failures are now
  legible — the fix that improved visibility gets blamed for the pre-existing
  fragility it exposed.

## Related

- `docs/done/version_upgrade_drain_masked_by_coincident_error.md` — Slice A (fixed);
  the precedent restructure this doc generalizes.
- `docs/done/critic_verification_subjobs_fail_systemically.md` — the `runner_kind`
  autonomy fix; **not** the cause here (Finding #3 raised visibility).
- `docs/issues/version_upgrade_drain_livelock.md` — the resume side of drain
  re-dispatch (`should_stop` checkpoint clearing); Slice B re-dispatch depends on it.
- `docs/issues/session_turn_hard_fails_on_transient_llm_outage.md` — sibling
  "hard-fail on transient" pattern in the session path.
- `docs/issues/agent_fast_freeze_on_dead_workspace.md`,
  `docs/issues/reviewing_parent_pod_reaped_under_critic.md` — the workspace/VM reaping
  layer behind the Slice C trigger.
- `docs/issues/gitmanager_local_git_fallback.md` — the clone-fallback hardening.

## Appendix — forensics recipe

Confirm the contradiction on any suspect "failed" job (MCP formatters hide most of
this; go to the row):

```sql
SELECT id, status, merge_status, completed_at, error_message,
       freeze_data->>'status'   AS freeze_status,
       freeze_data->>'freeze_type' AS freeze_type,
       context->'vm'->>'status' AS vm_status,
       parent_job_id
FROM jobs
WHERE id IN ('e15fab1f-9898-44f1-8557-66c183807c9c',
             '57be4c22-c669-46dc-9af7-0df4ad1561e6',
             'da9d5917-a84e-4564-8511-47ab84b328b6');
```

Tell-tale for Slice C: `status='failed'` **with** `merge_status='merged'` /
`completed_at` non-null / `freeze_status='job_completed'` and an infra-class
`error_message`. Tell-tale for Slice B: `parent_job_id` non-null,
`status='pending_review'`, `freeze_data.freeze_type ∈ {version_upgrade,
llm_unavailable, memory_unavailable, kb_unavailable}`, no `freeze_data.status`.

`get_frozen_job` can synthesize a `version_upgrade` freeze that contradicts the row —
**always confirm against the `jobs` row** (`freeze_data`, `error_message`) before
pinning a cause (lesson from `version_upgrade_drain_masked_by_coincident_error.md`).
