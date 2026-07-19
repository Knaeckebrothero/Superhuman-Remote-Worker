---
tags:
  - issue
  - workspace-lifecycle
  - git
  - scholar
  - fix-committed
---

# First dispatch into a scholar-pre-seeded workspace pod collides on the jobs-repo clone — error text falsely blames "jobs-repo URL reachability"

**Status:** **FIX COMMITTED 2026-07-18 as `47c65582` on `develop`, NOT YET
PUSHED** (dev cluster still runs broken code at time of filing). Owed:
k3d/dev e2e of the scholar-on-container flow, then push → Fleet rollout →
re-dispatch victims.
**Severity:** high while unpushed — kills project jobs whose research
scholar pre-provisioned the shared workspace pod, and destroys the
scholar's research output with the torn-down workspace.
**Component:** `src/core/workspace.py`
(`initialize_project_workspace` / `_clone_repo_with_retry`, fix in
`_attach_existing_jobs_repo`), `agent.py` `_setup_job_workspace`.

## Symptom

Job fails at startup with:

```
Failed to clone project jobs repo 'project-…-jobs' — refusing to fall back
to a disconnected git init (work would be lost on teardown). Check jobs-repo
URL reachability from this backend.
```

**Reachability is a red herring.** Actual git stdout:
`fatal: destination path '/home/agent-host/workspace' already exists and is
not an empty directory`.

Victims (all audit=0, same error): `0de9d7d2` (designer, Hotel Rheinland
ERP themes, project-68137e29, 2026-07-17 22:58), `ab8680c5` (designer,
2026-07-14), `42bbf782` (critic, project-1feeb7b8, 07-10), `ffb906db`
(product-qa, 07-09).

## Root cause chain

1. Scholar-first flow on the container backend: the pre-job research
   scholar **provisions the parent's ONE shared workspace pod under the
   parent's identity** and initializes it (jobs-repo clone as root +
   research output).
2. Scholar completion resets the parent to `status='created'` → dispatcher
   uses the **first-dispatch path → `resume=False`**.
3. All three populated-workspace protections in `agent.py
   _setup_job_workspace` (G2 reattach, pod handoff, resume-existing) are
   **gated on `resume=True`** → skipped → falls into
   `initialize_project_workspace()` → clone into populated root → exit 128.
4. `_clone_repo_with_retry` retries the non-retryable "not empty" error 3×,
   then hard-fails with the misleading reachability message. Workspace is
   snapshotted + deleted; the scholar's research is lost with it.

VM-backed parents don't collide (scholar self-provisions there) — the loop
only bleeds when jobs land on containers, e.g. during the headscale latch
outage (`vm_controller_headscale_latch_kills_provisioning.md`).

## The fix (committed `47c65582`)

`WorkspaceManager._attach_existing_jobs_repo`: before cloning, probe the
root — an existing `.git` whose origin matches the jobs-repo URL
(credential-stripped compare via `_normalize_repo_url`) → attach + refresh
origin creds + reuse; mismatched origin or non-empty-without-git →
immediate RuntimeError with accurate wording (no clone retries, no
reachability red herring). Plus `GitManager.remote_url()` accessor. Tests in
`tests/test_workspace_git.py::TestProjectWorkspacePrepopulatedRoot`.

## Remaining work

- [ ] k3d or dev e2e of scholar-on-container → parent redispatch flow
- [ ] push `47c65582` → Fleet rollout
- [ ] re-dispatch/resume the victims (at minimum `0de9d7d2`)

## Related

- Memory/topic: `scholar-shared-workspace-clone-collision`.
- `docs/issues/scholar_selfprovisioned_workspace_misclassified_as_inherited.md`
  — the same scholar-provisions-parent flow tripping a different guard.
- `docs/issues/jobs_repo_clone_timeout_abandons_healthy_transfer.md` — a
  SECOND failure mode behind the same error text (healthy clone abandoned at
  the shell wait cap); job `65ba6be8` hit it after this fix was live.
