---
tags:
  - issue
  - workspace-lifecycle
  - git
  - gitea
  - fix-committed
---

# Slow-but-healthy jobs-repo clone abandoned at the 120s shell wait cap — retries collide with the busy tab, error blames "reachability"


**Closed by the 2026-08-06 doc-truth sweep (batch #3):** Shipped `6d6a1698`+`39def488` — clone wait-and-verify + Gitea CPU headroom; tests/test_managers_git.py 134/134 green.

**Status:** fix implemented on `develop` 2026-07-19 (clone wait-and-verify in
`GitManager.clone` + Gitea resource bump in `helm/values.yaml`).
**Severity:** high and growing — once a project's jobs repo crosses the size
cloneable inside the wait window, **every fresh dispatch for that project
fails deterministically** at startup.
**Component:** `src/managers/git_manager.py` (`GitManager.clone`),
`src/core/workspace.py` (`_clone_repo_with_retry` interplay),
`helm/templates/services/gitea.yaml` (resources).

## Symptom

Identical error text to the pre-seeded-root collision bug
(`jobs_repo_clone_collision_on_first_dispatch_to_populated_workspace.md`):

```
Failed to clone project jobs repo 'project-…-jobs' — refusing to fall back
to a disconnected git init (work would be lost on teardown). Check jobs-repo
URL reachability from this backend.
```

Distinguish by log signature: this mode shows `Receiving objects: N%`
progress in the "git clone failed" warnings; the collision mode shows
`destination path … already exists and is not an empty directory`.

Confirmed victim: `65ba6be8` (critic, project-68137e29 Hotel Rheinland ERP,
2026-07-18 19:36, audit=0) — failed **after** the collision fix
(`47c65582`/sha-89ef4a9) was live, proving the distinct root cause.

## Root cause chain

1. The project jobs repo grew to 29,378 objects (~35–40 MiB). The loop
   compounds output into it every iteration, so size only goes up.
2. Chart-default Gitea resources (250m CPU limit) throttle server-side pack
   generation to ~1–2.5 MiB/s → the clone needs several minutes.
3. `GitManager.clone` ran `backend.shell_run(cmd, timeout=120, tab_name="git")`.
   At 120s the shell returns `Exit code: -1 / --- still running ---` — by its
   own wording "not an error" — but `clone()` only accepted `Exit code: 0`
   and reported failure while the transfer sat at 33%.
4. `_clone_repo_with_retry` re-sent the clone into the still-busy tab after
   2s/5s; both retries bounced off the colliding-command guard ("your new
   command was NOT executed") and were counted as attempts 2/3 and 3/3.
5. F29 hard-fail raised the misleading reachability error; teardown killed
   the clone at 60%.

## The fix

`GitManager.clone` (backend path):

- Single-call wait raised 120s → 600s (`HARD_TIMEOUT_CAP_SECONDS` — the most
  one `shell_run` can wait anyway).
- A "still running" or "tab busy" result now enters `_wait_for_remote_clone`:
  poll the tab (10s interval, 1800s overall deadline) with a
  `git -C <target> rev-parse --git-dir` probe. While the clone runs, the busy
  tab rejects the probe (that's the "still busy" signal); once the tab frees,
  the probe executes and its answer — is there a git repo at the target — is
  ground truth for success, regardless of who started the clone. This also
  makes a retry that lands mid-clone wait for and adopt the in-flight clone
  instead of insta-failing.
- Genuine fast failures (auth, 404) still return None immediately and retry
  as before.

Tests: `tests/test_managers_git.py::TestGitManagerBackendCloneWaitsForCompletion`
(production sequence repro, busy-on-entry adoption, failed-clone-after-wait,
deadline bound).

Capacity: `helm/values.yaml` Gitea limits bumped 250m/512Mi → 2 CPU/1Gi
(requests 100m/256Mi) so pack generation isn't the bottleneck.

## Remaining work

- [ ] Deploy to dev; re-dispatch `65ba6be8` and confirm the clone completes.
- [ ] Jobs-repo growth is unaddressed (curator bloat): consider
      `--filter=blob:none` partial clone and/or history curation if repos
      keep growing past what even a healthy Gitea serves quickly.

## Related

- `docs/issues/jobs_repo_clone_collision_on_first_dispatch_to_populated_workspace.md`
  — the other failure mode behind the same error text.
- Memory/topic: `srw-jobs-repo-clone-120s-timeout-mode`.
