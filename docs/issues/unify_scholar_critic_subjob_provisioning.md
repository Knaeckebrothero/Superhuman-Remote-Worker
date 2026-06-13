# Route scholar/critic subjob provisioning through the shared helper

**Status**: Backlog — deferred refactor (touches completion flow). Filed 2026-06-13.

## Context

Gitea/workspace provisioning used to be open-coded inside the `POST /api/jobs`
handler. Automation-spawned jobs called `db.create_job()` directly and skipped
it, so they never got a repo (the student-facing 404 bug). The fix extracted the
logic into `orchestrator/services/job_provisioning.py::provision_job_repo(...)`
and called it from the handler + both automation paths (cron dispatcher +
run-now).

Two **other** direct `postgres_db.create_job(...)` callers were intentionally
left out of that change and still carry their own copy of the subjob
branch-creation logic:
- scholar sub-job spawn — `orchestrator/main.py` ~line 7192
- critic / verification sub-job spawn — `orchestrator/main.py` ~line 7902

They are NOT broken by the original bug (automation jobs are root/project jobs,
never scholar/critic subjobs), which is why they were deferred. But they are a
third and fourth copy of nearly-identical branch logic and they have drifted.

## Problem — duplication + drift

Compared to the handler's subjob branch (now `provision_job_repo`), the
scholar/critic copies diverge:

1. **Legacy repo-name fallback differs.** `provision_job_repo` (ex-handler,
   `main.py:4504`) uses `f"job-{str(parent['id'])}"` (full UUID); the
   scholar/critic copies use `f"job-{str(job['id'])[:8]}"` (8-char). One of
   these is wrong; they should agree.
2. **They skip the creator access grant** that `provision_job_repo` performs
   (and that now passes username/full_name/sub for pre-provisioning).
3. **They do extra subjob-only work** `provision_job_repo` does not: set
   `worktree_path` from the inherited VM/container backend, and write the job
   context with a full `update_job_context(...)` rather than a narrow
   `merge_job_context({git_remote_url})`.

So a straight drop-in of `provision_job_repo` would lose (3) and change (1)/(2)
behavior in a completion-critical path.

## Proposal

Reconcile and unify, deliberately and with tests:
- Extend `provision_job_repo` to accept an optional `worktree_path` (and, if
  needed, a "write full context" toggle) so the subjob callers' extra concerns
  are expressible.
- Reconcile the legacy fallback repo-name discrepancy (1) to a single
  convention; add a regression test pinning it.
- Decide whether subjobs should get the creator grant (2) — almost certainly
  yes, for consistency — and fold it in.
- Replace the inline blocks at `main.py` ~7192 and ~7902 with calls to the
  extended helper.

## Why deferred (not urgent)

- These paths work today; this is dedup + consistency, not a live bug.
- They sit in the scholar-spawn / verification-loop completion flow, which is
  high-blast-radius. Worth its own PR with focused tests rather than riding
  along with the automation parity fix.

## Acceptance

- Scholar and critic subjobs create their branch via `provision_job_repo` (one
  code path for all subjob branch creation).
- Legacy fallback repo-name convention is single-sourced and tested.
- No behavior regression in subjob `worktree_path` / context writes.
- Relates to `orchestrator_main_py_monolith.md` (shrinking `main.py`).
