# Coincident-infra-error / reported-outcome fix — test coverage map

Companion to `knowledge-history/done/coincident_infra_error_overrides_reported_job_outcome.md`.
Records what is verified, by which mechanism, and — the point of this file —
**what could not be covered yet**, why, and how to close each gap. Last updated
2026-07-12 (after shipping Slices B/C + Phase 3 + Phase 5 Part 1 to `develop`:
`8a561f94`, `254bf2a3`, `2831202a`).

Scope: `determine_job_status` must let the agent's reported outcome (a completion,
or a re-dispatchable freeze) win over a coincident infrastructure/teardown error,
for subjobs and top-level jobs alike; and the jobs-repo clone must survive a
transient blip.

---

## 1. Covered

### 1.1 Unit tests (run in CI; pure-function / mocked)

| Area | File / selector | What it asserts |
|---|---|---|
| Slice C — completion + teardown error | `tests/test_drain_intent.py::TestCoincidentInfraErrorOverride::test_completion_with_teardown_error_completes` | `job_completed` + verbatim SSH-timeout error → `completed`, not `failed` |
| Slice C — loop variant | `::test_loop_completion_with_teardown_error_completes` | a loop job completes (advances) rather than counting a phantom failure |
| Slice C — control (real crash) | `::test_completion_with_non_teardown_error_still_fails` | a genuine `AssertionError` on a completion report still → `failed` |
| Slice C — mid-run guard | `::test_teardown_error_without_completion_still_fails` | a teardown timeout with **no** completion declared still → `failed` |
| Teardown classifier | `::test_classifier_matches_incident_strings`, `::test_classifier_rejects_genuine_errors_and_empty` | the four incident strings match; ordinary errors / `None` / `""` don't |
| Slice B — drained subjob, live parent | `::test_drained_subjob_pauses_for_redispatch` | `version_upgrade` freeze + no `fd.status` + live parent → `paused` |
| Slice B — dead parent | `::test_drained_subjob_under_failed_parent_resolves_terminal` | parent `failed` → `cancelled` (avoid the cascade-guard wedge) |
| Slice B — paused parent is temporary | `::test_drained_subjob_under_paused_parent_still_pauses` | parent `paused` → still `paused` (re-dispatches once parent resumes) |
| Slice B — regression guards | `::test_subjob_non_drain_stop_unchanged`, `::test_critic_status_routing_unchanged` | non-drain subjob stop still `pending_review`; critic `fd.status` routing unchanged |
| Phase 3 — idempotency | `::test_already_completed_job_not_downgraded_by_late_error`, `::test_already_merged_job_not_downgraded_by_late_error`, `::test_processing_job_with_error_still_fails`, `::test_first_completion_on_processing_row_unaffected` | a `completed`/`merged` row is never downgraded to `failed` by a late error; a `processing` row still fails; the first completion is unaffected |
| Slice A regression (pre-existing) | `tests/test_drain_intent.py::TestVersionUpgradeFreeze` | top-level `version_upgrade` + coincident error still → `paused` (unchanged) |
| Clone retry — exhaustion (F29) | `tests/test_workspace_git.py::...::test_project_jobs_clone_failure_raises_not_silent_init` | retries `_CLONE_ATTEMPTS` times, then RAISES (no silent `git init`) |
| Clone retry — transient recovery | `::test_project_jobs_clone_succeeds_after_transient_failure` | fail-twice-then-succeed → returns the manager; 3 clone calls |

### 1.2 k3d endpoint drill (cluster `srw`, ctx `k3d-srw`, ns `srw`) — 2026-07-12

Real `POST /api/jobs/{id}/complete` against the live orchestrator + Postgres, with
the uncommitted code confirmed Tilt-synced into the pod. Disposable jobs, all HTTP
200. Script: `scratchpad/k3d_drill.sh` (re-runnable; self-cleans).

- **C1** completion (`job_completed`) + verbatim SSH-timeout teardown error →
  `completed` (the e15fab1f fix, end-to-end).
- **C2** completion + a real `AssertionError` → `failed` (carve-out stays narrow).
- **P3** already-`completed` row + late teardown error → stays `completed`.
- **B1** drained subjob (`version_upgrade`) under a live parent → `paused`, and its
  `freeze_data` shed to NULL (the da9d5917 fix).
- **B2** drained subjob under a `failed` parent → `cancelled`.
- **Dispatchability** — a paused subjob (freeze NULL, agent NULL) under a
  `processing` parent satisfies the `get_dispatchable_jobs` predicate
  (`base_ok ∧ cascade_ok`): it genuinely re-dispatches, not a silent wedge.

---

## 2. Not covered yet (deferred, non-blocking)

### 2.1 Full live drain-mid-subjob → re-dispatch → resume

**Gap.** The endpoint drill proves the resolver *decides* `paused` for a drained
subjob and that the row is *dispatchable*, but not the full live loop: drain a
real busy subjob agent at a phase boundary → orchestrator pauses it → the
dispatcher re-picks it onto a fresh-version pod → it resumes from its checkpoint
and finishes (no re-freeze livelock).

**Why deferred.** The *decision* is proven (§1.2 B1) and the re-dispatch/resume
*mechanism* (dispatcher re-pick, freeze-shed, resume-clear, resolve-at-dispatch)
is pre-existing code this change does not touch — it's covered by
`tests/test_drain_intent.py::TestAutoContinueResumeClear` and
`knowledge-base/knowledge/issues/version_upgrade_drain_livelock.md`. Reproducing a live drain-mid-
subjob is heavy and flaky for little marginal signal.

**How to close.** On k3d: create a session/job that spawns a scholar research
subjob; while it's mid-phase, set `intents.should_drain` on its agent (or bump the
agent image to force a drift-drain); assert the subjob row goes
`processing → paused → processing` on a new pod and reaches a terminal status with
its research grafted. Drill recipe skeleton is in `scratchpad/k3d_drill.sh`.

### 2.2 Live VM-teardown race → completed

**Gap.** §1.2 C1 injects the teardown error via the `/complete` payload; it does
not reproduce the real race (VM reaped on completion, a trailing SSH/IO op times
out against the gone VM).

**Why deferred.** The masking is a `determine_job_status` decision, now proven; the
race only *produces* the error, and the resolver makes it harmless regardless of
which trailing op raised it.

**How to close.** Run a real VM-backed loop job to completion on k3d and confirm
the row ends `completed` (not `failed`) even when the agent log shows a
post-completion workspace timeout. Naturally exercised by the "live-observe the
loop after rollout" step.

### 2.3 Phase 5 Part 2 — no workspace op after VM delete

**Gap + why deferred.** Part 2 (sequence teardown so no workspace op is issued once
the VM is scheduled for reaping) is **not built** — Slices B/C already make the
trailing error *harmless*, so it's now cosmetic (log noise), and the exact
trailing-op caller is unpinned. See `knowledge-history/done/coincident_infra_error_overrides_
reported_job_outcome.md` → "The infra layer §1".

**How to close.** Pin the caller (orchestrator finalization diff/archive read, or a
teardown handler), guard it against a reaped VM, then add a unit test that the
finalization path short-circuits when `context.vm.status ∈ {deleting, deleted}`.
Reconcile with `knowledge-base/knowledge/issues/agent_fast_freeze_on_dead_workspace.md`.

### 2.4 Slice B — outage freezes (`memory/kb/llm_unavailable`) on subjobs

**Gap + why deferred.** The subjob re-dispatch path is scoped to `version_upgrade`
(the actual incident). Outage freezes on a subjob still fall to the visible
`pending_review` fallback, because their retry-cap/ceiling counters are
top-level-scoped and would misbehave if routed through the subjob path.

**How to close.** Wire per-subjob outage counters, then extend the subjob branch to
the full `AUTO_REDISPATCH_FREEZE_TYPES` set and add the matching unit tests
(mirrors `TestCoincidentInfraErrorOverride` Slice B cases per freeze type).
